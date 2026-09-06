"""Task 010 (PRD requirement B.1, #153): the no-progress stagnation counter
must read more than one witness of progress.

Before this task, `_step_worker`'s ordinary fallback compared only
`tasks.json` before/after (witness (a)) -- a worker iteration that made real
progress by any OTHER measure still stagnated the counter if it happened not
to touch the plan that exact pass. Two live gaps this closes:

* a worker that commits real work to the workspace, or spends the whole
  iteration reading/searching/running commands, but does not (yet) move a
  task's status, used to be scored identically to one that did nothing at
  all;
* a worker that files a structured impossibility declaration (a petition)
  riding on an otherwise-ordinary `"no_verdict"` reply used to have its own
  `stagnant = 0` reset silently overwritten a few dozen lines later by the
  SAME `tasks.json`-only formula, because nothing about `_apply_worker_
  petition_declaration`'s write touches the plan -- a genuine regression
  this module's own `test_a_petition...` case below is written to catch.

Four witnesses, now ORed together (`LoopSupervisor._step_worker`):
  (a) `tasks.json` changed (the pre-existing check);
  (b) the workspace's own git HEAD moved since the iteration started
      (`LoopSupervisor._workspace_head_sha`, read immediately around
      `_run_worker_iteration`);
  (c) the worker's contract payload was an APPLIED `complete`/
      `unsatisfiable`/petition (`contract_progress`, set at each
      `enforcement.ok` site);
  (d) the iteration's own transcript recorded at least
      `LoopSupervisor.STAGNATION_TOOL_ACTIVITY_MIN_CALLS` `tool_execution_
      start` events (`state.count_tool_calls`).

Driven the same way `tests/test_failed_task_vocabulary.py` and `tests/
test_stagnation_excludes_nonwork_faults.py` already do: a phase-dispatching
scripted runner (no subprocess, milliseconds) through `LoopSupervisor.
_run_job_core()`, with a real (but throwaway) git workspace SEPARATE from
the run directory so witness (b) can be exercised for real.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

from test_failed_task_vocabulary import (
    _ScriptedAgent,
    _events,
    _log_messages,
    _ok,
    _plan_pointer,
    _steps,
    _task,
    _write_proposal,
)

__all__ = []


# -- scaffolding: a supervisor whose workspace is NOT the run dir ----------


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


def _supervisor(root: Path, workspace: Path, handlers: dict,
                **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    kw = {"iterations": 20, "max_approaches": 1, "vigilant": True,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v019-task010", **kw),
                         RunDir(root=root), workspace)
    sup.runner = _ScriptedAgent(sup, handlers)  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup


def _run(root: Path, workspace: Path, handlers: dict, **cfg_kw):
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, workspace, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    _write_proposal(sup, [_task()])
    return _ok("planned one task\n" + _plan_pointer())


def _noop(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """An ordinary worker iteration: no tasks.json write, no commit, no
    tool-call evidence -- the shape the guard exists to catch."""
    return _ok("looked at task 001 and made no change")


def _commit_only(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """Witness (b): a real commit lands in the workspace; tasks.json is
    untouched."""
    n = sup.iterations_used
    (sup.workspace / f"f{n}.txt").write_text("x")
    _git(sup.workspace, "add", "-A")
    _git(sup.workspace, "commit", "-q", "-m", f"progress at iteration {n}")
    return _ok("made a commit but tasks.json is untouched")


def _tool_heavy(count: int):
    """Witness (d): `count` `tool_execution_start` events land in THIS
    iteration's own transcript (the file `_run_iteration_once` already
    created before dispatching to the runner); tasks.json is untouched."""
    def handler(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        n = sup.iterations_used
        path = state.iteration_output_path(sup.run.root, n)
        with open(path, "a") as f:
            for _ in range(count):
                f.write(json.dumps(
                    {"type": "tool_execution_start", "toolName": "bash",
                     "args": {"command": "true"}}) + "\n")
        return _ok("read a lot of files, made no change")
    return handler


def _make_petitions_file(workspace: Path, wave: str = "v0.19") -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{wave}-petitions.md"
    path.write_text(f"# {wave} petitions\n\nNo petition filed yet in this wave.\n")
    return path


def _petition_no_verdict(obligation: str) -> IterationResult:
    """Witness (c): a valid petition declaration riding on an otherwise
    ordinary `"no_verdict"` reply -- tasks.json is untouched."""
    payload = {"verdict": "no_verdict",
               "petition": {"obligation": obligation, "type": "environmental",
                            "evidence": "no such capability in this sandbox"}}
    return _ok(json.dumps(payload))


def _worker_iteration_count(root: Path) -> int:
    return sum(1 for ev in _events(root)
              if ev.get("type") == "iteration.end" and ev.get("phase") == "worker")


# -- direction 1: a witness other than tasks.json resets the counter -------


def test_commits_alone_do_not_increment_the_counter_and_reset_it(tmp_path):
    """Two ordinary no-op iterations (stagnant -> 2), a commit-only
    iteration (witness (b) resets to 0), two more no-ops (stagnant -> 2
    again, never 3): the guard must never fire, and all five worker
    iterations must actually run."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    # `_steps`' last handler repeats forever, so the iteration BUDGET (not
    # just the step list) has to be bounded to exactly 1 planning + 5
    # worker slots -- otherwise a 6th, repeated no-op iteration would run
    # on top of the five below and legitimately trip the guard on its own,
    # unrelated to whether the commit-only iteration reset the counter.
    sup, outcome = _run(root, workspace, {
        "planning": _plan,
        "worker": _steps(_noop, _noop, _commit_only, _noop, _noop),
    }, iterations=6)
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert _worker_iteration_count(root) == 5


