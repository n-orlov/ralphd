"""Flow 1 of the twelve (task 005, requirement G.5): **the happy path and its
exit modes**, over a real `ralphd-engine` subprocess with the stub `pi` binary
on PATH.

One plan, two tasks, one worker iteration each, one review pass that verifies,
then the run ends. Nothing rejects, nothing retries, nothing pauses -- so this
flow is the reference sequence every other flow in this directory diverges
from, and it is asserted as an EXACT step sequence (`assert_step_sequence`),
not a subsequence: on the shortest path there is no legitimate extra step, and
a new step appearing here silently is exactly what the merged flows are
supposed to catch.

Both `on_complete` exit modes the engine offers are covered, because they are
the two different *endings* the same path has:

  * `exit`  -- the engine process leaves once the run succeeds (the container
               shape: the job is over, the run dir is the record);
  * `idle`  -- the run reaches `succeeded` and the engine STAYS UP serving its
               API, so a host can still read the finished run over HTTP.

Everything is read back off the run's own record -- `events.jsonl`,
`status.json`, `tasks.json` -- never from the stub's own inputs.
"""

from __future__ import annotations

from tests.flows.flowlib import (assert_step_sequence, events_of, flow_steps,
                                 status, tasks, wait_engine_exit,
                                 wait_status_terminal, wait_terminal_recorded)

# The reference path, in the step vocabulary `flowlib.flow_steps` defines.
# Two tasks (the stub planner's default), one worker iteration each, one
# review pass.
HAPPY_PATH_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",
    "iteration:worker",
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",
    "verdict:review:verified@1",
    "signal:VERIFIED",
    "state:succeeded",
]


def test_the_happy_path_walks_plan_two_workers_review_and_exits(engine_factory):
    run = engine_factory(job={"run_id": "flow-happy-exit", "on_complete": "exit"})
    assert wait_engine_exit(run, timeout=60) == 0, (
        "flow happy path: the engine did not exit 0 under `on_complete: exit`")

    # The barrier (task 006, #169): read the terminal state through
    # `events.jsonl` itself rather than through `status.json` -- the engine
    # is guaranteed (SPEC.md's events table) to have appended this event
    # before status.json's own terminal write, and by the time the process
    # has exited both are long since on disk, so this returns immediately.
    wait_terminal_recorded(run)

    assert_step_sequence(flow_steps(run), HAPPY_PATH_STEPS,
                         flow="flow happy path")

    st = status(run)
    assert st.get("state") == "succeeded", (
        f"flow happy path: final state should be 'succeeded', got {st.get('state')!r}"
        f" (status.json: {st})")
    assert st.get("verdict") in ("verified", "verified-with-residuals"), (
        f"flow happy path: final verdict should be a verified label, got "
        f"{st.get('verdict')!r}")
    assert st.get("approach") == 1, (
        f"flow happy path: nothing may replan, so the run must end on approach 1, "
        f"got {st.get('approach')!r}")

    # The whole task record reached a completed status, and no task was ever
    # rejected, petitioned or failed on this path.
    ts = tasks(run)
    assert ts, "flow happy path: the planner recorded no tasks at all"
    assert all(t["status"] in ("completed", "validated") for t in ts), (
        f"flow happy path: every task must end completed/validated, got "
        f"{[(t['id'], t['status']) for t in ts]}")
    assert all(t.get("validationAttempts", 0) == 0 for t in ts), (
        f"flow happy path: no task may consume a validation attempt, got "
        f"{[(t['id'], t.get('validationAttempts')) for t in ts]}")

    # The path's own negative half: no rejection, no escalation, no cure pass.
    for absent in ("operator_escalation", "cure_pass", "contract_violation"):
        assert not events_of(run, absent), (
            f"flow happy path: step {absent!r} must never happen on the happy "
            f"path, but events.jsonl carries {len(events_of(run, absent))}")

    # The approaches archive stays empty: an archived approach means a replan.
    archive = run.run_dir / "approaches"
    assert not (archive.exists() and any(archive.rglob("*"))), (
        "flow happy path: no approach may be archived when nothing replans")


def test_the_same_path_under_on_complete_idle_keeps_the_engine_serving(engine_factory):
    run = engine_factory(job={"run_id": "flow-happy-idle", "on_complete": "idle"})
    # Task 006 (#169): read the terminal state through the barrier, not
    # through `LiveRun.wait_terminal()`'s status.json poll -- the barrier
    # blocks on `events.jsonl`'s own terminal `state` event, which the
    # engine now guarantees lands no later than the status.json write this
    # replaces polling for.
    wait_terminal_recorded(run)
    # Task 002 (M): under `on_complete: idle` the engine process never
    # exits, so (unlike the `exit` test above, which only reads status()
    # after `proc.wait()` has already returned) nothing here guarantees the
    # SECOND write -- status.json's own terminal write, which SPEC.md's
    # ordering invariant only promises comes strictly AFTER the event the
    # barrier above just observed -- has landed yet. A bare `status(run)`
    # here is exactly the nightly race (run 34921161300: status.json read
    # `running` immediately after the events barrier returned); the second
    # barrier below blocks on status.json itself instead.
    st = wait_status_terminal(run)
    assert st.get("state") == "succeeded", (
        f"flow happy path (idle): the run must reach 'succeeded', got "
        f"{st.get('state')!r} (status.json: {st})")

    # Same steps as the `exit` mode: the ending differs, the path does not.
    assert_step_sequence(flow_steps(run), HAPPY_PATH_STEPS,
                         flow="flow happy path (idle)")

    assert run.proc.poll() is None, (
        "flow happy path (idle): the engine exited instead of idling after the "
        "run succeeded")
    run.wait_api()
    code, body = run.api("GET", "/status")
    assert code == 200 and body.get("state") == "succeeded", (
        f"flow happy path (idle): the idling engine's own API should still "
        f"serve the finished run, got HTTP {code} / {body}")
