"""Task 002 (#103, requirement A): a sweep record's sha stops being a
sentence and becomes a checkable fact.

`docs/sweep-records.md` documents the `sweep-sha` / `discharge-sha` fenced
block forms and the discharge rule; `tests/sweep_sha_guard.py` implements
the parser and the two problem-list functions this module applies to every
report `artifacts/reports/` currently holds, plus two tmp-dir fixtures that
prove the guard actually fires on a stale sha and actually clears once that
sha is discharged -- the fixtures build their own throwaway record so the
"proves the failure fires" fixture can never itself trip the real guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import sweep_sha_guard as guard

REPORTS = guard.report_files()


def _run_git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, f"git {args} failed: {out.stderr}"
    return out.stdout.strip()


def _init_throwaway_repo(root: Path) -> Path:
    """A throwaway git repo under tmp_path -- never inside the real
    artifacts/reports/, so these fixtures can never trip the real guard.
    """
    repo = root / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "fixture@example.invalid")
    _run_git(repo, "config", "user.name", "Fixture")
    return repo


def _commit(repo: Path, relpath: str, content: str, message: str) -> str:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _run_git(repo, "add", relpath)
    _run_git(repo, "commit", "-q", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD")


def _ids(reports: list[Path]) -> list[str]:
    return [r.name for r in reports]


def test_the_reports_directory_is_discovered_not_enumerated() -> None:
    assert guard.REPORTS_DIR.is_dir(), f"{guard.REPORTS_DIR} is missing"
    names = {p.name for p in REPORTS}
    assert names == {p.name for p in guard.REPORTS_DIR.glob("*.md")}
    # This wave's own notification log exists, starts with the wave prefix,
    # names no pytest result line, and is therefore correctly NOT a sweep
    # record -- a sanity check that the "sweep record" definition is not
    # simply "starts with v0.14-".
    assert "v0.14-operator-notifications.md" in names
    assert not guard.is_wave_sweep_record(
        guard.REPORTS_DIR / "v0.14-operator-notifications.md"
    )


def test_every_declared_sweep_sha_is_current_or_discharged() -> None:
    head = guard.current_head()
    problems = guard.check_reports(REPORTS, head)
    assert not problems, "\n".join(problems)


def test_every_v014_sweep_record_declares_a_sha() -> None:
    problems = guard.wave_sha_missing_problems(REPORTS)
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------
# Fixtures: prove the guard fires, and prove discharge clears it. Built in a
# tmp_path, never under the real artifacts/reports/, so these can never trip
# the guard they exist to test.
# --------------------------------------------------------------------------


def test_stale_undischarged_sha_fails(tmp_path) -> None:
    stale = "a" * 40
    head = "b" * 40
    record = tmp_path / "v0.14-fixture-stale-sweep.md"
    record.write_text(
        "# fixture sweep record\n\n"
        f"```sweep-sha\n{stale}\n```\n\n"
        "```\n1 passed in 0.01s\n```\n"
    )

    problems = guard.check_reports([record], head)

    assert problems, "expected the guard to reject a stale, undischarged sha"
    assert len(problems) == 1
    assert record.name in problems[0]
    assert stale in problems[0]
    assert head in problems[0]
    # The message tells the reader what to do about it.
    assert "re-take" in problems[0]
    assert "discharge-sha" in problems[0]


def test_current_head_sha_passes(tmp_path) -> None:
    head = "c" * 40
    record = tmp_path / "v0.14-fixture-current-sweep.md"
    record.write_text(f"```sweep-sha\n{head}\n```\n\n1 passed in 0.01s\n")

    assert guard.check_reports([record], head) == []


def test_discharged_stale_sha_passes(tmp_path) -> None:
    stale = "a" * 40
    head = "b" * 40
    record = tmp_path / "v0.14-fixture-stale-sweep.md"
    record.write_text(f"```sweep-sha\n{stale}\n```\n\n1 passed in 0.01s\n")
    discharge = tmp_path / "v0.14-fixture-discharge.md"
    # The discharge names BOTH shas: what it retires, and the sha the
    # discharge check was run at -- here, HEAD itself, so the chain is one hop.
    discharge.write_text(
        "# fixture discharge\n\n"
        f"```discharge-sha\n{stale} {head}\n```\n"
    )

    problems = guard.check_reports([record, discharge], head)

    assert problems == [], problems


def test_a_transitive_discharge_chain_ending_at_head_passes(tmp_path) -> None:
    stale = "a" * 40
    middle = "d" * 40
    head = "b" * 40
    record = tmp_path / "v0.14-fixture-stale-sweep.md"
    record.write_text(f"```sweep-sha\n{stale}\n```\n\n1 passed in 0.01s\n")
    discharge = tmp_path / "v0.14-fixture-discharge-chain.md"
    discharge.write_text(
        f"```discharge-sha\n{stale} {middle}\n{middle} {head}\n```\n"
    )

    assert guard.check_reports([record, discharge], head) == []


def test_a_discharge_chain_that_never_reaches_head_fails(tmp_path) -> None:
    """The documented rule, enforced: "a chain that never reaches HEAD is
    not [accepted]". An earlier revision of this guard treated ANY matching
    discharge-sha block as sufficient, so a discharge verified at some other
    stale sha (or at nothing checkable at all) silently cleared the
    declaration -- exactly the undetected "ran at sha X" claim requirement A
    exists to kill."""
    stale = "a" * 40
    also_stale = "d" * 40
    head = "b" * 40
    record = tmp_path / "v0.14-fixture-stale-sweep.md"
    record.write_text(f"```sweep-sha\n{stale}\n```\n\n1 passed in 0.01s\n")
    discharge = tmp_path / "v0.14-fixture-dangling-discharge.md"
    discharge.write_text(f"```discharge-sha\n{stale} {also_stale}\n```\n")

    problems = guard.check_reports([record, discharge], head)

    assert len(problems) == 1, problems
    assert stale in problems[0] and head in problems[0]
    assert "never reaches HEAD" in problems[0]


def test_a_discharge_cycle_never_clears_a_stale_sha(tmp_path) -> None:
    stale = "a" * 40
    other = "d" * 40
    head = "b" * 40
    record = tmp_path / "v0.14-fixture-stale-sweep.md"
    record.write_text(f"```sweep-sha\n{stale}\n```\n\n1 passed in 0.01s\n")
    discharge = tmp_path / "v0.14-fixture-cyclic-discharge.md"
    discharge.write_text(
        f"```discharge-sha\n{stale} {other}\n{other} {stale}\n```\n"
    )

    problems = guard.check_reports([record, discharge], head)

    assert len(problems) == 1, problems
    assert stale in problems[0]


def test_a_one_sha_discharge_block_is_rejected_as_malformed(tmp_path) -> None:
    """A discharge block naming only the retired sha is a hard failure, not a
    tolerated shorthand: with no verified-at sha there is no chain to check."""
    stale = "a" * 40
    head = "b" * 40
    record = tmp_path / "v0.14-fixture-stale-sweep.md"
    record.write_text(f"```sweep-sha\n{stale}\n```\n\n1 passed in 0.01s\n")
    discharge = tmp_path / "v0.14-fixture-short-discharge.md"
    discharge.write_text(f"```discharge-sha\n{stale}\n```\n")

    problems = guard.check_reports([record, discharge], head)

    # Both the malformed block and the still-stale declaration are reported.
    assert len(problems) == 2, problems
    malformed = [p for p in problems if "malformed discharge-sha line" in p]
    assert len(malformed) == 1, problems
    assert discharge.name in malformed[0]
    assert stale in malformed[0]


def test_a_v014_sweep_record_with_no_sha_is_caught_by_the_companion_check(
    tmp_path,
) -> None:
    record = tmp_path / "v0.14-fixture-missing-sha-sweep.md"
    record.write_text("# fixture\n\n1 passed in 0.01s\n")

    problems = guard.wave_sha_missing_problems([record])

    assert len(problems) == 1
    assert record.name in problems[0]
    assert "sweep-sha" in problems[0]


def test_a_non_wave_or_non_sweep_report_is_never_required_to_declare_a_sha(
    tmp_path,
) -> None:
    not_this_wave = tmp_path / "v0.13-old-sweep.md"
    not_this_wave.write_text("1 passed in 0.01s\n")
    not_a_sweep = tmp_path / "v0.14-fixture-plain-notification.md"
    not_a_sweep.write_text("just a notification, no test result line\n")

    assert guard.wave_sha_missing_problems([not_this_wave, not_a_sweep]) == []


# --------------------------------------------------------------------------
# Task 001 (#103, requirement A hardening): a pytest result line is a WHOLE
# LINE of counts plus pytest's own "in <n>s" duration -- prose that puts a
# count next to an outcome word, and prose that quotes a real summary line
# inline, are both not result lines. Every test below fails against the
# pre-change rule (`re.compile(r"\b\d+\s+(?:passed|failed|skipped|errors?)\b")`,
# searched unanchored), which is exactly what let
# artifacts/reports/v0.14-operator-notifications.md's prose ("Attempt 1
# failed validation ...", "016 failed->skipped ...") get misclassified as a
# sweep record owing a `sweep-sha` block.
# --------------------------------------------------------------------------


def test_an_english_sentence_with_a_count_is_not_a_pytest_result_line() -> None:
    prose = (
        "reached in PRODUCTION. Attempt 1 failed validation because the "
        "real work never happened."
    )
    assert not guard.is_pytest_result_line(prose)
    assert not guard.contains_pytest_result_line(prose)
    # Also stated against the regex itself, so this test fails whether the
    # sharpening is reverted at the regex or at the helpers.
    assert not guard.PYTEST_SUMMARY_RE.search(prose)

    more_prose = (
        "container) cannot relabel 016 failed->skipped itself because "
        "`tasks.json` is read-only here."
    )
    assert not guard.is_pytest_result_line(more_prose)
    assert not guard.contains_pytest_result_line(more_prose)
    assert not guard.PYTEST_SUMMARY_RE.search(more_prose)

    # A sentence that quotes a REAL summary line inline is still prose: the
    # counts-plus-duration shape has to be the whole line, not a substring
    # of it. An unanchored search on the same text matches and is wrong.
    quoting_prose = "English says 4302 passed, 1 skipped in 856.12s yesterday."
    assert not guard.is_pytest_result_line(quoting_prose)
    assert not guard.contains_pytest_result_line(quoting_prose)
    assert not guard.PYTEST_SUMMARY_RE.search(quoting_prose)


def test_a_real_pytest_summary_line_is_a_pytest_result_line() -> None:
    lines = [
        "4302 passed, 1 skipped in 856.12s",
        "588 passed in 21.92s",
        "2227 passed, 1 skipped, 42 deselected in 164.09s",
        "1 failed, 1 passed in 0.07s",
    ]
    for line in lines:
        assert guard.is_pytest_result_line(line), line
        # The detector matches the line ENTIRE, not a leading fragment of it
        # ("4302 passed"): that is what makes a hit a statement about a whole
        # line of the report.
        match = guard.PYTEST_SUMMARY_RE.fullmatch(line)
        assert match is not None and match.group(0) == line, line

    # The forms a real record actually holds: indented inside a fenced block,
    # and pytest's non-quiet `=`-padded form.
    assert guard.is_pytest_result_line("    4302 passed, 1 skipped in 856.12s")
    assert guard.contains_pytest_result_line(
        "the fast tier:\n\n```\n    4302 passed, 1 skipped in 856.12s\n```\n"
    )
    assert guard.is_pytest_result_line(
        "===== 2 failed, 10 passed in 1.23s ====="
    )


def test_a_report_that_only_quotes_counts_inline_is_not_a_sweep_record(
    tmp_path,
) -> None:
    """The anchoring, at the level that matters: a v0.14 notification/prose
    file that mentions a sweep's counts inside a sentence does not thereby
    become a sweep record owing a `sweep-sha` block."""
    prose_only = tmp_path / "v0.14-fixture-prose-quoting-counts.md"
    prose_only.write_text(
        "Sent: the fast tier came back 4302 passed, 1 skipped in 856.12s, "
        "which is the same as last wave.\n"
    )

    assert not guard.is_wave_sweep_record(prose_only)
    assert not guard.PYTEST_SUMMARY_RE.search(prose_only.read_text())
    assert guard.wave_sha_missing_problems([prose_only]) == []

    # ... whereas the same counts on a line of their own DO make it one.
    real_record = tmp_path / "v0.14-fixture-real-sweep-line.md"
    real_record.write_text(
        "fast tier\n\n```\n4302 passed, 1 skipped in 856.12s\n```\n"
    )
    assert guard.is_wave_sweep_record(real_record)
    assert len(guard.wave_sha_missing_problems([real_record])) == 1


def test_the_real_notification_log_is_not_misclassified_as_a_sweep_record() -> None:
    """The measured defect itself: the notification log this wave already
    wrote contains the prose above, and must not be flagged as a v0.14
    sweep record that owes a `sweep-sha` block."""
    log = guard.REPORTS_DIR / "v0.14-operator-notifications.md"
    assert log in REPORTS
    assert not guard.is_wave_sweep_record(log)
    assert not guard.contains_pytest_result_line(log.read_text())
    assert not guard.PYTEST_SUMMARY_RE.search(log.read_text())
    assert guard.wave_sha_missing_problems([log]) == []


# --------------------------------------------------------------------------
# Task 003 (#103, requirement A hardening): the discharge chain's own end
# can never be literal HEAD -- the commit that ADDS a `discharge-sha` block
# can only name shas that already exist, so it always leaves HEAD one
# commit past the chain's end. These three tests build real throwaway git
# repos under tmp_path (never inside the real artifacts/reports/, so they
# can never trip the real guard) and pin the report-only-tail allowance
# that makes the rule satisfiable at all. Each of the first two fails
# against the pre-change `_reaches_head`, which only ever compared for
# literal string equality with HEAD and had no `repo_root` to run `git
# diff` against.
# --------------------------------------------------------------------------


def test_a_discharge_ending_short_of_head_passes_when_the_tail_is_report_only(
    tmp_path,
) -> None:
    repo = _init_throwaway_repo(tmp_path)
    stale = _commit(repo, "src/thing.py", "print('v1')\n", "add thing")
    verified_at = _commit(
        repo, "artifacts/reports/note-1.md", "note one\n", "report note one"
    )
    # Further report-only tail commits after the sha the discharge was
    # actually verified at -- exactly the shape a wave's own tail (roadmap
    # block, PRD index row, closing report, and the discharge commit
    # itself) produces.
    _commit(repo, "artifacts/reports/note-2.md", "note two\n", "report note two")
    head = _commit(
        repo, "artifacts/reports/note-3.md", "note three\n", "report note three"
    )

    record = repo / "artifacts" / "reports" / "v0.14-fixture-sweep.md"
    record.write_text(
        f"```sweep-sha\n{stale}\n```\n\n1 passed in 0.01s\n\n"
        f"```discharge-sha\n{stale} {verified_at}\n```\n"
    )

    problems = guard.check_reports([record], head, repo_root=repo)

    assert problems == [], problems


def test_a_discharge_ending_short_of_head_fails_when_one_tail_commit_touches_src(
    tmp_path,
) -> None:
    repo = _init_throwaway_repo(tmp_path)
    stale = _commit(repo, "src/thing.py", "print('v1')\n", "add thing")
    verified_at = _commit(
        repo, "artifacts/reports/note-1.md", "note one\n", "report note one"
    )
    # One intervening commit touches src/, not just artifacts/reports/ -- the
    # gap is no longer report-only and the allowance must not bridge it.
    _commit(repo, "src/thing.py", "print('v2')\n", "change thing")
    head = _commit(
        repo, "artifacts/reports/note-2.md", "note two\n", "report note two"
    )

    record = repo / "artifacts" / "reports" / "v0.14-fixture-sweep.md"
    record.write_text(
        f"```sweep-sha\n{stale}\n```\n\n1 passed in 0.01s\n\n"
        f"```discharge-sha\n{stale} {verified_at}\n```\n"
    )

    problems = guard.check_reports([record], head, repo_root=repo)

    assert len(problems) == 1, problems
    assert stale in problems[0] and head in problems[0]


def test_an_undischarged_sha_still_fails_even_with_a_report_only_tail(
    tmp_path,
) -> None:
    """The allowance only bridges the LAST leg of a chain a real
    `discharge-sha` entry actually walked to -- it must not let a bare,
    never-discharged sweep-sha pass just because nothing but reports
    happened to change since. Without this, the guard would stop being a
    hard failure and become a warning that clears itself with time."""
    repo = _init_throwaway_repo(tmp_path)
    stale = _commit(repo, "src/thing.py", "print('v1')\n", "add thing")
    head = _commit(
        repo, "artifacts/reports/note-1.md", "note one\n", "report note one"
    )

    record = repo / "artifacts" / "reports" / "v0.14-fixture-sweep.md"
    record.write_text(f"```sweep-sha\n{stale}\n```\n\n1 passed in 0.01s\n")

    problems = guard.check_reports([record], head, repo_root=repo)

    assert len(problems) == 1, problems
    assert stale in problems[0] and head in problems[0]
