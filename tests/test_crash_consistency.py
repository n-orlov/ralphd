"""Crash-consistency black-box tests (PRD req 17 / task 030).

Each test launches a real `ralphd-engine` process (via `test_e2e.py`'s
`engine_factory`/`EngineProc`), lets it get partway into a job, then
SIGKILLs that *specific* PID (never a pattern-based kill) at a chosen
moment, and proves that a second, freshly-launched engine over the exact
same run dir (a real `ralphctl resume` does exactly this: same run-dir
mount, fresh container) resumes cleanly to completion with no corrupted
state and monotonic, non-duplicated iteration numbering.

Two crash points are covered:
  (a) mid-worker-iteration -- killed while a task is genuinely
      `in-progress` in tasks.json (STUB_SLEEP holds the worker there,
      after it has written the in-progress flip but before it finishes
      the task) -- proves recovery from a *partially done* iteration.
  (b) at an iteration boundary -- killed while the very next iteration
      (a vigilant "verify" phase) has been started by the engine (its
      iterations/NNNN/meta.json has startedAt) but has not yet done any
      work at all (stub-pi's non-worker phases sleep before touching any
      file, including their own prompt-echo markers) -- proves recovery
      from a *freshly started but wholly unstarted* iteration, with the
      prior iteration's work already durably completed.
"""

from __future__ import annotations

import json
import os
import signal
import time

from test_e2e import engine_factory

__all__ = ["engine_factory"]


def _wait_for(predicate, timeout=30, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError("condition never became true")


def _kill(e):
    """SIGKILL this specific engine subprocess PID (never pkill/killall),
    and wait for the OS to actually reap it before returning."""
    pid = e.proc.pid
    os.kill(pid, signal.SIGKILL)
    e.proc.wait(timeout=10)
    assert e.proc.returncode is not None
    assert e.proc.returncode != 0


def _assert_events_parseable(run_dir):
    text = (run_dir / "events.jsonl").read_text()
    events = []
    for line in text.splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))  # raises if any line is malformed
    return events


def _assert_iterations_monotonic_no_dupes(run_dir):
    iters = sorted((run_dir / "iterations").iterdir())
    numbers = [int(d.name) for d in iters]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers)), f"duplicate iteration numbers: {numbers}"
    return numbers


