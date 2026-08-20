"""An implausible ZERO cost quote is unknown, never `$0.00` (task 049, v0.6).

Steering 001 of the v0.6 run corrected #14's premise with live evidence from
this very repository's own self-development run: the AIGW/Bedrock route does
not omit the cost block, it quotes **zero** -- and pre-v0.6 ralphd recorded
that as `costPriced: true` and rendered `$0.00, 506k tokens` for 505 628
billed tokens. The verbatim payload is `_LIVE_ITERATION_USAGE` below and is
used as the fixture everywhere in this file.

What is pinned here:

* `engine/state.is_zero_quote` / `cost_status` / `format_cost` -- one zero
  quote is `unknown` on every surface, whatever marked it (unit);
* the run-level rollup shape (an **int** `0` with **no** `costStatus`, exactly
  what `/run/.../status.json` held) and `loop._merge_usage`'s verdict;
* `runner._accumulate_cost` -- a zero quote is recorded like an absent one
  (unpriced, derivable) plus a `costZeroQuoted` marker, and the #10 int-0
  no-traffic sentinel is preserved byte-for-byte;
* the DECLARED-free case -- `pricing.free` patterns, the only way a $0 over
  billable tokens survives as `$0.00` (never inferred from the zero itself);
* the surfaces: `ralphctl status`, `ralphctl runs`, the hub's `costDisplay`;
* the anomaly is reported through task 053's existing mechanism
  (`artifacts/reports/pricing-anomaly.md`), not a parallel one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_cli_ui import _write_dead_run
from test_e2e import engine_factory

from ralphd.cli.main import _summarize_usage
from ralphd.cli.ui_server import run_detail
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.pricing import PricingMap
from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.engine.state import (
    COST_UNAVAILABLE,
    COST_ZERO_QUOTE_NOTICE,
    billable_tokens,
    cost_status,
    format_cost,
    is_zero_quote,
)

__all__ = ["ctl", "engine_factory", "unix_sock"]

# Verbatim from /run/ralphd/iterations/0001/meta.json of the v0.6 run
# (steering 001): 505 628 billed tokens quoted at exactly $0, marked priced.
_LIVE_ITERATION_USAGE = {
    "input": 32,
    "output": 18320,
    "cacheRead": 438945,
    "cacheWrite": 48331,
    "totalTokens": 505628,
    "costUSD": 0,
    "costPriced": True,
}
# Verbatim shape of that run's status.json rollup: an int 0, no costStatus.
_LIVE_ROLLUP_USAGE = {
    "input": 64,
    "output": 36640,
    "cacheRead": 877890,
    "cacheWrite": 96662,
    "totalTokens": 1011256,
    "costUSD": 0,
    "byPhase": {"worker": {"totalTokens": 1011256, "costUSD": 0}},
    "byApproach": {"1": {"totalTokens": 1011256, "costUSD": 0}},
}

_FREE_MAP = {"models": {"openai/gpt-5": {"input": 1.25, "output": 10.0}},
             "aliases": {"aigw-openai/*": "openai/*"},
             "free": ["ollama/*", "local-llama"]}


def _line(usage: dict | None) -> bytes:
    msg: dict = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    if usage is not None:
        msg["usage"] = usage
    return json.dumps({"type": "message_end", "message": msg}).encode()


def _scan(*lines: bytes, pricing: dict | None = None,
          model: str | None = None) -> dict:
    pm = PricingMap.from_config(pricing)
    r = IterationResult()
    for line in lines:
        PiRunner._scan_line(line, r, pricing=pm, model=model)
    return r.usage


# --------------------------------------------------------------------------
# the classifier
# --------------------------------------------------------------------------

def test_the_live_iteration_payload_is_classified_unknown_not_priced():
    assert is_zero_quote(_LIVE_ITERATION_USAGE) is True
    assert cost_status(_LIVE_ITERATION_USAGE) == "unknown"
    rendered = format_cost(_LIVE_ITERATION_USAGE)
    assert rendered == COST_UNAVAILABLE
    assert "$" not in rendered


def test_the_live_run_level_rollup_is_classified_unknown_not_priced():
    """The rollup carries an int 0 and NO costStatus -- unknown all the same."""
    assert "costStatus" not in _LIVE_ROLLUP_USAGE
    assert cost_status(_LIVE_ROLLUP_USAGE) == "unknown"
    assert format_cost(_LIVE_ROLLUP_USAGE, decimals=4) == COST_UNAVAILABLE
    for bucket in (_LIVE_ROLLUP_USAGE["byPhase"]["worker"],
                   _LIVE_ROLLUP_USAGE["byApproach"]["1"]):
        assert cost_status(bucket) == "unknown"


def test_a_float_zero_quote_over_billed_tokens_is_unknown_too():
    usage = {"totalTokens": 900, "costUSD": 0.0, "costPriced": True}
    assert cost_status(usage) == "unknown"
    assert format_cost(usage) == COST_UNAVAILABLE


def test_a_zero_quote_never_makes_a_bucket_merely_partial():
    """`$0.00+ (partial, ...)` would smuggle the same lie back in."""
    assert format_cost({"totalTokens": 900, "costUSD": 0.0,
                        "costPriced": False}) == COST_UNAVAILABLE


@pytest.mark.parametrize("usage", [
    {"costUSD": 0},                                    # no-traffic sentinel
    {"costUSD": 0, "input": 0, "output": 0, "totalTokens": 0},
    {"costUSD": 0.0, "costPriced": True},
    {"costUSD": 0, "totalTokens": 500, "costFree": True},   # declared free
    {"costUSD": 0.42, "totalTokens": 500},
    {"totalTokens": 500},                              # no cost field at all
    {"costUSD": "junk", "totalTokens": 500},
    {},
    None,
])
def test_shapes_that_are_not_the_anomaly(usage):
    assert is_zero_quote(usage) is False


def test_the_no_traffic_int_zero_still_renders_as_a_real_zero():
    """#10's sentinel is untouched: nothing billed means $0 is the truth."""
    assert cost_status({"costUSD": 0}) is None
    assert format_cost({"costUSD": 0}, decimals=None) == "$0"
    assert format_cost({"costUSD": 0.0, "costPriced": True}) == "$0.00"


