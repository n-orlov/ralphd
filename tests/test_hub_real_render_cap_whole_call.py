"""Task cure-01-08 (PRD G.2/G.6/G.9, DoD 7, non-negotiable 2): the WHOLE
client-facing tail render call, not just its cheapest inner fetch.

`tests/test_g4_frozen_cap_measurements.py` (task 022) pinned five figures
against `log_merge.merged_lines`/`_grown_rendered_tail` -- a growth loop
that TEST HELPER reimplements, independently of the real consumer. G.9's
own shipped test measured that same shape, which is exactly the gap this
finding names: the real consumer path, `ui_server.rendered_log_lines`
(both its live `/logs` ASGI-handler branch and its dead-run on-disk-merge
fallback), wraps `log_merge`'s bounded per-call read in its OWN outer
doubling loop (`_fetch_live_tail`/`_fetch_dead_tail`) -- and that outer
loop's own doubling cost was never measured by anything upstream of this
task. Measured here, through the real call (never `_grown_rendered_tail`,
never a hand-rolled stand-in), on every fixture PRD G.9 names:

  - PRD P's three interleaved-suffix fixtures (unshifted, shifted,
    shifted-and-padded) and the PRD-intro 200-line-tail-grown-10-to-20,000
    fixture: unaffected by the outer doubling loop at all (each resolves
    within the very first or second fetch), so they read exactly the same
    131,072 / 131,072 / 131,072 / 65,536 bytes `test_g4_frozen_cap_
    measurements.py` already pinned at the inner-fetch level -- the whole
    call costs no more than its one deciding fetch, under the frozen
    1,000,000-byte cap, through BOTH the live and dead-run surfaces.

  - PRD Q's own canonical fixture (`gap_count=1,000`, no prelude) and both
    silent-prelude depths (`gap_count=10,000`, prelude 1,000/1,700): the
    real consumer's outer doubling loop pays for the SUM of every smaller
    doubling step on top of the one read that actually resolves the
    render, because a `thinking_seen` transition's own PRINT ("[thinking…]")
    happens at the setter line itself -- never at a seeded restart boundary
    -- so a render whose deciding transition sits behind a long RECOGNIZED-
    but-SILENT flood (PRD Q's own shape) cannot resolve without the raw
    window actually reaching that transition line, however cheaply the
    O(1) collapse-index lookup answers every offset short of it. Measured
    at this task's own commit, BEFORE its cure: 4,398,462 / 38,087,918 /
    40,221,918 / 41,715,718 bytes -- the SUM-of-doublings waste on top of
    each fixture's own theoretical floor (a single non-doubling read of
    the whole iteration: 1,117,887 / 10,720,887 / 11,787,887 / 12,534,787
    bytes respectively). This task's own cure
    (`_TAIL_GROWTH_ROUNDS_BEFORE_FULL_ASK`, `ui_server.py`) removes that
    doubling-ladder SUM once a request has already doubled this many times
    without resolving, asking once more for effectively the whole
    remaining backlog instead of paying for another round -- corrected
    figures pinned below, roughly a third of the pre-cure measurement and
    within ~1.05x-~1.3x of each fixture's own theoretical floor (the
    residual gap is `_tail_lines_of_file`'s own internal, pre-existing
    chunk-doubling inside that ONE final read, PRD G.6's shipped shape,
    out of THIS task's scope to touch).

  Even the fully-cured, no-further-waste theoretical floor for every one
  of Q's four fixtures is itself over the frozen 1,000,000-byte cap (by
  1.12x on the smallest of them) -- PRD Q's own `padding='x'*1000` alone
  puts more than a megabyte of RECOGNIZED-but-silent raw bytes between a
  `thinking_start` and the file's own end, and that padding must actually
  be READ (there is no way to know a `message_end`'s content is empty/
  unrecognized -- Q's own recognized-silent property -- without reading
  the line that carries it) for the render to stay correct. No further
  reduction is available under the frozen cap, the frozen transcript
  format, an unweakened equality assertion, or without a further side
  index (a per-segment "renders nothing further" marker would be exactly
  that: a side index for something other than prefix-collapse state, PRD
  G.7/the wave's own prohibition list). Ruling 1 section 2's disclosed-
  residual disposition applies to these four fixtures: the corrected
  whole-call byte counts are pinned below, and every policy this task
  ruled out is named in its own commit message alongside the filed issue.

Task cure-01-09 (PRD G.4, DoD 7, #272) published its own disclosed residual
on this same module's terms -- the GENUINELY ABSENT half of it (a cheap,
under-cap, but WRONG render, replaying already-streamed text in place of
the full render's own suffix) is CURED by task 014: `ui_server.
rendered_log_lines`'s own outer growth loop (`_fetch_live_tail`/
`_fetch_dead_tail`) now refuses to settle for a fetch whose collapse seed
still carries `unresolved` (PRD Q.1's own disclosure) merely because it
already has enough rendered lines, and keeps growing until either the flag
resolves or the fetch has read this iteration's entire transcript --
`test_silent_prelude_case_renders_the_full_renderers_last_two_lines_on_
both_real_paths` (`tests/test_log_merge_bounded_reads.py`) pins the
corrected, always-correct render at both silent-prelude depths through
`ui_server.rendered_log_lines` itself. `log_merge`'s own generic
bounded-read contract (`merge_entries_tail`/`merged_lines`, read directly
rather than through `ui_server`) is UNCHANGED by task 014 and keeps this
residual exactly as cure-01-09 disclosed it.
"""

