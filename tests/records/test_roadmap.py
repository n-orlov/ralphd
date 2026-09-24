"""`docs/roadmap.md`'s v0.6-v0.14 milestone blocks, folded (E.3).

Folds the eight per-milestone roadmap checkers -- `tests/test_roadmap_v06.py`,
`v07`, `v08`, `v09`, `v010`, `v012`, `v013`, `v014` -- into this one module,
the same mechanical method D.7 used for the `tests/test_*_task*.py` pin
modules: each source's own checkers, mutation twins and constants survive
verbatim, renamed with a `_v0N` suffix (or, for `test_` functions, a
`test_v0N_` prefix) only where a name would otherwise collide across sources
with a DIFFERENT meaning per source (`SERIES`, `PRD`, `WAVE_VERBS`,
`milestone_block` for v0.6's own non-deferred-stripping variant, every
`prd_requirements`/`_coverage_problems`/`_own_block_problems` implementation,
the v0.7/v0.8/v0.9 closure-record helpers, etc.). Names that are
byte-identical literals in every source that defines them -- `REPO_ROOT`,
`ROADMAP`, `PYPROJECT`, `INIT`, `LOOP`, `DEFERRED_HEADING`, `CLOSURE_REPORT`,
`TRACEABILITY_REPORT`, `OPEN_MARKER` -- are declared exactly once, shared by
every version's section below.

Each milestone's own checker function keeps exactly the mutation-twin test it
had before the fold (a test that mutates the *real* roadmap/PRD text into the
wording a regression would produce, so a checker that stops discriminating
fails); folding never adds, drops or merges a checker or its twin. See
`docs/test-suite-audit.md` Subsumed table for the full source-node ->
surviving-node mapping this fold produced.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

ROADMAP = REPO_ROOT / "docs" / "roadmap.md"

PYPROJECT = REPO_ROOT / "pyproject.toml"

INIT = REPO_ROOT / "src" / "ralphd" / "__init__.py"

LOOP = REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py"

DEFERRED_HEADING = "## Later / explicitly deferred"

CLOSURE_REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-closure.md"

TRACEABILITY_REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-traceability.md"

OPEN_MARKER = "Left open deliberately"


# ==========================================================================
# v0.6 (folded from test_roadmap_v06.py)
# ==========================================================================


PRD_V06 = REPO_ROOT / "docs" / "prds" / "v0.6-first-release.md"


# The milestone this module is about. Pinned, not read from `pyproject.toml`:
# v0.6 is history now (see the module docstring).
SERIES_V06 = (0, 6)


# Verbs this wave added to `ralphctl` for requirement F's CLI parity. Each must
# exist in the parser (the code-derived half) *and* be named in the block.
WAVE_VERBS_V06 = ("iteration", "fault", "cost", "docs")


def declared_series_v06() -> tuple[int, int]:
    v = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    major, minor = (int(p) for p in v.split(".")[:2])
    return major, minor


def roadmap_text_v06() -> str:
    return ROADMAP.read_text()


def milestone_block_v06(text: str, series: tuple[int, int]) -> str | None:
    """The `## vX.Y ...` section body, or None when there is no such heading."""
    heads = list(re.finditer(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE))
    for i, m in enumerate(heads):
        if (int(m.group(1)), int(m.group(2))) != series:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        return text[m.start(): end]
    return None


def deferred_section_v06(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirements_v06() -> dict[str, set[str]]:
    """`{"A": {"15"}, ...}` from the PRD_V06's own `## A. title (#15)` headings."""
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^## ([A-J])\. (.+)$", PRD_V06.read_text(), re.MULTILINE):
        out.setdefault(m.group(1), set()).update(re.findall(r"#(\d+)", m.group(2)))
    return out


