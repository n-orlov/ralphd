"""Task 009 (requirement A.5, #154): the plan's EIGHTH ingestion lint --
`plan_ingestion.lint_proposal()` now also refuses a `successCriteria`
clause that pins the literal text of a shell command, a wrapper, a polling
cadence or a "nothing else" clause, and one that both pins a launch form
and demands the same process's captured status. Both checks live in
`plan_ingestion.py` right above `lint_proposal` itself
(`_command_form_shape_clause`, `_launch_form_with_captured_status_clause`),
scoped to ONE clause at a time (`_CLAUSE_SPLIT_RE`) -- the same #86/#76
discipline every earlier lint in that module already follows.

`pi.TASK_205_SUCCESS_CRITERIA` is a reconstruction, not a recovered
original -- issue #154's own text says the `deck-phase3k` run directory
that held the real task 205 JSON "belong[s] to a different product" and is
not reachable from this sandbox, exactly the limitation task 012's
`TASK_042_SUCCESS_CRITERIA` already documents for v0.8 task 042. What IS
checked below, the same way `test_task_042_fixture_embeds_the_clause_that_
survives_in_git_history` checks its own fixture: the exact quoted clause
this wave's own PRD (`docs/prds/v0.19-a-note-is-not-a-rejection.md`, "What
this PRD deliberately does not ask") names as "the must-catch fixture for
A.5" is a literal substring of that tracked file (after collapsing its own
whitespace-only hard line wrap -- no word changes), and the same clause is
a literal substring of `TASK_205_SUCCESS_CRITERIA` itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from ralphd.engine import plan_ingestion as pi

_PRD_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "prds"
    / "v0.19-a-note-is-not-a-rejection.md"
)


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


def test_task_205_fixture_embeds_the_clause_the_prd_names_as_must_catch():
    """Proves the "verbatim" claim in `TASK_205_SUCCESS_CRITERIA`'s own
    comment against the tracked PRD file, rather than trusting the
    comment."""
    normalised = re.sub(r"\s+", " ", _PRD_PATH.read_text())
    clause_one = (
        "launched as `nohup timeout \u2026 &` and polled with `sleep 120` "
        "and nothing else"
    )
    clause_two = "the captured exit status"
    assert clause_one in normalised, "clause one is not in the cited PRD file"
    assert clause_two in normalised, "clause two is not in the cited PRD file"
    assert clause_one in pi.TASK_205_SUCCESS_CRITERIA, (
        "the fixture no longer embeds the launch-form clause it claims to "
        "quote verbatim"
    )
    assert clause_two in pi.TASK_205_SUCCESS_CRITERIA, (
        "the fixture no longer embeds the captured-status clause it claims "
        "to quote verbatim"
    )


def test_deck_3k_task_205_shape_is_the_must_catch_fixture():
    """The verbatim `deck-phase3k` task 205 shape -- a literal launch form,
    a literal polling cadence, a "nothing else" clause, and the same
    process's captured exit status, all in one criterion -- is refused by
    both the command-form checks and the launch-form/captured-status combo
    check, and by `lint_proposal` end to end."""
    clause = pi._command_form_shape_clause(pi.TASK_205_SUCCESS_CRITERIA)
    assert clause is not None, "the literal command-form shape was not caught"
    assert clause[0] == pi.TASK_205_SUCCESS_CRITERIA.rstrip(".")

    combo_clause = pi._launch_form_with_captured_status_clause(
        pi.TASK_205_SUCCESS_CRITERIA
    )
    assert combo_clause is not None, (
        "the launch-form + captured-status combo was not caught"
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
        Path(__file__).resolve().parents[1]
        / "src"
        / "ralphd"
        / "prompts"
        / "plan_review.md"
    ).read_text()
    normalised = re.sub(r"\s+", " ", text)
    assert (
        "a criterion names the observable, never the literal shell line, "
        'its wrapper, its polling cadence or a "nothing else" clause'
        in normalised
    )
