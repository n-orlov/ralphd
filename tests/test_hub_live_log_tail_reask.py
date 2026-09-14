"""Task 046 (PRD H.4): a LIVE run's log fetch asks the container for a
bounded backlog instead of its entire history, and re-asks with a larger
bound when the render of what came back still falls short of the
requested tail.

`ui_server.rendered_log_lines`'s tail branch used to call `_proxy_text(reg,
run_id, "/logs")` with no `tail` at all -- the container always answered
with its whole backlog, however small the requested tail. Task 046 makes
that first ask carry `?tail=N` (`N` derived from the requested rendered
line count), and grows the bound (doubling) whenever the render of the
answer still comes up short of that count -- mirroring, one layer further
out, the same growing-chunk shape `render_tail_lines` (task 043/H.1)
already uses locally.

The fixture below reuses `test_log_render_bounded_tail.py`'s thinking-flood
helpers: a many-delta thinking block collapses to exactly ONE rendered
`[thinking...]` line, so a raw fetch bounded by RAW content-line count can
badly under-supply the requested RENDERED line count -- forcing a real
re-ask, not just a coincidentally-sufficient first answer. The stub
`_proxy_text` below mimics the real container's `GET /logs?tail=N`
contract (`log_merge.apply_tail`): it hands back only the last `N`
non-boundary (content) lines of the full fixture blob, plus whichever
boundary lines fall inside that window -- exactly what a real `/logs?tail=`
answer looks like -- so the re-ask path is exercised against a materially
realistic container response, not a hand-waved stand-in.
"""

from __future__ import annotations

import json
import re

from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.log_merge import apply_tail, boundary_line
from tests.test_log_render_bounded_tail import (
    _fixture_blob_with_thinking_flood,
    _iteration_meta,
)

_TAIL_RE = re.compile(r"tail=(\d+)")


def _is_boundary(raw_line: str) -> bool:
    if not raw_line.strip():
        return False
    try:
        ev = json.loads(raw_line)
    except json.JSONDecodeError:
        return False
    return isinstance(ev, dict) and ev.get("type") == "ralphd.iteration"


def _container_tail(full_blob: str, n: int) -> str:
    """Stand-in for the real container's `GET /logs?tail=N`: apply the same
    `log_merge.apply_tail` contract (last `n` content lines, plus any
    boundary lines inside that window) to the full fixture blob."""
    lines = full_blob.splitlines(keepends=True)
    entries = [(_is_boundary(ln), ln) for ln in lines]
    return "".join(line for _, line in apply_tail(entries, n))


def test_live_tail_fetch_reasks_with_a_larger_bound_when_the_render_falls_short(
        monkeypatch, tmp_path):
    """A many-delta thinking flood sits in the newest of 8 iterations, so a
    fetch bounded by the requested rendered-line count (10) alone hands
    back mostly-collapsing raw content and renders far fewer than 10
    lines -- forcing at least one re-ask with a doubled bound before the
    render is long enough, and the final result must still match a full
    render's own tail exactly."""
    (tmp_path / "runs" / "live-run").mkdir(parents=True)
    monkeypatch.setattr(ui_server, "host_meta",
                         lambda reg, run_id: {"apiUrl": "http://stub"})

    full_blob = _fixture_blob_with_thinking_flood(8, flood_iteration=8, deltas=500)
    calls: list[int] = []

    def _fake_proxy_text(reg, run_id, path, timeout=5.0):
        m = _TAIL_RE.search(path)
        assert m, f"expected a tail= bound in the fetch path, got {path!r}"
        bound = int(m.group(1))
        calls.append(bound)
        return True, _container_tail(full_blob, bound)

    monkeypatch.setattr(ui_server, "_proxy_text", _fake_proxy_text)

    live, tail_lines = ui_server.rendered_log_lines(tmp_path, "live-run", 10)

    assert live is True
    # More than one fetch: the first bound (10) undersupplies rendered
    # lines once the flood collapses, so a re-ask with a larger bound
    # actually happened.
    assert len(calls) > 1, calls
    # Each retry strictly grows the bound -- never re-asks with the same
    # or a smaller one.
    assert calls == sorted(set(calls)), calls
    assert calls[0] == 10

    full = render_to_lines(full_blob, tty=False, state=new_render_state())
    assert len(tail_lines) == 10
    assert tail_lines == full[-10:]
    assert any("thinking" in line for line in tail_lines)


def test_live_tail_fetch_asks_once_when_the_first_bound_already_renders_enough(
        monkeypatch, tmp_path):
    """No flood, no collapsing: the last 10 raw content lines already
    render to (at least) 10 lines, so the fetch must stop after its first
    ask -- never re-ask when the render already satisfies the request."""
    (tmp_path / "runs" / "live-run").mkdir(parents=True)
    monkeypatch.setattr(ui_server, "host_meta",
                         lambda reg, run_id: {"apiUrl": "http://stub"})

    parts = []
    for n in range(1, 9):
        meta = _iteration_meta(n)
        parts.append(boundary_line(meta, "start"))
        parts.append(json.dumps({"type": "message_end", "message": {"content": [
            {"type": "text", "text": f"iteration {n} did some work"}]}}) + "\n")
        parts.append(boundary_line(meta, "end"))
    full_blob = "".join(parts)

    calls: list[int] = []

    def _fake_proxy_text(reg, run_id, path, timeout=5.0):
        m = _TAIL_RE.search(path)
        assert m
        bound = int(m.group(1))
        calls.append(bound)
        return True, _container_tail(full_blob, bound)

    monkeypatch.setattr(ui_server, "_proxy_text", _fake_proxy_text)

    live, tail_lines = ui_server.rendered_log_lines(tmp_path, "live-run", 10)

    assert live is True
    assert calls == [10]
    full = render_to_lines(full_blob, tty=False, state=new_render_state())
    assert tail_lines == full[-10:]
