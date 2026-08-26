"""Requirement C (#72): the finishing/reflect tail gets wall-clock time it
cannot be robbed of.

`reflect` (PRD req 24) is the run's own account of itself and it is the
first thing a deadline that fires mid-run eats, because the same
`self.deadline` that ends the job also bounds the reflect attempt.
`_run_iteration_once`'s per-attempt timeout used to be

    timeout = min(cfg.iteration_timeout_s, max(60, int(deadline - now)))

for EVERY phase, including reflect -- so once the deadline is already in
the past (the ordinary case: reflect runs strictly after the job decided
its fate), reflect got exactly the 60s floor, nowhere near enough for a
real post-mortem write. `selfdev-v09-fsm-loop` lost its reflection this way:
iterations 183 (review), 184 (planning) and 185 (reflect) all ended
interrupted in a 2m01s window, and `artifacts/reflection/` held only
`FAILED.md`.

The fix (chosen from the PRD's three options -- see
`artifacts/reports/task-008-finishing-tail-budget.md` for the two rejected
ones and why): reflect's own per-attempt timeout is now independent of
`self.deadline` -- `LoopSupervisor.REFLECT_TAIL_TIMEOUT_S` (600s) is a hard
floor for its ONE attempt, still capped by `cfg.iteration_timeout_s` so it
cannot grow without bound. Every other phase keeps the exact old formula.

Tests:

1. `test_reflect_floor_independent_of_an_already_passed_deadline` -- the
   exact `timeout_s` the runner is handed, with the deadline 10000s gone.
2. `test_reflect_floor_is_capped_by_iteration_timeout_s` -- the hard cap: a
   smaller `iteration_timeout_s` still wins, so reflect cannot extend a run
   indefinitely.
3. `test_other_phases_keep_the_plain_60s_floor` -- the fix is reflect-only;
   every other phase's arithmetic is untouched.
4. `test_v09_iteration_185_regression_reflect_past_the_deadline_still_reports`
   -- the named regression, reproducing `selfdev-v09-fsm-loop`'s exact
   shape (deadline already gone before reflect's one attempt) end to end
   through `LoopSupervisor.run_job()`, asserting a real
   `artifacts/reflection/report.md` lands, not `FAILED.md`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir


class _RecordingRunner:
    """Stands in for PiRunner: records every call's kwargs (chiefly
    `timeout_s`, the thing this task changes), and for `reflect` writes a
    real report + valid contract payload so a successful attempt looks
    exactly like a real agent's."""

    def __init__(self, sup: LoopSupervisor):
        self.sup = sup
        self.calls: list[dict] = []

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.calls.append({"phase": phase, **kw})
        if phase == "reflect":
            outdir = self.sup.run.artifacts_dir / "reflection"
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "report.md").write_text("# stub reflection report\n")
            return IterationResult(
                exit_code=0,
                final_text=json.dumps({"report_written": True,
                                        "suggestions_written": False}))
        return IterationResult(exit_code=0, final_text="ok")


def _supervisor(root: Path, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    cfg = JobConfig(run_id="task008-reflect-tail", reflect=True, **cfg_kw)
    sup = LoopSupervisor(cfg, run, root)
    sup.runner = _RecordingRunner(sup)  # type: ignore[assignment]
    return sup


# ---------------------------------------------------------------------------
# 1. the exact timeout handed to the runner
# ---------------------------------------------------------------------------

def test_reflect_floor_independent_of_an_already_passed_deadline(tmp_path):
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    sup.deadline = time.monotonic() - 10_000.0  # long, long gone

    asyncio.run(sup._run_reflection())

    assert [c["phase"] for c in sup.runner.calls] == ["reflect"]
    # The old formula (`max(60, deadline - now)`) would floor this at 60s
    # once the deadline is 10000s behind. The fix's floor is 600s instead.
    assert sup.runner.calls[0]["timeout_s"] == pytest.approx(
        LoopSupervisor.REFLECT_TAIL_TIMEOUT_S)
    assert sup.runner.calls[0]["timeout_s"] > 60

    status = sup.run.read_status()
    assert status["reflect"]["ok"] is True
    assert (sup.run.artifacts_dir / "reflection" / "report.md").exists()
    assert not (sup.run.artifacts_dir / "reflection" / "FAILED.md").exists()


# ---------------------------------------------------------------------------
# 2. the hard cap: cannot extend a run indefinitely
# ---------------------------------------------------------------------------

def test_reflect_floor_is_capped_by_iteration_timeout_s(tmp_path):
    sup = _supervisor(tmp_path, job_timeout_s=3600, iteration_timeout_s=45)
    sup.deadline = time.monotonic() - 999_999.0

    asyncio.run(sup._run_reflection())

    assert sup.runner.calls[0]["timeout_s"] == 45
    assert 45 < LoopSupervisor.REFLECT_TAIL_TIMEOUT_S, (
        "the cap only proves anything if the configured iteration timeout "
        "is genuinely smaller than the reflect floor")


# ---------------------------------------------------------------------------
# 3. every other phase's arithmetic is untouched
# ---------------------------------------------------------------------------

def test_other_phases_keep_the_plain_60s_floor(tmp_path):
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    sup.deadline = time.monotonic() - 10_000.0

    asyncio.run(sup.run_iteration("worker"))

    assert [c["phase"] for c in sup.runner.calls] == ["worker"]
    assert sup.runner.calls[0]["timeout_s"] == 60


# ---------------------------------------------------------------------------
# 4. the named regression, end to end through run_job()
# ---------------------------------------------------------------------------

def test_v09_iteration_185_regression_reflect_past_the_deadline_still_reports(
        tmp_path):
    """Reproduces `selfdev-v09-fsm-loop`'s exact shape: by the time reflect
    gets its one attempt, the job's deadline is already gone (there, review
    183 and planning 184 burned the last of it; here, the deadline is set
    in the past before the job even starts, so `_step_planning`'s own
    `budget_left()` guard sends the job straight to the terminal tail with
    no agent call at all -- the fastest way to reach "reflect is the only
    iteration that runs, with the deadline already behind it"). Before this
    task's fix, reflect's own attempt got the 60s floor same as every other
    phase and this scenario is exactly what starved it; now it must produce
    a real report.
    """
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1, on_complete="exit")
    sup.run.prd_file.write_text("# PRD\n\nDo the thing.\n")
    sup.deadline = time.monotonic() - 5.0

    state = asyncio.run(sup.run_job())

    assert state in ("failed", "aborted")
    assert [c["phase"] for c in sup.runner.calls] == ["reflect"]

    report = root / "artifacts" / "reflection" / "report.md"
    assert report.exists(), (
        "reflect must still leave a real report once the deadline has "
        "already passed (v0.9 iteration 185, selfdev-v09-fsm-loop)")
    assert not (root / "artifacts" / "reflection" / "FAILED.md").exists()

    status = sup.run.read_status()
    assert status["reflect"]["ok"] is True
    # the job's own terminal record is unaffected by the tail fix -- reflect
    # never gets to grant a verdict it did not earn.
    assert status["state"] in ("failed", "aborted")
    assert status["verdict"] == "unverified"
