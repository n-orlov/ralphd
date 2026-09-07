"""Flow (task 016, requirement C.5): **crash, resume, and the refund
accounting that survives it** -- over a real `ralphd-engine` subprocess with
the stub `pi` binary on PATH, `STUB_SLEEP=0`.

Two facts this flow pins together that no earlier flow module combines:

  * a worker attempt that hangs (`STUB_INFRA_HANG_COUNT=1`) is classified
    infra, retried and REFUNDED (task 023, #32) -- `_refund_iteration`
    writes `status.json`'s `iterationsRefunded` in the same breath the
    retry decision is made, deliberately BEFORE anything the engine does
    next could kill it;
  * a SIGKILL landing later, mid the following `review` invocation (before
    it ever records a verdict), and a fresh engine resuming over the SAME
    run dir must not re-charge or drop that refund -- `_seed_refunds`
    reads `iterationsRefunded` straight back off disk rather than
    re-deriving it from the iteration directories, precisely because a
    grace refund carries no `faultClass` at all and could never be told
    apart from a charged attempt by directory contents alone.

The crash lands deterministically, never by a real-time race: the stub's
bounded hold primitive (task 007/008, B.1/B.2, `STUB_HOLD_PHASE=review`)
blocks the review invocation before it touches any file, so the SIGKILL
below always lands with that iteration's `meta.json` holding a `startedAt`
and no `endedAt` -- the exact `_crashed_mid_review` shape `_resume_point`
recognises, so the resumed engine lands directly back in `review` (no
throwaway worker iteration, no re-planning) instead of racing that
recognition against wall-clock timing.

Task 021 (requirement C.10) folds three more crash points into this same
module, each with the SAME held-then-SIGKILLed-then-resumed shape but at a
different boundary, reusing the `_kill`/`_wait_for` helpers below (formerly
`tests/test_crash_consistency.py` and `tests/test_vigilant_crash_resume.py`,
deleted, see `docs/test-suite-audit.md` § Subsumed):

  * mid-`worker` -- killed while a task is genuinely `in-progress`
    (partially done, not yet at any boundary at all);
  * at the `worker` -> `review` boundary -- the worker loop finished every
    task and durably recorded it, but the next (`review`) iteration has
    only just started, with zero work of its own on disk yet;
  * at the `worker` -> `verify` boundary under `vigilant: true` -- a task
    already relabelled `awaiting-validation` (the harness's own claim
    relabelling, not a diff) with its verify iteration started but not
    finished -- proving the mini-FSM status survives the crash and a fresh
    verify iteration still runs after resume rather than being silently
    skipped.

Each proves the same property the first test in this module proves for
`review`: tasks.json is never corrupted, `events.jsonl` never holds a torn
line, iteration numbers stay monotonic with no duplicates across the resume
boundary, and the crashed slot's number is reused (not skipped) by whatever
the resumed engine runs there next.

Two invocations of `pi` under `on_complete: idle`.

  1. `e1` -- worker attempt 1 hangs (infra fault, retried, refunded), attempt
     2 reaches the model and completes the run's one task
     (`STUB_TASKS=1`); review begins and is held. SIGKILLed with the review
     iteration mid-flight.
  2. `e2` -- a fresh engine over the identical run dir (same `tmp_path`,
     `engine_factory` reuses `tmp/run` on every call within one test), no
     hold, no hang: `_crashed_mid_review` lands it straight back in
     `review`, which now runs for real and verifies.

Everything is read back off the run's own record; `flowlib.flow_steps`'s
reduced sequence covers BOTH engine processes at once, since
`events.jsonl` is append-only across the resume boundary and both `LiveRun`
objects point at the same file.
"""

from __future__ import annotations

import json
import os
import signal
import time

from tests.flows.flowlib import assert_step_sequence, flow_steps, status

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


# The full path, in the step vocabulary `flowlib.flow_steps` defines,
# spanning both engine processes (events.jsonl is one append-only file
# across the resume).
CRASH_RESUME_REFUND_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",          # attempt 1: hangs (infra fault)
    "infra_retry:worker:1",
    "infra_wait:worker:1",
    "iteration:worker",          # attempt 2: reaches the model, completes the task
    "infra_recovered",
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",          # e1: held here, then SIGKILLed
    "state:running",             # e2 resumes: state event fires unconditionally
    "phase:review@1",            # _crashed_mid_review lands straight back in review
    "iteration:review",          # this time it runs for real
    "verdict:review:verified@1",
    "signal:VERIFIED",
    "state:succeeded",
]


