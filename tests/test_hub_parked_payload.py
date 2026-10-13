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
    cure_pass_preview,
    parked_question,
    read_events,
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
    # The residual finding is itemised too, not merely counted.
    assert len(pq["findings"]) == 2
    residual = pq["findings"][1]
    assert residual["id"] == "R1"
    assert residual["severity"] == "residual"
    assert residual["clause"] == _RESIDUAL["clause"]
    assert residual["evidence"] == _RESIDUAL["evidence"]
    assert residual["cured_when"] is None
    assert residual["invalidates"] == []
    assert set(pq["consequence"]["taskIdsValidatedToPending"]) == {"057", "058"}
    assert pq["consequence"]["insertCount"] == 1
    assert pq["consequence"]["costSpentThisApproach"] == "$0.42"
    assert pq["consequence"]["tokensSpentThisApproach"] == 321
    assert pq["consequence"]["nextStepIfUnanswered"] == NEXT_STEP_CURE_PASS


def test_operator_input_needed_archive_and_replan_next_step(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2,
                              "usage": {"byApproach": {"1": {
                                  "costUSD": 0.42, "totalTokens": 321}}}})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    # No `cure_preview` passed: the ordinary (non-driven) pause still has to
    # carry approach n of m and this approach's spend.
    sup._pause_and_escalate_operator_input_needed(
        1, _MIXED_FINDINGS, replan_available=True)

    status = sup.run.read_status()
    pq = parked_question(root, status)
    assert pq["reason"] == "operator_input_needed"
    assert pq["approach"] == 1
    assert pq["maxApproaches"] == 2
    assert pq["approachOfMax"] == "approach 1 of 2"
    assert pq["parkedAt"]
    assert pq["replanAvailable"] is True
    assert pq["blockingCount"] == 2
    assert pq["residualCount"] == 0
    assert [f["id"] for f in pq["findings"]] == ["F1", "F2"]
    for got, want in zip(pq["findings"], _MIXED_FINDINGS):
        assert got["severity"] == "blocking"
        assert got["clause"] == want["clause"]
        assert got["evidence"] == want["evidence"]
        assert got["cured_when"] == want.get("cured_when")
        assert got["invalidates"] == []
    assert pq["consequence"]["taskIdsValidatedToPending"] == []
    # Task 003 (this wave): `_MIXED_FINDINGS` is never curable at all (one
    # incurable finding) -- a cure pass never runs over it, so the preview
    # reads zero here too, exactly like the ceiling-spent/no-progress
    # causes above already do.
    assert pq["consequence"]["insertCount"] == 0
    assert pq["consequence"]["costSpentThisApproach"] == "$0.42"
    assert pq["consequence"]["tokensSpentThisApproach"] == 321
    assert pq["consequence"]["nextStepIfUnanswered"] == NEXT_STEP_ARCHIVE_AND_REPLAN
    # The event itself carries the A.7 fields -- the hub reads them off it.
    ev = read_events(root, types=("operator_escalation",))[-1]
    assert ev["approachOfMax"] == "approach 1 of 2"
    assert ev["costSpentThisApproach"] == "$0.42"
    assert ev["tokensSpentThisApproach"] == 321


def test_every_residual_finding_is_itemised_after_the_blocking_ones(tmp_path):
    """Criterion (1): EACH finding -- residual as well as blocking -- carries
    its id, severity, clause, full evidence, cured_when and invalidates. The
    residual entries come off the on-disk sidecar (the event carries only
    the blocking set), in the sidecar's own order, after the blocking ones,
    under their own `R<n>` ids so the `F<n>` ids the escalation text and
    `ralphctl amend` name are never shifted."""
    root = tmp_path / "run"
    long_residual_evidence = "r" * 12000 + " end-of-residual-evidence"
    residuals = [
        _finding("residual", True, "residual clause one", long_residual_evidence,
                 cured_when="the residual is disclosed in docs",
                 invalidates=["docs/api.md"]),
        _finding("residual", False, "residual clause two", "second residual"),
    ]
    blocking = [_finding("blocking", False, "requirement B (#111)", "incurable")]
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    # Residuals interleaved with the blocking finding on disk: order in the
    # payload is still blocking first, then residuals in sidecar order.
    findings.write_findings_json(sup.run.findings_json_file,
                                 [residuals[0]] + blocking + [residuals[1]])
    sup._pause_and_escalate_operator_input_needed(
        1, blocking, replan_available=True)

    pq = parked_question(root, sup.run.read_status())
    assert pq["blockingCount"] == 1
    assert pq["residualCount"] == 2
    assert [f["id"] for f in pq["findings"]] == ["F1", "R1", "R2"]
    assert [f["severity"] for f in pq["findings"]] == [
        "blocking", "residual", "residual"]
    r1, r2 = pq["findings"][1], pq["findings"][2]
    assert r1["clause"] == "residual clause one"
    assert r1["evidence"] == long_residual_evidence
    assert r1["cured_when"] == "the residual is disclosed in docs"
    assert r1["invalidates"] == ["docs/api.md"]
    assert r2["clause"] == "residual clause two"
    assert r2["evidence"] == "second residual"
    assert r2["cured_when"] is None
    assert r2["invalidates"] == []
    # The counts agree with the itemised list, severity by severity.
    assert pq["blockingCount"] == sum(
        1 for f in pq["findings"] if f["severity"] == "blocking")
    assert pq["residualCount"] == sum(
        1 for f in pq["findings"] if f["severity"] == "residual")


