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

Task 002 (PRD requirement B): the SAME four proofs, extended to the two
LEXICAL ingestion refusals -- `_unenumerated_quantifier_rewrite` and
`_clause_count_overrun_rewrite`, wired into `lint_proposal` the identical
way (`-- suggested rewrite: <text>`), reusing the exact SAME legality gate
(`_legal_rewrite`/`_clause_lint_refusals`) the six shapes above already
proved lint-clean. `test_the_unenumerated_quantifier_suggestion_is_itself_
accepted` and `test_the_clause_count_overrun_suggestion_is_itself_accepted`
cover proof 2 for these two lints (fixtures shared, not reconstructed,
with `tests/test_criteria_bound_lint.py`'s own must-catch pins);
`test_the_unenumerated_quantifier_suggestion_reaches_the_planners_next_
prompt` and `test_the_clause_count_overrun_suggestion_reaches_the_planners_
next_prompt` cover proof 4 -- the offending clause and the lint's own name
(`unenumerated-quantifier` / `clause-count-overrun`) ride beside the
rewrite in the SAME prompt.md message.

Task 002's SECOND attempt (its first attempt's `validationNotes`: the two
lexical rewrites were lint-clean but not usable -- the quantifier rewrite
threw away the clause's own demand and spliced mid-verb, the clause-count
rewrite deleted the overflow clause) adds section 2c: one test per faulted
defect, proving the quantifier rewrite keeps the offending clause verbatim
and only ADDS a named bounding slot, and that the clause-count rewrite
folds every clause into the bound rather than dropping one.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from ralphd.engine import plan_ingestion as pi
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json
from tests.conftest import supervisor_with_runner
from tests.test_criteria_bound_lint import (
    FIVE_CLAUSE_CRITERIA,
    SEVEN_CLAUSE_CRITERIA,
    TASK_012_013_EVERY_REPHRASING_CRITERIA,
    TASK_023_EACH_SHIPPED_PIECE_CRITERIA,
)


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
# 2b. task 002 (requirement B): the SAME legality proof, for the two
# LEXICAL ingestion refusals (the unenumerated-universal quantifier and
# the clause-count overrun) rather than the six structurally-unsatisfiable
# shapes above -- `_unenumerated_quantifier_rewrite` and
# `_clause_count_overrun_rewrite`, wired into `lint_proposal`'s own error
# strings the identical way (`-- suggested rewrite: <text>`), and proved
# legal the identical way: fed straight back through `lint_proposal` as a
# whole task's `successCriteria`, no fresh refusal. The three fixtures
# below are the SAME tracked fixtures `tests/test_criteria_bound_lint.py`
# uses to pin the two lints' must-catch behaviour -- not reconstructions.
# ---------------------------------------------------------------------------

_QUANTIFIER_OFFENDING_CLAUSES = [
    ("each-shipped-piece", TASK_023_EACH_SHIPPED_PIECE_CRITERIA),
    ("every-rephrasing", TASK_012_013_EVERY_REPHRASING_CRITERIA),
    ("no-anywhere", "No leftover debug prints appear anywhere in the codebase."),
]


@pytest.mark.parametrize("label,criteria",
                         _QUANTIFIER_OFFENDING_CLAUSES,
                         ids=[label for label, _ in _QUANTIFIER_OFFENDING_CLAUSES])
def test_the_unenumerated_quantifier_suggestion_is_itself_accepted(label, criteria):
    errors = pi.lint_proposal({"tasks": [_task("001", criteria)]})
    matches = [e for e in errors if "unenumerated-quantifier" in e]
    assert matches, (criteria, errors)
    message = matches[0]
    # The clause and the lint name ride beside the rewrite in one message.
    offending_clause = pi._unenumerated_quantifier_clause(criteria)
    assert offending_clause in message
    assert "suggested rewrite:" in message
    suggestion = _suggestion_for(criteria)
    assert suggestion != criteria
    payload = {"tasks": [_task("001", suggestion)]}
    assert pi.lint_proposal(payload) == []
    assert pi.lint_own_terminal_state(payload) == []
    assert pi.validate_proposal_shape({"version": 1, "tasks": [
        {"id": "001", "title": "t", "successCriteria": suggestion}]}) == []


