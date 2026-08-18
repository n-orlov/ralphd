"""Loop supervisor: planning → worker → review, approaches, budgets."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from .config import (
    DEFAULT_INFRA_RETRY_BACKOFF_S,
    PROMPTS_BUILTIN,
    JobConfig,
    overlay_or_config,
)
from .faults import classify_fault
from .llm import current_env
from .runner import IterationResult, PiRunner
from .state import RunDir, atomic_write_json, utc_from_epoch, utcnow

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
        # Task 003 (#11): operator-initiated abort/interrupt bookkeeping,
        # consulted by classify_fault(operator_abort=...) so a SIGINT the
        # *operator* asked for is never mistaken for a provider-side
        # stream abort and retried as an outage. POST /abort sets
        # _abort_reason (sticky: the run is ending); POST /interrupt only
        # ends the current iteration, so its flag is armed per attempt and
        # cleared when the next attempt starts.
        self._operator_interrupted = False
        self._instant_failure_streak = 0
        # Task 010 (#5): the error signature the current instant-failure
        # streak is made of, plus the memoised verdict for the attempt
        # _check_instant_failure() scored last (the infra-retry wrapper
        # scores every attempt as it happens; the planning/worker call
        # sites then hand the same resolved result back).
        self._instant_failure_sig: str | None = None
        self._instant_scored_result: IterationResult | None = None
        self._instant_scored_tripped = False
        # Task 001a: iterations refunded because they were retried after an
        # infra-classified fault (see _run_iteration_with_infra_retry) --
        # subtracted from self.iterations_used when checking budget_left()
        # so a hung/broken-endpoint retry never costs the job an iteration,
        # while self.iterations_used itself keeps monotonically increasing
        # (so every attempt still gets its own iteration directory/number).
        self._infra_refunded = 0
        # Task 008 (#5): the *episode clock* driving infra retries. One
        # "episode" is one continuous outage: consecutive infra-classified
        # attempts with no iteration reaching the model in between. Retries
        # continue while the episode's cumulative backoff wait stays under
        # cfg.infra_outage_budget_s (wall clock, not an attempt count -- a
        # gateway can be down for an hour and the right answer is to keep
        # waiting, not to give up after 3 tries), and the whole episode
        # resets as soon as an iteration gets through again.
        self._infra_episode_attempts = 0
        self._infra_episode_waited_s = 0.0
        self._infra_episode_started_at: float | None = None
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
        # Task 011 (#5): wall-clock twin of self.deadline (published as
        # status.json's deadlineAt) plus the running total of time this run
        # spent sitting out infra outages. DECISION: an outage must not eat
        # the job's working time -- self.deadline is wall clock, so a 4-hour
        # gateway outage would silently consume half an 8-hour job and the
        # run would die of "timeout" having done nothing wrong. Every infra
        # backoff wait therefore *extends* both deadlines by exactly the
        # waited seconds (see _account_infra_wait) and is accounted in
        # infraWaitTotalS, so job_timeout_s keeps its plain meaning: time
        # available to the agent, not time available to the agent's network.
        # The total is seeded from status.json so it survives `resume` (the
        # deadline itself is per-process by construction).
        self._deadline_epoch = time.time() + cfg.job_timeout_s
        self._infra_wait_total_s = float(
            run.read_status().get("infraWaitTotalS") or 0.0)
        # Task 012 (#5): does status.json currently say health "degraded"?
        # Tracked in memory so the transition back to "ok" is emitted (and
        # written) exactly once, on the iteration that proves the endpoint is
        # back, instead of on every non-infra result of a healthy run.
        self._infra_degraded = False

    # -- control surface (called from API) --------------------------------
    @property
    def operator_abort_requested(self) -> bool:
        """True when this run's current failure signal, whatever it looks
        like, was asked for by the operator (task 003, #11)."""
        return self._abort_reason is not None or self._operator_interrupted

    def interrupt(self) -> bool:
        delivered = self.runner.interrupt()
        if delivered:
            # Only record it when a signal actually reached a running agent
            # -- an interrupt with nothing running changes no iteration's
            # outcome and must not shield the next one from infra retry.
            self._operator_interrupted = True
        return delivered

    def pause(self) -> None:
        self._pause.clear()
        self.run.emit("log", message="paused at next iteration boundary")

    def resume(self) -> None:
        self._pause.set()
        self.run.emit("log", message="resumed")

    def abort(self, reason: str = "") -> None:
        self._abort_reason = reason or "aborted by operator"
        self._operator_interrupted = True
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
    # Phases protected by the infra-fault retry-with-backoff wrapper.
    #
    # Task 009 (#5): ALL five phases. A gateway/endpoint outage does not
    # care which prompt the engine happens to be running -- task 001a only
    # covered planning/worker because those are the two phases the original
    # incident hit, which left the other three scoring an outage as work:
    # an infra-shaped `review` failure rejected the approach and archived
    # it, an infra-shaped `verify` failure ate the task's bounded
    # error-retry budget (and, once that ran out, risked its
    # validationAttempts), and `reflect` simply lost the reflection report.
    #
    # Precedence (truthfully, as implemented): an infra-classified failure
    # is handled *here* -- retried in place, refunded, the episode clock
    # deciding when to give up -- and consumes none of the phase-local
    # error budgets. `run_iteration()` only returns to the phase's own
    # logic once the result is no longer an infra fault (a success or a
    # genuine work failure), or once the wrapper gave up, in which case
    # `_abort_reason` is set and `budget_left()` is already False -- so
    # `_verify_task`'s MAX_VERIFY_ERROR_RETRIES loop and the review
    # steering loop both exit at once instead of re-charging the same
    # outage against their own budgets. Nothing here touches a task's
    # `validationAttempts` (only an explicit non-error verdict miss in
    # `_verify_task` does). An infra fault that is also an *instant*
    # failure is retried here too (task 010, #5), with task 059's
    # broken-environment carve-out keeping the last word on a *run* of
    # identical instant faults -- see _check_instant_failure().
    INFRA_RETRY_PHASES = ("planning", "worker", "review", "verify", "reflect")

    async def run_iteration(self, phase: str, extra: str = "",
                             prompt_name: str | None = None):
        if phase not in self.INFRA_RETRY_PHASES:
            return await self._run_iteration_once(phase, extra, prompt_name)
        return await self._run_iteration_with_infra_retry(phase, extra, prompt_name)

    # Task 001a: escalating backoff defaults live on JobConfig
    # (cfg.infra_retry_backoff_s / cfg.infra_retry_backoff_max_s /
    # cfg.infra_outage_budget_s / cfg.infra_retry_max), overridable via the
    # matching RALPHD_INFRA_* env vars so tests (and operators) don't need a
    # job.yaml edit for every run.
    #
    # Task 008 (#5): the stopping rule is the wall-clock outage budget
    # (episode clock, see __init__), NOT an attempt count. cfg.infra_retry_max
    # is a back-compat cap honoured only when explicitly configured.
    async def _run_iteration_with_infra_retry(self, phase: str, extra: str,
                                              prompt_name: str | None):
        """Runs `phase` via _run_iteration_once(), retrying THE SAME
        phase/iteration with escalating backoff whenever the result
        classifies as an infra fault (broken LLM endpoint/provider/network
        -- see .faults.classify_fault) rather than a genuine work failure.

        Each infra-classified attempt is refunded (never counted against
        cfg.iterations, see budget_left()) and does NOT touch
        self._instant_failure_streak / the no-progress stagnation guard --
        callers (the planning/worker/review/verify/reflect call sites in
        _run_job_core) see a fully resolved IterationResult exactly as
        before, either a genuine
        success/work-failure or (once the episode ran out of stopping room,
        see below) the last failing attempt with self._abort_reason already
        set to a diagnostic naming the infra fault plainly.

        Task 008 (#5): retries are driven by a wall-clock *episode clock*,
        not an attempt count. Attempts are unlimited by default and stop
        only when the episode's cumulative backoff wait has reached
        cfg.infra_outage_budget_s (default 4h) -- so a 30-minute endpoint
        outage costs the job nothing but time, while a permanently broken
        endpoint still terminates the run with a reason naming the total
        outage duration and the last error. cfg.infra_retry_max, when
        explicitly configured, still caps the attempts (back-compat, and an
        escape hatch for operators who want a hard stop); the episode resets
        on any iteration that reaches the model (_reset_infra_episode).

        Task 010 (#5): an infra fault that is ALSO an *instant* failure
        (sub-INSTANT_FAILURE_MAX_DURATION_S with no observable work) is now
        retried here like any other -- a connection refused by a gateway
        that is still coming up returns in 0.2s and used to end the run on
        the spot. The pre-existing broken-environment carve-out keeps the
        last word on a *run* of them: every such attempt is scored by
        _check_instant_failure(), and once MAX_CONSECUTIVE_INSTANT_FAILURES
        attempts have failed instantly, with no traffic and with the *same*
        error signature, the wrapper stops with that carve-out's diagnosis
        instead of sitting out the whole outage budget on an environment
        that is never coming back. An instant failure that DID reach the
        model (tokens billed) is still handed straight back to the phase's
        own bounded error retry -- see the comment at that return below.

        Task 003 (#11): an operator-initiated abort/interrupt is never an
        infra fault (classify_fault(operator_abort=...) sees
        self.operator_abort_requested), so POST /abort and POST /interrupt
        take effect at once instead of being fought by a retry episode --
        pi reports both as the same bare "aborted" error a provider-side
        stream abort produces.
        """
        retry_max = self.cfg.infra_retry_max  # None == no explicit attempt cap
        budget = self.cfg.infra_outage_budget_s
        while True:
            result = await self._run_iteration_once(phase, extra, prompt_name)
            fault = self._classify_result(result)
            if fault != "infra":
                # The agent reached the model -- a success, or a genuine work
                # failure it produced itself. Either way the outage (if there
                # was one) is over: the next one starts from a clean clock.
                self._reset_infra_episode()
                return result
            # Task 010 (#5): decide which mechanism owns an *instant*
            # failure before retrying anything here.
            if (result.duration_s is not None
                    and result.duration_s < self.INSTANT_FAILURE_MAX_DURATION_S
                    and not result.no_traffic_timeout
                    and self._instant_failure_signature(result) is None):
                # An instant failure that DID reach the model (an in-band
                # provider error with tokens billed -- a 0.3s Bedrock 502
                # mid-verify) stays with the phase's own bounded error
                # retry, which records the attempt's outcome and retries it
                # without consuming a validation attempt. Task 010 only
                # moves the *zero-work* instant shape (nothing observable
                # at all: the refused-connection / broken-credential shape)
                # into this wrapper.
                return result
            # Score this attempt against the broken-environment carve-out (a
            # streak of identical instant zero-work failures). True == the
            # streak just reached its threshold and _abort_reason now holds
            # that diagnosis.
            broken_env = self._check_instant_failure(result, self.iterations_used)

            self._infra_refunded += 1
            if self._infra_episode_started_at is None:
                self._infra_episode_started_at = time.monotonic()
            self._infra_episode_attempts += 1
            attempt = self._infra_episode_attempts
            waited = self._infra_episode_waited_s
            error_desc = result.error_message or "no LLM traffic within startup window"
            # Escalating schedule with the last value repeating, capped by
            # infra_retry_backoff_max_s and by whatever is left of the outage
            # budget, so one episode's cumulative wait never exceeds it.
            backoff_schedule = (self.cfg.infra_retry_backoff_s
                                or DEFAULT_INFRA_RETRY_BACKOFF_S)
            backoff = backoff_schedule[min(attempt - 1, len(backoff_schedule) - 1)]
            backoff = min(backoff, self.cfg.infra_retry_backoff_max_s,
                          max(0.0, budget - waited))
            cap_reached = retry_max is not None and attempt >= retry_max
            budget_spent = waited >= budget
            self.run.emit("infra_retry", phase=phase, attempt=attempt,
                          maxAttempts=retry_max, error=error_desc,
                          noTrafficTimeout=result.no_traffic_timeout,
                          instantFailure=bool(self._instant_failure_streak),
                          backoffS=(None if cap_reached or budget_spent
                                    or broken_env else backoff),
                          waitedS=round(waited, 3), budgetS=budget)
            log.warning("iteration %s classified infra fault (attempt %d, %.0fs of "
                       "the %.0fs outage budget waited): %s",
                       phase, attempt, waited, budget, error_desc)
            self.run.update_status(
                iterationsUsed=self.iterations_used - self._infra_refunded)
            if broken_env:
                # _check_instant_failure() has already set _abort_reason to
                # the broken-environment diagnosis and logged it: the
                # attempts are still refunded, but this environment is not
                # coming back on its own, so stop now instead of waiting
                # out the outage budget.
                return result
            if cap_reached:
                # Back-compat: reachable only when a cap was set explicitly.
                self._abort_reason = (
                    f"infra fault: {phase} iteration failed after "
                    f"{retry_max} attempts ({error_desc})")
            elif budget_spent:
                outage_s = time.monotonic() - self._infra_episode_started_at
                self._abort_reason = (
                    f"infra fault: {phase} iteration failed throughout a "
                    f"{outage_s:.0f}s infra outage ({attempt} attempts, "
                    f"{waited:.0f}s of the {budget:.0f}s outage budget spent "
                    f"waiting): {error_desc}")
            if self._abort_reason is not None:
                self.run.emit("log", level="error", message=self._abort_reason)
                return result
            cap_note = f"/{retry_max}" if retry_max is not None else ""
            self.run.update_status(currentIteration={
                "phase": phase,
                "note": (f"retrying after infra fault (attempt {attempt}"
                         f"{cap_note}, next in {backoff:.0f}s): "
                         f"{error_desc}")})
            self._begin_infra_wait(phase=phase, attempt=attempt,
                                   error=error_desc, waited_s=waited,
                                   backoff_s=backoff, budget_s=budget)
            await asyncio.sleep(backoff)
            self._infra_episode_waited_s = waited + backoff
            self._account_infra_wait(backoff, phase, attempt)
            self._end_infra_wait()

    def _begin_infra_wait(self, *, phase: str, attempt: int, error: str,
                          waited_s: float, backoff_s: float,
                          budget_s: float) -> None:
        """Publishes the degraded half of the status contract (task 012, #5)
        for the backoff wait that is about to start.

        ``state`` deliberately stays ``running`` -- adding a "degraded" state
        value would break every consumer's terminal-state logic (``ralphctl
        watch`` included). The degraded case is carried by two new fields
        instead: ``health`` ("ok" | "degraded") and ``infraWait``, which is
        ``null`` whenever the run is not actually sitting in a backoff wait
        and otherwise says since when, which attempt, the error, the phase,
        when the next attempt is due, and how much of the outage budget is
        spent/left. The same payload goes out as an ``infra_wait`` event so
        the wait is visible in the event stream `ralphctl watch` follows,
        not only to whoever polls /status at the right moment.
        """
        now = time.time()
        remaining = max(0.0, budget_s - waited_s)
        wait = {
            "since": utc_from_epoch(now),
            "attempt": attempt,
            "error": error,
            "phase": phase,
            "nextAttemptAt": utc_from_epoch(now + backoff_s),
            "waitedS": round(waited_s, 3),
            "budgetS": budget_s,
            "remainingS": round(remaining, 3),
        }
        self._infra_degraded = True
        self.run.update_status(health="degraded", infraWait=wait)
        self.run.emit("infra_wait", **wait, backoffS=backoff_s)

    def _end_infra_wait(self) -> None:
        """The backoff wait is over and the next attempt is starting: nothing
        is being waited on any more, so ``infraWait`` goes back to ``null``
        (task 012, #5). ``health`` stays "degraded" until an iteration
        actually reaches the model again (_reset_infra_episode) -- a run whose
        endpoint is still broken has not recovered just because it is between
        two backoffs."""
        self.run.update_status(infraWait=None)

    def _account_infra_wait(self, waited_s: float, phase: str,
                            attempt: int) -> None:
        """Books one finished infra backoff wait (task 011, #5): it adds to
        status.json's ``infraWaitTotalS`` and pushes the job deadline
        (``self.deadline`` and its published wall-clock twin ``deadlineAt``)
        out by exactly the waited seconds, emitting ``deadline_extended`` so
        an extension is auditable rather than a silent clock adjustment.

        The amount booked is the backoff the wrapper slept for, which is also
        what the outage-budget episode clock counts -- one number, so
        ``infraWaitTotalS`` and the budget arithmetic can never disagree.
        """
        self._infra_wait_total_s += waited_s
        self.deadline += waited_s
        self._deadline_epoch += waited_s
        total = round(self._infra_wait_total_s, 3)
        deadline_at = utc_from_epoch(self._deadline_epoch)
        self.run.update_status(infraWaitTotalS=total, deadlineAt=deadline_at)
        self.run.emit("deadline_extended", phase=phase, attempt=attempt,
                      waitedS=round(waited_s, 3), infraWaitTotalS=total,
                      deadlineAt=deadline_at, reason="infra wait")

    def _reset_infra_episode(self) -> None:
        """Ends the current infra-outage episode (task 008, #5): the next
        outage gets the full backoff schedule and the full outage budget
        again, so a job hitting a short glitch every hour is never slowly
        starved of retry budget by the earlier ones. Task 010 (#5): it also
        ends any instant-failure streak -- an iteration that reached the
        model proves the environment works, so the next instant fault
        starts counting from scratch."""
        self._instant_failure_streak = 0
        self._instant_failure_sig = None
        self._infra_episode_attempts = 0
        self._infra_episode_waited_s = 0.0
        self._infra_episode_started_at = None
        if self._infra_degraded:
            # Task 012 (#5): recovery is the one event that clears the
            # degraded half of the status contract -- health back to "ok",
            # infraWait back to null, and an event so an operator watching
            # the stream sees the outage end, not just its start.
            self._infra_degraded = False
            self.run.update_status(health="ok", infraWait=None)
            self.run.emit("infra_recovered", health="ok",
                          infraWaitTotalS=round(self._infra_wait_total_s, 3))

    def _classify_result(self, result: IterationResult) -> str | None:
        """The engine's single fault verdict for one finished iteration:
        ``None`` (not a failure at all), ``"infra"`` or ``"work"``.

        Task 004 (#11): one place derives the classify_fault() inputs from an
        IterationResult, so the verdict recorded in the iteration's meta.json
        and the ``iteration.end`` event (``faultClass``) is by construction
        the same verdict the infra-retry wrapper acts on -- an operator
        reading `faultClass: "infra"` can be sure that is why the attempt was
        retried and refunded.
        """
        return classify_fault(
            error_text=result.error_message or "",
            exit_code=result.exit_code,
            interrupted=result.interrupted,
            timed_out=result.timed_out,
            no_traffic_timeout=result.no_traffic_timeout,
            produced_traffic=bool(result.final_text) or bool(result.usage),
            operator_abort=self.operator_abort_requested)

    async def _run_iteration_once(self, phase: str, extra: str = "",
                                  prompt_name: str | None = None):
        n = self.iterations_used + 1
        # Task 003 (#11): a POST /interrupt shields only the iteration it
        # actually interrupted -- re-arm for this attempt (an abort, which
        # ends the whole run, stays recorded in _abort_reason).
        self._operator_interrupted = False
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
        # Task 005 (#11): the *charged* count, not the raw attempt number --
        # an infra-classified attempt is refunded (budget_left()) and must
        # stay refunded in what the operator reads, not be silently
        # re-charged by the next iteration's status write. (A grace review's
        # refund is deliberately NOT subtracted here: it keeps showing as a
        # used iteration number, see test_e2e's grace-review contract.)
        self.run.update_status(phase=phase, iteration=n,
                               iterationsUsed=n - self._infra_refunded,
                               currentIteration={"number": n, "phase": phase,
                                                 "model": model,
                                                 "startedAt": meta["startedAt"]})
        if pending:
            self.run.consume_steering(pending, n)

        timeout = min(self.cfg.iteration_timeout_s,
                      max(60, int(self.deadline - time.monotonic())))
        # Task 001a: the startup-window watchdog applies to exactly the
        # phases the infra-retry wrapper protects -- a phase whose zero-
        # traffic hang nobody would retry should not be killed early
        # either. Since task 009 that is every phase: a hung `review`/
        # `verify`/`reflect` invocation is now cut short at
        # cfg.infra_startup_timeout_s (default 150s) and retried, instead
        # of hanging out the full iteration timeout.
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

        # Task 004 (#11): record the engine's own fault verdict next to the
        # raw signals it was derived from -- null for a clean iteration,
        # "infra"/"work" otherwise -- so an operator (or a later triage pass
        # over a finished run dir) can see *why* an attempt was retried and
        # refunded without re-deriving the classification from exit codes,
        # error text and token usage by hand.
        fault_class = self._classify_result(result)
        meta.update(endedAt=utcnow(), exitCode=result.exit_code,
                    interrupted=result.interrupted, timedOut=result.timed_out,
                    noTrafficTimeout=result.no_traffic_timeout,
                    sawComplete=result.saw_complete, sawVerified=result.saw_verified,
                    error=result.error_message or None,
                    faultClass=fault_class,
                    usage=result.usage)
        atomic_write_json(itdir / "meta.json", meta)
        self._accumulate_usage(result.usage, phase=phase, approach=meta.get("approach"))
        self.run.emit("iteration.end", number=n, phase=phase,
                      exitCode=result.exit_code, interrupted=result.interrupted,
                      sawComplete=result.saw_complete, sawVerified=result.saw_verified,
                      error=result.error_message or None,
                      faultClass=fault_class)
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
                               deadlineAt=utc_from_epoch(self._deadline_epoch),
                               infraWaitTotalS=round(self._infra_wait_total_s, 3),
                               health="ok", infraWait=None,
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
    #
    # Task 009 (#5): since `verify` runs through the infra-retry wrapper,
    # this budget is only reached by an error the wrapper did NOT handle --
    # an instant (sub-INSTANT_FAILURE_MAX_DURATION_S) in-band error, or a
    # non-infra one. An infra fault the wrapper retried never lands here at
    # all, so an outage cannot consume these retries; and when the wrapper
    # gives up it sets _abort_reason, so budget_left() ends the loop below
    # instead of double-counting the same outage against this budget.
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
    #
    # Task 010 (#5): such a failure is an infra fault too, so it is now
    # *retried* by _run_iteration_with_infra_retry() first -- what stays
    # here is the fail-fast half: a run of MAX_CONSECUTIVE_INSTANT_FAILURES
    # attempts that all fail this way with the same signature stops the job
    # in seconds instead of waiting out the multi-hour outage budget on an
    # environment that cannot work (see _instant_failure_signature).
    INSTANT_FAILURE_MAX_DURATION_S = 5.0
    MAX_CONSECUTIVE_INSTANT_FAILURES = 3

    def _instant_failure_signature(self, result: IterationResult) -> str | None:
        """A stable signature for an *instant, zero-work* failure, or None
        when `result` isn't that shape at all.

        Task 010 (#5): the signature is what tells a broken environment
        apart from a transient infra fault now that both are retried.
        Transient faults vary and take time -- a gateway 502 arrives after
        a connect, a DNS glitch resolves itself, the error text moves
        around -- whereas a broken credential (or a missing agent binary)
        fails identically in 0.6s, every single time. So attempts are only
        counted towards the fail-fast streak while they keep producing the
        *same* signature: the exit code plus the error text with digits
        normalised away (timestamps, ports, request ids).

        An exit-0 attempt counts too when it recorded an in-band error
        (task 001/005's shape): pi can report a fatal startup/provider
        error as an assistant error message and still shut down cleanly.
        """
        if result.interrupted or result.timed_out or result.no_traffic_timeout:
            return None  # not instant / not this shape
        # "No observable work" means no assistant text and no *tokens*: pi
        # zero-fills a usage block on every message_end, so an in-band
        # error's usage dict is non-empty while nothing was ever billed.
        usage = result.usage or {}
        tokens = sum(int(usage.get(k) or 0) for k in
                     ("input", "output", "cacheRead", "cacheWrite", "totalTokens"))
        if result.final_text or tokens:
            return None  # observable work: the agent reached the model
        if result.duration_s is None or \
                result.duration_s >= self.INSTANT_FAILURE_MAX_DURATION_S:
            return None
        error = (result.error_message or "").strip()
        if result.exit_code in (0, None) and not error:
            return None  # not a failure at all
        return f"exit={result.exit_code}|{re.sub(r'[0-9]+', 'N', error.lower())[:200]}"

    def _check_instant_failure(self, result: IterationResult, n: int) -> bool:
        """Update the running consecutive-instant-failure streak for
        `result` (any phase). Returns True once
        MAX_CONSECUTIVE_INSTANT_FAILURES has just been reached, in which
        case self._abort_reason has been set with a clear diagnostic --
        budget_left() is now False and every caller's existing "ran out of
        budget" exit path takes over from here, so the job fails fast with
        state=aborted (not state=failed via the no-progress path) and a
        reason naming the likely cause. A result that isn't an instant
        zero-work failure resets the streak to 0, and so does one whose
        error signature differs from the streak's
        (_instant_failure_signature); self._instant_failure_streak is also
        readable by callers that need to know whether *this* result was an
        instant failure below the abort threshold, to exclude it from their
        own progress bookkeeping without aborting yet.

        Task 010 (#5): the infra-retry wrapper scores every instant attempt
        through here as it happens (instant infra faults are retried now),
        then hands the resolved result back to its caller -- which scores
        it again at the planning/worker call sites. The verdict is
        therefore memoised per result object: scoring one attempt twice
        would inflate the streak and duplicate its log line.
        """
        if result is self._instant_scored_result:
            return self._instant_scored_tripped
        self._instant_scored_result = result
        self._instant_scored_tripped = False
        signature = self._instant_failure_signature(result)
        if signature is None:
            self._instant_failure_streak = 0
            self._instant_failure_sig = None
            return False
        if signature != self._instant_failure_sig:
            # A *different* instant fault: two unrelated transient causes
            # must not add up to a broken-environment verdict.
            self._instant_failure_streak = 0
            self._instant_failure_sig = signature
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
        self._instant_scored_tripped = True
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
