"""#94 (operator-added scope, steer 003 item 3): the in-flight budget knobs
`ralphctl budget`/`PATCH /config/budget` do NOT yet cover -- `max_approaches`,
`iteration_timeout_s`, and (as of task 002) the job wall-clock deadline.

This module drives ONLY the engine-side setters
(`LoopSupervisor.set_max_approaches`/`set_iteration_timeout`/
`set_job_deadline`), the same "rebind the live state, `update_status` where
a status.json field exists" shape `set_iteration_budget` (task 045, #3)
already established. The `PATCH /config/budget` route extension, the CLI
flags and the resume-persistence decision are NOT part of this commit --
#94 is carried across tasks 002-008 per steer 003, and this module grows by
one increment per landed engine-side setter.

Never calls `_run_iteration_once` -- all three setters are pure attribute
rebinds, tested the same way `set_iteration_budget` itself would be (direct
method calls on a real `LoopSupervisor`). `set_job_deadline`'s own tests
stub `sup.runner` regardless, since a stray `_run_iteration_once` call from
a helper reached through `_step_replan`/`run_iteration` must not silently
spawn a real subprocess.
"""

from __future__ import annotations

import time

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory


_supervisor = supervisor_factory(run_id="task094-budget-setters", cfg_defaults=False,
                                 status={"state": "running"})


def test_set_max_approaches_rebinds_cfg_and_status(tmp_path):
    sup = _supervisor(tmp_path, max_approaches=3)
    sup.set_max_approaches(7)
    assert sup.cfg.max_approaches == 7
    assert sup.run.read_status()["maxApproaches"] == 7
    # GET /config's live read (cfg.effective()) agrees too -- no separate
    # mechanism, same field.
    assert sup.cfg.effective()["budgets"]["maxApproaches"] == 7


def test_set_max_approaches_is_observed_at_the_next_replan_decision(tmp_path):
    """Pins the read site itself: `_step_replan`'s
    `if self._approach < self.cfg.max_approaches:` guard, at
    `src/ralphd/engine/loop.py`. Before the setter runs, the budget is
    already exhausted (`_approach == max_approaches`); after it, the SAME
    approach counter has room again -- proving the change is observed by a
    live read, not just by the accessor that set it."""
    sup = _supervisor(tmp_path, max_approaches=1)
    sup._approach = 1

    exhausted = sup._step_replan()
    assert exhausted == ("tail",)
    assert sup._approach == 1, "an exhausted replan must not bump the approach"

    sup.set_max_approaches(3)
    advanced = sup._step_replan()
    assert advanced == ("advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2


def test_set_iteration_timeout_rebinds_cfg(tmp_path):
    sup = _supervisor(tmp_path, iteration_timeout_s=45 * 60)
    sup.set_iteration_timeout(600)
    assert sup.cfg.iteration_timeout_s == 600
    # No status.json field of its own (unlike iterations/max_approaches) --
    # GET /config's live cfg.effective() read is the only surface, and it
    # already reflects the change with no extra write.
    assert sup.cfg.effective()["budgets"]["iterationTimeoutS"] == 600


def test_set_iteration_timeout_is_observed_by_run_iterations_own_formula(tmp_path):
    """Pins the read site: `run_iteration`'s own
    `timeout = min(self.cfg.iteration_timeout_s, max(...))` -- reproduced
    here (not re-imported, since it is inline in `run_iteration`, not a
    standalone helper) with a deadline far enough out that the configured
    timeout, not the deadline floor, is the binding term both before and
    after the change."""
    sup = _supervisor(tmp_path, iteration_timeout_s=45 * 60,
                      job_timeout_s=8 * 3600)

    def _binding_timeout(phase: str) -> float:
        return min(sup.cfg.iteration_timeout_s,
                   max(sup.REFLECT_TAIL_TIMEOUT_S if phase == "reflect"
                       else 60, int(sup.deadline - time.monotonic())))

    assert _binding_timeout("worker") == 45 * 60
    sup.set_iteration_timeout(90)
    assert _binding_timeout("worker") == 90


def test_set_job_deadline_moves_monotonic_and_epoch_deadlines_together(tmp_path):
    """Task 002 (#94), engine-side setter 3 of 3: `set_job_deadline` must
    move BOTH `self.deadline` (monotonic, `budget_left()`'s own clock) and
    `self._deadline_epoch` (wall clock, published as `deadlineAt`) by the
    SAME delta -- exactly the two-attribute move `_account_infra_wait`
    already performs for an infra extension, applied here for an
    operator-driven change instead. A setter that touched only one of them
    would let `budget_left()` and `GET /status`'s `deadlineAt` disagree
    about how much run time is actually left.
    """
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    before_mono = sup.deadline
    before_epoch = sup._deadline_epoch

    new_epoch = before_epoch + 900.0
    sup.set_job_deadline(new_epoch)

    assert sup._deadline_epoch == new_epoch
    assert sup.deadline == pytest.approx(before_mono + 900.0)


def test_set_job_deadline_republishes_deadline_at_in_status(tmp_path):
    """`deadlineAt` is published once at run start and nowhere else
    automatically -- `set_job_deadline` must rewrite it itself, the same
    way `set_iteration_budget` rewrites `iterationsBudget`, so `GET
    /status` reflects an operator-driven deadline change without waiting
    for the next infra wait to happen to touch the same field."""
    from ralphd.engine.state import utc_from_epoch

    sup = _supervisor(tmp_path, job_timeout_s=3600)
    new_epoch = sup._deadline_epoch + 1800.0

    sup.set_job_deadline(new_epoch)

    assert sup.run.read_status()["deadlineAt"] == utc_from_epoch(new_epoch)


def test_set_job_deadline_emits_an_event_distinguishable_from_infra_extension(tmp_path):
    """An operator-requested deadline change must not be mistaken for an
    automatic infra-outage extension when read back from `events.jsonl` --
    `_account_infra_wait` emits `deadline_extended` with `reason="infra
    wait"`; `set_job_deadline` must emit something else entirely (a
    different event type, not just a different `reason` on the same type),
    and must never emit `deadline_extended` itself. `sup.runner` is stubbed
    even though this setter never touches it, per this module's own rule
    for anything reached through the supervisor rather than a bare
    attribute rebind."""
    from ralphd.engine.state import read_events

    sup = _supervisor(tmp_path, job_timeout_s=3600)
    sup.runner = object()
    new_epoch = sup._deadline_epoch + 300.0

    sup.set_job_deadline(new_epoch)

    events = read_events(sup.run.root)
    deadline_events = [e for e in events if e.get("type") in
                      ("deadline_set", "deadline_extended")]
    assert len(deadline_events) == 1
    ev = deadline_events[0]
    assert ev["type"] == "deadline_set"
    assert ev["type"] != "deadline_extended"
    assert ev.get("reason") != "infra wait"