def test_the_clause_count_overrun_suggestion_is_itself_accepted():
    errors = pi.lint_proposal({"tasks": [_task("050", SEVEN_CLAUSE_CRITERIA)]})
    matches = [e for e in errors if "clause-count-overrun" in e]
    assert matches, errors
    message = matches[0]
    assert "The petition file is untouched" in message  # the offending clause
    assert "suggested rewrite:" in message
    suggestion = _suggestion_for(SEVEN_CLAUSE_CRITERIA, task_id="050")
    assert suggestion != SEVEN_CLAUSE_CRITERIA
    # The rewrite FOLDS rather than deletes (task 002's second attempt, the
    # first attempt's `validationNotes`): every clause of the original
    # criteria still appears, the last ones joined into one clause, and the
    # result is under the bound. The criteria's own first four clauses stay
    # verbatim -- `FIVE_CLAUSE_CRITERIA`'s first four sentences.
    assert pi._clause_count_overrun(suggestion) is None
    for clause in pi._checkable_clauses(SEVEN_CLAUSE_CRITERIA):
        head, _, rest = clause.partition(" ")
        assert f"{head.lower()} {rest}" in suggestion.lower(), (clause, suggestion)
    assert suggestion.startswith(
        ". ".join(pi._checkable_clauses(FIVE_CLAUSE_CRITERIA)[:4]) + "."
    ), suggestion
    payload = {"tasks": [_task("050", suggestion)]}
    assert pi.lint_proposal(payload) == []
    assert pi.lint_own_terminal_state(payload) == []
    assert pi.validate_proposal_shape({"version": 1, "tasks": [
        {"id": "050", "title": "t", "successCriteria": suggestion}]}) == []


# ---------------------------------------------------------------------------
# 2c. task 002, SECOND attempt: the rewrite is a usable rewrite, not merely
# a lint-clean string. Its first attempt's `validationNotes` faulted three
# concrete defects, and there is one test per defect below:
#   - the quantifier rewrite DROPPED the clause's own demand ("Every widget
#     works." lost `works`) and named no bounding command at all;
#   - it produced ungrammatical output when the subject trim cut into a
#     verb ("The review model must reject is the one ...");
#   - the clause-count rewrite DELETED the overflow clause of a six-clause
#     "A exists. ... F exists." criteria instead of rewriting it.
# ---------------------------------------------------------------------------

_A_TO_F_SIX_CLAUSE_CRITERIA = (
    "A exists. B exists. C exists. D exists. E exists. F exists."
)


@pytest.mark.parametrize("label,criteria",
                         _QUANTIFIER_OFFENDING_CLAUSES + [
                             ("every-widget-works", "Every widget works.")],
                         ids=[label for label, _ in _QUANTIFIER_OFFENDING_CLAUSES]
                         + ["every-widget-works"])
