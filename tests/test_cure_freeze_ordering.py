"""Task 033 (PRD requirement E.4, #167): `insert_cure_tasks` splices a
freshly-appended cure task in immediately BEFORE the plan's own named
last-code-touching task, and a freshly-appended re-take task immediately
AFTER it -- both in finding order -- rather than always appending at the
very end of `tasks.json`'s `tasks` list. `state.last_code_touching_task_
id(run, tasks)` is the id lookup this rests on: an explicit standing-
rules override (planning.md's own practice) checked first, then a task's
own self-declaring text, `None` when the plan names no such task at all
-- the fallback case that keeps today's append-at-the-end ordering.

Four groups of nodes:

* the standing-rules override, found via `RunDir.write_standing_rules`;
* the task-self-declaring fallback, with no standing rules at all;
* several cure/re-take tasks in one call landing in finding order, not
  reversed;
* the ordering used when the plan names no such task anywhere (the
  pre-existing append-at-the-end behaviour, still exercised so this
  task's own criteria clause about it is pinned by a named node).
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine.state import (
    RunDir,
    atomic_write_json,
    insert_cure_tasks,
    last_code_touching_task_id,
)


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
          criteria: str = "n/a") -> dict:
    return {"id": tid, "title": tid, "successCriteria": criteria,
            "status": status, "origin": "planner",
            "dependsOn": list(depends or []), "tier": "normal"}


def _ids_in_order(run: RunDir) -> list:
    return [t["id"] for t in run.read_tasks()["tasks"]]


# ---------------------------------------------------------------------------
# The standing-rules override.
# ---------------------------------------------------------------------------


def test_a_cure_task_lands_before_the_standing_rules_named_freeze_task(tmp_path):
    root = tmp_path / "run"
    tasks = [_task("010"), _task("020"), _task("030", depends=["020"])]
    run = _make_run(root, tasks)
    run.write_standing_rules(
        "## Standing rules\n\n"
        "- **The freeze.** The last code-touching task is still **020** "
        "(retiring the sweep-sha machinery).\n")

    assert last_code_touching_task_id(run, tasks) == "020"

    finding = _blocking_curable_finding("requirement E.4 (#167)", "a defect")
    inserted = insert_cure_tasks(run, 1, [finding])
    assert inserted == ["cure-01-01"]

    order = _ids_in_order(run)
    assert order == ["010", "cure-01-01", "020", "030"]


def test_a_retake_task_lands_after_the_standing_rules_named_freeze_task(tmp_path):
    root = tmp_path / "run"
    tasks = [_task("010"), _task("020"), _task("030", depends=["020"])]
    run = _make_run(root, tasks)
    run.write_standing_rules(
        "- **The freeze.** The last code-touching task is still **020**.\n")

    finding = _blocking_curable_finding(
        "requirement E.4 (#167)", "a stale record",
        invalidates=["docs/testing.md"])
    inserted = insert_cure_tasks(run, 1, [finding])
    cure_id, retake_id = inserted

    order = _ids_in_order(run)
    assert order == ["010", cure_id, "020", retake_id, "030"]


def test_several_cure_and_retake_tasks_from_two_findings_stay_in_finding_order(tmp_path):
    root = tmp_path / "run"
    tasks = [_task("010"), _task("020"), _task("030")]
    run = _make_run(root, tasks)
    run.write_standing_rules(
        "The last code-touching task is still **020**.\n")

    findings = [
        _blocking_curable_finding(
            "requirement E.4a (#167)", "first defect",
            invalidates=["docs/testing.md"]),
        _blocking_curable_finding(
            "requirement E.4b (#167)", "second defect",
            invalidates=["docs/architecture.md"]),
    ]
    inserted = insert_cure_tasks(run, 1, findings)
    cure_1, retake_1, cure_2, retake_2 = inserted

    order = _ids_in_order(run)
    assert order == [
        "010", cure_1, cure_2, "020", retake_1, retake_2, "030",
    ]


# ---------------------------------------------------------------------------
# The task-self-declaring fallback (no standing rules at all).
# ---------------------------------------------------------------------------


def test_a_task_that_self_declares_the_role_is_found_with_no_standing_rules(tmp_path):
    root = tmp_path / "run"
    tasks = [
        _task("010"),
        _task("053", criteria=(
            "tests/sweep_sha_guard.py is reduced to the one lint. "
            "This is the last code-touching task unless the plan names "
            "a later one explicitly in the standing rules.")),
        _task("060", depends=["053"]),
    ]
    run = _make_run(root, tasks)

    assert last_code_touching_task_id(run, tasks) == "053"

    finding = _blocking_curable_finding("requirement E.4 (#167)", "a defect")
    inserted = insert_cure_tasks(run, 1, [finding])
    order = _ids_in_order(run)
    assert order == ["010", inserted[0], "053", "060"]


# ---------------------------------------------------------------------------
# The ordering used when the plan names no such task anywhere.
# ---------------------------------------------------------------------------


def test_with_no_named_freeze_task_cure_and_retake_tasks_append_at_the_end(tmp_path):
    root = tmp_path / "run"
    tasks = [_task("010"), _task("020")]
    run = _make_run(root, tasks)
    # No standing rules pinned, and no task's own text self-declares the
    # role -- `last_code_touching_task_id` must return `None`.
    assert last_code_touching_task_id(run, tasks) is None

    finding = _blocking_curable_finding(
        "requirement E.4 (#167)", "a defect", invalidates=["docs/testing.md"])
    inserted = insert_cure_tasks(run, 1, [finding])
    cure_id, retake_id = inserted

    order = _ids_in_order(run)
    assert order == ["010", "020", cure_id, retake_id]
