"""v0.38 A.4/A.5 (#306, #320) through a phase gate: a pending task of a later
phase is never scheduled while the open phase's gate is closed (v0.37 B.1),
and that gate cannot pass while a task of the open phase waits on an
unsettled petition. So such a task is held by the same petition, with no
`dependsOn` edge naming it, and the engine must treat the remainder as held:
adjudicate on the first pass, reselect when the hold moves, park with the
`petition_hold` question otherwise -- never a no-task worker iteration.
"""

from __future__ import annotations

import asyncio

from ralphd.engine import state
from tests.conftest import ScriptedRunner, attach_runner
from tests.test_petition_hold_prompt_adjudication import (
    HELD, OBLIGATION, TITLE, _completing_worker, _judge, _no_task_worker,
    _park_recorder, _run_to_park, _supervisor)


def _phase(pid: str, gate: str = "closed") -> dict:
    return {"id": pid, "title": f"Phase {pid}", "reviewFocus": pid,
            "gate": gate, "rounds": 0, "records": []}


def _task(tid, phase, status="pending", deps=()):
    return {"id": tid, "title": f"thing {tid}", "phase": phase,
            "status": status, "successCriteria": f"criterion {tid}",
            "dependsOn": list(deps)}


def _two_phase_plan() -> dict:
    """build: 001 petitioned, 311 on 001, 310 on 311. ship: 410, no edges."""
    return {
        "phases": [_phase("build"), _phase("ship")],
        "tasks": [
            dict(_task(HELD, "build", "petitioned"), title=TITLE),
            _task("311", "build", deps=[HELD]),
            _task("310", "build", deps=["311"]),
            _task("410", "ship"),
        ]}


def _three_phase_plan() -> dict:
    """a: 001 petitioned, 120 on 001. b: 210 and 220 (on 210). c: 330."""
    return {
        "phases": [_phase("a"), _phase("b"), _phase("c")],
        "tasks": [
            dict(_task(HELD, "a", "petitioned"), title=TITLE),
            _task("120", "a", deps=[HELD]),
            _task("210", "b"),
            _task("220", "b", deps=["210"]),
            _task("330", "c"),
        ]}


def _with_doc(tmp_path, plan: dict, *, filed=True):
    sup = _supervisor(tmp_path)
    doc = sup.run.read_tasks()
    doc.update(plan)
    sup.run.write_tasks(doc)
    if not filed:
        (sup.workspace / "artifacts" / "reports"
         / "v0.38-petitions.md").unlink()
    return sup


# --- the finding's own shape --------------------------------------------------

def test_a_later_phase_task_does_not_hide_the_hold_and_the_run_parks(tmp_path):
    sup = _with_doc(tmp_path, _two_phase_plan())
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == ["review"], runner.phases_seen
    assert snapshot["state"] == "awaiting_input"
    question = snapshot["inputQuestion"]
    assert question["kind"] == "petition_hold"
    held = question["heldTasks"]
    assert [h["taskId"] for h in held] == [HELD]
    assert held[0]["dependents"] == ["310", "311", "410"]


def test_a_confirmed_hold_behind_a_gate_reselects_the_open_phase(tmp_path):
    sup = _with_doc(tmp_path, _two_phase_plan())
    seen: list = []
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([{"obligation": OBLIGATION,
                            "adjudication": "CONFIRMED"}])],
        "worker": [_completing_worker(seen)]}))
    _park_recorder(sup)

    asyncio.run(asyncio.wait_for(sup._step_worker(), 30.0))

    # One adjudication, then the open phase's chain in order; the review
    # after it is the build gate's own (its tasks are all terminal now), and
    # the gated 410 is never claimed before that gate.
    assert runner.phases_seen[:3] == ["review", "worker", "worker"], (
        runner.phases_seen)
    assert [s["claimed"] for s in seen[:2]] == [["311"], ["310"]], seen
    assert all(s["claimed"] != ["410"] for s in seen), seen
    statuses = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert statuses["410"] == "pending"


# --- further cases of the same rule -------------------------------------------

def test_an_unfiled_hold_gating_two_later_phases_parks_at_once(tmp_path):
    """Nothing to adjudicate, two phases behind the gate: still a hold, parked
    on the first pass without dispatching the judge or a worker."""
    sup = _with_doc(tmp_path, _three_phase_plan(), filed=False)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert runner.phases_seen == [], runner.phases_seen
    question = snapshot["inputQuestion"]
    assert question["kind"] == "petition_hold"
    assert question["heldTasks"][0]["dependents"] == [
        "120", "210", "220", "330"]


def test_the_chain_map_counts_the_gate_as_a_dependency():
    doc = _three_phase_plan()
    chain = state.task_blocked_by_chain(doc["tasks"], phases=doc["phases"])
    assert chain == {"120": [HELD], "210": [HELD], "220": [HELD],
                     "330": [HELD]}
    # Without the phases list there is no gate to wait on.
    assert state.task_blocked_by_chain(doc["tasks"]) == {"120": [HELD]}


def test_no_gate_hold_when_the_open_phase_holds_nothing():
    """The petitioned task's dependents are in a LATER phase; the open phase
    has free work, so nothing in it is held and the gate adds nobody."""
    doc = {
        "phases": [_phase("a"), _phase("b")],
        "tasks": [
            _task("110", "a"),
            dict(_task(HELD, "b", "petitioned"), title=TITLE),
            _task("210", "b", deps=[HELD]),
            _task("220", "b"),
        ]}
    chain = state.task_blocked_by_chain(doc["tasks"], phases=doc["phases"])
    assert chain == {"210": [HELD]}


def test_a_passed_gate_no_longer_holds_its_successor_phase():
    """Once phase a's gate passed, b is open: a held b task gates c, and
    nothing of the finished a is named."""
    doc = {
        "phases": [_phase("a", "passed"), _phase("b"), _phase("c")],
        "tasks": [
            _task("110", "a", "completed"),
            dict(_task(HELD, "b", "petitioned"), title=TITLE),
            _task("210", "b", deps=[HELD]),
            _task("310", "c"),
            _task("320", "c", "in-progress"),
        ]}
    chain = state.task_blocked_by_chain(doc["tasks"], phases=doc["phases"])
    # 320 is already claimed (in-progress is never phase-gated), so it is
    # not held by the gate.
    assert chain == {"210": [HELD], "310": [HELD]}


def test_a_settled_petition_lifts_the_gate_hold_too():
    doc = _two_phase_plan()
    assert state.task_blocked_by_chain(
        doc["tasks"], settled={HELD}, phases=doc["phases"]) == {}


def test_a_gate_held_only_by_a_dead_task_is_not_a_petition_hold(tmp_path):
    """No petitioned root anywhere: the gate still keeps 410 back, but there
    is no petition to adjudicate or park on, so no petition_hold question."""
    sup = _with_doc(tmp_path, {
        "phases": [_phase("build"), _phase("ship")],
        "tasks": [
            _task("301", "build", "skipped"),
            _task("311", "build", deps=["301"]),
            _task("410", "ship"),
        ]})
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker] * 5}))
    snapshot = _park_recorder(sup)

    asyncio.run(asyncio.wait_for(sup._step_worker(), 30.0))

    assert snapshot == {}, snapshot
    assert not state.read_events(sup.run.root, types=("input.parked",))
    assert "review" not in runner.phases_seen, runner.phases_seen
