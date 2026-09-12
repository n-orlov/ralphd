"""Task 009 (PRD requirement C.4): tasks 007/008 added a real scheduling
constraint (an inserted cure named on the freeze's own `dependsOn`) so that
`scheduler.select_next_task` prefers a cure/freeze prerequisite over a
higher-`priority` dependent that hasn't cleared it yet. This module pins the
ORDINARY case that constraint must never shadow: two pending candidate
tasks that carry NO cure/freeze relationship to one another (no `dependsOn`
naming each other, neither one an inserted cure, neither one a named freeze)
still get decided by plain `priority` -- the highest `priority` wins,
exactly as `select_next_task`'s own docstring (step 3) promises, unaffected
by the C.1-C.3 dependency-gating changes.
"""

from __future__ import annotations

from ralphd.engine import scheduler


def _task(tid: str, *, status: str = "pending", depends: list | None = None,
          priority: int | None = None, criteria: str = "n/a") -> dict:
    t = {"id": tid, "title": tid, "successCriteria": criteria,
         "status": status, "origin": "planner",
         "dependsOn": list(depends or []), "tier": "normal"}
    if priority is not None:
        t["priority"] = priority
    return t


def test_C4_priority_decides_between_unrelated_candidates_with_no_cure_or_freeze_link():
    """Two independent, unrelated pending tasks -- neither depends on the
    other, neither is an inserted cure, neither is a named freeze -- and
    the higher-`priority` one is picked, list order notwithstanding.
    """
    tasks = [
        _task("101", priority=5),   # earlier in list, lower priority
        _task("102", priority=42),  # later in list, higher priority, wins
    ]

    picked = scheduler.select_next_task(tasks)
    assert picked is not None
    assert picked["id"] == "102", (
        "ordinary priority semantics must still decide between two "
        "candidates that carry no cure or freeze relationship")

    # Confirm the fixture genuinely has no cure/freeze link between the two
    # candidates: neither names the other in dependsOn.
    assert tasks[0]["dependsOn"] == []
    assert tasks[1]["dependsOn"] == []


def test_C4_tie_on_priority_falls_back_to_list_order_for_unrelated_candidates():
    """When unrelated candidates tie on priority (including the default 0
    for a task that omits the field entirely), list order breaks the tie --
    the same as `select_next_task`'s documented step 3 tie rule.
    """
    tasks = [
        _task("201"),                # no priority field -> defaults to 0
        _task("202", priority=0),    # explicit 0, ties with 201
    ]

    picked = scheduler.select_next_task(tasks)
    assert picked is not None
    assert picked["id"] == "201", "first in list order wins a priority tie"
