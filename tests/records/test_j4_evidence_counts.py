"""Task cure-01-04 (R10): pin J.4's citation-count claims against the
ledger's own text, in-process, so a future edit that reintroduces a false
aggregate is caught here rather than by review.

`artifacts/issues/v0.25-issues.md`'s J.4 section used to close with a false
whole-section arithmetic claim ("the whole section holds 44 citations / 43
distinct shas") derived by adding one sha (`7f949bf4`, K.8's commit, not a
J.4 closure citation) to the correct nine-bullet count -- the whole section
actually carries far more citations than that once the corrected-count
preamble and the re-measurement postscript are included, so the summed
figure was never itself a citation count of anything explicitly delimited.
The nine-bullet statement it was built from (**43 sha citations, 42
distinct**) is independently correct and stays; the unnecessary
whole-section number is dropped instead of restated, per this cure's own
criterion ("drops the unnecessary whole-section number while retaining the
correct nine-bullet 43-citation/42-distinct statement").
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "artifacts" / "issues" / "v0.25-issues.md"

SHA_RE = re.compile(r"`([0-9a-f]{8,40})`")


def _j4_section() -> str:
    text = LEDGER.read_text()
    return text.split("## J.4 (task 041):")[1].split("## #269")[0]


def _nine_bullets() -> str:
    section = _j4_section()
    return section.split("- **#210**")[1].split("**On this record")[0]


def test_j4_nine_bullet_citation_count_is_43_distinct_42():
    refs = SHA_RE.findall(_nine_bullets())
    assert (len(refs), len(set(refs))) == (43, 42), (
        "the nine closure bullets must carry exactly 43 sha citations, "
        "42 distinct (bfe78743 answers both #216 and #217)"
    )
    print("J4 nine-bullet citations=43 distinct=42")


def test_j4_section_states_no_false_whole_section_aggregate():
    section = _j4_section()
    # The false claim this cure removes: an aggregate total (nine-bullet
    # count plus the one further K.8 sha named in the closing paragraph)
    # asserted as if it were itself a measured citation count.
    assert "whole section holds" not in section
    assert "44 citations" not in section
    assert "43 distinct shas" not in section
    # The K.8 sha is still named as an ancestor, just without an aggregate
    # citation-count claim built on top of it.
    assert "7f949bf4" in section
    print("J4 section carries no whole-section aggregate citation claim")
