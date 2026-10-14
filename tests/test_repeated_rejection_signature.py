"""A second identical findings-channel rejection signature in one run (task
016, PRD requirement E.5, #190) and the rejection-signature bookkeeping.

v0.36 requirement B removed the dedicated `rejected_repeated_signature` park:
a repeated signature is an ordinary rejection of the one ladder
(`tests/test_rejection_ladder.py`). This module keeps pinning that it is
parked or cured by the ladder's own steps -- never by a signature comparison
-- and that the per-approach signature is still recorded (a ruling filed
against a park resolves its signature from it).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from ralphd.engine import findings, transitions
from ralphd.engine.api import create_app
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import ruling_entries
from tests.conftest import make_supervisor, supervisor_factory


def _finding(severity: str, curable: bool, clause: str, evidence: str,
             cured_when: str | None = None) -> dict:
    payload = {"severity": severity, "curable": curable, "clause": clause,
               "evidence": evidence}
    # Task 011 (PRD requirement C.2, #219): a BLOCKING, curable finding
    # with no `cured_when` is now one of the three shapes that pauses on
    # `review_findings_unusable` instead of taking whatever edge this
    # module's own fixtures exist to drive -- default one in for that
    # exact shape, the same fix `tests/test_cure_pass.py`'s own `_finding`
    # helper carries.
    if cured_when is None and severity == "blocking" and curable is True:
        cured_when = f"{clause} is discharged"
    if cured_when is not None:
        payload["cured_when"] = cured_when
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


def _sig(sup, *clauses: str) -> str:
    """The exact signature `LoopSupervisor._review_rejection_signature`
    computes off a blocking-clause set: sorted, joined, normalised --
    duplicated here (never imported from production) so the test pins
    the observable contract, not merely calls back into the code under
    test to grade itself."""
    text = "; ".join(sorted(clauses))
    return sup._normalize_blocker_signature_text(text)


# Task 014 (PRD requirement A.1, #224): `cfg_defaults=False` means the bare
# `JobConfig` default applies, and that default is now `True` for
# `operator_input_needed_pause` -- explicitly off below, so this module's
# own subject (the repeated-rejection-signature edge, not task 014's gate)
# still reaches the ordinary replan outcome it always did; task 014's own
# edge has its own module (`tests/test_operator_input_needed_pause.py`).
_supervisor = supervisor_factory(
    run_id="task016-repeated-rejection-signature", cfg_defaults=False,
    prd="# task 016 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"}, operator_input_needed_pause=False)


# ===========================================================================
# 1. The edge itself is declared, in both variants and under the peer
#    verdict gate delta, and lands on `paused`.
# ===========================================================================


def test_the_retired_edge_is_declared_nowhere():
    for vigilant in (True, False):
        assert "rejected_repeated_signature" not in transitions.guard_names(
            "review", vigilant)
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["operator_input_needed"] == "paused"


def test_the_retired_edge_is_also_absent_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert "rejected_repeated_signature" not in by_guard
    assert by_guard["operator_input_needed"] == "paused"


# ===========================================================================
# 2. Two identical rejection signatures across consecutive approaches park
#    the run only through the ladder (here: an incurable finding).
# ===========================================================================


def test_a_second_identical_rejection_signature_parks_on_the_ladder_instead_of_replanning(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3, operator_input_needed_pause=True)
    sup._approach = 2
    notifier = _StubNotifier()
    sup.notifier = notifier

    # Approach 1 already rejected with this exact blocking-clause set --
    # seeded on status.json the same way `_record_rejection_signature`
    # would have persisted it live.
    same_clause = "requirement E.5 (#190): tests still fail"
    other_clause = "requirement Z: unrelated incurable one"
    sup.run.update_status(rejectionSignatures={
        "1": _sig(sup, same_clause, other_clause)})

    rejected_again = [
        _finding("blocking", True, same_clause, "the same probe still fails"),
        _finding("blocking", False, other_clause,
                "an incurable finding, present only so mixed curability "
                "never routes this rejection to the cure-pass edge instead"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, rejected_again)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "identical failure again",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    assert sup._pause.is_set() is True  # not paused yet

    outcome = _step_review_spent(sup)

    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status.get("pauseReason") == "operator_input_needed"
    assert status["inputQuestion"]["trigger"] == "incurable"
    assert status.get("state") not in ("succeeded", "failed")

    # never archived, never a composite PRD for a fresh approach -- the
    # approach counter is left exactly where it was (requirement E.5: "the
    # approach counter where it was").
    assert not (root / "approaches" / "02").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 2

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"
    assert escalations[0]["approach"] == 2
    assert escalations[0]["findings"][0]["clause"] == same_clause

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert same_clause in text
    assert "ralphctl answer task016-repeated-rejection-signature" in text

    # A ruling filed against the park is recorded, scoped to its signature,
    # and does not release it. Folded
    # into this existing test rather than a fresh collected node -- task
    # 024's own fast-tier ceiling stands at zero headroom.
    signature = status.get("rejectionSignatures", {}).get("2")
    assert signature is not None
    # `add_ruling` resolves the
    # signature off `status.json`'s own `approach` field -- ordinarily
    # written by `_step_planning` at approach start; this fixture drives
    # `_step_review()` directly without ever going through planning for
    # approach 2, so seed it here the way a real approach-2 planning pass
    # already would have.
    sup.run.update_status(approach=2)

    def _post_rule(body: dict) -> httpx.Response:
        async def _post():
            app = create_app(sup.cfg, sup.run, sup)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://engine") as client:
                return await client.post("/rule", json=body)
        return asyncio.run(_post())

    # An unrelated live pause (an operator's plain pause, a different
    # engine escalation, ...) is left exactly as it was, even though the
    # SAME signature is still on record -- "rulings filed against
    # unrelated pauses remain non-resuming".
    # (a stagnation hold is `paused`; the repeated-signature park itself is
    # `awaiting_input` in v0.36 -- covering node: tests/test_awaiting_input_question.py)
    sup.run.update_status(state="paused", pauseReason="stagnation")
    resp = _post_rule({"text": "ruling filed against an unrelated live pause"})
    assert resp.status_code == 202, resp.text
    unrelated = sup.run.read_status()
    assert unrelated["state"] == "paused"
    assert unrelated["pauseReason"] == "stagnation"
    assert not sup._pause.is_set()

    # Restore the ACTUAL current park and file a ruling against it: since
    # v0.36 (D.1) a ruling is recorded -- scoped to this rejection's
    # signature -- but never releases the park; only an answer does
    # (covering node: tests/test_answer_outcomes.py).
    sup.run.update_status(state="awaiting_input",
                          pauseReason="operator_input_needed")
    resp = _post_rule({"text": "These clauses are residuals; ship."})
    assert resp.status_code == 202, resp.text
    held = sup.run.read_status()
    assert held["state"] == "awaiting_input"
    assert held["pauseReason"] == "operator_input_needed"
    assert not sup._pause.is_set()
    assert ruling_entries(root)[-1]["signature"] == signature


def test_the_escalation_is_emitted_to_events_jsonl_with_no_notifier(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3, operator_input_needed_pause=True)
    sup._approach = 2
    assert sup.notifier is None

    same_clause = "requirement E.5 (#190): tests still fail"
    other_clause = "requirement Z: unrelated incurable one"
    sup.run.update_status(rejectionSignatures={
        "1": _sig(sup, same_clause, other_clause)})
    findings.write_findings_json(
        sup.run.findings_json_file,
        [_finding("blocking", True, same_clause, "still failing"),
         _finding("blocking", False, other_clause,
                  "an incurable finding, present only so mixed curability "
                  "never routes this rejection to the cure-pass edge instead")])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = _step_review_spent(sup)
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"


# ===========================================================================
# 3. A DIFFERENT signature on the second rejection is not a repeat: the
#    ordinary archive/replan edge still fires, and this approach's own
#    signature is recorded for the NEXT comparison.
# ===========================================================================


def test_a_different_rejection_signature_replans_normally_and_records_its_own(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2

    old_clause = "requirement A: an old, already-fixed wall"
    old_other = "requirement Y: an old, unrelated incurable one"
    sup.run.update_status(rejectionSignatures={
        "1": _sig(sup, old_clause, old_other)})

    new_clause = "requirement B: a brand new, distinct finding"
    other_clause = "requirement Z: unrelated incurable one"
    findings.write_findings_json(
        sup.run.findings_json_file,
        [_finding("blocking", True, new_clause, "genuinely different"),
         _finding("blocking", False, other_clause,
                  "an incurable finding, present only so mixed curability "
                  "never routes this rejection to the cure-pass edge instead")])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = _step_review_spent(sup)

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup._pause.is_set() is True  # never paused

    status = sup.run.read_status()
    signatures = status.get("rejectionSignatures") or {}
    assert signatures.get("2") == _sig(sup, new_clause, other_clause)
    # approach 1's own recorded signature is untouched.
    assert signatures.get("1") == _sig(sup, old_clause, old_other)

    assert not _events(root, "operator_escalation")


# ===========================================================================
# 4. A rejection with no findings channel at all never trips the guard --
#    there is nothing to compare, exactly like every sibling signature's
#    own missing-entry default.
# ===========================================================================


def test_no_findings_channel_never_trips_the_repeat_guard(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2
    sup.run.update_status(rejectionSignatures={"1": "anything-at-all"})
    assert not sup.run.findings_json_file.exists()

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = _step_review_spent(sup)

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert not _events(root, "operator_escalation")


# ===========================================================================
# 5. Task 015 (PRD requirement F.1/F.3, #198) gave an UNREADABLE findings
#    channel -- present on disk, but does not re-validate -- its own
#    channel-error signature, so a second identical channel failure used
#    to pause through the SAME `rejected_repeated_signature` edge a
#    readable rejection's own repeating blocking-clause set does above.
#
#    Task 011 (PRD requirement C.2, #219) supersedes that for this shape
#    specifically: `_step_review` now checks `self._review_findings_
#    unusable` -- set whenever `_review_contract_ladder` exhausts over a
#    findings channel present on disk that never re-validates, which is
#    exactly what an unreadable channel is -- BEFORE it ever reaches
#    `_review_rejection_signature`'s own channel-error branch (`_step_
#    review`'s dispatch order: `review_contract_unrepaired`, then
#    `review_findings_unusable`, then the rejection ladder the repeated-
#    signature check lives inside). An unreadable channel therefore now
#    pauses on `review_findings_unusable` on its very FIRST occurrence --
#    it never reaches the repeated-signature machinery at all, matching
#    or not, so neither test below seeds or asserts a `rejectionSignatures`
#    match any longer; the pause fires unconditionally.
# ===========================================================================


def _write_unreadable_channel(sup) -> None:
    """A findings sidecar PRESENT on disk that does not re-validate --
    `findings.read_findings_json`'s third case -- because the one blocking
    finding in it is missing its required `evidence` field. Written with a
    raw `write_text`, never `findings.write_findings_json` (which refuses
    to write an invalid channel at all) -- this is the on-disk state only
    a hand-edit/corruption/partially-malformed write produces."""
    payload = [{"severity": "blocking", "curable": True,
               "clause": "requirement F.1 (#198): the channel itself"}]
    sup.run.findings_json_file.write_text(json.dumps(payload), encoding="utf-8")


def test_an_unreadable_channel_rejection_pauses_on_findings_unusable(tmp_path):
    """Task 011 (PRD requirement C.2, #219): superseding this module's own
    original claim (see section 5's header above) -- an unreadable channel
    now pauses on its FIRST rejection, never needing (or even consulting)
    a repeated-signature match at all."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2
    sup.cfg.contract_retry_max = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    _write_unreadable_channel(sup)
    # The real channel read (never faked in this test).
    _, channel_errors = sup._review_findings_channel()
    assert channel_errors, "fixture channel must actually fail to re-validate"
    # Deliberately no `rejectionSignatures` seeded at all -- proving the
    # new pause fires without ever consulting one.

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "the findings channel cannot even be read",
        }))

    async def _stub_contract_fix(hat, errors, **kw):
        # The hat never repairs the sidecar on re-ask -- exactly what a
        # genuinely stuck approach would actually produce, and the real
        # ladder (`contract_enforcement.enforce_contract`) still re-reads
        # the channel fresh on this attempt too (`extra_validate`) before
        # giving up.
        return json.dumps({"verdict": "rejected",
                          "reason": "still cannot read the channel"}), {}

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    sup._ask_contract_fix = _stub_contract_fix  # type: ignore[method-assign]

    outcome = _step_review_spent(sup)

    assert outcome == ("tail",)
    assert sup._pause.is_set() is True  # never parked (v0.36 F.3)
    status = sup.run.read_status()
    assert status.get("pauseReason") is None
    assert status.get("state") not in ("succeeded", "failed")

    # never archived, never a composite PRD -- the approach is left for a
    # resume to re-review.
    assert not (root / "approaches" / "02").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 2

    # v0.36 F.3: no escalation and no notifier digest -- an infra fault;
    # the fault is recorded on `review.contract_fault`.
    assert _events(root, "operator_escalation") == []
    (cfault,) = _events(root, "review.contract_fault")
    assert cfault["guard"] == "review_findings_unusable"
    assert cfault["errors"] and channel_errors[0][:80] in cfault["errors"][0]
    assert notifier.messages == []


