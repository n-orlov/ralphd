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
import os
from pathlib import Path

import pytest

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


def test_same_size_rewrite_of_the_leading_indexed_line_falls_back_exactly(
        tmp_path, monkeypatch):
    """Task cure-01-03 (PRD C.1/C.3, D.1, DoD 3-4): a checkpoint's own
    STATE can be silently wrong even when the transcript's total byte
    count never changes -- an in-place, same-length rewrite of exactly the
    render index's own leading (`checkpoints[0]`) raw line changes what a
    fresh render of it would produce, but nothing downstream of
    `render_index.load_index` ever re-reads those particular bytes on its
    own (`_prepend_covered`'s backward walk always seeds a segment's
    render from the PRECEDING checkpoint's STORED state, and simply stops
    once it reaches the very first one, never validating it against
    anything). Before this task's cure, `ui_server.rendered_log_lines`
    returned the stale 'beta' (the ORIGINAL, pre-rewrite content) on both
    surfaces instead of 'alpha' (what the ACTUAL current bytes render) --
    a wrong-but-plausible answer, never an exception, so nothing forced a
    fallback. This pins the cured contract: the index is refused outright
    (`render_index.load_index`/`bounded_tail` -> `None`) and both surfaces
    fall back to the exact full-render tail instead."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 220})
    for path in ("disk", "api"):
        sub = tmp_path / f"rewrite-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_live_iteration(run_root, [silent, _said("beta")], ended=False,
                             indexed_lines=2)
        itdir = run_root / "iterations" / "0001"
        transcript = itdir / "output.jsonl"
        before = render_index.load_index(itdir)
        assert before is not None, "the unmodified index must validate"

        changed = json.dumps({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "delta": "alpha"}})
        changed += " " * (len(silent) - len(changed))
        old = transcript.read_text()
        new = changed + old[len(silent):]
        assert len(new) == len(old), "the rewrite must not change the file's size"
        transcript.write_text(new)

        # `load_index` refuses this rewritten index off its bounded
        # verification alone (`checkpoints[0]` plus the covered prefix's
        # last `VERIFY_WINDOW_BYTES`), never a whole-prefix scan.
        assert render_index.load_index(itdir) is None
        assert render_index.bounded_tail(run_root, 1) is None

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert full[-1:] == ["alpha"], full
        _, rendered = _measure(sub, monkeypatch, run_root, 1, path)
        assert rendered == full[-1:] == ["alpha"], (path, rendered)


@pytest.mark.parametrize("damage", [
    "empty-dict", "missing-one-key", "wrong-typed-bool", "extra-unknown-key"])
def test_missing_or_wrong_typed_checkpoint_state_falls_back_on_both_surfaces(
        tmp_path, monkeypatch, damage):
    """Task cure-01-03 (PRD C.1/C.3, D.1, DoD 3-4): a checkpoint whose own
    `state` value does not carry `log_render.new_render_state`'s exact
    shape -- missing a key entirely, an extra one, or a wrong-typed value
    for one -- is refused by `render_index.load_index` itself, on both
    surfaces, INSTEAD of reaching `log_render.render_to_text` and raising
    (`KeyError: 'text_seen'` before this task's cure) or silently
    replaying stale content."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 220})
    for path in ("disk", "api"):
        sub = tmp_path / f"malformed-{damage}-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_live_iteration(run_root, [silent, _said("beta")], ended=False,
                             indexed_lines=2)
        itdir = run_root / "iterations" / "0001"
        index_path = itdir / render_index.INDEX_FILENAME
        doc = json.loads(index_path.read_text())
        state = doc["checkpoints"][0]["state"]
        if damage == "empty-dict":
            doc["checkpoints"][0]["state"] = {}
        elif damage == "missing-one-key":
            del state["text_seen"]
        elif damage == "wrong-typed-bool":
            state["text_seen"] = "nope"
        else:
            state["an_unknown_extra_key"] = 1
        index_path.write_text(json.dumps(doc))

        assert render_index.load_index(itdir) is None
        assert render_index.bounded_tail(run_root, 1) is None

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert full[-1:] == ["beta"], full
        _, rendered = _measure(sub, monkeypatch, run_root, 1, path)
        assert rendered == full[-1:] == ["beta"], (path, damage, rendered)


@pytest.mark.parametrize("damage", [
    "tool-pending-int", "tool-pending-short-list", "tool-pending-non-str-name",
    "tool-pending-list", "unresolved-nested-list", "unresolved-int-member",
    "open-tool-line-int"])
def test_malformed_nested_state_in_a_later_checkpoint_falls_back_on_both_surfaces(
        tmp_path, monkeypatch, damage):
    """Task cure-01-03 retry (PRD C.1/C.3, D.1, DoD 3-4): the checkpoint-
    state shape check reaches INSIDE the containers, and applies to every
    checkpoint, not just the leading one. A LATER checkpoint (here the
    last, which seeds the unindexed suffix's render) whose nested value
    cannot hydrate -- `tool_pending = {"call-id": 42}` raised
    `TypeError: 'int' object is not iterable` out of
    `ui_server.rendered_log_lines` on both surfaces before this retry --
    makes `render_index.load_index` refuse the index, and both surfaces
    answer the exact full-render tail ('gamma', from the appended suffix)
    instead of raising."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 220})
    lines = [silent, _said("alpha"), _said("beta"), silent, _said("gamma")]
    for path in ("disk", "api"):
        sub = tmp_path / f"nested-{damage}-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_live_iteration(run_root, lines, ended=False, indexed_lines=3)
        itdir = run_root / "iterations" / "0001"
        index_path = itdir / render_index.INDEX_FILENAME
        doc = json.loads(index_path.read_text())
        assert len(doc["checkpoints"]) == 3
        state = doc["checkpoints"][-1]["state"]
        if damage == "tool-pending-int":
            state["tool_pending"] = {"call-id": 42}
        elif damage == "tool-pending-short-list":
            state["tool_pending"] = {"call-id": ["bash"]}
        elif damage == "tool-pending-non-str-name":
            state["tool_pending"] = {"call-id": [7, {}]}
        elif damage == "tool-pending-list":
            state["tool_pending"] = [["bash", {}]]
        elif damage == "unresolved-nested-list":
            state["collapse_unresolved"] = [["textSeen"]]
        elif damage == "unresolved-int-member":
            state["collapse_unresolved"] = [1]
        else:
            state["open_tool_line"] = 5
        index_path.write_text(json.dumps(doc))

        assert render_index.load_index(itdir) is None
        assert render_index.bounded_tail(run_root, 1) is None

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert full[-1:] == ["gamma"], full
        _, rendered = _measure(sub, monkeypatch, run_root, 1, path)
        assert rendered == full[-1:] == ["gamma"], (path, damage, rendered)


def test_bounded_tail_refuses_a_passed_in_index_whose_state_cannot_hydrate(tmp_path):
    """Defence in depth behind `load_index`: an `index=` handed straight to
    `render_index.bounded_tail` (the hub's absent-index cache path, which
    never re-runs `load_index`) whose last checkpoint state cannot be
    hydrated or rendered yields `None` -- the caller's full-render
    fallback -- never an exception."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 220})
    lines = [silent, _said("alpha"), _said("gamma")]
    run_root = tmp_path / "registry" / "runs" / "probe"
    index, _ = _write_live_iteration(run_root, lines, ended=False, indexed_lines=2)
    index["checkpoints"][-1]["state"]["tool_pending"] = {"call-id": 42}
    assert render_index.bounded_tail(run_root, 1, index=index) is None
    del index["checkpoints"][-1]["state"]["text_seen"]
    assert render_index.bounded_tail(run_root, 1, index=index) is None


