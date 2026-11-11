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
(`tests/test_phase_gate_cure.py`'s helpers); nothing sleeps out a real
timeout. The D.5 real-runner block captures `PiRunner` results over stand-in
agents with a compressed 0.3 s timeout and 0.2 s grace, the way
`tests/test_iteration_timeout_contained.py` does, once per shape."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import shutil
import signal
import stat

import pytest

from ralphd.engine import runner as runner_mod
from ralphd.engine import state
from ralphd.engine.runner import IterationResult, PiRunner
from tests.conftest import sigint_default_for_children
from tests.test_phase_gate_cure import (
    _cure_ids,
    _finding,
    _rejecting,
    _seeded_sup,
    _two_findings,
    _verified,
)
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
    """(case, `_sup` arguments, the trigger the question carries): the same
    three park triggers for both judges. `raise_operator_gate` is an optional
    field of the `review` and the `verdict_gate` contract alike."""
    incurable = _two_findings()
    incurable[1] = _finding("requirement B", curable=False)
    return [
        ("a curable: false finding", {"findings_list": incurable},
         "incurable"),
        ("raise_operator_gate",
         {"payload_extra": {"raise_operator_gate": True}},
         "reviewer_escalation"),
        ("spent cure rounds", {"cure_pass_ceiling": 0}, "cure_rounds_spent"),
    ]


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


def _unparseable(sup, prompt=""):
    r = IterationResult(exit_code=0)
    r.final_text = "not a verdict"
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 20, "totalTokens": 30}
    return r


def _no_verdict_cases(judge):
    """(case, the hat's reply, the `_park_judge_no_verdict` reason): a
    truncated reply and an unparseable one that the contract ladder (every
    re-ask also malformed) and every judge retry cannot repair."""
    return [
        ("truncated", _truncated, "output_truncated"),
        ("unparseable", _unparseable,
         "unreadable_verdict" if judge == "review"
         else "unparseable_reply"),
    ]


def _no_verdict(tmp_path, judge):
    for index, (case, hat, reason) in enumerate(_no_verdict_cases(judge)):
        sup = _sup(tmp_path / f"case{index}", judge, hat=hat,
                   infra_outage_budget_s=0.0, contract_retry_max=2)
        calls = []
        fixes = []
        real = sup._park_judge_no_verdict

        async def spy(hat, approach, reason, real=real, calls=calls):
            calls.append((hat, approach, reason))
            return await real(hat, approach, reason)

        async def malformed_fix(hat, errors, final_text=None, *,
                                fixes=fixes):
            fixes.append(hat)
            return "still not a verdict", None

        sup._park_judge_no_verdict = spy
        sup._ask_contract_fix = malformed_fix
        outcome = asyncio.run(sup._run_phase_review("one"))
        assert outcome == ("tail",), case
        assert [(c[0], c[2]) for c in calls] == [(judge, reason)], case
        if case == "unparseable":
            # the contract ladder's re-asks ran, and none repaired the reply
            assert fixes and set(fixes) == {judge}, case
        assert _gate(sup) == "closed", case
        # neither a rejection ...
        _no_cure_was_taken(sup)
        assert sup.run.read_status().get("inputQuestion") is None, case
        assert _events(sup, "approach.cure_pass") == []
        assert _events(sup, "approach.gate_cure_pass") == []
        approaches = sup.run.root / "approaches"
        assert not approaches.exists() or not any(approaches.iterdir())
        # ... nor a pass
        assert _events(sup, "phase_gate.passed") == [], case
        (fault,) = _events(sup, f"{judge}.no_verdict")
        assert fault["reason"] == reason, case
        assert (sup.run.read_status()["gateRejectionEnding"]
                == state.GATE_REJECTION_ENDING_NO_VERDICT), case
        assert sup._finish_tail_unverified() == "failed", case
        status = sup.run.read_status()
        assert status["verdict"] == "unreviewed", case


def test_a_phase_review_with_no_verdict_is_an_infra_fault_never_a_rejection_or_a_pass(tmp_path):
    _no_verdict(tmp_path, "review")


