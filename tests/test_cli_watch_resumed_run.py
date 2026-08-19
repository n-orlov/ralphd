"""Task 031 (#13): `ralphctl watch` must not close on a *historical*
terminal-state event.

A run dir's `events.jsonl` is append-only across resumes and the follower
replays it from id 0, so the first `state: succeeded|failed|aborted` event a
resumed run's stream carries may be the previous episode's marker. Closing
there made `watch` (and the documented completion-wait idiom) declare a
still-working job finished.

Black-box: the real `ralphctl watch` executable is pointed at a temp registry
whose `host.json` names a stub engine API that streams the run dir's
`events.jsonl` as SSE (exactly like the real `GET /events`) and answers
`GET /status` with a state the test controls.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.conftest import RALPHCTL


class StubEventApi:
    """Streams `<run dir>/events.jsonl` as SSE from `GET /events` (tailing
    the file the way the engine does) and serves a mutable `GET /status`."""

    def __init__(self, events_path: Path, state: str = "running"):
        self.events_path = events_path
        self.status = {"state": state}
        self.stopping = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # BaseHTTPRequestHandler API name
                path = self.path.split("?", 1)[0]
                if path == "/status":
                    payload = json.dumps(outer.status).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path != "/events":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                pos = 0
                try:
                    while not outer.stopping:
                        if outer.events_path.exists():
                            with open(outer.events_path) as f:
                                f.seek(pos)
                                while line := f.readline():
                                    pos = f.tell()
                                    try:
                                        ev = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue
                                    self.wfile.write(
                                        f"id: {ev['id']}\nevent: {ev['type']}\n"
                                        f"data: {json.dumps(ev)}\n\n".encode())
                                    self.wfile.flush()
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def log_message(self, *_a):  # silence stderr noise
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.stopping = True
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class Watcher:
    """`ralphctl --json watch <id>` as a subprocess, with its stdout drained
    by a reader thread so assertions can look at what has arrived so far."""

    def __init__(self, registry: Path, run_id: str):
        env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
        self.proc = subprocess.Popen(
            [str(RALPHCTL), "--json", "watch", run_id], env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.lines: list[str] = []
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        for line in self.proc.stdout:
            self.lines.append(line.strip())

    def events(self) -> list[dict]:
        out = []
        for line in list(self.lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def wait_for_event(self, predicate, timeout=15) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for ev in self.events():
                if predicate(ev):
                    return ev
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        raise AssertionError(f"event never arrived; got {self.lines}, "
                             f"rc={self.proc.poll()}")

    def wait_exit(self, timeout=20) -> int:
        return self.proc.wait(timeout=timeout)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


def _seed(registry: Path, run_id: str, events: list[dict],
          state: str = "running") -> tuple[Path, StubEventApi]:
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"runId": run_id, "state": state}))
    _append(run_dir, events)
    api = StubEventApi(run_dir / "events.jsonl", state=state)
    (run_dir / "host.json").write_text(json.dumps(
        {"runId": run_id, "port": api.port, "apiUrl": f"http://127.0.0.1:{api.port}"}))
    return run_dir, api


def _append(run_dir: Path, events: list[dict]) -> None:
    with open(run_dir / "events.jsonl", "a") as f:
        f.writelines(json.dumps(ev) + "\n" for ev in events)


@pytest.fixture
def watch_env(tmp_path):
    apis: list[StubEventApi] = []
    watchers: list[Watcher] = []

    def make(run_id: str, events: list[dict], state: str = "running"):
        registry = tmp_path / "registry"
        run_dir, api = _seed(registry, run_id, events, state=state)
        apis.append(api)
        w = Watcher(registry, run_id)
        watchers.append(w)
        return run_dir, api, w

    yield make
    for w in watchers:
        w.kill()
    for a in apis:
        a.close()


def test_watch_streams_past_historical_terminal_marker_on_resumed_run(watch_env):
    """The #13 scenario: a resumed run's log carries the *previous*
    episode's `state: succeeded` mid-log, followed by the new episode's
    events, and the engine is live. `watch` must stream past the marker and
    block until the real terminus."""
    run_dir, api, w = watch_env("resumed-run", [
        {"id": 1, "ts": "2026-01-01T00:00:00Z", "type": "log",
         "message": "iteration 1 (approach 1)"},
        {"id": 2, "ts": "2026-01-01T00:10:00Z", "type": "state",
         "state": "succeeded"},          # historical marker: episode 1 ended
        {"id": 3, "ts": "2026-01-02T00:00:00Z", "type": "log",
         "message": "resumed: iteration 4 (approach 2)"},
    ])

    # It streamed the post-marker event instead of closing on the marker.
    w.wait_for_event(lambda ev: ev.get("message", "").startswith("resumed:"))
    assert w.proc.poll() is None, "watch closed on a historical terminal marker"

    # ... and keeps blocking while the engine works.
    time.sleep(1.5)
    api.status = {"state": "running"}
    assert w.proc.poll() is None
    w.wait_for_event(lambda ev: ev.get("id") == 3)

    # The real terminus does end the stream.
    _append(run_dir, [{"id": 4, "ts": "2026-01-02T00:05:00Z", "type": "state",
                       "state": "failed"}])
    api.status = {"state": "failed"}
    assert w.wait_exit() == 0
    states = [ev for ev in w.events() if ev["type"] == "state"]
    assert [ev["state"] for ev in states] == ["succeeded", "failed"]


def test_watch_on_live_run_still_closes_on_its_own_terminal_event(watch_env):
    """Unchanged behaviour: a single-episode run's terminal event is the
    log's last word and the engine is no longer running -> stream ends."""
    _run_dir, _api, w = watch_env("live-run", [
        {"id": 1, "ts": "2026-01-01T00:00:00Z", "type": "log", "message": "iteration 1"},
        {"id": 2, "ts": "2026-01-01T00:01:00Z", "type": "state", "state": "succeeded"},
    ], state="succeeded")

    started = time.time()
    assert w.wait_exit() == 0
    assert time.time() - started < 15
    assert [ev["state"] for ev in w.events() if ev["type"] == "state"] == ["succeeded"]


def test_watch_on_finished_resumed_run_closes_at_the_final_marker(watch_env):
    """A resumed run that has since finished: the historical marker is
    superseded by a later state event, so the stream runs on to the final
    one and ends there (no hang on an already-finished run)."""
    _run_dir, _api, w = watch_env("resumed-and-done", [
        {"id": 1, "ts": "2026-01-01T00:00:00Z", "type": "state", "state": "failed"},
        {"id": 2, "ts": "2026-01-02T00:00:00Z", "type": "log", "message": "resumed"},
        {"id": 3, "ts": "2026-01-02T00:09:00Z", "type": "state", "state": "succeeded"},
    ], state="succeeded")

    assert w.wait_exit() == 0
    assert [ev["state"] for ev in w.events() if ev["type"] == "state"] == [
        "failed", "succeeded"]


def test_trailing_log_events_after_the_terminal_marker_do_not_hold_the_stream(watch_env):
    """`on_complete_cmd` log events are emitted strictly *after* the
    terminal state event (engine/main.py), so trailing non-state events must
    not be mistaken for 'the run moved on'."""
    _run_dir, _api, w = watch_env("with-hook", [
        {"id": 1, "ts": "2026-01-01T00:00:00Z", "type": "state", "state": "succeeded"},
        {"id": 2, "ts": "2026-01-01T00:00:01Z", "type": "log",
         "message": "on_complete_cmd finished (rc=0)"},
    ], state="succeeded")

    assert w.wait_exit() == 0
