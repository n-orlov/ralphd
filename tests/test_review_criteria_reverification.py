"""Unit tests for task 009: feeding criteriaEditedAfterValidationFailure
(task 008) tasks into the review prompt/context and requiring an explicit
independent re-verification of each such task before VERIFIED.

Exercises LoopSupervisor._flagged_criteria_review_context() and
build_prompt() directly against a bare RunDir -- no subprocess, no `pi`
stub -- since this is pure prompt-context construction with no agent
interaction of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir


def _supervisor(tmp_path: Path) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    cfg = JobConfig(run_id="unit")
    return LoopSupervisor(cfg, run, tmp_path)


def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))


def test_no_flagged_tasks_yields_empty_context(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "completed",
         "successCriteria": "do X"},
        {"id": "002", "title": "t2", "status": "validation-failed",
         "successCriteria": "do Y", "validationAttempts": 1},
    ])
    assert sup._flagged_criteria_review_context() == ""


def test_flagged_task_appears_in_context_with_reverification_requirement(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "completed",
         "successCriteria": "do X (rewritten by worker)",
         "validationAttempts": 1,
         "criteriaEditedAfterValidationFailure": True},
        {"id": "002", "title": "t2", "status": "completed",
         "successCriteria": "do Y"},
    ])
    ctx = sup._flagged_criteria_review_context()
    assert "001" in ctx
    assert "do X (rewritten by worker)" in ctx
    assert "re-verify" in ctx.lower()
    assert "002" not in ctx


def test_flagged_context_reaches_the_rendered_review_prompt(tmp_path):
    """The full build_prompt("review", extra=...) pipeline must surface the
    flagged task id and the re-verification instruction to the reviewer,
    while an unflagged task never appears in that section."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "007", "title": "flagged task", "status": "completed",
         "successCriteria": "new bar",
         "validationAttempts": 2,
         "criteriaEditedAfterValidationFailure": True},
        {"id": "008", "title": "clean task", "status": "completed",
         "successCriteria": "unchanged bar"},
    ])
    extra = sup._flagged_criteria_review_context()
    prompt = sup.build_prompt("review", extra=extra)
    assert "007" in prompt
    assert "new bar" in prompt
    assert "re-verify" in prompt.lower()
    # the requirement is stated as mandatory, not optional
    assert "must" in prompt.lower()


def test_review_prompt_unaffected_when_nothing_flagged(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "001", "title": "t1", "status": "completed",
         "successCriteria": "do X"},
    ])
    extra = sup._flagged_criteria_review_context()
    assert extra == ""
    prompt = sup.build_prompt("review", extra=extra)
    # No flagged-task section header (the review.md instructions mentioning
    # the section's existence conditionally are separate and always present)
    assert "## Criteria edited after a validation failure" not in prompt
