"""PRD v0.27 C.2/C.3 (#274), peer-gate finding VG-01: the one-shot
'proceed' a bare unpause of an A.1 (`operator_input_needed`) or gate C.1
(`verdict_gate_operator_input_needed`) pause grants is spent by the NEXT
matching rejection, whichever edge that rejection takes.

* Granting cure budget (`PATCH /config/budget curePassCeiling +1`) and then
  unpausing takes the cure edge, even when the repeated rejection would
  otherwise raise another same-signature operator gate (a reviewer's
  `raise_operator_gate`, or the mechanical reopen-validated trigger).
* The grant never survives that cure: a later same-signature rejection
  with the ceiling spent again parks afresh instead of archiving on the
  operator's earlier answer.

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


def _grant_and_unpause(sup) -> None:
    async def answer():
        app = create_app(sup.cfg, sup.run, sup)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://engine") as client:
            grant = await client.patch("/config/budget",
                                       json={"curePassCeiling": "+1"})
            assert grant.status_code == 200, grant.text
            resumed = await client.post("/resume")
            assert resumed.status_code == 200, resumed.text
    _bounded(answer())


def _review_rejects(sup, *, raise_gate: bool) -> None:
    async def reject(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "one gap",
                                   "raise_operator_gate": raise_gate}))
    sup.runner.run = reject  # type: ignore[method-assign]


def _proceeds(sup) -> dict:
    return sup.run.read_status().get("noRulingProceed") or {}


@pytest.mark.parametrize("trigger", ["reviewer-request", "reopen-validated"])
def test_review_budget_grant_unpause_takes_cure_even_with_operator_request(
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

    _grant_and_unpause(sup)
    assert sup.run.read_status()["curePassCeiling"] == 1
    assert _proceeds(sup).get("operator_input_needed")

    second = _bounded(sup._step_review())
    assert second == ("advance", "cure", "rejected_curable_pass_available")
    assert len(_events(sup.run.root, "operator_escalation")) == 1
    assert "operator_input_needed" not in _proceeds(sup)


def test_gate_budget_cure_consumes_proceed_before_a_later_rejection(
        tmp_path, monkeypatch):
    sup = _gate_fixture(tmp_path / "run", gate_findings=_gate_findings_a())
    monkeypatch.setattr(LoopSupervisor, "run_iteration",
                        _gate_rejects("same fixable gap"))

    first = _bounded(sup._step_verdict_gate())
    assert first == ("advance", "paused", "verdict_gate_operator_input_needed")

    _grant_and_unpause(sup)
    second = _bounded(sup._step_verdict_gate())
    assert second == ("advance", "cure",
                      "verdict_gate_rejected_curable_pass_available")
    assert not _proceeds(sup).get("verdict_gate_operator_input_needed")

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


def test_review_proceed_spent_on_cure_does_not_authorise_a_later_archive(
        tmp_path):
    """Review's mirror of the gate case above: after the granted cure, the
    same rejection with the ceiling spent again parks, it never archives
    on the operator's earlier unpause."""
    sup = _supervisor(tmp_path / "run", max_approaches=2,
                      operator_input_needed_pause=True, cure_pass_ceiling=0)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _CURABLE)
    _review_rejects(sup, raise_gate=False)

    assert _bounded(sup._step_review()) == (
        "advance", "paused", "operator_input_needed")
    _grant_and_unpause(sup)
    assert _bounded(sup._step_review()) == (
        "advance", "cure", "rejected_curable_pass_available")
    assert "operator_input_needed" not in _proceeds(sup)

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


def test_review_proceed_spent_by_an_incurable_edge_is_gone_afterwards(
        tmp_path):
    """The matching rejection spends the grant even when it takes the
    `rejected_incurable` edge (same clause set, curability flipped), so
    the next same-signature rejection parks rather than archiving."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2,
                      operator_input_needed_pause=True)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _review_rejects(sup, raise_gate=False)

    assert _bounded(sup._step_review()) == (
        "advance", "paused", "operator_input_needed")
    sup.resume()
    assert _proceeds(sup).get("operator_input_needed")

    incurable = [_finding("blocking", False, f["clause"], f["evidence"])
                 for f in _MIXED_FINDINGS]
    findings.write_findings_json(sup.run.findings_json_file, incurable)
    assert _bounded(sup._step_review()) == (
        "advance", "review", "rejected_incurable")
    assert "operator_input_needed" not in _proceeds(sup)
    sup.resume()  # releases the incurable hold; grants nothing new

    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    third = _bounded(sup._step_review())
    assert third == ("advance", "paused", "operator_input_needed")
    assert not (root / "approaches" / "01").exists()
