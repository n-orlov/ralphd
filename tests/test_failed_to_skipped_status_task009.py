"""Task 009 (C.1, #123, option a): sanction `failed -> skipped` in the
declared task-status table, carried by the pre-existing `task_status`
contract channel (`LoopSupervisor._apply_reported_task_status`) -- the
harness applies the move, never the worker, and only when the payload
carries a `justification` that then lands in the task's own `notes` field.

Three things this module pins that no pre-existing module does:

1. `failed -> skipped` is declared in `TASK_STATUS_TABLE`, and shows up in
   BOTH `task_status_table_for(True)` and `task_status_table_for(False)` --
   the vigilant delta never touches this key, so it is inherited, not
   duplicated.
2. The semantic check (`transitions.task_status_report_errors`) refuses a
   `failed -> skipped` report with no (or a blank) `justification`, and
   accepts one that carries a real one -- and the harness-side applier
   (`_apply_reported_task_status`) writes that justification into the
   task's `notes` field in the very same disk write that flips its status.
3. `skipped` is still absent from `state.TASK_DEPENDENCY_SATISFIED_STATUSES`
   -- the caveat this move does not get to quietly erase.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import TASK_DEPENDENCY_SATISFIED_STATUSES, RunDir


def _sup(tmp_path: Path, tasks: list[dict]) -> LoopSupervisor:
    run = RunDir(root=tmp_path / "run")
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))
    cfg = JobConfig(run_id="task009", iterations=1, max_approaches=1,
                    vigilant=True, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    return LoopSupervisor(cfg, run, tmp_path / "workspace")


# -- 1. declared in both variants -------------------------------------------

def test_failed_to_skipped_declared_in_the_base_table():
    assert tr.TASK_STATUS_TABLE["failed"] == ("skipped",)


def test_failed_to_skipped_declared_in_both_composed_variants():
    assert tr.task_status_table_for(True)["failed"] == ("skipped",)
    assert tr.task_status_table_for(False)["failed"] == ("skipped",)
    # not part of the vigilant delta -- it is inherited from the base
    # table unchanged, same as every other terminal-shaped entry that does
    # not differ across variants.
    assert "failed" not in tr.TASK_STATUS_VIGILANT_DELTA


def test_failed_to_skipped_is_legal_in_both_variants():
    assert tr.is_legal_task_status_transition("failed", "skipped", True)
    assert tr.is_legal_task_status_transition("failed", "skipped", False)


# -- 2. the semantic check: justification required, then written to notes --

def test_report_errors_refuses_a_failed_to_skipped_move_with_no_justification():
    errors = tr.task_status_report_errors(
        {"task_id": "010", "task_status": "skipped"}, "failed", True)
    assert errors
    assert "justification" in errors[0]


def test_report_errors_refuses_a_blank_justification():
    errors = tr.task_status_report_errors(
        {"task_id": "010", "task_status": "skipped", "justification": "   "},
        "failed", False)
    assert errors
    assert "justification" in errors[0]


def test_report_errors_accepts_a_real_justification():
    errors = tr.task_status_report_errors(
        {"task_id": "010", "task_status": "skipped",
         "justification": "criteria required hardware this sandbox lacks"},
        "failed", True)
    assert errors == []


def test_other_declared_moves_are_untouched_by_the_new_condition():
    # the justification requirement is scoped to failed -> skipped only --
    # an ordinary in-progress -> completed report needs no such field.
    errors = tr.task_status_report_errors(
        {"task_id": "010", "task_status": "completed"}, "in-progress", False)
    assert errors == []


def test_applier_refuses_and_writes_nothing_without_a_justification(tmp_path):
    sup = _sup(tmp_path, [
        {"id": "010", "title": "t", "status": "failed",
         "successCriteria": "do X"},
    ])
    errors = sup._apply_reported_task_status(
        {"task_id": "010", "task_status": "skipped"})
    assert errors
    assert "justification" in errors[0]
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "failed"
    assert "notes" not in task


def test_applier_applies_the_move_and_writes_the_justification_into_notes(tmp_path):
    sup = _sup(tmp_path, [
        {"id": "010", "title": "t", "status": "failed",
         "successCriteria": "do X"},
    ])
    errors = sup._apply_reported_task_status({
        "task_id": "010", "task_status": "skipped",
        "justification": "the residual gap was carved into task 011 instead",
    })
    assert errors == []
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "skipped"
    assert task["notes"] == "the residual gap was carved into task 011 instead"


def test_applier_emits_the_task_status_event_for_the_move(tmp_path):
    sup = _sup(tmp_path, [
        {"id": "010", "title": "t", "status": "failed",
         "successCriteria": "do X"},
    ])
    sup._apply_reported_task_status({
        "task_id": "010", "task_status": "skipped",
        "justification": "cannot be built in this sandbox",
    })
    from ralphd.engine.state import read_events
    events = [e for e in read_events(sup.run.root) if e.get("type") == "task"]
    assert any(e.get("taskId") == "010" and e.get("newStatus") == "skipped"
              for e in events)


# -- 3. the caveat: skipped never un-dooms a dependent -----------------------

def test_skipped_is_still_absent_from_dependency_satisfied_statuses():
    assert "skipped" not in TASK_DEPENDENCY_SATISFIED_STATUSES
    assert TASK_DEPENDENCY_SATISFIED_STATUSES == ("completed", "validated")
