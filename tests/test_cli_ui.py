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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.conftest import RALPHCTL, call_ralphctl_in_process, free_port


class UiServer:
    # `free_port()`'s own docstring (tests/conftest.py) names the residual
    # TOCTOU window it cannot close: the port can still lose the
    # release-then-bind race between this fixture calling it and the
    # *subprocess* actually binding, under a loaded `-n 8` run (sha
    # 6064fac3, tests/test_tasks_stale_cli.py::
    # test_a_forged_label_in_tasks_json_cannot_fake_staleness,
    # OSError EADDRINUSE). `ralphctl ui` dies fast on that exact error
    # (main.py's `cmd_ui` lets `make_server`'s bind OSError propagate as an
    # uncaught traceback), so `wait_ready` below detects that specific
    # early exit and relaunches on a freshly drawn port instead of handing
    # a lost race to the caller.
    _MAX_BIND_RETRIES = 5

    def __init__(self, registry: Path, env: dict | None = None,
                 bind: str = "127.0.0.1", extra_args: tuple[str, ...] = ()):
        self.registry = registry
        self._env_extra = env
        self._bind = bind
        self._extra_args = extra_args
        self._bind_retries = 0
        self._launch()

    def _launch(self):
        self.port = free_port()
        env = {**os.environ, "RALPHD_REGISTRY": str(self.registry),
               **(self._env_extra or {})}
        self.proc = subprocess.Popen(
            [str(RALPHCTL), "ui", "--port", str(self.port), "--bind",
             self._bind, *self._extra_args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        # A non-loopback bind (e.g. 0.0.0.0) still accepts loopback
        # connections, so tests always talk to the server over 127.0.0.1
        # regardless of what was requested on --bind.
        self.base = f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                if ("Address already in use" in out
                        and self._bind_retries < self._MAX_BIND_RETRIES):
                    # Lost the free_port() race after all -- draw a fresh
                    # port and relaunch rather than fail the test on a
                    # known, documented residual window.
                    self._bind_retries += 1
                    self._launch()
                    continue
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

    def delete(self, path: str) -> tuple[int, dict]:
        """Task 030 (#19): the hub's DELETE endpoint."""
        req = urllib.request.Request(f"{self.base}{path}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
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

    def make(registry, env: dict | None = None, bind: str = "127.0.0.1",
             extra_args: tuple[str, ...] = ()):
        s = UiServer(registry, env=env, bind=bind, extra_args=extra_args)
        servers.append(s)
        try:
            s.wait_ready()
        except Exception:
            s.stop()
            raise
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


class StubEngineApi:
    """Minimal stand-in for a run's *live* container API (task 017), so a
    hub test can exercise the proxy routes without booting a real engine
    container: answers the GETs the run-detail view issues (`/status`,
    `/tasks`, `/logs`, and -- task 056 -- `/prd`) plus `POST /retry` and
    (#155) `POST /steering`, and records every request (method, path,
    Authorization header) for assertions -- plus, in parallel,
    `control_tokens` (one `X-Ralphd-Control-Token` header value per
    request, `None` when absent), since that header shares no tuple shape
    with the many existing 3-tuple `requests` assertions across this
    module's callers."""

    def __init__(self, status: dict | None = None, retry_status: int = 200,
                 retry_body: dict | None = None, tasks: dict | None = None,
                 prd: str | None = None, steering: list | None = None,
                 steer_status: int = 202, steer_body: dict | None = None,
                 rule_status: int = 202, rule_body: dict | None = None,
                 resume_status: int = 200, resume_body: dict | None = None,
                 interrupt_status: int = 200, interrupt_body: dict | None = None,
                 undefer_status: int = 200, undefer_body: dict | None = None,
                 on_undefer=None, answer_status: int = 200,
                 answer_body: dict | None = None):
        self.requests: list[tuple[str, str, str | None]] = []
        self.control_tokens: list[str | None] = []
        # Task 011 (v0.26, #269): the raw request body, one entry per
        # request recorded above (aligned index-for-index with `requests`/
        # `control_tokens`, `b""` for a GET or a body-less POST) -- so a
        # test can assert what actually arrived at this stand-in engine's
        # own HTTP endpoint byte-for-byte, not merely which route/method
        # was hit.
        self.bodies: list[bytes] = []
        self.status_body = status if status is not None else {"state": "running"}
        self.tasks_body = tasks if tasks is not None else {"tasks": []}
        self.retry_status = retry_status
        self.retry_body = retry_body if retry_body is not None else {"retrying": True}
        self.prd_body = prd
        # Task 016 (#17): what this run's live `GET /steering` answers with.
        # `None` means "route not there" (404) so a test can exercise the
        # hub's fallback while the container is still up.
        self.steering_body = steering
        # #155: what this run's live `POST /steering` answers with -- default
        # 202 mirrors the real engine's `POST /steering` (engine/api.py).
        self.steer_status = steer_status
        self.steer_body = steer_body if steer_body is not None else {"file": "001-x.md"}
        # Task 013 (v0.23 requirement A.2, #224): the operator ruling
        # channel's own route -- a DISTINCT one from `/steering` above,
        # never a variant of it.
        self.rule_status = rule_status
        self.rule_body = rule_body if rule_body is not None else {"file": "001-r.md"}
        # Task 006: the run's own `POST /resume` -- the hub's `unpause`
        # proxy target. Default 200 mirrors the real engine's `POST
        # /resume` (`engine/api.py`), which always answers `{"resumed":
        # true}` whether or not the run was actually paused.
        self.resume_status = resume_status
        self.resume_body = resume_body if resume_body is not None else {"resumed": True}
        # Task 001 (#277): the hub's "apply now" steer proxy target --
        # `POST /interrupt`, a DISTINCT route from `/steering` above.
        # Default mirrors the real engine's `target: "iteration"` 200 shape
        # (`engine/api.py`).
        self.interrupt_status = interrupt_status
        self.interrupt_body = (interrupt_body if interrupt_body is not None
                               else {"interrupted": True, "target": "iteration",
                                    "signalled": True, "woken": False})
        # #292: `POST /tasks/<id>/undefer` -- the hub's "Undefer now" proxy
        # target. `on_undefer(task_id)` runs on a 200 so a test can move the
        # run's tasks the way the real loop thread would.
        self.undefer_status = undefer_status
        self.undefer_body = undefer_body
        self.on_undefer = on_undefer
        # v0.36 C.2: the run's own `POST /answer` -- the hub answer panel's
        # proxy target. Default mirrors the real engine's 200 outcome record.
        self.answer_status = answer_status
        self.answer_body = (answer_body if answer_body is not None
                            else {"outcome": "cure"})
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code: int, obj, content_type="application/json"):
                payload = (json.dumps(obj) if content_type == "application/json"
                           else str(obj)).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(
                    (self.command, self.path, self.headers.get("Authorization")))
                outer.control_tokens.append(
                    self.headers.get("X-Ralphd-Control-Token"))
                outer.bodies.append(body)

            def do_GET(self):  # BaseHTTPRequestHandler API name
                self._record()
                path = self.path.split("?", 1)[0]
                if path == "/status":
                    self._reply(200, outer.status_body)
                elif path == "/tasks":
                    self._reply(200, outer.tasks_body)
                elif path == "/logs":
                    self._reply(200, "", "text/plain")
                elif path == "/prd":
                    if outer.prd_body is None:
                        self._reply(404, {"detail": "no PRD"})
                    else:
                        self._reply(200, outer.prd_body, "text/markdown")
                elif path == "/steering":
                    if outer.steering_body is None:
                        self._reply(404, {"detail": "no such route"})
                    else:
                        self._reply(200, outer.steering_body)
                else:
                    self._reply(404, {"detail": "no such route"})

            def do_POST(self):  # BaseHTTPRequestHandler API name
                self._record()
                path = self.path.split("?", 1)[0]
                if path == "/retry":
                    self._reply(outer.retry_status, outer.retry_body)
                elif path == "/steering":
                    self._reply(outer.steer_status, outer.steer_body)
                elif path == "/rule":
                    self._reply(outer.rule_status, outer.rule_body)
                elif path == "/answer":
                    self._reply(outer.answer_status, outer.answer_body)
                elif path == "/resume":
                    self._reply(outer.resume_status, outer.resume_body)
                elif path == "/interrupt":
                    self._reply(outer.interrupt_status, outer.interrupt_body)
                elif path.startswith("/tasks/") and path.endswith("/undefer"):
                    task_id = urllib.parse.unquote(path[len("/tasks/"):-len("/undefer")])
                    if outer.undefer_status == 200 and outer.on_undefer:
                        outer.on_undefer(task_id)
                    self._reply(outer.undefer_status,
                                outer.undefer_body if outer.undefer_body is not None
                                else {"undeferring": True, "taskId": task_id})
                else:
                    self._reply(404, {"detail": "no such route"})

            def log_message(self, *_a):  # silence stderr noise
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _write_run_with_api(registry: Path, run_id: str, engine: StubEngineApi,
                        token: str | None = None,
                        control_token: str | None = None,
                        **status_fields) -> Path:
    """A registry run dir whose `host.json` points at `engine`, so the hub
    treats it as a LIVE run (`live: true`, proxied status/tasks/logs).
    `control_token` (#155) seeds `.control-token` beside `.api-token`, the
    file `_proxy_json` forwards as `X-Ralphd-Control-Token` on a mutating
    request -- read from `<registry>/control/<run-id>/`, host-only and
    never bind-mounted into any container (task 003, requirement A, #201),
    the same location every real `ralphctl start`/`resume` provisions."""
    run_dir = _write_dead_run(registry, run_id, **status_fields)
    (run_dir / "host.json").write_text(json.dumps(
        {"runId": run_id, "port": engine.port,
         "apiUrl": f"http://127.0.0.1:{engine.port}"}))
    if token or control_token:
        # Task 003 (requirement A, #201): current layout keeps both
        # tokens host-only, under `<registry>/control/<run-id>/`, never a
        # subdirectory of `run_dir` (which a real run bind-mounts into its
        # container).
        ccdir = registry / "control" / run_id
        ccdir.mkdir(parents=True, exist_ok=True)
        if token:
            (ccdir / ".api-token").write_text(token)
        if control_token:
            (ccdir / ".control-token").write_text(control_token)
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
    # Task 004 (#15): the on-disk task read is served by the hardened reader,
    # so the payload states its provenance alongside the (here genuinely
    # empty) plan instead of an unlabelled default.
    assert body["tasks"] == {"tasks": [], "tasksStale": False, "tasksSource": "file"}


def test_run_detail_and_unknown_run_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, body = server.get("/api/runs/does-not-exist")
    assert code == 404
    assert "not found" in body["error"]


def test_dead_nonterminal_run_is_flagged_container_gone_in_list_and_detail(tmp_path, ui):
    """Task 024 (#8): a run whose status.json still records `running` while
    its API no longer answers died without recording a terminal state. The
    hub payload has to say so (`containerGone`), otherwise the UI can only
    show `live: false` -- which is also true of every *finished* run and so
    renders a zombie exactly like a healthy running run."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-zombie", state="running", verdict=None)
    _write_dead_run(registry, "run-starting-zombie", state="starting", verdict=None)
    # terminal runs are unreachable BY DESIGN -- never flagged
    _write_dead_run(registry, "run-done", state="succeeded", verdict="verified")
    _write_dead_run(registry, "run-failed", state="failed", verdict="unverified")

    server = ui(registry)
    code, body = server.get("/api/runs")
    assert code == 200
    gone = {r["runId"]: r["containerGone"] for r in body["runs"]}
    assert gone == {"run-zombie": True, "run-starting-zombie": True,
                    "run-done": False, "run-failed": False}

    for run_id, expected in (("run-zombie", True), ("run-done", False)):
        code, detail = server.get(f"/api/runs/{run_id}")
        assert code == 200
        assert detail["live"] is False
        assert detail["containerGone"] is expected, run_id


def test_live_nonterminal_run_is_not_flagged_container_gone(tmp_path, ui):
    """Task 024 (#8): the healthy half of the contract -- a `running` run
    whose API answers must stay unflagged in BOTH views (the run list's
    verdict comes from a cheap TCP probe, the detail view's from the real
    proxy call, and the two must not disagree)."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"runId": "run-alive", "state": "running"})
    try:
        _write_run_with_api(registry, "run-alive", engine, state="running", verdict=None)
        server = ui(registry)
        code, body = server.get("/api/runs")
        assert code == 200
        assert [r["containerGone"] for r in body["runs"]] == [False]
        code, detail = server.get("/api/runs/run-alive")
        assert code == 200
        assert detail["live"] is True
        assert detail["containerGone"] is False
    finally:
        engine.close()


def test_hub_and_cli_share_one_nonterminal_state_set(tmp_path):
    """Task 024 (#8): the hub's zombie condition and the CLI's must be built
    on the same `LOADABLE_NONTERMINAL_STATES` tuple -- one definition, no
    drift. (v0.36: that tuple is `NONTERMINAL_STATES` plus the 0.35
    spelling of the budget hold, so a run directory 0.35 left at `timed_out`
    still counts as owned by an engine.)"""
    from ralphd.cli import main as cli_main
    from ralphd.cli import ui_server as ui_mod
    from ralphd.engine.state import (LOADABLE_NONTERMINAL_STATES,
                                     NONTERMINAL_STATES)

    assert LOADABLE_NONTERMINAL_STATES == NONTERMINAL_STATES + ("timed_out",)
    assert cli_main._NONTERMINAL_STATUS_STATES is LOADABLE_NONTERMINAL_STATES
    assert ui_mod.LOADABLE_NONTERMINAL_STATES is LOADABLE_NONTERMINAL_STATES
    for state in LOADABLE_NONTERMINAL_STATES:
        assert ui_mod.container_gone({"state": state}, api_reachable=False) is True
        assert ui_mod.container_gone({"state": state}, api_reachable=True) is False
    for state in ("succeeded", "failed", "aborted", None):
        assert ui_mod.container_gone({"state": state}, api_reachable=False) is False


def test_run_detail_proxies_live_status_tasks_and_logs(tmp_path, live, ui):
    # task 010 (B.4): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" holds the
    # first worker iteration open instead of racing a real STUB_SLEEP
    # window to observe the run non-terminal ("live") through the hub.
    hold_file = tmp_path / "hold-hubtest"
    run = live(run_id="hubtest", job={"iterations": 12, "max_approaches": 3,
                                      "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
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
    # task 014: server-rendered lines (through the same `log_render`
    # module `ralphctl logs` uses), not raw NDJSON text -- so app.js
    # doesn't need to reimplement event-to-text rendering.
    assert isinstance(logs["lines"], list)
    assert all(isinstance(line, str) for line in logs["lines"])

    hold_file.touch()
    run.wait_terminal()


def _in_process_ralphctl(run, capsys):
    """Task 004 (requirement F): swap `LiveRun`'s default (real-subprocess)
    `.ralphctl()` for the in-process helper -- the parity assertion below
    only needs `ralphctl logs`'s rendered text, and `call_ralphctl_in_process`
    captures that through `capsys` exactly as non-TTY as a real subprocess's
    piped stdout would."""
    def ralphctl(*argv: str):
        env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
        return call_ralphctl_in_process(list(argv), env=env, capsys=capsys)
    run.ralphctl = ralphctl
    return run


def test_ui_log_endpoint_matches_cli_pretty_rendering(tmp_path, live, ui, capsys):
    """Task 014: the hub's `/api/runs/<id>/logs` endpoint must render
    through the EXACT SAME `log_render.render_to_lines` function
    `ralphctl logs` uses -- so this asserts the endpoint's `lines` list,
    joined with newlines, is byte-for-byte identical to what `ralphctl logs
    <id> --tail 0` prints, driven in-process (task 004, requirement F) so
    only the process BOUNDARY moved -- the rendering stays non-TTY exactly
    as a piped subprocess's stdout would be (this endpoint's own
    `tty=False` rendering), modulo the CLI's own trailing
    newline, which `logs` prints and a list-of-lines representation has
    no equivalent slot for."""
    run = _in_process_ralphctl(
        live(run_id="hubparity", job={"iterations": 12, "max_approaches": 3,
                                      "on_complete": "idle"},
             stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "1", "STUB_SLEEP": "0"}),
        capsys)
    run.wait_api()
    run.wait_terminal()

    server = ui(run.registry)
    code, logs = server.get(f"/api/runs/{run.run_id}/logs?tail=0")
    assert code == 200
    assert logs["live"] is True

    cli = run.ralphctl("logs", run.run_id, "--tail", "0")
    assert cli.returncode == 0, (cli.stdout, cli.stderr)
    cli_lines = cli.stdout.split("\n")
    if cli_lines and cli_lines[-1] == "":
        cli_lines = cli_lines[:-1]  # `print()`'s trailing newline

    assert logs["lines"] == cli_lines
    # sanity: this fixture actually exercises the thinking-collapse path
    # this task fixes (each iteration collapses its own thinking-delta
    # burst to exactly one '[thinking…]' line, not one per delta), and
    # both renderings agree on how many such lines resulted.
    ui_thinking = sum(1 for line in logs["lines"] if "[thinking" in line)
    cli_thinking = sum(1 for line in cli_lines if "[thinking" in line)
    assert ui_thinking == cli_thinking >= 1


def _write_iteration(run_dir: Path, n: int, *, phase: str, lines: list[str]) -> None:
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"number": n, "phase": phase, "model": "stub-model", "approach": 1,
         "startedAt": f"2026-01-01T00:0{n}:00Z", "exitCode": 0, "error": None,
         "endedAt": f"2026-01-01T00:0{n}:30Z", "usage": {"totalTokens": 10 * n}}))
    (d / "output.jsonl").write_text("".join(line + "\n" for line in lines))


def test_log_endpoint_falls_back_to_on_disk_merge_for_a_dead_run(tmp_path, ui):
    """Task 039 (#6): the hub's log tail used to be live-only -- an
    unreachable run (container died, or simply finished) answered
    `{live: false, lines: []}`, so the operator investigating a dead run in
    the hub saw an empty box even though the transcript was sitting on disk
    in `iterations/*/output.jsonl`. Now the endpoint falls back to
    `log_merge.merged_lines` (the SAME merge the engine serves from inside
    the container, task 038) and only `live` goes false."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "gone-with-logs", state="running")
    _write_iteration(run_dir, 1, phase="planning", lines=[
        json.dumps({"type": "message_start"}),
        json.dumps({"type": "message_end", "message": {
            "content": [{"type": "text", "text": "on-disk planning line"}]}})])
    _write_iteration(run_dir, 2, phase="worker", lines=[
        json.dumps({"type": "message_end", "message": {
            "content": [{"type": "text", "text": "on-disk worker line"}]}})])

    server = ui(registry)
    code, logs = server.get("/api/runs/gone-with-logs/logs?tail=200")
    assert code == 200
    assert logs["live"] is False
    assert logs["lines"], "a dead run's on-disk transcript must still render"
    joined = "\n".join(logs["lines"])
    assert "on-disk planning line" in joined
    assert "on-disk worker line" in joined
    # boundary lines are synthesized by the shared merge, not by the API
    assert "iteration" in joined


def test_log_endpoint_on_disk_lines_match_the_shared_merge(tmp_path, ui):
    """Task 039: the fallback must be the shared module's output, not a
    second reimplementation -- so the endpoint's lines equal rendering
    `log_merge.merged_lines` for the same run dir."""
    from ralphd.cli.log_render import new_render_state, render_to_lines
    from ralphd.log_merge import merged_lines

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "gone-parity", state="failed")
    _write_iteration(run_dir, 1, phase="worker", lines=[
        json.dumps({"type": "message_end", "message": {
            "content": [{"type": "text", "text": "parity line one"}]}}),
        json.dumps({"type": "message_end", "message": {
            "content": [{"type": "text", "text": "parity line two"}]}})])

    server = ui(registry)
    code, logs = server.get("/api/runs/gone-parity/logs?tail=0")
    assert code == 200
    expected = render_to_lines("".join(merged_lines(run_dir)), tty=False,
                              state=new_render_state())
    assert logs["lines"] == expected


def test_steering_post_proxies_and_creates_steering_file(tmp_path, live, ui):
    # task 010 (B.4): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" holds the
    # first worker iteration open instead of racing a real STUB_SLEEP
    # window to POST the steering message while the run is still running.
    hold_file = tmp_path / "hold-hubsteer"
    run = live(run_id="hubsteer", job={"iterations": 12, "max_approaches": 3,
                                       "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()

    server = ui(run.registry)
    code, resp = server.post(f"/api/runs/{run.run_id}/steer",
                              {"message": "hub steering message", "name": "hubtest"})
    assert code == 202, resp
    assert "file" in resp

    steering_files = list((run.run_dir / "steering").glob("*.md"))
    assert steering_files, "steering POST via the hub never wrote a file"
    assert any("hub steering message" in f.read_text() for f in steering_files)

    hold_file.touch()
    run.wait_terminal()


def test_steering_post_unknown_run_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, _resp = server.post("/api/runs/nope/steer", {"message": "x"})
    assert code == 404


# -- task 017 (#5): POST /api/runs/<id>/retry proxy -------------------------


def test_retry_proxy_posts_to_the_runs_retry_and_forwards_the_token(tmp_path, ui):
    """The hub's "retry now" button (task 017) needs a same-origin route to
    reach the run's container API, since the page is served by the hub while
    the engine lives on another port behind a bearer token. Assert the proxy
    really issues `POST /retry` against the run's recorded `apiUrl`,
    forwards the run dir's `.api-token`, and hands the engine's body back
    verbatim."""
    registry = tmp_path / "registry"
    engine = StubEngineApi()
    try:
        _write_run_with_api(registry, "run-degraded", engine, token="hub-token",
                            state="running", health="degraded")
        server = ui(registry)
        code, body = server.post("/api/runs/run-degraded/retry", {})
        assert code == 200, (code, body)
        assert body == {"retrying": True}
        posts = [(m, p, t) for m, p, t in engine.requests if m == "POST"]
        assert [(m, p) for m, p, _t in posts] == [("POST", "/retry")], engine.requests
        assert posts[0][2] == "Bearer hub-token"
    finally:
        engine.close()


def test_steer_and_retry_proxies_forward_the_control_token_and_surface_a_401_reason(
        tmp_path, ui):
    """#155: the hub's steering form (`POST /api/runs/<id>/steer`) and its
    "retry now" button (`POST /api/runs/<id>/retry`) are the two mutating
    proxies `_proxy_json` forwards to a run's live container API. A real
    run's mutating routes gate on a SECOND, independent secret --
    `X-Ralphd-Control-Token`, checked against `.control-token` -- which
    `ralphctl start`/`resume` provisions unconditionally (see main.py's
    `api()` helper forwarding the same header for the CLI's own mutating
    commands). Before this fix `_proxy_json` forwarded only `.api-token`
    (`Authorization`), so every hub-initiated steer/retry against a real
    run 401'd.

    Two things pinned in one node, both against the SAME engine double so
    there is no room for the two proxies to drift out of sync with each
    other: (1) the control token is present, unforged, on BOTH requests;
    (2) when the engine refuses with the control-token gate's own 401 (the
    literal shape `engine/api.py`'s `auth` middleware sends), the hub's JSON
    answer still carries that reason rather than flattening it away -- the
    same body shape `app.js`'s steer-form and retry-button failure branches
    both parse (via the shared `innermostReason` helper, task 069) to
    render it."""
    registry = tmp_path / "registry"
    token = "the-run-control-token"
    unauthorized = {"title": "unauthorized", "status": 401,
                    "detail": "mutating routes require the control token"}
    engine = StubEngineApi()
    try:
        _write_run_with_api(registry, "run-ctl", engine, control_token=token,
                            state="running", health="degraded")
        server = ui(registry)

        # -- (1) the token is present, and correct, on both mutating proxies
        code, body = server.post("/api/runs/run-ctl/steer", {"message": "hi"})
        assert code == 202, body
        code, body = server.post("/api/runs/run-ctl/retry", {})
        assert code == 200, body
        posts = [(p, t) for m, p, t in engine.requests if m == "POST"]
        assert [p for p, _t in posts] == ["/steering", "/retry"], engine.requests
        assert engine.control_tokens == [token, token], engine.control_tokens

        # -- (2) a control-token rejection's reason survives the proxy
        engine.steer_status, engine.steer_body = 401, dict(unauthorized)
        engine.retry_status, engine.retry_body = 401, dict(unauthorized)
        code, steer_resp = server.post("/api/runs/run-ctl/steer", {"message": "hi"})
        assert code == 401, steer_resp
        assert steer_resp["detail"]["detail"] == \
            "mutating routes require the control token"
        code, retry_resp = server.post("/api/runs/run-ctl/retry", {})
        assert code == 401, retry_resp
        assert retry_resp["detail"] == "mutating routes require the control token"
    finally:
        engine.close()

    # -- both failure branches in app.js render exactly that nested reason,
    # not the generic wrapper string or a bare status code -- same shared
    # helper, so the same fallback order, for the same underlying cause
    # (task 069, #155, requirement G.7: `innermostReason` walks the nested
    # `detail` chain instead of each branch spelling its own `||` chain).
    app_js = (Path(__file__).parents[1] / "src" / "ralphd" / "cli" / "web"
              / "app.js").read_text()
    steer_branch = app_js[app_js.index('getElementById("steer-form").addEventListener'):]
    steer_branch = steer_branch[:steer_branch.index("function renderRetryNow")]
    assert "innermostReason(j" in steer_branch
    retry_branch = app_js[app_js.index("function renderRetryNow"):]
    assert "innermostReason(j" in retry_branch


def test_retry_proxy_passes_the_engines_409_refusal_through(tmp_path, ui):
    """`POST /retry` answers 409 when the run is not sitting in an infra
    wait (docs/api.md). The hub must pass that code AND its problem detail
    through rather than flattening every non-200 into a generic failure --
    otherwise the UI cannot tell "nothing to wake" from a real error."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(retry_status=409, retry_body={
        "title": "not waiting on an infra fault",
        "detail": "/retry only wakes a run whose /status shows health 'degraded'",
    })
    try:
        _write_run_with_api(registry, "run-not-waiting", engine, state="running")
        server = ui(registry)
        code, body = server.post("/api/runs/run-not-waiting/retry", {})
        assert code == 409, (code, body)
        assert "not waiting on an infra fault" in json.dumps(body)
    finally:
        engine.close()


