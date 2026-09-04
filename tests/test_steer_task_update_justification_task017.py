"""Task 017 (PRD requirement F.4, #147): the steer hat's `task-update` tool
carries a `justification` through to the transition check and into the
task's own `notes` on a `failed -> skipped` move, exactly as the worker
contract channel's `_apply_reported_task_status` already does for the same
declared move (task 011/#123).

The defect (issue #147, observed live at v0.17 event 1636): the steer
path's applier (`state.apply_task_status_change`) checked ONLY
`transitions.is_legal_task_status_transition` -- which the table has
declared legal since task 009 -- and never ran
`transitions.task_status_report_errors`'s extra condition on this one
move (a non-empty justification), so a `failed -> skipped` steer never
required one and never wrote anything to `notes`.

Task 038 (the operator-steered cure for task 017's one residual gap, same
requirement F.4) closes the case the first attempt missed: a call that
carries BOTH an explicit `justification` AND a distinct `changes["notes"]`
used to lose the justification, because `steer_task_update`'s plain
dict-merge of `changes` ran AFTER the applier's own `notes` write and
overwrote it. The justification is now appended to the requested notes as
their authoritative tail, never replaced by them.

This is the `hat_tools` unit tier: it drives `hat_tools.SteerToolBroker.
execute("task-update", ...)` directly -- the real dispatch a served
request goes through -- with no subprocess and no `pi` stub, mirroring
`test_steer_hat_tool_surface.py`'s (state-level) and
`test_steer_hat_tools_live.py`'s (live, in-process) sibling disciplines
one layer further down: exactly the call `state.STEER_HAT_TOOLS[
"task-update"]` is handed.
"""
from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import hat_tools
from ralphd.engine.state import RunDir, atomic_write_json


def _broker(tmp_path: Path, tasks: list[dict]) -> hat_tools.SteerToolBroker:
    run = RunDir(root=tmp_path / "run")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    return hat_tools.SteerToolBroker(run, vigilant=False,
                                     steering=("001-opX.md",))


def _task(broker: hat_tools.SteerToolBroker, task_id: str) -> dict:
    for t in broker.run.read_tasks()["tasks"]:
        if t.get("id") == task_id:
            return t
    raise AssertionError(f"no task {task_id!r}")


def _events(broker: hat_tools.SteerToolBroker, type_: str) -> list[dict]:
    log = broker.run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == type_]


# -- accepted: an explicit top-level justification lands in notes ---------

def test_explicit_justification_is_accepted_and_lands_in_notes(tmp_path):
    broker = _broker(tmp_path, [
        {"id": "013", "title": "t", "status": "failed",
         "successCriteria": "do X"},
    ])
    response = broker.execute("task-update", {
        "taskId": "013",
        "changes": {"status": "skipped"},
        "justification": "unmeetable per petition p1; residual disclosed",
    })
    assert response["ok"] is True, response
    task = _task(broker, "013")
    assert task["status"] == "skipped"
    assert task["notes"] == "unmeetable per petition p1; residual disclosed"
    # never touched by the status-only move
    assert task["successCriteria"] == "do X"
    events = _events(broker, "amendment_status_applied")
    assert len(events) == 1
    assert events[0]["source"] == "steer"
    assert events[0]["previous"] == "failed"
    assert events[0]["status"] == "skipped"
    assert events[0]["justification"] == (
        "unmeetable per petition p1; residual disclosed")


# -- accepted: no explicit justification, defaults to changes.notes -------

