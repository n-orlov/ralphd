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

Five scenarios: the three shapes requirement C.2 names by name, plus the two
COMPOSED shapes task 020 (C.9) folded in from
`tests/test_e2e_composed_gates_task032.py` when that module was deleted --
the composed pair was the one thing this flow did not cover, so it is here
rather than left as a disclosed narrowing:

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
  * **composed: vigilant + verdict_gate** -- task 019/020's per-task `verify`
    mini-FSM and the peer gate in the SAME run: both fire on every task the
    plan carries, each task ends vigilant's own `validated`, and the run's
    real success edge still sits at `verdict_gate`
    (`test_vigilant_mode_and_the_verdict_gate_compose_in_one_run`).
  * **composed: both peer gates** -- `peer_plan_review` AND
    `peer_verdict_gate` in one non-vigilant run, each with a real
    objection/rejection round trip before its own approval, so both gates
    are observably exercised rather than merely configured
    (`test_both_peer_gates_compose_in_one_run`).
  * **malformed** -- an unparseable `verdict_gate` reply is never a
    run-ending contract violation: it exhausts the uncharged re-ask ladder
    and defaults to the same safe choice an unparseable `review` reply
    already defaults to (rejected, never verified) -- and, with no approach
    left this time, the run fails open to `finishing`/`failed`, never a
    hang or a crash
    (`test_a_malformed_verdict_gate_reply_defaults_to_rejected_and_fails_open`).

Every scenario but the vigilant composition uses `STUB_TASKS=1` to keep the
worker/verify portion of the sequence to a single iteration -- the two gates
are what this module is about, not the plan's own size. The vigilant
composition keeps two tasks on purpose: "both mechanisms fire on every task
the plan carries" is exactly what it asserts, and one task cannot show it.
"""

from __future__ import annotations

import json

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


def _iteration_metas(run) -> list[dict]:
    """Every iteration's `meta.json`, in iteration order -- the per-iteration
    record `flowlib`'s step vocabulary deliberately does not carry (vigilant's
    `verifiedTask`/`verifyOutcome` are task-level bookkeeping, not steps of the
    run's path), read straight off the run dir like any other host would."""
    iters = sorted((run.run_dir / "iterations").iterdir())
    return [json.loads((d / "meta.json").read_text()) for d in iters
            if (d / "meta.json").exists()]


