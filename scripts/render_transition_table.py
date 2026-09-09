#!/usr/bin/env python3
"""Render `src/ralphd/engine/transitions.py`'s declared table as the
markdown block `docs/architecture.md` embeds between the
`<!-- BEGIN GENERATED TRANSITION TABLE -->` /
`<!-- END GENERATED TRANSITION TABLE -->` markers.

Task 016 (requirement B follow-through): the table in the docs is not a
hand copy — this script (and `tests/test_transitions.py`, which
calls the same function) is the ONLY place that turns `transitions.py`'s
data into prose, so a changed table entry shows up as doc drift instead of
silently going stale.

Two blocks, both generated from the same module: the step-state table
(`transitions.TABLE`) and the task-status mini-FSM table
(`transitions.TASK_STATUS_TABLE`, v0.20 requirement B.1/F, #156, task 019).

Usage:
    python scripts/render_transition_table.py            # print both blocks
    python scripts/render_transition_table.py --write     # rewrite both
                                                           # blocks in place
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

# v0.20 requirement B.1/F (#156, task 019): the SECOND generated block this
# script owns -- the task-status mini-FSM (`transitions.TASK_STATUS_TABLE` /
# `TASK_STATUS_VIGILANT_DELTA`, composed by `task_status_table_for`), which
# governs one task record's `status` field rather than the run's own FSM
# position. Same discipline as the block above: generated, never
# hand-edited, drift caught by `tests/test_transitions.py`. It exists
# because `superseded` is a real transition TARGET the engine writes
# (`state.supersede_task`) and a table nobody renders is a table nobody
# reads.
TASK_STATUS_BEGIN_MARKER = (
    "<!-- BEGIN GENERATED TASK-STATUS TABLE "
    "(scripts/render_transition_table.py) -->")
TASK_STATUS_END_MARKER = "<!-- END GENERATED TASK-STATUS TABLE -->"

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
        f"--write` and let `tests/test_transitions.py` catch drift. "
        f"Entry state: `{transitions.ENTRY_STATE}`. Run-terminal states "
        f"(`status.json`'s top-level `state`, not a step state): "
        f"{', '.join(f'`{s}`' for s in transitions.RUN_TERMINAL_STATES)}."
    )
    lines.append("")
    lines.append("| State | Hat | Variant | Target | Guard |")
    lines.append("|---|---|---|---|---|")
    for state, hat, variant, target, guard in _rows():
        lines.append(f"| `{state}` | {hat} | {variant} | `{target}` | `{guard}` |")
    notes = [(guard, transitions.guard_condition_note(guard))
             for guard in dict.fromkeys(
                 guard for _s, _h, _v, _t, guard in _rows())]
    notes = [(guard, note) for guard, note in notes if note]
    if notes:
        lines.append("")
        lines.append(
            "Guards whose declared condition is wider than their name "
            "(`transitions.GUARD_CONDITION_NOTES`, rendered here so the "
            "table states it instead of a reader inferring it):")
        lines.append("")
        for guard, note in notes:
            lines.append(f"- `{guard}` — {note}")
    lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def _task_status_rows() -> list[tuple[str, str, str]]:
    """One row per (status, variant, target) of the task-status mini-FSM, in
    `transitions.TASK_STATUSES` order.

    `variant` is `"both"` when the edge is declared in both variants,
    `"vigilant only"` when it exists only because
    `TASK_STATUS_VIGILANT_DELTA` replaced that status's successor tuple, and
    `"non-vigilant only"` when the delta's replacement DROPPED it (which is
    exactly what happens to `in-progress -> completed`: `awaiting-
    validation` replaces `completed` as a vigilant worker's claim state).
    Computed from `transitions.task_status_table_for` -- the ONE producer of
    a variant's task-status table -- for both variants, never from the raw
    dicts.
    """
    non_vigilant = transitions.task_status_table_for(False)
    vigilant = transitions.task_status_table_for(True)
    rows: list[tuple[str, str, str]] = []
    for status in transitions.TASK_STATUSES:
        plain = non_vigilant.get(status, ())
        vig = vigilant.get(status, ())
        seen: set[str] = set()
        for target in plain:
            seen.add(target)
            rows.append((status, "both" if target in vig
                         else "non-vigilant only", target))
        for target in vig:
            if target in seen:
                continue
            seen.add(target)
            rows.append((status, "vigilant only", target))
        if not seen:
            rows.append((status, "\u2014", "\u2014 (terminal)"))
    return rows


def render_task_status_table() -> str:
    """Return the full markdown block for the task-status mini-FSM, markers
    included."""
    engine_forced = ", ".join(
        f"`{s}`" for s in transitions.ENGINE_FORCED_TASK_STATUSES)
    lines = [TASK_STATUS_BEGIN_MARKER, ""]
    lines.append(
        f"Generated from `src/ralphd/engine/transitions.py`'s task-status "
        f"mini-FSM (`TASK_STATUS_TABLE` / `TASK_STATUS_VIGILANT_DELTA`, "
        f"composed by `task_status_table_for`) by "
        f"`scripts/render_transition_table.py` — do not hand-edit this "
        f"block; regenerate it with `python "
        f"scripts/render_transition_table.py --write` and let "
        f"`tests/test_transitions.py` catch drift. This table governs one "
        f"`tasks.json` record's `status` field, not the run's own FSM "
        f"position. Every move here is applied by the harness at a step "
        f"transition, never written by an agent itself; "
        f"{engine_forced} is forced by the engine alone — no hat may report "
        f"it (`task_status_report_errors`) and no operator may rule it "
        f"(`ralphctl amend --status`, which validates against "
        f"`OPERATOR_RULEABLE_TASK_STATUSES`)."
    )
    lines.append("")
    lines.append("| Status | Variant | Target |")
    lines.append("|---|---|---|")
    for status, variant, target in _task_status_rows():
        lines.append(f"| `{status}` | {variant} | `{target}` |")
    lines.append("")
    lines.append(TASK_STATUS_END_MARKER)
    return "\n".join(lines) + "\n"


def _replace_block(text: str, begin: str, end: str, block: str) -> str:
    start = text.index(begin)
    stop = text.index(end) + len(end)
    return text[:start] + block.rstrip("\n") + text[stop:]


def write_into_doc() -> None:
    text = ARCHITECTURE_DOC.read_text()
    text = _replace_block(text, BEGIN_MARKER, END_MARKER, render())
    text = _replace_block(text, TASK_STATUS_BEGIN_MARKER,
                          TASK_STATUS_END_MARKER, render_task_status_table())
    ARCHITECTURE_DOC.write_text(text)


if __name__ == "__main__":
    if "--write" in sys.argv[1:]:
        write_into_doc()
    else:
        print(render(), end="")
        print()
        print(render_task_status_table(), end="")
