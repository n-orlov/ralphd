"""PRD v0.37 requirement A.4: every path by which the engine inserts a task
into a phased plan gives it a phase, and a plan without phases never gets a
`phase` key from any of them.

One node per insertion path: a phase-gate cure, the final review's cure, the
peer verdict gate's cure, a refuted-petition remedy task, the children of a
worker `proposed_tasks` split, and a steer `task-insert` (no phase named, an
open phase named, a passed phase named). Each phased node drives the real
production writer; the unphased node of the same path asserts no key.
"""

from __future__ import annotations

import pytest

from ralphd.engine import findings, petitions
from ralphd.engine.state import (
    RunDir,
    STEER_HAT_TOOLS,
    atomic_write_json,
    insert_cure_tasks,
)

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="plan-phases-insertion",
                                 run_root="run", workspace="workspace")

PHASES = ("one", "two", "three")


def _task(tid: str, phase: str | None, status: str = "pending", **extra) -> dict:
    task = {"id": tid, "title": f"thing {tid}",
            "successCriteria": f"thing {tid} is done", "status": status,
            "origin": "planner", "dependsOn": [], "tier": "normal"}
    if phase is not None:
        task["phase"] = phase
    task.update(extra)
    return task


def _phase(pid: str, gate: str) -> dict:
    return {"id": pid, "title": pid, "reviewFocus": "", "gate": gate,
            "rounds": 0, "startHead": None, "records": []}


def _phased_doc(gates=("closed", "closed", "closed")) -> dict:
    return {"version": 1,
            "phases": [_phase(p, g) for p, g in zip(PHASES, gates)],
            "tasks": [_task("101", "one", "validated"),
                      _task("201", "two"),
                      _task("301", "three")]}


def _unphased_doc() -> dict:
    return {"version": 1,
            "tasks": [_task("101", None, "validated"), _task("201", None),
                      _task("301", None)]}


def _write(run: RunDir, doc: dict) -> None:
    run.root.mkdir(parents=True, exist_ok=True)
    run.write_tasks(doc)


def _blocking(clause: str = "requirement X", **extra) -> dict:
    finding = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": "observed", "cured_when": "the case is covered"}
    finding.update(extra)
    return finding


def _by_id(run: RunDir) -> dict:
    return {t["id"]: t for t in run.read_tasks()["tasks"]}


def _new_tasks(run: RunDir, before: set) -> list[dict]:
    return [t for t in run.read_tasks()["tasks"] if t["id"] not in before]


def _ids(run: RunDir) -> set:
    return {t["id"] for t in run.read_tasks()["tasks"]}


# -- cure tasks ---------------------------------------------------------------

def test_a_phase_gate_cure_lands_in_that_gates_phase(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _phased_doc())
    inserted = insert_cure_tasks(run, 1, [_blocking(), _blocking("requirement Y")],
                                 phase="two")
    assert len(inserted) == 2
    tasks = _by_id(run)
    assert [tasks[i]["phase"] for i in inserted] == ["two", "two"]


def test_a_phase_gate_cure_naming_an_unknown_phase_is_a_caller_error(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _phased_doc())
    with pytest.raises(ValueError, match="unknown plan phase 'nine'"):
        insert_cure_tasks(run, 1, [_blocking()], phase="nine")
    assert _ids(run) == {"101", "201", "301"}


def test_a_final_review_cure_lands_in_the_last_phase(tmp_path):
    sup = _supervisor(tmp_path / "run", approach=1)
    _write(sup.run, _phased_doc())
    findings.write_findings_json(sup.run.findings_json_file, [_blocking()])
    inserted = sup._take_cure_pass(1)
    assert inserted
    tasks = _by_id(sup.run)
    assert [tasks[i]["phase"] for i in inserted] == ["three"]


def test_a_peer_gate_cure_lands_in_the_last_phase(tmp_path):
    sup = _supervisor(tmp_path / "run", approach=1)
    _write(sup.run, _phased_doc())
    findings.write_findings_json(sup.run.verdict_gate_findings_json_file,
                                 [_blocking("requirement P")])
    before = _ids(sup.run)
    sup._take_gate_cure_pass(1)
    new = _new_tasks(sup.run, before)
    assert len(new) == 1 and new[0]["phase"] == "three"


def test_a_cure_task_is_not_ordered_onto_a_freeze_task_of_an_earlier_phase(tmp_path):
    """A last-phase cure must not become a dependency of a freeze task that
    sits in phase one: that edge would point at a later phase."""
    run = RunDir(tmp_path / "run")
    doc = _phased_doc()
    doc["tasks"][0]["successCriteria"] = (
        "this is the last code-touching task, unless the plan names a later one")
    _write(run, doc)
    (inserted,) = insert_cure_tasks(run, 1, [_blocking()])
    tasks = _by_id(run)
    assert tasks[inserted]["phase"] == "three"
    assert inserted not in tasks["101"]["dependsOn"]
    # while a cure in the freeze task's own (or an earlier) phase still gates it
    (early,) = insert_cure_tasks(run, 2, [_blocking("requirement E")], phase="one")
    assert early in _by_id(run)["101"]["dependsOn"]


# -- remedy tasks (refuted petitions) -----------------------------------------

def test_a_refuted_petition_remedy_lands_in_the_petitioned_tasks_phase(tmp_path):
    sup = _supervisor(tmp_path / "run", approach=1)
    doc = _phased_doc()
    doc["tasks"][1]["status"] = "failed"  # task 201, phase two: the petitioned one
    _write(sup.run, doc)
    obligation = "Task 201: thing 201"
    assert petitions.file_worker_petition(
        sup.workspace, obligation, "environmental", "measured in the sandbox",
        run=sup.run) == []
    inserted = sup._insert_refuted_remedy_tasks([
        {"obligation": obligation, "adjudication": "REFUTED",
         "remedy": "Fix the fixture. Done when: the fixture renders."}])
    assert len(inserted) == 1
    task = _by_id(sup.run)[inserted[0]]
    assert task["remedyFor"] == obligation
    assert task["phase"] == "two"  # not the last phase, where a plain cure goes


