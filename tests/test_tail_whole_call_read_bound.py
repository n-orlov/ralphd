"""Task 013 (v0.27 PRD D.1, #275): a whole tail call reads at most
1,000,000 bytes -- the transcript, `render_index.json` and every other
side-state file included -- through the real client-facing call
(`ui_server.rendered_log_lines`), on the live `/logs` surface and the
on-disk fallback, on the first and a repeated request, for PRD Q's own
canonical fixture (`gap_count=1,000`, no prelude) and both of its
silent-prelude depths (`gap_count=10,000`, prelude 1,000/1,700), and the
render equals the full renderer's last N lines exactly.

The counter is `test_log_render_bounded_tail._count_transcript_bytes`,
which counts every byte read off every file the call opens -- unlike
`test_log_merge_bounded_reads.py`'s own counter, which filters to
`output.jsonl` by name and so never saw a byte of `render_index.json`.

`render_index.bounded_tail`'s `load_index` reads `render_index.json` in
full on every call (it validates the whole document before trusting any
of it), so the file's own size is part of the bound. The 0.26 on-disk
shape -- one JSON object per raw transcript line, each carrying its own
full copy of the render state -- cost ~3 MB for the silent-prelude
fixtures (12,000+ raw lines but only a handful of distinct states), three
times the bound, on every call. The compact encoding
(`render_index.encode_index`: each distinct state stored once, the
checkpoints as parallel integer columns) keeps the same one-checkpoint-
per-raw-line density and the same exact render at ~20 bytes a line.
`test_the_0_26_dense_index_shape_alone_exceeds_the_bound` below shows the
fixtures really do exercise that cost: the 0.26 document for the same
checkpoints is itself over the bound before a single transcript byte is
read, so the 0.26 reader fails the bounded test.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from ralphd import collapse_index, render_index
from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.log_merge import OUTPUT_FILENAME, merged_lines

from test_log_merge_bounded_reads import (
    _api_logs_text,
    _write_iteration_with_recognized_nonrendering_gap,
)
from test_log_render_bounded_tail import _count_transcript_bytes

_FROZEN_CAP = 1_000_000  # PRD C.2/G.2/D.1: never rises in this wave.

_GAP_LINE = json.dumps(
    {"type": "message_end", "message": {"content": []}, "padding": "x" * 1000})

# (prelude_count, gap_count): PRD Q's canonical fixture, then both
# silent-prelude depths.
_FIXTURES = [(0, 1_000), (1_000, 10_000), (1_700, 10_000)]


def _write_and_index(reg_root, prelude_count: int, gap_count: int):
    """Builds the fixture, writes BOTH the render index and the legacy
    collapse index the engine itself carries for a finished iteration,
    and returns `(run_root, full_render_lines)`."""
    run_root = reg_root / "runs" / "probe"
    text = f"whole-call-read-bound prelude={prelude_count} gap={gap_count}"
    _write_iteration_with_recognized_nonrendering_gap(
        run_root, 1, text, _GAP_LINE, gap_count, prelude_count=prelude_count)
    itdir = run_root / "iterations" / "0001"
    collapse_index.write_index(itdir)
    render_index.write_index(itdir)
    assert render_index.load_index(itdir) is not None
    full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                           state=new_render_state())
    return run_root, full


def _wire(monkeypatch, surface, run_root, workspace_root):
    if surface == "disk":
        monkeypatch.setattr(ui_server, "_proxy_text", lambda *a, **k: (False, ""))
        return
    workspace_root.mkdir(exist_ok=True)

    def proxy(reg, run_id, path, **kw):
        n = int(re.search(r"tail=(\d+)", path).group(1))
        return True, asyncio.run(_api_logs_text(run_root, workspace_root, n))

    monkeypatch.setattr(ui_server, "_proxy_text", proxy)


def _measured_call(monkeypatch, surface, reg, run_root, workspace_root, n):
    """One `ui_server.rendered_log_lines` call, every byte it reads off
    any file counted; returns `(bytes_read, live, lines)`."""
    _wire(monkeypatch, surface, run_root, workspace_root)
    counts = _count_transcript_bytes(monkeypatch)
    try:
        live, lines = ui_server.rendered_log_lines(reg, "probe", n)
    finally:
        monkeypatch.undo()
    return counts[0], live, lines


@pytest.mark.parametrize("surface", ["disk", "live"])
@pytest.mark.parametrize("prelude_count,gap_count", _FIXTURES)
def test_whole_call_reads_at_most_the_bound_and_renders_exactly(
        tmp_path, monkeypatch, prelude_count, gap_count, surface):
    """Every fixture, both surfaces, first and repeated request: at most
    1,000,000 bytes read per call, index and side-state files included,
    and the lines returned equal the full renderer's last N exactly."""
    reg = tmp_path / "registry"
    run_root, full = _write_and_index(reg, prelude_count, gap_count)
    workspace_root = tmp_path / "workspace"
    assert len(full) >= 2, full
    ui_server._drop_run_detail_caches("probe")
    for n in (2, 1):
        for attempt in ("first", "repeated"):
            read, live, lines = _measured_call(
                monkeypatch, surface, reg, run_root, workspace_root, n)
            assert lines == full[-n:], (surface, attempt, n, lines, full[-n:])
            assert live is (surface == "live")
            assert read <= _FROZEN_CAP, (prelude_count, surface, attempt, n, read)


@pytest.mark.parametrize("prelude_count,gap_count", _FIXTURES[1:])
def test_the_0_26_dense_index_shape_alone_exceeds_the_bound(
        tmp_path, prelude_count, gap_count):
    """The silent-prelude fixtures really do exercise the index-read cost:
    the 0.26 on-disk document for the SAME checkpoints (one object per
    raw line, each with its own full state -- the in-memory shape
    serialised as is) is over the 1,000,000-byte bound by itself, so a
    reader that loads it whole cannot pass the test above. The compact
    file the engine now writes carries every one of those checkpoints
    (same count, same values once decoded) well under the bound."""
    run_root, _ = _write_and_index(tmp_path / "registry", prelude_count, gap_count)
    itdir = run_root / "iterations" / "0001"
    in_memory = render_index.compute_render_index(itdir / OUTPUT_FILENAME)
    dense = json.dumps(in_memory)
    compact = (itdir / render_index.INDEX_FILENAME).read_text()
    assert len(dense.encode()) > _FROZEN_CAP, len(dense)
    assert len(compact.encode()) < _FROZEN_CAP // 2, len(compact)
    loaded = render_index.load_index(itdir)
    assert loaded is not None
    assert len(loaded["checkpoints"]) == prelude_count + gap_count + 504
    assert loaded["checkpoints"] == in_memory["checkpoints"]
