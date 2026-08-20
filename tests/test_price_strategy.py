"""The `price_strategy` knob (task 010, #14).

Task 009 shipped a built-in AWS Bedrock rate table; nothing consulted it yet.
This task adds the single switch that decides whether it may be consulted at
all, through every layer a ralphd knob has to exist in:

* the LLM profile (`price_strategy:` in `<registry>/llm-profiles/<name>.yaml`
  -- a gateway profile is what knows which rate table bills its routes),
* `JobConfig` (the default, and the only place the default `none` is spelled),
* a `RALPHD_PRICE_STRATEGY` env override (a single run, no job.yaml edit),
* `GET /config`'s effective view (`priceStrategy`),
* `ralphctl start --price-strategy`, persisted into `job.yaml` so a later
  `ralphctl resume` re-runs with the strategy the run started with.

The load-bearing invariants asserted below:

* `none` is the shipped default, so ralphd never derives a number nobody asked
  for; a run that says nothing about pricing keeps the key out of `job.yaml`
  entirely and inherits `engine.config.DEFAULT_PRICE_STRATEGY`.
* An unknown/misspelled strategy degrades to `none` with a warning, never a
  crash and never a silently-different behaviour -- and the *effective* value
  is what `GET /config` reports, so the fallback is observable.
* Nothing here computes money: task 010 only decides "may the built-in table
  be consulted", so the knob's presence changes no cost field yet (task 011
  wires the derivation). A test pins that so the two tasks stay separable.
"""

from __future__ import annotations

import json
import logging

import pytest
import yaml
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_e2e import EngineProc

from ralphd.cli import llm_profiles
from ralphd.engine.config import (
    DEFAULT_PRICE_STRATEGY,
    PRICE_STRATEGIES,
    JobConfig,
    normalize_price_strategy,
)

__all__ = ["ctl", "unix_sock"]


def _write_job(path, **fields):
    path.write_text("".join(f"{k}: {json.dumps(v)}\n" for k, v in fields.items()))
    return path


def _write_profile(ctl: Ctl, name: str, doc: dict):
    """An `<registry>/llm-profiles/<name>.yaml` for the stub-docker `Ctl`
    (test_cli_llm_profiles' richer harness is not needed here -- these tests
    only care about one scalar field)."""
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


# --------------------------------------------------------------------------
# JobConfig: the default, the accepted values, the fallback
# --------------------------------------------------------------------------

def test_the_shipped_default_is_none():
    assert DEFAULT_PRICE_STRATEGY == "none"
    assert JobConfig().price_strategy == "none"
    assert set(PRICE_STRATEGIES) == {"none", "aws"}


def test_job_yaml_can_select_aws(tmp_path):
    cfg = JobConfig.load(_write_job(tmp_path / "job.yaml", run_id="r",
                                    price_strategy="aws"))
    assert cfg.price_strategy == "aws"


def test_a_job_yaml_without_the_key_keeps_the_default(tmp_path):
    cfg = JobConfig.load(_write_job(tmp_path / "job.yaml", run_id="r"))
    assert cfg.price_strategy == DEFAULT_PRICE_STRATEGY
    # ... and so does a run with no job.yaml at all
    assert JobConfig.load(tmp_path / "missing.yaml").price_strategy == "none"


def test_case_and_whitespace_are_tolerated(tmp_path):
    cfg = JobConfig.load(_write_job(tmp_path / "job.yaml", price_strategy="  AWS "))
    assert cfg.price_strategy == "aws"


def test_an_unknown_strategy_degrades_to_none_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="ralphd.config"):
        cfg = JobConfig.load(_write_job(tmp_path / "job.yaml",
                                        price_strategy="bedrock-ish"))
    assert cfg.price_strategy == "none"
    assert "bedrock-ish" in caplog.text and "price_strategy" in caplog.text


