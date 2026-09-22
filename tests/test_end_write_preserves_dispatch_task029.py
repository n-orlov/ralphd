"""Task 029 (PRD v0.21 requirement F.3, #158/#194): the end-of-iteration
write must not lose or contradict task 027's dispatch-time keys
(`assignedTaskId`/`effectiveTier`), and an existing reader that only ever
consulted `appliedTaskId`/`verifiedTask` (task 116/task 004, both written
before F.1 existed) must keep working once `meta.json` also carries the two
new keys it has never seen.

Two tests, one per half of F.3:

- `test_complete_verdicts_end_write_agrees_with_its_own_dispatch_values`
  drives a REAL worker iteration end to end (`_step_worker`, vigilant=False
  so `_apply_reported_task_status` -> `_record_worker_iteration_verdict`
  both run inline, no verify iteration needed) and reads the iteration's
  final `meta.json` back off disk: `_record_worker_iteration_verdict`
  (`meta.json` read -> two keys set -> written back, loop.py ~5780) never
  rebuilds `meta` from anything but its own prior content, so the dispatch
  write's `assignedTaskId`/`effectiveTier` (loop.py ~2067) must still be
  there afterwards, and must agree with (never contradict) the end write's
  own `appliedTaskId` for the SAME task.

- `test_pre_f1_reader_of_applied_task_id_and_verified_task_survives_new_keys`
  hand-writes an iteration dir's `meta.json` carrying all four keys (the
  post-F.1/F.2/F.3 shape) and calls `engine.state.iteration_detail` --
  the shared shaping every reader of `appliedTaskId`/`verifiedTask` goes
  through (`ralphctl iteration`, the hub iteration dialog, task 019/#18.1)
  -- then runs `_pre_f1_reader`, a stand-in for a caller written before
  `assignedTaskId`/`effectiveTier` existed and that names only the two old
  keys, and asserts it still extracts the right values: `iteration_detail`
  passes meta.json through near-verbatim (`ITERATION_DERIVED_KEYS` strips
  only the handful of fields IT itself derives), so an old reader that
  never heard of the new keys is not broken by their presence.

Bounded per the standing rules: `_step_worker` is awaited under
`asyncio.wait_for(..., 10)`, `iterations=2` (a fixed, small budget --
never an unbounded `while`; task 015, requirement E.1, #169 retired the
off-budget grace review `_maybe_grace_review` used to grant once all
tasks complete and the budget was exhausted, so `_step_worker`'s own
budget check now falls straight through to the timed-out tail on a
budget of exactly 1 with no `review` phase involved either way -- kept
at 2 anyway, since that is still what leaves room for the one completing
worker iteration this test drives), `on_complete="exit"`, no real
sleeping (`make_supervisor`'s default).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import iteration_detail
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor

TASK_ID = "007"


def _plan() -> list[dict]:
    return [{"id": TASK_ID, "title": "do the thing",
             "status": "pending", "successCriteria": "criterion holds"}]


def _complete_worker(sup, prompt) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "complete", "task_id": TASK_ID})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def test_complete_verdicts_end_write_agrees_with_its_own_dispatch_values(
        tmp_path):
    sup = make_supervisor(
        tmp_path / "run", run_id="task029-end-write-agrees",
        cfg_defaults=False, max_approaches=1, vigilant=False,
        on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0,
        approach=1, tasks=_plan(), iterations=2)
    attach_runner(sup, ScriptedRunner({"worker": [_complete_worker]}))

    asyncio.run(asyncio.wait_for(sup._step_worker(), 10))

    n = sup.iterations_used
    assert n >= 1, "expected at least one worker iteration to have run"
    meta_path = sup.run.iteration_dir(n) / "meta.json"
    meta = json.loads(meta_path.read_text())

    assert meta.get("phase") == "worker", meta
    assert "endedAt" in meta, "the iteration must have actually ended"

    # the task actually completed on tasks.json (the enrichment below is
    # meaningless if it did not)
    tasks = sup.run.read_tasks()["tasks"]
    assert tasks[0]["status"] in ("completed", "validated"), tasks[0]

    # task 027's dispatch-time keys, still on the end-written meta.json --
    # `_record_worker_iteration_verdict` read this SAME dict off disk and
    # only ever set `appliedVerdict`/`appliedTaskId` on it, so nothing here
    # was dropped by the end write.
    assert meta.get("assignedTaskId") == TASK_ID, meta
    assert meta.get("effectiveTier") is not None, meta

    # task 116/123's end-write keys, and they must AGREE with the dispatch
    # keys above for the same iteration -- never a stale/contradicting id.
    assert meta.get("appliedTaskId") == TASK_ID, meta
    assert meta.get("appliedVerdict") == "complete", meta
    assert meta.get("appliedTaskId") == meta.get("assignedTaskId"), (
        "end write contradicts its own dispatch write", meta)


def _pre_f1_reader(detail: dict) -> tuple[str | None, str | None]:
    """A stand-in for a caller written before task 027 existed: it names
    only the two keys task 116 (`appliedTaskId`) and task 004
    (`verifiedTask`) already gave it, and has never heard of
    `assignedTaskId`/`effectiveTier`."""
    return detail.get("appliedTaskId"), detail.get("verifiedTask")


def test_pre_f1_reader_of_applied_task_id_and_verified_task_survives_new_keys(
        tmp_path):
    run_dir = tmp_path / "run"
    itdir = run_dir / "iterations" / "0003"
    itdir.mkdir(parents=True)
    (itdir / "meta.json").write_text(json.dumps({
        "number": 3, "phase": "worker", "model": None, "approach": 1,
        "startedAt": "2026-09-14T10:00:00Z", "endedAt": "2026-09-14T10:05:00Z",
        "steeringDelivered": [], "steeringConsumed": [],
        # task 027/029's dispatch-time keys -- new to any reader written
        # before this wave.
        "assignedTaskId": TASK_ID, "effectiveTier": "hard",
        # the two keys a pre-existing reader already consults.
        "appliedTaskId": TASK_ID, "appliedVerdict": "complete",
        "verifiedTask": TASK_ID, "verifyOutcome": "pass",
    }))

    detail = iteration_detail(run_dir, 3)
    assert detail is not None and detail.get("hasMeta") is True, detail

    applied_task_id, verified_task = _pre_f1_reader(detail)
    assert applied_task_id == TASK_ID, detail
    assert verified_task == TASK_ID, detail
