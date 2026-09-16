"""Task 043 (PRD H.1): `log_render.render_tail_lines` reads backwards from
the newest events in bounded chunks, growing the window when a chunk still
yields fewer than the requested rendered-line count, instead of
`render_to_lines`'s render-everything-then-trim.

The fixture below builds a merged NDJSON blob out of 8 iterations, each
contributing exactly 3 rendered lines (a `ralphd.iteration` start boundary,
one `message_end` text line, and the matching end boundary) via
`log_merge.boundary_line` -- the same synthesized boundary shape a real
merged run transcript carries. Requesting a 10-line tail spans more than
one iteration and, with `render_tail_lines`'s chunk doubling (1, 2, 4, 8
boundaries back), forces the window to grow past its first (and second,
and third) attempt before it has enough lines -- pinning "more than one
chunk" concretely rather than just by line count.

The two parse-count tests below are the ones that pin "bounded" as WORK
rather than as output size: they prepend an arbitrary amount of older
history in front of the very same newest tail and assert the number of
`json.loads` calls -- the per-line cost both the boundary scan and the
render pay -- does not move, once directly on `render_tail_lines` and once
through the hub route (`ui_server.rendered_log_lines`) that serves
`GET /api/runs/<id>/logs`. A render-everything-then-trim implementation
(which is what this path did before task 043) fails both, because its
parse count grows one call per historical event.
"""

from __future__ import annotations

import json
import re

from ralphd.cli import log_render as log_render_mod
from ralphd.cli.log_render import new_render_state, render_tail_lines, render_to_lines
from ralphd.log_merge import boundary_line


def _iteration_meta(n: int) -> dict:
    return {
        "number": n, "phase": "worker", "model": "stub-model", "approach": 1,
        "startedAt": f"2025-01-01T00:{n:02d}:00Z",
        "endedAt": f"2025-01-01T00:{n:02d}:30Z",
        "exitCode": 0, "error": None, "usage": {"totalTokens": 10 * n},
    }