def test_a_directly_constructed_config_is_normalised_too():
    # `== "aws"` checks live downstream; a JobConfig built in code (not via
    # load) must not be able to carry an unknown value past them unnoticed.
    assert JobConfig(price_strategy="AWS").price_strategy == "aws"
    assert JobConfig(price_strategy="nonsense").price_strategy == "none"
    assert JobConfig(price_strategy=None).price_strategy == "none"


def test_normalize_price_strategy_is_the_one_coercion():
    assert normalize_price_strategy("aws") == "aws"
    assert normalize_price_strategy("") == "none"
    assert normalize_price_strategy(None) == "none"
    assert normalize_price_strategy(7) == "none"


# --------------------------------------------------------------------------
# RALPHD_PRICE_STRATEGY: one run, no job.yaml edit
# --------------------------------------------------------------------------

def test_env_override_engages_aws_without_a_job_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("RALPHD_PRICE_STRATEGY", "aws")
    assert JobConfig.load(tmp_path / "missing.yaml").price_strategy == "aws"


def test_env_override_beats_job_yaml_in_both_directions(tmp_path, monkeypatch):
    path = _write_job(tmp_path / "job.yaml", price_strategy="aws")
    monkeypatch.setenv("RALPHD_PRICE_STRATEGY", "none")
    assert JobConfig.load(path).price_strategy == "none"
    monkeypatch.setenv("RALPHD_PRICE_STRATEGY", "aws")
    assert JobConfig.load(_write_job(tmp_path / "j2.yaml",
                                     price_strategy="none")).price_strategy == "aws"


def test_a_junk_env_override_falls_back_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("RALPHD_PRICE_STRATEGY", "aws!!")
    assert JobConfig.load(_write_job(tmp_path / "job.yaml",
                                     price_strategy="aws")).price_strategy == "none"


# --------------------------------------------------------------------------
# GET /config's effective view
# --------------------------------------------------------------------------

def test_effective_reports_the_strategy(tmp_path):
    assert JobConfig().effective()["priceStrategy"] == "none"
    cfg = JobConfig.load(_write_job(tmp_path / "job.yaml", price_strategy="aws"))
    assert cfg.effective()["priceStrategy"] == "aws"


def test_effective_reports_the_behaviour_not_the_typo(tmp_path):
    cfg = JobConfig.load(_write_job(tmp_path / "job.yaml", price_strategy="AWZ"))
    assert cfg.effective()["priceStrategy"] == "none"


def test_the_strategy_is_independent_of_the_operator_pricing_map(tmp_path):
    # An operator `pricing:` map always applies; `price_strategy` only decides
    # whether the *built-in* table may answer. The two must not be entangled.
    cfg = JobConfig.load(_write_job(
        tmp_path / "job.yaml", price_strategy="aws",
        pricing={"models": {"openai/gpt-5": {"input": 1.0, "output": 2.0}}}))
    eff = cfg.effective()
    assert eff["priceStrategy"] == "aws"
    assert eff["pricing"]["models"]["openai/gpt-5"]["input"] == 1.0
    assert JobConfig().effective()["pricing"] is None


@pytest.fixture
def price_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "price-strategy-e2e", "iterations": 2,
                    "max_approaches": 1, "on_complete": "idle"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def test_get_config_serves_the_strategy_from_a_live_engine(price_engine):
    e = price_engine({"price_strategy": "aws"})
    e.wait_api()
    status, doc = e.api("GET", "/config")
    assert status == 200
    assert doc["priceStrategy"] == "aws"


def test_get_config_serves_none_for_a_run_that_never_configured_one(price_engine):
    e = price_engine()
    e.wait_api()
    _, doc = e.api("GET", "/config")
    assert doc["priceStrategy"] == "none"


def test_the_env_override_reaches_get_config(price_engine):
    e = price_engine(stub_env={"RALPHD_PRICE_STRATEGY": "aws"})
    e.wait_api()
    _, doc = e.api("GET", "/config")
    assert doc["priceStrategy"] == "aws"


