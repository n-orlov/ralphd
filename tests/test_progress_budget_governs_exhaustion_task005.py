"""Task 005 (v0.20.1 PRD requirement B.3): now that task 004 deleted the
legacy absorption-streak verify dispatch (`_record_task_absorption_streak`,
`TASK_ABSORPTION_STATUS_KEY`), what actually governs a task's EXHAUSTION --
the point past which the engine can no longer just keep dispatching worker
iterations against it forever -- is the pre-existing progress-budget /
forced-split path (task 016 of the v0.20 PRD: `LoopSupervisor.
PROGRESS_BUDGET_STATUS_KEY`, `_progress_budget_for` == `estimatedIterations
+ 2`, `_apply_forced_split_report`). Tasks 013/016/028 already pin that
mechanism's own pieces in isolation (a directly-injected `progressBudget`
status record, a unit-level `_step_worker()` call). This module drives a
single task through the REAL production pipeline -- planning, then real
worker iterations, each landing a genuine commit and a corroborated
`progress` claim -- until it spends its OWN budget for real, and asserts
that what happens next is the forced-split notice/acceptance, never the
deleted absorption hand-off, even with `task_absorption_max_iterations`
configured to a value (1) that would have forced verify on the very FIRST
of these iterations had that mechanism still been wired in.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, is_worker_origin

from tests.test_worker_hat import (
    _absorption_streak,
    _fv_log_messages,
    _fv_ok,
    _fv_task,
    _spw010_plan,
    _spw010_run,
    _steps,
    _task_record,
    _verify_ran,
    _worker_iteration_count,
)

TASK_ID = "005t"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_git_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    (workspace / "seed.txt").write_text("seed")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")


def _plan_this_task(sup, prompt: str = "") -> IterationResult:
    from tests.test_worker_hat import _write_proposal, _plan_pointer
    _write_proposal(sup, [_fv_task(
        id=TASK_ID, estimatedIterations=1,
        title="a task this run will drive to exhaustion",
        successCriteria="n/a")])
    return _fv_ok("planned one task\n" + _plan_pointer())


def _commit_with_corroborated_progress(prompts: list[str]):
    """A real commit AND a `progress` claim the engine's own fingerprint
    corroborates -- the shape task 013 established for spending the
    budget, driven here through the full worker phase (`prompts` records
    each dispatch's own prompt text so the forced-split notice's presence
    -- or absence -- on any one of them is checkable after the fact)."""

    def handler(sup, prompt: str = "") -> IterationResult:
        prompts.append(prompt)
        n = sup.iterations_used
        (sup.workspace / f"g{n}.txt").write_text("x")
        _git(sup.workspace, "add", "-A")
        _git(sup.workspace, "commit", "-q", "-m", f"progress {n}")
        payload = {"verdict": "no_verdict", "progress": {
            "summary": f"real progress at iteration {n}",
            "evidence": [{"kind": "commit", "ref": f"progress {n}"}]}}
        return _fv_ok(json.dumps(payload))

    return handler


def _accept_forced_split(prompts: list[str]):
    def handler(sup, prompt: str = "") -> IterationResult:
        prompts.append(prompt)
        payload = {"verdict": "no_verdict", "proposed_tasks": [
            {"id": f"{TASK_ID}-a", "title": "the split-off remainder",
             "successCriteria": "the remainder works on its own"}]}
        return _fv_ok(json.dumps(payload))

    return handler


def test_exhaustion_is_produced_by_the_progress_budget_forced_split_path_not_absorption(
        tmp_path):
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    prompts: list[str] = []

    # `estimatedIterations: 1` on the ingested task -> budget ==
    # `1 + 2 == 3` (`state.task_progress_budget_estimate`). Three
    # corroborated-progress worker iterations spend it EXACTLY, a fourth
    # is dispatched with it already spent, and `task_absorption_max_
    # iterations=1` is deliberately far BELOW that -- if the deleted
    # absorption streak still governed anything, three (let alone four)
    # consecutive commit-bearing iterations on one task would have forced
    # `awaiting-validation`/dispatched `verify` on the very first one.
    sup, outcome = _spw010_run(root, workspace, {
        "planning": _plan_this_task,
        "worker": _steps(
            _commit_with_corroborated_progress(prompts),
            _commit_with_corroborated_progress(prompts),
            _commit_with_corroborated_progress(prompts),
            _accept_forced_split(prompts)),
    }, iterations=5, vigilant=True, task_absorption_max_iterations=1)

    # `_steps` repeats its LAST handler forever, but the iteration budget
    # above is bounded to exactly 1 planning + 4 worker slots, so a 5th
    # worker dispatch (which would re-run `_accept_forced_split` on an
    # already-superseded parent) never happens -- confirm exactly four
    # worker dispatches ran, no more, no fewer, before trusting any
    # assertion keyed to "the fourth prompt".
    assert _worker_iteration_count(root) == 4, _worker_iteration_count(root)
    assert len(prompts) == 4, prompts

    # 1. the budget-exhausted notice is ABSENT from the first three
    #    dispatches (still under budget) and PRESENT on the fourth (the
    #    one dispatched once `used >= budget`) -- "presence is the signal"
    #    (task 016's own discipline), checked here against REAL dispatch
    #    prompts, not an injected status record.
    for p in prompts[:3]:
        assert "Progress budget exhausted" not in p, p
    assert "Progress budget exhausted" in prompts[3], prompts[3]
    assert TASK_ID in prompts[3]
    assert "3/3" in prompts[3]

    # 2. the persisted budget record really did reach exactly `used ==
    #    budget == 3` for this task, from real corroborated claims, not a
    #    directly-injected fixture.
    budget_record = sup.run.read_status().get("progressBudget")
    assert budget_record == {"taskId": TASK_ID, "used": 3, "budget": 3}, \
        budget_record

    # 3. the outcome the exhaustion actually produced: the forced-split
    #    path accepted the fourth iteration's `proposed_tasks`, superseded
    #    the parent and made the child schedulable -- never the deleted
    #    absorption hand-off's `awaiting-validation`/forced `verify`.
    task = _task_record(root, TASK_ID)
    assert task["status"] == "superseded", task
    data = sup.run.read_tasks()
    child = next(t for t in data["tasks"] if t["id"] == f"{TASK_ID}-a")
    assert child["status"] == "pending", child
    assert child["splitFrom"] == TASK_ID, child
    assert is_worker_origin(child), child

    # 4. the absorption threshold was genuinely uninvolved: `verify` never
    #    ran, the legacy streak status key never accumulated past `{}`,
    #    and the task's own notes never grew the retired hand-off sentence
    #    -- despite `task_absorption_max_iterations=1` sitting far below
    #    the three consecutive commit-bearing iterations actually driven.
    assert _verify_ran(root) is False
    assert _absorption_streak(root) == {}
    assert "task 011" not in (task.get("notes") or "")
    assert "absorbed 3 consecutive worker iterations" not in _fv_log_messages(root)