from __future__ import annotations

import json

from ralphd import collapse_index, render_index
from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.log_merge import merged_lines

from test_log_merge_bounded_reads import (
    _api_logs_text,
    _count_transcript_bytes,
    _write_iteration_with_ignored_head,
    _write_iteration_with_interleaved_ignored_suffix,
    _write_iteration_with_older_thinking_marker,
    _write_iteration_with_recognized_nonrendering_gap,
)

_FROZEN_CAP = 1_000_000  # PRD G.2: never rises in this wave.


def _wire_path(monkeypatch, tmp_path, run_root, path):
    """`path == 'disk'` forces the dead-run on-disk fallback (the live
    proxy always fails); `path == 'api'` routes the live branch through
    the REAL `/logs` ASGI handler (`_api_logs_text`), never a stand-in."""
    import re

    (tmp_path / "workspace").mkdir(exist_ok=True)

    def proxy(reg, run_id, url, **kw):
        if path == "disk":
            return False, ""
        n = int(re.search(r"tail=(\d+)", url).group(1))
        import asyncio
        return True, asyncio.run(
            _api_logs_text(run_root, tmp_path / "workspace", n))

    monkeypatch.setattr(ui_server, "_proxy_text", proxy)


def _measure(tmp_path, monkeypatch, run_root, tail, path):
    _wire_path(monkeypatch, tmp_path, run_root, path)
    counts = _count_transcript_bytes(monkeypatch)
    live, rendered = ui_server.rendered_log_lines(
        tmp_path / "registry", "probe", tail)
    measured = counts[0]
    monkeypatch.undo()
    return measured, rendered


