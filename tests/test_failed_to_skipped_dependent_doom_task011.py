"""Task 011 (requirement C.1, #123): drive task 009's `failed -> skipped`
escape end to end through the PRODUCTION worker channel -- `LoopSupervisor.
_step_worker` reading a real `worker` contract payload of exactly the shape
worker.md (task 010) instructs -- with a DEPENDENT task present, in exactly
one approach.

What this module pins that task 009's own battery
(`tests/test_failed_to_skipped_status_task009.py`, direct calls into
`task_status_report_errors`/`_apply_reported_task_status`) does not:

1. The move travels the REAL worker dispatch (`_step_worker` ->
   `contract_enforcement.enforce_contract` ->
   `_apply_worker_task_status_report` -> `_apply_reported_task_status` ->
   `RunState.write_tasks`), inside ONE approach and ONE charged iteration,
   from a payload that is legal under the SHIPPED `transitions.HAT_SCHEMAS
   ["worker"]` -- the gap this task closed: before it, the escape worker.md
   instructs was refused by that schema (`unexpected field(s)
   ['justification', 'task_status']`) and `_step_worker` had no application
   path for it at all, so a `failed` task with a dependent was resolvable
   only by an operator editing the record by hand.
2. A DEPENDENT task (`dependsOn` naming the failed task) is present
   throughout, and `state.task_blocked_by` shows it doomed by the same
   ancestor id BOTH before and after the move -- `skipped` never un-dooms
   it, because `skipped` is not in `state.TASK_DEPENDENCY_SATISFIED_
   STATUSES` any more than `failed` is.
3. The resulting `tasks.json` is checked directly against `transitions.
   task_status_table_for()` for the variant actually in force (both
   variants are exercised, parametrized), not merely asserted to "look
   right".

Plus the two refusals that keep the channel honest, both driven through the
same production path and both bounded exactly like task 001's verify ladder
(`1 + cfg.contract_retry_max` runner calls, zeroed backoffs): a report with
no `justification`, and a report naming the task the harness assigned THIS
iteration (whose outcome belongs to `verdict`, not to this channel).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import contracts
from ralphd.engine import state
from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_FAILED = "010"
TASK_DEPENDENT = "011"
TASK_ASSIGNED = "012"
JUSTIFICATION = ("criteria required a live third-party endpoint this sandbox "
                 "cannot reach; the parser half landed in abc1234, the live "
                 "probe did not land at all")


def _tasks() -> list[dict]:
    return [
        # permanently failed: validation attempts exhausted
        {"id": TASK_FAILED, "title": "reach the unreachable",
         "status": "failed", "successCriteria": "reach the unreachable",
         "validationAttempts": 3},
        # doomed by the one above, before AND after the escape
        {"id": TASK_DEPENDENT, "title": "build on top of it",
         "status": "pending", "successCriteria": "build on 010",
         "dependsOn": [TASK_FAILED]},
        # the actionable task the scheduler actually assigns this pass, so
        # the worker gets a real iteration instead of `select_next_task`
        # returning None and the doomed-plan routing firing first
        {"id": TASK_ASSIGNED, "title": "unrelated work",
         "status": "pending", "successCriteria": "the unrelated thing works"},
    ]


def _payload(task_id: str = TASK_FAILED, justification: str | None = JUSTIFICATION,
             verdict: str = "no_verdict") -> dict:
    """Exactly the shape worker.md instructs: the status report riding
    ALONGSIDE an ordinary verdict in the same JSON reply."""
    p: dict = {"verdict": verdict, "task_status": "skipped", "task_id": task_id}
    if justification is not None:
        p["justification"] = justification
    return p


class _SequencedRunner:
    """One `IterationResult` per call, in order, last one repeating -- the
    charged worker call and any uncharged `_ask_contract_fix` re-ask that
    follows it need to be countable and separately answerable (task 001's
    `tests/test_verify_contract_ladder_task001.py` pattern)."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        idx = min(self.calls, len(self.texts) - 1)
        self.calls += 1
        r = IterationResult(exit_code=0, duration_s=0.01)
        r.final_text = self.texts[idx]
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r