# -- worker split children ----------------------------------------------------

def _proposal(*ids: str) -> list[dict]:
    return [{"id": i, "title": f"piece {i}",
             "successCriteria": f"piece {i} is verifiably done"} for i in ids]


def test_worker_split_children_land_in_the_parents_phase(tmp_path):
    sup = _supervisor(tmp_path / "run", approach=1)
    _write(sup.run, _phased_doc())
    sup._apply_worker_proposed_tasks(_proposal("201a", "201b"), "201", 1)
    tasks = _by_id(sup.run)
    assert {"201a", "201b"} <= set(tasks)
    assert tasks["201a"]["phase"] == tasks["201b"]["phase"] == "two"
    assert tasks["201a"]["splitFrom"] == "201"


# -- steer task-insert --------------------------------------------------------

def _steer_insert(run: RunDir, task: dict) -> dict:
    return STEER_HAT_TOOLS["task-insert"](run, task)


def test_a_steer_insert_without_phase_lands_in_the_earliest_open_phase(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _phased_doc(gates=("passed", "closed", "closed")))
    assert _steer_insert(run, {"id": "s1", "title": "t"})["inserted"] is True
    assert _by_id(run)["s1"]["phase"] == "two"


def test_a_steer_insert_without_phase_lands_in_the_last_phase_when_all_gates_passed(
        tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _phased_doc(gates=("passed", "passed", "passed")))
    assert _steer_insert(run, {"id": "s1", "title": "t"})["inserted"] is True
    assert _by_id(run)["s1"]["phase"] == "three"


def test_a_steer_insert_naming_an_open_phase_lands_there(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _phased_doc(gates=("passed", "closed", "closed")))
    result = _steer_insert(run, {"id": "s1", "title": "t", "phase": "three"})
    assert result["inserted"] is True
    assert _by_id(run)["s1"]["phase"] == "three"


def test_a_steer_insert_naming_a_passed_phase_is_refused_naming_the_open_one(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _phased_doc(gates=("passed", "closed", "closed")))
    result = _steer_insert(run, {"id": "s1", "title": "t", "phase": "one"})
    assert result["inserted"] is False
    assert "'one'" in result["reason"] and "passed" in result["reason"]
    assert "earliest open phase is 'two'" in result["reason"]
    assert "s1" not in _ids(run)


def test_a_steer_insert_naming_a_phase_the_plan_lacks_is_refused(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _phased_doc())
    result = _steer_insert(run, {"id": "s1", "title": "t", "phase": "nine"})
    assert result["inserted"] is False and "not a phase of this plan" in result["reason"]
    assert "s1" not in _ids(run)


def test_a_steer_insert_drops_a_dependency_on_a_later_phase(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _phased_doc())
    result = _steer_insert(run, {"id": "s1", "title": "t", "phase": "one",
                                 "dependsOn": ["101", "301"]})
    assert result["inserted"] is True
    assert _by_id(run)["s1"]["dependsOn"] == ["101"]


# -- a plan without phases ----------------------------------------------------

def test_no_insertion_path_writes_a_phase_key_on_an_unphased_plan(tmp_path):
    sup = _supervisor(tmp_path / "run", approach=1)
    _write(sup.run, _unphased_doc())
    before = _ids(sup.run)

    # final-review cure, peer-gate cure
    findings.write_findings_json(sup.run.findings_json_file, [_blocking("requirement R")])
    sup._take_cure_pass(1)
    findings.write_findings_json(sup.run.verdict_gate_findings_json_file,
                                 [_blocking("requirement P")])
    sup._take_gate_cure_pass(1)
    
    # remedy task for a refuted petition against failed task 201
    doc = sup.run.read_tasks()
    next(t for t in doc["tasks"] if t["id"] == "201")["status"] = "failed"
    sup.run.write_tasks(doc)
    obligation = "Task 201: thing 201"
    assert petitions.file_worker_petition(
        sup.workspace, obligation, "environmental", "measured", run=sup.run) == []
    assert sup._insert_refuted_remedy_tasks([
        {"obligation": obligation, "adjudication": "REFUTED",
         "remedy": "Fix it. Done when: it is fixed."}])

    # worker split children and a steer insert
    sup._apply_worker_proposed_tasks(_proposal("301a"), "301", 1)
    assert _steer_insert(sup.run, {"id": "s1", "title": "t"})["inserted"] is True

    new = _new_tasks(sup.run, before)
    assert len(new) == 5  # review cure, peer cure, remedy, split child, steer
    assert all("phase" not in t for t in sup.run.read_tasks()["tasks"])
    assert "phases" not in sup.run.read_tasks()


def test_a_phase_gate_cure_call_on_an_unphased_plan_writes_no_phase_key(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _unphased_doc())
    inserted = insert_cure_tasks(run, 1, [_blocking()], phase="one")
    assert inserted and all("phase" not in t for t in run.read_tasks()["tasks"])


def test_a_steer_insert_naming_a_phase_on_an_unphased_plan_is_refused(tmp_path):
    run = RunDir(tmp_path / "run")
    _write(run, _unphased_doc())
    result = _steer_insert(run, {"id": "s1", "title": "t", "phase": "one"})
    assert result["inserted"] is False and "no phases" in result["reason"]
    assert "s1" not in _ids(run)
