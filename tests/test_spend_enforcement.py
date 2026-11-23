"""v0.39 (#321): spend caps are enforced before every dispatch.

The engine is driven with a scripted runner (fake hats) whose iterations
return injected costs, so no test spends a real dollar or waits out a clock.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import spend_caps
from ralphd.engine.pricing import resolve_pricing
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor


def _usage_result(text: str, cost: float = 0.0, **usage) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15,
               "costUSD": cost, **usage}
    return r


def _plan(cost: float = 0.0):
    def step(sup, prompt=""):
        atomic_write_json(sup.run.root / "plan-proposal.json", {
            "version": 1, "tasks": [{"id": "001", "title": "task 001",
                                     "successCriteria": "task 001 works"}]})
        return _usage_result("planned\n" + json.dumps(
            {"proposal_path": "plan-proposal.json", "summary": "planned"}), cost)
    return step


def _work(cost: float):
    """A worker that never finishes: prose only, at `cost` per iteration."""
    return lambda sup, prompt="": _usage_result("still working on it", cost)


def _caps(**levels) -> dict:
    return {level: {hat: {"usd": usd, "source": "flag"}
                    for hat, usd in entries.items()}
            for level, entries in levels.items()}


_supervisor = scripted_supervisor(
    run_id="spend-enforce", iterations=60, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0)


def _run(tmp_path, handlers, **cfg):
    root = tmp_path / "run"
    sup = _supervisor(root, handlers, **cfg)
    RunDir(root=root).update_status(state="starting")
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))
    return sup, final, json.loads((root / "status.json").read_text())


def _events(sup, type_: str) -> list[dict]:
    path = Path(sup.run.root) / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in rows if e.get("type") == type_]


# -- overall caps end the run ------------------------------------------------

def test_a_spent_overall_hat_cap_ends_the_run_exhausted_and_the_hat_is_not_dispatched_again(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.1)], "worker": [_work(0.6)]},
        spend_caps=_caps(overall={"worker": 1.0}), max_approaches=3)
    assert final == "exhausted", status
    assert status["state"] == "exhausted"
    assert status["exhaustedBudget"] == "spend:worker"
    assert "spend:worker" in status["reason"]
    # two worker iterations took the worker to $1.20; the third was refused
    assert sup.runner.calls["worker"] == 2
    assert status["spend"]["overall"]["worker"] == 1.2
    [event] = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"], event["cap"], event["spend"],
            event["action"]) == ("worker", "overall", 1.0, 1.2, "exhausted")


def test_a_spent_overall_total_cap_ends_the_run_exhausted_with_spend_total(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.4)], "worker": [_work(0.4)]},
        spend_caps=_caps(overall={"total": 1.0}), max_approaches=3)
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "spend:total"
    # planning $0.4 + worker $0.4 + worker $0.4 = $1.20: nothing runs after it
    assert sup.runner.calls == {"planning": 1, "worker": 2}
    [event] = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"], event["cap"], event["spend"],
            event["action"]) == ("total", "overall", 1.0, 1.2, "exhausted")
    assert event["refusedHat"] == "worker"


def test_a_cap_on_another_hat_leaves_the_worker_running(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.1)], "worker": [_work(5.0)]},
        spend_caps=_caps(overall={"review": 1.0}), max_approaches=1)
    assert status["exhaustedBudget"] == "approaches"
    assert _events(sup, "spend.exhausted") == []


def test_an_overall_cap_is_not_a_per_approach_replan_even_on_a_non_last_approach(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.6)]},
        spend_caps=_caps(overall={"worker": 1.0}), max_approaches=2)
    assert status["exhaustedBudget"] == "spend:worker"
    assert not (Path(sup.run.root) / "approaches" / "01").exists()


# -- per-approach caps replan, or end the run on the last approach -------------

def test_a_spent_per_approach_cap_archives_the_approach_and_replans_with_fresh_counters(
        tmp_path):
    seen: dict = {}

    def second_plan(sup, prompt=""):
        # the planning dispatch of approach 2: its counters start at zero
        # while the overall ones carry on
        seen["spend"] = json.loads(json.dumps(sup.run.read_spend()))
        seen["approach"] = sup.run.read_status()["approach"]
        return _plan(0.0)(sup, prompt)

    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0), second_plan],
                   "worker": [_work(0.6)]},
        spend_caps=_caps(per_approach={"worker": 1.0}), max_approaches=2)

    assert seen["approach"] == 2
    assert seen["spend"]["overall"]["worker"] == 1.2
    assert seen["spend"]["byApproach"].get("2", {}) == {}
    assert (Path(sup.run.root) / "approaches" / "01").is_dir()
    events = _events(sup, "spend.exhausted")
    assert [(e["hat"], e["level"], e["cap"], e["spend"], e["action"])
            for e in events] == [("worker", "per_approach", 1.0, 1.2, "replan"),
                                 ("worker", "per_approach", 1.0, 1.2, "exhausted")]
    # approach 2 ran two worker iterations of its own, then it was the last
    assert sup.runner.calls == {"planning": 2, "worker": 4}
    assert status["spend"]["overall"]["worker"] == 2.4
    assert status["spend"]["byApproach"]["2"]["worker"] == 1.2


def test_a_spent_per_approach_cap_on_the_last_approach_ends_the_run_exhausted(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.0)], "worker": [_work(0.6)]},
        spend_caps=_caps(per_approach={"worker": 1.0}), max_approaches=1)
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "spend:worker:approach"
    assert sup.runner.calls["worker"] == 2
    [event] = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"], event["cap"], event["spend"],
            event["action"]) == ("worker", "per_approach", 1.0, 1.2, "exhausted")


def test_a_per_approach_total_cap_names_total_in_the_reason(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.5)], "worker": [_work(0.5)]},
        spend_caps=_caps(per_approach={"total": 1.0}), max_approaches=1)
    assert status["exhaustedBudget"] == "spend:total:approach"


# -- unpriced iterations -------------------------------------------------------

_PRICING = {"models": {"anthropic/claude-opus-5": {"input": 15.0, "output": 75.0},
                       "anthropic/claude-haiku-5": {"input": 1.0, "output": 5.0},
                       "openai/gpt-5": {"input": 40.0, "output": 400.0}}}
_UNPRICED = {"input": 1_000_000, "output": 1_000_000, "totalTokens": 2_000_000,
             "costPriced": False}


def test_an_unpriced_iteration_on_a_capped_hat_counts_at_the_highest_family_rate_and_warns(
        tmp_path):
    handlers = {"planning": [_plan(0.0)],
                "worker": [lambda sup, prompt="": _usage_result(
                    "working", 0.0, **_UNPRICED)]}
    sup, final, status = _run(
        tmp_path, handlers, pricing=_PRICING, models={"worker": "anthropic/claude-new-9"},
        spend_caps=_caps(overall={"worker": 100.0}), max_approaches=1)
    # 1M input at $15 + 1M output at $75: the dearest anthropic rate, never $0
    assert status["spend"]["overall"]["worker"] == 180.0
    assert status["exhaustedBudget"] == "spend:worker"
    events = _events(sup, "spend.unpriced")
    assert events and all(e["hat"] == "worker" for e in events)
    assert events[0]["model"] == "anthropic/claude-new-9"
    assert events[0]["rate"]["input"] == 15.0 and events[0]["rate"]["output"] == 75.0
    assert events[0]["counted"] == 90.0


def test_an_unpriced_iteration_on_an_uncapped_hat_counts_as_before_and_emits_nothing(
        tmp_path):
    handlers = {"planning": [_plan(0.0)],
                "worker": [lambda sup, prompt="": _usage_result(
                    "working", 0.0, **_UNPRICED)]}
    sup, final, status = _run(
        tmp_path, handlers, pricing=_PRICING, models={"worker": "anthropic/claude-new-9"},
        spend_caps=_caps(overall={"review": 100.0}), max_approaches=1)
    assert status["spend"]["overall"]["worker"] == 0.0
    assert _events(sup, "spend.unpriced") == []


def test_the_family_rate_is_the_dearest_rate_of_the_models_family_else_of_the_chain():
    chain = resolve_pricing(_PRICING, "none")
    assert chain.family_rate("anthropic/claude-new-9").output == 75.0
    assert chain.family_rate("openai/gpt-9").output == 400.0
    # no model of the family is known: the dearest rate the chain holds
    assert chain.family_rate("mystery/model").output == 400.0
    assert chain.family_rate(None).output == 400.0
    assert resolve_pricing({}, "none") is None


def test_counted_spend_never_reads_an_unpriced_iteration_as_zero():
    chain = resolve_pricing(_PRICING, "none")
    amount, unpriced = spend_caps.counted_spend(
        {"totalTokens": 1_000_000, "costPriced": False}, "anthropic/x", chain)
    assert amount == 75.0 and unpriced["rate"]["output"] == 75.0
    # a priced iteration counts exactly its own cost
    assert spend_caps.counted_spend({"costUSD": 0.5, "totalTokens": 9}, "x", chain) \
        == (0.5, None)
    # a route the operator declared free is priced at $0 by declaration
    assert spend_caps.counted_spend(
        {"costUSD": 0.0, "costFree": True, "totalTokens": 9}, "x", chain) \
        == (0.0, None)


# -- no caps, no change ---------------------------------------------------------

def test_a_run_with_no_caps_writes_no_spend_events(tmp_path):
    sup, final, status = _run(
        tmp_path, {"planning": [_plan(0.1)], "worker": [_work(5.0)]},
        max_approaches=1)
    assert status["exhaustedBudget"] == "approaches"
    for kind in ("spend.exhausted", "spend.unpriced", "spend.warning"):
        assert _events(sup, kind) == []


def test_a_per_approach_cap_on_a_non_last_approach_replans_from_every_hat_bearing_step(
        tmp_path):
    """B.2: "per-approach cap spent, not the last approach: the approach is
    archived and the run replans" -- from whichever step was about to
    dispatch. No step exempts a hat: every hat-bearing step state, in every
    variant it exists in, has a legal edge into `replan` (its own, or the
    engine-only one), and the refusal names `replan` whatever the step."""
    import pytest

    from ralphd.engine import transitions
    from ralphd.engine.loop import _SpendCapSpent

    variants = [{"vigilant": v, "peer_plan_review": p, "peer_verdict_gate": g}
                for v in (False, True) for p in (False, True)
                for g in (False, True)]
    for state in transitions.STEP_STATES:
        if transitions.STATE_HAT.get(state) in (None, "reflect"):
            continue
        reachable = [v for v in variants
                     if transitions.successors(state, **v)]
        assert reachable, state
        for v in reachable:
            assert transitions.is_legal(state, "replan", **v), (state, v)
            assert transitions.TransitionExecutor(**v).advance(
                state, "replan", "stagnation_exhausted") == "replan"
    # the engine-only edge is never offered to a hat or the router
    assert [e.target for e in transitions.successors("steer", False)] == ["worker"]

    sup = _supervisor(tmp_path / "run", {},
                      spend_caps=_caps(per_approach={"plan_review": 1.0}),
                      max_approaches=3)
    sup.run.update_status(approach=1)
    sup.run.record_spend(1, "plan_review", {"costUSD": 1.5})
    with pytest.raises(_SpendCapSpent) as raised:
        sup._enforce_spend_caps("plan_review")
    assert raised.value.action == "replan"
    # reflect is a dispatch like any other (cure-01-03): its refusal names
    # the same outcome rules, never a special "skipped" action. The end-to-end
    # lifecycle is tests/test_spend_reflection.py.
    sup.cfg.spend_caps = _caps(per_approach={"total": 1.0})
    with pytest.raises(_SpendCapSpent) as raised:
        sup._enforce_spend_caps("reflect")
    assert raised.value.action == "replan"
    assert [(e["action"], e["refusedHat"])
            for e in _events(sup, "spend.exhausted")] == [
        ("replan", "plan_review"), ("replan", "reflect")]


# -- B.2 from the steps with no replan edge of their own (cure-01-01) ----------

def _malformed(cost: float):
    """A hat reply that is not its contract's JSON, at `cost`."""
    return lambda sup, prompt="": _usage_result("not a payload at all", cost)


