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
