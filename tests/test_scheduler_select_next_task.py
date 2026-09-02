"""Task 115 (#60, requirement C/F): unit tests for the pure scheduler
`engine/scheduler.select_next_task` -- see that module's docstring for the
four-branch algorithm this drives. This module has no side effects and is
not yet wired into `loop.py` (task 116's job) or `worker.md`; these tests
exercise the function directly against plain task lists.

Distinct from `tests/test_scheduler.py`, which is a pre-existing black-box
test of `tests/stub-pi/pi`'s own emulation of the same pick rule via a real
engine process -- that module is untouched by this task.
"""
from __future__ import annotations

from ralphd.engine.scheduler import select_next_task


def _task(id_, status="pending", **extra):
    t = {"id": id_, "title": f"task {id_}", "status": status,
         "successCriteria": "criteria"}
    t.update(extra)
    return t


def test_validation_failed_wins_over_everything_else():
    tasks = [
        _task("001", status="pending"),
        _task("002", status="in-progress"),
        _task("003", status="validation-failed"),
        _task("004", status="validation-failed"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "003"  # first validation-failed in list order


def test_in_progress_wins_over_pending_scheduler():
    tasks = [
        _task("001", status="pending"),
        _task("002", status="in-progress"),
        _task("003", status="in-progress"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"  # first in-progress in list order


def test_no_dependson_or_priority_degenerates_to_plain_list_order():
    tasks = [_task("001"), _task("002"), _task("003")]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_dependson_excludes_a_pending_task_whose_dependency_is_unfinished():
    tasks = [
        _task("001", status="pending", dependsOn=["002"]),
        _task("002", status="pending"),
    ]
    picked = select_next_task(tasks)
    # 001 depends on 002 which is not yet completed/validated -- 002 is the
    # only viable candidate.
    assert picked["id"] == "002"


def test_dependson_excludes_a_pending_task_whose_dependency_is_dead():
    tasks = [
        _task("001", status="pending", dependsOn=["002"]),
        _task("002", status="failed"),
        _task("003", status="pending"),
    ]
    picked = select_next_task(tasks)
    # 001 can never become viable (dead dependency); 003 has none, so it's
    # the only candidate.
    assert picked["id"] == "003"


def test_dependson_satisfied_by_completed():
    tasks = [
        _task("001", status="pending", dependsOn=["002"]),
        _task("002", status="completed"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_dependson_satisfied_by_validated():
    tasks = [
        _task("001", status="pending", dependsOn=["002"]),
        _task("002", status="validated"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_dependson_not_satisfied_by_skipped():
    # skipped is terminal-dead, not satisfying -- same rule
    # state.TASK_DEPENDENCY_SATISFIED_STATUSES enforces elsewhere.
    tasks = [
        _task("001", status="pending", dependsOn=["002"]),
        _task("002", status="skipped"),
        _task("003", status="pending"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "003"


def test_highest_priority_wins_among_pending_candidates():
    tasks = [
        _task("001", status="pending", priority=1),
        _task("002", status="pending", priority=5),
        _task("003", status="pending", priority=2),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"


def test_missing_priority_defaults_to_zero():
    tasks = [
        _task("001", status="pending"),  # implicit priority 0
        _task("002", status="pending", priority=-1),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_priority_ties_break_by_list_order():
    tasks = [
        _task("001", status="pending", priority=3),
        _task("002", status="pending", priority=3),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_nothing_viable_returns_none():
    tasks = [
        _task("001", status="completed"),
        _task("002", status="failed"),
        _task("003", status="skipped"),
        _task("004", status="pending", dependsOn=["002"]),  # dead dep
    ]
    assert select_next_task(tasks) is None


def test_empty_task_list_returns_none():
    assert select_next_task([]) is None


def test_non_dict_entries_are_ignored():
    tasks = ["not a task", _task("001", status="pending")]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_task_with_no_status_key_defaults_to_pending():
    tasks = [{"id": "001", "title": "task 001", "successCriteria": "c"}]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_returns_the_same_object_not_a_copy():
    tasks = [_task("001", status="pending")]
    picked = select_next_task(tasks)
    picked["status"] = "in-progress"
    assert tasks[0]["status"] == "in-progress"


# Task 013 (C.3, #127): the validation-failed and in-progress buckets honor
# `priority` (highest wins, missing -> 0, ties by list order) the same way
# the pending-task scheduler already did -- rather than always the first
# entry in list order.


def test_validation_failed_bucket_honors_priority():
    tasks = [
        _task("001", status="validation-failed", priority=1),
        _task("002", status="validation-failed", priority=5),
        _task("003", status="validation-failed", priority=2),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"


def test_validation_failed_bucket_priority_ties_break_by_list_order():
    tasks = [
        _task("001", status="validation-failed", priority=3),
        _task("002", status="validation-failed", priority=3),
        _task("003", status="validation-failed"),  # priority 0, loses
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_in_progress_bucket_honors_priority():
    tasks = [
        _task("001", status="in-progress", priority=1),
        _task("002", status="in-progress", priority=5),
        _task("003", status="in-progress", priority=2),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"


def test_in_progress_bucket_priority_ties_break_by_list_order():
    tasks = [
        _task("001", status="in-progress"),  # implicit priority 0
        _task("002", status="in-progress", priority=-1),
        _task("003", status="in-progress"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"  # 001 and 003 tie at 0, 001 is first


def test_degenerate_no_priority_anywhere_is_unchanged():
    # No task in the whole plan carries a `priority` key at all -- every
    # bucket must still degenerate to plain first-in-list-order, exactly
    # as before this task (all-zero priorities, ties broken by position).
    tasks = [
        _task("001", status="validation-failed"),
        _task("002", status="validation-failed"),
    ]
    assert select_next_task(tasks)["id"] == "001"

    tasks = [
        _task("001", status="in-progress"),
        _task("002", status="in-progress"),
    ]
    assert select_next_task(tasks)["id"] == "001"

    tasks = [_task("001"), _task("002"), _task("003")]
    assert select_next_task(tasks)["id"] == "001"


def test_bucket_precedence_is_unchanged_even_with_priority_set():
    # validation-failed still wins over in-progress and pending regardless
    # of priority, and in-progress still wins over pending regardless of
    # priority -- priority only orders WITHIN a bucket, never across them.
    tasks = [
        _task("001", status="pending", priority=100),
        _task("002", status="in-progress", priority=1),
        _task("003", status="validation-failed", priority=1),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "003"

    tasks = [
        _task("001", status="pending", priority=100),
        _task("002", status="in-progress", priority=1),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"
