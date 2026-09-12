"""Keeps `artifacts/reports/issue-traceability.md` honest.

The file holds three waves. The v0.5 wave (#1-#11 and #13, task 058 of
`selfdev-v05-resilience`) was written under a PRD that *forbade* closing the
GitHub issues from inside the run; the v0.6 wave (#14-#22, task 046 of
`selfdev-v06-release`) is written under a PRD whose requirement I is the
opposite -- task 047 closes them through the GitHub REST API and records each
closure in `artifacts/reports/issue-closure.md`. So the old
`test_no_issue_was_closed_from_inside_the_run` guard is gone, and in its place
this module holds the report's per-issue `**Closure:**` lines against that
record: a section may claim a closure only when the record shows one.

The v0.7 wave (#28 and #30-#49 excluding the closed #29, plus the mid-run
amendments #52, #53 and #55; task 037 of `selfdev-v07-unattended`) is the same
contract again, with two additions this module checks: its `**Closure:**` lines
start out `pending` because the map is written *before* task 038 closes
anything, and its requirements N, O and P closed SPEC entries rather than
issues, so they are traced to SPEC sections instead of to a `## #N` heading.

The v0.8 wave (#24, #26, #27, #50, #51, #54, #56, #57, #58; task 033 of
`selfdev-v08-auditable`) is the same contract again: its `**Closure:**` lines
also start out `pending`, written before task 035 closes anything, and its
requirement letters run A-H (the PRD does not use I-L).

A report that names a commit that never landed, or a file that has been
renamed away, is worse than no report, so its claims are re-read too. That
extraction and those two checks live in `tests/report_claims.py` (task 041,
narrowed from three to two by task 046: a cited test node id is no longer
resolved, ruling `001-039.md` point 3) and `tests/records/test_report_claims.py`
applies them to *every* report in the directory; they are re-applied here to
this file specifically, because this is the report the issue closures are
argued from. What is otherwise only ours: the per-issue sections of all
three waves, the density of the evidence, and the closure cross-check.
"""

from __future__ import annotations

import re

from report_claims import (
    REPORTS_DIR,
    commit_problems,
    parse_claims,
    path_problems,
    retired_problems,
)

REPORT = REPORTS_DIR / "issue-traceability.md"
CLOSURE_REPORT = REPORTS_DIR / "issue-closure.md"

# Wave 1 (v0.5): closed by the operator from the report, not by the run.
V05_ISSUES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13]
# Wave 2 (v0.6): closed from inside the run by task 047 (requirement I).
V06_ISSUES = [14, 15, 16, 17, 18, 19, 20, 21, 22]
# Wave 3 (v0.7): the 21 issues the PRD owns -- #28 and #30-#49, #29 having been
# closed before the wave started -- plus the three amendments that arrived
# mid-run by operator steering. Mapped by task 037, closed by task 038.
V07_ISSUES = [28] + [n for n in range(30, 50) if n != 29] + [52, 53, 55]
# v0.7 requirements that close a SPEC entry instead of an issue, so their
# traceability is recorded against SPEC sections and they carry no `## #N`
# heading: letter -> the SPEC section its own section must name.
V07_SPEC_REQUIREMENTS = {"N": "4.4", "O": "15", "P": "16"}
# Wave 4 (v0.8): the 9 issues the PRD owns for closure from inside the run.
# Mapped by task 033, closed (or left deliberately open) by task 035.
V08_ISSUES = [24, 26, 27, 50, 51, 54, 56, 57, 58]
ISSUES_IN_SCOPE = V05_ISSUES + V06_ISSUES + V07_ISSUES + V08_ISSUES

# What a `**Closure:**` line may claim about one issue.
CLOSURE_CLAIMED = "closed"
CLOSURE_STATES = {CLOSURE_CLAIMED, "pending", "open"}


def _report_text() -> str:
    assert REPORT.exists(), f"{REPORT} is missing"
    return REPORT.read_text()