def _rewrite_line_same_size(transcript, number, replacement):
    """Replace raw line `number` (0-based) of `transcript` in place with
    `replacement`, space-padded to the original line's exact length, so
    the file's total byte count never changes."""
    lines = transcript.read_bytes().split(b"\n")
    original = lines[number]
    new = replacement.encode()
    assert len(new) <= len(original), (len(new), len(original))
    lines[number] = new + b" " * (len(original) - len(new))
    data = b"\n".join(lines)
    assert len(data) == transcript.stat().st_size
    transcript.write_bytes(data)


def _text_delta(text):
    return json.dumps({"type": "message_update", "assistantMessageEvent": {
        "type": "text_delta", "delta": text}})


@pytest.mark.parametrize("variant", [
    "silent-to-streamed-text", "silent-renders-identically", "said-text-changed",
    "replaced-by-rename"])
def test_same_size_rewrite_of_a_later_covered_line_falls_back_exactly(
        tmp_path, monkeypatch, variant):
    """Task cure-01-03 second retry (PRD C.1/C.3, D.1, DoD 3-4): a same-size
    rewrite of a covered raw line that is NOT the leading one -- here the
    SECOND of three, a silent line the backward walk never reads for a
    one-line tail -- made the index lie: turning it into a `text_delta`
    'alpha' makes the full render suppress the later `message_end`
    'beta' (already-streamed text), yet `ui_server.rendered_log_lines`
    returned the stale 'beta' on both surfaces. Every checkpoint now
    carries its raw line's CRC-32, and `render_index.load_index` verifies
    the covered prefix's last `VERIFY_WINDOW_BYTES` (all of it here)
    segment by segment -- bytes, fragment count, resulting state -- so the
    index is refused and both surfaces answer the exact full-render tail.
    The variants cover a rewrite that changes the render, one that
    renders identically (a stale index is refused even when harmless),
    a changed text on a line the walk DOES read, and the whole transcript
    replaced by a same-size file through a rename."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 220})
    for ended in (False, True):
        for path in ("disk", "api"):
            sub = tmp_path / f"later-{variant}-{ended}-{path}"
            run_root = sub / "registry" / "runs" / "probe"
            _write_live_iteration(run_root, [silent, silent, _said("beta")],
                                  ended=ended, indexed_lines=3)
            itdir = run_root / "iterations" / "0001"
            transcript = itdir / "output.jsonl"
            assert render_index.load_index(itdir) is not None
            size = transcript.stat().st_size
            if variant == "silent-to-streamed-text":
                _rewrite_line_same_size(transcript, 1, _text_delta("alpha"))
            elif variant == "silent-renders-identically":
                _rewrite_line_same_size(transcript, 1, json.dumps(
                    {"type": "ignored", "padding": "y" * 220}))
            elif variant == "said-text-changed":
                _rewrite_line_same_size(transcript, 2, _said("gama"))
            else:
                data = transcript.read_bytes().replace(b'"x', b'"z', 1)
                lines = data.split(b"\n")
                lines[1] = _text_delta("alpha").encode().ljust(len(lines[1]))
                replacement = itdir / "output.jsonl.new"
                replacement.write_bytes(b"\n".join(lines))
                replacement.replace(transcript)
            assert transcript.stat().st_size == size

            assert render_index.load_index(itdir) is None, (variant, ended, path)
            assert render_index.bounded_tail(run_root, 1) is None

            full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                                   state=new_render_state())
            if variant in ("silent-to-streamed-text", "replaced-by-rename") and not ended:
                assert full[-1:] == ["alpha"], full
            for tail in (1, 2):
                _, rendered = _measure(sub, monkeypatch, run_root, tail, path)
                assert rendered == full[-tail:], (variant, ended, path, tail, rendered)


@pytest.mark.parametrize("damage", [
    "missing-crc", "string-crc", "bool-crc", "negative-crc", "wrong-crc",
    "flipped-flag-in-a-middle-state", "wrong-rendered-lines"])
def test_missing_wrong_typed_or_inconsistent_checkpoint_fields_fall_back_on_both_surfaces(
        tmp_path, monkeypatch, damage):
    """Task cure-01-03 second retry: a checkpoint whose `lineCrc32` is
    missing, wrong-typed or wrong, or whose well-formed `state` /
    `renderedLines` no longer follows from its predecessor's (a flipped
    `text_seen` in a MIDDLE checkpoint that no shape check can see), is
    refused by `render_index.load_index`, and both surfaces fall back to
    the exact full-render tail instead of a stale or raising render."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 220})
    lines = [silent, _said("alpha"), silent, _said("beta"), silent]
    for path in ("disk", "api"):
        sub = tmp_path / f"fields-{damage}-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_live_iteration(run_root, lines, ended=False, indexed_lines=4)
        itdir = run_root / "iterations" / "0001"
        index_path = itdir / render_index.INDEX_FILENAME
        doc = json.loads(index_path.read_text())
        middle = doc["checkpoints"][2]
        if damage == "missing-crc":
            del middle["lineCrc32"]
        elif damage == "string-crc":
            middle["lineCrc32"] = str(middle["lineCrc32"])
        elif damage == "bool-crc":
            middle["lineCrc32"] = True
        elif damage == "negative-crc":
            middle["lineCrc32"] = -1
        elif damage == "wrong-crc":
            middle["lineCrc32"] ^= 1
        elif damage == "flipped-flag-in-a-middle-state":
            middle["state"]["text_seen"] = not middle["state"]["text_seen"]
        else:
            doc["checkpoints"][3]["renderedLines"] += 1
        index_path.write_text(json.dumps(doc))

        assert render_index.load_index(itdir) is None, damage
        assert render_index.bounded_tail(run_root, 1) is None

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert full[-2:] == ["alpha", "beta"], full
        for tail in (1, 2):
            _, rendered = _measure(sub, monkeypatch, run_root, tail, path)
            assert rendered == full[-tail:], (damage, path, tail, rendered)


