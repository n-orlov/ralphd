"""Flow (task 017, requirement C.6): **budget exhaustion and the
deadline** -- the run-ending machinery that fires when the loop's own
worker cycle never reaches a `_step_review` on budget alone, over a real
`ralphd-engine` subprocess with the stub `pi` binary on PATH, `STUB_SLEEP=0`.

`LoopSupervisor.budget_left()` (loop.py) is a conjunction of two clocks: the
iteration count (`cfg.iterations`) and the wall-clock deadline
(`self.deadline`, published as `status.json`'s `deadlineAt`). Either one
reaching zero ends the worker cycle the same way -- `not budget_left()` in
`_step_worker`'s dispatch, landing on the plain out-of-budget `timed_out`
freeze (task 011, v0.24 requirement C.1, #169) with no review of any kind.

Task 015 (requirement E.1, #169) retired `_maybe_grace_review`, the off-
budget fork this module's name and its first two nodes used to be about:
if every task was already `completed` at the exact instant the budget ran
out, the run used to get one OFF-BUDGET review before its fate was sealed.
Requirements A and D already make the ordinary review path itself finish
before the budget/deadline cuts it short, so that fork no longer exists --
the two nodes that used to pin "grace review verified"/"grace review
rejected" are replaced below by a single pair of nodes proving the SAME
scenario (the iteration that completes the LAST task also exhausts the
budget) now holds `timed_out` with no review at all, whether or not a
review would have verified.

Four nodes, each exercising a different corner:

  * **ordinary exhaustion, tasks incomplete** -- the iteration count runs out
    with work still outstanding; no review, straight to `timed_out`
    (mirrors `tests/test_e2e.py::test_budget_exhaustion_fails_job`, read here
    through the reduced event sequence instead of iteration-directory counts);
  * **the last task exhausts the budget, no review** (two nodes, one per
    what review WOULD have said) -- the iteration that completes the LAST
    task is also the iteration that exhausts the iteration-count budget: no
    ordinary review slot exists, and (task 015/E.1) no off-budget one
    either, so the run holds `timed_out` with no review phase at all,
    regardless of `STUB_REVIEW_FAILS`;
  * **the deadline, not the iteration count, ends the cycle** -- a
    `job_timeout_s` far below what the iteration budget would allow forces
    `budget_left()` false on the wall clock, not on `cfg.iterations`, with
    tasks still incomplete. Because how
    many worker iterations fit before a wall-clock deadline fires is itself
    a race against process-spawn/scheduler jitter, this node's own worker
    count is NOT pinned exactly -- `flowlib.assert_steps_include` checks the
    fixed prefix/suffix in order instead of the whole sequence, and the
    deadline-not-iteration-count claim is pinned instead on `status.json`
    (`iterationsUsed` strictly under `iterationsBudget`, `deadlineAt`
    published with no `endedAt` at all -- task 011/C.1's `timed_out` hold
    is not a terminal outcome, so there is nothing to end).

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
    "state:timed_out",
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
    # Task 011 (v0.24 requirement C.1, #169): a plain out-of-budget ending
    # now holds `timed_out`/`verdict: null` through the same `_finish_
    # timed_out()` path task 010 established, never `failed`/`unverified`.
    assert st.get("state") == "timed_out" and st.get("verdict") is None, (
        f"flow budget/deadline/grace (ordinary): expected timed_out/None, "
        f"got state={st.get('state')!r} verdict={st.get('verdict')!r}")
    assert st.get("iterationsUsed") == 2, st
    assert not st.get("graceReview"), (
        "flow budget/deadline/grace (ordinary): `graceReview` is a dead "
        f"field now (task 015 removed the mechanism that ever set it), "
        f"got {st.get('graceReview')!r}")


# -- retired grace-review nodes: last task exhausts the budget, no review --

GRACE_RETIRED_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",          # task 1/2
    "iteration:worker",          # task 2/2 -- COMPLETE and the 3-iteration
                                 # budget both land on this same attempt
    "signal:COMPLETE",
    "state:timed_out",
]


def test_last_task_exhausting_the_budget_holds_timed_out_with_no_review(
        engine_factory):
    """RETIRED/replaced (task 015, requirement E.1, #169): this is the exact
    scenario the retired off-budget grace review existed for -- the
    iteration that completes the LAST task is also the iteration that
    exhausts the iteration-count budget, so no ordinary review slot ever
    existed. The grace review used to grant one off-budget review here
    (`_maybe_grace_review`); requirements A and D already make the
    ordinary review path finish before the budget/deadline cuts it short,
    so there is nothing left to grant -- the run now holds
    `timed_out`/`verdict: null` with NO review phase at all, the same
    plain out-of-budget freeze the first node above pins."""
    run = engine_factory(
        job={"run_id": "flow-budget-last-task-exhausts", "on_complete": "exit",
             "iterations": 3, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2"})
    assert run.proc.wait(timeout=60) == 1, (
        "flow budget/deadline/grace (retired grace node): the engine must "
        "exit non-zero once the run holds timed_out with no review")

    steps = flow_steps(run)
    assert_step_sequence(steps, GRACE_RETIRED_STEPS,
                         flow="flow budget/deadline/grace (retired grace node)")
    assert "phase:review" not in " ".join(steps), (
        "flow budget/deadline/grace (retired grace node): no review slot "
        f"existed, so no review phase may appear: {steps}")

    st = status(run)
    assert st.get("state") == "timed_out" and st.get("verdict") is None, (
        f"flow budget/deadline/grace (retired grace node): expected "
        f"timed_out/None, got state={st.get('state')!r} "
        f"verdict={st.get('verdict')!r}")
    assert not st.get("graceReview"), (
        "flow budget/deadline/grace (retired grace node): `graceReview` "
        f"is a dead field now (task 015 removed the mechanism that ever "
        f"set it), got {st.get('graceReview')!r}")
    assert st.get("iterationsUsed") == 3, st


# -- grace review granted but does not verify: retired the same way --------


def test_last_task_exhausting_the_budget_holds_timed_out_regardless_of_a_would_be_rejection(
        engine_factory):
    """RETIRED/replaced (task 015, requirement E.1, #169): the sibling of
    the node above for what used to be the grace review's OWN rejection
    outcome (`STUB_REVIEW_FAILS`). Since no review ever runs in this
    scenario anymore, whether the stub WOULD have rejected is now
    irrelevant -- the outcome is identical to the plain "grants nothing"
    node above: `timed_out`/`verdict: null`, no review phase, regardless
    of `STUB_REVIEW_FAILS`."""
    run = engine_factory(
        job={"run_id": "flow-budget-last-task-exhausts-review-fails",
             "on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={"STUB_TASKS": "2", "STUB_REVIEW_FAILS": "1"})
    assert run.proc.wait(timeout=60) == 1, (
        "flow budget/deadline/grace (retired grace node, would-be rejection): "
        "the engine must exit non-zero once the run holds timed_out with no "
        "review")

    steps = flow_steps(run)
    assert_step_sequence(
        steps, GRACE_RETIRED_STEPS,
        flow="flow budget/deadline/grace (retired grace node, would-be rejection)")
    assert "phase:review" not in " ".join(steps), (
        "flow budget/deadline/grace (retired grace node, would-be rejection): "
        f"no review slot existed, so no review phase may appear: {steps}")

    st = status(run)
    assert st.get("state") == "timed_out" and st.get("verdict") is None, (
        f"flow budget/deadline/grace (retired grace node, would-be "
        f"rejection): expected timed_out/None, got "
        f"state={st.get('state')!r} verdict={st.get('verdict')!r}")
    assert not st.get("graceReview"), st
    assert st.get("iterationsUsed") == 3, st
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
         "iteration:worker", "state:timed_out"],
        flow="flow budget/deadline/grace (deadline)")
    assert steps[-1] == "state:timed_out", steps
    assert "phase:review" not in " ".join(steps), (
        "flow budget/deadline/grace (deadline): no review slot existed and "
        f"tasks were incomplete, so no review phase may appear: {steps}")

    st = status(run)
    # Task 011 (v0.24 requirement C.1, #169): plain out-of-budget ending
    # (over the deadline half of `budget_left()`) holds `timed_out`/
    # `verdict: null`, never `failed`/`unverified`.
    assert st.get("state") == "timed_out" and st.get("verdict") is None, (
        f"flow budget/deadline/grace (deadline): expected timed_out/None, "
        f"got state={st.get('state')!r} verdict={st.get('verdict')!r}")
    assert not st.get("graceReview"), st
    budget = st.get("iterationsBudget")
    used = st.get("iterationsUsed")
    assert isinstance(budget, int) and isinstance(used, int) and used < budget, (
        "flow budget/deadline/grace (deadline): the cycle must end well "
        f"short of the 50-iteration budget -- the WALL CLOCK ended it, not "
        f"the iteration count -- got iterationsUsed={used!r} against "
        f"iterationsBudget={budget!r}")
    assert st.get("deadlineAt"), st
    # Task 010 (v0.24 requirement B.3, #169): `_finish(TIMED_OUT_STATE, ...)`
    # deliberately pops `endedAt` rather than defaulting it -- the run has
    # not ended, it is frozen exactly where it stands and immediately
    # resumable -- so there is no `endedAt` to compare the deadline
    # against here any more.
    assert st.get("endedAt") is None, st


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
    budget it started with. Task 011 (v0.24 requirement C.1, #169): a
    plain out-of-budget ending holds `timed_out`/`verdict: null` through
    the same `_finish_timed_out()` path task 010 established, never
    `failed`/`unverified`."""
    run = live(run_id="budget-control",
               job={"iterations": TOPUP_STARTING_BUDGET},
               stub_env={"STUB_TASKS": str(TOPUP_PLAN_TASKS)})
    # `wait_terminal()` only recognises the three genuinely TERMINAL
    # states -- `timed_out` (task 010, requirement B.3) is deliberately
    # NOT one of them (a resumable freeze, not an ending) -- so this is
    # `wait_state()`, the same helper `tests/test_deadline.py`'s own
    # `paused` waits already use for the same reason.
    st = run.wait_state(("timed_out",))

    assert st["state"] == "timed_out"
    assert st["verdict"] is None
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
