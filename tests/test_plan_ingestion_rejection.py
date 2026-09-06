"""Task 120 gap (found in validation): the loop-level half of "a malformed
plan proposal is rejected rather than silently applied".

`tests/test_plan_ingestion.py` already proves `plan_ingestion.lint_proposal`/
`lint_own_terminal_state`/`enforce_plan_ingestion` reject a cycle, an
unresolvable `dependsOn` and an own-terminal-state-gated task in isolation
(hand-written `respond()` callback, no `LoopSupervisor` involved at all --
confirmed by that module's own imports). What was still missing is proof
that when one of those SAME bad proposals arrives through the real wired
path -- `LoopSupervisor._run_job_core` -> `_step_planning` ->
`plan_ingestion.enforce_plan_ingestion` (task 120) -- the run actually
rejects it end to end: `tasks.json` is never overwritten with the bad
proposal's tasks, the FSM takes the `empty_plan` guard to `replan` (and,
with no approach budget left, straight to the terminal `failed` tail), and
`EVENT_VIOLATION`/`EVENT_AGENT_FAULT` land in `events.jsonl` naming the
offending task id.

Scaffolding mirrors `tests/test_engine_finishing_state.py`'s `_ScriptedRunner`
(stands in for `PiRunner`, no real subprocess) -- deliberately not shared
via import, exactly as every other `_run_job_core`-driving test module in
this tree keeps its own copy.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import contract_enforcement as ce
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_with_runner


# -- scaffolding -- mirrors tests/test_engine_finishing_state.py -----------


class _ScriptedRunner:
    """Stands in for PiRunner. Each script entry takes the supervisor and
    returns an IterationResult; the last entry repeats forever."""

    def __init__(self, sup: LoopSupervisor, script: list):
        self.sup = sup
        self.script = script
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.running = True
        try:
            step = self.script[min(self.calls - 1, len(self.script) - 1)]
            return step(self.sup)
        finally:
            self.running = False


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0          # not an "instant" failure
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _pointer_line(path: str = "plan-proposal.json", summary: str = "planned") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


def _bad_cycle_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1", "successCriteria": "c1", "dependsOn": ["002"]},
        {"id": "002", "title": "t2", "successCriteria": "c2", "dependsOn": ["001"]},
    ]})
    return _ok("planned\n" + _pointer_line())


def _bad_unresolvable_dep_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1", "successCriteria": "c1", "dependsOn": ["999"]},
    ]})
    return _ok("planned\n" + _pointer_line())


def _bad_own_terminal_state_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "final report",
         "successCriteria": ("the closing report can only send once this "
                              "run's own terminal state is reached")},
    ]})
    return _ok("planned\n" + _pointer_line())


_supervisor = supervisor_with_runner(
    lambda sup, script: _ScriptedRunner(sup, script),
    run_id="v09-plan-rejection", iterations=12, max_approaches=1, vigilant=False,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0, contract_retry_max=0)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


PRIOR_TASKS_DOC = {
    "version": 1, "goal": "prior", "scope": {}, "repositories": [],
    "tasks": [{"id": "prior-1", "title": "already here",
               "successCriteria": "was here before the bad replan"}],
    "discovered": {},
}


@pytest.mark.parametrize("bad_plan,offending_id", [
    (_bad_cycle_plan, "001"),
    (_bad_unresolvable_dep_plan, "001"),
    (_bad_own_terminal_state_plan, "001"),
])
def test_a_malformed_proposal_is_rejected_through_the_real_planning_step(
    tmp_path, bad_plan, offending_id,
):
    root = tmp_path / "run"
    sup = _supervisor(root, [bad_plan])
    # A prior tasks.json exists (as it would on a real approach boundary) --
    # ingestion refusing the bad proposal must leave it exactly as it was.
    sup.run.write_tasks(dict(PRIOR_TASKS_DOC))

    final_state = asyncio.run(sup.run_job())

    # (a) tasks.json is never overwritten with the bad proposal's tasks --
    # the prior doc survives byte-for-byte.
    assert sup.run.read_tasks() == PRIOR_TASKS_DOC

    # (b) with no approach budget left, the empty_plan guard's `replan`
    # target falls straight through budget_or_approach_exhausted to the
    # terminal failed tail -- not a silent "succeeded".
    assert final_state == "failed"
    status = sup.run.read_status()
    assert status["state"] == "failed"

    # (c) EVENT_VIOLATION and EVENT_AGENT_FAULT are both recorded, naming
    # the offending task id in their errors.
    violations = _events(root, ce.EVENT_VIOLATION)
    faults = _events(root, ce.EVENT_AGENT_FAULT)
    assert violations, "no EVENT_VIOLATION recorded for the rejected proposal"
    assert faults, "no EVENT_AGENT_FAULT recorded once retries were exhausted"
    assert any(ev.get("hat") == "planning" for ev in violations)
    assert any(ev.get("hat") == "planning" for ev in faults)
    assert any(
        offending_id in err
        for ev in violations + faults
        for err in ev.get("errors", [])
    ), "no recorded error names the offending task id"


def test_the_planning_step_advances_via_the_empty_plan_guard(tmp_path):
    """Same rejection, asserted at the FSM-edge level rather than only the
    outcome level: `_advance("planning", "replan", "empty_plan")` is the
    edge the table declares for a plan ingestion failure, and this is what
    actually fires -- not some other coincidental route to `failed`."""
    root = tmp_path / "run"
    sup = _supervisor(root, [_bad_cycle_plan])
    sup.run.write_tasks(dict(PRIOR_TASKS_DOC))

    seen_edges: list[tuple] = []
    real_advance = sup._advance

    def spy_advance(state, target, guard):
        seen_edges.append((state, target, guard))
        return real_advance(state, target, guard)

    sup._advance = spy_advance  # type: ignore[method-assign]

    asyncio.run(sup.run_job())

    assert ("planning", "replan", "empty_plan") in seen_edges