def test_a_version_1_index_without_line_checksums_falls_back_exactly(
        tmp_path, monkeypatch):
    """An index in the earlier, checksum-free shape (`version` 1, no
    `lineCrc32`) cannot be verified against the transcript, so it is
    refused and both surfaces render the exact full tail."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 220})
    for path in ("disk", "api"):
        sub = tmp_path / f"v1-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_live_iteration(run_root, [silent, _said("beta")], ended=True,
                              indexed_lines=2)
        itdir = run_root / "iterations" / "0001"
        index_path = itdir / render_index.INDEX_FILENAME
        doc = json.loads(index_path.read_text())
        doc["version"] = 1
        for cp in doc["checkpoints"]:
            del cp["lineCrc32"]
        index_path.write_text(json.dumps(doc))
        assert render_index.load_index(itdir) is None
        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        _, rendered = _measure(sub, monkeypatch, run_root, 2, path)
        assert rendered == full[-2:], (path, rendered)


def test_prefix_verification_reads_a_bounded_window_on_a_large_covered_prefix(
        tmp_path, monkeypatch):
    """The verification that catches a rewrite costs a FIXED allowance
    (PRD C.2), not the covered prefix's size: on PRD Q's canonical fixture
    (a covered prefix over 1,000,000 bytes, almost all of it silent) a
    valid index loads, and a same-size rewrite of a silent line near the
    end is refused, each reading at most `checkpoints[0]`'s own line plus
    `render_index.VERIFY_WINDOW_BYTES` off the transcript; the whole
    client-facing call on the valid index stays under the frozen cap and
    exact on both surfaces."""
    from test_log_merge_bounded_reads import _count_transcript_bytes

    text = "reviewer synthetic streamed text"
    gap = json.dumps({"type": "message_end", "message": {"content": []},
                      "padding": "x" * 1000})
    for path in ("disk", "api"):
        sub = tmp_path / f"window-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_iteration_with_recognized_nonrendering_gap(run_root, 1, text, gap, 1_000)
        itdir = run_root / "iterations" / "0001"
        render_index.write_index(itdir)
        transcript = itdir / "output.jsonl"
        size = transcript.stat().st_size
        assert size > _FROZEN_CAP
        first_line = len(transcript.read_bytes().split(b"\n", 1)[0]) + 1
        allowance = first_line + render_index.VERIFY_WINDOW_BYTES

        counts = _count_transcript_bytes(monkeypatch)
        assert render_index.load_index(itdir) is not None
        assert counts[0] <= allowance, counts[0]
        monkeypatch.undo()

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        measured, rendered = _measure(sub, monkeypatch, run_root, 2, path)
        assert rendered == full[-2:], (path, rendered)
        assert measured <= _FROZEN_CAP, measured

        lines = transcript.read_bytes().split(b"\n")
        victim = next(i for i in range(len(lines) - 10, 0, -1)
                      if b'"padding"' in lines[i])
        _rewrite_line_same_size(transcript, victim, json.dumps(
            {"type": "message_end", "message": {"content": []},
             "padding": "y" * 1000}))
        counts = _count_transcript_bytes(monkeypatch)
        assert render_index.load_index(itdir) is None
        assert counts[0] <= allowance, counts[0]
        monkeypatch.undo()


def test_a_genuine_append_only_lagging_index_still_validates_and_stays_bounded(
        tmp_path, monkeypatch):
    """Regression guard alongside the two rejection tests above: neither
    the new leading-checkpoint content check nor the stricter checkpoint-
    state shape check makes a genuinely untouched, append-only, lagging-
    behind-the-transcript index unusable -- `render_index.load_index`
    still validates it, and the whole client-facing call still costs no
    more than the small indexed-plus-suffix bound task 013 already
    measured (never a full rescan just because THIS task added a
    correctness check)."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 900})
    lines = [silent, _said("alpha")] + [silent] * 10 + [_said("gamma")]
    for path in ("disk", "api"):
        sub = tmp_path / f"grown-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_live_iteration(run_root, lines, ended=False, indexed_lines=2)
        itdir = run_root / "iterations" / "0001"
        assert render_index.load_index(itdir) is not None

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        measured, rendered = _measure(sub, monkeypatch, run_root, 1, path)
        assert rendered == full[-1:], (path, rendered)
        assert measured <= _FROZEN_CAP, measured


def test_tail_exceeding_the_latest_indexed_iterations_own_output_stays_bounded(
        tmp_path, monkeypatch):
    """Task cure-01-04 (PRD C.2, DoD 3): a review finding at
    22280f71-and-earlier -- `test_indexed_and_second_tails_are_bounded
    [large-tail-*]` in the final review's own `probes-c.log` -- requested
    200 lines (the hub's own default tail) of a fully-indexed silent-
    prelude iteration whose WHOLE rendered output is only four lines, and
    read 19,849,033 transcript bytes to answer it: `render_index.
    bounded_tail` walked its ONE iteration's covered prefix all the way
    back to byte 0 (there being nowhere else short for those four lines
    to come from), found the four lines and no more, and gave up with
    `None` purely because four is less than 200 -- discarding everything
    it had already, cheaply, proven and falling all the way back to the
    unbounded growth-based scan for an answer it already had. Fails at
    22280f71 (measured over 19.8M bytes on both surfaces); this task's
    cure makes `bounded_tail` return what it already walked, fully
    exhausted, instead of re-deriving the identical answer at that cost."""
    text = "large-tail streamed text must not trigger an unbounded rescan"
    gap = json.dumps({"type": "message_end", "message": {"content": []},
                      "padding": "x" * 1000})
    for path in ("disk", "api"):
        sub = tmp_path / f"large-tail-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_iteration_with_recognized_nonrendering_gap(
            run_root, 1, text, gap, 10_000, prelude_count=1_000)
        itdir = run_root / "iterations" / "0001"
        collapse_index.write_index(itdir)
        render_index.write_index(itdir)
        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert len(full) < 200, full  # the whole point: fewer lines exist than asked

        measured, rendered = _measure(sub, monkeypatch, run_root, 200, path)
        assert rendered == full, (path, rendered, full)
        assert measured <= _FROZEN_CAP, (path, measured)


def test_tail_spanning_two_indexed_iterations_never_rescans_either_ones_silence(
        tmp_path, monkeypatch):
    """Task cure-01-04 (PRD C.2, DoD 3): the same fix, but the extra lines
    a tail needs sit in a PRECEDING iteration, not just further back in
    the latest one -- `render_index.bounded_tail` must walk backward
    across an iteration boundary, purely off each iteration's own
    `render_index.json`, prepending the earlier one's own genuine
    `ralphd.iteration` "start" boundary line once its own covered prefix
    is fully exhausted, never rescanning either iteration's own silent
    flood to get there."""
    gap = json.dumps({"type": "message_end", "message": {"content": []},
                      "padding": "x" * 1000})
    for path in ("disk", "api"):
        sub = tmp_path / f"span-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        for number, text in ((1, "span first iteration streamed text"),
                             (2, "span second iteration streamed text")):
            _write_iteration_with_recognized_nonrendering_gap(
                run_root, number, text, gap, 5_000, prelude_count=500)
            itdir = run_root / "iterations" / f"{number:04d}"
            collapse_index.write_index(itdir)
            render_index.write_index(itdir)

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert len(full) < 200, full
        assert sum(1 for ln in full if "iteration 1" in ln) >= 1, full
        assert sum(1 for ln in full if "iteration 2" in ln) >= 1, full

        measured, rendered = _measure(sub, monkeypatch, run_root, 200, path)
        assert rendered == full, (path, rendered, full)
        assert measured <= _FROZEN_CAP, (path, measured)


def test_absent_index_cache_still_builds_when_a_valid_legacy_index_scan_stays_expensive(
        tmp_path, monkeypatch):
    """Task cure-01-04 (PRD C.4, DoD 3): a review finding at
    22280f71-and-earlier -- the `legacy-collapse-only-second` cases of
    `test_indexed_and_second_tails_are_bounded` read 12,115,529 transcript
    bytes on BOTH the first AND the second same-iteration/same-size call,
    because `ui_server._absent_index_cache_store` skipped building its
    cache purely because this iteration's own `collapse_index.json` was
    valid -- a proxy for "the scan was cheap" that PRD Q's own silent-
    prelude fixture (also written WITH a valid `collapse_index.json`)
    falsifies: its outer growth loop still has to grow all the way to the
    FULL-ask bound to resolve, reading nearly the whole transcript
    regardless. Fails at 22280f71 (second call measured over 12.1M
    bytes); this task's cure keys the skip on whether THIS scan actually
    needed that full-ask bound (`_fetch_live_tail`/`_fetch_dead_tail`'s
    own new third return value), not on the legacy index's mere
    presence, so the cache still gets built and the second call is
    bounded."""
    text = "legacy-collapse-only second call must not rescan"
    gap = json.dumps({"type": "message_end", "message": {"content": []},
                      "padding": "x" * 1000})
    for path in ("disk", "api"):
        sub = tmp_path / f"legacy-second-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_iteration_with_recognized_nonrendering_gap(
            run_root, 1, text, gap, 10_000, prelude_count=1_000)
        itdir = run_root / "iterations" / "0001"
        collapse_index.write_index(itdir)
        assert not (itdir / render_index.INDEX_FILENAME).exists()

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        ui_server._ABSENT_INDEX_CACHE.clear()

        # Call 1: pays the scan -- no byte cap (PRD C.3's own disclosed
        # residual, unaffected by this task).
        first, rendered1 = _measure(sub, monkeypatch, run_root, 2, path)
        assert rendered1 == full[-2:], (path, rendered1)
        assert "probe" in ui_server._ABSENT_INDEX_CACHE, (
            "the cache must actually build even though collapse_index.json is valid", path)

        # Call 2: served from the cache this task's fix now actually builds.
        second, rendered2 = _measure(sub, monkeypatch, run_root, 2, path)
        assert rendered2 == full[-2:], (path, rendered2)
        assert second <= _FROZEN_CAP, (path, first, second)


