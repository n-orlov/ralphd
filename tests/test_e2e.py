"""Black-box end-to-end tests.

Each test launches the real `ralphd-engine` process (the same entrypoint the
container runs) with a stub `pi` on PATH, then observes it strictly from the
outside: the HTTP API and the run-dir files. No engine internals are imported.
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EngineProc:
    """A running ralphd-engine with its run/config dirs and API helpers."""

    def __init__(self, tmp: Path, job: dict, stub_env: dict | None = None):
        self.run_dir = tmp / "run"
        self.config_dir = tmp / "config"
        self.workspace = tmp / "ws"
        for d in (self.run_dir, self.config_dir, self.workspace):
            d.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "prd.md").write_text("# E2E test PRD\n\nDo the thing.\n")
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
        self.proc = subprocess.Popen(
            ["ralphd-engine"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # -- black-box surface -------------------------------------------------
    def api(self, method: str, path: str, body: dict | None = None,
            expect_error: bool = False):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                return resp.status, (json.loads(data) if data else None)
        except urllib.error.HTTPError as e:
            if not expect_error:
                raise
            return e.code, json.loads(e.read() or b"{}")

    def wait_api(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.api("GET", "/healthz")
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        raise TimeoutError("engine API never came up")

    def wait_state(self, states: tuple[str, ...], timeout=60) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = json.loads((self.run_dir / "status.json").read_text()) \
                if (self.run_dir / "status.json").exists() else {}
            if status.get("state") in states:
                return status
            time.sleep(0.2)
        raise TimeoutError(f"never reached {states}; last: {status}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "e2e", "iterations": 12,
                    "max_approaches": 3, "on_complete": "idle"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


# --------------------------------------------------------------------------
def test_happy_path_exit_mode(engine_factory):
    e = engine_factory(job={"on_complete": "exit"})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)
    # iteration records exist: planning + 2 workers + review
    iters = sorted((e.run_dir / "iterations").iterdir())
    assert len(iters) == 4
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert [m["phase"] for m in metas] == ["planning", "worker", "worker", "review"]
    assert all((d / "output.jsonl").exists() for d in iters)
    # usage accumulated across iterations
    assert status["usage"]["totalTokens"] == 4 * 110


def test_review_rejection_starts_new_approach(engine_factory):
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"STUB_REVIEW_FAILS": "1"})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["approach"] == 2
    composite = (e.run_dir / "composite-prd.md").read_text()
    assert "finding from review #1" in composite
    assert "E2E test PRD" in composite
    assert (e.run_dir / "approaches" / "01" / "review-findings.md").exists()


def test_stagnation_guard_fails_job(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "max_approaches": 1},
                       stub_env={"STUB_WORKER_STALLS": "1"})
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"


def test_budget_exhaustion_fails_job(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "iterations": 2,
                            "max_approaches": 1},
                       stub_env={"STUB_TASKS": "5"})
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["iterationsUsed"] == 2


def test_api_observation_and_idle_lifecycle(engine_factory):
    e = engine_factory()  # idle mode
    e.wait_api()
    e.wait_state(("succeeded",))

    code, status = e.api("GET", "/status")
    assert code == 200 and status["verdict"] == "verified"
    assert status["tasks"]["completed"] == status["tasks"]["total"] == 2

    code, tasks = e.api("GET", "/tasks")
    assert len(tasks["tasks"]) == 2

    code, iterations = e.api("GET", "/iterations")
    assert [i["phase"] for i in iterations] == \
        ["planning", "worker", "worker", "review"]

    code, meta = e.api("GET", "/iterations/1")
    assert meta["phase"] == "planning" and meta["exitCode"] == 0

    # steering a finished job is rejected
    code, _ = e.api("POST", "/steering", {"message": "late"}, expect_error=True)
    assert code == 409
    # so is aborting or pausing it
    code, _ = e.api("POST", "/abort", {}, expect_error=True)
    assert code == 409
    code, _ = e.api("POST", "/pause", expect_error=True)
    assert code == 409

    # events replay contains the full story
    req = urllib.request.Request(
        f"http://127.0.0.1:{e.port}/events?since=0")
    types = []
    with urllib.request.urlopen(req, timeout=10) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data: "):
                types.append(json.loads(line[6:])["type"])
            if types and types[-1] == "state":
                break
    assert "iteration.start" in types and "signal" in types
    assert types.count("phase") >= 2  # planning + review

    # engine stays up (idle), then shuts down on request
    assert e.proc.poll() is None
    code, _ = e.api("POST", "/shutdown")
    assert code == 200
    assert e.proc.wait(timeout=10) == 0


def test_steering_reaches_next_iteration(engine_factory):
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"STUB_SLEEP": "1.5", "STUB_TASKS": "3"})
    e.wait_api()
    code, res = e.api("POST", "/steering", {"message": "prefer the shortcut",
                                            "name": "hint"})
    assert code == 202 and res["file"] == "001-hint.md"
    assert e.proc.wait(timeout=90) == 0
    # the stub records steering text it saw in the final worker prompt
    saw = (e.run_dir / ".stub-saw-steering").read_text()
    assert "prefer the shortcut" in saw
    # and the API/journal marks it consumed
    consumed = json.loads((e.run_dir / "steering" / ".consumed.json").read_text())
    assert "001-hint.md" in consumed


def test_abort_via_api(engine_factory):
    e = engine_factory(stub_env={"STUB_SLEEP": "2", "STUB_TASKS": "10"})
    e.wait_api()
    e.wait_state(("running",))
    code, _ = e.api("POST", "/abort", {"reason": "test abort"})
    assert code == 200
    status = e.wait_state(("aborted",), timeout=30)
    assert status["reason"] == "test abort"
    # idle mode: engine still up and queryable after abort
    code, s = e.api("GET", "/status")
    assert code == 200 and s["state"] == "aborted"
    e.api("POST", "/shutdown")
    assert e.proc.wait(timeout=10) == 1


def test_huge_output_line_does_not_kill_job(engine_factory):
    """pi emits full message snapshots per NDJSON event; a single line can be
    hundreds of KiB. Regression: this used to raise 'Separator is not found'
    and fail the entire job instead of at most one iteration."""
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"STUB_HUGE_LINE": "1"})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"


def test_api_token_auth(engine_factory, tmp_path):
    os.environ["RALPHD_API_TOKEN"] = "sekret"
    try:
        e = engine_factory()
        e.wait_api()  # healthz is unauthenticated
        with pytest.raises(urllib.error.HTTPError) as exc:
            e.api("GET", "/status")
        assert exc.value.code == 401
        req = urllib.request.Request(f"http://127.0.0.1:{e.port}/status")
        req.add_header("Authorization", "Bearer sekret")
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
    finally:
        del os.environ["RALPHD_API_TOKEN"]
