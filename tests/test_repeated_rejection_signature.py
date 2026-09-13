"""E.5 -- a second identical findings-channel rejection signature in one run
pauses the run and notifies, rather than consuming a fresh approach (task
016, PRD requirement E.5, #190).

Mirrors `tests/test_last_approach.py`'s shape: a real `LoopSupervisor`
(`_step_review()`) over a real run dir, never a mock of the guard itself.
The new edge, `rejected_repeated_signature`, self-loops `review -> paused`
exactly like `rejected_incurable`/`rejected_last_approach` already do --
`LoopSupervisor._pause_and_escalate_repeated_rejection_signature` pauses
through the EXISTING pause machinery (`self.pause()`, `self.notifier`, an
`operator_escalation` event) instead of archiving the approach and spending
a fresh one on a wall the run has already lost one approach to.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import findings, transitions
from ralphd.engine.runner import IterationResult
from tests.conftest import make_supervisor, supervisor_factory


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


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


_supervisor = supervisor_factory(
    run_id="task016-repeated-rejection-signature", cfg_defaults=False,
    prd="# task 016 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"})


# ===========================================================================
# 1. The edge itself is declared, in both variants and under the peer
#    verdict gate delta, and lands on `paused`.
# ===========================================================================


def test_the_edge_is_declared_in_both_variants():
    for vigilant in (True, False):
        assert transitions.is_legal("review", "paused", vigilant)
        guards = transitions.guard_names("review", vigilant)
        assert "rejected_repeated_signature" in guards
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["rejected_repeated_signature"] == "paused"


def test_the_edge_is_also_declared_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert by_guard["rejected_repeated_signature"] == "paused"


# ===========================================================================
# 2. Two identical rejection signatures across consecutive approaches pause
#    the run (existing pause machinery) instead of archiving/replanning.
# ===========================================================================


def test_a_second_identical_rejection_signature_pauses_instead_of_replanning(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
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

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "rejected_repeated_signature")
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status.get("pauseReason") == "rejected_repeated_signature"
    assert status.get("state") not in ("succeeded", "failed")

    # never archived, never a composite PRD for a fresh approach -- the
    # approach counter is left exactly where it was (requirement E.5: "the
    # approach counter where it was").
    assert not (root / "approaches" / "02").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 2

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_repeated_signature"
    assert escalations[0]["approach"] == 2
    assert escalations[0]["findings"][0]["clause"] == same_clause

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert same_clause in text
    assert "ralphctl resume task016-repeated-rejection-signature" in text


def test_the_escalation_is_emitted_to_events_jsonl_with_no_notifier(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
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

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "paused", "rejected_repeated_signature")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_repeated_signature"


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

    outcome = asyncio.run(sup._step_review())

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

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert not _events(root, "operator_escalation")


# ===========================================================================
# 5. Task 015 (PRD requirement F.1/F.3, #198): an UNREADABLE findings
#    channel -- present on disk, but does not re-validate -- fingerprints
#    the rejection ERROR itself, so a second identical channel failure
#    pauses through this exact edge exactly like a readable rejection's own
#    repeating blocking-clause set already does above. `_review_findings()`
#    alone (`_review_findings_channel()[0]`) cannot tell "no channel" apart
#    from "a channel present but unreadable" -- both read `None` -- so
#    this drives the SAME real `_step_review()` -> `_run_review_iteration`
#    -> `_review_contract_ladder` -> `_review_verdict` path every other
#    test in this module does, over a genuinely unreadable sidecar the
#    real `_review_findings_channel` reads off disk (never faked/mocked).
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


def test_a_second_identical_unreadable_channel_rejection_pauses(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2
    sup.cfg.contract_retry_max = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    _write_unreadable_channel(sup)
    # The real channel read (never faked in this test) is what approach 1's
    # own recorded signature must equal, exactly as `_record_rejection_
    # signature` would have persisted it live off THIS SAME formula.
    _, channel_errors = sup._review_findings_channel()
    assert channel_errors, "fixture channel must actually fail to re-validate"
    sup.run.update_status(rejectionSignatures={
        "1": "channel-error: " + sup._normalize_blocker_signature_text(
            "; ".join(sorted(channel_errors)))})

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "the findings channel cannot even be read",
        }))

    async def _stub_contract_fix(hat, errors, **kw):
        # The hat never repairs the sidecar on re-ask -- exactly what a
        # genuinely stuck second approach would actually produce, and the
        # real ladder (`contract_enforcement.enforce_contract`) still
        # re-reads the channel fresh on this attempt too (`extra_validate`)
        # before giving up.
        return json.dumps({"verdict": "rejected",
                          "reason": "still cannot read the channel"}), {}

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    sup._ask_contract_fix = _stub_contract_fix  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "rejected_repeated_signature")
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status.get("pauseReason") == "rejected_repeated_signature"
    assert status.get("state") not in ("succeeded", "failed")

    # never archived, never a composite PRD -- the approach is left for a
    # resume to re-review, exactly like the readable-rejection case above.
    assert not (root / "approaches" / "02").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 2

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_repeated_signature"
    # no findings LIST at all -- the channel itself, not a blocking
    # finding, is the wall that repeated.
    assert escalations[0]["findings"] == []
    assert escalations[0]["channelErrors"] == channel_errors

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert channel_errors[0] in text
    assert "ralphctl resume task016-repeated-rejection-signature" in text


def test_a_different_channel_error_never_matches_and_replans_normally(tmp_path):
    """A different unreadable-channel error text (a different malformed
    payload, so a different message) is not a repeat -- the ordinary
    archive/replan edge still fires, mirroring section 3's readable-
    rejection case above."""
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

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert not _events(root, "operator_escalation")
    status = sup.run.read_status()
    signatures = status.get("rejectionSignatures") or {}
    assert signatures.get("2", "").startswith("channel-error: ")


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
#    #198): the second-rejection pause must ALSO fire on the run's own LAST
#    approach -- `_step_review` used to guard the whole repeat-signature
#    check (and the record it depends on) with `approach < max_approaches`,
#    so `max_approaches=2`'s second (and final) identical rejection fell
#    through to the ordinary archive/`rejected_no_approach_left` tail
#    instead of pausing, leaving approach 02 archived and nothing to
#    resume. Requirement F.1's own text names no last-approach exemption.
#
#    An independent two-cycle regression: no `rejectionSignatures` seeded
#    on `status.json` -- both cycles run the REAL `_step_review()` ->
#    `_run_review_iteration` -> `_review_contract_ladder` ->
#    `_ask_contract_fix` ladder over a genuinely unreadable findings
#    sidecar on disk, and the supervisor is torn down and rebuilt against
#    the SAME run directory between cycles (the shape a real `resume`
#    takes), so the first cycle's signature can only reach the second
#    cycle through `status.json` itself, never through in-process state.
# ===========================================================================


def _second_cycle_supervisor(root: Path, max_approaches: int):
    return make_supervisor(
        root, run_id="cure0103-two-real-cycles", cfg_defaults=False,
        max_approaches=max_approaches, contract_retry_max=2, approach=2)


@pytest.mark.parametrize("max_approaches", [2, 3])
@pytest.mark.parametrize(
    "payload",
    ['[{"severity":"blocking","curable":true,"clause":"F.1"}]',
     '{truncated'])
def test_two_real_review_cycles_persist_error_pause_and_keep_second_unarchived_without_seeding(
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

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert (root / "approaches" / "01").is_dir()
    assert messages == []
    first_signature = sup.run.read_status()["rejectionSignatures"]["1"]
    assert first_signature.startswith("channel-error: ")
    assert sup._step_replan() == ("advance", "planning", "budget_and_approach_left")

    # A fresh supervisor over the SAME run directory -- the resume shape --
    # never inheriting anything the first supervisor still held in memory.
    sup = _second_cycle_supervisor(root, max_approaches)
    wire(sup)
    sup.run.findings_json_file.write_text(payload, encoding="utf-8")

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "paused", "rejected_repeated_signature")
    assert not (root / "approaches" / "02").exists()
    status = sup.run.read_status()
    assert status["pauseReason"] == "rejected_repeated_signature"
    assert status["rejectionSignatures"]["1"] == first_signature
    assert sup._approach == 2 and not sup._pause.is_set()
    assert len(messages) == 1
    assert len(repairs) == 4 and all(repairs)
    findings_list, channel_errors = sup._review_findings_channel()
    assert findings_list is None and channel_errors
