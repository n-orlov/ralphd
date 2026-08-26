"""v0.9 requirement O (#71): `docs/roadmap.md`'s v0.9 block and the bump.

`tests/test_roadmap_v08.py`'s module for the *current* release, until the next
wave retargets it the same way this one retargeted v0.8's: pin that module's
`SERIES` to `(0, 8)` and move the "declared version has a block" half here.
Two things established by earlier waves stay true:

* the roadmap's last milestone and `pyproject.toml`'s declared version are the
  same fact, so the block for the declared version must exist, must record
  shipped work, and nothing may claim a milestone *after* it;
* a block may not claim a wave landed while omitting requirements of that wave.
  The requirement letters and issue numbers are parsed out of the PRD the block
  is about (`docs/prds/v0.9-declarative-loop.md`), never retyped here. This
  PRD's letters run `A-I`, then `L`, `M` and `O` -- it uses neither `J` and `K`
  (its own "the harness you will measure with" note says why there is no J) nor
  `N`, so the regex is widened to `A-O` and the parse itself is pinned below.

Where v0.7's and v0.8's modules ended with a `WAVE_VERBS` check, this one does
not: **v0.9 added no `ralphctl` verb** (requirement A moved the control plane
without needing one -- `build_parser()`'s choices are unchanged from the
pre-launch sha). The code-derived half is the engine vocabulary the wave
introduced instead -- the declared table and its two variants, the per-hat
contract schemas, the router's model key, the two new task statuses, the
worker's refusal verdict and the cross-approach blocker signature -- each read
off the shipped code and then demanded of the block, so the block cannot become
prose nobody re-reads.

Requirement L (closing this wave's issues from inside the run) is cross-checked
against the receipt task 113 wrote (`artifacts/reports/issue-closure.md`'s v0.9
section): the issues the bullet claims closed, the total, and the one it names
as deliberately left open are all that record's own sets, parsed, never retyped
here -- so the roadmap cannot claim a closure GitHub does not have. Unlike
v0.8's wave, this one *does* leave an issue open inside its own scope (#60, five
pillars of which this wave lands three plus the boundary), so the shape is
v0.7's requirement Q rather than v0.8's requirement N.

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
PRD = REPO_ROOT / "docs" / "prds" / "v0.9-declarative-loop.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT = REPO_ROOT / "src" / "ralphd" / "__init__.py"
LOOP = REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py"
CLOSURE_REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-closure.md"
TRACEABILITY_REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-traceability.md"
CLOSURE_WAVE_HEADING = "# Issue closure record — ralphd v0.9"

DEFERRED_HEADING = "## Later / explicitly deferred"

# The milestone this wave ships. Derived, not pinned: this module is about the
# *declared* version's block, per the docstring above.
SERIES = (0, 9)


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
    """`{"A": {"62"}, ...}` from the PRD's own `## A. title (#62)` headings.

    The letter range is `A-O`: this PRD skips `J`, `K` and `N`, so widening the
    regex that far (rather than to the letter it actually stops at) is safe --
    no heading exists to match the gaps.
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


def test_the_declared_version_is_this_wave_and_both_literals_agree():
    """The bump half of requirement O: 0.9.0 in `pyproject.toml` and in
    `src/ralphd/__init__.py` (`test_packaging_metadata.py` owns the general
    rule; this pins the number this block claims)."""
    assert declared_series() == SERIES, (
        f"docs/roadmap.md's v0.9 block is the current milestone, but "
        f"pyproject.toml declares {declared_version()!r}")
    m = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(), re.MULTILINE)
    assert m and m.group(1) == declared_version()


def test_the_roadmap_has_a_block_for_the_declared_version():
    """The released version and the roadmap's own last milestone are the same
    thing: 0.9.0 ships v0.9, so `## v0.9` must exist and record shipped work."""
    assert not _version_block_problems(roadmap_text(), declared_series())


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), SERIES
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-#71 roadmap: no block for the version pyproject declares
    assert _version_block_problems(text.replace(heading, "## Not a milestone"), series)
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _version_block_problems(text.replace(block, block.replace("✅", "⏳")), series)
    # a version the roadmap has already moved past (v0.8, with v0.9 after it)
    assert _version_block_problems(text, (series[0], series[1] - 1))


def test_the_pre_v09_section_is_not_read_as_a_milestone_after_the_declared_one():
    """`## After v0.8 — ...` sits between the v0.8 and v0.9 blocks and is
    deliberately *not* a milestone heading: it does not start `## v`, so it
    cannot trip the "nothing after the declared version" half above. It also
    stays inside the v0.8 block's parsed body, where it belongs -- those three
    defects are v0.8's claims being corrected, not v0.9's work, and a v0.9
    coverage check satisfied by v0.8-era text would be exactly the hazard
    `milestone_block`'s deferred-split already guards against."""
    text = roadmap_text()
    assert "## After v0.8" in text, "fixture assumption: the section is there"
    heads = [(int(a), int(b)) for a, b in
             re.findall(r"^## v(\d+)\.(\d+)\b", text, re.MULTILINE)]
    assert max(heads) == SERIES, heads
    assert "## After v0.8" in (milestone_block(text, (0, 8)) or "")
    assert "## After v0.8" not in (milestone_block(text, SERIES) or "")


