"""Both harnesses' `cli_version` tells three probe outcomes apart: a version
read, no version in the output, and a probe that could not finish (a timeout,
or an OSError while the file exists). The last is a transient infra fault
under the outage ladder and never "too old"; the second stays
`*_version_too_old`, refused; a missing file stays `*_cli_not_found`.

The fake CLIs are real scripts; the timeout is injected by lowering the
module's `PROBE_TIMEOUT_S`, so no test sleeps out the real 30 s."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from ralphd.cli.versions import ProbeFailed
from ralphd.engine.faults import FAULT_CLASS_INFRA, FAULT_CLASS_REFUSED, explain_fault
from ralphd.harness import claude_code as cc_mod
from ralphd.harness import codex as codex_mod

HARNESSES = {
    "claude-code": dict(mod=cc_mod, argv_flag="-v", min=cc_mod.CLAUDE_CODE_MIN_VERSION,
                        too_old="claude_code_version_too_old",
                        not_found="claude_code_cli_not_found",
                        probe_failed="claude_code_version_probe_failed",
                        banner="2.1.1 (Claude Code)", below="2.1.75"),
    "codex": dict(mod=codex_mod, argv_flag="--version", min=codex_mod.CODEX_MIN_VERSION,
                  too_old="codex_version_too_old",
                  not_found="codex_cli_not_found",
                  probe_failed="codex_version_probe_failed",
                  banner="codex-cli 0.1.0", below="0.156.0"),
}


@pytest.fixture(params=sorted(HARNESSES))
def h(request):
    return HARNESSES[request.param]


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-cli"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _error_text(err: dict) -> str:
    # The wrapper's in-band error reaches the engine as "<type>: <message>".
    return f"{err['type']}: {err['message']}"


def _classify(err: dict) -> dict:
    return explain_fault(error_text=_error_text(err), exit_code=1,
                         produced_traffic=False)


def _gate(h, cli: str) -> dict:
    mod = h["mod"]
    err = mod.version_gate(cli, mod.cli_version(cli))
    assert err is not None
    return err


def test_a_cli_sleeping_past_the_timeout_is_a_transient_probe_failure(h, tmp_path, monkeypatch):
    monkeypatch.setattr(h["mod"], "PROBE_TIMEOUT_S", 0.3)
    cli = _script(tmp_path, "exec sleep 30")
    result = h["mod"].cli_version(cli)
    assert isinstance(result, ProbeFailed)
    assert "timed out after 0.3 s" in result
    err = _gate(h, cli)
    assert err["type"] == h["probe_failed"]
    assert "timed out after 0.3 s" in err["message"]
    assert "too old" not in err["message"] and "older" not in err["message"]
    exp = _classify(err)
    assert exp["faultClass"] == FAULT_CLASS_INFRA
    assert exp["faultClass"] != FAULT_CLASS_REFUSED
    assert exp["signature"]["family"] == "cli-probe"


def test_an_oserror_on_an_existing_file_is_a_transient_probe_failure(h, tmp_path, monkeypatch):
    cli = _script(tmp_path, "echo unused")

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied (injected)")

    monkeypatch.setattr(subprocess, "run", boom)
    result = h["mod"].cli_version(cli)
    assert isinstance(result, ProbeFailed)
    assert "Permission denied (injected)" in result
    err = h["mod"].version_gate(cli, result)
    assert err["type"] == h["probe_failed"]
    assert "Permission denied (injected)" in err["message"]
    assert "too old" not in err["message"]
    exp = _classify(err)
    assert exp["faultClass"] == FAULT_CLASS_INFRA


def test_a_not_executable_file_is_a_probe_failure_not_not_found(h, tmp_path):
    path = tmp_path / "noexec"
    path.write_text("#!/bin/sh\necho 9.9.9\n")
    path.chmod(0o644)
    if os.access(path, os.X_OK):
        pytest.skip("executable bit not enforced for this user")
    err = _gate(h, str(path))
    assert err["type"] == h["probe_failed"]
    assert _classify(err)["faultClass"] == FAULT_CLASS_INFRA


def test_output_with_no_parseable_version_is_too_old_and_refused(h, tmp_path):
    cli = _script(tmp_path, "echo banner without a number")
    assert h["mod"].cli_version(cli) is None
    err = _gate(h, cli)
    assert err["type"] == h["too_old"]
    assert err["type"] != h["probe_failed"]
    assert "timed out" not in err["message"]
    exp = _classify(err)
    assert exp["faultClass"] == FAULT_CLASS_REFUSED
    assert exp["signature"]["family"] in ("cc-version", "codex-version")


def test_the_two_failure_shapes_differ_in_type_and_message(h, tmp_path, monkeypatch):
    monkeypatch.setattr(h["mod"], "PROBE_TIMEOUT_S", 0.3)
    slow = _gate(h, _script(tmp_path, "exec sleep 30"))
    (tmp_path / "fake-cli").unlink()
    silent = _gate(h, _script(tmp_path, "echo no version here"))
    assert slow["type"] != silent["type"]
    assert slow["message"] != silent["message"]
    assert _classify(slow)["faultClass"] != _classify(silent)["faultClass"]


def test_a_version_below_the_minimum_is_still_too_old_and_refused(h, tmp_path):
    cli = _script(tmp_path, f"echo {h['below']}")
    assert h["mod"].cli_version(cli) == h["below"]
    err = _gate(h, cli)
    assert err["type"] == h["too_old"]
    assert h["below"] in err["message"] and h["min"] in err["message"]
    assert _classify(err)["faultClass"] == FAULT_CLASS_REFUSED


def test_a_version_at_the_minimum_passes(h, tmp_path):
    cli = _script(tmp_path, f"echo {h['min']}")
    assert h["mod"].version_gate(cli, h["mod"].cli_version(cli)) is None


def test_a_missing_file_stays_cli_not_found_and_refused(h, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    cli = str(tmp_path / "does-not-exist")
    assert h["mod"].cli_version(cli) is None
    err = _gate(h, cli)
    assert err["type"] == h["not_found"]
    assert _classify(err)["faultClass"] == FAULT_CLASS_REFUSED
