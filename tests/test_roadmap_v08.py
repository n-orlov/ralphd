"""Task 032 (v0.8 requirement O): `docs/roadmap.md`'s v0.8 block and the bump.

Retargeted by v0.9's requirement O (issue #71), exactly the way task 032 itself
retargeted `tests/test_roadmap_v07.py` when v0.8 became current: v0.8 is no
longer the declared release, so `SERIES` here is **pinned** to `(0, 8)` rather
than read from `pyproject.toml`, and the "declared version has a block" half
moved to `tests/test_roadmap_v09.py`. `declared_version()` /
`declared_series()` stay, used only to assert this block is a *past* milestone
-- if they were still used to select the block, every check below would grade
v0.9's block against v0.8's PRD. One thing established by earlier waves stays
true here, and one moved:

* (moved) the roadmap's last milestone and the released version are the same
  fact, so the block for `pyproject.toml`'s declared version must exist, must
  record shipped work, and nothing may claim a milestone *after* it. That is
  `tests/test_roadmap_v09.py`'s business now; the "nothing after" half is
  filtered out of the two checks here that would otherwise fail on v0.9's own
  block simply for existing;
* a block may not claim a wave landed while omitting requirements of that
  wave. The requirement letters and issue numbers are parsed out of the PRD
  the block is about (`docs/prds/v0.8-auditable-interruptible.md`), never
  retyped here. This PRD's letters run `A-H` and `M-O` -- it does not use
  `I-L` at all, unlike v0.7's PRD, which used every letter up to `R`.

Task 032 (this module's own commit) wrote **N** (closing this wave's issues
from inside the run) honestly pending -- it ran before task 035, which is the
task that actually closes anything over the API. Task 035 has since run and
closed all nine of this wave's issues, so this module (mirroring how task 042
amended `test_roadmap_v07.py` once v0.7's own requirement Q's receipts
existed) now pins N as shipped and cross-checks its claim against
`artifacts/reports/issue-closure.md`'s own v0.8 section, the same shape as
v0.7's requirement Q below -- except v0.8's closure ledger has no issue left
open within its own wave (#25 and #59 are simply outside its scope, never
given a comment or a state change, so they cannot appear as a *closure left
open* the way v0.7's #50 did).

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
PRD = REPO_ROOT / "docs" / "prds" / "v0.8-auditable-interruptible.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "src" / "ralphd" / "__init__.py"

DEFERRED_HEADING = "## Later / explicitly deferred"

# The milestone this module is about. Pinned, not derived: v0.8 is history now
# (see the module docstring). `test_roadmap_v09.py` is the current release's.
SERIES = (0, 8)

# Verb this wave's requirement H added to `ralphctl` (#26). Must exist in the
# parser (the code-derived half) *and* be named in the block.
WAVE_VERBS = ("amend",)


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
    """`{"A": {"56"}, ...}` from the PRD's own `## A. title (#56)` headings.

    The letter range is `A-O`: this PRD skips `I-L` entirely, so widening the
    regex that far (rather than to the letter it actually stops at) is safe --
    no heading exists to match the gap.
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
    if "✅" not in block:
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

    v0.9 exists now, so that half reports `[(0, 9)]` for v0.8 unconditionally and
    would swamp the two checks that are about *this* block's own content. The
    "nothing after the declared version" rule is not weakened, only relocated:
    `tests/test_roadmap_v09.py` enforces it for the declared version, which is
    the only version it was ever a rule about.
    """
    return [p for p in _version_block_problems(text, SERIES)
            if "milestone blocks after" not in p]


def test_the_roadmap_has_a_block_for_this_milestone():
    """v0.8 shipped, so `## v0.8` must exist and record shipped work. The
    *declared* version's block is `test_roadmap_v09.py`'s business."""
    assert not _own_block_problems(roadmap_text())


def test_v08_is_a_past_milestone_of_the_declared_version():
    """The pin above is only honest while 0.8 is behind the release: if the
    version ever went back to 0.8.x, this module would be the current one again
    and would have to enforce the no-later-blocks half itself. The two literals
    are also still checked to agree, since `test_packaging_metadata.py`'s rule
    and this pin only line up while they do."""
    assert declared_series() > SERIES, (
        f"pyproject declares {declared_series()}, so v0.8 is not history: this "
        "module's SERIES pin (v0.9 requirement O) needs revisiting")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version()


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), SERIES
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-032 roadmap: no block for v0.8 at all
    assert _own_block_problems(text.replace(heading, "## Not a milestone"))
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _own_block_problems(text.replace(block, block.replace("✅", "⏳")))
    # a version the roadmap has already moved past (v0.7, with v0.8 after it) --
    # this one is the unfiltered check, since it is exactly what it reports
    assert _version_block_problems(text, (series[0], series[1] - 1))


# --------------------------------------------------- the block covers the wave


def _coverage_problems(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.8 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD parse found
    nothing -- and pins that the PRD really skips I-L, so a naive assumption
    the letters run unbroken A-O would silently under-check nothing (there is
    nothing at I-L to under-check) but *would* over-claim if this PRD ever
    grew a requirement in that gap without this test noticing."""
    reqs = prd_requirements()
    assert set(reqs) == set("ABCDEFGHMNO"), sorted(reqs)
    assert reqs["A"] == {"56"} and reqs["G"] == {"54"} and reqs["H"] == {"26"}
    # N and O are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"N", "O"}


