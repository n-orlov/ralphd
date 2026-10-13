"""Flow 4 of the twelve (task 005, requirement G.5): **the cure pass and its
variants**, over a real `ralphd-engine` subprocess with the stub `pi` binary
on PATH.

A review rejection whose BLOCKING findings are all curable is not a replan:
the engine appends a cure task per finding to THIS approach's own
`tasks.json`, spends the approach's one cure pass, works the appended tasks
and reviews the same approach again. The two variants that distinguishes are
both here, and they differ only in how many curable rejections arrive:

  * **one** -> the cure pass is taken and the SAME approach verifies; no
    approach is archived, nothing replans, and the cure pass shows up on
    `status.json`'s own `curePasses` counter (which is what makes it survive a
    resume) plus an `approach.cure_pass` event naming the inserted task ids;
  * **two** -> the second rejection finds this approach's single cure pass
    already spent, so it falls through to the ordinary edge: the approach IS
    archived and the run replans into the next approach, which verifies.

The second variant is the one that proves the cure pass is bounded to one per
approach rather than an unbounded loop -- the whole reason the counter is
persisted per approach instead of being a boolean.

A third node (task 034, #167, #171) replays a real incident from this
project's own history rather than a synthetic shape: `selfdev-v0191-a-
gate-is-not-a-wall`'s approach 1, cure pass 1 discharged two of three
blocking findings but botched the third by landing test-fix commits
*after* the sweep its own first blocking finding had already recorded --
so the second review found `tests/test_sweep_sha_guard.py` red against a
now-stale sweep, a defect the cure pass's own insertion order manufactured
rather than fixed. Task 032's `invalidates` field is the general fix (a
finding names the paths its cure makes stale; the plan's own lane-evidence
record chain is re-opened rather than left stale); this node proves it
closes the ORIGINAL incident end to end through the real engine: a curable
rejection whose cure invalidates a recorded sweep re-opens that sweep's own
launch (verification) and record tasks in the SAME cure pass, the worker
re-does all three (the cure fix, then the re-launched sweep, then its
re-record) in one pass, and the run reaches `verified` without a second
cure pass or a replan -- the exact incident, cured by construction instead
of by accident.
"""

from __future__ import annotations

import json
import subprocess

from tests.flows.flowlib import (assert_step_sequence, engine_exit_timeout_diagnostic,
                                 events_of, flow_steps, status, tasks,
                                 wait_engine_exit)


def _cure_tasks(run) -> list[dict]:
    return [t for t in tasks(run) if t.get("origin") == "review-cure"]


def test_a_curable_rejection_takes_one_cure_pass_and_the_same_approach_verifies(
        engine_factory):
    # `max_approaches: 3` deliberately leaves replan budget on the table: the
    # cure pass must be PREFERRED over a replan, not merely used when there is
    # nothing else left.
    run = engine_factory(
        job={"run_id": "flow-cure-once", "on_complete": "exit",
             "max_approaches": 3, "iterations": 30},
        stub_env={"STUB_REVIEW_CURABLE_REJECT_COUNT": "1"})
    assert wait_engine_exit(run, timeout=60) == 0, (
        "flow cure pass: the engine did not exit 0 after the cured approach "
        "verified")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",
        "cure_pass:1@1",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow cure pass")

    st = status(run)
    assert st.get("state") == "succeeded", (
        f"flow cure pass: the cured approach must verify, got state="
        f"{st.get('state')!r} verdict={st.get('verdict')!r}")
    assert st.get("curePasses") == {"1": 1}, (
        f"flow cure pass: status.json must persist exactly one cure pass against "
        f"approach 1, got {st.get('curePasses')!r}")
    assert st.get("approach") == 1, (
        f"flow cure pass: a cure pass never advances the approach, got "
        f"{st.get('approach')!r}")

    # The cure tasks landed in THIS approach's task record and were worked.
    cures = _cure_tasks(run)
    assert cures, (
        f"flow cure pass: no cure task was appended; task record is "
        f"{[(t['id'], t['status'], t.get('origin')) for t in tasks(run)]}")
    assert all(t["id"].startswith("cure-01-") for t in cures), (
        f"flow cure pass: a cure task's id must name the approach it cures, got "
        f"{[t['id'] for t in cures]}")
    assert all(t["status"] in ("completed", "validated") for t in cures), (
        f"flow cure pass: every appended cure task must be worked, got "
        f"{[(t['id'], t['status']) for t in cures]}")

    # The event names what it inserted and what it was curing.
    passes = events_of(run, "approach.cure_pass")
    assert len(passes) == 1, (
        f"flow cure pass: exactly one approach.cure_pass event is expected, got "
        f"{len(passes)}")
    ev = passes[0]
    assert ev.get("taskIds") == [t["id"] for t in cures], (
        f"flow cure pass: the event must name the task ids it inserted, got "
        f"{ev.get('taskIds')!r} against {[t['id'] for t in cures]}")
    assert ev.get("findings") and all(
        f.get("severity") == "blocking" and f.get("curable") is True
        for f in ev["findings"]), (
        f"flow cure pass: the event must carry the curable blocking findings it "
        f"cured, got {ev.get('findings')!r}")

    # No replan happened: nothing was archived and no second approach planned.
    archive = run.run_dir / "approaches"
    assert not (archive.exists() and any(archive.rglob("*"))), (
        "flow cure pass: a cure pass must never archive the approach it cures")


