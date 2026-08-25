"""Task 117 (steering 005, run `selfdev-v09-fsm-loop`): the no-progress
stagnation guard must not count an iteration whose fault class is `"infra"` or
`"signal"` as no-progress evidence.

The live incident this guards against: approach 2 of `selfdev-v09-fsm-loop`
was failed by `LoopSupervisor`'s "3 iterations with no task progress; failing
approach" guard after three worker iterations (94/100/101) that each ended
with a bare in-band `aborted` error, exit 0, real traffic, no abort recorded
-- a shape task 014 (#49 part 2) already meant to read as `"infra"`, except a
120s duration bound (retired by this same task, see
`tests/test_aborted_stream_fault_class.py`) mis-scored all three as `"work"`.
Task 117 has two independent parts: faults.py decides the shape without a
clock (tested there), and THIS module proves the stagnation counter itself
now excludes any iteration classified `"infra"`/`"signal"`, mirroring the
`_instant_failure_streak` gate that already protects the OTHER no-progress
guard for its own, unrelated reason.

Fix B matters independently of fix A: `_run_iteration_with_infra_retry` can
still *return* an `"infra"`-classified result once its outage budget is
spent or `infra_retry_max` is reached -- that single returned result must not
also be scored as stagnation evidence.

Uses the same no-subprocess scripted-runner harness as
`tests/test_failed_task_vocabulary.py` (milliseconds, not an engine
subprocess) so three-iteration sequences can be driven and inspected exactly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json

from test_failed_task_vocabulary import (
    TASK_ID,
    _events,
    _log_messages,
    _ok,
    _plan_pointer,
    _steps,
    _supervisor,
    _task,
    _write_proposal,
)

__all__ = ["TASK_ID"]


def _run(root: Path, handlers: dict, **cfg_kw):
    from ralphd.engine.state import RunDir
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _plan_one(sup, prompt: str = "") -> IterationResult:
    _write_proposal(sup, [_task()])
    return _ok("planned one task\n" + _plan_pointer())


def _signal_iteration(sup, prompt: str = "") -> IterationResult:
    """A worker iteration ended by a signal after reaching the model: real
    traffic, but no exit code of its own (runner.py's own shape for this --
    see IterationResult.interrupted). Touches tasks.json not at all."""
    r = IterationResult(exit_code=None, interrupted=True)
    r.final_text = "partway through when the signal arrived"
    r.usage = {"input": 5, "output": 2, "totalTokens": 7}
    r.duration_s = 45.0
    return r


def _work_noop_iteration(sup, prompt: str = "") -> IterationResult:
    """An ordinary, unremarkable worker iteration that changes nothing --
    the shape the guard exists to catch."""
    return _ok("looked at task 001 and made no change")


def test_three_signal_iterations_do_not_fail_the_approach(tmp_path):
    """The fix, stated directly: three consecutive worker iterations that
    change nothing and end `"signal"` must NOT trip "3 iterations with no
    task progress" and must not fail the approach."""
    root = tmp_path / "run"
    sup, state = _run(root, {
        "planning": _plan_one,
        "worker": _steps(_signal_iteration, _signal_iteration,
                         _signal_iteration),
    }, iterations=5)
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert "failing approach" not in msgs, msgs
    faults = [ev.get("faultClass") for ev in _events(root)
             if ev.get("type") == "iteration.end" and ev.get("phase") == "worker"]
    assert faults and set(faults) == {"signal"}, faults


def test_three_ordinary_work_noop_iterations_still_fail_the_approach(tmp_path):
    """The guard this must NOT weaken: three consecutive ordinary
    `"work"`-classified no-op worker iterations still trip the guard exactly
    as before task 117 -- the INPUT to the gate changed, not its strength."""
    root = tmp_path / "run"
    sup, state = _run(root, {
        "planning": _plan_one,
        "worker": _steps(_work_noop_iteration, _work_noop_iteration,
                         _work_noop_iteration, _work_noop_iteration),
    }, iterations=20)
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    assert (state, sup.run.read_status()["verdict"]) == ("failed", "unverified")


def test_a_mix_of_two_work_noops_and_one_signal_does_not_reach_three(tmp_path):
    """The counter itself must not advance NOR reset on the excluded
    iteration: two ordinary no-progress work iterations (stagnant -> 2)
    followed by one signal-classified no-progress iteration must leave the
    counter at 2, not reset it to 0 and not advance it to 3 -- so a FOURTH
    (ordinary) no-op is what reaches the guard, not a third."""
    root = tmp_path / "run"
    sup, state = _run(root, {
        "planning": _plan_one,
        "worker": _steps(_work_noop_iteration, _work_noop_iteration,
                         _signal_iteration, _work_noop_iteration),
    }, iterations=20)
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    faults = [ev.get("faultClass") for ev in _events(root)
             if ev.get("type") == "iteration.end" and ev.get("phase") == "worker"]
    # Exactly 4 worker iterations ran before the guard fired -- if the signal
    # iteration had reset the counter (the bug this test would have caught),
    # a 5th, 6th and 7th no-op would have been needed instead.
    assert faults == [None, None, "signal", None], faults
