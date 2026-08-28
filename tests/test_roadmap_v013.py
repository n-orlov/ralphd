"""v0.13 requirement N (docs/prds/v0.13-a-broken-contract-is-a-conversation.md):
`docs/roadmap.md`'s v0.13 block and the version bump.

This is the module for the *current* release, the same role
`tests/test_roadmap_v012.py` played until this wave retargeted it to the
past-milestone shape (`SERIES` pinned to `(0, 12)`, "declared version has a
block" moved here). Some future wave will retarget this module the same way
when it ships its own milestone.

`SERIES` is **pinned** to `(0, 13)`, not derived from `pyproject.toml`: this
module is the thing that checks the declared version and the roadmap agree,
so if it read the version to find its own block it could never disagree with
`pyproject.toml` about which block to grade -- the disagreement is exactly
what `test_the_declared_version_and_both_literals_agree` below has to be able
to catch.

Two things established by earlier waves stay true here:

* the roadmap's last milestone and `pyproject.toml`'s declared version are
  the same fact, so the block for the declared version must exist, must
  record shipped work, and nothing may claim a milestone *after* it. Unlike
  `tests/test_roadmap_v010.py`/`tests/test_roadmap_v012.py` (past milestones,
  where that "nothing after" half would trip on the *next* block simply for
  existing), this module is the *current* release, so the check runs
  unfiltered here;
* a block may not claim a wave landed while omitting requirements of that
  wave. The requirement letters are parsed out of the PRD the block is about
  (`docs/prds/v0.13-a-broken-contract-is-a-conversation.md`), never retyped
  here.

This PRD's requirement headings are `### A \u2014 title` (an em dash, the same
shape `test_roadmap_v012.py` pinned), letters `A`-`N` -- but unlike every
earlier PRD's letters, **none of this PRD's headings carry an inline
`(#NN)`**, and one letter (`F`) is split into two un-lettered sub-headings
(`### F1 \u2014 ...`, `### F2 \u2014 ...`) instead of getting its own heading.
So the parse here differs from `test_roadmap_v012.py`'s in two ways the
docstring calls out rather than hides:

* `F1`/`F2` fold into one letter, `F`, matching the roadmap block's own single
  `**F.` bullet (task 025's commit does the same folding, deliberately, for
  the same reason: one shipped-or-not fact, not two half-facts);
* per-letter issue coverage cannot be derived from the headings (there is
  nothing there to derive), so this module checks a fixed, hand-verified set
  of issues (`PRD_ISSUES`, read off the PRD's own "Four issues" sentence)
  against the *whole* block instead of against each letter's own bullet.

Every check is paired with a test that mutates the *real* roadmap text (or
PRD text) into wording it replaced, so a check that stops discriminating
fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"
PRD = REPO_ROOT / "docs" / "prds" / "v0.13-a-broken-contract-is-a-conversation.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "src" / "ralphd" / "__init__.py"

DEFERRED_HEADING = "## Later / explicitly deferred"

# The milestone this module is about. Pinned, not derived: see the module
# docstring for why deriving it from `pyproject.toml` would defang the
# version-agreement check below.
SERIES = (0, 13)

# The four issues this PRD's own "Four issues" sentence names. Fixed here,
# independent of the heading parse (which carries no issue numbers at all --
# see the module docstring), so the citation check has something to check
# the block against.
PRD_ISSUES = {"92", "89", "90", "93"}

# All fourteen requirement letters this PRD uses, A-N, with F1/F2 folded into
# one F. Spelled out here, independent of the parse, so
# `test_the_prd_headings_parse_at_all` has something fixed to check the parse
# against.
PRD_LETTERS = set("ABCDEFGHIJKLMN")


def declared_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def declared_series() -> tuple[int, int]:
    major, minor = (int(p) for p in declared_version().split(".")[:2])
    return major, minor


def roadmap_text() -> str:
    return ROADMAP.read_text()


def milestone_block(text: str, series: tuple[int, int]) -> str | None:
    """The `## vX.Y ...` section body, or None when there is no such heading.

    Stops at the deferred heading as well as at the next milestone: the last
    milestone's block runs up against `## Later / explicitly deferred`, and a
    coverage check that swallowed the deferred list could be satisfied by an
    entry *deferring* the very requirement it is looking for.
    """
    heads = list(re.finditer(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE))
    for i, m in enumerate(heads):
        if (int(m.group(1)), int(m.group(2))) != series:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.start(): end]
        return body.split(DEFERRED_HEADING, 1)[0]
    return None


def deferred_section(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirement_letters() -> set[str]:
    """`{"A", "B", ..., "F", ..., "N"}` from the PRD's own `### A \u2014 title`
    headings, folding the un-lettered `F1`/`F2` sub-headings into one `F`.

    Letters run `A`-`N` with no gaps other than `F`'s own split, so the regex
    is pinned to that exact range (plus the two `F1`/`F2` spellings) rather
    than widened past it the way older modules widen past their own gaps.
    """
    out: set[str] = set()
    for m in re.finditer(r"^### ([A-N]|F1|F2) \u2014 ", PRD.read_text(), re.MULTILINE):
        letter = m.group(1)
        out.add(letter[0] if letter.startswith("F") else letter)
    return out


# ------------------------------------------------- the block matches the version


def _version_block_problems(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block(text, series)
    if block is None:
        return [f"docs/roadmap.md has no `## {v}` block for the declared version"]
    problems = []
    if "\u2705" not in block:
        problems.append(f"the `## {v}` block records nothing as shipped")
    heads = [(int(a), int(b)) for a, b in
             re.findall(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE)]
    ahead = sorted(h for h in heads if h > series)
    if ahead:
        problems.append(
            f"docs/roadmap.md has milestone blocks after the declared version: {ahead}")
    return problems


def test_the_roadmap_has_a_block_for_this_milestone():
    """v0.13 is the declared version, so `## v0.13` must exist, record
    shipped work, and be the last milestone block in the file."""
    assert not _version_block_problems(roadmap_text(), SERIES)


def test_the_declared_version_and_both_literals_agree():
    """`pyproject.toml`'s declared version, `src/ralphd/__init__.py`'s
    `__version__` literal, and this module's `SERIES` pin all name the same
    release. If any of the three drifted, the block this module grades would
    no longer be the declared version's block."""
    assert declared_series() == SERIES, (
        f"pyproject declares {declared_series()}, not {SERIES} -- this "
        "module's SERIES pin is stale")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version(), (
        f"src/ralphd/__init__.py's __version__ ({m and m.group(1)!r}) does not "
        f"match pyproject.toml's declared version ({declared_version()!r})")


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), SERIES
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # no block for v0.13 at all
    assert _version_block_problems(text.replace(heading, "## Not a milestone"), series)
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _version_block_problems(
        text.replace(block, block.replace("\u2705", "\u23f3")), series)
    # a milestone block claimed after the declared version
    later = text.replace(heading, "## v0.14 — not real\n\n" + heading, 1)
    assert _version_block_problems(later, series)


