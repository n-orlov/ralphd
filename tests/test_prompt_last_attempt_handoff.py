"""task-verify.md must turn the LAST failing validation attempt into a handoff
(#42).

A task's third failing verdict sets its status to `failed`
(engine/loop.py's `_verify_task`), and a `failed` task blocks
`<promise>COMPLETE</promise>` for the rest of the run. Whether that dead end is
recoverable depends entirely on what the verifier left behind in
`validationNotes`: a worker can only carve the residual gap into a new task if
the notes say what the gap actually is, which criteria already hold (so it does
not redo them), and what "done" would look like. So on its last available
attempt the verifier must write:

  * the residual gap, in one sentence,
  * which criteria DID verify, with the evidence that showed it,
  * a concrete follow-up task -- a title plus successCriteria -- closing just
    that gap.

Each element is asserted separately, so deleting any single one of them fails a
*named* test; two further tests pin the trigger condition (the last attempt is
recognisable from `validationAttempts`) and the ordering (the handoff step comes
after the failing-verdict step it refines).
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

# the step that records an ordinary failing verdict; the handoff refines it
FAILING_VERDICT_ANCHOR = "4. If ANY criterion is not met"


def _task_verify_text() -> str:
    path = PROMPTS_DIR / "task-verify.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _last_attempt_step() -> str:
    """The last-attempt handoff step's text, up to the next heading/step."""
    text = _task_verify_text()
    match = re.search(
        r"^5\.\s(.*?)(?=^\d+[a-z]?\.\s|^#)", text, re.DOTALL | re.MULTILINE)
    assert match, (
        "task-verify.md has no `5.` step describing what a failing verdict on "
        "the LAST available validation attempt must hand off (#42)")
    return match.group(1)


def _assert_all(rule: str, patterns: list[str]) -> None:
    step = _last_attempt_step()
    missing = [p for p in patterns
               if not re.search(p, step, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        f"task-verify.md's last-attempt step is missing the '{rule}' rule; "
        f"no match for: {missing}")


def test_task_verify_has_a_last_attempt_handoff_step():
    _assert_all("the step exists and is scoped to the last attempt", [
        r"LAST available attempt",
        r"validationNotes",
    ])


def test_the_last_attempt_step_says_how_to_recognise_the_last_attempt():
    _assert_all("recognise the last attempt", [
        r"validationAttempts",
        r"third miss marks it `failed`",
    ])


def test_the_last_attempt_step_requires_the_residual_gap_in_one_sentence():
    _assert_all("name the residual gap in one sentence", [
        r"name the residual gap\s*\n?\s*in one sentence",
    ])


def test_the_last_attempt_step_requires_the_criteria_that_did_verify():
    _assert_all("list which criteria verified, with evidence", [
        r"which criteria DID verify and with what\s*\n?\s*evidence",
    ])


def test_the_last_attempt_step_requires_a_concrete_follow_up_task():
    _assert_all("propose a follow-up task", [
        r"propose a concrete\s*\n?\s*follow-up task",
        r"title plus\s*\n?\s*successCriteria",
        r"closing just that gap",
    ])


def test_the_last_attempt_step_says_why_the_handoff_matters():
    _assert_all("why a precise handoff matters", [
        r"instead of the loop stalling on a `failed`",
    ])


def test_the_last_attempt_step_follows_the_failing_verdict_step():
    text = _task_verify_text()
    assert FAILING_VERDICT_ANCHOR in text, (
        f"task-verify.md no longer contains {FAILING_VERDICT_ANCHOR!r}; "
        "retarget this test at whichever step records a failing verdict")
    verdict_at = text.index(FAILING_VERDICT_ANCHOR)
    handoff_at = text.index("5. If this failing verdict")
    assert verdict_at < handoff_at, (
        "the last-attempt handoff step must come after the step that records "
        f"a failing verdict, but the verdict step is at {verdict_at} and the "
        f"handoff step at {handoff_at}")
