"""Black-box tests for `ralphctl config get/set` (PRD req 25, config part).

Reuses tests/test_cli_docker.py's `Ctl` runner (real ralphctl subprocess,
stub recording docker, temp registry) so it can inspect the recorded
`docker run` argv and job.yaml without importing any CLI internals.
"""

from __future__ import annotations

import json

import pytest
import yaml
from test_cli_docker import Ctl, docker_run_argv


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _config_yaml(ctl: Ctl) -> dict:
    p = ctl.registry / "config.yaml"
    if not p.is_file():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def _job_yaml(ctl: Ctl, run_id: str) -> dict:
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, f"expected one job.yaml for {run_id}, found {matches}"
    return yaml.safe_load(matches[0].read_text())


def test_set_persists_to_registry_config_yaml(ctl):
    res = ctl.run("config", "set", "image", "myimage:test")
    assert res.returncode == 0, res.stderr
    assert _config_yaml(ctl)["image"] == "myimage:test"


def test_get_prints_the_set_value(ctl):
    ctl.run("config", "set", "on_complete", "exit")
    res = ctl.run("config", "get", "on_complete")
    assert res.returncode == 0, res.stderr
    assert "exit" in res.stdout


def test_get_unset_key_reports_unset_not_an_error(ctl):
    res = ctl.run("config", "get", "default_llm_profile")
    assert res.returncode == 0, res.stderr
    assert "unset" in res.stdout


def test_get_json_shape(ctl):
    ctl.run("config", "set", "image", "myimage:test")
    res = ctl.run("--json", "config", "get", "image")
    assert res.returncode == 0, res.stderr
    obj = json.loads(res.stdout)
    assert obj == {"key": "image", "value": "myimage:test"}


def test_unknown_key_exits_2_on_get_and_set(ctl):
    res = ctl.run("config", "get", "not-a-real-key")
    assert res.returncode == 2, res.stderr
    assert "not-a-real-key" in res.stderr

    res = ctl.run("config", "set", "not-a-real-key", "x")
    assert res.returncode == 2, res.stderr
    assert "not-a-real-key" in res.stderr


def test_set_on_complete_validates_choices(ctl):
    res = ctl.run("config", "set", "on_complete", "bogus")
    assert res.returncode == 2, res.stderr
    assert "idle" in res.stderr and "exit" in res.stderr
    assert _config_yaml(ctl).get("on_complete") is None


def test_set_does_not_clobber_other_keys(ctl):
    ctl.run("config", "set", "image", "myimage:test")
    ctl.run("config", "set", "on_complete", "exit")
    cfg = _config_yaml(ctl)
    assert cfg["image"] == "myimage:test"
    assert cfg["on_complete"] == "exit"


def test_start_uses_registry_default_image(ctl):
    ctl.run("config", "set", "image", "myimage:registry-default")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-image")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)
    assert argv[-1] == "myimage:registry-default"


def test_explicit_image_flag_overrides_registry_default(ctl):
    ctl.run("config", "set", "image", "myimage:registry-default")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-image-override",
                  "--image", "explicit:override")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)
    assert argv[-1] == "explicit:override"


def test_start_uses_registry_default_on_complete(ctl):
    ctl.run("config", "set", "on_complete", "exit")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-on-complete")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "cfg-on-complete")
    assert job["on_complete"] == "exit"


def test_start_default_on_complete_is_exit(ctl):
    """Product default (no --on-complete flag, no registry config, no
    template) is `exit` -- `idle` is an explicit debugging opt-in (task 010).
    """
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-on-complete-default")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "cfg-on-complete-default")
    assert job["on_complete"] == "exit"


def test_registry_default_on_complete_idle_overrides_product_default(ctl):
    """A registry-wide `on_complete: idle` still wins over the hardcoded
    `exit` product default (precedence: CLI flag > template > registry >
    hardcoded default is preserved after task 010's default flip).
    """
    ctl.run("config", "set", "on_complete", "idle")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-on-complete-registry-idle")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "cfg-on-complete-registry-idle")
    assert job["on_complete"] == "idle"


def test_set_network_persists_to_registry_config_yaml(ctl):
    res = ctl.run("config", "set", "network", "host")
    assert res.returncode == 0, res.stderr
    assert _config_yaml(ctl)["network"] == "host"


def test_start_uses_registry_default_network(ctl):
    ctl.run("config", "set", "network", "tailnet0")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-network")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)
    i = argv.index("--network")
    assert argv[i + 1] == "tailnet0"
    host_json = json.loads(
        (ctl.registry / "runs" / "cfg-network" / "host.json").read_text())
    assert host_json["network"] == "tailnet0"


def test_explicit_network_flag_overrides_registry_default(ctl):
    ctl.run("config", "set", "network", "tailnet0")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-network-override",
                  "--network", "host")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)
    i = argv.index("--network")
    assert argv[i + 1] == "host"


def test_template_still_overrides_registry_default(ctl):
    ctl.run("config", "set", "on_complete", "exit")
    tdir = ctl.registry / "templates" / "quick"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text("on_complete: idle\n")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-template-wins",
                  "--template", "quick")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "cfg-template-wins")
    assert job["on_complete"] == "idle"