def test_a_declared_free_route_keeps_its_zero_on_every_surface():
    usage = {"totalTokens": 505628, "costUSD": 0.0, "costPriced": True,
             "costFree": True}
    assert is_zero_quote(usage) is False
    assert cost_status(usage) is None
    assert format_cost(usage) == "$0.00"
    assert _summarize_usage(usage) == "$0.00, 506k tokens"


def test_billable_tokens_counts_every_counter_and_survives_junk():
    assert billable_tokens(_LIVE_ITERATION_USAGE) > 0
    assert billable_tokens({"cacheRead": 5}) == 5
    assert billable_tokens({"input": None, "output": "x", "totalTokens": True}) == 0
    assert billable_tokens({}) == 0 and billable_tokens(None) == 0


def test_a_zero_quote_with_derived_money_reads_as_derived_only():
    """The zero must not be printed next to the derived amount as `$0.00 + ~`."""
    usage = {"totalTokens": 900, "costUSD": 0.0, "costPriced": False,
             "costDerivedUSD": 0.45, "costDerived": True}
    assert cost_status(usage) == "derived"
    assert format_cost(usage) == "~$0.45 derived"


# --------------------------------------------------------------------------
# the recorder (`runner._accumulate_cost`)
# --------------------------------------------------------------------------

def test_a_zero_quote_over_billed_tokens_is_recorded_as_unpriced():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0}}))
    assert "costUSD" not in usage, "a zero quote must not be recorded as money"
    assert usage["costPriced"] is False
    assert usage["costZeroQuoted"] is True
    assert usage["costDerived"] is False
    assert cost_status(usage) == "unknown"


def test_a_zero_quote_is_derivable_like_an_absent_one():
    """Steering 001 item 2: the derived path must fire on THIS route shape."""
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0}}),
                  pricing=_FREE_MAP, model="aigw-openai/gpt-5")
    assert usage["costDerivedUSD"] == 0.000225
    assert usage["costDerived"] is True
    assert usage["costZeroQuoted"] is True
    assert "costUSD" not in usage
    assert cost_status(usage) == "derived"


def test_a_declared_free_route_records_a_real_zero_with_the_declaration():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0}}),
                  pricing=_FREE_MAP, model="ollama/llama-4")
    assert usage["costUSD"] == 0.0
    assert usage["costPriced"] is True
    assert usage["costFree"] is True
    assert "costZeroQuoted" not in usage
    assert format_cost(usage) == "$0.00"


def test_an_undeclared_route_in_the_same_map_is_still_the_anomaly():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0}}),
                  pricing={"free": ["ollama/*"]}, model="bedrock/opus-5")
    assert usage["costPriced"] is False and usage["costZeroQuoted"] is True


def test_a_nonzero_quote_is_untouched_by_the_new_branch():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0.01}}))
    assert usage == {"input": 100, "output": 10, "cacheRead": 0,
                     "cacheWrite": 0, "totalTokens": 110,
                     "costUSD": 0.01, "costPriced": True}


def test_a_zero_quote_with_nothing_billed_keeps_the_historical_int_zero():
    usage = _scan(_line({"input": 0, "output": 0, "totalTokens": 0,
                         "cost": {"total": 0}}))
    assert usage["costUSD"] == 0 and json.dumps(usage["costUSD"]) == "0"
    assert usage["costPriced"] is True
    assert "costZeroQuoted" not in usage and "costFree" not in usage


