"""PRD v0.37 requirement D.1-D.3 and D.5 (and C.4's waived pass): a rejected
phase climbs the v0.36 rejection ladder with the phase as its scope, for the
phase review AND for the phase-scoped peer gate. Every node runs over a
verdict with exactly two blocking findings:

* the budget spent                      -> `exhausted`;
* both blocking findings waived         -> the gate is `passed`;
* park                                  -> `awaiting_input`, with
  `inputQuestion.planPhase` set (a `curable: false` finding, the reviewer's
  `raise_operator_gate`, spent cure rounds);
* no verdict                            -> `_park_judge_no_verdict`, an infra
  fault: neither a rejection nor a pass.

and the three answers of a park (K = 1 cures in that phase, waiving both
blocking findings opens the gate, K = 0 archives the approach and replans).

The loop is driven with fake hats over a git workspace
(`tests/test_phase_gate_cure.py`'s helpers); nothing sleeps."""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import state
from ralphd.engine.runner import IterationResult

from tests.test_phase_gate_cure import (
    _cure_ids, _finding, _rejecting, _seeded_sup, _two_findings, _verified)
from tests.test_phase_review_dispatch import _events

PARK_REASON = "phase_gate_operator_input_needed"


def _sup(tmp_path, judge, *, findings_list=None, payload_extra=None,
         hat=None, **cfg):
    """A supervisor whose `judge` (`review` or `verdict_gate`) rejects phase
    `one` with the two-finding fixture, or runs `hat` instead when given (the
    other judge, when on, verifies)."""
    def reject(sup, prompt=""):
        out = _rejecting(sup, findings_list, gate=judge == "verdict_gate")
        if payload_extra:
            out.final_text = json.dumps(
                {**json.loads(out.final_text), **payload_extra})
        return out

    hats = ({"review": [hat or reject]} if judge == "review"
            else {"review": [_verified], "verdict_gate": [hat or reject]})
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    (tmp_path / "run" / "prd.md").write_text("# PRD\n")
    return _seeded_sup(tmp_path, hats, peer_verdict_gate=judge == "verdict_gate",
                       **cfg)


def _gate(sup, pid="one") -> str:
    return state.phase_entry(sup.run.read_tasks(), pid)["gate"]


def _no_cure_was_taken(sup):
    assert _cure_ids(sup) == []
    assert _events(sup, "phase_gate.rejected") == []
    assert state.read_cure_lineage(sup.run.root)["passes"] == []


# -- D.1 budget spent -> exhausted ------------------------------------------------

def _exhausted(tmp_path, judge):
    def spend_the_budget(sup, prompt=""):
        sup.cfg.iterations = sup.iterations_used
        return _rejecting(sup, gate=judge == "verdict_gate")

    sup = _sup(tmp_path, judge, hat=spend_the_budget, iterations=60)
    outcome = asyncio.run(sup._run_phase_review("one"))
    assert outcome == ("tail",)
    assert sup._budget_exhausted_tail is True
    assert _gate(sup) == "closed"
    _no_cure_was_taken(sup)
    assert _events(sup, "phase_gate.passed") == []
    assert sup.run.read_status().get("inputQuestion") is None
    assert sup._finish_tail_unverified() == "exhausted"
    status = sup.run.read_status()
    assert status["state"] == "exhausted"
    assert status["exhaustedBudget"] == "iterations"


def test_a_phase_review_rejection_with_the_budget_spent_is_exhausted(tmp_path):
    _exhausted(tmp_path, "review")


def test_a_phase_peer_rejection_with_the_budget_spent_is_exhausted(tmp_path):
    _exhausted(tmp_path, "verdict_gate")


# -- D.2 every blocking finding waived -> pass ---------------------------------------

def _waived(tmp_path, judge):
    sup = _sup(tmp_path, judge)
    for clause in ("requirement A", "requirement B"):
        sup.run.add_waiver(clause=clause, finding=f"{clause} is not met",
                           judge=judge, note="accepted")
    assert asyncio.run(sup._run_phase_review("one")) is None
    assert _gate(sup) == "passed"
    (passed,) = _events(sup, "phase_gate.passed")
    assert passed["planPhase"] == "one" and passed["nextPhase"] == "two"
    (waived,) = _events(sup, "rejection.waived")
    assert waived["judge"] == judge and waived["planPhase"] == "one"
    assert len(waived["findings"]) == 2
    _no_cure_was_taken(sup)
    assert sup.run.read_status().get("inputQuestion") is None
    assert not sup.run.findings_json_file.exists()
    assert not sup.run.verdict_gate_findings_json_file.exists()


def test_a_phase_review_rejection_with_both_blocking_findings_waived_passes_the_gate(tmp_path):
    _waived(tmp_path, "review")


