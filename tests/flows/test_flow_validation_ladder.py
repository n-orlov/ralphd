"""Flow 2 of the twelve (task 005/012, requirement G.5, C.1): **validation
rejection -> retry -> three strikes -> review, and a criteria-tamper
refusal**, over a real `ralphd-engine` subprocess with the stub `pi` binary
on PATH, `STUB_SLEEP=0`.

Vigilant mode (`vigilant: true`) inserts one per-task `verify` iteration
after every worker iteration; a rejecting verdict marks the task
`validation-failed` and the SAME task is retried by another worker
iteration. `state.VALIDATION_ATTEMPT_LIMIT` is 3, so this one primitive --
"how many times has this task's verdict come back negative" -- produces
three different, worth-distinguishing outcomes depending on how the worker
and the verifier behave, and this module is the one path each of them walks:

  * **rejection -> retry -> verified** -- one rejection, then a passing
    retry, is today's ordinary case: the task ends `validated`, the run
    ends `succeeded`/`verified`, and `validationAttempts` records the one
    spent attempt (`test_a_validation_rejection_is_retried_and_then_verifies`).
  * **three strikes -> review** -- a verifier that never relents exhausts
    `VALIDATION_ATTEMPT_LIMIT`: the task is marked `failed` with
    `failureKind: "validation-exhausted"` (task 024, #33) rather than
    leaving the run stuck forever with nobody having looked at it -- once
    nothing is actionable, the engine itself routes straight to `review`,
    which still gets the final word
    (`test_three_strikes_exhausts_validation_and_routes_to_review`). The
    contract here (a bare "Do the thing." PRD) declares no petition
    protocol, so this is the plain `failed` route, not `petitioned`.
  * **criteria-tamper is inert (task 004, requirement B)** -- a worker
    that, after one rejection, opens `tasks.json` directly and rewrites
    `successCriteria` itself instead of fixing the work
    (`STUB_REWRITE_CRITERIA`, task 008/024) touches nothing but the
    agent-readable VIEW copy: the authoritative record it is aimed at
    lives in the protected copy `RunDir.write_tasks` maintains, which
    the raw `open(tasks_file, "w")` this stub performs was never able to
    reach even before task 004 existed as a name for it -- only ever a
    courtesy mirror. The run proceeds exactly as the plain retry case
    above does, oblivious, and the archived record shows the ORIGINAL
    criteria throughout, never the rewrite
    (`test_a_criteria_tamper_via_the_agent_readable_view_never_reaches_the_record`).
    task 008/024's own detect-and-refuse machinery
    (`LoopSupervisor._check_criteria_edits`) is still very much alive --
    `tests/test_criteria_fingerprint.py`/`test_contract_events.py` pin it
    against a rewrite landed the one way that still reaches the record,
    through `write_tasks()` itself (a future authorized-channel bug, not
    a dropped agent) -- this flow just no longer has a way to DEMONSTRATE
    that path, because the raw-file-write vector it used to use no longer
    lands anywhere that matters.

All three are asserted the same two ways: the exact `events.jsonl` step
sequence (`flowlib.assert_step_sequence`, which names the diverging index and
step on any mismatch) and the observable record on disk -- `tasks.json`'s own
`validationAttempts`/`status`/`failureKind`/`successCriteria`, and the
specific tamper-refusal events that never make it into the step vocabulary
(`task.criteria_edit_refused`, `review.criteria_tamper_finding`) because they
are a task-level correction, not a phase/iteration/verdict step of the run's
own path.
"""

from __future__ import annotations

import json

from tests.flows.flowlib import (assert_step_sequence, events_of, flow_steps,
                                 status, tasks, wait_engine_exit)


def test_a_validation_rejection_is_retried_and_then_verifies(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-validation-retry", "on_complete": "exit",
             "vigilant": True, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_VERIFY_FAILS": "1"})
    assert wait_engine_exit(run, timeout=60) == 0, (
        "flow validation ladder (retry): the engine did not exit 0 after "
        "the retried task verified")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",
        "iteration:verify",           # rejected -- validation-failed
        "iteration:worker",           # the SAME task, retried
        "iteration:verify",           # verified this time
        "signal:taskVerified",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow validation ladder (retry)")

    st = status(run)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow validation ladder (retry): expected succeeded/verified, got "
        f"state={st.get('state')!r} verdict={st.get('verdict')!r}")

    ts = tasks(run)
    assert len(ts) == 1, f"flow validation ladder (retry): expected 1 task, got {ts}"
    t = ts[0]
    assert t["status"] == "validated", (
        f"flow validation ladder (retry): the retried task must end "
        f"'validated', got {t['status']!r}")
    assert t.get("validationAttempts") == 1, (
        f"flow validation ladder (retry): exactly one rejection must be "
        f"recorded, got validationAttempts={t.get('validationAttempts')!r}")
    assert t.get("validationNotes"), (
        "flow validation ladder (retry): the rejecting attempt's reason "
        "must survive on the task record as validationNotes")


