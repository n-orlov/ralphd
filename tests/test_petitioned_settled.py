"""v0.38 (#306, #320): a `petitioned` task has defined exits.

A petitioned task is SETTLED, and then satisfies every `dependsOn` naming it
and never blocks `plan_complete`, when any of three sources covers it: an
applied task-level waiver (`ralphctl amend <run> <task> --waive`), a
clause-bound waiver whose clause matches the petition's obligation, or a
CONFIRMED adjudication. It keeps its status throughout (no status lies). The
rule lives next to `TASK_DEPENDENCY_SATISFIED_STATUSES`
(`state.task_dependency_satisfied`, `state.settled_petitioned_task_ids`).

Only an operator status ruling moves a petitioned task out
(`petitioned -> validated|skipped`, both variants), and only an operator
ruling takes the un-reopen edge on a cure-reopened task; a hat's report of
either stays refused.

Sections: (1) one test per settling source, (2) the plan_complete check,
(3) controls that hold before and after, (4) the operator exits.
"""

from __future__ import annotations

import json

import pytest

from ralphd.engine import loop as loop_mod
from ralphd.engine import petitions, state, transitions
from ralphd.engine.scheduler import select_next_task
from ralphd.engine.state import (
    RunDir,
    apply_amendment_status_rulings,
    write_amendment,
)
from tests.conftest import make_supervisor

OBLIGATION = "Task 001: make the impossible thing work"


def _petitioned(**over) -> dict:
    task = {"id": "001", "title": "make the impossible thing work",
            "status": "petitioned", "successCriteria": "criterion 2 holds"}
    task.update(over)
    return task


def _dependent(**over) -> dict:
    task = {"id": "002", "title": "needs 001", "status": "pending",
            "dependsOn": ["001"], "successCriteria": "uses 001"}
    task.update(over)
    return task


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """The `/config` directory the engine reads operator amendments from."""
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", d)
    return d


def _sup(tmp_path, tasks, vigilant=False):
    sup = make_supervisor(
        tmp_path / "run", run_id="petitioned-settled", cfg_defaults=False,
        workspace=tmp_path / "ws", iterations=5, max_approaches=1,
        vigilant=vigilant, on_complete="exit")
    sup.run.write_tasks({"version": 1, "tasks": tasks})
    return sup


def _select(sup):
    """What the worker loop does each pass: the scheduler over the plan, fed
    the settled ids the supervisor derives from the run's own records."""
    doc = sup.run.read_tasks()
    settled = sup._settled_petitioned_ids(doc)
    return select_next_task(doc["tasks"], doc.get("phases"), settled=settled)


def _status_of(sup, task_id):
    return next(t["status"] for t in sup.run.read_tasks()["tasks"]
                if t["id"] == task_id)


# ---------------------------------------------------------------------------
# (1) one test per settling source
# ---------------------------------------------------------------------------


def test_task_level_waiver_settles_a_petitioned_dependency(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)

    picked = _select(sup)

    assert picked is not None and picked["id"] == "002"
    assert _status_of(sup, "001") == "petitioned"


def test_clause_waiver_matching_the_obligation_settles_a_petitioned_dependency(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    sup.run.add_waiver(clause=OBLIGATION, finding="the petition is not a defect",
                       judge="review")

    picked = _select(sup)

    assert picked is not None and picked["id"] == "002"
    assert _status_of(sup, "001") == "petitioned"


def test_confirmed_adjudication_settles_a_petitioned_dependency(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    petitions.record_confirmed_adjudications(
        sup.run, [{"obligation": OBLIGATION, "adjudication": "CONFIRMED"}])

    picked = _select(sup)

    assert picked is not None and picked["id"] == "002"
    assert _status_of(sup, "001") == "petitioned"


def test_settled_petitioned_task_is_no_cause_in_the_blocked_by_overlay(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    tasks = sup.run.read_tasks()["tasks"]
    assert state.task_blocked_by(tasks, include_petitioned=True) == {"002": ["001"]}
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)

    settled = sup._settled_petitioned_ids()

    assert state.task_blocked_by(
        tasks, include_petitioned=True, settled=settled) == {}


# ---------------------------------------------------------------------------
# (2) plan_complete
# ---------------------------------------------------------------------------


def test_plan_complete_accepts_a_plan_whose_only_open_task_is_a_waived_petition(
        tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent(status="completed")])
    write_amendment(cfg_dir, "001", "operator accepts the petition", waive=True)
    doc = sup.run.read_tasks()

    assert sup._outstanding_for_completion(doc) == []
    assert sup._apply_worker_plan_complete_report(None, None, doc) == []
    assert _status_of(sup, "001") == "petitioned"


# ---------------------------------------------------------------------------
# (3) controls: these hold before and after the change
# ---------------------------------------------------------------------------


def test_control_unsettled_petitioned_dependency_still_blocks(tmp_path):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    tasks = sup.run.read_tasks()["tasks"]

    assert select_next_task(tasks) is None
    assert state.task_blocked_by(tasks, include_petitioned=True) == {"002": ["001"]}
    assert [t["id"] for t in sup._outstanding_for_completion(sup.run.read_tasks())
            ] == ["001", "002"]


def test_control_skipped_dependency_still_does_not_satisfy(tmp_path):
    sup = _sup(tmp_path, [_petitioned(status="skipped"), _dependent()])
    tasks = sup.run.read_tasks()["tasks"]

    assert select_next_task(tasks) is None
    assert state.task_blocked_by(tasks) == {"002": ["001"]}


def test_skipped_dependency_is_not_satisfied_by_a_waiver_either(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(status="skipped"), _dependent()])
    write_amendment(cfg_dir, "001", "waived", waive=True)

    assert _select(sup) is None


