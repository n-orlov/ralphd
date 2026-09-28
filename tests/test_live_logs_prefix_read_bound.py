"""Task 005 (v0.27 PRD D.1, cure round, #275): the whole-call read bound
`tests/test_tail_whole_call_read_bound.py` pins for a FINISHED (or, via
`tests/test_d1_real_pirunner_active_iteration_reads_within_cap.py`, a
still-active) iteration's own PUBLISHED index must hold for a still-
active iteration cut at an arbitrary PREFIX -- not only at the exact
boundaries a real subprocess happens to publish at -- and it must hold
identically whether the index a reader finds on disk is the LIVE
writer's own (possibly lagging) published state, or a from-scratch
rebuild over that same prefix.

Every prefix below is built by truncating one of the three named PRD Q
fixtures (`_FIXTURES`, its own canonical no-prelude shape and both
silent-prelude depths) to strictly fewer than its own full line count:
one line short of the full transcript, and 35%, 60%, 83% and 97% of its
raw line count. For each prefix, two `render_index.json` (plus, for the
live case, `render_index.tail.ndjson`) documents are built over the exact
same truncated transcript bytes:

  - `index_kind="live"`: fed through the REAL incremental writer
    (`render_index.LiveRenderIndex.append`/`.flush`, exactly the
    append-then-flush-per-line cadence `engine/runner.py`'s `pump()`
    uses), with NO final `force=True` flush -- the iteration genuinely
    has not ended, so the writer's own on-disk state may still lag
    behind a full MAIN republish, extended only by whatever
    `render_index.tail.ndjson` shards its own fixed cadence has appended
    since the last one, exactly as a real still-running iteration's own
    index looks on disk at that exact byte.
  - `index_kind="rebuilt"`: `render_index.write_index`, a full from-
    scratch rebuild over the identical bytes -- what a reader would see
    if the engine's own end-of-iteration bookkeeping had already run
    (even though, for this prefix, nothing downstream says it has).

Both variants describe an iteration whose `meta.json` carries no
`endedAt` -- the prefix is what a reader would see mid-stream, whichever
index happens to be on disk for it -- and both are read through the exact
client-facing call (`ui_server.rendered_log_lines`), on BOTH of its
surfaces (`test_tail_whole_call_read_bound._wire`'s own `"live"`/`"disk"`
wiring -- `bounded_tail`'s own answer, and therefore every byte either
surface reads and every line either surface renders, is identical either
way for a `tail`-bounded request; only the returned `live` flag differs),
first and a repeated request, with every byte read off every file counted
by `test_log_render_bounded_tail._count_transcript_bytes` (index and
side-state bytes included, exactly the counter `test_tail_whole_call_
read_bound.py` itself uses).

Petitioned obligation (see the run's own petitions record): criterion
(2)'s own text also names a specific historical claim -- that the
`minus_final_line`/`live` case at prelude depth 1,000, built exactly as
above over PRD Q's own named silent-prelude fixture, renders something
other than the full renderer's last two lines at `32352c15` (the sha
immediately preceding task 004, `669d02b3`/`d8ee7354`, PRD D.2/D.3
together, in this cure round). Measured directly (a `git worktree` at
that sha, this module copied in verbatim): that case renders IDENTICALLY
at `32352c15` and at HEAD, and reads 626,226/626,676 bytes there against
614,948/615,398 bytes here -- both comfortably under the 1,000,000-byte
cap, task 004's own fix (diff-encoding a checkpoint's evolving `state`)
narrowing the gap by only ~1.8% for this fixture's own four distinct
render states, nowhere near enough to have crossed the cap either way.
Task 004's own fix targets a transcript with many DISTINCT, ever-growing
render states (unmatched `tool_execution_start` events, `tests/test_
render_index_linear_writer.py`'s own `unended_tool_starts` sequence) --
PRD Q's silent-prelude fixture is not that shape (it carries exactly four
distinct states across 11,504+ checkpoints, per the review measurement
this cure round's own petition record already cites), so no truncation of
it, at any prefix or depth, can regress at `32352c15` for the reason
criterion (2) names. Every other clause of criterion (2) -- the byte
bound and the exact render, across every prefix, fixture, index kind and
surface this module actually parametrizes over -- is asserted below and
holds at HEAD.
"""

from __future__ import annotations

import json

import pytest

from ralphd import render_index
from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines

from test_d1_real_pirunner_active_iteration_reads_within_cap import _fixture_lines
from test_log_render_bounded_tail import _count_transcript_bytes  # noqa: F401 (re-exported via _measured_call)
from test_tail_whole_call_read_bound import _FROZEN_CAP, _measured_call

# PRD Q's own canonical fixture, then both of its named silent-prelude
# depths -- byte-for-byte the same three combinations
# `test_tail_whole_call_read_bound.py`'s own `_FIXTURES` names.
_FIXTURES = [(0, 1_000), (1_000, 10_000), (1_700, 10_000)]

_GAP_LINE = json.dumps(
    {"type": "message_end", "message": {"content": []}, "padding": "x" * 1000})

_PERCENTAGES = (35, 60, 83, 97)
_PREFIX_NAMES = ("minus_final_line",) + tuple(f"{p}pct" for p in _PERCENTAGES)
_TAIL_N = 2


