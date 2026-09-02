"""Task 024 (requirement E.3, #116 section 2): the criteria-tamper
residual task 008's draft left open, closed engine-side.

Two boundaries, unit-level (`_check_criteria_edits`/`_review_verdict`
called directly against a bare `RunDir`, no subprocess, no `pi` stub --
the same style `tests/test_criteria_fingerprint.py` already uses), plus a
byte-identical-in-both-directions pin on the one legal escape hatch:

1. `_check_criteria_edits` refuses (flags) a worker/planner-authored
   `successCriteria` rewrite UNCONDITIONALLY -- including while
   `validationAttempts` is still 0, which task 008 originally left as a
   silent, unflagged baseline update (`tests/test_criteria_fingerprint.py`'s
   `test_criteria_change_before_any_failure_is_now_flagged_task024`, in
   the SAME module, pins the superseded shape; this module adds its own
   independent assertion of the same fact so the new module's own pytest
   command line is sufficient evidence on its own).
2. `engine.findings.flagged_criteria_findings` -- wired into
   `LoopSupervisor._review_findings_channel` -- forces a recorded
   `severity: "blocking"` finding for any task carrying the
   `criteriaEditedAfterValidationFailure` marker, so a review payload's
   OWN claimed `verdict: "verified"` over such a task contradicts its own
   (engine-augmented) findings channel and is refused by
   `_review_verdict` exactly like any other engine-detected finding --
   never silently kept, regardless of what the review model itself said.
3. The judge-authored amendment path (`_apply_criteria_amendments`) is
   pinned unaffected in both directions: a task with NO tamper flag never
   trips the new forced finding, and a legitimate amendment's own review
   payload (which reports `rejected` with `petition_adjudications`, never
   `verified` over the SAME payload) is untouched by this task's change.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import findings as findings_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir


def _supervisor(tmp_path: Path) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    cfg = JobConfig(run_id="unit")
    return LoopSupervisor(cfg, run, tmp_path)


def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


# ---------------------------------------------------------------------------
# 1. _check_criteria_edits refuses (flags) a rewrite at validationAttempts 0.
# ---------------------------------------------------------------------------

def test_check_criteria_edits_flags_rewrite_at_validation_attempts_zero(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "pending",
         "successCriteria": "do X", "validationAttempts": 0},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = "do X, rewritten with no failure yet"
    sup.run.tasks_file.write_text(json.dumps(tasks_data))

    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]

    assert on_disk.get("validationAttempts", 0) == 0
    assert on_disk["criteriaEditedAfterValidationFailure"] is True


# ---------------------------------------------------------------------------
# 2. engine.findings.flagged_criteria_findings + the wired-in engine
#    refusal of a "verified" claim over a flagged task.
# ---------------------------------------------------------------------------

def test_flagged_criteria_findings_reports_one_blocking_finding_per_flagged_task():
    tasks = [
        {"id": "1", "successCriteria": "rewritten text",
         "criteriaEditedAfterValidationFailure": True},
        {"id": "2", "successCriteria": "clean text"},
    ]
    out = findings_mod.flagged_criteria_findings(tasks)
    assert len(out) == 1
    assert out[0]["severity"] == "blocking"
    assert out[0]["curable"] is True
    assert "1" in out[0]["clause"]
    assert findings_mod.validate_finding(out[0]) == []


def test_flagged_criteria_findings_empty_when_nothing_flagged():
    tasks = [{"id": "1", "successCriteria": "clean text"}]
    assert findings_mod.flagged_criteria_findings(tasks) == []


def test_review_verdict_refuses_verified_claim_over_a_flagged_task(tmp_path):
    sup = _supervisor(tmp_path)
    rewritten = "the criteria text a worker rewrote on its own authority"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": rewritten, "validationAttempts": 1,
         "criteriaEditedAfterValidationFailure": True,
         "criteriaFingerprint": sup._criteria_fingerprint(rewritten)},
    ])
    # review.md always produces a findings.json in production (task 004);
    # an empty-but-present channel is the minimal fixture that exercises
    # `_review_findings_channel`'s append path rather than its
    # no-channel-at-all early return.
    findings_mod.write_findings_json(sup.run.findings_json_file, [])

    result = _ok(json.dumps({"verdict": "verified"}))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is False
    assert contract_errors
    assert any("blocking" in e for e in contract_errors)
    # the rewrite does not stand: nothing about the task's own text was
    # touched by this refusal -- refusal happens at the verdict, not by
    # silently reverting successCriteria.
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == rewritten


def test_review_verdict_unaffected_when_no_task_is_flagged(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": "clean text", "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint("clean text")},
    ])
    findings_mod.write_findings_json(sup.run.findings_json_file, [])

    result = _ok(json.dumps({"verdict": "verified"}))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is True
    assert not contract_errors


# ---------------------------------------------------------------------------
# 3. The judge-authored amendment path is untouched, in both directions.
# ---------------------------------------------------------------------------

def test_judge_amendment_payload_is_unaffected_by_the_new_engine_check(tmp_path):
    """The legitimate amendment path's own review payload reports
    `rejected` alongside `petition_adjudications` (never `verified` over
    the SAME settled payload -- `_apply_criteria_amendments` runs from
    inside `_review_verdict` itself, on this exact call), so this task's
    change to `_review_findings_channel`/`_review_verdict` has nothing to
    contradict here: unaffected in the positive direction."""
    sup = _supervisor(tmp_path)
    old_text = ("this task's own successCriteria can only be verified using "
                "a state its own satisfaction would have to create first")
    new_text = "a fresh, achievable criterion that names no unmet state"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": old_text, "validationAttempts": 2,
         "criteriaFingerprint": sup._criteria_fingerprint(old_text)},
    ])
    reports = tmp_path / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.16-petitions.md").write_text(
        "# v0.16 petitions\n\n## Petition: task024 fixture\n\n"
        f"**Obligation**: {old_text}\n**Type**: self-referential\n"
        "**Evidence**: fixture evidence\n")

    result = _ok(json.dumps({
        "verdict": "rejected",
        "reason": "re-scoping task 1's criteria via a CONFIRMED petition",
        "petition_adjudications": [{
            "obligation": old_text,
            "adjudication": "CONFIRMED",
            "task_id": "1",
            "replacement_criteria": new_text,
        }],
    }))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is False  # honestly rejected, exactly as the payload claims
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == new_text
    assert on_disk["criteriaFingerprint"] == sup._criteria_fingerprint(new_text)
    assert on_disk["validationAttempts"] == 0
    # the amendment itself never sets the tamper marker -- so the NEW
    # forced-blocking-finding for a subsequent, honest "verified" pass
    # over this SAME (now legitimately re-scoped) task never fires.
    assert "criteriaEditedAfterValidationFailure" not in on_disk


def test_judge_amendment_of_a_non_task_target_is_still_refused_and_named(tmp_path):
    """Negative direction, unaffected: an amendment naming something that
    is not a real task at all is still refused by
    `petitions.criteria_amendment_refusals`, exactly as task 008 pinned,
    with the new engine check adding nothing and taking nothing away."""
    sup = _supervisor(tmp_path)
    subject_text = "task 1's own, perfectly ordinary successCriteria text"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": subject_text, "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint(subject_text)},
    ])
    reports = tmp_path / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.16-petitions.md").write_text(
        "# v0.16 petitions\n\n## Petition: task024 fixture\n\n"
        f"**Obligation**: {subject_text}\n**Type**: contradictory-clauses\n"
        "**Evidence**: fixture evidence\n")

    result = _ok(json.dumps({
        "verdict": "rejected",
        "reason": "attempted amendment of a non-task target",
        "petition_adjudications": [{
            "obligation": subject_text,
            "adjudication": "CONFIRMED",
            "task_id": "PRD requirement B.2",
            "replacement_criteria": "a rewritten PRD text",
        }],
    }))
    verified, payload, contract_errors = sup._review_verdict(result)

    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == subject_text

    events_path = sup.run.root / "events.jsonl"
    events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    refused = [e for e in events if e.get("type") == "task.criteria_amendment_refused"]
    assert len(refused) == 1
    assert refused[0]["target"] == "PRD requirement B.2"
