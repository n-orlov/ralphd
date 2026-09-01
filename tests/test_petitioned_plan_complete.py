"""Task 010 (PRD requirement C.2, #114): once a task exhausts its
validation ladder into `petitioned` (task 009, requirement C.1), this
module pins what happens to it AFTER a review adjudication rules on the
petition it filed:

1. **CONFIRMED counts as accepted-with-residual.** A `petitioned` task
   whose own engine-filed obligation (`petitions.task_petition_
   obligation`) has a run-scope CONFIRMED adjudication (the SAME record
   `record_confirmed_adjudications` persists, task 011/#112) is excluded
   from `LoopSupervisor._outstanding_for_completion` -- so
   `_apply_worker_plan_complete_report`'s `plan_complete` contract check
   accepts a plan with it still sitting at `petitioned`, never demanding
   it become `completed`/`validated`/`skipped` first.
2. **REFUTED lands it `failed`.** `petitions.apply_refuted_petition_
   adjudications` (and its `LoopSupervisor._apply_refuted_petition_
   adjudications` write-back wrapper, wired into `_review_verdict` beside
   `record_confirmed_adjudications`) moves a matching `petitioned` task to
   `failed`, carrying the refutation's own "what would satisfy it" text
   (its `remedy` field) onto the task's `validationNotes`.
3. **Un-adjudicated is neither.** A `petitioned` task with no run-scope
   CONFIRMED entry for its obligation still blocks `plan_complete`
   exactly like a `failed` task does (`task_blocks_completion`'s own
   existing rule, untouched by this task) -- and `petitioned` itself is
   never added to `state.TASK_COMPLETION_STATUSES`, so it is never
   confused with `validated`.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, atomic_write_json

TASK_ID = "001"
OTHER_TASK_ID = "002"
OBLIGATION = "Task 001: make the impossible thing work"
REMEDY_TEXT = "split criterion 2 into a separate task that runs after task 003 lands"


def _task(id_=TASK_ID, **over) -> dict:
    task = {"id": id_, "title": "make the impossible thing work",
            "status": "petitioned", "successCriteria": "criterion 2 holds",
            "validationNotes": "criterion 2 cannot be verified as written"}
    task.update(over)
    return task


def _supervisor(root: Path, workspace: Path | None = None) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    cfg = JobConfig(run_id="task010-petitioned-plan-complete", iterations=5,
                    max_approaches=1, vigilant=False, on_complete="exit")
    return LoopSupervisor(cfg, run, workspace or (root.parent / "ws"))


def _write_plan(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


# ---------------------------------------------------------------------------
# `petitions.task_petition_obligation` -- the shared obligation string
# ---------------------------------------------------------------------------


def test_task_petition_obligation_matches_the_exhaustion_filer():
    task = _task()
    assert petitions.task_petition_obligation(task) == OBLIGATION


# ---------------------------------------------------------------------------
# 1. CONFIRMED counts as accepted-with-residual
# ---------------------------------------------------------------------------


def test_a_confirmed_petitioned_task_is_not_outstanding_for_completion(tmp_path):
    sup = _supervisor(tmp_path / "run")
    _write_plan(sup, [_task()])
    petitions.record_confirmed_adjudications(
        sup.run, [{"obligation": OBLIGATION, "adjudication": "CONFIRMED"}])

    doc = sup.run.read_tasks()
    assert sup._outstanding_for_completion(doc) == []


def test_plan_complete_accepts_the_plan_with_a_confirmed_petitioned_task(tmp_path):
    sup = _supervisor(tmp_path / "run")
    petitions.record_confirmed_adjudications(
        sup.run, [{"obligation": OBLIGATION, "adjudication": "CONFIRMED"}])
    doc = {"version": 1, "tasks": [_task()]}

    errors = sup._apply_worker_plan_complete_report(None, None, doc)

    assert errors == []


def test_petitioned_task_accepted_helper_matches_a_confirmed_obligation():
    confirmed, tampered = set(), False
    confirmed = {"task 001: make the impossible thing work"}
    assert petitions.petitioned_task_accepted(_task(), confirmed) is True
    assert petitions.petitioned_task_accepted(_task(), set()) is False


# ---------------------------------------------------------------------------
# 2. REFUTED lands it `failed`, with the remedy recorded
# ---------------------------------------------------------------------------


def test_apply_refuted_petition_adjudications_moves_the_task_to_failed():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    changed = petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT},
    ])

    assert changed == [TASK_ID]
    task = tasks_doc["tasks"][0]
    assert task["status"] == "failed"
    assert task["validationNotes"] == REMEDY_TEXT
    assert task["failureKind"] == state.TASK_FAILURE_VALIDATION_EXHAUSTED


def test_apply_refuted_petition_adjudications_leaves_non_matching_tasks_alone():
    tasks_doc = {"version": 1,
                 "tasks": [_task(), _task(id_=OTHER_TASK_ID, title="unrelated")]}
    petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT},
    ])

    other = next(t for t in tasks_doc["tasks"] if t["id"] == OTHER_TASK_ID)
    assert other["status"] == "petitioned"


def test_apply_refuted_petition_adjudications_ignores_confirmed_entries():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    changed = petitions.apply_refuted_petition_adjudications(tasks_doc, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    assert changed == []
    assert tasks_doc["tasks"][0]["status"] == "petitioned"


def test_apply_refuted_petition_adjudications_is_a_noop_with_no_adjudications():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    assert petitions.apply_refuted_petition_adjudications(tasks_doc, None) == []
    assert petitions.apply_refuted_petition_adjudications(tasks_doc, []) == []
    assert tasks_doc["tasks"][0]["status"] == "petitioned"


def test_supervisor_write_back_persists_the_refuted_transition(tmp_path):
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
    assert on_disk["failureKind"] == state.TASK_FAILURE_VALIDATION_EXHAUSTED


def test_a_refuted_then_failed_task_still_blocks_plan_complete(tmp_path):
    sup = _supervisor(tmp_path / "run")
    _write_plan(sup, [_task()])
    sup._apply_refuted_petition_adjudications([
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY_TEXT},
    ])

    doc = sup.run.read_tasks()
    outstanding = sup._outstanding_for_completion(doc)
    assert [t["id"] for t in outstanding] == [TASK_ID]
    assert outstanding[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# 3. un-adjudicated: neither passes silently nor counts as validated
# ---------------------------------------------------------------------------


def test_an_unadjudicated_petitioned_task_blocks_plan_complete(tmp_path):
    sup = _supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_task()]}

    errors = sup._apply_worker_plan_complete_report(None, None, doc)

    assert errors
    assert TASK_ID in errors[0]
    assert "petitioned" in errors[0]


def test_an_unadjudicated_petitioned_task_is_outstanding(tmp_path):
    sup = _supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_task()]}

    outstanding = sup._outstanding_for_completion(doc)

    assert [t["id"] for t in outstanding] == [TASK_ID]


def test_petitioned_is_not_a_completion_status():
    assert "petitioned" not in state.TASK_COMPLETION_STATUSES
    assert "validated" != "petitioned"


def test_a_tampered_adjudication_record_is_treated_as_no_confirmation(tmp_path):
    """Same fail-closed posture `_review_findings_channel` already takes
    for the identical record (task 011's own docstring): a tampered
    CONFIRMED record must never manufacture an exemption for a
    `petitioned` task either."""
    sup = _supervisor(tmp_path / "run")
    _write_plan(sup, [_task()])
    petitions.record_confirmed_adjudications(
        sup.run, [{"obligation": OBLIGATION, "adjudication": "CONFIRMED"}])
    # Tamper with the record directly -- same technique other petition
    # tests use to force `read_confirmed_adjudications`'s tampered branch.
    sup.run.petition_adjudications_file.write_text(
        '{"entries": [], "digest": "bogus", "mac": "bogus"}')

    doc = sup.run.read_tasks()
    outstanding = sup._outstanding_for_completion(doc)
    assert [t["id"] for t in outstanding] == [TASK_ID]
