"""Optional host-side pricing map (task 052, #10).

Task 049 stopped reporting an unpriced iteration as `$0`; this task lets an
operator supply the missing rates themselves, which is the *only* way to get a
real number for a gateway alias like `aigw-openai/gpt-5` (no upstream pricing
table can know a model id that is local to one gateway).

The load-bearing rule under test everywhere below: a derived cost is never
conflated with a provider-reported one. It lives in its own `costDerivedUSD`
field, carries its own marker, and renders with a `~ ... derived` mark on
every surface -- and with no map configured, nothing changes at all (an
unpriced iteration stays *unknown*).

Layers covered: the map itself (unit), the runner's accumulation over real pi
NDJSON shapes, the bucket merge, the one shared formatter, `ralphctl status`,
the hub payload, `GET /config`, `ralphctl start`'s job.yaml wiring, and a
black-box engine run.
"""

from __future__ import annotations

import json

import pytest
import yaml
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_cli_ui import _write_dead_run
from test_e2e import engine_factory

from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.cli.main import _summarize_usage
from ralphd.cli.ui_server import run_detail
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.pricing import PricingMap
from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.engine.state import COST_UNAVAILABLE, cost_status, format_cost
from ralphd.log_merge import boundary_line

__all__ = ["ctl", "engine_factory", "unix_sock"]


# The map used across the layers: one gateway alias family, one exact alias,
# one pinned model and one family default.
MAP = {
    "aliases": {
        "aigw-openai/*": "openai/*",
        "eu.anthropic.claude-opus-5": "anthropic/claude-opus-5",
    },
    "models": {
        "openai/gpt-5": {"input": 1.25, "output": 10.0, "cacheRead": 0.125},
        "anthropic/claude-opus-5": {"input": 15.0, "output": 75.0},
        "anthropic/*": {"input": 3.0, "output": 15.0},
    },
}


# --------------------------------------------------------------------------
# the map itself
# --------------------------------------------------------------------------

def test_no_map_configured_is_none_so_cost_stays_unknown():
    assert PricingMap.from_config(None) is None
    assert PricingMap.from_config({}) is None
    assert PricingMap.from_config("nonsense") is None
    # a map whose only entry is unusable is no map at all (never fatal)
    assert PricingMap.from_config({"models": {"m": {"note": "todo"}}}) is None


def test_alias_rewrites_a_gateway_model_id_to_its_canonical_name():
    pm = PricingMap.from_config(MAP)
    assert pm.canonical("aigw-openai/gpt-5") == "openai/gpt-5"
    assert pm.canonical("eu.anthropic.claude-opus-5") == "anthropic/claude-opus-5"
    assert pm.canonical("openai/gpt-5") == "openai/gpt-5"  # already canonical


def test_rate_lookup_prefers_the_exact_model_over_a_family_wildcard():
    pm = PricingMap.from_config(MAP)
    assert pm.rate_for("anthropic/claude-opus-5").input == 15.0
    assert pm.rate_for("anthropic/claude-sonnet-9").input == 3.0
    assert pm.rate_for("eu.anthropic.claude-opus-5").input == 15.0
    assert pm.rate_for("google/gemini-3") is None
    assert pm.rate_for(None) is None


def test_derive_uses_per_million_rates_and_falls_back_for_cache_tokens():
    pm = PricingMap.from_config(MAP)
    # 1M input @1.25 + 1M output @10 + 1M cacheRead @0.125
    assert pm.derive({"input": 1_000_000, "output": 1_000_000,
                      "cacheRead": 1_000_000}, "aigw-openai/gpt-5") == 11.375
    # cacheWrite has no rate of its own -> the input rate, never a silent $0
    assert pm.derive({"cacheWrite": 1_000_000}, "openai/gpt-5") == 1.25
    assert pm.derive({"input": 100, "output": 10}, "unknown/model") is None


def test_flat_form_without_a_models_key_is_accepted():
    pm = PricingMap.from_config({"openai/gpt-5": {"input": 1.0, "output": 2.0}})
    assert pm.derive({"input": 1_000_000}, "openai/gpt-5") == 1.0


def test_describe_reports_the_rates_for_get_config():
    described = PricingMap.from_config(MAP).describe()
    assert described["aliases"]["aigw-openai/*"] == "openai/*"
    assert described["models"]["openai/gpt-5"] == {
        "input": 1.25, "output": 10.0, "cacheRead": 0.125, "cacheWrite": 1.25}


