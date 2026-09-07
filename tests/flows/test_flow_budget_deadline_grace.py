"""Flow (task 017, requirement C.6): **budget exhaustion, the deadline, and
the grace review** -- the run-ending machinery that fires when the loop's
own worker cycle never reaches a `_step_review` on budget alone, over a real
`ralphd-engine` subprocess with the stub `pi` binary on PATH, `STUB_SLEEP=0`.

`LoopSupervisor.budget_left()` (loop.py) is a conjunction of two clocks: the
iteration count (`cfg.iterations`) and the wall-clock deadline
(`self.deadline`, published as `status.json`'s `deadlineAt`). Either one
reaching zero ends the worker cycle the same way -- `not budget_left()` in
`_step_worker`'s dispatch. From there `_maybe_grace_review` (task 002) is the
one fork: if every task is already `completed` at that instant, the run gets
exactly one OFF-BUDGET review before its fate is sealed; otherwise (or if
that review does not verify) the ordinary `_finish_tail_unverified` write
ends the job `failed`/`unverified` with no review at all.

Four nodes, each exercising a different corner of that fork:

  * **ordinary exhaustion, tasks incomplete** -- the iteration count runs out
    with work still outstanding; no review, no grace, straight to `failed`
    (mirrors `tests/test_e2e.py::test_budget_exhaustion_fails_job`, read here
    through the reduced event sequence instead of iteration-directory counts);
  * **grace review granted and VERIFIED** -- the iteration that completes the
    LAST task is also the iteration that exhausts the iteration-count
    budget: no ordinary review slot exists, so the off-budget grace review
    runs and a satisfied stub review lands the job `succeeded`;
  * **grace review granted but does not verify** -- same shape, but the stub
    review rejects; the grace review is a single off-budget shot, never
    granted twice, and the job ends `failed`/`unverified` naming "grace
    review" in its `reason`;
  * **the deadline, not the iteration count, ends the cycle** -- a
    `job_timeout_s` far below what the iteration budget would allow forces
    `budget_left()` false on the wall clock, not on `cfg.iterations`, with
    tasks still incomplete (no grace review possible either). Because how
    many worker iterations fit before a wall-clock deadline fires is itself
    a race against process-spawn/scheduler jitter, this node's own worker
    count is NOT pinned exactly -- `flowlib.assert_steps_include` checks the
    fixed prefix/suffix in order instead of the whole sequence, and the
    deadline-not-iteration-count claim is pinned instead on `status.json`
    (`iterationsUsed` strictly under `iterationsBudget`, `deadlineAt` at or
    before `endedAt`).

Every job below uses `max_approaches: 1` -- a budget-exhaustion ending must
never spend a fresh approach, so keeping only one available and still
reaching a clean terminal state is itself part of what each node pins.
"""

from __future__ import annotations

import json
import time

from tests.flows.flowlib import (assert_step_sequence, assert_steps_include,
                                 events_of, flow_steps, status, tasks)

# -- ordinary exhaustion: iteration count runs out, tasks incomplete -------

ORDINARY_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",          # the only worker attempt the 2-iteration
                                 # budget allows; STUB_TASKS=5 leaves work
                                 # outstanding
    "state:failed",
]


def test_ordinary_budget_exhaustion_with_tasks_incomplete_fails_with_no_review(
        engine_factory):
    run = engine_factory(
        job={"run_id": "flow-budget-ordinary", "on_complete": "exit",
             "iterations": 2, "max_approaches": 1},
        stub_env={"STUB_TASKS": "5"})
    assert run.proc.wait(timeout=60) == 1, (
        "flow budget/deadline/grace (ordinary): the engine must exit "
        "non-zero once the iteration budget is spent with work outstanding")

    assert_step_sequence(flow_steps(run), ORDINARY_STEPS,
                         flow="flow budget/deadline/grace (ordinary)")

    st = status(run)
    assert st.get("state") == "failed" and st.get("verdict") == "unverified", (
        f"flow budget/deadline/grace (ordinary): expected failed/unverified, "
        f"got state={st.get('state')!r} verdict={st.get('verdict')!r}")
    assert st.get("iterationsUsed") == 2, st
    assert not st.get("graceReview"), (
        "flow budget/deadline/grace (ordinary): tasks were never all "
        f"completed, so no grace review may be granted, got {st.get('graceReview')!r}")