def test_retry_proxy_returns_503_for_an_unreachable_run(tmp_path, ui):
    """A dead run's card is read-only in the UI (no button rendered at all),
    but the route itself must still degrade to a clean 503 -- never a hang
    or a 500 -- if something posts to it anyway."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "gone", state="running")  # no host.json at all
    server = ui(registry)
    code, body = server.post("/api/runs/gone/retry", {})
    assert code == 503, (code, body)
    assert "unreachable" in body["error"]


def test_retry_proxy_unknown_run_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, body = server.post("/api/runs/nope/retry", {})
    assert code == 404
    assert "not found" in body["error"]


def _get_raw(server, path):
    """Fetch a (possibly non-JSON) path, returning (status, content-type, body bytes)."""
    try:
        with urllib.request.urlopen(f"{server.base}{path}", timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def test_static_bundle_serves_index_html_at_root_and_by_name(tmp_path, ui):
    """Task 034: the static hub bundle is now packaged -- `/` and
    `/index.html` both serve it with a text/html content type."""
    registry = tmp_path / "registry"
    server = ui(registry)
    for path in ("/", "/index.html"):
        code, ctype, body = _get_raw(server, path)
        assert code == 200, (path, code, body[:200])
        assert "text/html" in ctype
        assert b"<html" in body.lower()
        assert b"ralphd hub" in body.lower()


def test_static_bundle_serves_js_and_css_with_correct_content_types(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, ctype, body = _get_raw(server, "/app.js")
    assert code == 200
    assert "javascript" in ctype
    assert b"fetch(" in body  # the JS talks to the JSON endpoints only

    code, ctype, body = _get_raw(server, "/style.css")
    assert code == 200
    assert "text/css" in ctype
    assert len(body) > 0


def test_static_bundle_js_has_no_npm_or_node_build_artifacts(tmp_path):
    """No build step: plain hand-written files only, no package.json /
    node_modules / bundler output shipped alongside the hub bundle."""
    from ralphd.cli.ui_server import STATIC_DIR
    assert STATIC_DIR.is_dir()
    names = {p.name for p in STATIC_DIR.iterdir()}
    assert "node_modules" not in names
    assert "package.json" not in names
    assert "package-lock.json" not in names
    for js_file in STATIC_DIR.glob("*.js"):
        text = js_file.read_text()
        assert "//# sourceMappingURL" not in text  # bundler fingerprint
        assert "webpack" not in text.lower()


def test_static_bundle_unknown_path_falls_back_to_spa_index(tmp_path, ui):
    """A path that isn't a real asset (e.g. `/run/some-id` client-side hash
    route rendered without a hash by a non-JS client) still serves
    index.html rather than a bare 404, matching an SPA's routing model."""
    registry = tmp_path / "registry"
    server = ui(registry)
    code, ctype, body = _get_raw(server, "/run/does-not-exist-as-a-file")
    assert code == 200
    assert "text/html" in ctype
    assert b"ralphd hub" in body.lower()