def test_a_phase_peer_gate_with_no_verdict_is_an_infra_fault_never_a_rejection_or_a_pass(tmp_path):
    _no_verdict(tmp_path, "verdict_gate")


# -- D.5 a judge that crashes or times out has no verdict --------------------------

def _crashed(sup, prompt=""):
    r = IterationResult(exit_code=1)
    r.final_text = ""
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 4, "totalTokens": 14}
    return r


def _timed_out(sup, prompt=""):
    r = IterationResult(exit_code=None, timed_out=True)
    r.final_text = ""
    r.duration_s = 3600.0
    r.usage = {"input": 10, "output": 4, "totalTokens": 14}
    return r


def _counting(hat, calls):
    def counted(sup, prompt=""):
        calls.append(prompt)
        return hat(sup, prompt)
    return counted


def _guards(sup):
    """Record every `(state, target, guard)` the supervisor advances on (the
    executor writes no event for a self-loop), returned live."""
    seen: list[tuple[str, str, str]] = []
    real = sup._advance

    def spy(state_, target, guard):
        seen.append((state_, target, guard))
        return real(state_, target, guard)

    sup._advance = spy
    return seen


def _review_interrupted(guards):
    return [g for g in guards if g[2] == "review_interrupted"]


def _cut_short_run(tmp_path, judge, hat, reason):
    calls: list[str] = []
    sup = _sup(tmp_path, judge, hat=_counting(hat, calls),
               infra_outage_budget_s=0.0, iterations=50)
    retries = sup.cfg.judge_retry_max
    parked = []
    real = sup._park_judge_no_verdict

    async def spy(hat_name, approach, why):
        parked.append((hat_name, why))
        return await real(hat_name, approach, why)

    sup._park_judge_no_verdict = spy
    guards = _guards(sup)
    outcome = asyncio.run(sup._run_phase_review("one"))
    assert outcome == ("tail",)
    # the first attempt plus exactly the bounded judge retries, never the
    # review-interrupted self-loop spending the job's iterations
    assert len(calls) == 1 + retries
    assert len(_events(sup, f"{judge}.retry")) == retries
    assert parked == [(judge, reason)]
    assert _review_interrupted(guards) == []
    (fault,) = _events(sup, f"{judge}.no_verdict")
    assert fault["reason"] == reason
    assert (sup.run.read_status()["gateRejectionEnding"]
            == state.GATE_REJECTION_ENDING_NO_VERDICT)
    # no rejection, no pass: the gate stays closed
    assert _gate(sup) == "closed"
    _no_cure_was_taken(sup)
    assert _events(sup, "phase_gate.passed") == []
    assert sup.run.read_status().get("inputQuestion") is None
    assert sup._finish_tail_unverified() == "failed"
    assert sup.run.read_status()["verdict"] == "unreviewed"
    return sup


def test_a_phase_judge_that_crashes_gets_the_judge_retries_then_the_no_verdict_park(tmp_path):
    for judge in ("review", "verdict_gate"):
        _cut_short_run(tmp_path / judge, judge, _crashed, "judge_crashed")


def test_a_phase_judge_that_times_out_gets_the_judge_retries_then_the_no_verdict_park(tmp_path):
    for judge in ("review", "verdict_gate"):
        _cut_short_run(tmp_path / judge, judge, _timed_out, "judge_timed_out")


def test_a_phase_judge_that_crashes_once_and_then_answers_is_read_normally(tmp_path):
    for judge in ("review", "verdict_gate"):
        attempts = []

        def flaky(sup, prompt="", judge=judge, attempts=attempts):
            attempts.append(1)
            if len(attempts) == 1:
                return _crashed(sup, prompt)
            return _verified(sup, prompt)

        sup = _sup(tmp_path / judge, judge, hat=flaky,
                   infra_outage_budget_s=0.0)
        assert asyncio.run(sup._run_phase_review("one")) is None
        assert _gate(sup) == "passed"
        assert len(_events(sup, f"{judge}.retry")) == 1
        assert _events(sup, f"{judge}.no_verdict") == []


