"""Choosing a harness per hat: profile `harness:` < registry `harnesses:` <
template `harnesses:` < `--harness-for`, persisted in job.yaml, replayed on
resume, validated at `start`. In-process `ralphctl` against the recording
stub docker; no container, no network."""

from __future__ import annotations

import pytest
import yaml
from test_cli_docker import Ctl
from test_cli_resume_llm_wiring import _stop_container

from ralphd.cli import llm_profiles
from ralphd.engine.config import HARNESSES, MODEL_HATS, JobConfig


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _profile(ctl, harness="claude-code", name="sub"):
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"model": "anthropic/claude-sonnet-5"}
    if harness is not None:
        doc["harness"] = harness
    (d / f"{name}.yaml").write_text(yaml.safe_dump(doc))


def _registry_harnesses(ctl, table):
    (ctl.registry / "config.yaml").write_text(yaml.safe_dump({"harnesses": table}))


def _template(ctl, table, name="t"):
    d = ctl.registry / "templates" / name
    d.mkdir(parents=True)
    (d / "job.yaml").write_text(yaml.safe_dump({"harnesses": table}))


def _cdir(ctl, run_id):
    return ctl.registry / "configs" / run_id


def _harnesses(ctl, run_id) -> dict:
    p = _cdir(ctl, run_id) / "job.yaml"
    return yaml.safe_load(p.read_text())["harnesses"]


def _start(ctl, run_id, *extra, llm=None):
    argv = ["start", "--prd", str(ctl.prd), "--run-id", run_id, *extra]
    if llm:
        argv += ["--llm", llm]
    return ctl.run(*argv)


def test_default_run_writes_all_pi_for_every_hat(ctl):
    res = _start(ctl, "tst-h-default")
    assert res.returncode == 0, res.stderr
    assert _harnesses(ctl, "tst-h-default") == {h: "pi" for h in MODEL_HATS}
    assert len(MODEL_HATS) == 9


def test_job_yaml_key_set_is_pre_wave_plus_harnesses(ctl):
    res = _start(ctl, "tst-h-keys")
    assert res.returncode == 0, res.stderr
    doc = yaml.safe_load((_cdir(ctl, "tst-h-keys") / "job.yaml").read_text())
    assert doc["harnesses"] == {h: "pi" for h in MODEL_HATS}
    # every other key is a known JobConfig field, so nothing leaks to `extra`
    cfg = JobConfig.load(_cdir(ctl, "tst-h-keys") / "job.yaml")
    assert cfg.harnesses == doc["harnesses"]
    assert all(cfg.harness_for(h) == "pi" for h in MODEL_HATS)


def test_profile_harness_applies_to_every_hat(ctl):
    _profile(ctl)
    res = _start(ctl, "tst-h-profile", llm="sub")
    assert res.returncode == 0, res.stderr
    assert _harnesses(ctl, "tst-h-profile") == {h: "claude-code" for h in MODEL_HATS}


def test_registry_beats_profile(ctl):
    _profile(ctl)
    _registry_harnesses(ctl, {"review": "pi"})
    res = _start(ctl, "tst-h-reg", llm="sub")
    assert res.returncode == 0, res.stderr
    got = _harnesses(ctl, "tst-h-reg")
    assert got["review"] == "pi"
    assert got["worker"] == "claude-code"


def test_template_beats_registry(ctl):
    _registry_harnesses(ctl, {"worker": "claude-code", "verify": "claude-code"})
    _template(ctl, {"worker": "pi"})
    res = _start(ctl, "tst-h-tpl", "--template", "t")
    assert res.returncode == 0, res.stderr
    got = _harnesses(ctl, "tst-h-tpl")
    assert got["worker"] == "pi"
    assert got["verify"] == "claude-code"


def test_flag_beats_template(ctl):
    _template(ctl, {"worker": "claude-code", "review": "claude-code"})
    res = _start(ctl, "tst-h-flag", "--template", "t",
                 "--harness-for", "worker=pi", "--harness-for", "planning=claude-code")
    assert res.returncode == 0, res.stderr
    got = _harnesses(ctl, "tst-h-flag")
    assert got["worker"] == "pi"
    assert got["review"] == "claude-code"
    assert got["planning"] == "claude-code"


def test_unknown_profile_harness_is_a_profile_error_at_start(ctl, tmp_path):
    _profile(ctl, harness="nonsense", name="bad")
    res = _start(ctl, "tst-h-badprof", llm="bad")
    assert res.returncode == 2
    assert "bad" in res.stderr and "nonsense" in res.stderr
    assert "pi|claude-code|codex" in res.stderr
    with pytest.raises(llm_profiles.ProfileError):
        llm_profiles.resolve_profile("bad", ctl.registry)


@pytest.mark.parametrize("spec", ["nohat=pi", "worker=nonsense", "worker", "=pi", "worker="])
def test_bad_harness_for_exits_2(ctl, spec):
    res = _start(ctl, "tst-h-badflag", "--harness-for", spec)
    assert res.returncode == 2, res.stderr
    assert "--harness-for" in res.stderr


def test_unknown_registry_or_template_entry_exits_2(ctl):
    _registry_harnesses(ctl, {"worker": "nonsense"})
    res = _start(ctl, "tst-h-badreg")
    assert res.returncode == 2
    assert "nonsense" in res.stderr


def test_resume_replays_harnesses_unchanged(ctl):
    res = _start(ctl, "tst-h-resume", "--harness-for", "worker=claude-code")
    assert res.returncode == 0, res.stderr
    before = _harnesses(ctl, "tst-h-resume")
    _stop_container(ctl)
    res = ctl.run("resume", "tst-h-resume", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-h-resume", "STUB_DOCKER_RUNNING": ""})
    assert res.returncode == 0, res.stderr
    assert _harnesses(ctl, "tst-h-resume") == before
    assert before["worker"] == "claude-code"


def test_docs_job_yaml_shows_harnesses_unmasked(ctl):
    res = _start(ctl, "tst-h-docs", "--harness-for", "worker=claude-code")
    assert res.returncode == 0, res.stderr
    res = ctl.run("docs", "tst-h-docs", "job.yaml")
    assert res.returncode == 0, res.stderr
    assert "harnesses:" in res.stdout
    assert '"worker": "claude-code"' in res.stdout


def test_provisioning_lines_name_each_hats_harness(ctl):
    from ralphd.engine.state import format_provisioning_lines
    res = _start(ctl, "tst-h-status", "--harness-for", "worker=claude-code")
    assert res.returncode == 0, res.stderr
    cfg = JobConfig.load(_cdir(ctl, "tst-h-status") / "job.yaml")
    lines = format_provisioning_lines(cfg.provisioning_table())
    assert any(l.startswith("worker") and l.endswith("[claude-code]") for l in lines)
    assert any(l.startswith("planning") and l.endswith("[pi]") for l in lines)
    assert cfg.effective()["model"]["harnesses"]["worker"] == "claude-code"


def test_harnesses_constant():
    # codex joined in v0.33; tests/test_codex_harness_registration.py::
    # test_codex_is_the_third_harness covers the full tuple.
    assert HARNESSES == ("pi", "claude-code", "codex")