def test_an_unrelated_waiver_does_not_settle_the_petition(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "009", "waives some other task", waive=True)
    sup.run.add_waiver(clause="an unrelated PRD clause about logging",
                       finding="not this task", judge="review")

    assert _select(sup) is None


def test_a_later_replace_ruling_withdraws_a_task_waiver(tmp_path, cfg_dir):
    sup = _sup(tmp_path, [_petitioned(), _dependent()])
    write_amendment(cfg_dir, "001", "waived", waive=True)
    write_amendment(cfg_dir, "001", "actually, the criteria now read: ...")

    assert _select(sup) is None


@pytest.mark.parametrize("vigilant", [False, True])
def test_control_worker_report_petitioned_to_skipped_is_refused(vigilant):
    errors = transitions.task_status_report_errors(
        {"task_status": "skipped", "justification": "operator waived"},
        "petitioned", vigilant)

    assert errors and "not a legal transition" in errors[0]


@pytest.mark.parametrize("vigilant", [False, True])
def test_control_worker_report_petitioned_to_validated_is_refused(vigilant):
    assert transitions.task_status_report_errors(
        {"task_status": "validated"}, "petitioned", vigilant)


def test_control_worker_report_applied_through_the_supervisor_is_refused(tmp_path):
    sup = _sup(tmp_path, [_petitioned()])

    errors = sup._apply_reported_task_status(
        {"task_id": "001", "task_status": "skipped"})

    assert errors
    assert _status_of(sup, "001") == "petitioned"


@pytest.mark.parametrize("vigilant", [False, True])
@pytest.mark.parametrize("target", ["validated", "completed", "skipped", "failed"])
def test_control_cure_reopen_guard_still_refuses_a_hat(vigilant, target):
    errors = transitions.task_status_report_errors(
        {"task_status": target}, "pending", vigilant)

    # a hat's report out of `pending` is refused for every un-reopen target
    # the variant does not itself reach (`completed` non-vigilant is the
    # ordinary claim path's own terminal, still refused out of `pending`)
    assert errors


# ---------------------------------------------------------------------------
# (4) operator exits: these fail before the change and pass after it
# ---------------------------------------------------------------------------


def _run_with(tmp_path, tasks, **status) -> RunDir:
    run = RunDir(root=tmp_path / "run")
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))
    if status:
        run.update_status(**status)
    return run


def _events(run):
    path = run.root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()] if path.exists() else []


@pytest.mark.parametrize("vigilant", [False, True])
@pytest.mark.parametrize("target", ["validated", "skipped"])
def test_operator_ruling_applies_petitioned_exit(tmp_path, vigilant, target):
    run = _run_with(tmp_path, [_petitioned()])
    cdir = tmp_path / "config"
    fname = write_amendment(cdir, "001", "operator ruling on the petition",
                            status=target)

    results = apply_amendment_status_rulings(run, cdir, vigilant)

    assert [(r["applied"], r["reason"]) for r in results] == [(True, "")]
    assert run.read_tasks()["tasks"][0]["status"] == target
    applied = [e for e in _events(run) if e.get("type") == "amendment_status_applied"]
    assert [(e["taskId"], e["previous"], e["status"], e["source"], e["file"])
            for e in applied] == [("001", "petitioned", target, "operator", fname)]


@pytest.mark.parametrize("vigilant", [False, True])
def test_operator_ruling_validated_on_a_cure_reopened_task_is_applied(
        tmp_path, vigilant):
    run = _run_with(tmp_path, [_dependent(id="001", dependsOn=[])],
                    preCureStatuses={"001": "validated"})
    cdir = tmp_path / "config"
    write_amendment(cdir, "001", "operator accepts the re-opened task",
                    status="validated")

    results = apply_amendment_status_rulings(run, cdir, vigilant)

    assert [(r["applied"], r["reason"]) for r in results] == [(True, "")]
    assert run.read_tasks()["tasks"][0]["status"] == "validated"
    # the un-reopen edge emits its own dedicated event, naming the operator
    reopened = [e for e in _events(run) if e.get("type") == state.UN_REOPEN_EVENT]
    assert [(e["taskId"], e["status"], e["source"]) for e in reopened] == [
        ("001", "validated", "operator")]


@pytest.mark.parametrize("vigilant", [False, True])
def test_operator_ruling_validated_on_a_never_reopened_task_is_still_refused(
        tmp_path, vigilant):
    run = _run_with(tmp_path, [_dependent(id="001", dependsOn=[])])
    cdir = tmp_path / "config"
    write_amendment(cdir, "001", "no cure ever re-opened this", status="validated")

    results = apply_amendment_status_rulings(run, cdir, vigilant)

    assert [r["applied"] for r in results] == [False]
    assert run.read_tasks()["tasks"][0]["status"] == "pending"