def test_every_prd_requirement_is_accounted_for_in_the_v08_block():
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert not _coverage_problems(block)


def test_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block(roadmap_text(), SERIES)
    assert _coverage_problems(block.replace("**H.", "H."))
    assert _coverage_problems(block.replace("#26", "the operator-ruling issue"))


def test_the_block_cites_every_issue_this_wave_names():
    """Every issue this wave's PRD names by letter has to be citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block(roadmap_text(), SERIES)
    for issues in prd_requirements().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.8 block does not cite #{n}"


def test_the_v08_block_names_the_verb_the_wave_added():
    from ralphd.cli.main import build_parser

    verbs = {a.dest: a for a in build_parser()._subparsers._group_actions}
    choices = set(next(iter(verbs.values())).choices)
    block = milestone_block(roadmap_text(), SERIES)
    for verb in WAVE_VERBS:
        assert verb in choices, f"WAVE_VERBS names {verb!r}, which the parser lacks"
        assert f"`ralphctl {verb}" in block, (
            f"the v0.8 block claims requirement H shipped but never names "
            f"`ralphctl {verb}`")


# ------------------------------------------- requirement N and its closure receipt

CLOSURE_REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-closure.md"
TRACEABILITY_REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-traceability.md"


def n_bullet(block: str) -> str | None:
    bullets = [p for p in re.split(r"\n(?=- )", block) if "**N." in p]
    return bullets[0] if len(bullets) == 1 else None


def closure_wave_v08_section() -> str:
    text = CLOSURE_REPORT.read_text()
    heads = list(re.finditer(r"^# Issue closure record", text, re.MULTILINE))
    for i, m in enumerate(heads):
        if "v0.8" not in text[m.start(): m.start() + 80]:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        return text[m.start(): end]
    raise AssertionError("issue-closure.md has no v0.8 section")


def closed_issues_v08() -> set[int]:
    """The issues the v0.8 closure section's own `## #N` subsections mark
    `**closed**` in a `Resulting state` line."""
    out: set[int] = set()
    for m in re.finditer(r"^## #(\d+)\b([\s\S]*?)(?=^## |\Z)",
                         closure_wave_v08_section(), re.MULTILINE):
        if "**closed**" in m.group(2):
            out.add(int(m.group(1)))
    return out


def _issue_numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"#(\d+)", text)}


def _requirement_n_problems(block: str) -> list[str]:
    bullet = n_bullet(block)
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
    claimed = _issue_numbers(bullet.split("closed over the", 1)[0])
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


def test_the_v08_closure_record_parses_at_all():
    """Guards the checks below from passing on an empty parse -- and pins what
    task 035 actually did: all nine of this wave's issues closed, none left
    open."""
    closed = closed_issues_v08()
    assert closed == {24, 26, 27, 50, 51, 54, 56, 57, 58}, sorted(closed)
    assert len(closed) == 9


def test_requirement_n_is_shipped_and_says_what_the_receipts_say():
    """Task 035: the bullet flips to shipped only once the issues are closed,
    and its claim is the closure record's own set, not a retyped one."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert not _requirement_n_problems(block)


def test_the_requirement_n_check_catches_a_stale_or_wrong_claim():
    block = milestone_block(roadmap_text(), SERIES)
    bullet = n_bullet(block)
    assert bullet is not None, "fixture assumption: the bullet is there to mutate"

    def mutated(old: str, new: str) -> list[str]:
        assert old in bullet, f"fixture assumption: {old!r} is in the bullet"
        return _requirement_n_problems(block.replace(bullet, bullet.replace(old, new, 1)))

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
    assert not _requirement_n_problems(block)


# ------------------------------------------- what the wave delivered is not deferred


def test_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.8 does not resolve either standing deferred item (image publishing,
    PID namespaces) -- unlike v0.6/v0.7, this wave has no deferred-list entry
    of its own to correct, so the only property to hold is that the section
    still exists and still names both."""
    section = deferred_section(roadmap_text())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section
