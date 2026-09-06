"""Task 011 (PRD requirement B.2, #153): "a task that absorbs iterations is
judged, not starved."

`stagnant` (task 010, requirement B.1) only measures the OPPOSITE failure --
an iteration with none of its four progress witnesses. A task a worker keeps
landing real commits against, pass after pass, never trips it: commits are
progress by witness (b), every single time -- so before this task, such a
task could hold the whole run's attention forever with no verifier ever
judging the tree, and nothing would ever call that out.

This closes that gap with a SEPARATE counter
(`LoopSupervisor._record_task_absorption_streak`,
`status.json["taskAbsorptionStreak"]`): consecutive worker iterations spent
on the SAME task, each landing a commit but no completion signal, since the
last one that did not. Reaching `JobConfig.task_absorption_max_iterations`
(default 3, overridable with RALPHD_TASK_ABSORPTION_MAX_ITERATIONS) writes
`awaiting-validation` directly, appends a `notes` entry on the task record
naming the hand-off, and routes to `verify` in the SAME pass -- exactly as
an ordinary worker completion would.

Driven the same way `tests/test_stagnation_progress_witnesses_task010.py`
already does: a phase-dispatching scripted runner (no subprocess,
milliseconds) through `LoopSupervisor._run_job_core()`, with a real (but
throwaway) git workspace separate from the run directory so the commit
witness is exercised for real.
"""

from __future__ import annotations

import json

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult

from test_failed_task_vocabulary import (
    TASK_ID,
    _ok,
    _review_verified,
    _steps,
    _verify_pass,
)
from test_stagnation_progress_witnesses_task010 import (
    _commit_only,
    _events,
    _init_git_workspace,
    _log_messages,
    _plan,
    _run,
    _worker_iteration_count,
)

__all__ = []


def _plan_complete_if_idle(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """The scripted twin of a real worker's own resolution once the task the
    hand-off already resolved leaves nothing else selectable: worker.md
    instructs exactly this claim (`verdict: "plan_complete"`) once the
    prompt itself says nothing is assigned."""
    if "No task is currently selectable" in prompt:
        return _ok(json.dumps({"verdict": "plan_complete"}))
    return _ok("nothing to do yet")


def _absorption_streak(root) -> dict:
    from ralphd.engine.state import RunDir
    return RunDir(root=root).read_status().get("taskAbsorptionStreak") or {}


def _verify_ran(root) -> bool:
    return any(ev.get("phase") == "verify" for ev in _events(root)
              if ev.get("type") == "iteration.start")


def _task_record(root, task_id: str = TASK_ID) -> dict:
    from ralphd.engine.state import RunDir
    doc = RunDir(root=root).read_tasks()
    return next(t for t in doc.get("tasks", []) if t.get("id") == task_id)


# -- the config knob ---------------------------------------------------------


def test_default_config_knob_value_is_three():
    assert JobConfig().task_absorption_max_iterations == 3


def test_env_var_overrides_the_config_knob(tmp_path, monkeypatch):
    monkeypatch.setenv("RALPHD_TASK_ABSORPTION_MAX_ITERATIONS", "7")
    cfg = JobConfig.load(tmp_path / "nonexistent-job.yaml")
    assert cfg.task_absorption_max_iterations == 7


# -- the hand-off itself ------------------------------------------------------


def test_three_consecutive_commit_only_iterations_hand_the_task_to_verify(
        tmp_path):
    """The main case: three consecutive worker iterations on the one task,
    each landing a real commit and nothing else, never a completion signal.
    The engine takes the claim over itself: `awaiting-validation`, a `notes`
    entry naming the hand-off, `verify` runs in the same pass (a passing
    verdict here, so the task lands `validated`), and the plan finishes once
    the worker notices nothing is left to claim."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    sup, outcome = _run(root, workspace, {
        "planning": _plan,
        "worker": _steps(_commit_only, _commit_only, _commit_only,
                         _plan_complete_if_idle),
        "verify": _verify_pass,
        "review": _review_verified,
    })
    status = sup.run.read_status()
    assert (outcome, status["verdict"]) == ("succeeded", "verified"), status
    assert _verify_ran(root)
    assert _worker_iteration_count(root) == 4
    task = _task_record(root)
    assert task["status"] == "validated"
    assert "task 011" in (task.get("notes") or "")
    assert "awaiting-validation" in (task.get("notes") or "")
    msgs = _log_messages(root)
    assert "absorbed 3 consecutive worker iterations" in msgs, msgs
    # the streak record is cleared the moment the hand-off fires
    assert _absorption_streak(root) == {}


def test_a_lower_configured_threshold_fires_sooner(tmp_path):
    """The config knob genuinely changes behaviour, not just its own
    default: with the threshold configured to 2, the SAME shape hands the
    task off after only two consecutive commit-only iterations."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    sup, outcome = _run(root, workspace, {
        "planning": _plan,
        "worker": _steps(_commit_only, _commit_only, _plan_complete_if_idle),
        "verify": _verify_pass,
        "review": _review_verified,
    }, task_absorption_max_iterations=2)
    status = sup.run.read_status()
    assert (outcome, status["verdict"]) == ("succeeded", "verified"), status
    assert _worker_iteration_count(root) == 3
    task = _task_record(root)
    assert task["status"] == "validated"


def test_a_broken_streak_resets_the_counter_and_needs_a_fresh_run(tmp_path):
    """A non-commit iteration between two commit-only ones breaks the
    streak -- the two commits before it and the ones after it must never be
    added together. With the default threshold (3), a lone break followed
    by only two more commits must NOT trigger the hand-off."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    def _noop_touches_nothing(sup, prompt: str = "") -> IterationResult:
        return _ok("looked around, changed nothing")

    sup, outcome = _run(root, workspace, {
        "planning": _plan,
        "worker": _steps(_commit_only, _noop_touches_nothing,
                         _commit_only, _commit_only),
    }, iterations=5)
    task = _task_record(root)
    # the streak broke at iteration 2 -- only two consecutive commit-only
    # iterations follow it, never reaching the default threshold of 3.
    assert task["status"] == "in-progress"
    assert "task 011" not in (task.get("notes") or "")
    assert _absorption_streak(root) == {"taskId": TASK_ID, "count": 2}
