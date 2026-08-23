"""Task 033 (v0.7 requirement R): `docs/roadmap.md`'s v0.7 block and the bump.

Retargeted by task 032 (v0.8 requirement O), the same way task 033 itself
retargeted `tests/test_roadmap_v06.py` when this module was new: v0.7 is no
longer the current release, so the "declared version has a block" half moved
to `tests/test_roadmap_v08.py` and `SERIES` here is pinned to `(0, 7)` rather
than read from `pyproject.toml`. Everything else -- coverage of every v0.7
requirement letter and issue, the deferred-list checks, the requirement D/I/P/Q
code-derived checks -- stays: v0.7's block is history now, not deleted, and
still has to tell the truth about what shipped.

* a block may not claim a wave landed while omitting requirements of that wave,
  and the deferred list may not keep promising what the wave delivered. The
  requirement letters and issue numbers are parsed out of the PRD the block is
  about (`docs/prds/v0.7-trustworthy-unattended.md`), never retyped here --
  note `prd_requirements()`'s letter range is widened to `A-R`, since this PRD
  runs to R where v0.6's stopped at J.

Its code-derived halves, so the block cannot become prose nobody re-reads: the
verb requirement P advertises exists in `build_parser()`; requirement O's
literal really is flipped; requirement D's third fault class really exists; and
requirement I's uid boundary really is in the code, which is what makes the
old PID-namespace deferred entry wrong to leave standing. Requirement Q's
bullet is checked against the receipt task 038 wrote
(`artifacts/reports/issue-closure.md`): the issue numbers it claims closed and
the ones it names as left open are that record's own sets, parsed, never
retyped here -- so the roadmap cannot claim a closure GitHub does not have.

Every check is paired with a test that mutates the *real* roadmap text into the
wording it replaced (or a wrong version), so a check that stops discriminating
fails.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"
CLOSURE_REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-closure.md"
TRACEABILITY_REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-traceability.md"
CLOSURE_WAVE_HEADING = "# Issue closure record — ralphd v0.7"
PRD = REPO_ROOT / "docs" / "prds" / "v0.7-trustworthy-unattended.md"

DEFERRED_HEADING = "## Later / explicitly deferred"

# The milestone this module is about. Pinned, not read from `pyproject.toml`:
# v0.7 is history now (task 032 retargeted this the same way task 033 itself
# retargeted test_roadmap_v06.py's SERIES when v0.7 became current). The
# "declared version has a block" half lives in tests/test_roadmap_v08.py.
SERIES = (0, 7)

# The verb requirement P added to `ralphctl`. It must exist in the parser (the
# code-derived half) *and* be named in the block.
WAVE_VERBS = ("suggestions",)


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
    """`{"B": {"28"}, ...}` from the PRD's own `## B. title (#28)` headings.

    The letter range is `A-R`: v0.6's PRD ran to J, this one to R.
    """
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"^## ([A-R])\. (.+)$", PRD.read_text(), re.MULTILINE):
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


def test_the_version_block_check_catches_a_missing_or_unshipped_block():
    text, series = roadmap_text(), SERIES
    heading = f"## v{series[0]}.{series[1]}"
    assert heading in text, "fixture assumption: the block is there to remove"
    # the pre-033 roadmap: no block for the version pyproject declares
    assert _version_block_problems(text.replace(heading, "## Not a milestone"), series)
    # a block that only *plans* the release records nothing shipped
    block = milestone_block(text, series)
    assert _version_block_problems(text.replace(block, block.replace("✅", "⏳")), series)
    # a version the roadmap has already moved past (v0.6, with v0.7 after it)
    assert _version_block_problems(text, (series[0], series[1] - 1))


# --------------------------------------------------- the block covers the wave


def _coverage_problems(block: str) -> list[str]:
    problems = []
    for letter, issues in sorted(prd_requirements().items()):
        if not re.search(rf"\*\*{letter}\.", block):
            problems.append(f"requirement {letter} is not in the v0.7 block")
            continue
        for issue in sorted(issues):
            if f"#{issue}" not in block:
                problems.append(f"requirement {letter}'s issue #{issue} is not cited")
    return problems


def test_the_prd_headings_parse_at_all():
    """Guards the coverage check from passing because the PRD parse found
    nothing -- and pins the widened `A-R` range: with v0.6's `[A-J]` regex,
    requirements K through R would silently not be checked at all."""
    reqs = prd_requirements()
    assert set(reqs) == set("ABCDEFGHIJKLMNOPQR"), sorted(reqs)
    assert reqs["B"] == {"28"} and reqs["F"] == {"47", "43"} and reqs["M"] == {"33"}
    # O, P, Q and R are the letters with no issue number of their own.
    assert {k for k, v in reqs.items() if not v} == {"O", "P", "Q", "R"}


def test_every_prd_requirement_is_accounted_for_in_the_v07_block():
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert not _coverage_problems(block)


def test_the_coverage_check_catches_a_dropped_requirement_or_issue():
    block = milestone_block(roadmap_text(), SERIES)
    assert _coverage_problems(block.replace("**M.", "M."))
    assert _coverage_problems(block.replace("#44", "the transcript issue"))


def test_the_block_cites_every_issue_this_wave_names_not_only_the_range_ends():
    """The PRD writes requirement A's issues as the range `#35-#42`, so the
    parse above only demands #35 and #42. The block names all eight, which is
    what makes it usable as an issue -> requirement index."""
    block = milestone_block(roadmap_text(), SERIES)
    missing = [n for n in range(35, 43) if f"#{n}" not in block]
    assert not missing, f"the v0.7 block does not cite {missing}"


def test_the_v07_block_names_the_verb_the_wave_added():
    from ralphd.cli.main import build_parser

    verbs = {a.dest: a for a in build_parser()._subparsers._group_actions}
    choices = set(next(iter(verbs.values())).choices)
    block = milestone_block(roadmap_text(), SERIES)
    for verb in WAVE_VERBS:
        assert verb in choices, f"WAVE_VERBS names {verb!r}, which the parser lacks"
        assert f"`ralphctl {verb}" in block, (
            f"the v0.7 block claims requirement P shipped but never names "
            f"`ralphctl {verb}`")


# ------------------------------------------- what the wave delivered is not deferred


def _deferred_pid_namespace_problems(text: str) -> list[str]:
    """The PID-namespace entry may no longer be the answer to the kill hazard.

    Requirement I closed that hazard with the uid boundary; an entry still
    saying engine-level self-protection is *needed* is a promise for work that
    landed, which is how the roadmap rots (the same defect v0.6's requirement J
    caught in the image entry).
    """
    problems = []
    paras = [p for p in re.split(r"\n(?=- )", deferred_section(text))
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


def test_the_code_really_has_the_uid_boundary_so_deferring_it_would_be_wrong():
    """The premise of the check below, read off the code rather than the prose:
    the engine can separate its own identity from the agent's."""
    from ralphd.engine import privsep

    assert hasattr(privsep, "separate_engine_identity")
    assert hasattr(privsep, "agent_child_kwargs")
    assert privsep.DEFAULT_AGENT_UID == 1000


