"""Task 025 (E.4, #115 second cut): the verifier's injected context (its own
system prompt, `task-verify.md`, rendered into every verify iteration by
`LoopSupervisor.build_prompt`) names the notes file's harness-pinned
`## Standing rules` block and states, in the same file, that ENFORCING it
wave-wide is `review`'s job over the finished approach -- not a per-task
`successCriteria` clause for `verify` to grade. This removes the planner's
incentive (named in #115) to copy standing-rules obligations into every
task's own criteria "just so verify checks them": verify already does not
own that job, and now says so.

Prompt text only -- deliberately no engine-code assertions here (task 025's
own successCriteria bounds the diff to prompt text and the verify contract
shape, no new engine machinery); the companion suite
(`tests/test_prompt_standing_rules.py`, `tests/test_response_contracts.py`)
covers the rest of the standing-rules construction and the verify contract
shape respectively.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

# the same literal heading `test_prompt_standing_rules.py` pins for
# planning.md/worker.md -- verify must refer to it by the same literal text
# or a reader has no way to tell which block it means.
HEADING = "## Standing rules"


def _verify_text() -> str:
    path = PROMPTS_DIR / "task-verify.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def test_verify_names_the_literal_standing_rules_heading():
    text = _verify_text()
    assert HEADING in text, (
        f"task-verify.md never refers to the literal {HEADING!r} heading "
        "the notes file's pinned block uses -- the verifier has no way to "
        "recognise what 'the standing rules' means without it")


def test_verify_names_it_as_harness_pinned():
    text = _verify_text()
    assert re.search(r"harness-pinned", text, re.IGNORECASE), (
        "task-verify.md must call the block harness-pinned (task 107), the "
        "same wording the worker/planning prompts use for it")


def test_verify_states_review_owns_wave_wide_enforcement():
    text = _verify_text()
    assert re.search(
        r"review.{0,40}job", text, re.IGNORECASE | re.DOTALL), (
        "task-verify.md must state that enforcing the standing rules "
        "wave-wide is review's job")
    assert re.search(r"wave-wide", text, re.IGNORECASE), (
        "task-verify.md must say the enforcement is wave-wide (over the "
        "whole approach), not per task")


def test_verify_states_standing_rules_are_not_per_task_criteria():
    text = _verify_text()
    assert re.search(
        r"not\s+per-task\s+criteria|not\s+a\s+per-task\s+success",
        text, re.IGNORECASE) or re.search(
        r"successCriteria.{0,80}does not name", text, re.IGNORECASE), (
        "task-verify.md must say a standing rule is not itself a per-task "
        "success criterion, so the verifier does not reject a task for "
        "drifting from a standing rule its own successCriteria never named")


def test_verify_does_not_instruct_rejecting_for_unnamed_standing_rules():
    text = _verify_text()
    assert re.search(
        r"[Dd]o not reject a task for.{0,80}standing rule", text,
        re.DOTALL), (
        "task-verify.md must explicitly tell the verifier not to reject a "
        "task for a standing-rule drift its own criteria does not name")
