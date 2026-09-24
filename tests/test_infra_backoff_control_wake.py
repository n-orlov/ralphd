"""Cure task cure-01-06 (PRD requirement K.5, DoD 15, #267): an abort or a
steer landing during the REAL infra-retry backoff must not dispatch a second
agent child.

Review's own measurement (`artifacts/review-resume-0196/independent.log`,
node `test_review_contract.py::test_K5_abort_during_real_retry_wait_finishes_
without_another_child`): task 028b/028c wired `_wait_out_backoff` itself to
race a control-plane wake (`self._deferred_wake`, set by `RunDir.
add_steering` and by `_record_abort`) against its timer -- but the LOOP
CALLING `_wait_out_backoff` (`LoopSupervisor._run_iteration_with_infra_
retry`) never looked at how the wait ended: woken or not, it fell straight
through to the top of its own `while True:` and dispatched a fresh
`_run_iteration_once` for the very phase/iteration the wake was trying to
interrupt (`runner_calls=2`, `ended_within_5s=False`). This module pins the
caller-side fix: a wake that is neither the timer elapsing nor `POST /retry`
(`_wait_out_backoff`'s own `woken` flag) returns the LAST attempt's own
(already infra-classified) result straight back to the caller instead of
looping -- no second child, ever.

Two unit nodes drive this through the same stubbed-attempt harness
`tests/test_infra_retry_schedule.py` already established (fast, no real
sleeping); two integration nodes drive the REAL `_wait_out_backoff` (the
`tests/test_interrupt_wake_task028b.py` idiom: rebind the class's own
unbound method onto the instance, since this module is not one of the three
PRD-named `real_backoff` modules) with a REAL `sup.abort()` /
`sup.run.add_steering()` call landing mid-wait, against a backoff bound far
larger than the 5s DoD 15 asks for.

Mutation this guards against: reverting the `elif elapsed < backoff: return
result` branch in `_run_iteration_with_infra_retry` back to falling straight
through would fail every "one call" assertion below (`calls == ["worker"]`)
with `["worker", "worker"]` instead, and the integration nodes would time out
at their own `asyncio.wait_for(..., timeout=5.0)` against the 30s bound.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from test_infra_retry_schedule import (
    INFRA_ERROR,
    _events,
    _infra_result,
    _stub_attempts,
    _supervisor,
)

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import read_events

# Captured at import time, before the per-test autouse class-level
# `_wait_out_backoff` stub (`tests/conftest.py`) can touch the class -- the
# same idiom `tests/test_interrupt_wake_task028b.py` uses, since this module
# is deliberately NOT one of the three `real_backoff`-marked modules.
_REAL_WAIT_OUT_BACKOFF = LoopSupervisor._wait_out_backoff

_BACKOFF_BOUND_S = 30.0   # far larger than the 5s DoD 15 measures against


# ---------------------------------------------------------------------------
# Unit: a stubbed `_wait_out_backoff` that reports a control wake (elapsed
# well short of the backoff, `woken=False`) -- no real sleeping at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abort_during_backoff_dispatches_no_second_child(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[_BACKOFF_BOUND_S],
                      infra_outage_budget_s=1000.0)
    calls, waits, fake_sleep = _stub_attempts(sup, [_infra_result()])

    async def fake_backoff_abort_woken(seconds):
        waits.append(seconds)
        # Mirrors what `_record_abort` really does before ringing the bell.
        sup._abort_reason = "operator asked to stop"
        return 0.2, False   # not retry_now-woken, and well short of `seconds`
    sup._wait_out_backoff = fake_backoff_abort_woken
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await sup.run_iteration("worker")

    assert calls == ["worker"], (
        f"a second child was dispatched after the abort wake: {calls}")
    assert result.error_message == INFRA_ERROR
    # The episode is not over, only interrupted -- unlike `retry_now`'s own
    # branch, a control wake must not reset the outage-budget clock.
    assert sup._infra_episode_attempts == 1
    assert sup._infra_episode_waited_s == pytest.approx(0.2)
    assert sup._abort_reason == "operator asked to stop"


@pytest.mark.asyncio
async def test_steering_wake_during_backoff_dispatches_no_second_child(
        tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[_BACKOFF_BOUND_S],
                      infra_outage_budget_s=1000.0)
    calls, waits, fake_sleep = _stub_attempts(sup, [_infra_result()])

    async def fake_backoff_steer_woken(seconds):
        waits.append(seconds)
        return 0.3, False   # no abort recorded -- a plain steer wake
    sup._wait_out_backoff = fake_backoff_steer_woken
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await sup.run_iteration("worker")

    assert calls == ["worker"], (
        f"a second child was dispatched after the steering wake: {calls}")
    assert result.error_message == INFRA_ERROR
    assert sup._abort_reason is None
    assert sup._infra_episode_attempts == 1
    assert sup._infra_episode_waited_s == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Integration: the REAL `_wait_out_backoff`, a REAL `sup.abort()` /
# `sup.run.add_steering()` call landing mid-wait, against a 30s bound.
# ---------------------------------------------------------------------------


def _rig_real_backoff(sup, results):
    """Stubs only `_run_iteration_once` (never spawn a real agent) and
    rebinds the REAL `_wait_out_backoff` onto this instance."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None,
                        task_id=None):
        calls.append(phase)
        return results[min(len(calls) - 1, len(results) - 1)]

    sup._run_iteration_once = fake_once   # type: ignore[method-assign]
    sup._wait_out_backoff = _REAL_WAIT_OUT_BACKOFF.__get__(   # type: ignore[method-assign]
        sup, LoopSupervisor)
    return calls