def _prefix_count(total: int, prefix_name: str) -> int:
    """The raw line count `prefix_name` truncates `total` lines to --
    strictly fewer than the fixture's own full line count in every case,
    per this task's own criterion (1)."""
    if prefix_name == "minus_final_line":
        count = total - 1
    else:
        pct = int(prefix_name[:-3])
        count = int(total * pct / 100)
    assert 1 <= count < total, (prefix_name, total, count)
    return count


def _write_meta_still_running(itdir) -> None:
    """No `endedAt` at all -- the exact shape `engine/loop.py` leaves on
    disk before the runner has returned (see `test_d1_real_pirunner_
    active_iteration_reads_within_cap.py`'s own identical `meta`), so
    both index variants below describe a genuinely still-active
    iteration, never a finished one."""
    meta = {"number": 1, "phase": "worker", "model": "stub-model", "approach": 1,
           "startedAt": "2025-01-01T00:00:00Z"}
    (itdir / "meta.json").write_text(json.dumps(meta))


def _write_live_published_index(itdir, transcript, prefix_lines: list[str]) -> None:
    """Feed `prefix_lines` through the real incremental writer exactly as
    `engine/runner.py`'s `pump()` does for every raw line -- append then
    flush, one line at a time -- with NO final forced flush: this
    prefix's own writer state is left exactly as a genuinely still-
    running iteration's own on-disk index would be at this exact byte,
    lagging MAIN's own geometric publish schedule by however much
    `render_index.tail.ndjson`'s fixed cadence has not yet closed."""
    transcript.touch()
    live = render_index.LiveRenderIndex()
    with open(transcript, "a") as out:
        for line in prefix_lines:
            live.append(out, line + "\n", transcript)
            live.flush(itdir)


def _write_rebuilt_index(itdir, transcript, prefix_lines: list[str]) -> None:
    """The from-scratch rebuild over the identical bytes -- what a reader
    would see had the engine's end-of-iteration bookkeeping already run
    for this exact prefix."""
    transcript.write_text("".join(line + "\n" for line in prefix_lines))
    render_index.write_index(itdir)


def _build_case(tmp_path, prelude_count: int, gap_count: int, prefix_name: str,
                index_kind: str):
    """Builds one (fixture, prefix, index_kind) case's own run directory
    and returns `(reg, run_root, workspace_root, full)`, `full` being the
    full renderer's own lines over the EXACT SAME truncated prefix (no
    merge boundaries: this iteration has no `endedAt`, and none of these
    prefixes are short enough for `bounded_tail`'s own backward walk to
    reach byte 0 and synthesize a "start" boundary either, so a plain
    `render_to_lines` over the prefix's own bytes is exactly what
    `bounded_tail` must agree with)."""
    text = f"live-logs-prefix prelude={prelude_count} gap={gap_count}"
    full_lines = _fixture_lines(text, _GAP_LINE, gap_count, prelude_count)
    count = _prefix_count(len(full_lines), prefix_name)
    prefix_lines = full_lines[:count]

    label = f"{prelude_count}-{gap_count}-{prefix_name}-{index_kind}"
    reg = tmp_path / f"registry-{label}"
    run_root = reg / "runs" / "probe"
    itdir = run_root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    _write_meta_still_running(itdir)
    transcript = itdir / "output.jsonl"

    if index_kind == "live":
        _write_live_published_index(itdir, transcript, prefix_lines)
    else:
        _write_rebuilt_index(itdir, transcript, prefix_lines)

    assert render_index.load_index(itdir) is not None, (prelude_count, gap_count,
                                                        prefix_name, index_kind)

    full_text = "".join(line + "\n" for line in prefix_lines)
    full = render_to_lines(full_text, tty=False, state=new_render_state())
    workspace_root = tmp_path / f"workspace-{label}"
    return reg, run_root, workspace_root, full


@pytest.mark.parametrize("surface", ["live", "disk"])
@pytest.mark.parametrize("index_kind", ["live", "rebuilt"])
@pytest.mark.parametrize("prefix_name", _PREFIX_NAMES)
@pytest.mark.parametrize("prelude_count,gap_count", _FIXTURES)
def test_live_logs_prefix_stays_within_the_bound_and_renders_exactly(
        tmp_path, monkeypatch, prelude_count, gap_count, prefix_name, index_kind,
        surface):
    """Every named prefix of every named fixture, on both `/logs` surfaces,
    first and a repeated request, for both the live writer's own published
    index and a fresh rebuild over the identical bytes: at most 1,000,000
    bytes read per call (index and side-state bytes included), and the
    rendered lines equal the full renderer's own last `N` lines of that
    SAME truncated prefix exactly."""
    reg, run_root, workspace_root, full = _build_case(
        tmp_path, prelude_count, gap_count, prefix_name, index_kind)
    assert len(full) >= _TAIL_N, full
    ui_server._drop_run_detail_caches("probe")

    for attempt in ("first", "repeated"):
        read, live, lines = _measured_call(
            monkeypatch, surface, reg, run_root, workspace_root, _TAIL_N)
        assert lines == full[-_TAIL_N:], (
            prelude_count, gap_count, prefix_name, index_kind, surface, attempt,
            lines, full[-_TAIL_N:])
        assert live is (surface == "live"), (
            prelude_count, gap_count, prefix_name, index_kind, surface, attempt)
        assert read <= _FROZEN_CAP, (
            prelude_count, gap_count, prefix_name, index_kind, surface, attempt, read)
