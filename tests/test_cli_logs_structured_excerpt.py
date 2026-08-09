"""Unit tests for structured (non-string) `tool_execution_end.result`
excerpting in the pretty log renderer (task 015): `_tool_outcome_and_tail`
(src/ralphd/cli/log_render.py) walks the standard content-list shape
`{"content": [{"type": "text", "text": ...}]}` for a short one-line text
excerpt instead of showing nothing (or, before this fix, a stringified
JSON dump) when a tool result is structured rather than a plain string.

Covers all four PRD-required shapes: (a) plain string (unchanged
behavior), (b) structured content-list, (c) error with structured
detail, (d) unknown/unrecognized shape (degrades to NO excerpt, never a
JSON dump). Also asserts the buffered one-line path
(`_render_tool_result`) and the live in-place-rewrite completion path
(`_render_tool_completion`) produce the IDENTICAL excerpt for the same
event, since both route through `_tool_outcome_and_tail`.
"""

from __future__ import annotations

from ralphd.cli.log_render import (
    _render_tool_completion,
    _render_tool_result,
    _tool_outcome_and_tail,
    new_render_state,
)

# -- (a) plain string result: unchanged ------------------------------------

def test_string_result_excerpt_unchanged():
    outcome, tail = _tool_outcome_and_tail(
        {"isError": False, "result": "file contents here"}, tty=False)
    assert "ok" in outcome
    assert "file contents here" in tail


def test_string_result_truncated_around_60_chars():
    long_result = "x" * 200
    _, tail = _tool_outcome_and_tail({"isError": False, "result": long_result}, tty=False)
    assert tail.endswith("...)")
    assert len(tail) < 80


# -- (b) structured content-list result ------------------------------------

def test_structured_content_list_result_shows_text_excerpt():
    ev = {
        "isError": False,
        "result": {"content": [{"type": "text", "text": "wrote 42 bytes to out.txt"}]},
    }
    outcome, tail = _tool_outcome_and_tail(ev, tty=False)
    assert "ok" in outcome
    assert "wrote 42 bytes to out.txt" in tail


def test_structured_content_list_skips_leading_non_text_items():
    ev = {
        "isError": False,
        "result": {"content": [{"type": "image", "data": "..."},
                                 {"type": "text", "text": "second item text"}]},
    }
    _, tail = _tool_outcome_and_tail(ev, tty=False)
    assert "second item text" in tail


def test_structured_content_list_empty_yields_no_excerpt():
    ev = {"isError": False, "result": {"content": []}}
    _, tail = _tool_outcome_and_tail(ev, tty=False)
    assert tail == ""


# -- (c) error with structured detail --------------------------------------

def test_error_with_structured_detail_shows_excerpt():
    ev = {
        "isError": True,
        "result": {"error": {"type": "text", "text": "permission denied: /etc/shadow"}},
    }
    outcome, tail = _tool_outcome_and_tail(ev, tty=False)
    assert "error" in outcome
    assert "permission denied: /etc/shadow" in tail


def test_error_excerpt_allows_slightly_longer_truncation():
    long_error = "boom: " + ("y" * 150)
    ev = {"isError": True, "result": {"content": [{"type": "text", "text": long_error}]}}
    _, tail = _tool_outcome_and_tail(ev, tty=False)
    # error excerpts get more room (120) than success excerpts (60)
    assert len(tail) > 70
    assert tail.endswith("...)")


# -- (d) unknown/unrecognized shape: no excerpt, never a JSON dump ---------

def test_unknown_structured_shape_yields_no_excerpt():
    ev = {"isError": False, "result": {"weird": {"nested": [1, 2, 3]}}}
    _, tail = _tool_outcome_and_tail(ev, tty=False)
    assert tail == ""


def test_unknown_shape_never_stringifies_whole_object():
    ev = {"isError": False, "result": {"totally": "unrecognized", "shape": True}}
    _, tail = _tool_outcome_and_tail(ev, tty=False)
    assert "totally" not in tail
    assert tail == ""


# -- buffered vs live paths produce the identical excerpt ------------------

def test_buffered_and_live_paths_agree_on_structured_excerpt(capsys):
    ev = {
        "toolName": "write", "toolCallId": "tc-1", "isError": False,
        "result": {"content": [{"type": "text", "text": "wrote file ok"}]},
    }
    args = {"path": "out.txt"}

    _render_tool_result(ev, tty=False, args=args)
    buffered_out = capsys.readouterr().out

    state = new_render_state()
    _render_tool_completion(ev, tty=False, args=args, state=state)
    live_out = capsys.readouterr().out

    assert "wrote file ok" in buffered_out
    assert "wrote file ok" in live_out
