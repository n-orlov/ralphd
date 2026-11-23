"""Task 011 (PRD v0.21 requirement C.4/C.5, #187): the two things D-shaped
by C.1-C.3 (task 009/010) still leave open on a petition that never gets a
new adjudication outcome.

C.4: `_step_worker`'s own held branch (`petition_held_this_pass`) is
DELIBERATELY exempt from the ordinary `stagnant >= 3` guard -- "held, not
starved" -- which, on its own, left NOTHING but the run's overall
`iterations` budget stopping it from dispatching one no-task worker call
every single pass forever once the one bounded, off-budget mid-run
adjudication episode has already had its shot at the current open
obligation set with no new outcome. `LoopSupervisor.
PETITION_HELD_MAX_NO_TASK_ITERATIONS` (== 3) is the new, SEPARATE bound
this task adds: once that many no-task passes have been spent holding on
the same obligation set, `_step_worker` routes to `review` for a verdict
(the identical `doomed_plan_unverifiable`/`blocked_with_failures` shape,
declared in `transitions.TABLE["worker"]` as `petition_held_no_task_bound`)
instead of dispatching another one.

C.5: `skipped` is never presented to the adjudicating hat as a resolution
alongside CONFIRMED/REFUTED (`_midrun_adjudication_extra`'s own prompt
text), and the declared task-status table adds no edge at all out of
`petitioned` in either FSM variant -- in particular never one back to
`pending`.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import transitions
from ralphd.engine.runner import IterationResult
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor

HELD_TASK_ID = "001"
DEP_A = "002"
DEP_B = "003"


def _held_plan() -> list[dict]:
    """`001` is `petitioned` (held, not dead); `002` and `003` are its
    only two dependents, both DIRECT, both pending -- the entire
    actionable remainder of this plan, so nothing is ever selectable
    until an adjudication resolves `001`."""
    return [
        {"id": HELD_TASK_ID, "title": "the impossible one",
         "status": "petitioned", "successCriteria": "criterion 2 holds"},
        {"id": DEP_A, "title": "build on it", "status": "pending",
         "dependsOn": [HELD_TASK_ID]},
        {"id": DEP_B, "title": "also build on it", "status": "pending",
         "dependsOn": [HELD_TASK_ID]},
    ]


def _no_verdict_worker(sup, prompt) -> IterationResult:
    """Nothing is ever selectable in this plan, so the scripted worker
    just reports `no_verdict` every single pass -- exactly what a
    genuinely held plan is supposed to answer with."""
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "no_verdict"})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _supervisor(root, **cfg):
    return make_supervisor(
        root, run_id="task011-petition-hold-bound", cfg_defaults=False,
        max_approaches=1, vigilant=False, on_complete="exit",
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=0.0, approach=1, tasks=_held_plan(), **cfg)


class _Parked(Exception):
    """Raised by the stubbed park wait: the run reached `awaiting_input`."""


def test_a_petition_only_hold_spends_no_no_task_iteration_and_parks(tmp_path):
    """A run whose ENTIRE actionable remainder (002, 003) is blocked on a
    single petitioned ancestor (001) with no petition on file -- so the
    judge has nothing to adjudicate -- spends NO no-task worker iteration.

    v0.38 A.5 (#306, #320) rewrote this node: it used to pin that such a hold
    dispatched `PETITION_HELD_MAX_NO_TASK_ITERATIONS` no-task worker
    iterations and then routed to `review` (`petition_held_no_task_bound`).
    A hold the engine cannot settle now parks `awaiting_input` with the
    `petition_hold` question first, however large the iteration budget.
    Covered by tests/test_petition_hold_chain_and_unfiled.py::
    test_an_unfiled_direct_hold_parks_too as well."""
    sup = _supervisor(tmp_path / "run", iterations=20)
    runner = attach_runner(sup, ScriptedRunner({"worker": [_no_verdict_worker]}))

    async def parked():
        raise _Parked()

    sup._park_until_resumed = parked
    with pytest.raises(_Parked):
        asyncio.run(asyncio.wait_for(sup._step_worker(), 30.0))

    assert runner.phases_seen == [], runner.phases_seen
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["inputQuestion"]["kind"] == "petition_hold"
    # the guard stays declared for the unchanged-review route (B.2)
    assert transitions.is_legal("worker", "review", vigilant=False)
    assert "petition_held_no_task_bound" in transitions.guard_names(
        "worker", vigilant=False)


def test_skipped_is_never_offered_as_a_petition_resolution_and_no_petitioned_to_pending_edge(tmp_path):
    """C.5, in two independent halves: the adjudication prompt never
    offers `skipped` as a resolution, and the declared task-status table
    adds no successor at all to `petitioned` in either FSM variant --
    which in particular rules out an edge back to `pending`."""
    sup = _supervisor(tmp_path / "run")
    open_petitions = [{
        "obligation": "Task 001: make the impossible thing work",
        "type": "self-referential",
        "evidence": "criterion 2 refers to its own not-yet-existing sha",
    }]

    prompt_extra = sup._midrun_adjudication_extra(open_petitions)

    assert "skipped" not in prompt_extra.lower(), (
        "the mid-run adjudication episode's own instructions must offer "
        "only CONFIRMED/REFUTED as a petition resolution, never `skipped`: "
        f"{prompt_extra!r}")
    assert "CONFIRMED" in prompt_extra
    assert "REFUTED" in prompt_extra

    # The declared table itself, both variants: `petitioned` has NO
    # successors at all, so in particular no edge back to `pending` was
    # added by this wave -- that edge does not exist by design (PRD C.5).
    assert transitions.TASK_STATUS_TABLE["petitioned"] == ()
    assert transitions.task_status_table_for(True)["petitioned"] == ()
    assert transitions.task_status_table_for(False)["petitioned"] == ()
    assert "petitioned" not in transitions.TASK_STATUS_VIGILANT_DELTA
    assert not transitions.is_legal_task_status_transition(
        "petitioned", "pending", vigilant=True)
    assert not transitions.is_legal_task_status_transition(
        "petitioned", "pending", vigilant=False)
    assert not transitions.is_legal_task_status_transition(
        "petitioned", "skipped", vigilant=True)
    assert not transitions.is_legal_task_status_transition(
        "petitioned", "skipped", vigilant=False)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
