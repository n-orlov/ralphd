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
import sys
from pathlib import Path

from ralphd.engine import loop as loop_mod
from ralphd.engine import transitions
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


# ===========================================================================
# Requirement A.3: the same 'Cured when:' shape as A.2 above, but the
# fresh-process re-entry is replaced by the OTHER re-entry the PRD names --
# an operator steer landing while the run is paused, then WORKER RE-ENTRY
# over the very same approach (worker -> steer -> steer_settled -> worker),
# all inside the SAME `LoopSupervisor`/process. Commit -> stagnation pause
# -> operator steer -> worker re-entry -> renewed stagnation -- still on
# approach 1, pause/escalation emitted both times, no `approaches/NN`
# archive ever created.
# ===========================================================================


class _CommitOnceThenSteerAwareQuietRunner:
    """Same commit-once-then-quiet shape as `_CommitOnceThenQuietRunner`
    above, but also answers the STEER hat's own iteration -- dispatched
    through the one shared `_step_generic_hat` call site, keyed off the
    phase `status.json` already records before `self.runner.run` is ever
    invoked (exactly what `tests/conftest.py`'s `ScriptedRunner` reads,
    reproduced narrowly here rather than imported so this module stays a
    self-contained probe of the A.1 primitive). Only the WORKER phase's
    call count drives the one real commit -- a steer pass never counts as
    a worker iteration and never touches the workspace -- so the SAME
    instance can carry a supervisor through episode 1 (commit, then
    quiet), the steer round trip, and episode 2 (genuinely quiet, no
    further commit) without ever being swapped out.
    """

    def __init__(self, sup: LoopSupervisor, workspace: Path) -> None:
        self.sup = sup
        self.workspace = workspace
        self.worker_calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        if phase == "steer":
            return _result(json.dumps({"settled": True}))
        self.worker_calls += 1
        if self.worker_calls == 1:
            (self.workspace / "progress.txt").write_text("real work")
            _git(self.workspace, "add", "-A")
            _git(self.workspace, "commit", "-q", "-m", "real progress")
        return _result(
            f"iteration {self.worker_calls}: nothing further to report")


def test_worker_reentry_after_an_operator_steer_stays_on_approach_one_and_pauses_again(
        tmp_path):
    """Requirement A.3, the literal PRD `Cured when:` sequence: commit ->
    stagnation pause -> operator steer -> worker re-entry -> renewed
    stagnation, still on approach 1, pause/escalation emitted both times,
    no `approaches/NN` archive ever created.

    Pre-task-001 (`_step_worker` re-deriving `approach_start_head` fresh
    from `self._workspace_head_sha()` at its own method entry instead of
    reading `self._approach_start_head(approach)` back) this fails at the
    THIRD `_step_worker()` call below -- the worker RE-ENTRY that follows
    the steer round trip: that call's own fresh local re-derives the
    baseline from the CURRENT head, which already carries the earlier
    commit -- so the renewed, genuinely quiet stagnation round's commit
    witness reads as 'unchanged', a progressing-but-not-last approach is
    wrongly treated as fully silent:
    `('advance', 'replan', 'stagnation_exhausted')`, `approaches/01`
    archived, `sup._approach` advanced past 1.
    """
    workspace = tmp_path / "workspace"
    root = tmp_path / "run"
    _init_git_workspace(workspace)

    # One `LoopSupervisor` for the whole sequence -- the steer round trip
    # below is an IN-PROCESS re-entry into `worker`, not a fresh process
    # (that is task 002's own probe); this task's own subject is the
    # operator-steer path through the SAME `_step_worker`/`_step_generic_
    # hat` dispatch a real running process would take.
    sup = _make_worker_supervisor(root, workspace, None)
    sup.runner = _CommitOnceThenSteerAwareQuietRunner(sup, workspace)  # type: ignore[assignment]

    # Episode 1: one commit, then three genuinely quiet iterations trip the
    # three-strike counter -- but the commit witness means this approach
    # (1 of 3, not the last) pauses instead of replanning.
    outcome1 = asyncio.run(sup._step_worker())

    assert outcome1 == ("advance", "paused", "stagnation_last_approach")
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()

    # The operator steers while the run sits paused, awaiting the
    # stagnation ruling -- `RunDir.add_steering` is the real primitive
    # `ralphctl steer` / `POST /interrupt` uses.
    steer_name = sup.run.add_steering(
        "investigate the stagnation before continuing", "opSteer")
    assert sup.run.pending_steering() == [root / "steering" / steer_name]
    # The operator also resumes (`ralphctl resume`/`POST /resume`) -- the
    # engine's own `pause()` cleared `self._pause` on the escalation above,
    # and `_gate()` (the pause/resume seam every iteration boundary passes
    # through) blocks on that same event; a real steer round trip is
    # always preceded by a real resume, never a bare `_step_worker()` call
    # against a still-paused loop.
    sup.resume()

    # Worker re-entry (still the SAME process/supervisor): unsettled
    # steering is absolute top priority in `_step_worker`'s own dispatch
    # loop, above even the stagnation-pause ruling -- it routes straight to
    # `steer` rather than resuming `worker`'s self-loop.
    reentry_outcome = asyncio.run(sup._step_worker())
    assert reentry_outcome == ("advance", "steer", "steering_pending")

    # The steer hat settles the one pending message in a single iteration,
    # through the real, shared `_step_generic_hat` call site -- landing
    # back in `worker` over the exact same approach.
    steer_edges = transitions.successors("steer", vigilant=False)
    steer_outcome = asyncio.run(sup._step_generic_hat("steer", steer_edges))
    assert steer_outcome == ("advance", "worker", "steer_settled")
    assert sup.run.pending_steering() == []

    # Episode 2, the literal A.3 probe: WORKER RE-ENTRY after the steer
    # round trip -- a fresh call into `_step_worker()`, over the same
    # approach, with nothing further to commit. Post-fix this reads the
    # baseline recorded during episode 1 back off disk; pre-fix it
    # re-derives it from the current head (which already carries episode
    # 1's commit), collapsing the commit witness.
    outcome2 = asyncio.run(sup._step_worker())

    assert outcome2 == ("advance", "paused", "stagnation_last_approach")
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 2
    assert all(e["reason"] == "stagnation" for e in escalations)
    assert all(e["approach"] == 1 for e in escalations)
    # both escalations saw the approach as progressed (the commit witness),
    # never as the genuinely-silent case the A.4 replan node still covers.
    assert all(e["approachProgressed"] is True for e in escalations)