def _plan_noting(seen: list, cost: float = 0.0, steer: str | None = None):
    def step(sup, prompt=""):
        seen.append(sup.run.read_status()["approach"])
        if steer and len(seen) == 1:
            sup.run.add_steering(steer, "op1")
        return _plan(cost)(sup, prompt)
    return step


def _assert_replanned_on_spend(sup, seen, status, *, hat, level, refused, spend):
    assert list(dict.fromkeys(seen))[:2] == [1, 2], (seen, status)
    assert (Path(sup.run.root) / "approaches" / "01").is_dir()
    first = _events(sup, "spend.exhausted")[0]
    assert (first["hat"], first["level"], first["action"],
            first["refusedHat"]) == (hat, level, "replan", refused)
    assert abs(first["spend"] - spend) < 1e-9
    assert abs(status["spend"]["byApproach"]["1"][hat] - spend) < 1e-9


def test_a_steer_re_ask_refused_by_its_per_approach_cap_replans_on_a_non_last_approach(
        tmp_path):
    """The finding's own probe: steering queued after planning, a malformed
    steer settlement costing $0.70 against a $0.50 per-approach steer cap,
    two approaches. The contract re-ask is refused, approach 1 is archived
    and the run replans -- never `exhausted` on approach 1."""
    seen: list = []
    sup, final, status = _run(
        tmp_path, {"planning": [_plan_noting(seen, steer="reshape it")] * 3,
                   "steer": [_malformed(0.7)] * 3, "worker": [_work(0.0)]},
        spend_caps=_caps(per_approach={"steer": 0.5}), max_approaches=2)
    _assert_replanned_on_spend(sup, seen, status, hat="steer",
                               level="per_approach", refused="steer", spend=0.7)
    assert status.get("exhaustedBudget") != "spend:steer:approach"
    # the refused re-ask never reached a runner: one steer dispatch in all
    assert sup.runner.calls["steer"] == 1
    assert abs(status["spend"]["overall"]["steer"] - 0.7) < 1e-9


