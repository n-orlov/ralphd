"""Keeps `artifacts/reports/issue-traceability.md` (task 058) honest.

The PRD of the v0.5 wave forbade closing the GitHub issues from inside the run:
the operator closes #1-#11 and #13 from that report instead. A report that
names a commit that never landed, or a test that no longer exists, is worse
than no report, so its claims are re-read and asserted:

* every issue in scope has a section,
* every 7-hex commit sha it lists exists, every `tests/...` path it lists
  exists, and every `::node_id` after such a path is a real test,
* no `gh issue close` anywhere in the tree (the report claims this).

The middle bullet is no longer implemented here: task 041 moved the extraction
and the three checks into `tests/report_claims.py`, where
`tests/test_report_claims.py` applies them to *every* report in the directory
(this one included). What stays here is what only this report can assert --
its issue sections, the density of its evidence, and the closing-command
guard.
"""

from __future__ import annotations

import re
import subprocess

from report_claims import REPO_ROOT, REPORTS_DIR, parse_claims

REPORT = REPORTS_DIR / "issue-traceability.md"

ISSUES_IN_SCOPE = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13]


def _report_text() -> str:
    assert REPORT.exists(), f"{REPORT} is missing"
    return REPORT.read_text()


def test_report_covers_every_issue_in_scope() -> None:
    text = _report_text()
    for issue in ISSUES_IN_SCOPE:
        assert re.search(rf"^## #{issue}\b", text, re.MULTILINE), f"no section for #{issue}"


def test_the_report_still_carries_the_evidence_it_promised() -> None:
    """Existence of every claim is checked by `tests/test_report_claims.py`
    for every report; what is specific to this one is how much it claims -- a
    mapping of twelve issues thinned down to a handful of shas would pass the
    generic checks and be worthless."""
    claims = parse_claims(REPORT)
    shas = {c.value for c in claims.shas}
    paths = {c.value for c in claims.paths}
    assert len(shas) >= 40, f"suspiciously few commits listed: {sorted(shas)}"
    assert len(paths) >= 40, f"suspiciously few paths listed: {sorted(paths)}"
    assert len(claims.nodes) >= 80, (
        f"suspiciously few test node ids listed: {len(claims.nodes)}"
    )
    assert all(c.path for c in claims.nodes), "a node id with no path to attach to"


def test_no_issue_was_closed_from_inside_the_run() -> None:
    # An actual invocation always names an issue number, which is what this
    # looks for -- the report and this module both *quote* the forbidden
    # command without a number, and must not trip the check.
    invocation = r"gh issue close +#?[0-9]"
    proc = subprocess.run(
        ["git", "grep", "-rIn", "-E", invocation],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "", f"found an issue-closing command: {proc.stdout}"
    log = subprocess.run(
        ["git", "log", "--format=%B"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not re.search(invocation, log.stdout), "history closes a GitHub issue"
