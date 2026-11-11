"""SPEC.md §5.4's `review.retry` / `verdict_gate.retry` rows name every
`reason` the engine's one retry rule (`_judge_reply_with_retry`) can emit,
including the crash and timeout reasons of a phase-scoped judge, and agree with
the phase-review prose of §4.2 and the user docs."""

from __future__ import annotations

import re
from pathlib import Path

from ralphd.engine.loop import LoopSupervisor

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "SPEC.md").read_text()
FLAT_SPEC = re.sub(r"\s+", " ", SPEC)


def _row(event: str) -> str:
    for line in SPEC.splitlines():
        if line.startswith(f"| `{event}` |"):
            return line
    raise AssertionError(f"no §5.4 row for {event}")


def _doc(name: str) -> str:
    return re.sub(r"\s+", " ", (ROOT / "docs" / name).read_text())


def test_review_retry_row_names_crash_and_timeout_reasons():
    row = _row("review.retry")
    assert '"judge_crashed"' in row and '"judge_timed_out"' in row
    assert "phase review" in row


def test_verdict_gate_retry_row_names_crash_and_timeout_reasons():
    row = _row("verdict_gate.retry")
    assert '"judge_crashed"' in row and '"judge_timed_out"' in row
    assert "phase-scoped" in row


def test_review_retry_row_names_the_contract_fault_reasons():
    # The reasons `_read_review_reply` reports for a non-empty unreadable reply
    # are retry reasons too (`review.retry` carries them).
    row = _row("review.retry")
    for reason in LoopSupervisor.REVIEW_CONTRACT_FAULTS:
        assert f'"{reason}"' in row


def test_retry_rows_keep_the_bound_and_the_no_verdict_outcome():
    for event, no_verdict in (("review.retry", "review.no_verdict"),
                              ("verdict_gate.retry", "verdict_gate.no_verdict")):
        row = _row(event)
        assert "`judge_retry_max`" in row
        assert "uncharged" in row
        assert no_verdict in row
        assert "never a rejection" in row


def test_every_judge_no_verdict_cut_short_reason_is_a_documented_retry_reason():
    # Whatever reading the engine may end a phase judge on after its retries
    # (`JUDGE_NO_VERDICT_WORDING`) that comes from the engine cutting the
    # iteration short is also what the retry events carry beforehand.
    cut_short = [r for r in LoopSupervisor.JUDGE_NO_VERDICT_WORDING
                 if r in ("judge_crashed", "judge_timed_out")]
    assert sorted(cut_short) == ["judge_crashed", "judge_timed_out"]
    for reason in cut_short:
        for event in ("review.retry", "verdict_gate.retry"):
            assert f'"{reason}"' in _row(event)
        assert f'"{reason}"' in _row("review.no_verdict")
        assert f'"{reason}"' in _row("verdict_gate.no_verdict")


def test_phase_review_prose_agrees_with_the_rows():
    assert ("`review.retry` carrying `reason` `judge_crashed` or "
            "`judge_timed_out`") in FLAT_SPEC
    assert "`verdict_gate.retry` carries the same two reasons" in FLAT_SPEC


def test_user_docs_name_the_crash_and_timeout_retry_reasons():
    for doc in ("api.md", "architecture.md"):
        text = _doc(doc)
        for reason in ("judge_crashed", "judge_timed_out"):
            assert f"`{reason}`" in text or f'`"{reason}"`' in text