def test_a_priced_message_next_to_a_zero_quoted_one_stays_partial():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0.02}}),
                  _line({"input": 100, "output": 10, "totalTokens": 110,
                         "cost": {"total": 0}}))
    assert usage["costUSD"] == 0.02
    assert usage["costPriced"] is False and usage["costZeroQuoted"] is True
    assert cost_status(usage) == "partial"
    assert format_cost(usage) == "$0.02+ (partial, rest unavailable)"


# --------------------------------------------------------------------------
# the free declaration in the pricing map
# --------------------------------------------------------------------------

def test_pricing_map_free_patterns_match_like_rates_do():
    pm = PricingMap.from_config(_FREE_MAP)
    assert pm.is_free("ollama/llama-4") is True
    assert pm.is_free("local-llama") is True
    assert pm.is_free("openai/gpt-5") is False
    assert pm.is_free(None) is False


def test_pricing_map_free_declaration_follows_the_alias_table():
    pm = PricingMap.from_config({"aliases": {"aigw-local/*": "ollama/*"},
                                 "free": ["ollama/*"]})
    assert pm.is_free("aigw-local/llama-4") is True


def test_a_free_only_pricing_map_is_usable_and_described():
    pm = PricingMap.from_config({"free": ["ollama/*"]})
    assert pm is not None and pm.models == {}
    assert pm.describe()["free"] == ["ollama/*"]
    assert PricingMap.from_config({}) is None
    assert PricingMap.from_config({"aliases": {"a": "b"}}) is None


def test_a_flat_pricing_map_does_not_read_free_as_a_model():
    pm = PricingMap.from_config({"openai/gpt-5": {"input": 1.0},
                                 "free": ["ollama/*"]})
    assert set(pm.models) == {"openai/gpt-5"}
    assert pm.is_free("ollama/x") is True


# --------------------------------------------------------------------------
# the rollup (`loop._merge_usage`)
# --------------------------------------------------------------------------

def test_merging_the_live_payload_marks_the_bucket_unknown():
    bucket = LoopSupervisor._merge_usage({}, dict(_LIVE_ITERATION_USAGE))
    assert bucket["costStatus"] == "unknown"
    assert bucket["totalTokens"] == 505628
    assert format_cost(bucket) == COST_UNAVAILABLE


def test_merging_a_priced_iteration_after_a_zero_quoted_one_is_partial():
    bucket = LoopSupervisor._merge_usage({}, dict(_LIVE_ITERATION_USAGE))
    bucket = LoopSupervisor._merge_usage(bucket, {"totalTokens": 100, "costUSD": 0.5,
                                          "costPriced": True})
    assert bucket["costStatus"] == "partial"
    assert format_cost(bucket) == "$0.50+ (partial, rest unavailable)"


def test_the_free_declaration_survives_the_rollup():
    """Otherwise the rollup would re-read its own honest $0 as the anomaly."""
    free = {"totalTokens": 110, "costUSD": 0.0, "costPriced": True,
            "costFree": True}
    bucket = LoopSupervisor._merge_usage(LoopSupervisor._merge_usage({}, dict(free)), dict(free))
    assert bucket["costFree"] is True
    assert "costStatus" not in bucket
    assert format_cost(bucket) == "$0.00"


def test_a_fully_priced_rollup_is_unchanged():
    bucket = LoopSupervisor._merge_usage({}, {"totalTokens": 110, "costUSD": 0.01,
                                      "costPriced": True})
    assert bucket == {"totalTokens": 110, "costUSD": 0.01}


# --------------------------------------------------------------------------
# the surfaces: `ralphctl status`, `ralphctl runs`, the hub
# --------------------------------------------------------------------------

def test_status_summary_never_prints_zero_dollars_for_the_live_rollup():
    summary = _summarize_usage(_LIVE_ROLLUP_USAGE)
    assert summary == ("unavailable, 1011k tokens (worker unavailable)")
    assert "$0.00" not in summary


_BASE_STATUS = {
    "state": "failed", "verdict": "unverified", "phase": "worker",
    "approach": 1, "iterationsUsed": 4, "iterationsBudget": 25,
    "startedAt": "2024-01-01T00:00:00Z", "schemaVersion": 1,
}