def test_wheel_packages_the_static_bundle():
    """Successcriteria: verified by building the wheel and listing its
    contents -- proves task 034's files aren't merely present in the repo
    but actually ship inside the installable artifact."""
    import subprocess
    import sys
    import tempfile
    import zipfile
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as td:
        res = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "-o", td],
            cwd=repo_root, capture_output=True, text=True, timeout=180)
        assert res.returncode == 0, res.stdout + res.stderr
        wheels = list(Path(td).glob("*.whl"))
        assert wheels, "no wheel produced"
        with zipfile.ZipFile(wheels[0]) as zf:
            names = set(zf.namelist())
    web_files = {n for n in names if "/cli/web/" in n}
    assert any(n.endswith("index.html") for n in web_files), web_files
    assert any(n.endswith("app.js") for n in web_files), web_files
    assert any(n.endswith("style.css") for n in web_files), web_files


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


# -- PRD requirement H (task 002): the hub's bind-address guard and its
# startup line naming the mutating routes it publishes ---------------------

# Spelled out in full, on purpose: a startup line that merely mentions
# *some* `/api/runs/` route satisfies nothing -- requirement H asks for the
# mutating routes by identity, the run delete route and the steer route
# above all, so these literals are the identities themselves and a line
# naming some other route (say a retry route) in place of either one fails
# here.
_HUB_DELETE_ROUTE = "DELETE /api/runs/<id>"
_HUB_STEER_ROUTE = "POST /api/runs/<id>/steer"
_HUB_RETRY_ROUTE = "POST /api/runs/<id>/retry"
_HUB_RULE_ROUTE = "POST /api/runs/<id>/rule"
_HUB_MUTATING_ROUTE_STRINGS = (_HUB_DELETE_ROUTE, _HUB_STEER_ROUTE,
                               _HUB_RETRY_ROUTE, _HUB_RULE_ROUTE)
