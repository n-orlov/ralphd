"""Task 022 (requirement E.1, #115 lint half): the sixth ingestion lint --
`plan_ingestion.lint_proposal` now also flags a `successCriteria` clause
carrying a universal quantifier over an unenumerated set
(every/each/all/"no ... anywhere"), and criteria exceeding the declared
~5-checkable-clause bound. Both checks live in `plan_ingestion.py` right
above `lint_proposal` itself (`_unenumerated_quantifier_clause`,
`_clause_count_overrun`), scoped to ONE clause at a time
(`_CLAUSE_SPLIT_RE`) -- the same #86/#76 discipline every earlier lint in
that module already follows, so a criterion long enough to be useful does
not trip on an unrelated mention elsewhere in the same string.

The two must-catch quantifier fixtures below are not reconstructions: both
phrases are RECOVERED verbatim (whitespace-flattened) from this
repository's own tracked prompt files --
`src/ralphd/prompts/plan_review.md` ("each shipped piece" with the pieces
left unnamed, its own named example of the banned shape) and
`src/ralphd/prompts/planning.md` ("reject every rephrasing of X", its own
named example of an unenumerable aspiration). `test_..._is_recovered_
verbatim` below pins each recovery before any lint behaviour is asserted,
exactly like `tests/test_stranding_lints.py` does for its own fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import plan_ingestion as pi


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


REPO_ROOT = Path(__file__).resolve().parents[1]


def _flattened(rel_path: str) -> str:
    import re

    return re.sub(r"\s+", " ", (REPO_ROOT / rel_path).read_text())


# ---------------------------------------------------------------------------
# Recovery pins -- the two must-catch phrases exist verbatim in tracked
# prompt files, not merely in this test module's own imagination.
# ---------------------------------------------------------------------------

EACH_SHIPPED_PIECE_PHRASE = "each shipped piece"
EVERY_REPHRASING_PHRASE = "reject every rephrasing of X"


def test_each_shipped_piece_phrase_is_recovered_verbatim_from_plan_review_md():
    assert EACH_SHIPPED_PIECE_PHRASE in _flattened("src/ralphd/prompts/plan_review.md")


def test_every_rephrasing_phrase_is_recovered_verbatim_from_planning_md():
    assert EVERY_REPHRASING_PHRASE in _flattened("src/ralphd/prompts/planning.md")


# ---------------------------------------------------------------------------
# (f) unenumerated universal quantifier -- must-catch
# ---------------------------------------------------------------------------

TASK_023_EACH_SHIPPED_PIECE_CRITERIA = (
    "The closing report names the commit sha for " + EACH_SHIPPED_PIECE_PHRASE + "."
)

TASK_012_013_EVERY_REPHRASING_CRITERIA = (
    "The review model must " + EVERY_REPHRASING_PHRASE + " that a worker tries."
)


def test_v016_task_023s_each_shipped_piece_wording_is_caught():
    payload = {"tasks": [_basic_task("023", successCriteria=TASK_023_EACH_SHIPPED_PIECE_CRITERIA)]}
    errors = pi.lint_proposal(payload)
    assert any(
        "023" in e and "unenumerated set" in e and "each shipped piece" in e
        for e in errors
    ), errors


def test_v016_tasks_012_013s_every_rephrasing_wording_is_caught():
    payload = {"tasks": [_basic_task("012", successCriteria=TASK_012_013_EVERY_REPHRASING_CRITERIA)]}
    errors = pi.lint_proposal(payload)
    assert any(
        "012" in e and "unenumerated set" in e and "rephrasing" in e
        for e in errors
    ), errors


def test_a_no_anywhere_quantifier_over_an_unenumerated_set_is_caught():
    payload = {"tasks": [_basic_task(
        "040",
        successCriteria="No leftover debug prints appear anywhere in the codebase.",
    )]}
    errors = pi.lint_proposal(payload)
    assert any("040" in e and "unenumerated set" in e for e in errors), errors


# ---------------------------------------------------------------------------
# (f) unenumerated universal quantifier -- must-pass
# ---------------------------------------------------------------------------

def test_a_single_defining_command_bounds_the_quantifier_and_is_accepted():
    """The must-pass fixture this task's own successCriteria names by
    example: naming the single file that DEFINES the set quantified over
    bounds it -- must never trip."""
    payload = {"tasks": [_basic_task(
        "005",
        successCriteria="Every test in tests/test_x.py passes.",
    )]}
    assert pi.lint_proposal(payload) == []


def test_naming_the_members_explicitly_bounds_the_quantifier_and_is_accepted():
    payload = {"tasks": [_basic_task(
        "006",
        successCriteria="Each of tasks 001, 002 and 003 lands its own commit.",
    )]}
    assert pi.lint_proposal(payload) == []


def test_a_backticked_single_command_bounds_all_and_is_accepted():
    payload = {"tasks": [_basic_task(
        "007",
        successCriteria=(
            "All changes are committed, confirmed by "
            "`git status --porcelain` being empty."
        ),
    )]}
    assert pi.lint_proposal(payload) == []


def test_no_without_anywhere_is_not_a_quantifier_clause_and_is_accepted():
    """'No test in tests/test_x.py fails' is bounded on its own merits (a
    single defining file), but also never trips the 'no ... anywhere' cue
    in the first place -- both escapes are exercised."""
    payload = {"tasks": [_basic_task(
        "008",
        successCriteria="No test in tests/test_x.py fails.",
    )]}
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (f) elsewhere-bounded quantifier -- Task 006 (PRD requirement B.2, #135):
# an anaphoric quantifier ("each of them") is accepted when some OTHER
# clause of the SAME criteria is itself bounded on its own merits, while a
# bare unenumerated noun phrase ("each shipped piece") elsewhere in the
# same criteria stays refused -- the escape is narrow, so B.2 cannot be
# read as disabling the lint.
# ---------------------------------------------------------------------------


def test_an_anaphoric_quantifier_bound_by_an_earlier_clause_is_accepted():
    """'Each of them' points back at the two files the previous clause
    names with backticks -- bounded elsewhere in the same criteria, so it
    is no longer a hard refusal."""
    payload = {"tasks": [_basic_task(
        "062",
        successCriteria=(
            "This commit touches `src/a.py` and `src/b.py`. "
            "Each of them gets a new docstring."
        ),
    )]}
    assert pi.lint_proposal(payload) == []


def test_an_anaphoric_bound_elsewhere_does_not_rescue_an_unrelated_bare_quantifier():
    """The anaphoric escape only rescues the anaphoric clause itself --
    'each shipped piece' names no anaphor and no defining command/file of
    its own, so it is STILL refused even though an earlier clause in the
    very same criteria is bounded and even rescues its OWN anaphoric
    quantifier by the same rule."""
    payload = {"tasks": [_basic_task(
        "063",
        successCriteria=(
            "This commit touches `src/a.py` and `src/b.py`. "
            "Each of them gets a new docstring. "
            "The closing report names the commit sha for each shipped piece."
        ),
    )]}
    errors = pi.lint_proposal(payload)
    matches = [e for e in errors if "063" in e and "unenumerated set" in e]
    assert matches, errors
    assert all("each shipped piece" in e for e in matches), matches
    assert all("Each of them" not in e for e in matches), matches


# ---------------------------------------------------------------------------
# (g) clause-count overrun -- must-catch / must-pass
# ---------------------------------------------------------------------------

SEVEN_CLAUSE_CRITERIA = (
    "The fix lands in its own commit. The notification is logged. "
    "The report is committed. The test module is green. "
    "The sha is recorded. The petition file is untouched. "
    "The status is checked."
)

FIVE_CLAUSE_CRITERIA = (
    "The fix lands in its own commit. The notification is logged. "
    "The report is committed. The test module is green. "
    "The sha is recorded."
)


def test_a_criterion_past_the_five_clause_bound_is_caught():
    payload = {"tasks": [_basic_task("050", successCriteria=SEVEN_CLAUSE_CRITERIA)]}
    errors = pi.lint_proposal(payload)
    assert any(
        "050" in e and "7 checkable clauses" in e and "clause bound" in e
        for e in errors
    ), errors


def test_a_criterion_at_exactly_the_five_clause_bound_is_accepted():
    payload = {"tasks": [_basic_task("051", successCriteria=FIVE_CLAUSE_CRITERIA)]}
    assert pi.lint_proposal(payload) == []


def test_a_short_single_sentence_criterion_is_accepted():
    payload = {"tasks": [_basic_task("052", successCriteria="Thing 052 is verifiably done.")]}
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# Both checks scoped per-clause, not over the whole string (#86 discipline)
# ---------------------------------------------------------------------------

def test_a_quantifier_word_in_one_clause_does_not_taint_an_unrelated_clause():
    """A criterion whose SUBJECT elsewhere is an unrelated bounded mention
    of 'every'/'each' must not make an unrelated open-ended clause's
    absence of a quantifier get flagged, and vice versa -- each clause is
    judged on its own; the mere presence of the word somewhere in the
    string is not enough on its own to prove the offending clause is the
    one actually reported."""
    payload = {"tasks": [_basic_task(
        "060",
        successCriteria=(
            "Every test in tests/test_x.py passes. "
            "The closing report names the commit sha for each shipped piece."
        ),
    )]}
    errors = pi.lint_proposal(payload)
    matches = [e for e in errors if "060" in e and "unenumerated set" in e]
    assert matches, errors
    assert all("each shipped piece" in e for e in matches), matches
    assert all("Every test in tests/test_x.py passes" not in e for e in matches), matches


# ---------------------------------------------------------------------------
# Wired through the existing plan re-ask ladder, same shape as
# tests/test_stranding_lints.py's own end-to-end wiring tests.
# ---------------------------------------------------------------------------

def _pointer(path: str) -> str:
    return json.dumps({"proposal_path": path, "summary": "a plan"})


def _write(tmp_path, name: str, doc) -> None:
    text = doc if isinstance(doc, str) else json.dumps(doc)
    (tmp_path / name).write_text(text)


def test_an_unenumerated_quantifier_reprompts_then_recovers_end_to_end(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("023", successCriteria=TASK_023_EACH_SHIPPED_PIECE_CRITERIA)]})
    attempts = []

    def respond(attempt, prior_errors):
        attempts.append(prior_errors)
        if attempt == 1:
            _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("023")]})
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 2
    assert attempts[0] is None
    assert attempts[1] and any("unenumerated set" in e for e in attempts[1])


def test_a_clause_count_overrun_reprompts_then_recovers_end_to_end(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("050", successCriteria=SEVEN_CLAUSE_CRITERIA)]})
    attempts = []

    def respond(attempt, prior_errors):
        attempts.append(prior_errors)
        if attempt == 1:
            _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("050")]})
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 2
    assert attempts[0] is None
    assert attempts[1] and any("clause bound" in e for e in attempts[1])