def test_three_strikes_exhausts_validation_and_routes_to_review(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-validation-three-strikes", "on_complete": "exit",
             "vigilant": True, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_VERIFY_FAILS": "999"})
    assert wait_engine_exit(run, timeout=60) == 0, (
        "flow validation ladder (three strikes): the engine did not exit 0 "
        "once nothing was left actionable and review took over")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",
        "iteration:verify",                  # strike 1
        "iteration:worker",
        "iteration:verify",                  # strike 2
        "signal:tierLadderExhausted",
        "iteration:worker",
        "iteration:verify",                  # strike 3 -- attempts exhausted
        "signal:tierLadderExhausted",
        "iteration:worker",                  # nothing else to assign; routes to review
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow validation ladder (three strikes)")

    st = status(run)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow validation ladder (three strikes): review still gets the "
        f"final word over the exhausted task, got state={st.get('state')!r} "
        f"verdict={st.get('verdict')!r}")

    ts = tasks(run)
    assert len(ts) == 1, ts
    t = ts[0]
    assert t["status"] == "failed", (
        f"flow validation ladder (three strikes): the exhausted task must "
        f"be labelled 'failed', got {t['status']!r}")
    assert t.get("validationAttempts") == 3, (
        f"flow validation ladder (three strikes): all three attempts must "
        f"be spent, got {t.get('validationAttempts')!r}")
    assert t.get("failureKind") == "validation-exhausted", (
        f"flow validation ladder (three strikes): the failure must be "
        f"labelled 'validation-exhausted' (task 024, #33), not merely "
        f"'failed' with no kind, got {t.get('failureKind')!r}")

    # No taskVerified signal was ever emitted for the exhausted task, and the
    # route into review was the engine's own deliberate one, never a worker
    # claiming COMPLETE.
    assert not events_of(run, "signal") or all(
        ev.get("signal") != "taskVerified" for ev in events_of(run, "signal")
        if ev.get("taskId") == t["id"]), (
        "flow validation ladder (three strikes): the exhausted task must "
        "never have been reported taskVerified")
    logs = "\n".join(ev.get("message") or "" for ev in events_of(run, "log"))
    assert "routing to review" in logs and "(validation-exhausted)" in logs, (
        f"flow validation ladder (three strikes): the engine must name the "
        f"deliberate route-to-review decision in its own log, got:\n{logs}")


def test_a_criteria_tamper_via_the_agent_readable_view_never_reaches_the_record(engine_factory):
    """Task 004 (requirement B, #202): `STUB_REWRITE_CRITERIA` opens
    `tasks.json` (the agent-readable VIEW copy, `run.tasks_file`) directly
    and rewrites `successCriteria` in place -- exactly the shape a fully
    dropped agent uid can still perform against that copy, since it is a
    courtesy mirror, never trusted. `RunDir.read_tasks`/`read_tasks_result`
    read only the protected copy, so this lands nowhere the engine ever
    looks: the run proceeds exactly like the plain retry case above,
    oblivious, and the persisted record -- live and archived alike --
    carries the ORIGINAL criteria throughout. This used to be titled
    `..._is_refused_and_the_run_never_verifies`, pinning task 008/024's
    detect-and-refuse marker over a rewrite that reached the authoritative
    record via this exact vector; task 004 closes that vector outright, so
    there is nothing left here for that marker to catch -- its own
    coverage moved to `tests/test_criteria_fingerprint.py`/
    `test_contract_events.py`, which drive the same rewrite through
    `write_tasks()` itself, the one channel still capable of reaching the
    record at all.
    """
    run = engine_factory(
        job={"run_id": "flow-validation-tamper", "on_complete": "exit",
             "vigilant": True, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2", "STUB_VERIFY_FAILS": "1",
                  "STUB_REWRITE_CRITERIA": "1"})
    assert wait_engine_exit(run, timeout=60) == 0, (
        "flow validation ladder (tamper): a rewrite that never reaches the "
        "protected record must not stop the run from verifying normally")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",
        "iteration:verify",                  # task 1: rejected once
        "iteration:worker",                  # task 1: retried, this is where
                                              # the stub's raw open()/write()
                                              # against the VIEW copy happens
        "iteration:verify",                  # task 1: verified for real
        "signal:taskVerified",
        "iteration:worker",                  # task 2
        "iteration:verify",
        "signal:taskVerified",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow validation ladder (tamper)")

    st = status(run)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow validation ladder (tamper): a rewrite that never reaches the "
        f"protected record leaves nothing for review to object to, got "
        f"state={st.get('state')!r} verdict={st.get('verdict')!r}")

    # No refusal, no forced finding -- there was never anything on the
    # AUTHORITATIVE record to refuse in the first place.
    assert events_of(run, "task.criteria_edit_refused") == [], (
        "flow validation ladder (tamper): a rewrite confined to the view "
        "copy gives _check_criteria_edits nothing to catch")
    assert events_of(run, "review.criteria_tamper_finding") == [], (
        "flow validation ladder (tamper): no engine-forced finding is owed "
        "when the record itself was never touched")

    # The live record's own criteria are the ORIGINAL text throughout --
    # never the stub's "(rewritten by worker)" suffix.
    ts = tasks(run)
    assert len(ts) == 2, f"flow validation ladder (tamper): expected 2 tasks, got {ts}"
    for t in ts:
        assert not t["successCriteria"].endswith("(rewritten by worker)"), (
            f"flow validation ladder (tamper): the authoritative record's "
            f"successCriteria must never carry the stub's rewrite, got {t!r}")
        assert t.get("criteriaEditedAfterValidationFailure") is not True, (
            f"flow validation ladder (tamper): nothing was ever flagged, "
            f"the rewrite never reached the record, got {t!r}")

    # The stub's raw write DID land on the agent-readable VIEW copy at some
    # point during the run -- proving the vector was genuinely exercised,
    # not merely skipped -- but the engine's own next `write_tasks()` call
    # (task 1's verify pass recording its verdict) refreshes that view
    # copy from the protected record, so by the time the run has finished
    # the view carries the same, untampered text the live record does.
    view_tasks = json.loads(run.run_dir.joinpath("tasks.json").read_text())["tasks"]
    for t in view_tasks:
        assert not t["successCriteria"].endswith("(rewritten by worker)"), (
            f"flow validation ladder (tamper): the view copy must be back "
            f"in sync with the protected record by the time the run ends, "
            f"got {t!r}")
