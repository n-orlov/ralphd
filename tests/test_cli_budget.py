"""Task 046 (#3): black-box tests for `ralphctl budget <run-id> +N|N`.

`budget` is the operator-facing side of `PATCH /config/budget` (task 045): it
raises (or lowers) a *running* job's iteration budget without restarting the
container. The engine-side semantics (spec resolution, the 409 below
`iterationsUsed`, the audit event) are proven in tests/test_budget_patch.py
against a real app; what is tested here is the CLI contract:

- the verb round-trips: it PATCHes `/config/budget` with the operator's spec
  verbatim (`"+10"` stays relative, `"40"` stays absolute) and prints the
  engine's before/after numbers;
- exit codes match the other control commands (`pause`/`unpause`/`retry`,
  which share the same `api()` helper): 0 applied, 5 on the engine's 409
  refusals, 1 on its 422, 3 unknown run, 4 unreachable API, plus 2 for a
  locally malformed spec (usage error, same as `resume --iterations`);
- `--json` prints the engine's JSON body verbatim.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock"]

_OK_BODY = {"iterations": 40, "previous": 25, "iterationsUsed": 17}
_BELOW_USED = {
    "title": "budget below iterations used",
    "status": 409,
    "detail": "5 is below the 17 iteration(s) already used",
}
_INVALID = {
    "title": "invalid iterations",
    "status": 422,
    "detail": "'abc' is not an integer or a \"+N\" top-up",
}


class _StubEngine:
    """Minimal stand-in for the engine API: records method/path/body, replies
    with a canned status + JSON body."""

    def __init__(self, status: int, body: dict):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_PATCH(self):  # BaseHTTPRequestHandler API name
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

    def make(status: int = 200, body: dict | None = None) -> _StubEngine:
        e = _StubEngine(status, body if body is not None else _OK_BODY)
        made.append(e)
        return e

    yield make
    for e in made:
        e.close()


# --------------------------------------------------------------------------
@pytest.mark.parametrize("spec", ["+10", "40"])
def test_budget_round_trips_the_spec_verbatim(ctl: Ctl, engine, spec):
    """Relative and absolute forms both reach the engine unmangled -- the CLI
    must not resolve `+N` itself (only the engine knows the live budget)."""
    eng = engine()
    _seed(ctl, f"tst-budget-{spec.strip('+')}", eng)
    res = ctl.run("budget", f"tst-budget-{spec.strip('+')}", spec)
    assert res.returncode == 0, res.stderr
    assert eng.requests, "engine received no request"
    req = eng.requests[0]
    assert (req["method"], req["path"]) == ("PATCH", "/config/budget")
    assert req["body"] == {"iterations": spec}


def test_budget_prints_before_and_after(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-budget-human", eng)
    res = ctl.run("budget", "tst-budget-human", "+15")
    assert res.returncode == 0, res.stderr
    assert "25 -> 40" in res.stdout
    assert "17 used" in res.stdout


def test_budget_forwards_the_api_token(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-budget-token", eng, token="s3cret-token")
    res = ctl.run("budget", "tst-budget-token", "+1")
    assert res.returncode == 0, res.stderr
    assert eng.requests[0]["auth"] == "Bearer s3cret-token"


def test_budget_json_prints_the_engine_body(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-budget-json", eng)
    res = ctl.run("--json", "budget", "tst-budget-json", "+15")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == _OK_BODY


def test_budget_below_iterations_used_exits_5(ctl: Ctl, engine):
    """Same exit code pause/retry/abort use for the engine's 409 refusals."""
    eng = engine(409, _BELOW_USED)
    _seed(ctl, "tst-budget-409", eng)
    res = ctl.run("budget", "tst-budget-409", "5")
    assert res.returncode == 5, (res.returncode, res.stderr)
    assert "409" in res.stderr
    assert "below the 17 iteration(s) already used" in res.stderr
    assert res.stdout == ""


def test_budget_engine_rejection_exits_1(ctl: Ctl, engine):
    """A 422 from the engine (a value it cannot resolve) is a plain failure,
    not the 'refused, nothing to do' 409 path."""
    eng = engine(422, _INVALID)
    _seed(ctl, "tst-budget-422", eng)
    res = ctl.run("budget", "tst-budget-422", "0")
    assert res.returncode == 1, (res.returncode, res.stderr)
    assert "422" in res.stderr


def test_budget_malformed_spec_exits_2_without_calling_the_api(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-budget-bad", eng)
    res = ctl.run("budget", "tst-budget-bad", "ten")
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "invalid value" in res.stderr
    assert eng.requests == [], "malformed spec must not reach the engine"


def test_budget_unknown_run_exits_3(ctl: Ctl):
    res = ctl.run("budget", "no-such-run", "+10")
    assert res.returncode == 3, res.stderr
    assert "not found" in res.stderr


def test_budget_unreachable_api_exits_4(ctl: Ctl):
    _seed(ctl, "tst-budget-dead")  # apiUrl points at a closed port
    res = ctl.run("budget", "tst-budget-dead", "+10")
    assert res.returncode == 4, (res.returncode, res.stderr)
    assert "unreachable" in res.stderr


def test_budget_is_documented_in_cli_docs(ctl: Ctl):
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text()
    heading = "### `ralphctl budget <run-id> <+N|N>`"
    assert heading in doc
    section = doc.split(heading, 1)[1].split("\n### ", 1)[0]
    assert "PATCH /config/budget" in section
    # the live-engine-only caveat and the resume escape hatch must be stated
    assert "resume" in section and "--iterations" in section
    help_out = ctl.run("--help")
    assert "budget" in help_out.stdout