def _supervisor(tmp_path, texts, vigilant: bool):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": _tasks()})
    cfg = JobConfig(run_id=f"task011-escape-{vigilant}", iterations=1,
                    max_approaches=1, vigilant=vigilant, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup.runner = _SequencedRunner(texts)  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup


# ---------------------------------------------------------------------------
# the schema the shipped prompt's payload has to satisfy
# ---------------------------------------------------------------------------

def test_the_worker_schema_declares_the_two_optional_escape_fields():
    schema = tr.HAT_SCHEMAS["worker"]
    assert schema["properties"]["task_status"] == {"type": "string"}
    assert schema["properties"]["justification"] == {"type": "string"}
    assert "task_status" not in schema["required"]
    assert "justification" not in schema["required"]
    # the escape payload worker.md instructs validates clean, verdict and all
    assert contracts.validate_payload("worker", _payload()) == []


# ---------------------------------------------------------------------------
# 1-3: the escape, end to end, with the dependent present
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vigilant", [True, False])
def test_failed_to_skipped_travels_the_worker_channel_with_a_dependent_present(
        tmp_path, vigilant):
    sup = _supervisor(tmp_path, [json.dumps(_payload())], vigilant)
    outcome = asyncio.run(sup._step_worker())

    # -- one approach, one charged iteration, first attempt accepted: no
    #    re-ask ladder ran at all --------------------------------------
    assert sup.iterations_used == 1
    assert sup.runner.calls == 1
    # budget spent with work still outstanding -> the ordinary tail, never
    # an abort and never a replan on this pass
    assert outcome == ("tail",)
    assert sup._abort_reason is None

    tasks_after = sup.run.read_tasks()["tasks"]
    by_id = {t["id"]: t for t in tasks_after}

    # -- the move landed, and is legal against the declared table for the
    #    variant actually in force --------------------------------------
    assert by_id[TASK_FAILED]["status"] == "skipped"
    table = tr.task_status_table_for(vigilant)
    assert "skipped" in table["failed"]
    assert tr.is_legal_task_status_transition("failed", "skipped", vigilant)
    for t in tasks_after:
        assert t["status"] in table

    # -- the justification landed in the task's own `notes` --------------
    assert by_id[TASK_FAILED]["notes"] == JUSTIFICATION
    # the consumed attempts are part of the record, untouched
    assert by_id[TASK_FAILED]["validationAttempts"] == 3

    # -- dependency doom is NOT cleared: the dependent is doomed by the
    #    SAME ancestor id both before and after the move ----------------
    before_blocked = state.task_blocked_by(_tasks())
    after_blocked = state.task_blocked_by(tasks_after)
    assert before_blocked[TASK_DEPENDENT] == [TASK_FAILED]
    assert after_blocked[TASK_DEPENDENT] == [TASK_FAILED]
    assert by_id[TASK_DEPENDENT]["status"] == "pending"
    assert "skipped" not in state.TASK_DEPENDENCY_SATISFIED_STATUSES
    assert "failed" not in state.TASK_DEPENDENCY_SATISFIED_STATUSES

    # -- the assigned task's own status is the harness's business and was
    #    not touched by the escape ---------------------------------------
    assert by_id[TASK_ASSIGNED]["status"] == "in-progress"

    # -- a task event was emitted for the move ---------------------------
    events = [e for e in state.read_events(sup.run.root) if e.get("type") == "task"]
    assert any(e.get("taskId") == TASK_FAILED and e.get("newStatus") == "skipped"
              for e in events)


# ---------------------------------------------------------------------------
# the two refusals, driven through the same production path
# ---------------------------------------------------------------------------

def test_a_report_without_a_justification_is_re_asked_and_never_applied(tmp_path):
    unjustified = json.dumps(_payload(justification=None))
    sup = _supervisor(tmp_path, [unjustified], vigilant=False)
    asyncio.run(sup._step_worker())

    # the ladder asked for a fix `contract_retry_max` times and never got
    # one: charged call + every uncharged re-ask, exactly task 001's bound
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    assert sup.iterations_used == 1

    by_id = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert by_id[TASK_FAILED]["status"] == "failed"
    assert "notes" not in by_id[TASK_FAILED]


def test_a_report_naming_the_assigned_task_is_re_asked_and_never_applied(tmp_path):
    wrong_target = json.dumps(_payload(task_id=TASK_ASSIGNED))
    sup = _supervisor(tmp_path, [wrong_target], vigilant=False)
    asyncio.run(sup._step_worker())

    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    by_id = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    # the assigned task keeps the status the HARNESS wrote for it
    assert by_id[TASK_ASSIGNED]["status"] == "in-progress"
    assert by_id[TASK_FAILED]["status"] == "failed"


def test_a_repaired_report_is_applied_on_the_uncharged_reask(tmp_path):
    sup = _supervisor(tmp_path, [json.dumps(_payload(justification=None)),
                                 json.dumps(_payload())], vigilant=False)
    asyncio.run(sup._step_worker())

    assert sup.runner.calls == 2          # charged call + one re-ask
    assert sup.iterations_used == 1       # the re-ask charges no iteration
    by_id = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert by_id[TASK_FAILED]["status"] == "skipped"
    assert by_id[TASK_FAILED]["notes"] == JUSTIFICATION
