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
"""

from __future__ import annotations

import json

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


def test_bounded_tail_empty_or_non_positive_n_returns_no_lines():
    raw_text = _fixture_blob(2)
    assert render_tail_lines(raw_text, tty=False, n=0) == []
    assert render_tail_lines("", tty=False, n=5) == []
