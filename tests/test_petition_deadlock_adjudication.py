"""Task 009 (PRD v0.21 requirement C.1/C.2, #187): a task moving into
`petitioned` triggers the mid-run adjudication episode from that transition
point ITSELF, not merely from a later pass through `_step_worker`'s own
`while` loop.

`_verify_task` is the ONE place in the engine that ever writes
`t["status"] = "petitioned"` (validation-attempt exhaustion against a
contract that declares a petition protocol, `petitions.
contract_declares_petition_protocol`). Before this task, the bounded,
off-budget mid-run adjudication episode
(`LoopSupervisor._maybe_run_midrun_adjudication_episode`) was only ever
reached from two OTHER call sites inside `_step_worker`: the
`pending_verify` gate (before the vigilant verify sub-cycle even starts)
and the `selected is None` "held" branch (a LATER pass through
`_step_worker`'s own `while self.budget_left():` loop, reached only if
that loop has budget left to take another pass at all). `_verify_task` now
calls the episode itself, synchronously, the moment it writes the
`petitioned` status -- see `loop.py`'s `became_petitioned` local in
`_verify_task`.

This module builds the EXACT deadlock shape #187 describes: one
`petitioned` task, and every other open task in the plan reaching it
through `dependsOn` -- directly (002, 004) or transitively (003, through
002) -- then asserts:

1. calling `_verify_task` to exhaustion IN ISOLATION (never touching
   `_step_worker` at all) already dispatches a `review`-phase adjudication
   iteration before that call even returns -- so the trigger is the
   TRANSITION itself, not some later scan of the plan;
2. driving the SAME deadlock end to end through `_step_worker` shows the
   adjudication (`review`) phase dispatched IMMEDIATELY off the exhausting
   `verify` call -- no `worker` phase call (no-task or otherwise)
   intervenes between the transition and the episode -- "dispatches
   instead of a no-task worker iteration", the exact wording of this
   task's own success criteria.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import VALIDATION_ATTEMPT_LIMIT, atomic_write_json
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor

TASK_ID = "001"
DIRECT_DEP_ID = "002"
TRANSITIVE_DEP_ID = "003"
SECOND_DIRECT_DEP_ID = "004"

HANDOFF_TEXT = "criterion 2 cannot be satisfied as written"

# The contract text that opts a run into the petition protocol -- the same
# discriminant `tests/test_petitioned_status_routing.py` pins.
PROTOCOL_CONTRACT = """# PRD v0-test

## Petition protocol

A task whose criteria cannot be satisfied as written files a petition per
`docs/petition-format.md`; an exhausted task routes to `petitioned`.
"""


def _seed_petitions_file(root) -> None:
    (root / "prd.md").write_text(PROTOCOL_CONTRACT)
    reports = root / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "test-petitions.md").write_text(
        "# Test wave petitions\n\nNo petition filed yet.\n")


def _deadlock_plan() -> list[dict]:
    """One `petitioned`-bound task (001, `awaiting-validation`, about to
    exhaust) with every OTHER open task reaching it through `dependsOn`:
    002 and 004 directly, 003 transitively through 002 -- the exact shape
    #187 describes, "every other open task reaching it through `dependsOn`,
    directly or transitively"."""
    return [
        {"id": TASK_ID, "title": "make the impossible thing work",
         "status": "awaiting-validation", "successCriteria": "criterion 2 holds"},
        {"id": DIRECT_DEP_ID, "title": "build on it", "status": "pending",
         "dependsOn": [TASK_ID]},
        {"id": TRANSITIVE_DEP_ID, "title": "build on that", "status": "pending",
         "dependsOn": [DIRECT_DEP_ID]},
        {"id": SECOND_DIRECT_DEP_ID, "title": "also build on it",
         "status": "pending", "dependsOn": [TASK_ID]},
    ]


