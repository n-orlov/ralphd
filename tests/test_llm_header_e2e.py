"""Task 048 (#177, requirement J.5's last clause): "at the e2e tier the
stub-`pi` (or a capture endpoint) observes the header on a request."

Every earlier task in this PRD requirement proved the header lands in a
FILE the config dir carries (`<cdir>/pi/models.json`,
`tests/test_cli_llm_profiles.py`) or is displayed, redacted, on four
surfaces (`tests/test_llm_header_visibility.py`) -- none of them prove the
header ever reaches an outgoing REQUEST. This module closes that gap end to
end, with three real processes and a real request:

* a real `ralphd-engine` (the STUB_DOCKER_LIVE_ENGINE knob,
  tests/stub-docker/docker + live_engine_supervisor.py, the same mechanism
  tests/test_cli_start_nodetach.py and tests/test_cli_resume_llm_wiring.py
  already use), spawning
* the real `tests/stub-pi/pi` standing in for `pi` itself, which under
  STUB_LLM_REQUEST does what a real LLM client does with the config it was
  handed -- POSTs to the configured provider `baseUrl` with the provider's
  configured `headers` applied to the request -- and
* a CAPTURE ENDPOINT: a loopback HTTP server started by this test, named as
  the profile's own `baseUrl`, which records every request it receives.

The assertion is on what the capture endpoint received, so the evidence is
an observed request header on the wire, not a config file echoed back. The
earlier `.stub-llm-headers.json` marker (the provider `headers` maps the
`pi` process found in its ambient `~/.pi/agent/models.json`) is still
asserted alongside it: it localises a failure to "config never reached the
process" vs "the process never put it on the request".

`live_engine_supervisor.py` mirrors `container/entrypoint.sh`'s own "pi
provider/model config injected by the CLI" step (`<config-dir>/pi/*` copied
into `$HOME/.pi/agent/`) before the engine ever runs, so the config dir
`ralphctl start --llm-header` wrote reaches the ambient home exactly the way
a real container would place it.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_cli_llm_profiles import Ctl, ctl  # noqa: F401 (fixture factory reused)
from test_cli_start_nodetach import _live_env, _wait_for_supervisor
from test_llm_api import wait_for_env_marker

__all__ = ["ctl"]


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records every received request and answers a minimal completion."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler's own spelling)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.captured.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        })
        payload = json.dumps({
            "id": "capture-1",
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep pytest output clean
        return


class Capture:
    """A loopback capture endpoint: `url` to configure, `captured` to read."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self.server.captured = []
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def captured(self) -> list[dict]:
        return list(self.server.captured)

    def wait_for_request(self, header_name: str, timeout: float = 30) -> dict:
        """The first captured request carrying `header_name`, or {} on the
        deadline -- the caller's own assertion reports the failure."""
        key = header_name.lower()
        deadline = time.time() + timeout
        while True:
            for req in self.captured:
                if key in req["headers"]:
                    return req
            if time.time() >= deadline:
                return {}
            time.sleep(0.05)

    def close(self) -> None:
        # Only ever the server THIS object created (never a process or
        # socket selected by pattern): shut its own loop down and join.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


@pytest.fixture
def capture():
    cap = Capture()
    try:
        yield cap
    finally:
        cap.close()


def test_a_run_started_with_llm_header_shows_the_capture_endpoint_observing_it(
        ctl, capture):
    ctl.write_profile("acme-e2e", {
        "model": "acme/big", "env": {},
        "pi": {"providers": {"acme": {"baseUrl": capture.url}}},
    })
    run_id = "tst-header-e2e"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-e2e",
                  "--llm-header", "X-Jira-ID=EST-6588",
                  "--run-id", run_id, "--on-complete", "idle",
                  "--iterations", "10", "--max-approaches", "1",
                  env={**_live_env(), "STUB_TASKS": "1", "STUB_SLEEP": "0",
                       "STUB_LLM_REQUEST": "1"})
    assert res.returncode == 0, res.stderr
    run_dir = ctl.registry / "runs" / run_id
    sup = _wait_for_supervisor(run_dir)
    assert sup["killed"] is True  # the run really did reach a terminal state

    # 1. the config reached the process that builds the request ...
    observed = wait_for_env_marker(run_dir / ".stub-llm-headers.json")
    assert observed == {"acme": {"X-Jira-ID": "EST-6588"}}, observed
    # 2. ... and that process really did issue a request to the configured
    #    baseUrl (its own record of the attempt: names only, never values) ...
    attempts = wait_for_env_marker(run_dir / ".stub-llm-request.json",
                                   predicate=bool)
    assert [a["provider"] for a in attempts] == ["acme"], attempts
    assert attempts[0]["url"] == f"{capture.url}/chat/completions", attempts
    assert attempts[0]["headerNamesSent"] == ["X-Jira-ID"], attempts
    assert attempts[0]["status"] == 200, attempts
    # 3. ... and the CAPTURE ENDPOINT observed the header on that request.
    req = capture.wait_for_request("X-Jira-ID")
    assert req, f"capture endpoint saw no request carrying the header: {capture.captured}"
    assert req["headers"]["x-jira-id"] == "EST-6588", req
    assert req["path"] == "/v1/chat/completions", req


def test_a_header_ref_derived_value_is_resolved_before_it_reaches_the_request(
        ctl, capture):
    """A `${env:}` header value resolves ON THE HOST, at `start` time (PRD
    requirement J.1) -- the request carries the resolved literal, never the
    `${env:}` spec itself."""
    ctl.write_profile("acme-e2e-ref", {
        "model": "acme/big", "env": {},
        "pi": {"providers": {"acme": {"baseUrl": capture.url}}},
    })
    run_id = "tst-header-e2e-ref"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-e2e-ref",
                  "--llm-header", "X-Jira-ID=${env:E2E_HEADER_VAR}",
                  "--run-id", run_id, "--on-complete", "idle",
                  "--iterations", "10", "--max-approaches", "1",
                  env={**_live_env(), "STUB_TASKS": "1", "STUB_SLEEP": "0",
                       "STUB_LLM_REQUEST": "1",
                       "E2E_HEADER_VAR": "EST-9001"})
    assert res.returncode == 0, res.stderr
    run_dir = ctl.registry / "runs" / run_id
    _wait_for_supervisor(run_dir)

    observed = wait_for_env_marker(run_dir / ".stub-llm-headers.json")
    assert observed == {"acme": {"X-Jira-ID": "EST-9001"}}, observed
    req = capture.wait_for_request("X-Jira-ID")
    assert req, f"capture endpoint saw no request carrying the header: {capture.captured}"
    assert req["headers"]["x-jira-id"] == "EST-9001", req
    assert "${env:" not in req["headers"]["x-jira-id"], req