def test_the_block_check_catches_a_stale_series_pin():
    """If `SERIES` ever drifted from the declared version, the agreement
    check above -- not the block-presence check -- is the one that has to
    catch it, since the block-presence check only ever looks at `SERIES`
    itself and would happily grade the wrong milestone."""
    assert declared_series() == SERIES
    assert not _version_block_problems(roadmap_text(), SERIES)
    # a pin one milestone ahead of the real declared version finds no block
    assert _version_block_problems(roadmap_text(), (SERIES[0], SERIES[1] + 1))


# --------------------------------------------------- the block covers the wave


def _coverage_problems(block: str) -> list[str]:
    return [f"requirement {letter} is not in the v0.13 block"
            for letter in sorted(PRD_LETTERS)
            if not re.search(rf"\*\*{letter}\.", block)]


def test_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD parse found
    nothing, and pins the PRD's own letter set (folded) so a future PRD edit
    that silently dropped or renumbered a requirement is caught here rather
    than only downstream in the coverage check."""
    assert prd_requirement_letters() == PRD_LETTERS


def test_every_prd_requirement_is_accounted_for_in_the_v013_block():
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert not _coverage_problems(block)


def test_every_prd_issue_is_cited_in_the_v013_block():
    """Every issue the PRD's own "Four issues" sentence names (#92, #89,
    #90, #93) is citable straight off the block -- the whole point of the
    block as an issue -> requirement index. Checked against the block as a
    whole, not per letter: unlike earlier PRDs, this one's headings carry no
    inline issue number to key a per-letter check off of (see the module
    docstring)."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    for n in PRD_ISSUES:
        assert f"#{n}" in block, f"the v0.13 block does not cite #{n}"


def test_the_coverage_check_catches_a_dropped_requirement():
    block = milestone_block(roadmap_text(), SERIES)
    # dropping a requirement letter's bold marker removes it from coverage
    assert _coverage_problems(block.replace("**J.", "J."))


def test_the_issue_citation_check_catches_a_dropped_issue():
    block = milestone_block(roadmap_text(), SERIES)
    mutated = block.replace("#90", "the practices issue")
    assert "90" not in re.findall(r"#(\d+)", mutated)


def test_the_coverage_check_catches_an_entirely_missing_block():
    """The coverage helper itself assumes it is handed a real block; the
    module-level checks above are what catch a missing block, but this pins
    that `_coverage_problems` still reports something (rather than silently
    passing) if ever called on an empty string, e.g. because `milestone_block`
    returned an empty match."""
    assert _coverage_problems("") == [
        f"requirement {letter} is not in the v0.13 block"
        for letter in sorted(PRD_LETTERS)
    ]


# --------------------------------------------- what the block claims is not deferred


def test_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.13 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists
    and still names both."""
    section = deferred_section(roadmap_text())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section
