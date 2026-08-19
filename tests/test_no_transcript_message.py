"""Task 041 (#6): an empty transcript says so, on both surfaces.

A run dir whose `iterations/` dir is empty (the run just started, or its
container died before the first iteration was recorded) used to render as
*nothing at all*: `ralphctl logs <id>` printed zero bytes and exited 0, and
the hub's log box showed an empty rectangle. Both are indistinguishable
from a broken command / broken hub, which is exactly the moment an operator
is already suspicious.

Now both render the single explicit line `log_merge.NO_TRANSCRIPT`
("(no transcript yet)"), with the wording living in `ralphd.log_merge` so
the two surfaces cannot drift apart. `--raw` is deliberately excluded: it
is a 1:1 wire-format contract for machines, where an empty transcript
honestly is zero events.

Covered here for an unreachable run (on-disk merge path) AND a live run
whose API serves an empty `/logs` (proxy path), on both the CLI and the hub
endpoint.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from ralphd.log_merge import NO_TRANSCRIPT
from tests.conftest import RALPHCTL
from tests.test_cli_ui import (
    StubEngineApi,
    UiServer,
    _write_dead_run,
    _write_run_with_api,
)


@pytest.fixture
def ui():
    """Hub server factory -- same `UiServer` helper `test_cli_ui.py` drives
    (a real `ralphctl ui` subprocess), re-plumbed here because a fixture
    cannot be imported into another module without shadowing."""
    servers = []

    def make(registry: Path) -> UiServer:
        server = UiServer(registry)
        server.wait_ready()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _empty_dead_run(tmp_path: Path, run_id: str = "notranscript") -> Path:
    """Registry holding one unreachable run whose `iterations/` is empty."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, run_id, state="running", verdict=None,
                              iterationsUsed=0)
    (run_dir / "iterations").mkdir()
    (run_dir / "host.json").write_text(json.dumps(
        {"runId": run_id, "container": "e" * 12, "port": _closed_port(),
         "apiUrl": f"http://127.0.0.1:{_closed_port()}", "image": "n/a"}))
    return registry


def _ctl(registry: Path, *argv: str, timeout: int = 30):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------- CLI
def test_cli_logs_on_empty_iterations_dir_says_no_transcript(tmp_path):
    registry = _empty_dead_run(tmp_path)

    res = _ctl(registry, "logs", "notranscript")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "Traceback" not in res.stderr
    assert [line for line in res.stdout.splitlines() if line.strip()] == [
        NO_TRANSCRIPT]


def test_cli_logs_follow_on_empty_iterations_dir_says_no_transcript(tmp_path):
    """The follow path takes the same snapshot renderer, so it must not
    fall back to printing nothing before its 'nothing to follow' notice."""
    registry = _empty_dead_run(tmp_path)

    res = _ctl(registry, "logs", "notranscript", "--follow")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert NO_TRANSCRIPT in res.stdout
    assert "nothing to follow" in res.stderr


def test_cli_logs_raw_on_empty_iterations_dir_stays_byte_empty(tmp_path):
    """`--raw` keeps its 1:1 wire contract: no synthesized human line on
    stdout, so a machine consumer still sees exactly zero events."""
    registry = _empty_dead_run(tmp_path)

    res = _ctl(registry, "logs", "notranscript", "--raw", "--tail", "0")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert res.stdout == ""
    assert NO_TRANSCRIPT not in res.stdout


def test_cli_logs_on_live_run_with_empty_transcript_says_no_transcript(tmp_path):
    """Same message on the LIVE path: the run's API is up and answers
    `GET /logs` with an empty body (nothing recorded yet)."""
    engine = StubEngineApi(status={"state": "running", "runId": "livempty"})
    try:
        registry = tmp_path / "registry"
        _write_run_with_api(registry, "livempty", engine, state="running",
                            verdict=None, iterationsUsed=0)

        res = _ctl(registry, "logs", "livempty")

        assert res.returncode == 0, (res.stdout, res.stderr)
        assert [line for line in res.stdout.splitlines() if line.strip()] == [
            NO_TRANSCRIPT]
        # a live run says nothing about snapshots (task 040 contract intact)
        assert "on-disk snapshot" not in res.stderr
    finally:
        engine.close()


# ---------------------------------------------------------------------- hub
def test_hub_log_endpoint_on_empty_iterations_dir_says_no_transcript(tmp_path, ui):
    registry = _empty_dead_run(tmp_path, "hubnotranscript")

    server = ui(registry)
    code, body = server.get("/api/runs/hubnotranscript/logs?tail=200")

    assert code == 200
    assert body["live"] is False
    assert body["lines"] == [NO_TRANSCRIPT]


def test_hub_log_endpoint_on_live_run_with_empty_transcript(tmp_path, ui):
    engine = StubEngineApi(status={"state": "running", "runId": "hublivempty"})
    try:
        registry = tmp_path / "registry"
        _write_run_with_api(registry, "hublivempty", engine, state="running",
                            verdict=None, iterationsUsed=0)

        server = ui(registry)
        code, body = server.get("/api/runs/hublivempty/logs?tail=200")

        assert code == 200
        assert body["live"] is True
        assert body["lines"] == [NO_TRANSCRIPT]
    finally:
        engine.close()


def test_both_surfaces_use_the_one_shared_wording(tmp_path):
    """The message must exist in exactly one place -- neither the CLI nor
    the hub server (nor app.js) may spell it out itself."""
    from ralphd.cli import main as cli_main
    from ralphd.cli import ui_server as ui_mod

    assert cli_main.NO_TRANSCRIPT is NO_TRANSCRIPT
    assert ui_mod.NO_TRANSCRIPT is NO_TRANSCRIPT

    src = Path(cli_main.__file__).parent
    for path in (src / "main.py", src / "ui_server.py", src / "web" / "app.js"):
        assert NO_TRANSCRIPT not in path.read_text(), \
            f"{path.name} spells the no-transcript wording out instead of " \
            "importing log_merge.NO_TRANSCRIPT"
