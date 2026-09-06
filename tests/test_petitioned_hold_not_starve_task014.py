"""Task 014 (PRD requirement C.2, #149): a `petitioned` dependency HOLDS
the run, it does not STARVE it.

Task 013 (requirement C.1) made a dependent of a `petitioned` task
*visible* on every surface but left the worker loop's own behaviour
untouched: a pass whose only unscheduled work waits on a `petitioned`
ancestor still fell straight through to the ordinary "nothing selectable"
worker dispatch, and after three such passes with no `tasks.json` change
the stagnation counter (`LoopSupervisor._step_worker`) tripped exactly as
if the plan were genuinely stuck -- failing (or replanning) the approach
over a hold that was always going to resolve on its own once an
adjudication landed.

Two fixes, both in `LoopSupervisor._step_worker`:

1. **Held, not starved.** When `scheduler.select_next_task` returns
   `None` and the entire actionable remainder (`state.task_is_actionable`)
   is blocked on a `petitioned` ancestor (`state.task_blocked_by(...,
   include_petitioned=True)`, task 013's own derivation, read here for a
   fifth purpose), this pass's stagnation bookkeeping is skipped entirely
   -- `stagnant` is neither incremented nor reset, the exact same carve-
   out shape `_last_fault_class in ("infra", "signal")` already uses for
   "nothing was learned this pass" (never "progress was made this pass").
   The worker is still dispatched (a held plan still gets its ordinary
   chance to answer `plan_complete`/report a fresh petition/etc.) --
   only the STAGNATION counter is exempted, so a run that holds for many
   passes in a row never trips guard 10 (`stagnation_exhausted`) purely
   because of the hold.
2. **Immediate adjudication.** The SAME bounded, off-budget mid-run
   adjudication episode the `pending_verify` branch already runs before
   its own `_verify_task` calls (`_maybe_run_midrun_adjudication_
   episode`) is triggered right at the point the hold is detected --
   proactively, rather than waiting for a `pending_verify` cycle that
   will never arrive while nothing is being worked on at all. Already
   deduped per obligation (`self._petition_adjudication_seen`), so
   calling it on every held pass costs nothing once the bounded episode
   has actually run once for a given obligation set.

Driven the same way `tests/test_midrun_adjudication_episode.py` already
does: a phase-dispatching scripted agent through the real
`LoopSupervisor._step_worker()` (no subprocess, milliseconds), reusing
that module's own scaffolding rather than a second copy.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.state import atomic_write_json

from test_midrun_adjudication_episode import (
    OBLIGATION,
    _confirming_review,
    _make_petitions_file,
    _ok,
    _ScriptedAgent,
    _worker_supervisor,
)

__all__ = []


def _held_plan() -> list[dict]:
    """`A` is `petitioned` (held, not dead) with a matching filed
    obligation; `B` depends on it directly and is the plan's only other
    task -- so the entire actionable remainder (just `B`) is blocked on
    the petitioned ancestor and `scheduler.select_next_task` returns
    `None` every single pass, exactly the scenario this task's fix
    targets."""
    return [
        {"id": "A", "title": "the petitioned task", "status": "petitioned",
         "successCriteria": "n/a"},
        {"id": "B", "title": "depends on A", "status": "pending",
         "dependsOn": ["A"], "successCriteria": "n/a"},
    ]


def _no_verdict(sup, prompt: str = "") -> "IterationResult":  # noqa: F821
    return _ok(json.dumps({"verdict": "no_verdict"}))


def test_a_held_pass_runs_the_episode_and_still_dispatches_the_worker(tmp_path):
    """Node id: test_a_held_pass_runs_the_episode_and_still_dispatches_the_worker
    -- the immediate-adjudication half of requirement C.2."""
    sup, run, workspace = _worker_supervisor(tmp_path, iterations=1)
    _make_petitions_file(workspace, OBLIGATION)
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": _held_plan()})

    agent = _ScriptedAgent({
        "worker": [_no_verdict],
        "review": [_confirming_review],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # The mid-run adjudication episode ran, on the review-hat's model,
    # BEFORE this pass's ordinary worker dispatch -- proactively, at the
    # point the hold was detected, not merely as a side effect of some
    # other branch.
    assert agent.calls.get("review") == 1
    assert agent.model_calls["review"][0] == sup.cfg.model_for("review")
    assert "review" in agent.order and "worker" in agent.order
    assert agent.order.index("review") < agent.order.index("worker")

    # The worker was NOT skipped ("instead of idling" replaces the
    # episode's own dispatch point, not the worker's ordinary one) --
    # a held plan still gets its normal chance to reply.
    assert agent.calls.get("worker") == 1

    # Off-budget: the episode never competed with the run's own iteration
    # budget (iterations=1 covered exactly the one worker dispatch).
    assert sup.run.read_status().get(
        "iterationsRefunded", {}).get("grace", 0) >= 1
    charged = (sup.iterations_used - sup._infra_refunded
              - sup._grace_refunded - sup._steer_refunded)
    assert charged == 1

    # tasks.json is untouched by the hold itself -- A is still `petitioned`
    # (closing it on a CONFIRMED adjudication is a later task's scope).
    tasks = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert tasks == {"A": "petitioned", "B": "pending"}


def test_repeated_held_passes_never_trip_the_stagnation_counter(tmp_path):
    """Node id:
    test_repeated_held_passes_never_trip_the_stagnation_counter -- the
    stagnation-exemption half of requirement C.2. Five held passes in a
    row, each with `tasks.json` unchanged, no commit, no contract
    progress and no tool activity -- exactly the shape that trips
    `stagnant >= 3` (guard 10, `stagnation_exhausted`) for an ordinary
    worker loop -- must NOT fail or replan this approach; the run simply
    runs its ordinary iteration budget out and tails off."""
    sup, run, workspace = _worker_supervisor(
        tmp_path, iterations=5, max_approaches=2)
    _make_petitions_file(workspace, OBLIGATION)
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": _held_plan()})

    agent = _ScriptedAgent({
        "worker": [_no_verdict],
        "review": [_confirming_review],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    outcome = asyncio.run(sup._step_worker())

    # Every one of the 5 charged iterations dispatched the ordinary
    # worker -- none of them was skipped -- and NONE of them tripped
    # guard 10: the outcome is the ordinary budget-exhaustion tail, never
    # an approach failure/replan over a hold that was always going to
    # resolve on its own.
    assert agent.calls.get("worker") == 5
    assert outcome == ("tail",)

    # The bounded episode ran exactly ONCE for this obligation -- calling
    # it again on every later held pass is a no-op, never a repeat LLM
    # call (`self._petition_adjudication_seen` dedup, unchanged by this
    # task).
    assert agent.calls.get("review") == 1

    # A held run never gets silently failed/replanned: `self._approach`
    # is exactly where it started (a `stagnation_exhausted` outcome would
    # have returned "advance"/"replan" above instead), and tasks.json is
    # exactly where it started too.
    assert sup._approach == 1
    tasks = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert tasks == {"A": "petitioned", "B": "pending"}
