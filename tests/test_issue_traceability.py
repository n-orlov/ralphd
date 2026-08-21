"""Keeps `artifacts/reports/issue-traceability.md` honest.

The file holds two waves. The v0.5 wave (#1-#11 and #13, task 058 of
`selfdev-v05-resilience`) was written under a PRD that *forbade* closing the
GitHub issues from inside the run; the v0.6 wave (#14-#22, task 046 of
`selfdev-v06-release`) is written under a PRD whose requirement I is the
opposite -- task 047 closes them through the GitHub REST API and records each
closure in `artifacts/reports/issue-closure.md`. So the old
`test_no_issue_was_closed_from_inside_the_run` guard is gone, and in its place
this module holds the report's per-issue `**Closure:**` lines against that
record: a section may claim a closure only when the record shows one.

A report that names a commit that never landed, or a test that no longer
exists, is worse than no report, so its claims are re-read too. That
extraction and those three checks live in `tests/report_claims.py` (task 041)
and `tests/test_report_claims.py` applies them to *every* report in the
directory; they are re-applied here to this file specifically, because this is
the report the issue closures are argued from. What is otherwise only ours:
the per-issue sections of both waves, the density of the evidence, and the
closure cross-check.
"""

from __future__ import annotations

import re

from report_claims import (
    REPORTS_DIR,
    commit_problems,
    node_problems,
    parse_claims,
    path_problems,
)

REPORT = REPORTS_DIR / "issue-traceability.md"
CLOSURE_REPORT = REPORTS_DIR / "issue-closure.md"

# Wave 1 (v0.5): closed by the operator from the report, not by the run.
V05_ISSUES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13]
# Wave 2 (v0.6): closed from inside the run by task 047 (requirement I).
V06_ISSUES = [14, 15, 16, 17, 18, 19, 20, 21, 22]
ISSUES_IN_SCOPE = V05_ISSUES + V06_ISSUES

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


def test_the_report_still_carries_the_evidence_it_promised() -> None:
    """Existence of every claim is checked below (and by
    `tests/test_report_claims.py` for every report); what is specific to this
    one is how much it claims -- a mapping of twenty-one issues thinned down to
    a handful of shas would pass the existence checks and be worthless."""
    claims = parse_claims(REPORT)
    shas = {c.value for c in claims.shas}
    paths = {c.value for c in claims.paths}
    assert len(shas) >= 80, f"suspiciously few commits listed: {sorted(shas)}"
    assert len(paths) >= 80, f"suspiciously few paths listed: {sorted(paths)}"
    assert len(claims.nodes) >= 180, (
        f"suspiciously few test node ids listed: {len(claims.nodes)}"
    )
    assert all(c.path for c in claims.nodes), "a node id with no path to attach to"


def test_every_commit_this_report_names_is_a_real_commit() -> None:
    """The closures are argued from these shas, so they are re-checked here and
    not only in the directory-wide parametrization."""
    problems = commit_problems(parse_claims(REPORT))
    assert not problems, "\n".join(problems)


def test_every_path_and_test_node_this_report_names_exists() -> None:
    claims = parse_claims(REPORT)
    problems = path_problems(claims) + node_problems(claims)
    assert not problems, "\n".join(problems)


def test_every_issue_section_states_its_closure() -> None:
    """Requirement I made closure part of the report: no section may be silent
    about whether the issue was closed, and it may only use the three words
    this module knows how to check."""
    text = _report_text()
    claims = _closure_claims(text)
    problems = []
    for issue in V06_ISSUES:
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
