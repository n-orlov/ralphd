"""worker.md must state steering's delivery-once semantics and the durable-copy
rule that makes an operator note survive the iteration it lands in (#37).

The engine hands a steering note to a single iteration's prompt. If that
iteration acts on the note and then dies -- or if the note is a standing
instruction meant to govern the iterations that come after it -- the
instruction exists nowhere durable unless the worker writes it down. So the
prompt must say

  1. that a steering note is delivered to exactly one iteration,
  2. that the durable part must be copied into the notes file, under a heading
     naming the note, BEFORE acting on it,
  3. that the copy rule holds under either consumption rule (consumed on
     delivery, or consumed only on the carrying iteration's success), because
     standing instructions span many iterations either way -- so this module
     stays true whatever #34 lands.

Patterns are matched only inside the `## Operator steering` section, so a rule
that drifts out of it fails a named test instead of matching prose elsewhere.

Task 034 extends this module to the OTHER half of steering's durability: the
steering ledger the engine renders into every following iteration's prompt.
The two validating hats' prompts (`task-verify.md`, `review.md`) must state
the ledger's semantics themselves -- it informs, and a steer is NOT a waiver
(only `/config/amendments` waives) -- so a validator reading only its own
prompt still knows how to treat an applied or pending steer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
WORKER = PROMPTS_DIR / "worker.md"

# Task 034: the ledger's not-a-waiver sentence, as `engine.loop.
# STEERING_LEDGER_NOT_A_WAIVER` renders it, asserted in the prompts of the
# two hats that judge other hats' work.
NOT_A_WAIVER_PATTERNS = [
    r"A steer is NOT a waiver",
    r"only operator amendments in `/config/amendments`",
    r"waive an obligation",
]
LEDGER_HATS = ("task-verify", "review")


def _worker_text() -> str:
    assert WORKER.is_file(), f"missing prompt file {WORKER}"
    return WORKER.read_text()


def _steering_section() -> str:
    """The text of worker.md's `## Operator steering` section."""
    text = _worker_text()
    m = re.search(r"^##\s+Operator steering\s*$(.*?)(?=^##\s|\Z)",
                  text, re.MULTILINE | re.DOTALL)
    assert m, "worker.md has no '## Operator steering' section"
    return m.group(1)


def _assert_all(rule: str, patterns: list[str]) -> None:
    section = _steering_section()
    missing = [p for p in patterns
               if not re.search(p, section, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        "worker.md's operator-steering section is missing the "
        f"'{rule}' rule; no match for: {missing}")


def test_worker_has_an_operator_steering_section():
    assert _steering_section().strip(), (
        "worker.md's '## Operator steering' section is empty")


def test_a_steering_note_is_delivered_to_exactly_one_iteration():
    _assert_all("delivered to exactly one iteration", [
        r"delivered to exactly one iteration",
        r"injected into one iteration's prompt",
    ])


def test_the_durable_part_must_be_copied_into_the_notes_file():
    _assert_all("copy the durable part into the notes file", [
        r"copy its durable part into the notes\s*\n?\s*file",
        r"an iteration\s*\n?\s*which never saw the note can carry it out",
    ])


def test_the_copy_goes_under_a_heading_naming_the_note():
    _assert_all("under a heading naming the note", [
        r"under a heading that names the note",
        r"Steering:",
    ])


def test_the_copy_happens_before_acting_on_the_note():
    _assert_all("copy before acting", [
        r"[Bb]efore you act on a steering note",
        r"Do this first",
        r"Copy first",
    ])


def test_steering_still_outranks_the_prd_and_the_task_order():
    _assert_all("steering outranks the plan", [
        r"ahead of the PRD and the current task order",
    ])


def test_the_copy_rule_holds_under_consume_on_delivery():
    _assert_all("consume-on-delivery case", [
        r"consumed the moment it is delivered",
        r"(infra fault, timeout, abort)",
        r"nothing else\s*\n?\s*ever carries them",
    ])


def test_the_copy_rule_holds_under_consume_on_success_because_of_standing_instructions():
    _assert_all("consume-on-success case", [
        r"consumed only once its iteration completes",
        r"pending for the next one",
        r"durable-copy rule\s*\n?\s*still applies",
        r"standing.{0,40}instruction",
        r"govern many iterations",
    ])


def test_the_worker_is_told_not_to_guess_which_consumption_rule_is_in_force():
    _assert_all("do not guess the consumption rule", [
        r"cannot tell from inside which rule is in force",
    ])


def test_the_rules_list_points_at_the_steering_section():
    text = _worker_text()
    rules = re.search(r"^##\s+Rules\s*$(.*?)(?=^##\s|\Z)",
                      text, re.MULTILINE | re.DOTALL)
    assert rules, "worker.md has no '## Rules' section"
    assert re.search(r"`## Operator steering`", rules.group(1)), (
        "worker.md's steering bullet under '## Rules' must point at the "
        "'## Operator steering' section")


# -- Task 034: the steering ledger's semantics in the validating prompts ----

def _ledger_section(name: str) -> str:
    """The text of `name`.md's steering-ledger section, whitespace-collapsed
    so a reflowed line break can never fail a wording check."""
    path = PROMPTS_DIR / f"{name}.md"
    assert path.is_file(), f"missing prompt file {path}"
    m = re.search(r"^##\s+Steering ledger\b.*?$(.*?)(?=^##\s|\Z)",
                  path.read_text(), re.MULTILINE | re.DOTALL)
    assert m, f"{name}.md has no '## Steering ledger' section"
    return re.sub(r"\s+", " ", m.group(1))


@pytest.mark.parametrize("name", LEDGER_HATS)
def test_the_validating_prompts_have_a_steering_ledger_section(name):
    assert _ledger_section(name).strip(), (
        f"{name}.md's steering-ledger section is empty")


@pytest.mark.parametrize("name", LEDGER_HATS)
def test_the_not_a_waiver_sentence_is_in_both_validating_prompts(name):
    section = _ledger_section(name)
    missing = [p for p in NOT_A_WAIVER_PATTERNS
               if not re.search(p, section, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        f"{name}.md's steering-ledger section is missing the 'a steer is not "
        f"a waiver' rule; no match for: {missing}")


@pytest.mark.parametrize("name", LEDGER_HATS)
def test_the_validating_prompts_say_the_ledger_informs(name):
    section = _ledger_section(name)
    for pattern in (r"applied", r"partially-applied", r"pending",
                    r"conforming to an APPLIED steer",
                    r"[Ww]eigh a PENDING steer", r"stale"):
        assert re.search(pattern, section, re.DOTALL), (
            f"{name}.md's steering-ledger section must state the '{pattern}' "
            "half of the ledger's semantics")
