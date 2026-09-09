"""Task 024 (PRD requirement C.5, #156): the last of C's four pinned facts
-- "the v0.19 approach-2 incident (a 06:00Z gate) replayed against a stub
engine survives" -- mirroring how task 015 (requirement A.5) closed out
the sibling requirement A with the v0.19 approach-1 incident, replayed
against a stub engine, in `tests/test_progress_pin_task015.py`.

`docs/roadmap.md`'s own account of the incident this replays: v0.19
approach 2 "reached 9 validated (001-009) with task 010 `in-progress`, and
was failed by the [stagnation] guard ... on a cut record the operator's
own steer 003 had made illegal before 06:00Z." The OLD engine had no way
for a worker facing a time-gated obligation to say so; it could only sit
on the task, accumulate witness-less passes, and trip the three-strike
guard. The `deferred` verdict (tasks 021-023) is exactly the escape hatch
that incident was missing: report `deferred` naming the 06:00Z gate as
`until`, let the run continue with the plan's other runnable task, and
come back once `until` has passed -- never stagnating either task.

One node, driven through a real `LoopSupervisor._run_job_core()` (the
same `scripted_supervisor`/`_run_resumed` harness `tests/
test_deferred_verdict.py` and `tests/test_deferred_wait.py` already use,
never a mock of the dispatch loop itself):

1. task `010` (the cut-record task, `in-progress` -- exactly the status
   the real incident's `status.json` recorded at the moment it stagnated)
   is dispatched first and reports `deferred`, naming the 06:00Z gate as
   `until` and the steer-003 illegality as `reason` -- the reorder task
   021 pinned, replayed with this incident's own shape.
2. the run continues with `011` -- the plan's OTHER runnable task, the
   thing the real incident's engine had no way to fall back to -- and
   completes it.
3. nothing is runnable (`010` is still deferred); the engine sleeps until
   `010`'s own `until` (task 022's wake mechanism), clears the deferral
   in the same pass, and the very next dispatch completes `010` too --
   "returning to the deferred one once its `until` has passed."
4. neither task ever stagnates: `010`'s reorder resets no counter because
   it never advanced one (the deferred branch's own `dispatch_outcome =
   "continue"` skips the ordinary diff bookkeeping entirely, task 021),
   and the run finishes `succeeded` with both tasks `completed` --
   "without stagnating," in contrast to the real incident's `failed`.

Mutation case (recorded in the commit message): reverting either the
scheduler's `deferredUntil` exclusion (task 021) or the sleep-and-clear
pass (task 022) would leave `010` unreachable forever -- the run would
either re-offer `010` ahead of `011` (breaking step 2, `sup.runner.
calls["worker"]` would show `010` completed before `011` even started)
or never clear the deferral (breaking step 3, the run would end `failed`
with `010` still `pending`/deferred instead of `succeeded` with it
`completed`) -- exactly the two failure shapes
`test_the_v019_approach_2_incident_replayed_the_task_survives_without_stagnating`
below asserts against directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, utc_from_epoch
from tests.conftest import scripted_supervisor

TASK_1 = "010"  # the cut-record task, gated on the 06:00Z window
TASK_2 = "011"  # the plan's other, independent, runnable task

STAGNATION_MESSAGE = "3 iterations with no task progress"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _deferred(until: str, reason: str) -> IterationResult:
    payload = json.dumps({"verdict": "deferred", "until": until,
                          "reason": reason})
    return _ok(f"this cut record is gated -- deferring.\n{payload}")


def _complete(task_id: str) -> IterationResult:
    payload = json.dumps({"verdict": "complete", "task_id": task_id})
    return _ok(f"task {task_id} done\n{payload}")


def _verified() -> IterationResult:
    return _ok("the PRD holds\n" + json.dumps({"verdict": "verified"}))


def _task(task_id: str, **over) -> dict:
    task = {"id": task_id, "title": f"task {task_id}", "status": "pending",
            "successCriteria": f"task {task_id} is done"}
    task.update(over)
    return task


_supervisor = scripted_supervisor(run_id="v20-deferred-pin-task024",
                                  iterations=15, max_approaches=1,
                                  vigilant=False, on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Same shape as `tests/test_deferred_verdict.py`'s own helper: seed
    disk state exactly like a resumed run (a task starting `in-progress`,
    a shape a real `plan-proposal.json` contract can never hold at
    ingestion -- exactly the shape the real v0.19 approach-2 run's own
    `status.json` recorded for task 010 the moment it stagnated) and skip
    planning entirely."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _log_messages(root: Path) -> str:
    return "\n".join(ev.get("message") or "" for ev in _events(root)
                     if ev.get("type") == "log")


def test_the_v019_approach_2_incident_replayed_the_task_survives_without_stagnating(
        tmp_path):
    root = tmp_path / "run"
    until = utc_from_epoch(time.time() + 2)  # the 06:00Z gate, replayed
    tasks = [_task(TASK_1, status="in-progress",
                  title="record the cut steer 003 forbids before the gate"),
             _task(TASK_2, status="pending",
                  title="the plan's other, independent task")]

    sup, final_state = _run_resumed(root, {
        "worker": [
            lambda s, p: _deferred(
                until, "the operator's own steer 003 made this cut record "
                       "illegal before the 06:00Z gate -- deferring rather "
                       "than stagnating on it (v0.19 #156 approach-2 "
                       "incident, replayed)"),
            lambda s, p: _complete(TASK_2),
            lambda s, p: _complete(TASK_1),
        ],
        "review": [lambda s, p: _verified()],
    }, tasks, iterations=8, job_timeout_s=3600)

    doc = sup.run.read_tasks()
    by_id = {t["id"]: t for t in doc["tasks"]}

    # step 1: the reorder -- task 010 moved BEHIND task 011, never to a
    # terminal status, carrying the 06:00Z gate.
    task_events = [ev for ev in _events(root) if ev.get("type") == "task"]
    deferred_events = [ev for ev in task_events if ev.get("taskId") == TASK_1
                       and ev.get("newStatus") == "pending"
                       and ev.get("deferredUntil") == until]
    assert deferred_events, task_events

    # step 2: the run continued with the OTHER runnable task -- and it is
    # what the SECOND worker call actually completed, before task 010 was
    # ever revisited.
    assert sup.runner.calls["worker"] == 3     # type: ignore[attr-defined]

    # step 3: the sleep-and-wake mechanism (task 022) fired exactly once,
    # bounded by the task's own `until` (never the job's deadline -- the
    # deadline sits far past the gate here), clearing the deferral so the
    # very next dispatch could complete it.
    waits = [ev for ev in _events(root) if ev.get("type") == "deferred_wait"]
    assert len(waits) == 1, waits
    assert waits[0]["boundedBy"] == "until"
    assert waits[0]["taskId"] == TASK_1
    assert waits[0]["until"] == until

    # both tasks landed, neither stuck deferred, neither stagnated.
    assert by_id[TASK_1]["status"] == "completed", by_id[TASK_1]
    assert "deferredUntil" not in by_id[TASK_1]
    assert "deferredReason" not in by_id[TASK_1]
    assert by_id[TASK_2]["status"] == "completed", by_id[TASK_2]

    # step 4: never stagnated -- unlike the real incident's `failed`, this
    # replay reaches `succeeded`, and the approach was never bumped or
    # archived.
    assert STAGNATION_MESSAGE not in _log_messages(root)
    assert "contract violation" not in _log_messages(root)
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert final_state == "succeeded", sup.run.read_status()