def test_crash_mid_review_then_resume_preserves_the_infra_refund(engine_factory, tmp_path):
    hold_file = tmp_path / "hold-review-boundary"
    start = time.monotonic()

    e1 = engine_factory(
        job={"run_id": "flow-crash-resume-refunds", "on_complete": "idle",
             "iterations": 15, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1", "STUB_SLEEP": "0",
            "STUB_INFRA_HANG_SKIP": "1",    # planning stays healthy
            "STUB_INFRA_HANG_COUNT": "1",   # the next (first worker) attempt hangs
            "RALPHD_INFRA_STARTUP_TIMEOUT": "0.2",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.05",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "60",
            "STUB_HOLD_FILE": str(hold_file), "STUB_HOLD_PHASE": "review",
            "STUB_HOLD_TIMEOUT_S": "60",
        })
    e1.wait_api()

    # The refund is booked to disk BEFORE the run can ever reach review --
    # `_refund_iteration` writes it in the same breath as the retry
    # decision, well before the second (successful) worker attempt even
    # starts.
    infra_before_crash = _wait_for(
        lambda: (status(e1).get("iterationsRefunded") or {}).get("infra"),
        timeout=30)
    assert infra_before_crash == 1, (
        f"flow crash/resume/refunds: the hung worker attempt should already "
        f"be refunded before review ever starts, got "
        f"iterationsRefunded={status(e1).get('iterationsRefunded')!r}")

    def review_started_not_finished():
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

    review_meta = _wait_for(review_started_not_finished, timeout=30)
    review_number = review_meta["number"]

    _kill(e1)

    crashed_meta = json.loads(
        (e1.run_dir / "iterations" / f"{review_number:04d}" / "meta.json").read_text())
    assert crashed_meta["phase"] == "review"
    assert "endedAt" not in crashed_meta, (
        "flow crash/resume/refunds: the review iteration must genuinely "
        "never finish -- the hold blocks pi before it touches any file")

    # Resume: a fresh engine process over the SAME run dir (same tmp_path;
    # engine_factory's registry-less LiveRun always uses tmp/run).
    e2 = engine_factory(
        job={"run_id": "flow-crash-resume-refunds", "on_complete": "exit",
             "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"})
    assert e2.run_dir == e1.run_dir, (
        "flow crash/resume/refunds: e2 must resume over e1's own run dir")
    assert e2.proc.wait(timeout=60) == 0, (
        "flow crash/resume/refunds: the resumed engine did not exit 0")

    assert_step_sequence(flow_steps(e2), CRASH_RESUME_REFUND_STEPS,
                         flow="flow crash/resume/refunds")

    st = status(e2)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow crash/resume/refunds: expected succeeded/verified, got "
        f"state={st.get('state')!r} verdict={st.get('verdict')!r} "
        f"(status.json: {st})")

    # The refund accounting that survived the crash: exactly the one infra
    # refund earned before the kill, never re-charged and never dropped by
    # the resume; grace/steer stay at zero (nothing in this path earns
    # them).
    refunded = st.get("iterationsRefunded") or {}
    assert refunded == {"infra": 1, "grace": 0, "steer": 0}, (
        f"flow crash/resume/refunds: the crash/resume boundary must "
        f"preserve exactly one infra refund and invent none of the other "
        f"kinds, got iterationsRefunded={refunded!r}")

    # Raw finished attempts: planning(1) + worker hang(1) + worker
    # success(1) + review(1, the one that finally finished after resume) =
    # 4; charged (published) iterationsUsed = 4 - infra_refunded(1) = 3 --
    # the one hung attempt costs the run nothing, exactly as if the outage
    # had never happened, and the crash/resume boundary does not change
    # that arithmetic.
    assert st.get("iterationsUsed") == 3, (
        f"flow crash/resume/refunds: planning + charged worker + review == "
        f"3 -- the refunded hang must stay uncounted across the resume, "
        f"got iterationsUsed={st.get('iterationsUsed')!r}")

    elapsed = time.monotonic() - start
    assert elapsed < 20, (
        f"flow crash/resume/refunds: serial wall clock should be under 20s, "
        f"took {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# 2. mid-worker: killed while a task is genuinely in-progress, nowhere near
#    an iteration boundary (task 021, requirement C.10; formerly
#    tests/test_crash_consistency.py::test_sigkill_mid_worker_iteration_then_resume_completes).
# ---------------------------------------------------------------------------


def test_sigkill_mid_worker_iteration_then_resume_completes(engine_factory, tmp_path):
    hold_file = tmp_path / "hold-mid-worker"
    e1 = engine_factory(
        job={"run_id": "flow-crash-mid-worker", "on_complete": "idle",
             "iterations": 15, "max_approaches": 1},
        # STUB_HOLD_PHASE restricts the hold to the worker phase, so
        # planning (which necessarily runs first) proceeds unheld and
        # writes tasks.json for real; only the worker invocation that
        # flips a task to in-progress ever blocks on this hold file, which
        # this test never releases -- the kill below lands inside that
        # block, deterministically, never a real-time race.
        stub_env={"STUB_TASKS": "3", "STUB_HOLD_FILE": str(hold_file),
                  "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "60"})
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

    def second_iteration_dir_exists():
        d = e1.run_dir / "iterations"
        if not d.exists():
            return None
        dirs = sorted(d.iterdir())
        return dirs if len(dirs) >= 2 else None

    # See test_sigkill_mid_worker_iteration's original note (task 006): the
    # in-progress flip and this test's own worker iteration dir can land a
    # poll cycle apart under a loaded machine, so wait for the second
    # iteration dir too, not just the tasks.json flip.
    _wait_for(second_iteration_dir_exists, timeout=15)

    _kill(e1)

    tasks_after_kill = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_after_kill) == 3
    statuses = [t["status"] for t in tasks_after_kill]
    assert statuses.count("in-progress") == 1
    assert statuses.count("completed") == 0

    iters_before = sorted((e1.run_dir / "iterations").iterdir())
    assert len(iters_before) >= 2  # at least planning + the killed worker
    crashed_meta = json.loads((iters_before[-1] / "meta.json").read_text())
    assert crashed_meta["phase"] == "worker"
    assert "endedAt" not in crashed_meta, (
        "flow crash/resume/refunds (mid-worker): the killed worker "
        "iteration must genuinely never finish")
    crashed_number = crashed_meta["number"]

    _assert_events_parseable(e1.run_dir)  # no torn NDJSON lines either

    e2 = engine_factory(
        job={"run_id": "flow-crash-mid-worker", "on_complete": "exit",
             "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "3", "STUB_SLEEP": "0"})
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0

    st = status(e2)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow crash/resume/refunds (mid-worker): expected succeeded/"
        f"verified, got state={st.get('state')!r} verdict={st.get('verdict')!r}")

    tasks_final = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_final) == 3
    assert all(t["status"] == "completed" for t in tasks_final)

    numbers = _assert_iterations_monotonic_no_dupes(e2.run_dir)
    assert crashed_number in numbers  # the crashed slot's number was reused
    reused_meta = json.loads(
        (e2.run_dir / "iterations" / f"{crashed_number:04d}" / "meta.json").read_text())
    assert "endedAt" in reused_meta

    events_after = _assert_events_parseable(e2.run_dir)
    assert any(ev.get("type") == "log" and "resuming existing run-dir state"
               in ev.get("message", "") for ev in events_after)


# ---------------------------------------------------------------------------
# 3. worker -> review boundary: the prior worker work is 100% durable, the
#    freshly-started review iteration is 0% done (task 021, requirement
#    C.10; formerly tests/test_crash_consistency.py::
#    test_sigkill_at_iteration_boundary_then_resume_completes).
# ---------------------------------------------------------------------------


def test_sigkill_at_iteration_boundary_then_resume_completes(engine_factory, tmp_path):
    hold_file = tmp_path / "hold-review-iteration-boundary"
    e1 = engine_factory(
        job={"run_id": "flow-crash-iteration-boundary", "on_complete": "idle",
             "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2", "STUB_HOLD_FILE": str(hold_file),
                  "STUB_HOLD_PHASE": "review", "STUB_HOLD_TIMEOUT_S": "60"})
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

    assert not (e1.run_dir / ".stub-review-count").exists()
    assert not (e1.run_dir / "review-findings.md").exists()
    crashed_meta = json.loads(
        (e1.run_dir / "iterations" / f"{review_number:04d}" / "meta.json").read_text())
    assert crashed_meta["phase"] == "review"
    assert "endedAt" not in crashed_meta

    worker_meta = json.loads(
        (e1.run_dir / "iterations" / f"{review_number - 1:04d}" / "meta.json").read_text())
    assert worker_meta["phase"] == "worker"
    assert "endedAt" in worker_meta
    assert worker_meta["appliedVerdict"] == "complete"
    assert worker_meta["appliedTaskId"] == "002"
    tasks_after_kill = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert sum(1 for t in tasks_after_kill if t["status"] == "completed") == 2

    _assert_events_parseable(e1.run_dir)

    e2 = engine_factory(
        job={"run_id": "flow-crash-iteration-boundary", "on_complete": "exit",
             "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2", "STUB_SLEEP": "0"})
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0

    st = status(e2)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow crash/resume/refunds (iteration boundary): expected "
        f"succeeded/verified, got state={st.get('state')!r} "
        f"verdict={st.get('verdict')!r}")

    tasks_final = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_final) == 2
    assert all(t["status"] == "completed" for t in tasks_final)

    numbers = _assert_iterations_monotonic_no_dupes(e2.run_dir)
    assert review_number in numbers
    reused_meta = json.loads(
        (e2.run_dir / "iterations" / f"{review_number:04d}" / "meta.json").read_text())
    assert "endedAt" in reused_meta

    events_after = _assert_events_parseable(e2.run_dir)
    assert any(ev.get("type") == "log" and "resuming existing run-dir state"
               in ev.get("message", "") for ev in events_after)


