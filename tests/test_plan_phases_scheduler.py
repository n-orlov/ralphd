"""PRD v0.37 requirement B.1-B.4: scheduling behind a closed phase gate.

* B.1: with `phases`, `select_next_task` and `pending_candidates` never
  return a task of a phase later than the open phase (the earliest phase
  whose gate is not `passed`) -- while that gate is `closed`, while it is
  `reviewing`, and until it is `passed`; the `validation-failed` and
  `in-progress` steps are not phase-gated.
* B.2: a task of a passed phase that a final-review cure re-opens through
  `insert_cure_tasks` is scheduled, and the gate stays `passed`.
* B.3: the first phase's `startHead` is the workspace HEAD at the end of
  planning (fake hats, a real git workspace).
* B.4: the due rule, over a two-task phase.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from ralphd.engine import scheduler
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    RunDir,
    atomic_write_json,
    due_phase_id,
    insert_cure_tasks,
    phase_is_due,
    record_phase_start_head,
)

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(
    run_id="plan-phases-scheduler", infra_retry_backoff_s=[0.0],
    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
    vigilant=False, max_approaches=2, on_complete="exit")


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


def _two_phases(gate_one: str) -> list:
    return [_phase("one", gate_one), _phase("two", "closed")]


def _tasks_with_hot_later_phase(first_status: str = "pending") -> list:
    """Phase `one` holds a low-priority task; phase `two` holds a
    satisfied-`dependsOn`, highest-priority task."""
    return [_task("101", "one", first_status, priority=0),
            _task("201", "two", priority=100)]


# -- B.1 ----------------------------------------------------------------------

@pytest.mark.parametrize("gate", ["closed", "reviewing"])
def test_a_later_phase_task_is_not_picked_while_the_earlier_gate_is_not_passed(gate):
    tasks = _tasks_with_hot_later_phase()
    phases = _two_phases(gate)
    assert scheduler.select_next_task(tasks, phases=phases)["id"] == "101"
    assert [t["id"] for t in scheduler.pending_candidates(tasks, phases=phases)] \
        == ["101"]


@pytest.mark.parametrize("gate", ["closed", "reviewing"])
def test_a_later_phase_task_is_the_only_pending_one_and_is_still_not_picked(gate):
    tasks = _tasks_with_hot_later_phase(first_status="validated")
    phases = _two_phases(gate)
    assert scheduler.select_next_task(tasks, phases=phases) is None
    assert scheduler.pending_candidates(tasks, phases=phases) == []


def test_a_later_phase_task_is_picked_once_the_earlier_gate_has_passed():
    tasks = _tasks_with_hot_later_phase(first_status="validated")
    phases = _two_phases("passed")
    assert scheduler.select_next_task(tasks, phases=phases)["id"] == "201"
    assert [t["id"] for t in scheduler.pending_candidates(tasks, phases=phases)] \
        == ["201"]


def test_a_later_phase_task_with_the_highest_priority_loses_to_the_open_phase():
    tasks = _tasks_with_hot_later_phase()
    phases = _two_phases("passed")
    # with the gate passed and both pending, the higher priority (phase two)
    # wins: gating is by phase, never by a priority or dependsOn edge.
    assert scheduler.select_next_task(tasks, phases=phases)["id"] == "201"


def test_no_phases_argument_or_an_empty_one_means_no_gating():
    tasks = _tasks_with_hot_later_phase()
    assert scheduler.select_next_task(tasks)["id"] == "201"
    assert scheduler.select_next_task(tasks, phases=[])["id"] == "201"
    assert scheduler.select_next_task(tasks, phases=None)["id"] == "201"
    assert len(scheduler.pending_candidates(tasks, phases=[])) == 2


@pytest.mark.parametrize("status", ["validation-failed", "in-progress"])
def test_the_validation_failed_and_in_progress_steps_are_not_phase_gated(status):
    tasks = [_task("101", "one", priority=0),
             _task("201", "two", status, priority=0)]
    picked = scheduler.select_next_task(tasks, phases=_two_phases("closed"))
    assert picked["id"] == "201"


def test_a_third_phase_task_is_gated_behind_the_open_second_phase():
    phases = [_phase("one", "passed"), _phase("two", "closed"),
              _phase("three", "closed")]
    tasks = [_task("301", "three", priority=50), _task("201", "two")]
    assert scheduler.select_next_task(tasks, phases=phases)["id"] == "201"
    assert [t["id"] for t in scheduler.pending_candidates(tasks, phases=phases)] \
        == ["201"]


# -- B.2 ----------------------------------------------------------------------

RECORD_PATH = "artifacts/reports/lanes.md"


def test_a_final_review_cure_reopening_a_passed_phase_task_schedules_it_and_keeps_the_gate(tmp_path):
    run = RunDir(tmp_path / "run")
    run.root.mkdir(parents=True, exist_ok=True)
    run.write_tasks({
        "version": 1,
        "phases": _two_phases("passed"),
        "tasks": [_task("101", "one", "validated", successCriteria="verify every lane"),
                  _task("102", "one", "completed", dependsOn=["101"],
                        successCriteria=f"One commit adds {RECORD_PATH} naming the sha."),
                  _task("201", "two", "validated")]})
    finding = {"severity": "blocking", "curable": True,
               "clause": "requirement X", "evidence": "the lanes moved",
               "invalidates": [RECORD_PATH]}
    inserted = insert_cure_tasks(run, 1, [finding])   # final review: last phase
    cure_id = inserted[0]
    assert set(inserted[1:]) == {"101", "102"}

    data = run.read_tasks()
    by_id = {t["id"]: t for t in data["tasks"]}
    assert by_id["101"]["status"] == "pending" and by_id["101"]["phase"] == "one"
    assert by_id[cure_id]["phase"] == "two"
    phases = data["phases"]
    # the cure task is schedulable at once; the re-opened phase-1 task waits
    # on it and is then scheduled too, not held by any gate.
    assert scheduler.select_next_task(data["tasks"], phases=phases)["id"] == cure_id
    by_id[cure_id]["status"] = "validated"
    assert scheduler.select_next_task(data["tasks"], phases=phases)["id"] == "101"
    by_id["101"]["status"] = "validated"
    assert scheduler.select_next_task(data["tasks"], phases=phases)["id"] == "102"
    assert [p["gate"] for p in phases] == ["passed", "closed"]
    assert not phase_is_due(data, "one")          # a passed gate never re-closes


# -- B.3 ----------------------------------------------------------------------

def _git(ws, *args) -> str:
    done = subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e.x",
         *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _planning_hat(sup, phases: bool):
    """A fake planning hat: commits a file in the workspace, writes the
    proposal and answers with the pointer line."""
    async def fake_once(phase, extra="", prompt_name=None, tier=None,
                        task_id=None):
        sup.iterations_used += 1
        (sup.workspace / "planned.txt").write_text("planned\n")
        _git(sup.workspace, "add", "planned.txt")
        _git(sup.workspace, "commit", "-q", "-m", "planning commit")
        tasks = [{"id": "001", "title": "t1", "successCriteria": "criterion one",
                  "tier": "normal", "estimatedIterations": 1},
                 {"id": "002", "title": "t2", "successCriteria": "criterion two",
                  "tier": "normal", "estimatedIterations": 1}]
        proposal = {"version": 1, "tasks": tasks}
        if phases:
            tasks[0]["phase"] = "one"
            tasks[1]["phase"] = "two"
            proposal["phases"] = [
                {"id": "one", "title": "One", "reviewFocus": "first"},
                {"id": "two", "title": "Two", "reviewFocus": "second"}]
        atomic_write_json(sup.run.root / "plan-proposal.json", proposal)
        result = IterationResult(exit_code=0)
        result.final_text = "planned\n" + json.dumps(
            {"proposal_path": "plan-proposal.json", "summary": "planned"})
        result.duration_s = 30.0
        result.usage = {"input": 10, "output": 5, "totalTokens": 15}
        return result

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]


def test_the_first_phases_start_head_is_the_workspace_head_at_the_end_of_planning(tmp_path):
    sup = _supervisor(tmp_path, iterations=5)
    sup._approach = 1
    _git(sup.workspace, "init", "-q")
    _git(sup.workspace, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(sup.workspace, "rev-parse", "HEAD")
    _planning_hat(sup, phases=True)

    outcome = asyncio.run(sup._step_planning(skip_planning=False))

    assert outcome[0] == "advance"
    head = _git(sup.workspace, "rev-parse", "HEAD")
    assert head != base                      # planning itself committed
    phases = sup.run.read_tasks()["phases"]
    assert phases[0]["startHead"] == head
    assert phases[1]["startHead"] is None


def test_an_unphased_plan_records_no_start_head_and_writes_no_phases_key(tmp_path):
    sup = _supervisor(tmp_path, iterations=5)
    sup._approach = 1
    _git(sup.workspace, "init", "-q")
    _git(sup.workspace, "commit", "-q", "--allow-empty", "-m", "base")
    _planning_hat(sup, phases=False)

    assert asyncio.run(sup._step_planning(skip_planning=False))[0] == "advance"
    assert "phases" not in sup.run.read_tasks()


def test_start_head_is_written_once_and_an_unreadable_head_writes_nothing():
    doc = {"phases": _two_phases("closed")}
    assert not record_phase_start_head(doc, "one", None)
    assert doc["phases"][0]["startHead"] is None
    assert record_phase_start_head(doc, "one", "abc")
    assert not record_phase_start_head(doc, "one", "def")
    assert doc["phases"][0]["startHead"] == "abc"
    assert not record_phase_start_head(doc, "nine", "abc")


# -- B.4: the due rule, over a two-task fixture phase --------------------------

def _due_doc(first: str, second: str, gate: str = "closed") -> dict:
    return {"phases": _two_phases(gate),
            "tasks": [_task("101", "one", first), _task("102", "one", second),
                      _task("201", "two")]}


def test_a_phase_with_both_tasks_validated_is_due_with_vigilant_on():
    doc = _due_doc("validated", "validated")
    assert phase_is_due(doc, "one")
    assert due_phase_id(doc) == "one"


def test_a_phase_with_both_tasks_completed_is_due_with_vigilant_off():
    doc = _due_doc("completed", "completed")
    assert phase_is_due(doc, "one")
    assert due_phase_id(doc) == "one"


def test_a_phase_with_a_task_awaiting_validation_is_not_due():
    doc = _due_doc("validated", "awaiting-validation")
    assert not phase_is_due(doc, "one")
    assert due_phase_id(doc) is None


def test_a_phase_with_a_failed_task_is_due():
    assert phase_is_due(_due_doc("validated", "failed"), "one")


def test_a_phase_with_a_petitioned_task_is_due():
    assert phase_is_due(_due_doc("validated", "petitioned"), "one")


@pytest.mark.parametrize("status", ["validation-failed", "in-progress", "pending"])
def test_a_phase_with_a_non_terminal_task_is_not_due(status):
    assert not phase_is_due(_due_doc("validated", status), "one")


@pytest.mark.parametrize("gate", ["reviewing", "passed"])
def test_a_phase_whose_gate_is_not_closed_is_not_due(gate):
    assert not phase_is_due(_due_doc("validated", "validated", gate), "one")


def test_an_unphased_plan_or_unknown_phase_is_never_due():
    assert not phase_is_due({"tasks": [_task("1", None, "validated")]}, "one")
    assert not phase_is_due(_due_doc("validated", "validated"), "nine")
    assert due_phase_id({"tasks": [_task("1", None, "validated")]}) is None
