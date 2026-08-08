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
def ctl(tmp_path):
    return Ctl(tmp_path)


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
