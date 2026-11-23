"""v0.38 B.2 / G (#320): the `verified_unchanged` park says what blocks each
unfinished task it names.

The blockers of a task are its own `dependsOn` entries that are not
satisfied: an unfinished live prerequisite (pending, in-progress,
validation-failed, awaiting-validation), a dead (`failed`/`skipped`) one, an
unsettled `petitioned` one, or an id the plan does not hold. A satisfied
dependency (`completed`, `validated`, `superseded`, a settled petitioned
task) is never listed. The structured `heldTasks[].blockedBy` and the
operator-facing `text` say the same thing.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_supervisor

HEAD = "a" * 40


def _task(tid, status, deps=None, **extra):
    t = {"id": tid, "title": f"task {tid}", "status": status,
         "successCriteria": "done"}
    if deps is not None:
        t["dependsOn"] = deps
    t.update(extra)
    return t


def _sup(tmp_path, tasks, **kw):
    sup = make_supervisor(
        tmp_path / "run", run_id="vu-blockers", cfg_defaults=False,
        max_approaches=1, vigilant=False, on_complete="exit", iterations=30,
        approach=1, tasks=tasks, **kw)
    sup._now_epoch = lambda: 1_800_000_000.0
    sup._workspace_head_sha = lambda: HEAD
    return sup


def _entries(sup):
    return {e["taskId"]: e for e in sup._verified_unchanged_entries()}


def _park(sup):
    sup._park_for_verified_unchanged(
        1, {"head": HEAD, "tasks": []}, "review")
    return sup.run.read_status()["inputQuestion"]


def test_an_unfinished_live_prerequisite_is_the_blocker(tmp_path):
    sup = _sup(tmp_path, [
        _task("001", "pending"),
        _task("002", "pending", ["001"])])
    named = _entries(sup)
    assert named["002"]["blockedBy"] == ["001"]
    assert named["001"]["blockedBy"] == []


@pytest.mark.parametrize("status", [
    "pending", "in-progress", "validation-failed", "awaiting-validation"])
def test_every_unfinished_live_status_blocks_its_dependent(tmp_path, status):
    sup = _sup(tmp_path, [
        _task("001", status),
        _task("002", "pending", ["001"])])
    assert _entries(sup)["002"]["blockedBy"] == ["001"]


def test_satisfied_dependencies_are_never_listed_as_blockers(tmp_path):
    sup = _sup(tmp_path, [
        _task("001", "completed"),
        _task("002", "validated"),
        _task("003", "superseded"),
        _task("004", "in-progress"),
        _task("005", "pending", ["001", "002", "003", "004"])])
    named = _entries(sup)
    assert named["005"]["blockedBy"] == ["004"]


def test_a_task_whose_dependencies_are_all_satisfied_is_simply_not_done(
        tmp_path):
    sup = _sup(tmp_path, [
        _task("001", "completed"),
        _task("002", "pending", ["001"])])
    question = _park(sup)
    named = {e["taskId"]: e for e in question["heldTasks"]}
    assert named["002"]["blockedBy"] == []
    assert "task 002 [pending] (task 002): blocked by nothing" in \
        question["text"]


def test_mixed_dead_petitioned_live_and_missing_dependencies_are_all_named(
        tmp_path):
    sup = _sup(tmp_path, [
        _task("001", "skipped"),
        _task("002", "petitioned"),
        _task("003", "pending"),
        _task("004", "completed"),
        _task("005", "pending", ["001", "002", "003", "004", "999"])])
    assert _entries(sup)["005"]["blockedBy"] == [
        "001", "002", "003", "999"]


def test_a_settled_petitioned_prerequisite_is_not_a_blocker(tmp_path):
    sup = _sup(tmp_path, [
        _task("001", "petitioned"),
        _task("002", "pending"),
        _task("003", "pending", ["001", "002"])])
    sup._settled_petitioned_ids = lambda doc: {"001"}
    assert _entries(sup)["003"]["blockedBy"] == ["002"]


def test_a_chain_names_each_tasks_own_unsatisfied_dependency(tmp_path):
    sup = _sup(tmp_path, [
        _task("001", "pending"),
        _task("002", "pending", ["001"]),
        _task("003", "pending", ["002"])])
    named = _entries(sup)
    assert named["002"]["blockedBy"] == ["001"]
    assert named["003"]["blockedBy"] == ["002"]


def test_the_text_and_the_structure_name_the_same_blockers(tmp_path):
    sup = _sup(tmp_path, [
        _task("001", "completed"),
        _task("002", "in-progress"),
        _task("003", "failed"),
        _task("004", "pending", ["001", "002", "003"]),
        _task("005", "pending", ["001"])])
    question = _park(sup)
    lines = question["text"].splitlines()
    for e in question["heldTasks"]:
        line = next(ln for ln in lines
                    if ln.startswith(f"  task {e['taskId']} "))
        expected = (", ".join(e["blockedBy"]) if e["blockedBy"]
                    else "nothing (the task is simply not done)")
        assert line.endswith(f"blocked by {expected}"), line
    named = {e["taskId"]: e for e in question["heldTasks"]}
    assert named["004"]["blockedBy"] == ["002", "003"]
    assert "001" not in named["004"]["blockedBy"]
    assert named["005"]["blockedBy"] == []


def test_the_verdict_gate_park_text_names_the_same_blockers(tmp_path):
    sup = _sup(tmp_path, [
        _task("001", "pending"),
        _task("002", "pending", ["001"])], peer_verdict_gate=True)
    sup._park_for_verified_unchanged(
        1, {"head": HEAD, "tasks": []}, "verdict_gate")
    question = sup.run.read_status()["inputQuestion"]
    assert "task 002 [pending] (task 002): blocked by 001" in question["text"]
