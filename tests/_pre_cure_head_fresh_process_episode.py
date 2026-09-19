"""Helper spawned as a genuinely separate Python OS process by
`tests/test_cure_invalidates.py`'s
`test_a_fresh_os_process_reads_the_persisted_pre_cure_head_back_unchanged`
(task 006, PRD requirement B.2, #221) -- runs exactly ONE episode of the
"re-open a record, then restart in a fresh process and read the persisted
pre-cure head back unchanged" probe against a run-state tree/workspace
given on argv, entirely inside its OWN interpreter: no python object,
module-level cache, or asyncio event loop is shared with the parent test
process, nor with the OTHER episode's own subprocess -- only the run-state
tree (`status.json`, `events.jsonl`, `tasks.json`) and the workspace's git
history, both on disk, carry anything across the process boundary.

Not named `test_*.py` on purpose: pytest's default collector never picks
this module up as a test module of its own; it is only ever imported by
name inside a child `python -m ...` invocation the parent test spawns.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine import loop as loop_mod
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

RECORD_PATH = "artifacts/reports/v0.20-lanes.md"


def _bounded(coro, timeout: float = 30.0):
    """Task 073's guard (`tests/test_loop_driving_await_guard.py`, steer
    001 \u00a72): a loop-driving await (`_step_review` here) is never run
    bare -- `_step_paused`'s `await self._gate()` and a repeating scripted
    step are both forever-traps -- so every drive in this module goes
    through one bounded wrapper, the same shape task 072 gave
    `tests/test_peer_verdict_gate.py`.
    """
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _make_supervisor(root: Path, workspace: Path) -> LoopSupervisor:
    run = RunDir(root=root)
    cfg = loop_mod.JobConfig(run_id="task006-pre-cure-head-fresh-os-process",
                             vigilant=False, on_complete="exit",
                             iterations=20, infra_retry_backoff_s=[0.0],
                             infra_retry_backoff_max_s=0.0,
                             infra_outage_budget_s=1000.0, max_approaches=3)
    sup = LoopSupervisor(cfg, run, workspace)
    sup._approach = 1
    return sup


def episode_one(root: Path, workspace: Path, seed_sha: str) -> None:
    """This process's own run-state tree does not exist yet -- it creates
    it, seeds the same "verify every lane" (057) / "record every lane"
    (058) fixture `tests/test_cure_invalidates.py` itself uses, files a
    blocking curable finding whose `invalidates` names the record's own
    path, and drives a real `LoopSupervisor._step_review()` cure pass over
    it -- exactly the write path `insert_cure_tasks` takes in production.
    """
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {
        "version": 1,
        "tasks": [
            {"id": "057", "title": "057", "status": "validated",
             "origin": "planner", "dependsOn": [], "tier": "normal",
             "successCriteria": "verify every lane"},
            {"id": "058", "title": "058", "status": "completed",
             "origin": "planner", "dependsOn": ["057"], "tier": "normal",
             "successCriteria":
                f"One commit adds {RECORD_PATH} naming the shipped sha."},
        ],
    })
    sup = _make_supervisor(root, workspace)
    findings.write_findings_json(sup.run.findings_json_file, [{
        "severity": "blocking", "curable": True,
        "clause": "requirement B.2 (#221)",
        "evidence": "the lane record went stale",
        "invalidates": [RECORD_PATH],
    }])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "one blocking finding, curable, invalidating the record",
        }))

    sup.runner.run = _review_rejects   # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())
    assert outcome == ("advance", "cure", "rejected_curable_pass_available"), outcome

    status = sup.run.read_status()
    heads = status.get("preCureHeads") or {}
    assert heads.get("058") == seed_sha, heads

    data = sup.run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    assert tasks["058"]["status"] == "pending"


def episode_two(root: Path, workspace: Path, seed_sha: str) -> None:
    """A BRAND-NEW OS process, over the SAME run root/workspace
    episode_one already left on disk -- standing in for a fresh-process
    resume (a crash/restart, an ordinary `ralphctl resume`). Nothing in
    this process was ever constructed by, or shares memory with,
    `episode_one`'s own process; the persisted sha is read fresh off disk
    through a brand-new `LoopSupervisor`/`RunDir`, never re-derived from
    the workspace's CURRENT head."""
    sup = _make_supervisor(root, workspace)
    status = sup.run.read_status()
    heads = status.get("preCureHeads") or {}
    assert heads.get("058") == seed_sha, heads


def main(argv: list[str]) -> int:
    episode, root_s, workspace_s, seed_sha = argv[1], argv[2], argv[3], argv[4]
    root = Path(root_s)
    workspace = Path(workspace_s)
    if episode == "1":
        episode_one(root, workspace, seed_sha)
    elif episode == "2":
        episode_two(root, workspace, seed_sha)
    else:
        raise SystemExit(f"unknown episode {episode!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