def test_full_evidence_is_never_truncated(tmp_path):
    root = tmp_path / "run"
    long_evidence = "x" * 20000 + " end-of-evidence"
    finding = [_finding("blocking", False, "requirement L", long_evidence)]
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, finding)
    sup._pause_and_escalate_operator_input_needed(
        1, finding, replan_available=True)
    pq = parked_question(root, sup.run.read_status())
    assert pq["findings"][0]["evidence"] == long_evidence


def test_an_event_without_a7_fields_still_reports_approach_and_spend(tmp_path):
    """An `operator_escalation` event lacking `approachOfMax` and the spend
    pair (written by an engine that emitted them only for a driven gate)
    still yields approach n of m and the approach's spend, from the event's
    own approach and `status.json`'s `usage.byApproach` bucket."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2,
                              "usage": {"byApproach": {"1": {
                                  "costUSD": 1.5, "totalTokens": 900}}}})
    sup.run.emit("operator_escalation", approach=1,
                 reason="operator_input_needed", findings=_MIXED_FINDINGS,
                 replanAvailable=True, escalation="text",
                 nextStepIfUnanswered=NEXT_STEP_ARCHIVE_AND_REPLAN)
    sup._park_for_input(reason="operator_input_needed", judge="review",
                        trigger="incurable", blocking=_MIXED_FINDINGS,
                        approach=1)
    pq = parked_question(root, sup.run.read_status())
    assert pq["approachOfMax"] == "approach 1 of 2"
    assert pq["consequence"]["costSpentThisApproach"] == "$1.50"
    assert pq["consequence"]["tokensSpentThisApproach"] == 900


# ===========================================================================
# 2. `rejected_repeated_signature` -- same shape, `replanAvailable` absent.
# ===========================================================================


def test_repeated_signature_payload_has_no_replan_available(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3,
                      status={"state": "running", "phase": "review",
                              "approach": 2, "maxApproaches": 3,
                              "usage": {"byApproach": {"2": {
                                  "costUSD": 0.1, "totalTokens": 77}}}})
    sup._approach = 2
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup._pause_and_escalate_repeated_rejection_signature(2, "sig-abc")

    status = sup.run.read_status()
    assert status["pauseReason"] == "rejected_repeated_signature"
    pq = parked_question(root, status)
    assert pq["reason"] == "rejected_repeated_signature"
    assert pq["approachOfMax"] == "approach 2 of 3"
    assert pq["parkedAt"]
    assert pq["replanAvailable"] is None
    assert pq["blockingCount"] == 2
    assert pq["consequence"]["costSpentThisApproach"] == "$0.10"
    assert pq["consequence"]["tokensSpentThisApproach"] == 77
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


def test_rejected_last_approach_now_parks_with_a_panel(tmp_path):
    """v0.36 moved `rejected_last_approach` from a plain `paused` (no panel,
    outside `PARKED_QUESTION_REASONS`) into `awaiting_input`: it carries an
    `inputQuestion` and, like every `awaiting_input` run, the panel.
    Covering node for the stagnation exclusion:
    `test_stagnation_pause_has_no_parked_question`."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1,
                      status={"state": "running", "phase": "review",
                              "approach": 1})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup._pause_and_escalate_last_approach_rejection(1)
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "rejected_last_approach"
    pq = parked_question(root, status)
    assert pq is not None
    assert pq["inputQuestion"] == status["inputQuestion"]
    assert pq["inputQuestion"]["trigger"] == "cure_rounds_spent"


def test_stagnation_pause_has_no_parked_question(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, status={"state": "running", "approach": 1})
    sup.run.emit("operator_escalation", approach=1, reason="stagnation",
                 escalation="stalled")
    sup.pause(reason="stagnation")
    status = sup.run.read_status()
    assert status["pauseReason"] == "stagnation"
    assert parked_question(root, status) is None


def test_a_plain_operator_pause_has_no_parked_question(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, status={"state": "running"})
    sup.pause()  # no reason -- the ordinary operator `POST /pause`
    status = sup.run.read_status()
    assert status.get("pauseReason") is None
    assert parked_question(root, status) is None


# ===========================================================================
# 3b. Cure task cure-01-02-4 (PRD B.1, D.1; DoD 2/4): a later task-boundary
#     commit-secret-scan hold, sharing the `operator_input_needed` reason
#     string with an already-answered review gate, must not resurrect that
#     answered gate's own question or its obsolete cure preview.
# ===========================================================================


