"""`ralphctl doctor` `claude_code_runtime` check: the job image carries the
Claude Agent SDK and a Claude Code CLI at or above CLAUDE_CODE_MIN_VERSION.

The image probe is monkeypatched (the recording stub docker answers `run`
with a fake container id, which is not a probe answer); `None` from the probe
is report-only and leaves `ok` unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ralphd.cli import main as cli
from ralphd.engine.config import CLAUDE_CODE_MIN_VERSION
from tests.conftest import call_ralphctl_in_process

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"


def _doctor(tmp_path, capsys, monkeypatch, probe):
    (tmp_path / "registry").mkdir()
    monkeypatch.setattr(cli, "_probe_claude_code_runtime", probe)
    env = {**os.environ, "RALPHD_DOCKER": str(STUB_DOCKER),
           "RALPHD_REGISTRY": str(tmp_path / "registry"),
           "STUB_DOCKER_LOG": str(tmp_path / "docker.jsonl"),
           "STUB_DOCKER_INSPECT_OK": "1"}
    res = call_ralphctl_in_process(["--json", "doctor"], env=env, capsys=capsys)
    return res, json.loads(res.stdout)


def _text_doctor(tmp_path, capsys, monkeypatch, probe):
    (tmp_path / "registry").mkdir()
    monkeypatch.setattr(cli, "_probe_claude_code_runtime", probe)
    env = {**os.environ, "RALPHD_DOCKER": str(STUB_DOCKER),
           "RALPHD_REGISTRY": str(tmp_path / "registry"),
           "STUB_DOCKER_LOG": str(tmp_path / "docker.jsonl"),
           "STUB_DOCKER_INSPECT_OK": "1"}
    return call_ralphctl_in_process(["doctor"], env=env, capsys=capsys)


def test_an_ok_probe_passes_the_check(tmp_path, capsys, monkeypatch):
    _, doc = _doctor(tmp_path, capsys, monkeypatch,
                     lambda ref: {"sdk": "0.2.163", "cli": "2.1.286", "ok": True})
    assert doc["checks"]["claude_code_runtime"] is True
    assert doc["ok"] == all(doc["checks"].values())


def test_the_minimum_version_itself_passes(tmp_path, capsys, monkeypatch):
    _, doc = _doctor(tmp_path, capsys, monkeypatch,
                     lambda ref: {"sdk": "0.2.163", "cli": CLAUDE_CODE_MIN_VERSION,
                                  "ok": True})
    assert doc["checks"]["claude_code_runtime"] is True


def test_an_old_cli_fails_and_the_message_names_the_minimum(
        tmp_path, capsys, monkeypatch):
    res, doc = _doctor(tmp_path, capsys, monkeypatch,
                       lambda ref: {"sdk": "0.2.163", "cli": "2.1.75", "ok": False})
    assert doc["checks"]["claude_code_runtime"] is False
    assert doc["ok"] is False
    assert res.returncode == 1


def test_the_text_report_names_the_minimum(tmp_path, capsys, monkeypatch):
    res = _text_doctor(tmp_path, capsys, monkeypatch,
                       lambda ref: {"sdk": "0.2.163", "cli": "2.1.75", "ok": False})
    assert res.returncode == 1
    assert "✗ claude_code_runtime" in res.stdout
    assert CLAUDE_CODE_MIN_VERSION in res.stdout
    assert "2.1.75" in res.stdout


def test_an_sdk_import_failure_fails_the_check(tmp_path, capsys, monkeypatch):
    res, doc = _doctor(
        tmp_path, capsys, monkeypatch,
        lambda ref: {"sdk": None, "cli": None, "ok": False,
                     "error": "ModuleNotFoundError: No module named 'claude_agent_sdk'"})
    assert doc["checks"]["claude_code_runtime"] is False
    assert res.returncode == 1


def test_an_unprobeable_image_is_report_only(tmp_path, capsys, monkeypatch):
    res = _text_doctor(tmp_path, capsys, monkeypatch, lambda ref: None)
    assert "! claude_code_runtime: not probed" in res.stdout
    assert "claude_code_runtime" not in [
        ln.split()[-1] for ln in res.stdout.splitlines() if ln[:1] in "✓✗"]
    (tmp_path / "j").mkdir()
    _, doc = _doctor(tmp_path / "j", capsys, monkeypatch, lambda ref: None)
    assert "claude_code_runtime" not in doc["checks"]
    assert doc["ok"] == all(doc["checks"].values())


# ---- the probe itself, against a faked docker invocation
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
    return cli._probe_claude_code_runtime("img:x"), calls


def test_the_probe_runs_the_wrapper_with_the_images_python(monkeypatch):
    doc = {"sdk": "0.2.163", "cli": "2.1.286", "cliPath": "/x", "ok": True}
    got, calls = _probe_with(monkeypatch, _Res(0, json.dumps(doc) + "\n"))
    assert got == doc
    argv = calls[0]
    assert argv[argv.index("--entrypoint") + 1] == "python"
    assert argv[-3:] == ["-m", "ralphd.harness.claude_code", "--probe-version"]
    assert "img:x" in argv


@pytest.mark.parametrize("res", [
    _Res(0, "f" * 64 + "\n"),                       # stub docker: a container id
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
        1, "", "Traceback...\nModuleNotFoundError: No module named 'claude_agent_sdk'\n"))
    assert got["ok"] is False and got["cli"] is None
    assert "claude_agent_sdk" in got["error"]
