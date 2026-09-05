"""Task 005 (PRD requirement C.1, finding 4): `docs/architecture.md`'s FSM
diagram block is generated from `transitions.table_for(vigilant=False)` by
`scripts/render_transition_diagram.py`, never hand-drawn.

Checks, mirroring `tests/test_transition_table_docs.py`'s shape for the
markdown table:

* the mermaid block embedded in the doc between the `BEGIN`/`END GENERATED
  TRANSITION DIAGRAM` markers is byte-identical to what the generator
  produces from the live code, right now (and re-running the generator
  twice produces the identical block -- idempotent);
* every state in `transitions.STEP_STATES` appears in the diagram block;
* every declared edge of the non-vigilant table -- including
  `review -> cure` and `review -> paused` -- appears in the diagram block
  as a `source --> target : guard` line;
* a staleness case: if the doc's embedded block is missing one of those
  required edges (simulated by editing a copy of the rendered text, not
  the doc itself), the same assertion that passes against the real doc
  fails against the mangled copy -- proving the check actually reads the
  block's content rather than being a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import render_transition_diagram as rtd  # noqa: E402

from ralphd.engine import transitions  # noqa: E402

ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"


def _embedded_block() -> str:
    text = ARCHITECTURE_DOC.read_text()
    start = text.index(rtd.BEGIN_MARKER)
    end = text.index(rtd.END_MARKER) + len(rtd.END_MARKER)
    return text[start:end]


def _required_edge_lines() -> list[str]:
    table = transitions.table_for(vigilant=False)
    lines = []
    for state in transitions.ALL_STATES:
        for edge in table.get(state, ()):
            lines.append(f"{state} --> {edge.target} : {edge.guard}")
    return lines


def test_markers_present_exactly_once():
    text = ARCHITECTURE_DOC.read_text()
    assert text.count(rtd.BEGIN_MARKER) == 1
    assert text.count(rtd.END_MARKER) == 1
    assert text.index(rtd.BEGIN_MARKER) < text.index(rtd.END_MARKER)


def test_block_contains_a_mermaid_fence():
    block = _embedded_block()
    assert "```mermaid" in block
    assert "stateDiagram-v2" in block
    assert block.count("```mermaid") == 1
    assert block.rstrip().endswith(rtd.END_MARKER)


def test_doc_block_matches_the_generator_right_now():
    """The doc is not a hand copy: what's embedded must equal what the
    generator produces from the code, byte for byte."""
    generated = rtd.render().rstrip("\n")
    embedded = _embedded_block()
    assert embedded == generated, (
        "docs/architecture.md's transition diagram block has drifted from "
        "src/ralphd/engine/transitions.py -- regenerate it with "
        "`python scripts/render_transition_diagram.py --write`"
    )


def test_render_is_idempotent():
    """Re-running the generator against the unchanged table leaves the
    diagram block byte-identical (criterion 2)."""
    first = rtd.render()
    second = rtd.render()
    assert first == second


def test_every_step_state_appears_in_the_diagram():
    rendered = rtd.render()
    for state in transitions.STEP_STATES:
        assert state in rendered, (
            f"state {state!r} is in transitions.STEP_STATES but missing "
            f"from the rendered FSM diagram"
        )


def test_every_non_vigilant_edge_appears_in_the_diagram():
    rendered = rtd.render()
    required = _required_edge_lines()
    assert required, "sanity: the non-vigilant table declared no edges at all"
    for line in required:
        assert line in rendered, (
            f"edge {line!r} is a declared non-vigilant transition but is "
            f"missing from the rendered FSM diagram"
        )


def test_review_cure_and_review_paused_edges_appear():
    """Named explicitly by the criterion: the two edges task 004 added for
    finding 4 must show up in the diagram, not just be table entries."""
    rendered = rtd.render()
    assert "review --> cure : rejected_curable_pass_available" in rendered
    assert "review --> paused : rejected_last_approach" in rendered


def test_staleness_is_caught_when_a_required_edge_is_missing():
    """Mutation case: a diagram block missing one required edge line must
    fail the same containment check that passes against the real,
    up-to-date doc -- otherwise the check above would be vacuous."""
    rendered = rtd.render()
    required = _required_edge_lines()
    victim = "review --> cure : rejected_curable_pass_available"
    assert victim in required
    assert victim in rendered

    stale = rendered.replace(victim, "")
    assert victim not in stale, "sanity: the victim line was not actually removed"

    with pytest.raises(AssertionError):
        for line in required:
            assert line in stale


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