# ---------------------------------------------------------------------------
# 4. worker -> verify boundary under vigilant mode: a task already
#    relabelled to the mini-FSM claim status survives the crash, and the
#    resumed engine still runs a real, passing verify for it rather than
#    silently skipping it (task 021, requirement C.10; formerly
#    tests/test_vigilant_crash_resume.py::
#    test_sigkill_after_task_completed_before_verify_finishes_then_resume_verifies).
#    The trigger this proves is disk-persisted (the task record's own
#    `status`), never an in-process before/after diff a crash could blind.
# ---------------------------------------------------------------------------


def _events(run_dir):
    text = (run_dir / "events.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_sigkill_after_task_completed_before_verify_finishes_then_resume_verifies(
        engine_factory, tmp_path):
    hold_file = tmp_path / "hold-verify-boundary"
    e1 = engine_factory(
        job={"run_id": "flow-crash-verify-boundary", "on_complete": "idle",
             "vigilant": True, "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                  "STUB_HOLD_PHASE": "verify", "STUB_HOLD_TIMEOUT_S": "60"})
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
    # started is already relabelled to the claim state
    # `awaiting-validation` -- the harness does that the instant the
    # worker's own subprocess exits, strictly before the verify iteration
    # this test kills mid-flight ever starts.
    tasks_mid = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_mid) == 1
    assert tasks_mid[0]["status"] == "awaiting-validation"

    _kill(e1)

    crashed_meta = json.loads(
        (e1.run_dir / "iterations" / f"{verify_number:04d}" / "meta.json").read_text())
    assert crashed_meta["phase"] == "verify"
    assert "endedAt" not in crashed_meta
    assert "verifyOutcome" not in crashed_meta
    assert not (e1.run_dir / ".stub-verify-call-count").exists()

    events_before = _events(e1.run_dir)
    assert not any(ev.get("type") == "signal" and ev.get("signal") == "taskVerified"
                   for ev in events_before)
    tasks_mid_after_kill = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks_mid_after_kill[0]["status"] == "awaiting-validation"

    e2 = engine_factory(
        job={"run_id": "flow-crash-verify-boundary", "on_complete": "exit",
             "vigilant": True, "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"})
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0

    st = status(e2)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow crash/resume/refunds (verify boundary): expected succeeded/"
        f"verified, got state={st.get('state')!r} verdict={st.get('verdict')!r}")

    events_after = _events(e2.run_dir)
    tv_events = [ev for ev in events_after
                 if ev.get("type") == "signal" and ev.get("signal") == "taskVerified"
                 and ev.get("taskId") == "001"]
    assert len(tv_events) == 1, (
        "flow crash/resume/refunds (verify boundary): the task must get "
        "exactly one real taskVerified signal after resume, never silently "
        "skipped across the crash")

    iters = sorted((e2.run_dir / "iterations").iterdir())
    verify_metas = [json.loads((d / "meta.json").read_text()) for d in iters
                    if json.loads((d / "meta.json").read_text()).get("phase") == "verify"]
    passing = [m for m in verify_metas
              if m.get("verifiedTask") == "001" and m.get("verifyOutcome") == "pass"]
    assert len(passing) == 1

    assert not (e2.run_dir / "vigilant-verified.json").exists()
    tasks_final = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks_final[0]["status"] == "validated"
