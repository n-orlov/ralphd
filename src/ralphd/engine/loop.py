"""Loop supervisor: planning → worker → review, approaches, budgets."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

from .config import PROMPTS_BUILTIN, JobConfig, overlay_or_config
from .faults import classify_fault
from .llm import current_env
from .runner import IterationResult, PiRunner
from .state import RunDir, atomic_write_json, utcnow

log = logging.getLogger("ralphd.loop")


# Phases whose prompt (worker.md / planning.md) explicitly instructs the
# agent to act on operator steering. "review" and "verify" are pure
# verification roles ("Do NOT fix anything yourself") with no steering
# instructions in their prompts, so steering arriving just before one of
# those iterations must NOT be marked consumed there -- it would otherwise
# be silently discarded (recorded as "consumed" but never actioned). It
# stays pending until the next planning/worker iteration picks it up.
STEERING_ACTIONABLE_PHASES = {"planning", "worker"}


class LoopSupervisor:
    def __init__(self, cfg: JobConfig, run: RunDir, workspace: Path):
        self.cfg = cfg
        self.run = run
        self.workspace = workspace
        self.runner = PiRunner(workspace)
        # Seed from any already-completed iterations on disk (0 for a
        # fresh run dir) so a restarted engine numbers its next iteration
        # N+1 instead of reusing/duplicating past numbers (PRD req 16).
        self.iterations_used = run.max_iteration_number()
        self._pause = asyncio.Event()
        self._pause.set()  # set = not paused
        self._abort_reason: str | None = None
        self._instant_failure_streak = 0
        # Task 001a: iterations refunded because they were retried after an
        # infra-classified fault (see _run_iteration_with_infra_retry) --
        # subtracted from self.iterations_used when checking budget_left()
        # so a hung/broken-endpoint retry never costs the job an iteration,
        # while self.iterations_used itself keeps monotonically increasing
        # (so every attempt still gets its own iteration directory/number).
        self._infra_refunded = 0
        # Task 002: at most one grace review per approach (set of approach
        # numbers that have already had one), and a matching refund counter
        # (same mechanism as _infra_refunded) so a grace review never counts
        # against the job's iteration budget.
        self._grace_review_granted: set[int] = set()
        self._grace_refunded = 0
        # Descriptive note for the terminal `reason` when a grace review ran
        # but did NOT result in a VERIFIED verdict -- kept separate from
        # self._abort_reason so the terminal `state` (aborted vs failed)
        # keeps its existing meaning (aborted == an actual abort/infra
        # condition set _abort_reason); this is purely informational text.
        self._terminal_reason_note: str | None = None
        self._last_task_snapshot: dict = {}
        self.deadline = time.monotonic() + cfg.job_timeout_s

    # -- control surface (called from API) --------------------------------
    def interrupt(self) -> bool:
        return self.runner.interrupt()

    def pause(self) -> None:
        self._pause.clear()
        self.run.emit("log", message="paused at next iteration boundary")

    def resume(self) -> None:
        self._pause.set()
        self.run.emit("log", message="resumed")

    def abort(self, reason: str = "") -> None:
        self._abort_reason = reason or "aborted by operator"
        self.runner.interrupt()

    # -- prompts -----------------------------------------------------------
    def prompt_text(self, name: str) -> str:
        # Preference order: runtime overlay (API PUT, writable) > mounted
        # /config (operator-provided, read-only in real containers) > builtin.
        path = overlay_or_config(f"prompts/{name}.md")
        if not path.exists():
            path = PROMPTS_BUILTIN / f"{name}.md"
        return path.read_text()

    def build_prompt(self, phase: str, extra: str = "",
                      prompt_name: str | None = None) -> str:
        prd = self.run.composite_prd_file if self.run.composite_prd_file.exists() \
            else self.run.prd_file
        parts = [self.prompt_text(prompt_name or phase)]
        parts.append("\n\n## Job context\n")
        parts.append(f"- Run state directory: {self.run.root}\n")
        parts.append(self._workspace_note())
        parts.append(f"- PRD file: {prd}\n"
                     f"- Task state file: {self.run.tasks_file}\n"
                     f"- Handoff notes file: {self.run.notes_file}\n"
                     f"- Artifacts directory: {self.run.artifacts_dir}\n")
        docker_note = self._docker_siblings_note()
        if docker_note:
            parts.append(docker_note)
        creds_note = self._creds_note()
        if creds_note:
            parts.append(creds_note)
        pending = self.run.pending_steering()
        if pending:
            if phase in STEERING_ACTIONABLE_PHASES:
                parts.append("\n## Operator steering (MUST take priority)\n")
                for p in pending:
                    parts.append(f"\n### {p.name}\n{p.read_text()}\n")
            else:
                # Non-actionable phase (review/verify): surface as read-only
                # context only -- this phase must not consume it (see
                # STEERING_ACTIONABLE_PHASES), so it stays pending for the
                # next planning/worker iteration to actually act on.
                names = ", ".join(p.name for p in pending)
                parts.append(
                    "\n## Operator steering (pending, not for this phase)\n"
                    f"Steering file(s) {names} are waiting for the operator's "
                    "instructions to be actioned by the next planning/worker "
                    "iteration. This phase does not consume or act on them.\n")
        if extra:
            parts.append("\n" + extra)
        return "".join(parts)

    def _workspace_note(self) -> str:
        """Single-workspace jobs (the common case) get the original one-line
        form. Multi-workspace jobs (repeatable `ralphctl start --workspace
        DIR:NAME`, PRD req 27) mount each repo at /workspace/<name> and set
        RALPHD_WORKSPACES=<comma-separated names> on the container; list
        each mounted name/path so the agent knows what's there without
        having to guess from a directory listing."""
        names_env = os.environ.get("RALPHD_WORKSPACES", "")
        names = [n for n in names_env.split(",") if n]
        if not names:
            return f"- Workspace (code) directory: {self.workspace}\n"
        lines = [(f"- Workspaces (code directories), {len(names)} mounted "
                  f"under {self.workspace}:\n")]
        for name in names:
            lines.append(f"  - {name}: {self.workspace / name}\n")
        return "".join(lines)

    @staticmethod
    def _creds_note() -> str:
        """List the credential *.env file names currently placed at
        ~/.creds (never values) plus the sourcing rule, so every phase
        prompt knows what's available without any secret ever appearing
        in the prompt text (PRD req 7). Read fresh each call so runtime
        creds CRUD (PUT /config/creds/{name}) is reflected next iteration.
        """
        home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
        creds_dir = home / ".creds"
        if not creds_dir.is_dir():
            return ""
        names = sorted(p.name for p in creds_dir.glob("*.env"))
        if not names:
            return ""
        lines = ["\n## Credentials\n",
                 "Available credential files (values withheld from this prompt):\n"]
        for name in names:
            lines.append(f"- `~/.creds/{name}`\n")
        lines.append(
            "\nSource only the file(s) you need, in the shell command where "
            "you need them: `set -a; . ~/.creds/<name>.env; set +a`. Values "
            "are never auto-exported into your environment.\n"
            "\n**Never print, `cat`, `echo`, or otherwise dump a credential "
            "file's contents, and never paste a secret value into a command's "
            "arguments** (e.g. as a URL query string, a `--token` flag, or an "
            "inline `curl -H \"Authorization: Bearer <value>\"`). Every tool "
            "call's arguments and stdout are recorded verbatim in this run's "
            "iteration transcript (host-visible, permanent) -- doing either "
            "one permanently persists the secret outside the credential file "
            "itself. Refer to credentials only as `$VARNAME` after sourcing "
            "the file with `set -a; . ~/.creds/<name>.env; set +a`; let the "
            "tool (git, curl, etc.) read the variable from its own "
            "environment rather than echoing it. This also forbids "
            "token-bearing git remote URLs (e.g. `https://<token>@host/...`) "
            "-- configure the credential helper/`~/.git-credentials` instead "
            "so the token never appears in a `git remote`/`git clone` "
            "argument or in `git remote -v` output.\n")
        return "".join(lines)

    @staticmethod
    def _docker_siblings_note() -> str:
        """Guidance appended when the operator granted docker socket access
        (ralphctl start --allow-docker sets the RALPHD_HOST_* env vars)."""
        host_ws = os.environ.get("RALPHD_HOST_WORKSPACE")
        host_wss = os.environ.get("RALPHD_HOST_WORKSPACES")
        host_run = os.environ.get("RALPHD_HOST_RUN_DIR")
        if not host_ws and not host_wss and not host_run:
            return ""
        run_id = os.environ.get("RALPHD_RUN_ID", "")
        lines = ["\n## Docker siblings\n",
                 ("The host docker socket is mounted: `docker` starts SIBLING "
                  "containers on the HOST daemon, not children of this container.\n"),
                 ("- `-v` paths for siblings are HOST paths. This container's "
                  "paths (/workspace, /run/ralphd) are meaningless to the host "
                  "daemon — mounting them yields EMPTY dirs and can create "
                  "root-owned dirs on the host. Use these host-side equivalents:\n")]
        if host_ws:
            lines.append(f"  - workspace: `$RALPHD_HOST_WORKSPACE` = `{host_ws}`\n")
        elif host_wss:
            for name, path in json.loads(host_wss).items():
                lines.append(f"  - workspace `{name}` (mounted at /workspace/{name} "
                             f"in this container): host path `{path}`\n")
        if host_run:
            lines.append(f"  - run dir: `$RALPHD_HOST_RUN_DIR` = `{host_run}`\n")
        ws_mount = ("$RALPHD_HOST_WORKSPACE" if host_ws
                    else "<host workspace path from above>")
        lines.append(
            f"- Label every sibling `--label ralphd.run=$RALPHD_RUN_ID` "
            f"(= `{run_id}`) so it gets reaped with this job; prefer `--rm` "
            "for anything short-lived.\n"
            "- Images you build and volumes you create live on the HOST and "
            "outlive this job (`ralphctl stop`/`rm` reap labeled *containers* "
            "only) — label them too, and delete any you did not mean to keep.\n"
            "- **Need a toolchain this image lacks?** Run it in a sibling "
            "instead of trying to install it: this image is deliberately thin "
            "(Python/Node) and `agent` cannot `apt-get`. Proven working this "
            "way: Go build/test, real `tmux` (private `-L` socket + "
            "`capture-pane`), and pty-driven TUI tests.\n"
            "  - Commit a small `ci/Dockerfile` (base image + just that "
            "toolchain) and a `ci/run.sh` wrapper in the target repo, so the "
            "setup is reproducible without you, then build it here: "
            "`docker build -t <repo>-ci --label ralphd.run=$RALPHD_RUN_ID ci/`\n"
            "  - Run each command in a throwaway sibling: `docker run --rm "
            "--user 1000:1000 --label ralphd.run=$RALPHD_RUN_ID "
            f"-v {ws_mount}:/workspace -w /workspace <repo>-ci <cmd>`\n"
            "  - `--user 1000:1000` is mandatory (this container and the host "
            "user are both uid 1000): a root sibling leaves root-owned files "
            "in the workspace that you can then neither edit nor delete.\n"
            "  - Mount a **named volume** for the toolchain's caches and point "
            "the cache env vars at it (`-v <vol>:/cache -e HOME=/tmp -e "
            "GOMODCACHE=/cache/gomod -e GOCACHE=/cache/gobuild`), or every "
            "run re-downloads its dependencies. Name it after the "
            "repo+toolchain (`<repo>-gocache`) so runs and later jobs share "
            "it, and leave the run label off it; a per-run volume is fine "
            "only if you `docker volume rm` it before finishing. Never make a "
            "shared volume's use conditional on `$RALPHD_RUN_ID` — that "
            "breaks the next run.\n"
            "  - Siblings run on docker's default bridge network with normal "
            "internet (image pulls, dependency downloads) whatever network "
            "this container is on, and need no LLM/gateway access — do not "
            "pass any. Workspace writes are visible both ways immediately.\n")
        return "".join(lines)

    # -- iteration ----------------------------------------------------------
    # Phases protected by the infra-fault retry-with-backoff wrapper (task
    # 001a): planning and worker are the two phases the real incident hit
    # (an LLM-endpoint DNS/gateway glitch can strike either), and are the
    # same two phases task 059's instant-failure carve-out already covers.
    # review/verify/reflect keep their own existing, phase-specific retry
    # logic (steering-aware review loop, MAX_VERIFY_ERROR_RETRIES) and are
    # deliberately left out here to avoid two retry mechanisms colliding.
    INFRA_RETRY_PHASES = ("planning", "worker")

    async def run_iteration(self, phase: str, extra: str = "",
                             prompt_name: str | None = None):
        if phase not in self.INFRA_RETRY_PHASES:
            return await self._run_iteration_once(phase, extra, prompt_name)
        return await self._run_iteration_with_infra_retry(phase, extra, prompt_name)

    # Task 001a: escalating backoff/retry cap defaults live on JobConfig
    # (cfg.infra_retry_backoff_s / cfg.infra_retry_max), overridable via
    # RALPHD_INFRA_RETRY_BACKOFF_S / RALPHD_INFRA_RETRY_MAX so tests (and
    # operators) don't need a job.yaml edit for every run.
    async def _run_iteration_with_infra_retry(self, phase: str, extra: str,
                                              prompt_name: str | None):
        """Runs `phase` via _run_iteration_once(), retrying THE SAME
        phase/iteration with escalating backoff whenever the result
        classifies as an infra fault (broken LLM endpoint/provider/network
        -- see .faults.classify_fault) rather than a genuine work failure.

        Each infra-classified attempt is refunded (never counted against
        cfg.iterations, see budget_left()) and does NOT touch
        self._instant_failure_streak / the no-progress stagnation guard --
        callers (the planning/worker loops in _run_job_core) see a fully
        resolved IterationResult exactly as before, either a genuine
        success/work-failure or (once cfg.infra_retry_max is exhausted) the
        last failing attempt with self._abort_reason already set to a
        diagnostic naming the infra fault plainly.

        An infra fault that is ALSO an *instant* failure (sub-
        INSTANT_FAILURE_MAX_DURATION_S exit with no traffic) is left
        entirely to the pre-existing streak-based carve-out
        (_check_instant_failure) instead -- returning immediately here --
        so the two mechanisms never race or double-count the same failure.
        """
        attempt = 0
        while True:
            result = await self._run_iteration_once(phase, extra, prompt_name)
            fault = classify_fault(
                error_text=result.error_message or "",
                exit_code=result.exit_code,
                interrupted=result.interrupted,
                timed_out=result.timed_out,
                no_traffic_timeout=result.no_traffic_timeout,
                produced_traffic=bool(result.final_text) or bool(result.usage))
            is_instant = (result.duration_s is not None
                         and result.duration_s < self.INSTANT_FAILURE_MAX_DURATION_S
                         and not result.no_traffic_timeout)
            if fault != "infra" or is_instant:
                return result

            self._infra_refunded += 1
            attempt += 1
            error_desc = result.error_message or "no LLM traffic within startup window"
            self.run.emit("infra_retry", phase=phase, attempt=attempt,
                          maxAttempts=self.cfg.infra_retry_max, error=error_desc,
                          noTrafficTimeout=result.no_traffic_timeout)
            log.warning("iteration %s classified infra fault (attempt %d/%d): %s",
                       phase, attempt, self.cfg.infra_retry_max, error_desc)
            self.run.update_status(
                iterationsUsed=self.iterations_used - self._infra_refunded)
            if attempt >= self.cfg.infra_retry_max:
                self._abort_reason = (
                    f"infra fault: {phase} iteration failed after "
                    f"{self.cfg.infra_retry_max} attempts ({error_desc})")
                self.run.emit("log", level="error", message=self._abort_reason)
                return result
            backoff_schedule = self.cfg.infra_retry_backoff_s or [60.0, 300.0, 900.0]
            backoff = backoff_schedule[min(attempt - 1, len(backoff_schedule) - 1)]
            self.run.update_status(currentIteration={
                "phase": phase,
                "note": (f"retrying after infra fault (attempt {attempt}/"
                         f"{self.cfg.infra_retry_max}, next in {backoff:.0f}s): "
                         f"{error_desc}")})
            await asyncio.sleep(backoff)

    async def _run_iteration_once(self, phase: str, extra: str = "",
                                  prompt_name: str | None = None):
        n = self.iterations_used + 1
        self.iterations_used = n
        itdir = self.run.iteration_dir(n)
        model = self.cfg.model_for(phase)
        pending = (self.run.pending_steering()
                   if phase in STEERING_ACTIONABLE_PHASES else [])
        prompt = self.build_prompt(phase, extra, prompt_name=prompt_name)
        (itdir / "prompt.md").write_text(prompt)

        meta = {"number": n, "phase": phase, "model": model,
                "approach": self.run.read_status().get("approach"),
                "startedAt": utcnow(),
                "steeringConsumed": [p.name for p in pending]}
        atomic_write_json(itdir / "meta.json", meta)
        self.run.emit("iteration.start", number=n, phase=phase, model=model)
        log.info("iteration %d start: phase=%s model=%s", n, phase, model)
        self.run.update_status(phase=phase, iteration=n,
                               iterationsUsed=n,
                               currentIteration={"number": n, "phase": phase,
                                                 "model": model,
                                                 "startedAt": meta["startedAt"]})
        if pending:
            self.run.consume_steering(pending, n)

        timeout = min(self.cfg.iteration_timeout_s,
                      max(60, int(self.deadline - time.monotonic())))
        # Task 001a: the startup-window watchdog only applies to phases the
        # infra-retry wrapper protects (planning/worker) -- other phases
        # (review/verify/reflect) are unaffected, keeping their existing
        # timing/behavior exactly as before.
        startup_timeout_s = (min(self.cfg.infra_startup_timeout_s, timeout)
                             if phase in self.INFRA_RETRY_PHASES else None)
        # Poll tasks.json while the agent subprocess is running so status
        # transitions (e.g. pending -> in-progress) are emitted as "task"
        # events the moment the agent writes them, not only after the whole
        # iteration finishes (an operator watching events/ralphctl must see
        # the exact task being worked while it's still in flight).
        poll_task = asyncio.create_task(self._poll_task_changes())
        try:
            try:
                result = await self.runner.run(
                    prompt, itdir / "output.jsonl", model=model,
                    thinking=self.cfg.thinking, timeout_s=timeout,
                    extra_env=current_env(), startup_timeout_s=startup_timeout_s)
            except Exception as exc:
                # an engine-side iteration failure (stream error, OS error)
                # must cost one iteration, not the whole job
                result = IterationResult(exit_code=None)
                result.error_message = f"engine iteration failure: {exc!r}"
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task

        meta.update(endedAt=utcnow(), exitCode=result.exit_code,
                    interrupted=result.interrupted, timedOut=result.timed_out,
                    noTrafficTimeout=result.no_traffic_timeout,
                    sawComplete=result.saw_complete, sawVerified=result.saw_verified,
                    error=result.error_message or None,
                    usage=result.usage)
        atomic_write_json(itdir / "meta.json", meta)
        self._accumulate_usage(result.usage, phase=phase, approach=meta.get("approach"))
        self.run.emit("iteration.end", number=n, phase=phase,
                      exitCode=result.exit_code, interrupted=result.interrupted,
                      sawComplete=result.saw_complete, sawVerified=result.saw_verified,
                      error=result.error_message or None)
        log.info("iteration %d end: exit=%s complete=%s verified=%s%s",
                 n, result.exit_code, result.saw_complete, result.saw_verified,
                 f" ERROR: {result.error_message}" if result.error_message else "")
        if result.error_message:
            self.run.emit("log", level="error",
                          message=f"iteration {n} agent error: {result.error_message}")
        self._emit_task_changes()
        self.run.update_status(currentIteration=None)
        return result

    @staticmethod
    def _merge_usage(bucket: dict, usage: dict) -> dict:
        """Add `usage`'s counters into `bucket` (a per-phase/per-approach/
        overall usage dict) in place, returning it. Shared by every
        accumulation site so byPhase/byApproach sums always equal the
        overall total (PRD req 19)."""
        for k, v in usage.items():
            bucket[k] = round(bucket.get(k, 0) + v, 6) if isinstance(v, float) \
                else bucket.get(k, 0) + v
        return bucket

    def _accumulate_usage(self, usage: dict, phase: str | None = None,
                           approach: int | None = None) -> None:
        status = self.run.read_status()
        total = status.get("usage", {})
        by_phase = total.get("byPhase", {})
        by_approach = total.get("byApproach", {})
        self._merge_usage(total, usage)
        if phase:
            by_phase[phase] = self._merge_usage(by_phase.get(phase, {}), usage)
        if approach is not None:
            key = str(approach)
            by_approach[key] = self._merge_usage(by_approach.get(key, {}), usage)
        total["byPhase"] = by_phase
        total["byApproach"] = by_approach
        self.run.update_status(usage=total)

    def _emit_task_changes(self) -> None:
        tasks = {t["id"]: t.get("status") for t in self.run.read_tasks().get("tasks", [])}
        for tid, status in tasks.items():
            old = self._last_task_snapshot.get(tid)
            if old != status:
                self.run.emit("task", taskId=tid, oldStatus=old, newStatus=status)
        self._last_task_snapshot = tasks

    async def _poll_task_changes(self, interval: float = 0.25) -> None:
        """Background poller run alongside the agent subprocess: emits
        task status-transition events (e.g. pending -> in-progress) the
        moment tasks.json changes on disk, instead of only once the whole
        iteration has finished. Cancelled by run_iteration() as soon as the
        subprocess exits; tolerant of transient read/parse errors since the
        agent may be mid-write.
        """
        while True:
            try:
                self._emit_task_changes()
            except (OSError, ValueError):
                pass
            await asyncio.sleep(interval)

    # -- budget/limits -------------------------------------------------------
    def budget_left(self) -> bool:
        # Task 001a: iterations refunded after an infra-classified retry
        # (see _run_iteration_with_infra_retry) never count against the
        # configured budget.
        charged = self.iterations_used - self._infra_refunded - self._grace_refunded
        return (charged < self.cfg.iterations
                and time.monotonic() < self.deadline
                and self._abort_reason is None)

    async def _gate(self) -> None:
        await self._pause.wait()

    # -- main loop ------------------------------------------------------------
    def _resume_point(self) -> tuple[int, bool]:
        """Where run_job() should start on this engine invocation (PRD req
        16). A fresh run dir (no tasks.json yet, or no completed
        iterations recorded) starts at approach 1 with planning, exactly
        as before. A run dir that already has a completed planning
        iteration's tasks.json -- e.g. the engine restarted over a stale
        'running' status left by a killed prior process, or over a
        terminal failed/aborted run dir after the operator bumped the
        iteration budget -- resumes the existing approach's worker loop
        directly, skipping planning; iteration numbering continues from
        the existing max (already seeded into self.iterations_used in
        __init__).

        Returns (start_approach, skip_planning_for_start_approach).
        """
        if self.iterations_used == 0 or not self.run.read_tasks().get("tasks"):
            return 1, False
        approach = self.run.read_status().get("approach") or 1
        self.run.emit(
            "log",
            message=(
                f"resuming existing run-dir state: approach {approach}, "
                f"{self.iterations_used} iteration(s) already recorded; "
                "skipping planning"))
        return approach, True

    async def run_job(self) -> str:
        """Runs the job to a terminal state, then (if `reflect: true`) one
        extra 'reflect' iteration analyzing the run for prompt/skill
        improvements (PRD req 24). Returns final state: succeeded | failed |
        aborted -- unaffected by the reflect iteration, which runs strictly
        after the state below is already terminal."""
        state = await self._run_job_core()
        if self.cfg.reflect:
            await self._run_reflection()
        return state

    async def _run_reflection(self) -> None:
        """One extra 'reflect' iteration after the job has already reached a
        terminal state (PRD req 24). Runs unconditionally (not gated by
        budget_left(), which is normally already exhausted by the time a job
        reaches a terminal state) exactly once. The reflect prompt instructs
        the agent to write only under artifacts/reflection/ and touch
        nothing else; the engine's own bookkeeping restores status.json's
        `phase` field to None afterward so a terminal job never appears to
        still be "in phase reflect" once this returns."""
        self.run.emit("phase", phase="reflect")
        try:
            await self.run_iteration("reflect")
        finally:
            self.run.update_status(phase=None)

    async def _run_job_core(self) -> str:
        """Returns final state: succeeded | failed | aborted."""
        self.run.update_status(state="running", startedAt=utcnow(),
                               iterationsBudget=self.cfg.iterations,
                               maxApproaches=self.cfg.max_approaches,
                               onComplete=self.cfg.on_complete, verdict=None)
        start_approach, resuming = self._resume_point()
        try:
            for approach in range(start_approach, self.cfg.max_approaches + 1):
                self.run.update_status(approach=approach)
                if not self.budget_left():
                    # Task 002: covers the resume edge case where a prior
                    # process already completed every task for this
                    # approach but the process died/ran out of budget
                    # before any review ran at all.
                    if await self._maybe_grace_review(approach):
                        return "succeeded"
                    break
                skip_planning = resuming and approach == start_approach
                if not skip_planning:
                    self.run.emit("phase", phase="planning", approach=approach)

                    # Retry planning in place (same approach, task 059)
                    # for as long as it keeps hitting instant startup/infra
                    # failures -- an empty tasks.json produced by a crashed
                    # planning invocation is not a genuine "planning
                    # produced nothing useful" situation that should cost
                    # an approach; it's the same class of fault the worker
                    # loop's stagnation guard must not score either.
                    while True:
                        await self._gate()
                        presult = await self.run_iteration("planning")
                        if self._check_instant_failure(presult, self.iterations_used):
                            break  # abort_reason set; budget_left() now False
                        if self._instant_failure_streak and self.budget_left():
                            continue
                        break
                    if not self.budget_left():
                        break
                    if not self.run.read_tasks().get("tasks"):
                        self.run.emit("log", level="error",
                                      message="planning produced no tasks.json")
                        continue

                # Task 008: establish (or backfill, for a run resumed from
                # before this feature existed) a criteriaFingerprint
                # baseline for every task before the worker loop's first
                # before/after diff -- otherwise the fingerprint field
                # appearing for the first time inside that diff would look
                # like spurious task progress.
                self._ensure_criteria_baseline()

                # worker loop
                stagnant = 0
                verdict_ready = False
                while self.budget_left():
                    await self._gate()
                    tasks_before = self.run.read_tasks()
                    before = json.dumps(tasks_before, sort_keys=True)
                    result = await self.run_iteration("worker")
                    tasks_after = self.run.read_tasks()
                    self._warn_if_batched(tasks_before, tasks_after)

                    if self.cfg.vigilant:
                        # Do NOT gate this on a before/after diff scoped to
                        # this single run_iteration() call (task 052): if
                        # the engine crashed after a prior worker iteration
                        # completed a task but before (or during) its verify
                        # iteration, a resumed process's very first snapshot
                        # already shows that task as "completed", so a
                        # before/after diff taken *this* process would never
                        # see it as newly-completed again and its mandatory
                        # verification would be silently skipped forever.
                        # Instead, consult the engine-owned, disk-persisted
                        # verified-task record (survives crash/resume) --
                        # anything currently "completed" that isn't in it
                        # still needs a verify iteration, whether it just
                        # completed in this process or was left over from a
                        # killed prior one.
                        verified_ids = self.run.read_verified_tasks()
                        pending_verify = [
                            t for t in tasks_after.get("tasks", [])
                            if t.get("status") == "completed"
                            and t["id"] not in verified_ids
                        ]
                        for task in pending_verify:
                            if self.budget_left():
                                await self._gate()
                                await self._verify_task(task)
                        # Re-read after any verification-driven status updates
                        tasks_after = self.run.read_tasks()

                    # Task 008: a worker that rewrites a task's successCriteria
                    # after that task has already failed verification at
                    # least once is quietly moving the bar instead of doing
                    # the work -- flag it persistently so review (task 009)
                    # can demand independent re-verification of the new text.
                    self._check_criteria_edits(tasks_after)

                    if self._check_instant_failure(result, self.iterations_used):
                        break
                    if self._instant_failure_streak:
                        # An instant startup/infra failure that hasn't yet
                        # hit the abort threshold: must not count as either
                        # progress or no-progress evidence (task 059) --
                        # skip stagnation bookkeeping entirely this pass.
                        continue

                    after = json.dumps(tasks_after, sort_keys=True)
                    stagnant = stagnant + 1 if (before == after and not result.saw_complete
                                                and not result.interrupted) else 0
                    if stagnant >= 3:
                        self.run.emit("log", level="error",
                                      message="3 iterations with no task progress; "
                                              "failing approach")
                        break
                    if result.saw_complete:
                        verdict_ready = True
                        self.run.emit("signal", signal="COMPLETE")
                        break

                if not self.budget_left():
                    # Task 002: a job whose worker loop ran out of budget
                    # with every task already completed -- whether or not
                    # the worker happened to signal COMPLETE on that final
                    # iteration -- gets exactly one off-budget grace review
                    # rather than going terminal failed/unverified with all
                    # work done and no reviewer ever having looked at it.
                    if await self._maybe_grace_review(approach):
                        return "succeeded"
                    break
                if not verdict_ready:
                    self._archive_approach(approach)
                    continue

                # review
                await self._gate()
                self.run.emit("phase", phase="review", approach=approach)
                review = await self.run_iteration(
                    "review", extra=self._flagged_criteria_review_context())
                while review.saw_verified and self.run.pending_steering():
                    # Refuse to let a VERIFIED verdict make the run terminal
                    # while operator steering still sits unconsumed. review
                    # and verify are pure verification phases that never act
                    # on steering (STEERING_ACTIONABLE_PHASES); if steering
                    # lands just before what would otherwise be the final
                    # VERIFIED review, going terminal here would strand it
                    # forever -- nothing after a terminal-succeeded run ever
                    # reads pending steering again. Instead: discard this
                    # verdict, run one more (actionable) worker iteration so
                    # the steering actually gets consumed, then re-review.
                    names = ", ".join(p.name for p in self.run.pending_steering())
                    self.run.emit(
                        "log", level="warning",
                        message=(f"steering pending ({names}) at VERIFIED verdict; "
                                 "deferring, routing back to worker to consume it"))
                    if not self.budget_left():
                        break
                    await self._gate()
                    self.run.emit("phase", phase="worker", approach=approach)
                    await self.run_iteration("worker")
                    if not self.budget_left():
                        break
                    await self._gate()
                    self.run.emit("phase", phase="review", approach=approach)
                    review = await self.run_iteration(
                        "review", extra=self._flagged_criteria_review_context())
                if review.saw_verified and not self.run.pending_steering():
                    self.run.emit("signal", signal="VERIFIED")
                    self.run.update_status(state="succeeded", verdict="verified",
                                           phase=None, endedAt=utcnow(),
                                           **self._unconsumed_steering_patch())
                    return "succeeded"
                self.run.emit("log", message=f"review rejected approach {approach}")
                self._archive_approach(approach)
                self._write_composite_prd(approach)

            state = "aborted" if self._abort_reason else "failed"
            self.run.update_status(state=state, verdict="unverified", phase=None,
                                   endedAt=utcnow(),
                                   reason=self._abort_reason or self._terminal_reason_note,
                                   **self._unconsumed_steering_patch())
            return state
        except Exception as exc:  # engine bug — record, don't vanish
            self.run.emit("log", level="error", message=f"engine error: {exc!r}")
            self.run.update_status(state="failed", verdict="unverified",
                                   phase=None, endedAt=utcnow(),
                                   reason=f"engine error: {exc}",
                                   **self._unconsumed_steering_patch())
            return "failed"

    def _unconsumed_steering_patch(self) -> dict:
        """status.json patch (task 006) applied at every terminal-state write:
        names any steering files still unconsumed when the run went
        terminal, so a run that ends failed/aborted/succeeded with steering
        stranded (e.g. budget exhausted right after a rejected review that
        left a just-landed steering file pending, with no further
        actionable iteration to consume it) is loudly discoverable from
        status.json alone -- not just from combing steering/.consumed.json
        by hand. Empty list when nothing is stranded (the common case)."""
        return {"unconsumedSteering": [p.name for p in self.run.pending_steering()]}

    def _all_tasks_completed(self) -> bool:
        """True iff tasks.json exists, is non-empty, and every task's
        `status` is `completed`. Task 002's grace-review gate: an empty or
        missing tasks list (e.g. planning never ran/produced nothing) must
        never be treated as "all done"."""
        tasks = self.run.read_tasks().get("tasks") or []
        return bool(tasks) and all(t.get("status") == "completed" for t in tasks)

    async def _maybe_grace_review(self, approach: int) -> bool:
        """Task 002: invariant -- a job whose tasks are ALL completed by the
        time the iteration budget exhausts should still get a review
        verdict if at all possible, rather than going terminal
        failed/unverified with e.g. 7/7 tasks done and no reviewer ever
        having looked at it (the live incident this closes: the operator
        had to `ralphctl resume +3` just to get a review slot).

        Design choice (stated here and in docs/architecture.md): rather
        than reserving the final budget slot ahead of time (which would
        require predicting exhaustion before it happens), grant a single
        OFF-BUDGET review iteration at the moment budget is discovered to
        be exhausted, if and only if every task is already completed and
        this approach hasn't already had one. This is simpler to reason
        about (no speculative slot-reservation bookkeeping earlier in the
        loop) and bounded by construction: `_grace_review_granted` records
        one entry per approach, so this can never run twice for the same
        approach, and it never loops back into the worker -- exactly one
        review, then the job's fate is decided.

        Returns True iff the grace review came back VERIFIED and
        status.json has already been written terminal-succeeded (caller
        should return "succeeded" immediately). Returns False otherwise,
        having set `self._terminal_reason_note` to explain what happened
        (grace review didn't run because tasks weren't all complete, or it
        ran but did not verify) for the normal failed/aborted terminal
        write that follows in the caller.
        """
        if approach in self._grace_review_granted:
            return False
        if not self._all_tasks_completed():
            return False
        self._grace_review_granted.add(approach)
        self.run.emit("log", message=(
            "iteration budget exhausted with all tasks completed; granting "
            "a single off-budget grace review before ending the job "
            "(task 002)"))
        await self._gate()
        self.run.emit("phase", phase="review", approach=approach)
        review = await self.run_iteration(
            "review", extra=self._flagged_criteria_review_context())
        # Off-budget: this attempt must never count against the job's
        # iteration budget (same mechanism as the infra-retry refund).
        self._grace_refunded += 1
        if review.saw_verified and not self.run.pending_steering():
            self.run.emit("signal", signal="VERIFIED")
            self.run.update_status(
                state="succeeded", verdict="verified", phase=None,
                endedAt=utcnow(), graceReview=True,
                reason=("budget exhausted with all tasks completed; the "
                        "grace review ran and VERIFIED"),
                **self._unconsumed_steering_patch())
            return True
        if review.saw_verified:
            self._terminal_reason_note = (
                "budget exhausted with all tasks completed; the grace "
                "review VERIFIED but operator steering was still pending "
                "unconsumed, so the run cannot go terminal-succeeded")
        else:
            self._terminal_reason_note = (
                "budget exhausted with all tasks completed; a grace "
                "review ran but did not verify")
        self.run.emit("log", message=f"grace review did not verify approach {approach}")
        return False

    # Bounded retries for a verify iteration that errors out mid-stream
    # (agent/provider failure such as a Bedrock 502, message stopReason ==
    # "error") before ever emitting a verdict sentinel. Infrastructure
    # faults must never be scored as work failures (task 050) -- retry
    # verification instead of touching the task's status/validationAttempts.
    MAX_VERIFY_ERROR_RETRIES = 3

    # Task 059: an iteration whose agent process exits nonzero within a few
    # seconds having produced no observable work signal at all (no
    # assistant text, no usage) is almost certainly a provider/auth/infra
    # startup fault (e.g. missing or broken LLM credentials) rather than a
    # genuine attempted-but-failed work iteration. A run of these must
    # never be scored by -- or allowed to advance -- the "no progress"
    # stagnation guard, which exists to detect a stuck APPROACH, not a
    # broken environment; consuming approaches/attempts for them would burn
    # the whole job's budget in seconds without a single real attempt ever
    # having been made (the live incident behind this task: 11 consecutive
    # ~0.6s nonzero-exit iterations burned all 3 approaches in 7 seconds).
    INSTANT_FAILURE_MAX_DURATION_S = 5.0
    MAX_CONSECUTIVE_INSTANT_FAILURES = 3

    def _check_instant_failure(self, result: IterationResult, n: int) -> bool:
        """Update the running consecutive-instant-failure streak for
        `result` (any phase: planning or worker). Returns True once
        MAX_CONSECUTIVE_INSTANT_FAILURES has just been reached, in which
        case self._abort_reason has been set with a clear diagnostic --
        budget_left() is now False and every caller's existing "ran out of
        budget" exit path takes over from here, so the job fails fast with
        state=aborted (not state=failed via the no-progress path) and a
        reason naming the likely cause. A non-instant-failure result resets
        the streak to 0 (self._instant_failure_streak is also readable by
        callers that need to know whether *this* result was an instant
        failure below the abort threshold, to exclude it from their own
        progress bookkeeping without aborting yet).
        """
        is_instant = (
            result.exit_code not in (0, None)
            and not result.interrupted
            and not result.timed_out
            and not result.final_text
            and not result.usage
            and result.duration_s is not None
            and result.duration_s < self.INSTANT_FAILURE_MAX_DURATION_S
        )
        if not is_instant:
            self._instant_failure_streak = 0
            return False
        self._instant_failure_streak += 1
        self.run.emit(
            "log", level="error",
            message=(f"iteration {n} agent process exited instantly "
                      f"(exit={result.exit_code}, {result.duration_s:.1f}s) "
                      "with no observable work signal -- likely missing or "
                      "broken LLM credentials, or an agent-startup fault "
                      f"({self._instant_failure_streak}/"
                      f"{self.MAX_CONSECUTIVE_INSTANT_FAILURES} consecutive)"))
        if self._instant_failure_streak < self.MAX_CONSECUTIVE_INSTANT_FAILURES:
            return False
        diag = (f"{self._instant_failure_streak} consecutive iterations had "
                "the agent process exit instantly with no observable work "
                "(likely missing or broken LLM credentials, or an "
                "agent-startup fault) -- failing fast instead of burning "
                "through approaches via the no-progress escalation guard")
        self.run.emit("log", level="error", message=diag)
        self._abort_reason = diag
        return True

    @staticmethod
    def _criteria_fingerprint(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    def _ensure_criteria_baseline(self) -> None:
        """Task 008: give every task a criteriaFingerprint (sha256 of its
        successCriteria text) if it doesn't already have one -- true for a
        freshly planned task, and also backfills a run resumed from before
        this feature existed. Never flags anything: there is nothing yet to
        compare a first-sight fingerprint against."""
        data = self.run.read_tasks()
        changed = False
        for t in data.get("tasks", []):
            if "criteriaFingerprint" not in t:
                t["criteriaFingerprint"] = self._criteria_fingerprint(t.get("successCriteria", ""))
                changed = True
        if changed:
            atomic_write_json(self.run.tasks_file, data)

    def _check_criteria_edits(self, tasks_data: dict) -> None:
        """Task 008: compare each task's current successCriteria against its
        stored criteriaFingerprint. A task seen for the first time (no
        stored fingerprint, e.g. a task discovered mid-run) just gets a
        baseline recorded -- never flagged. A change observed while
        validationAttempts is still 0 (no validation failure has ever
        happened yet) silently updates the baseline -- also not flagged,
        per the negative case in task 008's successCriteria. Only a change
        observed once validationAttempts >= 1 sets the persistent
        criteriaEditedAfterValidationFailure marker, which -- once set --
        is never cleared (a worker doing this even once is the signal task
        009's review re-verification exists to catch, however the criteria
        keep evolving afterwards)."""
        changed = False
        for t in tasks_data.get("tasks", []):
            current = self._criteria_fingerprint(t.get("successCriteria", ""))
            stored = t.get("criteriaFingerprint")
            if stored is None:
                t["criteriaFingerprint"] = current
                changed = True
                continue
            if stored != current:
                if (t.get("validationAttempts", 0) >= 1
                        and not t.get("criteriaEditedAfterValidationFailure")):
                    t["criteriaEditedAfterValidationFailure"] = True
                t["criteriaFingerprint"] = current
                changed = True
        if changed:
            atomic_write_json(self.run.tasks_file, tasks_data)

    def _flagged_criteria_review_context(self) -> str:
        """Task 009: render the explicit list of tasks flagged
        criteriaEditedAfterValidationFailure (task 008) as extra review-
        prompt context, instructing the reviewer to independently re-verify
        each such task's CURRENT successCriteria text against the PRD and
        state a conclusion per task. This exists because _verify_task's own
        validationAttempts >= 3 skip would otherwise let a worker dodge
        every future automated check simply by rewriting the bar after a
        failure -- the review phase is the one place left that still sees
        every flagged task, every time, regardless of validationAttempts.
        Returns '' when no task is flagged (the common case), so review
        prompts are unaffected until this ever actually triggers.
        """
        data = self.run.read_tasks()
        flagged = [t for t in data.get("tasks", [])
                   if t.get("criteriaEditedAfterValidationFailure")]
        if not flagged:
            return ""
        lines = [
            "\n## Criteria edited after a validation failure (task 009)\n",
            ("The following task(s) had their `successCriteria` text rewritten "
             "AFTER at least one validation failure, before being re-marked "
             "`completed`. A worker doing this may have quietly moved the bar "
             "instead of doing the work. Before this review can emit "
             "`<promise>VERIFIED</promise>`, you MUST independently re-verify "
             "EACH task listed below against its CURRENT successCriteria text "
             "as written now -- not the original text, not what the worker's "
             "notes claim -- and state an explicit pass/fail conclusion for "
             "that task id. Do not let validationAttempts count (even if >= 3) "
             "substitute for this check; that automated skip is exactly what "
             "this manual re-verification exists to cover.\n"),
        ]
        for t in flagged:
            lines.append(
                f"\n- **{t['id']}** ({t.get('title', '')}): current "
                f"successCriteria: {t.get('successCriteria', '')}\n")
        return "".join(lines)

    async def _verify_task(self, task: dict) -> bool:
        """Run one verify iteration (with bounded retry on transient agent/
        provider errors) for a newly-completed task.

        Returns True when the verifier emits the correct sentinel,
        False otherwise -- either because the task genuinely failed
        verification (status forced to validation-failed or failed) or
        because verification kept erroring out / ran out of budget before
        ever producing a real verdict (task's status left untouched).
        """
        tid = task["id"]
        # Skip tasks that have already exhausted their verification budget
        if task.get("validationAttempts", 0) >= 3:
            log.warning("task %s already exhausted verification attempts; skipping", tid)
            return False
        title = task.get("title", "")
        criteria = task.get("successCriteria", "")
        extra = (
            f"## Task under verification\n\n"
            f"- **id**: {tid}\n"
            f"- **title**: {title}\n"
            f"- **successCriteria**: {criteria}\n"
        )

        result: IterationResult | None = None
        verified = False
        error_retries = 0
        while self.budget_left():
            result = await self.run_iteration("verify", extra=extra,
                                               prompt_name="task-verify")
            verify_iter_n = self.iterations_used  # captured after run_iteration increments it

            sentinel = f"<task-verified>{tid}</task-verified>"
            verified = sentinel in (result.final_text or "")

            # Enrich verify iteration meta.json with verifiedTask + verifyOutcome
            itdir = self.run.iteration_dir(verify_iter_n)
            meta_path = itdir / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except json.JSONDecodeError:
                    meta = {}
                meta["verifiedTask"] = tid
                if verified:
                    meta["verifyOutcome"] = "pass"
                elif result.error_message:
                    meta["verifyOutcome"] = "error"
                else:
                    meta["verifyOutcome"] = "fail"
                atomic_write_json(meta_path, meta)

            if verified:
                self.run.mark_task_verified(tid)
                self.run.emit("signal", signal="taskVerified", taskId=tid)
                log.info("task %s verified", tid)
                return True

            if result.error_message and error_retries < self.MAX_VERIFY_ERROR_RETRIES:
                error_retries += 1
                self.run.emit(
                    "log", level="warning",
                    message=(
                        f"verify iteration for task {tid} errored out before "
                        f"emitting a verdict ({result.error_message!r}); "
                        f"retrying verification ({error_retries}/"
                        f"{self.MAX_VERIFY_ERROR_RETRIES}) without consuming "
                        "a validation attempt"))
                continue

            break

        if result is not None and result.error_message and not verified:
            # Either exhausted the bounded error-retry budget or ran out of
            # iteration budget while retrying -- in both cases this is an
            # infrastructure fault, not a verified failure, so the task's
            # status and validationAttempts are left exactly as they were.
            self.run.emit(
                "log", level="error",
                message=(
                    f"verify iteration for task {tid} kept erroring "
                    f"({result.error_message!r}); leaving task status and "
                    "validationAttempts unchanged (not a validation failure)"))
            return False

        # Verification failed with an explicit (non-error) verdict miss:
        # ensure status is validation-failed and increment counter
        tasks_data = self.run.read_tasks()
        for t in tasks_data.get("tasks", []):
            if t["id"] == tid:
                attempts = t.get("validationAttempts", 0) + 1
                t["validationAttempts"] = attempts
                if t.get("status") not in ("validation-failed", "failed"):
                    t["status"] = "validation-failed"
                    if not t.get("validationNotes"):
                        t["validationNotes"] = (
                            "Verifier did not emit the task-verified sentinel."
                        )
                if attempts >= 3:
                    t["status"] = "failed"
                    log.warning("task %s failed after %d verification attempts",
                                tid, attempts)
                break
        atomic_write_json(self.run.tasks_file, tasks_data)
        self._emit_task_changes()
        return False

    def _warn_if_batched(self, before: dict, after: dict) -> None:
        """One task per worker iteration is a design invariant (checkpointing,
        steering, and vigilant verification all key off iteration boundaries).
        The engine can't roll back extra completions, but it must make the
        violation visible."""
        was = {t["id"]: t.get("status") for t in before.get("tasks", [])}
        newly = [t["id"] for t in after.get("tasks", [])
                 if t.get("status") == "completed" and was.get(t["id"]) != "completed"]
        if len(newly) > 1:
            msg = (f"worker completed {len(newly)} tasks in one iteration "
                   f"({', '.join(newly)}); design is one task per iteration")
            log.warning(msg)
            self.run.emit("log", level="warning", message=msg)

    def _archive_approach(self, approach: int) -> None:
        dest = self.run.root / "approaches" / f"{approach:02d}"
        dest.mkdir(parents=True, exist_ok=True)
        for f in (self.run.tasks_file, self.run.notes_file, self.run.findings_file):
            if f.exists():
                shutil.copy2(f, dest / f.name)

    def _write_composite_prd(self, approach: int) -> None:
        parts = [self.run.prd_file.read_text(),
                 "\n\n---\n\n# Previous attempt history\n"]
        for adir in sorted((self.run.root / "approaches").iterdir()):
            findings = adir / "review-findings.md"
            notes = adir / "notes.md"
            parts.append(f"\n## Approach {adir.name}\n")
            if notes.exists():
                parts.append(f"\n### Final notes\n{notes.read_text()}\n")
            if findings.exists():
                parts.append(f"\n### Review findings (unmet requirements)\n"
                             f"{findings.read_text()}\n")
        parts.append("\nAddress ALL review findings above in this new attempt. "
                     "Do not blindly repeat the previous approach.\n")
        from .state import atomic_write
        atomic_write(self.run.composite_prd_file, "".join(parts))
