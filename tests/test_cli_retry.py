"""Task 016 (#5): black-box tests for `ralphctl retry <run-id>`.

`retry` is the operator-facing side of `POST /retry` (task 015): it wakes a
degraded run's infra backoff wait immediately instead of letting the
escalating schedule run its course, and resets the outage-budget episode
clock. The engine-side semantics (interruptible wait, episode reset, the 409
when the run is not waiting) are proven in tests/test_retry_now.py against a
real loop; what is tested here is the CLI contract:

- the verb exists and posts `POST /retry` (path/method asserted from a stub
  HTTP server standing in for the engine API, addressed through the run's
  recorded `host.json` `apiUrl`);
- the bearer token from the run dir's `.api-token` is forwarded;
- exit codes match the other control commands (`pause`/`unpause`/`abort`,
  which share the same `api()` helper): 0 on success, 5 on the engine's 409
  "not waiting on an infra fault" path, 3 for an unknown run, 4 when the API
  is unreachable;
- `--json` prints the engine's JSON body verbatim.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock"]

_NOT_WAITING = {
    "title": "not waiting on an infra fault",
    "detail": "/retry only wakes a run whose /status shows health 'degraded'",
}


class _StubEngine:
    """Minimal stand-in for the engine API: records requests, replies with a
    canned status/body per path."""

    def __init__(self, status: int, body: dict):
        self.requests: list[tuple[str, str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # BaseHTTPRequestHandler API name
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.requests.append(
                    (self.command, self.path, self.headers.get("Authorization")))
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

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


def _seed(c: Ctl, run_id: str, engine: _StubEngine | None = None,
          token: str | None = None) -> None:
    rdir, _cdir = _seed_run(c, run_id, token=token)
    meta = json.loads((rdir / "host.json").read_text())
    port = engine.port if engine is not None else 1
    meta["port"] = port
    meta["apiUrl"] = f"http://127.0.0.1:{port}"
    (rdir / "host.json").write_text(json.dumps(meta))
    (rdir / "status.json").write_text(json.dumps({"state": "running"}))


@pytest.fixture
def engine():
    made: list[_StubEngine] = []

    def make(status: int = 200, body: dict | None = None) -> _StubEngine:
        e = _StubEngine(status, body if body is not None else {"retrying": True})
        made.append(e)
        return e

    yield make
    for e in made:
        e.close()


# --------------------------------------------------------------------------
def test_retry_posts_to_retry_and_exits_0(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-retry", eng)
    res = ctl.run("retry", "tst-retry")
    assert res.returncode == 0, res.stderr
    assert eng.requests and eng.requests[0][:2] == ("POST", "/retry")
    assert "retrying now" in res.stdout
    # the outage-clock reset is part of the contract the operator is told about
    assert "outage budget clock reset" in res.stdout


def test_retry_forwards_the_api_token(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-retry-token", eng, token="s3cret-token")
    res = ctl.run("retry", "tst-retry-token")
    assert res.returncode == 0, res.stderr
    assert eng.requests[0][2] == "Bearer s3cret-token"


def test_retry_json_prints_the_engine_body(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-retry-json", eng)
    res = ctl.run("--json", "retry", "tst-retry-json")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"retrying": True}


def test_retry_when_not_waiting_exits_5_with_the_engine_explanation(ctl: Ctl, engine):
    """Same exit code pause/abort use for the engine's 409 refusals, so a
    script can tell "nothing to wake" from a real failure."""
    eng = engine(409, _NOT_WAITING)
    _seed(ctl, "tst-retry-409", eng)
    res = ctl.run("retry", "tst-retry-409")
    assert res.returncode == 5, (res.returncode, res.stderr)
    assert "409" in res.stderr
    assert "not waiting on an infra fault" in res.stderr
    assert res.stdout == ""


def test_retry_unknown_run_exits_3(ctl: Ctl):
    res = ctl.run("retry", "no-such-run")
    assert res.returncode == 3, res.stderr
    assert "not found" in res.stderr


def test_retry_unreachable_api_exits_4(ctl: Ctl):
    _seed(ctl, "tst-retry-dead")  # apiUrl points at a closed port
    res = ctl.run("retry", "tst-retry-dead")
    assert res.returncode == 4, (res.returncode, res.stderr)
    assert "unreachable" in res.stderr


def test_retry_is_documented_next_to_pause(ctl: Ctl):
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text()
    assert "### `ralphctl retry <run-id>`" in doc
    section = doc.split("### `ralphctl retry <run-id>`", 1)[1].split("\n### ", 1)[0]
    assert "outage-budget episode clock" in section
    assert "unpause" in section
    help_out = ctl.run("--help")
    assert "retry" in help_out.stdout
