"""Task 007 (PRD requirement B.3, #135): what a planning death COSTS, and
what its terminal reason SAYS.

PRD B.3 offers two options and asks the task to pick one and say which:
either a planning death costs iterations instead of an approach, or --
if the existing two-strike ingestion termination (`_step_replan`'s
`repeated_blocker_signature` guard, task 012/#124, reusing task 026's #61
mechanism) is kept exactly as it is -- the terminal reason must name the
lexical lint that fired and quote the offending clause, and the commit
message states plainly that the two-strike termination stays and that both
planning-death approaches count normally against `max_approaches`.

This module picks (and proves) the second option: the mechanism itself is
untouched (`tests/test_stagnation_signature.py`'s ordinary worker-stagnation
scenarios are unaffected -- this module never touches that branch), and
`_step_replan`'s ingestion-origin branch of `repeated_blocker_signature` now
surfaces the RAW `plan_ingestion.lint_proposal` error text (which already
names the specific lexical lint -- e.g. "quantifies over an unenumerated
set" -- and quotes the offending clause via `!r`) instead of the folded,
case-stripped comparison signature.

Scaffolding mirrors `tests/test_plan_ingestion_rejection.py`'s `_ScriptedRunner`
(stands in for `PiRunner`, no real subprocess) -- deliberately not shared via
import, exactly as every other `_run_job_core`-driving test module in this
tree keeps its own copy.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json
from tests.conftest import supervisor_with_runner


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
    r.duration_s = 30.0  # not an "instant" failure
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _pointer_line(path: str = "plan-proposal.json", summary: str = "planned") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


# The SAME unenumerated-quantifier clause every planning attempt resubmits --
# `plan_ingestion.lint_proposal` refuses this on lexical grounds
# ("quantifies over an unenumerated set") every single time, so the two
# approaches' normalised blocker signatures compare equal and the two-strike
# guard fires on the second one.
_OFFENDING_CLAUSE = "Each of the shipped features works correctly"


def _bad_quantifier_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1",
         "successCriteria": f"{_OFFENDING_CLAUSE}."},
    ]})
    return _ok("planned\n" + _pointer_line())


_supervisor = supervisor_with_runner(
    lambda sup, script: _ScriptedRunner(sup, script),
    run_id="v021-planning-death", iterations=20, max_approaches=2, vigilant=False,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0, contract_retry_max=0)


def test_a_repeated_planning_death_names_the_lexical_lint_and_quotes_the_clause(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, [_bad_quantifier_plan])

    final_state = asyncio.run(sup.run_job())

    assert final_state == "failed"
    status = sup.run.read_status()
    reason = status.get("reason") or ""

    # -- the terminal reason names the lexical lint and quotes the clause --
    assert "lexical lint" in reason
    assert _OFFENDING_CLAUSE in reason
    assert "quantifies over an unenumerated set" in reason

    # -- the two-strike termination is KEPT AS-IS: both planning-death
    # approaches count normally against max_approaches (option 2 of B.3,
    # stated here and in the commit message) -- the run ended AT approach 2
    # (never entering a third, even though this behaviour is unrelated to
    # whether a third approach would have existed), and both approaches'
    # signatures are recorded and match.
    assert status["approach"] == 2
    assert "both approaches count normally against max_approaches" in reason
    signatures = status.get("blockerSignatures") or {}
    assert signatures.get("1") == signatures.get("2")
    assert signatures["1"] is not None
    origins = status.get("blockerSignatureOrigins") or {}
    assert origins.get("1") == "ingestion"
    assert origins.get("2") == "ingestion"
    # The run is marked degraded -- an ingestion-origin repeat, task 012's
    # own rule, unchanged by this task.
    assert status.get("health") == "degraded"

    # tasks.json was never overwritten with the rejected proposal's tasks.
    tasks_doc = sup.run.read_tasks() or {}
    assert (tasks_doc.get("tasks") or []) == []
