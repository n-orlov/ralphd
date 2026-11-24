"""v0.39 (#321) B.2: the per-approach / overall / last-approach cap outcome is
the same for EVERY hat-bearing step -- worker, verify, plan_review, the peer
verdict gate, steer -- whatever step the run is at when the dispatch is
refused. Drives the dispatch check and the step outcome directly with injected
counters, so nothing spends a dollar."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine import transitions
from ralphd.engine.loop import _SpendCapSpent
from tests.conftest import make_supervisor

# (step state, the phase name its dispatch carries, supervisor flags)
STEPS = [
    ("worker", "worker", {}),
    ("worker", "worker.hard", {}),
    ("verify", "verify", {"vigilant": True}),
    ("plan_review", "plan_review", {"peer_plan_review": True}),
    ("verdict_gate", "verdict_gate", {"peer_verdict_gate": True}),
    ("steer", "steer", {}),
]
IDS = [p for _, p, _ in STEPS]


def _sup(tmp_path, flags, caps, *, approach, max_approaches):
    sup = make_supervisor(tmp_path, prd=True, max_approaches=max_approaches,
                          spend_caps=caps, **flags)
    sup._approach = approach
    sup.run.update_status(approach=approach)
    (sup.run.root / "tasks.json").write_text(json.dumps({"tasks": []}))
    return sup


def _caps(level, hat, usd=1.0):
    return {level: {hat: {"usd": usd, "source": "flag"}}}


def _events(sup):
    path = Path(sup.run.root) / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in rows if e.get("type") == "spend.exhausted"]


def _spend(sup, phase, approach=1, *, own=1.2, other=0.3):
    sup.run.record_spend(approach, phase, {"costUSD": own})
    sup.run.record_spend(approach, "review", {"costUSD": other})


def _refuse(sup, phase) -> _SpendCapSpent:
    with pytest.raises(_SpendCapSpent) as raised:
        sup._enforce_spend_caps(phase)
    return raised.value


@pytest.mark.parametrize("step,phase,flags", STEPS, ids=IDS)
@pytest.mark.parametrize("capped", ["hat", "total"])
def test_a_spent_per_approach_cap_on_a_non_last_approach_archives_and_replans(
        tmp_path, step, phase, flags, capped):
    hat = phase.split(".")[0] if capped == "hat" else "total"
    sup = _sup(tmp_path, flags, _caps("per_approach", hat), approach=1,
               max_approaches=3)
    _spend(sup, phase)
    before = json.loads(json.dumps(sup.run.read_spend()))

    spent = _refuse(sup, phase)
    assert spent.action == "replan"
    [event] = _events(sup)
    assert (event["hat"], event["level"], event["action"],
            event["refusedHat"]) == (hat, "per_approach", "replan",
                                     phase.split(".")[0])
    assert event["spend"] >= 1.2

    # the step outcome is the stagnation_exhausted edge into replan, legal from
    # this very step, and the approach is archived
    assert sup._on_spend_cap_spent(spent) == (
        "advance", "replan", "stagnation_exhausted")
    assert (Path(sup.run.root) / "approaches" / "01").is_dir()
    assert transitions.is_legal(step, "replan", sup.cfg.vigilant,
                                sup.cfg.peer_plan_review,
                                sup.cfg.peer_verdict_gate)
    assert sup._advance(step, "replan", "stagnation_exhausted") == "replan"

    # the replan opens approach 2: no per-approach counters, overall retained
    assert sup._step_replan() == ("advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2
    after = sup.run.read_spend()
    assert after["overall"] == before["overall"]
    assert after["byApproach"].get("2", {}) == {}
    assert after["byApproach"]["1"] == before["byApproach"]["1"]


@pytest.mark.parametrize("step,phase,flags", STEPS, ids=IDS)
def test_a_spent_overall_cap_holds_exhausted_from_every_step(
        tmp_path, step, phase, flags):
    hat = phase.split(".")[0]
    sup = _sup(tmp_path, flags, _caps("overall", hat), approach=1,
               max_approaches=3)
    _spend(sup, phase)
    spent = _refuse(sup, phase)
    assert spent.action == "exhausted"
    assert sup._on_spend_cap_spent(spent) == ("tail",)
    assert not (Path(sup.run.root) / "approaches" / "01").exists()
    assert [e["action"] for e in _events(sup)] == ["exhausted"]


@pytest.mark.parametrize("step,phase,flags", STEPS, ids=IDS)
@pytest.mark.parametrize("approach", [2, 3])
def test_a_spent_per_approach_cap_on_the_last_approach_holds_exhausted(
        tmp_path, step, phase, flags, approach):
    """The last approach is the one `max_approaches` names; an earlier one is
    never `exhausted` by a per-approach cap, however far into the run."""
    last = approach
    hat = phase.split(".")[0]
    sup = _sup(tmp_path, flags, _caps("per_approach", hat), approach=approach,
               max_approaches=last)
    _spend(sup, phase, approach)
    spent = _refuse(sup, phase)
    assert spent.action == "exhausted"
    assert sup._on_spend_cap_spent(spent) == ("tail",)
    assert [e["action"] for e in _events(sup)] == ["exhausted"]


def test_a_per_approach_cap_counts_only_the_current_approach(tmp_path):
    """Approach 2 of 3 with the spend all in approach 1: nothing is spent yet,
    so the dispatch goes ahead; once approach 2 spends it, it replans again."""
    sup = _sup(tmp_path, {"vigilant": True}, _caps("per_approach", "verify"),
               approach=2, max_approaches=3)
    _spend(sup, "verify", approach=1)
    sup._enforce_spend_caps("verify")            # no raise
    sup.run.record_spend(2, "verify", {"costUSD": 1.1})
    assert _refuse(sup, "verify").action == "replan"
