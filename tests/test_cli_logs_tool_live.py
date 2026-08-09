"""Unit tests for task 003: live/follow rendering of `tool_execution_start`
in the pretty log renderer (`_render_log_line`, src/ralphd/cli/main.py).

Before this task `tool_execution_start` was silently skipped by
`_render_log_line` -- the invocation line only ever appeared once
`tool_execution_end` arrived, meaning an operator watching `ralphctl logs
-f` during a long-running tool call (e.g. a multi-minute `bash` command)
saw nothing at all until it finished. These tests drive `_render_log_line`
directly with `live=True` (the flag `_stream_logs`'s genuinely-live follow
path sets) on a partial stream that has a `tool_execution_start` with NO
matching `tool_execution_end` yet, proving the invocation line is visible
immediately -- and separately confirm that once the matching end DOES
arrive, live rendering shows a short completion line rather than
re-printing the invocation (so together the two lines carry the same
information the old single-line form did, never duplicated).

Buffered/bounded rendering (`live=False`, the default -- used by
`_render_to_lines` for `ralphctl logs <id>` without `--follow`) is
unchanged: a completed tool call still renders as exactly one line, which
tests/test_cli_logs.py and tests/test_cli_logs_rendered_tail.py already
pin.
"""

from __future__ import annotations

import json

from ralphd.cli.main import _new_render_state, _render_log_line


def _line(ev: dict) -> str:
    return json.dumps(ev)


def test_live_start_with_no_end_yet_renders_invocation(capsys):
    """The core task-003 proof: a stream containing only
    `tool_execution_start` (the matching end never arrived, e.g. the tool
    is still running) still shows the invocation line in live mode."""
    state = _new_render_state()
    start = {"type": "tool_execution_start", "toolCallId": "call_1",
             "toolName": "bash", "args": {"command": "sleep 300"}}
    _render_log_line(_line(start), tty=False, state=state, live=True)

    out = capsys.readouterr().out
    assert "bash $ sleep 300" in out
    # no outcome yet -- the end event never arrived
    assert "ok" not in out
    assert "error" not in out


def test_live_start_then_end_shows_completion_without_repeating_invocation(capsys):
    state = _new_render_state()
    start = {"type": "tool_execution_start", "toolCallId": "call_1",
             "toolName": "bash", "args": {"command": "true"}}
    end = {"type": "tool_execution_end", "toolCallId": "call_1",
           "toolName": "bash", "result": "done", "isError": False}

    _render_log_line(_line(start), tty=False, state=state, live=True)
    _render_log_line(_line(end), tty=False, state=state, live=True)

    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "bash $ true" in lines[0]
    assert "ok" not in lines[0]  # invocation line carries no outcome
    assert "bash $ true" not in lines[1]  # completion line doesn't repeat it
    assert "ok" in lines[1]


def test_live_start_then_error_end_shows_error_completion(capsys):
    state = _new_render_state()
    start = {"type": "tool_execution_start", "toolCallId": "call_1",
             "toolName": "bash", "args": {"command": "false"}}
    end = {"type": "tool_execution_end", "toolCallId": "call_1",
           "toolName": "bash", "result": "boom: exit 1", "isError": True}

    _render_log_line(_line(start), tty=False, state=state, live=True)
    _render_log_line(_line(end), tty=False, state=state, live=True)

    out = capsys.readouterr().out
    assert "bash $ false" in out
    assert "error" in out
    assert "boom: exit 1" in out


def test_buffered_start_then_end_still_renders_exactly_one_line(capsys):
    """Default (`live=False`, buffered) rendering is unchanged by task
    003: a completed call still renders as exactly one line, never a
    start+end duplicate."""
    state = _new_render_state()
    start = {"type": "tool_execution_start", "toolCallId": "call_1",
             "toolName": "bash", "args": {"command": "true"}}
    end = {"type": "tool_execution_end", "toolCallId": "call_1",
           "toolName": "bash", "result": "done", "isError": False}

    _render_log_line(_line(start), tty=False, state=state)
    _render_log_line(_line(end), tty=False, state=state)

    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 1
    assert "bash $ true" in lines[0]
    assert "ok" in lines[0]


def test_buffered_start_with_no_end_renders_nothing(capsys):
    """Buffered rendering has the whole transcript in hand already, so an
    unmatched start (e.g. a truncated/incomplete transcript) still
    renders nothing -- only live rendering shows a start with no end."""
    state = _new_render_state()
    start = {"type": "tool_execution_start", "toolCallId": "call_1",
             "toolName": "bash", "args": {"command": "sleep 300"}}
    _render_log_line(_line(start), tty=False, state=state)

    out = capsys.readouterr().out
    assert out == ""
