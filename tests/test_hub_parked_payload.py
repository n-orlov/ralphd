"""Task 007 (v0.26, #269): shape the latest `operator_escalation` into the
hub's `GET /api/runs/<id>` payload, for the two pause reasons an operator
RULING can actually answer -- `operator_input_needed` and
`rejected_repeated_signature`.

`state.parked_question(run_root, status)` is the derivation (no live
engine required -- `events.jsonl` + `status.json` + the on-disk structured
findings sidecar, the same on-disk-first discipline `escalation_pause_
markers` already uses); `ui_server.run_detail` splices its result in as the
top-level `parkedQuestion` field. Both are exercised here, plus the ETag
that has to move when a fresh escalation lands.

At 9b72b700 there is no `state.parked_question`, `state.PARKED_QUESTION_
REASONS`, `state.NEXT_STEP_CURE_PASS`/`NEXT_STEP_ARCHIVE_AND_REPLAN`, no
`nextStepIfUnanswered` on the `operator_escalation` event, and `ui_server.
run_detail`'s payload carries no `parkedQuestion` key at all -- every node
below fails against that checkout.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.state import (
    NEXT_STEP_ARCHIVE_AND_REPLAN,
    NEXT_STEP_CURE_PASS,
    PARKED_QUESTION_REASONS,
    cure_pass_preview,
    parked_question,
)
from tests.conftest import supervisor_factory

from ralphd.cli import ui_server


def _finding(severity: str, curable: bool, clause: str, evidence: str,
             cured_when: str | None = None, invalidates=None) -> dict:
    payload = {"severity": severity, "curable": curable, "clause": clause,
               "evidence": evidence}
    if cured_when is None and severity == "blocking" and curable is True:
        cured_when = f"{clause} is discharged"
    if cured_when is not None:
        payload["cured_when"] = cured_when
    if invalidates is not None:
        payload["invalidates"] = invalidates
    return payload


def _task(tid: str, *, status: str = "pending", depends: list | None = None,
          criteria: str = "n/a") -> dict:
    return {"id": tid, "title": tid, "successCriteria": criteria,
            "status": status, "origin": "planner",
            "dependsOn": list(depends or []), "tier": "normal"}


RECORD_PATH = "artifacts/reports/hub-parked-payload-lanes.md"

# Neither all-curable nor all-incurable -- lands on the ordinary
# archive/replan tail, exactly like `test_operator_input_needed_pause.py`'s
# own fixture of the same name.
_MIXED_FINDINGS = [
    _finding("blocking", False, "requirement B (#111)", "incurable one"),
    _finding("blocking", True, "requirement A.1 (#224)", "curable one"),
]

# All-curable, with an `invalidates` path that reopens an already-validated
# record task -- the same shape `test_discretionary_gate.py`'s own
# `_CURE_REOPEN_FINDING` fixture uses.
_CURE_REOPEN_FINDING = [
    _finding("blocking", True, "the shipped sha's lanes moved",
              "a later commit landed after the record",
              invalidates=[RECORD_PATH]),
]

_LANE_TASKS = [
    _task("057", status="validated", criteria="verify every lane"),
    _task("058", status="validated", depends=["057"],
          criteria=f"One commit adds {RECORD_PATH} naming the shipped sha."),
]

_RESIDUAL = {
    "severity": "residual", "curable": False,
    "clause": "a disclosed residual, never counted as blocking",
    "evidence": "residual evidence, still non-blocking",
}


_supervisor = supervisor_factory(
    run_id="task007-hub-parked-payload", cfg_defaults=False,
    prd="# task 007 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"}, operator_input_needed_pause=False)


def _reg_for(root: Path, run_id: str) -> Path:
    """A hub registry pointing straight at `root` -- `ui_server.run_detail`
    reads `<reg>/runs/<run_id>`, so the run's own on-disk root just needs to
    sit there under the id its own supervisor was built with."""
    reg = root.parent / "registry"
    target = reg / "runs" / run_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.symlink_to(root, target_is_directory=True)
    return reg


# ===========================================================================
# 1. `state.parked_question` itself -- `operator_input_needed`, both next
#    steps a not-yet-answered pause can predict.
# ===========================================================================


def test_operator_input_needed_cure_pass_next_step(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      tasks=_LANE_TASKS,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2,
                              "usage": {"byApproach": {"1": {
                                  "costUSD": 0.42, "totalTokens": 321}}}})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file,
                                 _CURE_REOPEN_FINDING + [_RESIDUAL])
    preview = cure_pass_preview(_LANE_TASKS, _CURE_REOPEN_FINDING)
    sup._pause_and_escalate_operator_input_needed(
        1, _CURE_REOPEN_FINDING, replan_available=True, cure_preview=preview)

    status = sup.run.read_status()
    assert status["pauseReason"] == "operator_input_needed"
    pq = parked_question(root, status)
    assert pq is not None
    assert pq["reason"] == "operator_input_needed"
    assert pq["approach"] == 1
    assert pq["maxApproaches"] == 2
    assert pq["approachOfMax"] == "approach 1 of 2"
    assert pq["parkedAt"]  # a real timestamp, not empty/None
    assert pq["replanAvailable"] is True
    assert pq["blockingCount"] == 1
    assert pq["residualCount"] == 1
    finding = pq["findings"][0]
    assert finding["id"] == "F1"
    assert finding["severity"] == "blocking"
    assert finding["clause"] == _CURE_REOPEN_FINDING[0]["clause"]
    assert finding["evidence"] == _CURE_REOPEN_FINDING[0]["evidence"]
    assert finding["cured_when"] == _CURE_REOPEN_FINDING[0]["cured_when"]
    assert finding["invalidates"] == [RECORD_PATH]
    assert set(pq["consequence"]["taskIdsValidatedToPending"]) == {"057", "058"}
    assert pq["consequence"]["insertCount"] == 1
    assert pq["consequence"]["costSpentThisApproach"] == "$0.42"
    assert pq["consequence"]["tokensSpentThisApproach"] == 321
    assert pq["consequence"]["nextStepIfUnanswered"] == NEXT_STEP_CURE_PASS


def test_operator_input_needed_archive_and_replan_next_step(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup._pause_and_escalate_operator_input_needed(
        1, _MIXED_FINDINGS, replan_available=True)

    status = sup.run.read_status()
    pq = parked_question(root, status)
    assert pq["blockingCount"] == 2
    assert pq["residualCount"] == 0
    assert {f["id"] for f in pq["findings"]} == {"F1", "F2"}
    assert pq["consequence"]["taskIdsValidatedToPending"] == []
    assert pq["consequence"]["insertCount"] == 0
    assert pq["consequence"]["nextStepIfUnanswered"] == NEXT_STEP_ARCHIVE_AND_REPLAN


# ===========================================================================
# 2. `rejected_repeated_signature` -- same shape, `replanAvailable` absent.
# ===========================================================================


def test_repeated_signature_payload_has_no_replan_available(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3,
                      status={"state": "running", "phase": "review",
                              "approach": 2, "maxApproaches": 3})
    sup._approach = 2
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup._pause_and_escalate_repeated_rejection_signature(2, "sig-abc")

    status = sup.run.read_status()
    assert status["pauseReason"] == "rejected_repeated_signature"
    pq = parked_question(root, status)
    assert pq["reason"] == "rejected_repeated_signature"
    assert pq["replanAvailable"] is None
    assert pq["blockingCount"] == 2
    assert pq["consequence"]["nextStepIfUnanswered"] == NEXT_STEP_ARCHIVE_AND_REPLAN


def test_repeated_signature_can_also_predict_a_cure_pass(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3,
                      status={"state": "running", "phase": "review",
                              "approach": 2, "maxApproaches": 3})
    sup._approach = 2
    findings.write_findings_json(sup.run.findings_json_file, _CURE_REOPEN_FINDING)
    preview = cure_pass_preview(_LANE_TASKS, _CURE_REOPEN_FINDING)
    sup._pause_and_escalate_repeated_rejection_signature(
        2, "sig-xyz", cure_preview=preview)

    status = sup.run.read_status()
    pq = parked_question(root, status)
    assert pq["consequence"]["nextStepIfUnanswered"] == NEXT_STEP_CURE_PASS


# ===========================================================================
# 3. `None` for a running run and for a pause reason a ruling cannot answer.
# ===========================================================================


def test_no_pause_reason_is_no_parked_question():
    assert parked_question(Path("/nonexistent"), {"state": "running"}) is None
    assert parked_question(Path("/nonexistent"),
                           {"state": "running", "pauseReason": None}) is None


def test_pause_reasons_a_ruling_cannot_answer_are_all_excluded(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1,
                      status={"state": "running", "phase": "review",
                              "approach": 1})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    # `rejected_last_approach` -- an existing, unrelated pause reason.
    sup._pause_and_escalate_last_approach_rejection(1)
    status = sup.run.read_status()
    assert status["pauseReason"] == "rejected_last_approach"
    assert status["pauseReason"] not in PARKED_QUESTION_REASONS
    assert parked_question(root, status) is None


def test_a_plain_operator_pause_has_no_parked_question(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, status={"state": "running"})
    sup.pause()  # no reason -- the ordinary operator `POST /pause`
    status = sup.run.read_status()
    assert status.get("pauseReason") is None
    assert parked_question(root, status) is None


# ===========================================================================
# 4. `ui_server.run_detail` splices it in end to end, and the run-detail
#    ETag moves when a fresh escalation lands.
# ===========================================================================


def test_run_detail_carries_the_parked_question(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup._pause_and_escalate_operator_input_needed(
        1, _MIXED_FINDINGS, replan_available=True)

    reg = _reg_for(root, sup.cfg.run_id)
    detail = ui_server.run_detail(reg, sup.cfg.run_id)
    assert detail["parkedQuestion"] is not None
    assert detail["parkedQuestion"]["reason"] == "operator_input_needed"
    assert detail["parkedQuestion"]["blockingCount"] == 2


def test_run_detail_parked_question_is_null_while_running(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, status={"state": "running"})
    reg = _reg_for(root, sup.cfg.run_id)
    detail = ui_server.run_detail(reg, sup.cfg.run_id)
    assert detail["parkedQuestion"] is None


def test_etag_changes_when_a_new_escalation_lands(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    reg = _reg_for(root, sup.cfg.run_id)
    before = ui_server.run_detail_etag(reg, sup.cfg.run_id)

    sup._pause_and_escalate_operator_input_needed(
        1, _MIXED_FINDINGS, replan_available=True)

    after = ui_server.run_detail_etag(reg, sup.cfg.run_id)
    assert before != after
