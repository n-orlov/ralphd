"""Vigilant-mode crash/resume regression (task 052; guard 4 in the
conformance ledger, folded onto the task record by task 020).

LoopSupervisor's vigilant-verify trigger used to detect a task's completion
by diffing tasks.json snapshots taken immediately before/after a single
`run_iteration("worker")` call, entirely within the current process. If the
engine crashed after a worker iteration wrote a task as "completed" but
before (or during) the corresponding verify iteration, a resumed process's
very first tasks.json snapshot already showed that task as "completed" --
so the newly-completed diff never fired for it again, and its mandatory
vigilant verification was silently skipped for the rest of the job (the job
could still reach verdict=verified via the reviewer phase without that task
ever having been vigilant-verified). The fix used to be a separate,
approach-namespaced `vigilant-verified.json` sidecar; task 020 folds that
role into the task record's own mini-FSM status instead -- the harness
relabels a worker's raw `completed` claim to `awaiting-validation` the
moment its subprocess exits, and only `_verify_task`'s own passing write
ever advances it to `validated`, so the crash/resume property below now
holds from that one field, with no sidecar to keep in sync.

This test proves the fix: a task reaches `awaiting-validation`, the engine
is SIGKILLed before the verify iteration for it ever finishes (its verify
iteration's meta.json has startedAt but never endedAt -- killed while `pi`
is still asleep at the top of its invocation, having touched no files yet),
then a fresh engine resumes over the same run dir and the task DOES
eventually get a real, passing verify iteration (a `taskVerified` signal is
emitted for it, and its verify meta.json shows `verifyOutcome: pass`) before
the job reaches its terminal succeeded/verified state.
"""

from __future__ import annotations

import json
import os
import signal
import time


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
    pid = e.proc.pid
    os.kill(pid, signal.SIGKILL)
    e.proc.wait(timeout=10)
    assert e.proc.returncode is not None
    assert e.proc.returncode != 0


def _events(run_dir):
    text = (run_dir / "events.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_sigkill_after_task_completed_before_verify_finishes_then_resume_verifies(
        engine_factory):
    e1 = engine_factory(
        job={"on_complete": "idle", "vigilant": True, "iterations": 15,
             "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "1"},
    )
    e1.wait_api()

    def verify_started_not_finished():
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
            if meta.get("phase") == "verify" and "endedAt" not in meta:
                return meta
        return None

    verify_meta = _wait_for(verify_started_not_finished, timeout=30)
    verify_number = verify_meta["number"]

    # The task the worker completed just before this verify iteration
    # started has already been relabelled to the vigilant claim state
    # `awaiting-validation` -- the harness does that the instant the
    # worker's own subprocess exits (task 020), strictly before the verify
    # iteration this test kills mid-flight ever starts.
    tasks_mid = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_mid) == 1
    assert tasks_mid[0]["status"] == "awaiting-validation"

    _kill(e1)

    # The crashed verify iteration genuinely never finished: no endedAt, no
    # verifiedTask/verifyOutcome recorded, no stub verify-call marker (stub
    # sleeps before touching any file at all in non-worker phases).
    crashed_meta = json.loads(
        (e1.run_dir / "iterations" / f"{verify_number:04d}" / "meta.json").read_text())
    assert crashed_meta["phase"] == "verify"
    assert "endedAt" not in crashed_meta
    assert "verifyOutcome" not in crashed_meta
    assert not (e1.run_dir / ".stub-verify-call-count").exists()

    # No taskVerified signal was ever emitted for it, and the task record
    # -- the ONLY place this is tracked since task 020's fold -- still
    # shows it unverified (not "validated"): asserted from disk state, not
    # a before/after diff.
    events_before = _events(e1.run_dir)
    assert not any(ev.get("type") == "signal" and ev.get("signal") == "taskVerified"
                   for ev in events_before)
    tasks_mid_after_kill = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks_mid_after_kill[0]["status"] == "awaiting-validation"

    # Resume: a fresh engine process over the SAME run dir.
    e2 = engine_factory(
        job={"on_complete": "exit", "vigilant": True, "iterations": 15,
             "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
    )
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0

    status = json.loads((e2.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    # The task genuinely got a real, passing verify iteration this time --
    # not silently skipped across the crash/resume boundary.
    events_after = _events(e2.run_dir)
    tv_events = [ev for ev in events_after
                 if ev.get("type") == "signal" and ev.get("signal") == "taskVerified"
                 and ev.get("taskId") == "001"]
    assert len(tv_events) == 1

    iters = sorted((e2.run_dir / "iterations").iterdir())
    verify_metas = [json.loads((d / "meta.json").read_text()) for d in iters
                    if json.loads((d / "meta.json").read_text()).get("phase") == "verify"]
    passing = [m for m in verify_metas
              if m.get("verifiedTask") == "001" and m.get("verifyOutcome") == "pass"]
    assert len(passing) == 1

    # The task record itself is now the durable verified marker (task 020
    # folded the old `vigilant-verified.json` sidecar into it) -- no
    # separate file survives to check.
    assert not (e2.run_dir / "vigilant-verified.json").exists()
    tasks_final = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks_final[0]["status"] == "validated"
