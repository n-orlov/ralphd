"""v0.39 (#321): the engine counts spend per hat and `total`, for the current
approach and for the whole run, in the approach's persisted state
(status.json `spend`)."""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import state
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import make_supervisor


_CFG = dict(run_id="spend", status={"state": "running"}, iterations=20,
            max_approaches=2, vigilant=False, on_complete="exit",
            infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
            infra_outage_budget_s=1000.0)


class _Costed:
    """A runner that returns the next injected usage dict per iteration."""

    def __init__(self, usages):
        self.usages = list(usages)
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        r = IterationResult(exit_code=0)
        r.final_text = "not json"
        r.duration_s = 0.01
        r.usage = dict(self.usages.pop(0))
        return r


def _sup(tmp_path, usages, **cfg):
    runner = _Costed(usages)
    sup = make_supervisor(tmp_path / "run", runner, **{**_CFG, **cfg})
    return sup, runner


def _spend(sup):
    return sup.run.read_status()["spend"]


def test_each_iterations_cost_lands_in_hat_and_total_counters_for_approach_and_run(tmp_path):
    sup, _ = _sup(tmp_path, [{"costUSD": 0.5}, {"costUSD": 0.25},
                             {"costUSD": 1.0}, {"costUSD": 2.0}])
    asyncio.run(sup.run_iteration("worker"))
    asyncio.run(sup.run_iteration("review"))
    assert _spend(sup)["overall"] == {"worker": 0.5, "review": 0.25, "total": 0.75}
    assert _spend(sup)["byApproach"]["1"] == {"worker": 0.5, "review": 0.25,
                                              "total": 0.75}
    # the next approach's counters start at zero; the overall ones carry on
    sup._approach = 2
    sup.run.update_status(approach=2)
    asyncio.run(sup.run_iteration("worker"))
    spend = _spend(sup)
    assert spend["byApproach"]["2"] == {"worker": 1.0, "total": 1.0}
    assert spend["byApproach"]["1"]["total"] == 0.75
    assert spend["overall"] == {"worker": 1.5, "review": 0.25, "total": 1.75}


@pytest.mark.parametrize("tier", ["trivial", "normal", "hard"])
def test_worker_tier_variants_count_as_worker(tmp_path, tier):
    sup, _ = _sup(tmp_path, [{"costUSD": 0.4}])
    asyncio.run(sup.run_iteration("worker", tier=tier))
    assert _spend(sup)["overall"] == {"worker": 0.4, "total": 0.4}
    assert state.spend_hat(f"worker.{tier}") == "worker"


def test_a_subscription_iteration_counts_at_its_list_equivalent_price(tmp_path):
    usage = {"costUSD": 0.0, "costBasis": "subscription",
             "listEquivalentUSD": 3.25, "totalTokens": 1000}
    sup, _ = _sup(tmp_path, [usage])
    asyncio.run(sup.run_iteration("worker"))
    assert _spend(sup)["overall"] == {"worker": 3.25, "total": 3.25}
    assert state.iteration_spend(usage) == 3.25


def test_a_payer_stamped_zero_priced_iteration_is_not_counted_twice():
    usage = {"costUSD": 0.0, "costFree": True, "listEquivalentUSD": 0.8,
             "listEquivalentQuotedUSD": 0.8}
    assert state.iteration_spend(usage) == 0.8
    assert state.iteration_spend({}) == 0.0
    assert state.iteration_spend({"costUSD": 1.0, "costDerivedUSD": 0.5}) == 1.5


def test_counters_are_unchanged_across_resume(tmp_path):
    sup, _ = _sup(tmp_path, [{"costUSD": 0.5}, {"costUSD": 0.5}])
    asyncio.run(sup.run_iteration("worker"))
    asyncio.run(sup.run_iteration("review"))
    before = _spend(sup)
    sup.resume()
    assert _spend(sup) == before
    # a fresh engine process over the same run dir reads the same counters
    reopened = RunDir(sup.run.root)
    assert reopened.seed_spend() is False
    assert reopened.read_spend() == before
    assert reopened.read_status()["spend"] == before


def _write_038_run(root, records):
    for n, (approach, phase, usage) in enumerate(records, start=1):
        d = root / "iterations" / f"{n:04d}"
        d.mkdir(parents=True)
        meta = {"number": n, "phase": phase, "approach": approach,
                "endedAt": "2026-01-01T00:00:00Z", "usage": usage}
        (d / "meta.json").write_text(json.dumps(meta))
    (root / "status.json").write_text(json.dumps({"state": "paused",
                                                  "approach": 2}))


def test_a_038_run_dir_loads_with_counters_rebuilt_from_its_iteration_records(tmp_path):
    records = [(1, "worker", {"costUSD": 1.5}),
               (1, "review", {"costUSD": 0.5}),
               (1, "worker", {"costUSD": 0.0, "costBasis": "subscription",
                              "listEquivalentUSD": 2.0}),
               (2, "worker", {"costUSD": 0.75}),
               (2, "planning", {})]
    root = tmp_path / "old"
    _write_038_run(root, records)
    run = RunDir(root)
    assert "spend" not in run.read_status()
    rebuilt = run.read_spend()
    expected_total = sum(state.iteration_spend(u) for _, _, u in records)
    assert expected_total == 4.75
    assert rebuilt["overall"]["total"] == expected_total
    assert rebuilt["overall"]["worker"] == 4.25
    assert rebuilt["byApproach"]["1"] == {"worker": 3.5, "review": 0.5,
                                          "total": 4.0}
    assert rebuilt["byApproach"]["2"] == {"worker": 0.75, "planning": 0.0,
                                          "total": 0.75}
    # an engine process opening the dir persists them; the next iteration adds on
    assert run.seed_spend() is True
    assert run.read_status()["spend"] == rebuilt
    run.record_spend(2, "worker", {"costUSD": 0.25}, number=6)
    assert run.read_status()["spend"]["overall"]["total"] == 5.0


def test_a_run_dir_with_no_records_gets_no_counters_written(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "status.json").write_text(json.dumps({"state": "running"}))
    run = RunDir(root)
    assert run.seed_spend() is False
    assert "spend" not in run.read_status()
    assert run.read_spend() == {"overall": {}, "byApproach": {}}
