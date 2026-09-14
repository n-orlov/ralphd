"""Requirement N.2 (task 060), second half: `tests/report_claims.py` and its
one remaining caller, `tests/test_issue_traceability.py`, go entire.

Task 059 already retired the corpus-wide half of this machinery
(`tests/records/test_report_claims.py`, `tests/test_report_claims_git_objects.py`,
and `report_files()`'s glob-discovery inside `tests/report_claims.py`
itself), deliberately leaving `tests/report_claims.py`'s single-report parser
(`parse_claims`/`commit_problems`/`path_problems`/`retired_problems`) alive
because `tests/test_issue_traceability.py` still called it directly against
one still-tracked report, `artifacts/reports/issue-traceability.md`. This
task removes that caller too -- its whole design was reading that report
(and `issue-closure.md`) live at test time, and requirement N.1 (task 061)
deletes both files from the tree outright, so a check whose only subject is
about to be deleted is retired ahead of the deletion, the same reasoning
task 059 already applied one layer up. With `tests/test_issue_traceability.py`
gone, `tests/report_claims.py` has no caller anywhere in the tree either, so
it is deleted too rather than left as dead code that still names
`artifacts/reports/` in its own `REPORTS_DIR` constant.

This module is the named test the retirement asks for -- the same shape as
`tests/test_sweep_guard_retired.py` (requirement A.1) for the sweep-sha
guard's own retirement: it proves the two files are actually gone, and that
nothing in the tree still reaches for either one by name, so a later commit
cannot quietly resurrect the coupling this task cuts.

`docs/test-suite-audit.md`'s Subsumed table repoints its seven
`tests/report_claims.py`-lineage rows here: the sha-resolution,
path-resolution, node-resolution (already retired, A.3 v0.20.2),
`!retired`-direction and claim-density checks that module and
`tests/test_issue_traceability.py` used to perform have no live successor at
all now, because the reports they checked are gone -- there is nothing left
to check, not merely a different place that checks it. Naming this module's
nodes there is the same "no live successor, so cite the node that records
the retirement instead of one that performs the check" move that table
already used for the node-resolution row.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

RETIRED_PATHS = (
    TESTS_DIR / "report_claims.py",
    TESTS_DIR / "test_issue_traceability.py",
)

_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+report_claims\b", re.MULTILINE)


def test_report_claims_and_issue_traceability_files_are_gone() -> None:
    for path in RETIRED_PATHS:
        assert not path.exists(), (
            f"{path.relative_to(REPO_ROOT)} still exists -- requirement N.2 "
            "(task 060) retires it entire"
        )


def test_no_test_module_imports_report_claims_by_name() -> None:
    """The bare `import report_claims`/`from report_claims import ...` style
    `tests/records/conftest.py` put `tests/` on `sys.path` for. Every `.py`
    file under `tests/` (this file included) is scanned; the deleted caller
    itself is gone, so a hit here means a NEW coupling was added, not the
    one this task removed."""
    offenders = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if _IMPORT_RE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"still importing the retired report_claims module: {offenders}"
    )
