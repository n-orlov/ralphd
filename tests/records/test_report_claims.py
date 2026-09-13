"""Task 032 (requirement E.1): `tests/test_report_claims.py` collapsed into
`tests/records/`.

The original module parametrised five of its checks over every report under
`artifacts/reports/` (`params=REPORTS`), so its own node count grew with the
number of reports on disk -- 231 reports times five families was 1,155 of
this module's 1,165 collected tests. That is the exact shape the PRD's
records family wants gone: "iterates the reports it discovers by glob"
inside a FIXED number of tests, not one pytest node per report per check.

`REPORTS = report_files()` below is still glob-discovered -- a report added
tomorrow is covered without editing this file -- but the five per-report
checks (commit/path/node/retired resolution, plus the "not claimless"
density floor) now run inside ONE test's own `for` loop over that list,
collecting every problem across every report before a single `assert`. The
two `!retired`-direction mutation cases are merged into one test the same
way. Nothing else about `tests/report_claims.py` (the shared parser and
checks) changes -- only how many pytest nodes apply it.

Requirement A.3 (v0.20.2) later retires the node-resolution check entire
(`node_problems()` in `tests/report_claims.py` is gone): a report is written
about the tree as it stood at the time, and the live tree renaming or
deleting a test later does not make that report's citation a lie, so the
loop below runs the remaining three (commit/path/retired resolution) plus
the density floor -- `path::node` claims are still parsed and still count
toward that density floor, they are simply no longer required to resolve.
A cure of the same requirement (cure-01-01) retires the NODE half of the
`!retired` STRONG direction too -- `retired_problems()` no longer checks a
`path::node!retired` citation against live test source either, because the
live tree reusing an old name for an unrelated test made a historical
report's untouched citation fail the very recheck A.3 already rejects for
the un-retired form; only the PATH form of `!retired` (a whole file must
not still be tracked) stays checked. The anti-fabrication check this module
keeps untouched is the sha reachability one (`commit_problems()`), covered
by its own two named tests below.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import report_claims as report_claims_module
from report_claims import (
    FROZEN_REPORTS,
    REPO_ROOT,
    REPORTS_DIR,
    Claim,
    ReportClaims,
    RetiredClaim,
    commit_problems,
    defines_test,
    parse_claims,
    path_problems,
    report_files,
    retired_problems,
)

REPORTS = report_files()


def test_the_reports_directory_is_discovered_not_enumerated() -> None:
    assert REPORTS_DIR.is_dir(), f"{REPORTS_DIR} is missing"
    names = {r.name for r in REPORTS}
    # The two reports task 041 exists for, plus the one that was already
    # checked: naming them keeps a rename or a deletion visible.
    assert {
        "issue-traceability.md",
        "traceability.md",
        "v0.5-definition-of-done.md",
    } <= names, names
    # Every LIVE report in the directory is covered -- no filter, no
    # allowlist. The only names subtracted are the ones frozen as history
    # (requirement A.2, v0.20.2), which the checker skips entirely: nothing
    # here reads a frozen report's presence, its lines or its claims.
    on_disk = {p.name for p in REPORTS_DIR.glob("*.md")}
    assert names == on_disk - set(FROZEN_REPORTS)
    assert FROZEN_REPORTS, "the frozen-report list is the only subtraction"
    assert not (set(FROZEN_REPORTS) & names), FROZEN_REPORTS


def test_every_report_passes_its_claim_checks() -> None:
    """The remaining three checks run once per report inside this loop --
    still every live report (discovered fresh by `report_files()`, imported
    at module scope above), never a filtered subset beyond `FROZEN_REPORTS`
    (requirement A.2, v0.20.2), and a fixed number of pytest nodes regardless
    of how many reports the directory holds. A fourth check
    (`node_problems()`, requiring a `path::node` citation to still resolve
    to a real test) used to run here too; requirement A.3 (v0.20.2) retires
    it entire -- a historical report's citation of a test the live tree has
    since renamed or deleted is not thereby a lie, so nothing here requires
    a `path::node` claim to resolve any more. The sha reachability check
    (`commit_problems()`, the anti-fabrication guard A.3 explicitly keeps) is
    unaffected."""
    reports = report_files()
    assert reports, "no reports discovered under artifacts/reports/"
    problems: list[str] = []
    for report in reports:
        claims = parse_claims(report)
        problems.extend(commit_problems(claims))
        problems.extend(path_problems(claims))
        problems.extend(retired_problems(claims))
        # "not all claimless": a report is evidence, it must point at
        # something checkable, and this also stops a report from passing
        # the checks above by naming nothing at all. A `!retired` path
        # citation counts too (requirement A.1, v0.20.2): it is still
        # checked, in the strong direction (retired_problems requires the
        # PATH form NOT resolve) -- a report that names several now-retired
        # sweep-sha citations, after A.1 deleted the files they pointed at,
        # is not thereby claimless. A `path::node` claim, retired or not,
        # also still counts toward this floor even though nothing checks it
        # resolves any more (requirement A.3, v0.20.2, and its cure
        # cure-01-01 for the `!retired` node form specifically) -- it is
        # still evidence of what the report is about, just not evidence
        # this module re-verifies.
        total = (
            len(claims.shas) + len(claims.paths) + len(claims.nodes)
            + len(claims.retired)
        )
        if total < 5:
            problems.append(
                f"{report.name}: makes almost no checkable claim ({total})"
            )
    assert not problems, "\n".join(problems)


def test_the_retired_marker_direction_is_enforced() -> None:
    """The STRONG direction, PATH form only: a `!retired` path must not
    resolve -- it must not still be tracked. This used to also cover the
    NODE form (a still-collecting node marked `!retired`), merging the two
    mutation cases the old module ran as separate tests; a cure of
    requirement A.3 (cure-01-01) retires that node form entire, because the
    live tree reusing an old test's name for something unrelated tripped
    this same recheck against an untouched historical report -- exactly the
    live-source recheck A.3 already rejects for the un-retired node check.
    `test_a_retired_node_citation_is_never_rechecked_against_live_source`
    below now covers that retirement directly."""
    live_path = "tests/report_claims.py"
    assert (REPO_ROOT / live_path).exists()
    path_claims = ReportClaims(
        report=REPORTS_DIR / "synthetic-fixture.md",
        shas=[],
        paths=[],
        nodes=[],
        retired=[RetiredClaim(live_path, None, line=1)],
    )
    path_problems_found = retired_problems(path_claims)
    assert path_problems_found, "a still-existing path marked !retired must be flagged"
    assert live_path in path_problems_found[0]


def test_a_retired_node_citation_is_never_rechecked_against_live_source() -> None:
    """Requirement A.3's cure (cure-01-01): a `path::node!retired` citation
    is no longer checked against live test source at all -- neither the
    ordinary node-resolution question A.3 itself already retired, nor the
    STRONG `!retired` direction `test_the_retired_marker_direction_is_enforced`
    above still enforces for the path form. A node the live tree happens to
    define, under a name an old, unrelated historical report once retired,
    is not thereby a lie that report tells -- so this must NOT be flagged,
    unlike the still-existing-path case above."""
    live_node_path = "tests/records/test_report_claims.py"
    live_node = "test_a_node_family_wildcard_needs_a_real_match"
    assert defines_test((REPO_ROOT / live_node_path).read_text(), live_node)
    node_claims = ReportClaims(
        report=REPORTS_DIR / "synthetic-fixture.md",
        shas=[],
        paths=[],
        nodes=[],
        retired=[RetiredClaim(live_node_path, live_node, line=1)],
    )
    assert retired_problems(node_claims) == [], (
        "a still-collecting node marked !retired must no longer be flagged "
        "(requirement A.3, v0.20.2, cure-01-01)"
    )


def test_a_dangling_commit_sha_fails_and_a_reachable_one_passes(monkeypatch) -> None:
    """H.2 (#176): `commit_problems()` requires a cited sha to be reachable
    from `HEAD`, not merely present as an object -- a sha git still keeps on
    disk as the pre-image of an amended, reset or rebased-away commit is a
    claim problem, exactly like a sha that never resolved to a commit at
    all. The three memoised dicts/set are set directly (bypassing the real
    "every sha the reports cite" population, which never sees a sha this
    fabricated) the same way the whitebox path test above fakes
    `_TRACKED_PATHS` -- no subprocess spent either way."""
    dangling = "d" * 40
    reachable = "e" * 40
    monkeypatch.setattr(
        report_claims_module, "_COMMIT_KINDS",
        {dangling: "commit", reachable: "commit"},
    )
    monkeypatch.setattr(
        report_claims_module, "_COMMIT_FULL_SHAS",
        {dangling: dangling, reachable: reachable},
    )
    monkeypatch.setattr(report_claims_module, "_REACHABLE_COMMITS", {reachable})
    claims = ReportClaims(
        report=REPORTS_DIR / "synthetic-fixture.md",
        shas=[Claim(dangling, line=1), Claim(reachable, line=2)],
        paths=[],
        nodes=[],
        retired=[],
    )
    problems = commit_problems(claims)
    assert len(problems) == 1, problems
    assert dangling in problems[0]
    assert "reachable from HEAD" in problems[0]
    assert reachable not in problems[0]


def test_a_missing_decimal_id_names_the_check_run_convention(monkeypatch) -> None:
    """Task 067 (steer 004, "optional and cheap"): a decimal check-run/job/
    run id caught by `SHA_RE` reads exactly like a missing commit sha, and
    the operator lost time to that once (`102507184201 is not a commit in
    this repo (missing)`). The failure text for the `missing` kind must now
    also point at the convention that avoids it, without touching `SHA_RE`,
    the reachability check or the dangling-object refusal -- a real
    dangling/missing sha still fails, just with more guidance attached."""
    decimal_id = "102507184011"
    monkeypatch.setattr(
        report_claims_module, "_COMMIT_KINDS", {decimal_id: "missing"}
    )
    monkeypatch.setattr(
        report_claims_module, "_COMMIT_FULL_SHAS", {decimal_id: None}
    )
    monkeypatch.setattr(report_claims_module, "_REACHABLE_COMMITS", set())
    claims = ReportClaims(
        report=REPORTS_DIR / "synthetic-fixture.md",
        shas=[Claim(decimal_id, line=1)],
        paths=[],
        nodes=[],
        retired=[],
    )
    problems = commit_problems(claims)
    assert len(problems) == 1, problems
    assert decimal_id in problems[0]
    assert "is not a commit in this repo" in problems[0]
    assert "check-run, job or run id" in problems[0]
    assert "do not backtick it" in problems[0]


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
    subprocess call per claim -- even across many claims in one report.

    No real `git` process here: a canned `CompletedProcess` stands in for
    it, so this whitebox check of the *memoisation itself* never spends one
    of the family's own real `git ls-files` calls (task 035, requirement
    E.4 pins the family to exactly one real such call across a whole run;
    this test proves the caching logic that makes that true without adding
    a second real invocation of its own)."""
    monkeypatch.setattr(report_claims_module, "_TRACKED_PATHS", None)
    calls = []
    fake_stdout = "tests/report_claims.py\ntests/records/test_report_claims.py\n"

    def fake_run(*args, **kwargs):
        calls.append(args)
        argv = args[0] if args else kwargs.get("args")
        return subprocess.CompletedProcess(args=argv, returncode=0,
                                            stdout=fake_stdout, stderr="")

    monkeypatch.setattr(report_claims_module.subprocess, "run", fake_run)
    many_claims = ReportClaims(
        report=REPORTS_DIR / "synthetic-fixture.md",
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
        report=REPORTS_DIR / "synthetic-fixture.md",
        shas=[],
        paths=[Claim("tests/records/test_report_claims.py", line=1)],
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
            report=REPORTS_DIR / "synthetic-fixture.md",
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
        report=REPORTS_DIR / "synthetic-fixture.md",
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