def test_a_steer_first_dispatch_refused_by_a_spent_total_per_approach_cap_replans(
        tmp_path):
    """Not in the finding: the steer hat's FIRST dispatch is the refused one
    (a worker iteration that queued steering spent the per-approach `total`),
    so the refusal comes from the `steer` step itself."""
    seen: list = []

    def worker_queues_steering(sup, prompt=""):
        if not sup.run.pending_steering() and seen == [1]:
            sup.run.add_steering("change course", "op2")
        return _usage_result("still working on it", 1.2)

    sup, final, status = _run(
        tmp_path, {"planning": [_plan_noting(seen)] * 3,
                   "worker": [worker_queues_steering] + [_work(0.0)] * 5,
                   "steer": [_malformed(0.0)] * 3},
        spend_caps=_caps(per_approach={"total": 1.0}), max_approaches=2)
    _assert_replanned_on_spend(sup, seen, status, hat="total",
                               level="per_approach", refused="steer", spend=1.2)
    assert sup.runner.calls.get("steer", 0) <= 1   # never on approach 1


def test_a_peer_plan_review_refused_by_its_per_approach_cap_replans(tmp_path):
    """Not in the finding: the peer plan review objects at $0.70 against its
    $0.50 per-approach cap; the revised plan's review is refused at the
    `plan_review` step, approach 1 is archived and approach 2 plans afresh.
    On approach 2 (the last) the same refusal ends the run exhausted."""
    seen: list = []
    objects = lambda sup, prompt="": _usage_result(json.dumps(
        {"verdict": "objected", "reason": "task 001 is too vague"}), 0.7)
    sup, final, status = _run(
        tmp_path, {"planning": [_plan_noting(seen)] * 5,
                   "plan_review": [objects] * 5, "worker": [_work(0.0)]},
        spend_caps=_caps(per_approach={"plan_review": 0.5}), max_approaches=2,
        peer_plan_review=True)
    _assert_replanned_on_spend(sup, seen, status, hat="plan_review",
                               level="per_approach", refused="plan_review",
                               spend=0.7)
    assert final == "exhausted"
    assert status["exhaustedBudget"] == "spend:plan_review:approach"
    assert [e["action"] for e in _events(sup, "spend.exhausted")] == [
        "replan", "exhausted"]
    assert sup.runner.calls["plan_review"] == 2      # one per approach


def test_a_vigilant_verify_re_ask_refused_by_its_per_approach_cap_replans(tmp_path):
    """Not in the finding: the vigilant verifier's malformed reply spends its
    per-approach cap; the re-ask is refused and the approach replans."""
    seen: list = []
    complete = lambda sup, prompt="": _usage_result(json.dumps(
        {"verdict": "complete", "task_id": "001"}), 0.0)
    sup, final, status = _run(
        tmp_path, {"planning": [_plan_noting(seen)] * 3,
                   "worker": [complete] * 4, "verify": [_malformed(0.7)] * 4},
        spend_caps=_caps(per_approach={"verify": 0.5}), max_approaches=2,
        vigilant=True)
    _assert_replanned_on_spend(sup, seen, status, hat="verify",
                               level="per_approach", refused="verify", spend=0.7)
    assert status["exhaustedBudget"] == "spend:verify:approach"
