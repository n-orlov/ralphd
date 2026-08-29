#!/usr/bin/env python3
"""Render `src/ralphd/engine/transitions.py`'s declared table as the
markdown block `docs/architecture.md` embeds between the
`<!-- BEGIN GENERATED TRANSITION TABLE -->` /
`<!-- END GENERATED TRANSITION TABLE -->` markers.

Task 016 (requirement B follow-through): the table in the docs is not a
hand copy — this script (and `tests/test_transition_table_docs.py`, which
calls the same function) is the ONLY place that turns `transitions.py`'s
data into prose, so a changed table entry shows up as doc drift instead of
silently going stale.

Usage:
    python scripts/render_transition_table.py            # print the block
    python scripts/render_transition_table.py --write     # rewrite the
                                                           # block in place
                                                           # inside
                                                           # docs/architecture.md
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ralphd.engine import transitions  # noqa: E402

BEGIN_MARKER = "<!-- BEGIN GENERATED TRANSITION TABLE (scripts/render_transition_table.py) -->"
END_MARKER = "<!-- END GENERATED TRANSITION TABLE -->"

ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"


def _rows() -> list[tuple[str, str, str, str, str]]:
    """One row per (state, hat, variant, target, guard), in table order.

    `variant` is `"both"` when the edge is present identically for
    vigilant and non-vigilant, `"vigilant only"` when it is only reachable
    because `VIGILANT_DELTA` adds it (or replaces the state's edges, as it
    does for `verify`), `"peer plan review only"` when it is only
    reachable because `PEER_PLAN_REVIEW_DELTA` adds it (task 018,
    requirement K, #101), and `"peer verdict gate only"` when it is only
    reachable because `PEER_VERDICT_GATE_DELTA` adds it (task 020,
    requirement L, #101) -- each an INDEPENDENT axis from vigilant/
    non-vigilant and from each other, gated by `JobConfig.peer_plan_review`/
    `JobConfig.peer_verdict_gate` (both default OFF) rather than
    `cfg.vigilant`.
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for state in transitions.ALL_STATES:
        hat = transitions.STATE_HAT.get(state)
        hat_label = hat if hat else "\u2014"
        non_vigilant = transitions.successors(state, vigilant=False)
        vigilant = transitions.successors(state, vigilant=True)
        peer_plan = transitions.successors(state, vigilant=False, peer_plan_review=True)
        peer_verdict = transitions.successors(state, vigilant=False, peer_verdict_gate=True)
        seen: set[tuple[str, str]] = set()
        for edge in non_vigilant:
            key = (edge.target, edge.guard)
            seen.add(key)
            rows.append((state, hat_label, "both", edge.target, edge.guard))
        for edge in vigilant:
            key = (edge.target, edge.guard)
            if key in seen:
                continue
            seen.add(key)
            rows.append((state, hat_label, "vigilant only", edge.target, edge.guard))
        for edge in peer_plan:
            key = (edge.target, edge.guard)
            if key in seen:
                continue
            seen.add(key)
            rows.append((state, hat_label, "peer plan review only", edge.target, edge.guard))
        for edge in peer_verdict:
            key = (edge.target, edge.guard)
            if key in seen:
                continue
            seen.add(key)
            rows.append((state, hat_label, "peer verdict gate only", edge.target, edge.guard))
        if not non_vigilant and not vigilant and not peer_plan and not peer_verdict:
            rows.append((state, hat_label, "\u2014", "\u2014 (terminal)", "\u2014"))
    return rows


def render() -> str:
    """Return the full markdown block, markers included."""
    lines = [BEGIN_MARKER, ""]
    lines.append(
        f"Generated from `src/ralphd/engine/transitions.py` by "
        f"`scripts/render_transition_table.py` — do not hand-edit this "
        f"block; regenerate it with `python scripts/render_transition_table.py "
        f"--write` and let `tests/test_transition_table_docs.py` catch drift. "
        f"Entry state: `{transitions.ENTRY_STATE}`. Run-terminal states "
        f"(`status.json`'s top-level `state`, not a step state): "
        f"{', '.join(f'`{s}`' for s in transitions.RUN_TERMINAL_STATES)}."
    )
    lines.append("")
    lines.append("| State | Hat | Variant | Target | Guard |")
    lines.append("|---|---|---|---|---|")
    for state, hat, variant, target, guard in _rows():
        lines.append(f"| `{state}` | {hat} | {variant} | `{target}` | `{guard}` |")
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
