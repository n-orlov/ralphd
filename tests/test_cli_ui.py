"""Black-box tests for `ralphctl ui` -- the local hub HTTP server (PRD reqs
21-22, server side; the static bundle itself is task 034).

Launches the real `ralphctl ui` executable as a subprocess pointed at a
temp registry (via `RALPHD_REGISTRY`), then drives it with plain HTTP
requests -- strictly black-box, no importing `ralphd.cli.ui_server`
internals from the assertions (only from the "no heavy dependency" probe,
which deliberately inspects `sys.modules` in a *separate* subprocess).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import RALPHCTL, free_port


class UiServer:
    def __init__(self, registry: Path):
        self.port = free_port()
        env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
        self.proc = subprocess.Popen(
            [str(RALPHCTL), "ui", "--port", str(self.port), "--bind", "127.0.0.1"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.base = f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(f"ralphctl ui exited early: {out}")
            try:
                with urllib.request.urlopen(f"{self.base}/api/runs", timeout=2):
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        raise TimeoutError("ralphctl ui never came up")

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"{self.base}{path}", method="POST", data=data)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def ui(tmp_path):
    servers = []

    def make(registry):
        s = UiServer(registry)
        s.wait_ready()
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()


def _write_dead_run(registry: Path, run_id: str, **status_fields):
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    status = {"runId": run_id, "state": "succeeded", "verdict": "verified",
              "phase": "review", "approach": 1, "iterationsUsed": 3,
              "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z",
              **status_fields}
    (run_dir / "status.json").write_text(json.dumps(status))
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": []}))
    return run_dir


def test_run_list_reads_registry_without_docker_or_live_engine(tmp_path, ui):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-alpha", state="succeeded", verdict="verified")
    _write_dead_run(registry, "run-beta", state="failed", verdict=None)

    server = ui(registry)
    code, body = server.get("/api/runs")
    assert code == 200
    ids = {r["runId"]: r for r in body["runs"]}
    assert set(ids) == {"run-alpha", "run-beta"}
    assert ids["run-alpha"]["state"] == "succeeded"
    assert ids["run-alpha"]["verdict"] == "verified"
    assert ids["run-beta"]["state"] == "failed"


def test_run_detail_degrades_gracefully_for_dead_run(tmp_path, ui):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "gone", state="failed")
    # no host.json at all -- simulates a run whose container/API is long gone

    server = ui(registry)
    code, body = server.get("/api/runs/gone")
    assert code == 200
    assert body["live"] is False
    assert body["status"]["state"] == "failed"  # fell back to status.json
    assert body["tasks"] == {"tasks": []}


def test_run_detail_and_unknown_run_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, body = server.get("/api/runs/does-not-exist")
    assert code == 404
    assert "not found" in body["error"]


def test_run_detail_proxies_live_status_tasks_and_logs(tmp_path, live, ui):
    run = live(run_id="hubtest", job={"iterations": 12, "max_approaches": 3,
                                      "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "3"})
    run.wait_api()

    server = ui(run.registry)

    code, detail = server.get(f"/api/runs/{run.run_id}")
    assert code == 200
    assert detail["live"] is True
    assert detail["status"]["runId"] == run.run_id
    assert isinstance(detail["tasks"], dict)

    code, logs = server.get(f"/api/runs/{run.run_id}/logs?tail=50")
    assert code == 200
    assert logs["live"] is True
    assert isinstance(logs["text"], str)

    run.wait_terminal()


def test_steering_post_proxies_and_creates_steering_file(tmp_path, live, ui):
    run = live(run_id="hubsteer", job={"iterations": 12, "max_approaches": 3,
                                       "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "3"})
    run.wait_api()

    server = ui(run.registry)
    code, resp = server.post(f"/api/runs/{run.run_id}/steer",
                              {"message": "hub steering message", "name": "hubtest"})
    assert code == 202, resp
    assert "file" in resp

    steering_files = list((run.run_dir / "steering").glob("*.md"))
    assert steering_files, "steering POST via the hub never wrote a file"
    assert any("hub steering message" in f.read_text() for f in steering_files)

    run.wait_terminal()


def test_steering_post_unknown_run_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, _resp = server.post("/api/runs/nope/steer", {"message": "x"})
    assert code == 404


def test_static_path_404s_cleanly_before_bundle_exists(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{server.base}/index.html", timeout=5)
    assert exc_info.value.code == 404


def test_ui_server_module_never_imports_fastapi_or_uvicorn():
    """The hub server must stay stdlib-only on the ralphctl side (PRD req
    22): importing it must not pull fastapi/uvicorn into sys.modules, even
    though those are dependencies of the *engine* side of this same
    package. Run in a fresh subprocess so nothing else in the test process
    has already imported them first."""
    probe = (
        "import sys\n"
        "import ralphd.cli.ui_server\n"
        "assert 'fastapi' not in sys.modules, sys.modules.keys()\n"
        "assert 'uvicorn' not in sys.modules, sys.modules.keys()\n"
        "print('ok')\n"
    )
    res = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert res.stdout.strip() == "ok"
