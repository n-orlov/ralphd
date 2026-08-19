"""Task 008 (#5, #11): infra retries are driven by a wall-clock outage
budget with an episode clock, not by an attempt count.

The unit half stubs `_run_iteration_once` and records every backoff wait, so
the escalating schedule (last value repeating, capped by
`infra_retry_backoff_max_s` and by what is left of the budget) and the
episode reset are asserted exactly, with a compressed schedule and no real
sleeping of any consequence. The black-box half proves the same thing through
the real engine + stub-pi: more consecutive infra faults than the old
hardcoded 3-attempt cap allowed are ridden out, cost no iterations and no
approach, and the job still reaches a verified terminal state.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from test_e2e import engine_factory

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

__all__ = ["engine_factory"]

INFRA_ERROR = "Connection error."


def _infra_result() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.error_message = INFRA_ERROR
    r.duration_s = 30.0  # not an "instant" failure: this is the retry path
    return r


def _ok_result() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.duration_s = 30.0
    r.final_text = "done"
    return r


def _supervisor(tmp_path, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    cfg = JobConfig(run_id="unit", **cfg_kw)
    return LoopSupervisor(cfg, run, tmp_path)


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]):
    """Feeds `results` to the wrapper one attempt at a time (the last one
    repeats forever), and replaces the backoff wait with a recorder."""
    calls: list[str] = []
    waits: list[float] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        return results[min(len(calls) - 1, len(results) - 1)]

    async def fake_backoff(seconds):
        # Task 015 (#5): the wrapper's wait is an interruptible Event race
        # (_wait_out_backoff), no longer a bare asyncio.sleep -- stub that
        # seam and report "waited the whole backoff, not woken by anyone".
        waits.append(seconds)
        return seconds, False

    async def fake_sleep(seconds):
        # Back-compat shim: call sites that still patch asyncio.sleep get a
        # no-op (the recorded waits come from fake_backoff above).
        return None

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    sup._wait_out_backoff = fake_backoff  # type: ignore[method-assign]
    sup._infra_sleep_patch = fake_sleep  # keep a reference for clarity
    return calls, waits, fake_sleep


def _events(run: RunDir, type_: str) -> list[dict]:
    log = run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# -- unit: episode clock ---------------------------------------------------


@pytest.mark.asyncio
async def test_retries_past_the_old_attempt_cap_then_succeeds(tmp_path, monkeypatch):
    # 6 consecutive infra faults (twice the historical 3-attempt cap) then a
    # healthy attempt: with no explicit infra_retry_max the wrapper keeps
    # going, and the backoff follows the schedule with the last value
    # repeating (0.1, 0.2, 0.4, 0.4, 0.4, 0.4).
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[0.1, 0.2, 0.4],
                      infra_retry_backoff_max_s=10.0,
                      infra_outage_budget_s=1000.0)
    results = [_infra_result()] * 6 + [_ok_result()]
    calls, waits, fake_sleep = _stub_attempts(sup, results)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await sup.run_iteration("worker")

    assert not result.error_message
    assert len(calls) == 7, "unlimited attempts by default: 6 faults + 1 success"
    assert waits == [0.1, 0.2, 0.4, 0.4, 0.4, 0.4]
    assert [ev["attempt"] for ev in _events(sup.run, "infra_retry")] == [1, 2, 3, 4, 5, 6]
    assert [ev["backoffS"] for ev in _events(sup.run, "infra_retry")] == waits
    assert sup._infra_refunded == 6
    # ... and the successful iteration reset the episode clock.
    assert sup._infra_episode_attempts == 0
    assert sup._infra_episode_waited_s == 0.0
    assert sup._infra_episode_started_at is None


@pytest.mark.asyncio
async def test_backoff_is_capped_by_backoff_max_s(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[1.0, 100.0],
                      infra_retry_backoff_max_s=2.0,
                      infra_outage_budget_s=1000.0)
    _calls, waits, fake_sleep = _stub_attempts(
        sup, [_infra_result()] * 3 + [_ok_result()])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await sup.run_iteration("worker")
    assert waits == [1.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_episode_resets_between_separate_outages(tmp_path, monkeypatch):
    # Two independent glitches: the second one gets the full schedule from
    # the start again rather than continuing where the first left off.
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[0.1, 0.2, 0.4],
                      infra_outage_budget_s=1000.0)
    seq = [_infra_result(), _infra_result(), _ok_result(),
           _infra_result(), _ok_result()]
    calls, waits, fake_sleep = _stub_attempts(sup, seq)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await sup.run_iteration("worker")   # consumes the first 3 results
    await sup.run_iteration("worker")   # consumes the last 2
    assert len(calls) == 5
    assert waits == [0.1, 0.2, 0.1]


@pytest.mark.asyncio
async def test_budget_exhaustion_reason_names_outage_duration_and_error(
        tmp_path, monkeypatch):
    # Budget 2s with a 0.5/1/1... schedule: waits are clamped to the budget
    # remainder (0.5, 1.0, 0.5) and the 4th attempt finds the budget spent.
    sup = _supervisor(tmp_path, infra_retry_backoff_s=[0.5, 1.0],
                      infra_outage_budget_s=2.0)
    calls, waits, fake_sleep = _stub_attempts(sup, [_infra_result()])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await sup.run_iteration("worker")

    assert result.error_message == INFRA_ERROR
    assert waits == [0.5, 1.0, 0.5], "cumulative wait never exceeds the budget"
    assert len(calls) == 4
    reason = sup._abort_reason
    assert re.search(r"\d+s infra outage", reason), reason
    assert "4 attempts" in reason
    assert "2s of the 2s outage budget spent waiting" in reason
    assert INFRA_ERROR in reason
    # The give-up is on record for the operator, not just in the return value.
    assert any(reason == ev.get("message")
               for ev in _events(sup.run, "log"))
    last = _events(sup.run, "infra_retry")[-1]
    assert (last["attempt"], last["backoffS"], last["budgetS"]) == (4, None, 2.0)


@pytest.mark.asyncio
async def test_explicit_retry_max_still_caps_attempts(tmp_path, monkeypatch):
    # Back-compat: an operator (or an existing job.yaml/test) that pins a cap
    # keeps the old attempt-count behaviour and the old reason wording, even
    # though the outage budget would have allowed many more attempts.
    sup = _supervisor(tmp_path, infra_retry_max=2,
                      infra_retry_backoff_s=[0.1],
                      infra_outage_budget_s=10_000.0)
    calls, waits, fake_sleep = _stub_attempts(sup, [_infra_result()])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await sup.run_iteration("worker")
    assert len(calls) == 2
    assert waits == [0.1]
    assert sup._abort_reason == (
        f"infra fault: worker iteration failed after 2 attempts ({INFRA_ERROR})")


# -- black box: through the real engine ------------------------------------


def test_five_consecutive_infra_faults_are_ridden_out_free(engine_factory):
    # No RALPHD_INFRA_RETRY_MAX at all: five hung worker invocations (the old
    # default would have given up after three) are retried on a compressed
    # schedule, then the healthy attempt finishes the only task. Charged
    # budget is exactly the happy path (planning + worker + review == 3), so
    # the run can only succeed if every retried attempt was refunded.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",   # invocation 1 (planning) is healthy
            "STUB_INFRA_HANG_COUNT": "5",  # the next five worker attempts hang
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.2,0.4",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "60",
        })
    assert e.proc.wait(timeout=60) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    assert status["iterationsUsed"] == 3, "waiting out an outage costs no iterations"
    assert status["approach"] == 1, "an infra outage never escalates the approach"

    events = [json.loads(line) for line in
              (e.run_dir / "events.jsonl").read_text().splitlines()]
    infra = [ev for ev in events if ev.get("type") == "infra_retry"]
    assert [ev["attempt"] for ev in infra] == [1, 2, 3, 4, 5]
    assert [ev["backoffS"] for ev in infra] == [0.1, 0.2, 0.4, 0.4, 0.4]
    assert all(ev["maxAttempts"] is None for ev in infra), "no cap configured"
    assert [ev["waitedS"] for ev in infra] == [0.0, 0.1, 0.3, 0.7, 1.1]


def test_outage_budget_exhaustion_ends_terminal_naming_the_duration(engine_factory):
    # A permanently broken endpoint: with a 2s outage budget the run gives up
    # quickly and the terminal reason tells the operator how long the outage
    # lasted, how much of the budget went into waiting, and the last error.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 12, "max_approaches": 3},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",
            "STUB_INFRA_HANG_COUNT": "20",  # every worker attempt hangs
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.5,1",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "2",
        })
    assert e.proc.wait(timeout=60) == 1

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "aborted"
    reason = status["reason"]
    assert re.search(r"\d+s infra outage", reason), reason
    assert "2s outage budget spent waiting" in reason
    assert "no llm traffic" in reason.lower()
    assert status["approach"] == 1
    assert status["iterationsUsed"] == 1, "only the healthy planning iteration"