def test_second_cold_tail_across_two_iterations_is_bounded(
        tmp_path, monkeypatch):
    """Task cure-01-02-2 (PRD C.4, D.1; DoD 3): task 015's own absent-index
    cache only ever remembered the LATEST iteration's own scan -- a tail
    spanning TWO absent-index (no `render_index.json`, no legacy
    `collapse_index.json` either) silent-prelude iterations paid the
    growth-based scan's full, unbounded cost on the SECOND call exactly
    as on the first, because the earlier iteration's own coverage was
    never cached at all. Two such iterations (silent-prelude depths 1,000
    and 1,700, PRD Q's own falsifying shape), a 200-line request -- the
    FIRST call pays the scan (no byte cap: PRD C.3's own disclosed
    residual, unaffected by this task); the SECOND call must be exact and
    stay within the frozen 1,000,000-byte C.2 bound, on both the live and
    dead-run surfaces, because this task's own cache now covers BOTH
    iterations, not merely the newest one."""
    gap = json.dumps({"type": "message_end", "message": {"content": []},
                      "padding": "x" * 1000})
    for path in ("disk", "api"):
        sub = tmp_path / f"cold-span-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        for number, (text, prelude_count) in enumerate(
                (("cold span first iteration streamed text", 1_000),
                 ("cold span second iteration streamed text", 1_700)), start=1):
            _write_iteration_with_recognized_nonrendering_gap(
                run_root, number, text, gap, 10_000, prelude_count=prelude_count)
            itdir = run_root / "iterations" / f"{number:04d}"
            assert not (itdir / render_index.INDEX_FILENAME).exists()
            assert not (itdir / collapse_index.INDEX_FILENAME).exists()

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert len(full) < 200, full
        assert sum(1 for ln in full if "iteration 1" in ln) >= 1, full
        assert sum(1 for ln in full if "iteration 2" in ln) >= 1, full
        ui_server._ABSENT_INDEX_CACHE.clear()

        # Call 1: pays the scan across both cold iterations -- no byte
        # cap (PRD C.3).
        first, rendered1 = _measure(sub, monkeypatch, run_root, 200, path)
        assert rendered1 == full, (path, rendered1)

        # Call 2: served from this task's own per-iteration cache,
        # covering BOTH iterations, not merely the latest one.
        second, rendered2 = _measure(sub, monkeypatch, run_root, 200, path)
        assert rendered2 == full, (path, rendered2)
        assert second <= _FROZEN_CAP, (path, first, second)


def test_unchanged_cached_iteration_is_not_rescanned_after_another_changes(
        tmp_path, monkeypatch):
    """Task cure-01-02-3 (PRD C.4, D.1; DoD 3, review-135): cure-01-02-2's
    own per-iteration cache covered every iteration a tail's backward walk
    needed, but the FIRST-pass lookup (`ui_server.
    _absent_index_cache_lookup_or_rebuild_refused`) declined outright the
    moment ANY iteration in that walk -- typically the newest, most
    recently changed one -- had no valid cache and no on-disk index,
    abandoning the whole `bounded_tail` attempt before it ever reached an
    EARLIER, genuinely unchanged iteration whose own cache was still
    perfectly valid. `rendered_log_lines`'s growth-based fallback then
    re-read every iteration the tail needed straight off disk, rescanning
    that unchanged earlier iteration's full bytes all over again purely
    because a LATER one had changed. Two absent-index (no
    `render_index.json`, no legacy `collapse_index.json`) silent-prelude
    cold iterations, a bounded warm 200-line tail across both -- then
    only iteration 2's transcript is appended to or rewritten at the same
    size -- must still answer exactly, reading at most 1,000,000 bytes off
    iteration 1's UNCHANGED transcript, on both the live and dead-run
    surfaces; fails at 1ff085d8 (over 11.7M bytes read off iteration 1)."""
    gap = json.dumps({"type": "message_end", "message": {"content": []},
                      "padding": "x" * 1000})
    for path in ("disk", "api"):
        for change in ("append", "rewrite"):
            sub = tmp_path / f"unaffected-{change}-{path}"
            run_root = sub / "registry" / "runs" / "probe"
            for number in (1, 2):
                _write_iteration_with_recognized_nonrendering_gap(
                    run_root, number, f"exact text {number}", gap, 10_000,
                    prelude_count=1_000)
            ui_server._ABSENT_INDEX_CACHE.clear()

            # Warm the cache for BOTH cold iterations first (cure-01-02-2's
            # own contract): the first call pays the scan, the second is
            # bounded and both get cached.
            _measure(sub, monkeypatch, run_root, 200, path)
            warm, _ = _measure(sub, monkeypatch, run_root, 200, path)
            assert warm <= _FROZEN_CAP, (path, change, warm)
            assert {1, 2} <= ui_server._ABSENT_INDEX_CACHE["probe"].keys()
            old_cached = ui_server._ABSENT_INDEX_CACHE["probe"][1]
            old_output = run_root / "iterations" / "0001" / "output.jsonl"
            old_stat = old_output.stat()
            new_output = run_root / "iterations" / "0002" / "output.jsonl"

            if change == "append":
                with new_output.open("a") as f:
                    f.write(json.dumps({"type": "ignored", "note": "new bytes"}) + "\n")
            else:
                new_output.write_bytes(
                    new_output.read_bytes().replace(b"exact text 2", b"other text 2"))

            expected = render_to_lines(
                "".join(merged_lines(run_root)), tty=False,
                state=new_render_state())[-200:]

            # Per-ITERATION byte tally (unlike `_count_transcript_bytes`,
            # which only tallies the whole-call total) -- the whole point
            # of this node is to tell iteration 1's own reads apart from
            # iteration 2's.
            counts = {1: 0, 2: 0}
            real_open = Path.open

            def opened(self, mode="r", *a, **kw):
                f = real_open(self, mode, *a, **kw)
                if self.name == "output.jsonl":
                    number = int(self.parent.name)
                    real_read = f.read

                    def read(*args, **kwargs):
                        data = real_read(*args, **kwargs)
                        counts[number] += (
                            len(data) if isinstance(data, bytes) else len(data.encode()))
                        return data

                    f.read = read
                return f

            _wire_path(monkeypatch, sub, run_root, path)
            monkeypatch.setattr(Path, "open", opened)
            _, actual = ui_server.rendered_log_lines(sub / "registry", "probe", 200)
            monkeypatch.undo()

            assert old_output.stat().st_mtime_ns == old_stat.st_mtime_ns
            assert ui_server._ABSENT_INDEX_CACHE["probe"][1] is old_cached
            assert actual == expected, (path, change, actual, expected)
            assert counts[1] <= 1_000_000, (path, change, counts)


