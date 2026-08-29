"""Task 016 (requirement B follow-through): `docs/architecture.md`'s
transition-table block is generated from `src/ralphd/engine/transitions.py`
by `scripts/render_transition_table.py`, not hand-copied.

Two checks:

* the block embedded in the doc between the `BEGIN`/`END GENERATED
  TRANSITION TABLE` markers is byte-identical to what the generator
  produces from the live code, right now;
* a mutation case: if the *code's* table were different from what it
  actually is (simulated here by rendering against a deliberately mangled
  copy of the module's data, not by editing the doc), the comparison would
  no longer match the doc -- proving the check actually looks at the
  table's content and is not a no-op string match against itself.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import render_transition_table as rtt  # noqa: E402

from ralphd.engine import transitions  # noqa: E402

ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"


def _embedded_block() -> str:
    text = ARCHITECTURE_DOC.read_text()
    start = text.index(rtt.BEGIN_MARKER)
    end = text.index(rtt.END_MARKER) + len(rtt.END_MARKER)
    return text[start:end]


def test_markers_present_exactly_once():
    text = ARCHITECTURE_DOC.read_text()
    assert text.count(rtt.BEGIN_MARKER) == 1
    assert text.count(rtt.END_MARKER) == 1
    assert text.index(rtt.BEGIN_MARKER) < text.index(rtt.END_MARKER)


def test_doc_block_matches_the_generator_right_now():
    """The doc is not a hand copy: what's embedded must equal what the
    generator produces from the code, byte for byte."""
    generated = rtt.render().rstrip("\n")
    embedded = _embedded_block()
    assert embedded == generated, (
        "docs/architecture.md's transition table block has drifted from "
        "src/ralphd/engine/transitions.py -- regenerate it with "
        "`python scripts/render_transition_table.py --write`"
    )


def test_generator_output_is_derived_from_the_table_not_a_constant():
    """Mutation case: prove the generator's output tracks the table's
    actual content rather than being a fixed string that happens to equal
    the doc. Render once against the real table, then render again after
    swapping one edge's guard name for a decoy and assert the two renders
    differ -- if they didn't, the "generated from code" claim above would
    be checking nothing.
    """
    baseline = rtt.render()

    real_table = transitions.TABLE
    mutated_worker = real_table["worker"][:-1] + (
        transitions.T("replan", "totally_decoy_guard_name_not_real"),
    )
    mutated_table = dict(real_table)
    mutated_table["worker"] = mutated_worker

    original_successors = transitions.successors

    def _mutated_successors(state, vigilant, peer_plan_review=False):  # noqa: ANN001
        if state != "worker":
            return original_successors(state, vigilant, peer_plan_review)
        table = mutated_table if not vigilant else {
            **mutated_table,
            "worker": mutated_table["worker"] + (
                transitions.T("verify", "completed_task_pending_verification"),
            ),
        }
        return table[state]

    transitions.successors = _mutated_successors
    try:
        mutated_render = rtt.render()
    finally:
        transitions.successors = original_successors

    assert mutated_render != baseline, (
        "mutating a table edge produced an identical render -- the "
        "generator is not actually reading transitions.py's data"
    )
    assert "totally_decoy_guard_name_not_real" in mutated_render
    assert "totally_decoy_guard_name_not_real" not in baseline
    # And the doc, unmutated, must still equal the (unmutated) baseline --
    # confirming the earlier "matches right now" check isn't vacuously
    # true because both sides always agree regardless of content.
    assert _embedded_block() == baseline.rstrip("\n")


def test_every_all_states_entry_appears_in_the_render():
    """Every state the table declares shows up in the rendered block, so a
    state added to ALL_STATES without a render row would be caught."""
    rendered = rtt.render()
    for state in transitions.ALL_STATES:
        assert f"`{state}`" in rendered, (
            f"state {state!r} is in transitions.ALL_STATES but missing "
            f"from the rendered transition table"
        )


def test_render_is_idempotent():
    assert rtt.render() == rtt.render()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
