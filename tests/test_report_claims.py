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
import shutil
from pathlib import Path

import pytest
import report_claims as report_claims_module
from report_claims import (
    REPO_ROOT,
    REPORTS_DIR,
    Claim,
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


def test_path_problems_takes_at_most_one_git_ls_files_snapshot(monkeypatch) -> None:
    """F: membership is checked against one `git ls-files` snapshot, not a
    subprocess call per claim -- even across many claims in one report."""
    monkeypatch.setattr(report_claims_module, "_TRACKED_PATHS", None)
    real_run = report_claims_module.subprocess.run
    calls = []

    def counting_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(report_claims_module.subprocess, "run", counting_run)
    many_claims = ReportClaims(
        report=REPORTS_DIR / "conformance-ledger.md",
        shas=[],
        paths=[Claim("tests/report_claims.py", line=i) for i in range(1, 21)],
        nodes=[],
        retired=[],
    )
    problems = path_problems(many_claims)
    assert not problems, problems
    assert len(calls) == 1, f"expected exactly one git ls-files call, got {len(calls)}"
    # A second, independent call must reuse the snapshot -- still one total.
    more_claims = ReportClaims(
        report=REPORTS_DIR / "conformance-ledger.md",
        shas=[],
        paths=[Claim("tests/test_report_claims.py", line=1)],
        nodes=[],
        retired=[],
    )
    assert not path_problems(more_claims)
    assert len(calls) == 1


def test_untracked_generated_path_fails_with_the_untracked_message() -> None:
    """F: `artifacts/screenshots/hub/` is a directory the browser tier
    creates but git never tracks -- v0.11's task-027 report cited it and
    passed only in the job's own workspace. `.exists()` must no longer be
    enough; the failure must say the path is generated, not committed."""
    generated = REPO_ROOT / "artifacts" / "screenshots" / "hub"
    assert "artifacts/screenshots/hub" not in report_claims_module._tracked_paths()
    generated.mkdir(parents=True, exist_ok=True)
    try:
        claims = ReportClaims(
            report=REPORTS_DIR / "conformance-ledger.md",
            shas=[],
            paths=[Claim("artifacts/screenshots/hub/", line=1)],
            nodes=[],
            retired=[],
        )
        problems = path_problems(claims)
        assert problems, "an untracked-but-present directory must be flagged"
        assert "generated, not committed" in problems[0]
    finally:
        shutil.rmtree(REPO_ROOT / "artifacts" / "screenshots", ignore_errors=True)


def test_the_same_absolute_run_dir_path_still_passes(tmp_path) -> None:
    """F: an absolute run-dir citation of the same generated directory is
    ignored by `PATH_RE` entirely, exactly as before -- it never becomes a
    path claim, so it never becomes a path problem either."""
    report = tmp_path / "fixture.md"
    report.write_text(
        "See `/run/ralphd/artifacts/screenshots/hub/` for the capture.\n"
    )
    claims = parse_claims(report)
    assert claims.paths == []
    assert not path_problems(claims)


def test_tracked_but_missing_path_fails_with_the_other_message() -> None:
    """F: a path that is neither tracked nor present on disk -- the shape of
    a citation to something deleted or renamed after the report was
    written -- gets the other message, distinguishable from the untracked
    one above."""
    claims = ReportClaims(
        report=REPORTS_DIR / "conformance-ledger.md",
        shas=[],
        paths=[
            Claim(
                "tests/this_module_was_deleted_or_renamed_after_the_report.py",
                line=1,
            )
        ],
        nodes=[],
        retired=[],
    )
    problems = path_problems(claims)
    assert problems, "a gone-entirely path must be flagged"
    assert "deleted or renamed" in problems[0]
    assert "generated, not committed" not in problems[0]


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