# --------------------------------------------------------------------------
def test_sigkill_mid_worker_iteration_then_resume_completes(engine_factory):
    e1 = engine_factory(
        job={"on_complete": "idle", "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "3", "STUB_SLEEP": "5"},
    )
    e1.wait_api()

    def task_in_progress():
        p = e1.run_dir / "tasks.json"
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text())
        except json.JSONDecodeError:
            return None
        tasks = d.get("tasks", [])
        return d if any(t["status"] == "in-progress" for t in tasks) else None

    _wait_for(task_in_progress, timeout=30)
    # kill THIS specific pid, right in the middle of the worker's 5s sleep
    # (the task has already been flipped to in-progress and persisted, but
    # the iteration is nowhere near finishing).
    _kill(e1)

    # tasks.json is not corrupted: it still parses, and the picked task is
    # exactly the one left in-progress (nothing torn/half-written).
    tasks_after_kill = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_after_kill) == 3
    statuses = [t["status"] for t in tasks_after_kill]
    assert statuses.count("in-progress") == 1
    assert statuses.count("completed") == 0

    # the crashed iteration itself never got an endedAt -- a genuinely
    # partial iteration record was left on disk, exactly as expected.
    iters_before = sorted((e1.run_dir / "iterations").iterdir())
    assert len(iters_before) >= 2  # at least planning + the killed worker
    crashed_meta = json.loads((iters_before[-1] / "meta.json").read_text())
    assert crashed_meta["phase"] == "worker"
    assert "endedAt" not in crashed_meta
    crashed_number = crashed_meta["number"]

    _assert_events_parseable(e1.run_dir)  # no torn NDJSON lines either

    # Resume: a fresh engine process over the SAME run dir, budget topped up.
    e2 = engine_factory(
        job={"on_complete": "exit", "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "3", "STUB_SLEEP": "0"},
    )
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0

    status = json.loads((e2.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    tasks_final = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_final) == 3
    assert all(t["status"] == "completed" for t in tasks_final)

    numbers = _assert_iterations_monotonic_no_dupes(e2.run_dir)
    # the crashed slot's number was reused (overwritten), not skipped/duped
    assert crashed_number in numbers
    reused_meta = json.loads(
        (e2.run_dir / "iterations" / f"{crashed_number:04d}" / "meta.json").read_text())
    assert "endedAt" in reused_meta  # this time it actually finished

    events_after = _assert_events_parseable(e2.run_dir)
    assert any(ev.get("type") == "log" and "resuming existing run-dir state"
               in ev.get("message", "") for ev in events_after)


def test_sigkill_at_iteration_boundary_then_resume_completes(engine_factory):
    """Non-worker boundary: the worker loop finishes all tasks and emits
    COMPLETE (that iteration is durably endedAt-recorded, all tasks fully
    'completed'), the engine starts the very next iteration -- review --
    (meta.json startedAt written) but stub-pi's non-worker top-level sleep
    means it has not yet touched a single file. Kill lands exactly at that
    boundary: the prior work is 100% durable, the next iteration is 0%
    done."""
    e1 = engine_factory(
        job={"on_complete": "idle", "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2", "STUB_SLEEP": "3"},
    )
    e1.wait_api()

    def review_started():
        itdir = e1.run_dir / "iterations"
        if not itdir.is_dir():
            return None
        for d in sorted(itdir.iterdir()):
            p = d / "meta.json"
            if not p.exists():
                continue
            try:
                meta = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if meta.get("phase") == "review" and "endedAt" not in meta:
                return meta
        return None

    review_meta = _wait_for(review_started, timeout=30)
    review_number = review_meta["number"]
    _kill(e1)

    # The review iteration itself started (engine-side bookkeeping) but its
    # pi invocation never got past its top-of-script sleep: no review-count
    # marker, no endedAt, no review-findings.md.
    assert not (e1.run_dir / ".stub-review-count").exists()
    assert not (e1.run_dir / "review-findings.md").exists()
    crashed_meta = json.loads(
        (e1.run_dir / "iterations" / f"{review_number:04d}" / "meta.json").read_text())
    assert crashed_meta["phase"] == "review"
    assert "endedAt" not in crashed_meta

    # The worker iteration immediately before it is durably, fully done:
    # both tasks really did reach "completed" before the crash, untouched
    # by the kill.
    worker_meta = json.loads(
        (e1.run_dir / "iterations" / f"{review_number - 1:04d}" / "meta.json").read_text())
    assert worker_meta["phase"] == "worker"
    assert "endedAt" in worker_meta
    assert worker_meta["sawComplete"] is True
    tasks_after_kill = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert sum(1 for t in tasks_after_kill if t["status"] == "completed") == 2

    _assert_events_parseable(e1.run_dir)

    # Resume over the same run dir.
    e2 = engine_factory(
        job={"on_complete": "exit", "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2", "STUB_SLEEP": "0"},
    )
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0

    status = json.loads((e2.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    tasks_final = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_final) == 2
    assert all(t["status"] == "completed" for t in tasks_final)

    numbers = _assert_iterations_monotonic_no_dupes(e2.run_dir)
    assert review_number in numbers
    # the crashed slot's number was reused (whatever phase the resumed
    # engine chose to run next there), and this time it actually finished
    reused_meta = json.loads(
        (e2.run_dir / "iterations" / f"{review_number:04d}" / "meta.json").read_text())
    assert "endedAt" in reused_meta

    events_after = _assert_events_parseable(e2.run_dir)
    assert any(ev.get("type") == "log" and "resuming existing run-dir state"
               in ev.get("message", "") for ev in events_after)
