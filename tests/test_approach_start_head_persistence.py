"""v0.20.1 requirement A.1 (task 001, #-- peer-gate finding): the approach
progress baseline (`approach_start_head`, the workspace HEAD an approach
started at) is persisted to run state, not held only in a `_step_worker`
LOCAL re-derived from `_workspace_head_sha()` at that method's own entry --
because a fresh-process re-entry into the SAME approach (a crash/restart, an
ordinary resume, an operator-steer round trip) would otherwise silently
reset the baseline to whatever HEAD happens to be RIGHT NOW, defeating the
"did this approach gain any commits" stagnation-exhaustion witness (task
018) for exactly the case it exists to cover.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ralphd.engine import loop as loop_mod
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, atomic_write_json


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_git_workspace(workspace: Path) -> str:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    (workspace / "seed.txt").write_text("seed")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace,
                          check=True, capture_output=True,
                          text=True).stdout.strip()


def _make_supervisor(root: Path, workspace: Path) -> LoopSupervisor:
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {
        "version": 1,
        "tasks": [{"id": "001", "title": "do the thing", "status": "pending",
                   "successCriteria": "the thing works"}],
    })
    cfg = loop_mod.JobConfig(run_id="task001-approach-start-head",
                             vigilant=False, on_complete="exit",
                             iterations=20, infra_retry_backoff_s=[0.0],
                             infra_retry_backoff_max_s=0.0,
                             infra_outage_budget_s=1000.0, max_approaches=3)
    return LoopSupervisor(cfg, run, workspace)


def test_approach_start_head_is_persisted_on_first_read(tmp_path):
    workspace = tmp_path / "workspace"
    root = tmp_path / "run"
    seed_sha = _init_git_workspace(workspace)
    sup = _make_supervisor(root, workspace)

    head = sup._approach_start_head(1)

    assert head == seed_sha
    status = sup.run.read_status()
    assert status.get("approachStartHeads") == {"1": seed_sha}


def test_a_second_loopsupervisor_over_the_same_run_state_reads_the_baseline_back_instead_of_rederiving_it(
        tmp_path):
    """The literal requirement A.1 probe: build one `LoopSupervisor`, let it
    record approach 1's baseline, advance the workspace's HEAD out from
    under it (a real new commit, so a re-derivation would visibly disagree),
    then build a SECOND, independent `LoopSupervisor` over the exact same
    run-state tree and ask it for the SAME approach's baseline. It must read
    the ORIGINAL sha back off disk, never recompute a fresh one from the
    workspace's current (now different) HEAD.
    """
    workspace = tmp_path / "workspace"
    root = tmp_path / "run"
    seed_sha = _init_git_workspace(workspace)

    first = _make_supervisor(root, workspace)
    baseline = first._approach_start_head(1)
    assert baseline == seed_sha

    # The workspace gains a real commit AFTER the baseline was recorded --
    # so `_workspace_head_sha()` called fresh right now would return a
    # DIFFERENT sha than what was persisted.
    (workspace / "progress.txt").write_text("real work")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "real progress")
    advanced_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, check=True,
        capture_output=True, text=True).stdout.strip()
    assert advanced_sha != seed_sha
    assert first._workspace_head_sha() == advanced_sha

    # A genuinely fresh `LoopSupervisor`, over the same run root/workspace,
    # standing in for a fresh process re-entering `_step_worker` mid-approach
    # (a crash/restart, a resume, an operator steer).
    second = _make_supervisor(root, workspace)
    reread = second._approach_start_head(1)

    assert reread == seed_sha
    assert reread != advanced_sha
    status = second.run.read_status()
    assert status.get("approachStartHeads") == {"1": seed_sha}


def test_a_non_git_workspace_persists_null_and_reads_it_back(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    root = tmp_path / "run"
    sup = _make_supervisor(root, workspace)

    head = sup._approach_start_head(1)

    assert head is None
    status = sup.run.read_status()
    assert status.get("approachStartHeads") == {"1": None}
    # a second read for the same approach still returns None, read back
    # from the persisted entry, not merely re-derived the same way twice.
    second = _make_supervisor(root, workspace)
    assert second._approach_start_head(1) is None
