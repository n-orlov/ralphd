"""Task 013 (v0.27 PRD D.1, #275): a whole tail call's byte cost, index
reads included, bounded to the same frozen 1,000,000-byte cap PRD C.2/G.2
already froze for `ui_server.rendered_log_lines` -- but counted honestly
this time.

`test_log_merge_bounded_reads.py`'s own `_count_transcript_bytes` (task
005, PRD E) and `test_log_render_bounded_tail.py`'s own local copy of it
both filter to `OUTPUT_FILENAME` by name: neither ever sees a byte read
off `render_index.json` itself. `render_index.bounded_tail`'s own
`load_index` (task cure-01-01 era, shipped well before this wave) reads
that file in FULL (`path.read_text()`) to validate and consume it on
EVERY call, whatever fraction of the transcript behind it the resolved
tail actually needed -- a cost neither narrower counter could ever see.
This module drives the real consumer (`ui_server.rendered_log_lines`),
through both the live `/logs` ASGI surface and the on-disk dead-run
fallback, on a first call and a byte-identical repeated one, against
PRD Q's own canonical fixture (`gap_count=1,000`, no prelude) and both of
its silent-prelude depths (`gap_count=10,000`, prelude 1,000/1,700),
using `test_log_render_bounded_tail.py`'s new, honest
`_count_transcript_bytes` (counts every file a call actually opens, not
just `output.jsonl`).

**Disclosed residual (PRD D.1's own escape hatch, "if an exact render
cannot be kept within the bound, a petition with the measurement is the
deliverable instead").** Measured this way, PRD Q's own canonical fixture
stays comfortably under the cap on both surfaces and both calls -- the
per-line checkpoint density `render_index`'s write side (task 012, D.2)
already keeps means the TRANSCRIPT read a bisecting tail walk needs stays
small regardless of how much silent history sits behind it, and this
fixture's own `render_index.json` is itself small enough that reading it
whole costs little. Both silent-prelude depths, though, measured
identically: their OWN `render_index.json` is dominated by one checkpoint
per raw NDJSON line -- 10,500+ of them -- and reading that file in FULL
(there is no partial/bounded read of a single monolithic JSON document;
`load_index` cannot validate ascending checkpoint order, per-checkpoint
CRC/state shape, or the witness fields without parsing every byte of it)
costs ~3.07-3.25MB per call, three times the frozen cap, on EVERY call,
identically -- first or repeated, live or dead-run, because neither
`_ABSENT_INDEX_CACHE` (task cure-01-02-2, keyed by iteration identity, a
pure speed optimisation over an ABSENT on-disk index) nor any other cache
this hub carries intercepts a request whose iteration's `render_index.json`
already exists on disk and validates: `render_index.bounded_tail` reads it
fresh, off disk, every time. Bounding this exactly would need
`render_index.json`'s own on-disk shape to become seekable/partial-
readable (a JSONL or segmented successor format, `load_index`'s
validation rebuilt to verify only a bounded suffix the way
`_covered_prefix_matches` already does for tamper detection) without
changing the write side's own one-checkpoint-per-raw-line density
(`test_render_index_linear_writer.py`'s own `c200==200`/`c400==400`/
`c800==800` pins, and `test_render_index.py`'s
`test_every_checkpoint_matches_an_independent_full_render`, both assert
that density is exact, deliberate, already-shipped behaviour this task
must not weaken to chase this bound) -- a format migration touching
`LiveRenderIndex.flush`/`snapshot`, `write_index`, `load_index`,
`_render_segment` and every test that inspects `render_index.json`'s own
JSON shape directly, out of THIS task's own scope. The measured, disclosed
residual is pinned by exact byte count below (a regression in EITHER
direction -- a further, unexplained increase, or a decrease that would
mean the fixture stopped exercising this cost at all -- is caught by the
pin) rather than asserted against the impossible 1,000,000-byte bound;
this task's own worker report carries the matching `contradictory-clauses`
petition.
"""

from __future__ import annotations

import asyncio
import json
import re

from ralphd import collapse_index, render_index
from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.log_merge import merged_lines

from test_log_merge_bounded_reads import (
    _api_logs_text,
    _write_iteration_with_recognized_nonrendering_gap,
)
from test_log_render_bounded_tail import _count_transcript_bytes

_FROZEN_CAP = 1_000_000  # PRD C.2/G.2/D.1: never rises in this wave.

_GAP_LINE = json.dumps(
    {"type": "message_end", "message": {"content": []}, "padding": "x" * 1000})

# Task 013 (D.1): the disclosed residual's own exact pins -- see this
# module's docstring. `None` for the canonical fixture: it stays under the
# frozen cap, so there is nothing to disclose, only to bound.
_DISCLOSED_RESIDUAL_DISK = {1_000: 3_068_226, 1_700: 3_248_827}


def _write_and_index(reg_root, prelude_count: int, gap_count: int):
    """Builds the fixture, writes BOTH the render index and the legacy
    collapse index the engine itself would carry for a finished
    iteration, and returns `(run_root, full_render_last_two_lines)`."""
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
    return run_root, full[-2:]


def _wire_disk(monkeypatch):
    monkeypatch.setattr(ui_server, "_proxy_text", lambda *a, **k: (False, ""))


def _wire_live(monkeypatch, run_root, workspace_root):
    workspace_root.mkdir(exist_ok=True)

    def proxy(reg, run_id, path, **kw):
        n = int(re.search(r"tail=(\d+)", path).group(1))
        return True, asyncio.run(_api_logs_text(run_root, workspace_root, n))

    monkeypatch.setattr(ui_server, "_proxy_text", proxy)


