"""#94 (operator-added scope, steer 003 item 3): the in-flight budget knobs
`ralphctl budget`/`PATCH /config/budget` do NOT yet cover -- `max_approaches`
and `iteration_timeout_s` (the job-deadline knob is a separate, larger piece,
not landed by this commit -- see the module docstring on
`LoopSupervisor.set_iteration_budget`'s two new siblings for what is and is
not covered here).

This module drives ONLY the engine-side setters
(`LoopSupervisor.set_max_approaches`/`set_iteration_timeout`), the same
"rebind the live cfg field, `update_status` where a status.json field
exists" shape `set_iteration_budget` (task 045, #3) already established.
The `PATCH /config/budget` route extension, the CLI flags and the
resume-persistence decision are NOT part of this commit -- #94 is carried
across tasks 004-008 per steer 003, and this is its first increment.

Never calls `_run_iteration_once`/spawns a subprocess -- both setters are
pure field rebinds, tested the same way `set_iteration_budget` itself would
be (direct method calls on a real `LoopSupervisor`, no stubbed runner
needed).
"""

from __future__ import annotations

import time

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir


def _supervisor(tmp_path, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    sup = LoopSupervisor(JobConfig(run_id="task094-budget-setters", **cfg_kw),
                         run, tmp_path)
    run.update_status(state="running")
    return sup


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