# --------------------------------------------------------------------------
# LLM profile field
# --------------------------------------------------------------------------

def test_a_profile_can_declare_the_strategy(ctl: Ctl):
    _write_profile(ctl, "gw", {"model": "aigw-openai/gpt-5", "price_strategy": "aws"})
    resolved = llm_profiles.resolve_profile("gw", ctl.registry, host_env={})
    assert resolved["price_strategy"] == "aws"


def test_a_profile_without_the_field_declares_nothing(ctl: Ctl):
    _write_profile(ctl, "plain", {"model": "acme/big"})
    # None (not "none") = "this profile has no opinion", so a template or
    # registry default is still free to decide.
    assert llm_profiles.resolve_profile("plain", ctl.registry,
                                        host_env={})["price_strategy"] is None


def test_a_profile_with_a_bogus_strategy_is_a_profile_error(ctl: Ctl):
    _write_profile(ctl, "bad", {"price_strategy": "bedrock"})
    with pytest.raises(llm_profiles.ProfileError) as e:
        llm_profiles.resolve_profile("bad", ctl.registry, host_env={})
    assert "price_strategy" in str(e.value) and "bad" in str(e.value)


def test_llm_show_prints_the_strategy(ctl: Ctl):
    _write_profile(ctl, "gw", {"price_strategy": "aws"})
    res = ctl.run("llm", "show", "gw")
    assert res.returncode == 0, res.stderr
    assert "price_strategy: aws" in res.stdout
    res = ctl.run("--json", "llm", "show", "gw")
    assert json.loads(res.stdout)["price_strategy"] == "aws"


# --------------------------------------------------------------------------
# `ralphctl start`: job.yaml persistence and precedence
# --------------------------------------------------------------------------

def _job_yaml(ctl: Ctl, run_id: str) -> dict:
    return yaml.safe_load((ctl.registry / "configs" / run_id / "job.yaml").read_text())


def test_start_persists_the_flag_into_job_yaml(ctl: Ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--price-strategy", "aws", "--run-id", "tst-ps-flag")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-ps-flag")["price_strategy"] == "aws"
    cfg = JobConfig.load(ctl.registry / "configs" / "tst-ps-flag" / "job.yaml")
    assert cfg.price_strategy == "aws"


def test_start_without_the_flag_writes_no_key_at_all(ctl: Ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-ps-absent")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "tst-ps-absent")
    assert "price_strategy" not in job  # the default lives in the engine alone
    assert JobConfig.load(
        ctl.registry / "configs" / "tst-ps-absent" / "job.yaml").price_strategy == "none"


def test_start_rejects_an_unknown_strategy(ctl: Ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--price-strategy", "bedrock", "--run-id", "tst-ps-bad")
    assert res.returncode != 0
    assert "price-strategy" in res.stderr
    assert not (ctl.registry / "configs" / "tst-ps-bad").exists()


def test_start_takes_the_strategy_from_the_llm_profile(ctl: Ctl):
    _write_profile(ctl, "gw", {"model": "aigw-openai/gpt-5", "price_strategy": "aws"})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "gw",
                  "--run-id", "tst-ps-profile")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-ps-profile")["price_strategy"] == "aws"


def test_an_explicit_flag_beats_the_profile(ctl: Ctl):
    _write_profile(ctl, "gw", {"price_strategy": "aws"})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "gw",
                  "--price-strategy", "none", "--run-id", "tst-ps-flagwins")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-ps-flagwins")["price_strategy"] == "none"


def test_the_registry_default_applies_when_no_flag_is_given(ctl: Ctl):
    (ctl.registry / "config.yaml").write_text(yaml.safe_dump({"price_strategy": "aws"}))
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-ps-registry")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-ps-registry")["price_strategy"] == "aws"