def test_a_phase_judge_that_crashes_and_then_times_out_is_a_no_verdict_with_the_last_reason(tmp_path):
    for judge in ("review", "verdict_gate"):
        attempts = []

        def mixed(sup, prompt="", attempts=attempts):
            attempts.append(1)
            return (_crashed if len(attempts) == 1 else _timed_out)(
                sup, prompt)

        _cut_short_run(tmp_path / judge, judge, mixed, "judge_timed_out")


def test_a_cut_short_phase_judge_spends_no_iterations_while_the_outage_budget_waits(tmp_path):
    sup = _sup(tmp_path, "review", hat=_crashed, infra_outage_budget_s=0.0,
               iterations=50)
    asyncio.run(sup._run_phase_review("one"))
    assert sup.iterations_used_charged <= 1


def _operator_interrupts(sup, result):
    """What `POST /interrupt` does to a running judge: `interrupt()` delivers
    the signal (the fake runner has no child, so delivery is stubbed) and the
    supervisor records that its operator interrupted this iteration."""
    sup._interrupt_runners = lambda: True
    assert sup.interrupt() is True
    return result


def test_an_operator_interrupt_of_a_phase_review_keeps_the_review_interrupted_handling(tmp_path):
    calls = []

    def interrupted_then_verified(sup, prompt=""):
        calls.append(1)
        if len(calls) == 1:
            # the shape the real runner returns for an operator's SIGINT
            return _operator_interrupts(sup, _replay("operator_interrupt"))
        return _verified(sup, prompt)

    _captured("operator_interrupt")  # captured outside the engine's event loop
    sup = _sup(tmp_path, "review", hat=interrupted_then_verified,
               infra_outage_budget_s=0.0)
    guards = _guards(sup)
    assert asyncio.run(sup._run_phase_review("one")) is None
    assert len(calls) == 2
    assert len(_review_interrupted(guards)) == 1
    assert _events(sup, "review.retry") == []
    assert _events(sup, "review.no_verdict") == []
    assert _gate(sup) == "passed"


def test_a_cut_short_review_outside_a_phase_scope_keeps_the_review_interrupted_loop(tmp_path):
    calls = []

    def crashed_then_verified(sup, prompt=""):
        calls.append(1)
        return (_crashed if len(calls) == 1 else _verified)(sup, prompt)

    sup = _sup(tmp_path, "review", hat=crashed_then_verified,
               infra_outage_budget_s=0.0)
    sup._phase_review_scope = None
    result = asyncio.run(sup._run_review_iteration(1))
    assert result is not None and len(calls) == 2
    assert sup._review_no_verdict is None
    assert _events(sup, "review.retry") == []
    assert sup._phase_judge_cut_short(_crashed(sup)) is None


# -- D.5 over the results the REAL runner returns ----------------------------------
#
# `PiRunner` marks every negative exit `interrupted`: the engine's own timeout
# (SIGINT, exit -2; or SIGKILL after the grace, exit -9) and a death by signal
# such as SIGSEGV (-11) all carry the flag an operator's interrupt carries.
# These results are captured from the real runner over a stand-in agent that
# prints partial, non-verdict text first, then replayed into both phase judges.

_PARTIAL = ("echo '{\"type\": \"message_end\", \"message\": "
            "{\"role\": \"assistant\", \"content\": [{\"type\": \"text\","
            " \"text\": \"hi\"}], \"usage\": {\"input\": 1, \"output\": 1,"
            " \"totalTokens\": 2}}}'\n")

_AGENTS = {
    # the engine's full iteration timeout; the agent exits on its SIGINT
    "timeout_obeys_sigint": "cat >/dev/null\n" + _PARTIAL + "exec sleep 9999\n",
    # the engine's full iteration timeout; the agent ignores SIGINT -> SIGKILL
    "timeout_ignores_sigint": ("trap '' INT\ncat >/dev/null\n" + _PARTIAL
                               + "exec sleep 9999\n"),
    # the agent dies by SIGSEGV on its own
    "sigsegv": "cat >/dev/null\n" + _PARTIAL + "kill -SEGV $$\n",
    # the agent dies by SIGKILL nobody in the engine sent (an OOM kill)
    "sigkill": "cat >/dev/null\n" + _PARTIAL + "kill -KILL $$\n",
    # an operator's interrupt (`PiRunner.interrupt`) of a running agent
    "operator_interrupt": "cat >/dev/null\n" + _PARTIAL + "exec sleep 9999\n",
}


