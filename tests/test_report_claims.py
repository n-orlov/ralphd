"""Task 041 (#22): re-read *every* evidence report under `artifacts/reports/`.

`tests/test_issue_traceability.py` (task 058) proved that a report's claims can
be machine-checked; it only ever checked its own file, so `traceability.md` and
`v0.5-definition-of-done.md` -- which name just as many paths and test node ids
-- were free to rot, and one of them had (`fault_classifier.py` was renamed to
`faults.py` in v0.5 and the report never noticed).

The checks are not copied here: they live in `tests/report_claims.py` and this
module applies them to whatever the reports directory holds, so a report added
later is covered without touching this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from report_claims import (
    REPO_ROOT,
    REPORTS_DIR,
    ReportClaims,
    RetiredClaim,
    commit_problems,
    defines_test,
    node_problems,
    parse_claims,
    path_problems,
    report_files,
    retired_problems,
)

REPORTS = report_files()


def _ids(reports: list[Path]) -> list[str]:
    return [r.name for r in reports]


@pytest.fixture(params=REPORTS, ids=_ids(REPORTS))
def claims(request):
    return parse_claims(request.param)


def test_the_reports_directory_is_discovered_not_enumerated() -> None:
    assert REPORTS_DIR.is_dir(), f"{REPORTS_DIR} is missing"
    names = set(_ids(REPORTS))
    # The two reports task 041 exists for, plus the one that was already
    # checked: naming them keeps a rename or a deletion visible.
    assert {
        "issue-traceability.md",
        "traceability.md",
        "v0.5-definition-of-done.md",
    } <= names, names
    # Every report in the directory is parametrized -- no filter, no allowlist.
    assert names == {p.name for p in REPORTS_DIR.glob("*.md")}


def test_every_listed_commit_sha_exists(claims) -> None:
    problems = commit_problems(claims)
    assert not problems, "\n".join(problems)


def test_every_listed_path_exists(claims) -> None:
    problems = path_problems(claims)
    assert not problems, "\n".join(problems)


def test_every_listed_test_node_exists(claims) -> None:
    problems = node_problems(claims)
    assert not problems, "\n".join(problems)


def test_every_retired_claim_stays_retired(claims) -> None:
    """The STRONG direction: a `!retired` path must not exist, a `!retired`
    node must not collect."""
    problems = retired_problems(claims)
    assert not problems, "\n".join(problems)


def test_a_live_path_wrongly_marked_retired_is_caught() -> None:
    """Mutation case: `retired_problems` must fail loudly when a `!retired`
    citation names something that is, in fact, still there -- the marker
    would otherwise be a silent way to switch off a real check."""
    live_path = "tests/report_claims.py"
    assert (REPO_ROOT / live_path).exists()
    claims = ReportClaims(
        report=REPORTS_DIR / "conformance-ledger.md",
        shas=[],
        paths=[],
        nodes=[],
        retired=[RetiredClaim(live_path, None, line=1)],
    )
    problems = retired_problems(claims)
    assert problems, "a still-existing path marked !retired must be flagged"
    assert live_path in problems[0]


def test_a_live_node_wrongly_marked_retired_is_caught() -> None:
    """Same mutation, for the node form: a test that still collects must not
    be silently accepted just because a report calls it retired."""
    live_path = "tests/test_report_claims.py"
    live_node = "test_a_node_family_wildcard_needs_a_real_match"
    assert defines_test((REPO_ROOT / live_path).read_text(), live_node)
    claims = ReportClaims(
        report=REPORTS_DIR / "conformance-ledger.md",
        shas=[],
        paths=[],
        nodes=[],
        retired=[RetiredClaim(live_path, live_node, line=1)],
    )
    problems = retired_problems(claims)
    assert problems, "a still-collecting node marked !retired must be flagged"
    assert live_node in problems[0]


def test_the_reports_are_not_all_claimless(claims) -> None:
    """A report is evidence: it points at something checkable. This also stops
    a report from passing the three checks above by naming nothing at all."""
    total = len(claims.shas) + len(claims.paths) + len(claims.nodes)
    assert total >= 5, f"{claims.name} makes almost no checkable claim ({total})"


def test_the_claim_checks_have_exactly_one_implementation() -> None:
    """The point of this module: generalised, not copy-pasted. A sha regex or a
    `def <node>` probe anywhere else under tests/ means someone started a
    second implementation."""
    fingerprints = (r"[0-9a-f]{7,", r"(async )?def ")
    # The implementation, and this guard which has to quote it.
    home = {"report_claims.py", Path(__file__).name}
    offenders = []
    for path in sorted((REPO_ROOT / "tests").glob("*.py")):
        if path.name in home:
            continue
        text = path.read_text()
        for fingerprint in fingerprints:
            if fingerprint in text:
                offenders.append(f"{path.name}: {fingerprint!r}")
    assert not offenders, (
        "claim extraction belongs in tests/report_claims.py: " + ", ".join(offenders)
    )


def test_a_node_family_wildcard_needs_a_real_match() -> None:
    """`::test_retry_proxy_*` (the v0.5 report's way of naming four tests at
    once) is a prefix, not a licence: it still has to match something."""
    source = "def test_retry_proxy_forwards_the_token():\n    pass\n"
    assert defines_test(source, "test_retry_proxy_*")
    assert not defines_test(source, "test_retry_nope_*")
    assert not defines_test(source, "test_retry_proxy_forwards_the_token_and_more")


def test_the_v05_report_still_names_its_own_module() -> None:
    """The report task 041 was told to keep honest carries its own module's
    node ids; if that module is ever renamed, both must move together."""
    text = (REPORTS_DIR / "v0.5-definition-of-done.md").read_text()
    assert "tests/test_v05_definition_of_done.py" in text
    assert (REPO_ROOT / "tests" / "test_v05_definition_of_done.py").exists()
    # ... and its own checks are a separate module, untouched by this one.
    assert re.search(r"^def test_", (REPO_ROOT / "tests"
                                    / "test_v05_definition_of_done.py").read_text(),
                     re.MULTILINE)
