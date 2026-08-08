"""Loop supervisor: planning → worker → review, approaches, budgets."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from .config import CONFIG_DIR, PROMPTS_BUILTIN, JobConfig
from .runner import IterationResult, PiRunner
from .state import RunDir, atomic_write_json, utcnow

log = logging.getLogger("ralphd.loop")


class LoopSupervisor:
    def __init__(self, cfg: JobConfig, run: RunDir, workspace: Path):
        self.cfg = cfg
        self.run = run
        self.workspace = workspace
        self.runner = PiRunner(workspace)
        self.iterations_used = 0
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
        override = CONFIG_DIR / "prompts" / f"{name}.md"
        path = override if override.exists() else PROMPTS_BUILTIN / f"{name}.md"
        return path.read_text()

    def build_prompt(self, phase: str, extra: str = "") -> str:
        prd = self.run.composite_prd_file if self.run.composite_prd_file.exists() \
            else self.run.prd_file
        parts = [self.prompt_text(phase)]
        parts.append("\n\n## Job context\n")
        parts.append(f"- Run state directory: {self.run.root}\n"
                     f"- Workspace (code) directory: {self.workspace}\n"
                     f"- PRD file: {prd}\n"
                     f"- Task state file: {self.run.tasks_file}\n"
                     f"- Handoff notes file: {self.run.notes_file}\n"
                     f"- Artifacts directory: {self.run.artifacts_dir}\n")
        pending = self.run.pending_steering()
        if pending:
            parts.append("\n## Operator steering (MUST take priority)\n")
            for p in pending:
                parts.append(f"\n### {p.name}\n{p.read_text()}\n")
        if extra:
            parts.append("\n" + extra)
        return "".join(parts)

    # -- iteration ----------------------------------------------------------
    async def run_iteration(self, phase: str, extra: str = ""):
        n = self.iterations_used + 1
        self.iterations_used = n
        itdir = self.run.iteration_dir(n)
        model = self.cfg.model_for(phase)
        pending = self.run.pending_steering()
        prompt = self.build_prompt(phase, extra)
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
        try:
            result = await self.runner.run(
                prompt, itdir / "output.jsonl", model=model,
                thinking=self.cfg.thinking, timeout_s=timeout)
        except Exception as exc:
            # an engine-side iteration failure (stream error, OS error) must
            # cost one iteration, not the whole job
            result = IterationResult(exit_code=None)
            result.error_message = f"engine iteration failure: {exc!r}"

        meta.update(endedAt=utcnow(), exitCode=result.exit_code,
                    interrupted=result.interrupted, timedOut=result.timed_out,
                    sawComplete=result.saw_complete, sawVerified=result.saw_verified,
                    error=result.error_message or None,
                    usage=result.usage)
        atomic_write_json(itdir / "meta.json", meta)
        self._accumulate_usage(result.usage)
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

    def _accumulate_usage(self, usage: dict) -> None:
        status = self.run.read_status()
        total = status.get("usage", {})
        for k, v in usage.items():
            total[k] = round(total.get(k, 0) + v, 6) if isinstance(v, float) \
                else total.get(k, 0) + v
        self.run.update_status(usage=total)

    def _emit_task_changes(self) -> None:
        tasks = {t["id"]: t.get("status") for t in self.run.read_tasks().get("tasks", [])}
        for tid, status in tasks.items():
            old = self._last_task_snapshot.get(tid)
            if old != status:
                self.run.emit("task", taskId=tid, oldStatus=old, newStatus=status)
        self._last_task_snapshot = tasks

    # -- budget/limits -------------------------------------------------------
    def budget_left(self) -> bool:
        return (self.iterations_used < self.cfg.iterations
                and time.monotonic() < self.deadline
                and self._abort_reason is None)

    async def _gate(self) -> None:
        await self._pause.wait()

    # -- main loop ------------------------------------------------------------
    async def run_job(self) -> str:
        """Returns final state: succeeded | failed | aborted."""
        self.run.update_status(state="running", startedAt=utcnow(),
                               iterationsBudget=self.cfg.iterations,
                               maxApproaches=self.cfg.max_approaches,
                               onComplete=self.cfg.on_complete, verdict=None)
        try:
            for approach in range(1, self.cfg.max_approaches + 1):
                if not self.budget_left():
                    break
                self.run.update_status(approach=approach)
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
                    after = json.dumps(tasks_after, sort_keys=True)
                    self._warn_if_batched(tasks_before, tasks_after)
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
