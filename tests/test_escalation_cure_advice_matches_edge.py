"""PRD v0.27 requirement C.4 / E.1 (peer-gate finding VG-02): every
operator-facing escalation surface -- the `operator_escalation` event's own
`escalation` text and the digest actually delivered to the notifier --
derives its budget advice (`ralphctl budget <run> --cure-passes`) and the
edge it contrasts that advice with from the SAME actual cure eligibility.

A mixed or all-incurable rejection parked on a spent cure-pass ceiling
takes its merits tail whatever the ceiling, so neither surface may offer a
ceiling grant as the way to a cure; an all-curable one does take the cure
edge once the ceiling is raised, so both surfaces name the lever, and on
the run's last approach they contrast it with finishing the run rather
than with a replan that cannot happen.

Each advice is followed through the real `PATCH /config/budget` +
`POST /answer` API (v0.36: a parked run is answered, never unpaused) to prove
it describes the transition the engine takes.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ralphd.engine import findings
from ralphd.engine.api import create_app
from ralphd.engine.loop import LoopSupervisor
from tests.test_escalation_digest import _StubNotifier
from tests.test_unpause_one_shot_proceed import (
    _bounded,
    _events,
    _gate_findings_a,
    _gate_fixture,
    _gate_rejects,
    _mixed_1c_1i_gate_findings,
    _result,
    _supervisor,
)


def _grant_one_cure_pass_and_answer(sup):
    """Follow the advice: grant the ceiling, then answer the park with one
    cure round. Returns the `POST /answer` response."""
    async def answer():
        app = create_app(sup.cfg, sup.run, sup)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://engine") as client:
            grant = await client.patch(
                "/config/budget", json={"curePassCeiling": "+1"})
            assert grant.status_code == 200, grant.text
            return await client.post("/answer", json={"cureRounds": 1})
    return _bounded(answer())


def _gate_shape(shape: str) -> list[dict]:
    if shape == "curable":
        return _gate_findings_a()
    items = _mixed_1c_1i_gate_findings()
    if shape == "incurable":
        for item in items:
            item["curable"] = False
    return items


def _park_gate(tmp_path, monkeypatch, shape: str, *, max_approaches: int = 2):
    sup = _gate_fixture(tmp_path / "run", gate_findings=_gate_shape(shape))
    sup.cfg.max_approaches = max_approaches
    notifier = _StubNotifier()
    sup.notifier = notifier
    monkeypatch.setattr(LoopSupervisor, "run_iteration",
                        _gate_rejects(f"{shape} gate rejection"))
    outcome = _bounded(sup._step_verdict_gate())
    assert outcome == ("advance", "paused", "verdict_gate_operator_input_needed")
    escalation = _events(sup.run.root, "operator_escalation")[-1]
    assert len(notifier.messages) == 1
    return sup, escalation, notifier.messages[0]


# -- the gate's own reproducer (VG-02) --------------------------------------


@pytest.mark.parametrize("shape", ["mixed", "incurable"])
def test_gate_notifier_does_not_offer_an_unavailable_cure(
        tmp_path, monkeypatch, shape):
    sup, escalation, digest = _park_gate(tmp_path, monkeypatch, shape)
    assert escalation["insertCount"] == 0
    # an unanswered park of an uncurable gate rejection moves to the next
    # approach (v0.36 requirement B), never a cure it cannot take.
    assert escalation["nextStepIfUnanswered"] == "archive_and_replan"
    assert "--cure-passes" not in escalation["escalation"]
    assert "--cure-passes" not in digest

    # Following a ceiling grant cannot cure these findings: the answer's
    # cure rounds are refused for the accepted incurable finding.
    answered = _grant_one_cure_pass_and_answer(sup)
    assert answered.status_code == 409, answered.text
    assert "not curable" in answered.text
    assert sup.run.read_status()["state"] == "awaiting_input"
    assert sup.run.read_status().get(
        "gateRejectionEnding") == "rejected_on_the_merits"


# -- sibling cases ----------------------------------------------------------


@pytest.mark.parametrize("shape", ["curable", "mixed", "incurable"])
def test_gate_event_text_and_digest_agree_on_the_budget_lever(
        tmp_path, monkeypatch, shape):
    _, escalation, digest = _park_gate(tmp_path, monkeypatch, shape)
    offered = shape == "curable"
    assert ("--cure-passes" in escalation["escalation"]) is offered
    assert ("--cure-passes" in digest) is offered


def test_gate_all_curable_notification_offers_the_cure_it_can_take(
        tmp_path, monkeypatch):
    sup, escalation, digest = _park_gate(tmp_path, monkeypatch, "curable")
    assert escalation["nextStepIfUnanswered"] == "archive_and_replan"
    assert "instead of archive-and-replan" in digest
    assert "instead of archive-and-replan" in escalation["escalation"]

    answered = _grant_one_cure_pass_and_answer(sup)
    assert answered.status_code == 200, answered.text
    assert _bounded(sup._step_paused()) == (
        "advance", "cure", "verdict_gate_rejected_curable_pass_available")


def test_gate_last_approach_budget_advice_contrasts_with_finishing(
        tmp_path, monkeypatch):
    _, escalation, digest = _park_gate(
        tmp_path, monkeypatch, "curable", max_approaches=1)
    assert escalation["nextStepIfUnanswered"] == "finishing"
    for surface in (escalation["escalation"], digest):
        assert "--cure-passes" in surface
        assert "instead of finishing the run" in surface
        assert "archive-and-replan" not in surface


_REVIEW_MIXED = [
    {"severity": "blocking", "curable": False, "clause": "requirement B",
     "evidence": "an environment fact no cure task can change"},
    {"severity": "blocking", "curable": True, "clause": "requirement A.1",
     "evidence": "one fixable behaviour is missing",
     "cured_when": "the missing behaviour works"},
]

_REVIEW_CURABLE = [
    {"severity": "blocking", "curable": True, "clause": "requirement C.3",
     "evidence": "one fixable behaviour is missing",
     "cured_when": "the missing behaviour works"},
]


def _park_review(tmp_path, items, *, max_approaches: int,
                 reason: str = "operator_input_needed"):
    sup = _supervisor(tmp_path / "run", max_approaches=max_approaches,
                      operator_input_needed_pause=True, cure_pass_ceiling=0)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, items)
    notifier = _StubNotifier()
    sup.notifier = notifier

    async def reject(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "gap"}))
    sup.runner.run = reject  # type: ignore[method-assign]
    outcome = _bounded(sup._step_review())
    assert outcome == ("advance", "paused", reason)
    escalation = _events(sup.run.root, "operator_escalation")[-1]
    assert len(notifier.messages) == 1
    return sup, escalation, notifier.messages[0]


def test_review_mixed_ceiling_spent_surfaces_offer_no_ceiling_grant(tmp_path):
    sup, escalation, digest = _park_review(
        tmp_path, _REVIEW_MIXED, max_approaches=2)
    assert escalation["nextStepIfUnanswered"] == "archive_and_replan"
    assert "--cure-passes" not in escalation["escalation"]
    assert "--cure-passes" not in digest

    answered = _grant_one_cure_pass_and_answer(sup)
    assert answered.status_code == 409, answered.text
    assert "not curable" in answered.text


def test_review_last_approach_ceiling_spent_names_the_grant_that_cures(
        tmp_path):
    # All-curable on the last approach with the run-wide ceiling spent:
    # the last-approach park's own text and digest name the
    # ceiling grant, contrasted with finishing the run, and following it
    # takes the cure edge.
    sup, escalation, digest = _park_review(
        tmp_path, _REVIEW_CURABLE, max_approaches=1)
    for surface in (escalation["escalation"], digest):
        assert "--cure-passes" in surface
        assert "instead of finishing the run" in surface
        assert "archive-and-replan" not in surface

    answered = _grant_one_cure_pass_and_answer(sup)
    assert answered.status_code == 200, answered.text
    assert _bounded(sup._step_paused()) == (
        "advance", "cure", "rejected_curable_pass_available")