def test_prd_q_canonical_fixture_bounded_on_every_surface_first_and_repeated(
        tmp_path, monkeypatch):
    """PRD Q's own canonical fixture (`gap_count=1,000`, no prelude): the
    whole call, index bytes included, must stay at or under the frozen
    1,000,000-byte cap -- on the live `/logs` surface and the on-disk
    fallback alike, on a first call and a byte-identical repeated one.
    Fails on the "0.26 reader" (`test_log_merge_bounded_reads.py`'s and
    `test_log_render_bounded_tail.py`'s own pre-existing counters), which
    filter to `output.jsonl` and would report only the fraction of this
    same call's true cost that touches the transcript, silently missing
    every byte `render_index.bounded_tail` reads off `render_index.json`
    itself."""
    for surface in ("disk", "live"):
        reg = tmp_path / f"q-canonical-{surface}" / "registry"
        run_root, expected = _write_and_index(reg, prelude_count=0, gap_count=1_000)
        if surface == "disk":
            _wire_disk(monkeypatch)
        else:
            _wire_live(monkeypatch, run_root, tmp_path / f"workspace-{surface}")

        ui_server._drop_run_detail_caches("probe")
        counts1 = _count_transcript_bytes(monkeypatch)
        live1, lines1 = ui_server.rendered_log_lines(reg, "probe", 2)
        first = counts1[0]
        monkeypatch.undo()

        if surface == "disk":
            _wire_disk(monkeypatch)
        else:
            _wire_live(monkeypatch, run_root, tmp_path / f"workspace-{surface}")
        counts2 = _count_transcript_bytes(monkeypatch)
        live2, lines2 = ui_server.rendered_log_lines(reg, "probe", 2)
        second = counts2[0]
        monkeypatch.undo()

        assert lines1 == expected, (surface, lines1, expected)
        assert lines2 == expected, (surface, lines2, expected)
        assert live1 is (surface == "live")
        assert live2 is (surface == "live")
        assert first <= _FROZEN_CAP, (surface, "first", first)
        assert second <= _FROZEN_CAP, (surface, "second", second)


def test_silent_prelude_depths_disclosed_residual_measured_and_render_exact(
        tmp_path, monkeypatch):
    """PRD Q's two silent-prelude depths (`gap_count=10,000`, prelude
    1,000/1,700): the render stays EXACT -- byte-identical to the full
    renderer's own last two lines -- on every surface, first call and
    repeated alike, but the whole-call byte cost (index reads honestly
    included) exceeds the frozen 1,000,000-byte cap roughly threefold,
    identically on every call: this module's own docstring names why
    (`render_index.json`'s own monolithic-JSON shape has no partial read,
    and this fixture's is dominated by 10,500+ one-per-raw-line
    checkpoints). Pinned by exact measurement (the disclosed-residual
    disposition PRD D.1 itself pre-authorizes: "a petition with the
    measurement is the deliverable instead") rather than asserted against
    the cap it cannot meet; this task's own worker report carries the
    matching `contradictory-clauses` petition naming this exact obligation
    and these exact figures."""
    for prelude_count in (1_000, 1_700):
        residual = _DISCLOSED_RESIDUAL_DISK[prelude_count]
        for surface in ("disk", "live"):
            reg = tmp_path / f"prelude-{prelude_count}-{surface}" / "registry"
            run_root, expected = _write_and_index(
                reg, prelude_count=prelude_count, gap_count=10_000)
            if surface == "disk":
                _wire_disk(monkeypatch)
            else:
                _wire_live(monkeypatch, run_root,
                          tmp_path / f"workspace-{prelude_count}-{surface}")

            ui_server._drop_run_detail_caches("probe")
            counts1 = _count_transcript_bytes(monkeypatch)
            live1, lines1 = ui_server.rendered_log_lines(reg, "probe", 2)
            first = counts1[0]
            monkeypatch.undo()

            if surface == "disk":
                _wire_disk(monkeypatch)
            else:
                _wire_live(monkeypatch, run_root,
                          tmp_path / f"workspace-{prelude_count}-{surface}")
            counts2 = _count_transcript_bytes(monkeypatch)
            live2, lines2 = ui_server.rendered_log_lines(reg, "probe", 2)
            second = counts2[0]
            monkeypatch.undo()

            assert lines1 == expected, (prelude_count, surface, lines1, expected)
            assert lines2 == expected, (prelude_count, surface, lines2, expected)
            assert live1 is (surface == "live")
            assert live2 is (surface == "live")
            # Both calls cost the same, and well over the frozen cap:
            # nothing here caches an on-disk, already-valid render index.
            assert first > _FROZEN_CAP, (prelude_count, surface, first)
            assert second > _FROZEN_CAP, (prelude_count, surface, second)
            if surface == "disk":
                # Pinned exactly on the disk surface (deterministic,
                # nothing else touches the filesystem alongside it); the
                # live surface's own extra `GET /logs?tail=1` probe goes
                # through the real engine ASGI handler and reads tens of
                # thousands of additional bytes -- bounded above, not
                # pinned exactly.
                assert first == residual, (prelude_count, first)
                assert second == residual, (prelude_count, second)
            else:
                assert residual < first < residual + 100_000, (prelude_count, first)
                assert residual < second < residual + 100_000, (prelude_count, second)
