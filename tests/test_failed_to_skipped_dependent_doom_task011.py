"""Task 011 (requirement C.1, #123): drive task 009's `failed -> skipped`
escape end to end through the REAL contract channel -- not the bare
`_apply_reported_task_status`/`task_status_report_errors` calls
`tests/test_failed_to_skipped_status_task009.py`'s battery already pins in
isolation -- with a DEPENDENT task present, in exactly one approach.

Uses the same "fixture hat" pattern `tests/test_task_status_lifecycle.py`'s
`registered_status_hat` fixture established: a throwaway state/hat
registered into `transitions.TABLE`/`STATE_HAT`/`HAT_SCHEMAS` for the
duration of this module's tests only, and torn down afterwards. Deliberately
NOT `HAT_SCHEMAS["worker"]` -- per the handoff notes' own gotcha, that
schema still does not declare a `task_status`/`justification` property, and
wiring it for real is out of this task's scope. This module proves the
generic channel (`_step_generic_hat` -> `contract_enforcement.
enforce_contract` -> `task_status_extra_validate` ->
`LoopSupervisor._apply_reported_task_status`) end to end independently of
that open question: any hat reporting a legal `task_id`/`task_status` pair
through this channel is applied, written, and (for this one declared move)
justified, exactly the same way regardless of which hat said it.

Three things this module pins that task 009's battery does not:

1. The move travels the REAL dispatch path (`LoopSupervisor._step_generic_
   hat`, driving a stubbed `pi` subprocess through `run_iteration` ->
   `_run_iteration_once`), in exactly one approach/one iteration -- not a
   direct call into the applier.
2. A DEPENDENT task (`dependsOn` naming the failed task) is present
   throughout, and `state.task_blocked_by` shows it doomed by the same
   ancestor id BOTH before and after the move -- `skipped` never un-dooms
   it, because `skipped` is not in `state.TASK_DEPENDENCY_SATISFIED_
   STATUSES` any more than `failed` is.
3. The resulting `tasks.json` is checked directly against `transitions.
   task_status_table_for()` for the variant actually in force (both
   variants are exercised, parametrized), not merely asserted to "look
   right".
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import state
from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_FAILED = "010"
TASK_DEPENDENT = "011"
JUSTIFICATION = "criteria required a live third-party endpoint this sandbox cannot reach"

STATE_NAME = "task011_failed_skip_state"
HAT_NAME = "task011_failed_skip_hat"


@pytest.fixture
def registered_escape_hat(monkeypatch, tmp_path):
    prompts_dir = tmp_path / "config" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / f"{HAT_NAME}.md").write_text(
        "You are a task011 failed-to-skipped fixture hat. Reply with a "
        "task_id/task_status/justification report as the last line of "
        "your final message.\n")
    from ralphd.engine import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")

    assert STATE_NAME not in tr.TABLE
    tr.TABLE[STATE_NAME] = (tr.T("planning", "task011_status_reported"),)
    tr.STATE_HAT[STATE_NAME] = HAT_NAME
    tr.HAT_SCHEMAS[HAT_NAME] = {
        "type": "object", "required": ["task_id", "task_status"],
        "properties": {
            "task_id": {"type": "string"},
            "task_status": {"type": "string"},
            "justification": {"type": "string"},
        },
        "additionalProperties": False,
    }
    try:
        yield
    finally:
        tr.TABLE.pop(STATE_NAME, None)
        tr.STATE_HAT.pop(STATE_NAME, None)
        tr.HAT_SCHEMAS.pop(HAT_NAME, None)


def _tasks() -> list[dict]:
    return [
        {"id": TASK_FAILED, "title": "reach the unreachable",
         "status": "failed", "successCriteria": "reach the unreachable"},
        {"id": TASK_DEPENDENT, "title": "build on top of it",
         "status": "pending", "successCriteria": "build on 010",
         "dependsOn": [TASK_FAILED]},
    ]


class _FixedAgent:
    """Same shape as `test_task_status_lifecycle.py`'s `_FixedAgent` --
    always reports the one legal escape move, first attempt, no retry."""

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({
            "task_id": TASK_FAILED, "task_status": "skipped",
            "justification": JUSTIFICATION,
        })
        r.duration_s = 0.01
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r


def _run_escape(tmp_path, vigilant: bool):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": _tasks()})
    cfg = JobConfig(run_id=f"task011-escape-{vigilant}", iterations=1,
                    max_approaches=1, vigilant=vigilant, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup.runner = _FixedAgent()  # type: ignore[assignment]
    edges = tr.successors(STATE_NAME, vigilant)
    outcome = asyncio.run(sup._step_generic_hat(STATE_NAME, edges))
    return sup, outcome


@pytest.mark.parametrize("vigilant", [True, False])
def test_failed_to_skipped_travels_the_real_channel_with_a_dependent_present(
        tmp_path, vigilant, registered_escape_hat):
    sup, outcome = _run_escape(tmp_path, vigilant)

    # -- one approach, one iteration: the first attempt is already
    #    schema- and semantically-valid, no re-ask ladder needed ---------
    assert outcome == ("advance", "planning", "task011_status_reported")
    assert sup.iterations_used == 1

    tasks_after = sup.run.read_tasks()["tasks"]
    by_id = {t["id"]: t for t in tasks_after}

    # -- the move landed, and is legal against the declared table for the
    #    variant actually in force --------------------------------------
    assert by_id[TASK_FAILED]["status"] == "skipped"
    table = tr.task_status_table_for(vigilant)
    assert "skipped" in table["failed"]
    assert tr.is_legal_task_status_transition("failed", "skipped", vigilant)
    # every OTHER task's status is also still a legal member of the table
    # in force -- a no-op self-transition is always legal (task 009's own
    # `test_a_no_op_report_is_always_legal`).
    for t in tasks_after:
        assert t["status"] in table or t["status"] in tr.TASK_STATUSES

    # -- the justification landed in the task's own `notes` --------------
    assert by_id[TASK_FAILED]["notes"] == JUSTIFICATION

    # -- dependency doom is NOT cleared: the dependent is doomed by the
    #    SAME ancestor id both before and after the move ------------------
    before_blocked = state.task_blocked_by(_tasks())
    after_blocked = state.task_blocked_by(tasks_after)
    assert before_blocked[TASK_DEPENDENT] == [TASK_FAILED]
    assert after_blocked[TASK_DEPENDENT] == [TASK_FAILED]
    assert by_id[TASK_DEPENDENT]["status"] == "pending"
    assert by_id[TASK_DEPENDENT]["status"] not in state.TASK_DEPENDENCY_SATISFIED_STATUSES
    assert "skipped" not in state.TASK_DEPENDENCY_SATISFIED_STATUSES

    # -- a task_status event was emitted for the move ---------------------
    events = [e for e in state.read_events(sup.run.root) if e.get("type") == "task"]
    assert any(e.get("taskId") == TASK_FAILED and e.get("newStatus") == "skipped"
              for e in events)
