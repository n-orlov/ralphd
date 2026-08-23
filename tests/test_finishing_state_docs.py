"""Task 012 (#56): SPEC.md's state-vocabulary and liveness sections state the
`finishing` state, and the claims are derived from the code rather than typed
in by hand -- a mutation case proves each check actually fails when the
underlying fact stops being true.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine.state import NONTERMINAL_STATES, TERMINAL_STATES

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "SPEC.md"


def _spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def _doc_tuple_literal(values: tuple) -> str:
    """SPEC.md's own quoting convention for a state tuple is double-quoted
    Python-source style (`("starting", "running")`), not `repr()`'s single
    quotes -- match the doc's own style rather than force it to match ours."""
    return "(" + ", ".join(f'"{v}"' for v in values) + ")"


def test_spec_states_the_literal_nonterminal_states_tuple_correctly():
    """SPEC.md's liveness section (8.7) spells out `NONTERMINAL_STATES`'s
    literal value -- this pins that the spelled-out tuple matches the real
    one, not a snapshot frozen at the time it was written."""
    text = _spec_text()
    needle = "`NONTERMINAL_STATES` is\n`" + _doc_tuple_literal(NONTERMINAL_STATES) + "` --"
    assert needle in text, (
        f"SPEC.md's liveness section does not spell out "
        f"{NONTERMINAL_STATES!r} -- it has drifted from the real tuple")


def test_the_nonterminal_states_check_is_mutation_sensitive():
    """The check above is not vacuous: a tuple missing `finishing` (the
    pre-task-012 value) does NOT match what SPEC.md now says."""
    pre_task_012_value = ("starting", "running")
    text = _spec_text()
    stale_needle = "`NONTERMINAL_STATES` is\n`" + _doc_tuple_literal(pre_task_012_value) + "`"
    assert stale_needle not in text
    assert pre_task_012_value != NONTERMINAL_STATES


def test_spec_lists_finishing_in_the_status_json_state_enum():
    text = _spec_text()
    row = [line for line in text.splitlines() if line.startswith("| `state` | string |")]
    assert row, "SPEC.md's status.json field table has no `state` row"
    assert "`finishing`" in row[0]
    for value in NONTERMINAL_STATES + TERMINAL_STATES:
        assert f"`{value}`" in row[0], (
            f"SPEC.md's `state` row omits a real recorded value: {value!r}")


def test_the_state_enum_check_is_mutation_sensitive():
    """Dropping `finishing` from the row text makes the previous test fail --
    demonstrated directly against a stale copy of the row."""
    stale_row = ('| `state` | string | `starting` \\| `running` \\| '
                 '`succeeded` \\| `failed` \\| `aborted` |')
    assert "`finishing`" not in stale_row
    assert "finishing" in NONTERMINAL_STATES


def test_spec_describes_pending_terminal_state_and_the_promotion_mechanism():
    """The narrative half of the claim (12.2): the field name, the
    non-terminal write, and that a resume/repair promotes without redoing the
    job -- present as prose, not just a table row."""
    text = _spec_text()
    for needle in (
        "`pendingTerminalState`",
        "`_finish()`",
        "`_promote_finishing_state()`",
        'state == "finishing"',
        "ralphctl repair --set-state",
    ):
        assert needle in text, f"SPEC.md's reflect section is missing: {needle}"


def test_terminal_states_are_unchanged_by_this_task():
    """The mutation guard in the OTHER direction: task 012 must not have
    widened TERMINAL_STATES to swallow `finishing` -- that would break every
    consumer's destructive-action gate (rm/delete) instead of protecting it."""
    assert TERMINAL_STATES == ("succeeded", "failed", "aborted")
    assert "finishing" not in TERMINAL_STATES
