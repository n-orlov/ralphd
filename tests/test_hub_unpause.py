"""Task 006: the hub route `POST /api/runs/<id>/unpause` -- the SAME engine
verb `ralphctl unpause`/`cli/main.py::cmd_unpause` calls, `POST /resume`
(docs/api.md), so there is no second unpause mechanism.

Same shape as `test_retry_proxy_*` in `test_cli_ui.py`: a pure proxy, an
unreachable run collapsing to 503, an unknown run answering 404, and the
engine's own status code otherwise passed straight through (`StubEngineApi`
covers those three -- no genuine pause needed for them). The module's own
subject -- a real, LIVE run actually paused with `pauseReason:
"operator_input_needed"`, released by the hub route -- is proven against
production code on both sides: a real `LoopSupervisor`/`create_app` engine
bound to a real TCP port (no Docker, no `pi` subprocess needed since the
pause is raised directly through `LoopSupervisor.pause()`, exactly the
method the engine's own `operator_input_needed` escalation edges call) and
a real `ralphctl ui` hub subprocess proxying to it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from pathlib import Path

from test_cli_ui import StubEngineApi, _write_dead_run, _write_run_with_api, ui  # noqa: F401

from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import free_port

__all__ = ["ui"]


class _RealEngine:
    """A real engine (`LoopSupervisor` + `create_app`, production code on
    both sides) bound to a real `127.0.0.1` TCP port in a background
    thread -- so the hub's proxy (`urllib` against a real socket, exactly
    what a real run's `host.json` `apiUrl` points at) can reach it, with no
    Docker and no `pi` subprocess needed for a test that is only about the
    pause/resume seam itself."""

    def __init__(self, tmp_path: Path, run_id: str):
        self.run_id = run_id
        self.run = RunDir(root=tmp_path / "engine-run")
        self.cfg = JobConfig(run_id=run_id, max_approaches=3,
                             infra_retry_backoff_s=[0.0],
                             infra_retry_backoff_max_s=0.0)
        self.loop = LoopSupervisor(self.cfg, self.run, tmp_path / "engine-ws")
        app = create_app(self.cfg, self.run, self.loop)
        self.port = free_port()
        import uvicorn
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port,
                           log_level="warning"))
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._wait_ready()

    def _serve(self) -> None:
        asyncio.run(self.server.serve())

    def _wait_ready(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                return
            time.sleep(0.01)
        raise TimeoutError("in-thread engine API never came up")

    def status(self) -> dict:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/status", timeout=5) as resp:
            return json.loads(resp.read())

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=5)


def test_unpause_proxy_releases_a_real_operator_input_needed_pause(tmp_path, ui):
    """The genuine article: a real run's engine holds `pauseReason:
    "operator_input_needed"` on `status.json` (`LoopSupervisor.pause`,
    exactly the method the engine's own `operator_input_needed` escalation
    edges call -- see `loop.py::_pause_and_escalate_operator_input_needed`).
    The hub's `POST /api/runs/<id>/unpause` -- never a second, hub-owned
    unpause mechanism -- reaches the SAME `POST /resume` route
    `ralphctl unpause` calls, and the pause is released on the real engine's
    own `/status`, not merely echoed back by the hub."""
    run_id = "hub-unpause-live"
    engine = _RealEngine(tmp_path, run_id)
    try:
        engine.loop.pause(reason="operator_input_needed")
        paused = engine.status()
        assert paused["state"] == "paused"
        assert paused["pauseReason"] == "operator_input_needed"

        registry = tmp_path / "registry"
        run_dir = registry / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(json.dumps(paused))
        (run_dir / "tasks.json").write_text(json.dumps({"tasks": []}))
        (run_dir / "host.json").write_text(json.dumps({
            "runId": run_id, "port": engine.port,
            "apiUrl": f"http://127.0.0.1:{engine.port}"}))

        server = ui(registry)
        code, body = server.post(f"/api/runs/{run_id}/unpause", {})
        assert code == 200, (code, body)
        assert body == {"resumed": True}

        released = engine.status()
        assert released["state"] == "running"
        assert released.get("pauseReason") is None
    finally:
        engine.stop()


def test_unpause_proxy_posts_to_the_runs_resume_and_forwards_the_token(
        tmp_path, ui):
    """Never a second unpause mechanism: the proxy's target is literally
    `/resume` -- the same route `ralphctl unpause` hits -- and the run's
    `.api-token` is forwarded exactly like every other proxied route."""
    registry = tmp_path / "registry"
    engine = StubEngineApi()
    try:
        _write_run_with_api(registry, "run-unpause-tok", engine, token="hub-token",
                            state="paused")
        server = ui(registry)
        code, body = server.post("/api/runs/run-unpause-tok/unpause", {})
        assert code == 200, (code, body)
        assert body == {"resumed": True}
        posts = [(m, p, t) for m, p, t in engine.requests if m == "POST"]
        assert [(m, p) for m, p, _t in posts] == [("POST", "/resume")], engine.requests
        assert posts[0][2] == "Bearer hub-token"
    finally:
        engine.close()


def test_unpause_proxy_passes_the_engines_status_code_through(tmp_path, ui):
    """The engine's own status code is passed through unedited -- the hub
    never special-cases `/resume`'s answer the way it never special-cases
    `/retry`'s."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(resume_status=500,
                           resume_body={"detail": "engine-side failure"})
    try:
        _write_run_with_api(registry, "run-unpause-500", engine, state="paused")
        server = ui(registry)
        code, body = server.post("/api/runs/run-unpause-500/unpause", {})
        assert code == 500, (code, body)
        assert "engine-side failure" in json.dumps(body)
    finally:
        engine.close()


def test_unpause_proxy_returns_503_for_an_unreachable_run(tmp_path, ui):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "gone", state="paused")  # no host.json at all
    server = ui(registry)
    code, body = server.post("/api/runs/gone/unpause", {})
    assert code == 503, (code, body)
    assert "unreachable" in body["error"]


def test_unpause_proxy_unknown_run_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, body = server.post("/api/runs/nope/unpause", {})
    assert code == 404
    assert "not found" in body["error"]
