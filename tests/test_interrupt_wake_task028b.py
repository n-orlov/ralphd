"""v0.25 task 028b (PRD requirement K.3/K.4, #267): `POST /interrupt` stops
lying about an idle loop, and an abort during a deferred wait lands in under
5s rather than at the wait's own bound.

The pre-028b handler (`api.py`) refused with a bare `409 "no iteration
running"` the instant `loop.interrupt()` found nothing to signal -- AFTER
already enqueuing an accompanying `{"message": ...}` body through `run.
add_steering` a few lines earlier. That is the exact `deck-phase4c` incident
K's PRD section measures: the operator's steer landed on disk, the caller
was told it failed, and the loop sat inside `_sleep_until_earliest_deferred`
(task 028a's own subject) unreachable for the whole bound.

Three groups here:

1. `POST /interrupt`'s two 200 shapes, and the 409-that-enqueues-nothing
   refusal, against a real `create_app()` ASGI app (no LiveRun/subprocess --
   this module stays in the smoke tier).
2. `ralphctl steer --now` / `ralphctl interrupt`'s CLI-side reporting,
   against a real (stub) HTTP server on loopback -- `_interrupt_report`
   keyed off the engine's own `target` field, asserted on exit code AND
   stdout wording, plus the 409 -> exit 5 case.
3. The K.4 guard: an abort delivered while the loop is inside a deferred
   wait winds the whole run down in well under 5s even though the wait's
   own bound is set far larger than that -- proving the wake (task 028a's
   `_deferred_wake`), not the timer, is what ends it.
4. The third idle state K.3 names by name: an INFRA-BACKOFF wait. The first
   pass of this task answered `{"target": "idle", "woken": true}` for a
   loop parked there while `_wait_out_backoff` waited only on
   `self._retry_now` -- so the loop demonstrably slept out its whole bound
   and the 200 body was a lie about that state exactly as the pre-028b
   `409` was a lie about the deferred one. These nodes drive the REAL
   `_wait_out_backoff` coroutine (rebound from the import-time original,
   the `tests/test_v05_definition_of_done.py` idiom -- this module is not
   one of the three PRD-named `real_backoff` modules and must not carry
   that marker) and assert the wait actually ends early, which bell ended
   it, and that the timer bound still holds when no bell rings at all.

Mutation cases (recorded in the commit message): reverting `interrupt_or_
wake` to the old `if not loop.interrupt(): raise problem(409, ...)` shape
would fail every "idle wakes with 200" assertion below with a 409 instead;
moving the `finished()` check after `run.add_steering` (the exact pre-028b
bug) would fail `test_a_refused_interrupt_leaves_no_steering_message_
behind`, whose whole point is a message-bearing 409 against an already-
finished run; and reverting the K.4 guard's own site (`_sleep_until_
earliest_deferred`'s `asyncio.wait(..., return_when=FIRST_COMPLETED)`) back
to a bare `asyncio.sleep(wait_s)` would make
`test_an_abort_during_a_deferred_wait_winds_down_in_under_5s` time out
against its own bound instead of returning promptly. Group 4's own mutation
is the first pass of this very task: `_wait_out_backoff` waiting only on
`self._retry_now` (`asyncio.wait_for(self._retry_now.wait(), ...)`) leaves
`test_interrupt_wakes_a_loop_parked_in_an_infra_backoff_wait` and
`test_an_abort_wakes_a_loop_parked_in_an_infra_backoff_wait` timing out at
`asyncio.wait_for(task, timeout=5.0)` against a 30s bound, while collapsing
the two bells into one flag fails
`test_retry_now_is_still_the_bell_that_reports_a_manual_retry` or the
`retry_woke is False` assertion beside it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from test_cli_docker import Ctl, ctl
from test_cli_resume import _seed_run

from ralphd.engine.api import create_app
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, utc_from_epoch
from tests.conftest import ScriptedRunner, make_supervisor

# Captured at IMPORT time, before tests/conftest.py's autouse class-level
# `_wait_out_backoff` stub can touch the class (the
# `tests/test_v05_definition_of_done.py` idiom): group 4 below needs the REAL
# coroutine on its own instance, and this module must NOT carry the
# `real_backoff` opt-out marker -- `tests/test_stub_pi_harness.py` pins that
# marker to exactly the PRD's three named modules.
_REAL_WAIT_OUT_BACKOFF = LoopSupervisor._wait_out_backoff
# Likewise captured before `make_supervisor()` patches `asyncio.sleep` to an
# instant yield: group 4 polls for a real state change on a real timer.
_real_sleep = asyncio.sleep

__all__ = ["ctl"]

TASK_1 = "001"


# ---------------------------------------------------------------------------
# Group 1: POST /interrupt's two 200 shapes + the 409-enqueues-nothing rule.
# ---------------------------------------------------------------------------


def _client(sup) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


def _supervisor(tmp_path, **status_over):
    status = {"state": "running", "iterationsBudget": 5, "iterationsUsed": 0}
    status.update(status_over)
    return make_supervisor(tmp_path, run_id="k34", cfg_defaults=False,
                           status=status, iterations=5,
                           runner=ScriptedRunner())


@pytest.mark.asyncio
async def test_interrupt_signals_a_running_iteration(tmp_path):
    sup = _supervisor(tmp_path)
    sup.runner.running = True   # an iteration is actually in flight
    async with _client(sup) as client:
        resp = await client.post("/interrupt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"interrupted": True, "target": "iteration",
                    "signalled": True, "woken": False}, body
    # the shielding rule `interrupt()` already had stays intact through
    # the wrapper: a delivered signal marks it operator-caused.
    assert sup.operator_abort_requested


@pytest.mark.asyncio
async def test_interrupt_wakes_an_idle_loop_between_iterations(tmp_path):
    sup = _supervisor(tmp_path)
    sup.runner.running = False   # nothing running -- the idle case
    async with _client(sup) as client:
        resp = await client.post("/interrupt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"interrupted": True, "target": "idle",
                    "signalled": False, "woken": True}, body
    # the bell this rings is the SAME one task 028a's deferred wait and
    # `_wait_out_backoff` (group 4 below) race their timer against.
    assert sup._deferred_wake.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["succeeded", "failed", "aborted",
                                    "timed_out"])
async def test_409_is_returned_only_for_a_finished_run(tmp_path, state):
    sup = _supervisor(tmp_path, state=state)
    async with _client(sup) as client:
        resp = await client.post("/interrupt")
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_a_refused_interrupt_leaves_no_steering_message_behind(tmp_path):
    """The exact `deck-phase4c` shape: a `{"message": ...}` body accompanies
    a request against an already-finished run. The pre-028b handler
    enqueued the message via `run.add_steering` and only THEN refused with
    409 -- the caller was told the request failed about a message that had,
    in fact, landed. `finished()` now gates first, so nothing is enqueued at
    all: the steering ledger before and after the refusal is identical."""
    sup = _supervisor(tmp_path, state="succeeded")
    before = sup.run.steering_entries()
    async with _client(sup) as client:
        resp = await client.post(
            "/interrupt", json={"message": "do not defer -- proceed"})
    assert resp.status_code == 409, resp.text
    after = sup.run.steering_entries()
    assert after == before, (
        "a refused /interrupt enqueued a steering message: "
        f"before={before} after={after}")
    assert after == [], after


@pytest.mark.asyncio
async def test_a_message_that_lands_is_never_reported_as_a_failure(tmp_path):
    """The other half: while the run is NOT finished (idle or running),
    an accompanying message really is enqueued, and the handler's own
    answer is a plain 200 -- never something a caller could mistake for a
    failure about the message itself."""
    sup = _supervisor(tmp_path)
    sup.runner.running = False
    async with _client(sup) as client:
        resp = await client.post(
            "/interrupt", json={"message": "do not defer -- proceed"})
    assert resp.status_code == 200, resp.text
    entries = sup.run.steering_entries()
    assert len(entries) == 1, entries
    assert entries[0]["body"].strip() == "do not defer -- proceed"
    assert resp.json()["target"] == "idle"


# ---------------------------------------------------------------------------
# Group 2: the CLI side -- `ralphctl steer --now` / `ralphctl interrupt`
# exit 0 in both 200 shapes with matching wording, and exit 5 on 409.
# ---------------------------------------------------------------------------


class _StubInterruptEngine:
    """Minimal HTTP stand-in for the engine's own `/interrupt`: records
    the request, replies with a canned status + JSON body -- the CLI's own
    reporting is what is under test here, not the engine's real dispatch
    (that is Group 1, against a real `create_app()`)."""

    def __init__(self, status: int, body: dict):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # BaseHTTPRequestHandler API name
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.requests.append({
                    "method": self.command, "path": self.path,
                    "body": json.loads(raw) if raw else None})
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
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def engine():
    made: list[_StubInterruptEngine] = []

    def make(status: int = 200, body: dict | None = None):
        e = _StubInterruptEngine(status, body if body is not None else {})
        made.append(e)
        return e

    yield make
    for e in made:
        e.close()


def _seed(c: Ctl, run_id: str, eng: _StubInterruptEngine) -> None:
    rdir, _cdir = _seed_run(c, run_id)
    meta = json.loads((rdir / "host.json").read_text())
    meta["port"] = eng.port
    meta["apiUrl"] = f"http://127.0.0.1:{eng.port}"
    (rdir / "host.json").write_text(json.dumps(meta))
    (rdir / "status.json").write_text(json.dumps({"state": "running"}))


_ITERATION_BODY = {"interrupted": True, "target": "iteration",
                   "signalled": True, "woken": False}
_IDLE_BODY = {"interrupted": True, "target": "idle",
             "signalled": False, "woken": True}
_FINISHED_BODY = {"title": "job finished", "status": 409,
                  "detail": "no iteration to interrupt"}


def test_steer_now_reports_a_signalled_running_iteration(ctl: Ctl, engine):
    eng = engine(200, _ITERATION_BODY)
    _seed(ctl, "tst-steer-now-iter", eng)
    res = ctl.run("steer", "tst-steer-now-iter", "proceed", "--now")
    assert res.returncode == 0, res.stderr
    assert "a running iteration was signalled" in res.stdout, res.stdout
    assert "an idle loop was woken" not in res.stdout


def test_steer_now_reports_a_woken_idle_loop(ctl: Ctl, engine):
    eng = engine(200, _IDLE_BODY)
    _seed(ctl, "tst-steer-now-idle", eng)
    res = ctl.run("steer", "tst-steer-now-idle", "proceed", "--now")
    assert res.returncode == 0, res.stderr
    assert "an idle loop was woken" in res.stdout, res.stdout
    assert "a running iteration was signalled" not in res.stdout


def test_interrupt_cmd_reports_a_signalled_running_iteration(ctl: Ctl, engine):
    eng = engine(200, _ITERATION_BODY)
    _seed(ctl, "tst-interrupt-iter", eng)
    res = ctl.run("interrupt", "tst-interrupt-iter")
    assert res.returncode == 0, res.stderr
    assert "a running iteration was signalled" in res.stdout, res.stdout


def test_interrupt_cmd_reports_a_woken_idle_loop(ctl: Ctl, engine):
    eng = engine(200, _IDLE_BODY)
    _seed(ctl, "tst-interrupt-idle", eng)
    res = ctl.run("interrupt", "tst-interrupt-idle")
    assert res.returncode == 0, res.stderr
    assert "an idle loop was woken" in res.stdout, res.stdout


def test_interrupt_cmd_409_exits_5_and_never_claims_success(ctl: Ctl, engine):
    eng = engine(409, _FINISHED_BODY)
    _seed(ctl, "tst-interrupt-409", eng)
    res = ctl.run("interrupt", "tst-interrupt-409")
    assert res.returncode == 5, (res.returncode, res.stdout, res.stderr)
    assert "signalled" not in res.stdout
    assert "woken" not in res.stdout


def test_steer_now_409_exits_5_not_0(ctl: Ctl, engine):
    eng = engine(409, _FINISHED_BODY)
    _seed(ctl, "tst-steer-now-409", eng)
    res = ctl.run("steer", "tst-steer-now-409", "proceed", "--now")
    assert res.returncode == 5, (res.returncode, res.stdout, res.stderr)


# ---------------------------------------------------------------------------
# Group 3: K.4's own guard -- an abort delivered while the loop is inside a
# deferred wait winds the run down in well under 5s, even though the wait's
# own bound is far larger than 5s.
# ---------------------------------------------------------------------------


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "wait on a gated window", "status": "pending",
            "successCriteria": "the window opened"}
    task.update(over)
    return task


_ABORT_GUARD_BOUND_S = 30.0   # far larger than the 5s this guard proves


async def _wake_after(sleep_fn, delay: float, action) -> None:
    await sleep_fn(delay)
    action()


async def _drive_with_waker(sleep_fn, sup, delay: float, action):
    waker = asyncio.ensure_future(_wake_after(sleep_fn, delay, action))
    result = await sup._run_job_core()
    await waker
    return result


def test_an_abort_during_a_deferred_wait_winds_down_in_under_5s(tmp_path):
    root = tmp_path / "run"
    until = utc_from_epoch(time.time() + _ABORT_GUARD_BOUND_S)
    tasks = [_task(deferredUntil=until,
                   deferredReason="waiting on a gated window to open")]

    # Built the same way `test_deferred_wait_wake_task028a.py` does, through
    # `scripted_supervisor`, so an abort mid-wait is driven through the REAL
    # dispatch pass (`_run_job_core`), not just `_sleep_until_earliest_
    # deferred` in isolation.
    from tests.conftest import scripted_supervisor
    from ralphd.engine.state import atomic_write_json

    build = scripted_supervisor(run_id="v25-abort-wake", iterations=15,
                                max_approaches=1, vigilant=False,
                                on_complete="exit",
                                infra_outage_budget_s=1000.0)
    RunDir(root=root).update_status(state="starting")
    sup = build(root, {"worker": [], "review": []}, job_timeout_s=3600,
               real_sleep=True)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1

    real_sleep_fn = asyncio.sleep
    wake_delay = 0.2
    started = time.time()
    final_state = asyncio.run(_drive_with_waker(
        real_sleep_fn, sup, wake_delay,
        lambda: sup.abort("operator asked to stop deferring")))
    elapsed = time.time() - started

    assert final_state == "aborted", sup.run.read_status()
    assert sup.run.read_status().get("state") == "aborted", sup.run.read_status()
    assert elapsed < 5.0, (
        f"abort during a {_ABORT_GUARD_BOUND_S}s deferred wait took "
        f"{elapsed:.3f}s wall clock -- the wake, not the bound, must have "
        "ended it")


# ---------------------------------------------------------------------------
# Group 4: the infra-backoff wait -- the third idle state K.3 names. The
# 200 body must not claim `woken` for a loop that then sleeps out its bound.
# ---------------------------------------------------------------------------


_BACKOFF_BOUND_S = 30.0    # far larger than the wake this group measures


async def _parked_in_a_backoff_wait(sup, bound: float):
    """Start the REAL `_wait_out_backoff` on `sup` and hand back the task
    once the loop is genuinely parked in it (`_infra_waiting` is what
    `retry_now()`'s own 409 gate reads, and it is set inside the wait)."""
    sup._wait_out_backoff = _REAL_WAIT_OUT_BACKOFF.__get__(  # type: ignore[method-assign]
        sup, LoopSupervisor)
    task = asyncio.ensure_future(sup._wait_out_backoff(bound))
    for _ in range(200):
        if sup._infra_waiting:
            break
        await _real_sleep(0.005)
    assert sup._infra_waiting, "the supervisor never entered the backoff wait"
    return task


@pytest.mark.asyncio
async def test_interrupt_wakes_a_loop_parked_in_an_infra_backoff_wait(tmp_path):
    """K.3's third idle state, end to end through the real handler: while the
    loop sits in a 30s infra backoff, `POST /interrupt` answers the idle 200
    shape AND the wait really ends -- in well under the bound, with only the
    seconds actually spent booked."""
    sup = _supervisor(tmp_path)
    sup.runner.running = False        # nothing running: the idle case
    task = await _parked_in_a_backoff_wait(sup, _BACKOFF_BOUND_S)

    loop = asyncio.get_running_loop()
    started = loop.time()
    async with _client(sup) as client:
        resp = await client.post("/interrupt",
                                 json={"message": "stop waiting, proceed"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"interrupted": True, "target": "idle",
                          "signalled": False, "woken": True}, resp.text

    waited_s, retry_woke = await asyncio.wait_for(task, timeout=5.0)
    elapsed = loop.time() - started
    assert elapsed < 5.0, (
        f"a {_BACKOFF_BOUND_S}s backoff wait took {elapsed:.3f}s to end "
        "after the 200 said the idle loop was woken -- the body lied")
    assert waited_s < 5.0, "only the seconds actually spent are booked"
    # The retry-now flag is the caller's "restart the outage-budget episode
    # clock" signal (`if woken:` in `run_iteration`'s ladder). A steer is not
    # the operator asserting the endpoint is back, so it must stay False.
    assert retry_woke is False, (
        "a control-plane wake must not be reported as a manual retry -- "
        "that would reset the outage-budget episode clock")
    assert not sup._infra_waiting, "the wait cleared its own flag on the way out"


@pytest.mark.asyncio
async def test_an_abort_wakes_a_loop_parked_in_an_infra_backoff_wait(tmp_path):
    """The same bell, rung by `_record_abort` instead of `add_steering`: a run
    ending must not sit out a 30s backoff first."""
    sup = _supervisor(tmp_path)
    task = await _parked_in_a_backoff_wait(sup, _BACKOFF_BOUND_S)

    loop = asyncio.get_running_loop()
    started = loop.time()
    sup.abort("operator asked to stop")
    waited_s, retry_woke = await asyncio.wait_for(task, timeout=5.0)

    assert loop.time() - started < 5.0, (
        f"an abort during a {_BACKOFF_BOUND_S}s backoff wait was not woken")
    assert waited_s < 5.0 and retry_woke is False


@pytest.mark.asyncio
async def test_retry_now_is_still_the_bell_that_reports_a_manual_retry(tmp_path):
    """Task 015's own bell is untouched by K: `POST /retry` -> `retry_now()`
    still ends the wait AND still reports `woken=True`, the flag that
    restarts the outage-budget episode clock. Racing a second event must not
    have merged the two senses into one."""
    sup = _supervisor(tmp_path)
    task = await _parked_in_a_backoff_wait(sup, _BACKOFF_BOUND_S)

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert sup.retry_now() is True, "parked in a wait -- there IS something to wake"
    waited_s, retry_woke = await asyncio.wait_for(task, timeout=5.0)

    assert retry_woke is True, "the manual-retry sense of `woken` must survive"
    assert loop.time() - started < 5.0 and waited_s < 5.0
    # Narrow by construction, unchanged: nothing waiting -> False (the 409).
    assert sup.retry_now() is False


@pytest.mark.asyncio
async def test_an_unrung_backoff_wait_still_waits_out_its_own_bound(tmp_path):
    """The other half of the race, so "it ends early" can never be reached by
    ending early always: with no bell rung at all the wait spends its bound
    and reports the timer outcome."""
    sup = _supervisor(tmp_path)
    sup._wait_out_backoff = _REAL_WAIT_OUT_BACKOFF.__get__(  # type: ignore[method-assign]
        sup, LoopSupervisor)
    bound = 0.25
    loop = asyncio.get_running_loop()
    started = loop.time()
    waited_s, retry_woke = await sup._wait_out_backoff(bound)
    elapsed = loop.time() - started

    assert retry_woke is False
    assert elapsed >= bound, f"the bound was not honoured ({elapsed:.3f}s)"
    assert waited_s <= bound, "the booked wait never exceeds the planned backoff"
    assert waited_s == pytest.approx(bound, abs=0.05)