def test_spec_and_cli_docs_describe_the_indexed_bounded_tail_read():
    """Task cure-01-04-2 (D.1; DoD 4, cure-01-04 retry): SPEC.md §11.4 and
    docs/cli.md must describe the log tail `rendered_log_lines` actually
    serves -- the `render_index.bounded_tail` read first, a `/logs?tail=1`
    probe only for the `live` flag, and a bounded `/logs?tail=<bound>`
    fallback -- but the fallback's own growing-bound re-ask loop has no
    byte cap and, on a cold or unusable-index run, CAN end up fetching and
    rendering the entire available transcript for a small POSITIVE `tail`
    too, not only for `tail=0`. The docs must no longer pin the false
    'never fetches the whole backlog' claim the review found falsified by
    the real cold-tail fallback (see
    `test_a_cold_positive_tail_fallback_can_render_the_entire_backlog_exactly`
    below); they must instead distinguish the bounded indexed read (cost
    proportional to `tail`) from the uncapped cold/unusable-index scan
    (positive-tail fallback may fetch everything), and describe `tail=0`
    as an explicit whole-transcript request, not the only path that can
    read the whole thing."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = (root / "SPEC.md").read_text(encoding="utf-8")
    section = spec.split("### 11.4 Live transcript", 1)[1].split("### 11.5", 1)[0]
    cli = (root / "docs" / "cli.md").read_text(encoding="utf-8")
    route = cli.split("`GET /api/runs/<id>/logs?tail=N`", 1)[1][:3000]
    for name, text in (("SPEC.md §11.4", section), ("docs/cli.md", route)):
        flat = " ".join(text.split())
        assert "bounded_tail" in flat, name
        assert "GET /logs?tail=1" in flat, name
        assert "GET /logs?tail=<bound>" in flat, name
        # Distinguishes the bounded indexed read from the uncapped
        # cold/unusable-index correctness scan.
        assert "bounded indexed read" in flat, name
        assert "correctness scan" in flat, name
        assert "no byte cap" in flat, name
        # The false never-fetches-the-whole-backlog claim is retired: a
        # positive-tail fallback may fetch/render the entire transcript.
        assert "positive `tail`" in flat, name
        assert "entire" in flat and "transcript" in flat, name
        # tail=0 is described as an explicit whole-transcript request,
        # not the only path capable of reading the whole thing.
        assert "tail=0" in flat, name
        assert "not the only path" in flat, name
        assert "full** raw NDJSON backlog" not in flat, name
        assert "full raw NDJSON backlog" not in flat, name


def test_spec_and_cli_docs_key_cache_reuse_on_transcript_identity_not_size():
    """Task cure-01-02-3 retry (C.4, D.1; DoD 3): the hub's absent-index
    cache is keyed on each iteration's full transcript identity
    `(inode, size, mtime_ns, ctime_ns)` (`_absent_index_cache_get`), so a
    same-size rewrite or replacement invalidates that iteration's entry and
    is rescanned. SPEC.md §11.4 and docs/cli.md must say reuse needs an
    unchanged transcript identity, never merely the same transcript size,
    and must scope invalidation to the one changed iteration."""
    root = Path(__file__).resolve().parent.parent
    spec = (root / "SPEC.md").read_text(encoding="utf-8")
    section = spec.split("### 11.4 Live transcript", 1)[1].split("### 11.5", 1)[0]
    cli = (root / "docs" / "cli.md").read_text(encoding="utf-8")
    route = cli.split("`GET /api/runs/<id>/logs?tail=N`", 1)[1][:4000]
    for name, text in (("SPEC.md §11.4", section), ("docs/cli.md", route)):
        flat = " ".join(text.split())
        assert "same iteration and transcript size" not in flat, name
        assert "unchanged transcript identity" in flat, name
        for part in ("inode", "size", "`mtime`", "`ctime`"):
            assert part in flat, (name, part)
        assert "same-size rewrite or replacement" in flat, name
        assert "per iteration" in flat, name
        assert "never rescanned merely because a different iteration" in flat, name


def test_a_cold_positive_tail_fallback_can_render_the_entire_backlog_exactly(
        tmp_path, monkeypatch):
    """Task cure-01-04-2 (D.1; DoD 4, criterion 3): the falsifying case the
    review found at e5cf72a2 -- a COLD run (no `render_index.json`, no
    legacy `collapse_index.json` either) with 1,000 non-rendering
    `message_end` padding lines between the streamed text and the run's
    end, asked for a small POSITIVE tail (2). The fast indexed path
    answers `None` (nothing to walk), so the growing-bound fallback runs;
    each round doubles the bound (2, 4, 8, 16, 32) because there is always
    more raw history to read and the doubled request never yields 2
    rendered lines, and once `_TAIL_GROWTH_ROUNDS_BEFORE_FULL_ASK` rounds
    are spent it jumps straight to `_TAIL_FULL_ASK_BOUND` -- which, for
    this fixture's raw content-line count, is the round that finally
    reads (and, on the live surface, is asked to serve) the ENTIRE raw
    backlog, not a bounded slice of it. Both real `ui_server.
    rendered_log_lines` surfaces (the live API proxy and the dead-run
    on-disk merge) still render the exact same last-2-lines tail a full,
    uncapped render of the whole transcript would, proving the fallback's
    correctness even when it reads everything -- and `_TAIL_FULL_ASK_BOUND`
    itself is read from production, never a raised or re-declared literal
    in this test."""
    import re

    gap = json.dumps({"type": "message_end", "message": {"content": []},
                      "padding": "x" * 1000})
    for path in ("disk", "api"):
        sub = tmp_path / f"cold-full-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_iteration_with_recognized_nonrendering_gap(
            run_root, 1, "cold full tail text", gap, 1_000)
        itdir = run_root / "iterations" / "0001"
        assert not (itdir / render_index.INDEX_FILENAME).exists()
        assert not (itdir / collapse_index.INDEX_FILENAME).exists()

        full_raw = "".join(merged_lines(run_root))
        full = render_to_lines(full_raw, tty=False, state=new_render_state())
        ui_server._ABSENT_INDEX_CACHE.clear()
        _wire_path(monkeypatch, sub, run_root, path)

        bounds_asked = []
        if path == "api":
            real_proxy = ui_server._proxy_text

            def observe_proxy(reg, run_id, url, **kw):
                ok, raw = real_proxy(reg, run_id, url, **kw)
                m = re.search(r"tail=(\d+)", url)
                if m:
                    bounds_asked.append((int(m.group(1)), len(raw.encode())))
                return ok, raw

            monkeypatch.setattr(ui_server, "_proxy_text", observe_proxy)
        else:
            real_merged = ui_server.merged_lines

            def observe_merged(root, *a, tail=None, **kw):
                raw = "".join(real_merged(root, *a, tail=tail, **kw))
                if tail is not None:
                    bounds_asked.append((tail, len(raw.encode())))
                return raw.splitlines(keepends=True)

            monkeypatch.setattr(ui_server, "merged_lines", observe_merged)

        live, rendered = ui_server.rendered_log_lines(
            sub / "registry", "probe", 2)
        monkeypatch.undo()

        assert rendered == full[-2:], (path, rendered, full[-2:])
        # The growth loop actually reached the full-ask bound production
        # declares -- never a bound this test raised or re-picked.
        assert bounds_asked, path
        last_bound, last_bytes = bounds_asked[-1]
        assert last_bound == ui_server._TAIL_FULL_ASK_BOUND, (path, bounds_asked)
        # That last, full-ask round's own response is the ENTIRE raw
        # backlog -- the very fetch SPEC.md §11.4 used to claim never
        # happens for a positive tail.
        assert last_bytes == len(full_raw.encode()), (path, bounds_asked,
                                                        len(full_raw.encode()))


def _write_live_iteration_with_padding(run_root, pad_count, final_line, *,
                                       ended, indexed_lines, pad_bytes=1000):
    """Like `_write_live_iteration`, but shaped for the witness tests
    below: a short leading silent line (`checkpoints[0]`, which
    `_covered_prefix_matches` always re-reads), then the VICTIM -- a
    NON-leading, recognized-but-silent padded line -- then `pad_count`
    more padded silent lines and `final_line`. Enough bytes that the
    victim sits well outside `render_index.VERIFY_WINDOW_BYTES` of the
    covered prefix, and a one-line tail's backward walk never reads it,
    so no byte a reader reads can reveal a same-size change to it."""
    lines = [json.dumps({"type": "ignored", "lead": True})] + [json.dumps(
        {"type": "ignored", "padding": "x" * pad_bytes})
        for _ in range(pad_count + 1)] + [final_line]
    return _write_live_iteration(run_root, lines, ended=ended,
                                 indexed_lines=indexed_lines) + (lines,)


def _victim_to_streamed_alpha(transcript, *, replace):
    """The same-size change the witness tests make to raw line 1 (the
    victim above): a silent line becomes an unterminated `text_delta`
    'alpha', so the FULL render now ends in 'alpha' (the final
    `message_end`'s text is already-streamed and suppressed) while a
    stale index would still answer the untouched render's last line.
    `replace` swaps the whole transcript for a same-size copy through a
    rename instead of rewriting it in place."""
    size = transcript.stat().st_size
    if replace:
        data = transcript.read_bytes().split(b"\n")
        data[1] = _text_delta("alpha").encode().ljust(len(data[1]))
        new_path = transcript.with_name("output.jsonl.new")
        new_path.write_bytes(b"\n".join(data))
        new_path.replace(transcript)
    else:
        _rewrite_line_same_size(transcript, 1, _text_delta("alpha"))
    assert transcript.stat().st_size == size


def _assert_exact_fallback(sub, monkeypatch, run_root, expected_last):
    """`load_index`/`bounded_tail` refuse, and both real
    `ui_server.rendered_log_lines` surfaces answer the exact full-render
    tail, never an exception."""
    itdir = run_root / "iterations" / "0001"
    assert render_index.load_index(itdir) is None
    assert render_index.bounded_tail(run_root, 1) is None
    full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                           state=new_render_state())
    assert full[-1].startswith(expected_last), full[-3:]
    for path in ("disk", "api"):
        ui_server._ABSENT_INDEX_CACHE.clear()
        for tail in (1, 2):
            _, rendered = _measure(sub, monkeypatch, run_root, tail, path)
            assert rendered == full[-tail:], (path, tail, rendered)


@pytest.mark.parametrize("tamper", ["rewrite", "replace"])
@pytest.mark.parametrize("coverage", ["complete", "lagging"])
def test_same_size_rewrite_or_replacement_outside_the_window_falls_back_on_both_surfaces(
        tmp_path, monkeypatch, tamper, coverage):
    """Task cure-01-01-4 (petition remedy for cure-01-03 criterion (1)):
    a same-size REWRITE or REPLACEMENT of a NON-leading covered line --
    outside `VERIFY_WINDOW_BYTES`, never read by a one-line tail's
    backward walk -- is refused by the engine-published identity/
    provenance witness alone, for a FULLY covered index and for a LAGGING
    one alike, and both real `ui_server.rendered_log_lines` surfaces fall
    back to the exact full-render tail ('alpha', where the stale index
    would still say 'kept'). Also proves no byte of the victim is read to
    refuse it: `load_index` reads at most the leading line plus the
    window."""
    from test_log_merge_bounded_reads import _count_transcript_bytes

    final = _said("kept")
    for ended in (False, True):
        sub = tmp_path / f"{tamper}-{coverage}-{ended}"
        run_root = sub / "registry" / "runs" / "probe"
        n_pad = 180
        index, size, lines = _write_live_iteration_with_padding(
            run_root, n_pad, final, ended=ended,
            indexed_lines=len([0] * (n_pad + 3)) if coverage == "complete" else n_pad - 18)
        itdir = run_root / "iterations" / "0001"
        transcript = itdir / "output.jsonl"
        victim_end = len(lines[0]) + len(lines[1]) + 2
        assert index["transcriptBytes"] - victim_end > render_index.VERIFY_WINDOW_BYTES
        if coverage == "lagging":
            assert index["transcriptBytes"] < size
        assert render_index.load_index(itdir) is not None
        assert render_index.bounded_tail(run_root, 1)[-1].startswith("kept" if not ended
                                                                   else "  iteration 1 done")
        _victim_to_streamed_alpha(transcript, replace=tamper == "replace")

        counts = _count_transcript_bytes(monkeypatch)
        assert render_index.load_index(itdir) is None, (tamper, coverage, ended)
        assert counts[0] == 0, "refused by stat() alone, before any read"
        monkeypatch.undo()
        _assert_exact_fallback(sub, monkeypatch, run_root, "alpha")


@pytest.mark.parametrize("protocol", ["append", "feed-only"])
def test_a_non_leading_out_of_window_rewrite_stays_refused_across_later_appends(
        tmp_path, monkeypatch, protocol):
    """Task cure-01-01-4 criterion (2), the exact case the previous
    attempt missed: the writer publishes coverage, raw line 1 (not the
    leading line, outside the window, never read by the tail) is rewritten
    same-size, and THEN the same writer appends and flushes 20 more
    legitimate lines. Growth never blesses the modified prefix: whether
    the lines arrive through the engine's own `LiveRenderIndex.append`
    (custody checked before each write) or are written and merely
    `feed`-ed (a chain of custody the writer cannot vouch for), the
    published index is `poisoned`, it is refused from the very first
    post-tamper append on, and both surfaces answer 'alpha' -- the full
    render -- never the stale 'beta'."""
    pad = json.dumps({"type": "ignored", "padding": "x" * 1000})
    sub = tmp_path / protocol
    run_root = sub / "registry" / "runs" / "probe"
    itdir = run_root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    (itdir / "meta.json").write_text(json.dumps({
        "number": 1, "phase": "worker", "model": "stub-model", "approach": 1,
        "startedAt": "2025-01-01T00:00:00Z", "exitCode": 0, "error": None,
        "usage": {"totalTokens": 1}}))
    transcript = itdir / "output.jsonl"
    live = render_index.LiveRenderIndex()

    def write(out, line, *, flush=True):
        raw = (line + "\n").encode()
        if protocol == "append":
            live.append(out, raw, transcript)
        else:
            out.write(raw)
            out.flush()
            live.feed(raw)
        if flush:
            live.flush(itdir)

    # A `feed`-only writer can vouch only for what it published in ONE
    # go (its first publish takes custody of the transcript as it
    # stands); the engine's own `append` writer publishes at its cadence.
    lead = json.dumps({"type": "ignored", "lead": True})
    with transcript.open("ab") as out:
        for line in [lead, pad] + [pad] * 150 + [_said("beta")]:
            write(out, line, flush=protocol == "append")
    assert live.flush(itdir, force=True)
    doc = json.loads((itdir / render_index.INDEX_FILENAME).read_text())
    assert doc["poisoned"] is False
    assert doc["transcriptBytes"] - len(lead) - len(pad) - 2 > render_index.VERIFY_WINDOW_BYTES
    assert render_index.load_index(itdir) is not None
    assert render_index.bounded_tail(run_root, 1) == ["beta"]

    _victim_to_streamed_alpha(transcript, replace=False)
    with transcript.open("ab") as out:
        write(out, pad)
        # Refused from the first legitimate append after the tamper on.
        assert render_index.load_index(itdir) is None
        for _ in range(19):
            write(out, pad)
    assert live.flush(itdir, force=True)
    doc = json.loads((itdir / render_index.INDEX_FILENAME).read_text())
    assert doc["poisoned"] is True, "20 legitimate appends must not bless the rewrite"
    _assert_exact_fallback(sub, monkeypatch, run_root, "alpha")


def test_a_hub_cached_index_is_refused_after_an_out_of_window_rewrite(
        tmp_path, monkeypatch):
    """Task cure-01-01-4 criterion (2), cached indexes: an iteration with
    NO on-disk render index has its index built and cached by the hub
    (`ui_server._ABSENT_INDEX_CACHE`, keyed by iteration number and the
    transcript's own identity witness). A same-size rewrite of a
    non-leading, out-of-window covered line leaves the size unchanged
    (still visible through `latest_iteration_cache_key`) yet moves the
    mtime, so the cached index's own witness no longer holds:
    `bounded_tail(..., index=cached)` refuses it, and the next call on
    both surfaces answers the exact full-render 'alpha' instead of the
    cached, stale 'kept'."""
    for path in ("disk", "api"):
        sub = tmp_path / path
        run_root = sub / "registry" / "runs" / "probe"
        _write_live_iteration_with_padding(run_root, 180, _said("kept"),
                                           ended=False, indexed_lines=1)
        itdir = run_root / "iterations" / "0001"
        (itdir / render_index.INDEX_FILENAME).unlink()
        transcript = itdir / "output.jsonl"
        ui_server._ABSENT_INDEX_CACHE.clear()
        _, rendered = _measure(sub, monkeypatch, run_root, 1, path)
        assert rendered == ["kept"]
        identity, cached = ui_server._ABSENT_INDEX_CACHE["probe"][1]
        assert render_index.bounded_tail(run_root, 1, index=cached) == ["kept"]

        _victim_to_streamed_alpha(transcript, replace=False)
        assert render_index.latest_iteration_cache_key(run_root) == (1, identity[1])
        assert render_index.bounded_tail(run_root, 1, index=cached) is None
        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert full[-1] == "alpha"
        for tail in (1, 2):
            _, rendered = _measure(sub, monkeypatch, run_root, tail, path)
            assert rendered == full[-tail:], (path, tail, rendered)


@pytest.mark.parametrize("during_read", ["foreign-rewrite", "engine-append"])
def test_a_read_sees_one_consistent_transcript_and_index_snapshot(
        tmp_path, during_read):
    """Task cure-01-01-4 criterion (2), snapshot consistency: a bounded
    read validates the witness against one `stat()`, reads only up to that
    snapshot's size, and re-checks the witness after reading. A same-size
    rewrite landing WHILE the read is in progress makes it refuse (`None`,
    the caller then falls back) rather than answer from a half-seen
    transcript; the engine's own provenance-stamped append landing at
    that same moment is accepted, and the answer is the snapshot's own
    exact tail."""
    run_root = tmp_path / "registry" / "runs" / "probe"
    itdir = run_root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    (itdir / "meta.json").write_text(json.dumps({
        "number": 1, "phase": "worker", "model": "stub-model", "approach": 1,
        "startedAt": "2025-01-01T00:00:00Z", "exitCode": 0, "error": None,
        "usage": {"totalTokens": 1}}))
    transcript = itdir / "output.jsonl"
    live = render_index.LiveRenderIndex()
    pad = json.dumps({"type": "ignored", "padding": "x" * 1000})
    out = transcript.open("ab")
    try:
        for line in [json.dumps({"type": "ignored", "lead": True}), pad]:
            live.append(out, (line + "\n").encode(), transcript)
        for line in [pad] * 150 + [_said("beta")]:
            live.append(out, (line + "\n").encode(), transcript)
        assert live.flush(itdir, force=True)
        assert render_index.bounded_tail(run_root, 1) == ["beta"]

        real = render_index._prepend_covered
        fired = []

        def interleaved(*a, **k):
            if not fired:
                fired.append(True)
                if during_read == "foreign-rewrite":
                    _victim_to_streamed_alpha(transcript, replace=False)
                else:
                    live.append(out, (_said("later") + "\n").encode(), transcript)
            return real(*a, **k)

        render_index._prepend_covered = interleaved
        try:
            result = render_index.bounded_tail(run_root, 1)
        finally:
            render_index._prepend_covered = real
        assert fired
        if during_read == "foreign-rewrite":
            assert result is None
            assert render_index.load_index(itdir) is None
        else:
            assert result == ["beta"], result
            assert render_index.load_index(itdir) is not None
            assert render_index.bounded_tail(run_root, 1) == ["later"]
    finally:
        out.close()


def test_a_rewrite_outside_the_window_stays_refused_across_a_later_legitimate_append(
        tmp_path, monkeypatch):
    """Task cure-01-01-4 criterion (2): a same-size rewrite caught by
    `LiveRenderIndex.flush`'s own bounded self-check, WHILE the tampered
    line was still inside that self-check's own window, must stay refused
    once a LATER legitimate append (fed and flushed by the exact same
    writer object -- never a from-scratch rebuild) has since pushed the
    tampered line outside the window relative to the NEW, larger covered
    prefix. Never blessing a previously modified prefix just because
    coverage grew: the persisted `poisoned` witness carries the
    invalidation forward, at no extra read cost beyond the bounded
    self-check `flush` already pays on every publish."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 220})
    said_beta = _said("beta")
    for path in ("disk", "api"):
        sub = tmp_path / f"persist-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        itdir = run_root / "iterations" / "0001"
        itdir.mkdir(parents=True)
        meta = {"number": 1, "phase": "worker", "model": "stub-model",
                "approach": 1, "startedAt": "2025-01-01T00:00:00Z",
                "exitCode": 0, "error": None, "usage": {"totalTokens": 1}}
        (itdir / "meta.json").write_text(json.dumps(meta))
        transcript = itdir / "output.jsonl"

        live = render_index.LiveRenderIndex()
        with transcript.open("ab") as out:
            for line in (silent, said_beta, silent):
                raw = (line + "\n").encode()
                out.write(raw)
                out.flush()
                live.feed(raw)
        assert live.flush(itdir, force=True)
        assert render_index.load_index(itdir) is not None

        # A same-size rewrite of the leading (already-published) line --
        # still comfortably inside `flush`'s own next self-check window.
        _rewrite_line_same_size(transcript, 0, json.dumps(
            {"type": "ignored", "padding": "y" * 220}))
        doc_before_growth = json.loads((itdir / render_index.INDEX_FILENAME).read_text())
        assert doc_before_growth["poisoned"] is False, (
            "tampering the disk file alone must not retroactively poison "
            "an index nothing has re-published yet")

        # The SAME writer object grows the transcript well past the
        # verification window, in several small flush batches -- exactly
        # `pump()`'s own live cadence, never one `force=True` rebuild.
        big_pad = json.dumps({"type": "ignored", "padding": "z" * 2000})
        with transcript.open("ab") as out:
            for _ in range(120):
                raw = (big_pad + "\n").encode()
                out.write(raw)
                out.flush()
                live.feed(raw)
                live.flush(itdir)
        assert live.flush(itdir, force=True)

        doc = json.loads((itdir / render_index.INDEX_FILENAME).read_text())
        assert doc["poisoned"] is True, "the growth must not wash away the earlier tamper"
        covered = doc["transcriptBytes"]
        assert covered - len(silent) - 1 > render_index.VERIFY_WINDOW_BYTES, (
            "the tampered line must now sit outside the CURRENT window", covered)

        assert render_index.load_index(itdir) is None
        assert render_index.bounded_tail(run_root, 1) is None

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        _, rendered = _measure(sub, monkeypatch, run_root, 1, path)
        assert rendered == full[-1:], (path, rendered)


def test_genuine_append_only_growth_across_many_flushes_never_poisons_the_index(
        tmp_path, monkeypatch):
    """Regression guard alongside the rejection tests above: ordinary,
    untouched, append-only growth through the engine's own writer
    (`LiveRenderIndex.append`, exactly `engine/runner.py`'s `pump()`
    call, then `flush` at its own cadence) never trips `poisoned`; the
    index validates while its coverage LAGS the provenance-stamped
    transcript between flushes and once flushed, and the whole call stays
    bounded and exact on both real `ui_server.rendered_log_lines`
    surfaces."""
    silent = json.dumps({"type": "ignored", "padding": "x" * 900})
    said_gamma = _said("gamma")
    for path in ("disk", "api"):
        sub = tmp_path / f"safe-growth-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        itdir = run_root / "iterations" / "0001"
        itdir.mkdir(parents=True)
        meta = {"number": 1, "phase": "worker", "model": "stub-model",
                "approach": 1, "startedAt": "2025-01-01T00:00:00Z",
                "exitCode": 0, "error": None, "usage": {"totalTokens": 1}}
        (itdir / "meta.json").write_text(json.dumps(meta))
        transcript = itdir / "output.jsonl"

        live = render_index.LiveRenderIndex()
        with transcript.open("a") as out:
            for _ in range(150):
                live.append(out, silent + "\n", transcript)
                live.flush(itdir)
            live.append(out, said_gamma + "\n", transcript)
            lagging = render_index.load_index(itdir)
            assert lagging is not None
            assert lagging["transcriptBytes"] < transcript.stat().st_size
            ui_server._ABSENT_INDEX_CACHE.clear()
            measured, rendered = _measure(sub, monkeypatch, run_root, 1, path)
            assert rendered == ["gamma"], (path, rendered)
            assert measured <= _FROZEN_CAP, measured
        assert live.flush(itdir, force=True)

        doc = json.loads((itdir / render_index.INDEX_FILENAME).read_text())
        assert doc["poisoned"] is False
        assert doc["transcriptBytes"] > render_index.VERIFY_WINDOW_BYTES
        assert render_index.load_index(itdir) is not None

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        measured, rendered = _measure(sub, monkeypatch, run_root, 1, path)
        assert rendered == full[-1:] == ["gamma"], (path, rendered)
        assert measured <= _FROZEN_CAP, measured


@pytest.mark.parametrize("coverage", ["complete", "lagging"])
def test_same_size_rewrite_with_preserved_mtime_falls_back_exactly(
        tmp_path, monkeypatch, coverage):
    """Task cure-01-03-2 (PRD C.1/C.3, D.1; DoD 3; cure-01-03 and
    cure-01-01-4 retry): the exact gap cure-01-01-4's mtime-only witness
    missed -- a same-size, out-of-window rewrite of a covered line,
    followed by an ORDINARY `os.utime` call that restores the
    transcript's original mtime (no forged provenance stamp, just an
    everyday tool that preserves timestamps across a rewrite). Mtime
    alone therefore still agrees with the published witness; only ctime
    moved, since a normal write can never restore ctime the way it can
    mtime. `load_index`'s witness check now covers ctime too, refuses the
    tampered index for both fully covered and lagging coverage, and both
    real `ui_server.rendered_log_lines` surfaces fall back to the exact
    full-render tail ('alpha') rather than the stale 'kept'."""
    final = _said("kept")
    for ended in (False, True):
        sub = tmp_path / f"preserved-mtime-{coverage}-{ended}"
        run_root = sub / "registry" / "runs" / "probe"
        n_pad = 180
        index, size, lines = _write_live_iteration_with_padding(
            run_root, n_pad, final, ended=ended,
            indexed_lines=len([0] * (n_pad + 3)) if coverage == "complete" else n_pad - 18)
        itdir = run_root / "iterations" / "0001"
        transcript = itdir / "output.jsonl"
        victim_end = len(lines[0]) + len(lines[1]) + 2
        assert index["transcriptBytes"] - victim_end > render_index.VERIFY_WINDOW_BYTES
        if coverage == "lagging":
            assert index["transcriptBytes"] < size
        assert render_index.load_index(itdir) is not None
        assert render_index.bounded_tail(run_root, 1)[-1].startswith(
            "kept" if not ended else "  iteration 1 done")

        st_before = transcript.stat()
        _victim_to_streamed_alpha(transcript, replace=False)
        os.utime(transcript, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))
        st_after = transcript.stat()
        assert st_after.st_mtime_ns == st_before.st_mtime_ns, "mtime must stay preserved"
        assert st_after.st_ctime_ns != st_before.st_ctime_ns, "ctime always moves on a write"

        assert render_index.load_index(itdir) is None, (coverage, ended)
        _assert_exact_fallback(sub, monkeypatch, run_root, "alpha")


def test_a_hub_cached_index_is_refused_after_a_preserved_mtime_rewrite(
        tmp_path, monkeypatch):
    """Task cure-01-03-2, cached indexes: the hub's own absent-index
    cache key (`(inode, size, mtime_ns, ctime_ns)`) also catches a
    preserved-mtime rewrite -- mtime alone would still match the cached
    identity, but ctime does not, so both a fresh cache lookup misses and
    a previously cached index handed straight to `bounded_tail(index=)`
    is refused, and both real `ui_server.rendered_log_lines` surfaces
    answer the exact full-render 'alpha' instead of the cached, stale
    'kept'."""
    for path in ("disk", "api"):
        sub = tmp_path / f"cached-preserved-{path}"
        run_root = sub / "registry" / "runs" / "probe"
        _write_live_iteration_with_padding(run_root, 180, _said("kept"),
                                           ended=False, indexed_lines=1)
        itdir = run_root / "iterations" / "0001"
        (itdir / render_index.INDEX_FILENAME).unlink()
        transcript = itdir / "output.jsonl"
        ui_server._ABSENT_INDEX_CACHE.clear()
        _, rendered = _measure(sub, monkeypatch, run_root, 1, path)
        assert rendered == ["kept"]
        identity, cached = ui_server._ABSENT_INDEX_CACHE["probe"][1]

        st_before = transcript.stat()
        _victim_to_streamed_alpha(transcript, replace=False)
        os.utime(transcript, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))
        st_after = transcript.stat()
        assert st_after.st_mtime_ns == st_before.st_mtime_ns
        assert st_after.st_ctime_ns != st_before.st_ctime_ns

        assert render_index.bounded_tail(run_root, 1, index=cached) is None
        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert full[-1] == "alpha"
        for tail in (1, 2):
            _, rendered = _measure(sub, monkeypatch, run_root, tail, path)
            assert rendered == full[-tail:], (path, tail, rendered)


