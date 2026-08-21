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
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
WORKER = PROMPTS_DIR / "worker.md"


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
