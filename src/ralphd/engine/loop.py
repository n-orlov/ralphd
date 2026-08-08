"""Loop supervisor: planning → worker → review, approaches, budgets."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

from .config import PROMPTS_BUILTIN, JobConfig, overlay_or_config
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
        parts.append(f"- Run state directory: {self.run.root}\n"
                     f"- Workspace (code) directory: {self.workspace}\n"
                     f"- PRD file: {prd}\n"
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
        host_run = os.environ.get("RALPHD_HOST_RUN_DIR")
        if not host_ws and not host_run:
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
        if host_run:
            lines.append(f"  - run dir: `$RALPHD_HOST_RUN_DIR` = `{host_run}`\n")
        lines.append(
            f"- Label every sibling `--label ralphd.run=$RALPHD_RUN_ID` "
            f"(= `{run_id}`) so it gets reaped with this job; prefer `--rm` "
            "for anything short-lived.\n"
            "- Images you build and volumes you create live on the HOST and "
            "are reaped by that same label — label them too.\n")
        return "".join(lines)

    # -- iteration ----------------------------------------------------------
    async def run_iteration(self, phase: str, extra: str = "",
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
                    extra_env=current_env())
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
        return (self.iterations_used < self.cfg.iterations
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
        """Returns final state: succeeded | failed | aborted."""
        self.run.update_status(state="running", startedAt=utcnow(),
                               iterationsBudget=self.cfg.iterations,
                               maxApproaches=self.cfg.max_approaches,
                               onComplete=self.cfg.on_complete, verdict=None)
        start_approach, resuming = self._resume_point()
        try:
            for approach in range(start_approach, self.cfg.max_approaches + 1):
                if not self.budget_left():
                    break
                self.run.update_status(approach=approach)
                skip_planning = resuming and approach == start_approach
                if not skip_planning:
                    self.run.emit("phase", phase="planning", approach=approach)

                    await self._gate()
                    await self.run_iteration("planning")
                    if not self.run.read_tasks().get("tasks"):
                        self.run.emit("log", level="error",
                                      message="planning produced no tasks.json")
                        continue

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
                        before_statuses = {
                            t["id"]: t.get("status")
                            for t in tasks_before.get("tasks", [])
                        }
                        newly_completed = [
                            t for t in tasks_after.get("tasks", [])
                            if t.get("status") == "completed"
                            and before_statuses.get(t["id"]) != "completed"
                        ]
                        for task in newly_completed:
                            if self.budget_left():
                                await self._gate()
                                await self._verify_task(task)
                        # Re-read after any verification-driven status updates
                        tasks_after = self.run.read_tasks()

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

                if not verdict_ready or not self.budget_left():
                    if not self.budget_left():
                        break
                    self._archive_approach(approach)
                    continue

                # review
                await self._gate()
                self.run.emit("phase", phase="review", approach=approach)
                review = await self.run_iteration("review")
                if review.saw_verified:
                    self.run.emit("signal", signal="VERIFIED")
                    self.run.update_status(state="succeeded", verdict="verified",
                                           phase=None, endedAt=utcnow())
                    return "succeeded"
                self.run.emit("log", message=f"review rejected approach {approach}")
                self._archive_approach(approach)
                self._write_composite_prd(approach)

            state = "aborted" if self._abort_reason else "failed"
            self.run.update_status(state=state, verdict="unverified", phase=None,
                                   endedAt=utcnow(),
                                   reason=self._abort_reason)
            return state
        except Exception as exc:  # engine bug — record, don't vanish
            self.run.emit("log", level="error", message=f"engine error: {exc!r}")
            self.run.update_status(state="failed", verdict="unverified",
                                   phase=None, endedAt=utcnow(),
                                   reason=f"engine error: {exc}")
            return "failed"

    # Bounded retries for a verify iteration that errors out mid-stream
    # (agent/provider failure such as a Bedrock 502, message stopReason ==
    # "error") before ever emitting a verdict sentinel. Infrastructure
    # faults must never be scored as work failures (task 050) -- retry
    # verification instead of touching the task's status/validationAttempts.
    MAX_VERIFY_ERROR_RETRIES = 3

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
