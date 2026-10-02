"""PRD v0.30 E: cost on a subscription.

A `claude-code` iteration bills against the plan, so its cost is exactly `$0`
(`costUSD 0`, `costFree`, `costBasis "subscription"`), never `costZeroQuoted`
and never derived from a rate table. The SDK's list-price figure rides apart
as `listEquivalentUSD`, summed per phase and approach, shown on its own line
by `ralphctl cost` and `ralphctl status`, and never added to the cost.

The iteration test drives the real wrapper and SDK against the stub Claude
Code CLI (no network, no real token).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from ralphd.engine import state
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import ClaudeCodeRunner
from tests.conftest import call_ralphctl_in_process

REPO = Path(__file__).resolve().parents[1]
STUB = REPO / "tests" / "stub-claude" / "claude"

PI_PRICED = {"input": 100, "output": 50, "totalTokens": 150, "costUSD": 1.23,
             "costPriced": True}
PI_UNPRICED = {"input": 100, "output": 50, "totalTokens": 150, "costUSD": 0,
               "costPriced": False, "costDerived": False}
PI_FREE = {"input": 100, "output": 50, "totalTokens": 150, "costUSD": 0.0,
           "costPriced": True, "costFree": True}
SUB = {"input": 10, "output": 5, "totalTokens": 15, "costUSD": 0.0,
       "costFree": True, "costBasis": "subscription", "listEquivalentUSD": 0.4}


def _run_stub(mp, tmp_path: Path, **stub):
    mp.setenv("RALPHD_CLAUDE_CLI_PATH", str(STUB))
    mp.setenv("STUB_CLAUDE_OBSERVE", str(tmp_path / "observed.json"))
    for k in [k for k in os.environ if k.startswith("STUB_CLAUDE_")
              and k != "STUB_CLAUDE_OBSERVE"]:
        mp.delenv(k)
    for k, v in stub.items():
        mp.setenv(k, v)
    ws = tmp_path / "ws"
    ws.mkdir()
    it = tmp_path / "it"
    it.mkdir()
    return asyncio.run(ClaudeCodeRunner(ws).run(
        "the prompt", it / "output.jsonl", model="anthropic/claude-sonnet-5-5",
        timeout_s=60))


def test_a_stub_iteration_records_subscription_cost(tmp_path, monkeypatch):
    result = _run_stub(monkeypatch, tmp_path, STUB_CLAUDE_COST="0.4")
    usage = result.usage
    assert usage["costUSD"] == 0
    assert isinstance(usage["costUSD"], float)
    assert usage["costFree"] is True
    assert usage["costBasis"] == "subscription"
    assert usage["listEquivalentUSD"] == pytest.approx(0.4)
    assert "costZeroQuoted" not in usage
    assert "costDerived" not in usage
    assert "costDerivedUSD" not in usage
    assert state.cost_source(usage) == state.COST_SOURCE_SUBSCRIPTION
    assert not state.is_zero_quote(usage)


def test_merge_usage_handles_string_values_without_a_type_error():
    bucket = LoopSupervisor._merge_usage({}, dict(SUB))
    bucket = LoopSupervisor._merge_usage(bucket, dict(SUB))
    assert bucket["costBasis"] == "subscription"
    assert bucket["costFree"] is True
    assert bucket["costUSD"] == 0
    assert bucket["listEquivalentUSD"] == pytest.approx(0.8)
    task = state._accumulate_task_usage(state._accumulate_task_usage({}, dict(SUB)),
                                        dict(SUB))
    assert task == bucket


def test_a_bucket_mixing_pi_money_and_subscription_stays_priced():
    for order in ((PI_PRICED, SUB), (SUB, PI_PRICED)):
        for merge in (LoopSupervisor._merge_usage, state._accumulate_task_usage):
            bucket: dict = {}
            for u in order:
                merge(bucket, dict(u))
            assert "costFree" not in bucket, order
            assert bucket["costBasis"] == "mixed"
            assert bucket["costUSD"] == pytest.approx(1.23)
            assert bucket["listEquivalentUSD"] == pytest.approx(0.4)
            assert state.cost_source(bucket) == state.COST_SOURCE_PROVIDER


def test_a_bucket_of_free_pi_and_subscription_stays_free_and_mixed():
    for merge in (LoopSupervisor._merge_usage, state._accumulate_task_usage):
        bucket: dict = {}
        merge(bucket, dict(PI_FREE))
        merge(bucket, dict(SUB))
        assert bucket["costFree"] is True
        assert bucket["costBasis"] == "mixed"


def test_a_subscription_zero_never_turns_unknown_into_partial():
    for merge in (LoopSupervisor._merge_usage, state._accumulate_task_usage):
        for order in ((PI_UNPRICED, SUB), (SUB, PI_UNPRICED)):
            bucket: dict = {}
            for u in order:
                merge(bucket, dict(u))
            assert bucket["costStatus"] == "unknown", order
            assert "costFree" not in bucket
    assert not LoopSupervisor._has_reported_price(dict(SUB))
    assert LoopSupervisor._has_reported_price(dict(PI_PRICED))


def _mixed_usage() -> dict:
    total: dict = {}
    phases: dict = {}
    for phase, u in (("worker", PI_PRICED), ("review", SUB)):
        LoopSupervisor._merge_usage(total, dict(u))
        phases[phase] = LoopSupervisor._merge_usage({}, dict(u))
    total["byPhase"] = phases
    return total


def _dead_run(tmp_path: Path, usage: dict) -> Path:
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / "mixed"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": "mixed", "state": "succeeded", "iterationsUsed": 2, "usage": usage}))
    return registry


def test_cost_breakdown_shows_list_equivalent_apart_from_cost(tmp_path):
    usage = _mixed_usage()
    bd = state.cost_breakdown(_dead_run(tmp_path, usage) / "runs" / "mixed")
    assert bd["listEquivalentUSD"] == pytest.approx(0.4)
    assert bd["total"]["costDisplay"] == "$1.2300"
    lines = bd["summaryLines"]
    assert lines[0] == "cost:      $1.2300"
    assert lines[1].startswith("list-equiv: $0.4000")
    assert bd["total"]["costSource"] != state.COST_SOURCE_FREE
    rows = {ln.split()[0]: ln for ln in lines if ln.startswith("  ")}
    assert "list-equiv" not in rows["worker"]
    assert "list-equiv: $0.4000" in rows["review"]
    assert state.cost_bucket(usage["byPhase"]["review"])["costSource"] == "subscription"


def test_a_run_without_subscription_has_no_list_equivalent_line(tmp_path):
    usage = {"totalTokens": 150, "costUSD": 1.23,
             "byPhase": {"worker": dict(PI_PRICED)}}
    bd = state.cost_breakdown(_dead_run(tmp_path, usage) / "runs" / "mixed")
    assert "listEquivalentUSD" not in bd
    assert not any("list-equiv" in ln for ln in bd["summaryLines"])


def test_ralphctl_cost_and_status_show_the_list_equivalent_line(tmp_path, capsys):
    registry = _dead_run(tmp_path, _mixed_usage())
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    cost = call_ralphctl_in_process(["cost", "mixed"], env=env, capsys=capsys)
    assert cost.returncode == 0, cost.stderr
    lines = cost.stdout.splitlines()
    assert "cost:      $1.2300" in lines
    assert any(ln.startswith("list-equiv: $0.4000") for ln in lines)
    status = call_ralphctl_in_process(["status", "mixed"], env=env, capsys=capsys)
    assert status.returncode == 0, status.stderr
    assert any(ln.startswith("list-equiv: $0.4000") for ln in status.stdout.splitlines())


def test_the_hub_view_carries_the_list_equivalent_display():
    from ralphd.cli.ui_server import _with_cost_display
    doc = _with_cost_display({"usage": _mixed_usage()})
    assert doc["usage"]["listEquivalentDisplay"] == "$0.4000"
    assert doc["usage"]["byPhase"]["review"]["listEquivalentDisplay"] == "$0.4000"
    assert "listEquivalentDisplay" not in doc["usage"]["byPhase"]["worker"]


def test_run_record_names_the_cost_basis():
    from ralphd.engine import run_record
    text = "\n".join(run_record._usage_lines(_mixed_usage()))
    assert "- costBasis: mixed" in text
    assert "- listEquivalentUSD: 0.4" in text
