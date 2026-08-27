"""v0.10 requirement I (docs/prds/v0.10-clock-is-not-a-verdict.md):
`docs/roadmap.md`'s v0.10 block and the version bump.

Retargeted by v0.12's requirement H, exactly the way v0.10's own requirement I
retargeted `tests/test_roadmap_v09.py` when v0.10 became current: v0.10 is no
longer the declared release (v0.11 shipped its features without a bump, and
v0.12 carries `0.10.0` -> `0.12.0` in one step), so `SERIES` here is
**pinned** to `(0, 10)` rather than read from `pyproject.toml`, and the
"declared version has a block" half moves to `tests/test_roadmap_v012.py`.
`declared_version()` / `declared_series()` stay, used only to assert this
block is a *past* milestone -- if they were still used to select the block,
every check below would grade v0.12's block against v0.10's PRD.

Two things established by earlier waves stay true here:

* the roadmap's last milestone and the released version are the same fact,
  so the block for `pyproject.toml`'s declared version must exist, must
  record shipped work, and nothing may claim a milestone *after* it. That is
  `tests/test_roadmap_v012.py`'s business now; the "nothing after" half is
  filtered out of the two checks here that would otherwise fail on v0.12's
  own block simply for existing;
* a block may not claim a wave landed while omitting requirements of that
  wave. The requirement letters and issue numbers are parsed out of the PRD
  the block is about (`docs/prds/v0.10-clock-is-not-a-verdict.md`), never
  retyped here. This PRD uses `A`-`J` and `M`, skipping `K`, `L`, `N` and `O`,
  so the regex is widened to `A-O` and the parse itself is pinned below.

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

# The milestone this module is about. Pinned, not derived: v0.10 is history
# now (see the module docstring). `test_roadmap_v012.py` is the current
# release's.
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


def _own_block_problems(text: str) -> list[str]:
    """`_version_block_problems` minus the "nothing after this milestone" half.

    v0.12 exists now, so that half reports `[(0, 12)]` for v0.10
    unconditionally and would swamp the two checks that are about *this*
    block's own content. The "nothing after the declared version" rule is
    not weakened, only relocated: `tests/test_roadmap_v012.py` enforces it
    for the declared version, which is the only version it was ever a rule
    about.
    """
    return [p for p in _version_block_problems(text, SERIES)
            if "milestone blocks after" not in p]


def test_the_roadmap_has_a_block_for_this_milestone():
    """v0.10 shipped, so `## v0.10` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v012.py`'s business."""
    assert not _own_block_problems(roadmap_text())


def test_v010_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.10 is behind the release: if the
    version ever went back to 0.10.x, this module would be the current one
    again and would have to enforce the no-later-blocks half itself. The two
    literals are also still checked to agree, since
    `test_packaging_metadata.py`'s rule and this pin only line up while they
    do."""
    assert declared_series() > SERIES, (
        f"pyproject declares {declared_series()}, so v0.10 is not history: this "
        "module's SERIES pin (v0.12 requirement H) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version()


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), SERIES
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-v0.10 roadmap: no block for v0.10 at all
    assert _own_block_problems(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _own_block_problems(text.replace(block, block.replace("\u2705", "\u23f3")))
    # a version the roadmap has already moved past (v0.9, with v0.10 after it) --
    # this one is the unfiltered check, since it is exactly what it reports
    assert _version_block_problems(text, (series[0], series[1] - 1))


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
    block = milestone_block(roadmap_text(), SERIES)
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
    block = milestone_block(roadmap_text(), SERIES)
    for issues in prd_requirements().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.10 block does not cite #{n}"


# --------------------------------------------- every requirement shipped, with a sha


def _bullet(block: str, letter: str) -> str:
    bullets = [p for p in re.split(r"\n(?=- )", block) if f"**{letter}." in p]
    assert len(bullets) == 1, f"expected exactly one requirement {letter} bullet"
    return bullets[0]


def test_every_shipped_requirement_names_a_real_commit():
    """Unlike v0.10's own then-unfinished module (which pinned `DGHM` as
    still pending because the block was written before the wave landed),
    v0.10 finished within its own run: every requirement bullet reads shipped
    and names at least one real commit sha, not a bare flip."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    shas = {"D": "f40d053", "G": None, "H": "75b1dd8", "I": None, "M": "7b4a9b5"}
    for letter, sha in shas.items():
        bullet = _bullet(block, letter)
        assert bullet.startswith("- \u2705"), (
            f"requirement {letter} is marked pending, but v0.10 shipped it -- "
            "flip the roadmap bullet alongside this test")
        if sha is not None:
            assert sha in bullet, (
                f"requirement {letter}'s bullet does not cite its landing "
                f"commit {sha}")


def test_the_shipped_requirement_check_catches_a_regressed_bullet():
    """Mutation pair for the check above: flipping a shipped bullet back to
    pending must be caught."""
    block = milestone_block(roadmap_text(), SERIES)
    bullet = _bullet(block, "D")
    assert bullet.startswith("- \u2705"), "fixture assumption: D is shipped"
    mutated = block.replace(bullet, bullet.replace("- \u2705", "- \u23f3", 1))
    assert not _bullet(mutated, "D").startswith("- \u2705")



# ------------------------------------------- what the wave delivered is not deferred


def test_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.10 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists and
    still names both."""
    section = deferred_section(roadmap_text())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section