def test_a_preserved_mtime_rewrite_poisons_a_later_legitimate_append(
        tmp_path, monkeypatch):
    """Task cure-01-03-2 criterion (2): a same-size, out-of-window rewrite
    that restores the transcript's original mtime via `os.utime` -- ctime
    moved, mtime did not -- still poisons the SAME writer's custody check
    (`LiveRenderIndex._check_custody`, whose own stat key now carries
    ctime too) on its very next legitimate append, so growth can never
    bless the tampered prefix: both surfaces answer the full render
    ('alpha'), never the stale 'beta'."""
    pad = json.dumps({"type": "ignored", "padding": "x" * 1000})
    sub = tmp_path / "preserved-mtime-append"
    run_root = sub / "registry" / "runs" / "probe"
    itdir = run_root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    (itdir / "meta.json").write_text(json.dumps({
        "number": 1, "phase": "worker", "model": "stub-model", "approach": 1,
        "startedAt": "2025-01-01T00:00:00Z", "exitCode": 0, "error": None,
        "usage": {"totalTokens": 1}}))
    transcript = itdir / "output.jsonl"
    live = render_index.LiveRenderIndex()
    lead = json.dumps({"type": "ignored", "lead": True})
    with transcript.open("ab") as out:
        for line in [lead, pad] + [pad] * 150 + [_said("beta")]:
            live.append(out, (line + "\n").encode(), transcript)
    assert live.flush(itdir, force=True)
    assert render_index.load_index(itdir) is not None
    assert render_index.bounded_tail(run_root, 1) == ["beta"]

    st_before = transcript.stat()
    _victim_to_streamed_alpha(transcript, replace=False)
    os.utime(transcript, ns=(st_before.st_atime_ns, st_before.st_mtime_ns))
    st_after = transcript.stat()
    assert st_after.st_mtime_ns == st_before.st_mtime_ns
    assert st_after.st_ctime_ns != st_before.st_ctime_ns
    assert render_index.load_index(itdir) is None

    with transcript.open("ab") as out:
        for _ in range(20):
            live.append(out, (pad + "\n").encode(), transcript)
    assert live.flush(itdir, force=True)
    doc = json.loads((itdir / render_index.INDEX_FILENAME).read_text())
    assert doc["poisoned"] is True, "20 legitimate appends must not bless the preserved-mtime rewrite"
    _assert_exact_fallback(sub, monkeypatch, run_root, "alpha")