_HUB_NON_LOOPBACK_FLAG = "--allow-non-loopback-bind"


def test_non_loopback_bind_without_flag_refuses_naming_address_and_flag(tmp_path):
    """A non-loopback `--bind` with no opt-in flag must exit non-zero, and
    the message must name both the address that was rejected and the flag
    that would have allowed it -- never a generic refusal."""
    registry = tmp_path / "reg"
    registry.mkdir()
    port = free_port()
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    res = subprocess.run(
        [str(RALPHCTL), "ui", "--port", str(port), "--bind", "0.0.0.0"],
        env=env, capture_output=True, text=True, timeout=15)
    assert res.returncode != 0, (res.stdout, res.stderr)
    combined = res.stdout + res.stderr
    assert "0.0.0.0" in combined, combined
    assert _HUB_NON_LOOPBACK_FLAG in combined, combined


def test_non_loopback_bind_with_flag_serves_and_names_routes_on_startup(tmp_path, ui):
    """The same non-loopback bind, with the opt-in flag, must actually
    start and serve -- and its startup line must name the mutating routes
    plus the no-authentication fact."""
    registry = tmp_path / "reg"
    registry.mkdir()
    server = ui(registry, bind="0.0.0.0", extra_args=(_HUB_NON_LOOPBACK_FLAG,))
    status, body = server.get("/api/runs")
    assert status == 200
    assert body == {"runs": []}
    out = server.proc.stdout
    # drain whatever startup output is already buffered without blocking on
    # a running, un-terminated process
    import select
    startup_line = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        ready, _, _ = select.select([out], [], [], 0.2)
        if ready:
            line = out.readline()
            if not line:
                break
            startup_line += line
            if "serving hub" in startup_line:
                break
        else:
            if startup_line:
                break
    assert "0.0.0.0" in startup_line, startup_line
    for route in _HUB_MUTATING_ROUTE_STRINGS:
        assert route in startup_line, startup_line
    assert "no authentication of its own" in startup_line, startup_line


def test_loopback_bind_without_flag_is_unchanged(tmp_path, ui):
    """A loopback bind needs no opt-in flag -- unchanged behaviour -- and
    its startup line still names the mutating routes and the
    no-authentication fact."""
    registry = tmp_path / "reg"
    registry.mkdir()
    server = ui(registry)  # default bind=127.0.0.1, no extra_args
    status, body = server.get("/api/runs")
    assert status == 200
    assert body == {"runs": []}
    out = server.proc.stdout
    import select
    startup_line = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        ready, _, _ = select.select([out], [], [], 0.2)
        if ready:
            line = out.readline()
            if not line:
                break
            startup_line += line
            if "serving hub" in startup_line:
                break
        else:
            if startup_line:
                break
    assert "127.0.0.1" in startup_line, startup_line
    for route in _HUB_MUTATING_ROUTE_STRINGS:
        assert route in startup_line, startup_line
    assert "no authentication of its own" in startup_line, startup_line