def test_vigilant_mode_and_the_verdict_gate_compose_in_one_run(engine_factory):
    """`vigilant: true` + `peer_verdict_gate: true`, no rejection knob set:
    the two mechanisms are independent axes of the same state table
    (`transitions.table_for`), and this drives them TOGETHER through the real
    subprocess -- each of the plan's two tasks gets its own `verify`
    iteration and ends vigilant's own `validated` (never the non-vigilant
    `completed`), and the run reaches `succeeded` only after the second,
    independent `verdict_gate` opinion agrees with `review`'s own verified
    verdict. This is the composition `tests/test_e2e_composed_gates_task032
    .py::test_verdict_gate_with_vigilant_mode_both_observably_exercised`
    used to own (task 020, C.9)."""
    run = engine_factory(
        job={"run_id": "flow-vigilant-plus-verdict-gate", "on_complete": "exit",
             "vigilant": True, "peer_verdict_gate": True, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2"})
    assert run.proc.wait(timeout=60) == 0, (
        "flow peer gates (vigilant + verdict gate): the engine did not exit 0 "
        "after both per-task verifies and the gate agreed")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",                # task 1
        "iteration:verify",                # vigilant's own per-task verify
        "signal:taskVerified",
        "iteration:worker",                # task 2
        "iteration:verify",
        "signal:taskVerified",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "phase:verdict_gate@1",
        "iteration:verdict_gate",          # the independent second opinion
        "verdict:verdictGate:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow peer gates (vigilant + verdict gate)")

    st = status(run)
    assert (st.get("state"), st.get("verdict")) == ("succeeded", "verified"), (
        f"flow peer gates (vigilant + verdict gate): expected succeeded/"
        f"verified, got state={st.get('state')!r} verdict={st.get('verdict')!r}")

    # vigilant's own mechanism fired per TASK, not once for the plan: two
    # verify iterations, each naming the task it verified, each passing.
    metas = _iteration_metas(run)
    verify_metas = [m for m in metas if m["phase"] == "verify"]
    assert len(verify_metas) == 2, (
        f"flow peer gates (vigilant + verdict gate): one verify iteration per "
        f"task (2 expected), got {len(verify_metas)}: "
        f"{[m['phase'] for m in metas]}")
    for vm in verify_metas:
        assert vm.get("verifiedTask"), (
            f"flow peer gates (vigilant + verdict gate): a vigilant verify "
            f"iteration must record the task it verified, got {vm!r}")
        assert vm.get("verifyOutcome") == "pass", (
            f"flow peer gates (vigilant + verdict gate): expected a passing "
            f"verify outcome, got {vm.get('verifyOutcome')!r}")

    # the peer gate's own mechanism fired too: exactly one invocation, after
    # review, and -- never having rejected -- no findings channel on disk.
    gate_metas = [m for m in metas if m["phase"] == "verdict_gate"]
    assert len(gate_metas) == 1, (
        f"flow peer gates (vigilant + verdict gate): exactly one gate "
        f"invocation expected, got {len(gate_metas)}")
    assert not (run.run_dir / "verdict-gate-findings.md").exists(), (
        "flow peer gates (vigilant + verdict gate): an approving gate must "
        "leave no findings artifact")

    # both mechanisms' terminal bookkeeping holds together: every task carries
    # vigilant's `validated`, which is the state the gate had to see to agree.
    ts = tasks(run)
    assert len(ts) == 2 and all(t["status"] == "validated" for t in ts), (
        f"flow peer gates (vigilant + verdict gate): both tasks must end "
        f"vigilant's own 'validated', never the non-vigilant 'completed', got "
        f"{[(t['id'], t['status']) for t in ts]}")


def test_both_peer_gates_compose_in_one_run(engine_factory):
    """`peer_plan_review: true` + `peer_verdict_gate: true`, non-vigilant,
    with BOTH gates driven through a real round trip in the same run: the
    first `plan_review` invocation objects (spending approach 1's one
    revision) and the second approves; then, after `review` verifies, the
    first `verdict_gate` invocation REJECTS -- spending approach 1 -- and the
    fresh approach's own `plan_review` (the objection knob is spent, not
    reset) and `verdict_gate` both approve, which is the run's real success
    edge. This is the composition `tests/test_e2e_composed_gates_task032.py
    ::test_both_peer_gates_enabled_in_the_same_run` used to own (task 020,
    C.9): both gates observably exercised, not merely configured."""
    run = engine_factory(
        job={"run_id": "flow-both-peer-gates", "on_complete": "exit",
             "peer_plan_review": True, "peer_verdict_gate": True,
             "max_approaches": 2},
        stub_env={"STUB_TASKS": "1", "STUB_PLAN_REVIEW_OBJECTIONS": "1",
                  "STUB_VERDICT_GATE_REJECT_COUNT": "1"})
    assert run.proc.wait(timeout=60) == 0, (
        "flow peer gates (both gates): the engine did not exit 0 after "
        "approach 2's own gate invocations approved")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        # approach 1: plan_review objects once, then approves; after review
        # verifies, the verdict gate rejects, spending the approach.
        "phase:planning@1",
        "iteration:planning",
        "phase:plan_review@1",
        "iteration:plan_review",           # objects -- spends a revision
        "phase:planning@1",
        "iteration:planning",
        "phase:plan_review@1",
        "iteration:plan_review",           # approves
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "phase:verdict_gate@1",
        "iteration:verdict_gate",          # rejects -- an approach is left
        "verdict:verdictGate:rejected@1",
        # approach 2: a fresh planning/plan_review round, approving on its own
        # first try, then the gate's second invocation approves for real.
        "phase:planning@2",
        "iteration:planning",
        "phase:plan_review@2",
        "iteration:plan_review",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@2",
        "iteration:review",
        "verdict:review:verified@2",
        "phase:verdict_gate@2",
        "iteration:verdict_gate",
        "verdict:verdictGate:verified@2",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow peer gates (both gates)")

    st = status(run)
    assert (st.get("state"), st.get("verdict"), st.get("approach")) == (
        "succeeded", "verified", 2), (
        f"flow peer gates (both gates): expected succeeded/verified on "
        f"approach 2, got state={st.get('state')!r} "
        f"verdict={st.get('verdict')!r} approach={st.get('approach')!r}")
    # plan_review's own mechanism fired for real: one revision recorded
    # against approach 1, none against approach 2 (its first pass approved).
    assert st.get("planReviewRevisions") == {"1": 1}, (
        f"flow peer gates (both gates): exactly one plan revision, against "
        f"approach 1, got {st.get('planReviewRevisions')!r}")
    # verdict_gate's own mechanism fired for real too: approach 1 archived
    # with the gate-attributed findings channel, approach 2 -- the run's real
    # terminal success -- never archived.
    approaches_dir = run.run_dir / "approaches"
    assert (approaches_dir / "01").is_dir(), sorted(
        p.name for p in approaches_dir.iterdir())
    assert not (approaches_dir / "02").exists()
    assert (approaches_dir / "01" / "verdict-gate-findings.md").exists()
    findings = json.loads(
        (approaches_dir / "01" / "verdict-gate-findings.json").read_text())
    assert findings and all(f.get("author") == "verdict_gate" for f in findings), (
        f"flow peer gates (both gates): the archived findings must be "
        f"attributed to the gate itself, got {findings!r}")
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
