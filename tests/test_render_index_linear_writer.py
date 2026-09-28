"""Task 012 (PRD v0.27 requirement D.2, #275): the live-index writer's own
total write cost, across a whole iteration, grows LINEARLY in the
transcript's own line count, not quadratically.

Before this task, `LiveRenderIndex.flush` rewrote `render_index.json` from
scratch with the WHOLE accumulated snapshot on every periodic publish
(`LIVE_FLUSH_EVERY_LINES`), so each of the ~n / `LIVE_FLUSH_EVERY_LINES`
publishes cost O(total checkpoints so far) and the SUM across a whole
iteration was quadratic in the transcript's own line count -- PRD D.2's own
measured figures: 1,300, 4,600 and 17,200 checkpoint records serialised in
total at 200, 400 and 800 transcript lines (`tests/test_render_index_live_
write_cost.py`, retired by this task -- its whole point was pinning that
now-fixed shape; this module is its replacement, covering the corrected
behaviour, and both the bytes and the RECORD count it pinned).

The fix (`render_index._next_flush_threshold`): `flush` now schedules its
next publish geometrically -- after publishing at `total` checkpoints, the
next publish is not due again until `total` has doubled (or grown by the
base batch size `LIVE_FLUSH_EVERY_LINES`, whichever asks for more lines
first). Publishing at `every, 2*every, 4*every, ...` sums to a geometric
series bounded by `2 * N`, so both the total checkpoint RECORDS written and
the total BYTES written across every publish grow at most linearly (with a
small constant, ~2x) in the transcript's own final line count -- doubling
the line count at most doubles either measure, plus a small fixed
allowance for noise.

Mutation pin: reverting `_next_flush_threshold` to the old fixed-interval
schedule (`total + every`, dropping the `total * 2` doubling term) makes
both assertions below fail -- doubling `n` then roughly QUADRUPLES both
measures again (confirmed directly: at 200/400/800 lines the old schedule
serialises 1,300/4,600/17,200 records and ~331,763/1,170,983/4,373,423
bytes, matching PRD D.2's own figures and each ratio far past this test's
allowance).
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

from ralphd import render_index
from ralphd.engine.runner import PiRunner
from ralphd.log_merge import OUTPUT_FILENAME

# Fixed additive slack on top of "at most doubles" -- comfortably bigger
# than the real (~20-record / ~4-6 KB) noise the geometric schedule's own
# rounding leaves, but far smaller than what the old quadratic writer's
# ~4x growth would need to slip under.
_RECORD_ALLOWANCE = 200
_BYTE_ALLOWANCE = 20_000


def _fake_pi_emits_n_lines(tmp_path: Path, n: int, name: str) -> Path:
    """A `pi_bin` stand-in that consumes stdin, emits exactly `n` silent
    NDJSON lines, then exits normally -- no hang, so `PiRunner.run` returns
    on its own once the whole transcript has been fed through the real
    live-write path (`pump()`'s `live_index.append`/`.flush` calls)."""
    script = tmp_path / name
    lines = "\n".join(
        "echo '{\"type\": \"message_update\", \"assistantMessageEvent\": "
        "{\"type\": \"text_delta\", \"delta\": \"x\"}}'"
        for _ in range(n))
    script.write_text(f"#!/bin/sh\ncat >/dev/null\n{lines}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _run_and_measure(tmp_path: Path, n: int, label: str):
    """Run a real `PiRunner` against a script emitting exactly `n` silent
    lines to completion, instrumenting every write `LiveRenderIndex.flush`
    actually makes to `render_index.json`'s own tmp file, and return
    `(total_bytes_written, total_records_written, transcript_bytes,
    checkpoint_count)`. `total_records_written` sums, across every
    publish, the number of checkpoint objects that publish's own JSON
    payload carries -- each has exactly one `"byteOffset"` key, so
    counting that substring's occurrences counts checkpoints without
    parsing JSON on every write."""
    itdir = tmp_path / label / "iterations" / "0001"
    itdir.mkdir(parents=True)
    transcript = itdir / OUTPUT_FILENAME
    script = _fake_pi_emits_n_lines(tmp_path, n, f"fake-pi-{label}")
    runner = PiRunner(workspace=tmp_path, pi_bin=str(script))

    total_bytes = 0
    total_records = 0
    tmp_name = render_index.INDEX_FILENAME + ".tmp"
    original_write_text = Path.write_text

    def spy_write_text(self, data, *a, **kw):
        nonlocal total_bytes, total_records
        if self.name == tmp_name:
            total_bytes += len(data.encode("utf-8"))
            total_records += data.count('"byteOffset"')
        return original_write_text(self, data, *a, **kw)

    Path.write_text = spy_write_text
    try:
        asyncio.run(runner.run("prompt", transcript, timeout_s=60))
    finally:
        Path.write_text = original_write_text

    index = render_index.load_index(itdir)
    assert index is not None, "the live-written index must validate"
    return (total_bytes, total_records, index["transcriptBytes"],
            len(index["checkpoints"]))


class TestLiveWriteCostIsLinear:
    """D.2: the sum of every periodic flush's own write cost across a
    whole iteration grows at most LINEARLY (within a fixed additive
    allowance) in the transcript's own line count -- doubling the line
    count at most doubles both the total checkpoint records and the total
    bytes written, plus that allowance. Fails on the 0.26 writer (fixed
    every-N-lines full-snapshot rewrites), which quadruples both instead."""

    def test_records_and_bytes_written_grow_at_most_linearly(self, tmp_path):
        b200, r200, t200, c200 = _run_and_measure(tmp_path, 200, "n200")
        b400, r400, t400, c400 = _run_and_measure(tmp_path, 400, "n400")
        b800, r800, t800, c800 = _run_and_measure(tmp_path, 800, "n800")

        # Existing correctness is untouched: every run indexed all of its
        # own lines, and the transcript's own covered byte count grows.
        assert c200 == 200 and c400 == 400 and c800 == 800
        assert t200 < t400 < t800

        # Doubling n from 200 to 400, and from 400 to 800, must at most
        # DOUBLE the total records written and the total bytes written,
        # plus a small fixed allowance -- never the ~4x this same probe
        # measures against the old fixed-interval, full-rewrite schedule.
        assert r400 <= 2 * r200 + _RECORD_ALLOWANCE, (
            f"records written more than doubled: {r200} -> {r400}")
        assert r800 <= 2 * r400 + _RECORD_ALLOWANCE, (
            f"records written more than doubled: {r400} -> {r800}")
        assert b400 <= 2 * b200 + _BYTE_ALLOWANCE, (
            f"bytes written more than doubled: {b200} -> {b400}")
        assert b800 <= 2 * b400 + _BYTE_ALLOWANCE, (
            f"bytes written more than doubled: {b400} -> {b800}")
