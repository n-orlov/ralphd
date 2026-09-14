"""Task 005 (PRD requirement B.1): an ingestion refusal message carries a
CONCRETE LEGAL rewrite of the offending clause beside the rule it broke --
`plan_ingestion._unsatisfiable_shape_clause`'s third return value, spliced
into `lint_proposal`'s error string as "-- suggested rewrite: <text>".

Four things are proved here. The first two are the task's own criteria; the
third is the gap this task's FIRST attempt was rejected on (its
`validationNotes`: the emitted rewrite was itself refused when fed back into
`lint_proposal`, because the old implementation substituted a fragment for
the matched span alone and left the rest of the illegal demand standing);
the fourth is the loop-level reach of the suggestion.

1. `test_two_different_offending_clauses_yield_two_different_suggestions`
   and `test_the_rule_broken_and_the_rewrite_both_appear_in_the_same_message`
   -- the suggestion is DERIVED from the offending clause's own subject, so
   two different offending clauses yield two different suggestions, and the
   rule broken and the rewrite ride in one message.
2. `test_the_emitted_suggestion_is_itself_accepted` (parametrised over all
   six banned shapes, including the two clauses the first attempt's
   rejection quoted verbatim) -- the suggestion, fed straight back through
   `lint_proposal` and `lint_own_terminal_state` as a task's whole
   `successCriteria`, draws no refusal at all. This is what makes it a
   *legal* rewrite rather than merely a different string.
3. `test_every_shapes_own_default_rewrite_is_legal` -- the fallback end of
   the candidate ladder (the shape's default subject plus its predicate) is
   lint-clean for each of the six shapes, so a clause whose own words offer
   no usable subject still gets a legal suggestion.
4. `test_the_suggested_rewrite_reaches_the_planners_next_prompt` -- loop
   level, mirrors `tests/test_plan_ingestion_rejection.py`'s
   `_ScriptedRunner` scaffolding (kept as its own copy, exactly as every
   other `_run_job_core`-driving test module in this tree does): a rejected
   first plan-proposal.json, followed by a second planning attempt whose
   OWN prompt.md (the file `_run_iteration_once` writes for every
   iteration) is read straight off disk and asserted to contain the
   suggested rewrite text.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import plan_ingestion as pi
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json
from tests.conftest import supervisor_with_runner


def _task(task_id: str, criteria: str) -> dict:
    return {"id": task_id, "title": f"do thing {task_id}", "successCriteria": criteria}


def _suggestion_for(criteria: str, task_id: str = "001") -> str:
    """The suggested rewrite `lint_proposal` emits for `criteria`, lifted out
    of the refusal message exactly as a planner reading its next prompt
    would read it (the message quotes it with `repr`)."""
    errors = pi.lint_proposal({"tasks": [_task(task_id, criteria)]})
    matches = [e for e in errors if "suggested rewrite:" in e]
    assert matches, (criteria, errors)
    quoted = matches[0].split("suggested rewrite:", 1)[1].strip()
    return quoted.strip("'\"")


# ---------------------------------------------------------------------------
# 1. the suggestion is derived from the offending clause, beside the rule
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
    # Each rewrite keeps the subject ITS OWN clause named -- "report" for the
    # first, "wave" for the second -- so the suggestion is clause-specific,
    # not one shared per-shape template.
    assert "The report" in suggestion_001
    assert "report" not in suggestion_002
    assert "The wave" in suggestion_002
    assert "wave" not in suggestion_001


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
    assert "The task lands, committed and verified by its own evidence." in message


# ---------------------------------------------------------------------------
# 2. the emitted suggestion is itself LEGAL -- fed back, it draws no refusal
# ---------------------------------------------------------------------------

# One offending clause per banned shape, plus the two clauses task 005's
# first-attempt rejection quoted verbatim ("The audit range must be empty.",
# "The row cites the commit that writes it.").
_OFFENDING_CLAUSES = [
    ("later-commit", "The report must contain the final sha."),
    ("later-commit-suffix", "This is the final commit of the wave."),
    ("predeclared-state",
     "The petition state is recorded (both prior petitions REFUTED)."),
    ("record-cites-own-commit", "The row cites the commit that writes it."),
    ("record-cites-own-commit-verbatim",
     "A record row must cite the commit that writes the row."),
    ("empty-range", "The audit range must be empty."),
    ("empty-range-git", "git log over docs/ shows no commits."),
    ("exactly-once", "The task is committed exactly once."),
    ("http-status",
     "The task appends endpoint, substance, and observed HTTP status."),
    ("notification-ledger",
     "The notification ledger entry is appended by the task."),
]


@pytest.mark.parametrize("label,criteria",
                         _OFFENDING_CLAUSES,
                         ids=[label for label, _ in _OFFENDING_CLAUSES])
def test_the_emitted_suggestion_is_itself_accepted(label, criteria):
    suggestion = _suggestion_for(criteria)
    # It is a rewrite, not a restatement.
    assert suggestion != criteria
    assert suggestion.endswith(".")
    payload = {"tasks": [_task("001", suggestion)]}
    # Fed straight back in as a whole successCriteria, the suggestion draws
    # no refusal from ANY of the module's lints -- including the
    # structurally-unsatisfiable one that produced it.
    assert pi.lint_proposal(payload) == []
    assert pi.lint_own_terminal_state(payload) == []
    assert pi.validate_proposal_shape({"version": 1, "tasks": [
        {"id": "001", "title": "t", "successCriteria": suggestion}]}) == []


def test_every_shapes_own_default_rewrite_is_legal():
    """The fallback end of the candidate ladder: a clause whose own words
    offer no usable subject falls back on the shape's default subject plus
    its predicate, which must be lint-clean for each of the six shapes."""
    for pattern, _alternative, predicate, default_subject in (
        pi._UNSATISFIABLE_SHAPE_CHECKS
    ):
        fallback = f"{default_subject} {predicate}."
        assert pi._clause_lint_refusals(fallback) == [], (pattern.pattern, fallback)


# ---------------------------------------------------------------------------
# 3. loop level: the suggestion reaches the planner's NEXT prompt
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


# Task 072 (steer 001 §2): bound the loop-driving await below instead of
# a bare `asyncio.run`. The two known forever-awaits are `_step_paused`'s
# `await self._gate()` (parked on the `asyncio.Event` only `POST /pause`'s
# `loop.pause()` clears) and a `_ScriptedRunner` script whose repeating
# last step never reaches a terminal phase -- but THIS module's own
# script is deliberately built to avoid the second shape already (see
# `_finish_task`'s own comment above: it exists precisely so the script
# reaches a terminal `complete` verdict before `_review_ok`'s repeat could
# ever run again), and the plan-ingestion rejection this module drives
# through never routes through `review`/`worker` stagnation's `paused`
# edge either, so this module does not reproduce either historical
# deadlock shape. Bounding the await anyway is defense against a FUTURE
# regression, not evidence one is live here today.
def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_the_suggested_rewrite_reaches_the_planners_next_prompt(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root,
        [_bad_final_commit_plan, _good_plan, _finish_task, _review_ok])

    _bounded(sup.run_job())

    # The second planning attempt is iteration 0002 (iteration 0001 was the
    # rejected first attempt) -- `_run_iteration_once` writes this run's
    # OWN prompt.md for every iteration, unconditionally.
    second_prompt = (root / "iterations" / "0002" / "prompt.md").read_text()
    assert "Your previous plan-proposal.json was rejected" in second_prompt
    assert "structurally unsatisfiable" in second_prompt
    assert "suggested rewrite:" in second_prompt
    # The concrete rewrite text itself, derived from the offending clause
    # ("This is the final commit of the wave.") this run's own bad plan
    # named -- not a generic per-shape template -- and legal on its own
    # terms, which `test_the_emitted_suggestion_is_itself_accepted` proves
    # for this same clause.
    suggestion = _suggestion_for("This is the final commit of the wave.")
    assert suggestion == "The wave is recorded at the code sha this task lands."
    assert suggestion in second_prompt
