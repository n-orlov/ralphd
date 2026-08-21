"""worker.md must tell a worker how to resolve a terminally failed task (#36).

A task whose validation attempts are exhausted is recorded `failed`, and a
`failed` task can never satisfy the completion signal's "every task is
terminal" gate. A worker that does not know what to do with it grinds until
the loop's last resort fires: fail the approach and replan the whole job from
scratch, against a workspace where the work is already finished and with no
review pass at the end. So the completion-signal section must state

  1. that a `failed` task blocks <promise>COMPLETE</promise> forever,
  2. why that is unsafe (approach replan over already-finished work),
  3. resolution A -- carve the residual gap into a NEW task,
  4. resolution B -- relabel `skipped` with a justification that does not claim
     the criteria were met and that leaves `validationAttempts` untouched,
  5. the prohibition on editing `successCriteria` to make a task pass.

Each element gets its own named test, so deleting any one of them fails a test
that says which rule went missing rather than a catch-all.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
WORKER = PROMPTS_DIR / "worker.md"


def _worker_text() -> str:
    assert WORKER.is_file(), f"missing prompt file {WORKER}"
    return WORKER.read_text()


def _completion_signal_section() -> str:
    """The text of worker.md's `## Completion signal` section."""
    text = _worker_text()
    m = re.search(r"^##\s+Completion signal\s*$(.*?)(?=^##\s|\Z)",
                  text, re.MULTILINE | re.DOTALL)
    assert m, "worker.md has no '## Completion signal' section"
    return m.group(1)


def _assert_all(rule: str, patterns: list[str]) -> None:
    section = _completion_signal_section()
    missing = [p for p in patterns
               if not re.search(p, section, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        "worker.md's completion-signal section is missing the "
        f"'{rule}' rule for a terminally failed task; no match for: {missing}")


def test_completion_signal_names_the_terminal_statuses():
    _assert_all("terminal statuses", [
        r"terminal status",
        r"`completed`",
        r"`skipped`",
    ])


def test_failed_task_is_stated_to_block_the_completion_signal_forever():
    _assert_all("failed blocks the signal forever", [
        r"task left `failed` blocks",
        r"forever",
        r"validation attempts are exhausted",
    ])


def test_the_danger_of_a_permanently_blocked_plan_is_spelled_out():
    _assert_all("why blocking forever is unsafe", [
        r"fail the approach and replan",
        r"from scratch",
        r"work is already finished",
        r"no review pass",
    ])


def test_resolution_a_carve_the_residual_gap_into_a_new_task():
    _assert_all("carve the gap into a new task", [
        r"residual gap",
        r"carve[^\n]*NEW task",
        r"stand as history",
    ])


def test_resolution_b_relabel_skipped_with_an_honest_justification():
    _assert_all("relabel skipped with a justification", [
        r"relabel the failed task `skipped`",
        r"justification",
        r"what its criteria required",
        r"which commits delivered",
        r"`skipped` is not a claim that the criteria were met",
    ])


def test_relabelling_skipped_leaves_validation_attempts_untouched():
    _assert_all("validationAttempts stays untouched", [
        r"[Ll]eave `validationAttempts` exactly as it stands",
    ])


def test_editing_success_criteria_to_pass_is_prohibited():
    _assert_all("no editing successCriteria", [
        r"[Nn]ever edit a task's `successCriteria` to make it pass",
    ])


def test_resolving_one_task_does_not_touch_another_task_status():
    _assert_all("do not touch other tasks", [
        r"never change another\s*\n?\s*task's status",
    ])
