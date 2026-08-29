"""Task 010 (PRD requirement H, #98): the steer hat's tool surface is
declared in exactly one place (`state.STEER_HAT_TOOLS`) and its
status-changing tool (`task-update`) routes any `status` field change
through requirement E's single applier (`state.apply_task_status_change`,
extracted from `apply_amendment_status_rulings` by this same task) --
never a hand-written `task["status"] = ...` anywhere else.

No subprocess, no `pi` stub -- pure tasks.json bookkeeping, mirroring
`test_amendment_status_applier.py`'s own discipline for the sibling
mechanism this module shares an applier with.
"""
from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import state
from ralphd.engine.state import (
    STEER_HAT_TOOLS,
    RunDir,
    apply_task_status_change,
    steer_standing_rules,
    steer_task_delete,
    steer_task_insert,
    steer_task_update,
)


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))


def _events(run: RunDir) -> list[dict]:
    path = run.root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- the tool set is declared in ONE place, by equality, not by omission ---

def test_steer_hat_tools_is_exactly_this_set_no_more_no_less():
    """The whole set, by equality (the same shape as
    `CONTROL_PLANE_ENV_VARS`'s own equality test) -- a tool added to the
    steer hat later that is not ALSO added to this exact set fails this
    assertion instead of silently expanding what the hat can do."""
    assert set(STEER_HAT_TOOLS) == {
        "task-insert", "task-update", "task-delete", "standing-rules",
    }


def test_no_budget_iteration_approach_or_deadline_tool_in_the_set():
    """PRD requirement H: budgets stay operator-only. Spelled out
    separately from the equality check above so a reviewer reading just
    this test still sees the authority boundary stated, not just implied
    by what the equality check happens to exclude today."""
    forbidden_substrings = ("budget", "iteration", "approach", "deadline")
    for name in STEER_HAT_TOOLS:
        for bad in forbidden_substrings:
            assert bad not in name, f"{name!r} looks budget/iteration/approach/deadline-shaped"


def test_every_tool_is_callable():
    for name, fn in STEER_HAT_TOOLS.items():
        assert callable(fn), f"{name!r} is not callable"


# -- task-update (the status-changing tool) routes through E's applier -----

def test_task_update_status_change_calls_apply_task_status_change(tmp_path, monkeypatch):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "030", "title": "t", "status": "pending",
         "successCriteria": "do X"},
    ])
    calls = []
    real = state.apply_task_status_change

    def spy(run_arg, task_id, target_status, vigilant, **kw):
        calls.append((run_arg, task_id, target_status, vigilant, kw))
        return real(run_arg, task_id, target_status, vigilant, **kw)

    monkeypatch.setattr(state, "apply_task_status_change", spy)

    result = STEER_HAT_TOOLS["task-update"](run, "030", {"status": "in-progress"}, False)

    assert len(calls) == 1
    call_run, call_task_id, call_target, call_vigilant, call_kw = calls[0]
    assert call_run is run
    assert call_task_id == "030"
    assert call_target == "in-progress"
    assert call_vigilant is False
    assert call_kw.get("source") == "steer"
    assert result["statusResult"]["applied"] is True
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress"


def test_task_update_illegal_status_transition_is_refused_via_the_applier(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "031", "title": "t", "status": "pending"},
    ])
    result = steer_task_update(run, "031", {"status": "validated"}, False)
    assert result["statusResult"]["applied"] is False
    assert result["statusResult"]["reason"] == "illegal transition"
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "pending"


def test_task_update_status_change_emits_the_same_audit_event_type(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "032", "title": "t", "status": "validation-failed",
         "successCriteria": "do Y", "validationAttempts": 3,
         "tierLadderExhausted": True},
    ])
    steer_task_update(run, "032", {"status": "skipped"}, False)
    events = [e for e in _events(run) if e["type"] == "amendment_status_applied"]
    assert len(events) == 1
    assert events[0]["source"] == "steer"
    assert events[0]["taskId"] == "032"
    assert events[0]["previous"] == "validation-failed"
    assert events[0]["status"] == "skipped"
    task = run.read_tasks()["tasks"][0]
    # never touched by the status-only applier
    assert task["validationAttempts"] == 3
    assert task["tierLadderExhausted"] is True
    assert task["successCriteria"] == "do Y"


