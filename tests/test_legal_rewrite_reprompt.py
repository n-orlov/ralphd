"""Task 005 (PRD requirement B.1): an ingestion refusal message now carries
a CONCRETE legal rewrite of the offending clause beside the rule it broke
-- `plan_ingestion._unsatisfiable_shape_clause`'s new third return value,
spliced into `lint_proposal`'s error string as "-- suggested rewrite:
<text>". Two things are proved here, matching the task's two success
criteria:

1. `test_two_different_offending_clauses_yield_two_different_suggestions`
   -- unit level, `plan_ingestion.lint_proposal` directly: the suggestion
   is DERIVED from the offending clause's own text (the words around the
   matched span survive into the rewrite), not a fixed per-shape template
   -- two different offending clauses produce two different suggestions.
2. `test_the_suggested_rewrite_reaches_the_planners_next_prompt` -- loop
   level, mirrors `tests/test_plan_ingestion_rejection.py`'s
   `_ScriptedRunner` scaffolding (kept as its own copy, exactly as every
   other `_run_job_core`-driving test module in this tree does): a
   rejected first plan-proposal.json, followed by a second planning
   attempt whose OWN prompt.md (the file `_run_iteration_once` writes for
   every iteration) is read straight off disk and asserted to contain the
   suggested rewrite text -- proof the rewrite reaches the planner's next
   prompt, not just the in-process error list.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import plan_ingestion as pi
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json
from tests.conftest import supervisor_with_runner


def _task(task_id: str, criteria: str) -> dict:
    return {"id": task_id, "title": f"do thing {task_id}", "successCriteria": criteria}


# ---------------------------------------------------------------------------
# 1. unit level: two different offending clauses, two different suggestions
# ---------------------------------------------------------------------------

def test_two_different_offending_clauses_yield_two_different_suggestions():
    payload = {
        "tasks": [
            _task("001", "The report must contain the final sha."),
            _task("002", "This is the final commit of the wave."),
        ]
    }
    errors = pi.lint_proposal(payload)

    def _suggestion(task_id: str) -> str:
        matches = [e for e in errors if task_id in e and "suggested rewrite:" in e]
        assert matches, (task_id, errors)
        return matches[0].split("suggested rewrite:", 1)[1].strip()

    suggestion_001 = _suggestion("001")
    suggestion_002 = _suggestion("002")
    assert suggestion_001 != suggestion_002
    # Each rewrite is genuinely derived from ITS OWN clause's surrounding
    # words, not a shared fixed template: the text around the illegal span
    # survives into the suggestion for each clause, distinctly.
    assert "report must contain" in suggestion_001
    assert "of the wave" in suggestion_002


def test_the_rule_broken_and_the_rewrite_both_appear_in_the_same_message():
    payload = {"tasks": [_task("003", "The task is committed exactly once.")]}
    errors = pi.lint_proposal(payload)
    matches = [e for e in errors if "003" in e and "structurally unsatisfiable" in e]
    assert matches, errors
    message = matches[0]
    # The rule it broke (the existing per-shape advice) ...
    assert 'demands an "exactly once" commit count' in message
    # ... beside a concrete rewrite of the offending clause.
    assert "suggested rewrite:" in message
    assert "verified by its own evidence" in message


# ---------------------------------------------------------------------------
# 2. loop level: the suggestion reaches the planner's NEXT prompt
# ---------------------------------------------------------------------------

class _ScriptedRunner:
    """Stands in for PiRunner. Each script entry takes the supervisor and
    returns an IterationResult; the last entry repeats forever. Copied from
    `tests/test_plan_ingestion_rejection.py` (deliberately not shared via
    import -- every other `_run_job_core`-driving test module in this tree
    keeps its own copy)."""

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
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _pointer_line(path: str = "plan-proposal.json", summary: str = "planned") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


def _bad_final_commit_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1",
         "successCriteria": "This is the final commit of the wave."},
    ]})
    return _ok("planned\n" + _pointer_line())


def _good_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1", "successCriteria": "thing 001 is verifiably done"},
    ]})
    return _ok("planned\n" + _pointer_line())


def _finish_task(sup: LoopSupervisor) -> IterationResult:
    # Mirrors tests/test_engine_finishing_state.py's `_finish_task`: the
    # harness-owned `complete` verdict for the task the scheduler assigned,
    # so the run actually reaches a terminal state instead of stagnating
    # (and pausing forever) on a script whose last entry never does any
    # real worker/review work.
    task_id = sup.run.read_tasks()["tasks"][0]["id"]
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": task_id}))


def _review_ok(sup: LoopSupervisor) -> IterationResult:
    return _ok("looks good\n" + json.dumps({"verdict": "verified"}))


_supervisor = supervisor_with_runner(
    lambda sup, script: _ScriptedRunner(sup, script),
    run_id="task005-legal-rewrite-reprompt", iterations=12, max_approaches=2,
    vigilant=False, on_complete="exit", infra_retry_backoff_s=[0.0],
    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
    contract_retry_max=1)


def test_the_suggested_rewrite_reaches_the_planners_next_prompt(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root,
        [_bad_final_commit_plan, _good_plan, _finish_task, _review_ok])

    asyncio.run(sup.run_job())

    # The second planning attempt is iteration 0002 (iteration 0001 was the
    # rejected first attempt) -- `_run_iteration_once` writes this run's
    # OWN prompt.md for every iteration, unconditionally.
    second_prompt = (root / "iterations" / "0002" / "prompt.md").read_text()
    assert "Your previous plan-proposal.json was rejected" in second_prompt
    assert "structurally unsatisfiable" in second_prompt
    assert "suggested rewrite:" in second_prompt
    # The concrete rewrite text itself, derived from the offending clause
    # ("This is the final commit of the wave.") this run's own bad plan
    # named -- not a generic per-shape template.
    assert "commit at `<sha>` this task lands" in second_prompt
    assert "of the wave" in second_prompt
