"""Task 042 (#170, #175, PRD requirement I.4): land the v0.19.1
reflection's `suggestions.diff` (#175) additions to `review.md`,
`verdict_gate.md` and `steer.md` -- three statements the prompt set did
not previously make: a curable finding is a specification naming
`cured_when` and `invalidates`; the engine owns the terminal record so
the gate never reads `events.jsonl`; a steering message's premises are
checked against the live plan before it is applied.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO / "src" / "ralphd" / "prompts"
REVIEW_MD = (PROMPTS_DIR / "review.md").read_text()
VERDICT_GATE_MD = (PROMPTS_DIR / "verdict_gate.md").read_text()
STEER_MD = (PROMPTS_DIR / "steer.md").read_text()


def test_review_md_says_a_curable_finding_is_a_specification():
    assert "A curable finding is a specification" in REVIEW_MD
    idx = REVIEW_MD.index("A curable finding is a specification")
    para = " ".join(REVIEW_MD[idx:idx + 700].split())
    assert "`cured_when`" in para
    assert "`invalidates`" in para
    assert "successCriteria" in para


def test_verdict_gate_md_says_the_gate_never_reads_events_jsonl():
    assert "the engine owns the terminal record" in VERDICT_GATE_MD.lower() or \
        "The engine owns the terminal record" in VERDICT_GATE_MD
    idx = VERDICT_GATE_MD.index("The engine owns the terminal record")
    para = " ".join(VERDICT_GATE_MD[idx:idx + 700].split())
    assert "events.jsonl" in para
    assert "never reads" in para


def test_steer_md_checks_every_premise_against_the_live_plan():
    assert "Check every" in STEER_MD
    idx = STEER_MD.index("Check every")
    para = " ".join(STEER_MD[idx:idx + 700].split())
    assert "premise" in para
    assert "live plan" in para
    assert "before you apply" in para


def test_none_of_the_three_additions_introduced_a_new_fence():
    """These additions are plain prose, never a fenced code block -- the
    fence-conformance pin (tests/test_prompt_fence_conformance.py) covers
    the contract-key scan itself; this is only a cheap same-file sanity
    check that fence counts stayed balanced (opened/closed pairs)."""
    assert REVIEW_MD.count("```") % 2 == 0
    assert VERDICT_GATE_MD.count("```") % 2 == 0
    assert STEER_MD.count("```") % 2 == 0
