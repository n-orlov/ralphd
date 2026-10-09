"""`ralphctl doctor` `codex_runtime` check, and `ralphctl llm test` on the
codex harness.

The image probe is monkeypatched where the check's verdict is the subject (the
recording stub docker answers `run` with a fake container id, which is not a
probe answer). The probe's own wrapper output is checked against the stub
`codex` (`tests/stub-codex/codex`); `llm test` runs the real
`ralphd-engine --llm-ping` against the same stub. No network, no real Codex.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ralphd.cli import main as cli
from ralphd.engine.config import CODEX_MIN_VERSION
from tests.conftest import call_ralphctl_in_process
from tests.test_llm_test_real_path import PINNED_IMAGE, Ctl, _ping_args

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
STUB_CODEX = REPO / "tests" / "stub-codex" / "codex"
FAKE_TOKEN = "fake-aigw-bearer-FOR-TESTS-ONLY"
FAKE_BASE_URL = "https://aigw.invalid/api/openai/v1"


def _env(tmp_path):
    return {**os.environ, "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(tmp_path / "registry"),
            "STUB_DOCKER_LOG": str(tmp_path / "docker.jsonl"),
            "STUB_DOCKER_INSPECT_OK": "1"}


def _doctor(tmp_path, capsys, monkeypatch, probe, *, codex_job=False, text=False):
    reg = tmp_path / "registry"
    reg.mkdir(exist_ok=True)
    if codex_job:
        (reg / "config.yaml").write_text(
            yaml.safe_dump({"harnesses": {"worker": "codex"}}))
    monkeypatch.setattr(cli, "_probe_codex_runtime", probe)
    argv = ["doctor"] if text else ["--json", "doctor"]
    res = call_ralphctl_in_process(argv, env=_env(tmp_path), capsys=capsys)
    return res, (res.stdout if text else json.loads(res.stdout))


def _ok(version):
    return lambda ref: {"cli": version, "cliPath": "/usr/bin/codex", "ok": True}


def test_a_current_codex_passes_the_check(tmp_path, capsys, monkeypatch):
    _, doc = _doctor(tmp_path, capsys, monkeypatch, _ok("0.156.1"))
    assert doc["checks"]["codex_runtime"] is True
    assert doc["ok"] == all(doc["checks"].values())


def test_a_newer_codex_passes_the_check(tmp_path, capsys, monkeypatch):
    _, doc = _doctor(tmp_path, capsys, monkeypatch, _ok("0.200.0"))
    assert doc["checks"]["codex_runtime"] is True


def test_the_minimum_is_0_156_1():
    assert CODEX_MIN_VERSION == "0.156.1"


def test_an_old_codex_fails_when_a_job_uses_codex(tmp_path, capsys, monkeypatch):
    res, doc = _doctor(tmp_path, capsys, monkeypatch,
                       lambda ref: {"cli": "0.155.0", "ok": False},
                       codex_job=True)
    assert doc["checks"]["codex_runtime"] is False
    assert doc["ok"] is False
    assert res.returncode == 1


def test_the_text_report_names_the_minimum_and_the_found_version(
        tmp_path, capsys, monkeypatch):
    res, out = _doctor(tmp_path, capsys, monkeypatch,
                       lambda ref: {"cli": "0.155.0", "ok": False},
                       codex_job=True, text=True)
    assert res.returncode == 1
    assert "✗ codex_runtime" in out
    assert CODEX_MIN_VERSION in out and "0.155.0" in out


def test_a_missing_codex_fails_when_a_job_uses_codex(tmp_path, capsys, monkeypatch):
    res, doc = _doctor(
        tmp_path, capsys, monkeypatch,
        lambda ref: {"cli": None, "ok": False, "error": "Codex CLI not found at codex"},
        codex_job=True)
    assert doc["checks"]["codex_runtime"] is False
    assert res.returncode == 1


def test_an_old_codex_is_advisory_when_no_job_uses_codex(
        tmp_path, capsys, monkeypatch):
    res, out = _doctor(tmp_path, capsys, monkeypatch,
                       lambda ref: {"cli": "0.155.0", "ok": False}, text=True)
    assert "! codex_runtime:" in out and "advisory" in out
    assert "✗ codex_runtime" not in out
    (tmp_path / "j").mkdir()
    _, doc = _doctor(tmp_path / "j", capsys, monkeypatch,
                     lambda ref: {"cli": "0.155.0", "ok": False})
    assert "codex_runtime" not in doc["checks"]
    assert doc["ok"] == all(doc["checks"].values())


def test_a_missing_codex_is_advisory_when_no_job_uses_codex(
        tmp_path, capsys, monkeypatch):
    _, out = _doctor(
        tmp_path, capsys, monkeypatch,
        lambda ref: {"cli": None, "ok": False, "error": "Codex CLI not found at codex"},
        text=True)
    assert "! codex_runtime:" in out and "Codex CLI not found" in out
    assert "advisory" in out


@pytest.mark.parametrize("where", ["template", "run", "profile"])
def test_each_registry_source_of_a_codex_hat_makes_a_failure(
        tmp_path, capsys, monkeypatch, where):
    reg = tmp_path / "registry"
    reg.mkdir()
    if where == "template":
        (reg / "templates" / "t").mkdir(parents=True)
        (reg / "templates" / "t" / "job.yaml").write_text(
            yaml.safe_dump({"harnesses": {"reviewer": "codex"}}))
    elif where == "run":
        (reg / "runs" / "r1").mkdir(parents=True)
        (reg / "runs" / "r1" / "status.json").write_text(
            json.dumps({"state": "running"}))
        (reg / "configs" / "r1").mkdir(parents=True)
        (reg / "configs" / "r1" / "job.yaml").write_text(
            yaml.safe_dump({"harnesses": {"worker": "codex"}}))
    else:
        (reg / "llm-profiles").mkdir()
        (reg / "llm-profiles" / "cx.yaml").write_text(
            yaml.safe_dump({"description": "cx", "harness": "codex",
                            "model": "aigw-openai/openai.gpt-5.6-sol"}))
        (reg / "config.yaml").write_text(
            yaml.safe_dump({"default_llm_profile": "cx"}))
    _, doc = _doctor(tmp_path, capsys, monkeypatch,
                     lambda ref: {"cli": "0.155.0", "ok": False})
    assert doc["checks"]["codex_runtime"] is False


def test_a_finished_run_on_codex_does_not_count_as_a_codex_job(
        tmp_path, capsys, monkeypatch):
    reg = tmp_path / "registry"
    (reg / "runs" / "r1").mkdir(parents=True)
    (reg / "runs" / "r1" / "status.json").write_text(json.dumps({"state": "done"}))
    (reg / "configs" / "r1").mkdir(parents=True)
    (reg / "configs" / "r1" / "job.yaml").write_text(
        yaml.safe_dump({"harnesses": {"worker": "codex"}}))
    _, doc = _doctor(tmp_path, capsys, monkeypatch,
                     lambda ref: {"cli": "0.155.0", "ok": False})
    assert "codex_runtime" not in doc["checks"]


def test_an_unprobeable_image_is_report_only(tmp_path, capsys, monkeypatch):
    _, out = _doctor(tmp_path, capsys, monkeypatch, lambda ref: None,
                     codex_job=True, text=True)
    assert "! codex_runtime: not probed" in out
    assert "✗ codex_runtime" not in out
    (tmp_path / "j").mkdir()
    _, doc = _doctor(tmp_path / "j", capsys, monkeypatch, lambda ref: None,
                     codex_job=True)
    assert "codex_runtime" not in doc["checks"]


# ---- the probe itself
class _Res:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _probe_with(monkeypatch, res):
    calls = []

    def fake_sh(cmd, **kw):
        calls.append(cmd)
        if isinstance(res, Exception):
            raise res
        return res
    monkeypatch.setattr(cli, "sh", fake_sh)
    return cli._probe_codex_runtime("img:x"), calls


def test_the_probe_runs_the_wrapper_with_the_images_python(monkeypatch):
    doc = {"cli": "0.156.1", "cliPath": "/x", "ok": True}
    got, calls = _probe_with(monkeypatch, _Res(0, json.dumps(doc) + "\n"))
    assert got == doc
    argv = calls[0]
    assert argv[argv.index("--entrypoint") + 1] == "python"
    assert argv[-3:] == ["-m", "ralphd.harness.codex", "--probe-version"]
    assert "img:x" in argv


@pytest.mark.parametrize("res", [
    _Res(0, "f" * 64 + "\n"),
    _Res(0, ""),
    _Res(125, "", "Unable to find image 'img:x' locally"),
    OSError("no docker"),
    subprocess.TimeoutExpired("docker", 1),
])
def test_the_probe_is_none_when_nothing_can_be_said(monkeypatch, res):
    got, _ = _probe_with(monkeypatch, res)
    assert got is None


def test_the_probe_reports_an_import_failure_as_an_answer(monkeypatch):
    got, _ = _probe_with(monkeypatch, _Res(
        1, "", "Traceback...\nModuleNotFoundError: No module named 'ralphd'\n"))
    assert got["ok"] is False and got["cli"] is None
    assert "ralphd" in got["error"]


# ---- the wrapper's --probe-version, against the stub codex
def _wrapper_probe(monkeypatch, **env):
    full = {**os.environ, "RALPHD_CODEX_CLI_PATH": str(STUB_CODEX), **env}
    res = subprocess.run([sys.executable, "-m", "ralphd.harness.codex",
                          "--probe-version"], capture_output=True, text=True,
                         env=full, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_the_wrapper_probe_reports_a_current_stub(monkeypatch):
    doc = _wrapper_probe(monkeypatch)
    assert doc["cli"] == "0.156.1" and doc["ok"] is True
    assert doc["cliPath"] == str(STUB_CODEX)


def test_the_wrapper_probe_reports_a_too_old_stub(monkeypatch):
    doc = _wrapper_probe(monkeypatch, STUB_CODEX_VERSION="0.155.9")
    assert doc["cli"] == "0.155.9" and doc["ok"] is False
    assert CODEX_MIN_VERSION in doc["error"]


def test_the_wrapper_probe_reports_a_missing_cli(monkeypatch, tmp_path):
    doc = _wrapper_probe(monkeypatch,
                         RALPHD_CODEX_CLI_PATH=str(tmp_path / "no-such-codex"))
    assert doc["cli"] is None and doc["ok"] is False
    assert "not found" in doc["error"]


def test_the_doctor_check_end_to_end_through_the_probe_output(
        tmp_path, capsys, monkeypatch):
    """The wrapper's own JSON, as docker would relay it, fed through the real
    probe and the real check."""
    probe_json = json.dumps(_wrapper_probe(monkeypatch, STUB_CODEX_VERSION="0.155.9"))
    monkeypatch.setattr(cli, "sh", lambda cmd, **kw: _Res(0, probe_json + "\n"))
    verdict, line = cli._doctor_codex_runtime("img:x", True, True)
    assert verdict is False and "0.155.9" in line and CODEX_MIN_VERSION in line
    probe_json = json.dumps(_wrapper_probe(monkeypatch))
    verdict, line = cli._doctor_codex_runtime("img:x", True, False)
    assert verdict is True and line == ""


# ---- `ralphctl llm test` on the codex harness
@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _codex_profile(ctl):
    ctl.write_profile("cx", {
        "description": "codex", "harness": "codex",
        "model": "aigw-openai/openai.gpt-5.6-sol",
        "billing": {"payer": "AIGW gateway", "basis": "chargeback"},
        "env": {"AIGW_OPENAI_BASE_URL": FAKE_BASE_URL,
                "AWS_BEARER_TOKEN_BEDROCK": FAKE_TOKEN}})


def test_llm_test_pings_a_codex_pair_and_reports_ok(ctl):
    _codex_profile(ctl)
    res = ctl.run("llm", "test", "cx", "--image", PINNED_IMAGE,
                  env={"RALPHD_CODEX_CLI_PATH": str(STUB_CODEX)})
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.splitlines() == ["ok  codex  aigw-openai/openai.gpt-5.6-sol"]
    (argv,) = ctl.runs()
    assert _ping_args(argv) == ["--llm-ping", "--harness", "codex",
                                "--model", "aigw-openai/openai.gpt-5.6-sol"]


def test_llm_test_reports_a_failing_codex_ping(ctl):
    _codex_profile(ctl)
    res = ctl.run("llm", "test", "cx", "--image", PINNED_IMAGE, env={
        "RALPHD_CODEX_CLI_PATH": str(STUB_CODEX),
        "STUB_CODEX_SCENARIO": "failed",
        "STUB_CODEX_FAILED_MESSAGE": f"401 unauthorized {FAKE_TOKEN}"})
    assert res.returncode == 1, res.stdout
    (line,) = res.stdout.splitlines()
    assert line.startswith("FAIL codex aigw-openai/openai.gpt-5.6-sol  ")
    assert "401" in line
    assert FAKE_TOKEN not in res.stdout + res.stderr


def test_llm_test_reports_a_codex_ping_without_gateway_variables(ctl):
    ctl.write_profile("cx", {
        "description": "codex", "harness": "codex",
        "model": "aigw-openai/openai.gpt-5.6-sol",
        "billing": {"payer": "AIGW gateway", "basis": "chargeback"}})
    res = ctl.run("llm", "test", "cx", "--image", PINNED_IMAGE,
                  env={"RALPHD_CODEX_CLI_PATH": str(STUB_CODEX),
                       "AIGW_OPENAI_BASE_URL": "", "AWS_BEARER_TOKEN_BEDROCK": ""})
    assert res.returncode == 1
    (line,) = res.stdout.splitlines()
    assert line.startswith("FAIL codex ")
    assert "codex_gateway_not_configured" in line


def test_the_engine_ping_builds_a_codex_runner_and_accepts_the_flag():
    from ralphd.engine import llm_ping
    from ralphd.engine.runner import CodexRunner
    assert isinstance(llm_ping.build_runner("codex", Path("/tmp")), CodexRunner)
    args = llm_ping.build_arg_parser().parse_args(["--harness", "codex"])
    assert args.harness == "codex"


def test_llm_test_harness_flag_pings_the_profiles_model_on_codex(ctl):
    ctl.write_profile("gw", {
        "description": "gateway", "model": "aigw-openai/openai.gpt-5.6-sol",
        "billing": {"payer": "AIGW gateway", "basis": "chargeback"},
        "env": {"AIGW_OPENAI_BASE_URL": FAKE_BASE_URL,
                "AWS_BEARER_TOKEN_BEDROCK": FAKE_TOKEN}})
    res = ctl.run("llm", "test", "gw", "--harness", "codex", "--image", PINNED_IMAGE,
                  env={"RALPHD_CODEX_CLI_PATH": str(STUB_CODEX)})
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.splitlines() == ["ok  codex  aigw-openai/openai.gpt-5.6-sol"]
    (argv,) = ctl.runs()
    assert _ping_args(argv) == ["--llm-ping", "--harness", "codex",
                                "--model", "aigw-openai/openai.gpt-5.6-sol"]


def test_llm_test_harness_flag_rejects_an_unknown_harness_and_hats(ctl):
    ctl.write_profile("gw", {"description": "gateway", "model": "acme/m1"})
    assert ctl.run("llm", "test", "gw", "--harness", "nonsense").returncode == 2
    res = ctl.run("llm", "test", "gw", "--harness", "codex", "--hats", "--no-ping")
    assert res.returncode == 2
    assert "--harness-for" in res.stderr


def test_the_engine_llm_ping_runs_a_codex_stub_ping(tmp_path):
    env = {**os.environ, "RALPHD_CODEX_CLI_PATH": str(STUB_CODEX),
           "AIGW_OPENAI_BASE_URL": FAKE_BASE_URL,
           "AWS_BEARER_TOKEN_BEDROCK": FAKE_TOKEN, "STUB_RUN_DIR": str(tmp_path)}
    res = subprocess.run(
        [sys.executable, "-m", "ralphd.engine.llm_ping", "--harness", "codex",
         "--model", "aigw-openai/openai.gpt-5.6-sol"],
        capture_output=True, text=True, env=env, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    verdict = json.loads(res.stdout.strip().splitlines()[-1])
    assert verdict["harness"] == "codex" and verdict["ok"] is True
    assert FAKE_TOKEN not in res.stdout + res.stderr