def test_justification_defaults_to_changes_notes_when_omitted(tmp_path):
    broker = _broker(tmp_path, [
        {"id": "014", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "014",
        "changes": {"status": "skipped",
                    "notes": "superseded by task 020, carved gap landed"},
    })
    assert response["ok"] is True, response
    task = _task(broker, "014")
    assert task["status"] == "skipped"
    assert task["notes"] == "superseded by task 020, carved gap landed"


# -- task 038: an explicit justification survives a distinct changes.notes

def test_explicit_justification_survives_a_distinct_changes_notes(tmp_path):
    """Task 038 (the cure for task 017's residual gap, requirement F.4):
    when the SAME call carries both a top-level `justification` and a
    different `changes["notes"]`, the justification used to be written by
    the applier and then silently overwritten by the plain dict-merge of
    `changes`. It is now appended to the requested notes -- as the last
    segment, never replaced by them -- and every other requested field
    update still lands."""
    broker = _broker(tmp_path, [
        {"id": "018", "title": "t", "status": "failed",
         "successCriteria": "do Z", "priority": 3},
    ])
    response = broker.execute("task-update", {
        "taskId": "018",
        "changes": {"status": "skipped",
                    "notes": "see artifacts/reports/v0.18-petitions.md",
                    "priority": 9},
        "justification": "criteria unmeetable in this sandbox; "
                         "residual disclosed",
    })
    assert response["ok"] is True, response
    task = _task(broker, "018")
    assert task["status"] == "skipped"
    notes = task["notes"]
    # BOTH texts are present ...
    assert "see artifacts/reports/v0.18-petitions.md" in notes
    assert ("criteria unmeetable in this sandbox; residual disclosed"
            in notes)
    # ... and the justification is the authoritative tail, appended after
    # the requested notes rather than overwritten by them.
    assert notes.endswith(
        "criteria unmeetable in this sandbox; residual disclosed")
    assert notes != "see artifacts/reports/v0.18-petitions.md"
    # other requested field updates are preserved
    assert task["priority"] == 9
    assert task["successCriteria"] == "do Z"
    events = _events(broker, "amendment_status_applied")
    assert len(events) == 1
    assert events[0]["justification"] == (
        "criteria unmeetable in this sandbox; residual disclosed")


def test_a_justification_equal_to_changes_notes_is_not_duplicated(tmp_path):
    """The append is idempotent: a caller that passes the same text in both
    places (or lets `justification` default to `changes.notes`) gets it
    exactly once, not twice."""
    broker = _broker(tmp_path, [
        {"id": "019", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "019",
        "changes": {"status": "skipped", "notes": "superseded by task 038"},
        "justification": "superseded by task 038",
    })
    assert response["ok"] is True, response
    assert _task(broker, "019")["notes"] == "superseded by task 038"


def test_a_blank_changes_notes_does_not_shadow_the_justification(tmp_path):
    broker = _broker(tmp_path, [
        {"id": "020", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "020",
        "changes": {"status": "skipped", "notes": "   "},
        "justification": "gap carved into task 041",
    })
    assert response["ok"] is True, response
    assert _task(broker, "020")["notes"] == "gap carved into task 041"


def test_only_failed_to_skipped_appends_the_justification(tmp_path):
    """The extra condition -- and so the notes rule that carries it -- is
    declared for exactly one edge; any other move writes the requested
    `notes` verbatim."""
    broker = _broker(tmp_path, [
        {"id": "021", "title": "t", "status": "validation-failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "021",
        "changes": {"status": "skipped", "notes": "operator call"},
        "justification": "not required on this edge",
    })
    assert response["ok"] is True, response
    assert _task(broker, "021")["notes"] == "operator call"


# -- refused: no justification anywhere, status stays put -----------------

def test_missing_justification_is_refused_with_the_appliers_own_diagnostic(
        tmp_path):
    broker = _broker(tmp_path, [
        {"id": "015", "title": "t", "status": "failed",
         "successCriteria": "do Y"},
    ])
    response = broker.execute("task-update", {
        "taskId": "015",
        "changes": {"status": "skipped"},
    })
    assert response["ok"] is False, response
    assert "requires a non-empty justification" in (response["error"] or "")
    assert response["detail"]["statusResult"]["applied"] is False
    task = _task(broker, "015")
    # the status must NOT have moved, and notes must not have been touched
    assert task["status"] == "failed"
    assert "notes" not in task
    assert _events(broker, "amendment_status_applied") == []


def test_missing_justification_with_blank_notes_is_still_refused(tmp_path):
    """An empty/whitespace-only `notes` does not count as a stated reason
    any more than an empty `justification` argument would -- both are
    "no justification given" to `transitions.task_status_report_errors`."""
    broker = _broker(tmp_path, [
        {"id": "016", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "016",
        "changes": {"status": "skipped", "notes": "   "},
    })
    assert response["ok"] is False, response
    task = _task(broker, "016")
    assert task["status"] == "failed"


def test_the_refusal_still_surfaces_the_diagnostic_beside_other_changes(
        tmp_path):
    """Task 038: the refused-without-justification case is unchanged by the
    append rule -- the status stays `failed`, the applier's own diagnostic
    reaches the caller, and nothing writes a justification into `notes`."""
    broker = _broker(tmp_path, [
        {"id": "022", "title": "t", "status": "failed", "priority": 2},
    ])
    response = broker.execute("task-update", {
        "taskId": "022",
        "changes": {"status": "skipped", "priority": 7},
    })
    assert response["ok"] is False, response
    assert "requires a non-empty justification" in (response["error"] or "")
    assert response["detail"]["statusResult"]["applied"] is False
    task = _task(broker, "022")
    assert task["status"] == "failed"
    assert "notes" not in task
    assert _events(broker, "amendment_status_applied") == []


# -- an unaffected transition is untouched by the new check ---------------

def test_a_transition_other_than_failed_to_skipped_needs_no_justification(
        tmp_path):
    """The extra condition is declared for exactly one edge
    (`transitions.task_status_report_errors`); every other legal move
    through this same tool is unaffected."""
    broker = _broker(tmp_path, [
        {"id": "017", "title": "t", "status": "validation-failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "017",
        "changes": {"status": "skipped"},
    })
    assert response["ok"] is True, response
    assert _task(broker, "017")["status"] == "skipped"