def _sections(text: str) -> dict[int, str]:
    """Issue number -> the text of its `## #N` section (up to the next
    heading of the same or a higher level)."""
    found: dict[int, str] = {}
    heads = list(re.finditer(r"^(#{1,2}) #(\d+)\b(.*)$", text, re.MULTILINE))
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        # A later `# ` (wave) heading also ends the section.
        nxt = re.search(r"^# ", text[head.end():end], re.MULTILINE)
        stop = head.end() + nxt.start() if nxt else end
        found[int(head.group(2))] = text[head.start():stop]
    return found


def _closure_claims(text: str) -> dict[int, str]:
    """Issue number -> the state its section's `**Closure:**` line claims."""
    claims: dict[int, str] = {}
    for issue, section in _sections(text).items():
        hit = re.search(r"^\*\*Closure:\*\*\s+(\w+)", section, re.MULTILINE)
        if hit:
            claims[issue] = hit.group(1).lower()
    return claims


def test_report_covers_every_issue_in_scope() -> None:
    text = _report_text()
    for issue in ISSUES_IN_SCOPE:
        assert re.search(rf"^## #{issue}\b", text, re.MULTILINE), f"no section for #{issue}"


def test_the_v06_wave_maps_every_issue_to_a_requirement_and_a_task() -> None:
    """#14-#22 are the wave this report is the closing argument for: each of
    them has to carry the whole chain (issue -> requirement letter -> task
    number -> commit -> tests), not just a heading."""
    sections = _sections(_report_text())
    problems = []
    for issue in V06_ISSUES:
        section = sections.get(issue, "")
        if not re.search(r"requirement [A-J]", section):
            problems.append(f"#{issue}: no PRD requirement letter")
        if not re.search(r"^\| 0\d\d\b", section, re.MULTILINE):
            problems.append(f"#{issue}: no task row in its evidence table")
        if "tests/" not in section:
            problems.append(f"#{issue}: names no test")
    assert not problems, "\n".join(problems)


def test_the_v07_wave_maps_every_issue_to_a_requirement_and_a_task() -> None:
    """Same chain as v0.6, one wave on: #28, #30-#49 and the three steering
    amendments each carry issue -> requirement letter -> task number -> commit
    -> tests. The letters run A-U, because the amendments that arrived mid-run
    were lettered by the roadmap rather than by the PRD."""
    sections = _sections(_report_text())
    problems = []
    for issue in V07_ISSUES:
        section = sections.get(issue, "")
        if not re.search(r"requirement [A-U]\d?\b", section):
            problems.append(f"#{issue}: no requirement letter")
        if not re.search(r"^\| 0\d\d\b", section, re.MULTILINE):
            problems.append(f"#{issue}: no task row in its evidence table")
        if "tests/" not in section:
            problems.append(f"#{issue}: names no test")
    assert not problems, "\n".join(problems)


def test_the_v08_wave_maps_every_issue_to_a_requirement_and_a_task() -> None:
    """Same chain again, one wave on: #24, #26, #27, #50, #51, #54, #56, #57
    and #58 each carry issue -> requirement letter -> task number -> commit ->
    tests. The letters run A-H, the PRD's own (it does not use I-L)."""
    sections = _sections(_report_text())
    problems = []
    for issue in V08_ISSUES:
        section = sections.get(issue, "")
        if not re.search(r"requirement [A-H]\b", section):
            problems.append(f"#{issue}: no requirement letter")
        if not re.search(r"^\| 0\d\d\b", section, re.MULTILINE):
            problems.append(f"#{issue}: no task row in its evidence table")
        if "tests/" not in section:
            problems.append(f"#{issue}: names no test")
    assert not problems, "\n".join(problems)


