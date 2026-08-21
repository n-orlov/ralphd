"""planning.md must spot-check the PRD's claims about the code before it turns
them into tasks (#41).

A PRD is written at some commit and read at a later one. Its `file:line`
citations, its "X is missing" and its "nothing does Y yet" are therefore claims,
not facts, and a plan built on a stale one spends whole iterations reimplementing
something that already exists — or designing around a shape the code no longer
has. So planning.md must, BEFORE task breakdown:

  * spot-check those claim shapes against the workspace,
  * say plainly that the code is the authority when the two disagree,
  * rescope an already-satisfied requirement from "implement X" to "prove X
    holds and add the check that keeps it holding" rather than dropping it,
  * record the corrections in the handoff notes so no later iteration
    re-derives them.

Each element gets its own test, so deleting any single one of them fails a
*named* test; one further test pins the ordering (the step must precede the
step that writes tasks.json).
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

# the step that writes the task plan; the fact-check must come before it
TASK_BREAKDOWN_ANCHOR = "3. Write the task state file"


def _planning_text() -> str:
    path = PROMPTS_DIR / "planning.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _fact_check_step() -> str:
    """The fact-check step's text, up to the next numbered step."""
    text = _planning_text()
    match = re.search(
        r"^2a\.\s(.*?)(?=^\d+[a-z]?\.\s)", text, re.DOTALL | re.MULTILINE)
    assert match, (
        "planning.md has no `2a.` step spot-checking the PRD's claims about "
        "the code (#41)")
    return match.group(1)


def _assert_all(rule: str, patterns: list[str]) -> None:
    step = _fact_check_step()
    missing = [p for p in patterns
               if not re.search(p, step, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        f"planning.md's fact-check step is missing the '{rule}' rule; "
        f"no match for: {missing}")


def test_planning_has_a_prd_fact_check_step():
    _assert_all("spot-check the PRD's claims", [
        r"Spot-check the PRD's factual claims about the code",
    ])


def test_the_fact_check_step_comes_before_task_breakdown():
    text = _planning_text()
    assert TASK_BREAKDOWN_ANCHOR in text, (
        f"planning.md no longer contains {TASK_BREAKDOWN_ANCHOR!r}; retarget "
        "this test at whichever step now writes tasks.json")
    fact_check_at = text.index("2a.")
    breakdown_at = text.index(TASK_BREAKDOWN_ANCHOR)
    assert fact_check_at < breakdown_at, (
        "the PRD fact-check step must be ordered BEFORE task breakdown, "
        f"but 2a. is at {fact_check_at} and task breakdown at {breakdown_at}")


def test_the_fact_check_step_names_the_stale_claim_shapes():
    _assert_all("which claims to check", [
        r"`file:line` citation",
        r"X is missing",
        r"nothing does Y",
    ])


def test_the_fact_check_step_says_the_code_is_the_authority():
    _assert_all("the code is the authority", [
        r"The CODE is the\s*\n?\s*authority",
    ])


def test_the_fact_check_step_names_the_rescope_rule():
    _assert_all("rescope an already-satisfied requirement", [
        r"Where a requirement already holds",
        r"rescope its task from",
        r"implement X",
        r"prove X holds and add the check that keeps it holding",
    ])


def test_the_fact_check_step_records_corrections_in_the_notes():
    _assert_all("record the corrections", [
        r"record the\s*\n?\s*correction in the notes",
    ])
