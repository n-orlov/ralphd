"""Task 002 (#103, requirement A): a sweep record's sha stops being a
sentence and becomes a checkable fact.

`docs/sweep-records.md` documents the `sweep-sha` / `archived-sha` fenced
block forms; `tests/sweep_sha_guard.py` implements the parser and the two
problem-list functions this module applies to every report
`artifacts/reports/` currently holds.

Task 053 (v0.20, #113 step 4) deleted every test node below that existed
only to exercise the now-retired currency check (`check_reports`), the
hand-maintained discharge chain, and the self-anchoring/fixed-point
acceptance walk -- `test_every_declared_sweep_sha_is_current_or_discharged`,
`test_stale_undischarged_sha_fails`, `test_current_head_sha_passes`,
`test_discharged_stale_sha_passes`,
`test_a_transitive_discharge_chain_ending_at_head_passes`,
`test_a_discharge_chain_that_never_reaches_head_fails`,
`test_a_discharge_cycle_never_clears_a_stale_sha`,
`test_a_one_sha_discharge_block_is_rejected_as_malformed`,
`test_a_post_launch_sha_declared_as_archived_is_refused`,
`test_archived_sha_accepts_the_launch_sha_itself`,
`test_a_discharge_ending_short_of_head_passes_when_the_tail_is_report_only`,
`test_a_discharge_ending_short_of_head_fails_when_one_tail_commit_touches_src`,
`test_a_bare_undischarged_sha_with_a_fully_report_only_tail_now_passes_via_self_anchoring`,
`test_a_record_only_add_commit_naming_its_parent_passes_with_no_discharge`,
`test_a_self_anchoring_record_followed_by_a_report_only_commit_passes`,
`test_a_self_anchoring_record_followed_by_a_src_commit_fails` and
`test_a_self_anchoring_declared_sha_not_an_ancestor_of_head_fails`. None of
them is subsumed by a surviving test node -- the behaviour they pinned is
retired outright, not moved -- so there is no `docs/test-suite-audit.md` §
"Subsumed" row for any of them; the surviving cover for what they tested is
the GitHub check run itself (#113 step 4), not a pytest node. Every
historical report that cited one of these node ids (or the now-deleted
`tests/sweep_sha_guard.py` functions `check_reports`, `_reaches_head`,
`_report_only_tail`, `_is_self_anchoring_add_commit` and
`_self_anchoring_reaches_head`) by exact node id was updated in the same
commit to mark that citation `!retired` -- the declared sha each of those
records names is untouched; only the drift-chasing citation is retired.

What remains: the two fixtures below that prove the ONE surviving lint
fires and clears, plus the result-line detector's own tests (unaffected by
this retirement, since `is_pytest_result_line`/`contains_pytest_result_line`
never had anything to do with currency, discharge or self-anchoring).
"""

from __future__ import annotations

import sweep_sha_guard as guard

REPORTS = guard.report_files()


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


def test_every_sweep_record_declares_a_sha() -> None:
    problems = guard.sha_missing_problems(REPORTS)
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------
# Fixtures: prove the one surviving lint fires and clears. Built in a
# tmp_path, never under the real artifacts/reports/, so these can never trip
# the guard they exist to test.
# --------------------------------------------------------------------------


def test_a_sweep_record_with_no_sha_is_caught_by_the_missing_check(
    tmp_path,
) -> None:
    """Task 008 (#103, requirement A): the missing-declaration check applies
    regardless of filename -- there is no `v0.14-` gate any more."""
    record = tmp_path / "fixture-missing-sha-sweep.md"
    record.write_text("# fixture\n\n1 passed in 0.01s\n")

    problems = guard.sha_missing_problems([record])

    assert len(problems) == 1
    assert record.name in problems[0]
    assert "sweep-sha" in problems[0]
    assert "archived-sha" in problems[0]


def test_a_pre_launch_filename_no_longer_exempts_a_sweep_record(
    tmp_path,
) -> None:
    """The old rule exempted every report not named `v0.14-*`, whatever it
    reported -- a report of a real sweep with no declaration at all still
    passed as long as its filename predated the wave. Task 008 (#103,
    requirement A) closed that: a pytest result line now owes a declaration
    (sweep-sha or archived-sha) no matter what the file is named. Only a
    report that names no pytest result line at all is exempt, filename
    aside.
    """
    undeclared_old_sweep = tmp_path / "v0.13-old-sweep.md"
    undeclared_old_sweep.write_text("1 passed in 0.01s\n")
    not_a_sweep = tmp_path / "v0.14-fixture-plain-notification.md"
    not_a_sweep.write_text("just a notification, no test result line\n")

    problems = guard.sha_missing_problems([undeclared_old_sweep, not_a_sweep])

    assert len(problems) == 1
    assert undeclared_old_sweep.name in problems[0]


def test_an_archived_sha_predating_launch_satisfies_the_missing_check(
    tmp_path,
) -> None:
    """The documented alternative to sweep-sha: a record that declares
    `archived-sha` instead is not flagged as missing a declaration. Neither
    form is checked against anything else any more (task 053, #113 step 4,
    which is why this test's own name no longer mentions the launch sha it
    used to be checked against) -- declaring one is the whole requirement.
    """
    record = tmp_path / "v0.13-old-sweep.md"
    record.write_text(
        f"```archived-sha\n{'a' * 40}\n```\n\n1 passed in 0.01s\n"
    )

    assert guard.sha_missing_problems([record]) == []


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


def test_a_real_elapsed_hms_summary_line_is_a_pytest_result_line() -> None:
    """Task 002 (#105, requirement A): pytest's `-q` summary grows a
    parenthesized `(H:MM:SS)` suffix once a run passes ~60s of wall time, so
    the pre-widening matcher rejected every real sweep this project runs.
    Both lines below are taken verbatim from the PRD -- the fast tier's own
    real summary and the guard subset's -- and both must be recognized as
    whole-line pytest result lines.
    """
    fast_tier_line = (
        "4198 passed, 1 skipped, 62 deselected in 852.84s (0:14:12)"
    )
    guard_subset_line = "42 passed, 2 deselected in 473.94s (0:07:53)"

    for line in (fast_tier_line, guard_subset_line):
        assert guard.is_pytest_result_line(line), line
        match = guard.PYTEST_SUMMARY_RE.fullmatch(line)
        assert match is not None and match.group(0) == line, line

    # Same forms a real record actually holds: indented in a fenced block,
    # and pytest's non-quiet `=`-padded form, now with the suffix present.
    assert guard.is_pytest_result_line(f"    {fast_tier_line}")
    assert guard.contains_pytest_result_line(
        f"the fast tier:\n\n```\n    {fast_tier_line}\n```\n"
    )
    assert guard.is_pytest_result_line(f"===== {guard_subset_line} =====")


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