def test_the_second_curable_rejection_finds_the_cure_pass_spent_and_replans(
        engine_factory):
    # Task 002 (A.5, #216/#217): re-measured after task 001 added acquire_lock's
    # bounded retry (every engine this flow starts now pays that retry's
    # fast-path check on launch). Fresh wall clock, three runs, this sha:
    # whole-node ~0.8-0.9s (pytest's own "call" duration), engine-exit wall
    # clock alone (isolated by timing just this proc.wait() call) ~0.7-0.8s --
    # roughly 1.2-1.4% of the single 60 s budget below, nowhere near the 75%
    # (45 s) start of the "within 25% of budget" band. Budget is unchanged and
    # no barrier is added ahead of the wait (F.4/non-negotiable 2).
    run = engine_factory(
        job={"run_id": "flow-cure-spent", "on_complete": "exit",
             "max_approaches": 2, "iterations": 30,
             # v0.36 C.2: one cure round per approach, so the second
             # rejection finds the approach's allowance spent.
             "cure_pass_ceiling": 1},
        stub_env={"STUB_REVIEW_CURABLE_REJECT_COUNT": "2"})
    # task 017 (F.2/F.4): this specific `proc.wait(timeout=60)` -- the node
    # task 002 (A.2) measured -- keeps its bare shape deliberately. No
    # barrier ahead of it, no bound raised, and not routed through
    # `wait_engine_exit`: F.4/non-negotiable 2 forbids all three, and the
    # guard test `test_engine_exit_wait_wiring.py` asserts this exact line
    # stays this exact shape.
    #
    # task cure-01-07 (F.3/F.5, DoD 6): a bare `TimeoutExpired` here used to
    # leave a reader guessing between #216's two shapes with nothing to look
    # at. The wait itself is unchanged -- still the one bare
    # `run.proc.wait(timeout=60)` call, nothing ahead of it -- but a timeout
    # is now caught right here and turned into the SAME reusable
    # four-section diagnostic `wait_engine_exit` builds
    # (`engine_exit_timeout_diagnostic`), instead of routing the wait itself
    # through `wait_engine_exit` (still forbidden by F.4 for this one node).
    try:
        assert run.proc.wait(timeout=60) == 0, (
            "flow cure pass (spent): the engine did not exit 0 after approach 2 "
            "verified")
    except subprocess.TimeoutExpired:
        raise AssertionError(
            engine_exit_timeout_diagnostic(run, 60)) from None

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",
        # approach 1's one cure pass
        "cure_pass:1@1",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",
        # spent -- the ordinary rejected-with-an-approach-left edge
        "phase:planning@2",
        "iteration:planning",
        "iteration:worker",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@2",
        "iteration:review",
        "verdict:review:verified@2",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow cure pass (spent)")

    st = status(run)
    assert st.get("curePasses") == {"1": 1}, (
        f"flow cure pass (spent): approach 1 may spend exactly one cure pass and "
        f"approach 2 never needed one, got {st.get('curePasses')!r}")
    assert (st.get("state"), st.get("approach")) == ("succeeded", 2), (
        f"flow cure pass (spent): the run must succeed on approach 2, got state="
        f"{st.get('state')!r} approach={st.get('approach')!r}")

    assert len(events_of(run, "approach.cure_pass")) == 1, (
        "flow cure pass (spent): the second curable rejection must NOT take a "
        "second cure pass against the same approach")

    archived = sorted(p.name for p in (run.run_dir / "approaches").rglob("*"))
    assert "01" in archived and "tasks.json" in archived, (
        f"flow cure pass (spent): the un-cured approach must be archived when "
        f"the run replans, found {archived}")
    # Approach 2's live task record carries no cure task at all: the cure tasks
    # belonged to the archived approach's own plan.
    assert not _cure_tasks(run), (
        f"flow cure pass (spent): the replanned approach must start from a fresh "
        f"plan, but its task record still carries "
        f"{[t['id'] for t in _cure_tasks(run)]}")