def test_p_fixtures_and_200_line_tail_cost_no_more_than_their_deciding_fetch(
        tmp_path, monkeypatch):
    """PRD P's three fixtures plus the PRD-intro 200-line grown-history
    tail, through the REAL whole call on both surfaces -- none of these
    ever triggers the outer doubling loop's own SUM-of-smaller-reads
    waste, so the whole call costs exactly what task 022's inner-fetch
    figures already measured."""
    real_lines = [json.dumps({"type": "message_end", "message": {"content": [
        {"type": "text", "text": f"line {i}"}]}}) for i in range(200)]

    for path in ("disk", "api"):
        for trailing_ignored, older_padding, expected in (
                (0, 0, 131_072), (1, 0, 131_072), (1, 500, 131_072)):
            run_root = (tmp_path / f"p-{trailing_ignored}-{older_padding}-{path}"
                       / "registry" / "runs" / "probe")
            _write_iteration_with_interleaved_ignored_suffix(
                run_root, 1, 100_000, trailing_ignored=trailing_ignored,
                older_padding=older_padding)
            collapse_index.write_index(run_root / "iterations" / "0001")
            full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                                   state=new_render_state())
            measured, rendered = _measure(
                tmp_path / f"p-{trailing_ignored}-{older_padding}-{path}",
                monkeypatch, run_root, 200, path)
            assert rendered == full[-200:]
            assert measured == expected, (path, trailing_ignored, older_padding, measured)
            assert measured < _FROZEN_CAP, measured

        run_root = tmp_path / f"tail200-{path}" / "registry" / "runs" / "probe"
        _write_iteration_with_ignored_head(run_root, 1, 20_000, real_lines)
        collapse_index.write_index(run_root / "iterations" / "0001")
        full_tail = render_to_lines("".join(merged_lines(run_root)), tty=False,
                                    state=new_render_state())
        measured, rendered = _measure(
            tmp_path / f"tail200-{path}", monkeypatch, run_root, 200, path)
        assert rendered == full_tail[-200:]
        assert measured == 65_536, (path, measured)
        assert measured < _FROZEN_CAP, measured


def test_q_fixture_and_silent_prelude_depths_disclosed_residual_after_the_cure(
        tmp_path, monkeypatch):
    """Task 013's own cure for the disclosed residual this node used to
    pin: PRD Q's own canonical fixture and both silent-prelude depths,
    through the REAL whole call on both surfaces, once the LATEST
    iteration also carries a `render_index.json` (task 012) -- the side
    index whose whole point was to let a bounded reader resume rendering
    from a checkpoint instead of paying for the SILENT flood between the
    deciding `thinking_start` transition and the transcript's own end.

    `ui_server.rendered_log_lines` (`render_index.bounded_tail`, both its
    live `/logs` branch and its dead-run on-disk fallback -- neither of
    `_fetch_live_tail`/`_fetch_dead_tail`'s own outer doubling loop even
    runs once this index is present) now answers every one of these three
    fixtures for comfortably UNDER the frozen 1,000,000-byte cap this same
    node used to measure at 1,445,567 / 12,115,567 / 12,862,467 bytes --
    and the render is still exactly correct: the full, unbounded render's
    own last two lines, no replay, no added blank line, regardless of how
    many silent bytes (1,000 vs. 10,000 padded gap lines, prelude depth
    1,000 vs. 1,700) sit between the deciding line and the transcript's
    own end."""
    text = "reviewer synthetic streamed text"
    gap = json.dumps({"type": "message_end", "message": {"content": []},
                      "padding": "x" * 1000})

    for path in ("disk", "api"):
        for gap_count, prelude_count in (
                (1_000, 0),
                (10_000, 1_000),
                (10_000, 1_700)):
            run_root = (tmp_path / f"q-{gap_count}-{prelude_count}-{path}"
                       / "registry" / "runs" / "probe")
            _write_iteration_with_recognized_nonrendering_gap(
                run_root, 1, text, gap, gap_count, prelude_count=prelude_count)
            itdir = run_root / "iterations" / "0001"
            collapse_index.write_index(itdir)
            render_index.write_index(itdir)
            full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                                   state=new_render_state())
            measured, rendered = _measure(
                tmp_path / f"q-{gap_count}-{prelude_count}-{path}",
                monkeypatch, run_root, 2, path)
            assert rendered == full[-2:], (path, gap_count, prelude_count, rendered)
            assert all(ln != "" for ln in rendered)
            assert measured <= _FROZEN_CAP, (
                "task 013: bounded read off the render index",
                path, gap_count, prelude_count, measured)