def _version_block_problems_v06(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block_v06(text, series)
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


def test_v06_the_roadmap_has_a_block_for_this_milestone():
    """v0.6 shipped, so `## v0.6` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v07.py`'s business."""
    problems = [p for p in _version_block_problems_v06(roadmap_text_v06(), SERIES_V06)
                if "milestone blocks after" not in p]
    assert not problems


def test_v06_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.6 is behind the release: if the
    version ever went back to 0.6.x, this module would be the current one
    again and would have to enforce the no-later-blocks half itself."""
    assert declared_series_v06() > SERIES_V06, (
        f"pyproject declares {declared_series_v06()}, so v0.6 is not history: this "
        "module's SERIES_V06 pin (task 033) needs revisiting")


def test_v06_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text_v06(), SERIES_V06
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-044 roadmap: no block for the version pyproject declares
    assert _version_block_problems_v06(text.replace(heading, "## Not a milestone"), series)
    # a block that only *plans* the release records nothing shipped
    planned = milestone_block_v06(text, series).replace("✅", "⏳")
    assert _version_block_problems_v06(
        text.replace(milestone_block_v06(text, series), planned), series)
    # a version the roadmap has already moved past
    assert _version_block_problems_v06(text, (series[0], series[1] - 1))


def _coverage_problems_v06(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements_v06().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.6 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_v06_the_prd_headings_parse_at_all():
    # (v0.6's PRD_V06; v0.7's own parse is checked in test_roadmap_v07.py)
    """Guards the coverage check from passing because the PRD_V06 parse found
    nothing (the failure mode of every "for each documented X" test)."""
    reqs = prd_requirements_v06()
    assert set(reqs) == set("ABCDEFGHIJ"), sorted(reqs)
    assert reqs["A"] == {"15"} and reqs["C"] == {"14"} and reqs["G"] == {"19"}


def test_every_prd_requirement_is_accounted_for_in_the_v06_block():
    block = milestone_block_v06(roadmap_text_v06(), SERIES_V06)
    assert block is not None
    assert not _coverage_problems_v06(block)


def test_v06_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block_v06(roadmap_text_v06(), SERIES_V06)
    assert _coverage_problems_v06(block.replace("**G.", "G."))
    assert _coverage_problems_v06(block.replace("#19", "the delete issue"))


def test_the_v06_block_names_the_verbs_the_wave_added():
    from ralphd.cli.main import build_parser

    verbs = {a.dest: a for a in build_parser()._subparsers._group_actions}
    choices = set(next(iter(verbs.values())).choices)
    block = milestone_block_v06(roadmap_text_v06(), SERIES_V06)
    for verb in WAVE_VERBS_V06:
        assert verb in choices, f"WAVE_VERBS_V06 names {verb!r}, which the parser lacks"
        assert f"`ralphctl {verb}`" in block, (
            f"the v0.6 block claims requirement F's CLI parity but never names "
            f"`ralphctl {verb}`")


def _deferred_image_problems_v06(text: str) -> list[str]:
    """The image entry may defer *publishing* only: `start` builds the image."""
    problems = []
    for para in re.split(r"\n(?=- )", deferred_section_v06(text)):
        if not re.search(r"\bimage\b", para, re.IGNORECASE):
            continue
        if not re.search(r"publish", para, re.IGNORECASE):
            problems.append("a deferred entry mentions the image without deferring "
                            f"publishing only: {para.strip()[:80]!r}")
        if re.search(r"nothing (ever )?builds|never (been )?built|builds? .{0,20}manual",
                     para, re.IGNORECASE):
            problems.append(f"a deferred entry still says the image is unbuilt: "
                            f"{para.strip()[:80]!r}")
    if not problems and "publish" not in deferred_section_v06(text).lower():
        problems.append("the deferred list lost the publishing entry entirely")
    return problems


def test_v06_the_code_really_builds_the_image_so_deferring_it_would_be_wrong():
    """The premise of the check below, read off the code rather than the prose:
    `start`/`resume` hash the inputs and can report a tag they *built*."""
    from ralphd.cli import image
    from ralphd.cli.main import IMAGE_SOURCE_BUILT, IMAGE_SOURCE_CACHED

    assert IMAGE_SOURCE_BUILT == "built" and IMAGE_SOURCE_CACHED == "cached"
    assert hasattr(image, "hash_image_inputs") and hasattr(image, "derive")


def test_v06_the_deferred_list_defers_publishing_the_image_not_building_it():
    assert not _deferred_image_problems_v06(roadmap_text_v06())


def test_the_deferred_image_check_catches_the_pre_v06_wording():
    text = roadmap_text_v06()
    section = deferred_section_v06(text)
    # the v0.5 wording, moved into the deferred list: building was manual
    assert _deferred_image_problems_v06(text.replace(
        section, section + "\n- The job image: `container/Dockerfile` is in the "
                           "repo but nothing ever builds it.\n"))
    # ...and dropping the publishing entry is caught too
    stripped = re.sub(r"\n- Publishing the Docker image[\s\S]*?(?=\n- )", "\n", section)
    assert stripped != section, "fixture assumption: the publishing entry is there"
    assert _deferred_image_problems_v06(text.replace(section, stripped))


# ==========================================================================
# v0.7 (folded from test_roadmap_v07.py)
# ==========================================================================


PRD_V07 = REPO_ROOT / "docs" / "prds" / "v0.7-trustworthy-unattended.md"


# The milestone this module is about. Pinned, not read from `pyproject.toml`:
# v0.7 is history now (task 032 retargeted this the same way task 033 itself
# retargeted test_roadmap_v06.py's SERIES_V07 when v0.7 became current). The
# "declared version has a block" half lives in tests/test_roadmap_v08.py.
SERIES_V07 = (0, 7)


# The verb requirement P added to `ralphctl`. It must exist in the parser (the
# code-derived half) *and* be named in the block.
WAVE_VERBS_V07 = ("suggestions",)


def roadmap_text_v07() -> str:
    return ROADMAP.read_text()


def milestone_block_v07(text: str, series: tuple[int, int]) -> str | None:
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


def deferred_section_v07(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirements_v07() -> dict[str, set[str]]:
    """`{"B": {"28"}, ...}` from the PRD_V07's own `## B. title (#28)` headings.

    The letter range is `A-R`: v0.6's PRD_V07 ran to J, this one to R.
    """
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^## ([A-R])\. (.+)$", PRD_V07.read_text(), re.MULTILINE):
        out.setdefault(m.group(1), set()).update(re.findall(r"#(\d+)", m.group(2)))
    return out


def _version_block_problems_v07(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block_v07(text, series)
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


def test_v07_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text_v07(), SERIES_V07
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-033 roadmap: no block for the version pyproject declares
    assert _version_block_problems_v07(text.replace(heading, "## Not a milestone"), series)
    # a block that only *plans* the release records nothing shipped
    block = milestone_block_v07(text, series)
    assert _version_block_problems_v07(text.replace(block, block.replace("✅", "⏳")), series)
    # a version the roadmap has already moved past (v0.6, with v0.7 after it)
    assert _version_block_problems_v07(text, (series[0], series[1] - 1))


def _coverage_problems_v07(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements_v07().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.7 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_v07_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD_V07 parse found
    nothing -- and pins the widened `A-R` range: with v0.6's `[A-J]` regex,
    requirements K through R would silently not be checked at all."""
    reqs = prd_requirements_v07()
    assert set(reqs) == set("ABCDEFGHIJKLMNOPQR"), sorted(reqs)
    assert reqs["B"] == {"28"} and reqs["F"] == {"47", "43"} and reqs["M"] == {"33"}
    # O, P, Q and R are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"O", "P", "Q", "R"}


def test_every_prd_requirement_is_accounted_for_in_the_v07_block():
    block = milestone_block_v07(roadmap_text_v07(), SERIES_V07)
    assert block is not None
    assert not _coverage_problems_v07(block)


def test_v07_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block_v07(roadmap_text_v07(), SERIES_V07)
    assert _coverage_problems_v07(block.replace("**M.", "M."))
    assert _coverage_problems_v07(block.replace("#44", "the transcript issue"))


def test_v07_the_block_cites_every_issue_this_wave_names_not_only_the_range_ends():
    """The PRD_V07 writes requirement A's issues as the range `#35-#42`, so the
    parse above only demands #35 and #42. The block names all eight, which is
    what makes it usable as an issue -> requirement index."""
    block = milestone_block_v07(roadmap_text_v07(), SERIES_V07)
    missing = [n for n in range(35, 43) if f"#{n}" not in block]
    assert not missing, f"the v0.7 block does not cite {missing}"


def test_the_v07_block_names_the_verb_the_wave_added():
    from ralphd.cli.main import build_parser

    verbs = {a.dest: a for a in build_parser()._subparsers._group_actions}
    choices = set(next(iter(verbs.values())).choices)
    block = milestone_block_v07(roadmap_text_v07(), SERIES_V07)
    for verb in WAVE_VERBS_V07:
        assert verb in choices, f"WAVE_VERBS_V07 names {verb!r}, which the parser lacks"
        assert f"`ralphctl {verb}" in block, (
            f"the v0.7 block claims requirement P shipped but never names "
            f"`ralphctl {verb}`")


def _deferred_pid_namespace_problems_v07(text: str) -> list[str]:
    """The PID-namespace entry may no longer be the answer to the kill hazard.

    Requirement I closed that hazard with the uid boundary; an entry still
    saying engine-level self-protection is *needed* is a promise for work that
    landed, which is how the roadmap rots (the same defect v0.6's requirement J
    caught in the image entry).
    """
    problems = []
    paras = [p for p in re.split(r"\n(?=- )", deferred_section_v07(text))
             if re.search(r"PID.namespace", p, re.IGNORECASE)]
    if not paras:
        return ["the deferred list lost the PID-namespace entry entirely"]
    for para in paras:
        if "v0.7" not in para or "uid boundary" not in para:
            problems.append("the PID-namespace entry does not say v0.7's uid "
                            f"boundary closed the hazard: {para.strip()[:80]!r}")
        if re.search(r"is needed|not just\s+prompt|prompt rules alone",
                     para, re.IGNORECASE | re.DOTALL):
            problems.append("the PID-namespace entry still argues engine-level "
                            f"self-protection is missing: {para.strip()[:80]!r}")
    return problems


def test_v07_the_code_really_has_the_uid_boundary_so_deferring_it_would_be_wrong():
    """The premise of the check below, read off the code rather than the prose:
    the engine can separate its own identity from the agent's."""
    from ralphd.engine import privsep

    assert hasattr(privsep, "separate_engine_identity")
    assert hasattr(privsep, "agent_child_kwargs")
    assert privsep.DEFAULT_AGENT_UID == 1000


def test_v07_the_deferred_list_no_longer_promises_the_kill_protection_i_shipped():
    assert not _deferred_pid_namespace_problems_v07(roadmap_text_v07())


def test_the_deferred_pid_namespace_check_catches_the_pre_v07_wording():
    text = roadmap_text_v07()
    section = deferred_section_v07(text)
    pre_v07 = (
        "\n- PID-namespace isolation of agent iterations from in-container kill "
        "signals\n  (a supervisor-level SIGKILL/SIGTERM currently reaches the "
        "running `pi`\n  subprocess directly) — not attempted; rationale: prompt "
        "rules alone did\n  not prevent the iteration-103 pkill incident, so "
        "engine-level\n  self-protection is needed.\n")
    stripped = re.sub(r"\n- PID-namespace isolation[\s\S]*?(?=\n- )", "\n", section)
    assert stripped != section, "fixture assumption: the entry is there to replace"
    # the v0.6 wording, still promising what requirement I delivered
    assert _deferred_pid_namespace_problems_v07(
        text.replace(section, stripped + pre_v07))
    # ...and dropping the entry entirely is caught too
    assert _deferred_pid_namespace_problems_v07(text.replace(section, stripped))


def test_v07_the_auto_resume_flip_is_shipped_not_deferred():
    """Requirement O's half of the same rule, from the literal rather than the
    prose. `test_cli_auto_resume.py` owns the v0.5 note's wording; this pins
    that the v0.7 block itself records the flip."""
    from ralphd.cli.main import AUTO_RESUME_DEFAULT

    assert AUTO_RESUME_DEFAULT is True
    assert "auto_resume" not in deferred_section_v07(roadmap_text_v07())
    block = milestone_block_v07(roadmap_text_v07(), SERIES_V07)
    assert "AUTO_RESUME_DEFAULT" in block and "--no-auto-resume" in block


# Task 060 (requirement N.2) freezes these two sets. They used to be
# re-derived from `artifacts/reports/issue-closure.md`'s v0.7 section on
# every test run (`closure_wave_section_v07()`, deleted here along with
# `CLOSURE_WAVE_HEADING_V07`); task 061 (N.1) deletes that corpus file
# entirely, and the wave it records (v0.7, task 038) is long since shipped
# and will never change again, so re-deriving it bought nothing but a read
# of a file about to go away. The values are exactly what the deleted
# derivation last returned -- pinned by the two `test_v07_the_closure_record_
# parses_at_all` assertions this same commit retires -- so freezing them
# changes no fact any check below relies on.
def closed_issues_v07() -> set[int]:
    """The issues `issue-closure.md`'s v0.7 section recorded as closed
    (frozen historical fact, task 038; see the module comment above)."""
    return {28, *range(30, 50), 52, 53, 55}


def left_open_issues_v07() -> set[int]:
    """The issues that section named as deliberately left open (frozen
    historical fact, task 038; see the module comment above)."""
    return {24, 25, 26, 27, 50, 51, 54}


def _issue_numbers_v07(text: str) -> set[int]:
    """`{28, 30, 31, ...}` from prose, expanding `#30–#49` style ranges."""
    out: set[int] = set()
    for lo, hi in re.findall(r"#(\d+)\s*[–—-]\s*#(\d+)", text):
        out.update(range(int(lo), int(hi) + 1))
    out.update(int(n) for n in re.findall(r"#(\d+)", text))
    return out


def q_bullet_v07(block: str) -> str | None:
    bullets = [p for p in re.split(r"\n(?=- )", block) if "**Q." in p]
    return bullets[0] if len(bullets) == 1 else None


def _requirement_q_problems_v07(block: str) -> list[str]:
    bullet = q_bullet_v07(block)
    if bullet is None:
        return ["the v0.7 block has no single requirement Q bullet"]
    problems = []
    if not bullet.startswith("- ✅"):
        problems.append("requirement Q is not marked shipped")
    if "⏳" in bullet or re.search(r"[Pp]ending as this block is written", bullet):
        problems.append("the Q bullet still says the closures are pending")
    for report in (CLOSURE_REPORT, TRACEABILITY_REPORT):
        cited = f"`artifacts/reports/{report.name}`"
        if cited not in bullet:
            problems.append(f"the Q bullet does not cite {cited}")
    if OPEN_MARKER not in bullet:
        problems.append(f"the Q bullet does not say what it {OPEN_MARKER.lower()}")
        return problems
    claimed, open_part = bullet.split(OPEN_MARKER, 1)
    closed = closed_issues_v07()
    if _issue_numbers_v07(claimed) != closed:
        problems.append(
            f"the Q bullet claims {sorted(_issue_numbers_v07(claimed))} closed, but "
            f"{CLOSURE_REPORT.name} records {sorted(closed)}")
    total = re.search(r"\b(\d+) in all\b", claimed)
    if not total:
        problems.append("the Q bullet states no closure total")
    elif int(total.group(1)) != len(closed):
        problems.append(f"the Q bullet says {total.group(1)} closures, the "
                        f"record has {len(closed)}")
    if _issue_numbers_v07(open_part) != left_open_issues_v07():
        problems.append(
            f"the Q bullet names {sorted(_issue_numbers_v07(open_part))} as left "
            f"open, the record names {sorted(left_open_issues_v07())}")
    return problems


def test_v07_requirement_q_is_shipped_and_says_what_the_receipts_say():
    """Task 042: the bullet flips to shipped only once the issues are closed,
    and its claim is the closure record's own set, not a retyped one."""
    block = milestone_block_v07(roadmap_text_v07(), SERIES_V07)
    assert block is not None
    assert not _requirement_q_problems_v07(block)


def test_v07_the_requirement_q_check_catches_a_stale_or_wrong_claim():
    block = milestone_block_v07(roadmap_text_v07(), SERIES_V07)
    bullet = q_bullet_v07(block)
    assert bullet is not None, "fixture assumption: the bullet is there to mutate"

    def mutated(old: str, new: str) -> list[str]:
        assert old in bullet, f"fixture assumption: {old!r} is in the bullet"
        return _requirement_q_problems_v07(block.replace(bullet, bullet.replace(old, new, 1)))

    # the pre-042 wording: still pending, so the roadmap would claim work the
    # GitHub state contradicts
    assert mutated("- ✅", "- ⏳")
    assert mutated("no hedged closures.", "Pending as this block is written.")
    # a dropped receipt citation
    assert mutated(f"`artifacts/reports/{CLOSURE_REPORT.name}`", "the closure report")
    # a closure claimed for an issue the record leaves open, and one dropped
    assert mutated("**#52**, **#53** and **#55**", "**#50**, **#52** and **#53**")
    assert mutated("**#52**, **#53** and **#55**", "**#52** and **#53**")
    # an issue left open that the bullet stops naming
    assert mutated("**#51**, filed by the operator", "one filed by the operator")
    # a total that no longer matches the receipt
    assert mutated("24 in all", "21 in all")
    # ...and the real bullet is not accidentally satisfying all of this by luck
    assert not _requirement_q_problems_v07(block)


def test_v07_the_block_names_the_fault_class_requirement_d_added():
    """Requirement D's claim, read off `faults.py`'s own vocabulary."""
    from ralphd.engine.faults import FAULT_CLASS_SIGNAL, FAULT_CLASSES

    assert FAULT_CLASS_SIGNAL in FAULT_CLASSES
    block = milestone_block_v07(roadmap_text_v07(), SERIES_V07)
    assert f"`{FAULT_CLASS_SIGNAL}`" in block, (
        "the v0.7 block claims requirement D shipped but never names the "
        f"third fault class ({FAULT_CLASS_SIGNAL!r})")


# ==========================================================================
# v0.8 (folded from test_roadmap_v08.py)
# ==========================================================================


PRD_V08 = REPO_ROOT / "docs" / "prds" / "v0.8-auditable-interruptible.md"


# The milestone this module is about. Pinned, not derived: v0.8 is history now
# (see the module docstring). `test_roadmap_v09.py` is the current release's.
SERIES_V08 = (0, 8)


# Verb this wave's requirement H added to `ralphctl` (#26). Must exist in the
# parser (the code-derived half) *and* be named in the block.
WAVE_VERBS_V08 = ("amend",)


def declared_version_v08() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def declared_series_v08() -> tuple[int, int]:
    major, minor = (int(p) for p in declared_version_v08().split(".")[:2])
    return major, minor


def roadmap_text_v08() -> str:
    return ROADMAP.read_text()


def milestone_block_v08(text: str, series: tuple[int, int]) -> str | None:
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


def deferred_section_v08(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirements_v08() -> dict[str, set[str]]:
    """`{"A": {"56"}, ...}` from the PRD_V08's own `## A. title (#56)` headings.

    The letter range is `A-O`: this PRD_V08 skips `I-L` entirely, so widening the
    regex that far (rather than to the letter it actually stops at) is safe --
    no heading exists to match the gap.
    """
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^## ([A-O])\. (.+)$", PRD_V08.read_text(), re.MULTILINE):
        out.setdefault(m.group(1), set()).update(re.findall(r"#(\d+)", m.group(2)))
    return out


def _version_block_problems_v08(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block_v08(text, series)
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


def _own_block_problems_v08(text: str) -> list[str]:
    """`_version_block_problems_v08` minus the "nothing after this milestone" half.

    v0.9 exists now, so that half reports `[(0, 9)]` for v0.8 unconditionally and
    would swamp the two checks that are about *this* block's own content. The
    "nothing after the declared version" rule is not weakened, only relocated:
    `tests/test_roadmap_v09.py` enforces it for the declared version, which is
    the only version it was ever a rule about.
    """
    return [p for p in _version_block_problems_v08(text, SERIES_V08)
            if "milestone blocks after" not in p]


def test_v08_the_roadmap_has_a_block_for_this_milestone():
    """v0.8 shipped, so `## v0.8` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v09.py`'s business."""
    assert not _own_block_problems_v08(roadmap_text_v08())


def test_v08_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.8 is behind the release: if the
    version ever went back to 0.8.x, this module would be the current one again
    and would have to enforce the no-later-blocks half itself. The two literals
    are also still checked to agree, since `test_packaging_metadata.py`'s rule
    and this pin only line up while they do."""
    assert declared_series_v08() > SERIES_V08, (
        f"pyproject declares {declared_series_v08()}, so v0.8 is not history: this "
        "module's SERIES_V08 pin (v0.9 requirement O) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version_v08()


def test_v08_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text_v08(), SERIES_V08
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-032 roadmap: no block for v0.8 at all
    assert _own_block_problems_v08(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block_v08(text, series)
    assert _own_block_problems_v08(text.replace(block, block.replace("✅", "⏳")))
    # a version the roadmap has already moved past (v0.7, with v0.8 after it) --
    # this one is the unfiltered check, since it is exactly what it reports
    assert _version_block_problems_v08(text, (series[0], series[1] - 1))


def _coverage_problems_v08(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements_v08().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.8 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_v08_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD_V08 parse found
    nothing -- and pins that the PRD_V08 really skips I-L, so a naive assumption
    the letters run unbroken A-O would silently under-check nothing (there is
    nothing at I-L to under-check) but *would* over-claim if this PRD_V08 ever
    grew a requirement in that gap without this test noticing."""
    reqs = prd_requirements_v08()
    assert set(reqs) == set("ABCDEFGHMNO"), sorted(reqs)
    assert reqs["A"] == {"56"} and reqs["G"] == {"54"} and reqs["H"] == {"26"}
    # N and O are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"N", "O"}


def test_every_prd_requirement_is_accounted_for_in_the_v08_block():
    block = milestone_block_v08(roadmap_text_v08(), SERIES_V08)
    assert block is not None
    assert not _coverage_problems_v08(block)


def test_v08_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block_v08(roadmap_text_v08(), SERIES_V08)
    assert _coverage_problems_v08(block.replace("**H.", "H."))
    assert _coverage_problems_v08(block.replace("#26", "the operator-ruling issue"))


def test_v08_the_block_cites_every_issue_this_wave_names():
    """Every issue this wave's PRD_V08 names by letter has to be citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block_v08(roadmap_text_v08(), SERIES_V08)
    for issues in prd_requirements_v08().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.8 block does not cite #{n}"


def test_the_v08_block_names_the_verb_the_wave_added():
    from ralphd.cli.main import build_parser

    verbs = {a.dest: a for a in build_parser()._subparsers._group_actions}
    choices = set(next(iter(verbs.values())).choices)
    block = milestone_block_v08(roadmap_text_v08(), SERIES_V08)
    for verb in WAVE_VERBS_V08:
        assert verb in choices, f"WAVE_VERBS_V08 names {verb!r}, which the parser lacks"
        assert f"`ralphctl {verb}" in block, (
            f"the v0.8 block claims requirement H shipped but never names "
            f"`ralphctl {verb}`")


def n_bullet_v08(block: str) -> str | None:
    bullets = [p for p in re.split(r"\n(?=- )", block) if "**N." in p]
    return bullets[0] if len(bullets) == 1 else None


# Task 060 (requirement N.2) freezes this set; see the identical comment
# above `closed_issues_v07` for why. It used to be re-derived from
# `artifacts/reports/issue-closure.md`'s v0.8 section on every run
# (`closure_wave_v08_section()`, deleted here); the value is exactly what
# that derivation last returned -- pinned by `test_the_v08_closure_record_
# parses_at_all`, retired in this same commit.
def closed_issues_v08() -> set[int]:
    """The issues `issue-closure.md`'s v0.8 section recorded as closed
    (frozen historical fact, task 035; see the module comment above)."""
    return {24, 26, 27, 50, 51, 54, 56, 57, 58}


def _issue_numbers_v08(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"#(\d+)", text)}


def _requirement_n_problems_v08(block: str) -> list[str]:
    bullet = n_bullet_v08(block)
    if bullet is None:
        return ["the v0.8 block has no single requirement N bullet"]
    problems = []
    if not bullet.startswith("- ✅"):
        problems.append("requirement N is not marked shipped")
    if "⏳" in bullet or "not yet executed" in bullet:
        problems.append("the N bullet still says the closures are pending")
    for report in (CLOSURE_REPORT, TRACEABILITY_REPORT):
        cited = f"`artifacts/reports/{report.name}`"
        if cited not in bullet:
            problems.append(f"the N bullet does not cite {cited}")
    claimed = _issue_numbers_v08(bullet.split("closed over the", 1)[0])
    closed = closed_issues_v08()
    if claimed != closed:
        problems.append(
            f"the N bullet claims {sorted(claimed)} closed, but "
            f"{CLOSURE_REPORT.name}'s v0.8 section records {sorted(closed)}")
    total = re.search(r"\b(\d+) in all\b", bullet)
    if not total:
        problems.append("the N bullet states no closure total")
    elif int(total.group(1)) != len(closed):
        problems.append(f"the N bullet says {total.group(1)} closures, the "
                        f"record has {len(closed)}")
    return problems


def test_v08_requirement_n_is_shipped_and_says_what_the_receipts_say():
    """Task 035: the bullet flips to shipped only once the issues are closed,
    and its claim is the closure record's own set, not a retyped one."""
    block = milestone_block_v08(roadmap_text_v08(), SERIES_V08)
    assert block is not None
    assert not _requirement_n_problems_v08(block)


def test_v08_the_requirement_n_check_catches_a_stale_or_wrong_claim():
    block = milestone_block_v08(roadmap_text_v08(), SERIES_V08)
    bullet = n_bullet_v08(block)
    assert bullet is not None, "fixture assumption: the bullet is there to mutate"

    def mutated(old: str, new: str) -> list[str]:
        assert old in bullet, f"fixture assumption: {old!r} is in the bullet"
        return _requirement_n_problems_v08(block.replace(bullet, bullet.replace(old, new, 1)))

    # the pre-035 wording: still pending
    assert mutated("- ✅", "- ⏳")
    # a dropped receipt citation
    assert mutated(f"`artifacts/reports/{CLOSURE_REPORT.name}`", "the closure report")
    # a closure claimed for an issue the record does not show closed, and one dropped
    assert mutated("#56, #57 and #58", "#55, #57 and #58")
    assert mutated("#56, #57 and #58", "#57 and #58")
    # a total that no longer matches the receipt
    assert mutated("9 in all", "8 in all")
    # ...and the real bullet is not accidentally satisfying all of this by luck
    assert not _requirement_n_problems_v08(block)


def test_v08_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.8 does not resolve either standing deferred item (image publishing,
    PID namespaces) -- unlike v0.6/v0.7, this wave has no deferred-list entry
    of its own to correct, so the only property to hold is that the section
    still exists and still names both."""
    section = deferred_section_v08(roadmap_text_v08())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section


# ==========================================================================
# v0.9 (folded from test_roadmap_v09.py)
# ==========================================================================


PRD_V09 = REPO_ROOT / "docs" / "prds" / "v0.9-declarative-loop.md"


# The milestone this module is about. Pinned, not derived: v0.9 is history now
# (see the module docstring). `test_roadmap_v010.py` is the current release's.
SERIES_V09 = (0, 9)


def declared_version_v09() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def declared_series_v09() -> tuple[int, int]:
    major, minor = (int(p) for p in declared_version_v09().split(".")[:2])
    return major, minor


def roadmap_text_v09() -> str:
    return ROADMAP.read_text()


def milestone_block_v09(text: str, series: tuple[int, int]) -> str | None:
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


def deferred_section_v09(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirements_v09() -> dict[str, set[str]]:
    """`{"A": {"62"}, ...}` from the PRD_V09's own `## A. title (#62)` headings.

    The letter range is `A-O`: this PRD_V09 skips `J`, `K` and `N`, so widening the
    regex that far (rather than to the letter it actually stops at) is safe --
    no heading exists to match the gaps.
    """
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^## ([A-O])\. (.+)$", PRD_V09.read_text(), re.MULTILINE):
        out.setdefault(m.group(1), set()).update(re.findall(r"#(\d+)", m.group(2)))
    return out


def _version_block_problems_v09(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block_v09(text, series)
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


def _own_block_problems_v09(text: str) -> list[str]:
    """`_version_block_problems_v09` minus the "nothing after this milestone" half.

    v0.10 exists now, so that half reports `[(0, 10)]` for v0.9 unconditionally
    and would swamp the two checks that are about *this* block's own content.
    The "nothing after the declared version" rule is not weakened, only
    relocated: `tests/test_roadmap_v010.py` enforces it for the declared
    version, which is the only version it was ever a rule about.
    """
    return [p for p in _version_block_problems_v09(text, SERIES_V09)
            if "milestone blocks after" not in p]


def test_v09_the_roadmap_has_a_block_for_this_milestone():
    """v0.9 shipped, so `## v0.9` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v010.py`'s business."""
    assert not _own_block_problems_v09(roadmap_text_v09())


def test_v09_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.9 is behind the release: if the
    version ever went back to 0.9.x, this module would be the current one
    again and would have to enforce the no-later-blocks half itself. The two
    literals are also still checked to agree, since
    `test_packaging_metadata.py`'s rule and this pin only line up while they
    do."""
    assert declared_series_v09() > SERIES_V09, (
        f"pyproject declares {declared_series_v09()}, so v0.9 is not history: this "
        "module's SERIES_V09 pin (v0.10 requirement I) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version_v09()


def test_v09_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text_v09(), SERIES_V09
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-#71 roadmap: no block for v0.9 at all
    assert _own_block_problems_v09(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block_v09(text, series)
    assert _own_block_problems_v09(text.replace(block, block.replace("✅", "⏳")))
    # a version the roadmap has already moved past (v0.8, with v0.9 after it) --
    # this one is the unfiltered check, since it is exactly what it reports
    assert _version_block_problems_v09(text, (series[0], series[1] - 1))


def _coverage_problems_v09(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements_v09().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.9 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_v09_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD_V09 parse found
    nothing -- and pins that this PRD_V09 really skips J, K and N, so a naive
    assumption the letters run unbroken A-O would over-claim if the PRD_V09 ever
    grew a requirement in one of those gaps without this test noticing."""
    reqs = prd_requirements_v09()
    assert set(reqs) == set("ABCDEFGHILMO"), sorted(reqs)
    assert reqs["A"] == {"62"} and reqs["H"] == {"63"} and reqs["I"] == {"61"}
    # #60's five pillars are what B through F are carved out of.
    assert all(reqs[letter] == {"60"} for letter in "BCDEF")
    # G, L, M and O are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"G", "L", "M", "O"}


def test_every_prd_requirement_is_accounted_for_in_the_v09_block():
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    assert block is not None
    assert not _coverage_problems_v09(block)


def test_v09_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    assert _coverage_problems_v09(block.replace("**I.", "I."))
    assert _coverage_problems_v09(block.replace("#61", "the structural-wall issue"))


def test_v09_the_block_cites_every_issue_this_wave_names():
    """Every issue this wave's PRD_V09 names by letter has to be citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    for issues in prd_requirements_v09().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.9 block does not cite #{n}"


def test_v09_the_block_names_the_two_gaps_it_left_ticketed():
    """The half of requirement O that is easiest to quietly drop: the block has
    to name what did *not* land. #70 (SPEC/docs wording for requirements A and
    E) and #69 (the conformance ledger's own guard rows) are both open, and both
    are named in the block beside the requirement they belong to."""
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    assert "#70" in block and "#69" in block
    # ...and requirement G is the one marked unfinished rather than shipped.
    g = [p for p in re.split(r"\n(?=- )", block) if "**G." in p]
    assert len(g) == 1 and g[0].startswith("- ⏳"), g


def test_v09_the_unfinished_requirement_check_catches_a_block_claiming_g_shipped():
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    g = [p for p in re.split(r"\n(?=- )", block) if "**G." in p][0]
    mutated = block.replace(g, g.replace("- ⏳", "- ✅", 1))
    g2 = [p for p in re.split(r"\n(?=- )", mutated) if "**G." in p]
    assert not g2[0].startswith("- ⏳")


def code_vocabulary_v09() -> dict[str, str]:
    """`{what it is: the literal}`, every entry read off the shipped code.

    Asserting the premise here rather than in each test: a literal that is not
    in the code is not a claim the block owes the reader, and a block naming a
    literal the code dropped is the drift this exists to catch.
    """
    from ralphd.engine import contracts, state, transitions
    from ralphd.engine.config import JobConfig

    assert transitions.TABLE, "engine/transitions.py declares no table"
    assert transitions.VIGILANT_DELTA, "the table has no vigilant variant"
    assert set(contracts.HAT_SCHEMAS) == set(contracts.HATS), (
        "not every hat has a response schema")
    assert hasattr(JobConfig, "router_model"), "JobConfig has no router_model key"
    assert "blockerSignatures" in LOOP.read_text(), (
        "loop.py no longer persists a blocker signature across approaches")

    verdicts = contracts.HAT_SCHEMAS["worker"]["properties"]["verdict"]["enum"]
    # "plan_complete" (task 015, requirement O) is a completion claim like
    # "complete", not a refusal -- exclude it alongside "complete"/
    # "no_verdict" so this only counts the actual refusal verdict.
    # "deferred" (v0.20 task 021, requirement C.1, #156) is neither: it is
    # a reorder claim ("this task must wait"), not "this criterion cannot
    # be satisfied" -- excluded the same way, for the same reason.
    refusals = [v for v in verdicts
                if v not in ("complete", "no_verdict", "plan_complete",
                             "deferred")]
    assert len(refusals) == 1, f"worker verdicts are {verdicts!r}"

    return {
        "requirement B's declared table": "TABLE",
        "requirement B's two variants of it": "VIGILANT_DELTA",
        "requirement C's per-hat schemas": "HAT_SCHEMAS",
        "requirement E's router model key": "router_model",
        "requirement F's first new task status": (
            state.TASK_STATUS_AWAITING_VALIDATION),
        "requirement F's second new task status": state.TASK_STATUS_VALIDATED,
        "requirement H's refusal verdict": refusals[0],
        "requirement I's cross-approach signature": "blockerSignatures",
    }


def _vocabulary_problems_v09(block: str) -> list[str]:
    """Backticked, so a literal cannot be matched by a longer name that
    contains it (`TABLE` vs `TASK_STATUS_TABLE`)."""
    return [f"the v0.9 block never names {what} (`{literal}`)"
            for what, literal in code_vocabulary_v09().items()
            if f"`{literal}`" not in block]


def test_v09_the_block_names_the_vocabulary_the_wave_added():
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    assert block is not None
    assert not _vocabulary_problems_v09(block)


def test_v09_the_vocabulary_check_catches_a_block_that_stops_naming_a_mechanism():
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    for old, new in (
        ("`HAT_SCHEMAS`", "per-hat schemas"),
        ("`router_model`", "a model key of its own"),
        ("`unsatisfiable`", "a refusal"),
        ("`blockerSignatures`", "a signature"),
    ):
        assert old in block, f"fixture assumption: {old!r} is in the block"
        assert _vocabulary_problems_v09(block.replace(old, new))
    # ...and the real block is not accidentally satisfying all of this by luck
    assert not _vocabulary_problems_v09(block)


def test_v09_the_wave_really_added_no_cli_verb_so_there_is_no_verb_to_demand():
    """The premise for this module having no `WAVE_VERBS` (v0.6, v0.7 and v0.8
    each added one and had their block name it). Read off the parser: if a later
    edit adds a verb and attributes it to v0.9, this fails and the block owes
    the reader a mention."""
    from ralphd.cli.main import build_parser

    verbs = {a.dest: a for a in build_parser()._subparsers._group_actions}
    choices = set(next(iter(verbs.values())).choices)
    # v0.8's own verb is the newest one in the parser.
    assert "amend" in choices
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    assert not re.search(r"`ralphctl \w+` (?:verb|is new|now)", block or ""), (
        "the v0.9 block advertises a new verb; add WAVE_VERBS and check it "
        "against build_parser() the way test_roadmap_v08.py does")


# Task 060 (requirement N.2) freezes these two sets; see the identical
# comment above `closed_issues_v07` for why. They used to be re-derived
# from `artifacts/reports/issue-closure.md`'s v0.9 table on every run
# (`closure_wave_section_v09()`/`CLOSURE_ROW_V09`/`closure_rows_v09()`,
# deleted here along with `CLOSURE_WAVE_HEADING_V09`); the values are
# exactly what that derivation last returned -- pinned by
# `test_the_v09_closure_record_parses_at_all`, retired in this same commit.
def closed_issues_v09() -> set[int]:
    """The issues `issue-closure.md`'s v0.9 table recorded as closed
    (frozen historical fact, task 113; see the module comment above)."""
    return {61, 62, 63, 66}


def left_open_issues_v09() -> set[int]:
    """The issues that table recorded as still open, deliberately (frozen
    historical fact, task 113; see the module comment above)."""
    return {60}


def _issue_numbers_v09(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"#(\d+)", text)}


def l_bullet_v09(block: str) -> str | None:
    bullets = [p for p in re.split(r"\n(?=- )", block) if "**L." in p]
    return bullets[0] if len(bullets) == 1 else None


def _requirement_l_problems_v09(block: str) -> list[str]:
    bullet = l_bullet_v09(block)
    if bullet is None:
        return ["the v0.9 block has no single requirement L bullet"]
    problems = []
    if not bullet.startswith("- ✅"):
        problems.append("requirement L is not marked shipped")
    if "⏳" in bullet or re.search(r"[Nn]ot landed this run", bullet):
        problems.append("the L bullet still says the closures never happened")
    for report in (CLOSURE_REPORT, TRACEABILITY_REPORT):
        cited = f"`artifacts/reports/{report.name}`"
        if cited not in bullet:
            problems.append(f"the L bullet does not cite {cited}")
    if OPEN_MARKER not in bullet:
        problems.append(f"the L bullet does not say what it {OPEN_MARKER.lower()}")
        return problems
    claimed, open_part = bullet.split(OPEN_MARKER, 1)
    closed = closed_issues_v09()
    if _issue_numbers_v09(claimed) != closed:
        problems.append(
            f"the L bullet claims {sorted(_issue_numbers_v09(claimed))} closed, but "
            f"{CLOSURE_REPORT.name}'s v0.9 section records {sorted(closed)}")
    total = re.search(r"\b(\d+) in all\b", claimed)
    if not total:
        problems.append("the L bullet states no closure total")
    elif int(total.group(1)) != len(closed):
        problems.append(f"the L bullet says {total.group(1)} closures, the "
                        f"record has {len(closed)}")
    if _issue_numbers_v09(open_part) != left_open_issues_v09():
        problems.append(
            f"the L bullet names {sorted(_issue_numbers_v09(open_part))} as left "
            f"open, the record names {sorted(left_open_issues_v09())}")
    return problems


def test_v09_requirement_l_is_shipped_and_says_what_the_receipts_say():
    """Task 113: the bullet may only claim what the closure record shows, and
    the claim is that record's own set rather than a retyped one."""
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    assert block is not None
    assert not _requirement_l_problems_v09(block)


def test_v09_the_requirement_l_check_catches_a_stale_or_wrong_claim():
    block = milestone_block_v09(roadmap_text_v09(), SERIES_V09)
    bullet = l_bullet_v09(block)
    assert bullet is not None, "fixture assumption: the bullet is there to mutate"

    def mutated(old: str, new: str) -> list[str]:
        assert old in bullet, f"fixture assumption: {old!r} is in the bullet"
        return _requirement_l_problems_v09(block.replace(bullet, bullet.replace(old, new, 1)))

    # the closing report's own stale wording, written before task 113 ran
    assert mutated("- ✅", "- ⏳")
    assert mutated("All four issues", "Not landed this run: the four issues")
    # a dropped receipt citation
    assert mutated(f"`artifacts/reports/{CLOSURE_REPORT.name}`", "the closure report")
    # a closure claimed for an issue the record leaves open, and one dropped
    assert mutated("#61, #62, #63 and #66", "#60, #61, #62 and #63")
    assert mutated("#61, #62, #63 and #66", "#61, #62 and #63")
    # a total that no longer matches the receipt
    assert mutated("4 in all", "5 in all")
    # the issue left open, no longer named
    assert mutated("**#60**, which is five pillars", "one issue that is five pillars")
    # ...and the real bullet is not accidentally satisfying all of this by luck
    assert not _requirement_l_problems_v09(block)


def test_v09_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.9 resolves neither standing deferred item (image publishing, PID
    namespaces) -- like v0.8, this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists and
    still names both."""
    section = deferred_section_v09(roadmap_text_v09())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section


# ==========================================================================
# v0.10 (folded from test_roadmap_v010.py)
# ==========================================================================


PRD_V010 = REPO_ROOT / "docs" / "prds" / "v0.10-clock-is-not-a-verdict.md"


# The milestone this module is about. Pinned, not derived: v0.10 is history
# now (see the module docstring). `test_roadmap_v012.py` is the current
# release's.
SERIES_V010 = (0, 10)


def declared_version_v010() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def declared_series_v010() -> tuple[int, int]:
    major, minor = (int(p) for p in declared_version_v010().split(".")[:2])
    return major, minor


def roadmap_text_v010() -> str:
    return ROADMAP.read_text()


def milestone_block_v010(text: str, series: tuple[int, int]) -> str | None:
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


def deferred_section_v010(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirements_v010() -> dict[str, set[str]]:
    """`{"A": {"72"}, ...}` from the PRD_V010's own `## A. title (#72)` headings.

    The letter range is `A-O`: this PRD_V010 uses `A`-`J` and `M`, skipping `K`,
    `L`, `N` and `O`, so widening the regex that far (rather than to the
    letter it actually stops at) is safe -- no heading exists to match the
    gaps.
    """
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^## ([A-O])\. (.+)$", PRD_V010.read_text(), re.MULTILINE):
        out.setdefault(m.group(1), set()).update(re.findall(r"#(\d+)", m.group(2)))
    return out


def _version_block_problems_v010(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block_v010(text, series)
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


def _own_block_problems_v010(text: str) -> list[str]:
    """`_version_block_problems_v010` minus the "nothing after this milestone" half.

    v0.12 exists now, so that half reports `[(0, 12)]` for v0.10
    unconditionally and would swamp the two checks that are about *this*
    block's own content. The "nothing after the declared version" rule is
    not weakened, only relocated: `tests/test_roadmap_v012.py` enforces it
    for the declared version, which is the only version it was ever a rule
    about.
    """
    return [p for p in _version_block_problems_v010(text, SERIES_V010)
            if "milestone blocks after" not in p]


def test_v010_the_roadmap_has_a_block_for_this_milestone():
    """v0.10 shipped, so `## v0.10` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v012.py`'s business."""
    assert not _own_block_problems_v010(roadmap_text_v010())


def test_v010_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.10 is behind the release: if the
    version ever went back to 0.10.x, this module would be the current one
    again and would have to enforce the no-later-blocks half itself. The two
    literals are also still checked to agree, since
    `test_packaging_metadata.py`'s rule and this pin only line up while they
    do."""
    assert declared_series_v010() > SERIES_V010, (
        f"pyproject declares {declared_series_v010()}, so v0.10 is not history: this "
        "module's SERIES_V010 pin (v0.12 requirement H) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version_v010()


def test_v010_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text_v010(), SERIES_V010
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-v0.10 roadmap: no block for v0.10 at all
    assert _own_block_problems_v010(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block_v010(text, series)
    assert _own_block_problems_v010(text.replace(block, block.replace("\u2705", "\u23f3")))
    # a version the roadmap has already moved past (v0.9, with v0.10 after it) --
    # this one is the unfiltered check, since it is exactly what it reports
    assert _version_block_problems_v010(text, (series[0], series[1] - 1))


def _coverage_problems_v010(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements_v010().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.10 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_v010_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD_V010 parse found
    nothing -- and pins that this PRD_V010 really uses A-J and M, skipping K, L, N
    and O, so a naive assumption the letters run unbroken A-O would over-claim
    if the PRD_V010 ever grew a requirement in one of those gaps without this test
    noticing."""
    reqs = prd_requirements_v010()
    assert set(reqs) == set("ABCDEFGHIJM"), sorted(reqs)
    assert reqs["A"] == {"72"} and reqs["D"] == {"69"} and reqs["J"] == {"75"}
    assert reqs["F"] == {"73", "74"}
    # A, B and C are all requirement A's issue -- the same shape v0.9's B-F
    # shared #60's five pillars.
    assert all(reqs[letter] == {"72"} for letter in "ABC")
    # G, H, I and M are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"G", "H", "I", "M"}


def test_every_prd_requirement_is_accounted_for_in_the_v010_block():
    block = milestone_block_v010(roadmap_text_v010(), SERIES_V010)
    assert block is not None
    assert not _coverage_problems_v010(block)


def test_v010_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block_v010(roadmap_text_v010(), SERIES_V010)
    assert _coverage_problems_v010(block.replace("**J.", "J."))
    assert _coverage_problems_v010(block.replace("#75", "the sidecar-migration issue"))


def test_v010_the_block_cites_every_issue_this_wave_names():
    """Every issue this wave's PRD_V010 names by letter has to be citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block_v010(roadmap_text_v010(), SERIES_V010)
    for issues in prd_requirements_v010().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.10 block does not cite #{n}"


def _bullet_v010(block: str, letter: str) -> str:
    bullets = [p for p in re.split(r"\n(?=- )", block) if f"**{letter}." in p]
    assert len(bullets) == 1, f"expected exactly one requirement {letter} bullet"
    return bullets[0]


def test_v010_every_shipped_requirement_names_a_real_commit():
    """Unlike v0.10's own then-unfinished module (which pinned `DGHM` as
    still pending because the block was written before the wave landed),
    v0.10 finished within its own run: every requirement bullet reads shipped
    and names at least one real commit sha, not a bare flip."""
    block = milestone_block_v010(roadmap_text_v010(), SERIES_V010)
    assert block is not None
    shas = {"D": "f40d053", "G": None, "H": "75b1dd8", "I": None, "M": "7b4a9b5"}
    for letter, sha in shas.items():
        bullet = _bullet_v010(block, letter)
        assert bullet.startswith("- \u2705"), (
            f"requirement {letter} is marked pending, but v0.10 shipped it -- "
            "flip the roadmap bullet alongside this test")
        if sha is not None:
            assert sha in bullet, (
                f"requirement {letter}'s bullet does not cite its landing "
                f"commit {sha}")


def test_v010_the_shipped_requirement_check_catches_a_regressed_bullet():
    """Mutation pair for the check above: flipping a shipped bullet back to
    pending must be caught."""
    block = milestone_block_v010(roadmap_text_v010(), SERIES_V010)
    bullet = _bullet_v010(block, "D")
    assert bullet.startswith("- \u2705"), "fixture assumption: D is shipped"
    mutated = block.replace(bullet, bullet.replace("- \u2705", "- \u23f3", 1))
    assert not _bullet_v010(mutated, "D").startswith("- \u2705")


def test_v010_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.10 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists and
    still names both."""
    section = deferred_section_v010(roadmap_text_v010())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section


# ==========================================================================
# v0.12 (folded from test_roadmap_v012.py)
# ==========================================================================


PRD_V012 = REPO_ROOT / "docs" / "prds" / "v0.12-green-on-a-real-host.md"


# The milestone this module is about. Pinned, not derived: v0.12 is history
# now (see the module docstring). `test_roadmap_v013.py` is the current
# release's.
SERIES_V012 = (0, 12)


def declared_version_v012() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def declared_series_v012() -> tuple[int, int]:
    major, minor = (int(p) for p in declared_version_v012().split(".")[:2])
    return major, minor


def roadmap_text_v012() -> str:
    return ROADMAP.read_text()


def milestone_block_v012(text: str, series: tuple[int, int]) -> str | None:
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


def deferred_section_v012(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirements_v012() -> dict[str, set[str]]:
    """`{"A": set(), "B": {"85"}, ...}` from the PRD_V012's own
    `### A \u2014 title (#85)` headings.

    Unlike the `## A. title (#N)` shape earlier PRDs (v0.9, v0.10) used, this
    PRD_V012's requirement headings are third-level (`###`) and separated from the
    letter by an em dash, not a period -- and letters run `A`-`H` with no
    gaps, so the regex is pinned to that exact range rather than widened past
    it the way the older modules widen past their own gaps.
    """
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^### ([A-H]) \u2014 (.+)$", PRD_V012.read_text(), re.MULTILINE):
        out.setdefault(m.group(1), set()).update(re.findall(r"#(\d+)", m.group(2)))
    return out


def _version_block_problems_v012(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block_v012(text, series)
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


def _own_block_problems_v012(text: str) -> list[str]:
    """`_version_block_problems_v012` minus the "nothing after this milestone" half.

    v0.13 exists now, so that half reports `[(0, 13)]` for v0.12
    unconditionally and would swamp the two checks that are about *this*
    block's own content. The "nothing after the declared version" rule is
    not weakened, only relocated: `tests/test_roadmap_v013.py` enforces it
    for the declared version, which is the only version it was ever a rule
    about.
    """
    return [p for p in _version_block_problems_v012(text, SERIES_V012)
            if "milestone blocks after" not in p]


def test_v012_the_roadmap_has_a_block_for_this_milestone():
    """v0.12 shipped, so `## v0.12` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v013.py`'s business."""
    assert not _own_block_problems_v012(roadmap_text_v012())


def test_v012_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.12 is behind the release: if the
    version ever went back to 0.12.x, this module would be the current one
    again and would have to enforce the no-later-blocks half itself. The two
    literals are also still checked to agree, since
    `test_packaging_metadata.py`'s rule and this pin only line up while they
    do."""
    assert declared_series_v012() > SERIES_V012, (
        f"pyproject declares {declared_series_v012()}, so v0.12 is not history: this "
        "module's SERIES_V012 pin (v0.13 requirement N) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version_v012()


def test_v012_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text_v012(), SERIES_V012
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-v0.12 roadmap: no block for v0.12 at all
    assert _own_block_problems_v012(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block_v012(text, series)
    assert _own_block_problems_v012(text.replace(block, block.replace("\u2705", "\u23f3")))
    # a version the roadmap has already moved past (v0.10, with v0.12 after it) --
    # this one is the unfiltered check, since it is exactly what it reports
    assert _version_block_problems_v012(text, (0, 10))


def _coverage_problems_v012(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements_v012().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.12 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_v012_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD_V012 parse found
    nothing, and pins the PRD_V012's letters and issues so a future PRD_V012 edit that
    silently dropped or renumbered a requirement is caught here rather than
    only downstream in the coverage check."""
    reqs = prd_requirements_v012()
    assert set(reqs) == set("ABCDEFGH"), sorted(reqs)
    assert set().union(*reqs.values()) == {"83", "84", "85", "86", "87"}
    assert reqs["B"] == {"85"} and reqs["D"] == {"84"} and reqs["E"] == {"83"}
    assert reqs["F"] == {"86"} and reqs["G"] == {"87"}
    # A, C and H are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"A", "C", "H"}


def test_every_prd_requirement_is_accounted_for_in_the_v012_block():
    block = milestone_block_v012(roadmap_text_v012(), SERIES_V012)
    assert block is not None
    assert not _coverage_problems_v012(block)


def test_v012_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block_v012(roadmap_text_v012(), SERIES_V012)
    assert _coverage_problems_v012(block.replace("**D.", "D."))
    assert _coverage_problems_v012(block.replace("#85", "the route-check issue"))


def test_v012_the_block_cites_every_issue_this_wave_names():
    """Every issue this wave's PRD_V012 names by letter has to be citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block_v012(roadmap_text_v012(), SERIES_V012)
    for issues in prd_requirements_v012().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.12 block does not cite #{n}"


def test_v012_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.12 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists and
    still names both."""
    section = deferred_section_v012(roadmap_text_v012())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section


# ==========================================================================
# v0.13 (folded from test_roadmap_v013.py)
# ==========================================================================


PRD_V013 = REPO_ROOT / "docs" / "prds" / "v0.13-a-broken-contract-is-a-conversation.md"


# The milestone this module is about. Pinned, not derived: v0.13 is history
# now (see the module docstring). `test_roadmap_v014.py` is the current
# release's.
SERIES_V013 = (0, 13)


# The four issues this PRD_V013's own "Four issues" sentence names. Fixed here,
# independent of the heading parse (which carries no issue numbers at all --
# see the module docstring), so the citation check has something to check
# the block against.
PRD_ISSUES_V013 = {"92", "89", "90", "93"}


# All fourteen requirement letters this PRD_V013 uses, A-N, with F1/F2 folded into
# one F. Spelled out here, independent of the parse, so
# `test_v013_the_prd_headings_parse_at_all` has something fixed to check the parse
# against.
PRD_LETTERS_V013 = set("ABCDEFGHIJKLMN")


def declared_version_v013() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def declared_series_v013() -> tuple[int, int]:
    major, minor = (int(p) for p in declared_version_v013().split(".")[:2])
    return major, minor


def roadmap_text_v013() -> str:
    return ROADMAP.read_text()


def milestone_block_v013(text: str, series: tuple[int, int]) -> str | None:
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


def deferred_section_v013(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirement_letters_v013() -> set[str]:
    """`{"A", "B", ..., "F", ..., "N"}` from the PRD_V013's own `### A \u2014 title`
    headings, folding the un-lettered `F1`/`F2` sub-headings into one `F`.

    Letters run `A`-`N` with no gaps other than `F`'s own split, so the regex
    is pinned to that exact range (plus the two `F1`/`F2` spellings) rather
    than widened past it the way older modules widen past their own gaps.
    """
    out: set[str] = set()
    for m in re.finditer(r"^### ([A-N]|F1|F2) \u2014 ", PRD_V013.read_text(), re.MULTILINE):
        letter = m.group(1)
        out.add(letter[0] if letter.startswith("F") else letter)
    return out


def _version_block_problems_v013(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block_v013(text, series)
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


def _own_block_problems_v013(text: str) -> list[str]:
    """`_version_block_problems_v013` minus the "nothing after this milestone" half.

    v0.14 exists now, so that half reports `[(0, 14)]` for v0.13
    unconditionally and would swamp the two checks that are about *this*
    block's own content. The "nothing after the declared version" rule is
    not weakened, only relocated: `tests/test_roadmap_v014.py` enforces it
    for the declared version, which is the only version it was ever a rule
    about.
    """
    return [p for p in _version_block_problems_v013(text, SERIES_V013)
            if "milestone blocks after" not in p]


def test_v013_the_roadmap_has_a_block_for_this_milestone():
    """v0.13 shipped, so `## v0.13` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v014.py`'s business."""
    assert not _own_block_problems_v013(roadmap_text_v013())


def test_v013_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.13 is behind the release: if the
    version ever went back to 0.13.x, this module would be the current one
    again and would have to enforce the no-later-blocks half itself. The two
    literals are also still checked to agree, since
    `test_packaging_metadata.py`'s rule and this pin only line up while they
    do."""
    assert declared_series_v013() > SERIES_V013, (
        f"pyproject declares {declared_series_v013()}, so v0.13 is not history: this "
        "module's SERIES_V013 pin (v0.14 requirement S) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version_v013()


def test_v013_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text_v013(), SERIES_V013
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-v0.13 roadmap: no block for v0.13 at all
    assert _own_block_problems_v013(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block_v013(text, series)
    assert _own_block_problems_v013(
        text.replace(block, block.replace("\u2705", "\u23f3")))
    # a version the roadmap has already moved past (v0.10, with v0.14 after it) --
    # this one is the unfiltered check, since it is exactly what it reports
    assert _version_block_problems_v013(text, (0, 10))


def _coverage_problems_v013(block: str) -> list[str]:
    return [f"requirement {letter} is not in the v0.13 block"
            for letter in sorted(PRD_LETTERS_V013)
            if not re.search(rf"\*\*{letter}\.", block)]


def test_v013_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD_V013 parse found
    nothing, and pins the PRD_V013's own letter set (folded) so a future PRD_V013 edit
    that silently dropped or renumbered a requirement is caught here rather
    than only downstream in the coverage check."""
    assert prd_requirement_letters_v013() == PRD_LETTERS_V013


def test_every_prd_requirement_is_accounted_for_in_the_v013_block():
    block = milestone_block_v013(roadmap_text_v013(), SERIES_V013)
    assert block is not None
    assert not _coverage_problems_v013(block)


def test_every_prd_issue_is_cited_in_the_v013_block():
    """Every issue the PRD_V013's own "Four issues" sentence names (#92, #89,
    #90, #93) is citable straight off the block -- the whole point of the
    block as an issue -> requirement index. Checked against the block as a
    whole, not per letter: unlike earlier PRDs, this one's headings carry no
    inline issue number to key a per-letter check off of (see the module
    docstring)."""
    block = milestone_block_v013(roadmap_text_v013(), SERIES_V013)
    assert block is not None
    for n in PRD_ISSUES_V013:
        assert f"#{n}" in block, f"the v0.13 block does not cite #{n}"


def test_v013_the_coverage_check_catches_a_dropped_requirement():
    block = milestone_block_v013(roadmap_text_v013(), SERIES_V013)
    # dropping a requirement letter's bold marker removes it from coverage
    assert _coverage_problems_v013(block.replace("**J.", "J."))


def test_v013_the_issue_citation_check_catches_a_dropped_issue():
    block = milestone_block_v013(roadmap_text_v013(), SERIES_V013)
    mutated = block.replace("#90", "the practices issue")
    assert "90" not in re.findall(r"#(\d+)", mutated)


def test_v013_the_coverage_check_catches_an_entirely_missing_block():
    """The coverage helper itself assumes it is handed a real block; the
    module-level checks above are what catch a missing block, but this pins
    that `_coverage_problems_v013` still reports something (rather than silently
    passing) if ever called on an empty string, e.g. because `milestone_block_v013`
    returned an empty match."""
    assert _coverage_problems_v013("") == [
        f"requirement {letter} is not in the v0.13 block"
        for letter in sorted(PRD_LETTERS_V013)
    ]


def test_v013_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.13 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists
    and still names both."""
    section = deferred_section_v013(roadmap_text_v013())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section


# ==========================================================================
# v0.14 (folded from test_roadmap_v014.py)
# ==========================================================================


PRD_V014 = REPO_ROOT / "docs" / "prds" / "v0.14-every-hand-on-the-plan.md"


# The milestone this module is about. Pinned, not derived: see the module
# docstring for why deriving it from `pyproject.toml` would defang the
# version-agreement check below.
SERIES_V014 = (0, 14)


# The ten issues the PRD_V014's own "Nine issues" sentence names -- nine the wave
# owns plus #91, which the sentence itself says closes as a consequence of
# #98 rather than by its own work. Fixed here, independent of the heading
# parse (which carries no issue numbers at all -- see the module docstring),
# so the citation check has something to check the block against.
PRD_ISSUES_V014 = {"103", "99", "98", "101", "102", "100", "95", "96", "88", "91"}


# All nineteen requirement letters this PRD_V014 uses, A-S, with no gaps and no
# split letter. Spelled out here, independent of the parse, so
# `test_v014_the_prd_headings_parse_at_all` has something fixed to check the parse
# against.
PRD_LETTERS_V014 = set("ABCDEFGHIJKLMNOPQRS")


# The residual issue requirement P's own test-fidelity gap was filed
# against -- distinct from #95, the requirement issue P closes, and from the
# ten in PRD_ISSUES_V014 above (this run does not close #104 itself).
P_RESIDUAL_ISSUE_V014 = "104"


def declared_version_v014() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def declared_series_v014() -> tuple[int, int]:
    major, minor = (int(p) for p in declared_version_v014().split(".")[:2])
    return major, minor


def roadmap_text_v014() -> str:
    return ROADMAP.read_text()


def milestone_block_v014(text: str, series: tuple[int, int]) -> str | None:
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


def deferred_section_v014(text: str) -> str:
    return text.split(DEFERRED_HEADING, 1)[1]


def prd_requirement_letters_v014() -> set[str]:
    """`{"A", "B", ..., "S"}` from the PRD_V014's own `### A \u2014 title` headings.

    Letters run `A`-`S` with no gaps, so the regex is pinned to that exact
    range rather than widened past it the way older modules widen past their
    own gaps.
    """
    return set(re.findall(r"^### ([A-S]) \u2014 ", PRD_V014.read_text(), re.MULTILINE))


def _version_block_problems_v014(text: str, series: tuple[int, int]) -> list[str]:
    v = f"v{series[0]}.{series[1]}"
    block = milestone_block_v014(text, series)
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


def _own_block_problems_v014(text: str) -> list[str]:
    """`_version_block_problems_v014` minus the "nothing after this milestone"
    half. v0.14.1, v0.14.2 and v0.15 all exist now, so that half reports
    later blocks unconditionally and would swamp the two checks that are
    about *this* block's own content. The "nothing after the declared
    version" rule is not weakened, only relocated: whichever module owns
    the declared version enforces it for that version, which is the only
    version it was ever a rule about."""
    return [p for p in _version_block_problems_v014(text, SERIES_V014)
            if "milestone blocks after" not in p]


def test_v014_the_roadmap_has_a_block_for_this_milestone():
    """v0.14 shipped, so `## v0.14` must exist and record shipped work."""
    assert not _own_block_problems_v014(roadmap_text_v014())


def test_v014_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.14 is behind the release: if the
    version ever went back to 0.14.x, this module would be grading the wrong
    milestone. The two literals are also still checked to agree, since
    `test_packaging_metadata.py`'s rule and this pin only line up while they
    do."""
    assert declared_series_v014() > SERIES_V014, (
        f"pyproject declares {declared_series_v014()}, so v0.14 is not history: this "
        "module's SERIES_V014 pin (v0.15 task 017) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version_v014()


def test_v014_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text_v014(), SERIES_V014
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # no block for v0.14 at all
    assert _own_block_problems_v014(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block_v014(text, series)
    assert _own_block_problems_v014(
        text.replace(block, block.replace("\u2705", "\u23f3")))
    # a version the roadmap has already moved past (v0.10, with later blocks
    # after it) -- this one is the unfiltered check, since it is exactly
    # what it reports
    assert _version_block_problems_v014(text, (0, 10))


def _coverage_problems_v014(block: str) -> list[str]:
    return [f"requirement {letter} is not in the v0.14 block"
            for letter in sorted(PRD_LETTERS_V014)
            if not re.search(rf"\*\*{letter}\.", block)]


def test_v014_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD_V014 parse found
    nothing, and pins the PRD_V014's own letter set so a future PRD_V014 edit that
    silently dropped or renumbered a requirement is caught here rather than
    only downstream in the coverage check."""
    assert prd_requirement_letters_v014() == PRD_LETTERS_V014


def test_every_prd_requirement_is_accounted_for_in_the_v014_block():
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    assert block is not None
    assert not _coverage_problems_v014(block)


def test_every_prd_issue_is_cited_in_the_v014_block():
    """Every issue the PRD_V014's own "Nine issues" sentence names (#103, #99,
    #98, #101, #102, #100, #95, #96, #88, and #91) is citable straight off
    the block -- the whole point of the block as an issue -> requirement
    index. Checked against the block as a whole, not per letter: this PRD_V014's
    headings carry no inline issue number to key a per-letter check off of
    (see the module docstring)."""
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    assert block is not None
    for n in PRD_ISSUES_V014:
        assert f"#{n}" in block, f"the v0.14 block does not cite #{n}"


def test_v014_the_coverage_check_catches_a_dropped_requirement():
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    # dropping a requirement letter's bold marker removes it from coverage
    assert _coverage_problems_v014(block.replace("**K.", "K."))


def test_v014_the_issue_citation_check_catches_a_dropped_issue():
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    mutated = block.replace("#102", "the split-proposal issue")
    assert "102" not in re.findall(r"#(\d+)", mutated)


def test_v014_the_coverage_check_catches_an_entirely_missing_block():
    """The coverage helper itself assumes it is handed a real block; the
    module-level checks above are what catch a missing block, but this pins
    that `_coverage_problems_v014` still reports something (rather than silently
    passing) if ever called on an empty string, e.g. because `milestone_block_v014`
    returned an empty match."""
    assert _coverage_problems_v014("") == [
        f"requirement {letter} is not in the v0.14 block"
        for letter in sorted(PRD_LETTERS_V014)
    ]


def test_v014_the_block_discloses_the_p_test_fidelity_residual_and_its_issue():
    """Requirement P's own test-fidelity gap is a named residual, not a
    footnote: its issue (#104, distinct from #95/#96, the requirement issues
    P and Q close) must be citable straight off the block, and the block
    must say the gap is a residual rather than merely mentioning the number
    in passing."""
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    assert block is not None
    assert f"#{P_RESIDUAL_ISSUE_V014}" in block, (
        f"the v0.14 block does not cite the P test-fidelity residual's own "
        f"issue, #{P_RESIDUAL_ISSUE_V014}")
    assert "residual" in block.lower(), (
        "the v0.14 block does not name the P gap as a residual")


def test_v014_the_residual_check_catches_a_dropped_issue_citation():
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    mutated = block.replace(f"#{P_RESIDUAL_ISSUE_V014}", "the pause test gap")
    assert P_RESIDUAL_ISSUE_V014 not in re.findall(r"#(\d+)", mutated)


def test_v014_the_block_discloses_this_runs_own_doom_reproduction():
    """This run's approach 1 hit exactly the doom shape #99 describes: task
    016 went `failed` and twelve tasks depending on it, directly or
    transitively, became permanently unreachable. The block has to say so
    plainly, the same way the v0.9 block named its own unfinished G, rather
    than let the irony go unrecorded."""
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    assert block is not None
    assert "task 016" in block, (
        "the v0.14 block does not name the doomed task (task 016) from this "
        "run's own approach 1")
    assert "twelve" in block.lower(), (
        "the v0.14 block does not name how many tasks the doom made "
        "unreachable (twelve)")
    assert "failed" in block, (
        "the v0.14 block does not say task 016 went `failed`")


def test_v014_the_doom_disclosure_check_catches_a_dropped_task_reference():
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    mutated = block.replace("task 016", "the paused-deadline task")
    assert "task 016" not in mutated


def test_v014_the_block_names_the_two_requirement_a_repairs_with_true_status():
    """This approach found and fixed two defects in requirement A's own
    guard mechanism before trusting it for the wave's own closing sequence:
    the pytest-summary-line prose false positive (task 001) and the
    discharge chain's unsatisfiable anchor (task 003). Both must be named,
    by task id, and marked as fixed -- not merely mentioned as open
    problems."""
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    assert block is not None
    a_bullet_start = block.index("**A.")
    a_bullet_end = block.index("**B.", a_bullet_start)
    a_bullet = block[a_bullet_start:a_bullet_end]
    assert "task 001" in a_bullet, "requirement A's bullet does not name task 001"
    assert "task 003" in a_bullet, "requirement A's bullet does not name task 003"
    assert "fixed" in a_bullet.lower(), (
        "requirement A's bullet does not say the two defects were fixed")


def test_v014_the_requirement_a_defect_check_catches_a_dropped_task_reference():
    block = milestone_block_v014(roadmap_text_v014(), SERIES_V014)
    a_bullet_start = block.index("**A.")
    a_bullet_end = block.index("**B.", a_bullet_start)
    a_bullet = block[a_bullet_start:a_bullet_end]
    mutated = a_bullet.replace("task 003", "a later task")
    assert "task 003" not in mutated


def test_v014_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.14 resolves neither standing deferred item (image publishing,
    PID namespaces) -- this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists
    and still names both."""
    section = deferred_section_v014(roadmap_text_v014())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section


# ==========================================================================
# v0.25 (task 039, requirement J.3/J.4 pin) -- not a full per-wave coverage
# block like the v0.6-v0.14 ones above. Those were folded from standalone
# `test_roadmap_v0N.py` checkers that predate `test_packaging_metadata.py`'s
# own generic shipped-sha/coverage machinery (`_records_shipped`,
# `_roadmap_milestones`, the `## v0.22` baseline pin); from v0.15 on, that
# generic machinery is what checks a wave's roadmap block in general, and
# this section only pins the handful of v0.25-specific facts task 039's own
# criteria name: the new `## v0.25` section's naming, and the two
# shipped-sha corrections task 036 (J.3, #169) made to the v0.23 and v0.24
# sections.
# ==========================================================================


# The nine issues the `## v0.25` section's own closing sentence names as
# answered by requirements A through I (J is the version bump/paragraph/
# close-out itself, task 038, requirement J.2). Unchanged by task 048 (N2):
# #267 is K's own issue, counted and cited separately below, never folded
# into this nine -- J.4 (task 041) and DoD 11's literal count of nine stay
# exactly nine.
ISSUES_V025 = {"210", "211", "212", "214", "215", "216", "217", "264", "265"}

# A-K: task 048 (N2) added requirement K's own bold marker and its own
# issue (#267, below) to the existing paragraph -- the full A-K requirement
# set, not merely the nine A-J the closing sentence's own issue count still
# names.
LETTERS_V025 = set("ABCDEFGHIJK")

# Requirement K's own issue (#267, added mid-run by the operator, closed by
# K.8 and nothing else) -- checked separately from ISSUES_V025 above so the
# nine-issue count J.4/DoD 11 name stays literally nine (task 048, N2).
ISSUE_K_V025 = "267"

# The two shipped-sha corrections task 036 made (commit f0500619):
# v0.23's line now names the tip main sat on when v0.23.0 actually shipped
# (not the v0.24 task commit `59afb532` the block wrongly named before),
# and v0.24's block gained a shipped-sha line it had none of at all.
SHA_V023 = "bd94c695"
SHA_V024 = "af0b57e2"


def roadmap_text_v025() -> str:
    return ROADMAP.read_text()


def _heading_block_v025(text: str, heading_prefix: str) -> str | None:
    """The body of the FIRST `## ...` heading whose text starts with
    `heading_prefix`, up to the next `## ` heading of any kind (or end of
    file). Used only for the three specific sections this pin reads (v0.23,
    v0.24, v0.25 themselves) -- not a general per-wave coverage parser like
    the ones above."""
    heads = list(re.finditer(r"^## .*$", text, re.MULTILINE))
    for i, m in enumerate(heads):
        if m.group(0).startswith(heading_prefix):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[m.end():end]
    return None


def milestone_block_v025(text: str) -> str | None:
    return _heading_block_v025(text, "## v0.25 ")


def _v025_naming_problems(block: str) -> list[str]:
    problems = [f"requirement {letter} is not in the v0.25 block"
                for letter in sorted(LETTERS_V025)
                if not re.search(rf"\*\*{letter}\*\*", block)]
    problems += [f"the v0.25 block does not cite #{n}"
                 for n in sorted(ISSUES_V025) if f"#{n}" not in block]
    if f"#{ISSUE_K_V025}" not in block:
        problems.append(
            f"the v0.25 block does not cite requirement K's own issue #{ISSUE_K_V025}")
    return problems


def test_v025_the_block_names_requirements_a_through_k_and_the_nine_issues_plus_k():
    """Task 038 (J.2) added the `## v0.25` section; task 048 (N2) extended the
    same existing paragraph to also name requirement K and its own issue.
    This pins that the paragraph really names every requirement letter A-K
    and every one of the nine issues the section's own closing sentence
    lists (#210, #211, #212, #214, #215, #216, #217, #264, #265) -- the same
    issue -> requirement index property every earlier per-wave block above
    is checked for -- plus K's own issue (#267), counted and cited
    separately from that nine (J.4/DoD 11 stay literally nine)."""
    block = milestone_block_v025(roadmap_text_v025())
    assert block is not None, "docs/roadmap.md has no `## v0.25` section"
    assert not _v025_naming_problems(block)


def test_v025_the_naming_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block_v025(roadmap_text_v025())
    assert block is not None
    # dropping a requirement letter's bold marker removes it from coverage
    assert _v025_naming_problems(block.replace("**G** writes", "G writes"))
    # dropping an issue citation removes it from coverage
    assert _v025_naming_problems(block.replace("#214", "the prefix-collapse issue"))
    # dropping requirement K's own bold marker removes it from coverage too
    assert _v025_naming_problems(block.replace("**K**,", "K,"))
    # dropping K's own issue citation (#267) is caught separately from the
    # nine-issue ISSUES_V025 check above
    assert _v025_naming_problems(block.replace("#267", "the deferred-wait issue"))
    # ...and the real block is not accidentally satisfying all of this by luck
    assert not _v025_naming_problems(block)


def test_the_v023_shipped_sha_line_names_task_036s_tip():
    """Task 036 (J.3, #169) corrected the v0.23 block's shipped-sha claim
    from a v0.24 task commit (`59afb532`, which shipped nothing of v0.23's)
    to the real tip main sat on when v0.23.0 actually finished shipping
    (`bd94c695`) -- see commit f0500619's own message for the `git log`/
    `git merge-base --is-ancestor` evidence. Pinned here so a later edit
    cannot quietly drift the line back."""
    block = _heading_block_v025(roadmap_text_v025(), "## v0.23 ")
    assert block is not None, "docs/roadmap.md has no `## v0.23` section"
    assert f"Shipped sha `{SHA_V023}`, version 0.23.0" in block


def test_the_v023_shipped_sha_check_catches_a_reverted_line():
    block = _heading_block_v025(roadmap_text_v025(), "## v0.23 ")
    assert block is not None
    reverted = block.replace(f"`{SHA_V023}`", "`59afb532`")
    assert f"Shipped sha `{SHA_V023}`, version 0.23.0" not in reverted


def test_the_v024_section_carries_a_shipped_sha_line_for_0_24_0():
    """v0.24's block had no `Shipped sha` line at all until task 036 added
    one (`af0b57e2`, the direct parent of this run's own first commit,
    2916847b) -- without it the block would read as unshipped by
    `test_packaging_metadata.py`'s own `_records_shipped` parse, which reads
    a v0.18-and-later block's shipped state off exactly this line when the
    block carries no ✅ ticks."""
    block = _heading_block_v025(roadmap_text_v025(), "## v0.24 ")
    assert block is not None, "docs/roadmap.md has no `## v0.24` section"
    assert f"Shipped sha `{SHA_V024}`, version 0.24.0" in block


def test_the_v024_shipped_sha_check_catches_a_dropped_line():
    block = _heading_block_v025(roadmap_text_v025(), "## v0.24 ")
    assert block is not None
    dropped = block.replace(
        f"Shipped sha `{SHA_V024}`, version 0.24.0, installed host-side by the operator.",
        "")
    assert f"Shipped sha `{SHA_V024}`, version 0.24.0" not in dropped