def test_a_phase_peer_rejection_with_both_blocking_findings_waived_passes_the_gate(tmp_path):
    _waived(tmp_path, "verdict_gate")


# -- D.3 park -> awaiting_input with planPhase ---------------------------------------

def _park_cases(judge):
    """(case, `_sup` arguments, the trigger the question carries). The
    reviewer's `raise_operator_gate` is a field of the `review` contract; the
    `verdict_gate` contract is a closed schema without it (so the final peer
    gate has no such trigger either), which leaves a peer gate two cases."""
    incurable = _two_findings()
    incurable[1] = _finding("requirement B", curable=False)
    cases = [
        ("a curable: false finding", {"findings_list": incurable},
         "incurable"),
        ("spent cure rounds", {"cure_pass_ceiling": 0}, "cure_rounds_spent"),
    ]
    if judge == "review":
        cases.insert(1, ("raise_operator_gate",
                         {"payload_extra": {"raise_operator_gate": True}},
                         "reviewer_escalation"))
    return cases


def _parks(tmp_path, judge):
    for index, (case, extra, trigger) in enumerate(_park_cases(judge)):
        sup = _sup(tmp_path / f"case{index}", judge,
                   operator_input_needed_pause=True, **extra)
        outcome = asyncio.run(sup._run_phase_review("one"))
        assert outcome == ("advance", "paused", PARK_REASON), case
        status = sup.run.read_status()
        assert status["state"] == "awaiting_input", case
        question = status["inputQuestion"]
        assert question["planPhase"] == "one", case
        assert question["judge"] == judge and question["trigger"] == trigger, case
        assert question["kind"] == "findings"
        assert [f["clause"] for f in question["findings"]] == [
            "requirement A", "requirement B"]
        (parked,) = _events(sup, "input.parked")
        assert parked["planPhase"] == "one"
        assert _gate(sup) == "closed"
        _no_cure_was_taken(sup)
        assert _events(sup, "phase_gate.passed") == []
        # the park leaves `worker` on a declared edge
        assert sup._advance("worker", "paused", PARK_REASON) == "paused"


def test_a_phase_review_rejection_parks_in_awaiting_input_with_the_plan_phase(tmp_path):
    _parks(tmp_path, "review")


def test_a_phase_peer_rejection_parks_in_awaiting_input_with_the_plan_phase(tmp_path):
    _parks(tmp_path, "verdict_gate")


# -- D.5 no verdict -> _park_judge_no_verdict ------------------------------------------

def _truncated(sup, prompt=""):
    r = IterationResult(exit_code=0)
    r.final_text = ""
    r.output_truncated = True
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 4096, "totalTokens": 4106}
    return r


def _no_verdict(tmp_path, judge):
    sup = _sup(tmp_path, judge, hat=_truncated, infra_outage_budget_s=0.0)
    calls = []
    real = sup._park_judge_no_verdict

    async def spy(hat, approach, reason):
        calls.append((hat, approach, reason))
        return await real(hat, approach, reason)

    sup._park_judge_no_verdict = spy
    outcome = asyncio.run(sup._run_phase_review("one"))
    assert outcome == ("tail",)
    assert [(c[0], c[2]) for c in calls] == [(judge, "output_truncated")]
    assert _gate(sup) == "closed"
    # neither a rejection ...
    _no_cure_was_taken(sup)
    assert sup.run.read_status().get("inputQuestion") is None
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    # ... nor a pass
    assert _events(sup, "phase_gate.passed") == []
    assert (sup.run.read_status()["gateRejectionEnding"]
            == state.GATE_REJECTION_ENDING_NO_VERDICT)
    assert sup._finish_tail_unverified() == "failed"
    status = sup.run.read_status()
    assert status["verdict"] == "unreviewed"


def test_a_phase_review_with_no_verdict_is_an_infra_fault_never_a_rejection_or_a_pass(tmp_path):
    _no_verdict(tmp_path, "review")


def test_a_phase_peer_gate_with_no_verdict_is_an_infra_fault_never_a_rejection_or_a_pass(tmp_path):
    _no_verdict(tmp_path, "verdict_gate")


# -- the answers of a park (v0.36 C.3, with the phase as scope) ------------------------

def _parked_sup(tmp_path, **cfg):
    """Phase one parked on spent cure rounds over the two curable findings."""
    sup = _sup(tmp_path, "review", operator_input_needed_pause=True,
               cure_pass_ceiling=0, **cfg)
    assert asyncio.run(sup._run_phase_review("one")) == (
        "advance", "paused", PARK_REASON)
    return sup