# ---------------------------------------------------------------------------
# Task 034 (#167, #171): the v0.19.1 approach-2 incident replayed end to end
# -- a curable rejection whose cure `invalidates` the plan's own recorded
# sweep re-opens that sweep's launch and record tasks in the SAME cure pass,
# and the run reaches `verified` without a second cure pass or a replan.
# ---------------------------------------------------------------------------

SWEEP_RECORD_PATH = "artifacts/reports/v0.19.1-lane-sweep.md"


def _sweep_fixture_tasks() -> list[dict]:
    return [
        {"id": "launch-sweep", "title": "Launch the seven-lane sweep",
         "successCriteria": "the seven-lane sweep is launched and exits 0 "
                            "at the final code sha"},
        {"id": "record-sweep", "title": "Record the seven-lane sweep",
         "successCriteria": (f"One commit adds {SWEEP_RECORD_PATH} naming "
                             "the shipped sha."),
         "dependsOn": ["launch-sweep"]},
    ]


def test_a_curable_rejection_that_invalidates_a_recorded_sweep_reopens_it_and_verifies_in_one_cure_pass(
        engine_factory):
    run = engine_factory(
        job={"run_id": "flow-cure-replay-v0191-incident", "on_complete": "exit",
             "max_approaches": 3, "iterations": 30},
        stub_env={"STUB_REVIEW_CURABLE_REJECT_COUNT": "1",
                  "STUB_REVIEW_CURABLE_INVALIDATES": json.dumps([SWEEP_RECORD_PATH]),
                  "STUB_TASKS_JSON": json.dumps(_sweep_fixture_tasks())})
    assert wait_engine_exit(run, timeout=60) == 0, (
        "flow cure pass (replay v0.19.1 incident): the engine did not exit 0 "
        "after the cured approach verified")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        # the seed plan's own launch/record pair, worked in dependency order
        "iteration:worker",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:rejected@1",
        "cure_pass:1@1",
        # the cure task, then the re-opened launch/record pair, re-worked
        "iteration:worker",
        "iteration:worker",
        "iteration:worker",
        "signal:COMPLETE",
        "phase:review@1",
        "iteration:review",
        "verdict:review:verified@1",
        "signal:VERIFIED",
        "state:succeeded",
    ], flow="flow cure pass (replay v0.19.1 incident)")

    st = status(run)
    assert st.get("state") == "succeeded", (
        f"flow cure pass (replay v0.19.1 incident): the cured approach must "
        f"verify, got state={st.get('state')!r} verdict={st.get('verdict')!r}")
    assert st.get("curePasses") == {"1": 1}, (
        f"flow cure pass (replay v0.19.1 incident): exactly ONE cure pass -- "
        f"the whole point of the incident -- must be spent, got "
        f"{st.get('curePasses')!r}")

    all_tasks = {t["id"]: t for t in tasks(run)}
    cures = _cure_tasks(run)
    assert len(cures) == 1, (
        f"flow cure pass (replay v0.19.1 incident): exactly one cure task "
        f"expected, got {[t['id'] for t in cures]}")
    cure_id = cures[0]["id"]
    assert cures[0]["status"] in ("completed", "validated")

    # The sweep's own launch and record tasks were re-opened by the cure
    # pass (chained cure -> launch -> record) and re-worked to completion --
    # never left stale, the exact defect the v0.19.1 incident's own second
    # rejection found.
    launch = all_tasks["launch-sweep"]
    record = all_tasks["record-sweep"]
    assert cure_id in launch["dependsOn"], (
        f"flow cure pass (replay v0.19.1 incident): the re-opened sweep "
        f"launch must depend on the cure task, got {launch['dependsOn']!r}")
    assert record["dependsOn"] == ["launch-sweep"]
    assert launch["status"] in ("completed", "validated")
    assert record["status"] in ("completed", "validated")

    # No brand-new re-take task was appended for the invalidated path -- the
    # existing launch/record chain was re-opened instead.
    assert set(all_tasks) == {"launch-sweep", "record-sweep", cure_id}, (
        f"flow cure pass (replay v0.19.1 incident): no extra task expected, "
        f"got {sorted(all_tasks)}")

    ev = events_of(run, "approach.cure_pass")
    assert len(ev) == 1, (
        "flow cure pass (replay v0.19.1 incident): exactly one cure pass "
        "event -- the run must never take a second one to converge")
    assert set(ev[0]["taskIds"]) == {cure_id, "launch-sweep", "record-sweep"}, (
        f"flow cure pass (replay v0.19.1 incident): the cure pass event must "
        f"name the cure task plus the reopened launch/record pair, got "
        f"{ev[0]['taskIds']!r}")

    # No approach was archived: this is a cure, not a replan.
    archive = run.run_dir / "approaches"
    assert not (archive.exists() and any(archive.rglob("*"))), (
        "flow cure pass (replay v0.19.1 incident): the incident's own cure "
        "must never archive the approach it cures")
