"""Flow 3 of the twelve (task 005, requirement G.5): **review rejection ->
replan -> exhaustion -> last-approach pause -> resume**, over a real
`ralphd-engine` subprocess with the stub `pi` binary on PATH.

The three endings a rejected review can have are one flow, because they are
the same edge (`_step_review`'s rejection branch) resolving against a
different amount of remaining budget -- and telling them apart is the whole
point of the module:

  * **an approach is left** -> the approach is ARCHIVED under `approaches/`,
    a composite PRD is written, the loop replans and the next approach's own
    review verifies (`test_a_rejection_with_an_approach_left_replans...`);
  * **no approach is left, and the rejection is not curable** -> the run ends
    `failed`/`unverified` without ever pausing: nothing an operator could
    release would change the verdict
    (`test_a_rejection_on_the_last_approach_with_nothing_curable_fails`);
  * **no approach is left, the rejection IS curable, and this approach's one
    cure pass is already spent** -> the run does NOT fail: it pauses for the
    operator (`pauseReason: "rejected_last_approach"`, an
    `operator_escalation` event), leaves the approach UNARCHIVED, and a
    `POST /resume` re-enters review over that same approach, where the next
    review verifies (`test_the_last_approach_pauses_for_the_operator...`).

That third case is the one the criteria name "the last-approach pause with a
resume", and it is asserted end to end: the pause is observed off the run's
own `status.json` (a host's view, not the engine's memory), the release goes
through the engine's real HTTP API, and the resumed run is followed all the
way to `succeeded`.

Note on the step vocabulary: a pause is NOT a `state` event -- it is recorded
on `status.json` (`state: "paused"`, `pauseReason`) plus the
`operator_escalation` event, so `escalation:rejected_last_approach@1` is the
step the sequence carries and the persisted pair is asserted separately.
"""

from __future__ import annotations

from tests.flows.flowlib import (assert_step_sequence, events_of, flow_steps,
                                 status, tasks)

_PLAN_AND_WORK = [
    "phase:planning@{a}",
    "iteration:planning",
    "iteration:worker",
    "iteration:worker",
    "signal:COMPLETE",
]


def _approach_steps(approach: int) -> list[str]:
    return [s.format(a=approach) for s in _PLAN_AND_WORK]


def test_a_rejection_with_an_approach_left_replans_and_the_next_approach_verifies(
        engine_factory):
    run = engine_factory(
        job={"run_id": "flow-replan", "on_complete": "exit",
             "max_approaches": 2, "iterations": 30},
        stub_env={"STUB_REVIEW_FAILS": "1"})
    assert run.proc.wait(timeout=60) == 0, (
        "flow replan: the engine did not exit 0 after the second approach verified")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        *_approach_steps(1),
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",
        *_approach_steps(2),
        "phase:review@2",
        "iteration:review",
        "verdict:review:verified@2",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow replan")

    st = status(run)
    assert (st.get("state"), st.get("approach")) == ("succeeded", 2), (
        f"flow replan: the run must succeed on approach 2, got state="
        f"{st.get('state')!r} approach={st.get('approach')!r}")

    # The rejected approach is archived -- the disk-persisted difference
    # between a replan and both other endings of this same edge.
    archived = sorted(p.name for p in (run.run_dir / "approaches").rglob("*"))
    assert "tasks.json" in archived and "01" in archived, (
        f"flow replan: approach 1 must be archived under approaches/01/, found "
        f"{archived}")
    assert not events_of(run, "operator_escalation"), (
        "flow replan: a rejection with an approach left must never escalate to "
        "the operator")


def test_a_rejection_on_the_last_approach_with_nothing_curable_fails(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-exhausted", "on_complete": "exit",
             "max_approaches": 1, "iterations": 30},
        stub_env={"STUB_REVIEW_FAILS": "1"})
    assert run.proc.wait(timeout=60) == 1, (
        "flow exhaustion: an unverified run must exit nonzero")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        *_approach_steps(1),
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",
        "state:failed",
    ], flow="flow exhaustion")

    st = status(run)
    assert (st.get("state"), st.get("verdict")) == ("failed", "unverified"), (
        f"flow exhaustion: the run must end failed/unverified, got state="
        f"{st.get('state')!r} verdict={st.get('verdict')!r}")
    assert st.get("pauseReason") is None, (
        f"flow exhaustion: an incurable last-approach rejection must never "
        f"pause, but pauseReason is {st.get('pauseReason')!r}")


def test_the_last_approach_pauses_for_the_operator_and_a_resume_verifies(
        engine_factory):
    # Two curable rejections on a single-approach run: the first spends this
    # approach's one cure pass, the second has nothing left to spend and
    # nowhere to replan to -- the `rejected_last_approach` pause.
    run = engine_factory(
        job={"run_id": "flow-last-approach-pause", "on_complete": "exit",
             "max_approaches": 1, "iterations": 30},
        stub_env={"STUB_REVIEW_CURABLE_REJECT_COUNT": "2"})

    paused = run.wait_state(("paused",), timeout=60)
    assert paused.get("pauseReason") == "rejected_last_approach", (
        f"flow last-approach pause: status.json must name the pause reason "
        f"'rejected_last_approach', got {paused.get('pauseReason')!r}")
    assert paused.get("approach") == 1, (
        f"flow last-approach pause: the paused run must still be on approach 1, "
        f"got {paused.get('approach')!r}")
    assert not any((run.run_dir / "approaches").rglob("*")), (
        "flow last-approach pause: the approach must be left UNARCHIVED so a "
        "resume re-enters review over the same approach")

    escalations = events_of(run, "operator_escalation")
    assert len(escalations) == 1 and escalations[0].get("reason") == "rejected_last_approach", (
        f"flow last-approach pause: exactly one operator_escalation with reason "
        f"'rejected_last_approach' is expected, got "
        f"{[(e.get('reason')) for e in escalations]}")

    # The operator releases the pause through the engine's own API.
    run.wait_api()
    code, body = run.api("POST", "/resume")
    assert (code, body) == (200, {"resumed": True}), (
        f"flow last-approach pause: POST /resume should release the pause, got "
        f"HTTP {code} / {body}")

    assert run.proc.wait(timeout=60) == 0, (
        "flow last-approach pause: the resumed run did not exit 0 after review "
        "verified")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        *_approach_steps(1),
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",
        # the cure pass for this approach: the appended cure task is worked,
        # then the same approach is reviewed again
        "cure_pass:1@1",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",
        "escalation:rejected_last_approach@1",
        # the resume lands back on review over the SAME approach
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow last-approach pause")

    st = status(run)
    assert st.get("state") == "succeeded", (
        f"flow last-approach pause: the resumed run must reach 'succeeded', got "
        f"{st.get('state')!r}")
    assert st.get("pauseReason") is None, (
        f"flow last-approach pause: the resume must clear pauseReason, got "
        f"{st.get('pauseReason')!r}")
    assert st.get("approach") == 1, (
        f"flow last-approach pause: the run must never leave approach 1, got "
        f"{st.get('approach')!r}")
    assert st.get("pausedTotalS", 0) >= 0, "flow last-approach pause: no pausedTotalS"
    assert all(t["status"] in ("completed", "validated") for t in tasks(run)), (
        f"flow last-approach pause: every task (the cure task included) must end "
        f"completed/validated, got {[(t['id'], t['status']) for t in tasks(run)]}")
