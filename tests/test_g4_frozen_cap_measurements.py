"""Task 022 (PRD G.4/G.9): the five transcript-byte measurements G.9 asks
to be "recorded in the commit" -- P's three fixtures, Q's own fixture, and
a 200-line tail with older history grown 10 -> 20,000 -- pinned here as
their own named, green test nodes (rather than only prose in a commit
message) so a later change that moves one of them fails a specific node
instead of only a stale comment.

Methodology, one convention per figure -- "a render call" (PRD P.1/P.5's
own words) means the WHOLE client-facing tail request, including every
doubling re-ask a real consumer (`ui_server._fetch_dead_tail`/
`_fetch_live_tail`) makes when a first attempt's raw bound under-delivers
rendered lines, mirrored here by `_grown_rendered_tail`
(`tests/test_log_merge_bounded_reads.py`'s own copy of that retry):

- P's three fixtures (`_write_iteration_with_interleaved_ignored_suffix`):
  a tail=200 request against P's own 1-visible-to-1-opaque interleaved
  suffix needs exactly one doubling re-ask (200 -> 400) to actually
  deliver 200 rendered lines, for the SAME reason PRD G.3's own pinned
  figures (131,072 / 393,216 / 393,216) were measured that way. Measured
  here at this task's own sha: unchanged from the pin -- collapse_index
  (tasks 020/021) is never wired into these dead-run fixtures (nothing
  ever calls `collapse_index.write_index` for them, since no live engine
  iteration boundary ever ran), so `merge_entries_tail` takes the
  unchanged growth-ladder fallback path (PRD G.4) exactly as before.

- Q's own fixture (`_write_iteration_with_recognized_nonrendering_gap`,
  PRD Q's own recognized-but-silent-`message_end`-gap shape): ONE bounded
  render call at the SAME tail=2 bound task 021's own commit measured
  (`test_collapse_index_resolves_the_seed_flag_in_o1_bytes_regardless_of_
  setter_depth`) -- confirmed here to still read the same single
  `_TAIL_READ_CHUNK` (65,536 bytes) on Q's own canonical fixture shape
  (gap_count=1,000, and again at the silent-prelude depths 1,000/1,700
  G.5's own test already renders correctly), with a validated
  `collapse_index` present, regardless of how far back the deciding
  setter sits. This is deliberately NOT the outer consumer-level
  growing-bound retry a caller uses to keep re-asking until it has as
  many RENDERED lines as it originally wanted: unlike P's fixtures (whose
  retained window always contains SOME visible content once grown to the
  requested count), Q's own fixture can render as few as one or two
  visible lines in the WHOLE iteration, so a caller insisting on more
  rendered lines than the iteration ever produces re-asks until it has
  read close to the iteration's own full size -- an axis P.1/P.5's cap
  was measured against OLDER PRECEDING history (unrelated growth outside
  the retained window), never against a single iteration's own intrinsic
  size once the correct answer only exists past every byte of it. That
  outer-retry cost is the residual this task's own commit message names
  as the "G vs the frozen cap" tension the standing rules already
  pre-authorise a petition for; it is not one of the five figures this
  file pins, and no test here calls `_grown_rendered_tail` against Q's
  own fixture.

- The 200-line tail grown 10 -> 20,000 (`_write_iteration_with_ignored_
  head`, the PRD's own intro-paragraph scenario, cited there at
  63,841,305 bytes on the parked, unbounded-ladder branch): ONE render
  call at tail=200, which this fixture's 200-real-lines-preceded-by-
  ignored-filler shape always satisfies without any retry (the retained
  200-line tail needs no further growth once the backward read reaches
  it) -- unrelated to `collapse_index`, since nothing in this fixture's
  own retained tail ever needs the collapse-state seed resolved at all.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

from ralphd import collapse_index
from ralphd.log_merge import OUTPUT_FILENAME, merged_lines

from test_log_merge_bounded_reads import (
    _count_transcript_bytes,
    _grown_rendered_tail,
    _write_iteration_with_ignored_head,
    _write_iteration_with_interleaved_ignored_suffix,
    _write_iteration_with_recognized_nonrendering_gap,
)

_FROZEN_CAP = 1_000_000  # PRD G.2: never rises in this wave.


async def _measure_p_fixture(tmp_path, monkeypatch, *, trailing_ignored: int,
                             older_padding: int) -> int:
    run_root = tmp_path / "runs" / "probe"
    _write_iteration_with_interleaved_ignored_suffix(
        run_root, 1, 100_000, trailing_ignored=trailing_ignored,
        older_padding=older_padding)

    async def _fetch(bound: int) -> str:
        return "".join(merged_lines(run_root, tail=bound))

    counts = _count_transcript_bytes(monkeypatch)
    rendered = await _grown_rendered_tail(_fetch, 200)
    monkeypatch.undo()
    assert len(rendered) == 200
    return counts[0]


async def test_p_fixture_1_unshifted_reads_131072_bytes_under_the_frozen_cap(
        tmp_path, monkeypatch):
    """PRD G.9's first of P's three fixtures -- unshifted alignment."""
    measured = await _measure_p_fixture(
        tmp_path, monkeypatch, trailing_ignored=0, older_padding=0)
    assert measured == 131_072, measured
    assert measured < _FROZEN_CAP, measured


async def test_p_fixture_2_shifted_reads_393216_bytes_under_the_frozen_cap(
        tmp_path, monkeypatch):
    """PRD G.9's second of P's three fixtures -- shifted by one trailing
    `ignored` event, so the retained tail's own oldest raw line is the
    opaque member of the interleaving cycle rather than its visible one."""
    measured = await _measure_p_fixture(
        tmp_path, monkeypatch, trailing_ignored=1, older_padding=0)
    assert measured == 393_216, measured
    assert measured < _FROZEN_CAP, measured


async def test_p_fixture_3_shifted_and_padded_reads_393216_bytes_under_the_frozen_cap(
        tmp_path, monkeypatch):
    """PRD G.9's third of P's three fixtures -- the same shift, with every
    older, non-rendering entry also padded by 500 bytes: the SAME figure
    as fixture 2, because `_SEED_LOOKBACK_SPECULATIVE_BYTE_FLOOR` is
    counted in bytes spent at the file's HEAD, not in a line count a
    padded event could inflate."""
    measured = await _measure_p_fixture(
        tmp_path, monkeypatch, trailing_ignored=1, older_padding=500)
    assert measured == 393_216, measured
    assert measured < _FROZEN_CAP, measured


def _measure_q_fixture_single_call(tmp_path, monkeypatch, *, gap_count: int,
                                   prelude_count: int) -> int:
    text = "Q's own fixture: streamed text must not replay"
    gap_line = json.dumps(
        {"type": "message_end", "message": {"content": []}, "padding": "x" * 1000})
    run_root = tmp_path / f"gap-{gap_count}-prelude-{prelude_count}" / "runs" / "probe"
    _write_iteration_with_recognized_nonrendering_gap(
        run_root, 1, text, gap_line, gap_count, prelude_count=prelude_count)
    collapse_index.write_index(run_root / "iterations" / "0001")

    counts = _count_transcript_bytes(monkeypatch)
    merged_lines(run_root, tail=2)
    measured = counts[0]
    monkeypatch.undo()
    return measured


def test_q_own_fixture_single_render_call_reads_65536_bytes_under_the_frozen_cap(
        tmp_path, monkeypatch):
    """PRD G.9's "Q's own fixture" figure: ONE bounded render call
    (`merged_lines(run_root, tail=2)`) against Q's own canonical fixture
    shape (task 007's `gap_count=1,000`, no prelude), with a validated
    `collapse_index` present, reads exactly one `_TAIL_READ_CHUNK`
    (65,536 bytes) -- the same O(1) figure task 021's own commit measured
    on its depth-stress variant of this fixture shape, confirmed here to
    hold on the shape Q's own requirement actually specifies."""
    from ralphd import log_merge

    measured = _measure_q_fixture_single_call(
        tmp_path, monkeypatch, gap_count=1000, prelude_count=0)
    assert measured == log_merge._TAIL_READ_CHUNK == 65_536, measured
    assert measured < _FROZEN_CAP, measured

    # Confirmed O(1) at G.5's own silent-prelude depths too (1,000 and
    # 1,700 padded silent events before the streamed text, 10,000 padded
    # silent events after it) -- the SAME single-call figure, however far
    # back the deciding setter sits.
    for prelude_count in (1000, 1700):
        with_prelude = _measure_q_fixture_single_call(
            tmp_path, monkeypatch, gap_count=10_000, prelude_count=prelude_count)
        assert with_prelude == 65_536, (prelude_count, with_prelude)
        assert with_prelude < _FROZEN_CAP, (prelude_count, with_prelude)


def test_200_line_tail_grown_10_to_20000_reads_65536_bytes_under_the_frozen_cap(
        tmp_path, monkeypatch):
    """PRD G.9's fifth figure -- the PRD's own intro-paragraph scenario
    (a 200-line rendered tail with unrelated OLDER history sharing the
    same newest iteration's `output.jsonl`, grown from 10 to 20,000
    events), cited there at 63,841,305 bytes on the parked, unbounded
    look-back-ladder branch (`wip/v022-q-silent-prelude`). ONE render
    call (`merged_lines(run_root, tail=200)`) against the GROWN fixture
    reads a single bounded `_TAIL_READ_CHUNK` (65,536 bytes) -- far under
    both the frozen cap and the grown file's own much larger on-disk size
    -- because the retained 200-line tail here is never interleaved with
    the older filler (task 005/PRD E's own fixture shape), so growing
    `ignored_count` alone never grows what a tail=200 request has to
    read."""
    real_lines = [json.dumps({"type": "message_end", "message": {"content": [
        {"type": "text", "text": f"line {i}"}]}}) for i in range(200)]

    small_root = tmp_path / "small" / "runs" / "probe"
    large_root = tmp_path / "large" / "runs" / "probe"
    _write_iteration_with_ignored_head(small_root, 1, 10, real_lines)
    _write_iteration_with_ignored_head(large_root, 1, 20_000, real_lines)
    large_size = (large_root / "iterations" / "0001" / OUTPUT_FILENAME).stat().st_size

    small_counts = _count_transcript_bytes(monkeypatch)
    small_lines = merged_lines(small_root, tail=200)
    small_measured = small_counts[0]
    monkeypatch.undo()

    large_counts = _count_transcript_bytes(monkeypatch)
    large_lines = merged_lines(large_root, tail=200)
    large_measured = large_counts[0]
    monkeypatch.undo()

    assert sum(1 for ln in large_lines if "ralphd.iteration" not in ln) == 200
    assert ([ln for ln in small_lines if "ralphd.iteration" not in ln]
            == [ln for ln in large_lines if "ralphd.iteration" not in ln])
    assert large_measured == 65_536, large_measured
    assert large_measured < _FROZEN_CAP, large_measured
    assert large_measured < large_size, "read the entire grown transcript"
    assert small_measured <= large_measured, (small_measured, large_measured)