# --------------------------------------------------------------------------
# the runner: derived cost recorded separately from a provider price
# --------------------------------------------------------------------------

def _line(usage: dict) -> bytes:
    return json.dumps({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "hi"}],
        "usage": usage}}).encode()


def _scan(*lines: bytes, model: str | None = "aigw-openai/gpt-5",
          pricing: dict | None = MAP) -> dict:
    r = IterationResult()
    pm = PricingMap.from_config(pricing)
    for line in lines:
        PiRunner._scan_line(line, r, pricing=pm, model=model)
    return r.usage


def test_unpriced_iteration_with_a_rate_records_a_derived_cost():
    usage = _scan(_line({"input": 1_000_000, "output": 1_000_000, "totalTokens": 2_000_000}))
    assert usage["costDerivedUSD"] == 11.25
    assert usage["costDerived"] is True
    # the provider quoted nothing, and no derived money leaks into costUSD
    assert usage["costPriced"] is False
    assert "costUSD" not in usage
    assert cost_status(usage) == "derived"


def test_a_provider_price_is_never_replaced_by_the_map():
    usage = _scan(_line({"input": 1_000_000, "output": 0, "totalTokens": 1_000_000,
                         "cost": {"total": 0.02}}))
    assert usage["costUSD"] == 0.02
    assert usage["costPriced"] is True
    assert "costDerivedUSD" not in usage
    assert cost_status(usage) is None


def test_unpriced_messages_without_a_rate_stay_unknown_even_with_a_map():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110}),
                  model="google/gemini-3")
    assert "costDerivedUSD" not in usage
    assert usage["costDerived"] is False, "a missing rate must not read as covered"
    assert cost_status(usage) == "unknown"


def test_no_map_at_all_leaves_task_049_behaviour_untouched():
    usage = _scan(_line({"input": 100, "output": 10, "totalTokens": 110}), pricing=None)
    assert usage == {"input": 100, "output": 10, "cacheRead": 0, "cacheWrite": 0,
                     "totalTokens": 110, "costPriced": False, "costDerived": False}
    assert cost_status(usage) == "unknown"


def test_no_traffic_iteration_is_never_given_a_derived_cost():
    usage = _scan(_line({"input": 0, "output": 0, "totalTokens": 0}))
    assert usage["costUSD"] == 0 and "costDerivedUSD" not in usage
    assert "costDerived" not in usage


def test_provider_price_plus_derived_price_in_one_iteration_stay_separate():
    usage = _scan(
        _line({"input": 1_000_000, "output": 0, "totalTokens": 1_000_000,
               "cost": {"total": 0.02}}),
        _line({"input": 1_000_000, "output": 0, "totalTokens": 1_000_000}),
    )
    assert usage["costUSD"] == 0.02
    assert usage["costDerivedUSD"] == 1.25
    assert usage["costPriced"] is False and usage["costDerived"] is True
    assert cost_status(usage) == "derived"


# --------------------------------------------------------------------------
# the bucket merge (status.json usage / byPhase / byApproach)
# --------------------------------------------------------------------------

def _merge(*usages: dict) -> dict:
    bucket: dict = {}
    for usage in usages:
        LoopSupervisor._merge_usage(bucket, usage)
    return bucket


_DERIVED_IT = {"totalTokens": 110, "costDerivedUSD": 0.5, "costPriced": False,
               "costDerived": True}
_PRICED_IT = {"totalTokens": 110, "costUSD": 0.25, "costPriced": True}
_UNKNOWN_IT = {"totalTokens": 110, "costPriced": False, "costDerived": False}


def test_a_fully_derived_bucket_is_marked_derived_not_unknown():
    bucket = _merge(_DERIVED_IT, _DERIVED_IT)
    assert bucket["costStatus"] == "derived"
    assert bucket["costDerivedUSD"] == 1.0
    assert "costUSD" not in bucket
    assert bucket["totalTokens"] == 220


def test_a_bucket_mixing_provider_and_derived_money_is_still_derived():
    bucket = _merge(_PRICED_IT, _DERIVED_IT)
    assert bucket["costStatus"] == "derived"
    assert bucket["costUSD"] == 0.25 and bucket["costDerivedUSD"] == 0.5