# --------------------------------------------------- the block covers the wave


def _coverage_problems(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.9 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD parse found
    nothing -- and pins that this PRD really skips J, K and N, so a naive
    assumption the letters run unbroken A-O would over-claim if the PRD ever
    grew a requirement in one of those gaps without this test noticing."""
    reqs = prd_requirements()
    assert set(reqs) == set("ABCDEFGHILMO"), sorted(reqs)
    assert reqs["A"] == {"62"} and reqs["H"] == {"63"} and reqs["I"] == {"61"}
    # #60's five pillars are what B through F are carved out of.
    assert all(reqs[letter] == {"60"} for letter in "BCDEF")
    # G, L, M and O are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"G", "L", "M", "O"}


def test_every_prd_requirement_is_accounted_for_in_the_v09_block():
    block = milestone_block(roadmap_text(), declared_series())
    assert block is not None
    assert not _coverage_problems(block)


def test_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block(roadmap_text(), SERIES)
    assert _coverage_problems(block.replace("**I.", "I."))
    assert _coverage_problems(block.replace("#61", "the structural-wall issue"))


def test_the_block_cites_every_issue_this_wave_names():
    """Every issue this wave's PRD names by letter has to be citable straight
    off the block -- the whole point of the block as an issue -> requirement
    index."""
    block = milestone_block(roadmap_text(), declared_series())
    for issues in prd_requirements().values():
        for n in issues:
            assert f"#{n}" in block, f"the v0.9 block does not cite #{n}"


def test_the_block_names_the_two_gaps_it_left_ticketed():
    """The half of requirement O that is easiest to quietly drop: the block has
    to name what did *not* land. #70 (SPEC/docs wording for requirements A and
    E) and #69 (the conformance ledger's own guard rows) are both open, and both
    are named in the block beside the requirement they belong to."""
    block = milestone_block(roadmap_text(), declared_series())
    assert "#70" in block and "#69" in block
    # ...and requirement G is the one marked unfinished rather than shipped.
    g = [p for p in re.split(r"\n(?=- )", block) if "**G." in p]
    assert len(g) == 1 and g[0].startswith("- ⏳"), g


def test_the_unfinished_requirement_check_catches_a_block_claiming_g_shipped():
    block = milestone_block(roadmap_text(), SERIES)
    g = [p for p in re.split(r"\n(?=- )", block) if "**G." in p][0]
    mutated = block.replace(g, g.replace("- ⏳", "- ✅", 1))
    g2 = [p for p in re.split(r"\n(?=- )", mutated) if "**G." in p]
    assert not g2[0].startswith("- ⏳")


# ------------------------------------------- the vocabulary is read off the code


def code_vocabulary() -> dict[str, str]:
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
    refusals = [v for v in verdicts if v not in ("complete", "no_verdict")]
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


def _vocabulary_problems(block: str) -> list[str]:
    """Backticked, so a literal cannot be matched by a longer name that
    contains it (`TABLE` vs `TASK_STATUS_TABLE`)."""
    return [f"the v0.9 block never names {what} (`{literal}`)"
            for what, literal in code_vocabulary().items()
            if f"`{literal}`" not in block]


def test_the_block_names_the_vocabulary_the_wave_added():
    block = milestone_block(roadmap_text(), declared_series())
    assert block is not None
    assert not _vocabulary_problems(block)


def test_the_vocabulary_check_catches_a_block_that_stops_naming_a_mechanism():
    block = milestone_block(roadmap_text(), SERIES)
    for old, new in (
        ("`HAT_SCHEMAS`", "per-hat schemas"),
        ("`router_model`", "a model key of its own"),
        ("`unsatisfiable`", "a refusal"),
        ("`blockerSignatures`", "a signature"),
    ):
        assert old in block, f"fixture assumption: {old!r} is in the block"
        assert _vocabulary_problems(block.replace(old, new))
    # ...and the real block is not accidentally satisfying all of this by luck
    assert not _vocabulary_problems(block)


def test_the_wave_really_added_no_cli_verb_so_there_is_no_verb_to_demand():
    """The premise for this module having no `WAVE_VERBS` (v0.6, v0.7 and v0.8
    each added one and had their block name it). Read off the parser: if a later
    edit adds a verb and attributes it to v0.9, this fails and the block owes
    the reader a mention."""
    from ralphd.cli.main import build_parser

    verbs = {a.dest: a for a in build_parser()._subparsers._group_actions}
    choices = set(next(iter(verbs.values())).choices)
    # v0.8's own verb is the newest one in the parser.
    assert "amend" in choices
    block = milestone_block(roadmap_text(), declared_series())
    assert not re.search(r"`ralphctl \w+` (?:verb|is new|now)", block or ""), (
        "the v0.9 block advertises a new verb; add WAVE_VERBS and check it "
        "against build_parser() the way test_roadmap_v08.py does")


# ------------------------------------------- requirement L matches the receipts


def closure_wave_section() -> str:
    """`artifacts/reports/issue-closure.md`'s v0.9 half (the file holds four
    waves, so this stops at the next `# Issue closure record` heading rather
    than running to end of file)."""
    text = CLOSURE_REPORT.read_text()
    assert CLOSURE_WAVE_HEADING in text, (
        f"{CLOSURE_REPORT.name} has no {CLOSURE_WAVE_HEADING!r} section")
    rest = text.split(CLOSURE_WAVE_HEADING, 1)[1]
    nxt = re.search(r"^# Issue closure record", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


CLOSURE_ROW = re.compile(
    r"^\|\s*\[#(\d+)\]\([^)]*\)\s*\|(?P<rest>.*)\|\s*$", re.MULTILINE)


def closure_rows() -> dict[int, str]:
    """`{61: "... | **closed** ", ...}` from the v0.9 section's summary table.

    v0.9's record is a table where v0.7's and v0.8's were `## #N` subsections,
    so this parses the shape task 113 actually wrote rather than the shape an
    earlier wave's parser expected.
    """
    return {int(m.group(1)): m.group("rest")
            for m in CLOSURE_ROW.finditer(closure_wave_section())}


def closed_issues() -> set[int]:
    """The issues whose row records the state a fresh `GET` answered: closed."""
    return {n for n, row in closure_rows().items() if "**closed**" in row}


def left_open_issues() -> set[int]:
    """The issues whose row records them still open, deliberately."""
    return {n for n, row in closure_rows().items() if "**open**" in row}


def _issue_numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"#(\d+)", text)}


def l_bullet(block: str) -> str | None:
    bullets = [p for p in re.split(r"\n(?=- )", block) if "**L." in p]
    return bullets[0] if len(bullets) == 1 else None


OPEN_MARKER = "Left open deliberately"


def _requirement_l_problems(block: str) -> list[str]:
    bullet = l_bullet(block)
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
    closed = closed_issues()
    if _issue_numbers(claimed) != closed:
        problems.append(
            f"the L bullet claims {sorted(_issue_numbers(claimed))} closed, but "
            f"{CLOSURE_REPORT.name}'s v0.9 section records {sorted(closed)}")
    total = re.search(r"\b(\d+) in all\b", claimed)
    if not total:
        problems.append("the L bullet states no closure total")
    elif int(total.group(1)) != len(closed):
        problems.append(f"the L bullet says {total.group(1)} closures, the "
                        f"record has {len(closed)}")
    if _issue_numbers(open_part) != left_open_issues():
        problems.append(
            f"the L bullet names {sorted(_issue_numbers(open_part))} as left "
            f"open, the record names {sorted(left_open_issues())}")
    return problems


def test_the_v09_closure_record_parses_at_all():
    """Guards the checks below from passing on an empty parse -- and pins what
    task 113 actually did: four closures, #60 left open on purpose."""
    closed, left_open = closed_issues(), left_open_issues()
    assert closed == {61, 62, 63, 66}, sorted(closed)
    assert len(closed) == 4
    assert left_open == {60}, sorted(left_open)
    assert not closed & left_open