# ---------------------------------------------------------------------------
# Task 043 (#52): the four keys that decide which models a job runs. One test
# per rung of the chain, each asserting on the `job.yaml` the run actually
# gets (the file the engine reads and `resume` replays), not on argparse.
# ---------------------------------------------------------------------------

MODEL_KEYS = ("model", "thinking")


def test_the_two_model_keys_are_settable_and_readable(ctl):
    for key, value in zip(MODEL_KEYS, ("prov/strong", "high")):
        assert ctl.run("config", "set", key, value).returncode == 0
        res = ctl.run("config", "get", key)
        assert res.returncode == 0, res.stderr
        assert value in res.stdout
    cfg = _config_yaml(ctl)
    assert cfg["model"] == "prov/strong" and cfg["thinking"] == "high"


def test_start_uses_the_registry_defaults_for_the_two_model_keys(ctl):
    """Rung 3: registry beats the hardcoded fallback, in job.yaml."""
    ctl.run("config", "set", "model", "prov/registry-strong")
    ctl.run("config", "set", "thinking", "medium")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-models-registry")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "cfg-models-registry")
    assert job["model"] == "prov/registry-strong"
    assert job["thinking"] == "medium"


def test_a_template_beats_the_registry_default_for_the_two_model_keys(ctl):
    """Rung 2."""
    for key, value in zip(MODEL_KEYS, ("prov/reg", "low")):
        ctl.run("config", "set", key, value)
    tdir = ctl.registry / "templates" / "models"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text("model: prov/tmpl\nthinking: high\n")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-models-template", "--template", "models")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "cfg-models-template")
    assert job["model"] == "prov/tmpl" and job["thinking"] == "high"


def test_explicit_flags_beat_a_template_and_the_registry_default(ctl):
    """Rung 1: the flag wins over both lower rungs at once."""
    for key, value in zip(MODEL_KEYS, ("prov/reg", "low")):
        ctl.run("config", "set", key, value)
    tdir = ctl.registry / "templates" / "models"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text("model: prov/tmpl\nthinking: high\n")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-models-flag", "--template", "models",
                  "--model", "prov/flag", "--thinking", "none")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "cfg-models-flag")
    assert job["model"] == "prov/flag" and job["thinking"] == "none"


def test_with_nothing_set_anywhere_the_hardcoded_fallbacks_still_apply(ctl):
    """Rung 4, and the reason `_TEMPLATE_SCALAR_FIELDS["model"]` is None:
    ralphd ships no model ref of its own (pi's own default applies), so an
    unset `model`/`thinking` must stay out of job.yaml entirely rather than
    acquiring some host's route.
    """
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-models-hardcoded")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "cfg-models-hardcoded")
    assert "model" not in job and "thinking" not in job
    # Task 006 (#60 pillar 4 cleanup): the shipped default used to be a
    # third literal here (`model_strategy`); that whole knob is retired, so
    # an unconfigured run's job.yaml carries no strategy key of any kind any
    # more -- there is no fallback tier without an explicit `models`/
    # `model_overrides` entry.
    assert "model_strategy" not in job and "fast_model" not in job


def test_set_model_strategy_is_removed_with_a_pointer_message(ctl):
    """Task 006: retired, not a no-op -- exits 2 naming the replacement
    (`models:`/`model_overrides:` in job.yaml), never silently accepted."""
    res = ctl.run("config", "set", "model_strategy", "balanced")
    assert res.returncode == 2, res.stderr
    assert "model_strategy is removed" in res.stderr
    assert "models:" in res.stderr
    assert _config_yaml(ctl).get("model_strategy") is None


def test_set_fast_model_is_removed_with_a_pointer_message(ctl):
    res = ctl.run("config", "set", "fast_model", "prov/fast")
    assert res.returncode == 2, res.stderr
    assert "fast_model is removed" in res.stderr
    assert "models:" in res.stderr
    assert _config_yaml(ctl).get("fast_model") is None


def test_start_model_strategy_flag_is_removed_with_a_pointer_message(ctl):
    """The `start` flag half of the same retirement -- exits 2 rather than
    writing `model_strategy` into job.yaml (the old no-op-shaped behaviour)."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-models-strategy-flag",
                  "--model-strategy", "balanced")
    assert res.returncode == 2, res.stderr
    assert "--model-strategy is removed" in res.stderr
    assert "models:" in res.stderr


def test_start_fast_model_flag_is_removed_with_a_pointer_message(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "cfg-models-fast-flag",
                  "--fast-model", "prov/fast")
    assert res.returncode == 2, res.stderr
    assert "--fast-model is removed" in res.stderr
    assert "models:" in res.stderr


def test_model_and_thinking_are_free_form(ctl):
    """Unlike the retired `model_strategy`, these are provider/pi strings
    ralphd cannot enumerate -- validating them would break every host whose
    gateway ralphd has never heard of (same reason `image` takes any string).
    """
    for key in ("model", "thinking"):
        res = ctl.run("config", "set", key, "anything-at-all/x")
        assert res.returncode == 0, res.stderr
        assert _config_yaml(ctl)[key] == "anything-at-all/x"
