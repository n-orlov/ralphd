"""Flow (task 018, requirement C.7): **the control plane's own auth
boundary** -- the API token, the control token, and a mutating-route
refusal -- over a real `ralphd-engine` subprocess with the stub `pi` binary
on PATH, `STUB_SLEEP=0`.

`engine/api.py`'s auth middleware runs two independent gates ahead of every
route handler (task 004, #62 candidate 1, requirement A; see
`tests/test_control_plane_auth.py` for the in-process/component coverage of
every mutating route):

  * `cfg.api_token` (`Authorization: Bearer <token>`) gates every route
    except `/healthz`, observation and mutating alike -- an operator opt-in.
  * `cfg.control_token` (`X-Ralphd-Control-Token`) additionally gates only
    the *mutating* half of the surface (`classify_method`) -- provisioned
    unconditionally by `ralphctl start`/`resume` on every real run, closing
    the mutating surface by default even when the operator never asked for
    an api token.

This module is the one flow that drives that boundary end to end through a
real subprocess and its own HTTP client, rather than `httpx.ASGITransport`
against an in-process app: the happy path runs to completion first (asserted
as the exact `events.jsonl` step sequence, same vocabulary and same shape as
`test_flow_happy_path.py`'s reference sequence -- nothing about the token
gates perturbs the path itself), then the finished, still-idling engine is
probed over its real HTTP port with `/status` (observation) and `/shutdown`
(mutating, and the one mutating route safe to call for real once the run has
already succeeded) under four presented-credential combinations:

  1. no token at all           -> refused (401) on both routes
  2. the api token only        -> `/status` accepted; `/shutdown` still
                                   refused (401) -- the control gate is a
                                   second, independent check
  3. api token + control token -> `/shutdown` accepted, and the engine
                                   takes the shutdown and exits 0 on its own

Steps (2)-(3) are exactly the task's own ask: "the unauthenticated mutating
call is refused while the token-bearing call is accepted".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from tests.flows.flowlib import assert_step_sequence, flow_steps, wait_engine_exit

API_TOKEN = "flow-control-plane-api-token"
CONTROL_TOKEN = "flow-control-plane-control-token"

# The reference happy-path sequence (two tasks, one worker iteration each,
# one verifying review -- `stub-pi`'s own `STUB_TASKS` default is 2, left
# untouched here since the token gates are the subject, not the plan shape).
# Identical in shape to `test_flow_happy_path.py`'s `HAPPY_PATH_STEPS`; kept
# as this module's own copy rather than a cross-module import, matching
# every other flow module's precedent of stating its own expected sequence.
CONTROL_PLANE_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",
    "iteration:worker",
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",
    "verdict:review:verified@1",
    "signal:VERIFIED",
    "state:succeeded",
]


def _request(run, method: str, path: str, *, api_token: str | None = None,
              control_token: str | None = None,
              body: dict | None = None) -> tuple[int, bytes]:
    """One raw HTTP call against the real engine's real port, with exactly
    the headers a caller presenting (or not presenting) each token would
    send -- deliberately not `LiveRun.api()`, which always sends no headers
    at all and cannot express "api token but no control token"."""
    data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{run.port}{path}", method=method, data=data)
    req.add_header("Content-Type", "application/json")
    if api_token is not None:
        req.add_header("Authorization", f"Bearer {api_token}")
    if control_token is not None:
        req.add_header("X-Ralphd-Control-Token", control_token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_control_plane_flow_walks_the_happy_path_then_gates_by_token(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-control-plane", "on_complete": "idle"},
        stub_env={"STUB_SLEEP": "0", "RALPHD_API_TOKEN": API_TOKEN,
                  "RALPHD_CONTROL_TOKEN": CONTROL_TOKEN})
    st = run.wait_terminal(timeout=60)
    assert st.get("state") == "succeeded", (
        f"flow control plane: the run must reach 'succeeded' before the "
        f"token gates are probed, got {st.get('state')!r} (status.json: {st})")

    assert_step_sequence(flow_steps(run), CONTROL_PLANE_STEPS,
                         flow="flow control plane")

    # `/healthz` stays open even with `api_token` configured -- the one
    # unauthenticated probe every caller (including a load balancer with no
    # token at all) must always be able to reach.
    code, _ = _request(run, "GET", "/healthz")
    assert code == 200, f"unauthenticated GET /healthz should stay open, got {code}"

    # -- gate 1: the api token, observation route -------------------------
    code, body = _request(run, "GET", "/status")
    assert code == 401, (
        f"GET /status with no api token should be refused with 401, got "
        f"{code}: {body!r}")

    code, body = _request(run, "GET", "/status", api_token=API_TOKEN)
    assert code == 200, (
        f"GET /status with the correct api token should be accepted, got "
        f"{code}: {body!r}")

    # -- gate 2: the control token, mutating route (`/shutdown`) ----------
    # The run has already succeeded, so `/shutdown` is the one mutating
    # route safe to exercise for real without disturbing the run itself.
    code, body = _request(run, "POST", "/shutdown")
    assert code == 401, (
        f"the unauthenticated mutating call (POST /shutdown, no api token, "
        f"no control token) should be refused, got {code}: {body!r}")

    code, body = _request(run, "POST", "/shutdown", api_token=API_TOKEN)
    assert code == 401, (
        f"POST /shutdown with the api token but no control token should "
        f"still be refused -- the control token gates the mutating half "
        f"independently of the api token -- got {code}: {body!r}")

    code, body = _request(run, "POST", "/shutdown", api_token=API_TOKEN,
                          control_token=CONTROL_TOKEN)
    assert code == 200, (
        f"the token-bearing mutating call (POST /shutdown with both the "
        f"api token and the control token) should be accepted, got "
        f"{code}: {body!r}")

    # The accepted shutdown call is real: the engine takes it and exits on
    # its own, no `LiveRun.stop()` teardown kill needed.
    rc = wait_engine_exit(run, timeout=10)
    assert rc == 0, (
        f"flow control plane: the engine should exit 0 after an authorized "
        f"/shutdown, got {rc}")
