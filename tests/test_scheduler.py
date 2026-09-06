"""Black-box tests for the optional tasks.json scheduler fields
(`dependsOn`, `priority` -- PRD-adjacent task 048).

Task selection is entirely prompt-driven (the engine never parses `dependsOn`
or `priority` in code -- see docs/architecture.md); `tests/stub-pi/pi`
implements the documented pick rule exactly as worker.md specifies it, so
these tests exercise the stub's scheduler as a stand-in for what a real
agent following the prompt would do, and assert the *order* of task
completions observed live via `GET /events`' `task` events.
"""

from __future__ import annotations

import json

import pytest
from tests.conftest import STUB_PI, LiveRun  # noqa: F401


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[LiveRun] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> LiveRun:
        defaults = {"run_id": "e2e", "iterations": 12,
                    "max_approaches": 3, "on_complete": "idle"}
        e = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _completion_order(run_dir) -> list[str]:
    events = [json.loads(line) for line in
              (run_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    return [ev["taskId"] for ev in events
            if ev.get("type") == "task" and ev.get("newStatus") == "completed"]


def test_priority_preempts_list_order(engine_factory):
    """A later-listed task with higher priority is picked before earlier,
    unblocked, lower/default-priority tasks (successCriteria (a))."""
    tasks = [
        {"id": "001", "title": "low prio, first in list", "status": "pending",
         "successCriteria": "c1"},
        {"id": "002", "title": "low prio, second in list", "status": "pending",
         "successCriteria": "c2"},
        {"id": "003", "title": "high prio, last in list", "status": "pending",
         "successCriteria": "c3", "priority": 100},
    ]
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"STUB_TASKS_JSON": json.dumps(tasks)})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded" and status["verdict"] == "verified"
    final_tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert all(t["status"] == "completed" for t in final_tasks)
    # 003 (priority 100) must be completed strictly before 001/002 even
    # though it's last in list order; 001/002 tie-break by list order.
    order = _completion_order(e.run_dir)
    assert order == ["003", "001", "002"], order


def test_fieldless_schema_behaves_sequentially(engine_factory):
    """With no dependsOn/priority fields anywhere in the plan, pick order is
    identical to today's plain list order (successCriteria (b))."""
    tasks = [{"id": f"{i:03d}", "title": f"task {i}", "status": "pending",
              "successCriteria": f"c{i}"} for i in range(1, 4)]
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"STUB_TASKS_JSON": json.dumps(tasks)})
    assert e.proc.wait(timeout=60) == 0
    order = _completion_order(e.run_dir)
    assert order == ["001", "002", "003"], order


def test_blocked_by_failed_dependency_surfaced_as_a_named_fault_not_ground_against(
        engine_factory):
    """A pending task whose dependsOn names a dead (failed/skipped) task can
    never become unblocked; the worker must make progress on the other,
    viable task instead of grinding against it.

    Task 120 (#60): the planner's contract now rejects a task that smuggles
    its own `status`, so "already failed at plan time" is no longer
    reachable through STUB_TASKS_JSON -- task 001 instead becomes genuinely
    dead via a real worker `verdict: "unsatisfiable"` report
    (STUB_UNSATISFIABLE_TASK_IDS), which the harness applies as `skipped`,
    itself one of `state.TASK_DEPENDENCY_DEAD_STATUSES` -- the scheduler
    treats a `skipped` ancestor exactly as dead as a `failed` one.

    Task 007 (requirement D, #99) changed what happens once 003 completes
    and 001 is skipped: task 002 is then the ENTIRE actionable remainder,
    and it is doomed -- `_doomed_plan_fault` fires on the very next pass,
    before another worker iteration is ever dispatched, and routes straight
    to `review` instead of the OLD behaviour this test used to pin (append
    a note to notes.md, then grind through three no-progress iterations
    into a `stagnation_exhausted` verdict). The OLD assertion here was
    `"002" in notes and "001" in notes and "blocked" in notes.lower()`; it no
    longer holds -- the per-task blocked-notes mechanism (task 021) only
    fires from the `selected is None` branch that this task's fault check
    now short-circuits BEFORE reaching, for exactly this doomed-whole-
    remainder case. The stronger, new claim is checked directly on the
    fault's own EVENT payload instead of grepping a message string.
    """
    tasks = [
        {"id": "001", "title": "cannot be satisfied as written",
         "successCriteria": "c1"},
        {"id": "002", "title": "blocked forever on 001", "successCriteria": "c2",
         "dependsOn": ["001"]},
        {"id": "003", "title": "viable, unblocked", "successCriteria": "c3"},
    ]
    e = engine_factory(job={"on_complete": "idle", "max_approaches": 1},
                       stub_env={"STUB_TASKS_JSON": json.dumps(tasks),
                                 "STUB_UNSATISFIABLE_TASK_IDS": "001"})
    e.wait_api()
    # Only 003 is workable; 002 stays pending forever (blocked), 001 ends
    # skipped (dead in its own right). Once 003 is done, 002 is the whole
    # actionable remainder and it is doomed, so the fault routes straight
    # to review -- which the stub's naive reviewer verifies regardless of
    # 002's own leftover pending status (same shape as the pre-existing
    # `blocked_with_failures` route: routing to review decides nothing by
    # itself, the REVIEWER'S verdict does).
    e.wait_state(("failed", "succeeded", "aborted"), timeout=60)
    final_tasks = {t["id"]: t["status"]
                   for t in json.loads((e.run_dir / "tasks.json").read_text())["tasks"]}
    assert final_tasks["003"] == "completed"
    assert final_tasks["002"] == "pending"  # never unblocked, never ground against
    assert final_tasks["001"] == "skipped"

    events = [json.loads(line) for line in
              (e.run_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    fault_events = [ev for ev in events
                    if ev.get("type") == "log" and "doomedTaskIds" in ev]
    assert len(fault_events) == 1, fault_events
    assert fault_events[0]["doomedTaskIds"] == ["002"]
    assert fault_events[0]["deadAncestorIds"] == ["001"]
    assert fault_events[0]["planUnverifiable"] is True
    phases = [ev.get("phase") for ev in events if ev.get("type") == "phase"]
    assert "review" in phases
    # never ground through three no-progress worker iterations first -- the
    # fault fires on the very first pass with nothing selectable.
    assert phases.count("worker") <= 2, phases  # 001's report, then 003's
