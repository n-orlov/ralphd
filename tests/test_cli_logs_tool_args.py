"""Unit tests for the pretty log renderer's tool-argument rendering
(PRD req A1 / task 001): bash shows its command, read/write/edit-style
tools show the path, grep/glob/find-style tools show the pattern, and
unknown tools fall back to the first scalar argument value -- all
generously truncated, with the ✓/✗ outcome kept and a short error excerpt
shown on failure when available.

These exercise `_fmt_invocation`/`_render_tool_result` directly rather
than through a live engine: the argument-shape rules are pure functions
of (toolName, args), so a unit test is the most direct way to pin every
rendering shape from the PRD without needing the stub agent to emit
tool calls it doesn't know how to construct (e.g. `grep`/`read`).

Redaction (tests/test_secret_redaction.py) happens at write/serve time in
src/ralphd/engine/redact.py, upstream of anything this module renders --
by the time a raw NDJSON line reaches `_render_tool_result`, any secret
value has already been replaced with a `[REDACTED:...]` marker, so
showing the full command/path/pattern here does not widen the
secret-exposure surface.
"""

from __future__ import annotations

import pytest

from ralphd.cli.log_render import _fmt_invocation, _render_tool_result

# -- _fmt_invocation: pure argument-shape rendering -------------------------

def test_bash_shows_command_with_newlines_collapsed():
    inv = _fmt_invocation("bash", {"command": "echo one\necho two\n  echo three"})
    assert inv == "bash $ echo one echo two echo three"
    assert "\n" not in inv


def test_bash_truncates_around_300_chars():
    long_cmd = "echo " + ("x" * 400)
    inv = _fmt_invocation("bash", {"command": long_cmd})
    assert inv.startswith("bash $ echo ")
    assert inv.endswith("...")
    assert len(inv) <= len("bash $ ") + 300


@pytest.mark.parametrize("tool", ["read", "write", "edit"])
def test_path_tools_show_path(tool):
    inv = _fmt_invocation(tool, {"path": "src/ralphd/cli/main.py"})
    assert inv == f"{tool} src/ralphd/cli/main.py"


@pytest.mark.parametrize("tool", ["grep", "glob", "find"])
def test_pattern_tools_show_pattern(tool):
    inv = _fmt_invocation(tool, {"pattern": "TODO.*fixme", "path": "src/"})
    assert inv == f"{tool} TODO.*fixme"


def test_unknown_tool_falls_back_to_first_scalar_arg():
    inv = _fmt_invocation("frobnicate", {"target": "widget-42", "count": 3})
    assert inv == "frobnicate widget-42"


def test_unknown_tool_first_scalar_skips_nested_values():
    inv = _fmt_invocation("frobnicate", {"nested": {"a": 1}, "flag": True})
    assert inv == "frobnicate True"


def test_unknown_tool_with_no_scalar_args_shows_bare_name():
    inv = _fmt_invocation("frobnicate", {"nested": {"a": 1}})
    assert inv == "frobnicate"


def test_first_scalar_value_truncated():
    inv = _fmt_invocation("frobnicate", {"target": "x" * 400})
    assert inv.startswith("frobnicate x")
    assert inv.endswith("...")
    assert len(inv) <= len("frobnicate ") + 300


# -- _render_tool_result: outcome status and error excerpt ------------------

def test_render_tool_result_ok(capsys):
    _render_tool_result(
        {"toolName": "bash", "args": {"command": "true"}, "isError": False,
         "result": "done"},
        tty=False)
    out = capsys.readouterr().out
    assert "bash $ true" in out
    assert "ok" in out


def test_render_tool_result_error_shows_excerpt(capsys):
    _render_tool_result(
        {"toolName": "bash", "args": {"command": "false"}, "isError": True,
         "result": "boom: command failed with exit code 1"},
        tty=False)
    out = capsys.readouterr().out
    assert "bash $ false" in out
    assert "error" in out
    assert "boom: command failed" in out


def test_render_tool_result_read_shows_path(capsys):
    _render_tool_result(
        {"toolName": "read", "args": {"path": "docs/cli.md"}, "isError": False,
         "result": "file contents..."},
        tty=False)
    out = capsys.readouterr().out
    assert "read docs/cli.md" in out


def test_render_tool_result_grep_shows_pattern(capsys):
    _render_tool_result(
        {"toolName": "grep", "args": {"pattern": "def foo", "path": "src/"},
         "isError": False, "result": "src/foo.py:1:def foo():"},
        tty=False)
    out = capsys.readouterr().out
    assert "grep def foo" in out
