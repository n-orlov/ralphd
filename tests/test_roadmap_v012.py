"""v0.12 requirement H (docs/prds/v0.12-green-on-a-real-host.md):
`docs/roadmap.md`'s v0.12 block and the version bump.

This is the module for the *current* release, the same role
`tests/test_roadmap_v010.py` played until this wave retargeted it to the
past-milestone shape (`SERIES` pinned to `(0, 10)`, "declared version has a
block" moved here). Some future wave will retarget this module the same way
when it ships its own milestone.

`SERIES` is **pinned** to `(0, 12)`, not derived from `pyproject.toml`: this
module is the thing that checks the declared version and the roadmap agree,
so if it read the version to find its own block it could never disagree with
`pyproject.toml` about which block to grade -- the disagreement is exactly
what `test_the_declared_version_and_both_literals_agree` below has to be able
to catch.

Two things established by earlier waves stay true here:

* the roadmap's last milestone and `pyproject.toml`'s declared version are
  the same fact, so the block for the declared version must exist, must
  record shipped work, and nothing may claim a milestone *after* it. Unlike
  `test_roadmap_v010.py` (a past milestone, where that "nothing after" half
  would trip on v0.12's own block simply for existing), this module is the
  *current* release, so the check runs unfiltered here;
* a block may not claim a wave landed while omitting requirements of that
  wave. The requirement letters and issue numbers are parsed out of the PRD
  the block is about (`docs/prds/v0.12-green-on-a-real-host.md`), never
  retyped here. This PRD's requirement headings are `### A \u2014 title (#N)`
  (an em dash, not the `## A. title (#N)` shape earlier PRDs used), letters
  `A`-`H` with no gaps, so the parse below is pinned to that shape rather
  than reused verbatim from the older modules.

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
PRD = REPO_ROOT / "docs" / "prds" / "v0.12-green-on-a-real-host.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "src" / "ralphd" / "__init__.py"

DEFERRED_HEADING = "## Later / explicitly deferred"

# The milestone this module is about. Pinned, not derived: see the module
# docstring for why deriving it from `pyproject.toml` would defang the
# version-agreement check below.
SERIES = (0, 12)

# All five issues this PRD names, across its requirement headings (see the
# module docstring for the heading shape). Spelled out here, independent of
# the parse, so `test_the_prd_headings_parse_at_all` has something fixed to
# check the parse against.
PRD_ISSUES = {"83", "84", "85", "86", "87"}


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


def prd_requirements() -> dict[str, set[str]]:
    """`{"A": set(), "B": {"85"}, ...}` from the PRD's own
    `### A \u2014 title (#85)` headings.

    Unlike the `## A. title (#N)` shape earlier PRDs (v0.9, v0.10) used, this
    PRD's requirement headings are third-level (`###`) and separated from the
    letter by an em dash, not a period -- and letters run `A`-`H` with no
    gaps, so the regex is pinned to that exact range rather than widened past
    it the way the older modules widen past their own gaps.
    """
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^### ([A-H]) \u2014 (.+)$", PRD.read_text(), re.MULTILINE):
        out.setdefault(m.group(1), set()).update(re.findall(r"#(\d+)", m.group(2)))
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
    """v0.12 is the declared version, so `## v0.12` must exist, record
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
    # no block for v0.12 at all
    assert _version_block_problems(text.replace(heading, "## Not a milestone"), series)
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _version_block_problems(
        text.replace(block, block.replace("\u2705", "\u23f3")), series)
    # a milestone block claimed after the declared version
    later = text.replace(heading, "## v0.13 — not real\n\n" + heading, 1)
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
    problems = []
    for letter, issues in sorted(prd_requirements().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.12 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD parse found
    nothing, and pins the PRD's letters and issues so a future PRD edit that
    silently dropped or renumbered a requirement is caught here rather than
    only downstream in the coverage check."""
    reqs = prd_requirements()
    assert set(reqs) == set("ABCDEFGH"), sorted(reqs)
    assert set().union(*reqs.values()) == PRD_ISSUES
    assert reqs["B"] == {"85"} and reqs["D"] == {"84"} and reqs["E"] == {"83"}
    assert reqs["F"] == {"86"} and reqs["G"] == {"87"}
    # A, C and H are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"A", "C", "H"}


def test_every_prd_requirement_is_accounted_for_in_the_v012_block():
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert not _coverage_problems(block)


def test_every_prd_issue_is_cited_in_the_v012_block():
    """Every issue the PRD names (#83, #84, #85, #86, #87) is citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    for n in PRD_ISSUES:
        assert f"#{n}" in block, f"the v0.12 block does not cite #{n}"


def test_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block(roadmap_text(), SERIES)
    # dropping a requirement letter's bold marker removes it from coverage
    assert _coverage_problems(block.replace("**D.", "D."))
    # dropping a cited issue number
    assert _coverage_problems(block.replace("#85", "the route-check issue"))


def test_the_coverage_check_catches_an_entirely_missing_block():
    """The coverage helper itself assumes it is handed a real block; the
    module-level checks above are what catch a missing block, but this pins
    that `_coverage_problems` still reports something (rather than silently
    passing) if ever called on an empty string, e.g. because `milestone_block`
    returned an empty match."""
    assert _coverage_problems("") == [
        f"requirement {letter} is not in the v0.12 block" for letter in "ABCDEFGH"
    ]


# --------------------------------------------- what the block claims is not deferred


def test_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.12 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists
    and still names both."""
    section = deferred_section(roadmap_text())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section
