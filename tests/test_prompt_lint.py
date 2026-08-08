"""Prompt files are read by fresh-context agents with no notion of "before"
or "after" the current text -- there is only the current prompt. Revision-
history commentary (phrases that describe how the prompt used to read, or
that something changed relative to an earlier version) is noise addressed
to a diff reviewer, not the agent, and can actively confuse the reader.
This test keeps that invariant durable across future edits.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

# Each pattern targets a phrase that only makes sense when comparing the
# current prompt text to some prior revision of itself. Kept as regexes so
# word-boundary variants (case-insensitive) are all caught.
BANNED_PATTERNS = [
    r"unchanged from before",
    r"\bpreviously\b",
    r"\bnow also\b",
    r"\bwas added\b",
    r"\bno longer\b",
    r"\bused to be\b",
    r"\bas before\b",
    r"\bsame as before\b",
    r"\bearlier (pass|draft|version|iteration)\b",
    r"\bin the past\b",
    r"\bthis replaces\b",
    r"\bold behavio(u)?r\b",
    r"\bnew behavio(u)?r\b",
]


def _prompt_files() -> list[Path]:
    return sorted(PROMPTS_DIR.glob("*.md"))


def test_prompts_dir_has_files():
    files = _prompt_files()
    assert files, f"expected prompt files under {PROMPTS_DIR}"


def test_no_revision_history_phrases_in_prompts():
    offenders: list[str] = []
    for path in _prompt_files():
        text = path.read_text()
        for pattern in BANNED_PATTERNS:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                line_no = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.name}:{line_no}: matched /{pattern}/ "
                                  f"-> {m.group(0)!r}")
    assert not offenders, (
        "prompt files must be timeless (no evolution-trace commentary):\n"
        + "\n".join(offenders))
