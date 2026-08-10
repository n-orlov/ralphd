"""Black-box e2e tests for task 001a: infra-fault fail-fast + retry with
backoff.

Uses the stub `pi`'s STUB_INFRA_HANG_COUNT knob (a phase invocation that
sleeps for a very long time producing ZERO stdout/NDJSON output) to prove:

- the engine's startup-window watchdog kills a hung iteration well within
  RALPHD_INFRA_STARTUP_TIMEOUT, not the full iteration_timeout_s;
- an infra-classified retry does NOT consume the iteration budget, and a
  subsequently-healthy stub run completes the job in a verified terminal
  state;
- when retries are exhausted, the job ends in a terminal failed/aborted
  state whose `reason` names the infra fault plainly.
"""

from __future__ import annotations

import json
import time

from test_e2e import engine_factory

__all__ = ["engine_factory"]


def test_startup_watchdog_kills_hang_within_startup_window_not_full_timeout(engine_factory):
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",   # let planning succeed normally
            "STUB_INFRA_HANG_COUNT": "10",  # hang forever after that
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_MAX": "1",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1",
        })
    t0 = time.monotonic()
    assert e.proc.wait(timeout=30) == 1
    elapsed = time.monotonic() - t0
    # Iteration timeout defaults to 45 minutes; the watchdog (1s window) must
    # have killed this well before any full-timeout wait would ever finish.
    assert elapsed < 25
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "aborted"
    assert "infra fault" in status["reason"].lower()
    assert "no llm traffic" in status["reason"].lower()


def test_infra_hang_retry_does_not_consume_budget_then_recovers(engine_factory):
    # Exactly enough charged budget for the happy path (1 planning + 1
    # worker + 1 review == 3): STUB_TASKS=1 means a single successful
    # worker iteration finishes the only task and emits COMPLETE. One
    # hung worker invocation is injected before the healthy one; if it
    # were (wrongly) charged against the budget, review would never run
    # and the job would fail instead of succeeding.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",  # invocation 1 (planning) is healthy
            "STUB_INFRA_HANG_COUNT": "1",  # invocation 2 (1st worker) hangs
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_MAX": "3",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.1,0.1",
        })
    assert e.proc.wait(timeout=30) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)

    events = [json.loads(line) for line in
              (e.run_dir / "events.jsonl").read_text().splitlines()]
    infra_events = [ev for ev in events if ev.get("type") == "infra_retry"]
    assert len(infra_events) == 1
    assert infra_events[0]["phase"] == "worker"
    assert infra_events[0]["attempt"] == 1


def test_infra_retries_exhausted_ends_terminal_with_infra_reason(engine_factory):
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 12, "max_approaches": 3},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",
            "STUB_INFRA_HANG_COUNT": "10",  # hangs on every worker attempt
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_MAX": "2",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.1",
        })
    assert e.proc.wait(timeout=30) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "aborted"
    assert "infra fault" in status["reason"].lower()
    assert "worker" in status["reason"].lower()
    # never switched approaches or burned the ordinary iteration budget --
    # this is the infra-retry give-up path, not the no-progress escalation
    assert status["approach"] == 1

    events = [json.loads(line) for line in
              (e.run_dir / "events.jsonl").read_text().splitlines()]
    infra_events = [ev for ev in events if ev.get("type") == "infra_retry"]
    assert len(infra_events) == 2
    assert [ev["attempt"] for ev in infra_events] == [1, 2]
