"""Task 006 (PRD requirement C.1/C.2, #211): a harness kill of the reflect
iteration is not an agent fault.

Before this task, `_reflect_contract_ladder` (`loop.py`) guarded only on
`result is None` and a missing `artifacts/reflection/report.md` -- an
interrupted, timed-out or no-traffic reflect attempt with a report ALREADY
on disk (a stale one from an earlier attempt, or a partial one the killed
process itself left behind) fell straight into `enforce_contract`'s re-ask
ladder over truncated/empty output, and an exhausted ladder emitted
`contract.agent_fault` for a harness kill nobody committed. `_iteration_
cut_short_by_engine` (`loop.py:10568`) already exists and is already applied
ahead of the same ladder-shaped re-ask at every other hat-driving call site
(`_step_generic_hat`, `_review_interrupted`, planning) -- reflect was the one
path with no such gate.

Tests:

1. `test_a_reflect_killed_at_the_tail_timeout_skips_the_ladder_and_the_
   agent_fault_path` -- a reflect iteration that times out
   (`REFLECT_TAIL_TIMEOUT_S`) with a stale `report.md` already on disk never
   reaches `enforce_contract` (zero re-ask calls, no `contract.violation`/
   `contract.agent_fault` event), is classified `REFLECT_FAULT_FATAL` (a
   class spelled nothing like `contract.agent_fault`), and emits a
   `reflect_engine_cutoff` event naming the clock (`REFLECT_TAIL_TIMEOUT_S`)
   and its value. The job's own terminal `state`/`verdict`/`reason` --
   settled strictly before `_run_reflection` ever runs -- are untouched
   (C.7).
2. `test_the_ordinary_reflect_success_path_is_unchanged` -- an ordinary
   reflect iteration that runs to completion with a valid contract payload
   is recorded exactly as before this task: `reflect.ok is True`, no
   `reflect_engine_cutoff` event, no ladder re-ask, terminal state/verdict/
   reason untouched.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.contract_enforcement import EVENT_AGENT_FAULT, EVENT_VIOLATION
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, read_events


def _bare_supervisor(root: Path, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    cfg = JobConfig(run_id="task006-reflect-cutoff", reflect=True,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    **cfg_kw)
    return LoopSupervisor(cfg, run, root)


class _CutoffReflectRunner:
    """Reflect's own subprocess is killed by the engine (`timed_out=True`,
    no parseable output) exactly like a `REFLECT_TAIL_TIMEOUT_S` firing
    would leave it. Any call made with `phase is None` is a `_ask_contract_
    fix` re-ask -- recording it lets the test assert the ladder was never
    entered at all (zero such calls), not merely that it failed."""

    def __init__(self, sup: LoopSupervisor):
        self.sup = sup
        self.reflect_calls: list[dict] = []
        self.fix_calls: list[dict] = []

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        if phase == "reflect":
            self.reflect_calls.append(kw)
            return IterationResult(exit_code=None, timed_out=True, final_text="")
        self.fix_calls.append(kw)
        return IterationResult(exit_code=0, final_text="should never be asked for")


class _OrdinaryReflectRunner:
    """A reflect attempt that runs cleanly and reports a valid contract
    payload on the first try -- the ladder's "already valid, nothing to
    repair" path (`_reflect_contract_ladder`'s own docstring)."""

    def __init__(self, sup: LoopSupervisor):
        self.sup = sup
        self.reflect_calls: list[dict] = []
        self.fix_calls: list[dict] = []

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        if phase == "reflect":
            self.reflect_calls.append(kw)
            outdir = self.sup.run.artifacts_dir / "reflection"
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "report.md").write_text("# stub reflection report\n")
            return IterationResult(
                exit_code=0,
                final_text=json.dumps({"report_written": True,
                                        "suggestions_written": False}))
        self.fix_calls.append(kw)
        return IterationResult(exit_code=0, final_text="should never be asked for")


def _terminal_marker(sup: LoopSupervisor) -> dict:
    """The job's own settled-but-not-yet-final-terminal record (task 012,
    #56): `state: finishing`, `pendingTerminalState`, `verdict`, `reason` --
    exactly the shape `_finish()` leaves behind strictly before
    `_run_reflection` is ever called. C.7 requires none of this move once
    reflect runs."""
    fields = {"state": "finishing", "pendingTerminalState": "failed",
              "verdict": None, "reason": "the work's own settled reason"}
    sup.run.update_status(**fields)
    return fields


