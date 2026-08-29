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

from pathlib import Path

import sweep_sha_guard as guard

REPORTS = guard.report_files()


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
    discharge.write_text(
        "# fixture discharge\n\n"
        f"```discharge-sha\n{stale}\n```\n"
    )

    problems = guard.check_reports([record, discharge], head)

    assert problems == [], problems


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
