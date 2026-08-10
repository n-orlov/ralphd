"""Task 003: unit tests for `ralphctl status`'s human-readable summaries --
a `reason:` line when present (and its absence otherwise), a one-line
`tasks:` summary (e.g. '7/7 completed') instead of a raw JSON dump, and a
one-line `usage:` summary (e.g. '$0.56, 625k tokens (planning $0.10 /
worker $0.40 / review $0.06)').

These exercise the pure formatting helpers directly (no live engine, no
docker) -- the same pattern as test_cli_logs_tail_syntax.py's
`_preprocess_logs_argv` unit tests.
"""

from __future__ import annotations

from ralphd.cli.main import (
    _format_reason_lines,
    _summarize_tasks,
    _summarize_usage,
)

# --------------------------------------------------------------------------
# reason:
# --------------------------------------------------------------------------

def test_reason_present_renders_a_line():
    lines = _format_reason_lines("budget exhausted with all tasks completed")
    assert lines == ["reason:    budget exhausted with all tasks completed"]


def test_reason_absent_renders_nothing():
    assert _format_reason_lines(None) == []
    assert _format_reason_lines("") == []


def test_reason_long_text_wraps_across_multiple_lines():
    reason = "engine error: " + ("x" * 200)
    lines = _format_reason_lines(reason)
    assert len(lines) > 1
    assert lines[0].startswith("reason:    ")
    for extra in lines[1:]:
        assert extra.startswith("           ")
    # no line loses/duplicates content: rejoin (stripping the fixed-width
    # label/continuation prefixes) reconstructs the wrapped text losslessly
    rejoined = " ".join(
        line.removeprefix("reason:    ").removeprefix("           ")
        for line in lines
    )
    assert rejoined.replace(" ", "") == reason.replace(" ", "")


# --------------------------------------------------------------------------
# tasks:
# --------------------------------------------------------------------------

def test_tasks_summary_all_completed():
    assert _summarize_tasks({"total": 7, "completed": 7}) == "7/7 completed"


def test_tasks_summary_mixed_statuses():
    summary = _summarize_tasks({
        "total": 7, "completed": 5, "inProgress": 1, "pending": 1,
    })
    assert summary.startswith("5/7 completed (")
    assert "1 in-progress" in summary
    assert "1 pending" in summary


def test_tasks_summary_zero_counts_omitted():
    summary = _summarize_tasks({
        "total": 3, "completed": 3, "failed": 0, "skipped": 0,
    })
    assert summary == "3/3 completed"


def test_tasks_summary_validation_failed_label():
    summary = _summarize_tasks({
        "total": 2, "completed": 1, "validationFailed": 1,
    })
    assert "1 validation-failed" in summary


def test_tasks_summary_empty():
    assert _summarize_tasks({}) == "(none)"


# --------------------------------------------------------------------------
# usage:
# --------------------------------------------------------------------------

def test_usage_summary_with_phase_breakdown():
    usage = {
        "costUSD": 0.56,
        "totalTokens": 625_000,
        "byPhase": {
            "planning": {"costUSD": 0.10},
            "worker": {"costUSD": 0.40},
            "review": {"costUSD": 0.06},
        },
    }
    summary = _summarize_usage(usage)
    assert summary == "$0.56, 625k tokens (planning $0.10 / worker $0.40 / review $0.06)"


def test_usage_summary_no_phase_breakdown():
    usage = {"costUSD": 0.0, "totalTokens": 500}
    summary = _summarize_usage(usage)
    assert summary == "$0.00, 500 tokens"


def test_usage_summary_partial_phase_breakdown_only_shows_present_phases():
    usage = {
        "costUSD": 0.12,
        "totalTokens": 1_500,
        "byPhase": {"planning": {"costUSD": 0.12}},
    }
    summary = _summarize_usage(usage)
    assert summary == "$0.12, 1.5k tokens (planning $0.12)"


def test_usage_summary_empty():
    assert _summarize_usage({}) == "(none)"