@functools.cache
def _captured(kind: str) -> IterationResult:
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix=f"d5-{kind}-"))
    agent = root / "fake-pi"
    agent.write_text("#!/bin/sh\n" + _AGENTS[kind])
    agent.chmod(agent.stat().st_mode | stat.S_IEXEC)
    runner = PiRunner(root, pi_bin=str(agent))
    timeout = 0.3 if kind.startswith("timeout") else 60

    async def go():
        task = asyncio.ensure_future(
            runner.run("prompt", root / "output.jsonl", timeout_s=timeout))
        if kind == "operator_interrupt":
            transcript = root / "output.jsonl"
            while not (transcript.exists()
                       and "message_end" in transcript.read_text()):
                await asyncio.sleep(0.02)
            assert runner.interrupt() is True
        return await task

    grace = runner_mod.SHUTDOWN_GRACE_S
    runner_mod.SHUTDOWN_GRACE_S = 0.2
    try:
        with sigint_default_for_children():
            return asyncio.run(asyncio.wait_for(go(), 30))
    finally:
        runner_mod.SHUTDOWN_GRACE_S = grace
        shutil.rmtree(root, ignore_errors=True)


def _replay(kind: str) -> IterationResult:
    return dataclasses.replace(_captured(kind), usage=dict(_captured(kind).usage))


def _replaying(kind):
    _captured(kind)  # captured here, outside the engine's event loop

    def hat(sup, prompt=""):
        return _replay(kind)
    return hat


_REAL_CUT_SHORT = [
    ("timeout_obeys_sigint", "judge_timed_out"),
    ("timeout_ignores_sigint", "judge_timed_out"),
    ("sigsegv", "judge_crashed"),
    ("sigkill", "judge_crashed"),
]


def test_the_real_runner_marks_engine_timeouts_and_signal_deaths_interrupted():
    expected = {"timeout_obeys_sigint": -signal.SIGINT,
                "timeout_ignores_sigint": -signal.SIGKILL,
                "sigsegv": -signal.SIGSEGV, "sigkill": -signal.SIGKILL,
                "operator_interrupt": -signal.SIGINT}
    for kind, code in expected.items():
        r = _captured(kind)
        assert r.exit_code == code, kind
        assert r.interrupted is True, kind
        assert r.final_text.strip() == "hi", kind
        assert r.timed_out is kind.startswith("timeout"), kind


@pytest.mark.parametrize("judge", ["review", "verdict_gate"])
@pytest.mark.parametrize("kind,reason", _REAL_CUT_SHORT,
                         ids=[k for k, _ in _REAL_CUT_SHORT])
def test_a_real_runner_timeout_or_signal_death_of_a_phase_judge_is_a_no_verdict(
        tmp_path, judge, kind, reason):
    calls: list[str] = []
    sup = _sup(tmp_path, judge, hat=_counting(_replaying(kind), calls),
               infra_outage_budget_s=0.0, iterations=50, judge_retry_max=2)
    parked = []
    real = sup._park_judge_no_verdict

    async def spy(hat_name, approach, why):
        parked.append((hat_name, why))
        return await real(hat_name, approach, why)

    sup._park_judge_no_verdict = spy
    guards = _guards(sup)
    assert asyncio.run(asyncio.wait_for(
        sup._run_phase_review("one"), 60)) == ("tail",)
    assert len(calls) == 3
    assert _review_interrupted(guards) == []
    retries = _events(sup, f"{judge}.retry")
    assert [e["reason"] for e in retries] == [reason, reason]
    assert parked == [(judge, reason)]
    (fault,) = _events(sup, f"{judge}.no_verdict")
    assert fault["reason"] == reason
    assert (sup.run.read_status()["gateRejectionEnding"]
            == state.GATE_REJECTION_ENDING_NO_VERDICT)
    assert _gate(sup) == "closed"
    _no_cure_was_taken(sup)
    assert _events(sup, "phase_gate.passed") == []
    assert sup.run.read_status().get("inputQuestion") is None
    assert sup._finish_tail_unverified() == "failed"
    assert sup.run.read_status()["verdict"] == "unreviewed"


