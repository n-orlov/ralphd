"""Both harnesses' `cli_version` tells three probe outcomes apart: a version
read, no version in the output, and a probe that could not finish (a timeout,
or an OSError while the file exists). The last is a transient infra fault
under the outage ladder and never "too old"; no version in output that is
present stays `*_version_too_old`, refused, while a probe that printed nothing
at all is the transient `*_version_probe_empty` (PRD D.3: "too old" needs a
version below the minimum or output that is present); a missing file stays
`*_cli_not_found`, and a file that is there but not executable -- even one
found only through PATH -- is a probe failure, not "not found".

The fake CLIs are real scripts; the timeout is injected by lowering the
module's `PROBE_TIMEOUT_S`, so no test sleeps out the real 30 s."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from ralphd.cli.versions import EmptyProbeOutput, ProbeFailed
from ralphd.engine.faults import FAULT_CLASS_INFRA, FAULT_CLASS_REFUSED, explain_fault
from ralphd.harness import claude_code as cc_mod
from ralphd.harness import codex as codex_mod

HARNESSES = {
    "claude-code": dict(mod=cc_mod, argv_flag="-v", min=cc_mod.CLAUDE_CODE_MIN_VERSION,
                        too_old="claude_code_version_too_old",
                        not_found="claude_code_cli_not_found",
                        probe_failed="claude_code_version_probe_failed",
                        probe_empty="claude_code_version_probe_empty",
                        cli_env="RALPHD_CLAUDE_CLI_PATH",
                        banner="2.1.1 (Claude Code)", below="2.1.75"),
    "codex": dict(mod=codex_mod, argv_flag="--version", min=codex_mod.CODEX_MIN_VERSION,
                  too_old="codex_version_too_old",
                  not_found="codex_cli_not_found",
                  probe_failed="codex_version_probe_failed",
                  probe_empty="codex_version_probe_empty",
                  cli_env="RALPHD_CODEX_CLI_PATH",
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


# -- empty output is not "output present" (D.3) -------------------------------

@pytest.mark.parametrize("exit_code", [0, 3])
def test_a_cli_that_prints_nothing_is_transient_not_too_old(h, tmp_path, exit_code):
    cli = _script(tmp_path, f"exit {exit_code}")
    result = h["mod"].cli_version(cli)
    assert isinstance(result, EmptyProbeOutput)
    assert result is not None and not isinstance(result, ProbeFailed)
    assert f"exited {exit_code} and printed nothing" in result
    err = _gate(h, cli)
    assert err["type"] == h["probe_empty"]
    assert err["type"] not in (h["too_old"], h["not_found"])
    assert "printed nothing" in err["message"]
    assert "older than" not in err["message"]
    exp = _classify(err)
    assert exp["faultClass"] == FAULT_CLASS_INFRA
    assert exp["faultClass"] != FAULT_CLASS_REFUSED
    assert exp["signature"]["family"] == "cli-probe"


def test_empty_and_unparseable_output_get_different_verdicts(h, tmp_path):
    empty = _gate(h, _script(tmp_path, "exit 0"))
    (tmp_path / "fake-cli").unlink()
    garbage = _gate(h, _script(tmp_path, "echo banner without a number"))
    assert empty["type"] == h["probe_empty"]
    assert garbage["type"] == h["too_old"]
    assert _classify(empty)["faultClass"] == FAULT_CLASS_INFRA
    assert _classify(garbage)["faultClass"] == FAULT_CLASS_REFUSED


def test_unparseable_output_on_stderr_alone_is_present_output(h, tmp_path):
    cli = _script(tmp_path, "echo 'unknown option' >&2; exit 2")
    assert h["mod"].cli_version(cli) is None
    err = _gate(h, cli)
    assert err["type"] == h["too_old"]
    assert _classify(err)["faultClass"] == FAULT_CLASS_REFUSED


# -- a non-executable CLI found only through PATH -----------------------------

def _noexec_on_path(tmp_path: Path, monkeypatch) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    path = bindir / "fake-noexec-cli"
    path.write_text("#!/bin/sh\necho 9.9.9\n")
    path.chmod(0o644)
    if os.access(path, os.X_OK):
        pytest.skip("executable bit not enforced for this user")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return path.name


def test_a_not_executable_cli_found_through_path_is_a_probe_failure(h, tmp_path, monkeypatch):
    name = _noexec_on_path(tmp_path, monkeypatch)
    result = h["mod"].cli_version(name)
    assert isinstance(result, ProbeFailed)
    assert "ermission denied" in result
    err = _gate(h, name)
    assert err["type"] == h["probe_failed"]
    assert err["type"] != h["not_found"]
    assert _classify(err)["faultClass"] == FAULT_CLASS_INFRA


def test_a_bare_name_on_no_path_entry_stays_cli_not_found(h, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert h["mod"].cli_version("no-such-cli-anywhere") is None
    err = _gate(h, "no-such-cli-anywhere")
    assert err["type"] == h["not_found"]


# -- the live wrappers, end to end --------------------------------------------

def _live(h, tmp_path: Path, monkeypatch, cli: str):
    """Run the real wrapper subprocess with its CLI pointed at `cli`; returns
    the IterationResult. No query is ever sent: the gate refuses first."""
    if h["mod"] is cc_mod:
        from test_claude_code_runner import _run, _runner, _stub_env
        _stub_env(monkeypatch, tmp_path)
        monkeypatch.setenv(h["cli_env"], cli)
        return _run(_runner(tmp_path), tmp_path)
    from test_codex_runner import _run, _setup
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv(h["cli_env"], cli)
    return _run(tmp_path)[0]


def _live_class(result) -> str:
    return explain_fault(error_text=result.error_message, exit_code=result.exit_code,
                         produced_traffic=False)["faultClass"]


def test_the_live_wrapper_reports_an_empty_probe_as_transient(h, tmp_path, monkeypatch):
    result = _live(h, tmp_path, monkeypatch, _script(tmp_path, "exit 0"))
    assert result.error_message.startswith(h["probe_empty"]), result.error_message
    assert h["too_old"] not in result.error_message
    assert _live_class(result) == FAULT_CLASS_INFRA


def test_the_live_wrapper_reports_a_noexec_path_cli_as_transient(h, tmp_path, monkeypatch):
    name = _noexec_on_path(tmp_path, monkeypatch)
    result = _live(h, tmp_path, monkeypatch, name)
    assert result.error_message.startswith(h["probe_failed"]), result.error_message
    assert h["not_found"] not in result.error_message
    assert _live_class(result) == FAULT_CLASS_INFRA


def test_the_probe_document_never_reports_an_empty_probe_as_a_version(h, tmp_path, monkeypatch):
    cli = _script(tmp_path, "exit 0")
    monkeypatch.setattr(h["mod"], "resolve_cli_path", lambda: cli)
    doc = h["mod"].probe()
    assert doc["cli"] is None and doc["ok"] is False
    assert "printed nothing" in doc["error"]
