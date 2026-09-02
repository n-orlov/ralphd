"""Task 024 (requirement E.3, #116 section 2): the criteria-tamper
residual task 008's draft left open, closed engine-side.

Task 008 could only ever DETECT a worker- or planner-authored
`successCriteria` rewrite (a sha256 cannot be inverted, so the pre-rewrite
text was unrecoverable) and surface it to the review model as a prompt
instruction review could decline to act on. This module pins the three
things that changed:

1. `_check_criteria_edits` REFUSES such a rewrite -- unconditionally,
   including while `validationAttempts` is still 0, which task 008
   originally left as a silent, unflagged baseline update. Refusal means
   the rewrite does not stand: `successCriteria` is restored from the
   verbatim `criteriaOfRecord` copy the engine keeps beside the hash, the
   stored fingerprint is unchanged, the persistent tamper marker is set,
   and a `task.criteria_edit_refused` event witnesses both texts. The one
   unrestorable shape (a task carrying no recorded text, i.e. a run
   resumed from before this task) keeps task 008's flag-and-track-forward
   behaviour, pinned here too so the fallback is deliberate rather than
   accidental.
2. `engine.findings.flagged_criteria_findings` -- wired into
   `LoopSupervisor._review_findings_channel` -- forces a RECORDED
   `severity: "blocking"` finding for any task carrying the
   `criteriaEditedAfterValidationFailure` marker: persisted into review's
   own findings sidecar and announced as an event, so a review payload's
   OWN claimed `verdict: "verified"` over such a task contradicts its own
   (engine-augmented) findings channel and is refused by `_review_verdict`
   exactly like any other engine-detected finding. Pinned both with a
   present channel AND with no channel on disk at all -- a missing sidecar
   must not be an escape hatch from the refusal.
3. The judge-authored amendment path (`_apply_criteria_amendments`) is
   pinned unaffected in both directions: a legitimate amendment moves the
   criteria-of-record forward with the hash (so a later
   `_check_criteria_edits` pass never reverts it) and never sets the
   tamper flag, while an amendment naming a non-task target is still
   refused and named exactly as task 008 pinned.
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


def _events(sup: LoopSupervisor, kind: str) -> list[dict]:
    path = sup.run.root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip() and json.loads(line).get("type") == kind]


# ---------------------------------------------------------------------------
# 1. _check_criteria_edits REFUSES a rewrite -- the rewrite does not stand.
# ---------------------------------------------------------------------------

def test_check_criteria_edits_refuses_rewrite_at_validation_attempts_zero(tmp_path):
    """At `validationAttempts` 0 -- task 008's original silent case -- the
    rewrite is both flagged AND undone."""
    sup = _supervisor(tmp_path)
    original = "do X, exactly as the plan says"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "pending",
         "successCriteria": original, "validationAttempts": 0},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = "do X, rewritten with no failure yet"
    sup.run.tasks_file.write_text(json.dumps(tasks_data))

    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]

    assert on_disk.get("validationAttempts", 0) == 0
    assert on_disk["criteriaEditedAfterValidationFailure"] is True
    # the refusal: the rewrite does not stand
    assert on_disk["successCriteria"] == original
    assert on_disk["criteriaFingerprint"] == sup._criteria_fingerprint(original)

    refused = _events(sup, "task.criteria_edit_refused")
    assert len(refused) == 1
    assert refused[0]["taskId"] == "1"
    assert refused[0]["refusedCriteria"] == "do X, rewritten with no failure yet"
    assert refused[0]["restoredCriteria"] == original


def test_check_criteria_edits_refuses_rewrite_after_a_validation_failure(tmp_path):
    """The same refusal after a real validation failure -- the shape the
    marker was originally named for -- and it is idempotent: a second pass
    over the now-restored task changes nothing and emits nothing new."""
    sup = _supervisor(tmp_path)
    original = "ship the feature and prove it with a test"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "validation-failed",
         "successCriteria": original, "validationAttempts": 1,
         "validationNotes": "did not meet the bar"},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = "ship anything at all"
    tasks_data["tasks"][0]["status"] = "completed"
    sup.run.tasks_file.write_text(json.dumps(tasks_data))

    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == original
    assert on_disk["criteriaEditedAfterValidationFailure"] is True
    # nothing else about the record is rewritten by the refusal
    assert on_disk["status"] == "completed"
    assert on_disk["validationAttempts"] == 1
    assert len(_events(sup, "task.criteria_edit_refused")) == 1

    sup._check_criteria_edits(sup.run.read_tasks())
    assert sup.run.read_tasks()["tasks"][0]["successCriteria"] == original
    assert len(_events(sup, "task.criteria_edit_refused")) == 1


def test_unrestorable_legacy_task_is_flagged_and_tracked_forward(tmp_path):
    """Fallback, pinned deliberately: a task carrying a fingerprint but no
    verbatim `criteriaOfRecord` (a run resumed from before task 024) cannot
    be restored -- sha256 is one-way -- so task 008's behaviour stands:
    flag, track the new text forward, and let the engine-forced blocking
    finding below carry the enforcement."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "completed",
         "successCriteria": "do X (rewritten)", "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint("do X")},
    ])
    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["criteriaEditedAfterValidationFailure"] is True
    assert on_disk["successCriteria"] == "do X (rewritten)"
    assert on_disk["criteriaFingerprint"] == sup._criteria_fingerprint(
        "do X (rewritten)")
    assert _events(sup, "task.criteria_edit_refused") == []