def test_the_quantifier_rewrite_keeps_the_clauses_own_demand(label, criteria, tmp_path):
    """The clause's own words -- subject, verb and the demand it makes --
    survive into the rewrite verbatim; the rewrite only ADDS the bound the
    lint is missing, as a CONCRETE, already-runnable observable
    (`pytest`) -- never a backticked fill-in slot that only tells the
    planner to go name a bound itself (cure-01-01, PRD requirement B.1,
    #135: the first cure of this exact gap emitted precisely that
    fill-in slot -- `` `<the one file or command that lists the widget
    set>` `` -- in the actual next planner prompt, and this test asserted
    it verbatim).

    The "every-widget-works" case additionally folds in this task's OWN
    named regressions (no new collected test item is added -- the fast
    tier's collected count is pinned, `tests/test_tier_budgets.py`) for
    both "Every widget works." and "Each handler returns a value.": each
    proves that the offered rewrite preserves the offending clause's own
    demand, is a concrete bounded observable with no unresolved
    placeholder, and is itself accepted by the unchanged applicable lints.

    #209 defect 1: that second half used to drive two real loop runs and
    assert the rewrite arrived in `iterations/0002/prompt.md` beside "Your
    previous plan-proposal.json was rejected" -- the refusal itself. The
    rewrite is now APPLIED in a pre-pass instead of quoted as advice, so
    there is no rejected-proposal re-ask to carry it: the loop-level half
    asserts the bounded criteria reached `tasks.json` on the FIRST planning
    iteration, that no planning `contract.violation` was recorded at all,
    and that iteration 0002 is the worker's. Every direct-rewrite
    assertion in the first half, and every one in this loop over the two
    named regressions, is kept verbatim -- what the rewrite says is
    unchanged; only where it is delivered changed."""
    clause, rewrite = pi._unenumerated_quantifier_rewrite(criteria)
    # The demand survives: every word of the offending clause is still
    # there, in order, except the bare scope adverb "anywhere".
    kept = [
        word for word in clause.rstrip(".").split(" ")
        if word.lower() != "anywhere"
    ]
    assert rewrite.startswith(" ".join(kept).rstrip(".") + ","), (clause, rewrite)
    # ... and the rewrite names a CONCRETE bounding command -- no
    # unresolved `<...>` placeholder left for the planner to fill in --
    # that is itself accepted by the unchanged quantifier lint.
    assert not re.search(r"`<[^>]+>`", rewrite), rewrite
    assert "pytest" in rewrite, rewrite
    assert pi._is_bounded_quantifier_clause(rewrite), rewrite
    assert pi.lint_proposal({"tasks": [_task("001", rewrite)]}) == []

    if label != "every-widget-works":
        return

    for named_criteria in ("Every widget works.", "Each handler returns a value."):
        named_clause, named_rewrite = pi._unenumerated_quantifier_rewrite(named_criteria)
        assert named_clause.rstrip(".") in named_rewrite
        assert not re.search(r"`<[^>]+>`", named_rewrite), named_rewrite
        assert "pytest" in named_rewrite
        assert pi._is_bounded_quantifier_clause(named_rewrite), named_rewrite
        payload = {"tasks": [_task("001", named_rewrite)]}
        assert pi.lint_proposal(payload) == []
        assert pi.lint_own_terminal_state(payload) == []
        assert pi.validate_proposal_shape({"version": 1, "tasks": [
            {"id": "001", "title": "t", "successCriteria": named_rewrite}]}) == []

        def _bad_named_plan(sup: LoopSupervisor, _criteria=named_criteria) -> IterationResult:
            atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
                {"id": "001", "title": "t1", "successCriteria": _criteria},
            ]})
            return _ok("planned\n" + _pointer_line())

        root = tmp_path / f"run-{named_criteria[:5]}"
        sup = _supervisor(root, [_bad_named_plan, _finish_task, _review_ok])
        _bounded(sup.run_job())
        # The rewrite lands as the task's own criteria on the FIRST planning
        # iteration, and the planning contract was never violated at all --
        # `contract.violation` is the event every re-ask emits, so its
        # absence IS the absence of the re-ask, and iteration 0002 is the
        # worker's rather than a second attempt at the same proposal.
        assert sup.run.read_tasks()["tasks"][0]["successCriteria"] == named_rewrite
        assert _planning_violations(root) == []
        assert _iteration_phase(root, 2) == "worker"
        repaired = [e for e in _events(root)
                    if e["type"] == pi.CRITERIA_REPAIRED_EVENT]
        assert len(repaired) == 1, repaired
        assert repaired[0]["source"] == "plan-ingestion"
        assert repaired[0]["hat"] == "planning"
        assert repaired[0]["attempt"] == 1
        assert repaired[0]["taskId"] == "001"
        assert repaired[0]["oldCriteria"] == named_criteria
        assert repaired[0]["newCriteria"] == named_rewrite
        assert repaired[0]["lints"] == ["unenumerated-quantifier"]


def test_the_quantifier_rewrite_is_a_grammatical_sentence():
    """No mid-verb splice: the rewrite of the tracked every-rephrasing
    fixture is the clause plus a bounding phrase, never the subject-trim
    wreckage the first attempt emitted ("The review model must reject is
    the one ...")."""
    _clause, rewrite = pi._unenumerated_quantifier_rewrite(
        TASK_012_013_EVERY_REPHRASING_CRITERIA)
    assert "reject is" not in rewrite, rewrite
    assert rewrite.startswith(
        TASK_012_013_EVERY_REPHRASING_CRITERIA.rstrip(".") + ","), rewrite
    assert rewrite.endswith(".")


