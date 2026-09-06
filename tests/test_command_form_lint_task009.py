"""Task 009 (requirement A.5, #154): the plan's EIGHTH ingestion lint --
`plan_ingestion.lint_proposal()` now also refuses a `successCriteria`
clause that pins the literal text of a shell command, a wrapper, a polling
cadence or a "nothing else" clause, and a criterion that both pins a launch
form and demands the same process's captured status. Both checks live in
`plan_ingestion.py` right above `lint_proposal` itself
(`_command_form_shape_clause`, `_launch_form_with_captured_status_clause`),
the first scoped to ONE clause at a time (`_CLAUSE_SPLIT_RE`) -- the same
#86/#76 discipline every earlier lint in that module already follows -- the
second deliberately spanning the whole criterion, because the must-catch
fixture splits its two halves over two sentences.

`pi.TASK_205_SUCCESS_CRITERIA` is the VERBATIM `deck-phase3k` task 205
criterion the PRD names as the must-catch fixture, not a reconstruction and
not the PRD's abbreviation of it: the task record was recovered from that
run's own `tasks.json` and is tracked in this repo at
`tests/data/deck-phase3k-task-205.json` (the same fetch-and-track
discipline task 013 uses for `tests/data/issue-143-body.md`), and
`test_task_205_fixture_is_the_verbatim_deck_3k_criterion` below asserts
direct string equality against it. The recovery command is recorded in
`plan_ingestion.py`'s own block comment above the constant; the run
directory it came from is host-side and not on this container's
filesystem, which is why the record is tracked here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ralphd.engine import plan_ingestion as pi

_REPO = Path(__file__).resolve().parents[1]
_TASK_205_RECORD = _REPO / "tests" / "data" / "deck-phase3k-task-205.json"
_PRD_PATH = _REPO / "docs" / "prds" / "v0.19-a-note-is-not-a-rejection.md"


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


def test_task_205_fixture_is_the_verbatim_deck_3k_criterion():
    """Direct equality with the tracked `deck-phase3k` task 205 record's
    own `successCriteria` -- the "verbatim" claim is checked, not trusted.
    The PRD's abbreviated quote of the same criterion (its `…` elides the
    middle of the launch line) is shown to be an abbreviation OF this text:
    every segment of that quote appears in the fixture, in order."""
    record = json.loads(_TASK_205_RECORD.read_text())
    assert record["id"] == "205"
    assert pi.TASK_205_SUCCESS_CRITERIA == record["successCriteria"], (
        "the fixture is no longer byte-for-byte the tracked deck-phase3k "
        "task 205 criterion"
    )

    prd = re.sub(r"\s+", " ", _PRD_PATH.read_text())
    abbreviated = (
        "launched as `nohup timeout \u2026 &` and polled with `sleep 120` "
        "and nothing else"
    )
    assert abbreviated in prd, "the PRD no longer quotes the must-catch shape"
    cursor = 0
    for segment in abbreviated.split("\u2026"):
        found = pi.TASK_205_SUCCESS_CRITERIA.find(segment, cursor)
        assert found >= 0, f"the fixture does not contain {segment!r} in order"
        cursor = found + len(segment)
    assert "captured exit status" in pi.TASK_205_SUCCESS_CRITERIA
    assert "captured exit status" in prd


def test_deck_3k_task_205_shape_is_the_must_catch_fixture():
    """The verbatim `deck-phase3k` task 205 criterion -- a literal launch
    form, a literal polling cadence, a "nothing else" clause, and the same
    process's captured exit status -- is refused by both the command-form
    checks and the launch-form/captured-status combo check, and by
    `lint_proposal` end to end."""
    clause = pi._command_form_shape_clause(pi.TASK_205_SUCCESS_CRITERIA)
    assert clause is not None, "the literal command-form shape was not caught"
    assert "launched as `nohup timeout 7200" in clause[0]

    combo = pi._launch_form_with_captured_status_clause(
        pi.TASK_205_SUCCESS_CRITERIA
    )
    assert combo is not None, (
        "the launch-form + captured-status combo was not caught"
    )
    launch_clause, status_clause = combo
    assert "launched as" in launch_clause
    assert "captured exit status" in status_clause
    assert launch_clause != status_clause, (
        "task 205 splits the two halves over two sentences -- the combo "
        "check must span clauses, not require both in one"
    )

    payload = {
        "tasks": [_basic_task("205", successCriteria=pi.TASK_205_SUCCESS_CRITERIA)]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "205" in e and "command form" in e for e in errors
    ), f"no command-form refusal in {errors!r}"
    assert any(
        "205" in e and "captured status" in e for e in errors
    ), f"no launch-form/captured-status refusal in {errors!r}"


def test_observable_naming_sibling_is_accepted():
    """The must-pass sibling this task's own criteria names by example:
    `summary.log.exitstatus` holds the script's exit status -- an
    observable, no launch form, no cadence, no exclusivity clause -- trips
    neither check."""
    criteria = (
        "`summary.log.exitstatus` holds the script's exit status. Task 206 "
        "lands at the code sha this task's own commit produces."
    )
    assert pi._command_form_shape_clause(criteria) is None
    assert pi._launch_form_with_captured_status_clause(criteria) is None

    payload = {"tasks": [_basic_task("206", successCriteria=criteria)]}
    errors = pi.lint_proposal(payload)
    assert not any("command form" in e or "captured status" in e for e in errors), (
        f"observable-naming sibling was wrongly refused: {errors!r}"
    )


def test_equivalent_command_form_phrasings_are_refused():
    """The same three bans, phrased the way a planner actually reaches for
    them rather than the way task 205 happened to: an exactness demand on a
    literal command, a wrapper named with "with"/"using" or bare, and a
    cadence written as an imperative. Each is a refusal from
    `lint_proposal` end to end, not merely from the helper."""
    cases = {
        "Execute `ci/stability.sh 10` exactly, then publish the report": "command",
        "Run `ci/stability.sh 10` verbatim and commit its output": "command",
        "The launch line is exactly `nohup ci/stability.sh 10 &`": "command",
        "The gate is wrapped with `bash -c '...'` and its report lands": "wrapper",
        "The gate run is wrapped in bash -c so the log survives": "wrapper",
        "The wrapper is `timeout 7200` and the report lands in docs/": "wrapper",
        "Poll every 120 seconds until the report directory exists": "cadence",
        "The gate is checked every 30 seconds while it runs": "cadence",
    }
    for i, (sentence, expected) in enumerate(sorted(cases.items())):
        criteria = f"{sentence}. The report directory holds summary.log."
        match = pi._command_form_shape_clause(criteria)
        assert match is not None, f"not caught: {sentence!r}"
        assert expected in match[1], (
            f"{sentence!r} was caught, but its remedy text {match[1]!r} does "
            f"not name the {expected} shape"
        )
        errors = pi.lint_proposal(
            {"tasks": [_basic_task(f"3{i:02d}", successCriteria=criteria)]}
        )
        assert any("command form" in e for e in errors), (
            f"lint_proposal did not refuse {sentence!r}: {errors!r}"
        )


def test_a_criterion_that_merely_names_the_ban_is_accepted():
    """The words "polling cadence" alone are not a cue: this wave's own
    task 009 criterion talks ABOUT the ban and must still ingest."""
    criteria = (
        "lint_proposal() refuses a criterion that pins the literal text of "
        "a shell command, a wrapper, a polling cadence or a \"nothing "
        "else\" clause, and the test node ids are named in the completion."
    )
    assert pi._command_form_shape_clause(criteria) is None
    errors = pi.lint_proposal({"tasks": [_basic_task("009", successCriteria=criteria)]})
    assert not any("command form" in e for e in errors), (
        f"a criterion naming the ban was wrongly refused: {errors!r}"
    )


def test_wrapper_clause_alone_is_refused():
    """A pinned wrapper, independent of the deck-3k combo, is its own
    banned shape."""
    criteria = (
        "The gate is wrapped in `bash -c '...'` and its report is committed. "
        "Task 207 lands at the code sha this task's own commit produces."
    )
    clause = pi._command_form_shape_clause(criteria)
    assert clause is not None
    assert "wrapper" in clause[1]


def test_polling_cadence_clause_alone_is_refused():
    """A pinned polling cadence, independent of the deck-3k combo, is its
    own banned shape."""
    criteria = (
        "The report exists and the launch is polled with `sleep 120` "
        "throughout. Task 208 lands at the code sha this task's own commit "
        "produces."
    )
    clause = pi._command_form_shape_clause(criteria)
    assert clause is not None
    assert "cadence" in clause[1]


def test_plan_review_states_the_same_command_form_ban():
    """`plan_review.md` states the same command-form ban `lint_proposal`
    enforces, in its own text, so the plan-review hat refuses the shape
    the lint refuses even if a clause is close enough to slip past the
    lint's own regexes."""
    text = (
        _REPO / "src" / "ralphd" / "prompts" / "plan_review.md"
    ).read_text()
    normalised = re.sub(r"\s+", " ", text)
    assert (
        "a criterion names the observable, never the literal shell line, "
        'its wrapper, its polling cadence or a "nothing else" clause'
        in normalised
    )
