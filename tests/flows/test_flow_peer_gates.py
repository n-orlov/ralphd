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
  * **reject** -- `verdict_gate` rejects once, carrying a genuinely
    non-curable blocking finding, with an approach still left: v0.21.1
    requirement A means this ends the run failed/unverified RIGHT THERE
    -- no archive, no composite PRD, no fresh approach, no cure task --
    rather than folding into a fresh approach the way it used to before
    that fix
    (`test_a_verdict_gate_rejection_with_a_non_curable_finding_ends_the_run_even_with_an_approach_left`).
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

A sixth scenario (task 029, #157, requirement D.2/D.4) drives an OVERSIZED
plan through the same `peer_plan_review` pipeline: the planner's first
proposal carries a single task with `estimatedIterations: 4` and no staged
decomposition, which `plan_ingestion.enforce_plan_ingestion` refuses --
naming the offending task in a `contract.violation` event -- strictly
BEFORE `plan_review` is ever invoked (`_step_planning` runs the whole
ingestion retry loop first; the peer gate is the next state after ingestion
succeeds, never a participant in it). That refusal folds the offending
task's own errors into the very next planning re-prompt, so the SECOND
`iteration:planning` call is the re-ask this requirement's own D.2 wording
asks for; it resubmits a genuinely staged proposal (the oversized parent
plus two dependency-chained `Stage N` tasks with measurable criteria),
which ingestion accepts, and `plan_review`'s own single invocation then
approves it for real, reaching the run's ordinary success edge
(`test_an_oversized_unstaged_task_is_refused_at_ingestion_then_a_staged_
resubmission_reaches_plan_review_and_the_run_succeeds`). This is a true
account of the real pipeline, not a simplification: `plan_review.md`'s own
checklist item 8 asks the peer to catch the SAME shape as a second,
near-miss-only line of defence, but the ingestion lint is the one that
actually fires first on an exact-shape violation like this one, which is
exactly why task 026 built it as an ingestion-time refusal in the first
place rather than leaving the peer hat as the only backstop.

A seventh scenario (task 029, #157, requirement D.4) drives the OTHER half
of that pair -- the refusal `plan_review` itself makes. Its first proposal
is item 8's near-miss rather than an exact-shape violation: task `001` still
carries `estimatedIterations: 4` with one flat, unstaged demand, but the
proposal also carries two tasks that satisfy `plan_ingestion._staged_
decomposition_gap`'s cross-task regex mechanically (labelled `Stage 1`/
`Stage 2` in proposal order, bound to `001` by `dependsOn`, each with its
own measurable criteria, no repeats) while being about unrelated work. So
ingestion accepts it -- this run emits NO `contract.violation` at all -- and
the peer gate is the first thing that refuses it: `plan_review` objects
naming the offending task and its estimate, spends one real revision
(`planReviewRevisions == {"1": 1}`), the engine re-prompts planning with
that objection (observable in the second planning iteration's own recorded
`prompt.md`), and the resubmission whose stages genuinely decompose task
`001` is approved by the second `plan_review` invocation
(`test_a_near_miss_oversized_task_that_clears_ingestion_is_refused_by_plan_
review_then_the_staged_resubmission_is_approved`).
"""

from __future__ import annotations

import json

from tests.flows.flowlib import (
    assert_step_sequence, events_of, flow_steps, status, tasks)


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


def test_a_verdict_gate_rejection_with_a_non_curable_finding_ends_the_run_even_with_an_approach_left(
        engine_factory):
    """v0.21.1 requirement A (task 001, #192): the exact defect the
    requirement's own `Cured when:` clause names -- a verdict_gate
    rejection carrying a genuinely non-curable blocking finding (the
    stub's `STUB_VERDICT_GATE_REJECT_COUNT` knob writes `curable: False`)
    must end the run failed/unverified right there, whatever `max_
    approaches` leaves available -- no archive, no composite PRD, no
    fresh approach, no cure task. Before task 001's fix this same fixture
    folded the rejection into a fresh approach 2 and reached the run's
    real success edge once THAT approach's own gate invocation approved;
    the corrected engine never gets that far -- there is no second
    `verdict_gate` invocation at all."""
    run = engine_factory(
        job={"run_id": "flow-verdict-gate-reject", "on_complete": "exit",
             "peer_verdict_gate": True, "max_approaches": 2},
        stub_env={"STUB_TASKS": "1", "STUB_VERDICT_GATE_REJECT_COUNT": "1"})
    code = run.proc.wait(timeout=60)
    assert code != 0, (
        "flow peer gates (verdict gate reject): a run ended by a "
        "non-curable verdict_gate rejection must not exit 0")

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
        "iteration:verdict_gate",          # rejects a non-curable finding
        "verdict:verdictGate:rejected@1",
        "state:failed",
    ], flow="flow peer gates (verdict gate reject)")

    st = status(run)
    assert (st.get("state"), st.get("verdict"), st.get("approach")) == (
        "failed", "unverified", 1), (
        f"flow peer gates (verdict gate reject): expected failed/unverified "
        f"on approach 1, got state={st.get('state')!r} verdict="
        f"{st.get('verdict')!r} approach={st.get('approach')!r}")

    # requirement A.2: no archive, no replan, no second approach -- the
    # rejection ended the run right where it was found.
    approaches_dir = run.run_dir / "approaches"
    assert not approaches_dir.exists() or not list(approaches_dir.iterdir()), (
        f"flow peer gates (verdict gate reject): no approach archive "
        f"expected, found "
        f"{sorted(p.name for p in approaches_dir.iterdir()) if approaches_dir.exists() else []}")
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
    `verdict_gate` invocation REJECTS carrying a genuinely non-curable
    blocking finding (the stub's `STUB_VERDICT_GATE_REJECT_COUNT` knob,
    same shape `test_a_verdict_gate_rejection_with_a_non_curable_finding_
    ends_the_run_even_with_an_approach_left` drives alone). v0.21.1
    requirement A (task 001) means that rejection ends the run failed/
    unverified RIGHT THERE, even with `max_approaches=2` leaving a second
    approach available -- no archive, no fresh approach, no second
    `verdict_gate` invocation. This is the composition `tests/test_e2e_
    composed_gates_task032.py::test_both_peer_gates_enabled_in_the_same_
    run` used to own (task 020, C.9), updated for requirement A: both
    gates are still observably exercised for real (plan_review's full
    objection-then-approval round trip, verdict_gate's real rejection),
    just never reaching a second approach now that the rejection itself
    ends the run instead of spending one."""
    run = engine_factory(
        job={"run_id": "flow-both-peer-gates", "on_complete": "exit",
             "peer_plan_review": True, "peer_verdict_gate": True,
             "max_approaches": 2},
        stub_env={"STUB_TASKS": "1", "STUB_PLAN_REVIEW_OBJECTIONS": "1",
                  "STUB_VERDICT_GATE_REJECT_COUNT": "1"})
    code = run.proc.wait(timeout=60)
    assert code != 0, (
        "flow peer gates (both gates): a run ended by a non-curable "
        "verdict_gate rejection must not exit 0")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        # approach 1: plan_review objects once, then approves; after review
        # verifies, the verdict gate rejects a non-curable finding, which
        # now ends the run right there (requirement A) instead of spending
        # the approach on a fresh planning round.
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
        "iteration:verdict_gate",          # rejects -- non-curable, ends it
        "verdict:verdictGate:rejected@1",
        "state:failed",
    ], flow="flow peer gates (both gates)")

    st = status(run)
    assert (st.get("state"), st.get("verdict"), st.get("approach")) == (
        "failed", "unverified", 1), (
        f"flow peer gates (both gates): expected failed/unverified on "
        f"approach 1, got state={st.get('state')!r} "
        f"verdict={st.get('verdict')!r} approach={st.get('approach')!r}")
    # plan_review's own mechanism fired for real: one revision recorded
    # against approach 1.
    assert st.get("planReviewRevisions") == {"1": 1}, (
        f"flow peer gates (both gates): exactly one plan revision, against "
        f"approach 1, got {st.get('planReviewRevisions')!r}")
    # requirement A.2: no archive, no replan, no second approach -- the run
    # ended right on the gate's own rejection.
    approaches_dir = run.run_dir / "approaches"
    assert not approaches_dir.exists() or not list(approaches_dir.iterdir()), (
        f"flow peer gates (both gates): no approach archive expected, "
        f"found {sorted(p.name for p in approaches_dir.iterdir())}"
        if approaches_dir.exists() else "")
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


def test_an_oversized_unstaged_task_is_refused_at_ingestion_then_a_staged_resubmission_reaches_plan_review_and_the_run_succeeds(
        engine_factory):
    """Task 029 (requirement D.2/D.4, #157): the planner's FIRST proposal
    is one flat task carrying `estimatedIterations: 4` and no staged
    decomposition -- `plan_ingestion.enforce_plan_ingestion` refuses it
    (naming task `001` in a `contract.violation` event, before
    `plan_review` is ever invoked) and re-prompts the SAME planning
    approach; the stub's second `Role: Planner` invocation (recognising the
    engine's own "your previous plan-proposal.json was rejected" re-prompt
    text -- `STUB_PLANNING_RETRY_TASKS_JSON`) resubmits a genuinely staged
    proposal: the same oversized parent beside two dependency-chained
    `Stage N` tasks with their own measurable criteria. Ingestion accepts
    that one, `plan_review`'s single invocation approves it (no objection
    knob set), and the three tasks run to completion."""
    oversized_only = json.dumps([
        {"id": "001", "title": "the big oversized thing",
         "successCriteria": "the big oversized thing is verifiably finished",
         "estimatedIterations": 4},
    ])
    staged = json.dumps([
        {"id": "001", "title": "the big oversized thing",
         "successCriteria": "the big oversized thing is verifiably finished",
         "estimatedIterations": 4},
        {"id": "002", "title": "Stage 1: first half",
         "successCriteria": "`tests/test_flow_stage_a.py` passes with at "
                             "least 3 tests",
         "dependsOn": ["001"]},
        {"id": "003", "title": "Stage 2: second half",
         "successCriteria": "`tests/test_flow_stage_b.py` passes with at "
                             "least 3 tests",
         "dependsOn": ["002"]},
    ])
    run = engine_factory(
        job={"run_id": "flow-plan-oversized-task-staged", "on_complete": "exit",
             "peer_plan_review": True, "max_approaches": 1},
        stub_env={"STUB_TASKS_JSON": oversized_only,
                  "STUB_PLANNING_RETRY_TASKS_JSON": staged})
    assert run.proc.wait(timeout=60) == 0, (
        "flow peer gates (oversized task staged): the engine did not exit "
        "0 after the staged resubmission's own plan_review invocation "
        "approved")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",              # oversized, unstaged -- refused
        "iteration:planning",               # staged resubmission -- accepted
        "phase:plan_review@1",
        "iteration:plan_review",           # approves
        "iteration:worker",                # 001 (no deps)
        "iteration:worker",                # 002 (Stage 1, depends on 001)
        "iteration:worker",                # 003 (Stage 2, depends on 002)
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow peer gates (oversized task staged)")

    st = status(run)
    assert (st.get("state"), st.get("verdict")) == ("succeeded", "verified"), (
        f"flow peer gates (oversized task staged): expected succeeded/"
        f"verified, got state={st.get('state')!r} verdict="
        f"{st.get('verdict')!r}")
    # plan_review never had to spend a revision of its own -- the refusal
    # that actually blocked the first proposal was ingestion's, not the
    # peer's, and the peer's own single invocation approved outright.
    assert st.get("planReviewRevisions") in (None, {}), (
        f"flow peer gates (oversized task staged): plan_review approved on "
        f"its first invocation, so no revision should be recorded against "
        f"it, got {st.get('planReviewRevisions')!r}")

    ts = tasks(run)
    assert ts and all(t["status"] == "completed" for t in ts), ts
    assert {t["id"] for t in ts} == {"001", "002", "003"}, (
        f"flow peer gates (oversized task staged): the staged resubmission "
        f"is the plan that actually ran, got {[t['id'] for t in ts]!r}")

    # the ingestion refusal itself: a `contract.violation` naming task 001
    # and the estimatedIterations bound, on the very first attempt.
    violations = events_of(run, "contract.violation")
    assert violations and violations[0].get("hat") == "planning", (
        f"flow peer gates (oversized task staged): expected at least one "
        f"planning contract.violation event, got {violations!r}")
    first_errors = " ".join(violations[0].get("errors") or [])
    assert "'001'" in first_errors and "estimatedIterations" in first_errors, (
        f"flow peer gates (oversized task staged): the ingestion refusal "
        f"must name the offending task, got errors={first_errors!r}")


def _planning_prompts(run) -> list[str]:
    """Every `planning` iteration's own recorded `prompt.md`, in iteration
    order -- the run's own on-disk record of what the planner was actually
    handed (`iterations/NNNN/prompt.md` beside that iteration's
    `meta.json`), read the same black-box way this module reads
    `events.jsonl`. The engine keeps a `plan_review` objection out of
    `events.jsonl` on purpose (`_step_plan_review` hands it to the very next
    planning attempt through `_pending_plan_review_objection`), so the
    prompt the re-prompted planner received IS where the objection is
    observable."""
    out: list[tuple[int, str]] = []
    for itdir in sorted((run.run_dir / "iterations").iterdir()):
        meta_path, prompt_path = itdir / "meta.json", itdir / "prompt.md"
        if not meta_path.exists() or not prompt_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        if meta.get("phase") == "planning":
            out.append((int(meta.get("number") or 0), prompt_path.read_text()))
    return [text for _, text in sorted(out)]


def test_a_near_miss_oversized_task_that_clears_ingestion_is_refused_by_plan_review_then_the_staged_resubmission_is_approved(
        engine_factory):
    """Task 029 (requirement D.4, #157): the refusal that this scenario
    exercises is **`plan_review`'s own**, not `enforce_plan_ingestion`'s.

    The first proposal is item 8's near-miss: task `001` carries
    `estimatedIterations: 4` with one flat, unstaged demand, beside two
    tasks that satisfy `plan_ingestion._staged_decomposition_gap`'s regex
    mechanically (both labelled `Stage 1`/`Stage 2` in proposal order, both
    bound to `001` by `dependsOn`, each with its own measurable criteria,
    no repeats) while being about entirely unrelated work. That plan
    therefore CLEARS ingestion -- no `contract.violation` is emitted at all
    in this run, which is what separates this scenario from the ingestion
    one above -- and reaches the peer gate with the oversized task still
    undecomposed. `plan_review` objects, naming the offending task and its
    estimate (`STUB_PLAN_REVIEW_OVERSIZED_OBJECTION`), which spends one real
    revision (`planReviewRevisions == {"1": 1}`) and routes back to a
    genuine second `planning` iteration whose own prompt carries the
    objection; that attempt resubmits a plan whose two `Stage N` tasks
    decompose task `001`'s own work, and the SECOND `plan_review`
    invocation approves it for real.
    """
    near_miss = json.dumps([
        {"id": "001", "title": "the whole search index rewrite",
         "successCriteria": "the search index rewrite is verifiably finished",
         "estimatedIterations": 4},
        {"id": "002", "title": "Stage 1: unrelated changelog tidy-up",
         "successCriteria": "`tests/test_flow_tidy_a.py` passes with at "
                            "least 3 tests",
         "dependsOn": ["001"]},
        {"id": "003", "title": "Stage 2: unrelated changelog follow-up",
         "successCriteria": "`tests/test_flow_tidy_b.py` passes with at "
                            "least 4 tests",
         "dependsOn": ["001"]},
    ])
    genuinely_staged = json.dumps([
        {"id": "001", "title": "the whole search index rewrite",
         "successCriteria": "the search index rewrite is verifiably finished",
         "estimatedIterations": 4},
        {"id": "002", "title": "Stage 1: the search index ingest half",
         "successCriteria": "`tests/test_search_index_ingest.py` passes with "
                            "at least 3 tests",
         "dependsOn": ["001"]},
        {"id": "003", "title": "Stage 2: the search index query half",
         "successCriteria": "`tests/test_search_index_query.py` passes with "
                            "at least 4 tests",
         "dependsOn": ["001"]},
    ])
    run = engine_factory(
        job={"run_id": "flow-plan-review-refuses-oversized",
             "on_complete": "exit", "peer_plan_review": True,
             "max_approaches": 1},
        stub_env={"STUB_TASKS_JSON": near_miss,
                  "STUB_PLANNING_AFTER_OBJECTION_TASKS_JSON": genuinely_staged,
                  "STUB_PLAN_REVIEW_OVERSIZED_OBJECTION": "1"})
    assert run.proc.wait(timeout=60) == 0, (
        "flow peer gates (plan review refuses oversized): the engine did not "
        "exit 0 after the staged resubmission was approved")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",              # near-miss plan -- clears ingestion
        "phase:plan_review@1",
        "iteration:plan_review",           # objects: task 001 is oversized
        "phase:planning@1",                # the revision the peer asked for
        "iteration:planning",              # genuinely staged resubmission
        "phase:plan_review@1",
        "iteration:plan_review",           # approves
        "iteration:worker",                # 001 (no deps)
        "iteration:worker",                # 002 (Stage 1)
        "iteration:worker",                # 003 (Stage 2)
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow peer gates (plan review refuses oversized)")

    # the peer's refusal cost a real revision, recorded on disk...
    st = status(run)
    assert st.get("planReviewRevisions") == {"1": 1}, (
        f"flow peer gates (plan review refuses oversized): the plan_review "
        f"objection must have spent exactly one revision against approach 1, "
        f"got {st.get('planReviewRevisions')!r}")
    assert (st.get("state"), st.get("verdict")) == ("succeeded", "verified"), (
        f"flow peer gates (plan review refuses oversized): expected "
        f"succeeded/verified, got state={st.get('state')!r} verdict="
        f"{st.get('verdict')!r}")

    # ...and ingestion never refused anything in this run, so the refusal
    # under test is unambiguously the peer gate's own.
    violations = events_of(run, "contract.violation")
    assert violations == [], (
        f"flow peer gates (plan review refuses oversized): the near-miss "
        f"plan must CLEAR plan_ingestion so plan_review is the refusing "
        f"gate, but ingestion refused something: {violations!r}")

    # the objection named the offending task and its estimate, and reached
    # the re-prompted planner: the second planning prompt carries it.
    planning_prompts = _planning_prompts(run)
    assert len(planning_prompts) == 2, (
        f"flow peer gates (plan review refuses oversized): expected exactly "
        f"two planning iterations (the near-miss and the revision), got "
        f"{len(planning_prompts)}")
    reprompt = planning_prompts[1]
    assert "Peer plan review objected to your previous plan" in reprompt, (
        "flow peer gates (plan review refuses oversized): the second "
        "planning prompt must carry the engine's own objection block")
    for needle in ("'001'", "estimatedIterations 4"):
        assert needle in reprompt, (
            f"flow peer gates (plan review refuses oversized): the objection "
            f"handed to the re-prompted planner must name {needle!r}; the "
            f"objection block it carried was:\n{reprompt[-1500:]}")

    # the plan that actually ran is the genuinely staged resubmission
    ts = tasks(run)
    assert ts and all(t["status"] == "completed" for t in ts), ts
    by_id = {t["id"]: t for t in ts}
    assert set(by_id) == {"001", "002", "003"}, [t["id"] for t in ts]
    assert by_id["001"].get("estimatedIterations") == 4, (
        f"flow peer gates (plan review refuses oversized): the approved plan "
        f"keeps the oversized parent, now genuinely staged, got "
        f"{by_id['001']!r}")
    assert all("search index" in by_id[tid]["title"]
               for tid in ("002", "003")), (
        f"flow peer gates (plan review refuses oversized): the accepted plan "
        f"must be the resubmission whose stages cover task 001's own work, "
        f"got {[by_id[tid]['title'] for tid in ('002', '003')]!r}")
