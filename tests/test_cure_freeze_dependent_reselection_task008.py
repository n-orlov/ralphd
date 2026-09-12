"""Task 008 (PRD requirement C.3, gate `Cured when:` clause): task 007
proved the scheduler picks the inserted cure ahead of the named freeze
itself. This module proves the NEXT link in that same chain: a plan task
that depends on the freeze -- a "dependent re-take", the ordinary shape a
planner gives a task that must follow the freeze -- must not be selected
by `scheduler.select_next_task` while either its cure prerequisite OR its
freeze prerequisite is still pending, even when the dependent's own
`priority` would otherwise win the pending bucket outright.

Same harness as `tests/test_cure_freeze_scheduling_task007.py`: drive
`insert_cure_tasks` for real, then assert on `select_next_task`'s own
choice against the resulting `tasks.json`, at each of the three stages
the chain passes through.
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


def test_C3_dependent_retake_waits_on_both_cure_and_freeze(tmp_path):
    """`030` is a dependent re-take: it names the freeze (`020`) in its own
    `dependsOn`, and carries a `priority` (99) that would beat every other
    pending candidate outright if the scheduler only looked at priority.
    Walk the chain stage by stage:

    1. cure pending, freeze pending (now depending on the cure), dependent
       pending (depending on the freeze) -> the scheduler must pick the
       cure, not the high-priority dependent.
    2. cure done, freeze still pending -> the scheduler must pick the
       freeze, not the dependent -- the freeze prerequisite alone still
       blocks the dependent even though its own cure prerequisite cleared.
    3. cure done, freeze done -> only then is the dependent selectable.
    """
    root = tmp_path / "run"
    tasks = [
        _task("020", priority=10),                  # the named freeze
        _task("030", depends=["020"], priority=99),  # the dependent re-take
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
    assert by_id["020"]["dependsOn"] == ["cure-01-01"]
    assert by_id["030"]["dependsOn"] == ["020"]

    # Stage 1: cure pending, freeze pending, dependent pending. The
    # dependent's priority (99) beats both the freeze's (10) and the
    # cure's (unset, default 0) -- proving this is the dependency filter
    # at work, not a priority coincidence.
    picked = scheduler.select_next_task(live_tasks)
    assert picked is not None
    assert picked["id"] == "cure-01-01", (
        "the dependent re-take must not be picked while its cure "
        "prerequisite is still pending, whatever its own priority is")

    # Stage 2: cure completed, freeze still pending. The dependent's
    # `dependsOn` names the freeze directly and is satisfied only once
    # THAT task, not just the cure, reaches a satisfied status.
    by_id["cure-01-01"]["status"] = "completed"
    picked = scheduler.select_next_task(live_tasks)
    assert picked is not None
    assert picked["id"] == "020", (
        "the dependent re-take must not be picked while its freeze "
        "prerequisite is still pending, even after the cure is done")

    # Stage 3: cure and freeze both done -- the dependent is now the only
    # actionable pending task and is finally selectable.
    by_id["020"]["status"] = "completed"
    picked = scheduler.select_next_task(live_tasks)
    assert picked is not None
    assert picked["id"] == "030", (
        "once both the cure and the freeze prerequisites are satisfied "
        "the dependent re-take becomes selectable")