def test_requirement_l_is_shipped_and_says_what_the_receipts_say():
    """Task 113: the bullet may only claim what the closure record shows, and
    the claim is that record's own set rather than a retyped one."""
    block = milestone_block(roadmap_text(), declared_series())
    assert block is not None
    assert not _requirement_l_problems(block)


def test_the_requirement_l_check_catches_a_stale_or_wrong_claim():
    block = milestone_block(roadmap_text(), SERIES)
    bullet = l_bullet(block)
    assert bullet is not None, "fixture assumption: the bullet is there to mutate"

    def mutated(old: str, new: str) -> list[str]:
        assert old in bullet, f"fixture assumption: {old!r} is in the bullet"
        return _requirement_l_problems(block.replace(bullet, bullet.replace(old, new, 1)))

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
    assert not _requirement_l_problems(block)


# ------------------------------------------- what the wave delivered is not deferred


def test_the_deferred_list_is_unchanged_in_kind_by_this_wave():
    """v0.9 resolves neither standing deferred item (image publishing, PID
    namespaces) -- like v0.8, this wave has no deferred-list entry of its own to
    correct, so the only property to hold is that the section still exists and
    still names both."""
    section = deferred_section(roadmap_text())
    assert "PID-namespace" in section
    assert "Publishing the Docker image" in section
