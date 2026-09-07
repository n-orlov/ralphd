"""Flow 6 of the twelve (task 013, requirement C.2): **the two opt-in peer
gates** -- `plan_review` (task 018) between `planning` and `worker`, and
`verdict_gate` (task 020) between `review`'s own `verified` edge and
`finishing` -- over a real `ralphd-engine` subprocess with the stub `pi`
binary on PATH, `STUB_SLEEP=0`.

The run-level black-box coverage for these two gates already existed as
four separate modules (`tests/test_e2e_peer_gates_task029.py`,
`tests/test_e2e_verdict_gate_second_approach_task031.py`,
`tests/test_e2e_composed_gates_task032.py`,
`tests/test_ladder_exhaustion_e2e_task033.py`'s `verdict_gate` case) --
this module is their `tests/flows/` counterpart: the same `engine_factory`
tier, but asserted through `flowlib`'s exact `events.jsonl` step sequence
(so a failure names the diverging step) plus the observable on-disk record,
rather than only `status.json`/`meta.json` phases. C.9/C.10 (the fold) are
the tasks that retire the older modules once every flow they cover exists;
this task only adds the flow.

Three scenarios, the three shapes requirement C.2 names by name:

  * **objection** -- `plan_review`'s first invocation objects (spending one
    real revision, persisted on disk as `planReviewRevisions[str(approach)]`)
    and routes back to a genuine second `planning` iteration; the SECOND
    `plan_review` invocation approves and the plan proceeds to `worker`
    untouched (`test_a_plan_review_objection_spends_a_revision_then_approves`).
  * **reject** -- `verdict_gate` rejects once with an approach still left:
    the rejection folds into a fresh approach (`_archive_approach`,
    `_write_composite_prd`, the same machinery `review`'s own rejection
    uses) and that fresh approach's own `verdict_gate` invocation approves
    for real, reaching the run's real success edge
    (`test_a_verdict_gate_rejection_with_an_approach_left_starts_a_fresh_approach`).
  * **malformed** -- an unparseable `verdict_gate` reply is never a
    run-ending contract violation: it exhausts the uncharged re-ask ladder
    and defaults to the same safe choice an unparseable `review` reply
    already defaults to (rejected, never verified) -- and, with no approach
    left this time, the run fails open to `finishing`/`failed`, never a
    hang or a crash
    (`test_a_malformed_verdict_gate_reply_defaults_to_rejected_and_fails_open`).

Every scenario uses `STUB_TASKS=1` to keep the worker/verify portion of the
sequence to a single iteration -- the two gates are what this module is
about, not the plan's own size.
"""

from __future__ import annotations

from tests.flows.flowlib import assert_step_sequence, flow_steps, status, tasks


def test_a_plan_review_objection_spends_a_revision_then_approves(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-plan-review-objection", "on_complete": "exit",
             "peer_plan_review": True, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_PLAN_REVIEW_OBJECTIONS": "1"})
    assert run.proc.wait(timeout=60) == 0, (
        "flow peer gates (plan review objection): the engine did not exit 0 "
        "after the second plan_review invocation approved")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "phase:plan_review@1",
        "iteration:plan_review",           # objects -- spends a revision
        "phase:planning@1",                # a genuine second planning pass
        "iteration:planning",
        "phase:plan_review@1",
        "iteration:plan_review",           # approves
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow peer gates (plan review objection)")

    st = status(run)
    assert (st.get("state"), st.get("verdict")) == ("succeeded", "verified"), (
        f"flow peer gates (plan review objection): expected succeeded/"
        f"verified, got state={st.get('state')!r} verdict={st.get('verdict')!r}")
    assert st.get("planReviewRevisions") == {"1": 1}, (
        f"flow peer gates (plan review objection): the objection must have "
        f"spent exactly one revision against approach 1, got "
        f"{st.get('planReviewRevisions')!r}")
    ts = tasks(run)
    assert ts and all(t["status"] == "completed" for t in ts), ts


def test_a_verdict_gate_rejection_with_an_approach_left_starts_a_fresh_approach(
        engine_factory):
    run = engine_factory(
        job={"run_id": "flow-verdict-gate-reject", "on_complete": "exit",
             "peer_verdict_gate": True, "max_approaches": 2},
        stub_env={"STUB_TASKS": "1", "STUB_VERDICT_GATE_REJECT_COUNT": "1"})
    assert run.proc.wait(timeout=60) == 0, (
        "flow peer gates (verdict gate reject): the engine did not exit 0 "
        "after the second approach's own verdict_gate invocation approved")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "phase:verdict_gate@1",
        "iteration:verdict_gate",          # rejects -- an approach is left
        "verdict:verdictGate:rejected@1",
        "phase:planning@2",
        "iteration:planning",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@2",
        "iteration:review",
        "verdict:review:verified@2",
        "phase:verdict_gate@2",
        "iteration:verdict_gate",          # approves -- the run's real success edge
        "verdict:verdictGate:verified@2",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow peer gates (verdict gate reject)")

    st = status(run)
    assert (st.get("state"), st.get("verdict"), st.get("approach")) == (
        "succeeded", "verified", 2), (
        f"flow peer gates (verdict gate reject): expected succeeded/verified "
        f"on approach 2, got state={st.get('state')!r} verdict="
        f"{st.get('verdict')!r} approach={st.get('approach')!r}")

    # the rejection's own record: approach 1 is archived (with the peer's
    # own, engine-attributed findings channel), approach 2 -- the run's real
    # terminal success -- never is.
    approaches_dir = run.run_dir / "approaches"
    assert (approaches_dir / "01").is_dir(), sorted(
        p.name for p in approaches_dir.iterdir())
    assert not (approaches_dir / "02").exists()
    assert (approaches_dir / "01" / "verdict-gate-findings.md").exists()
    ts = tasks(run)
    assert ts and all(t["status"] == "completed" for t in ts), ts


def test_a_malformed_verdict_gate_reply_defaults_to_rejected_and_fails_open(
        engine_factory):
    run = engine_factory(
        job={"run_id": "flow-verdict-gate-malformed", "on_complete": "exit",
             "peer_verdict_gate": True, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_VERDICT_GATE_MALFORMED_COUNT": "1"})
    code = run.proc.wait(timeout=60)
    assert code != 0, (
        "flow peer gates (verdict gate malformed): a run that never got a "
        "verified verdict_gate must not exit 0")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "phase:verdict_gate@1",
        "iteration:verdict_gate",          # unparseable -- ladder exhausts,
                                           # defaults to the safe rejected choice
        "verdict:verdictGate:rejected@1",
        "state:failed",
    ], flow="flow peer gates (verdict gate malformed)")

    st = status(run)
    assert (st.get("state"), st.get("verdict")) == ("failed", "unverified"), (
        f"flow peer gates (verdict gate malformed): a peer rejection is "
        f"never a terminal success, budget or not, got state="
        f"{st.get('state')!r} verdict={st.get('verdict')!r}")
    assert st.get("pauseReason") is None, (
        f"flow peer gates (verdict gate malformed): an incurable last-"
        f"approach rejection must never pause, got "
        f"{st.get('pauseReason')!r}")
