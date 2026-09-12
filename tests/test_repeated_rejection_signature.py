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

from ralphd.engine import findings, transitions
from ralphd.engine.runner import IterationResult
from tests.conftest import supervisor_factory


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
