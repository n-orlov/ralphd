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

Task 017 (requirement Q, #96) adds:

5. `test_reflect_ladder_never_extends_the_subprocess_timeout` -- a
   malformed reflect payload now goes through `_reflect_contract_ladder`,
   but the reflect SUBPROCESS itself (`_run_iteration_once`'s one call) is
   made exactly once, still with `timeout_s == REFLECT_TAIL_TIMEOUT_S` --
   the ladder's re-asks are a wholly separate, uncounted channel
   (`_ask_contract_fix`) on `cfg.router_timeout_s`, never a second
   subprocess attempt on the tail budget.
6. `test_reflect_ladder_is_bounded_by_contract_retry_max_not_the_deadline`
   -- with a persistently malformed reflect payload AND every re-ask also
   malformed, the ladder makes exactly `cfg.contract_retry_max` extra
   `_ask_contract_fix` calls, never more, however far behind the deadline
   already is (varied across two deadlines to show the count does not
   move) -- pinning the PRD's "bounded, stated" requirement rather than
   trusting the comment alone. The outcome still lands DEGRADED (v0.12
   requirement G stays true: the run's own state/verdict is never in
   scope here, and a failed reflect stays non-fatal).
7. `test_reflect_ladder_recovers_a_malformed_first_attempt` -- the other
   half: when a re-ask DOES return a valid payload, the ladder recovers it
   and `_record_reflect_outcome` reads the corrected payload as a genuine
   success, not a degraded one -- the whole point of wiring reflect into
   `enforce_contract` in the first place.
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
from tests.conftest import supervisor_with_runner


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


_supervisor = supervisor_with_runner(
    lambda sup: _RecordingRunner(sup),
    run_id="task008-reflect-tail", cfg_defaults=False, status={"state": "starting"},
    reflect=True)


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


# ---------------------------------------------------------------------------
# 5-7. task 017 (requirement Q, #96): the ladder's own tail-budget bound
# ---------------------------------------------------------------------------

class _MalformedReflectRunner:
    """Reflect's own subprocess ALWAYS returns a malformed payload (but
    writes a real report.md -- #87's own shape: ran cleanly, left no valid
    deliverable) exactly once per `_run_reflection()` call. Every
    subsequent call is a `_ask_contract_fix` re-ask: `_run_reflection`
    resets `phase` to `None` before `_reflect_contract_ladder` ever runs,
    so these calls see `phase is None`, never `"reflect"` -- the same key
    `_RecordingRunner` above already reads. `fix_outcomes` is the raw text
    handed back to each successive re-ask in order; once exhausted, further
    calls are recorded but still return malformed text, so a script that
    ran on longer than expected is never accidentally masked as a pass.
    """

    def __init__(self, sup: LoopSupervisor, fix_outcomes: list[str] | None = None):
        self.sup = sup
        self.fix_outcomes = list(fix_outcomes or [])
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
                final_text="not a reflect contract payload at all")
        idx = len(self.fix_calls)
        self.fix_calls.append(kw)
        if idx < len(self.fix_outcomes):
            return IterationResult(exit_code=0, final_text=self.fix_outcomes[idx])
        return IterationResult(exit_code=0, final_text="still not a payload")


def _bare_supervisor(root: Path, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    cfg = JobConfig(run_id="task017-reflect-ladder", reflect=True,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    **cfg_kw)
    return LoopSupervisor(cfg, run, root)


def test_reflect_ladder_never_extends_the_subprocess_timeout(tmp_path):
    sup = _bare_supervisor(tmp_path / "run", job_timeout_s=3600)
    sup.runner = _MalformedReflectRunner(sup)  # type: ignore[assignment]
    sup.deadline = time.monotonic() - 10_000.0

    asyncio.run(sup._run_reflection())

    # the reflect SUBPROCESS is attempted exactly once, no matter how many
    # re-asks the ladder makes afterward -- the ladder is a wholly separate,
    # uncounted channel, never a second subprocess attempt on the tail.
    assert len(sup.runner.reflect_calls) == 1
    assert sup.runner.reflect_calls[0]["timeout_s"] == pytest.approx(
        LoopSupervisor.REFLECT_TAIL_TIMEOUT_S)
    # the ladder DID run (report.md existed, the payload was malformed) --
    # otherwise this test would prove nothing about the ladder at all.
    assert len(sup.runner.fix_calls) >= 1
    for call in sup.runner.fix_calls:
        assert call["timeout_s"] == sup.cfg.router_timeout_s

    status = sup.run.read_status()
    assert status["reflect"]["ok"] is False
    assert status["reflect"]["errorClass"] == LoopSupervisor.REFLECT_FAULT_DEGRADED


def test_reflect_ladder_is_bounded_by_contract_retry_max_not_the_deadline(tmp_path):
    for deadline_offset in (10_000.0, 999_999.0):
        sup = _bare_supervisor(
            tmp_path / f"run-{int(deadline_offset)}", job_timeout_s=3600)
        sup.runner = _MalformedReflectRunner(sup)  # every re-ask still malformed
        sup.deadline = time.monotonic() - deadline_offset

        asyncio.run(sup._run_reflection())

        # an exhausted ladder makes EXACTLY contract_retry_max re-asks,
        # never one more for being further behind the deadline.
        assert len(sup.runner.fix_calls) == sup.cfg.contract_retry_max, (
            f"deadline offset={deadline_offset}")

        status = sup.run.read_status()
        # a failed reflect ladder is still DEGRADED, never FATAL (a failed
        # reflect must not become fatal).
        assert status["reflect"]["ok"] is False
        assert status["reflect"]["errorClass"] == LoopSupervisor.REFLECT_FAULT_DEGRADED
        # v0.12 requirement G: reflect never touches state/verdict on ANY
        # path, ladder or not -- an unparseable reflect (even after a
        # ladder that never recovers it) cannot overwrite the work's
        # verdict, because reflect is never handed the power to set one.
        assert status["state"] == "starting"
        assert status.get("verdict") is None


def test_reflect_ladder_recovers_a_malformed_first_attempt(tmp_path):
    sup = _bare_supervisor(tmp_path / "run", job_timeout_s=3600)
    good_payload = json.dumps({"report_written": True,
                                "suggestions_written": False})
    sup.runner = _MalformedReflectRunner(  # type: ignore[assignment]
        sup, fix_outcomes=[good_payload])
    sup.deadline = time.monotonic() - 10_000.0

    asyncio.run(sup._run_reflection())

    # one re-ask is enough to recover once it returns a valid payload --
    # the ladder must not keep asking past a success.
    assert len(sup.runner.fix_calls) == 1
    status = sup.run.read_status()
    assert status["reflect"]["ok"] is True
    assert "errorClass" not in status["reflect"]
    assert not (sup.run.artifacts_dir / "reflection" / "FAILED.md").exists()