# ===========================================================================
# cure-01-01 (PRD A.2 / Definition of done 1, H.3 evidence integrity): the
# probe above answers requirement A.2's `Cured when:` clause with two
# `LoopSupervisor` PYTHON OBJECTS in one shared interpreter -- a fresh
# object, not a fresh process. That is not what "fresh-process resume"
# means: a real crash/restart or `ralphctl resume` re-enters `worker`
# inside a BRAND-NEW OS process, with no python state whatsoever surviving
# from before, only the run-state tree and the workspace's git history on
# disk. The node below drives the identical commit -> stagnation pause ->
# fresh-process resume -> renewed stagnation sequence, but each episode
# runs in its own `python -m tests._fresh_process_stagnation_episode`
# child process (`sys.executable`, never the same interpreter, never a
# shared asyncio event loop) over the SAME run root/workspace passed on
# argv -- see that module's own docstring for exactly what does and does
# not cross the process boundary.
# ===========================================================================


def test_a_genuinely_fresh_os_process_resume_after_a_commit_and_stagnation_pause_stays_on_approach_one_and_pauses_again(
        tmp_path):
    """cure-01-01, requirement A.2's `Cured when:` clause driven across a
    REAL process boundary: commit -> stagnation pause -> fresh-OS-process
    resume -> renewed stagnation, still on approach 1, pause/escalation
    emitted both times, no `approaches/NN` archive ever created.

    Episode 1 (`_fresh_process_stagnation_episode.episode_one`, its own
    `python -m` child process) creates the run-state tree, commits once,
    then runs three genuinely quiet iterations -- the three-strike counter
    trips, but the commit witness means this approach (1 of 3, not the
    last) pauses instead of replanning.

    Episode 2, a SECOND, independently spawned `python -m` child process --
    no shared python object, module cache or event loop with episode 1's
    process, or with this test's own -- resumes into `worker` over the
    identical run root/workspace and runs a genuinely quiet round.
    Pre-task-001 (`_step_worker` re-deriving `approach_start_head` fresh
    from `self._workspace_head_sha()` at its own method entry instead of
    reading `self._approach_start_head(approach)` back off `status.json`)
    this fails inside episode 2's OWN process: a brand-new OS process
    re-entering the same approach re-derives its baseline from the CURRENT
    head, which already carries episode 1's commit -- so the commit
    witness reads as "unchanged" for the second, genuinely quiet
    stagnation round, and a progressing-but-not-last approach is wrongly
    treated as fully silent: episode 2 raises
    `AssertionError: ('advance', 'replan', 'stagnation_exhausted') ==
    ('advance', 'paused', 'stagnation_last_approach')`, its own process
    exits non-zero, and `approaches/01` is archived on disk where this
    test (running in a THIRD process, its own) can see it.
    """
    workspace = tmp_path / "workspace"
    root = tmp_path / "run"
    _init_git_workspace(workspace)

    helper = Path(__file__).parent / "_fresh_process_stagnation_episode.py"
    assert helper.exists()

    episode1 = subprocess.run(
        [sys.executable, str(helper), "1", str(root), str(workspace)],
        capture_output=True, text=True, timeout=60)
    assert episode1.returncode == 0, (
        f"episode 1 (its own fresh OS process) failed:\n"
        f"stdout:\n{episode1.stdout}\nstderr:\n{episode1.stderr}")

    episode2 = subprocess.run(
        [sys.executable, str(helper), "2", str(root), str(workspace)],
        capture_output=True, text=True, timeout=60)
    assert episode2.returncode == 0, (
        f"episode 2 (a SECOND, independently spawned fresh OS process, no "
        f"python state shared with episode 1's process or this test's "
        f"own) failed:\nstdout:\n{episode2.stdout}\nstderr:\n"
        f"{episode2.stderr}")

    assert not (root / "approaches" / "01").exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 2
    assert all(e["reason"] == "stagnation" for e in escalations)
    assert all(e["approach"] == 1 for e in escalations)
    # both escalations saw the approach as progressed (the commit witness),
    # never as the genuinely-silent case the A.4 replan node still covers.
    assert all(e["approachProgressed"] is True for e in escalations)
