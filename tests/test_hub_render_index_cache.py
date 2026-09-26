"""Task 015 (PRD C.4, #271/#272): the absent-index cost is paid ONCE per
iteration.

C.3's own fallback (task 014) is correct but unbounded: with neither
`render_index.json` nor `collapse_index.json` present, `ui_server.
rendered_log_lines` must scan back through the transcript (the growth-based
`_fetch_live_tail`/`_fetch_dead_tail` loop) to resolve a disclosed-unresolved
collapse seed, and that scan pays for the WHOLE remaining backlog on PRD Q's
own silent-prelude fixtures (measured, pre-cure, at ~10.7-12.5M bytes even at
its own theoretical floor -- `tests/test_hub_real_render_cap_whole_call.py`'s
own docstring). Before this task, a SECOND request against the exact same,
still-absent-index iteration paid that scan again, every time -- the hub
never remembered what the first scan had already learned.

This task adds a hub-owned, in-memory cache (`ui_server._ABSENT_INDEX_CACHE`)
keyed by `(run id, iteration number, transcript byte size)`: the growth-based
scan, once it resolves, also builds this iteration's own render index
straight off its on-disk transcript (`render_index.compute_render_index` --
the SAME from-scratch builder the engine's own `render_index.write_index`
uses) and stores it here -- never into the run dir, which the hub only reads
and does not own. The next tail request against the SAME iteration and the
SAME transcript size is then served through `render_index.
bounded_tail(..., index=cached_index)` at the C.2 bound, exactly as if an
on-disk index had been there all along. Any transcript growth -- a new byte
appended, a new iteration started -- changes the cache key and the next
request scans (and recaches) again: this trades a repeat of the SAME query
for cost, never trades correctness for it.
"""

from __future__ import annotations

import json

from ralphd import collapse_index
from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.log_merge import merged_lines

from test_log_merge_bounded_reads import (
    _count_transcript_bytes,
    _write_iteration_with_recognized_nonrendering_gap,
)

_FROZEN_CAP = 1_000_000  # PRD C.2/G.2: never rises in this wave.

_GAP_LINE = json.dumps(
    {"type": "message_end", "message": {"content": []}, "padding": "x" * 1000})


def _wire_disk_only(monkeypatch):
    """Forces the dead-run on-disk fallback: the live proxy always fails,
    matching the "genuinely absent index" fixtures elsewhere in this suite
    (`tests/test_log_merge_bounded_reads.py`'s own `_wire` helper for the
    same fixture shape)."""
    monkeypatch.setattr(ui_server, "_proxy_text", lambda *a, **k: (False, ""))