async def _wait_until_parked(sup) -> None:
    for _ in range(400):
        if sup._infra_waiting:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the supervisor never entered the backoff wait")


@pytest.mark.asyncio
async def test_real_abort_during_real_backoff_ends_within_5s_no_second_child(
        tmp_path):
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[_BACKOFF_BOUND_S],
                      infra_outage_budget_s=1000.0)
    calls = _rig_real_backoff(sup, [_infra_result()])

    async def _abort_soon():
        await _wait_until_parked(sup)
        sup.abort("operator asked to stop")

    started = time.monotonic()
    result, _ = await asyncio.wait_for(
        asyncio.gather(sup.run_iteration("worker"), _abort_soon()),
        timeout=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, (
        f"abort during a {_BACKOFF_BOUND_S}s backoff took {elapsed:.3f}s -- "
        "the wake, not the bound, must end it")
    assert calls == ["worker"], (
        f"a second child was dispatched after the abort: {calls}")
    assert result.error_message == INFRA_ERROR
    assert sup.operator_abort_requested
    logs = [e for e in read_events(sup.run.root)
            if e.get("type") == "log" and e.get("boundedBy")]
    assert logs and logs[-1]["boundedBy"] == "abort", logs


@pytest.mark.asyncio
async def test_real_steer_during_real_backoff_returns_promptly_no_second_child(
        tmp_path):
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[_BACKOFF_BOUND_S],
                      infra_outage_budget_s=1000.0)
    calls = _rig_real_backoff(sup, [_infra_result()])

    async def _steer_soon():
        await _wait_until_parked(sup)
        sup.run.add_steering("proceed without waiting")

    started = time.monotonic()
    result, _ = await asyncio.wait_for(
        asyncio.gather(sup.run_iteration("worker"), _steer_soon()),
        timeout=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, (
        f"a steering wake during a {_BACKOFF_BOUND_S}s backoff took "
        f"{elapsed:.3f}s to return control")
    assert calls == ["worker"], (
        f"a second child was dispatched after the steer: {calls}")
    assert result.error_message == INFRA_ERROR
    assert sup._abort_reason is None
    # The episode is interrupted, not over: the next actionable pass's own
    # worker dispatch resumes the SAME schedule instead of restarting it.
    assert sup._infra_episode_attempts == 1
    # The steer is left untouched for the dispatch pass's own top-of-pass
    # `pending_steering()` check -- this phase's own (never-dispatched)
    # second attempt must never have consumed it.
    assert len(sup.run.pending_steering()) == 1
    logs = [e for e in read_events(sup.run.root)
            if e.get("type") == "log" and e.get("boundedBy")]
    assert logs and logs[-1]["boundedBy"] == "steering", logs


@pytest.mark.asyncio
async def test_manual_retry_now_during_real_backoff_still_dispatches_a_second_child(
        tmp_path):
    """Contrast: `POST /retry` -> `retry_now()` keeps its existing
    reset/accounting semantics untouched -- it is a DELIBERATE second
    attempt, not a control-plane wake, and this task changes nothing about
    it (`if woken:` in the caller is a separate branch from the new `elif`
    this module otherwise exercises)."""
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[_BACKOFF_BOUND_S],
                      infra_outage_budget_s=1000.0)
    calls = _rig_real_backoff(sup, [_infra_result(), _infra_result()])

    async def _retry_soon():
        await _wait_until_parked(sup)
        assert sup.retry_now() is True

    started = time.monotonic()
    # `run_iteration` itself does NOT return within 5s here -- the second
    # attempt is infra-classified too, so the wrapper sits out a fresh 30s
    # backoff after it, unwoken this time. Only the SECOND DISPATCH itself
    # (the thing this contrast is proving `retry_now` still does) is bounded.
    driver = asyncio.ensure_future(sup.run_iteration("worker"))
    await asyncio.wait_for(_retry_soon(), timeout=5.0)
    for _ in range(400):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.005)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert calls == ["worker", "worker"], (
        "a manual retry must still dispatch a fresh attempt: "
        f"{calls}")
    # The operator asserting "it is back" resets the outage-budget episode
    # clock -- unchanged from task 015.
    assert sup._infra_episode_waited_s == 0.0

    driver.cancel()
    try:
        await driver
    except asyncio.CancelledError:
        pass