def test_tool_activity_above_threshold_does_not_increment_the_counter(tmp_path):
    """The same shape as the commits case, with witness (d) (tool-call
    activity above `STAGNATION_TOOL_ACTIVITY_MIN_CALLS`) resetting the
    counter instead of a commit."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    threshold = LoopSupervisor.STAGNATION_TOOL_ACTIVITY_MIN_CALLS
    sup, outcome = _run(root, workspace, {
        "planning": _plan,
        "worker": _steps(_noop, _noop, _tool_heavy(threshold), _noop, _noop),
    }, iterations=6)
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert _worker_iteration_count(root) == 5


def test_a_petition_riding_on_no_verdict_counts_as_progress_without_a_tasks_json_change(
        tmp_path):
    """The regression this task actually fixes: a valid petition
    declaration alongside `"no_verdict"` must reset the counter (witness
    (c)) exactly like a commit or tool activity would -- NOT have its own
    `stagnant = 0` silently overwritten by the ordinary tasks.json-only
    formula a few lines later, which is what the pre-fix code did (the
    petition iteration itself would have read as the third no-progress
    iteration and failed the approach on the spot, so the two follow-up
    no-ops below would never even have run)."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_petitions_file(workspace)
    sup, outcome = _run(root, workspace, {
        "planning": _plan,
        "worker": _steps(
            _noop, _noop,
            lambda s, p="": _petition_no_verdict(
                "requirement Z (task 010 coverage fixture)"),
            _noop, _noop),
    }, iterations=6)
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert _worker_iteration_count(root) == 5


# -- direction 2: absence of every witness still stagnates ------------------


def test_none_of_the_four_witnesses_still_increments_the_counter(tmp_path):
    """The guard this must NOT weaken: three ordinary no-op iterations
    (no tasks.json change, no commit, no contract payload, no tool-call
    evidence) still trip the guard exactly as before this task, even with
    a real git workspace present and simply never touched."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    sup, outcome = _run(root, workspace, {
        "planning": _plan,
        "worker": _steps(_noop, _noop, _noop, _noop),
    })
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    assert (outcome, sup.run.read_status()["verdict"]) == ("failed", "unverified")


def test_tool_activity_below_the_threshold_still_increments_the_counter(tmp_path):
    """Witness (d) is a THRESHOLD, not "any tool call counts": one call
    below `STAGNATION_TOOL_ACTIVITY_MIN_CALLS` on every iteration must
    still stagnate exactly like zero calls would."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    below = LoopSupervisor.STAGNATION_TOOL_ACTIVITY_MIN_CALLS - 1
    assert below >= 1, "the threshold must be > 1 for this test to mean anything"
    sup, outcome = _run(root, workspace, {
        "planning": _plan,
        "worker": _steps(_tool_heavy(below), _tool_heavy(below),
                         _tool_heavy(below), _tool_heavy(below)),
    })
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    assert (outcome, sup.run.read_status()["verdict"]) == ("failed", "unverified")