def test_a_park_answer_with_one_cure_round_inserts_the_cure_tasks_in_that_phase(tmp_path):
    sup = _parked_sup(tmp_path)
    sup.answer(cure_rounds=1)
    outcome = asyncio.run(sup._step_paused())
    assert outcome == ("advance", "worker", "paused_resumed_phase_gate")
    assert sup._advance("paused", outcome[1], outcome[2]) == "worker"
    cures = [t for t in sup.run.read_tasks()["tasks"]
             if t.get("origin") == "review-cure"]
    assert len(cures) == 2 and {t["phase"] for t in cures} == {"one"}
    assert all(t["status"] == "pending" for t in cures)
    (entry,) = state.read_cure_lineage(sup.run.root)["passes"]
    assert entry["planPhase"] == "one" and entry["kind"] == "granted"
    assert _gate(sup) == "closed"
    assert state.phase_entry(sup.run.read_tasks(), "one")["rounds"] == 1
    (rejected,) = _events(sup, "phase_gate.rejected")
    assert rejected["planPhase"] == "one" and rejected["blockingFindings"] == 2
    status = sup.run.read_status()
    assert status["state"] == "running" and status.get("inputQuestion") is None
    assert status["curePasses"] == {"1": 1}
    assert _events(sup, "phase_gate.passed") == []


def test_a_park_answer_waiving_both_blocking_findings_sets_the_gate_passed(tmp_path):
    sup = _parked_sup(tmp_path)
    ids = [f["id"] for f in sup.run.read_status()["inputQuestion"]["findings"]]
    assert len(ids) == 2
    record = sup.answer(waive=ids)
    assert record["outcome"] == "verified" and record["planPhase"] == "one"
    outcome = asyncio.run(sup._step_paused())
    assert outcome == ("advance", "worker", "paused_resumed_phase_gate")
    assert sup._advance("paused", outcome[1], outcome[2]) == "worker"
    assert _gate(sup) == "passed"
    (passed,) = _events(sup, "phase_gate.passed")
    assert passed["planPhase"] == "one" and passed["nextPhase"] == "two"
    assert state.phase_entry(sup.run.read_tasks(), "two")["startHead"]
    _no_cure_was_taken(sup)
    assert sup.run.read_status()["state"] == "running"
    assert not sup.run.findings_json_file.exists()


def test_a_park_answer_with_no_cure_rounds_archives_the_approach_and_replans(tmp_path):
    sup = _parked_sup(tmp_path, max_approaches=2)
    sup.answer(cure_rounds=0)
    outcome = asyncio.run(sup._step_paused())
    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup._advance("paused", outcome[1], outcome[2]) == "replan"
    archive = sup.run.root / "approaches" / "01"
    assert (archive / "tasks.json").exists()
    # the rejection's findings were archived with the approach, then cleared
    archived = json.loads((archive / "review-findings.json").read_text())
    assert [f["clause"] for f in archived] == [
        "requirement A", "requirement B"]
    assert not sup.run.findings_json_file.exists()
    _no_cure_was_taken(sup)
    assert _events(sup, "phase_gate.passed") == []
    assert sup.run.read_status()["state"] == "running"


def test_a_parked_phase_is_answered_through_the_dispatch_loop(tmp_path):
    """The park and its answer ride the real dispatch loop (`worker ->
    paused -> worker`): the run leaves `running` for `awaiting_input` on the
    phase's own question, an answer waiving both findings opens the gate, and
    the plan goes on to the last phase and the final review."""
    from ralphd.engine.state import RunDir
    from tests.test_phase_gate_cure import _Judges, _supervisor
    from tests.test_phase_review_dispatch import _init_workspace, _plan, _worker

    judges = _Judges(reject={("one", 1)})
    _init_workspace(tmp_path / "workspace")
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = _supervisor(
        tmp_path, {"planning": [_plan], "worker": [_worker],
                   "review": [judges.review]},
        operator_input_needed_pause=True, cure_pass_ceiling=0)

    async def drive():
        job = asyncio.ensure_future(sup._run_job_core())
        answered = False
        while not job.done():
            status = sup.run.read_status()
            if status.get("state") == "awaiting_input" and not answered:
                question = status["inputQuestion"]
                assert question["planPhase"] == "one"
                sup.answer(waive=[f["id"] for f in question["findings"]])
                answered = True
            await asyncio.sleep(0.005)
        assert answered
        return await job

    assert asyncio.run(asyncio.wait_for(drive(), 90)) == "succeeded"
    assert _gate(sup, "one") == "passed"
    assert [e["planPhase"] for e in _events(sup, "phase_gate.passed")] == ["one"]
    assert [e["planPhase"] for e in _events(sup, "input.parked")] == ["one"]
    assert [e["planPhase"] for e in _events(sup, "input.answered")] == ["one"]
    _no_cure_was_taken(sup)