def test_the_v07_requirements_with_no_issue_are_traced_to_spec() -> None:
    """Requirements N, O and P close SPEC entries rather than GitHub issues, and
    the PRD asks for their traceability against those sections instead. A
    section that named only the tasks would leave the claim unanchored."""
    text = _report_text()
    problems = []
    for letter, spec_section in V07_SPEC_REQUIREMENTS.items():
        head = re.search(rf"^## Requirement {letter} \u2014 .*$", text, re.MULTILINE)
        if head is None:
            problems.append(f"requirement {letter}: no `## Requirement {letter}` section")
            continue
        nxt = re.search(r"^#{1,2} ", text[head.end():], re.MULTILINE)
        body = text[head.start():head.end() + (nxt.start() if nxt else 0)]
        if f"\u00a7{spec_section}" not in body:
            problems.append(
                f"requirement {letter}: its section does not name SPEC "
                f"\u00a7{spec_section}"
            )
        if not re.search(r"^\| 0\d\d\b", body, re.MULTILINE):
            problems.append(f"requirement {letter}: no task row in its evidence table")
        if "tests/" not in body:
            problems.append(f"requirement {letter}: names no test")
    assert not problems, "\n".join(problems)


def test_the_report_still_carries_the_evidence_it_promised() -> None:
    """Existence of every claim is checked below (and by
    `tests/records/test_report_claims.py` for every report); what is specific to this
    one is how much it claims -- a mapping of twenty-one issues thinned down to
    a handful of shas would pass the existence checks and be worthless."""
    claims = parse_claims(REPORT)
    shas = {c.value for c in claims.shas}
    paths = {c.value for c in claims.paths}
    assert len(shas) >= 140, f"suspiciously few commits listed: {sorted(shas)}"
    assert len(paths) >= 150, f"suspiciously few paths listed: {sorted(paths)}"
    assert len(claims.nodes) >= 500, (
        f"suspiciously few test node ids listed: {len(claims.nodes)}"
    )
    assert all(c.path for c in claims.nodes), "a node id with no path to attach to"


def test_every_commit_this_report_names_is_a_real_commit() -> None:
    """The closures are argued from these shas, so they are re-checked here and
    not only in the directory-wide parametrization."""
    problems = commit_problems(parse_claims(REPORT))
    assert not problems, "\n".join(problems)


def test_every_path_this_report_names_exists() -> None:
    """Task 046 (ruling `001-039.md` point 3): the node-existence half of
    this check is retired -- a cited `::node` is no longer resolved against
    the file it names, here any more than in the directory-wide
    parametrization. Only the path half (and the path-only `!retired`
    strong direction) survives."""
    claims = parse_claims(REPORT)
    problems = path_problems(claims) + retired_problems(claims)
    assert not problems, "\n".join(problems)


def test_every_issue_section_states_its_closure() -> None:
    """Requirement I made closure part of the report: no section may be silent
    about whether the issue was closed, and it may only use the three words
    this module knows how to check."""
    text = _report_text()
    claims = _closure_claims(text)
    problems = []
    for issue in V06_ISSUES + V07_ISSUES + V08_ISSUES:
        state = claims.get(issue)
        if state is None:
            problems.append(f"#{issue}: no **Closure:** line")
        elif state not in CLOSURE_STATES:
            problems.append(f"#{issue}: unknown closure state {state!r}")
    assert not problems, "\n".join(problems)


def test_a_claimed_closure_is_recorded_in_the_closure_report() -> None:
    """`closed` is a claim about GitHub, so it needs the receipt task 047
    writes: a `## #N` section in `issue-closure.md` that says the issue ended
    up closed. An issue this report leaves `pending`/`open` needs no record --
    and must not have a closed one, which would mean the two reports disagree.
    """
    claims = _closure_claims(_report_text())
    claimed = sorted(issue for issue, state in claims.items()
                     if state == CLOSURE_CLAIMED)
    if not claimed:
        return
    assert CLOSURE_REPORT.exists(), (
        f"{REPORT.name} claims closures for {claimed} but "
        f"{CLOSURE_REPORT.name} does not exist"
    )
    recorded = _sections(CLOSURE_REPORT.read_text())
    problems = []
    for issue in claimed:
        section = recorded.get(issue)
        if section is None:
            problems.append(f"#{issue}: claimed closed, no section in "
                            f"{CLOSURE_REPORT.name}")
        elif CLOSURE_CLAIMED not in section.lower():
            problems.append(f"#{issue}: {CLOSURE_REPORT.name} records no "
                            "closed state for it")
    assert not problems, "\n".join(problems)
