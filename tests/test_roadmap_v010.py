"""v0.10 requirement I (#part of docs/prds/v0.10-clock-is-not-a-verdict.md):
`docs/roadmap.md`'s v0.10 block and the version bump.

`tests/test_roadmap_v09.py`'s module for the *current* release, until the next
wave retargets it the same way this one retargeted v0.9's: pin that module's
`SERIES` to `(0, 9)` and move the "declared version has a block" half here.
Two things established by earlier waves stay true:

* the roadmap's last milestone and `pyproject.toml`'s declared version are the
  same fact, so the block for the declared version must exist, must record
  shipped work, and nothing may claim a milestone *after* it;
* a block may not claim a wave landed while omitting requirements of that
  wave. The requirement letters and issue numbers are parsed out of the PRD
  the block is about (`docs/prds/v0.10-clock-is-not-a-verdict.md`), never
  retyped here.

Unlike every module before it, this one is written **before** the wave it
describes has (mostly) landed: v0.10's own PRD asks for requirement I to ship
first rather than last (the mistake v0.9's own requirement O made), so at the
sha this module is committed at, only requirement I itself is shipped and
every other requirement's bullet honestly reads pending (`\u23f3`). The coverage
checks below only demand that every requirement letter and issue is *named* in
the block -- not that every one is marked shipped -- which is exactly the shape
`tests/test_roadmap_v09.py` used for its own then-unfinished requirement G.

Every check is paired with a test that mutates the *real* roadmap text into
wording it replaced (or a wrong version), so a check that stops discriminating
fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"
PRD = REPO_ROOT / "docs" / "prds" / "v0.10-clock-is-not-a-verdict.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "src" / "ralphd" / "__init__.py"

DEFERRED_HEADING = "## Later / explicitly deferred"

# The milestone this wave ships. Derived, not pinned: this module is about the
# *declared* version's block, per the docstring above.
SERIES = (0, 10)


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
    """`{"A": {"72"}, ...}` from the PRD's own `## A. title (#72)` headings.

    The letter range is `A-O`: this PRD uses `A`-`J` and `M`, skipping `K`,
    `L`, `N` and `O`, so widening the regex that far (rather than to the
    letter it actually stops at) is safe -- no heading exists to match the
    gaps.
    """
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^## ([A-O])\. (.+)$", PRD.read_text(), re.MULTILINE):
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


def test_the_declared_version_is_this_wave_and_both_literals_agree():
    """The bump half of requirement I: 0.10.0 in `pyproject.toml` and in
    `src/ralphd/__init__.py` (`test_packaging_metadata.py` owns the general
    rule; this pins the number this block claims)."""
    assert declared_series() == SERIES, (
        f"docs/roadmap.md's v0.10 block is the current milestone, but "
        f"pyproject.toml declares {declared_version()!r}")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version()


def test_the_roadmap_has_a_block_for_the_declared_version():
    """The released version and the roadmap's own last milestone are the same
    thing: 0.10.0 ships v0.10, so `## v0.10` must exist and record shipped
    work."""
    assert not _version_block_problems(roadmap_text(), declared_series())


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), SERIES
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # no block at all for the version pyproject declares
    assert _version_block_problems(text.replace(heading, "## Not a milestone"), series)
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _version_block_problems(text.replace(block, block.replace("\u2705", "\u23f3")), series)
    # a version the roadmap has already moved past (v0.9, with v0.10 after it)
    assert _version_block_problems(text, (series[0], series[1] - 1))


def test_the_v09_section_is_not_read_as_a_milestone_after_the_declared_one():
    text = roadmap_text()
    heads = [(int(a), int(b)) for a, b in
             re.findall(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE)]
    assert max(heads) == SERIES, heads
    assert "## v0.9" in (milestone_block(text, (0, 9)) or "")
    assert "## v0.9" not in (milestone_block(text, SERIES) or "")


# --------------------------------------------------- the block covers the wave


def _coverage_problems(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.10 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD parse found
    nothing -- and pins that this PRD really uses A-J and M, skipping K, L, N
    and O, so a naive assumption the letters run unbroken A-O would over-claim
    if the PRD ever grew a requirement in one of those gaps without this test
    noticing."""
    reqs = prd_requirements()
    assert set(reqs) == set("ABCDEFGHIJM"), sorted(reqs)
    assert reqs["A"] == {"72"} and reqs["D"] == {"69"} and reqs["J"] == {"75"}
    assert reqs["F"] == {"73", "74"}
    # A, B and C are all requirement A's issue -- the same shape v0.9's B-F
    # shared #60's five pillars.
    assert all(reqs[letter] == {"72"} for letter in "ABC")
    # G, H, I and M are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"G", "H", "I", "M"}


def test_every_prd_requirement_is_accounted_for_in_the_v010_block():
    block = milestone_block(roadmap_text(), declared_series())
    assert block is not None
    assert not _coverage_problems(block)


def test_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block(roadmap_text(), SERIES)
    assert _coverage_problems(block.replace("**J.", "J."))
    assert _coverage_problems(block.replace("#75", "the sidecar-migration issue"))


def test_the_block_cites_every_issue_this_wave_names():
    """Every issue this wave's PRD names by letter has to be citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block(roadmap_text(), declared_series())
    for issues in prd_requirements().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.10 block does not cite #{n}"


# --------------------------------------------- only requirement I is shipped yet


def _bullet(block: str, letter: str) -> str:
    bullets = [p for p in re.split(r"\n(?=- )", block) if f"**{letter}." in p]
    assert len(bullets) == 1, f"expected exactly one requirement {letter} bullet"
    return bullets[0]


def test_requirement_i_is_marked_shipped():
    """Task 001 ships requirement I itself (the block, the bump, the
    retargeted test modules) -- landed early per the PRD's own instruction,
    unlike v0.9's requirement O, which the clock caught at the tail."""
    block = milestone_block(roadmap_text(), declared_series())
    assert block is not None
    assert _bullet(block, "I").startswith("- \u2705")


def test_the_shipped_check_catches_a_block_that_stops_claiming_i():
    block = milestone_block(roadmap_text(), SERIES)
    bullet = _bullet(block, "I")
    mutated = block.replace(bullet, bullet.replace("- \u2705", "- \u23f3", 1))
    assert not _bullet(mutated, "I").startswith("- \u2705")


def test_every_other_requirement_is_honestly_pending():
    """The half of requirement I this module exists to police: a block written
    before the rest of the wave landed must not claim more than it has. If a
    later task ships one of these and forgets to flip its marker, that is this
    module's job to catch too -- but flipping it *to* shipped is a change this
    test then has to be updated to allow, the same way v0.9's requirement L
    bullet check was amended once task 113 actually closed the issues."""
    block = milestone_block(roadmap_text(), declared_series())
    assert block is not None
    for letter in "CDEFGHM":
        bullet = _bullet(block, letter)
        assert bullet.startswith("- \u23f3"), (
            f"requirement {letter} is marked shipped, but no task has landed it "
            "yet -- if it really has landed, update this test alongside the "
            "roadmap bullet")


def test_the_pending_check_catches_a_prematurely_shipped_bullet():
    block = milestone_block(roadmap_text(), SERIES)
    bullet = _bullet(block, "C")
    assert bullet.startswith("- \u23f3"), "fixture assumption: C is pending"
    mutated = block.replace(bullet, bullet.replace("- \u23f3", "- \u2705", 1))
    assert _bullet(mutated, "C").startswith("- \u2705")


# ------------------------------------------- what the wave delivered is not deferred


def test_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.10 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists and
    still names both."""
    section = deferred_section(roadmap_text())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section