def _rejected(sup, prompt) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(
        {"task_id": TASK_ID, "verdict": "rejected", "reason": HANDOFF_TEXT})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _adjudicated(sup, prompt) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(
        {"verdict": "adjudicated", "petition_adjudications": []})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _worker_reply(sup, prompt) -> IterationResult:
    """The engine -- not the worker -- decides which task a worker
    iteration works on, and persists an `in-progress` claim to disk BEFORE
    the runner is ever invoked (fact 9, `_step_worker`'s own docstring).
    So the scripted worker just reads that claim back and reports
    `complete` for whichever task is currently `in-progress` -- the
    ordinary vigilant retry cycle (`validation-failed` -> re-claimed ->
    `complete` -> `awaiting-validation` -> `verify` again) -- or
    `no_verdict` when nothing is claimed at all (the deadlocked pass)."""
    in_progress = [t for t in sup.run.read_tasks().get("tasks", [])
                   if t.get("status") == "in-progress"]
    r = IterationResult(exit_code=0)
    if in_progress:
        r.final_text = json.dumps(
            {"verdict": "complete", "task_id": in_progress[0]["id"]})
    else:
        r.final_text = json.dumps({"verdict": "no_verdict"})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _supervisor(root, **cfg):
    sup = make_supervisor(
        root, run_id="task009-petition-deadlock", cfg_defaults=False,
        iterations=20, max_approaches=1, vigilant=True, on_complete="exit",
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=0.0, approach=1, **cfg)
    _seed_petitions_file(root)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": _deadlock_plan()})
    return sup


def test_verify_task_itself_dispatches_adjudication_on_exhaustion(tmp_path):
    """`_verify_task`, called on its own -- `_step_worker`'s own `while`
    loop is never entered at all -- already runs the adjudication episode
    before its last (exhausting) call returns. Proves C.1: the TRANSITION
    into `petitioned` is the trigger point, not a later scan for a
    deadlocked plan."""
    sup = _supervisor(tmp_path / "run")
    handlers = {"verify": [_rejected], "review": [_adjudicated]}
    attach_runner(sup, ScriptedRunner(handlers))

    seen_review_before_return = []
    for _ in range(VALIDATION_ATTEMPT_LIMIT):
        task = sup.run.read_tasks()["tasks"][0]
        ok = asyncio.run(sup._verify_task(task))
        assert ok is False
        seen_review_before_return.append("review" in sup.runner.phases_seen)

    final = sup.run.read_tasks()["tasks"][0]
    assert final["status"] == "petitioned"
    # The review (adjudication) phase never ran until the LAST call -- the
    # one that actually wrote `petitioned` -- and it ran by the time THAT
    # call returned, never needing a separate `_step_worker` pass.
    assert seen_review_before_return == [False, False, True]
    assert sup.runner.phases_seen[-1] == "review"


def test_step_worker_dispatches_adjudication_not_a_no_task_worker_iteration(tmp_path):
    """The full deadlock shape, driven end to end through `_step_worker`:
    every other open task (002, 003, 004) reaches the exhausting task (001)
    through `dependsOn`, directly or transitively. The adjudication episode
    dispatches (`review`) immediately after the last `verify` -- no no-task
    `worker` iteration is spent in between."""
    sup = _supervisor(tmp_path / "run")
    handlers = {"verify": [_rejected], "review": [_adjudicated],
                "worker": [_worker_reply]}
    runner = attach_runner(sup, ScriptedRunner(handlers))

    asyncio.run(sup._step_worker())

    phases = runner.phases_seen
    assert "review" in phases, (
        "the mid-run adjudication episode never dispatched at all")
    review_ix = phases.index("review")
    # The call immediately BEFORE the adjudication is the exhausting
    # `verify` that wrote `petitioned` -- nothing intervenes, so in
    # particular no no-task `worker` iteration was spent spinning on the
    # deadlock between the transition and the episode dispatching.
    assert phases[review_ix - 1] == "verify", (
        "the adjudication episode did not dispatch immediately off the "
        "verify call that exhausted the task into `petitioned` -- "
        f"phases: {phases!r}")

    final = sup.run.read_tasks()["tasks"][0]
    assert final["status"] == "petitioned"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
