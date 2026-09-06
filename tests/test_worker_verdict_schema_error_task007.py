"""v0.13 task 007 (#92 site 5, the LAST of the five-site cutover map):
`_step_worker`'s remaining `elif schema_errors:` branch -- a worker payload
whose `verdict` is present but is neither `"unsatisfiable"` nor `"complete"`
above it, and which the schema itself rejects (an unrecognised enum value,
here) -- used to be a single validate-or-abort check: the FIRST bad payload
set `self._abort_reason` and returned `("tail",)` outright, no re-ask, no
ladder. No existing test module pinned this exact site before this task.

This module drives that branch through `contract_enforcement.enforce_contract`
instead, the same shape tasks 003-006 already gave the other four sites: a
schema violation is carried back to the SAME worker, verbatim, up to
`contract_retry_max` extra times, and an exhausted ladder is an agent-fault
iteration that continues per the table (`T("worker", "no_verdict_yet")`,
the pre-existing self-loop -- `_step_worker`'s own `while self.budget_left():`
loop decides when the run actually ends, never the ladder itself), rather
than aborting on the first offence.

A documented one-line revert of this cutover -- putting back the inline
`self._abort_reason = message; return ("tail",)` pair in place of the
`enforce_contract` call -- makes `test_an_unrecognised_verdict_value_...`
below fail on its very first assertion (`state == "failed"` becomes
`"aborted"`, and `sup._abort_reason` is no longer `None`).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor

TASK_1 = "001"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _bad_verdict() -> IterationResult:
    """A worker final line whose `verdict` the schema's closed enum
    (`["complete", "no_verdict", "unsatisfiable"]`) rejects outright --
    valid JSON, but not any recognised verdict at all."""
    payload = json.dumps({"verdict": "give_up"})
    return _ok(f"I am stuck on task {TASK_1}.\n{payload}")


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "do the thing",
            "status": "in-progress", "successCriteria": "the thing works"}
    task.update(over)
    return task



_supervisor = scripted_supervisor(run_id="v013-task007-schema-verdict", iterations=2,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)



def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Mirrors `tests/test_unsatisfiable_criteria.py`'s helper of the same
    name: seed tasks.json exactly like a resumed run, skip planning
    entirely (`handlers` must have no `"planning"` key)."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _log_messages(root: Path) -> str:
    return "\n".join(ev.get("message") or "" for ev in _events(root)
                     if ev.get("type") == "log")


def test_the_schema_still_rejects_an_unrecognised_verdict_value():
    from ralphd.engine import contracts

    assert contracts.validate_payload("worker", {"verdict": "complete"}) == []
    assert contracts.validate_payload("worker", {"verdict": "give_up"}) != []


def test_an_unrecognised_verdict_value_exhausts_the_ladder_and_still_refuses_the_move(
        tmp_path):
    """Site 5's own version of tasks 005/006's exhaustion test: a worker
    payload whose `verdict` the schema rejects outright (never
    `"unsatisfiable"` or `"complete"`, so neither of those two branches
    above this one ever fires) is carried back through
    `contract_enforcement.enforce_contract`'s SAME re-ask ladder those
    sites already use, not a single validate-or-abort check.

    The move is refused (the task record never moves off `in-progress` --
    there was nothing legal to apply in the first place, and nothing was);
    the ladder ran the FULL `contract_retry_max + 1` attempts (every call,
    the one charged `run_iteration` plus every uncharged `_ask_contract_fix`
    re-ask, lands on the SAME `ScriptedRunner.run` (tests/conftest.py), so this call count IS
    the ladder's own attempt count directly); `sup._abort_reason` stays
    `None` and no "contract violation" log message is emitted; and the run
    continues per the table (`T("worker", "no_verdict_yet")`, the
    undeclared self-loop -- no `_advance` call at all, exactly like an
    ordinary `"no_verdict"` reply) rather than aborting outright. With the
    run's own iteration budget already spent by the one charged attempt
    below (`iterations=2`, one already seeded used), the run ends
    `"failed"` (unverified, no abort) via the ordinary tail, never
    `"aborted"`.
    """
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _bad_verdict()],
    }, tasks)

    # the move is still refused -- the task record is untouched.
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"

    # the ladder ran the FULL contract_retry_max + 1 attempts.
    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]

    events = _events(root)
    violation_events = [ev for ev in events if ev.get("type") == "contract.violation"]
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1, "the ladder must be exhausted exactly once"

    # agent-fault continuation, NEVER a contract-violation abort: no
    # `_abort_reason`, no "contract violation" log message, and the run
    # ends `"failed"` (budget genuinely exhausted), not `"aborted"`.
    assert sup._abort_reason is None
    msgs = _log_messages(root)
    assert "contract violation" not in msgs
    assert state == "failed", sup.run.read_status()