def test_ralphctl_status_and_runs_never_render_the_zero(ctl: Ctl):
    rdir, _cdir = _seed_run(ctl, "tst-zeroquote")
    (rdir / "status.json").write_text(json.dumps(
        {**_BASE_STATUS, "runId": "tst-zeroquote", "usage": _LIVE_ROLLUP_USAGE}))
    res = ctl.run("status", "tst-zeroquote")
    assert res.returncode == 0, res.stderr
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("usage:")]
    assert len(line) == 1 and COST_UNAVAILABLE in line[0], res.stdout
    assert "$" not in line[0], line[0]
    # `ralphctl runs` has no cost column, so it must not grow one by accident
    res = ctl.run("runs")
    assert res.returncode == 0, res.stderr
    assert "$0.00" not in res.stdout and "$0.0000" not in res.stdout
    # the raw contract is untouched: the honest zero stays on disk
    res = ctl.run("--json", "status", "tst-zeroquote")
    assert json.loads(res.stdout)["usage"]["costUSD"] == 0


def test_hub_run_detail_ships_unavailable_for_the_live_rollup(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-zeroquote", state="failed",
                    usage=json.loads(json.dumps(_LIVE_ROLLUP_USAGE)))
    usage = run_detail(registry, "run-zeroquote")["status"]["usage"]
    assert usage["costDisplay"] == COST_UNAVAILABLE
    assert usage["byPhase"]["worker"]["costDisplay"] == COST_UNAVAILABLE
    assert usage["byApproach"]["1"]["costDisplay"] == COST_UNAVAILABLE
    assert usage["costUSD"] == 0, "the raw quote is preserved as recorded"


def test_hub_run_detail_still_ships_a_real_zero_for_a_declared_free_run(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-free", state="succeeded",
                    usage={"totalTokens": 900, "costUSD": 0.0,
                           "costFree": True})
    usage = run_detail(registry, "run-free")["status"]["usage"]
    assert usage["costDisplay"] == "$0.0000"


# --------------------------------------------------------------------------
# the anomaly is reported through task 053's existing mechanism
# --------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_the_notice_points_at_the_existing_pricing_anomaly_report():
    report = _repo_root() / "artifacts" / "reports" / "pricing-anomaly.md"
    assert "artifacts/reports/pricing-anomaly.md" in COST_ZERO_QUOTE_NOTICE
    assert report.exists(), "task 053's report is the anomaly surface"
    text = report.read_text()
    assert "implausible zero" in text.lower()
    assert "505" in text, "the live evidence must be recorded in the report"


def test_no_parallel_anomaly_report_was_added():
    reports = {p.name for p in (_repo_root() / "artifacts" / "reports").iterdir()}
    extra = {n for n in reports if "anomal" in n and n != "pricing-anomaly.md"}
    assert not extra, f"report the zero-quote anomaly in the existing file: {extra}"


# --------------------------------------------------------------------------
# black box: the real engine over a zero-quoting stub gateway
# --------------------------------------------------------------------------

def test_a_zero_quoting_run_records_unknown_cost_end_to_end(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "iterations": 6},
                       stub_env={"STUB_ZERO_COST_COUNT": "99"})
    assert e.proc.wait(timeout=60) == 0

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas
    for meta in metas:
        usage = meta["usage"]
        assert usage["totalTokens"] > 0, meta
        assert usage["costPriced"] is False, meta
        assert usage["costZeroQuoted"] is True, meta
        assert "costUSD" not in usage, meta

    status = json.loads((e.run_dir / "status.json").read_text())
    usage = status["usage"]
    assert usage["costStatus"] == "unknown"
    assert "costUSD" not in usage
    assert usage["totalTokens"] > 0
    assert format_cost(usage, decimals=4) == COST_UNAVAILABLE
    assert usage["byPhase"]["worker"]["costStatus"] == "unknown"
    # the engine says so out loud, in the shared wording
    assert COST_ZERO_QUOTE_NOTICE in e.proc.stdout.read()


def test_a_zero_quoting_run_with_a_free_declaration_stays_zero(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "iterations": 6,
                            "model": "ollama/llama-4", "pricing": _FREE_MAP},
                       stub_env={"STUB_ZERO_COST_COUNT": "99"})
    assert e.proc.wait(timeout=60) == 0

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas
    for meta in metas:
        assert meta["usage"]["costUSD"] == 0.0, meta
        assert meta["usage"]["costFree"] is True, meta

    usage = json.loads((e.run_dir / "status.json").read_text())["usage"]
    assert "costStatus" not in usage
    assert format_cost(usage) == "$0.00"


def test_a_zero_quoting_run_with_a_rate_reports_derived_money(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "iterations": 6,
                            "model": "aigw-openai/gpt-5", "pricing": _FREE_MAP},
                       stub_env={"STUB_ZERO_COST_COUNT": "99"})
    assert e.proc.wait(timeout=60) == 0

    usage = json.loads((e.run_dir / "status.json").read_text())["usage"]
    assert usage["costStatus"] == "derived"
    assert usage["costDerivedUSD"] > 0
    assert "costUSD" not in usage
    assert format_cost(usage, decimals=4).endswith("derived")