def _snapshot_run_dir(run_root) -> dict[str, bytes]:
    """Every file under the run directory `run_root`, by relative path,
    mapped to its full byte content -- used to prove a scan never writes
    into, removes from, or rewrites (even at the same size) any file in a
    run dir the hub only reads. Directories are listed too (mapped to
    None), so a created empty directory also counts as a write."""
    run_dir = run_root
    return {
        str(p.relative_to(run_dir)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(run_dir.rglob("*"))
    }


def test_a_scan_of_an_absent_index_iteration_never_writes_into_the_run_dir(
        tmp_path, monkeypatch):
    reg = tmp_path / "registry"
    run_root = reg / "runs" / "probe"
    text = "run-dir-untouched streamed text"
    _write_iteration_with_recognized_nonrendering_gap(
        run_root, 1, text, _GAP_LINE, 10_000, prelude_count=1_000)
    itdir = run_root / "iterations" / "0001"
    assert not (itdir / "render_index.json").exists()
    assert not (itdir / collapse_index.INDEX_FILENAME).exists()

    ui_server._drop_run_detail_caches("probe")
    _wire_disk_only(monkeypatch)

    before = _snapshot_run_dir(run_root)
    live, lines = ui_server.rendered_log_lines(reg, "probe", 2)
    after = _snapshot_run_dir(run_root)

    assert live is False
    assert lines, lines
    assert after == before
    # The scan really did build a cache entry (not a no-op) -- otherwise
    # the "never writes into the run dir" assertion above would be true
    # for a trivial reason.
    assert "probe" in ui_server._ABSENT_INDEX_CACHE
    # And the snapshot really compares bytes, not just sizes: a same-size
    # rewrite of the transcript must make it differ.
    transcript = itdir / "output.jsonl"
    data = transcript.read_bytes()
    flipped = bytes([data[0] ^ 0x01]) + data[1:]
    transcript.write_bytes(flipped)
    assert len(flipped) == len(data)
    assert _snapshot_run_dir(run_root) != before


def test_second_call_on_an_absent_index_iteration_is_bounded_and_both_calls_render_exactly(
        tmp_path, monkeypatch):
    """PRD C.4's own test: two `ui_server.rendered_log_lines` calls on the
    absent-index silent-prelude fixture at both named depths (1,000 and
    1,700) -- the FIRST pays the scan (no byte cap: PRD C.3), the SECOND
    is served from the cache this task adds and must cost no more than
    the frozen 1,000,000-byte C.2 bound. Both calls render the exact
    full-render tail; fails at 9b72b700 (no such cache exists there, so
    the second call re-scans and re-pays the same unbounded cost as the
    first)."""
    for prelude_count in (1_000, 1_700):
        text = f"absent-index cache depth={prelude_count} must not replay"
        reg = tmp_path / f"prelude-{prelude_count}" / "registry"
        run_root = reg / "runs" / "probe"
        _write_iteration_with_recognized_nonrendering_gap(
            run_root, 1, text, _GAP_LINE, 10_000, prelude_count=prelude_count)
        itdir = run_root / "iterations" / "0001"
        assert not (itdir / "render_index.json").exists()
        assert not (itdir / collapse_index.INDEX_FILENAME).exists()

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert full[-2].strip() == "[thinking\u2026]", (prelude_count, full[-2:])

        ui_server._drop_run_detail_caches("probe")
        _wire_disk_only(monkeypatch)

        # Call 1: pays the scan. No byte cap (PRD C.3).
        live1, lines1 = ui_server.rendered_log_lines(reg, "probe", 2)
        assert live1 is False
        assert lines1 == full[-2:], (prelude_count, lines1)

        # Call 2: served from the cache this task adds.
        counts = _count_transcript_bytes(monkeypatch)
        live2, lines2 = ui_server.rendered_log_lines(reg, "probe", 2)
        measured = counts[0]
        monkeypatch.undo()
        _wire_disk_only(monkeypatch)

        assert live2 is False
        assert lines2 == full[-2:], (prelude_count, lines2)
        assert measured <= _FROZEN_CAP, (prelude_count, measured)


def test_appending_to_the_transcript_between_calls_changes_the_cache_key_and_the_render_stays_exact(
        tmp_path, monkeypatch):
    reg = tmp_path / "registry"
    run_root = reg / "runs" / "probe"
    text1 = "cache-key-change first pass text"
    _write_iteration_with_recognized_nonrendering_gap(
        run_root, 1, text1, _GAP_LINE, 1_000, prelude_count=200)
    itdir = run_root / "iterations" / "0001"
    output = itdir / "output.jsonl"
    assert not (itdir / "render_index.json").exists()

    ui_server._drop_run_detail_caches("probe")
    _wire_disk_only(monkeypatch)

    full1 = render_to_lines("".join(merged_lines(run_root)), tty=False,
                            state=new_render_state())
    _live1, lines1 = ui_server.rendered_log_lines(reg, "probe", 2)
    assert lines1 == full1[-2:]
    key1 = ui_server._ABSENT_INDEX_CACHE["probe"][0]
    assert key1[0] == 1  # this iteration's own number
    assert key1[1] == output.stat().st_size

    # Append more transcript directly to the SAME iteration's own
    # `output.jsonl` -- a tool call the render must show regardless of
    # this iteration's already-`textSeen` collapse state, so the tail
    # visibly changes -- without any index ever having been written for
    # it.
    tool_start = json.dumps({"type": "tool_execution_start", "toolCallId": "t1",
                             "toolName": "bash", "args": {"command": "ls -la"}})
    tool_end = json.dumps({"type": "tool_execution_end", "toolCallId": "t1",
                           "toolName": "bash", "isError": False,
                           "result": {"content": [{"type": "text", "text": "ok"}]}})
    with output.open("a") as f:
        f.write(tool_start + "\n")
        f.write(tool_end + "\n")
    assert not (itdir / "render_index.json").exists()

    full2 = render_to_lines("".join(merged_lines(run_root)), tty=False,
                            state=new_render_state())
    assert full2[-2:] != full1[-2:], (full1[-2:], full2[-2:])
    assert any("$ ls -la" in ln for ln in full2)

    _live2, lines2 = ui_server.rendered_log_lines(reg, "probe", 2)
    assert lines2 == full2[-2:], lines2
    key2 = ui_server._ABSENT_INDEX_CACHE["probe"][0]
    assert key2 != key1
    assert key2[1] == output.stat().st_size