def test_an_uncovered_iteration_downgrades_a_derived_bucket_to_partial():
    bucket = _merge(_DERIVED_IT, _UNKNOWN_IT)
    assert bucket["costStatus"] == "partial"
    # and the marker is monotone: a later fully-derived iteration cannot
    # un-learn the unknown remainder
    assert _merge(_DERIVED_IT, _UNKNOWN_IT, _DERIVED_IT)["costStatus"] == "partial"


def test_a_fully_priced_bucket_is_unchanged_by_this_task():
    assert _merge(_PRICED_IT, _PRICED_IT) == {"totalTokens": 220, "costUSD": 0.5}


def test_markers_are_never_summed_into_counters():
    bucket = _merge(_DERIVED_IT)
    assert "costPriced" not in bucket and "costDerived" not in bucket


# --------------------------------------------------------------------------
# the one shared formatter -> every surface
# --------------------------------------------------------------------------

def test_format_cost_marks_derived_money_and_never_hides_it_in_a_sum():
    assert format_cost({"costDerivedUSD": 0.45, "costStatus": "derived"}) == \
        "~$0.45 derived"
    assert format_cost({"costUSD": 0.56, "costDerivedUSD": 0.45,
                        "costStatus": "derived"}) == "$0.56 + ~$0.45 derived"
    # a partial bucket that also has derived money says both things
    assert format_cost({"costUSD": 0.56, "costDerivedUSD": 0.45,
                        "costStatus": "partial"}) == \
        "$0.56 + ~$0.45 derived, partial (rest unavailable)"
    # the no-traffic int 0 is not a quoted price, so it is not shown as one
    assert format_cost({"costUSD": 0, "costDerivedUSD": 0.45,
                        "costStatus": "derived"}) == "~$0.45 derived"
    # decimals still belong to the caller (hub uses 4, the logs footer raw)
    assert format_cost({"costDerivedUSD": 0.45, "costStatus": "derived"},
                       decimals=4) == "~$0.4500 derived"
    assert format_cost({"costDerivedUSD": 0.45, "costStatus": "derived"},
                       decimals=None) == "~$0.45 derived"
    # nothing derived, nothing known -> unchanged
    assert format_cost({"costStatus": "unknown"}) == COST_UNAVAILABLE


def test_status_cli_summary_marks_derived_cost(ctl: Ctl):
    usage = {"costDerivedUSD": 0.5, "costStatus": "derived", "totalTokens": 12_000,
             "byPhase": {"worker": {"costDerivedUSD": 0.5, "costStatus": "derived"}}}
    assert _summarize_usage(usage) == \
        "~$0.50 derived, 12k tokens (worker ~$0.50 derived)"

    rdir, _cdir = _seed_run(ctl, "tst-derived")
    (rdir / "status.json").write_text(json.dumps(
        {"runId": "tst-derived", "state": "succeeded", "verdict": "verified",
         "phase": "review", "approach": 1, "iterationsUsed": 4,
         "iterationsBudget": 25, "startedAt": "2024-01-01T00:00:00Z",
         "schemaVersion": 1, "usage": usage}))
    res = ctl.run("status", "tst-derived")
    assert res.returncode == 0, res.stderr
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("usage:")]
    assert len(line) == 1 and "derived" in line[0], res.stdout
    # --json keeps the raw contract fields untouched
    doc = json.loads(ctl.run("--json", "status", "tst-derived").stdout)
    assert doc["usage"]["costDerivedUSD"] == 0.5
    assert doc["usage"]["costStatus"] == "derived"


def test_logs_footer_marks_derived_cost():
    meta = {"number": 2, "phase": "worker", "model": "aigw-openai/gpt-5",
            "approach": 1, "startedAt": "2024-01-01T00:00:00Z",
            "endedAt": "2024-01-01T00:01:00Z", "exitCode": 0,
            "usage": {"totalTokens": 900, "costPriced": False,
                      "costDerived": True, "costDerivedUSD": 0.02}}
    lines = render_to_lines(boundary_line(meta, "end"), tty=False,
                            state=new_render_state())
    assert "cost=~$0.02 derived" in "\n".join(lines)


