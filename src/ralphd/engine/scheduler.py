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

1. Any task at `validation-failed` -- the highest `priority` wins (missing
   `priority` counts as 0), ties broken by plan (list) order.
2. Otherwise any task at `in-progress` -- the highest `priority` wins (same
   default-0/list-order tie rule; a resumed, still-open claim, picking it
   again is idempotent).
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

v0.20 task 021 (PRD requirement C.1, #156): step 3's candidate pool also
excludes any `pending` task carrying `deferredUntil` -- a worker's
`verdict: "deferred"` report moved it there deliberately, asking to be
reordered behind every runnable task. It is excluded unconditionally, the
same way a task with an unmet `dependsOn` is: never picked ahead of an
ordinary runnable task, and (for this function alone) never picked even
when it is the only `pending` task left -- resuming a deferred task once
nothing else is runnable is task 022's sleep mechanism, layered on top of
this function's `None` return, not a rule this function encodes itself.
"""
from __future__ import annotations

from typing import Any

from .state import TASK_DEPENDENCY_SATISFIED_STATUSES


def pending_candidates(tasks: list) -> list:
    """The full pool step 3 of `select_next_task` (below) considers --
    every `pending` task whose `dependsOn` entries are all satisfied and
    which does not carry `deferredUntil` -- in plan (list) order, WITHOUT
    collapsing it down to the single highest-priority pick.

    Task 002 (#278 B.1/B.4/B.7) split this pool out as its own pure
    function so a caller (`engine.loop.LoopSupervisor._step_worker`) can
    see how many genuinely runnable candidates the pending step actually
    has -- the router is only ever consulted when this list holds 2 or
    more entries -- without duplicating step 3's candidate-selection logic
    a second time. `select_next_task` itself is unaffected: it still
    returns exactly the same single deterministic pick as before this
    task, over exactly this same pool.
    """
    entries = [t for t in tasks if isinstance(t, dict)]
    status_by_id: dict[str, Any] = {
        str(t.get("id")): t.get("status", "pending")
        for t in entries if t.get("id") is not None
    }
    candidates = []
    for t in entries:
        if t.get("status", "pending") != "pending":
            continue
        # v0.20 task 021 (PRD requirement C.1, #156): a task carrying
        # `deferredUntil` (a worker's `verdict: "deferred"` report,
        # `LoopSupervisor._apply_worker_deferred_report`) has explicitly
        # asked to move BEHIND every runnable task -- excluded from the
        # ordinary candidate pool entirely, the same way an unmet
        # `dependsOn` excludes a task just below, but for a different
        # reason: it is not blocked, it was asked to wait. Whether it is
        # ever picked back up once nothing else is runnable is task 022's
        # sleep mechanism's business, not this pure function's.
        if t.get("deferredUntil"):
            continue
        raw_deps = t.get("dependsOn")
        deps = raw_deps if isinstance(raw_deps, list) else []
        unmet = [dep for dep in deps
                 if status_by_id.get(str(dep)) not in TASK_DEPENDENCY_SATISFIED_STATUSES]
        if unmet:
            continue
        candidates.append(t)
    return candidates


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

    def _highest_priority_first(bucket: list) -> Any | None:
        """Highest `priority` wins (missing -> 0); ties keep list order.
        `enumerate` gives a stable secondary key so `max()` never needs to
        fall back to comparing the dicts themselves."""
        if not bucket:
            return None
        return max(enumerate(bucket),
                    key=lambda pair: (pair[1].get("priority", 0), -pair[0]))[1]

    validation_failed = [t for t in entries
                          if t.get("status") == "validation-failed"]
    if validation_failed:
        return _highest_priority_first(validation_failed)

    in_progress = [t for t in entries if t.get("status") == "in-progress"]
    if in_progress:
        return _highest_priority_first(in_progress)

    candidates = pending_candidates(entries)
    if not candidates:
        return None

    best_priority = max(c.get("priority", 0) for c in candidates)
    for c in candidates:
        if c.get("priority", 0) == best_priority:
            return c
    return None  # unreachable: candidates is non-empty and every c has a priority
