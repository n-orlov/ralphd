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