def test_hub_run_detail_marks_derived_cost(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-derived", state="succeeded", usage={
        "costDerivedUSD": 0.02, "costStatus": "derived", "totalTokens": 900,
        "byPhase": {"worker": {"costDerivedUSD": 0.02, "costStatus": "derived"}},
        "byApproach": {"1": {"costDerivedUSD": 0.02, "costStatus": "derived"}},
    })
    usage = run_detail(registry, "run-derived")["status"]["usage"]
    assert usage["costDisplay"] == "~$0.0200 derived"
    assert usage["byPhase"]["worker"]["costDisplay"] == "~$0.0200 derived"
    assert usage["byApproach"]["1"]["costDisplay"] == "~$0.0200 derived"
    assert usage["costDerivedUSD"] == 0.02  # raw fields untouched


# --------------------------------------------------------------------------
# config surfaces: GET /config and `ralphctl start`'s job.yaml
# --------------------------------------------------------------------------

def test_get_config_reports_the_configured_rates(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("".join(f"{k}: {json.dumps(v)}\n"
                            for k, v in {"run_id": "r", "pricing": MAP}.items()))
    cfg = JobConfig.load(path)
    assert cfg.effective()["pricing"]["models"]["openai/gpt-5"]["input"] == 1.25
    # the shipped default is no map at all
    assert JobConfig().effective()["pricing"] is None


def test_env_override_supplies_a_map_without_a_job_yaml_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("RALPHD_PRICING", json.dumps(MAP))
    cfg = JobConfig.load(tmp_path / "missing.yaml")
    assert PricingMap.from_config(cfg.pricing).rate_for("aigw-openai/gpt-5")
    monkeypatch.setenv("RALPHD_PRICING", "{not json")
    assert JobConfig.load(tmp_path / "missing.yaml").pricing == {}


def test_start_inlines_the_registry_pricing_map_into_job_yaml(ctl: Ctl):
    (ctl.registry / "config.yaml").write_text(yaml.safe_dump({"pricing": MAP}))
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-pricing")
    assert res.returncode == 0, res.stderr
    job_yaml = ctl.registry / "configs" / "tst-pricing" / "job.yaml"
    job = yaml.safe_load(job_yaml.read_text())
    assert job["pricing"] == MAP
    cfg = JobConfig.load(job_yaml)
    assert PricingMap.from_config(cfg.pricing).rate_for("aigw-openai/gpt-5")


def test_start_without_a_registry_pricing_map_writes_no_pricing_key(ctl: Ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-nopricing")
    assert res.returncode == 0, res.stderr
    job_yaml = ctl.registry / "configs" / "tst-nopricing" / "job.yaml"
    assert "pricing" not in yaml.safe_load(job_yaml.read_text())
    assert JobConfig.load(job_yaml).pricing == {}


# --------------------------------------------------------------------------
# black box: the real engine, unpriced stub traffic, a map in job.yaml
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model,expect_derived", [
    ("aigw-openai/gpt-5", True),     # alias -> openai/gpt-5 rates
    ("google/gemini-3", False),      # no rate anywhere -> still unknown
])
def test_unpriced_run_with_a_pricing_map(engine_factory, model, expect_derived):
    e = engine_factory(job={"on_complete": "exit", "iterations": 6,
                            "model": model, "pricing": MAP},
                       stub_env={"STUB_UNPRICED_COUNT": "99"})
    assert e.proc.wait(timeout=60) == 0

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas
    for meta in metas:
        usage = meta["usage"]
        assert usage["totalTokens"] > 0, meta
        assert usage["costPriced"] is False, meta
        assert "costUSD" not in usage, "derived money must never enter costUSD"
        if expect_derived:
            # stub pi bills 100 input + 10 output per iteration:
            # 100*1.25/1e6 + 10*10/1e6 = 0.000225
            assert usage["costDerivedUSD"] == 0.000225, meta
            assert usage["costDerived"] is True, meta
        else:
            assert "costDerivedUSD" not in usage, meta
            assert usage["costDerived"] is False, meta

    status = json.loads((e.run_dir / "status.json").read_text())
    usage = status["usage"]
    if expect_derived:
        assert usage["costStatus"] == "derived"
        assert usage["costDerivedUSD"] == round(0.000225 * len(metas), 6)
        assert format_cost(usage, decimals=4).endswith("derived")
    else:
        assert usage["costStatus"] == "unknown"
        assert "costDerivedUSD" not in usage
        assert format_cost(usage, decimals=4) == COST_UNAVAILABLE