@pytest.mark.parametrize("judge", ["review", "verdict_gate"])
def test_the_startup_watchdog_and_exit_130_are_read_as_timeout_and_crash(tmp_path, judge):
    sup = _sup(tmp_path, judge)
    sup._phase_review_scope = {"planPhase": "one", "round": 1}
    watchdog = IterationResult(exit_code=-signal.SIGINT, interrupted=True,
                               no_traffic_timeout=True)
    assert sup._phase_judge_cut_short(watchdog) == "judge_timed_out"
    # a shell-style SIGINT exit the operator did not send
    assert sup._phase_judge_cut_short(
        IterationResult(exit_code=130, interrupted=True)) == "judge_crashed"
    for kind, reason in _REAL_CUT_SHORT:
        assert sup._phase_judge_cut_short(_replay(kind)) == reason, kind


@pytest.mark.parametrize("judge", ["review", "verdict_gate"])
def test_an_operator_interrupt_or_an_abort_is_never_a_cut_short_phase_judge(tmp_path, judge):
    sup = _sup(tmp_path, judge)
    sup._phase_review_scope = {"planPhase": "one", "round": 1}
    real = _replay("operator_interrupt")
    # the runner gives no way to tell it from a crash; only the operator's record can
    assert sup._phase_judge_cut_short(real) == "judge_crashed"
    _operator_interrupts(sup, real)
    for kind in ("operator_interrupt", "timeout_obeys_sigint", "sigsegv"):
        assert sup._phase_judge_cut_short(_replay(kind)) is None, kind
    sup._operator_interrupted = False
    sup._abort_reason = "operator abort"
    for kind in ("timeout_ignores_sigint", "sigsegv"):
        assert sup._phase_judge_cut_short(_replay(kind)) is None, kind


def test_an_operator_interrupt_shields_only_its_own_attempt_and_later_timeouts_park(tmp_path):
    calls = []

    def interrupted_then_timing_out(sup, prompt=""):
        calls.append(1)
        if len(calls) == 1:
            return _operator_interrupts(sup, _replay("operator_interrupt"))
        return _replay("timeout_ignores_sigint")

    _captured("operator_interrupt")
    _captured("timeout_ignores_sigint")
    sup = _sup(tmp_path, "review", hat=interrupted_then_timing_out,
               infra_outage_budget_s=0.0, iterations=50, judge_retry_max=2)
    guards = _guards(sup)
    assert asyncio.run(asyncio.wait_for(
        sup._run_phase_review("one"), 60)) == ("tail",)
    assert len(_review_interrupted(guards)) == 1
    # one review-interrupted re-ask for the operator, then the first timeout
    # and its two bounded retries
    assert len(calls) == 1 + 3
    assert [e["reason"] for e in _events(sup, "review.retry")] == [
        "judge_timed_out"] * 2
    (fault,) = _events(sup, "review.no_verdict")
    assert fault["reason"] == "judge_timed_out"
    assert _gate(sup) == "closed"
    _no_cure_was_taken(sup)


def test_a_real_runner_timeout_of_the_final_review_keeps_the_review_interrupted_loop(tmp_path):
    calls = []

    def timed_out_then_verified(sup, prompt=""):
        calls.append(1)
        if len(calls) == 1:
            return _replay("timeout_ignores_sigint")
        return _verified(sup, prompt)

    _captured("timeout_ignores_sigint")
    sup = _sup(tmp_path, "review", hat=timed_out_then_verified,
               infra_outage_budget_s=0.0)
    sup._phase_review_scope = None
    guards = _guards(sup)
    result = asyncio.run(sup._run_review_iteration(1))
    assert result is not None and len(calls) == 2
    assert len(_review_interrupted(guards)) == 1
    assert sup._review_no_verdict is None
    assert _events(sup, "review.retry") == []
    assert sup._phase_judge_cut_short(_replay("sigsegv")) is None


# -- C.3 a completed verdict is persisted before any later judge can lose it ------------

