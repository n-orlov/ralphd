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

import asyncio
import json
import subprocess
from pathlib import Path

from ralphd.engine import loop as loop_mod
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
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


# ===========================================================================
# Requirement A.2: the real end-to-end proof this primitive exists for --
# a real `_step_worker()` commit, a real stagnation pause, a genuinely
# FRESH-PROCESS `LoopSupervisor` (no shared python state, everything read
# off disk) resuming into `worker`, and a SECOND, renewed stagnation --
# still on approach 1, pause/escalation emitted both times, no
# `approaches/NN` archive ever created.
# ===========================================================================


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _CommitOnceThenQuietRunner:
    """Call 1 commits a real file into `workspace` (this approach gains a
    commit); every call after that leaves the tree untouched and reports
    plain text with no structured claim -- the three-strike stagnation
    counter still trips on the following calls, exactly like a genuinely
    silent approach would, but this approach's own HEAD (read once before
    call 1) no longer equals the HEAD at the stagnation-exhaustion decision
    point. Same shape as `tests/test_last_approach.py`'s
    `_CommitsThenGoesQuietRunner` (task 018), reproduced here rather than
    imported so this module stays a self-contained probe of the A.1
    primitive it already exercises."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        if self.calls == 1:
            (self.workspace / "progress.txt").write_text("real work")
            _git(self.workspace, "add", "-A")
            _git(self.workspace, "commit", "-q", "-m", "real progress")
        return _result(f"iteration {self.calls}: nothing further to report")


class _QuietRunner:
    """Every call leaves the tree untouched and reports plain text with no
    structured claim -- a genuinely quiet run, for the SECOND, fresh-process
    stagnation round, where no further commit should ever land."""

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        return _result("still nothing further to report")


def _make_worker_supervisor(root: Path, workspace: Path, runner) -> LoopSupervisor:
    """Same run id/config across both `LoopSupervisor` instances built over
    `root` in the test below -- standing in for two episodes of the SAME
    job, one crashing/resuming into the other -- `max_approaches=3` so
    approach 1 is emphatically NOT the last approach and the pause-vs-
    replan outcome genuinely turns on the commit witness rather than on
    the last-approach edge (which pauses unconditionally either way)."""
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {
        "version": 1,
        "tasks": [{"id": "001", "title": "do the thing", "status": "pending",
                   "successCriteria": "the thing works"}],
    })
    cfg = loop_mod.JobConfig(run_id="task002-fresh-process-resume",
                             vigilant=False, on_complete="exit",
                             iterations=20, infra_retry_backoff_s=[0.0],
                             infra_retry_backoff_max_s=0.0,
                             infra_outage_budget_s=1000.0, max_approaches=3)
    sup = LoopSupervisor(cfg, run, workspace)
    sup.runner = runner  # type: ignore[assignment]
    sup._approach = 1
    return sup


def test_a_fresh_process_resume_after_a_commit_and_stagnation_pause_stays_on_approach_one_and_pauses_again(
        tmp_path):
    """Requirement A.2, the literal PRD `Cured when:` sequence: commit ->
    stagnation pause -> fresh-process resume -> renewed stagnation, still
    on approach 1, pause/escalation emitted both times, no
    `approaches/NN` archive ever created.

    Pre-task-001 (`_step_worker` re-deriving `approach_start_head` fresh
    from `self._workspace_head_sha()` at its own method entry instead of
    reading `self._approach_start_head(approach)` back) this fails at the
    SECOND `_step_worker()` call below: a brand-new process re-entering the
    same approach re-derives its baseline from the CURRENT head, which
    already carries call 1's commit -- so the commit witness reads as
    "unchanged" for the second, genuinely quiet stagnation round, and a
    progressing-but-not-last approach is wrongly treated as fully silent:
    `('advance', 'replan', 'stagnation_exhausted')`, `approaches/01`
    archived, `sup2._approach` advanced past 1.
    """
    workspace = tmp_path / "workspace"
    root = tmp_path / "run"
    _init_git_workspace(workspace)

    # Episode 1: one commit, then three genuinely quiet iterations trip the
    # three-strike counter -- but the commit witness means this approach
    # (1 of 3, not the last) pauses instead of replanning.
    sup1 = _make_worker_supervisor(root, workspace,
                                   _CommitOnceThenQuietRunner(workspace))
    outcome1 = asyncio.run(sup1._step_worker())

    assert outcome1 == ("advance", "paused", "stagnation_last_approach")
    assert sup1._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup1.run.composite_prd_file.exists()

    # Episode 2: a BRAND-NEW `LoopSupervisor`, no python state shared with
    # `sup1` at all, standing in for a fresh process resuming this same
    # run/approach (a crash/restart, an ordinary `ralphctl resume`) --
    # reads `status.json`/`events.jsonl`/the workspace's git history fresh
    # off disk, same run root, same workspace, same config.
    sup2 = _make_worker_supervisor(root, workspace, _QuietRunner())
    assert sup2._paused_awaiting_stagnation_last_approach_ruling(1) is True
    start_approach, skip_planning, start_step = sup2._resume_point()
    assert start_approach == 1
    assert skip_planning is False
    assert start_step == "worker"

    outcome2 = asyncio.run(sup2._step_worker())

    assert outcome2 == ("advance", "paused", "stagnation_last_approach")
    assert sup2._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup2.run.composite_prd_file.exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 2
    assert all(e["reason"] == "stagnation" for e in escalations)
    assert all(e["approach"] == 1 for e in escalations)
    # both escalations saw the approach as progressed (the commit witness),
    # never as the genuinely-silent case the A.4 replan node still covers.
    assert all(e["approachProgressed"] is True for e in escalations)