def test_the_deferred_list_no_longer_promises_the_kill_protection_i_shipped():
    assert not _deferred_pid_namespace_problems(roadmap_text())


def test_the_deferred_pid_namespace_check_catches_the_pre_v07_wording():
    text = roadmap_text()
    section = deferred_section(text)
    pre_v07 = (
        "\n- PID-namespace isolation of agent iterations from in-container kill "
        "signals\n  (a supervisor-level SIGKILL/SIGTERM currently reaches the "
        "running `pi`\n  subprocess directly) — not attempted; rationale: prompt "
        "rules alone did\n  not prevent the iteration-103 pkill incident, so "
        "engine-level\n  self-protection is needed.\n")
    stripped = re.sub(r"\n- PID-namespace isolation[\s\S]*?(?=\n- )", "\n", section)
    assert stripped != section, "fixture assumption: the entry is there to replace"
    # the v0.6 wording, still promising what requirement I delivered
    assert _deferred_pid_namespace_problems(
        text.replace(section, stripped + pre_v07))
    # ...and dropping the entry entirely is caught too
    assert _deferred_pid_namespace_problems(text.replace(section, stripped))


def test_the_auto_resume_flip_is_shipped_not_deferred():
    """Requirement O's half of the same rule, from the literal rather than the
    prose. `test_cli_auto_resume.py` owns the v0.5 note's wording; this pins
    that the v0.7 block itself records the flip."""
    from ralphd.cli.main import AUTO_RESUME_DEFAULT

    assert AUTO_RESUME_DEFAULT is True
    assert "auto_resume" not in deferred_section(roadmap_text())
    block = milestone_block(roadmap_text(), SERIES)
    assert "AUTO_RESUME_DEFAULT" in block and "--no-auto-resume" in block


# ------------------------------------------- requirement Q matches the receipts


