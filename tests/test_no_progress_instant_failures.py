"""Black-box test for task 059: the no-progress escalation guard must not
count instant agent-startup/infra failures (e.g. no LLM credentials -- the
agent process exits nonzero in well under a second, having done no
observable work at all) as approach-burning evidence.

Live incident this guards against: with no LLM credentials, 11 consecutive
~0.6s nonzero-exit iterations burned through all 3 approaches and hit
state=failed in seven seconds flat, without ever attempting real work.
"""

from __future__ import annotations

import json

from test_e2e import engine_factory

__all__ = ["engine_factory"]


def test_instant_planning_failures_abort_fast_without_burning_approaches(engine_factory):
    e = engine_factory(
        job={"on_complete": "exit", "max_approaches": 3, "iterations": 30},
        stub_env={"STUB_INSTANT_FAIL_COUNT": "10"})
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
    assert status["iterationsUsed"] == 3
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
        stub_env={"STUB_INSTANT_FAIL_SKIP": "1", "STUB_INSTANT_FAIL_COUNT": "10"})
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "aborted"
    assert "credential" in status["reason"].lower() or "instant" in status["reason"].lower()
    assert status["approach"] == 1
    # 1 planning + 3 consecutive instant worker failures, not 1 + 9 (which
    # would mean 3 approaches' worth of the OLD stagnation guard fired
    # instead of the new carve-out).
    assert status["iterationsUsed"] == 4
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert all(t["status"] == "pending" for t in tasks)


def test_genuine_stagnation_guard_still_fails_job_unaffected(engine_factory):
    """Regression check: genuine no-progress (a worker that runs to
    completion every time but never changes tasks.json -- no crash, no
    instant exit) must still trip the ordinary 3-iterations-no-progress
    guard and fail the job exactly as before task 059."""
    e = engine_factory(job={"on_complete": "exit", "max_approaches": 1},
                       stub_env={"STUB_WORKER_STALLS": "1"})
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"
