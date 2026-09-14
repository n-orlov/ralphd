"""Task 028 (PRD v0.21 requirement F.2, #158/#194): an iteration that ended
with no verdict applied -- the ordinary `no_verdict` reply `_step_worker`'s
own `while` treats as "try again next pass" -- still carries the task id.

Task 027's dispatch write (`assignedTaskId`, recorded in `meta.json` BEFORE
the worker subprocess is even spawned) is a plain dict that is only ever
mutated in place all the way to the end-of-iteration `atomic_write_json`
call, never re-read from disk and rebuilt -- so this is a regression guard
proving that invariant survives specifically the no-verdict path (precisely
the iterations #158/#194 says an operator most wants attributed), not a
claim that production code changed to make it so.

Uses the same in-process `make_supervisor`/`ScriptedRunner` seam
`tests/test_petition_hold_report.py` uses for its own `_no_verdict_worker`,
bounded by `iterations=1` (never an unbounded `while`) plus
`asyncio.wait_for(..., 10)` so this cannot hang the way the standing rules
warn a `_step_worker` test can.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine.runner import IterationResult
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor

TASK_ID = "001"


def _plan() -> list[dict]:
    return [{"id": TASK_ID, "title": "do the thing",
             "status": "pending", "successCriteria": "criterion holds"}]


def _no_verdict_worker(sup, prompt) -> IterationResult:
    """A bare `no_verdict` reply -- nothing is ever applied to tasks.json,
    exactly the shape `_record_worker_iteration_verdict` (task 123) is
    documented to skip ("never for ... a bare 'no_verdict'")."""
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "no_verdict"})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def test_no_verdict_worker_iteration_still_carries_assigned_task_id(tmp_path):
    sup = make_supervisor(
        tmp_path / "run", run_id="task028-no-verdict-task-id",
        cfg_defaults=False, max_approaches=1, vigilant=False,
        on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0,
        approach=1, tasks=_plan(), iterations=1)
    attach_runner(sup, ScriptedRunner({"worker": [_no_verdict_worker]}))

    asyncio.run(asyncio.wait_for(sup._step_worker(), 10))

    n = sup.iterations_used
    assert n >= 1, "expected at least one worker iteration to have run"
    meta_path = sup.run.iteration_dir(n) / "meta.json"
    meta = json.loads(meta_path.read_text())

    assert meta.get("phase") == "worker", meta
    assert "endedAt" in meta, "the iteration must have actually ended"
    assert meta.get("assignedTaskId") == TASK_ID, meta
    # the verdict-application enrichment (`_record_worker_iteration_
    # verdict`, task 123) never ran on a bare no_verdict reply -- the
    # dispatch-time key from task 027 is the ONLY place the task id
    # survives on this iteration's meta.json.
    assert meta.get("appliedTaskId") is None, meta
    assert meta.get("appliedVerdict") is None, meta

    # the harness claims the task in-progress BEFORE dispatch (task 116)
    # -- a bare no_verdict reply must not move it any further than that.
    tasks = sup.run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "in-progress", (
        "a no_verdict reply must not have completed/failed the task")
