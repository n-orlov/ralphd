"""Task 018 (#77/#78): the completion gate and the task fraction each had
one spelling of "done" hardcoded to the literal `completed` status, so a
fully-validated VIGILANT run (whose terminal status is `validated`, never
`completed`) could neither reach the grace-review gate nor render its
progress fraction correctly -- `format_task_fraction` read `0/17` for a
plan that was actually all done, and `format_task_counts` double-counted
`validated`/`skipped` once in the numerator's shadow and once again in the
parenthesised remainder.

Fix: both readers key off `state.TASK_COMPLETION_STATUSES` (`completed`,
`validated`, `skipped`) instead of the bare string, and the remainder list
in `format_task_counts` skips whatever the numerator already counted.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    RunDir,
    format_task_counts,
    format_task_fraction,
    task_counts,
)
from tests.conftest import supervisor_factory


# The shared harness (tests/conftest.py, G.1).
_supervisor = supervisor_factory(run_id="unit")



def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))


# ---------------------------------------------------------------------------
# _all_tasks_completed: vigilant terminal statuses reach the gate
# ---------------------------------------------------------------------------

def test_all_tasks_completed_true_for_an_all_validated_vigilant_plan(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": str(i), "status": "validated"} for i in range(1, 18)
    ])
    assert sup._all_tasks_completed() is True


def test_all_tasks_completed_false_for_a_plan_with_a_pending_task(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "status": "validated"},
        {"id": "2", "status": "pending"},
    ])
    assert sup._all_tasks_completed() is False


def test_all_tasks_completed_true_for_a_mixed_terminal_plan(tmp_path):
    """completed/validated/skipped all count -- no run has to be
    exclusively one vigilant-ness to reach the gate."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "status": "completed"},
        {"id": "2", "status": "validated"},
        {"id": "3", "status": "skipped"},
    ])
    assert sup._all_tasks_completed() is True


# ---------------------------------------------------------------------------
# format_task_fraction: numerator sums every TASK_COMPLETION_STATUSES key
# ---------------------------------------------------------------------------

def test_fraction_reads_full_for_an_all_validated_seventeen_task_plan():
    tasks = [{"id": str(i), "status": "validated"} for i in range(1, 18)]
    counts = task_counts(tasks)
    assert format_task_fraction(counts) == "17/17"


def test_fraction_sums_completed_validated_and_skipped():
    counts = {"total": 5, "completed": 2, "validated": 2, "skipped": 1}
    assert format_task_fraction(counts) == "5/5"


# ---------------------------------------------------------------------------
# format_task_counts: parenthesised remainder never double-counts
# ---------------------------------------------------------------------------

def test_counts_sentence_for_an_all_validated_plan_has_no_remainder():
    tasks = [{"id": str(i), "status": "validated"} for i in range(1, 18)]
    counts = task_counts(tasks)
    assert format_task_counts(counts) == "17/17 completed"


def test_counts_sentence_parenthesised_values_add_up_to_total():
    tasks = (
        [{"id": f"c{i}", "status": "completed"} for i in range(3)]
        + [{"id": f"v{i}", "status": "validated"} for i in range(2)]
        + [{"id": f"s{i}", "status": "skipped"} for i in range(1)]
        + [{"id": "p", "status": "pending"}]
        + [{"id": "ip", "status": "in-progress"}]
    )
    counts = task_counts(tasks)
    total = counts["total"]
    rendered = format_task_counts(counts)
    numerator, _, rest = rendered.partition("/")
    assert int(numerator) == 6  # 3 completed + 2 validated + 1 skipped
    paren_total = int(numerator)
    if "(" in rest:
        inside = rest[rest.index("(") + 1: rest.index(")")]
        for piece in inside.split(", "):
            paren_total += int(piece.split(" ", 1)[0])
    assert paren_total == total
