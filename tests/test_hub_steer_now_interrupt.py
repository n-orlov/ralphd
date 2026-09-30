"""Task 001 (#277): the hub's `POST /api/runs/<id>/steer` splits `now: true`
off to the run's `POST /interrupt` -- the SAME mechanism `ralphctl steer
--now`/`ralphctl interrupt` already drive (`cli/main.py`'s `cmd_steer`) --
rather than always proxying to `POST /steering` regardless of the flag.

Two groups:

1. The negative case, against a stub engine: `now` absent and `now: false`
   both still reach `/steering` and never touch `/interrupt` at all --
   proven by inspecting the stub's own recorded request paths, not just by
   the response shape (which could coincidentally match even if the wrong
   route had been hit).
   Alongside it, `now: true` against a stub engine that REJECTS `POST
   /interrupt` (409 job finished, 400): the hub answers with the engine's
   own status code and the engine's own body, not a hub wrapper.
2. The positive case, against a REAL running engine (`live`, no docker): a
   held worker iteration is interrupted through the hub route, the hub's
   response is the engine's `POST /interrupt` body passed through
   unchanged (status code and `target` included), the held iteration's own
   `iteration.end` event actually records `interrupted: true`, and the
   steering message the hub enqueued is consumed by a later iteration
   (`STEERING_APPLIED`) -- not merely accepted and forgotten.

Mutation cases (recorded in the commit message): reverting the hub route to
always proxy to `/steering` regardless of `now` would still pass group 1
(that is the behaviour it pins) but fail group 2's response-shape assertion
(`/steering`'s 202 `{"file": ...}` body, never `target`) and its
`iteration.end`/`STEERING_APPLIED` polls would time out (a queued-but-never-
interrupted message is never forced to settle by the held worker pass
ending). Making the hub branch call `/interrupt` unconditionally would fail
group 1's `never touches /interrupt` assertion. Re-wrapping an engine
rejection in the hub's `{"error", "detail"}` shape fails the rejection
passthrough node's body-equality assertion.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from ralphd.engine.state import STEERING_APPLIED, read_events, steering_entries

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import StubEngineApi, UiServer, _write_run_with_api

from tests.conftest import wait_hold_entered


@pytest.fixture
def ui():
    servers = []

    def make(registry: Path) -> UiServer:
        s = UiServer(registry)
        s.wait_ready()
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()


def _wait_for(fn, timeout=30, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.2)
    raise TimeoutError(f"{what} never happened; last: {last!r}")


# ---------------------------------------------------------------------------
# Group 1: now absent / now: false -- /steering only, /interrupt untouched.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"message": "queued, no now key at all"},
    {"message": "queued, now explicitly false", "now": False},
])
def test_now_absent_or_false_reaches_steering_never_interrupt(tmp_path, ui, body):
    engine = StubEngineApi(
        steer_status=202, steer_body={"file": "001-queued.md"},
        interrupt_status=200,
        interrupt_body={"interrupted": True, "target": "iteration",
                        "signalled": True, "woken": False})
    registry = tmp_path / "registry"
    try:
        _write_run_with_api(registry, "hubnow-neg", engine, state="running")
        server = ui(registry)
        code, resp = server.post("/api/runs/hubnow-neg/steer", body)
        assert code == 202, resp
        assert resp == {"file": "001-queued.md"}, resp
        hit_paths = [path.split("?", 1)[0] for _method, path, _auth in engine.requests]
        assert "/steering" in hit_paths, hit_paths
        assert "/interrupt" not in hit_paths, hit_paths
    finally:
        engine.close()


@pytest.mark.parametrize("status,engine_body", [
    (409, {"detail": "job finished", "example": "preserved"}),
    (400, {"error": "message must be a string", "field": "message"}),
])
def test_now_true_passes_an_engine_rejection_through_unchanged(
        tmp_path, ui, status, engine_body):
    """`now: true` + an engine that REJECTS `POST /interrupt`: the hub's
    answer is the engine's own status code and its own body, byte-for-byte
    the same JSON object -- never re-wrapped in the hub's
    `{"error": ..., "detail": ...}` shape."""
    engine = StubEngineApi(interrupt_status=status, interrupt_body=engine_body)
    registry = tmp_path / "registry"
    try:
        _write_run_with_api(registry, "hubnow-rej", engine, state="running")
        server = ui(registry)
        code, resp = server.post("/api/runs/hubnow-rej/steer",
                                 {"message": "too late", "now": True})
        assert code == status, resp
        assert resp == engine_body, resp
        hit_paths = [path.split("?", 1)[0] for _method, path, _auth in engine.requests]
        assert "/interrupt" in hit_paths and "/steering" not in hit_paths, hit_paths
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Group 2: now: true against a real, running engine.
# ---------------------------------------------------------------------------

def test_now_true_interrupts_a_real_running_iteration_and_is_consumed(
        tmp_path, live, ui):
    hold_file = tmp_path / "hold-hubnow"
    run = live(run_id="hubnow-pos",
              job={"iterations": 8, "max_approaches": 3, "on_complete": "idle"},
              stub_env={"STUB_TASKS": "4", "STUB_HOLD_FILE": str(hold_file),
                       "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()
    wait_hold_entered(hold_file)

    server = ui(run.registry)
    code, resp = server.post(f"/api/runs/{run.run_id}/steer",
                             {"message": "stop and steer now", "name": "via-hub-now",
                              "now": True})
    # Task 001 (#277): the hub's response is the ENGINE's own `POST
    # /interrupt` body, passed through with no reshaping -- `target`
    # included, and the same 200 the engine itself would have answered.
    assert code == 200, resp
    assert resp.get("target") == "iteration", resp
    assert resp.get("interrupted") is True, resp

    def interrupted_iteration_end():
        events = read_events(run.run_dir, types=["iteration.end"])
        hits = [e for e in events if e.get("phase") == "worker" and e.get("interrupted")]
        return hits or None

    _wait_for(interrupted_iteration_end,
             what="the held worker iteration ending with interrupted: true")

    def applied():
        entries = steering_entries(run.run_dir)
        return entries if entries and entries[0]["state"] == STEERING_APPLIED else None

    settled = _wait_for(applied, what="the loop consuming the hub-enqueued steer")
    assert settled[0]["name"] == "via-hub-now", settled
    run.stop()
