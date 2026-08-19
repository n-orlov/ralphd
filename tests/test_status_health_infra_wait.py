"""Task 012 (#5): the status contract says a run is degraded, without
growing a new `state` value.

`state` must stay `starting|running|succeeded|failed|aborted` -- a "degraded"
state value would break every consumer's terminal-state logic (`ralphctl
watch` included). The degraded case is carried by `health` ("ok"/"degraded")
and `infraWait` (null unless actually sitting in a backoff wait), plus the
`infra_wait`/`infra_recovered` events so the episode is visible in the stream
`ralphctl watch` follows.

The unit half samples status.json from *inside* the backoff wait (the stubbed
sleep reads the file, so no test ever really sleeps); the black-box half
proves the same fields reach a real run dir and `ralphctl status --json`.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from test_infra_outage_budget import (
    _events,
    _infra_result,
    _ok_result,
    _supervisor,
)

from ralphd.engine.state import parse_utc

TERMINAL = ("succeeded", "failed", "aborted")
STATES = ("starting", "running", *TERMINAL)


def _status(sup) -> dict:
    f = sup.run.root / "status.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _sampling_attempts(sup, results):
    """Like test_infra_outage_budget._stub_attempts, but the stubbed backoff
    wait also snapshots status.json -- that snapshot is what an operator
    polling GET /status mid-wait would have seen."""
    calls: list[str] = []
    waits: list[float] = []
    mid_wait: list[dict] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        return results[min(len(calls) - 1, len(results) - 1)]

    async def fake_backoff(seconds):
        # Task 015 (#5): _wait_out_backoff (an interruptible Event race)
        # replaced the wrapper's asyncio.sleep.
        waits.append(seconds)
        mid_wait.append(_status(sup))
        return seconds, False

    async def fake_sleep(seconds):
        return None  # back-compat no-op for the asyncio.sleep patch below

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    sup._wait_out_backoff = fake_backoff  # type: ignore[method-assign]
    return calls, waits, mid_wait, fake_sleep


# -- unit: the fields, mid-wait and after recovery -------------------------


@pytest.mark.asyncio
async def test_health_degraded_and_infra_wait_populated_mid_wait(
        tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[1.0, 2.0],
                      infra_outage_budget_s=100.0)
    sup.run.update_status(state="running", health="ok", infraWait=None)
    results = [_infra_result()] * 2 + [_ok_result()]
    _calls, waits, mid_wait, fake_sleep = _sampling_attempts(sup, results)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    before = time.time()
    await sup.run_iteration("worker")

    assert waits == [1.0, 2.0]
    assert len(mid_wait) == 2
    for i, snap in enumerate(mid_wait, start=1):
        assert snap["health"] == "degraded"
        assert snap["state"] == "running", "no new `state` value is introduced"
        wait = snap["infraWait"]
        assert wait is not None, "infraWait must be populated while waiting"
        assert wait["attempt"] == i
        assert wait["phase"] == "worker"
        assert "Connection error" in wait["error"]
        assert wait["budgetS"] == 100.0
        assert parse_utc(wait["since"]) >= before - 1
        # nextAttemptAt == since + this attempt's backoff.
        assert parse_utc(wait["nextAttemptAt"]) == pytest.approx(
            parse_utc(wait["since"]) + waits[i - 1], abs=1.5)
        # waitedS is what this episode has already spent; remainingS the rest.
        assert wait["waitedS"] == pytest.approx(sum(waits[: i - 1]))
        assert wait["remainingS"] == pytest.approx(100.0 - wait["waitedS"])

    # ... and the successful attempt clears both halves again.
    final = _status(sup)
    assert final["health"] == "ok"
    assert final["infraWait"] is None
    assert final["state"] == "running"


@pytest.mark.asyncio
async def test_between_two_backoffs_infra_wait_is_null_but_health_degraded(
        tmp_path, monkeypatch):
    # A run that is *running an attempt* is not waiting on anything, so
    # infraWait is null -- but it has not recovered either, so health stays
    # degraded until an iteration actually reaches the model.
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[1.0],
                      infra_outage_budget_s=100.0)
    seen: list[tuple[str, object]] = []

    async def fake_once(phase, extra="", prompt_name=None):
        s = _status(sup)
        seen.append((s.get("health", "ok"), s.get("infraWait")))
        return _infra_result() if len(seen) < 3 else _ok_result()

    async def fake_sleep(seconds):
        pass

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await sup.run_iteration("worker")

    assert seen[0] == ("ok", None), "healthy before the first fault"
    assert seen[1] == ("degraded", None), "attempt 2: degraded, not waiting"
    assert seen[2] == ("degraded", None), "attempt 3: still degraded"
    assert _status(sup)["health"] == "ok"


@pytest.mark.asyncio
async def test_wait_and_recovery_are_events_in_the_stream(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[1.0, 2.0],
                      infra_outage_budget_s=100.0)
    results = [_infra_result()] * 2 + [_ok_result()]
    _calls, _waits, _mid, fake_sleep = _sampling_attempts(sup, results)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await sup.run_iteration("worker")

    waits = _events(sup.run, "infra_wait")
    assert [ev["attempt"] for ev in waits] == [1, 2]
    assert [ev["backoffS"] for ev in waits] == [1.0, 2.0]
    for ev in waits:
        for key in ("since", "error", "phase", "nextAttemptAt",
                    "waitedS", "budgetS", "remainingS"):
            assert key in ev, key
    recovered = _events(sup.run, "infra_recovered")
    assert len(recovered) == 1 and recovered[0]["health"] == "ok"
    # No `state` event ever carries a value outside the documented set.
    for ev in _events(sup.run, "state"):
        assert ev.get("state") in STATES


@pytest.mark.asyncio
async def test_a_healthy_iteration_never_writes_the_degraded_fields(
        tmp_path, monkeypatch):
    sup = _supervisor(tmp_path)
    sup.run.update_status(state="running", health="ok", infraWait=None)
    _calls, _waits, _mid, fake_sleep = _sampling_attempts(sup, [_ok_result()])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await sup.run_iteration("worker")

    assert _status(sup)["health"] == "ok"
    assert _status(sup)["infraWait"] is None
    assert _events(sup.run, "infra_wait") == []
    assert _events(sup.run, "infra_recovered") == []


# -- black box: through a real engine and `ralphctl status --json` ---------


def test_ralphctl_status_json_carries_health_and_infra_wait(live):
    # Planning is healthy; the first two worker invocations hang with zero
    # LLM traffic, get killed by the startup watchdog, are classified infra
    # and retried after a 5s backoff -- long enough to poll the degraded
    # window, short enough that the test stays fast.
    run = live(run_id="health-infra-wait", job={"iterations": 6},
               stub_env={
                   "STUB_TASKS": "1",
                   "STUB_INFRA_HANG_SKIP": "1",
                   "STUB_INFRA_HANG_COUNT": "2",
                   "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
                   "RALPHD_INFRA_RETRY_BACKOFF_S": "5,5",
                   "RALPHD_INFRA_OUTAGE_BUDGET_S": "600",
               })
    run.wait_api()

    deadline = time.time() + 30
    degraded: dict = {}
    seen_states: set = set()
    while time.time() < deadline and not degraded:
        res = run.ralphctl("--json", "status", run.run_id)
        if res.returncode == 0:
            try:
                status = json.loads(res.stdout)
            except json.JSONDecodeError:
                status = {}
            if status.get("state"):
                seen_states.add(status["state"])
            if status.get("health") == "degraded" and status.get("infraWait"):
                degraded = status
        time.sleep(0.2)

    assert degraded, "GET /status never reported health degraded with an infraWait"
    wait = degraded["infraWait"]
    assert degraded["state"] == "running", "no new `state` value"
    assert wait["attempt"] >= 1
    assert wait["phase"] == "worker"
    assert wait["error"]
    assert wait["budgetS"] == 600.0
    assert wait["remainingS"] <= 600.0
    assert parse_utc(wait["nextAttemptAt"]) >= parse_utc(wait["since"])

    status = run.wait_terminal(timeout=60)
    assert status["state"] == "succeeded"
    # Recovery cleared both halves again.
    assert status["health"] == "ok"
    assert status["infraWait"] is None
    assert seen_states <= set(STATES), seen_states

    events = [json.loads(x) for x in
              (run.run_dir / "events.jsonl").read_text().splitlines()]
    types = [ev["type"] for ev in events]
    assert "infra_wait" in types, "the wait must be visible in the event stream"
    assert "infra_recovered" in types
    assert all(ev.get("state") in STATES
               for ev in events if ev["type"] == "state")