def _write_live_iteration(run_root, lines, *, ended, indexed_lines):
    """One iteration whose `render_index.json` covers only its first
    `indexed_lines` raw lines -- exactly what `engine/runner.py`'s `pump()`
    leaves on disk between two `LiveRenderIndex.flush` batches, i.e. the
    ordinary lag of a LIVE iteration's index behind its transcript."""
    itdir = run_root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    meta = {"number": 1, "phase": "worker", "model": "stub-model", "approach": 1,
            "startedAt": "2025-01-01T00:00:00Z", "exitCode": 0, "error": None,
            "usage": {"totalTokens": 1}}
    if ended:
        meta["endedAt"] = "2025-01-01T00:00:01Z"
    (itdir / "meta.json").write_text(json.dumps(meta))
    raw = [(ln + "\n").encode() for ln in lines]
    (itdir / "output.jsonl").write_bytes(b"".join(raw))
    live = render_index.LiveRenderIndex()
    for chunk in raw[:indexed_lines]:
        live.feed(chunk)
    assert live.flush(itdir, force=True)
    index = render_index.load_index(itdir)
    assert index is not None
    return index, sum(len(c) for c in raw)


def _said(text):
    return json.dumps({"type": "message_end", "message": {"content": [
        {"type": "text", "text": text}]}})


def test_lagging_live_index_tail_includes_the_unindexed_suffix(tmp_path, monkeypatch):
    """Task 013 retry: a valid render index that lags the transcript (the
    normal state of a live one) must not cut the tail off at what it
    covers -- the unindexed suffix's own newest line ('gamma', behind
    ~9 KB of silent events) is the full render's last line, on both
    surfaces, live and finished alike."""
    silent = json.dumps({"type": "message_end", "message": {"content": []},
                         "padding": "x" * 900})
    lines = [_said("alpha"), _said("beta")] + [silent] * 10 + [_said("gamma")]
    for ended in (False, True):
        for path in ("disk", "api"):
            sub = tmp_path / f"lag-{ended}-{path}"
            run_root = sub / "registry" / "runs" / "probe"
            index, size = _write_live_iteration(run_root, lines, ended=ended,
                                                indexed_lines=2)
            assert size - index["transcriptBytes"] > 9_000
            full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                                   state=new_render_state())
            for tail in (1, 2, 3):
                measured, rendered = _measure(sub, monkeypatch, run_root, tail, path)
                assert rendered == full[-tail:], (ended, path, tail, rendered)
                assert measured <= _FROZEN_CAP, measured
            if not ended:
                assert full[-1] == "gamma", full


def test_streamed_text_and_tool_calls_tail_equals_full_render_at_every_index_lag(
        tmp_path, monkeypatch):
    """Task 013 retry: the bounded tail is exact for content whose rendered
    lines do not line up with raw lines -- a streamed text line spanning
    several `text_delta` events, a tool call whose invocation line needs
    the arguments its start event recorded several lines earlier -- at
    every point a live index could have been flushed, through the whole
    client-facing call on both surfaces."""
    def delta(text):
        return json.dumps({"type": "message_update",
                           "assistantMessageEvent": {"type": "text_delta", "delta": text}})
    text_end = json.dumps({"type": "message_update",
                           "assistantMessageEvent": {"type": "text_end"}})
    silent = json.dumps({"type": "message_end", "message": {"content": []},
                         "padding": "x" * 300})
    tool_start = json.dumps({"type": "tool_execution_start", "toolCallId": "t1",
                             "toolName": "bash", "args": {"command": "ls -la"}})
    tool_end = json.dumps({"type": "tool_execution_end", "toolCallId": "t1",
                           "toolName": "bash", "isError": False,
                           "result": {"content": [{"type": "text", "text": "ok"}]}})
    lines = ([delta("one "), silent, delta("streamed\nline "), delta("two"), text_end,
              tool_start, silent, silent, tool_end, _said("said"), delta("tail "),
              silent, delta("open")])
    for ended in (False, True):
        full = None
        for cut in range(len(lines) + 1):
            for path in ("disk", "api") if cut in (0, 6, len(lines)) else ("disk",):
                sub = tmp_path / f"mix-{ended}-{cut}-{path}"
                run_root = sub / "registry" / "runs" / "probe"
                _write_live_iteration(run_root, lines, ended=ended, indexed_lines=cut)
                full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                                       state=new_render_state())
                for tail in (1, 2, 3, 4, 5):
                    _, rendered = _measure(sub, monkeypatch, run_root, tail, path)
                    assert rendered == full[-tail:], (ended, cut, path, tail, rendered)
        assert any("$ ls -la" in ln for ln in full), full
