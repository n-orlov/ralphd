"""#300 (PRD v0.32.3 requirement B, the review hat's half): a review hat whose
reply was cut off by the provider (`stopReason: length`, no text) is
re-dispatched as a fresh, uncharged iteration (`review.retry`); when every
retry is also cut off the fault is an infra fault under the outage budget (v0.36
F.3: never a park; `failed` with verdict `unreviewed` once it is spent),
archiving nothing and recording no rejection. A real gate
rejection (and a malformed non-empty gate reply) is unchanged.

Black-box through the scripted stand-in for `pi` (`tests/conftest.py`'s
`ScriptedRunner`), in the style of `tests/test_judge_no_verdict_park.py`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

from ralphd.engine import state
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor

TASK_1 = "001"
NO_VERDICT_WORDING = "no verdict: output truncated"
BUDGET_WORDING = "ran out of output budget"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _plan(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1,
        "tasks": [{"id": TASK_1, "title": "t", "successCriteria": "c"}],
    })
    return _ok(json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _worker(sup, prompt=""):
    return _ok(json.dumps({"verdict": "complete", "task_id": TASK_1}))


def _review_verifies(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


def _review_truncated(sup, prompt=""):
    r = IterationResult(exit_code=0)
    r.final_text = ""
    r.output_truncated = True
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 4096, "totalTokens": 4106}
    return r


def _review_empty(sup, prompt=""):
    return _ok("")


def _review_blank(sup, prompt=""):
    return _ok("  \n\t\n")


def _review_prose(sup, prompt=""):
    return _ok("I looked at it and it seems fine to me")


def _gate_approves(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


def _gate_rejects(sup, prompt=""):
    return _ok(json.dumps({"verdict": "rejected", "reason": "does not meet it"}))


def _gate_prose(sup, prompt=""):
    return _ok("I looked at it and it seems fine to me")


_supervisor = scripted_supervisor(run_id="review-retry", iterations=20,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _build(root: Path, review_steps: list, *, gate_steps=None, **cfg):
    RunDir(root=root).update_status(state="starting")
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    scripts = {"planning": [_plan, _plan], "worker": [_worker, _worker],
               "review": review_steps}
    if gate_steps is not None:
        scripts["verdict_gate"] = gate_steps
        cfg.setdefault("peer_verdict_gate", True)
    return _supervisor(root, scripts, **cfg)


def _run(root: Path, review_steps: list, **kw):
    sup = _build(root, review_steps, **kw)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 30.0))
    return sup, final


async def _drive(sup):
    """Run the job to its end. The engine never parks on a judge fault
    (v0.36 F.3), so a `paused`/`awaiting_input` state seen on the way is
    returned for the test to assert on (it must be empty)."""
    task = asyncio.ensure_future(sup._run_job_core())
    seen: list[dict] = []
    while not task.done():
        status = sup.run.read_status()
        if status.get("state") in ("paused", "awaiting_input"):
            seen.append(status)
        await asyncio.sleep(0.005)
    return await asyncio.wait_for(task, 30.0), seen


def _run_faulted(root: Path, review_steps: list, **kw):
    sup = _build(root, review_steps, **kw)
    final, parks = asyncio.run(_drive(sup))
    return sup, final, parks


def _events(sup, *types) -> list[dict]:
    return state.read_events(sup.run.root, types=list(types))


def _recorded_verdicts(sup) -> list[tuple[str, str]]:
    return [(e["channel"], e["verdict"])
            for e in _events(sup, "approach.verdict_recorded")]


def _review_prompts(sup) -> list[str]:
    agent = sup.runner
    return [p for ph, p in zip(agent.phases_seen, agent.prompts)
            if ph == "review"]


# ---------------------------------------------------------------------------
# (1) a truncated review reply is retried, uncharged
# ---------------------------------------------------------------------------


def test_a_truncated_review_reply_is_retried_and_the_run_ends_verified(tmp_path):
    sup, final = _run(tmp_path / "run", [_review_truncated, _review_verifies])
    status = sup.run.read_status()
    assert final == "succeeded", status
    assert status["verdict"] == "verified", status
    assert sup._approach == 1
    assert sup.runner.phases_seen.count("review") == 2
    events = _events(sup, "review.retry")
    assert len(events) == 1, events
    ev = events[0]
    assert ev["hat"] == "review"
    assert ev["attempt"] == 1
    assert ev["cap"] == 2
    assert ev["reason"] == "output_truncated"
    assert _events(sup, "verdict_gate.retry") == []
    assert _recorded_verdicts(sup) == [("review", "verified")]
    assert not (sup.run.root / "approaches").exists() or not any(
        (sup.run.root / "approaches").iterdir())


def test_the_review_retry_is_not_charged_against_the_budget(tmp_path):
    base, base_final = _run(tmp_path / "base", [_review_verifies])
    trunc, trunc_final = _run(tmp_path / "trunc",
                              [_review_truncated, _review_verifies])
    assert base_final == trunc_final == "succeeded"
    assert (trunc.run.read_status()["iterationsUsed"]
            == base.run.read_status()["iterationsUsed"])
    assert trunc.iterations_used_charged == base.iterations_used_charged


def test_the_review_retry_prompt_carries_the_bounded_budget_wording(tmp_path):
    sup, final = _run(tmp_path / "run", [_review_truncated, _review_verifies])
    assert final == "succeeded"
    first, retry = _review_prompts(sup)
    assert BUDGET_WORDING not in first
    assert BUDGET_WORDING in retry


# ---------------------------------------------------------------------------
# (2) every attempt truncated: park, never reject
# ---------------------------------------------------------------------------


def test_a_review_that_truncates_every_attempt_ends_failed_unreviewed_never_parks(
        tmp_path):
    sup, final, parks = _run_faulted(
        tmp_path / "run", [_review_truncated], infra_outage_budget_s=0.0)
    assert parks == [], "v0.36 F.3: a judge fault is an infra fault, not a park"
    status = sup.run.read_status()
    assert final == "failed" and status["verdict"] == "unreviewed", status
    assert NO_VERDICT_WORDING in (status.get("reason") or "")
    assert "review" in (status.get("reason") or "")
    assert status["gateRejectionEnding"] == state.GATE_REJECTION_ENDING_NO_VERDICT
    # the default cap of retries ran first, all uncharged
    assert len(_events(sup, "review.retry")) == 2
    assert sup.runner.phases_seen.count("review") == 3
    # not a rejection: nothing archived, no composite PRD, nothing recorded
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    assert not list(sup.run.root.glob("**/composite*"))
    verdicts = _recorded_verdicts(sup)
    assert not any(v == "rejected" for _c, v in verdicts), verdicts
    (ev,) = _events(sup, "review.no_verdict")
    assert ev["parked"] is False and ev["outcome"] == "spent"


def test_inside_the_outage_budget_review_is_asked_again_and_a_verified_reply_ends_the_run(
        tmp_path):
    sup, final, parks = _run_faulted(
        tmp_path / "run",
        [_review_truncated, _review_truncated, _review_truncated,
         _review_verifies])
    assert parks == []
    status = sup.run.read_status()
    assert final == "succeeded", status
    assert status["verdict"] == "verified", status
    assert sup.runner.phases_seen.count("review") == 4
    assert status.get("gateRejectionEnding") is None
    assert _recorded_verdicts(sup) == [("review", "verified")]


def test_review_judge_retry_max_zero_waits_without_a_retry(tmp_path):
    sup, final, parks = _run_faulted(
        tmp_path / "run", [_review_truncated, _review_verifies],
        judge_retry_max=0)
    assert parks == []
    assert _events(sup, "review.retry") == []
    assert final == "succeeded"


def test_a_spent_budget_at_the_review_fault_ends_unreviewed_not_rejected(tmp_path):
    # plan, worker and the three review attempts (the retries are refunded,
    # so only one is charged) leave nothing to ask again at iterations=3.
    sup, final, parks = _run_faulted(
        tmp_path / "run", [_review_truncated], iterations=3)
    status = sup.run.read_status()
    assert parks == [], "the run waited with nothing left to ask again"
    assert final == "failed", status
    assert status["verdict"] == "unreviewed", status
    assert status["gateRejectionEnding"] == state.GATE_REJECTION_ENDING_NO_VERDICT
    assert "no verdict" in (status.get("reason") or "")
    assert not any(v == "rejected" for _c, v in _recorded_verdicts(sup))
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    assert _events(sup, "review.no_verdict")[0]["parked"] is False


# ---------------------------------------------------------------------------
# (2b) a review ladder exhausted on empty replies is retried, then parked
# ---------------------------------------------------------------------------

EMPTY_WORDING = "no verdict: empty reply"
# one review dispatch whose reply is empty costs the first reply plus
# `contract_retry_max` (2) empty contract re-asks, all scripted on `review`
LADDER = 3


def test_an_empty_review_reply_is_retried_uncharged_with_reason_empty_reply(
        tmp_path):
    sup, final = _run(tmp_path / "run", [_review_empty] * LADDER + [_review_verifies])
    status = sup.run.read_status()
    assert final == "succeeded", status
    assert status["verdict"] == "verified", status
    events = _events(sup, "review.retry")
    assert len(events) == 1, events
    assert events[0]["hat"] == "review"
    assert events[0]["reason"] == "empty_reply"
    assert events[0]["attempt"] == 1 and events[0]["cap"] == 2
    assert _events(sup, "review.no_verdict") == []
    assert _recorded_verdicts(sup) == [("review", "verified")]
    base, _ = _run(tmp_path / "base", [_review_verifies])
    assert sup.iterations_used_charged == base.iterations_used_charged
    first, retry = _review_prompts(sup)[0], _review_prompts(sup)[-1]
    assert BUDGET_WORDING not in first and BUDGET_WORDING in retry


def test_a_review_that_replies_empty_every_attempt_faults_not_holds(tmp_path):
    sup, final, parks = _run_faulted(
        tmp_path / "run", [_review_empty], infra_outage_budget_s=0.0)
    assert parks == []
    status = sup.run.read_status()
    assert final == "failed" and status["verdict"] == "unreviewed", status
    assert EMPTY_WORDING in (status.get("reason") or "")
    assert status["gateRejectionEnding"] == state.GATE_REJECTION_ENDING_NO_VERDICT
    assert len(_events(sup, "review.retry")) == 2
    assert {e["reason"] for e in _events(sup, "review.retry")} == {"empty_reply"}
    (ev,) = _events(sup, "review.no_verdict")
    assert ev["parked"] is False and ev["reason"] == "empty_reply"
    assert not _events(sup, "escalation.review_contract_unrepaired")
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    assert not any(v == "rejected" for _c, v in _recorded_verdicts(sup))


def test_review_is_asked_again_after_an_empty_reply_fault(tmp_path):
    sup, final, parks = _run_faulted(
        tmp_path / "run",
        [_review_empty] * (3 * LADDER) + [_review_verifies])
    assert parks == []
    assert final == "succeeded", sup.run.read_status()
    assert sup.run.read_status().get("gateRejectionEnding") is None
    assert _recorded_verdicts(sup) == [("review", "verified")]


def test_a_whitespace_only_review_reply_counts_as_empty(tmp_path):
    sup, final = _run(tmp_path / "run", [_review_blank] * LADDER + [_review_verifies])
    assert final == "succeeded", sup.run.read_status()
    assert [e["reason"] for e in _events(sup, "review.retry")] == ["empty_reply"]


def test_review_judge_retry_max_zero_waits_on_an_empty_reply_without_a_retry(
        tmp_path):
    sup, final, parks = _run_faulted(
        tmp_path / "run", [_review_empty] * LADDER + [_review_verifies],
        judge_retry_max=0)
    assert parks == []
    (ev,) = _events(sup, "review.no_verdict")
    assert ev["reason"] == "empty_reply" and ev["outcome"] == "retry"
    assert _events(sup, "review.retry") == []
    assert final == "succeeded"


def test_a_spent_budget_after_empty_review_replies_ends_unreviewed(tmp_path):
    sup, final, parks = _run_faulted(
        tmp_path / "run", [_review_empty], iterations=3)
    status = sup.run.read_status()
    assert parks == [], "the run waited with nothing left to ask again"
    assert final == "failed", status
    assert status["verdict"] == "unreviewed", status
    assert EMPTY_WORDING in (status.get("reason") or "")
    assert _events(sup, "review.no_verdict")[0]["parked"] is False


def test_a_non_empty_malformed_review_reply_is_an_infra_fault_never_a_hold(
        tmp_path):
    """v0.36 F.3: the contract-ladder exhaustion (a reply that is not empty
    but unreadable) is routed like the no-verdict faults: no pause, `failed`
    with verdict `unreviewed` once the outage budget is spent."""
    sup, final, parks = _run_faulted(
        tmp_path / "run", [_review_prose], infra_outage_budget_s=0.0)
    assert parks == []
    status = sup.run.read_status()
    assert final == "failed" and status["verdict"] == "unreviewed", status
    assert sup._judge_fault_tail == "review_contract_unrepaired"
    assert _events(sup, "review.retry") == []
    assert _events(sup, "review.no_verdict") == []
    (cfault,) = _events(sup, "review.contract_fault")
    assert cfault["guard"] == "review_contract_unrepaired"


# ---------------------------------------------------------------------------
# (3) a real gate rejection is unchanged
# ---------------------------------------------------------------------------


def test_a_gate_rejection_with_a_reason_still_replans_with_no_retry(tmp_path):
    sup, final = _run(
        tmp_path / "run", [_review_verifies, _review_verifies],
        gate_steps=[_gate_rejects, _gate_approves], max_approaches=2)
    assert final == "succeeded", sup.run.read_status()
    assert sup.runner.phases_seen == [
        "planning", "worker", "review", "verdict_gate",
        "planning", "worker", "review", "verdict_gate"]
    assert _events(sup, "verdict_gate.retry") == []
    assert _events(sup, "review.retry") == []
    assert (sup.run.root / "approaches" / "01").is_dir()
    assert ("verdictGate", "rejected") in _recorded_verdicts(sup)


def test_a_gate_rejection_with_no_approach_left_holds_exhausted(tmp_path):
    """v0.36 requirement B: with the park disabled the gate rejection is an
    unanswered park, and with no approach left it holds `exhausted`
    (approaches) -- never `failed`/`unverified` on the spot."""
    sup, final = _run(tmp_path / "run", [_review_verifies],
                      gate_steps=[_gate_rejects])
    status = sup.run.read_status()
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "approaches", status
    assert status["verdict"] is None, status
    assert _events(sup, "verdict_gate.retry") == []
    assert status["gateRejectionEnding"] != state.GATE_REJECTION_ENDING_NO_VERDICT


def test_a_gate_rejection_with_curable_findings_still_takes_the_cure_edge(
        tmp_path):
    from ralphd.engine import findings
    from ralphd.engine.config import JobConfig

    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="review-retry-cure", iterations=5,
                    max_approaches=1, vigilant=False, peer_verdict_gate=True,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    findings.write_findings_json(run.verdict_gate_findings_json_file, [
        {"severity": "blocking", "curable": True, "clause": "clause A",
         "evidence": "a gap review missed",
         "cured_when": "a cure task adds the missing behaviour"}])

    async def gate_rejects(self, phase, extra="", *a, **kw):
        return _ok(json.dumps({"verdict": "rejected",
                               "reason": "the blocking finding is curable"}))

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = gate_rejects
    try:
        outcome = asyncio.run(asyncio.wait_for(sup._step_verdict_gate(), 30.0))
    finally:
        LoopSupervisor.run_iteration = orig
    assert outcome == ("advance", "cure",
                       "verdict_gate_rejected_curable_pass_available")
    assert not (root / "approaches" / "01").exists()
    assert run.read_status().get("gateRejectionEnding") == "rejected_cure_taken"
    assert _events(sup, "verdict_gate.retry") == []


def test_a_malformed_non_empty_gate_reply_takes_the_ladder_then_the_safe_default(
        tmp_path):
    sup, final = _run(tmp_path / "run", [_review_verifies],
                      gate_steps=[_gate_prose])
    status = sup.run.read_status()
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "approaches", status
    assert status["verdict"] is None, status
    assert _events(sup, "verdict_gate.retry") == []
    assert _events(sup, "verdict_gate.no_verdict") == []
    assert ("verdictGate", "rejected") in _recorded_verdicts(sup)


# ---------------------------------------------------------------------------
# (4) one retry rule
# ---------------------------------------------------------------------------


def test_both_judging_hats_share_one_retry_helper():
    helper = LoopSupervisor._judge_reply_with_retry
    gate_src = inspect.getsource(LoopSupervisor._verdict_gate_verdict)
    review_src = inspect.getsource(LoopSupervisor._run_review_iteration)
    assert "_judge_reply_with_retry" in gate_src
    assert "_judge_reply_with_retry" in review_src
    # the rule itself (refund + event) is written once, in the helper
    helper_src = inspect.getsource(helper)
    assert "_refund_iteration" in helper_src and ".retry" in helper_src
    for src in (gate_src, review_src):
        assert "_refund_iteration" not in src
        assert "cfg.judge_retry_max" not in src