# -- grace review granted and VERIFIED --------------------------------------

GRACE_VERIFIED_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",          # task 1/2
    "iteration:worker",          # task 2/2 -- COMPLETE and the 3-iteration
                                 # budget both land on this same attempt
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",
    "verdict:review:verified@1",
    "signal:VERIFIED",
    "state:succeeded",
]


def test_grace_review_granted_when_the_last_task_exhausts_the_budget_and_verifies(
        engine_factory):
    run = engine_factory(
        job={"run_id": "flow-budget-grace-verified", "on_complete": "exit",
             "iterations": 3, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2"})
    assert run.proc.wait(timeout=60) == 0, (
        "flow budget/deadline/grace (grace verified): the engine must exit "
        "0 once the off-budget grace review verifies")

    assert_step_sequence(flow_steps(run), GRACE_VERIFIED_STEPS,
                         flow="flow budget/deadline/grace (grace verified)")

    st = status(run)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow budget/deadline/grace (grace verified): expected "
        f"succeeded/verified, got state={st.get('state')!r} "
        f"verdict={st.get('verdict')!r}")
    assert st.get("graceReview") is True, st
    assert "grace review" in (st.get("reason") or ""), st
    # The off-budget review got its own iteration number (4) even though it
    # never counted against the 3-iteration budget -- the job would
    # otherwise have failed at 3 with no review ever having run.
    assert st.get("iterationsUsed") == 4, (
        f"flow budget/deadline/grace (grace verified): planning + 2 workers "
        f"+ the off-budget review == 4, got "
        f"iterationsUsed={st.get('iterationsUsed')!r}")
    assert st.get("iterationsRefunded", {}).get("grace") == 1, st


# -- grace review granted but does not verify: no second attempt -----------

GRACE_REJECTED_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",
    "iteration:worker",
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",
    "verdict:review:rejected@1",
    "state:failed",
]