def test_task_update_non_status_fields_are_plain_writes_no_applier_call(tmp_path, monkeypatch):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "033", "title": "old title", "status": "pending",
         "priority": 1},
    ])
    calls = []
    monkeypatch.setattr(state, "apply_task_status_change",
                        lambda *a, **k: calls.append((a, k)))

    result = steer_task_update(run, "033", {"title": "new title", "priority": 5}, False)

    assert calls == []  # no status key -> the applier is never consulted
    assert result["statusResult"] is None
    task = run.read_tasks()["tasks"][0]
    assert task["title"] == "new title"
    assert task["priority"] == 5
    assert task["status"] == "pending"


def test_task_update_can_change_status_and_other_fields_in_one_call(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "034", "title": "t", "status": "pending", "priority": 1},
    ])
    result = steer_task_update(run, "034", {"status": "in-progress", "priority": 9}, False)
    assert result["statusResult"]["applied"] is True
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress"
    assert task["priority"] == 9


# -- apply_task_status_change: requirement H's extraction of requirement E -

def test_apply_task_status_change_is_the_same_function_both_callers_use():
    """PRD requirement H: 'one place decides which transitions are legal
    ... two callers use it'. Assert the identity, not just the behaviour:
    `steer_task_update` and `apply_amendment_status_rulings` must literally
    call the same function object."""
    import inspect
    src = inspect.getsource(state.apply_amendment_status_rulings)
    assert "apply_task_status_change(" in src
    src2 = inspect.getsource(state.steer_task_update)
    assert "apply_task_status_change(" in src2


def test_apply_task_status_change_preserves_fields_and_no_ops_safely(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "035", "title": "t", "status": "completed",
         "validationAttempts": 4, "tierLadderExhausted": False},
    ])
    # already at target -> no-op, no event
    rec = apply_task_status_change(run, "035", "completed", False, source="steer")
    assert rec["applied"] is False
    assert rec["reason"] == "already at that status"
    assert _events(run) == []


# -- task-insert / task-delete ----------------------------------------------

def test_task_insert_lands_pending_regardless_of_a_supplied_status(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [])
    result = steer_task_insert(run, {"id": "040", "title": "new work",
                                     "successCriteria": "do it",
                                     "status": "completed"})
    assert result["inserted"] is True
    task = run.read_tasks()["tasks"][0]
    assert task["id"] == "040"
    assert task["status"] == "pending"


def test_task_insert_refuses_a_duplicate_id(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [{"id": "041", "title": "t", "status": "pending"}])
    result = steer_task_insert(run, {"id": "041", "title": "dup"})
    assert result["inserted"] is False
    assert len(run.read_tasks()["tasks"]) == 1


def test_task_delete_removes_an_unreferenced_task(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [{"id": "042", "title": "t", "status": "pending"}])
    result = steer_task_delete(run, "042")
    assert result["deleted"] is True
    assert run.read_tasks()["tasks"] == []


def test_task_delete_refuses_while_still_depended_on(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "043", "title": "t", "status": "pending"},
        {"id": "044", "title": "u", "status": "pending", "dependsOn": ["043"]},
    ])
    result = steer_task_delete(run, "043")
    assert result["deleted"] is False
    assert "still depended on" in result["reason"]
    assert len(run.read_tasks()["tasks"]) == 2


# -- standing-rules ----------------------------------------------------------

def test_standing_rules_tool_writes_through_run_dir(tmp_path):
    run = _run(tmp_path)
    result = steer_standing_rules(run, "## Standing rules\n\n- new rule\n")
    assert result["written"] is True
    assert run.read_standing_rules() == "## Standing rules\n\n- new rule\n"
