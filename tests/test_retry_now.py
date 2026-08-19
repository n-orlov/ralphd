"""Task 015 (#5): `POST /retry` wakes an infra backoff wait immediately.

The wrapper's backoff is an interruptible `asyncio.Event` race
(`LoopSupervisor._wait_out_backoff` / `self._retry_now`), shaped like
`_pause`, so an operator who can see the endpoint is healthy again does not
have to sit out a 5-minute countdown. These tests drive the *real* wait (no
stubbed sleep) through the *real* HTTP route (ASGI, same event loop as the
loop task) and assert wall clock, the outage-budget episode-clock reset, the
409 paths, and the deliberate non-overlap with `/resume` and steering.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from test_infra_outage_budget import INFRA_ERROR, _infra_result, _ok_result

from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

# Long enough that a test finishing quickly can only mean the wait was woken.
LONG_BACKOFF = 30.0


def _supervisor(tmp_path, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    kw = {"infra_retry_backoff_s": [LONG_BACKOFF, LONG_BACKOFF + 10.0],
          "infra_retry_backoff_max_s": 300.0,
          "infra_outage_budget_s": 1000.0, **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="unit", **kw), run, tmp_path)
    run.update_status(state="running", health="ok", infraWait=None)
    return sup


def _feed(sup: LoopSupervisor, results: list[IterationResult]) -> list[str]:
    """Feeds `results` to the wrapper one attempt at a time (last repeats)."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        return results[min(len(calls) - 1, len(results) - 1)]

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    return calls


def _client(sup: LoopSupervisor) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


def _events(run: RunDir, type_: str) -> list[dict]:
    log = run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _instrument_waits(sup: LoopSupervisor) -> list[float]:
    """Records every real backoff wait as it *starts* (the wait itself is the
    production one -- nothing is stubbed), so a test can tell wait #2 from
    wait #1 without racing the `_infra_waiting` flag."""
    real = sup._wait_out_backoff
    started: list[float] = []

    async def wrapped(backoff):
        started.append(backoff)
        return await real(backoff)

    sup._wait_out_backoff = wrapped  # type: ignore[method-assign]
    return started


async def _retry_when_waiting(client, sup: LoopSupervisor,
                             started: list[float], times: int) -> list[int]:
    """For each of the first `times` backoff waits: waits until the loop is
    parked in it, then POSTs /retry. Returns the response status codes."""
    codes: list[int] = []
    for n in range(1, times + 1):
        deadline = time.monotonic() + 15.0
        while not (len(started) >= n and sup._infra_waiting):
            assert time.monotonic() < deadline, f"backoff wait #{n} never started"
            await asyncio.sleep(0.01)
        codes.append((await client.post("/retry")).status_code)
    return codes


@pytest.mark.asyncio
async def test_post_retry_cuts_a_long_backoff_short(tmp_path):
    sup = _supervisor(tmp_path)
    _feed(sup, [_infra_result(), _infra_result(), _ok_result()])
    started = _instrument_waits(sup)

    async with _client(sup) as client:
        began = time.monotonic()
        task = asyncio.ensure_future(sup.run_iteration("worker"))
        codes = await _retry_when_waiting(client, sup, started, times=2)
        result = await asyncio.wait_for(task, timeout=10)
        elapsed = time.monotonic() - began

    assert codes == [200, 200]
    assert not result.error_message, "the third attempt succeeded"
    # Two 30s+ backoffs were scheduled; both were woken by the operator.
    assert elapsed < 5.0, f"backoff was not cut short (took {elapsed:.1f}s)"
    assert [ev["backoffS"] for ev in _events(sup.run, "infra_retry")] == [
        LONG_BACKOFF, LONG_BACKOFF + 10.0], \
        "the attempt counter (and so the escalating schedule) survives a manual retry"
    # The manual retries are on the record, not just in the response.
    manual = _events(sup.run, "infra_retry_now")
    assert len(manual) == 2
    assert manual[0]["source"] == "operator"
    assert manual[0]["phase"] == "worker" and manual[0]["error"] == INFRA_ERROR


@pytest.mark.asyncio
async def test_post_retry_resets_the_outage_budget_episode_clock(tmp_path):
    sup = _supervisor(tmp_path)
    _feed(sup, [_infra_result(), _infra_result(), _ok_result()])
    started = _instrument_waits(sup)

    async with _client(sup) as client:
        task = asyncio.ensure_future(sup.run_iteration("worker"))
        await _retry_when_waiting(client, sup, started, times=2)
        await asyncio.wait_for(task, timeout=10)

    # Attempt 2 opened with a *zeroed* cumulative wait even though attempt 1
    # really did wait: the manual retry restarted the outage budget.
    assert [ev["waitedS"] for ev in _events(sup.run, "infra_retry")] == [0.0, 0.0]
    # ... while the seconds actually waited are still booked honestly, and
    # only those seconds (not the 30s backoff that was cut short).
    total = json.loads((sup.run.root / "status.json").read_text())["infraWaitTotalS"]
    assert 0.0 < total < 5.0, total
    waits = [ev["waitedS"] for ev in _events(sup.run, "deadline_extended")]
    assert len(waits) == 2 and all(0.0 < w < 5.0 for w in waits), waits
    assert abs(sum(waits) - total) < 0.01


@pytest.mark.asyncio
async def test_post_retry_409s_when_the_run_is_not_waiting(tmp_path):
    sup = _supervisor(tmp_path)
    async with _client(sup) as client:
        resp = await client.post("/retry")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["title"] == "not waiting on an infra fault"
    assert "infraWait" in detail["detail"]
    # The message points at the right verb for the *other* held state.
    assert "/resume" in detail["detail"]
    assert not _events(sup.run, "infra_retry_now")


@pytest.mark.asyncio
async def test_post_retry_409s_on_a_finished_job(tmp_path):
    sup = _supervisor(tmp_path)
    sup.run.update_status(state="succeeded")
    async with _client(sup) as client:
        resp = await client.post("/retry")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "job finished"


@pytest.mark.asyncio
async def test_post_retry_does_not_unpause_or_touch_steering(tmp_path):
    sup = _supervisor(tmp_path)
    _feed(sup, [_infra_result(), _ok_result()])
    started = _instrument_waits(sup)
    sup.pause()
    assert not sup._pause.is_set()

    async with _client(sup) as client:
        task = asyncio.ensure_future(sup.run_iteration("worker"))
        assert await _retry_when_waiting(client, sup, started, times=1) == [200]
        await asyncio.wait_for(task, timeout=10)

    # /retry is only about the backoff wait: the run is still paused (that is
    # /resume's job) and no steering was consumed or created.
    assert not sup._pause.is_set(), "/retry must not unpause a paused run"
    assert sup.run.pending_steering() == []
    assert not list(sup.run.steering_dir.glob("*.md"))