def test_a_reflect_killed_at_the_tail_timeout_skips_the_ladder_and_the_agent_fault_path(
        tmp_path):
    # Task 018 (#5)'s reflect-only outage budget (REFLECT_OUTAGE_BUDGET_S)
    # would otherwise retry a `timed_out`/no-traffic result through the
    # infra-retry wrapper (`_run_iteration_with_infra_retry`, since reflect
    # is one of `INFRA_RETRY_PHASES`) for up to that budget's real wall
    # clock before ever returning -- unrelated to what this test is about.
    # `infra_outage_budget_s=0.0` makes `_outage_budget_for("reflect")`
    # (`min(budget, REFLECT_OUTAGE_BUDGET_S)`) zero, so the wrapper's own
    # `budget_spent` check fires on attempt 1 and returns the SAME
    # `timed_out=True` result immediately, with no backoff wait at all.
    sup = _bare_supervisor(tmp_path / "run", job_timeout_s=3600,
                           infra_outage_budget_s=0.0)
    sup.runner = _CutoffReflectRunner(sup)  # type: ignore[assignment]
    sup.deadline = time.monotonic() - 10_000.0

    # A stale report.md already on disk -- from an earlier attempt, or a
    # partial write the killed process itself left behind -- is exactly the
    # shape that used to slip past `_reflect_contract_ladder`'s two guards
    # (`result is None`, "no report file") and into `enforce_contract`.
    outdir = sup.run.artifacts_dir / "reflection"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# a stale report from an earlier attempt\n")

    marker = _terminal_marker(sup)

    asyncio.run(sup._run_reflection())

    # -- the ladder was never entered at all --------------------------------
    assert sup.runner.fix_calls == []
    events = read_events(sup.run.root)
    assert [e for e in events if e.get("type") in (EVENT_VIOLATION, EVENT_AGENT_FAULT)] == []

    # -- a distinct fault class, never spelled like contract.agent_fault ---
    status = sup.run.read_status()
    reflect = status["reflect"]
    assert reflect["ok"] is False
    assert reflect["errorClass"] == LoopSupervisor.REFLECT_FAULT_FATAL
    assert reflect["errorClass"] != EVENT_AGENT_FAULT
    assert EVENT_AGENT_FAULT not in reflect["error"]

    # -- the clock that fired is named, with its value ----------------------
    cutoff_events = [e for e in events if e.get("type") == "reflect_engine_cutoff"]
    assert len(cutoff_events) == 1
    cutoff = cutoff_events[0]
    assert cutoff["reason"] == "timed_out"
    assert cutoff["clock"] == "REFLECT_TAIL_TIMEOUT_S"
    assert cutoff["clock_value"] == pytest.approx(LoopSupervisor.REFLECT_TAIL_TIMEOUT_S)

    # -- C.7: the job's own terminal record is untouched ---------------------
    for key, value in marker.items():
        assert status[key] == value, key


def test_the_ordinary_reflect_success_path_is_unchanged(tmp_path):
    sup = _bare_supervisor(tmp_path / "run", job_timeout_s=3600)
    sup.runner = _OrdinaryReflectRunner(sup)  # type: ignore[assignment]
    sup.deadline = time.monotonic() - 10_000.0

    marker = _terminal_marker(sup)

    asyncio.run(sup._run_reflection())

    assert sup.runner.fix_calls == []  # already valid -- nothing to repair
    events = read_events(sup.run.root)
    assert [e for e in events if e.get("type") in (EVENT_VIOLATION, EVENT_AGENT_FAULT)] == []
    assert [e for e in events if e.get("type") == "reflect_engine_cutoff"] == []

    status = sup.run.read_status()
    assert status["reflect"]["ok"] is True
    assert "errorClass" not in status["reflect"]
    assert not (sup.run.artifacts_dir / "reflection" / "FAILED.md").exists()

    # -- C.7, checked on the success path too --------------------------------
    for key, value in marker.items():
        assert status[key] == value, key