def test_the_clause_count_rewrite_folds_rather_than_deleting_a_clause():
    """The exact shape the first attempt got wrong: a six-clause
    "A exists. ... F exists." criteria comes back with `F exists` still in
    it, folded into the final clause rather than deleted.

    #209 defect 1 folds in the COMBINED case, which no other node covered:
    a criteria tripping BOTH lexical lints at once is repaired in one
    `repair_criteria` call, both lint names are recorded in the order they
    acted, and the result draws no refusal at all -- so a proposal is never
    accepted on a criteria that merely traded one lint for another."""
    overrun = pi._clause_count_overrun_rewrite(_A_TO_F_SIX_CLAUSE_CRITERIA)
    assert overrun is not None
    clause_count, overflow_clause, rewrite = overrun
    assert clause_count == 6
    assert overflow_clause == "F exists"
    assert "F exists" in rewrite, rewrite
    for letter in "ABCDE":
        assert f"{letter} exists" in rewrite, (letter, rewrite)
    assert pi._clause_count_overrun(rewrite) is None
    assert pi.lint_proposal({"tasks": [_task("060", rewrite)]}) == []

    both = "Every widget works. " + _A_TO_F_SIX_CLAUSE_CRITERIA
    repaired = pi.repair_criteria(both)
    assert repaired is not None
    lints, criteria = repaired
    assert lints == ["unenumerated-quantifier", "clause-count-overrun"]
    assert "pytest" in criteria
    for letter in "ABCDEF":
        assert f"{letter} exists" in criteria, (letter, criteria)
    assert pi._clause_count_overrun(criteria) is None
    assert pi.lint_proposal({"tasks": [_task("061", criteria)]}) == []


def test_both_lexical_rewrite_fallbacks_are_themselves_legal():
    """The far end of each ladder (used only if bounding or folding the
    criteria still leaves it refused) is lint-clean, same discipline as
    `test_every_shapes_own_default_rewrite_is_legal` above.

    #209 defect 1 folds in THE losslessness guard, the single most
    important line of that fix. Neither rewrite can return `None` once its
    own lint has fired -- each ends its ladder on one of these two
    constants -- and `_OVERRUN_REWRITE_FALLBACK` replaces the WHOLE
    criteria, which is exactly "a deletion dressed as a rewrite ... one
    that quietly drops a deliverable" (task 002's own recorded verdict on
    its first attempt, the comment above that constant). Lint-clean as
    advice is fine; APPLIED it destroys the criterion. So `repair_criteria`
    must return `None` -- falling back to the plain refusal, which is only
    wasteful -- whenever the computed rewrite IS either constant, and both
    branches are reached by a real fixture here rather than asserted in
    the abstract."""
    for fallback in (pi._QUANTIFIER_REWRITE_FALLBACK,
                     pi._OVERRUN_REWRITE_FALLBACK):
        assert pi._clause_lint_refusals(fallback) == [], fallback
        assert pi.lint_proposal({"tasks": [_task("070", fallback)]}) == []

    # The quantifier ladder's own fallback: a clause carrying a quantifier
    # AND an unsatisfiable empty-range demand, so bounding it with `pytest`
    # is still refused and the ladder gives up on the fixed constant.
    quantifier_dead_end = "The audit range must be empty for every widget."
    _clause, rewrite = pi._unenumerated_quantifier_rewrite(quantifier_dead_end)
    assert rewrite == pi._QUANTIFIER_REWRITE_FALLBACK
    assert pi.repair_criteria(quantifier_dead_end) is None

    # The overrun ladder's own fallback: seven clauses, one of which is
    # itself unsatisfiable, so no fold is lint-clean.
    overrun_dead_end = (
        "clause 1. clause 2. clause 3. clause 4. clause 5. "
        "the audit range must be empty. clause 7")
    _count, _overflow, rewrite = pi._clause_count_overrun_rewrite(overrun_dead_end)
    assert rewrite == pi._OVERRUN_REWRITE_FALLBACK
    assert pi.repair_criteria(overrun_dead_end) is None
    assert pi.repair_criteria(
        overrun_dead_end, lints=("clause-count-overrun",)) is None
    # ... so today's refusal survives verbatim for it, which is the whole
    # point of returning `None` rather than applying the constant.
    assert any("clause bound" in e for e in pi.lint_proposal(
        {"tasks": [_task("071", overrun_dead_end)]}))


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


def _events(root) -> list[dict]:
    """This run's `events.jsonl`, read straight off disk -- the per-module
    two-liner every other in-process module in this tree keeps its own copy
    of (`tests/test_contract_ladder.py`'s `_events`)."""
    path = root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


def _planning_violations(root) -> list[dict]:
    """Every planning contract violation this run recorded. #209 defect 1:
    `enforce_plan_ingestion` emits one of these per refused attempt, so an
    empty list is the positive statement that the proposal was accepted
    first time -- the property the nodes below used to prove by looking for
    the re-ask preamble in the next iteration's `prompt.md`."""
    return [e for e in _events(root)
            if e["type"] == "contract.violation" and e.get("hat") == "planning"]


