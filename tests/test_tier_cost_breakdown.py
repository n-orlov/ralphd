"""Task 017 (#68 half 2): a per-tier spend/iteration-count breakdown,
alongside `byPhase`/`byApproach`, so "how much did the `hard` tier cost, and
how many iterations bought that" is answerable from `status.json` alone.

No new pricing path: `loop._accumulate_usage` folds each iteration's usage
into `usage.byTier[tier]` with the SAME `_merge_usage` accumulator byPhase/
byApproach already use (task 050's `_merge_cost_status`, task 052's derived
markers -- all reused verbatim), plus a plain `iterations` counter the
accumulator never touches otherwise (never present on an incoming per-
iteration usage dict, so `_merge_usage`'s "add every numeric field" pass
cannot double-count it).

What is proven here, driving the real state machine (`sup._step_worker()`,
the standing "stub sup.runner, never _run_iteration_once" rule):

  * two iterations at two different tiers land in two distinct `byTier`
    buckets, each with its own spend and `iterations: 1`;
  * `state.cost_breakdown`/`ui_server.cost_view` (the hub's cost-dialog
    payload) carry the same `byTier` buckets, tier-ladder ordered
    (trivial < normal < hard), with the same `iterations` counts passed
    through untouched;
  * `ralphctl status --json` exposes `usage.byTier` verbatim (it is a bare
    passthrough of status.json, so this is the same on-disk write read back).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

from ralphd.cli.ui_server import cost_view
from ralphd.engine import loop as loop_module
from ralphd.engine import state
from ralphd.engine import tiers as T
from tests.conftest import RALPHCTL


def _make_sup(registry_root, run_id, tasks, **cfg_kw):
    """Same shape as `test_tier_dispatch._make_sup`, except the run dir
    lands at `<registry_root>/runs/<run_id>` -- the layout the hub/CLI's
    registry helpers (`cost_view`, `ralphctl --json status`) expect."""
    root = registry_root / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    kw = {"iterations": 5, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id=run_id, **kw)
    sup = loop_module.LoopSupervisor(cfg, run, root)
    return sup, run


class _ScriptedAgent:
    """A fake `sup.runner` that replies to worker iterations in a fixed
    order, one scripted `(task_id, usage)` reply per call -- deterministic
    because `scheduler.select_next_task` picks pending tasks in list order,
    so this drives the two tasks (and their two tiers) in exactly the order
    they were declared."""

    def __init__(self, replies: list[dict]):
        self._replies = list(replies)

    def interrupt(self):
        return False

    async def run(self, prompt, transcript, **kw):
        reply = self._replies.pop(0)
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"task_id": reply["task_id"], "verdict": "complete"})
        r.duration_s = 0.05
        r.usage = reply["usage"]
        return r


def _two_tier_run(registry_root, run_id="tier-cost"):
    tasks = [
        {"id": "001", "title": "cheap one", "status": "pending",
         "successCriteria": "c", "tier": "trivial"},
        {"id": "002", "title": "expensive one", "status": "pending",
         "successCriteria": "c", "tier": "hard"},
    ]
    sup, run = _make_sup(registry_root, run_id, tasks)
    sup.runner = _ScriptedAgent([
        {"task_id": "001",
         "usage": {"input": 10, "output": 5, "totalTokens": 15, "costUSD": 0.01}},
        {"task_id": "002",
         "usage": {"input": 400, "output": 200, "totalTokens": 600, "costUSD": 0.42}},
    ])
    asyncio.run(sup._step_worker())
    return run


def test_two_iterations_at_different_tiers_land_in_distinct_by_tier_buckets(tmp_path):
    registry = tmp_path / "registry"
    run = _two_tier_run(registry)

    status = run.read_status()
    by_tier = status["usage"]["byTier"]
    assert set(by_tier) == {"trivial", "hard"}

    cheap, pricey = by_tier["trivial"], by_tier["hard"]
    assert cheap["iterations"] == 1
    assert pricey["iterations"] == 1
    assert cheap["totalTokens"] == 15
    assert pricey["totalTokens"] == 600
    assert round(cheap["costUSD"], 6) == 0.01
    assert round(pricey["costUSD"], 6) == 0.42

    # No new pricing path: byTier's total sums exactly to the run's overall
    # total, the same invariant byPhase/byApproach already hold (PRD req 19).
    assert (cheap["totalTokens"] + pricey["totalTokens"]
            == status["usage"]["totalTokens"])
    assert round(cheap["costUSD"] + pricey["costUSD"], 6) == round(
        status["usage"]["costUSD"], 6)


def test_cost_breakdown_and_hub_cost_view_carry_the_tier_buckets(tmp_path):
    registry = tmp_path / "registry"
    _two_tier_run(registry, run_id="tier-cost")

    bd = state.cost_breakdown(registry / "runs" / "tier-cost")
    by_tier = {b["key"]: b for b in bd["byTier"]}
    assert set(by_tier) == {"trivial", "hard"}
    assert by_tier["trivial"]["iterations"] == 1
    assert by_tier["hard"]["iterations"] == 1
    # tier-ladder ordered (trivial < normal < hard), not insertion order --
    # here insertion order already agrees, so assert the ladder order
    # directly against `tiers.TIERS`.
    assert [b["key"] for b in bd["byTier"]] == ["trivial", "hard"]
    assert bd["byTier"][0]["key"] == T.TIERS[0]

    assert "by tier:" in bd["summaryLines"] or any(
        "by tier:" in ln for ln in bd["summaryLines"])

    # The hub's cost-dialog payload (task 028's `cost_view`) is byte-for-byte
    # `cost_breakdown` plus its rendered text -- proven generically elsewhere
    # (test_hub_cost_dialog.py); here we only need the tier buckets to have
    # actually made it through that one shaping into the hub payload.
    view = cost_view(registry, "tier-cost")
    assert view["byTier"] == bd["byTier"]
    assert "by tier:" in view["text"]
    assert "trivial" in view["text"] and "hard" in view["text"]


def test_ralphctl_status_json_exposes_the_tier_bucket(tmp_path):
    registry = tmp_path / "registry"
    _two_tier_run(registry, run_id="tier-cost")

    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    r = subprocess.run([str(RALPHCTL), "--json", "status", "tier-cost"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    by_tier = doc["usage"]["byTier"]
    assert set(by_tier) == {"trivial", "hard"}
    assert by_tier["trivial"]["iterations"] == 1
    assert by_tier["hard"]["iterations"] == 1
    assert by_tier["trivial"]["totalTokens"] == 15
    assert by_tier["hard"]["totalTokens"] == 600
