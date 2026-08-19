"""Task 002 (#15): the hardened `tasks.json` read path.

`tasks.json` is written by the agent, not the engine, so any reader can land
inside a rewrite window. These tests pin the three-way result
(absent / unparseable / parsed-empty), the bounded re-read, the last-good
fallback and its survival across an engine restart, and the property the whole
feature exists for: a reader polling a file that is being rewritten never
observes an empty task list.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ralphd.engine import state as state_mod
from ralphd.engine.state import (
    TASKS_LAST_GOOD_NAME,
    RunDir,
    read_json,
    read_tasks_doc,
)

PLAN = {
    "version": 1,
    "tasks": [
        {"id": "001", "title": "one", "status": "completed"},
        {"id": "002", "title": "two", "status": "in-progress"},
        {"id": "003", "title": "three", "status": "pending"},
    ],
}


def _write(root: Path, doc: dict) -> None:
    (root / "tasks.json").write_text(json.dumps(doc, indent=2) + "\n")


def _forget_process_cache() -> None:
    """Drop the in-memory last-good map -- what an engine restart looks like."""
    with state_mod._tasks_last_good_lock:
        state_mod._tasks_last_good.clear()


# -- the three-way distinction ---------------------------------------------

def test_absent_tasks_file_is_absent_not_stale(tmp_path):
    _forget_process_cache()
    res = read_tasks_doc(tmp_path)
    assert (res.source, res.stale, res.tasks, res.present) == ("absent", False, [], False)
    assert res.counts == {"total": 0}


def test_parsed_but_empty_plan_is_the_truth(tmp_path):
    _forget_process_cache()
    _write(tmp_path, {"version": 1, "tasks": []})
    res = read_tasks_doc(tmp_path)
    assert (res.source, res.stale, res.tasks) == ("file", False, [])
    # ... and is distinguishable from "no plan at all"
    assert res.present is True


def test_unparseable_serves_last_good_flagged_stale(tmp_path):
    _forget_process_cache()
    _write(tmp_path, PLAN)
    assert read_tasks_doc(tmp_path).source == "file"
    (tmp_path / "tasks.json").write_text('{"version": 1, "tas')
    res = read_tasks_doc(tmp_path, attempts=2, delay=0.001)
    assert res.source == "last-good"
    assert res.stale is True
    assert res.doc == PLAN
    assert [t["id"] for t in res.tasks] == ["001", "002", "003"]
    assert "JSONDecodeError" in (res.error or "")


def test_unparseable_with_no_last_good_is_unreadable_not_empty(tmp_path):
    _forget_process_cache()
    (tmp_path / "tasks.json").write_text("{{{")
    res = read_tasks_doc(tmp_path, attempts=2, delay=0.001)
    assert res.source == "unreadable"
    assert res.stale is True
    assert res.tasks == []
    # emptiness here is ignorance, and says so: present + stale, not absent
    assert res.present is True


def test_valid_json_of_the_wrong_shape_is_treated_as_unparseable(tmp_path):
    _forget_process_cache()
    _write(tmp_path, PLAN)
    read_tasks_doc(tmp_path)
    (tmp_path / "tasks.json").write_text("[1, 2, 3]")
    res = read_tasks_doc(tmp_path, attempts=1)
    assert res.source == "last-good"
    assert res.doc == PLAN


# -- bounded re-read --------------------------------------------------------

def test_bounded_reread_recovers_a_write_that_lands_mid_read(tmp_path):
    _forget_process_cache()
    (tmp_path / "tasks.json").write_text('{"version": 1, "tas')

    def finish_the_write():
        time.sleep(0.02)
        _write(tmp_path, PLAN)

    writer = threading.Thread(target=finish_the_write)
    writer.start()
    try:
        res = read_tasks_doc(tmp_path, attempts=40, delay=0.01)
    finally:
        writer.join()
    # fresh data off disk, no fallback, not stale
    assert (res.source, res.stale) == ("file", False)
    assert res.doc == PLAN


def test_happy_path_does_a_single_read(tmp_path, monkeypatch):
    _forget_process_cache()
    _write(tmp_path, PLAN)
    slept: list[float] = []
    monkeypatch.setattr(state_mod.time, "sleep", lambda s: slept.append(s))
    assert read_tasks_doc(tmp_path).source == "file"
    assert slept == []


# -- the last-good cache file ----------------------------------------------

def test_no_cache_file_is_written_on_the_happy_path(tmp_path):
    _forget_process_cache()
    _write(tmp_path, PLAN)
    for _ in range(3):
        read_tasks_doc(tmp_path)
    assert not (tmp_path / TASKS_LAST_GOOD_NAME).exists()


def test_last_good_survives_an_engine_restart(tmp_path):
    _forget_process_cache()
    _write(tmp_path, PLAN)
    read_tasks_doc(tmp_path)                      # populates the in-memory copy
    (tmp_path / "tasks.json").write_text('{"version": 1, "tas')
    assert read_tasks_doc(tmp_path, attempts=1).source == "last-good"
    cache = tmp_path / TASKS_LAST_GOOD_NAME       # persisted only now
    assert json.loads(cache.read_text()) == PLAN

    _forget_process_cache()                        # engine restart
    res = read_tasks_doc(tmp_path, attempts=1)
    assert res.source == "last-good"
    assert res.stale is True
    assert res.doc == PLAN


def test_persist_false_never_writes_into_the_run_dir(tmp_path):
    _forget_process_cache()
    _write(tmp_path, PLAN)
    read_tasks_doc(tmp_path, persist=False)
    (tmp_path / "tasks.json").write_text("nope")
    res = read_tasks_doc(tmp_path, attempts=1, persist=False)
    assert res.source == "last-good"
    assert not (tmp_path / TASKS_LAST_GOOD_NAME).exists()


# -- the property the feature exists for -----------------------------------

def test_a_poller_never_observes_an_empty_list_during_agent_rewrites(tmp_path):
    """Agent-style truncate + rewrite in a loop; every poll sees the plan."""
    _forget_process_cache()
    _write(tmp_path, PLAN)
    read_tasks_doc(tmp_path)
    stop = threading.Event()
    tasks_file = tmp_path / "tasks.json"

    def rewriter():
        while not stop.is_set():
            # non-atomic write, exactly like a naive `json.dump(open(...))`
            with open(tasks_file, "w") as fh:
                fh.write('{"version": 1, "tasks": [')
                fh.flush()
                time.sleep(0.001)
                fh.write(json.dumps(PLAN["tasks"])[1:-1])
                fh.write("]}")
            time.sleep(0.001)

    writer = threading.Thread(target=rewriter)
    writer.start()
    try:
        observations = []
        for _ in range(300):
            res = read_tasks_doc(tmp_path, attempts=3, delay=0.005)
            observations.append((res.source, len(res.tasks), res.counts["total"]))
        # Control: the pre-task-002 reader (`read_json(path, {})`) really does
        # observe the plan as empty on this exact workload -- otherwise the
        # assertions above would pass on a workload that never races.
        naive_empty = 0
        for _ in range(300):
            if not (read_json(tasks_file, {}) or {}).get("tasks"):
                naive_empty += 1
    finally:
        stop.set()
        writer.join()
    assert all(n == 3 for _, n, _ in observations), observations
    assert all(total == 3 for _, _, total in observations), observations
    assert all(src in ("file", "last-good") for src, _, _ in observations)
    assert naive_empty > 0, "the rewrite workload never raced"


# -- RunDir integration -----------------------------------------------------

def test_rundir_read_tasks_uses_the_hardened_path(tmp_path):
    _forget_process_cache()
    run = RunDir(root=tmp_path)
    _write(tmp_path, PLAN)
    assert run.read_tasks() == PLAN
    assert run.read_tasks_result().source == "file"
    run.tasks_file.write_text('{"version": 1, "tas')
    assert run.read_tasks() == PLAN               # not {}
    res = run.read_tasks_result()
    assert (res.source, res.stale) == ("last-good", True)
    assert res.counts == {"total": 3, "completed": 1, "inProgress": 1, "pending": 1}


def test_rundir_read_tasks_on_a_run_dir_with_no_plan(tmp_path):
    _forget_process_cache()
    run = RunDir(root=tmp_path)
    assert run.read_tasks() == {}
    assert run.read_tasks_result().source == "absent"
