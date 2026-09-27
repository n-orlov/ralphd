"""cure-01-01-10 (D.1, DoD 4): probe `render_index.py`'s own live write
path (`engine/runner.py`'s `pump()` -> `LiveRenderIndex.append`/`flush`)
through a real `PiRunner`, and pin its actual per-iteration write cost as
QUADRATIC in the transcript's own line count -- the shape
`docs/architecture.md`'s "Write timing" paragraph must describe, not the
non-quadratic claim it made before this task.

`LiveRenderIndex.flush` rewrites `render_index.json` from scratch with
EVERY checkpoint fed so far (`snapshot()` -> `json.dumps` of the full
`self._checkpoints` list), not just the newly-fed batch. Batching
(`LIVE_FLUSH_EVERY_LINES`) means that rewrite happens only once every 20
lines instead of on every single line, but each occurrence still costs
O(total checkpoints so far) -- so the SUM of every flush's own cost across
a whole iteration is quadratic in the transcript's own line count, not
linear/non-quadratic. Doubling the line count roughly QUADRUPLES the total
bytes written across every flush, not merely doubling it.

Mutation pin: this module fails at 9b72b700-shaped code (no `render_index`
module existed there at all -- the assertions below cannot even import).
It also directly contradicts the OLD `docs/architecture.md` wording this
task corrects: reverting `LiveRenderIndex.flush` to write only the newly
fed batch instead of the full accumulated snapshot each time (making the
write path genuinely non-quadratic, and true to the old doc) would flip
the ratio assertions below from passing to failing, because the growth
would then be linear (~2x, not >3x) when doubling the line count.
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

from ralphd import render_index
from ralphd.engine.runner import PiRunner
from ralphd.log_merge import OUTPUT_FILENAME


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


def _run_and_total_flush_bytes(tmp_path: Path, n: int, label: str):
    """Run a real `PiRunner` against a script emitting exactly `n` silent
    lines to completion, instrumenting every byte `LiveRenderIndex.flush`
    actually writes to `render_index.json`'s own tmp file along the way
    (never `engine/loop.py`'s separate end-of-iteration rebuild -- this
    isolated `PiRunner.run` call never reaches `loop.py` at all), and
    return `(total_bytes_written, transcript_bytes, checkpoint_count)`."""
    itdir = tmp_path / label / "iterations" / "0001"
    itdir.mkdir(parents=True)
    transcript = itdir / OUTPUT_FILENAME
    script = _fake_pi_emits_n_lines(tmp_path, n, f"fake-pi-{label}")
    runner = PiRunner(workspace=tmp_path, pi_bin=str(script))

    total_bytes = 0
    tmp_name = render_index.INDEX_FILENAME + ".tmp"
    original_write_text = Path.write_text

    def spy_write_text(self, data, *a, **kw):
        nonlocal total_bytes
        if self.name == tmp_name:
            total_bytes += len(data.encode("utf-8"))
        return original_write_text(self, data, *a, **kw)

    Path.write_text = spy_write_text
    try:
        asyncio.run(runner.run("prompt", transcript, timeout_s=60))
    finally:
        Path.write_text = original_write_text

    index = render_index.load_index(itdir)
    assert index is not None, "the live-written index must validate"
    return total_bytes, index["transcriptBytes"], len(index["checkpoints"])


class TestLiveWriteCostIsQuadratic:
    """DoD 4 / D.1: the sum of every periodic flush's own write cost across
    a whole iteration grows QUADRATICALLY in the transcript's own line
    count, batching notwithstanding -- so the architecture doc's "Write
    timing" paragraph may describe what batching buys (bounded staleness
    of the uncovered suffix, not reduced total write cost) but must not
    claim non-quadratic total write cost. Alongside the cost measurement,
    also pins that batching changes nothing about index CORRECTNESS or the
    transcript's own bytes: every line is still indexed and the transcript
    is untouched by the instrumentation."""

    def test_live_index_total_flush_bytes_grow_quadratically_not_linearly(
            self, tmp_path):
        b200, t200, c200 = _run_and_total_flush_bytes(tmp_path, 200, "n200")
        b400, t400, c400 = _run_and_total_flush_bytes(tmp_path, 400, "n400")
        b800, t800, c800 = _run_and_total_flush_bytes(tmp_path, 800, "n800")

        # Existing correctness is untouched: every run indexed all of its
        # own lines (checkpoint counts double exactly as line counts
        # double), and the transcript's own covered byte count grows too.
        assert c200 == 200 and c400 == 400 and c800 == 800
        assert t200 < t400 < t800

        # If total write cost were LINEAR (the shape the old doc claimed),
        # doubling n would roughly double the total bytes written across
        # every flush. It does not: it roughly QUADRUPLES, because each of
        # the ~n / LIVE_FLUSH_EVERY_LINES flushes rewrites the FULL
        # accumulated snapshot, not just its own newly fed batch.
        ratio_400_200 = b400 / b200
        ratio_800_400 = b800 / b400
        assert ratio_400_200 > 3.0, (
            "expected super-linear (~4x) growth doubling n, got "
            f"{ratio_400_200:.2f}x ({b200} -> {b400} bytes)")
        assert ratio_800_400 > 3.0, (
            "expected super-linear (~4x) growth doubling n, got "
            f"{ratio_800_400:.2f}x ({b400} -> {b800} bytes)")
