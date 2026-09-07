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
  * **criteria-tamper refusal** -- a worker that, after one rejection,
    rewrites `successCriteria` instead of fixing the work
    (`STUB_REWRITE_CRITERIA`, task 008/024) never gets away with it: the
    engine restores the criteria-of-record, flags the task, and forces a
    RECORDED blocking finding that contradicts review's own `verified`
    claim over that task -- so the run can never end `verified` while the
    tamper stands (`test_a_criteria_tamper_is_refused_and_the_run_never_verifies`).

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
                                 status, tasks)


def test_a_validation_rejection_is_retried_and_then_verifies(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-validation-retry", "on_complete": "exit",
             "vigilant": True, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_VERIFY_FAILS": "1"})
    assert run.proc.wait(timeout=60) == 0, (
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
    assert run.proc.wait(timeout=60) == 0, (
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


def test_a_criteria_tamper_is_refused_and_the_run_never_verifies(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-validation-tamper", "on_complete": "exit",
             "vigilant": True},
        stub_env={"STUB_TASKS": "2", "STUB_VERIFY_FAILS": "1",
                  "STUB_REWRITE_CRITERIA": "1"})
    code = run.proc.wait(timeout=60)
    assert code != 0, (
        "flow validation ladder (tamper): a run that never sheds the "
        "tamper-contradicted 'verified' claim must not exit 0")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",
        "iteration:verify",                  # task 1: rejected once
        "iteration:worker",                  # task 1: retried, this is where
                                              # the worker rewrites the criteria
        "iteration:verify",                  # task 1: "verified" over the tamper
        "signal:taskVerified",
        "iteration:worker",                  # task 2
        "iteration:verify",
        "signal:taskVerified",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",         # engine-forced blocking finding
        "phase:planning@2",
        "iteration:planning",
        "iteration:worker",
        "iteration:verify",
        "signal:taskVerified",
        "iteration:worker",
        "signal:COMPLETE",
        "state:failed",
    ], flow="flow validation ladder (tamper)")

    st = status(run)
    assert st.get("verdict") != "verified", (
        f"flow validation ladder (tamper): no verdict may ever read "
        f"'verified' while a tamper-contradicted claim stands, got "
        f"{st.get('verdict')!r}")

    # The refusal itself, on the record: the rewrite did not stand, and the
    # restored text is the ORIGINAL criteria, not the worker's rewrite.
    refused = events_of(run, "task.criteria_edit_refused")
    assert len(refused) == 1, (
        f"flow validation ladder (tamper): expected exactly one refused "
        f"rewrite, got {len(refused)}: {refused}")
    assert refused[0]["refusedCriteria"].endswith("(rewritten by worker)"), (
        f"flow validation ladder (tamper): the refused text must be the "
        f"worker's own rewrite, got {refused[0]!r}")
    assert not refused[0]["restoredCriteria"].endswith("(rewritten by worker)"), (
        f"flow validation ladder (tamper): the restored text must be the "
        f"criteria-of-record, not the rewrite, got {refused[0]!r}")

    # The tampered task's `successCriteria` is restored on approach 1's OWN
    # record, which is what the run archives once it rejects and replans --
    # approach 2 plans a fresh `tasks.json` from "001" again (task 020), so
    # the persisted marker is read off the archive, not the live file.
    tampered_id = refused[0]["taskId"]
    archived_tasks = json.loads(
        (run.run_dir / "approaches" / "01" / "tasks.json").read_text())["tasks"]
    tampered = next(t for t in archived_tasks if t["id"] == tampered_id)
    assert tampered["successCriteria"] == refused[0]["restoredCriteria"], (
        f"flow validation ladder (tamper): the archived record must carry "
        f"the restored (not the rewritten) criteria, got {tampered!r}")
    assert tampered.get("criteriaEditedAfterValidationFailure") is True, (
        f"flow validation ladder (tamper): the tamper marker must persist "
        f"on the archived task record, got {tampered!r}")

    # The RECORDED blocking finding that contradicts review's own 'verified'
    # claim over the tampered task.
    tamper_findings = events_of(run, "review.criteria_tamper_finding")
    assert tamper_findings and tamper_findings[0].get("severity") == "blocking", (
        f"flow validation ladder (tamper): the engine-forced finding must "
        f"be recorded and blocking, got {tamper_findings}")
