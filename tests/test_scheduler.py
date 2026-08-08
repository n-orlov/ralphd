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
from test_e2e import STUB_PI, EngineProc  # noqa: F401


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "e2e", "iterations": 12,
                    "max_approaches": 3, "on_complete": "idle"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
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


def test_blocked_by_failed_dependency_surfaced_in_notes_not_ground_against(
        engine_factory):
    """A pending task whose dependsOn names a failed task can never become
    unblocked; the worker must note the blockage (not silently spin on it
    forever) and make progress on the other, viable task instead."""
    tasks = [
        {"id": "001", "title": "already failed", "status": "failed",
         "successCriteria": "c1"},
        {"id": "002", "title": "blocked forever on 001", "status": "pending",
         "successCriteria": "c2", "dependsOn": ["001"]},
        {"id": "003", "title": "viable, unblocked", "status": "pending",
         "successCriteria": "c3"},
    ]
    e = engine_factory(job={"on_complete": "idle", "max_approaches": 1},
                       stub_env={"STUB_TASKS_JSON": json.dumps(tasks)})
    e.wait_api()
    # Only 003 is workable; 002 stays pending forever (blocked), 001 stays
    # failed -- neither "completed" nor "all completed", so this never
    # reaches a succeeded/verified terminal state; wait for it to fail out
    # via the stagnation guard instead.
    e.wait_state(("failed", "succeeded", "aborted"), timeout=60)
    final_tasks = {t["id"]: t["status"]
                   for t in json.loads((e.run_dir / "tasks.json").read_text())["tasks"]}
    assert final_tasks["003"] == "completed"
    assert final_tasks["002"] == "pending"  # never unblocked, never ground against
    assert final_tasks["001"] == "failed"
    notes = (e.run_dir / "notes.md").read_text()
    assert "002" in notes and "001" in notes and "blocked" in notes.lower()
