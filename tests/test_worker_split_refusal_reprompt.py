"""Task 008 (PRD requirement B.4, #135): a refused VOLUNTARY worker split
(`LoopSupervisor._apply_worker_proposed_tasks`) used to be refused-and-
logged only -- an `EVENT_VIOLATION` on `events.jsonl` and nothing else --
which is exactly the "vanishes" the PRD names. This module pins the fix:

* `LoopSupervisor._record_refused_worker_split` persists the lint's own
  errors onto `status.json` (`REFUSED_WORKER_SPLIT_STATUS_KEY`) the moment
  `_apply_worker_proposed_tasks` refuses a batch, whether refused by the
  lint or by the per-approach budget.
* `LoopSupervisor._worker_assignment_extra` gains a `refused_split` param
  that renders a "## Your last proposed task split was refused" section
  quoting those errors verbatim.
* `_step_worker`'s own dispatch reads-and-clears that status key each time
  it builds a fresh prompt, so the refusal reaches exactly the worker's
  NEXT prompt (the PRD's own stated minimum bar: "at minimum the refusal
  reaches its next prompt") and never a second time after that.

The re-ask path used is `_worker_assignment_extra`'s own next-prompt
composition (`status.json`'s `REFUSED_WORKER_SPLIT_STATUS_KEY`, read and
cleared by `_step_worker`'s dispatch) -- not `contract_enforcement`'s
same-iteration re-ask ladder, which the voluntary channel deliberately
does not use (that ladder is reserved for the MANDATORY forced-split
channel, `_apply_forced_split_report`).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor, supervisor_factory

TASK_1 = "021"


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "a task big enough to split",
            "status": "in-progress",
            "successCriteria": "the whole thing works end to end"}
    task.update(over)
    return task


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


# ---------------------------------------------------------------------------
# `_worker_assignment_extra` directly
# ---------------------------------------------------------------------------

_ssn_supervisor = supervisor_factory(run_id="task008-split-refusal",
                                     run_root="run", workspace="ws")


def _sup(tmp_path) -> LoopSupervisor:
    return _ssn_supervisor(tmp_path)


def test_worker_assignment_extra_renders_the_refused_split_errors(tmp_path):
    sup = _sup(tmp_path)
    task = _task()
    refused = {"taskId": TASK_1, "errors": ["duplicate id '021a' in proposed_tasks"]}
    extra = sup._worker_assignment_extra(task, refused_split=refused)
    assert "## Your last proposed task split was refused" in extra
    section = extra.split("## Your last proposed task split was refused", 1)[1]
    assert "duplicate id '021a' in proposed_tasks" in section
    assert TASK_1 in section


def test_worker_assignment_extra_omits_the_section_when_nothing_refused(tmp_path):
    sup = _sup(tmp_path)
    task = _task()
    extra = sup._worker_assignment_extra(task)
    assert "## Your last proposed task split was refused" not in extra
    assert "## Your last proposed task split was refused" not in (
        sup._worker_assignment_extra(task, refused_split=None))


def test_worker_assignment_extra_renders_the_refusal_even_with_no_task_selectable(
        tmp_path):
    sup = _sup(tmp_path)
    refused = {"taskId": TASK_1, "errors": ["over budget"]}
    extra = sup._worker_assignment_extra(None, refused_split=refused)
    assert "No task is currently selectable" in extra
    assert "## Your last proposed task split was refused" in extra
    assert "over budget" in extra


# ---------------------------------------------------------------------------
# `_apply_worker_proposed_tasks` persists the refusal onto status.json
# ---------------------------------------------------------------------------

def _apply_sup(tmp_path) -> LoopSupervisor:
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    from ralphd.engine.config import JobConfig
    sup = LoopSupervisor(
        JobConfig(run_id="task008-apply", iterations=15, max_approaches=1,
                  vigilant=False, infra_retry_backoff_s=[0.0],
                  infra_retry_backoff_max_s=0.0),
        run, root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [_task()]})
    return sup


def test_a_lint_refused_proposal_is_recorded_on_status_json(tmp_path):
    sup = _apply_sup(tmp_path)
    sup._apply_worker_proposed_tasks(
        [{"id": TASK_1, "title": "collides with the parent",
          "successCriteria": "x works"}],
        TASK_1, 1)
    refused = sup.run.read_status().get(sup.REFUSED_WORKER_SPLIT_STATUS_KEY)
    assert refused is not None
    assert refused["taskId"] == TASK_1
    assert refused["errors"]


def test_a_budget_refused_proposal_is_also_recorded(tmp_path):
    sup = _apply_sup(tmp_path)
    sup.cfg.worker_split_budget_per_approach = 1
    sup._apply_worker_proposed_tasks(
        [{"id": "021a", "title": "a", "successCriteria": "a works"}],
        TASK_1, 1)
    sup._apply_worker_proposed_tasks(
        [{"id": "021b", "title": "b", "successCriteria": "b works"}],
        TASK_1, 1)
    refused = sup.run.read_status().get(sup.REFUSED_WORKER_SPLIT_STATUS_KEY)
    assert refused is not None
    assert "budget" in refused["errors"][0]


def test_an_accepted_proposal_does_not_record_a_refusal(tmp_path):
    sup = _apply_sup(tmp_path)
    sup._apply_worker_proposed_tasks(
        [{"id": "021a", "title": "a", "successCriteria": "a works"}],
        TASK_1, 1)
    assert sup.REFUSED_WORKER_SPLIT_STATUS_KEY not in sup.run.read_status()


# ---------------------------------------------------------------------------
# the real production call site: `_step_worker`'s own dispatch loop
# ---------------------------------------------------------------------------

def _propose_bad_split(sup, prompt=""):
    payload = {"verdict": "no_verdict",
              "proposed_tasks": [{"id": TASK_1, "title": "collides",
                                 "successCriteria": "x works"}]}
    return _ok("proposing a colliding split\n" + json.dumps(payload))


def _complete(task_id: str):
    def _step(sup, prompt=""):
        payload = json.dumps({"verdict": "complete", "task_id": task_id})
        return _ok(f"task {task_id} done\n{payload}")
    return _step


def _review_verified(sup, prompt=""):
    return _ok("verified\n" + json.dumps({"verdict": "verified"}))


# Task 072 (steer 001 §2): bound the loop-driving await below instead of
# a bare `asyncio.run`. The two known forever-awaits are `_step_paused`'s
# `await self._gate()` (parked on the `asyncio.Event` only `POST /pause`'s
# `loop.pause()` clears) and a `_ScriptedRunner`/scripted-hat script whose
# repeating last step never reaches a terminal phase -- but THIS module's
# own `review` script (`_review_verified`) always returns a plain
# `verified` verdict, so the one worker task (`_task()`/`TASK_1`) reaches
# terminal `succeeded` the very first time review runs, well before the
# script's last entry could ever repeat, and this module's `worker`
# script never proposes a split that routes through review's
# `rejected_last_approach` `paused` edge either -- neither historical
# deadlock shape reproduces here. Bounding the await anyway is defense
# against a FUTURE regression, not evidence one is live here today.
def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_a_refused_split_reaches_the_next_worker_prompt_not_only_the_log(
        tmp_path):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    factory = scripted_supervisor(run_id="task008-real-dispatch",
                                  iterations=5, max_approaches=1,
                                  vigilant=False, on_complete="exit",
                                  infra_outage_budget_s=1000.0)
    sup = factory(root, {
        "worker": [_propose_bad_split, _complete(TASK_1)],
        "review": [_review_verified],
    })
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [_task()]})
    sup.iterations_used = 1
    _bounded(sup._run_job_core())

    prompts = sup.runner.prompts  # type: ignore[attr-defined]
    assert len(prompts) >= 2
    # the FIRST prompt (before any refusal happened yet) never carries it
    assert "## Your last proposed task split was refused" not in prompts[0]
    # the SECOND prompt -- dispatched after the refusal was recorded --
    # carries the lint text
    assert "## Your last proposed task split was refused" in prompts[1]
    section = prompts[1].split(
        "## Your last proposed task split was refused", 1)[1]
    assert TASK_1 in section

    events = [json.loads(x) for x in
              (root / "events.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "contract.violation" and e.get("hat") == "worker"
              for e in events)
