"""Task 003: unit tests for `ralphctl status`'s human-readable summaries --
a `reason:` line when present (and its absence otherwise), a one-line
`tasks:` summary (e.g. '7/7 completed') instead of a raw JSON dump, and a
one-line `usage:` summary (e.g. '$0.56, 625k tokens (planning $0.10 /
worker $0.40 / review $0.06)').

Task 007 (#72) adds a fourth: the `deadline:` line for a terminal write
task 006's `_deadline_beat_verdict_patch()` produced -- status.json's
`deadlineBeatVerdict` field -- so a human staring at plain `ralphctl status`
can tell that shape apart from an ordinary failed run instead of reading
both as the same `state: failed` / `verdict: unverified` with nothing else
to go on.

These exercise the pure formatting helpers directly (no live engine, no
docker) -- the same pattern as test_cli_logs_tail_syntax.py's
`_preprocess_logs_argv` unit tests.
"""

from __future__ import annotations

from ralphd.cli.main import (
    _format_deadline_beat_verdict_lines,
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
    # a real (non-zero) quote: a $0 next to billed tokens is task 049's
    # implausible zero and renders `unavailable` (tests/test_cost_zero_quote.py)
    usage = {"costUSD": 0.02, "totalTokens": 500}
    summary = _summarize_usage(usage)
    assert summary == "$0.02, 500 tokens"


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


# --------------------------------------------------------------------------
# deadline: (task 007, #72)
# --------------------------------------------------------------------------

def test_deadline_beat_verdict_absent_renders_nothing():
    # the common, correct-for-every-OTHER-terminal-write case: no marker at
    # all (a pre-v0.10 run dir, or any terminal shape other than the one
    # task 006 patches) prints nothing extra.
    assert _format_deadline_beat_verdict_lines(None) == []
    assert _format_deadline_beat_verdict_lines({}) == []


def test_deadline_beat_verdict_present_names_both_components():
    marker = {"deadline": "2026-08-26T06:53:21Z",
              "completeHonouredAt": "2026-08-26T06:53:15Z"}
    lines = _format_deadline_beat_verdict_lines(marker)
    assert lines, "a present marker must render at least one line"
    assert lines[0].startswith("deadline:  ")
    text = " ".join(lines)
    # names both timestamps the marker carries...
    assert marker["deadline"] in text
    assert marker["completeHonouredAt"] in text
    # ...and says, in words a human can act on, that this is not a verdict
    # on the work -- the whole reason this line exists rather than leaving
    # the run to read as an ordinary failure.
    assert "not a failure judgement" in text.lower()
    assert "#72" in text


def test_deadline_beat_verdict_wraps_and_hangs_indented():
    marker = {"deadline": "2026-08-26T06:53:21Z",
              "completeHonouredAt": "2026-08-26T06:53:15Z"}
    lines = _format_deadline_beat_verdict_lines(marker)
    assert len(lines) > 1, "the message is long enough to need wrapping"
    for extra in lines[1:]:
        assert extra.startswith("           "), extra


def test_deadline_beat_verdict_is_distinguishable_from_an_ordinary_reason_line():
    """The success criterion in one assertion: rendering both a plain
    `reason:` (what every other failed run gets) and the `deadline:` line for
    the same status must not collapse to the same text -- a human reading
    the two outputs side by side can tell which run this is."""
    ordinary_reason_lines = _format_reason_lines(
        "budget exhausted with all tasks completed")
    marker_lines = _format_deadline_beat_verdict_lines(
        {"deadline": "2026-08-26T06:53:21Z",
         "completeHonouredAt": "2026-08-26T06:53:15Z"})
    assert ordinary_reason_lines != marker_lines
    assert not marker_lines[0].startswith("reason:")
    assert marker_lines[0].startswith("deadline:")