def test_grace_review_granted_but_unsatisfied_ends_failed_with_no_second_attempt(
        engine_factory):
    run = engine_factory(
        job={"run_id": "flow-budget-grace-rejected", "on_complete": "exit",
             "iterations": 3, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2", "STUB_REVIEW_FAILS": "1"})
    assert run.proc.wait(timeout=60) == 1, (
        "flow budget/deadline/grace (grace rejected): the engine must exit "
        "non-zero once the off-budget grace review does not verify")

    assert_step_sequence(flow_steps(run), GRACE_REJECTED_STEPS,
                         flow="flow budget/deadline/grace (grace rejected)")

    st = status(run)
    assert st.get("state") == "failed" and st.get("verdict") == "unverified", (
        f"flow budget/deadline/grace (grace rejected): expected "
        f"failed/unverified, got state={st.get('state')!r} "
        f"verdict={st.get('verdict')!r}")
    assert not st.get("graceReview"), (
        "flow budget/deadline/grace (grace rejected): the grace review did "
        f"not verify, so graceReview must not be set, got {st.get('graceReview')!r}")
    assert "grace review" in (st.get("reason") or ""), st
    # Exactly one grace attempt: no second review, no fresh approach.
    assert st.get("iterationsUsed") == 4, st
    assert st.get("approach") == 1, st


# -- the deadline, not the iteration count, ends the cycle ------------------

def test_the_wall_clock_deadline_ends_the_cycle_before_the_iteration_count_does(
        engine_factory):
    """`job_timeout_s: 1` against an `iterations` budget (50) no wall-clock
    second could ever exhaust: whichever worker attempt is running when the
    deadline fires is the run's last, and `_all_tasks_completed()` is False
    (`STUB_TASKS: 50` guarantees outstanding work at any single-digit
    iteration count) -- so this is the SAME ordinary tail-exit write the
    first node pins, reached over the deadline half of `budget_left()`
    instead of its iteration-count half."""
    run = engine_factory(
        job={"run_id": "flow-budget-deadline", "on_complete": "exit",
             "iterations": 50, "max_approaches": 1, "job_timeout_s": 1},
        stub_env={"STUB_TASKS": "50"})
    assert run.proc.wait(timeout=60) == 1, (
        "flow budget/deadline/grace (deadline): the engine must exit "
        "non-zero once the wall-clock deadline ends the cycle")

    steps = flow_steps(run)
    # Fixed prefix/suffix; however many "iteration:worker" steps land
    # between them is a race against process-spawn jitter, not the subject.
    assert_steps_include(
        steps,
        ["state:running", "phase:planning@1", "iteration:planning",
         "iteration:worker", "state:failed"],
        flow="flow budget/deadline/grace (deadline)")
    assert steps[-1] == "state:failed", steps
    assert "phase:review" not in " ".join(steps), (
        "flow budget/deadline/grace (deadline): no review slot existed and "
        f"tasks were incomplete, so no review phase may appear: {steps}")

    st = status(run)
    assert st.get("state") == "failed" and st.get("verdict") == "unverified", (
        f"flow budget/deadline/grace (deadline): expected failed/unverified, "
        f"got state={st.get('state')!r} verdict={st.get('verdict')!r}")
    assert not st.get("graceReview"), st
    budget = st.get("iterationsBudget")
    used = st.get("iterationsUsed")
    assert isinstance(budget, int) and isinstance(used, int) and used < budget, (
        "flow budget/deadline/grace (deadline): the cycle must end well "
        f"short of the 50-iteration budget -- the WALL CLOCK ended it, not "
        f"the iteration count -- got iterationsUsed={used!r} against "
        f"iterationsBudget={budget!r}")
    assert st.get("deadlineAt") and st.get("endedAt"), st
    assert st.get("deadlineAt") <= st.get("endedAt"), (
        f"flow budget/deadline/grace (deadline): the run must not end "
        f"before its own published deadline, got deadlineAt="
        f"{st.get('deadlineAt')!r} endedAt={st.get('endedAt')!r}")


# ---------------------------------------------------------------------------
# Task 021 (requirement C.10): the two nodes folded in from
# `tests/test_e2e_budget_topup.py` (deleted in the same commit, one §
# "Subsumed" row per node). Same subject as this flow -- what the iteration
# budget does to a live run -- from the other side: a job about to die of
# exhaustion is topped up MID-FLIGHT from another process (`ralphctl budget`)
# and reaches a terminal state the counterfactual node proves it could not
# otherwise reach. Moved verbatim: same `live` fixture (the registry layout
# the real `ralphctl` executable needs to address the run by id), same
# STUB_HOLD re-arm shape, same assertions; only the module and its two local
# readers changed (`flowlib.status`/`flowlib.events_of`/`flowlib.tasks`
# replace the deleted module's own `_status`/`_events` copies).
# ---------------------------------------------------------------------------

# 4 tasks -> planning + 4 worker iterations + 1 review = 6 iterations needed.
TOPUP_PLAN_TASKS = 4
TOPUP_NEEDED_ITERATIONS = 6
# Deliberately short: enough for planning + 2 workers, never enough to finish.
TOPUP_STARTING_BUDGET = 3


def _wait_for_last_budget_slot(run, timeout: float = 60.0) -> dict:
    """Block until the run has *started* its final affordable iteration
    (`iterationsUsed == TOPUP_STARTING_BUDGET`, written at iteration start),
    i.e. it is one boundary away from exhausting its budget. Fails loudly if
    the run went terminal first -- that would mean the window was missed and
    any later assertion would be meaningless."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = status(run)
        if st.get("state") in ("succeeded", "failed", "aborted"):
            raise AssertionError(
                f"run went terminal before the top-up window: {st}")
        if st.get("iterationsUsed", 0) >= TOPUP_STARTING_BUDGET:
            return st
        time.sleep(0.1)
    raise AssertionError(f"never reached the last budget slot; last: {status(run)}")


def test_run_without_topup_dies_of_budget_exhaustion(live):
    """Counterfactual for the test below: this job cannot finish on the
    budget it started with."""
    run = live(run_id="budget-control",
               job={"iterations": TOPUP_STARTING_BUDGET},
               stub_env={"STUB_TASKS": str(TOPUP_PLAN_TASKS)})
    st = run.wait_terminal()

    assert st["state"] == "failed"
    assert st["verdict"] == "unverified"
    assert st["iterationsUsed"] == TOPUP_STARTING_BUDGET
    all_tasks = tasks(run)
    assert len(all_tasks) == TOPUP_PLAN_TASKS
    assert any(t["status"] != "completed" for t in all_tasks), all_tasks


def test_topup_midflight_lets_the_job_reach_a_terminal_state_it_could_not_have(
        live, tmp_path):
    hold_file = tmp_path / "hold-budgettopup"
    run = live(run_id="budget-topup",
               job={"iterations": TOPUP_STARTING_BUDGET},
               stub_env={"STUB_TASKS": str(TOPUP_PLAN_TASKS),
                        "STUB_HOLD_FILE": str(hold_file), "STUB_HOLD_PHASE": "worker",
                        "STUB_HOLD_TIMEOUT_S": "30", "STUB_HOLD_REARM_MAX": "1"})
    run.wait_api()
    # release the first worker iteration (task 1) at once -- nothing to
    # observe there; the stub's own re-arm immediately re-blocks the SAME
    # file for the second (task 2 == overall iteration 3, the last one
    # this budget affords).
    hold_file.touch()
    _wait_for_last_budget_slot(run)

    # ...still running, one boundary from exhaustion: top up from outside.
    res = run.ralphctl("budget", run.run_id, "+10")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert (f"iteration budget: {TOPUP_STARTING_BUDGET} -> "
            f"{TOPUP_STARTING_BUDGET + 10}") in res.stdout
    assert status(run)["state"] == "running", "top-up must apply to a live run"
    hold_file.touch()  # release the held iteration; no re-arm budget left

    # The run keeps going past the original budget and finishes.
    st = run.wait_terminal(timeout=120)
    assert st["state"] == "succeeded", st
    assert st["verdict"] == "verified"
    assert st["iterationsBudget"] == TOPUP_STARTING_BUDGET + 10
    # Further iterations really happened, and the job needed them all.
    assert st["iterationsUsed"] > TOPUP_STARTING_BUDGET
    assert st["iterationsUsed"] == TOPUP_NEEDED_ITERATIONS, st
    phases = [json.loads((d / "meta.json").read_text())["phase"]
              for d in sorted((run.run_dir / "iterations").iterdir())]
    assert phases == ["planning"] + ["worker"] * TOPUP_PLAN_TASKS + ["review"]
    assert all(t["status"] == "completed" for t in tasks(run))

    # The audit trail records who changed what, mid-run.
    changes = events_of(run, "budget_changed")
    assert len(changes) == 1, changes
    change = changes[0]
    assert change["field"] == "iterations"
    assert change["previous"] == TOPUP_STARTING_BUDGET
    assert change["iterations"] == TOPUP_STARTING_BUDGET + 10
    assert change["delta"] == 10
    assert change["source"] == "api"
    assert change["iterationsUsed"] == TOPUP_STARTING_BUDGET
