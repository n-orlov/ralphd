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


def _tool_call_lines(tool_call_id: str, name: str, args: dict, result: str = "ok",
                     ignored_gap: int = 0) -> list[str]:
    """A `tool_execution_start` (with `args`) and its matching
    `tool_execution_end`, optionally separated by `ignored_gap` opaque raw
    events `render_log_line` does not recognize (task cure-01-02, #195):
    large enough, the gap pushes the start beyond a single
    `_TAIL_READ_CHUNK` read, so a bounded tail landing on just the end
    event must still find the start's arguments by TARGETED look-back --
    not merely by reading past a recognizable run, since `{"type":
    "ignored"}` filler is not one."""
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


def _restart_iteration_lines(deltas: int, ignored_gap: int = 0) -> list[str]:
    return (_tool_call_lines("t1", _RESTART_TOOL_NAME, _RESTART_TOOL_ARGS,
                             ignored_gap=ignored_gap)
            + _text_stream_lines(_RESTART_TEXT)
            + [line + "\n" for line in _thinking_flood_lines(deltas)]
            + [_message_end_repeating_everything()])


def _fixture_blob_with_restart_mid_iteration(num_iterations: int, restart_iteration: int,
                                             deltas: int) -> str:
    """Same per-iteration shape as `_fixture_blob`, except
    `restart_iteration` carries a tool call, a streamed text block, and a
    many-delta thinking flood, ALL repeated in that iteration's own
    `message_end` -- see `_message_end_repeating_everything`."""
    parts = []
    for n in range(1, num_iterations + 1):
        meta = _iteration_meta(n)
        parts.append(boundary_line(meta, "start"))
        if n == restart_iteration:
            parts.extend(_restart_iteration_lines(deltas))
        else:
            parts.append(_message_end_line(f"iteration {n} did some work"))
        parts.append(boundary_line(meta, "end"))
    return "".join(parts)


def test_collapse_state_carries_across_a_cut_mid_iteration_direct_dead_run_and_live(
        monkeypatch, tmp_path):
    """Task 006 (PRD F), all three code paths in one function (cure-01-04,
    #204: consolidated from three separate test functions --
    `test_bounded_tail_carries_collapse_state_across_a_cut_mid_iteration`,
    `test_dead_run_tail_carries_collapse_state_across_a_cut_mid_iteration`,
    `test_live_tail_carries_collapse_state_across_a_cut_mid_iteration` --
    to bring the fast tier's collected-test count back under its 5,800
    launch ceiling; every assertion below is unchanged from those three,
    only merged into one body). No `TIER_CEILINGS` change is coupled to
    this file: the net effect (three functions -> one) is part of
    cure-01-04's own reduction, not a per-file raise.

    Task cure-01-02 (#195) widened this SAME fixture rather than adding
    new test functions, for the same collected-count reason: the flood is
    now 3,000 deltas (not 500) -- large enough that even the flood itself
    does not fit in one `_TAIL_READ_CHUNK` read, let alone the streamed
    text before it -- and the tool call's start/end are separated by 3,000
    unrelated, ignored raw events in Parts 2 and 3 (the on-disk paths;
    Part 1 never reads from disk at all, so it is unaffected either way).
    Without this task's bounded extra look-back / dangling-tool-call
    lookup, `text_seen` would stay wrongly `False` and the tool call would
    render bare on those two paths.

    Part 1 -- `render_tail_lines` directly: every tail size that lands
    somewhere inside the restart iteration's flood (or right after it, at
    the `message_end`) must still match the full render's own suffix --
    no duplicated text/tool-call line, no re-shown `[thinking...]`, no
    extra blank line."""
    raw_text = _fixture_blob_with_restart_mid_iteration(3, restart_iteration=3, deltas=3000)
    full = render_to_lines(raw_text, tty=False, state=new_render_state())

    for n in (1, 2, 3, 4, 5, 6, 10, 50):
        tail = render_tail_lines(raw_text, tty=False, n=n)
        assert tail == full[-n:], (n, tail, full[-n:])
        # No spurious blank line anywhere in the tail (PRD F.4).
        assert all(line != "" for line in tail), (n, tail)
    # The fixture actually exercises all three guards: the flood collapses
    # to one line, and the tool call/text lines each appear exactly once
    # across the WHOLE render (never twice, i.e. `message_end` never
    # re-prints any of them).
    assert sum(1 for ln in full if "thinking" in ln) == 1
    assert sum(1 for ln in full if _RESTART_TEXT in ln) == 1
    assert sum(1 for ln in full if "bash" in ln) == 1

    # Part 2 -- the on-disk-merge half: a dead run's `merged_lines`
    # fallback must carry the same collapse state across the same kind of
    # cut.
    from ralphd.cli import ui_server

    run_dir = tmp_path / "runs" / "dead-run-restart"
    run_dir.mkdir(parents=True)
    for n in range(1, 3):
        _write_dead_run_iteration(run_dir, n, [
            json.dumps({"type": "message_end", "message": {"content": [
                {"type": "text", "text": f"iteration {n} did some work"}]}})])
    _write_dead_run_iteration(
        run_dir, 3,
        [line.rstrip("\n") for line in _restart_iteration_lines(3000, ignored_gap=3000)])

    monkeypatch.setattr(ui_server, "_proxy_text", lambda *a, **k: (False, ""))

    live_full, full_lines = ui_server.rendered_log_lines(tmp_path, "dead-run-restart", None)
    assert live_full is False
    assert sum(1 for ln in full_lines if "bash" in ln) == 1
    assert any(_RESTART_TOOL_ARGS["command"] in ln for ln in full_lines)

    for n in (1, 2, 3, 4, 5, 6, 10, 50):
        live_tail, tail_lines = ui_server.rendered_log_lines(tmp_path, "dead-run-restart", n)
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

    container_run_root = tmp_path / "container-run"
    for n in range(1, 3):
        _write_dead_run_iteration(container_run_root, n, [
            json.dumps({"type": "message_end", "message": {"content": [
                {"type": "text", "text": f"iteration {n} did some work"}]}})])
    _write_dead_run_iteration(
        container_run_root, 3,
        [line.rstrip("\n") for line in _restart_iteration_lines(3000, ignored_gap=3000)])

    (tmp_path / "runs" / "live-run-restart").mkdir(parents=True)
    monkeypatch.setattr(ui_server, "host_meta",
                        lambda reg, run_id: {"apiUrl": "http://stub"})

    full_blob = "".join(line for _, line in
                        apply_tail(merge_entries_tail(container_run_root, 10**9), 10**9))
    full = render_to_lines(full_blob, tty=False, state=new_render_state())

    tail_re = re.compile(r"tail=(\d+)")

    def _fake_proxy_text(reg, run_id, path, timeout=5.0):
        m = tail_re.search(path)
        assert m, f"expected a tail= bound in the fetch path, got {path!r}"
        bound = int(m.group(1))
        entries = apply_tail(merge_entries_tail(container_run_root, bound), bound)
        return True, "".join(line for _, line in entries)

    monkeypatch.setattr(ui_server, "_proxy_text", _fake_proxy_text)

    for n in (1, 2, 3, 4, 5, 6, 10, 50):
        live, tail_lines = ui_server.rendered_log_lines(tmp_path, "live-run-restart", n)
        assert live is True
        assert tail_lines == full[-n:], (n, tail_lines, full[-n:])
        assert all(line != "" for line in tail_lines), (n, tail_lines)

