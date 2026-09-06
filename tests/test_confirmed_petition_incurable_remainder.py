"""Task 013: pin the A/B/C interaction end to end -- a CONFIRMED petition
removes its obligation from the blocking set (requirement A's own
computation, `engine.petitions.downgrade_confirmed_findings`, exercised by
task 011's `tests/test_petition_adjudications_run_scope.py`), and when
whatever blocking findings remain are ALL incurable the run takes
`rejected_incurable` and pauses through the existing machinery (requirement
B, task 006's `tests/test_rejected_incurable_edge.py`) rather than the
ordinary `rejected_approach_left` replan edge.

Neither task 006 nor task 011's own suite drives this combination: 006
seeds an all-incurable blocking set directly, with no petition involved at
all; 011 confirms a petition and shows the matching finding downgraded, but
its own "still rejected" case (`test_approach_n_plus_1_review_no_longer_
sees_the_obligation_as_blocking`) leaves the SECOND, unrelated finding at
its fixture default `curable: True`, so that scenario takes the ordinary
replan edge, not `rejected_incurable`. This module is the missing case: the
obligation a petition confirms would, on its own, have blocked replan
forever (it is incurable); after the downgrade only an UNRELATED, also
incurable finding remains blocking, and it alone must be enough to trigger
the edge -- proving the two computations compose in the order the PRD
implies (A's downgrade happens, then B's all-incurable check runs against
what is left, never against the pre-downgrade set).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings, petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory

CONFIRMED_OBLIGATION = "Requirement B, DoD predicate 9"


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


_supervisor = supervisor_factory(run_id="task013-confirmed-then-incurable",
                                 cfg_defaults=False, status={"state": "running"})


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


def test_confirmed_obligation_removed_then_unrelated_incurable_remainder_pauses(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    # Requirement A: an EARLIER approach's review already confirmed a
    # petition against CONFIRMED_OBLIGATION -- run-scope persistence, the
    # exact mechanism `tests/test_petition_adjudications_run_scope.py`
    # (task 011) pins.
    petitions.record_confirmed_adjudications(
        sup.run, [{"obligation": CONFIRMED_OBLIGATION,
                   "adjudication": "CONFIRMED"}])
    obligations, tampered = petitions.confirmed_obligations(sup.run)
    assert tampered is False
    assert obligations == {CONFIRMED_OBLIGATION.lower()}

    # This approach's own findings sidecar carries TWO blocking findings:
    # the confirmed one (would, alone, have been an incurable blocker --
    # the "unachievable obligation" petitions exist for in the first
    # place) and a second, wholly unrelated finding that is ALSO incurable.
    confirmed_finding = _finding(
        "blocking", False, CONFIRMED_OBLIGATION,
        "the obligation is unachievable as written -- the exact fact "
        "petitioned and confirmed")
    unrelated_incurable = _finding(
        "blocking", False, "requirement D (#99)",
        "the deadline already passed before this approach started")
    findings.write_findings_json(
        sup.run.findings_json_file, [confirmed_finding, unrelated_incurable])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "both findings block, one of them already "
                      "petitioned and confirmed",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    # Requirement A's own computation: the confirmed obligation's finding
    # is downgraded to a disclosed residual, and is therefore no longer in
    # the blocking set -- the unrelated finding still is.
    live_findings, live_errors = sup._review_findings_channel()
    assert live_errors == []
    by_clause = {f["clause"]: f["severity"] for f in live_findings}
    assert by_clause[CONFIRMED_OBLIGATION] == "residual"
    assert by_clause["requirement D (#99)"] == "blocking"

    assert sup._pause.is_set() is True  # not paused yet

    outcome = asyncio.run(sup._step_review())

    # Requirement B: what remains blocking (just the unrelated finding) is
    # entirely incurable, so the run takes rejected_incurable -- NOT the
    # ordinary replan edge -- and pauses through the existing machinery.
    assert outcome == ("advance", "review", "rejected_incurable")
    assert sup._pause.is_set() is False
    assert not (root / "approaches" / "01").exists()
    assert sup._approach == 1

    # The escalation names the surviving blocking finding and its amend
    # invocation, and does NOT re-surface the confirmed-and-downgraded
    # obligation as something an amend would need to resolve -- it is a
    # disclosed residual, not part of the incurable set that caused the
    # edge (the operator ruling on task 006/DoD predicate 4: "each
    # finding" means the incurable BLOCKING set, /config/amendments/
    # 001-006.md).
    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert unrelated_incurable["clause"] in text
    assert unrelated_incurable["evidence"] in text
    assert text.count("ralphctl amend") == 1
    assert confirmed_finding["evidence"] not in text


def test_without_the_confirmation_the_same_pair_would_still_be_all_incurable(
        tmp_path):
    """Sanity check for the composition claim above: with NO confirmed
    petition on file, the very same two findings are already all
    incurable and already take the edge on their own -- proving the first
    test's edge is not an artefact of only one finding surviving, and that
    the downgrade in the first test genuinely changed which findings were
    in play (one residual, one blocking) rather than leaving the outcome
    unchanged either way."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    sup.notifier = _StubNotifier()

    obligations, tampered = petitions.confirmed_obligations(sup.run)
    assert tampered is False
    assert obligations == set()

    confirmed_finding = _finding(
        "blocking", False, CONFIRMED_OBLIGATION,
        "the obligation is unachievable as written -- the exact fact "
        "petitioned and confirmed")
    unrelated_incurable = _finding(
        "blocking", False, "requirement D (#99)",
        "the deadline already passed before this approach started")
    findings.write_findings_json(
        sup.run.findings_json_file, [confirmed_finding, unrelated_incurable])

    live_findings, live_errors = sup._review_findings_channel()
    assert live_errors == []
    by_clause = {f["clause"]: f["severity"] for f in live_findings}
    assert by_clause[CONFIRMED_OBLIGATION] == "blocking"
    assert by_clause["requirement D (#99)"] == "blocking"

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "both findings block, neither confirmed",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "review", "rejected_incurable")
    assert sup._pause.is_set() is False
    text = sup.notifier.messages[0]
    assert confirmed_finding["clause"] in text
    assert unrelated_incurable["clause"] in text
    assert text.count("ralphctl amend") == 2
