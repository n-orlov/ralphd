"""Fix for #132: a steer `task-update` edit to `successCriteria` is
operator-origin and just as legal as a judge-authored criteria amendment
(`LoopSupervisor._apply_criteria_amendments`, loop.py:6891-6898). Before
this fix, `steer_task_update` wrote the new text with a plain
`task.update(changes)` and left `criteriaFingerprint`/`criteriaOfRecord`
pointing at the OLD text. The very next pass of
`LoopSupervisor._check_criteria_edits` (loop.py:7253-7321) then saw the
mismatch, RESTORED the old text, emitted `task.criteria_edit_refused` and
set the sticky `criteriaEditedAfterValidationFailure` marker -- undoing a
legitimate operator steer and dooming the approach via
`findings.flagged_criteria_findings` on every later review pass.

This module pins two things directly against `steer_task_update` (no
LoopSupervisor, no review pass -- state.py's own contract):

1. A steer update carrying `successCriteria` moves `criteriaFingerprint`
   (the SAME sha256-hexdigest formula `LoopSupervisor._criteria_fingerprint`
   uses, loop.py:7213-7215) and `criteriaOfRecord` (the new text verbatim)
   forward, in the SAME `run.write_tasks` call as the text itself.
2. A steer update carrying no `successCriteria` leaves both untouched --
   this fix must not start stamping a fingerprint on every unrelated
   field-only edit.

`tests/test_contract_events.py` stays green unmodified:
this module never touches `_check_criteria_edits` or the tamper marker,
only the write path the tamper check reads its baseline from.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ralphd.engine.state import RunDir, steer_task_update


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))


def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def test_steer_edit_moves_fingerprint_and_criteria_of_record_forward(tmp_path):
    old_criteria = "do X"
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "132a", "title": "t", "status": "pending",
         "successCriteria": old_criteria,
         "criteriaFingerprint": _fingerprint(old_criteria),
         "criteriaOfRecord": old_criteria},
    ])

    new_criteria = "do X, relaxed per operator steer 003"
    result = steer_task_update(
        run, "132a", {"successCriteria": new_criteria}, False)

    assert result["updated"] is True
    task = run.read_tasks()["tasks"][0]
    assert task["successCriteria"] == new_criteria
    assert task["criteriaFingerprint"] == _fingerprint(new_criteria)
    assert task["criteriaOfRecord"] == new_criteria
    # the write actually landed on disk, not just in the in-memory dict
    assert task["criteriaFingerprint"] != _fingerprint(old_criteria)


def test_steer_edit_with_no_criteria_key_leaves_fingerprint_untouched(tmp_path):
    old_criteria = "do Y"
    fp = _fingerprint(old_criteria)
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "132b", "title": "old title", "status": "pending",
         "successCriteria": old_criteria,
         "criteriaFingerprint": fp,
         "criteriaOfRecord": old_criteria},
    ])

    result = steer_task_update(run, "132b", {"title": "new title"}, False)

    assert result["updated"] is True
    task = run.read_tasks()["tasks"][0]
    assert task["title"] == "new title"
    assert task["successCriteria"] == old_criteria
    assert task["criteriaFingerprint"] == fp
    assert task["criteriaOfRecord"] == old_criteria


def test_steer_edit_on_a_task_with_no_prior_fingerprint_still_stamps_one(tmp_path):
    """A task discovered mid-run with no baseline yet (e.g. task-insert
    landed it without one) still gets a consistent fingerprint/
    criteriaOfRecord pair stamped alongside the new text -- never a write
    that leaves the two out of sync."""
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "132c", "title": "t", "status": "pending",
         "successCriteria": "do Z"},
    ])

    new_criteria = "do Z, amended"
    steer_task_update(run, "132c", {"successCriteria": new_criteria}, False)

    task = run.read_tasks()["tasks"][0]
    assert task["successCriteria"] == new_criteria
    assert task["criteriaFingerprint"] == _fingerprint(new_criteria)
    assert task["criteriaOfRecord"] == new_criteria
