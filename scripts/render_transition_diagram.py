#!/usr/bin/env python3
"""Render `src/ralphd/engine/transitions.py`'s non-vigilant table as the
```mermaid`` `stateDiagram-v2` block `docs/architecture.md` embeds between
the `<!-- BEGIN GENERATED TRANSITION DIAGRAM -->` /
`<!-- END GENERATED TRANSITION DIAGRAM -->` markers.

Task 005 (PRD requirement C.1, finding 4): the FSM diagram in the docs is
not hand-drawn — this script (and `tests/test_transitions.py`,
which calls the same function) is the only place that turns
`transitions.table_for(...)`'s data into a diagram, so a changed edge shows
up as doc drift instead of silently going stale, exactly like
`scripts/render_transition_table.py` already does for the markdown table.

Usage:
    python scripts/render_transition_diagram.py            # print the block
    python scripts/render_transition_diagram.py --write     # rewrite the
                                                             # block in
                                                             # place inside
                                                             # docs/architecture.md
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ralphd.engine import transitions  # noqa: E402

BEGIN_MARKER = "<!-- BEGIN GENERATED TRANSITION DIAGRAM (scripts/render_transition_diagram.py) -->"
END_MARKER = "<!-- END GENERATED TRANSITION DIAGRAM -->"

ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"


def _edges() -> list[tuple[str, str, str]]:
    """One `(state, target, guard)` tuple per declared edge of the
    non-vigilant table (`transitions.table_for(vigilant=False)`), in
    `transitions.ALL_STATES` order -- the same variant
    `scripts/render_transition_table.py` labels `"both"`/plain rows, so
    every edge that is legal without any opt-in flag (including
    `review -> cure` and `review -> paused`) is here, and no
    vigilant-only/peer-only edge is."""
    table = transitions.table_for(vigilant=False)
    edges: list[tuple[str, str, str]] = []
    for state in transitions.ALL_STATES:
        for edge in table.get(state, ()):
            edges.append((state, edge.target, edge.guard))
    return edges


def render() -> str:
    """Return the full markdown block, markers and mermaid fence included."""
    lines = [BEGIN_MARKER, ""]
    lines.append(
        f"Generated from `src/ralphd/engine/transitions.py` (the "
        f"non-vigilant table, `transitions.table_for(vigilant=False)`) by "
        f"`scripts/render_transition_diagram.py` — do not hand-edit this "
        f"block; regenerate it with `python "
        f"scripts/render_transition_diagram.py --write` and let "
        f"`tests/test_transitions.py` catch drift."
    )
    lines.append("")
    edges = _edges()
    touched = {state for state, _target, _guard in edges} | {
        target for _state, target, _guard in edges
    }
    # Declare states that no non-vigilant edge ever mentions (as a source
    # or a target) explicitly, so they still show up in the diagram --
    # `verify`/`plan_review`/`verdict_gate` are only reachable through the
    # vigilant/peer-plan-review/peer-verdict-gate deltas, not the plain
    # table this diagram renders, so no `_edges()` line ever names them.
    # (`paused` needs no such line: `review -> paused` / `paused -> review`
    # already put it in `touched`.)
    lines.append("```mermaid")
    lines.append("stateDiagram-v2")
    lines.append(f"    [*] --> {transitions.ENTRY_STATE}")
    for state in transitions.STEP_STATES:
        if state not in touched:
            lines.append(f"    state {state}")
    for state, target, guard in edges:
        lines.append(f"    {state} --> {target} : {guard}")
    for terminal in transitions.RUN_TERMINAL_STATES:
        lines.append(f"    {terminal} --> [*]")
    lines.append("```")
    lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def write_into_doc() -> None:
    text = ARCHITECTURE_DOC.read_text()
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER) + len(END_MARKER)
    new_text = text[:start] + render().rstrip("\n") + text[end:]
    ARCHITECTURE_DOC.write_text(new_text)


if __name__ == "__main__":
    if "--write" in sys.argv[1:]:
        write_into_doc()
    else:
        print(render(), end="")