def _iteration_phase(root, number: int) -> str:
    """The phase the engine recorded for iteration `number`, from the
    `meta.json` it writes for every iteration."""
    meta = json.loads(
        (root / "iterations" / f"{number:04d}" / "meta.json").read_text())
    return meta["phase"]


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


def _bad_quantifier_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1",
         "successCriteria": TASK_023_EACH_SHIPPED_PIECE_CRITERIA},
    ]})
    return _ok("planned\n" + _pointer_line())


def _bad_overrun_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "050", "title": "t1", "successCriteria": SEVEN_CLAUSE_CRITERIA},
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


# Task 002 (requirement B): the SAME loop-level reach, for the two lexical
# ingestion refusals -- an unenumerated-universal criterion and a
# clause-count overrun.
#
# #209 defect 1: both nodes below used to assert that reach as PROMPT TEXT
# -- "Your previous plan-proposal.json was rejected" plus the suggestion in
# `iterations/0002/prompt.md`, i.e. the refusal and the re-ask that are the
# defect. `selfdev-v022` burnt planning's whole 3-attempt ladder to
# `contract.agent_fault` on the overrun lint and hit it again after
# replanning: 10 planning phases for 3 approaches. The rewrite is now
# APPLIED (`plan_ingestion.repair_criteria`, a pre-pass over the proposal),
# so the reach these nodes prove is to `tasks.json` on the FIRST planning
# iteration, with `task.criteria_repaired` on `events.jsonl` naming both
# texts as the audit trail the prompt used to be. Every assertion about
# WHAT the rewrite says is kept -- it moved to the suggestion computed
# directly, which `test_the_*_suggestion_is_itself_accepted` above already
# pins is the same string the lint would have quoted. "No re-ask" is
# asserted positively rather than as the absence of the preamble in the next
# prompt: no `contract.violation` at all, and iteration 0002 is the worker's.
# That is both the stronger proof and the one the tree's own absence-assertion
# audit (`tests/test_prompt_sentinel_audit.py`) does not have to enumerate.

def test_the_unenumerated_quantifier_repair_reaches_tasks_json_not_a_reask(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root,
        [_bad_quantifier_plan, _finish_task, _review_ok])

    _bounded(sup.run_job())

    suggestion = _suggestion_for(TASK_023_EACH_SHIPPED_PIECE_CRITERIA)
    assert sup.run.read_tasks()["tasks"][0]["successCriteria"] == suggestion
    assert _planning_violations(root) == []
    assert _iteration_phase(root, 2) == "worker"
    repaired = [e for e in _events(root)
                if e["type"] == pi.CRITERIA_REPAIRED_EVENT]
    assert len(repaired) == 1, repaired
    assert repaired[0]["source"] == "plan-ingestion"
    assert repaired[0]["hat"] == "planning"
    assert repaired[0]["attempt"] == 1
    assert repaired[0]["lints"] == ["unenumerated-quantifier"]
    assert repaired[0]["oldCriteria"] == TASK_023_EACH_SHIPPED_PIECE_CRITERIA
    assert repaired[0]["newCriteria"] == suggestion


def test_the_clause_count_overrun_repair_reaches_tasks_json_not_a_reask(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root,
        [_bad_overrun_plan, _finish_task, _review_ok])

    _bounded(sup.run_job())

    suggestion = _suggestion_for(SEVEN_CLAUSE_CRITERIA, task_id="050")
    # The folded rewrite (task 002's second attempt) keeps the overflow
    # clause the refusal names instead of deleting it, and the WHOLE fold
    # is what lands as the task's criteria.
    assert "the petition file is untouched" in suggestion
    assert "the status is checked" in suggestion
    assert suggestion.startswith(FIVE_CLAUSE_CRITERIA.rstrip(".").rstrip())
    assert sup.run.read_tasks()["tasks"][0]["successCriteria"] == suggestion
    assert _planning_violations(root) == []
    assert _iteration_phase(root, 2) == "worker"
    repaired = [e for e in _events(root)
                if e["type"] == pi.CRITERIA_REPAIRED_EVENT]
    assert len(repaired) == 1, repaired
    assert repaired[0]["source"] == "plan-ingestion"
    assert repaired[0]["lints"] == ["clause-count-overrun"]
    assert repaired[0]["oldCriteria"] == SEVEN_CLAUSE_CRITERIA
    assert repaired[0]["newCriteria"] == suggestion
