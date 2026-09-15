"""Prompt conformance (task 003, requirement K.4): both judging prompts must
state that a `cancelled` CI conclusion is not a verdict, and must say how the
`RALPHD_TIMEOUT_KILL:` marker line and its evidence file distinguish a
timeout kill from an ordinary cancel (the K.3 mechanism in
`scripts/run_bounded.sh` / `scripts/job_timeout_marker.sh` / `docs/ci.md`).

Each assertion below fails the moment either sentence is dropped from either
file, not merely when the file goes missing entirely.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
REVIEW = PROMPTS_DIR / "review.md"
VERDICT_GATE = PROMPTS_DIR / "verdict_gate.md"

# `docs/ci.md` names these two fixed, mutually exclusive, greppable prefixes.
TIMEOUT_MARKER = "RALPHD_TIMEOUT_KILL:"
EVIDENCE_GLOB = "*.expired"


def _text(path: Path) -> str:
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _assert_cancelled_not_a_verdict(text: str, label: str) -> None:
    assert re.search(r"`cancelled`[^\n]{0,80}\bnot a verdict\b", text), (
        f"{label} no longer states that a `cancelled` conclusion is not a "
        "verdict (K.4)")


def _assert_timeout_vs_cancel_mechanism(text: str, label: str) -> None:
    assert TIMEOUT_MARKER in text, (
        f"{label} no longer names the {TIMEOUT_MARKER} marker line used to "
        "tell a timeout kill from a cancel (K.3/K.4)")
    assert EVIDENCE_GLOB in text, (
        f"{label} no longer names the {EVIDENCE_GLOB} evidence file used to "
        "tell a timeout kill from a cancel (K.3/K.4)")


def test_review_prompt_states_cancelled_is_not_a_verdict():
    _assert_cancelled_not_a_verdict(_text(REVIEW), "review.md")


def test_review_prompt_names_the_timeout_kill_marker_and_evidence_file():
    _assert_timeout_vs_cancel_mechanism(_text(REVIEW), "review.md")


def test_verdict_gate_prompt_states_cancelled_is_not_a_verdict():
    _assert_cancelled_not_a_verdict(_text(VERDICT_GATE), "verdict_gate.md")


def test_verdict_gate_prompt_names_the_timeout_kill_marker_and_evidence_file():
    _assert_timeout_vs_cancel_mechanism(_text(VERDICT_GATE), "verdict_gate.md")
