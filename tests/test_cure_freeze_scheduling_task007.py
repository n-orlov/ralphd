"""Task 007 (PRD requirements C.1, C.2, gate finding VG-003): `insert_cure_
tasks` used to splice a cure task before the plan's named freeze task by
LIST POSITION alone (task 033, requirement E.4) -- but `scheduler.select_
next_task` never consults list position for its `pending` bucket, only
`priority` (highest wins) among candidates whose `dependsOn` are all
satisfied. The gate's own evidence: a pending named freeze with `priority:
10` still gets picked by the real production scheduler even though the
inserted cure task sits earlier in `tasks.json`'s list.

The fix (see `insert_cure_tasks`'s own docstring): every cure task this
call inserts is now also added to the named freeze task's own `dependsOn`
list -- an ACTUAL scheduling constraint, not list position. Once the
freeze depends on the cure, `select_next_task`'s pending-candidate filter
(`state.TASK_DEPENDENCY_SATISFIED_STATUSES`) excludes the freeze from the
candidate pool entirely until the cure task reaches `completed`/
`validated`, regardless of the freeze's own `priority`.

This module drives `insert_cure_tasks` for real (same harness `tests/
test_cure_freeze_ordering.py` uses) and then feeds the resulting
`tasks.json` list straight into `scheduler.select_next_task` -- asserting
on the SCHEDULER's own choice, never on list order.

Red/green provenance of `test_E4_cure_precedes_high_priority_freeze_in_
schedule`, so the pre-fix result travels with the test and not only with
a commit message. Measured in a detached `git worktree` at `e1ae9d2`
(the parent of the fix commit `6cb592d`) with its own venv resolving
`/tmp/w007/src/ralphd/__init__.py`, THIS version of the file copied in:

  - pre-fix: FAILED at the scheduler assertion (the first assertion
    after `insert_cure_tasks` returns its inserted id) --
    `AssertionError: assert '020' == 'cure-01-01'`, i.e.
    `select_next_task` picked the `priority: 10` pending named freeze
    `020` over the inserted cure. 1 failed.
  - post-fix (`insert_cure_tasks` as shipped): PASSED.

The pre-fix failure quoted in `6cb592d`'s own message was measured
against the file as THAT commit shipped it, whose `dependsOn` mechanism
assertion ran BEFORE the scheduler assertion; that ordering failed
earlier, at `assert [] == ['cure-01-01']`, and never reached the
scheduler at all. `7bc0b6c` reordered the two assertions for exactly
that reason. Keep the scheduler assertion first: it is the claim the
gate finding is about.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import scheduler
from ralphd.engine.state import RunDir, atomic_write_json, insert_cure_tasks


def _blocking_curable_finding(clause: str, evidence: str, **overrides) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": evidence}
    payload.update(overrides)
    return payload


def _make_run(root: Path, existing_tasks: list) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": existing_tasks})
    return run


def _task(tid: str, *, status: str = "pending", depends: list | None = None,
          priority: int | None = None, criteria: str = "n/a") -> dict:
    t = {"id": tid, "title": tid, "successCriteria": criteria,
         "status": status, "origin": "planner",
         "dependsOn": list(depends or []), "tier": "normal"}
    if priority is not None:
        t["priority"] = priority
    return t


def test_E4_cure_precedes_high_priority_freeze_in_schedule(tmp_path):
    """The gate's own named test, reproduced against the fixed code:
    `select_next_task` returns the inserted cure while the pending named
    freeze carries `priority: 10` -- the higher priority alone does not
    let the scheduler skip past the unmet dependency now recorded on it.
    """
    root = tmp_path / "run"
    tasks = [
        _task("020", priority=10),  # the named freeze, high priority
        _task("030", depends=["020"]),
    ]
    run = _make_run(root, tasks)
    run.write_standing_rules(
        "## Standing rules\n\n"
        "- **The freeze.** The last code-touching task is still **020**.\n")

    finding = _blocking_curable_finding("requirement C.1/C.2 (#VG-003)",
                                        "a defect")
    inserted = insert_cure_tasks(run, 1, [finding])
    assert inserted == ["cure-01-01"]

    live_tasks = run.read_tasks()["tasks"]
    by_id = {t["id"]: t for t in live_tasks}

    # The claim under test FIRST, so that this node's own failure against
    # pre-fix code is the SCHEDULER's wrong pick and nothing earlier: the
    # scheduler's choice -- not list order -- prefers the cure over the
    # freeze even though the freeze's `priority` (10) beats the cure
    # task's own (default 0, unset).
    picked = scheduler.select_next_task(live_tasks)
    assert picked is not None
    assert picked["id"] == "cure-01-01", (
        "the scheduler must pick the inserted cure ahead of the pending "
        "named freeze, whatever the freeze's own priority is")

    # ...and then the mechanism that makes it so: the freeze task's own
    # `dependsOn` names the inserted cure -- not merely list position.
    assert by_id["020"]["dependsOn"] == ["cure-01-01"]


def test_the_freeze_becomes_selectable_once_its_cure_is_done(tmp_path):
    """Once the cure task the freeze now depends on reaches a status
    `TASK_DEPENDENCY_SATISFIED_STATUSES` recognises, the freeze re-enters
    the candidate pool and, being the only other pending task, is picked
    -- the dependency this task adds is a real gate, not a permanent
    block.
    """
    root = tmp_path / "run"
    tasks = [_task("020", priority=10)]
    run = _make_run(root, tasks)
    run.write_standing_rules(
        "The last code-touching task is still **020**.\n")

    finding = _blocking_curable_finding("requirement C.1/C.2 (#VG-003)",
                                        "a defect")
    insert_cure_tasks(run, 1, [finding])

    live_tasks = run.read_tasks()["tasks"]
    by_id = {t["id"]: t for t in live_tasks}
    assert scheduler.select_next_task(live_tasks)["id"] == "cure-01-01"

    by_id["cure-01-01"]["status"] = "completed"
    picked = scheduler.select_next_task(live_tasks)
    assert picked is not None and picked["id"] == "020"


def test_a_second_cure_pass_adds_a_second_dependency_not_a_duplicate(tmp_path):
    """A freeze that already depends on an earlier cure task (from a
    previous cure pass on this same approach) gets the new cure task
    appended to its `dependsOn`, never a duplicate of the first entry
    and never losing it.
    """
    root = tmp_path / "run"
    tasks = [
        _task("cure-01-01", status="completed"),
        _task("020", priority=10, depends=["cure-01-01"]),
    ]
    run = _make_run(root, tasks)
    run.write_standing_rules("The last code-touching task is still **020**.\n")

    finding = _blocking_curable_finding("requirement C.1/C.2 (#VG-003)",
                                        "a second defect")
    inserted = insert_cure_tasks(run, 1, [finding])
    assert inserted == ["cure-01-01-2"]

    live_tasks = run.read_tasks()["tasks"]
    by_id = {t["id"]: t for t in live_tasks}
    assert by_id["020"]["dependsOn"] == ["cure-01-01", "cure-01-01-2"]
