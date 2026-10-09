"""Codex as a registered harness (v0.33 A.1-A.4): the name validates on every
surface that validates a harness, the supervisor carries a `CodexRunner` and
dispatches to it with hat tool names (never pi extensions), the provisioning
lines and the hub cell print it, and the route check serves it OpenAI-family
models only. In-process `ralphctl` against the recording stub docker; no
container, no network, no real `codex`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from test_cli_docker import Ctl

from ralphd.cli import llm_profiles
from ralphd.cli.ui_server import provisioning_table_cells, run_detail
from ralphd.engine import hat_tools
from ralphd.engine import tiers
from ralphd.engine.config import (HARNESS_FORCED_BASIS, HARNESSES, MODEL_HATS,
                                  HatBundle, JobConfig, _model_family)
from ralphd.harness import codex as codex_harness
from ralphd.engine.llm import UnroutableHatModel, check_hat_routes
from ralphd.engine.runner import CodexRunner, IterationResult
from ralphd.engine.state import format_provisioning_lines
from tests.conftest import make_supervisor

OPENAI_MODEL = "aigw-openai/openai.gpt-6.1-sol"
ANTHROPIC_MODEL = "anthropic/claude-opus-5-5"


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _start(ctl, run_id, *extra, llm=None):
    argv = ["start", "--prd", str(ctl.prd), "--run-id", run_id, *extra]
    if llm:
        argv += ["--llm", llm]
    return ctl.run(*argv)


def _job_yaml(ctl, run_id) -> dict:
    return yaml.safe_load(
        (ctl.registry / "configs" / run_id / "job.yaml").read_text())


# -- A.1: the name validates everywhere ---------------------------------------

def test_codex_is_the_third_harness():
    assert HARNESSES == ("pi", "claude-code", "codex")


def test_harness_for_codex_is_accepted_and_persisted(ctl):
    res = _start(ctl, "tst-cx-flag", "--harness-for", "worker=codex")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-cx-flag")["harnesses"]["worker"] == "codex"
    assert _job_yaml(ctl, "tst-cx-flag")["harnesses"]["planning"] == "pi"


def test_harness_for_nonsense_exits_2_naming_all_three(ctl):
    res = _start(ctl, "tst-cx-bad", "--harness-for", "worker=nonsense")
    assert res.returncode == 2
    for name in ("pi", "claude-code", "codex"):
        assert name in res.stderr


def test_profile_harness_codex_resolves(ctl):
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cx.yaml").write_text(yaml.safe_dump(
        {"model": OPENAI_MODEL, "harness": "codex"}))
    resolved = llm_profiles.resolve_profile("cx", ctl.registry)
    assert resolved["harness"] == "codex"
    res = _start(ctl, "tst-cx-prof", llm="cx")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-cx-prof")["harnesses"] == {
        h: "codex" for h in MODEL_HATS}


def test_registry_harnesses_entry_for_codex_resolves(ctl):
    (ctl.registry / "config.yaml").write_text(
        yaml.safe_dump({"harnesses": {"verify": "codex"}}))
    res = _start(ctl, "tst-cx-reg")
    assert res.returncode == 0, res.stderr
    table = _job_yaml(ctl, "tst-cx-reg")["harnesses"]
    assert table["verify"] == "codex" and table["worker"] == "pi"


# -- A.3: provisioning surfaces -------------------------------------------------

def test_provisioning_lines_print_codex_for_a_codex_hat():
    cfg = JobConfig(model_overrides={"worker": OPENAI_MODEL},
                    harnesses={"worker": "codex"})
    lines = format_provisioning_lines(cfg.provisioning_table())
    assert any(l.startswith("worker") and l.endswith("[codex]") for l in lines)
    assert any(l.startswith("planning") and l.endswith("[pi]") for l in lines)


def test_ralphctl_status_prints_codex(ctl):
    from test_cli_ui import _write_dead_run
    run_id = "cx-status"
    _write_dead_run(ctl.registry, run_id, iterationsUsed=0, iterationsBudget=25)
    cdir = ctl.registry / "configs" / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "job.yaml").write_text(yaml.safe_dump(
        {"models": {"worker": OPENAI_MODEL}, "harnesses": {"worker": "codex"}}))
    res = ctl.run("status", run_id)
    assert res.returncode == 0, res.stderr
    assert any(l.strip().startswith("worker") and l.rstrip().endswith("[codex]")
               for l in res.stdout.splitlines())
    detail = run_detail(ctl.registry, run_id)
    assert any(l.startswith("worker") and l.endswith("[codex]")
               for l in detail["status"]["provisioningLines"])


def test_hub_provisioning_cell_names_codex():
    cfg = JobConfig(model_overrides={"worker": OPENAI_MODEL},
                    harnesses={"worker": "codex"})
    cells = provisioning_table_cells(cfg)
    by_hat = {row[0]: row for row in cells["rows"]}
    assert "codex" in by_hat["worker"]
    assert "codex" not in by_hat["planning"]
    assert by_hat["worker"][3] == "codex"


# -- A.2: dispatch -------------------------------------------------------------

_CFG = dict(run_id="cx", status={"state": "running"}, iterations=5,
            max_approaches=1, vigilant=False, on_complete="exit",
            infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
            infra_outage_budget_s=1000.0)


class _Fake:
    def __init__(self):
        self.calls: list[dict] = []
        self.running = False

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls.append(kw)
        r = IterationResult(exit_code=0)
        r.final_text = "not json"
        r.duration_s = 0.01
        r.usage = {}
        return r


def _sup(tmp_path, **cfg):
    return make_supervisor(tmp_path / "run", **{**_CFG, **cfg})


def test_supervisor_holds_a_codex_runner_and_returns_it(tmp_path):
    sup = _sup(tmp_path, harnesses={"worker": "codex"})
    assert isinstance(sup._runners["codex"], CodexRunner)
    assert sup._runner_for("codex") is sup._runners["codex"]
    assert sup._runner_for("codex") is not sup._runner_for("claude-code")


def test_codex_bundle_dispatch_passes_hat_tools_and_no_extensions(tmp_path):
    sup = _sup(tmp_path, harnesses={"worker": "codex", "review": "pi"})
    cx, pi = _Fake(), _Fake()
    sup._runners["codex"] = cx
    sup._runners["pi"] = sup.runner = pi
    broker = hat_tools.SteerToolBroker(
        sup.run, tools=frozenset(hat_tools.WORKER_WIRED_TOOLS), source="worker")
    broker.place()
    sup._worker_tool_broker = broker
    asyncio.run(sup.run_iteration("worker"))
    assert len(cx.calls) == 1 and pi.calls == []
    kw = cx.calls[0]
    assert kw["hat_tools"] == sorted(hat_tools.WORKER_WIRED_TOOLS)
    assert "extensions" not in kw


def test_a_scripted_sup_runner_still_overrides_the_codex_runner(tmp_path):
    sup = _sup(tmp_path, harnesses={"worker": "codex"})
    cx = _Fake()
    sup._runners["codex"] = cx
    double = _Fake()
    sup.runner = double
    asyncio.run(sup.run_iteration("worker"))
    assert len(double.calls) == 1 and cx.calls == []


# -- A.4: the route check ------------------------------------------------------

def _cfg(hat_model: str, harness: str) -> JobConfig:
    return JobConfig(model_overrides={"worker": hat_model},
                     harnesses={"worker": harness})


def _check(cfg: JobConfig) -> None:
    check_hat_routes(cfg, home=Path("/nonexistent"), builtin_providers=())


def test_codex_hat_accepts_an_openai_family_route():
    assert _model_family(OPENAI_MODEL) == "openai"
    _check(_cfg(OPENAI_MODEL, "codex"))


def test_codex_hat_refuses_a_non_openai_route_naming_hat_and_model():
    with pytest.raises(UnroutableHatModel) as err:
        _check(_cfg(ANTHROPIC_MODEL, "codex"))
    msg = str(err.value)
    assert "worker" in msg and ANTHROPIC_MODEL in msg
    assert "the codex harness only serves OpenAI-family models" in msg


def test_codex_tier_rung_on_a_non_openai_route_is_refused():
    cfg = JobConfig(
        model_overrides={"worker": OPENAI_MODEL}, harnesses={"worker": "codex"},
        model_tiers={"worker": {"hard": ANTHROPIC_MODEL}})
    with pytest.raises(UnroutableHatModel, match="only serves OpenAI-family"):
        _check(cfg)


def test_claude_code_hat_on_an_openai_model_is_still_refused():
    with pytest.raises(UnroutableHatModel) as err:
        _check(_cfg(OPENAI_MODEL, "claude-code"))
    assert "claude-code harness only serves anthropic models" in str(err.value)


# -- A.5: reasoning effort ------------------------------------------------------

@pytest.mark.parametrize("level,expected", [
    ("off", "low"), ("minimal", "low"), ("low", "low"), ("medium", "medium"),
    ("high", "high"), ("xhigh", "high")])
def test_effort_table_rows(level, expected):
    assert codex_harness.CODEX_EFFORT_TABLE[level] == expected
    assert codex_harness.effort_options(level, None) == {
        "model_reasoning_effort": expected}
    assert codex_harness.effort_options(None, level) == {
        "model_reasoning_effort": expected}


@pytest.mark.parametrize("level", ["max", "ultra", "", None, "HIGH"])
def test_unmapped_effort_falls_back_to_medium(level):
    assert codex_harness.effort_options(level, None) == {
        "model_reasoning_effort": "medium"}


def test_effort_table_values_are_only_low_medium_high():
    assert set(codex_harness.CODEX_EFFORT_TABLE.values()) <= {
        "low", "medium", "high"}
    assert {"off", "minimal", "low", "medium", "high", "xhigh"} == set(
        codex_harness.CODEX_EFFORT_TABLE)


def test_thinking_wins_over_effort():
    assert codex_harness.effort_options("low", "high") == {
        "model_reasoning_effort": "low"}


def test_codex_module_does_not_load_the_sdk():
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, ralphd.harness.codex; "
         "print('claude_agent_sdk' in sys.modules, "
         "'ralphd.harness.claude_code' in sys.modules)"],
        capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["False", "False"]


@pytest.mark.parametrize("tier", tiers.TIERS)
@pytest.mark.parametrize("level", ["off", "minimal", "low", "medium", "high",
                                   "xhigh"])
def test_codex_hat_step_down_yields_a_mappable_effort(tier, level):
    # a codex verify hat's ladder: each rung carries a level; the step-down
    # tier resolves a bundle whose level effort_options maps without error.
    ladder = {t: HatBundle(model=OPENAI_MODEL, thinking=level, harness="codex")
              for t in tiers.TIERS}
    host_map = {"verify": ladder, "planning": ladder}
    stepped = tiers.step_down_tier(tier)
    bundle = tiers.resolve_bundle("verify", stepped, host_map)
    opts = codex_harness.effort_options(bundle.thinking, bundle.effort)
    assert opts["model_reasoning_effort"] in {"low", "medium", "high"}
    assert opts["model_reasoning_effort"] == codex_harness.CODEX_EFFORT_TABLE[level]


def test_codex_hat_step_down_from_a_configured_job():
    cfg = JobConfig(model_overrides={"verify": OPENAI_MODEL},
                    harnesses={"verify": "codex"},
                    thinking_overrides={"verify": "xhigh"})
    host_map = tiers.default_host_map(cfg)
    for tier in tiers.TIERS:
        b = tiers.resolve_bundle("verify", tiers.step_down_tier(tier), host_map)
        assert codex_harness.effort_options(b.thinking, b.effort)[
            "model_reasoning_effort"] == "high"


# -- A.6: billing ---------------------------------------------------------------

def test_subscription_basis_on_a_codex_profile_exits_2(ctl):
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cxb.yaml").write_text(yaml.safe_dump(
        {"model": OPENAI_MODEL, "harness": "codex"}))
    res = _start(ctl, "tst-cx-bill", "--billing-basis", "subscription",
                 llm="cxb")
    assert res.returncode == 2
    assert "--billing-basis" in res.stderr
    assert "contradicts harness 'codex'" in res.stderr
    assert not (ctl.registry / "configs" / "tst-cx-bill" / "job.yaml").exists()


def test_metered_basis_on_a_codex_profile_is_accepted(ctl):
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cxb.yaml").write_text(yaml.safe_dump(
        {"model": OPENAI_MODEL, "harness": "codex"}))
    res = _start(ctl, "tst-cx-bill-ok", llm="cxb")
    assert res.returncode == 0, res.stderr
    billing = _job_yaml(ctl, "tst-cx-bill-ok")["billing"]
    assert billing["harness"] == "codex" and billing["basis"] == "metered"


def test_profile_billing_subscription_on_codex_is_refused(ctl):
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cxs.yaml").write_text(yaml.safe_dump(
        {"model": OPENAI_MODEL, "harness": "codex",
         "billing": {"basis": "subscription"}}))
    with pytest.raises(llm_profiles.ProfileError) as err:
        llm_profiles.resolve_profile("cxs", ctl.registry)
    assert "contradicts harness 'codex'" in str(err.value)


def test_billing_for_codex_is_metered_and_not_forced():
    assert "codex" not in HARNESS_FORCED_BASIS
    cfg = JobConfig(billing={"payer": "gw", "basis": "metered",
                             "profile": "cx", "harness": "codex",
                             "subscription_payer": "claude-code subscription"})
    assert cfg.billing_for("codex") == ("gw", "metered")
    other = JobConfig(billing={"payer": "p", "basis": "metered",
                               "profile": "pi-prof", "harness": "pi",
                               "subscription_payer": "claude-code subscription"})
    assert other.billing_for("codex") == ("pi-prof", "metered")
    assert other.billing_for("claude-code")[1] == "subscription"


def test_a_hand_edited_subscription_basis_on_codex_bills_metered():
    from ralphd.engine.config import normalize_billing
    billing = normalize_billing({"payer": "gw", "basis": "subscription",
                                 "profile": "cx", "harness": "codex"})
    assert billing["basis"] == "metered"
    assert JobConfig(billing=billing).billing_for("codex") == ("gw", "metered")
    raw = dict(billing, basis="subscription")
    assert JobConfig(billing=raw).billing_for("codex") == ("gw", "metered")