def test_a_different_channel_error_still_pauses_on_findings_unusable(tmp_path):
    """A DIFFERENT unreadable-channel error text (a different malformed
    payload, so a different message) still pauses on the same edge --
    task 011's `review_findings_unusable` never compares against a prior
    signature at all, unlike section 3's readable-rejection case above."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2
    sup.run.update_status(rejectionSignatures={
        "1": "channel-error: some entirely different error text"})

    _write_unreadable_channel(sup)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "the findings channel cannot even be read",
        }))

    async def _stub_contract_fix(hat, errors, **kw):
        return json.dumps({"verdict": "rejected",
                          "reason": "still cannot read the channel"}), {}

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    sup.cfg.contract_retry_max = 1
    sup._ask_contract_fix = _stub_contract_fix  # type: ignore[method-assign]

    outcome = _step_review_spent(sup)

    assert outcome == ("tail",)
    assert _events(root, "operator_escalation") == []
    (cfault,) = _events(root, "review.contract_fault")
    assert cfault["guard"] == "review_findings_unusable"
    status = sup.run.read_status()
    # approach 1's own previously-recorded signature is untouched -- this
    # pause never records or compares one of its own.
    signatures = status.get("rejectionSignatures") or {}
    assert signatures == {"1": "channel-error: some entirely different error text"}
    assert "2" not in signatures


# ===========================================================================
# 6. Task 015 (PRD requirement F.2, #198): the residual-only / empty-
#    blocking case MUST keep returning no signature, and must stay
#    distinguishable from the unreadable-channel case above -- both
#    ultimately read `findings_list is None` through `_review_findings()`
#    alone, but only one of them is a wall to repeat.
# ===========================================================================


def test_a_residual_only_rejection_still_yields_no_signature_unlike_an_unreadable_channel(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)

    # A VALID channel with nothing blocking in it -- no wall at all, per
    # requirement F.2, and this exclusion must survive unchanged.
    residual_only = [_finding("residual", False,
                              "requirement Q: cosmetic only",
                              "nothing here ever blocked anything")]
    findings.write_findings_json(sup.run.findings_json_file, residual_only)
    findings_list, channel_errors = sup._review_findings_channel()
    assert channel_errors == []
    assert findings_list == residual_only
    assert sup._review_rejection_signature(findings_list, channel_errors) is None

    # The SAME run dir, now with an UNREADABLE channel (F.1) -- missing the
    # required `evidence` field. `_review_findings()` alone reads BOTH of
    # these as the bare fact "findings_list is None" would too if it were
    # asked about this second state instead -- but this one IS a wall to
    # repeat, unlike the residual-only case just proven above.
    _write_unreadable_channel(sup)
    findings_list, channel_errors = sup._review_findings_channel()
    assert findings_list is None
    assert channel_errors
    signature = sup._review_rejection_signature(findings_list, channel_errors)
    assert signature is not None
    assert signature.startswith("channel-error: ")

    # And "no channel at all" -- the THIRD shape `findings_list is None`
    # covers -- stays `None` too, never confused with the unreadable case.
    sup.run.findings_json_file.unlink()
    findings_list, channel_errors = sup._review_findings_channel()
    assert (findings_list, channel_errors) == (None, [])
    assert sup._review_rejection_signature(findings_list, channel_errors) is None


# ===========================================================================
# 7. Cure task cure-01-03 (PRD requirement F.1, definition of done 12,
#    #198) required the second-rejection pause to ALSO fire on the run's
#    own LAST approach. Task 011 (PRD requirement C.2, #219) supersedes
#    the mechanism entirely for THIS shape (see section 5's header above):
#    an unreadable/truncated findings sidecar now pauses on `review_
#    findings_unusable` on its very FIRST `_step_review()` cycle -- there
#    is no "second identical rejection" to reach here at all, on the last
#    approach or any other, because the pause fires before the rejection
#    ladder (and the approach counter) are ever touched.
#
#    What survives from the original two-cycle regression is the shape of
#    the proof: the supervisor is torn down and rebuilt against the SAME
#    run directory between cycles (the shape a real `resume` takes), and
#    both cycles run the REAL `_step_review()` -> `_run_review_iteration`
#    -> `_review_contract_ladder` -> `_ask_contract_fix` ladder over a
#    genuinely unreadable findings sidecar on disk -- proving the pause is
#    reached (and reached identically) the first time, AND that a fresh
#    process resuming into the same still-unarchived approach reaches it
#    again rather than silently falling through to some other edge.
# ===========================================================================


def _step_review_spent(sup):
    """Drive one `_step_review` with the outage budget already spent: a review
    contract fault (v0.36 F.3) is an infra fault, so the run ends `failed`
    (`("tail",)`) rather than pausing."""
    sup.cfg.infra_outage_budget_s = 0
    return asyncio.run(asyncio.wait_for(sup._step_review(), 120))


def _second_cycle_supervisor(root: Path, max_approaches: int):
    return make_supervisor(
        root, run_id="cure0103-two-real-cycles", cfg_defaults=False,
        max_approaches=max_approaches, contract_retry_max=2, approach=1)


@pytest.mark.parametrize("max_approaches", [2, 3])
@pytest.mark.parametrize(
    "payload",
    ['[{"severity":"blocking","curable":true,"clause":"F.1"}]',
     '{truncated'])
def test_two_real_review_cycles_persist_findings_unusable_pause_and_keep_first_unarchived(
        tmp_path, payload, max_approaches):
    root = tmp_path / "run"
    repairs: list[list[str]] = []
    messages: list[str] = []

    def wire(sup):
        sup.notifier = messages.append
        sup.cfg.contract_retry_max = 2

        async def run(prompt, transcript, **kw):
            return _result(json.dumps({
                "verdict": "rejected", "reason": "channel broken"}))

        async def repair(hat, errors, final_text=None):
            repairs.append(errors)
            return json.dumps({
                "verdict": "rejected",
                "reason": "channel still broken"}), {}

        sup.runner.run = run  # type: ignore[method-assign]
        sup._ask_contract_fix = repair  # type: ignore[method-assign]

    sup = make_supervisor(
        root, run_id="cure0103-two-real-cycles", cfg_defaults=False,
        max_approaches=max_approaches, contract_retry_max=2, prd="# fixture",
        status={"state": "running"}, approach=1)
    wire(sup)
    sup.run.findings_json_file.write_text(payload, encoding="utf-8")

    outcome = _step_review_spent(sup)
    assert outcome == ("tail",)
    assert not (root / "approaches" / "01").exists()
    assert len(messages) == 0
    status = sup.run.read_status()
    assert status.get("pauseReason") is None
    assert sup._approach == 1 and sup._pause.is_set()

    # A fresh supervisor over the SAME run directory -- the resume shape --
    # never inheriting anything the first supervisor still held in memory,
    # re-entering `review` over the SAME (still unarchived) approach.
    sup = _second_cycle_supervisor(root, max_approaches)
    wire(sup)
    sup.run.findings_json_file.write_text(payload, encoding="utf-8")

    outcome = _step_review_spent(sup)
    assert outcome == ("tail",)
    assert not (root / "approaches" / "01").exists()
    status = sup.run.read_status()
    assert status.get("pauseReason") is None
    assert sup._approach == 1 and sup._pause.is_set()
    assert len(messages) == 0
    # two cycles, each the ladder's re-asks on the first attempt and on
    # every `judge_retry_max` judge retry (v0.36 F.3)
    assert len(repairs) == (2 * (1 + sup.cfg.judge_retry_max)
                            * sup.cfg.contract_retry_max) and all(repairs)
    findings_list, channel_errors = sup._review_findings_channel()
    assert findings_list is None and channel_errors


# ===========================================================================
# 8. Cure cure-01-01-3 (PRD requirement F.1, definition of done 12): the
#    SAME two-real-cycle shape as case 7 above, but the filesystem read
#    failure is a genuinely unreadable file (`chmod 000`) rather than a
#    malformed payload -- a present sidecar `findings.read_findings_json`
#    cannot even `read_text()`, not merely one that fails to parse/
#    validate. Before this cure, `state.read_json` let the bare
#    `PermissionError` escape `read_findings_json` -> `_review_findings_
#    channel` -> `_review_contract_ladder` -> `_step_review` uncaught,
#    crashing the real review/contract-repair path instead of being
#    treated as one more invalid-findings-channel case.
#
#    Task 011 (PRD requirement C.2, #219) supersedes the second-cycle,
#    repeated-signature half of the original story exactly as section 7's
#    header above explains -- both cycles below now pause on `review_
#    findings_unusable`, on the first cycle already, keeping approach 1
#    itself unarchived across both.
# ===========================================================================


def test_two_real_review_cycles_persist_permission_denied_error_pause_and_keep_second_unarchived(
        tmp_path):
    max_approaches = 2
    root = tmp_path / "run"
    repairs: list[list[str]] = []
    messages: list[str] = []

    def wire(sup):
        sup.notifier = messages.append
        sup.cfg.contract_retry_max = 2

        async def run(prompt, transcript, **kw):
            return _result(json.dumps({
                "verdict": "rejected", "reason": "channel unreadable"}))

        async def repair(hat, errors, final_text=None):
            repairs.append(errors)
            return json.dumps({
                "verdict": "rejected",
                "reason": "channel still unreadable"}), {}

        sup.runner.run = run  # type: ignore[method-assign]
        sup._ask_contract_fix = repair  # type: ignore[method-assign]

    def _write_permission_denied_channel(sup) -> None:
        path = sup.run.findings_json_file
        if path.exists():
            # cycle 1 left this file behind at mode 000 (the fallback
            # archive copy is skipped for a genuinely unreadable sidecar,
            # per `_archive_findings_json` above) -- restore write access
            # before rewriting it for the second cycle.
            os.chmod(path, 0o644)
        path.write_text(
            '[{"severity":"blocking","curable":true,"clause":"F.1"}]',
            encoding="utf-8")
        os.chmod(path, 0o000)

    sup = make_supervisor(
        root, run_id="cure010103-permission-denied", cfg_defaults=False,
        max_approaches=max_approaches, contract_retry_max=2, prd="# fixture",
        status={"state": "running"}, approach=1)
    wire(sup)
    _write_permission_denied_channel(sup)
    try:
        # Confirm the fixture itself really is unreadable (a real
        # `PermissionError` on read), and that the real channel reader
        # converts it to a channel error rather than letting it escape --
        # never faked/mocked for the rest of this test.
        with pytest.raises(PermissionError):
            sup.run.findings_json_file.read_text()
        findings_list, channel_errors = sup._review_findings_channel()
        assert findings_list is None and channel_errors

        outcome = _step_review_spent(sup)
        assert outcome == ("tail",)
        assert not (root / "approaches" / "01").exists()
        assert len(messages) == 0
        status = sup.run.read_status()
        assert status.get("pauseReason") is None
        assert sup._approach == 1 and sup._pause.is_set()

        # A fresh supervisor over the SAME run directory -- the resume
        # shape -- never inheriting anything the first supervisor still
        # held in memory, re-entering `review` over the SAME (still
        # unarchived) approach. `chmod 000` must be restored first, the
        # same restore a real resume would need after an operator actually
        # fixed the sidecar (here, re-broken identically on purpose).
        os.chmod(sup.run.findings_json_file, 0o644)
        sup = _second_cycle_supervisor(root, max_approaches)
        wire(sup)
        _write_permission_denied_channel(sup)

        outcome = _step_review_spent(sup)
        assert outcome == ("tail",)
        assert not (root / "approaches" / "01").exists()
        status = sup.run.read_status()
        assert status.get("pauseReason") is None
        assert sup._approach == 1 and sup._pause.is_set()
        assert len(messages) == 0
        # two cycles, each the ladder's re-asks on the first attempt and on
        # every `judge_retry_max` judge retry (v0.36 F.3)
        assert len(repairs) == (2 * (1 + sup.cfg.judge_retry_max)
                                * sup.cfg.contract_retry_max) and all(repairs)
        findings_list, channel_errors = sup._review_findings_channel()
        assert findings_list is None and channel_errors
    finally:
        # Restore permissions before `tmp_path`'s own teardown -- deleting
        # the entry itself only needs the containing directory's write
        # permission, but leaving a mode-000 file behind is needless
        # sandbox residue.
        target = root / "review-findings.json"
        if target.exists():
            os.chmod(target, 0o644)
