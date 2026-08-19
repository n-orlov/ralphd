"""Task 010 (#5): an *instant* infra fault is retried like any other, but a
run of identical instant zero-work failures still fails fast.

Before this task the infra-retry wrapper handed any sub-5s no-traffic failure
straight back to the broken-environment carve-out, so a gateway that refused
one connection while coming up (0.2s, no traffic) ended the run on the third
attempt regardless of how transient it was. Now both halves coexist:

- a transient instant fault is retried on the escalating backoff and refunded;
- MAX_CONSECUTIVE_INSTANT_FAILURES attempts that all fail instantly, with no
  traffic and with the SAME error signature, stop the job in seconds with the
  pre-existing missing-credential diagnosis -- never after the multi-hour
  outage budget.

The unit half stubs `_run_iteration_once` and the backoff sleep (nothing real
is ever slept); the black-box half drives the real engine + stub-pi.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest
from test_e2e import engine_factory

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

__all__ = ["engine_factory"]

REFUSED = "connect ECONNREFUSED 127.0.0.1:4000"


def _instant_infra(error: str = REFUSED, exit_code: int = 0) -> IterationResult:
    """The shape task 010 is about: sub-INSTANT_FAILURE_MAX_DURATION_S, no
    assistant text, zero tokens (pi zero-fills usage on its in-band error
    message_end), infra-shaped error, clean exit."""
    r = IterationResult(exit_code=exit_code)
    r.error_message = error
    r.duration_s = 0.2
    r.usage = {"input": 0, "output": 0, "totalTokens": 0, "costUSD": 0}
    return r


def _instant_crash(error: str = "no LLM credentials configured") -> IterationResult:
    """The broken-environment shape: the agent process dies before emitting
    a single NDJSON event, so there is no usage block at all (task 059's
    live incident: ~0.6s nonzero exits with a stable error)."""
    r = IterationResult(exit_code=1)
    r.error_message = error
    r.duration_s = 0.6
    return r


def _ok_result() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.duration_s = 30.0
    r.final_text = "done"
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _supervisor(tmp_path, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    kw = {"infra_retry_backoff_s": [0.1, 0.2, 0.4],
          "infra_retry_backoff_max_s": 10.0,
          "infra_outage_budget_s": 1000.0, **cfg_kw}
    cfg = JobConfig(run_id="unit", **kw)
    return LoopSupervisor(cfg, run, tmp_path)


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]):
    calls: list[str] = []
    waits: list[float] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        # A fresh object per attempt, like the real runner: identity is what
        # _check_instant_failure() memoises its per-attempt verdict on.
        return copy.copy(results[min(len(calls) - 1, len(results) - 1)])

    async def fake_backoff(seconds):
        # Task 015 (#5): _wait_out_backoff is the interruptible-Event seam
        # that replaced the wrapper's asyncio.sleep.
        waits.append(seconds)
        return seconds, False

    async def fake_sleep(seconds):
        return None  # back-compat no-op for call sites patching asyncio.sleep

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    sup._wait_out_backoff = fake_backoff  # type: ignore[method-assign]
    return calls, waits, fake_sleep


def _events(run: RunDir, type_: str) -> list[dict]:
    log = run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# -- unit: instant faults are retried --------------------------------------


@pytest.mark.asyncio
async def test_instant_infra_fault_recovering_on_attempt_3_is_retried(
        tmp_path, monkeypatch):
    sup = _supervisor(tmp_path)
    calls, waits, fake_sleep = _stub_attempts(
        sup, [_instant_infra(), _instant_infra(), _ok_result()])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await sup.run_iteration("worker")

    assert result.final_text == "done", "the wrapper rode the glitch out"
    assert len(calls) == 3
    assert waits == [0.1, 0.2], "escalating backoff, exactly as for any infra fault"
    assert sup._infra_refunded == 2, "the two instant attempts cost no iterations"
    assert sup._abort_reason is None
    # Below the threshold the carve-out only counted, and the recovery reset it.
    assert sup._instant_failure_streak == 0
    infra = _events(sup.run, "infra_retry")
    assert [ev["attempt"] for ev in infra] == [1, 2]
    assert all(ev["instantFailure"] is True for ev in infra), infra


@pytest.mark.asyncio
async def test_varying_instant_signatures_never_trip_the_broken_env_verdict(
        tmp_path, monkeypatch):
    # Transient faults vary: four instant failures with four different error
    # texts are four glitches, not a broken environment, so the streak keeps
    # restarting and the wrapper carries on until the healthy attempt.
    sup = _supervisor(tmp_path)
    seq = [_instant_infra("connect ECONNREFUSED 127.0.0.1:4000"),
           _instant_infra("Connection error."),
           _instant_infra("getaddrinfo EAI_AGAIN gateway.internal"),
           _instant_infra("upstream 503 Service Unavailable"),
           _ok_result()]
    calls, waits, fake_sleep = _stub_attempts(sup, seq)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await sup.run_iteration("worker")

    assert result.final_text == "done"
    assert len(calls) == 5, "no fail-fast: the signature never stayed stable"
    assert sup._abort_reason is None
    assert waits == [0.1, 0.2, 0.4, 0.4]


@pytest.mark.asyncio
async def test_digits_only_differences_still_count_as_the_same_signature(
        tmp_path, monkeypatch):
    # The same broken credential reported with a fresh request id / port each
    # time is one signature -- digits are normalised away.
    sup = _supervisor(tmp_path)
    seq = [_instant_infra("connect ECONNREFUSED 127.0.0.1:4000"),
           _instant_infra("connect ECONNREFUSED 127.0.0.1:4001"),
           _instant_infra("connect ECONNREFUSED 127.0.0.1:4002")]
    calls, _waits, fake_sleep = _stub_attempts(sup, seq)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await sup.run_iteration("worker")

    assert len(calls) == 3
    assert sup._abort_reason is not None


# -- unit: the broken-environment fast-fail survives -----------------------


@pytest.mark.asyncio
async def test_stable_instant_failures_fail_fast_with_the_broken_env_diagnosis(
        tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, infra_outage_budget_s=14_400.0)
    calls, waits, fake_sleep = _stub_attempts(sup, [_instant_crash()])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await sup.run_iteration("planning")

    assert result.error_message == "no LLM credentials configured"
    assert len(calls) == LoopSupervisor.MAX_CONSECUTIVE_INSTANT_FAILURES == 3, \
        "stops at the carve-out's threshold, not at the 4h outage budget"
    assert waits == [0.1, 0.2], "only the waits between those three attempts"
    reason = sup._abort_reason
    assert reason is not None
    assert "3 consecutive iterations" in reason
    assert "credential" in reason
    # The last retry event says the wrapper is not going to wait again.
    assert _events(sup.run, "infra_retry")[-1]["backoffS"] is None
    # The verdict is memoised per attempt: the planning/worker call sites
    # score the very same result again and must not inflate the streak.
    assert sup._check_instant_failure(result, 3) is True
    assert sup._instant_failure_streak == 3


@pytest.mark.asyncio
async def test_a_reached_model_between_instant_faults_resets_the_streak(
        tmp_path, monkeypatch):
    sup = _supervisor(tmp_path)
    # Fresh IterationResult per attempt, exactly as the runner produces them.
    seq = [_instant_crash(), _instant_crash(), _ok_result(),
           _instant_crash(), _instant_crash(), _ok_result()]
    calls, _waits, fake_sleep = _stub_attempts(sup, seq)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await sup.run_iteration("worker")   # 2 instant faults, then success
    assert sup._abort_reason is None
    assert sup._instant_failure_streak == 0, "reaching the model ended the streak"
    await sup.run_iteration("worker")   # 2 more: still below the threshold
    assert len(calls) == 6, "2+1 attempts per episode; the streak never reached 3"
    assert sup._abort_reason is None


# -- black box: through the real engine ------------------------------------


def test_instant_refused_connection_recovers_and_the_job_completes(engine_factory):
    # Two instant, exit-0, zero-token "connection refused" worker attempts
    # (STUB_INBAND_ERROR_DELAY_S unset == 0, well inside the 5s instant
    # window) then a healthy one. Charged budget is exactly the happy path
    # (planning + worker + review == 3), so the run can only reach a
    # verified terminal state if both instant attempts were retried in
    # place AND refunded.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INBAND_ERROR_SKIP": "1",   # invocation 1 (planning) is healthy
            "STUB_INBAND_ERROR_COUNT": "2",
            "STUB_INBAND_ERROR_MESSAGE": REFUSED,
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.2",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "60",
        })
    assert e.proc.wait(timeout=60) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    assert status["iterationsUsed"] == 3, "instant infra attempts are refunded"
    assert status["approach"] == 1

    infra = _events(RunDir(root=e.run_dir), "infra_retry")
    assert [ev["attempt"] for ev in infra] == [1, 2]
    assert all(ev["instantFailure"] is True for ev in infra), infra
    assert all(REFUSED in ev["error"] for ev in infra)
    metas = [json.loads((d / "meta.json").read_text())
             for d in sorted((e.run_dir / "iterations").iterdir())]
    assert [m["faultClass"] for m in metas] == [None, "infra", "infra", None, None]


def test_broken_credentials_still_fail_fast_not_after_the_outage_budget(
        engine_factory):
    # The missing-credential shape, on the DEFAULT backoff schedule and the
    # DEFAULT 4h outage budget: the run must be over in seconds because the
    # broken-environment carve-out -- not the outage budget -- is what stops
    # it. Every attempt fails identically and instantly.
    started = time.monotonic()
    e = engine_factory(
        job={"on_complete": "exit", "max_approaches": 3, "iterations": 30},
        stub_env={"STUB_INSTANT_FAIL_COUNT": "10"})
    assert e.proc.wait(timeout=60) == 1
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"failed fast? took {elapsed:.1f}s (backoff 2+5s + startup)"

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "aborted"
    assert "credential" in status["reason"].lower()
    assert status["approach"] == 1, "no approach burned on a broken environment"
    # Exactly MAX_CONSECUTIVE_INSTANT_FAILURES attempts, all of them planning.
    iters = sorted((e.run_dir / "iterations").iterdir())
    assert [json.loads((d / "meta.json").read_text())["phase"] for d in iters] == \
        ["planning"] * 3