def test_commit_scan_hold_after_answered_review_gate_has_no_stale_parked_question(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      tasks=_LANE_TASKS,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup._pause_and_escalate_operator_input_needed(
        1, _MIXED_FINDINGS, replan_available=True)

    status = sup.run.read_status()
    assert status["pauseReason"] == "operator_input_needed"
    # Sanity: the review gate's own question shows while it stands unanswered.
    assert parked_question(root, status) is not None

    # The operator answers the review gate -- a cure worker then runs.
    sup.resume()
    status = sup.run.read_status()
    assert status.get("pauseReason") is None
    assert parked_question(root, status) is None

    # A task-boundary commit-secret-scan hold now raises, sharing the SAME
    # `operator_input_needed` reason string but emitting NO fresh
    # `operator_escalation` event -- its record is the value-free
    # `commit_secret_scan` event alone (`_pause_for_commit_secret_hit`).
    sup.run.emit(sup.COMMIT_SECRET_SCAN_EVENT, sha="0" * 40, part="blob",
                 variable="REVIEW_TEST_VARIABLE", pushed=False)
    sup._pause_for_commit_secret_hit()
    status = sup.run.read_status()
    assert status["pauseReason"] == "operator_input_needed"
    # The stale, already-answered review question -- and its obsolete
    # `_MIXED_FINDINGS` preview -- must NOT resurface for this unrelated
    # commit-scan hold.
    assert parked_question(root, status) is None

    # A genuinely new review escalation, raised after this hold is
    # answered, still appears with its own consequences.
    sup.resume()
    findings.write_findings_json(sup.run.findings_json_file, _CURE_REOPEN_FINDING)
    preview = cure_pass_preview(_LANE_TASKS, _CURE_REOPEN_FINDING)
    sup._pause_and_escalate_operator_input_needed(
        1, _CURE_REOPEN_FINDING, replan_available=True, cure_preview=preview)
    status = sup.run.read_status()
    pq = parked_question(root, status)
    assert pq is not None
    assert pq["reason"] == "operator_input_needed"
    assert pq["findings"][0]["clause"] == _CURE_REOPEN_FINDING[0]["clause"]
    assert pq["consequence"]["insertCount"] == 1


def test_run_detail_has_no_stale_parked_question_after_a_commit_scan_hold(tmp_path):
    """Same scenario end to end through `ui_server.run_detail`, the exact
    surface the hub panel reads."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup._pause_and_escalate_operator_input_needed(
        1, _MIXED_FINDINGS, replan_available=True)
    sup.resume()
    sup.run.emit(sup.COMMIT_SECRET_SCAN_EVENT, sha="0" * 40, part="blob",
                 variable="REVIEW_TEST_VARIABLE", pushed=False)
    sup._pause_for_commit_secret_hit()

    reg = _reg_for(root, sup.cfg.run_id)
    detail = ui_server.run_detail(reg, sup.cfg.run_id)
    assert detail["status"]["pauseReason"] == "operator_input_needed"
    assert detail["parkedQuestion"] is None


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


def test_run_detail_shows_the_input_question_for_awaiting_input_and_no_other_state(
        tmp_path):
    """v0.36: the hub's parked-question panel reads the run's persisted
    `inputQuestion`; the payload carries it for every `awaiting_input` run and
    is `null` for every other state."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup._pause_and_escalate_operator_input_needed(
        1, _MIXED_FINDINGS, replan_available=True)
    reg = _reg_for(root, sup.cfg.run_id)

    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    detail = ui_server.run_detail(reg, sup.cfg.run_id)
    assert detail["parkedQuestion"]["inputQuestion"] == status["inputQuestion"]
    assert detail["parkedQuestion"]["inputQuestion"]["judge"] == "review"

    for other in ("running", "paused", "exhausted", "finishing", "failed"):
        sup.run.update_status(state=other)
        detail = ui_server.run_detail(reg, sup.cfg.run_id)
        assert detail["parkedQuestion"] is None, other


def test_run_detail_itemises_the_residual_finding_too(tmp_path):
    """The run-detail payload end to end, one blocking and one residual
    finding on disk: both counted AND both listed, each with its clause and
    evidence."""
    root = tmp_path / "run"
    blocking = [_finding("blocking", False, "requirement B (#111)", "incurable")]
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file,
                                 blocking + [_RESIDUAL])
    sup._pause_and_escalate_operator_input_needed(
        1, blocking, replan_available=True)

    reg = _reg_for(root, sup.cfg.run_id)
    pq = ui_server.run_detail(reg, sup.cfg.run_id)["parkedQuestion"]
    assert pq["blockingCount"] == 1
    assert pq["residualCount"] == 1
    by_id = {f["id"]: f for f in pq["findings"]}
    assert set(by_id) == {"F1", "R1"}
    assert by_id["F1"]["severity"] == "blocking"
    assert by_id["F1"]["clause"] == "requirement B (#111)"
    assert by_id["F1"]["evidence"] == "incurable"
    assert by_id["R1"]["severity"] == "residual"
    assert by_id["R1"]["clause"] == _RESIDUAL["clause"]
    assert by_id["R1"]["evidence"] == _RESIDUAL["evidence"]
    assert "cured_when" in by_id["R1"] and "invalidates" in by_id["R1"]


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
