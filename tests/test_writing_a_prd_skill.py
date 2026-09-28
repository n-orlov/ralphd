"""Guards the bundled PRD-writing skill's "Clauses that make acceptance
unreachable" catalogue against silently losing an entry (E.2). An entry that
regresses out of the file costs nothing to notice here, and everything to
notice on a later run that repeats the mistake the entry was meant to
prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DOC = REPO_ROOT / "skills" / "ralphd" / "writing-a-prd.md"

HEADING = "## Clauses that make acceptance unreachable"


def _section_under_heading(text: str, heading: str) -> str:
    """Return the body of the named `##` section, up to the next `##`."""
    idx = text.index(heading)
    rest = text[idx + len(heading):]
    nxt = re.search(r"\n## ", rest)
    return rest[: nxt.start()] if nxt else rest


def test_heading_exists():
    text = SKILL_DOC.read_text()
    assert HEADING in text, (
        f"{SKILL_DOC} is missing the {HEADING!r} heading")


def test_open_domain_heuristic_entry_present_under_the_heading():
    """The never-converging open-domain entry: warns that a requirement
    saying "any"/"every" over an open domain enforced by a heuristic always
    has one more counterexample, tells the author to enumerate the cases
    instead, uses R150-shaped wording as the example, and names no product.
    """
    text = SKILL_DOC.read_text()
    section = _section_under_heading(text, HEADING)
    flat = re.sub(r"\s+", " ", section)

    assert '"any"' in flat and '"every"' in flat, (
        "the entry must warn about clauses quantifying with any/every")
    assert "open domain" in flat, (
        "the entry must name the open-domain shape explicitly")
    assert "heuristic" in flat, (
        "the entry must say the enforcement mechanism is a heuristic")
    assert "one more counterexample" in flat, (
        "the entry must state the always-one-more-counterexample failure")
    assert "Enumerate" in flat or "enumerate" in flat, (
        "the entry must tell the author to enumerate the cases instead")
    assert "R150" in flat, (
        "the entry must cite R150-shaped wording as the example")

    # No product named inside this entry specifically (the file's other
    # sections are free to name ralphd; this entry is not).
    entry_start = section.index('"any"/"every"')
    # Walk back to the start of the bullet this sentence belongs to.
    bullet_start = section.rfind("\n- ", 0, entry_start)
    bullet_end = section.find("\n- ", entry_start)
    entry_text = section[bullet_start:bullet_end if bullet_end != -1 else None]
    for product in ("ralphd", "deck", "ralphd's"):
        assert product not in entry_text.lower(), (
            f"the open-domain entry names a product ({product!r}); "
            "it must stay product-agnostic")


def test_entry_absent_without_the_change():
    """Sanity check that the assertions above actually discriminate: text
    that lacks the new entry (e.g. the file's earlier revision) must fail
    the presence check, so this test cannot pass vacuously.
    """
    text = SKILL_DOC.read_text()
    section = _section_under_heading(text, HEADING)
    without_entry = section.split(
        '- **DO NOT write a requirement that says "any"/"every"')[0]
    assert "R150" not in without_entry, (
        "the entry text should be isolated to the new bullet, not scattered "
        "earlier in the section")
