"""Task 014 (PRD requirement A.4, #156): "the surface" -- the fourth of the
five #156 stages. Task 013 landed the WRITE side (`LoopSupervisor.
PROGRESS_BUDGET_STATUS_KEY`, status.json's `progressBudget: {taskId, used,
budget}`); this task is the READ side across every human-facing surface:
`ralphctl status`, `ralphctl tasks` and the hub's task row.

What is pinned here, one test (or pair) per surface, in the same shape
`tests/test_dead_dependency_blocking.py` uses for `blocked_by`:

  * `format_task_status()`'s ` (progress budget N/M)` suffix -- and that it
    is gated on the NAMED task's own status still being `in-progress`, so a
    stale record next to a finished (or a different) task never misdescribes
    either;
  * `_format_progress_budget_lines()`, the pure formatter behind `ralphctl
    status`'s new line, unit-tested the same way
    `tests/test_cli_status_summaries.py` tests its siblings;
  * `ralphctl status` and `ralphctl tasks`, both against an on-disk (dead-run)
    snapshot;
  * the engine writing `progressBudget` never changes `health` -- pinned
    against the real production call site (`LoopSupervisor._step_worker`),
    the same style `tests/test_progress_budget_task013.py` already uses.

The hub's own browser-rendered pill is covered separately by
`tests/test_browser_hub.py::test_run_detail_shows_the_progress_budget_pill_on_the_in_progress_task_row`
(browser tier, `@pytest.mark.browser`).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from test_cli_docker import Ctl, ctl, unix_sock  # noqa: F401
from test_cli_resume import _seed_run

from ralphd.cli.main import _format_progress_budget_lines
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, format_task_status

__all__ = ["ctl", "unix_sock"]

TASK_ID = "014t"


# --------------------------------------------------------------------------
# format_task_status()
# --------------------------------------------------------------------------

def test_format_task_status_appends_the_progress_budget_suffix_for_the_named_in_progress_task():
    task = {"id": TASK_ID, "status": "in-progress"}
    budget = {"taskId": TASK_ID, "used": 2, "budget": 3}
    assert format_task_status(task) == "in-progress"
    assert (format_task_status(task, progress_budget=budget)
            == "in-progress (progress budget 2/3)")
    # absent/empty budget: unchanged
    assert format_task_status(task, progress_budget=None) == "in-progress"
    assert format_task_status(task, progress_budget={}) == "in-progress"


def test_format_task_status_progress_budget_never_names_a_different_task():
    other = {"id": "other", "status": "in-progress"}
    budget = {"taskId": TASK_ID, "used": 1, "budget": 3}
    assert format_task_status(other, progress_budget=budget) == "in-progress"


def test_format_task_status_progress_budget_is_silent_once_the_task_is_no_longer_in_progress():
    """The record survives on disk after the task it named finishes (task
    013 only resets `used` on a task CHANGE, never on completion) -- a
    finished task must never be shown re-spending a budget it no longer
    holds."""
    done = {"id": TASK_ID, "status": "completed"}
    budget = {"taskId": TASK_ID, "used": 3, "budget": 3}
    assert format_task_status(done, progress_budget=budget) == "completed"


def test_format_task_status_progress_budget_composes_with_blocked_by():
    task = {"id": TASK_ID, "status": "in-progress"}
    budget = {"taskId": TASK_ID, "used": 1, "budget": 3}
    text = format_task_status(task, ["004"], budget)
    assert text == "in-progress (blocked by 004) (progress budget 1/3)"


# --------------------------------------------------------------------------
# _format_progress_budget_lines() -- ralphctl status's own pure formatter
# --------------------------------------------------------------------------

def test_progress_budget_line_renders_task_used_and_budget():
    lines = _format_progress_budget_lines(
        {"taskId": TASK_ID, "used": 2, "budget": 3})
    assert lines == ["progress budget: task 014t  2/3 iterations used"]


def test_progress_budget_line_absent_renders_nothing():
    assert _format_progress_budget_lines(None) == []
    assert _format_progress_budget_lines({}) == []
    assert _format_progress_budget_lines("not a dict") == []


# --------------------------------------------------------------------------
# ralphctl status / ralphctl tasks, on-disk (dead-run) snapshot
# --------------------------------------------------------------------------

_PLAN = {"version": 1, "tasks": [
    {"id": "001", "title": "already done", "status": "completed"},
    {"id": TASK_ID, "title": "the one spending its budget", "status": "in-progress"},
    {"id": "003", "title": "not started yet", "status": "pending"},
]}


def _seed_progress_budget_run(c: Ctl, run_id: str):
    rdir, _ = _seed_run(c, run_id)
    status = json.loads((rdir / "status.json").read_text())
    status["progressBudget"] = {"taskId": TASK_ID, "used": 2, "budget": 3}
    (rdir / "status.json").write_text(json.dumps(status))
    (rdir / "tasks.json").write_text(json.dumps(_PLAN))
    return rdir


def test_ralphctl_status_of_a_dead_run_shows_the_progress_budget_line(ctl: Ctl):
    _seed_progress_budget_run(ctl, "tst-budget-status")
    res = ctl.run("status", "tst-budget-status")
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("progress budget:")]
    assert lines == [f"progress budget: task {TASK_ID}  2/3 iterations used"], res.stdout


def test_ralphctl_status_json_of_a_dead_run_carries_the_raw_progress_budget(ctl: Ctl):
    _seed_progress_budget_run(ctl, "tst-budget-status-json")
    doc = json.loads(ctl.run("--json", "status", "tst-budget-status-json").stdout)
    assert doc["progressBudget"] == {"taskId": TASK_ID, "used": 2, "budget": 3}


def test_ralphctl_status_of_a_run_with_no_progress_budget_shows_no_line(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-no-budget-status")
    res = ctl.run("status", "tst-no-budget-status")
    assert res.returncode == 0, res.stderr
    assert "progress budget:" not in res.stdout


def test_ralphctl_tasks_of_a_dead_run_shows_the_budget_suffix_on_the_named_task(ctl: Ctl):
    _seed_progress_budget_run(ctl, "tst-budget-tasks")
    res = ctl.run("tasks", "tst-budget-tasks")
    assert res.returncode == 0, res.stderr
    lines = {ln.split("] ")[1].split(" ")[0]: ln.split("]")[0].strip("[").strip()
             for ln in res.stdout.splitlines()}
    assert lines["001"] == "completed"
    assert lines[TASK_ID] == "in-progress (progress budget 2/3)"
    assert lines["003"] == "pending"
    # aligned: every status cell padded to the same width
    widths = {len(ln.split("]")[0]) for ln in res.stdout.splitlines()}
    assert len(widths) == 1, res.stdout


def test_ralphctl_tasks_of_a_run_with_no_progress_budget_is_unchanged(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-no-budget-tasks")
    (rdir / "tasks.json").write_text(json.dumps(_PLAN))
    res = ctl.run("tasks", "tst-no-budget-tasks")
    assert res.returncode == 0, res.stderr
    assert "progress budget" not in res.stdout


# --------------------------------------------------------------------------
# health is unaffected by a progress claim (PRD requirement A.4's own
# "`health` is unaffected (progress is not infra)" clause)
# --------------------------------------------------------------------------

def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


class _ScriptedWorker:
    def __init__(self, step):
        self.step = step
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        text = self.step(self)
        self.calls += 1
        return _ok(text)


def test_a_corroborated_progress_claim_never_touches_health(tmp_path):
    """The exact production call site task 013's own tests use
    (`LoopSupervisor._step_worker`), driving a single corroborated `file`
    progress claim, and asserting `health` is `"ok"` both before AND after
    -- the PRD's own words for this requirement are "`health` is unaffected
    (progress is not infra)", and `_record_progress_budget_usage` (task 013)
    writes only the `progressBudget` key, never touching `health`/
    `infraWait` at all."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    root.mkdir(parents=True)
    workspace.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": TASK_ID, "title": "spend the budget", "status": "in-progress",
         "successCriteria": "n/a"},
    ]})
    cfg = JobConfig(run_id="task014-health", max_approaches=3, vigilant=False,
                    on_complete="exit", infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
                    iterations=1)
    sup = LoopSupervisor(cfg, run, workspace)
    sup._approach = 1
    sup._ensure_criteria_baseline()

    assert run.read_status().get("health", "ok") == "ok"

    def step(worker):
        (worker._sup.workspace / "evidence.txt").write_text("x")
        payload = {"verdict": "no_verdict", "progress": {
            "summary": "touched a file",
            "evidence": [{"kind": "file", "ref": "evidence.txt"}]}}
        return json.dumps(payload)

    worker = _ScriptedWorker(step)
    worker._sup = sup
    sup.runner = worker  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    after = run.read_status()
    assert after.get("progressBudget", {}).get("taskId") == TASK_ID, after
    assert after.get("health", "ok") == "ok", after
    assert after.get("infraWait") is None, after
