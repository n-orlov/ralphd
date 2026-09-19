"""Task 013 (v0.23 PRD requirement A.2, #224): black-box tests for
`ralphctl rule <run-id> --file ruling.md --name <slug>`.

`rule` is the operator-facing side of `POST /rule` (engine-side semantics
-- storage, signature resolution, prompt injection -- are proven against a
real supervisor in tests/test_operator_rule_channel.py). What is tested
here is the CLI contract:

- the verb POSTs `POST /rule` with `{"text": <file contents>, "name": ...}`
  (through a stub HTTP server standing in for the engine API, addressed
  through the run's recorded `host.json` `apiUrl`, same harness as
  test_cli_retry.py/test_cli_budget.py);
- the bearer token from the run dir's `.api-token` is forwarded;
- **a bare positional ruling message is refused with a non-zero exit
  naming `--file`** -- the success criterion's own named incident (#224:
  a positional message slot has previously swallowed a ruling silently
  while `--file` was ignored) -- and this refusal never reaches the
  network at all;
- `--json` prints the engine's JSON body verbatim;
- exit codes match the other control commands: 3 unknown run, 4
  unreachable API.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock"]


class _StubEngine:
    """Minimal stand-in for the engine API: records method/path/body,
    replies with a canned status + JSON body."""

    def __init__(self, status: int, body: dict):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # BaseHTTPRequestHandler API name
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(raw) if raw else None,
                })
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

    def make(status: int = 202, body: dict | None = None) -> _StubEngine:
        e = _StubEngine(status, body if body is not None else {"file": "001-ruling.md"})
        made.append(e)
        return e

    yield make
    for e in made:
        e.close()


# --------------------------------------------------------------------------
def test_rule_posts_file_contents_and_exits_0(ctl: Ctl, engine, tmp_path):
    eng = engine()
    _seed(ctl, "tst-rule", eng)
    rfile = tmp_path / "ruling.md"
    rfile.write_text("These three findings are residuals; ship it.")
    res = ctl.run("rule", "tst-rule", "--file", str(rfile), "--name", "residuals")
    assert res.returncode == 0, res.stderr
    assert len(eng.requests) == 1
    req = eng.requests[0]
    assert (req["method"], req["path"]) == ("POST", "/rule")
    assert req["body"]["text"] == "These three findings are residuals; ship it."
    assert req["body"]["name"] == "residuals"
    assert "ruling recorded" in res.stdout


def test_rule_forwards_the_api_token(ctl: Ctl, engine, tmp_path):
    eng = engine()
    _seed(ctl, "tst-rule-token", eng, token="s3cret-token")
    rfile = tmp_path / "ruling.md"
    rfile.write_text("Waived.")
    res = ctl.run("rule", "tst-rule-token", "--file", str(rfile))
    assert res.returncode == 0, res.stderr
    assert eng.requests[0]["auth"] == "Bearer s3cret-token"


def test_rule_json_prints_the_engine_body(ctl: Ctl, engine, tmp_path):
    eng = engine(body={"file": "007-x.md"})
    _seed(ctl, "tst-rule-json", eng)
    rfile = tmp_path / "ruling.md"
    rfile.write_text("text")
    res = ctl.run("--json", "rule", "tst-rule-json", "--file", str(rfile))
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"file": "007-x.md"}


def test_rule_bare_positional_message_is_refused_naming_file(ctl: Ctl, engine, tmp_path):
    """The success criterion's own named incident (#224): a positional
    message slot has previously swallowed a ruling silently while `--file`
    was ignored. There must be no positional ruling-text form at all, and
    the refusal must never reach the network."""
    eng = engine()
    _seed(ctl, "tst-rule-bare", eng)
    res = ctl.run("rule", "tst-rule-bare", "some ruling text typed inline")
    assert res.returncode != 0
    assert "--file" in res.stderr or "--file" in res.stdout
    assert eng.requests == []


def test_rule_without_file_flag_is_refused_naming_file(ctl: Ctl, engine, tmp_path):
    eng = engine()
    _seed(ctl, "tst-rule-nofile", eng)
    res = ctl.run("rule", "tst-rule-nofile")
    assert res.returncode != 0
    assert "--file" in res.stderr or "--file" in res.stdout
    assert eng.requests == []


def test_rule_empty_file_is_refused_locally(ctl: Ctl, engine, tmp_path):
    eng = engine()
    _seed(ctl, "tst-rule-empty", eng)
    rfile = tmp_path / "empty.md"
    rfile.write_text("   \n")
    res = ctl.run("rule", "tst-rule-empty", "--file", str(rfile))
    assert res.returncode != 0
    assert eng.requests == []


def test_rule_unknown_run_exits_3(ctl: Ctl, tmp_path):
    rfile = tmp_path / "ruling.md"
    rfile.write_text("text")
    res = ctl.run("rule", "no-such-run", "--file", str(rfile))
    assert res.returncode == 3, res.stderr
    assert "not found" in res.stderr


def test_rule_unreachable_api_exits_4(ctl: Ctl, tmp_path):
    _seed(ctl, "tst-rule-unreachable", None)
    rfile = tmp_path / "ruling.md"
    rfile.write_text("text")
    res = ctl.run("rule", "tst-rule-unreachable", "--file", str(rfile))
    assert res.returncode == 4
