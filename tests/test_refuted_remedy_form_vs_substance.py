"""Task 008 (PRD requirement A.4, #155): a mid-run adjudication's REFUTED
path now classifies the remedy it carries as `"form"` (the remedy changes
no deliverable, only how it is produced) or `"substance"` (the default,
including every REFUTED entry that predates this task and never supplied
`remedy_type` at all) and routes accordingly:

- `"substance"` behaves EXACTLY as before this task: the matching
  `petitioned` task moves to `failed`, carrying the remedy on
  `validationNotes` (the retry driver `tests/test_petitioned_plan_
  complete.py` already pins byte-for-byte, untouched by this task).
- `"form"` instead moves the task to `validated` and records the remedy
  as a debt item (`state.record_debt_entries`'s own shape, task 006's ONE
  write path) -- `validationNotes` is never touched, so it drives no
  retry.

Two tiers: `engine.petitions.apply_refuted_petition_adjudications` at the
pure-function level (both branches, plus the `form_debt` output channel),
and `LoopSupervisor._apply_refuted_petition_adjudications` at the
production write-back level (both branches persisted to `tasks.json` and,
for `form`, to the run's own `debt.json`/`debt.md` through
`RunDir.record_debt`).
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import make_supervisor

TASK_ID = "001"
OTHER_TASK_ID = "002"
OBLIGATION = "Task 001: make the impossible thing work"
REMEDY_TEXT = "split criterion 2 into a separate task that runs after task 003 lands"
ORIGINAL_NOTES = "criterion 2 cannot be verified as written"


def _task(id_=TASK_ID, **over) -> dict:
    task = {"id": id_, "title": "make the impossible thing work",
            "status": "petitioned", "successCriteria": "criterion 2 holds",
            "validationNotes": ORIGINAL_NOTES}
    task.update(over)
    return task


_supervisor = lambda root, workspace=None, **cfg: make_supervisor(
    root, run_id="task008-refuted-remedy-classify", cfg_defaults=False,
    workspace=workspace or root.parent / 'ws', iterations=5, max_approaches=1,
    vigilant=False, on_complete="exit", **cfg)


def _write_plan(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


# ---------------------------------------------------------------------------
# Substance branch (default, and explicit) -- behaves exactly as today
# ---------------------------------------------------------------------------


def test_missing_remedy_type_defaults_to_substance_and_fails_the_task():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    form_debt: dict = {}
    changed = petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT},
    ], form_debt=form_debt)

    assert changed == [TASK_ID]
    assert form_debt == {}
    task = tasks_doc["tasks"][0]
    assert task["status"] == "failed"
    assert task["validationNotes"] == REMEDY_TEXT
    assert task["failureKind"] == state.TASK_FAILURE_VALIDATION_EXHAUSTED


def test_explicit_substance_remedy_type_fails_the_task_same_as_default():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    changed = petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "substance"},
    ])

    assert changed == [TASK_ID]
    task = tasks_doc["tasks"][0]
    assert task["status"] == "failed"
    assert task["validationNotes"] == REMEDY_TEXT


def test_supervisor_write_back_substance_persists_failed_and_no_debt(tmp_path):
    sup = _supervisor(tmp_path / "run")
    _write_plan(sup, [_task()])

    changed = sup._apply_refuted_petition_adjudications([
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT},
    ])

    assert changed == [TASK_ID]
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["status"] == "failed"
    assert on_disk["validationNotes"] == REMEDY_TEXT
    assert sup.run.read_debt() == []


# ---------------------------------------------------------------------------
# Form branch -- recorded as a debt item, drives no validationNotes retry
# ---------------------------------------------------------------------------


def test_form_remedy_type_validates_the_task_instead_of_failing_it():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    form_debt: dict = {}
    changed = petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "form"},
    ], form_debt=form_debt)

    task = tasks_doc["tasks"][0]
    assert changed == []
    assert task["status"] == "validated"
    assert "failureKind" not in task


def test_form_remedy_type_leaves_validation_notes_untouched():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "form"},
    ])

    task = tasks_doc["tasks"][0]
    # The whole point: no validationNotes retry driver.
    assert task["validationNotes"] == ORIGINAL_NOTES


def test_form_remedy_type_is_case_and_whitespace_tolerant():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "  Form  "},
    ])

    assert tasks_doc["tasks"][0]["status"] == "validated"


def test_form_remedy_type_populates_form_debt_with_the_remedy():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    form_debt: dict = {}
    petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "form"},
    ], form_debt=form_debt)

    assert list(form_debt.keys()) == [TASK_ID]
    notes = form_debt[TASK_ID]
    assert len(notes) == 1
    assert notes[0]["clause"] == OBLIGATION
    assert notes[0]["suggested"] == REMEDY_TEXT
    assert "form" in notes[0]["observed"].lower()


def test_form_remedy_type_without_form_debt_sink_still_routes_to_validated():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    changed = petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "form"},
    ])

    assert changed == []
    assert tasks_doc["tasks"][0]["status"] == "validated"


def test_form_remedy_leaves_non_matching_tasks_alone():
    tasks_doc = {"version": 1,
                 "tasks": [_task(), _task(id_=OTHER_TASK_ID, title="unrelated")]}
    petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "form"},
    ])

    other = next(t for t in tasks_doc["tasks"] if t["id"] == OTHER_TASK_ID)
    assert other["status"] == "petitioned"


def test_supervisor_write_back_form_persists_validated_and_records_debt(tmp_path):
    sup = _supervisor(tmp_path / "run")
    _write_plan(sup, [_task()])

    changed = sup._apply_refuted_petition_adjudications([
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "form"},
    ])

    assert changed == []
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["status"] == "validated"
    assert on_disk["validationNotes"] == ORIGINAL_NOTES

    debt = sup.run.read_debt()
    assert len(debt) == 1
    entry = debt[0]
    assert entry["taskId"] == TASK_ID
    assert entry["clause"] == OBLIGATION
    assert entry["suggested"] == REMEDY_TEXT
    assert entry["status"] == state.DEBT_STATUS_OPEN
    assert sup.run.debt_file.exists()


def test_form_validated_task_is_not_outstanding_for_completion(tmp_path):
    sup = _supervisor(tmp_path / "run")
    _write_plan(sup, [_task()])
    sup._apply_refuted_petition_adjudications([
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT, "remedy_type": "form"},
    ])

    doc = sup.run.read_tasks()
    assert sup._outstanding_for_completion(doc) == []
