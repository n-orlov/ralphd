"""v0.38 A.4/A.5 (#306, #320): every unsettled-petition obstacle is handled on
the first pass, not only a direct dependent with an adjudicable petition.

Beside tests/test_petition_hold_prompt_adjudication.py (direct dependents, a
filed petition), these cover the same rule for:

* a dependency CHAIN (a task waiting on a task that waits on the petitioned
  one is held too);
* a hold mixed with a dead-doomed task in the remaining set;
* a petitioned task with no petition on file, so nothing can be adjudicated.

In each, the engine adjudicates what it can, reselects when the hold moves and
parks with the `petition_hold` question otherwise: never a no-task worker
iteration, never the final review.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import state
from tests.conftest import ScriptedRunner, attach_runner
from tests.test_petition_hold_prompt_adjudication import (
    DEP_A, HELD, OBLIGATION, TITLE, _Parked, _completing_worker, _judge,
    _no_task_worker, _park_recorder, _run_to_park, _supervisor)


def _chain_plan() -> list[dict]:
    return [
        {"id": HELD, "title": TITLE, "status": "petitioned",
         "successCriteria": "criterion 2 holds"},
        {"id": DEP_A, "title": "direct", "status": "pending",
         "dependsOn": [HELD], "successCriteria": "a builds"},
        {"id": "004", "title": "one hop further", "status": "pending",
         "dependsOn": [DEP_A], "successCriteria": "c builds"},
        {"id": "005", "title": "two hops further", "status": "pending",
         "dependsOn": ["004"], "successCriteria": "d builds"},
    ]


def _with_plan(tmp_path, plan, *, filed=True):
    sup = _supervisor(tmp_path)
    doc = sup.run.read_tasks()
    doc["tasks"] = plan
    sup.run.write_tasks(doc)
    if not filed:
        (sup.workspace / "artifacts" / "reports"
         / "v0.38-petitions.md").unlink()
    return sup


def test_a_chain_hold_adjudicates_then_parks_without_a_no_task_worker(tmp_path):
    sup = _with_plan(tmp_path, _chain_plan())
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == ["review"], runner.phases_seen
    assert snapshot["state"] == "awaiting_input"
    question = snapshot["inputQuestion"]
    assert question["kind"] == "petition_hold"
    held = question["heldTasks"]
    assert [h["taskId"] for h in held] == [HELD]
    assert held[0]["dependents"] == [DEP_A, "004", "005"]


def test_a_chain_hold_confirmed_reselects_the_head_of_the_chain(tmp_path):
    sup = _with_plan(tmp_path, _chain_plan())
    seen: list = []
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([{"obligation": OBLIGATION,
                            "adjudication": "CONFIRMED"}])],
        "worker": [_completing_worker(seen)]}))
    _park_recorder(sup)

    asyncio.run(asyncio.wait_for(sup._step_worker(), 30.0))

    assert runner.phases_seen[:2] == ["review", "worker"], runner.phases_seen
    assert seen[0]["claimed"] == [DEP_A], seen
    statuses = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert statuses["004"] == "completed" and statuses["005"] == "completed"
    assert runner.phases_seen.count("review") == 1


def test_a_hold_beside_a_dead_doomed_task_still_parks(tmp_path):
    plan = _chain_plan() + [
        {"id": "006", "title": "dead", "status": "skipped",
         "successCriteria": "n/a"},
        {"id": "007", "title": "doomed", "status": "pending",
         "dependsOn": ["006"], "successCriteria": "never"},
    ]
    sup = _with_plan(tmp_path, plan)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == ["review"], runner.phases_seen
    assert snapshot["inputQuestion"]["kind"] == "petition_hold"


def test_an_unfiled_petition_parks_instead_of_idling_a_worker(tmp_path):
    """No petition text on file: the judge has nothing to adjudicate, so the
    hold cannot be settled by it and the run parks for the operator."""
    sup = _with_plan(tmp_path, _chain_plan(), filed=False)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == [], runner.phases_seen
    assert snapshot["state"] == "awaiting_input"
    assert snapshot["inputQuestion"]["kind"] == "petition_hold"
    assert snapshot["inputQuestion"]["heldTasks"][0]["taskId"] == HELD
    assert snapshot.get("pauseReason") == "petition_hold"


def test_an_unfiled_direct_hold_parks_too(tmp_path):
    sup = _supervisor(tmp_path)
    (sup.workspace / "artifacts" / "reports"
     / "v0.38-petitions.md").unlink()
    runner = attach_runner(sup, ScriptedRunner({"worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == [], runner.phases_seen
    assert snapshot["inputQuestion"]["kind"] == "petition_hold"


def test_chain_map_names_the_petitioned_root_for_every_member():
    tasks = _chain_plan()
    one_hop = state.task_blocked_by(tasks, include_petitioned=True)
    assert set(one_hop) == {DEP_A}
    chain = state.task_blocked_by_chain(tasks)
    assert chain == {DEP_A: [HELD], "004": [HELD], "005": [HELD]}


def test_chain_map_ignores_settled_finished_and_unheld_branches():
    tasks = _chain_plan() + [
        {"id": "008", "title": "free", "status": "pending",
         "successCriteria": "x"},
        {"id": "009", "title": "on free", "status": "pending",
         "dependsOn": ["008"], "successCriteria": "y"},
    ]
    chain = state.task_blocked_by_chain(tasks)
    assert "008" not in chain and "009" not in chain
    assert state.task_blocked_by_chain(tasks, settled={HELD}) == {}
    done = [dict(t, status="completed") if t["id"] == "004" else t
            for t in tasks]
    assert "004" not in state.task_blocked_by_chain(done)


# --- every root, on every branch (cure-01-01-4 retry) -------------------------
#
# A task held directly by one petitioned task and, through a chain on another
# branch, by a second one is a dependent of BOTH: its blocker list is the union
# of the roots over all of its `dependsOn` branches, never only the direct one.

SECOND = "101"
SECOND_TITLE = "the other impossible one"


def _file_second_petition(sup):
    path = sup.workspace / "artifacts" / "reports" / "v0.38-petitions.md"
    obligation = f"Task {SECOND}: {SECOND_TITLE}"
    with path.open("a") as f:
        f.write(f"\n## Petition: {obligation}\n\n"
                f"**Obligation**: {obligation}\n"
                "**Type**: environmental\n"
                "**Evidence**: no route to the device from this sandbox\n")


def _two_root_plan() -> list[dict]:
    """HELD (001) and SECOND (101) petitioned; 103 waits on HELD; 104 waits
    on SECOND directly and on 103 (so on HELD through a chain)."""
    return [
        {"id": SECOND, "title": SECOND_TITLE, "status": "petitioned",
         "successCriteria": "the device answers"},
        {"id": HELD, "title": TITLE, "status": "petitioned",
         "successCriteria": "criterion 2 holds"},
        {"id": "103", "title": "on held", "status": "pending",
         "dependsOn": [HELD], "successCriteria": "c"},
        {"id": "104", "title": "on both", "status": "pending",
         "dependsOn": [SECOND, "103"], "successCriteria": "d"},
    ]


def _dependents(question) -> dict:
    return {h["taskId"]: h["dependents"] for h in question["heldTasks"]}


def test_a_direct_blocker_does_not_hide_a_chain_blocker_on_another_branch():
    chain = state.task_blocked_by_chain(_two_root_plan())
    assert chain == {"103": [HELD], "104": [HELD, SECOND]}


def test_fruitless_two_root_hold_names_the_transitive_dependent_of_each(tmp_path):
    """The verifier's probe: one fruitless adjudication, zero workers, and
    the question lists 104 under BOTH petitioned tasks."""
    sup = _with_plan(tmp_path, _two_root_plan())
    _file_second_petition(sup)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == ["review"], runner.phases_seen
    question = snapshot["inputQuestion"]
    assert question["kind"] == "petition_hold"
    assert _dependents(question) == {HELD: ["103", "104"], SECOND: ["104"]}
    assert "104" in question["text"].split(f"Petitioned task {HELD}")[1]


def test_two_deep_branches_with_no_petition_on_file_park_naming_both(tmp_path):
    """Not in the finding: no direct edge to either root, each reached two
    hops deep, nothing on file to adjudicate -> park at once, 104 listed
    under both roots, no hat dispatched."""
    plan = [
        {"id": SECOND, "title": SECOND_TITLE, "status": "petitioned",
         "successCriteria": "x"},
        {"id": HELD, "title": TITLE, "status": "petitioned",
         "successCriteria": "y"},
        {"id": "102", "title": "a", "status": "pending",
         "dependsOn": [HELD], "successCriteria": "a"},
        {"id": "103", "title": "b", "status": "pending",
         "dependsOn": [SECOND], "successCriteria": "b"},
        {"id": "105", "title": "a2", "status": "pending",
         "dependsOn": ["102"], "successCriteria": "a2"},
        {"id": "106", "title": "b2", "status": "pending",
         "dependsOn": ["103"], "successCriteria": "b2"},
        {"id": "104", "title": "join", "status": "pending",
         "dependsOn": ["105", "106"], "successCriteria": "j"},
    ]
    sup = _with_plan(tmp_path, plan, filed=False)
    runner = attach_runner(sup, ScriptedRunner({"worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == [], runner.phases_seen
    assert _dependents(snapshot["inputQuestion"]) == {
        HELD: ["102", "104", "105"], SECOND: ["103", "104", "106"]}


def test_a_join_of_a_petitioned_and_a_dead_branch_parks_naming_the_join(tmp_path):
    """Not in the finding: 104 waits on HELD directly and on 105, doomed by a
    skipped 106. Its roots are both, and the park still lists it under HELD.
    (103 waits on HELD alone: without it the whole remainder is dead-doomed,
    which is the doomed-plan route, not a petition hold.)"""
    plan = [
        {"id": HELD, "title": TITLE, "status": "petitioned",
         "successCriteria": "y"},
        {"id": "103", "title": "on held", "status": "pending",
         "dependsOn": [HELD], "successCriteria": "c"},
        {"id": "106", "title": "dead", "status": "skipped",
         "successCriteria": "n/a"},
        {"id": "105", "title": "doomed", "status": "pending",
         "dependsOn": ["106"], "successCriteria": "never"},
        {"id": "104", "title": "join", "status": "pending",
         "dependsOn": [HELD, "105"], "successCriteria": "j"},
    ]
    assert state.task_blocked_by_chain(plan) == {
        "103": [HELD], "105": ["106"], "104": [HELD, "106"]}
    sup = _with_plan(tmp_path, plan)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == ["review"], runner.phases_seen
    assert _dependents(snapshot["inputQuestion"]) == {HELD: ["103", "104"]}


def test_chain_map_terminates_on_a_cycle_and_names_the_root_for_each_member():
    plan = [
        {"id": HELD, "title": TITLE, "status": "petitioned",
         "successCriteria": "y"},
        {"id": "201", "title": "a", "status": "pending",
         "dependsOn": ["202", HELD], "successCriteria": "a"},
        {"id": "202", "title": "b", "status": "pending",
         "dependsOn": ["201"], "successCriteria": "b"},
    ]
    assert state.task_blocked_by_chain(plan) == {"201": [HELD], "202": [HELD]}


def test_spec_states_an_unfiled_hold_parks_with_no_no_task_iteration():
    from pathlib import Path
    spec = (Path(__file__).resolve().parents[1] / "SPEC.md").read_text()
    flat = " ".join(spec.split())
    assert "keeps the earlier bound" not in flat
    assert "three no-task worker iterations, then `petition_held_no_task_bound`" \
        not in flat
    assert ("petition is not on file) cannot be settled by the judge either, so "
            "it parks `petition_hold` the same way on that first pass") in flat
