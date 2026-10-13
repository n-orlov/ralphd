"""#300 (PRD v0.32.3 requirement B, the review hat's half): a review hat whose
reply was cut off by the provider (`stopReason: length`, no text) is
re-dispatched as a fresh, uncharged iteration (`review.retry`); when every
retry is also cut off the run parks `operator_input_needed` with `no verdict:
output truncated`, archiving nothing and recording no rejection. A real gate
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


async def _drive(sup, *, unpause: bool):
    """Run the job; when it parks, snapshot status.json, then unpause or
    abort (as `ralphctl unpause` / `ralphctl abort` would)."""
    task = asyncio.ensure_future(sup._run_job_core())
    parked = None
    for _ in range(1500):
        if task.done():
            break
        if sup.run.read_status().get("state") == "paused":
            parked = json.loads((sup.run.root / "status.json").read_text())
            break
        await asyncio.sleep(0.01)
    if parked is not None:
        await asyncio.sleep(0.05)
        assert not task.done(), "a no-verdict review must wait for the operator"
        if not unpause:
            sup.abort("test done")
        sup.resume()
    final = await asyncio.wait_for(task, 30.0)
    return final, parked


def _run_parked(root: Path, review_steps: list, *, unpause: bool, **kw):
    sup = _build(root, review_steps, **kw)
    final, parked = asyncio.run(_drive(sup, unpause=unpause))
    return sup, final, parked


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


def test_a_review_that_truncates_every_attempt_parks_the_run(tmp_path):
    sup, final, parked = _run_parked(
        tmp_path / "run", [_review_truncated], unpause=False)
    assert parked is not None, "the run never parked"
    assert parked["pauseReason"] == "operator_input_needed"
    detail = parked["pauseDetail"]
    assert detail["reason"] == "judge_no_verdict"
    assert detail["hat"] == "review"
    assert "review" in detail["message"]
    assert NO_VERDICT_WORDING in detail["message"]
    assert parked["gateRejectionEnding"] == state.GATE_REJECTION_ENDING_NO_VERDICT
    # the default cap of retries ran first, all uncharged
    assert len(_events(sup, "review.retry")) == 2
    assert sup.runner.phases_seen.count("review") == 3
    # not a rejection: nothing archived, no composite PRD, nothing recorded
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    assert not list(sup.run.root.glob("**/composite*"))
    verdicts = _recorded_verdicts(sup)
    assert not any(v == "rejected" for _c, v in verdicts), verdicts
    assert _events(sup, "review.no_verdict")[0]["parked"] is True
    assert final == "aborted"


def test_unpause_redispatches_review_and_a_verified_reply_ends_the_run(tmp_path):
    sup, final, parked = _run_parked(
        tmp_path / "run",
        [_review_truncated, _review_truncated, _review_truncated,
         _review_verifies], unpause=True)
    assert parked is not None
    status = sup.run.read_status()
    assert final == "succeeded", status
    assert status["verdict"] == "verified", status
    assert sup.runner.phases_seen.count("review") == 4
    assert status.get("gateRejectionEnding") is None
    assert _recorded_verdicts(sup) == [("review", "verified")]


def test_review_judge_retry_max_zero_parks_without_a_retry(tmp_path):
    sup, final, parked = _run_parked(
        tmp_path / "run", [_review_truncated, _review_verifies],
        unpause=True, judge_retry_max=0)
    assert parked is not None
    assert parked["pauseDetail"]["reason"] == "judge_no_verdict"
    assert _events(sup, "review.retry") == []
    assert final == "succeeded"


def test_a_spent_budget_at_the_review_park_ends_unreviewed_not_rejected(tmp_path):
    # plan, worker and the three review attempts (the retries are refunded,
    # so only one is charged) leave nothing to resume into at iterations=3.
    sup, final, parked = _run_parked(
        tmp_path / "run", [_review_truncated], unpause=True, iterations=3)
    status = sup.run.read_status()
    assert parked is None, "the run parked with nothing left to resume into"
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


def test_a_review_that_replies_empty_every_attempt_parks_not_holds(tmp_path):
    sup, final, parked = _run_parked(
        tmp_path / "run", [_review_empty], unpause=False)
    assert parked is not None, "the run never parked"
    assert parked["pauseReason"] == "operator_input_needed", parked
    detail = parked["pauseDetail"]
    assert detail["reason"] == "judge_no_verdict"
    assert detail["hat"] == "review"
    assert detail["cause"] == "empty_reply"
    assert EMPTY_WORDING in detail["message"]
    assert parked["gateRejectionEnding"] == state.GATE_REJECTION_ENDING_NO_VERDICT
    assert len(_events(sup, "review.retry")) == 2
    assert {e["reason"] for e in _events(sup, "review.retry")} == {"empty_reply"}
    assert _events(sup, "review.no_verdict")[0]["parked"] is True
    assert not _events(sup, "escalation.review_contract_unrepaired")
    approaches = sup.run.root / "approaches"
    assert not approaches.exists() or not any(approaches.iterdir())
    assert not any(v == "rejected" for _c, v in _recorded_verdicts(sup))
    assert final == "aborted"


def test_unpause_redispatches_review_after_an_empty_reply_park(tmp_path):
    sup, final, parked = _run_parked(
        tmp_path / "run",
        [_review_empty] * (3 * LADDER) + [_review_verifies],
        unpause=True)
    assert parked is not None
    assert final == "succeeded", sup.run.read_status()
    assert sup.run.read_status().get("gateRejectionEnding") is None
    assert _recorded_verdicts(sup) == [("review", "verified")]


def test_a_whitespace_only_review_reply_counts_as_empty(tmp_path):
    sup, final = _run(tmp_path / "run", [_review_blank] * LADDER + [_review_verifies])
    assert final == "succeeded", sup.run.read_status()
    assert [e["reason"] for e in _events(sup, "review.retry")] == ["empty_reply"]


def test_review_judge_retry_max_zero_parks_an_empty_reply_without_a_retry(
        tmp_path):
    sup, final, parked = _run_parked(
        tmp_path / "run", [_review_empty] * LADDER + [_review_verifies],
        unpause=True, judge_retry_max=0)
    assert parked is not None
    assert parked["pauseDetail"]["cause"] == "empty_reply"
    assert _events(sup, "review.retry") == []
    assert final == "succeeded"


def test_a_spent_budget_after_empty_review_replies_ends_unreviewed(tmp_path):
    sup, final, parked = _run_parked(
        tmp_path / "run", [_review_empty], unpause=True, iterations=3)
    status = sup.run.read_status()
    assert parked is None, "the run parked with nothing left to resume into"
    assert final == "failed", status
    assert status["verdict"] == "unreviewed", status
    assert EMPTY_WORDING in (status.get("reason") or "")
    assert _events(sup, "review.no_verdict")[0]["parked"] is False


def test_a_non_empty_malformed_review_reply_keeps_the_contract_hold(tmp_path):
    sup, final, parked = _run_parked(
        tmp_path / "run", [_review_prose], unpause=False)
    assert parked is not None
    assert parked["pauseReason"] == "review_contract_unrepaired", parked
    assert _events(sup, "review.retry") == []
    assert _events(sup, "review.no_verdict") == []


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
