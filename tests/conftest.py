"""Shared pytest fixtures for black-box CLI tests that need a real,
directly-launched `ralphd-engine` (no Docker) behind `ralphctl` — the
"live test engine" pattern.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
STUB_PI = REPO / "tests" / "stub-pi"
RALPHCTL = Path(sys.executable).parent / "ralphctl"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveRun:
    """A real ralphd-engine, wired so `ralphctl` (pointed at `registry`)
    can talk to it as if a container had been started normally."""

    def __init__(self, tmp: Path, run_id: str, job: dict, stub_env: dict | None = None):
        self.run_id = run_id
        self.registry = tmp / "registry"
        self.run_dir = self.registry / "runs" / run_id
        self.config_dir = self.registry / "configs" / run_id
        self.workspace = tmp / "ws" / run_id
        for d in (self.run_dir, self.config_dir, self.workspace):
            d.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "prd.md").write_text("# CLI logs test PRD\n\nDo the thing.\n")
        (self.config_dir / "job.yaml").write_text(
            "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items()))
        self.port = free_port()
        env = {
            **os.environ,
            "PATH": f"{STUB_PI}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "STUB_RUN_DIR": str(self.run_dir),
            "RALPHD_RUN_DIR": str(self.run_dir),
            "RALPHD_CONFIG_DIR": str(self.config_dir),
            "RALPHD_WORKSPACE_DIR": str(self.workspace),
            "RALPHD_PORT": str(self.port),
            **(stub_env or {}),
        }
        self.proc = subprocess.Popen(["ralphd-engine"], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True)
        (self.run_dir / "host.json").write_text(json.dumps({
            "runId": run_id, "container": "f" * 12, "port": self.port,
            "apiUrl": f"http://127.0.0.1:{self.port}", "image": "n/a",
        }))

    def wait_terminal(self, timeout=60) -> dict:
        deadline = time.time() + timeout
        status = {}
        while time.time() < deadline:
            sf = self.run_dir / "status.json"
            if sf.exists():
                try:
                    status = json.loads(sf.read_text())
                except json.JSONDecodeError:
                    status = {}
            if status.get("state") in ("succeeded", "failed", "aborted"):
                return status
            time.sleep(0.2)
        raise TimeoutError(f"job never finished; last status: {status}")

    def ralphctl(self, *argv: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "RALPHD_REGISTRY": str(self.registry)}
        return subprocess.run([str(RALPHCTL), *argv], env=env,
                              capture_output=True, text=True, timeout=60)

    def wait_api(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/healthz")
                with urllib.request.urlopen(req, timeout=2):
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        raise TimeoutError("engine API never came up")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def live(tmp_path):
    """Factory fixture: `live(run_id=..., job=..., stub_env=...)` -> LiveRun.
    All runs created are stopped at teardown."""
    runs = []

    def make(run_id="logtest", job=None, stub_env=None):
        defaults = {"run_id": run_id, "iterations": 12, "max_approaches": 3,
                    "on_complete": "idle"}
        r = LiveRun(tmp_path, run_id, {**defaults, **(job or {})}, stub_env)
        runs.append(r)
        return r

    yield make
    for r in runs:
        r.stop()