# ---------------------------------------------------------------------------
# 2. The engine-forced, RECORDED blocking finding + the refused verdict.
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


def test_review_verdict_refuses_verified_claim_over_a_flagged_edit(tmp_path):
    """End to end at unit level: the worker rewrites the criteria, the
    engine refuses the rewrite, and the review model's `verified` claim
    over that same task is refused with a RECORDED blocking finding."""
    sup = _supervisor(tmp_path)
    original = "the bar the plan actually set"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": original, "validationAttempts": 1},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = (
        "the criteria text a worker rewrote on its own authority")
    sup.run.tasks_file.write_text(json.dumps(tasks_data))
    sup._check_criteria_edits(sup.run.read_tasks())

    # review.md always produces a findings.json in production (task 004);
    # an empty-but-present channel is the review model reporting "I found
    # nothing blocking" over a task whose criteria were tampered with.
    findings_mod.write_findings_json(sup.run.findings_json_file, [])

    result = _ok(json.dumps({"verdict": "verified"}))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is False
    assert contract_errors
    assert any("blocking" in e for e in contract_errors)

    # the rewrite does not stand
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == original

    # the blocking finding is RECORDED, not merely computed in memory
    persisted = findings_mod.read_findings_json(sup.run.findings_json_file)
    blocking = [f for f in persisted if f["severity"] == "blocking"]
    assert len(blocking) == 1
    assert "1" in blocking[0]["clause"]
    assert findings_mod.validate_finding(blocking[0]) == []
    recorded = _events(sup, "review.criteria_tamper_finding")
    assert len(recorded) == 1
    assert recorded[0]["severity"] == "blocking"

    # re-reading the channel does not duplicate the engine's own finding
    again, channel_errors = sup._review_findings_channel()
    assert channel_errors == []
    assert len([f for f in again if f["severity"] == "blocking"]) == 1
    assert len(findings_mod.read_findings_json(sup.run.findings_json_file)) == 1


def test_absent_findings_channel_is_not_an_escape_hatch(tmp_path):
    """With NO findings sidecar on disk at all, a `verified` claim over a
    flagged task must still be refused (it used to be trusted: the
    no-channel early return happened before the engine's own finding was
    added)."""
    sup = _supervisor(tmp_path)
    rewritten = "a rewritten bar nobody authorized"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": rewritten, "validationAttempts": 1,
         "criteriaEditedAfterValidationFailure": True,
         "criteriaFingerprint": sup._criteria_fingerprint(rewritten)},
    ])
    assert not sup.run.findings_json_file.exists()

    result = _ok(json.dumps({"verdict": "verified"}))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is False
    assert contract_errors
    # and the finding the refusal rests on is on disk afterwards
    persisted = findings_mod.read_findings_json(sup.run.findings_json_file)
    assert [f["severity"] for f in persisted] == ["blocking"]


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
    assert findings_mod.read_findings_json(sup.run.findings_json_file) == []
    assert _events(sup, "review.criteria_tamper_finding") == []


def test_no_findings_channel_and_nothing_flagged_still_trusts_the_claim(tmp_path):
    """The pre-existing no-channel posture is unchanged for a clean plan:
    absent sidecar + no tamper marker still means "nothing exists to check
    the claim against", and the engine creates no sidecar of its own."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": "clean text", "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint("clean text")},
    ])
    findings_list, channel_errors = sup._review_findings_channel()
    assert findings_list is None
    assert channel_errors == []
    assert not sup.run.findings_json_file.exists()


# ---------------------------------------------------------------------------
# 3. The judge-authored amendment path is untouched, in both directions.
# ---------------------------------------------------------------------------

def test_judge_amendment_is_applied_and_survives_the_next_edit_check(tmp_path):
    """The legitimate amendment path's own review payload reports
    `rejected` alongside `petition_adjudications` (never `verified` over
    the SAME settled payload -- `_apply_criteria_amendments` runs from
    inside `_review_verdict` itself, on this exact call). The amendment
    must be applied, must NOT set the tamper marker, and -- task 024's own
    new obligation -- must SURVIVE the next `_check_criteria_edits` pass
    rather than being reverted as if a worker had written it."""
    sup = _supervisor(tmp_path)
    old_text = ("this task's own successCriteria can only be verified using "
                "a state its own satisfaction would have to create first")
    new_text = "a fresh, achievable criterion that names no unmet state"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": old_text, "validationAttempts": 2},
    ])
    sup._ensure_criteria_baseline()
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
    assert on_disk["criteriaOfRecord"] == new_text
    assert on_disk["validationAttempts"] == 0
    assert "criteriaEditedAfterValidationFailure" not in on_disk

    sup._check_criteria_edits(sup.run.read_tasks())
    after = sup.run.read_tasks()["tasks"][0]
    assert after["successCriteria"] == new_text
    assert "criteriaEditedAfterValidationFailure" not in after
    assert _events(sup, "task.criteria_edit_refused") == []


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

    refused = _events(sup, "task.criteria_amendment_refused")
    assert len(refused) == 1
    assert refused[0]["target"] == "PRD requirement B.2"