def _message_end_line(text: str) -> str:
    return json.dumps({"type": "message_end",
                        "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def _fixture_blob(num_iterations: int) -> str:
    """`num_iterations` iterations, 3 rendered lines each (start boundary,
    one text line, end boundary) -- 24 rendered lines for 8 iterations."""
    parts = []
    for n in range(1, num_iterations + 1):
        meta = _iteration_meta(n)
        parts.append(boundary_line(meta, "start"))
        parts.append(_message_end_line(f"iteration {n} did some work"))
        parts.append(boundary_line(meta, "end"))
    return "".join(parts)


def test_bounded_tail_returns_requested_line_count_across_growing_chunks():
    raw_text = _fixture_blob(8)

    result = render_tail_lines(raw_text, tty=False, n=10)

    # 10 lines spans iterations 5-8 (4 * 3 = 12 >= 10 lines): the single
    # most recent boundary (iteration 8's start) yields only 3 lines, so
    # the window must grow (1 -> 2 -> 4 -> 8 boundaries back) more than
    # once before it has enough -- this is the "more than one chunk" case.
    assert len(result) == 10

    full = render_to_lines(raw_text, tty=False, state=new_render_state())
    assert result == full[-10:]


def test_bounded_tail_falls_back_to_full_blob_when_tail_exceeds_history():
    raw_text = _fixture_blob(3)
    full = render_to_lines(raw_text, tty=False, state=new_render_state())

    result = render_tail_lines(raw_text, tty=False, n=1000)

    assert result == full


def _blob_with_history(old_iterations: int) -> str:
    """The SAME newest 8 iterations as `_fixture_blob(8)`, with
    `old_iterations` further iterations of history in front of them -- so
    two blobs built with different `old_iterations` share a byte-identical
    tail and differ only in how much history precedes it."""
    old = []
    for _ in range(old_iterations):
        meta = _iteration_meta(0)
        old.append(boundary_line(meta, "start"))
        old.append(_message_end_line("older history nobody asked for"))
        old.append(boundary_line(meta, "end"))
    return "".join(old) + _fixture_blob(8)


class _CountingJson:
    """Stands in for `log_render`'s `json` module and counts `loads` calls:
    one call per raw NDJSON line either the boundary scan or the render
    actually looks at, which is the per-event work H.1 bounds."""

    def __init__(self, real):
        self._real = real
        self.loads_calls = 0

    def loads(self, *args, **kwargs):
        self.loads_calls += 1
        return self._real.loads(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _counted(monkeypatch, fn):
    """Run `fn()` with `log_render`'s json parsing counted; return
    `(result, loads_calls)`."""
    counter = _CountingJson(json)
    monkeypatch.setattr(log_render_mod, "json", counter)
    try:
        result = fn()
    finally:
        monkeypatch.undo()
    return result, counter.loads_calls


def test_bounded_tail_parse_work_does_not_grow_with_older_history(monkeypatch):
    small = _blob_with_history(0)
    large = _blob_with_history(2000)

    small_lines, small_calls = _counted(
        monkeypatch, lambda: render_tail_lines(small, tty=False, n=10))
    large_lines, large_calls = _counted(
        monkeypatch, lambda: render_tail_lines(large, tty=False, n=10))

    # Same tail rendered either way ...
    assert small_lines == large_lines
    assert len(large_lines) == 10
    assert large_lines == render_to_lines(
        large, tty=False, state=new_render_state())[-10:]
    # ... for the same parse work, though `large` carries 6000 more raw
    # events in front of it. A render-everything-then-trim path would need
    # 6000 more `json.loads` calls here.
    assert large_calls == small_calls
    # And that work is a small multiple of the requested tail, not of the
    # 6024-event blob.
    assert large_calls < 100


def test_hub_log_tail_route_parse_work_does_not_grow_with_older_history(
        monkeypatch, tmp_path):
    """The wiring half: `GET /api/runs/<id>/logs`'s server-side renderer
    (`ui_server.rendered_log_lines`, the function the route calls) must go
    through the bounded tail, not render the whole backlog and slice."""
    from ralphd.cli import ui_server

    (tmp_path / "runs" / "bounded-tail").mkdir(parents=True)

    def _served(blob):
        monkeypatch.setattr(ui_server, "_proxy_text",
                            lambda *a, **k: (True, blob))
        out = _counted(monkeypatch, lambda: ui_server.rendered_log_lines(
            tmp_path, "bounded-tail", 10))
        monkeypatch.undo()
        return out

    small = _blob_with_history(0)
    large = _blob_with_history(2000)
    (small_result, small_calls) = _served(small)
    (large_result, large_calls) = _served(large)

    assert small_result[0] is True and large_result[0] is True
    assert large_result[1] == small_result[1]
    assert len(large_result[1]) == 10
    assert large_result[1] == render_to_lines(
        large, tty=False, state=new_render_state())[-10:]
    assert large_calls == small_calls
    assert large_calls < 100


def test_bounded_tail_empty_or_non_positive_n_returns_no_lines():
    raw_text = _fixture_blob(2)
    assert render_tail_lines(raw_text, tty=False, n=0) == []
    assert render_tail_lines("", tty=False, n=5) == []


def _thinking_flood_lines(deltas: int) -> list[str]:
    """A `thinking_start` plus `deltas` `thinking_delta` events, wrapped in
    the `message_update`/`assistantMessageEvent` shape `log_render`'s
    `_render_message_update` reads -- the exact flood task 014's docstring
    names (many deltas, ONE `[thinking\u2026]` line, guarded by
    `state['thinking_seen']`). Used to stress that guard's reset at a
    chunk's chosen restart boundary, not just its steady-state behaviour."""
    lines = [json.dumps({"type": "message_update",
                          "assistantMessageEvent": {"type": "thinking_start"}})]
    lines += [json.dumps({"type": "message_update",
                          "assistantMessageEvent": {"type": "thinking_delta",
                                                     "delta": f"step {i}"}})
              for i in range(deltas)]
    return lines


def _fixture_blob_with_thinking_flood(num_iterations: int, flood_iteration: int,
                                      deltas: int) -> str:
    """Same per-iteration shape as `_fixture_blob`, except `flood_iteration`
    also carries a `deltas`-long thinking flood right after its start
    boundary -- collapsing to exactly one extra `[thinking\u2026]` line in
    the rendered output, however many raw delta events it took to get
    there."""
    parts = []
    for n in range(1, num_iterations + 1):
        meta = _iteration_meta(n)
        parts.append(boundary_line(meta, "start"))
        if n == flood_iteration:
            for line in _thinking_flood_lines(deltas):
                parts.append(line + "\n")
        parts.append(_message_end_line(f"iteration {n} did some work"))
        parts.append(boundary_line(meta, "end"))
    return "".join(parts)


def test_bounded_tail_is_byte_identical_to_full_render_across_a_many_delta_thinking_block():
    """Task 044 (PRD H.2): the bounded-tail path (`render_tail_lines`) and
    the previous full-render path (`render_to_lines`, unchanged by task
    043) must render the SAME bytes for the same tail -- not merely the
    same line count -- once a many-delta thinking block sits inside the
    requested window. The flood lands in the NEWEST iteration so a
    10-line tail (iterations 6-8) spans it, forcing the bounded path's
    chunk-grown window to restart at the boundary immediately before the
    flood and re-render it from a fresh `new_render_state()`, exactly as
    the full render's single pass would have."""
    raw_text = _fixture_blob_with_thinking_flood(8, flood_iteration=8, deltas=500)

    full = render_to_lines(raw_text, tty=False, state=new_render_state())
    tail = render_tail_lines(raw_text, tty=False, n=10)

    assert len(tail) == 10
    # The tail includes the flood's one collapsed `[thinking...]` line, not
    # 500 of them -- confirms the fixture actually exercises the guard.
    assert any("thinking" in line for line in tail)
    assert tail == full[-10:]
    # Byte-for-byte, not just element-wise string equality: the two render
    # paths must produce the identical newline-joined bytes an operator
    # would actually see, trailing newline included.
    tail_bytes = ("\n".join(tail) + "\n").encode()
    full_tail_bytes = ("\n".join(full[-10:]) + "\n").encode()
    assert tail_bytes == full_tail_bytes


def _write_dead_run_iteration(run_root, n: int, lines: list[str]) -> None:
    """Write one finished iteration's `meta.json` + `output.jsonl` directly
    to disk -- the shape `log_merge.merge_entries` reads and the same shape
    `tests/test_log_merge.py::_iteration` builds -- so `merged_lines` (and,
    through it, `ui_server.rendered_log_lines`'s on-disk fallback) has a
    real, finished-run transcript to serve with no live container behind
    it."""
    d = run_root / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    meta = {"number": n, "phase": "worker", "model": "stub-model", "approach": 1,
            "startedAt": f"2025-01-01T00:{n:02d}:00Z",
            "endedAt": f"2025-01-01T00:{n:02d}:30Z",
            "exitCode": 0, "error": None, "usage": {"totalTokens": 10 * n}}
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "output.jsonl").write_text("\n".join(lines) + "\n")


def test_bounded_tail_is_byte_identical_to_full_render_for_a_dead_run_on_disk_merge(
        monkeypatch, tmp_path):
    """Task 044 (PRD H.2), the on-disk-merge half: a dead run (no live
    container, so `ui_server._proxy_text` fails and `rendered_log_lines`
    falls back to `log_merge.merged_lines`) must serve the same bytes for
    its bounded tail as for its full render, across the same many-delta
    thinking block -- proving the byte-identity holds for the ACTUAL
    dead-run serving path, not only for an in-memory NDJSON blob."""
    from ralphd.cli import ui_server

    run_dir = tmp_path / "runs" / "dead-run"
    run_dir.mkdir(parents=True)
    for n in range(1, 8):
        _write_dead_run_iteration(run_dir, n, [
            json.dumps({"type": "message_end", "message": {"content": [
                {"type": "text", "text": f"iteration {n} did some work"}]}})])
    flood_lines = _thinking_flood_lines(500) + [
        json.dumps({"type": "message_end", "message": {"content": [
            {"type": "text", "text": "iteration 8 did some work"}]}})]
    _write_dead_run_iteration(run_dir, 8, flood_lines)

    monkeypatch.setattr(ui_server, "_proxy_text", lambda *a, **k: (False, ""))

    live_full, full_lines = ui_server.rendered_log_lines(tmp_path, "dead-run", None)
    live_tail, tail_lines = ui_server.rendered_log_lines(tmp_path, "dead-run", 10)

    assert live_full is False and live_tail is False
    assert len(tail_lines) == 10
    assert any("thinking" in line for line in tail_lines)
    assert tail_lines == full_lines[-10:]
    tail_bytes = ("\n".join(tail_lines) + "\n").encode()
    full_tail_bytes = ("\n".join(full_lines[-10:]) + "\n").encode()
    assert tail_bytes == full_tail_bytes


# ---------------------------------------------------------------------------
# Task 006 (PRD F): a tail that cuts INTO an iteration -- not at one of its
# boundaries -- must carry the `text_seen`/`thinking_seen`/`toolcall_seen`
# collapse state the OMITTED earlier raw lines of that SAME iteration would
# have set, so a `message_end` line inside the retained window renders
# exactly as it would from a full render, and a many-delta thinking flood
# inside the retained window still collapses to one `[thinking...]` line
# even though its own `thinking_start` fell outside the window.
#
# The fixture below packs ALL THREE guards into one iteration -- a tool
# call, a streamed text block, and a many-delta thinking flood -- then
# repeats all three in a single `message_end` (the shape a real assistant
# turn's final message takes: the wire replays everything it already
# streamed). A bounded tail sized to land inside the flood (or right after
# it) omits the tool call and the text stream entirely; without task 006's
# fix, `message_end`'s repeat of them would print AGAIN (a duplicated text
# line) and/or the flood would print `[thinking...]` a second time -- while
# `merge_entries_tail`'s pre-task-005 "reads the whole file" behavior never
# hit this at all, because it never fabricated a lying restart point in the
# first place.


def _ignored_filler_lines(count: int, line_bytes: int) -> list[str]:
    """`count` opaque `{"type": "ignored"}` events (task cure-01-01-2) --
    `render_log_line` never recognizes `"ignored"` -- each padded to
    EXACTLY `line_bytes` bytes (the line plus its trailing newline), so a
    regression can pin the exact BYTE size of a gap, not merely its event
    count, matching cure-01-01-2's own named scenario sizes (1,000
    1,000-byte events / 9,000 1,024-byte events) precisely rather than
    approximately."""
    base = json.dumps({"type": "ignored", "pad": ""})
    pad_len = max(0, line_bytes - len(base) - 1)
    line = json.dumps({"type": "ignored", "pad": "x" * pad_len})
    assert len(line) + 1 == line_bytes, (len(line) + 1, line_bytes)
    return [line + "\n" for _ in range(count)]


def _tool_call_lines(tool_call_id: str, name: str, args: dict, result: str = "ok",
                     ignored_gap: int = 0, ignored_line_bytes: int | None = None) -> list[str]:
    """A `tool_execution_start` (with `args`) and its matching
    `tool_execution_end`, optionally separated by `ignored_gap` opaque raw
    events `render_log_line` does not recognize (task cure-01-02, #195):
    large enough, the gap pushes the start beyond a single
    `_TAIL_READ_CHUNK` read, so a bounded tail landing on just the end
    event must still find the start's arguments by TARGETED look-back --
    not merely by reading past a recognizable run, since `{"type":
    "ignored"}` filler is not one. `ignored_line_bytes`, when given, sizes
    each filler event to exactly that many bytes (`_ignored_filler_lines`,
    cure-01-01-2) instead of the small default shape -- letting a
    regression pin the gap's total BYTE size, not just its event count."""
    if ignored_line_bytes is not None:
        ignored = _ignored_filler_lines(ignored_gap, ignored_line_bytes)
    else:
        ignored = [json.dumps({"type": "ignored", "seq": i}) + "\n"
                  for i in range(ignored_gap)]
    return ([json.dumps({"type": "tool_execution_start", "toolCallId": tool_call_id,
                        "toolName": name, "args": args}) + "\n"]
            + ignored
            + [json.dumps({"type": "tool_execution_end", "toolCallId": tool_call_id,
                          "toolName": name, "result": result, "isError": False}) + "\n"])


def _text_stream_lines(text: str) -> list[str]:
    return [
        json.dumps({"type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": text}}) + "\n",
        json.dumps({"type": "message_update",
                    "assistantMessageEvent": {"type": "text_end"}}) + "\n",
    ]


_RESTART_TOOL_NAME = "bash"
_RESTART_TOOL_ARGS = {"command": "echo hello"}
_RESTART_TEXT = "hello world this is the streamed text"


def _message_end_repeating_everything() -> str:
    """The turn's final `message_end`, replaying the SAME tool call, text,
    and thinking block already streamed earlier in the same iteration --
    the exact shape that makes `text_seen`/`thinking_seen`/`toolcall_seen`
    matter: a full render suppresses all three (already shown), and a
    bounded tail that starts AFTER the events which set those guards must
    suppress them too."""
    return json.dumps({"type": "message_end", "message": {"content": [
        {"type": "toolCall", "name": _RESTART_TOOL_NAME, "arguments": _RESTART_TOOL_ARGS},
        {"type": "text", "text": _RESTART_TEXT},
        {"type": "thinking", "thinking": "..."},
    ]}}) + "\n"


def _restart_iteration_lines(deltas: int, ignored_gap: int = 0,
                             ignored_line_bytes: int | None = None,
                             text_thinking_gap_lines: list[str] | None = None,
                             post_thinking_gap_lines: list[str] | None = None) -> list[str]:
    """`text_thinking_gap_lines`, when given, is opaque filler (task
    cure-01-01-2) spliced BETWEEN the streamed text block and the thinking
    flood -- unlike `ignored_gap` (which only ever separated the tool
    call's own start/end), this widens the gap a still-open TEXT run's own
    start sits behind, the other half of PRD F.1/F.2/F.4 this task's own
    regressions name.

    `post_thinking_gap_lines`, when given, is opaque filler (task
    cure-01-03-2) spliced AFTER the thinking flood and BEFORE the
    `message_end` that repeats everything: widens the gap between the
    LAST relevant event of the whole iteration and the `message_end`
    that needs both `textSeen` and `thinkingSeen` resolved, so
    `tail_lines` for a small requested tail is nothing but opaque filler
    plus that `message_end` -- no `_TYPE_FLAG_GROUPS` member anywhere in
    `tail_lines` itself, unlike scenarios 1-4 above (whose `tail_lines`
    always still holds a live thinking delta or tool call event for a
    small `n`)."""
    return (_tool_call_lines("t1", _RESTART_TOOL_NAME, _RESTART_TOOL_ARGS,
                             ignored_gap=ignored_gap, ignored_line_bytes=ignored_line_bytes)
            + _text_stream_lines(_RESTART_TEXT)
            + (text_thinking_gap_lines or [])
            + [line + "\n" for line in _thinking_flood_lines(deltas)]
            + (post_thinking_gap_lines or [])
            + [_message_end_repeating_everything()])


def _blob_with_iteration3_lines(iteration3_lines: list[str]) -> str:
    """Same per-iteration shape as `_fixture_blob`, except iteration 3
    carries whatever raw lines the caller built (a tool call, a streamed
    text block, and a many-delta thinking flood, ALL repeated in that
    iteration's own `message_end` -- see `_message_end_repeating_everything`),
    so a single fixture built once by a caller drives Part 1 (in-memory
    `render_tail_lines`) and Parts 2/3 (the on-disk/live paths) against
    the EXACT SAME bytes (cure-01-01-2)."""
    parts = []
    for n in range(1, 3):
        meta = _iteration_meta(n)
        parts.append(boundary_line(meta, "start"))
        parts.append(_message_end_line(f"iteration {n} did some work"))
        parts.append(boundary_line(meta, "end"))
    meta3 = _iteration_meta(3)
    parts.append(boundary_line(meta3, "start"))
    parts.extend(iteration3_lines)
    parts.append(boundary_line(meta3, "end"))
    return "".join(parts)



def _assert_collapse_state_carries_across_a_cut(
        monkeypatch, tmp_path, run_suffix: str, iteration3_lines: list[str],
        tail_ns: tuple, text_occurrences: int, thinking_occurrences: int,
        tool_occurrences: int) -> None:
    """Parts 1-3 of the collapse-state-across-a-cut regression (task 006,
    PRD F): `render_tail_lines` directly, the on-disk dead-run fallback,
    and the live-container fetch path, all driven off the SAME
    `iteration3_lines`. Pulled out as a helper (not a fourth top-level
    `def test_...`) so cure-01-01-2's own named gap shapes can each run
    all three code paths without growing the fast tier's pinned
    collected-test count (cure-01-04, #204).

    Every assertion mirrors the pre-cure-01-01-2 single-scenario body: no
    duplicated text/tool-call line, no re-shown `[thinking...]`, no extra
    blank line (PRD F.1/F.2/F.4), and (when `tool_occurrences`) the tool
    call's own arguments survive into the rendered line, not just its
    bare name (PRD F.3/F.4's `retained tool arguments` half)."""
    raw_text = _blob_with_iteration3_lines(iteration3_lines)
    full = render_to_lines(raw_text, tty=False, state=new_render_state())

    for n in tail_ns:
        tail = render_tail_lines(raw_text, tty=False, n=n)
        assert tail == full[-n:], (n, tail, full[-n:])
        assert all(line != "" for line in tail), (n, tail)
    assert sum(1 for ln in full if "thinking" in ln) == thinking_occurrences
    assert sum(1 for ln in full if _RESTART_TEXT in ln) == text_occurrences
    assert sum(1 for ln in full if "bash" in ln) == tool_occurrences

    # Part 2 -- the on-disk-merge half: a dead run's `merged_lines`
    # fallback (the engine's own on-disk read, and the hub's fallback when
    # a run's container is unreachable) must carry the same collapse
    # state across the same kind of cut.
    from ralphd.cli import ui_server

    run_dir = tmp_path / "runs" / f"dead-run-restart-{run_suffix}"
    run_dir.mkdir(parents=True)
    for n in range(1, 3):
        _write_dead_run_iteration(run_dir, n, [
            json.dumps({"type": "message_end", "message": {"content": [
                {"type": "text", "text": f"iteration {n} did some work"}]}})])
    _write_dead_run_iteration(
        run_dir, 3, [line.rstrip("\n") for line in iteration3_lines])

    monkeypatch.setattr(ui_server, "_proxy_text", lambda *a, **k: (False, ""))

    # cure-01-01-2 (second attempt), PRD E.3's safe-restart half, asserted
    # on the bounded read itself and not only on what it renders: the seed
    # look-back may stop for exactly two REAL reasons -- it walked to byte
    # 0 (`reached_start`, so there is provably no unread history left) or
    # `_should_extend_seed_lookback` finds nothing left to resolve. Any
    # other stop (a doubling/look-back cap running out, the oldest parsed
    # event happening to be opaque filler) publishes an incomplete prefix
    # as if it were a genuine iteration start -- exactly how a
    # cap-exhausted read rendered a bare tool line with its arguments
    # dropped.
    from ralphd import log_merge as log_merge_mod

    out3 = run_dir / "iterations" / "0003" / "output.jsonl"
    for n in tail_ns:
        kept, reached_start, prefix = log_merge_mod._tail_lines_of_file(out3, n)
        assert (reached_start
                or not log_merge_mod._should_extend_seed_lookback(kept, prefix)), n

    live_full, full_lines = ui_server.rendered_log_lines(
        tmp_path, f"dead-run-restart-{run_suffix}", None)
    assert live_full is False
    assert sum(1 for ln in full_lines if "bash" in ln) == tool_occurrences
    if tool_occurrences:
        assert any(_RESTART_TOOL_ARGS["command"] in ln for ln in full_lines)

    for n in tail_ns:
        live_tail, tail_lines = ui_server.rendered_log_lines(
            tmp_path, f"dead-run-restart-{run_suffix}", n)
        assert live_tail is False
        assert tail_lines == full_lines[-n:], (n, tail_lines, full_lines[-n:])
        assert all(line != "" for line in tail_lines), (n, tail_lines)
        tail_bytes = ("\n".join(tail_lines) + "\n").encode()
        full_tail_bytes = ("\n".join(full_lines[-n:]) + "\n").encode()
        assert tail_bytes == full_tail_bytes

    # Part 3 -- the live-container half: `ui_server.rendered_log_lines`'s
    # live path (`_proxy_text` answering `True`) must carry the same
    # collapse state across the same kind of cut. The stub below answers
    # `GET /logs?tail=N` the same way the REAL container's route does
    # (`engine.api.logs`): `log_merge.merge_entries_tail(run.root, tail)`
    # followed by `log_merge.apply_tail` -- reading from an on-disk run
    # directory that is a stand-in for the container's own, so the
    # container's answer carries the very same per-iteration
    # `collapseSeed` the dead-run fallback's `merged_lines` call produces.
    from ralphd.log_merge import apply_tail, merge_entries_tail

    container_run_root = tmp_path / f"container-run-{run_suffix}"
    for n in range(1, 3):
        _write_dead_run_iteration(container_run_root, n, [
            json.dumps({"type": "message_end", "message": {"content": [
                {"type": "text", "text": f"iteration {n} did some work"}]}})])
    _write_dead_run_iteration(
        container_run_root, 3, [line.rstrip("\n") for line in iteration3_lines])

    (tmp_path / "runs" / f"live-run-restart-{run_suffix}").mkdir(parents=True)
    monkeypatch.setattr(ui_server, "host_meta",
                        lambda reg, run_id: {"apiUrl": "http://stub"})

    full_blob = "".join(line for _, line in
                        apply_tail(merge_entries_tail(container_run_root, 10**9), 10**9))
    full2 = render_to_lines(full_blob, tty=False, state=new_render_state())

    tail_re = re.compile(r"tail=(\d+)")

    def _fake_proxy_text(reg, run_id, path, timeout=5.0):
        m = tail_re.search(path)
        assert m, f"expected a tail= bound in the fetch path, got {path!r}"
        bound = int(m.group(1))
        entries = apply_tail(merge_entries_tail(container_run_root, bound), bound)
        return True, "".join(line for _, line in entries)

    monkeypatch.setattr(ui_server, "_proxy_text", _fake_proxy_text)

    for n in tail_ns:
        live, tail_lines = ui_server.rendered_log_lines(
            tmp_path, f"live-run-restart-{run_suffix}", n)
        assert live is True
        assert tail_lines == full2[-n:], (n, tail_lines, full2[-n:])
        assert all(line != "" for line in tail_lines), (n, tail_lines)


def test_collapse_state_carries_across_a_cut_mid_iteration_direct_dead_run_and_live(
        monkeypatch, tmp_path):
    """Task 006 (PRD F), all three code paths, across three named gap
    shapes, in one function (cure-01-04, #204: consolidated from three
    separate test functions to bring the fast tier's collected-test count
    back under its 5,800 launch ceiling; cure-01-01-2 extended it with two
    MORE gap shapes in the same function for the same reason, rather than
    adding new top-level `def test_...`s; cure-01-03-2 added a fifth,
    same reason).

    Without a sound `_should_extend_seed_lookback` trigger, growth would
    stop the moment the SINGLE OLDEST line it had read happened to be
    `"ignored"` filler -- even with a genuinely relevant event (a
    `text_delta`, a `tool_execution_start`) still sitting further back
    behind it -- which is exactly the defect cure-01-01-2 fixes. Scenario
    5 (cure-01-03-2) sharpens this further: the opaque filler sits
    directly ADJACENT to `message_end` with no relevant event between
    them at all, so a small tail's own `tail_lines` carries no
    `_TYPE_FLAG_GROUPS` member whatsoever -- the absence-in-an-incidental-
    read-window case E.3/F.1/F.2/F.4 name.

    Scenario 1 (cure-01-02, #195): the tool call's start/end are separated
    by 3,000 small ignored events, and the thinking flood is widened to
    3,000 deltas -- large enough that even the flood itself does not fit
    in one `_TAIL_READ_CHUNK` read, let alone the streamed text before it.
    """
    _assert_collapse_state_carries_across_a_cut(
        monkeypatch, tmp_path, "wide-tool-gap",
        _restart_iteration_lines(3000, ignored_gap=3000),
        tail_ns=(1, 2, 3, 4, 5, 6, 10, 50),
        text_occurrences=1, thinking_occurrences=1, tool_occurrences=1)

    # Scenario 2 (cure-01-01-2): streamed text is separated from a
    # 500-delta thinking block (and the `message_end` that replays both)
    # by 1,000 opaque events sized to exactly 1,000 bytes each (~1 MB) --
    # unlike scenario 1's gap (which only ever separates the TOOL call's
    # own start/end), this filler sits between the text run and the
    # thinking run, so the text run's own `text_delta` (what actually sets
    # `textSeen`) is the thing that must still be found across it.
    _assert_collapse_state_carries_across_a_cut(
        monkeypatch, tmp_path, "wide-text-thinking-gap",
        _restart_iteration_lines(
            500, text_thinking_gap_lines=_ignored_filler_lines(1000, 1000)),
        tail_ns=(1, 2, 3, 4, 5, 6, 10, 50),
        text_occurrences=1, thinking_occurrences=1, tool_occurrences=1)

    # Scenario 3 (cure-01-01-2): a tool call's own start -- carrying its
    # invocation arguments -- is separated from its end by 9,000 opaque
    # events sized to exactly 1,024 bytes each (~9.2 MB), comfortably past
    # both scenario 1's gap and the un-widened first read's own reach.
    _assert_collapse_state_carries_across_a_cut(
        monkeypatch, tmp_path, "wide-tool-byte-gap",
        _restart_iteration_lines(3, ignored_gap=9000, ignored_line_bytes=1024),
        tail_ns=(1, 2, 3, 4, 5, 6, 10, 50),
        text_occurrences=1, thinking_occurrences=1, tool_occurrences=1)

    # Scenario 4 (cure-01-01-2, second attempt): the same shape once more
    # with `_SEED_LOOKBACK_EXTRA_GROWTHS` deliberately starved to ONE, so
    # the look-back's chunk-growth budget is spent long before the tool
    # call's own start (~1 MB further back: 1,000 opaque events of exactly
    # 1,024 bytes each) comes into reach. Starving that constant is a
    # scenario parameter, not a weakened assertion -- it constructs cap
    # exhaustion at test speed instead of by writing the ~537 MB a
    # default-sized budget would need. Every assertion in the helper still
    # has to hold: while that constant ENDED the look-back (its shape
    # before this task), the read published its incomplete prefix as a
    # restart and the tool line rendered bare, with its `echo hello`
    # arguments dropped.
    from ralphd import log_merge as starved_log_merge

    monkeypatch.setattr(starved_log_merge, "_SEED_LOOKBACK_EXTRA_GROWTHS", 1)
    _assert_collapse_state_carries_across_a_cut(
        monkeypatch, tmp_path, "starved-growth-budget",
        _restart_iteration_lines(3, ignored_gap=1000, ignored_line_bytes=1024),
        tail_ns=(1, 2, 3, 4, 5, 6, 10, 50),
        text_occurrences=1, thinking_occurrences=1, tool_occurrences=1)

    # Scenario 5 (cure-01-03-2, PRD F.1/F.2/F.4, E.3): 1,000 opaque
    # 1,000-byte events sit between the 500-delta thinking flood and the
    # `message_end` that repeats the tool call, the text, and the
    # thinking -- so a small requested tail's own `tail_lines` is nothing
    # but that opaque filler plus `message_end` itself: no `text_delta`,
    # `thinking_start`/`thinking_delta`, or tool event anywhere IN
    # `tail_lines`, unlike every scenario above (whose small-`n` tails
    # always still hold a live thinking delta or tool event). Before this
    # task, `_should_extend_seed_lookback`'s only trigger keyed off
    # `tail_lines` containing a `_TYPE_FLAG_GROUPS` member at all, so it
    # never grew past this window and certified `textSeen`/`thinkingSeen`
    # both `False` on the strength of that absence -- printing the
    # streamed text and tool call a second time instead of the collapsed
    # `[thinking...]` marker the full render actually shows.
    _assert_collapse_state_carries_across_a_cut(
        monkeypatch, tmp_path, "wide-post-thinking-gap",
        _restart_iteration_lines(
            500, post_thinking_gap_lines=_ignored_filler_lines(1000, 1000)),
        tail_ns=(1, 2, 3, 4, 5, 6, 10, 50),
        text_occurrences=1, thinking_occurrences=1, tool_occurrences=1)

