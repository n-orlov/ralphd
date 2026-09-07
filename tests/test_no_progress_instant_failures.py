"""Black-box test for task 059: the no-progress escalation guard must not
count instant agent-startup/infra failures (e.g. no LLM credentials -- the
agent process exits nonzero in well under a second, having done no
observable work at all) as approach-burning evidence.

Live incident this guards against: with no LLM credentials, 11 consecutive
~0.6s nonzero-exit iterations burned through all 3 approaches and hit
state=failed in seven seconds flat, without ever attempting real work.

Task 010 (#5) changed one thing about this shape, deliberately: an instant
zero-work failure is an infra fault, so it now goes through the infra-retry
wrapper first -- it is retried on the backoff schedule and REFUNDED, so
`iterationsUsed` no longer counts the failed attempts (the iteration
directories on disk still do, and are asserted here instead). The fail-fast
itself is unchanged: MAX_CONSECUTIVE_INSTANT_FAILURES identical instant
failures still abort with the same diagnosis, in seconds, without burning an
approach. The backoff schedule is compressed here only to keep the suite
fast; tests/test_instant_infra_retry.py proves the fast-fail on the real
default schedule and outage budget.
"""

from __future__ import annotations

import json

from tests.conftest import LiveRun

__all__ = ["engine_factory"]

# Compressed infra backoff (the retries between the three instant attempts).
FAST_BACKOFF = {"RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.2,0.4"}


def test_instant_planning_failures_abort_fast_without_burning_approaches(engine_factory):
    e = engine_factory(
        job={"on_complete": "exit", "max_approaches": 3, "iterations": 30},
        stub_env={"STUB_INSTANT_FAIL_COUNT": "10", **FAST_BACKOFF})
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    # Must fail fast via the instant-failure carve-out (state=aborted, a
    # clear diagnostic reason) -- NOT via the no-progress escalation path
    # (which would eventually also reach a terminal failure, but only
    # after silently burning through all 3 approaches).
    assert status["state"] == "aborted"
    assert "credential" in status["reason"].lower() or "instant" in status["reason"].lower()
    # Never got past approach 1 -- the carve-out must trip well before any
    # approach-switch, in exactly MAX_CONSECUTIVE_INSTANT_FAILURES (3)
    # iterations, not the 10 the stub was configured to keep failing for.
    assert status["approach"] == 1
    # Task 010: all three attempts were retried as infra faults, so they are
    # refunded -- the broken environment costs the job no iteration budget.
    assert status["iterationsUsed"] == 0
    iters = sorted((e.run_dir / "iterations").iterdir())
    assert [json.loads((d / "meta.json").read_text())["phase"] for d in iters] == \
        ["planning", "planning", "planning"]


def test_instant_worker_failures_abort_fast_without_burning_approaches(engine_factory):
    # Skip the first invocation (planning succeeds normally, producing
    # tasks.json) then instant-fail every worker iteration after that --
    # mirrors the real incident, where planning itself never even ran
    # under the broken credentials (it presumably used a cached/earlier
    # tasks.json) and only the worker loop kept crashing.
    e = engine_factory(
        job={"on_complete": "exit", "max_approaches": 3, "iterations": 30},
        stub_env={"STUB_INSTANT_FAIL_SKIP": "1", "STUB_INSTANT_FAIL_COUNT": "10",
                  **FAST_BACKOFF})
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "aborted"
    assert "credential" in status["reason"].lower() or "instant" in status["reason"].lower()
    assert status["approach"] == 1
    # 1 planning + 3 consecutive instant worker failures on disk, not 1 + 9
    # (which would mean 3 approaches' worth of the OLD stagnation guard fired
    # instead of the carve-out). Only the healthy planning iteration is
    # charged: task 010 refunds the three retried instant attempts.
    assert status["iterationsUsed"] == 1
    assert len(list((e.run_dir / "iterations").iterdir())) == 4
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert all(t["status"] == "pending" for t in tasks)


