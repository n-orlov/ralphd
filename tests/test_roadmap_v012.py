"""v0.12 requirement H (docs/prds/v0.12-green-on-a-real-host.md):
`docs/roadmap.md`'s v0.12 block and the version bump.

Retargeted by v0.13's requirement N, exactly the way v0.12's own requirement H
retargeted `tests/test_roadmap_v010.py` when v0.12 became current: v0.12 is no
longer the declared release, so `SERIES` here is **pinned** to `(0, 12)`
rather than read from `pyproject.toml`, and the "declared version has a
block" half moves to `tests/test_roadmap_v013.py`. `declared_version()` /
`declared_series()` stay, used only to assert this block is a *past*
milestone -- if they were still used to select the block, every check below
would grade v0.13's block against v0.12's PRD.

Two things established by earlier waves stay true here:

* the roadmap's last milestone and the released version are the same fact,
  so the block for `pyproject.toml`'s declared version must exist, must
  record shipped work, and nothing may claim a milestone *after* it. That is
  `tests/test_roadmap_v013.py`'s business now; the "nothing after" half is
  filtered out of the two checks here that would otherwise fail on v0.13's
  own block simply for existing;
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

# The milestone this module is about. Pinned, not derived: v0.12 is history
# now (see the module docstring). `test_roadmap_v013.py` is the current
# release's.
SERIES = (0, 12)


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


def _own_block_problems(text: str) -> list[str]:
    """`_version_block_problems` minus the "nothing after this milestone" half.

    v0.13 exists now, so that half reports `[(0, 13)]` for v0.12
    unconditionally and would swamp the two checks that are about *this*
    block's own content. The "nothing after the declared version" rule is
    not weakened, only relocated: `tests/test_roadmap_v013.py` enforces it
    for the declared version, which is the only version it was ever a rule
    about.
    """
    return [p for p in _version_block_problems(text, SERIES)
            if "milestone blocks after" not in p]


def test_the_roadmap_has_a_block_for_this_milestone():
    """v0.12 shipped, so `## v0.12` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v013.py`'s business."""
    assert not _own_block_problems(roadmap_text())


def test_v012_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.12 is behind the release: if the
    version ever went back to 0.12.x, this module would be the current one
    again and would have to enforce the no-later-blocks half itself. The two
    literals are also still checked to agree, since
    `test_packaging_metadata.py`'s rule and this pin only line up while they
    do."""
    assert declared_series() > SERIES, (
        f"pyproject declares {declared_series()}, so v0.12 is not history: this "
        "module's SERIES pin (v0.13 requirement N) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version()


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), SERIES
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-v0.12 roadmap: no block for v0.12 at all
    assert _own_block_problems(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _own_block_problems(text.replace(block, block.replace("\u2705", "\u23f3")))
    # a version the roadmap has already moved past (v0.10, with v0.12 after it) --
    # this one is the unfiltered check, since it is exactly what it reports
    assert _version_block_problems(text, (0, 10))


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
    assert set().union(*reqs.values()) == {"83", "84", "85", "86", "87"}
    assert reqs["B"] == {"85"} and reqs["D"] == {"84"} and reqs["E"] == {"83"}
    assert reqs["F"] == {"86"} and reqs["G"] == {"87"}
    # A, C and H are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"A", "C", "H"}


def test_every_prd_requirement_is_accounted_for_in_the_v012_block():
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert not _coverage_problems(block)


def test_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block(roadmap_text(), SERIES)
    assert _coverage_problems(block.replace("**D.", "D."))
    assert _coverage_problems(block.replace("#85", "the route-check issue"))


def test_the_block_cites_every_issue_this_wave_names():
    """Every issue this wave's PRD names by letter has to be citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block(roadmap_text(), SERIES)
    for issues in prd_requirements().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.12 block does not cite #{n}"


# ------------------------------------------- what the wave delivered is not deferred


def test_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.12 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists and
    still names both."""
    section = deferred_section(roadmap_text())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section
