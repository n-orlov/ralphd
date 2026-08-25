"""Task 115 (#60, requirement C/F): the pure task-selection algorithm the
harness uses to decide WHICH task a worker iteration works on.

This module is deliberately inert on its own: `select_next_task()` is a pure
function over a plain task list, with no side effects and no knowledge of
`RunState`/`write_tasks()`. Task 116 is the wiring job -- calling this from
`loop.py`'s `_step_worker` BEFORE `run_iteration("worker")` is spawned, and
persisting the `in-progress` transition through the one designated writer
(`state.RunState.write_tasks`, see `tests/test_task_state_writer.py`) -- and,
in the same commit, retiring `worker.md`'s prose that currently asks the
AGENT to run this exact algorithm itself and write `tasks.json` directly
(fact 9; the harness applies, the agent no longer touches the state file).

The algorithm mirrors, byte-for-byte in its decision structure,
`worker.md`'s documented pick rule (the "## What to do" step 2 scheduler
prose) and `tests/stub-pi/pi`'s existing emulation of it
(`_TASK_SATISFIES_DEPENDENCY`/the candidates loop in the "Role: Worker"
branch) -- this is the ONE place that logic is decided in engine code,
rather than a third independent copy in a new location:

1. Any task at `validation-failed` -- the first one in plan (list) order.
2. Otherwise any task at `in-progress` -- the first one in plan order (a
   resumed, still-open claim; picking it again is idempotent).
3. Otherwise the pending-task scheduler: consider only `pending` tasks whose
   `dependsOn` entries (if the field is present at all) are ALL satisfied --
   `state.TASK_DEPENDENCY_SATISFIED_STATUSES`, the same pair
   `task_blocked_by()` uses, so a task whose dependency merely hasn't
   finished yet (not dead) is excluded from consideration exactly like a
   task whose dependency is dead, but for a different reason: it isn't
   accepted for cheating past the ordering, not marked doomed. Among the
   remaining candidates, the highest `priority` wins (a plain number;
   missing `priority` counts as 0); ties are broken by plain list order.
4. If nothing is viable at all, `None` -- callers keep today's behaviour for
   "nothing actionable" (task 116's problem, not this module's).

A plan with no `dependsOn`/`priority` field anywhere degenerates to exactly
"the first pending task in list order", same as `worker.md`'s prose promises
and the stub's emulation already behaves.
"""
from __future__ import annotations

from typing import Any

from .state import TASK_DEPENDENCY_SATISFIED_STATUSES


def select_next_task(tasks: list) -> dict | None:
    """Pick the next task a worker iteration should work on, or `None` if
    nothing is viable. `tasks` is the plain list from `tasks.json`'s
    `{"tasks": [...]}` doc -- pass `doc.get("tasks", [])` if you have the
    whole document. Non-dict entries are ignored (never crash on a
    malformed plan); a task with no `status` key defaults to `pending`,
    the same convention `state.task_blocked_by`/`task_is_actionable` use.

    Returns the SAME dict object passed in (never a copy), so a caller that
    mutates the returned task's `status` in place and re-serialises `tasks`
    persists the change -- exactly the shape `write_tasks()` callers need.
    """
    entries = [t for t in tasks if isinstance(t, dict)]

    validation_failed = [t for t in entries
                          if t.get("status") == "validation-failed"]
    if validation_failed:
        return validation_failed[0]

    in_progress = [t for t in entries if t.get("status") == "in-progress"]
    if in_progress:
        return in_progress[0]

    status_by_id: dict[str, Any] = {
        str(t.get("id")): t.get("status", "pending")
        for t in entries if t.get("id") is not None
    }

    candidates = []
    for t in entries:
        if t.get("status", "pending") != "pending":
            continue
        raw_deps = t.get("dependsOn")
        deps = raw_deps if isinstance(raw_deps, list) else []
        unmet = [dep for dep in deps
                 if status_by_id.get(str(dep)) not in TASK_DEPENDENCY_SATISFIED_STATUSES]
        if unmet:
            continue
        candidates.append(t)

    if not candidates:
        return None

    best_priority = max(c.get("priority", 0) for c in candidates)
    for c in candidates:
        if c.get("priority", 0) == best_priority:
            return c
    return None  # unreachable: candidates is non-empty and every c has a priority