def closure_wave_section() -> str:
    """`artifacts/reports/issue-closure.md`'s v0.7 half (it now holds three
    waves, so this stops at the next `# Issue closure record` heading rather
    than running to end of file -- otherwise a later wave's own section, e.g.
    v0.8's, would silently be scanned as if it were v0.7's)."""
    text = CLOSURE_REPORT.read_text()
    assert CLOSURE_WAVE_HEADING in text, (
        f"{CLOSURE_REPORT.name} has no {CLOSURE_WAVE_HEADING!r} section")
    rest = text.split(CLOSURE_WAVE_HEADING, 1)[1]
    nxt = re.search(r"^# Issue closure record", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def closed_issues() -> set[int]:
    """The issues the closure record says it actually closed, per-section.

    Derived from the report rather than retyped here: the roadmap bullet's
    claim is only worth anything if it is the same set the receipt records.
    A section counts as a closure only where it states the state GitHub
    answered on the re-read (`**closed**`), which is what task 038 wrote.
    """
    out = set()
    for m in re.finditer(r"^## #(\d+)\b([\s\S]*?)(?=^## |\Z)",
                         closure_wave_section(), re.MULTILINE):
        if "**closed**" in m.group(2):
            out.add(int(m.group(1)))
    return out


def left_open_issues() -> set[int]:
    """The issues the closure record names as deliberately left open."""
    section = closure_wave_section()
    assert "Deliberately **not** closed" in section, (
        f"{CLOSURE_REPORT.name}'s v0.7 section no longer names what it left open")
    para = section.split("Deliberately **not** closed", 1)[1].split("\n\n", 1)[0]
    return {int(n) for n in re.findall(r"#(\d+)", para)}


def _issue_numbers(text: str) -> set[int]:
    """`{28, 30, 31, ...}` from prose, expanding `#30–#49` style ranges."""
    out: set[int] = set()
    for lo, hi in re.findall(r"#(\d+)\s*[–—-]\s*#(\d+)", text):
        out.update(range(int(lo), int(hi) + 1))
    out.update(int(n) for n in re.findall(r"#(\d+)", text))
    return out


def q_bullet(block: str) -> str | None:
    bullets = [p for p in re.split(r"\n(?=- )", block) if "**Q." in p]
    return bullets[0] if len(bullets) == 1 else None


OPEN_MARKER = "Left open deliberately"


def _requirement_q_problems(block: str) -> list[str]:
    bullet = q_bullet(block)
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
    closed = closed_issues()
    if _issue_numbers(claimed) != closed:
        problems.append(
            f"the Q bullet claims {sorted(_issue_numbers(claimed))} closed, but "
            f"{CLOSURE_REPORT.name} records {sorted(closed)}")
    total = re.search(r"\b(\d+) in all\b", claimed)
    if not total:
        problems.append("the Q bullet states no closure total")
    elif int(total.group(1)) != len(closed):
        problems.append(f"the Q bullet says {total.group(1)} closures, the "
                        f"record has {len(closed)}")
    if _issue_numbers(open_part) != left_open_issues():
        problems.append(
            f"the Q bullet names {sorted(_issue_numbers(open_part))} as left "
            f"open, the record names {sorted(left_open_issues())}")
    return problems


def test_the_closure_record_parses_at_all():
    """Guards the checks below from passing on an empty parse -- and pins what
    task 038 actually did: 24 closures, seven issues left open on purpose."""
    closed, left_open = closed_issues(), left_open_issues()
    assert closed == {28, *range(30, 50), 52, 53, 55}, sorted(closed)
    assert len(closed) == 24
    assert left_open == {24, 25, 26, 27, 50, 51, 54}, sorted(left_open)
    assert not closed & left_open


def test_requirement_q_is_shipped_and_says_what_the_receipts_say():
    """Task 042: the bullet flips to shipped only once the issues are closed,
    and its claim is the closure record's own set, not a retyped one."""
    block = milestone_block(roadmap_text(), SERIES)
    assert block is not None
    assert not _requirement_q_problems(block)


def test_the_requirement_q_check_catches_a_stale_or_wrong_claim():
    block = milestone_block(roadmap_text(), SERIES)
    bullet = q_bullet(block)
    assert bullet is not None, "fixture assumption: the bullet is there to mutate"

    def mutated(old: str, new: str) -> list[str]:
        assert old in bullet, f"fixture assumption: {old!r} is in the bullet"
        return _requirement_q_problems(block.replace(bullet, bullet.replace(old, new, 1)))

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
    assert not _requirement_q_problems(block)


def test_the_block_names_the_fault_class_requirement_d_added():
    """Requirement D's claim, read off `faults.py`'s own vocabulary."""
    from ralphd.engine.faults import FAULT_CLASS_SIGNAL, FAULT_CLASSES

    assert FAULT_CLASS_SIGNAL in FAULT_CLASSES
    block = milestone_block(roadmap_text(), SERIES)
    assert f"`{FAULT_CLASS_SIGNAL}`" in block, (
        "the v0.7 block claims requirement D shipped but never names the "
        f"third fault class ({FAULT_CLASS_SIGNAL!r})")