def test_config_set_validates_and_persists_the_registry_default(ctl: Ctl):
    bad = ctl.run("config", "set", "price_strategy", "bedrock")
    assert bad.returncode == 2, bad.stderr
    assert not (ctl.registry / "config.yaml").exists()
    ok = ctl.run("config", "set", "price_strategy", "aws")
    assert ok.returncode == 0, ok.stderr
    assert yaml.safe_load((ctl.registry / "config.yaml").read_text()) == \
        {"price_strategy": "aws"}
    got = ctl.run("--json", "config", "get", "price_strategy")
    assert json.loads(got.stdout) == {"key": "price_strategy", "value": "aws"}


def test_a_template_beats_the_registry_default(ctl: Ctl):
    (ctl.registry / "config.yaml").write_text(yaml.safe_dump({"price_strategy": "aws"}))
    tdir = ctl.registry / "templates" / "cheap"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text(yaml.safe_dump({"price_strategy": "none"}))
    (tdir / "prd.md").write_text("# T\n\nDo it.\n")
    res = ctl.run("start", "--template", "cheap", "--llm", "none",
                  "--run-id", "tst-ps-template")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-ps-template")["price_strategy"] == "none"


# --------------------------------------------------------------------------
# resume replay
# --------------------------------------------------------------------------

def test_resume_replays_the_strategy_the_run_started_with(ctl: Ctl):
    start = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                    "--price-strategy", "aws", "--run-id", "tst-ps-resume")
    assert start.returncode == 0, start.stderr
    cdir = ctl.registry / "configs" / "tst-ps-resume"
    (ctl.registry / "runs" / "tst-ps-resume" / "status.json").write_text(
        json.dumps({"state": "failed"}))
    ctl.log.unlink(missing_ok=True)
    res = ctl.run("resume", "tst-ps-resume")
    assert res.returncode == 0, res.stderr
    # the resumed container mounts the same config dir, and the strategy is
    # still in it -- never re-derived from the resuming shell's environment.
    argv = next(a for a in ctl.recorded() if a[:2] == ["run", "-d"])
    assert f"{cdir}:/config:ro" in argv
    assert JobConfig.load(cdir / "job.yaml").price_strategy == "aws"


def test_an_iterations_topup_on_resume_does_not_drop_the_strategy(ctl: Ctl):
    _seed_run(ctl, "tst-ps-topup")
    cdir = ctl.registry / "configs" / "tst-ps-topup"
    job = yaml.safe_load((cdir / "job.yaml").read_text())
    # job.yaml is the `key: <json>`-per-line format cmd_start writes, not
    # free-form YAML -- write it back in exactly that shape.
    _write_job(cdir / "job.yaml", **{**job, "price_strategy": "aws"})
    res = ctl.run("resume", "tst-ps-topup", "--iterations", "+5")
    assert res.returncode == 0, res.stderr
    rewritten = yaml.safe_load((cdir / "job.yaml").read_text())
    assert rewritten["iterations"] == 10
    assert rewritten["price_strategy"] == "aws"
    assert JobConfig.load(cdir / "job.yaml").price_strategy == "aws"


# --------------------------------------------------------------------------
# task 010 is a knob, not a behaviour change: nothing derives money yet
# --------------------------------------------------------------------------

def test_selecting_aws_does_not_by_itself_price_anything(tmp_path):
    """Task 011 wires the derivation; until then `aws` must change no cost
    field. Pinning that keeps the two tasks (and their tests) separable: if
    this starts failing, it is because 011 landed, and this test should then
    be replaced by 011's own assertions rather than deleted quietly."""
    from ralphd.engine.pricing import PricingMap

    cfg = JobConfig.load(_write_job(tmp_path / "job.yaml", price_strategy="aws"))
    # the knob does NOT smuggle the built-in table into the operator map
    assert cfg.pricing == {}
    assert PricingMap.from_config(cfg.pricing) is None
    assert cfg.effective()["pricing"] is None
