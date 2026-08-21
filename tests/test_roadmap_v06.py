"""Task 044 (#22): `docs/roadmap.md`'s v0.6 block, re-read against the code.

The roadmap is the one doc that is *only* prose: nothing in the engine reads it,
so it rots silently. It already rotted once in exactly the way #22 recorded --
`pyproject.toml` said `0.1.0.dev0` while the roadmap recorded v0.5 as shipped --
and task 040 tied the version to it (`test_packaging_metadata.py`). This module
covers the other two ways a milestone block lies:

* it exists for a version nobody released, or the released version has no block
  at all (checked here against `pyproject.toml`, and in
  `test_packaging_metadata.py` against the shipped markers);
* it claims a wave landed while quietly omitting requirements of that wave, or
  keeps deferring what the wave delivered. The requirement letters and issue
  numbers are not retyped here: they are parsed out of the PRD the block is
  about (`docs/prds/v0.6-first-release.md`, which is byte-identical to the run's
  own PRD), and the CLI verbs the block advertises have to exist in
  `build_parser()`.

Every check is paired with a test that mutates the *real* roadmap text into the
wording it replaced, so a check that stops discriminating fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"
PRD = REPO_ROOT / "docs" / "prds" / "v0.6-first-release.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

DEFERRED_HEADING = "## Later / explicitly deferred"

# Verbs this wave added to `ralphctl` for requirement F's CLI parity. Each must
# exist in the parser (the code-derived half) *and* be named in the block.
WAVE_VERBS = ("iteration", "fault", "cost", "docs")


def declared_series() -> tuple[int, int]:
    v = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    major, minor = (int(p) for p in v.split(".")[:2])
    return major, minor


def roadmap_text() -> str:
    return ROADMAP.read_text()


def milestone_block(text: str, series: tuple[int, int]) -> str | None:
    """The `## vX.Y ...` section body, or None when there is no such heading."""
    heads = list(re.finditer(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE))
    for i, m in enumerate(heads):
        if (int(m.group(1)), int(m.group(2))) != series:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        return text[m.start(): end]
    return None


def deferred_section(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirements() -> dict[str, set[str]]:
    """`{"A": {"15"}, ...}` from the PRD's own `## A. title (#15)` headings."""
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^## ([A-J])\. (.+)$", PRD.read_text(), re.MULTILINE):
        out.setdefault(m.group(1), set()).update(re.findall(r"#(\d+)", m.group(2)))
    return out


# ------------------------------------------------- the block matches the version


def _version_block_problems(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block(text, series)
    if block is None:
        return [f"docs/roadmap.md has no `## {v}` block for the declared version"]
    problems = []
    if "✅" not in block:
        problems.append(f"the `## {v}` block records nothing as shipped")
    heads = [(int(a), int(b)) for a, b in
             re.findall(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE)]
    ahead = sorted(h for h in heads if h > series)
    if ahead:
        problems.append(
            f"docs/roadmap.md has milestone blocks after the declared version: {ahead}")
    return problems


def test_the_roadmap_has_a_block_for_the_declared_version():
    """The released version and the roadmap's own last milestone are the same
    thing: 0.6.0 ships v0.6, so `## v0.6` must exist and record shipped work."""
    assert not _version_block_problems(roadmap_text(), declared_series())


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), declared_series()
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-044 roadmap: no block for the version pyproject declares
    assert _version_block_problems(text.replace(heading, "## Not a milestone"), series)
    # a block that only *plans* the release records nothing shipped
    planned = milestone_block(text, series).replace("✅", "⏳")
    assert _version_block_problems(
        text.replace(milestone_block(text, series), planned), series)
    # a version the roadmap has already moved past
    assert _version_block_problems(text, (series[0], series[1] - 1))


# --------------------------------------------------- the block covers the wave


def _coverage_problems(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.6 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD parse found
    nothing (the failure mode of every "for each documented X" test)."""
    reqs = prd_requirements()
    assert set(reqs) == set("ABCDEFGHIJ"), sorted(reqs)
    assert reqs["A"] == {"15"} and reqs["C"] == {"14"} and reqs["G"] == {"19"}


def test_every_prd_requirement_is_accounted_for_in_the_v06_block():
    block = milestone_block(roadmap_text(), declared_series())
    assert block is not None
    assert not _coverage_problems(block)


def test_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block(roadmap_text(), declared_series())
    assert _coverage_problems(block.replace("**G.", "G."))
    assert _coverage_problems(block.replace("#19", "the delete issue"))


def test_the_v06_block_names_the_verbs_the_wave_added():
    from ralphd.cli.main import build_parser

    verbs = {a.dest: a for a in build_parser()._subparsers._group_actions}
    choices = set(next(iter(verbs.values())).choices)
    block = milestone_block(roadmap_text(), declared_series())
    for verb in WAVE_VERBS:
        assert verb in choices, f"WAVE_VERBS names {verb!r}, which the parser lacks"
        assert f"`ralphctl {verb}`" in block, (
            f"the v0.6 block claims requirement F's CLI parity but never names "
            f"`ralphctl {verb}`")


# ------------------------------------------- what the wave delivered is not deferred


def _deferred_image_problems(text: str) -> list[str]:
    """The image entry may defer *publishing* only: `start` builds the image."""
    problems = []
    for para in re.split(r"\n(?=- )", deferred_section(text)):
        if not re.search(r"\bimage\b", para, re.IGNORECASE):
            continue
        if not re.search(r"publish", para, re.IGNORECASE):
            problems.append("a deferred entry mentions the image without deferring "
                            f"publishing only: {para.strip()[:80]!r}")
        if re.search(r"nothing (ever )?builds|never (been )?built|builds? .{0,20}manual",
                     para, re.IGNORECASE):
            problems.append(f"a deferred entry still says the image is unbuilt: "
                            f"{para.strip()[:80]!r}")
    if not problems and "publish" not in deferred_section(text).lower():
        problems.append("the deferred list lost the publishing entry entirely")
    return problems


def test_the_code_really_builds_the_image_so_deferring_it_would_be_wrong():
    """The premise of the check below, read off the code rather than the prose:
    `start`/`resume` hash the inputs and can report a tag they *built*."""
    from ralphd.cli import image
    from ralphd.cli.main import IMAGE_SOURCE_BUILT, IMAGE_SOURCE_CACHED

    assert IMAGE_SOURCE_BUILT == "built" and IMAGE_SOURCE_CACHED == "cached"
    assert hasattr(image, "hash_image_inputs") and hasattr(image, "derive")


def test_the_deferred_list_defers_publishing_the_image_not_building_it():
    assert not _deferred_image_problems(roadmap_text())


def test_the_deferred_image_check_catches_the_pre_v06_wording():
    text = roadmap_text()
    section = deferred_section(text)
    # the v0.5 wording, moved into the deferred list: building was manual
    assert _deferred_image_problems(text.replace(
        section, section + "\n- The job image: `container/Dockerfile` is in the "
                           "repo but nothing ever builds it.\n"))
    # ...and dropping the publishing entry is caught too
    stripped = re.sub(r"\n- Publishing the Docker image[\s\S]*?(?=\n- )", "\n", section)
    assert stripped != section, "fixture assumption: the publishing entry is there"
    assert _deferred_image_problems(text.replace(section, stripped))
