"""Task 028 (PRD requirement D.4, #157): "the estimate feeds A.3's budget;
`ralphctl tasks` shows `est` per task." Tasks 013/014 built the LIVE usage
side (`progressBudget`, ` (progress budget N/M)`, scoped to whichever ONE
task is currently in-progress). This task is the STATIC side: every task's
own `estimatedIterations + 2` figure -- default 3 -- named once
(`state.task_progress_budget_estimate`) and displayed on EVERY row of
`ralphctl tasks`, regardless of that task's status.

Pinned here, mirroring `tests/test_progress_budget_surface_task014.py`'s
own shape:

  * `state.task_progress_budget_estimate()` for an explicit estimate and
    for the default (absent field, non-numeric field, non-dict task);
  * `LoopSupervisor._progress_budget_for()` still agrees with it exactly
    (the runtime spend and the display-only reader share one source);
  * `ralphctl tasks`' new ` (est N)` column, against an on-disk (dead-run)
    snapshot, for both an explicit estimate and the default.
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock  # noqa: F401
from test_cli_resume import _seed_run

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    DEFAULT_TASK_PROGRESS_BUDGET_ESTIMATE,
    RunDir,
    task_progress_budget_estimate,
)

__all__ = ["ctl", "unix_sock"]


# --------------------------------------------------------------------------
# state.task_progress_budget_estimate()
# --------------------------------------------------------------------------

def test_explicit_estimate_derives_estimate_plus_two():
    assert task_progress_budget_estimate({"estimatedIterations": 5}) == 7
    assert task_progress_budget_estimate({"estimatedIterations": 1}) == 3


def test_missing_estimate_defaults_to_three():
    assert task_progress_budget_estimate({"id": "001"}) == 3
    assert DEFAULT_TASK_PROGRESS_BUDGET_ESTIMATE == 3


def test_non_numeric_estimate_and_non_dict_task_both_default_to_three():
    assert task_progress_budget_estimate({"estimatedIterations": "x"}) == 3
    assert task_progress_budget_estimate(None) == 3
    assert task_progress_budget_estimate("not a dict") == 3


# --------------------------------------------------------------------------
# LoopSupervisor._progress_budget_for() agrees with the shared reader
# --------------------------------------------------------------------------

def test_loop_supervisors_own_budget_method_matches_the_shared_reader(tmp_path):
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    root.mkdir(parents=True)
    workspace.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    cfg = JobConfig(run_id="task028-budget-match", max_approaches=3,
                    vigilant=False, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, workspace)

    explicit = {"id": "001", "estimatedIterations": 4}
    absent = {"id": "002"}
    assert sup._progress_budget_for(explicit) == task_progress_budget_estimate(explicit) == 6
    assert sup._progress_budget_for(absent) == task_progress_budget_estimate(absent) == 3
    assert sup._progress_budget_for(None) == task_progress_budget_estimate(None) == 3


# --------------------------------------------------------------------------
# ralphctl tasks' ` (est N)` column, on-disk (dead-run) snapshot
# --------------------------------------------------------------------------

_PLAN = {"version": 1, "tasks": [
    {"id": "001", "title": "an explicit estimate", "status": "completed",
     "estimatedIterations": 5},
    {"id": "002", "title": "no estimate annotated at all", "status": "pending"},
    {"id": "003", "title": "in progress with an explicit estimate",
     "status": "in-progress", "estimatedIterations": 1},
]}


def _est_lines(stdout: str) -> dict[str, str]:
    out = {}
    for ln in stdout.splitlines():
        if not ln.startswith("["):
            continue
        rest = ln.split("] ", 1)[1]
        tid = rest.split(" ", 1)[0]
        out[tid] = ln
    return out


def test_ralphctl_tasks_shows_est_for_an_explicit_estimate(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-est-explicit")
    (rdir / "tasks.json").write_text(json.dumps(_PLAN))
    res = ctl.run("tasks", "tst-est-explicit")
    assert res.returncode == 0, res.stderr
    lines = _est_lines(res.stdout)
    assert lines["001"].endswith("(est 7)"), res.stdout


def test_ralphctl_tasks_shows_est_defaulting_to_three_when_absent(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-est-default")
    (rdir / "tasks.json").write_text(json.dumps(_PLAN))
    res = ctl.run("tasks", "tst-est-default")
    assert res.returncode == 0, res.stderr
    lines = _est_lines(res.stdout)
    assert lines["002"].endswith("(est 3)"), res.stdout


def test_ralphctl_tasks_shows_est_on_every_row_regardless_of_status(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-est-every-row")
    (rdir / "tasks.json").write_text(json.dumps(_PLAN))
    res = ctl.run("tasks", "tst-est-every-row")
    assert res.returncode == 0, res.stderr
    lines = _est_lines(res.stdout)
    assert lines["001"].endswith("(est 7)"), res.stdout
    assert lines["002"].endswith("(est 3)"), res.stdout
    assert lines["003"].endswith("(est 3)"), res.stdout
    # the in-progress row still carries its ordinary status text before
    # the new suffix -- this task adds a column, it does not replace one
    assert "in-progress" in lines["003"], res.stdout
