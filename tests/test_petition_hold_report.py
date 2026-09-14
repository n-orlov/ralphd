"""Task 010 (PRD v0.21 requirement C.3, #149): when no task is selectable
because a petitioned task holds the graph, the engine reports it on an
operator-facing surface -- the SAME `operator_escalation` event +
`self.notifier` seam every other pause/escalation in `loop.py` already
uses (`_pause_and_escalate_incurable_rejection` et al.) -- exactly once,
naming the held (petitioned) task and its dependents. A second
unselectable pass, whether that is a second lap of `_step_worker`'s own
`while` loop inside the SAME call or an entirely fresh process resuming
the same run, produces no repeat of that report.

Dedup is read back from `events.jsonl` itself
(`LoopSupervisor._petition_hold_already_reported`), the same seam
`_second_exhaustion_already_escalated` already uses for its own
one-shot escalation -- never an in-memory flag a fresh process would
forget.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import state
from ralphd.engine.runner import IterationResult
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor

HELD_TASK_ID = "001"
DIRECT_DEP_A = "002"
DIRECT_DEP_B = "003"


def _held_plan() -> list[dict]:
    """`001` is `petitioned` (held, not dead); `002` and `003` are its
    only two dependents, both direct, both pending -- the ENTIRE
    actionable remainder of this tiny plan, so nothing is ever
    selectable until an adjudication resolves `001`."""
    return [
        {"id": HELD_TASK_ID, "title": "the impossible one",
         "status": "petitioned", "successCriteria": "criterion 2 holds"},
        {"id": DIRECT_DEP_A, "title": "build on it",
         "status": "pending", "dependsOn": [HELD_TASK_ID]},
        {"id": DIRECT_DEP_B, "title": "also build on it",
         "status": "pending", "dependsOn": [HELD_TASK_ID]},
    ]


def _no_verdict_worker(sup, prompt) -> IterationResult:
    """Nothing is ever `in-progress` in this plan (nothing is ever
    selectable), so the scripted worker just reports `no_verdict` every
    single pass, exactly what a genuinely held plan is supposed to get
    back."""
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "no_verdict"})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _supervisor(root, **cfg):
    sup = make_supervisor(
        root, run_id="task010-petition-hold-report", cfg_defaults=False,
        max_approaches=1, vigilant=False, on_complete="exit",
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=0.0, approach=1, tasks=_held_plan(), **cfg)
    return sup


def _petition_held_events(root) -> list[dict]:
    return [ev for ev in state.read_events(root, types={"operator_escalation"})
            if ev.get("reason") == "petition_held"]


def test_petition_hold_reports_once_across_two_held_passes(tmp_path):
    """Budget for exactly TWO worker iterations, both held -- the second
    pass through `_step_worker`'s own `while` loop must not repeat the
    report the first pass already made."""
    sup = _supervisor(tmp_path / "run", iterations=2)
    attach_runner(sup, ScriptedRunner({"worker": [_no_verdict_worker]}))

    asyncio.run(sup._step_worker())

    events = _petition_held_events(sup.run.root)
    assert len(events) == 1, (
        f"expected exactly one petition_held escalation across two held "
        f"passes, got {len(events)}: {events!r}")
    ev = events[0]
    assert ev["heldTaskIds"] == [HELD_TASK_ID]
    assert ev["dependentTaskIds"] == sorted([DIRECT_DEP_A, DIRECT_DEP_B])
    assert HELD_TASK_ID in ev["escalation"]
    assert DIRECT_DEP_A in ev["escalation"]
    assert DIRECT_DEP_B in ev["escalation"]

    # `notifier` (the operator-facing text) is what a real notifier gets
    # handed, exactly like the pause/escalation sites this reuses.
    sent = []
    sup.notifier = sent.append
    # A THIRD pass, still held, still within budget-exhausted territory --
    # nothing left to spend, so this call is a no-op either way, but the
    # dedup must never fire the notifier retroactively either.
    asyncio.run(sup._step_worker())
    assert sent == []
    assert len(_petition_held_events(sup.run.root)) == 1


def test_petition_hold_report_does_not_repeat_on_a_fresh_process(tmp_path):
    """The dedup survives a process restart: a SECOND `LoopSupervisor`
    built fresh over the same run dir (the shape a real engine restart
    takes) must not re-send the report the first process already made,
    because it is read back from `events.jsonl`, never an in-memory flag."""
    root = tmp_path / "run"
    sup1 = _supervisor(root, iterations=1)
    sent1 = []
    sup1.notifier = sent1.append
    attach_runner(sup1, ScriptedRunner({"worker": [_no_verdict_worker]}))
    asyncio.run(sup1._step_worker())

    assert len(sent1) == 1
    assert len(_petition_held_events(root)) == 1

    sup2 = _supervisor(root, iterations=2)
    sent2 = []
    sup2.notifier = sent2.append
    attach_runner(sup2, ScriptedRunner({"worker": [_no_verdict_worker]}))
    asyncio.run(sup2._step_worker())

    assert sent2 == [], (
        "a fresh process resuming the same held run re-sent the "
        "petition-held report instead of reading the earlier one back "
        "off events.jsonl")
    assert len(_petition_held_events(root)) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
