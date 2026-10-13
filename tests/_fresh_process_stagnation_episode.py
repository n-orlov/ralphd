"""Helper spawned as a genuinely separate Python OS process by
`tests/test_approach_start_head_persistence.py`'s
`test_a_genuinely_fresh_os_process_re_entry_after_a_commit_still_sees_the_approach_as_progressed`
(cure-01-01, PRD A.2 / Definition of done 1) -- runs exactly ONE episode of
the commit -> mid-approach end -> re-entry sequence against a run-state tree/workspace given
on argv, entirely inside its OWN interpreter: no python object, module-level
cache, or asyncio event loop is shared with the parent test process, nor
with the OTHER episode's own subprocess -- only the run-state tree
(`status.json`, `events.jsonl`) and the workspace's git history, both on
disk, carry anything across the process boundary.

Not named `test_*.py` on purpose: pytest's default collector never picks
this module up as a test module of its own; it is only ever imported by
name inside a child `python -m ...` invocation the parent test spawns.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from ralphd.engine import loop as loop_mod
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _CommitOnceThenQuietRunner:
    """Same shape as the in-process probe's own runner (episode 1): the
    first call commits a real file, every call after that is genuinely
    quiet."""

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
    """Every call is genuinely quiet -- for episode 2, where no further
    commit should ever land."""

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        return _result("still nothing further to report")


def _make_worker_supervisor(root: Path, workspace: Path, runner,
                            iterations: int = 20) -> LoopSupervisor:
    run = RunDir(root=root)
    cfg = loop_mod.JobConfig(run_id="cure-01-01-fresh-os-process",
                             vigilant=False, on_complete="exit",
                             iterations=iterations, infra_retry_backoff_s=[0.0],
                             infra_retry_backoff_max_s=0.0,
                             infra_outage_budget_s=1000.0, max_approaches=3)
    sup = LoopSupervisor(cfg, run, workspace)
    sup.runner = runner  # type: ignore[assignment]
    sup._approach = 1
    return sup


def episode_one(root: Path, workspace: Path) -> None:
    """This process's own run-state tree does not exist yet -- it creates
    it, exactly like the very first `_step_worker` entry of a real job."""
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {
        "version": 1,
        "tasks": [{"id": "001", "title": "do the thing", "status": "pending",
                   "successCriteria": "the thing works"}],
    })
    sup = _make_worker_supervisor(root, workspace,
                                   _CommitOnceThenQuietRunner(workspace),
                                   iterations=1)
    outcome = asyncio.run(sup._step_worker())
    # the one-iteration budget ends the episode MID-approach
    assert outcome == ("tail",), outcome
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()


def episode_two(root: Path, workspace: Path) -> None:
    """A BRAND-NEW OS process, over the SAME run root/workspace episode_one
    already left on disk -- standing in for a fresh-process resume (a
    crash/restart, an ordinary `ralphctl resume`). Nothing in this process
    was ever constructed by, or shares memory with, `episode_one`'s own
    process; everything below is read fresh off disk."""
    sup = _make_worker_supervisor(root, workspace, _QuietRunner())
    start_approach, skip_planning, start_step = sup._resume_point()
    assert (start_approach, skip_planning, start_step) == (1, True, "planning")

    outcome = asyncio.run(sup._step_worker())
    assert outcome == ("advance", "replan", "stagnation_exhausted"), outcome
    assert sup._approach == 1
    assert (root / "approaches" / "01").is_dir()
    stagnation = [
        json.loads(line) for line in
        (root / "events.jsonl").read_text().splitlines() if line.strip()]
    stagnation = [ev for ev in stagnation if ev.get("type") == "log"
                  and "3 iterations with no task progress"
                  in (ev.get("message") or "")]
    assert len(stagnation) == 1, stagnation
    # the baseline persisted by episode 1 is what makes this True
    assert stagnation[0]["approachProgressed"] is True, stagnation


def main(argv: list[str]) -> int:
    episode, root_s, workspace_s = argv[1], argv[2], argv[3]
    root = Path(root_s)
    workspace = Path(workspace_s)
    if episode == "1":
        episode_one(root, workspace)
    elif episode == "2":
        episode_two(root, workspace)
    else:
        raise SystemExit(f"unknown episode {episode!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
