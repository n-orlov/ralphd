"""Task 015 (PRD requirement C.3, #149): "closed by CONFIRMED" -- once a
task exhausts its validation ladder into `petitioned` (task 009,
requirement C.1) and review CONFIRMS the obligation it filed, the engine
closes the loop instead of leaving the task sitting at `petitioned`
forever (C.2's own tolerance, task 010):

1. The task moves to `skipped`, with the adjudication recorded on its own
   `notes` field -- the obligation is void, not merely tolerated.
2. Every OTHER task's `dependsOn` is re-pointed away from it -- to nothing
   by default, or to the `replacement_task_id` the CONFIRMED entry names,
   when that id actually exists in the current plan.
3. A REFUTED adjudication of the SAME `petitioned` task's own obligation
   is untouched by any of this -- `petitions.apply_refuted_petition_
   adjudications` (task 010) still moves it to `failed`/`validated`
   exactly as before; that path is pinned by
   `tests/test_petitioned_plan_complete.py::
   test_apply_refuted_petition_adjudications_moves_the_task_to_failed`
   and `tests/test_refuted_remedy_form_vs_substance.py`, not re-proved
   here.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, atomic_write_json

TASK_ID = "001"
DEPENDENT_ID = "002"
REPLACEMENT_ID = "003"
OBLIGATION = "Task 001: make the impossible thing work"


def _task(id_=TASK_ID, **over) -> dict:
    task = {"id": id_, "title": "make the impossible thing work",
            "status": "petitioned", "successCriteria": "criterion 2 holds",
            "validationNotes": "criterion 2 cannot be verified as written"}
    task.update(over)
    return task


def _supervisor(root: Path, workspace: Path | None = None) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    cfg = JobConfig(run_id="task015-confirmed-closes-task", iterations=5,
                    max_approaches=1, vigilant=False, on_complete="exit")
    return LoopSupervisor(cfg, run, workspace or (root.parent / "ws"))


def _write_plan(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


# ---------------------------------------------------------------------------
# 1. CONFIRMED moves the task to `skipped`, notes carry the adjudication
# ---------------------------------------------------------------------------


def test_apply_confirmed_petition_adjudications_moves_the_task_to_skipped():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    changed = petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    assert changed == [TASK_ID]
    task = tasks_doc["tasks"][0]
    assert task["status"] == "skipped"
    assert "CONFIRMED" in task["notes"]
    assert OBLIGATION in task["notes"]


def test_apply_confirmed_petition_adjudications_appends_to_existing_notes():
    tasks_doc = {"version": 1, "tasks": [_task(notes="an earlier note")]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    notes = tasks_doc["tasks"][0]["notes"]
    assert notes.startswith("an earlier note\n")
    assert "CONFIRMED" in notes


def test_apply_confirmed_petition_adjudications_leaves_non_matching_tasks_alone():
    tasks_doc = {"version": 1,
                 "tasks": [_task(), _task(id_=DEPENDENT_ID, title="unrelated")]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    other = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert other["status"] == "petitioned"


def test_apply_confirmed_petition_adjudications_ignores_refuted_entries():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    changed = petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": "do something else"},
    ])

    assert changed == []
    assert tasks_doc["tasks"][0]["status"] == "petitioned"


def test_apply_confirmed_petition_adjudications_is_a_noop_with_no_adjudications():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    assert petitions.apply_confirmed_petition_adjudications(tasks_doc, None) == []
    assert petitions.apply_confirmed_petition_adjudications(tasks_doc, []) == []
    assert tasks_doc["tasks"][0]["status"] == "petitioned"


# ---------------------------------------------------------------------------
# 2. dependents are re-pointed -- to nothing, or to the named replacement
# ---------------------------------------------------------------------------


def test_a_dependents_dependson_loses_the_confirmed_task_with_no_replacement():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": DEPENDENT_ID, "title": "depends on it",
         "status": "pending", "successCriteria": "x",
         "dependsOn": [TASK_ID]},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert dependent["dependsOn"] == []


def test_a_dependents_dependson_gains_the_named_replacement_task():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": REPLACEMENT_ID, "title": "the replacement",
         "status": "pending", "successCriteria": "y"},
        {"id": DEPENDENT_ID, "title": "depends on it",
         "status": "pending", "successCriteria": "x",
         "dependsOn": [TASK_ID]},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED",
         "replacement_task_id": REPLACEMENT_ID},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert dependent["dependsOn"] == [REPLACEMENT_ID]


def test_a_replacement_task_id_naming_no_real_task_repoints_to_nothing():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": DEPENDENT_ID, "title": "depends on it",
         "status": "pending", "successCriteria": "x",
         "dependsOn": [TASK_ID]},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED",
         "replacement_task_id": "does-not-exist"},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert dependent["dependsOn"] == []


def test_a_dependent_naming_both_the_task_and_its_replacement_has_no_duplicate():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": REPLACEMENT_ID, "title": "the replacement",
         "status": "pending", "successCriteria": "y"},
        {"id": DEPENDENT_ID, "title": "depends on both",
         "status": "pending", "successCriteria": "x",
         "dependsOn": [TASK_ID, REPLACEMENT_ID]},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED",
         "replacement_task_id": REPLACEMENT_ID},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert dependent["dependsOn"] == [REPLACEMENT_ID]


def test_a_dependent_with_no_dependson_at_all_is_left_alone():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": DEPENDENT_ID, "title": "no deps", "status": "pending",
         "successCriteria": "x"},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert "dependsOn" not in dependent


# ---------------------------------------------------------------------------
# 3. the supervisor's write-back wrapper persists the same transition
# ---------------------------------------------------------------------------


def test_supervisor_write_back_persists_the_confirmed_transition(tmp_path):
    sup = _supervisor(tmp_path / "run")
    _write_plan(sup, [
        _task(),
        {"id": DEPENDENT_ID, "title": "depends on it", "status": "pending",
         "successCriteria": "x", "dependsOn": [TASK_ID]},
    ])

    changed = sup._apply_confirmed_petition_adjudications([
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    assert changed == [TASK_ID]
    on_disk = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert on_disk[TASK_ID]["status"] == "skipped"
    assert "CONFIRMED" in on_disk[TASK_ID]["notes"]
    assert on_disk[DEPENDENT_ID]["dependsOn"] == []


def test_supervisor_write_back_is_a_noop_with_no_adjudications(tmp_path):
    sup = _supervisor(tmp_path / "run")
    _write_plan(sup, [_task()])
    before = sup.run.tasks_file.stat().st_mtime_ns

    assert sup._apply_confirmed_petition_adjudications(None) == []
    assert sup._apply_confirmed_petition_adjudications([]) == []

    assert sup.run.tasks_file.stat().st_mtime_ns == before
    assert sup.run.read_tasks()["tasks"][0]["status"] == "petitioned"
