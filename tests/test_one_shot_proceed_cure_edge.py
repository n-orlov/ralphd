"""v0.36 C.3/D.2 (was PRD v0.27 C.2/C.3, #274, peer-gate finding VG-01): the
one-shot 'proceed' a bare unpause used to grant is gone (`noRulingProceed`);
its meaning -- "carry on with what the engine would do" -- is the input
timeout's outcome, or an explicit `POST /answer`. What these nodes still pin
is the rule the proceed existed for: an answer authorises ONE thing and is
spent by it.

* Answering a park with `cureRounds: 1` adds one round to the current
  approach's allowance and takes the cure edge, even when the rejection
  would otherwise raise another same-signature operator gate (a reviewer's
  `raise_operator_gate`, or the mechanical reopen-validated trigger).
* The answer never survives that cure: a later same-signature rejection with
  the approach's rounds spent again parks afresh instead of archiving on the
  operator's earlier answer.
* A bare unpause grants nothing, even when the rejection's curability
  flips.

The first three tests are the gate's own reproducers
(`artifacts/peer-gate-current/probes.py`); the rest are sibling cases of
the same rule.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ralphd.engine import findings
from ralphd.engine.api import create_app
from ralphd.engine.loop import LoopSupervisor
from tests.test_escalation_preview_real_edge import (
    _CURE_REOPEN_FINDING, _lane_tasks)
from tests.test_unpause_one_shot_proceed import (
    _MIXED_FINDINGS, _bounded, _events, _finding, _gate_findings_a,
    _gate_fixture, _gate_rejects, _result, _supervisor)


_CURABLE = [{"severity": "blocking", "curable": True,
             "clause": "C.3 live budget",
             "evidence": "one fixable behavior is missing",
             "cured_when": "The missing behavior works"}]


def _answer_one_cure_round(sup) -> None:
    """Answer the park with one cure round through the real `POST /answer`."""
    async def answer():
        app = create_app(sup.cfg, sup.run, sup)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://engine") as client:
            answered = await client.post("/answer", json={"cureRounds": 1})
            assert answered.status_code == 200, answered.text
    _bounded(answer())


def _review_rejects(sup, *, raise_gate: bool) -> None:
    async def reject(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "one gap",
                                   "raise_operator_gate": raise_gate}))
    sup.runner.run = reject  # type: ignore[method-assign]


def _proceeds(sup) -> dict:
    return sup.run.read_status().get("noRulingProceed") or {}


def _step_paused(sup) -> tuple:
    return _bounded(sup._step_paused())


@pytest.mark.parametrize("trigger", ["reviewer-request", "reopen-validated"])
def test_review_answer_takes_cure_even_with_operator_request(
        tmp_path, trigger):
    sup = _supervisor(tmp_path / "run", max_approaches=2,
                      operator_input_needed_pause=True, cure_pass_ceiling=0)
    sup._approach = 1
    items = _CURABLE
    if trigger == "reopen-validated":
        items = _CURE_REOPEN_FINDING
        sup.run.write_tasks({"version": 1, "tasks": _lane_tasks()})
    findings.write_findings_json(sup.run.findings_json_file, items)
    _review_rejects(sup, raise_gate=trigger == "reviewer-request")

    first = _bounded(sup._step_review())
    assert first == ("advance", "paused", "operator_input_needed")

    _answer_one_cure_round(sup)
    assert sup.run.read_status()["cureRoundTopUps"] == {"1": 1}
    assert not _proceeds(sup)

    second = _step_paused(sup)
    assert second == ("advance", "cure", "rejected_curable_pass_available")
    assert len(_events(sup.run.root, "operator_escalation")) == 1


def test_gate_answer_is_spent_by_its_cure_before_a_later_rejection(
        tmp_path, monkeypatch):
    sup = _gate_fixture(tmp_path / "run", gate_findings=_gate_findings_a())
    monkeypatch.setattr(LoopSupervisor, "run_iteration",
                        _gate_rejects("same fixable gap"))

    first = _bounded(sup._step_verdict_gate())
    assert first == ("advance", "paused", "verdict_gate_operator_input_needed")

    _answer_one_cure_round(sup)
    second = _step_paused(sup)
    assert second == ("advance", "cure",
                      "verdict_gate_rejected_curable_pass_available")
    assert not _proceeds(sup)

    # All cure work completes, but the gate rejects the same rule again.
    data = sup.run.read_tasks()
    for task in data["tasks"]:
        task["status"] = "validated"
    sup.run.write_tasks(data)

    third = _bounded(sup._step_verdict_gate())
    assert third == ("advance", "paused", "verdict_gate_operator_input_needed")
    assert not (sup.run.root / "approaches" / "01").exists()
    assert len(_events(sup.run.root, "operator_escalation")) == 2


# --- sibling cases of the same rule --------------------------------------


def test_review_answer_spent_on_cure_does_not_authorise_a_later_archive(
        tmp_path):
    """Review's mirror of the gate case above: after the answered cure, the
    same rejection with the approach's rounds spent again parks, it never
    archives on the operator's earlier answer."""
    sup = _supervisor(tmp_path / "run", max_approaches=2,
                      operator_input_needed_pause=True, cure_pass_ceiling=0)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _CURABLE)
    _review_rejects(sup, raise_gate=False)

    assert _bounded(sup._step_review()) == (
        "advance", "paused", "operator_input_needed")
    _answer_one_cure_round(sup)
    assert _step_paused(sup) == (
        "advance", "cure", "rejected_curable_pass_available")
    assert not _proceeds(sup)

    # The cure work completes; the confirmation review re-files the same
    # finding against the cure task it did not discharge.
    data = sup.run.read_tasks()
    for task in data["tasks"]:
        task["status"] = "validated"
    sup.run.write_tasks(data)
    (cure_id,) = [t["id"] for t in data["tasks"]
                  if t.get("origin") == "review-cure"]
    findings.write_findings_json(sup.run.findings_json_file,
                                 [dict(_CURABLE[0], reopens=cure_id)])

    third = _bounded(sup._step_review())
    assert third == ("advance", "paused", "operator_input_needed")
    assert not (sup.run.root / "approaches" / "01").exists()
    assert len(_events(sup.run.root, "operator_escalation")) == 2


def test_review_bare_resume_grants_nothing_even_when_curability_flips(
        tmp_path):
    """A bare unpause is not an answer: the same clause set, now incurable,
    parks again (v0.36 D.2 -- there is no one-shot proceed to spend, and no
    `rejected_incurable` edge), and so does the next one."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2,
                      operator_input_needed_pause=True)
    sup._approach = 1
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _review_rejects(sup, raise_gate=False)

    assert _bounded(sup._step_review()) == (
        "advance", "paused", "operator_input_needed")
    sup.resume()
    assert not _proceeds(sup)

    incurable = [_finding("blocking", False, f["clause"], f["evidence"])
                 for f in _MIXED_FINDINGS]
    findings.write_findings_json(sup.run.findings_json_file, incurable)
    assert _bounded(sup._step_review()) == (
        "advance", "paused", "operator_input_needed")
    assert not (root / "approaches" / "01").exists()
    sup.resume()

    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    third = _bounded(sup._step_review())
    assert third == ("advance", "paused", "operator_input_needed")