def test_claim_rollback_is_two_directional_by_fault_class(engine_factory, tmp_path_factory):
    """Task 123 (#60): the harness's pre-spawn `in-progress` claim (task
    116) on the task a worker iteration is assigned must be rolled back to
    the task's PRIOR status when that iteration never reaches the model at
    all (an infra fault -- reuses the exact instant-startup-failure shape
    the two tests above already assert the terminal state for), but must
    be LEFT `in-progress` when the iteration genuinely reached the model
    and failed doing real work on it (a `work` fault) -- a real, if
    unsuccessful, attempt is not the same claim as a bet that never paid
    off, and rolling it back too would erase that the task was actually
    being worked on.

    Two separate engine processes, each over its OWN tmp dir
    (`engine_factory`'s own fixture reuses one `tmp_path` for every engine
    it starts within a single test -- fine for every other test in this
    module, which only ever starts one, but wrong here since the two
    directions need two independent run dirs).
    """
    # Direction 1: infra fault (never reached the model) -> rolled back to
    # `pending` -- the same STUB_INSTANT_FAIL_COUNT shape as the test above,
    # asserted independently here so this node's own two assertions do not
    # depend on that test surviving unmodified.
    e_infra = engine_factory(
        job={"on_complete": "exit", "max_approaches": 3, "iterations": 30},
        stub_env={"STUB_INSTANT_FAIL_SKIP": "1", "STUB_INSTANT_FAIL_COUNT": "10",
                  **FAST_BACKOFF})
    assert e_infra.proc.wait(timeout=60) == 1
    infra_tasks = json.loads((e_infra.run_dir / "tasks.json").read_text())["tasks"]
    assert all(t["status"] == "pending" for t in infra_tasks)

    # Direction 2: work fault (reached the model, failed doing the task) ->
    # left `in-progress`, never silently reverted to `pending`. Because
    # this is the run's last (and only) approach, the worker never seeing
    # a verdict across 3 iterations trips the SAME last-approach
    # stagnation guard as `test_genuine_stagnation_guard_still_fails_job_
    # unaffected` below -- task 012 (PRD requirement B.3, #153) means it
    # now pauses and escalates (`state: "paused"`, `pauseReason:
    # "stagnation"`) rather than exiting `failed`, so this waits for the
    # PAUSE, not for process exit (the process never exits on its own).
    e_work = LiveRun(
        tmp_path_factory.mktemp("work_fault"),
        {"run_id": "e2e", "iterations": 12, "max_approaches": 1, "on_complete": "exit"},
        stub_env={"STUB_TASKS": "1", "STUB_WORKER_WORK_FAULT": "1"})
    try:
        status = e_work.wait_state(("paused",), timeout=60)
        assert status["pauseReason"] == "stagnation"
        assert status["state"] != "failed"
        assert status.get("verdict") is None, status
        assert e_work.proc.poll() is None
        work_tasks = json.loads((e_work.run_dir / "tasks.json").read_text())["tasks"]
        assert len(work_tasks) == 1
        assert work_tasks[0]["status"] == "in-progress"
    finally:
        e_work.stop()


def test_genuine_stagnation_guard_still_fails_job_unaffected(engine_factory):
    """Regression check: genuine no-progress (a worker that runs to
    completion every time but never changes tasks.json -- no crash, no
    instant exit) must still trip the ordinary 3-iterations-no-progress
    guard exactly as before task 059 -- but task 012 (PRD requirement B.3,
    #153) changed what tripping it means on the LAST approach (`max_
    approaches=1` here, so every approach is the last one): the run now
    pauses and escalates (`state: "paused"`, `pauseReason: "stagnation"`)
    instead of exiting `failed` silently, so this waits for the pause
    (bounded by `wait_state`'s own deadline) rather than for process exit,
    which never happens on its own here. The pre-B.3 `verdict:
    "unverified"` was written BY that `failed` ending, so its
    current-behaviour counterpart is asserted here instead: a parked run
    carries no settled verdict and its engine is still alive."""
    e = engine_factory(job={"on_complete": "exit", "max_approaches": 1},
                       stub_env={"STUB_WORKER_STALLS": "1"})
    status = e.wait_state(("paused",), timeout=60)
    assert status["pauseReason"] == "stagnation"
    assert status["state"] != "failed"
    assert status.get("verdict") is None, status
    assert e.proc.poll() is None, "a stagnation pause must not end the process"