REVIEW_RECORDS = ["artifacts/phase-reviews/one-r1-review.json",
                  "artifacts/phase-reviews/one-r1-review.md"]


def _records(sup, pid="one"):
    return list(state.phase_entry(sup.run.read_tasks(), pid)["records"])


def _interrupted_peer_sup(tmp_path, peer_hat, **cfg):
    sup = _sup(tmp_path, "verdict_gate", hat=peer_hat,
               infra_outage_budget_s=0.0, contract_retry_max=0, **cfg)
    return sup


def test_a_peer_with_no_verdict_after_a_passing_review_keeps_the_review_records(tmp_path):
    sup = _interrupted_peer_sup(tmp_path, _truncated)
    assert asyncio.run(sup._run_phase_review("one")) == ("tail",)
    assert _gate(sup) == "closed"
    assert _records(sup) == REVIEW_RECORDS
    for rel in REVIEW_RECORDS:
        assert (sup.run.root / rel).exists()
    assert not (sup.run.artifacts_dir / "phase-reviews"
                / "one-r1-verdict_gate.json").exists()


def test_a_peer_stage_that_finds_the_budget_spent_keeps_the_review_records(tmp_path):
    def spend_the_budget_in_the_review(sup, prompt=""):
        sup.cfg.iterations = sup.iterations_used
        return _verified(sup)

    sup = _seeded_sup(
        tmp_path, {"review": [spend_the_budget_in_the_review],
                   "verdict_gate": [_verified]}, peer_verdict_gate=True)
    assert asyncio.run(sup._run_phase_review("one")) == ("tail",)
    assert _records(sup) == REVIEW_RECORDS


def test_a_peer_dispatch_that_raises_keeps_the_review_records(tmp_path):
    sup = _sup(tmp_path, "verdict_gate")

    async def boom():
        raise RuntimeError("the peer iteration died")

    sup._dispatch_verdict_gate = boom
    try:
        asyncio.run(sup._run_phase_review("one"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("the peer's crash must propagate")
    assert _records(sup) == REVIEW_RECORDS


def test_a_re_dispatch_after_an_interrupted_peer_keeps_every_earlier_record_path(tmp_path):
    interrupted = [True]
    sup = _sup(tmp_path, "verdict_gate",
               hat=lambda s, prompt="": (
                   _truncated if interrupted[0] else _verified)(s, prompt),
               infra_outage_budget_s=0.0, contract_retry_max=0)
    assert asyncio.run(sup._run_phase_review("one")) == ("tail",)
    first = _records(sup)
    assert first == REVIEW_RECORDS
    before = {rel: (sup.run.root / rel).read_bytes() for rel in first}
    interrupted[0] = False
    assert asyncio.run(sup._run_phase_review("one")) is None
    after = _records(sup)
    # the earlier verdict's paths stay, in order, and the re-dispatch's own
    # verdicts get distinct ones (a second round, never an overwrite)
    assert after[:2] == REVIEW_RECORDS
    assert after[2:] == [
        "artifacts/phase-reviews/one-r2-review.json",
        "artifacts/phase-reviews/one-r2-review.md",
        "artifacts/phase-reviews/one-r2-verdict_gate.json",
        "artifacts/phase-reviews/one-r2-verdict_gate.md"]
    assert len(set(after)) == len(after)
    for rel, blob in before.items():
        assert (sup.run.root / rel).read_bytes() == blob
    assert _gate(sup) == "passed"


def test_a_review_with_no_verdict_keeps_the_earlier_rounds_records(tmp_path):
    truncate = [False]
    sup = _sup(tmp_path, "review",
               hat=lambda s, prompt="": (
                   _truncated(s, prompt) if truncate[0] else _rejecting(s)),
               infra_outage_budget_s=0.0, contract_retry_max=0)
    # round 1: a rejection whose cure the worker would take
    assert asyncio.run(sup._run_phase_review("one")) is None
    recorded = _records(sup)
    assert recorded == REVIEW_RECORDS
    truncate[0] = True
    # round 2: the review gives no verdict -- round 1's records stay
    assert asyncio.run(sup._run_phase_review("one")) == ("tail",)
    assert _records(sup) == recorded


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
