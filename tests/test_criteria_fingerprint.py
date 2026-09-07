"""Unit tests for task 008: per-task successCriteria fingerprinting and the
criteriaEditedAfterValidationFailure marker.

These exercise LoopSupervisor's helper methods directly against a bare
RunDir (no subprocess, no `pi` stub) since the fingerprinting/flagging logic
is pure tasks.json bookkeeping with no agent interaction of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory


# The shared harness (tests/conftest.py, G.1).
_supervisor = supervisor_factory(run_id="unit")



def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))


def test_ensure_baseline_sets_fingerprint_without_flagging(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "pending", "successCriteria": "do X"},
    ])
    sup._ensure_criteria_baseline()
    data = sup.run.read_tasks()
    t = data["tasks"][0]
    assert "criteriaFingerprint" in t
    assert t["criteriaFingerprint"] == sup._criteria_fingerprint("do X")
    assert "criteriaEditedAfterValidationFailure" not in t


def test_unchanged_criteria_never_flagged(tmp_path):
    """Negative case: criteria that never change, even after a validation
    failure, must not be flagged."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "validation-failed",
         "successCriteria": "do X", "validationAttempts": 1},
    ])
    sup._ensure_criteria_baseline()
    tasks_data = sup.run.read_tasks()
    # simulate another observation with identical criteria text
    sup._check_criteria_edits(tasks_data)
    on_disk = sup.run.read_tasks()
    assert "criteriaEditedAfterValidationFailure" not in on_disk["tasks"][0]


def test_criteria_change_before_any_failure_is_now_refused_task024(tmp_path):
    """Task 024 (requirement E.3, #116 section 2) supersedes task 008's
    original negative case here: a criteria change observed while
    validationAttempts is still 0 is NO LONGER a silent, unflagged
    baseline update -- the engine refuses a worker/planner rewrite
    regardless of how many validation failures (if any) preceded it, so
    the marker is set even at validationAttempts 0 AND the rewrite is
    undone from the verbatim criteria-of-record the engine keeps beside
    the hash."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "in-progress",
         "successCriteria": "do X"},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = "do X, revised"
    sup.run.tasks_file.write_text(json.dumps(tasks_data))

    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()
    t = on_disk["tasks"][0]
    assert t["criteriaEditedAfterValidationFailure"] is True
    assert t.get("validationAttempts", 0) == 0
    # the rewrite does not stand: successCriteria is restored and the
    # stored fingerprint still names the authoritative text (task 024
    # supersedes task 008's "track the new text forward" behaviour, which
    # now survives only for a task carrying no criteria-of-record --
    # tests/test_contract_events.py pins both halves).
    assert t["successCriteria"] == "do X"
    assert t["criteriaFingerprint"] == sup._criteria_fingerprint("do X")


def test_criteria_change_after_failure_flags_and_is_reverted(tmp_path):
    """Positive case: task fails validation, then its successCriteria is
    rewritten and it's re-marked completed -- the flag must be set and
    persist in tasks.json, and (task 024) the rewrite itself must not
    stand, while nothing else on the record is touched."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "pending",
         "successCriteria": "do X"},
    ])
    sup._ensure_criteria_baseline()

    # Simulate a failed verification (mirrors _verify_task's own mutation)
    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["status"] = "validation-failed"
    tasks_data["tasks"][0]["validationAttempts"] = 1
    tasks_data["tasks"][0]["validationNotes"] = "did not meet the bar"
    sup.run.tasks_file.write_text(json.dumps(tasks_data))

    # Simulate a worker rewriting the criteria and re-marking completed,
    # instead of doing the work
    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = "do X (rewritten by worker)"
    tasks_data["tasks"][0]["status"] = "completed"
    sup.run.tasks_file.write_text(json.dumps(tasks_data))

    sup._check_criteria_edits(sup.run.read_tasks())

    on_disk = sup.run.read_tasks()
    t = on_disk["tasks"][0]
    assert t["criteriaEditedAfterValidationFailure"] is True
    assert t["status"] == "completed"
    assert t["validationAttempts"] == 1
    assert t["successCriteria"] == "do X"
    assert t["criteriaFingerprint"] == sup._criteria_fingerprint("do X")


def test_flag_persists_once_set_even_if_criteria_changes_again(tmp_path):
    """A task carrying a fingerprint but no verbatim criteria-of-record (a
    run resumed from before task 024) cannot have its rewrite undone -- a
    sha256 is one-way -- so task 008's flag-and-track-forward behaviour
    stands there, and the flag persists."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "completed",
         "successCriteria": "do X (rewritten)",
         "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint("do X"),
         "criteriaEditedAfterValidationFailure": True},
    ])
    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()
    assert on_disk["tasks"][0]["criteriaEditedAfterValidationFailure"] is True


def test_new_task_discovered_mid_run_gets_baseline_not_flagged(tmp_path):
    """A task added mid-run (e.g. discovered work) with no stored fingerprint
    yet must be baselined on first sight, never flagged, even if some other
    task in the same file already has validationAttempts >= 1."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "completed",
         "successCriteria": "do X", "validationAttempts": 2,
         "criteriaFingerprint": sup._criteria_fingerprint("do X")},
        {"id": "002", "title": "t2 (discovered)", "status": "pending",
         "successCriteria": "do Y"},
    ])
    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()
    t2 = next(t for t in on_disk["tasks"] if t["id"] == "002")
    assert t2["criteriaFingerprint"] == sup._criteria_fingerprint("do Y")
    assert "criteriaEditedAfterValidationFailure" not in t2
