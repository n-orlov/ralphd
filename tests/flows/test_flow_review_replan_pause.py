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

import json

from tests.flows.flowlib import (assert_step_sequence, events, events_of,
                                 flow_steps, status, tasks)

# Task cure-01-01 (requirement I): the deadline this module gives a real
# engine subprocess to reach its own terminal state and exit. The nodes below
# used `timeout=60`, which is ~25x the ~2.4 s each of them actually needs when
# it runs alone -- generous on an idle machine, yet
# `test_a_rejection_on_the_last_approach_with_nothing_curable_fails` still hit
# it once in the fast lane at `6e3053e` (`--durations` recorded exactly 60.00 s
# of call and a 10.00 s teardown, i.e. the wait expired and the fixture had to
# kill the child) and passed solo immediately afterwards. Waiting longer is the
# bounded, assertion-preserving half of the response -- nothing here is
# loosened, the exit code and every step assertion stay exactly as they were --
# and it keeps this module's own wait far inside the 20-minute ceiling the
# run's standing rules put on any process wait.
ENGINE_EXIT_WAIT_S = 180

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
    assert run.proc.wait(timeout=ENGINE_EXIT_WAIT_S) == 0, (
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
    assert run.proc.wait(timeout=ENGINE_EXIT_WAIT_S) == 1, (
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

    paused = run.wait_state(("paused",), timeout=ENGINE_EXIT_WAIT_S)
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

    assert run.proc.wait(timeout=ENGINE_EXIT_WAIT_S) == 0, (
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


# ---------------------------------------------------------------------------
# Task 021 (requirement C.10): the four nodes folded in from
# `tests/test_ladder_exhaustion_e2e_task033.py` (deleted in the same commit,
# one § "Subsumed" row per node). Same subject as this flow -- what a
# judging site's ladder exhaustion does with budget REMAINING -- moved
# verbatim: same `live` fixture, same stub knobs
# (`STUB_VERIFY_MALFORMED_COUNT`/`STUB_REVIEW_MALFORMED_COUNT`/
# `STUB_VERDICT_GATE_MALFORMED_COUNT`), same assertions, only the module and
# its three local readers changed (`flowlib.status`/`flowlib.events` replace
# the deleted module's own `_status`/`_events` copies).
#
# The three sites: `verify` retries the SAME task in another worker
# iteration; `review`/`verdict_gate` archive approach 1 (today's
# `rejected_approach_left` safe default) and approach 2's own invocation of
# the same site answers normally. The fourth node is task 032's
# fenced-payload rescue at the `verify` site -- rescued on the FIRST attempt
# with ZERO extra stub invocations, the direct contrast to exhaustion.
# ---------------------------------------------------------------------------


def _iteration_metas(run) -> list[dict]:
    iters = sorted((run.run_dir / "iterations").iterdir())
    return [json.loads((d / "meta.json").read_text()) for d in iters]


def _marker(run, name: str) -> str | None:
    p = run.run_dir / f".stub-{name}"
    return p.read_text().strip() if p.exists() else None


def test_verify_ladder_exhaustion_with_budget_remaining_does_not_end_the_run(live):
    run = live(run_id="g5-verify-ladder-exhaustion",
               job={"vigilant": True, "on_complete": "exit"},
               stub_env={"STUB_TASKS": "1", "STUB_VERIFY_MALFORMED_COUNT": "1"})
    assert run.proc.wait(timeout=ENGINE_EXIT_WAIT_S) == 0
    st = status(run)
    assert st.get("state") == "succeeded", st
    assert st.get("verdict") == "verified", st

    # the run did NOT end at the exhausted verify -- another worker
    # iteration picked the same task back up (its validation-failed
    # retry), a second verify invocation passed, and review still ran.
    metas = _iteration_metas(run)
    phases = [m["phase"] for m in metas]
    assert phases == ["planning", "worker", "verify", "worker", "verify",
                       "review"], phases

    # the ladder genuinely exhausted (never merely "got lucky and passed
    # first try"): two real verify invocations were made -- the exhausted
    # one and the successful retry.
    assert _marker(run, "verify-call-count") == "2"

    # today's ordinary safe default fired exactly once: one validation-
    # failed round recorded, then the task reached vigilant's own
    # terminal `validated` -- never stuck at `validation-failed`.
    all_tasks = tasks(run)
    assert len(all_tasks) == 1
    assert all_tasks[0]["status"] == "validated", all_tasks[0]
    assert all_tasks[0]["validationAttempts"] == 1, all_tasks[0]

    # the exhausted attempt's uncharged re-asks are recorded under
    # usage.byPhase.contract and the recorded contract.agent_fault --
    # mirrors task 001's unit-tier invariant, now observed through the
    # engine's own recorded run state rather than a scripted runner.
    by_phase = st.get("usage", {}).get("byPhase", {})
    assert by_phase.get("contract", {}).get("totalTokens", 0) > 0, by_phase
    faults = events_of(run, "contract.agent_fault")
    assert len(faults) == 1, events(run)
    assert faults[0].get("hat") == "verify", faults[0]

    # there was genuine iteration budget left over -- the run did not
    # merely limp to its own cap.
    assert st.get("iterationsUsed", 0) < st.get("iterationsBudget", 0), st


def test_review_ladder_exhaustion_with_budget_remaining_does_not_end_the_run(live):
    run = live(run_id="g5-review-ladder-exhaustion",
               job={"on_complete": "exit", "max_approaches": 2},
               stub_env={"STUB_REVIEW_MALFORMED_COUNT": "1"})
    assert run.proc.wait(timeout=ENGINE_EXIT_WAIT_S) == 0
    st = status(run)
    assert st.get("state") == "succeeded", st
    assert st.get("verdict") == "verified", st
    assert st.get("approach") == 2, st

    metas = _iteration_metas(run)
    phase_approach = [(m["phase"], m["approach"]) for m in metas]
    assert phase_approach == [
        ("planning", 1), ("worker", 1), ("worker", 1), ("review", 1),
        ("planning", 2), ("worker", 2), ("worker", 2), ("review", 2),
    ], phase_approach

    # only ONE real review invocation happened per approach: the ladder's
    # own uncharged re-asks never advance this marker (they carry no
    # `Role: Reviewer` text), so it reads exactly 2 -- one exhausted call
    # for approach 1, one passing call for approach 2.
    assert _marker(run, "review-count") == "2"

    # today's ordinary archive-with-safe-default consequences of an
    # exhausted (unparseable) review reply, unchanged: approach 1 is
    # archived, approach 2 -- the run's real terminal success -- never is.
    approaches_dir = run.run_dir / "approaches"
    assert (approaches_dir / "01").is_dir(), sorted(
        p.name for p in approaches_dir.iterdir())
    assert not (approaches_dir / "02").exists()
    assert (approaches_dir / "01" / "tasks.json").exists()

    all_tasks = tasks(run)
    assert all_tasks and all(t["status"] == "completed" for t in all_tasks)

    by_phase = st.get("usage", {}).get("byPhase", {})
    assert by_phase.get("contract", {}).get("totalTokens", 0) > 0, by_phase
    assert st.get("iterationsUsed", 0) < st.get("iterationsBudget", 0), st


def test_verdict_gate_ladder_exhaustion_with_budget_remaining_does_not_end_the_run(live):
    run = live(run_id="g5-verdict-gate-ladder-exhaustion",
               job={"peer_verdict_gate": True, "on_complete": "exit",
                    "max_approaches": 2},
               stub_env={"STUB_VERDICT_GATE_MALFORMED_COUNT": "1"})
    assert run.proc.wait(timeout=ENGINE_EXIT_WAIT_S) == 0
    st = status(run)
    assert st.get("state") == "succeeded", st
    assert st.get("verdict") == "verified", st
    assert st.get("approach") == 2, st

    metas = _iteration_metas(run)
    phase_approach = [(m["phase"], m["approach"]) for m in metas]
    assert phase_approach == [
        ("planning", 1), ("worker", 1), ("worker", 1), ("review", 1),
        ("verdict_gate", 1),
        ("planning", 2), ("worker", 2), ("worker", 2), ("review", 2),
        ("verdict_gate", 2),
    ], phase_approach

    # the marker counts EVERY `Role: Peer verdict gate` invocation seen
    # (its own `_vg_mseen` counter keeps advancing once armed): 2 total --
    # the exhausted approach-1 call plus approach 2's own, later, normal
    # invocation (which falls through once `_vg_mseen >= wanted`).
    assert _marker(run, "verdict-gate-malformed-count") == "2"

    # the exhausted gate invocation still reaches today's ordinary
    # archive-with-safe-default path: approach 1 is archived, approach 2
    # -- the run's real terminal success -- never is.
    approaches_dir = run.run_dir / "approaches"
    assert (approaches_dir / "01").is_dir(), sorted(
        p.name for p in approaches_dir.iterdir())
    assert not (approaches_dir / "02").exists()

    all_tasks = tasks(run)
    assert all_tasks and all(t["status"] == "completed" for t in all_tasks)

    by_phase = st.get("usage", {}).get("byPhase", {})
    assert by_phase.get("contract", {}).get("totalTokens", 0) > 0, by_phase
    assert st.get("iterationsUsed", 0) < st.get("iterationsBudget", 0), st


def test_verify_fenced_payload_rescue_settles_with_no_extra_stub_invocation(live):
    run = live(run_id="g5-verify-fenced-rescue",
               job={"vigilant": True, "on_complete": "exit"},
               stub_env={"STUB_TASKS": "1", "STUB_VERIFY_MALFORMED_COUNT": "1",
                         "STUB_VERIFY_MALFORMED_FENCED": "1"})
    assert run.proc.wait(timeout=ENGINE_EXIT_WAIT_S) == 0
    st = status(run)
    assert st.get("state") == "succeeded", st
    assert st.get("verdict") == "verified", st

    # the ordinary success shape -- never a validation-failed round, and
    # never a second worker retry -- because the FIRST verify reply was
    # rescued in place.
    metas = _iteration_metas(run)
    phases = [m["phase"] for m in metas]
    assert phases == ["planning", "worker", "verify", "review"], phases

    all_tasks = tasks(run)
    assert len(all_tasks) == 1
    assert all_tasks[0]["status"] == "validated", all_tasks[0]
    # never touched -- the rescue fixed the FIRST attempt's own reply, so
    # no validation-failed round was ever recorded against this task.
    assert all_tasks[0].get("validationAttempts", 0) == 0, all_tasks[0]

    # exactly ONE real stub-pi invocation for the whole verify episode --
    # the rescue never asked the model again, unlike the exhaustion test
    # above (whose own marker reads "2").
    assert _marker(run, "verify-call-count") == "1"

    # no contract-phase usage at all -- `_ask_contract_fix`'s
    # fenced-payload branch returns before ever calling `self.runner.run`.
    by_phase = st.get("usage", {}).get("byPhase", {})
    assert not by_phase.get("contract"), by_phase
    assert events_of(run, "contract.agent_fault") == [], events(run)
