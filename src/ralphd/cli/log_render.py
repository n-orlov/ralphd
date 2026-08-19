"""Shared NDJSON-events -> rendered-lines pretty renderer (task 001-004,
057, 014).

Originally lived inline in `main.py` (the `ralphctl logs` CLI path). Task
014 pulled it out into its own module so the control-plane hub UI server
(`ui_server.py`) can render a run's log tail through the EXACT SAME code
path as the CLI -- rather than `web/app.js` reimplementing an
event-to-HTML mapping client-side (the state of affairs before this task,
which is how the many-delta-thinking-block flood defect happened: the JS
`thinking_start`/`thinking_delta` branch had no `thinking_seen` guard
where this module's `_render_message_update`/`_render_message_end` do).

`main.py` cannot itself be the shared home for these functions: it
imports `ui_server` (for the `ralphctl ui` subcommand), so `ui_server`
importing back from `main` would be circular. This module has no
dependency on either.

Public surface used by both callers:
  - `new_render_state()` -- a fresh mutable render-state dict.
  - `render_to_lines(raw_text, tty, state)` -- render a full NDJSON blob
    into a list of terminal lines (used by CLI buffered/bounded
    rendering, task 057, and by the hub's non-follow log tail).
  - `render_log_line(raw_line, tty, state, live=...)` -- render exactly
    one NDJSON line, printing directly (used by the CLI's live follow
    path, `_stream_logs`/`_stream_logs_pretty_tailed`).

`tty` controls only ANSI color/style codes (`_ansi`) -- callers that want
plain, uncolored/unstyled output (e.g. the hub, which HTML-escapes the
text and applies its OWN CSS classes rather than ANSI) pass `tty=False`.
Passing `tty=False` also guarantees the rendered bytes contain no `\r` or
ANSI control sequences (task 004's piped-output contract), which is
exactly what the hub JSON endpoint needs to embed safely in a `<pre>`/
text node without stray escape bytes leaking into the DOM.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys

from ..engine.state import (
    elapsed_seconds,
    format_cost,
    format_duration,
    format_local_time,
)

# Tool-name groupings used by `_fmt_invocation` to pick which argument is
# "the salient one" for a compact one-liner. These are pi's built-in tool
# names (docs/extensions.md: read, bash, edit, write, grep, find, ls) but
# the unknown-tool fallback below means an unrecognized/custom tool name
# still renders something useful rather than nothing.
_PATH_TOOLS = {"read", "write", "edit"}
_PATTERN_TOOLS = {"grep", "glob", "find"}

# A tail value far larger than any real transcript can ever be: used to
# force both `GET /logs` and `GET /iterations/{n}/output` to replay their
# FULL current backlog when opening a follow connection for the pretty,
# rendered-line-trimmed follow path (task 057) -- see
# `_stream_logs_pretty_tailed` in main.py.
FULL_BACKLOG_TAIL = 10**9


def _ansi(tty: bool, code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if tty else text


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _first_scalar(obj) -> str | None:
    """Best-effort first scalar value out of a tool-call argument mapping,
    for unknown tools we have no dedicated rendering rule for."""
    if isinstance(obj, (str, int, float, bool)):
        return str(obj)
    if not isinstance(obj, dict):
        return None
    for v in obj.values():
        if isinstance(v, (str, int, float, bool)):
            return str(v)
    return None


def _fmt_invocation(name: str, args) -> str:
    """Render a tool call's salient argument into a single-line
    `<tool> <arg>` invocation string (task 001 / PRD req A1): bash shows
    its command (newlines collapsed, generously truncated at ~300 chars so
    the operator sees enough to know what ran); read/write/edit show the
    path; grep/glob/find-style tools show the pattern; anything else falls
    back to the first scalar argument value, truncated. Redaction happens
    at write/serve time (src/ralphd/engine/redact.py), not here, so
    rendering the full command/path/pattern does not widen the
    secret-exposure surface -- it only shows what the transcript already
    contains once scrubbed."""
    if not isinstance(args, dict):
        args = {}
    if name == "bash":
        cmd = " ".join(str(args.get("command", "")).split())
        return f"bash $ {_truncate(cmd, 300)}" if cmd else "bash"
    if name in _PATH_TOOLS:
        path = args.get("path")
        return f"{name} {_truncate(str(path), 300)}" if path else name
    if name in _PATTERN_TOOLS:
        pattern = args.get("pattern")
        return f"{name} {_truncate(str(pattern), 300)}" if pattern else name
    val = _first_scalar(args)
    return f"{name} {_truncate(val, 300)}" if val is not None else name


def _render_boundary(ev: dict, tty: bool) -> None:
    n, phase, model, approach = (ev.get("number"), ev.get("phase"),
                                  ev.get("model"), ev.get("approach"))
    # Task 048 (#4): the boundary lines are the only place a transcript can
    # be anchored in wall-clock time -- without them a reader scrolling a
    # merged log knows an iteration "took 4m 12s" but not *when*, which is
    # exactly what an operator correlating a run against an upstream outage
    # needs. Rendered through the one shared `format_local_time` formatter
    # (`engine/state.py`); the raw ISO values stay in the boundary JSON line
    # itself (`log_merge.boundary_line`) for machine consumers.
    if ev.get("event") == "start":
        print(_ansi(tty, "1;36",
              f"── iteration {n} · phase={phase} · model={model} · "
              f"approach={approach} · started {format_local_time(ev.get('startedAt'))} ──"))
        return
    usage = ev.get("usage") or {}
    bits = [f"iteration {n} done"]
    if ev.get("endedAt"):
        bits.append(f"at {format_local_time(ev.get('endedAt'))}")
    dur = elapsed_seconds(ev.get("startedAt"), ev.get("endedAt"))
    if dur is not None:
        bits.append(f"took {format_duration(dur)}")
    if ev.get("exitCode") is not None:
        bits.append(f"exit={ev['exitCode']}")
    if usage:
        bits.append(f"tokens={usage.get('totalTokens', 0)}")
        # Task 051 (#10): the per-iteration footer goes through the one shared
        # `format_cost` too (`decimals=None` == the historical raw `str(float)`
        # rendering, so a priced iteration's line is byte-identical), which is
        # what turns an iteration whose tokens the provider never priced into
        # `cost=unavailable` instead of silently dropping the cost bit.
        cost = format_cost(usage, decimals=None)
        if cost is not None:
            bits.append(f"cost={cost}")
    print(_ansi(tty, "2", "  " + ", ".join(bits)))
    if ev.get("error"):
        print(_ansi(tty, "1;31", f"!! iteration {n} error: {ev['error']}"))


def _render_message_update(evt: dict, state: dict, tty: bool) -> None:
    t = evt.get("type")
    if t == "text_delta":
        sys.stdout.write(evt.get("delta", ""))
        sys.stdout.flush()
        state["text_open"] = True
        state["text_seen"] = True
    elif t == "text_end":
        if state["text_open"]:
            print()
            state["text_open"] = False
    elif t in ("thinking_start", "thinking_delta"):
        if not state["thinking_seen"]:
            print(_ansi(tty, "2;3", "  [thinking…]"))
            state["thinking_seen"] = True


def _render_tool_result(ev: dict, tty: bool, args: dict | None = None) -> None:
    """`args` is the tool call's arguments as captured off the earlier
    `tool_execution_start` event for the same `toolCallId` -- the wire
    format's `tool_execution_end` event (docs/json.md) carries only
    `toolName`/`result`/`isError`, never the arguments, so without this
    the invocation line would always render bare (`bash ✓ ok`, the exact
    defect this task fixes). Callers that have no start event to key off
    of (e.g. legacy/buffered call sites) may pass `None` / omit it and
    fall back to whatever `ev` itself carries, if anything.

    This is the BUFFERED/one-shot rendering of a completed tool call --
    invocation + outcome on a single line. Live/follow rendering (task
    003) instead splits this into two calls: `_render_tool_start` at
    `tool_execution_start` time and `_render_tool_completion` at
    `tool_execution_end` time, so the operator sees the invocation the
    moment the call starts rather than only once it finishes."""
    name = ev.get("toolName", "?")
    if args is None:
        args = ev.get("args") or ev.get("arguments") or {}
    invocation = _fmt_invocation(name, args)
    outcome, tail = _tool_outcome_and_tail(ev, tty)
    print(f"  → {invocation} {outcome}{tail}")


def _text_from_content_item(item) -> str | None:
    """A single content-list entry's text, if it is (or resembles) the
    standard `{"type": "text", "text": "..."}` shape -- tolerant of a
    missing/other `type` as long as a non-empty string `text` is present,
    since some tool results omit the discriminator."""
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None


def _extract_structured_excerpt(result, limit: int) -> str | None:
    """Best-effort short text excerpt out of a STRUCTURED (non-string)
    `tool_execution_end.result` value (task 015 / PRD req), by walking the
    standard MCP/pi content-list shape
    `{"content": [{"type": "text", "text": ...}, ...]}` for the first
    non-empty text item, truncated to `limit` chars. Also tolerates a
    bare top-level content LIST (no wrapping `content` key) and an
    `error`/`detail` field carrying the same shape (structured error
    payloads). Returns `None` for any shape this doesn't recognize --
    deliberately never falls back to stringifying/JSON-dumping the whole
    object, since that would defeat the point of a SHORT excerpt (and
    could dump arbitrary structured noise into the pretty renderer)."""
    if isinstance(result, str):
        return _truncate(result, limit) if result else None
    candidates: list = []
    if isinstance(result, dict):
        for key in ("content", "error", "detail"):
            val = result.get(key)
            if isinstance(val, list):
                candidates = val
                break
            text = _text_from_content_item(val)
            if text is not None:
                return _truncate(text, limit)
    elif isinstance(result, list):
        candidates = result
    for item in candidates:
        text = _text_from_content_item(item)
        if text is not None:
            return _truncate(text, limit)
    return None


def _tool_outcome_and_tail(ev: dict, tty: bool) -> tuple[str, str]:
    """Shared ✓/✗-plus-excerpt formatting used by both the buffered
    one-line form (`_render_tool_result`) and the live completion-only
    line (`_render_tool_completion`, task 003), and by both TTY and piped
    rendering paths (task 004) since it never touches the cursor itself.

    On success the excerpt is truncated to 60 chars (unchanged from
    before); on failure it is allowed a bit more room (120 chars) since
    error text is where an operator most needs the detail (task 015).
    A plain string result is used as-is (unchanged behavior). A
    STRUCTURED (non-string) result -- e.g. the standard
    `{"content": [{"type": "text", "text": ...}]}` shape -- is walked by
    `_extract_structured_excerpt` for its first non-empty text item; an
    unrecognized/unknown structured shape yields NO excerpt at all (never
    a stringified JSON dump of the whole result)."""
    is_error = bool(ev.get("isError"))
    outcome = _ansi(tty, "1;31", "✗ error") if is_error else _ansi(tty, "1;32", "✓ ok")
    result = ev.get("result")
    limit = 120 if is_error else 60
    excerpt = _extract_structured_excerpt(result, limit)
    tail = f" ({excerpt})" if excerpt else ""
    return outcome, tail


def _render_tool_start(name: str, args, tty: bool, state: dict | None = None,
                       tool_call_id=None) -> None:
    """Print the invocation line the moment `tool_execution_start`
    arrives (task 003 / PRD req A3), with NO ✓/✗ status yet -- the
    outcome isn't known until `tool_execution_end`. Used only for live
    (follow) rendering; buffered rendering waits for the end event and
    calls `_render_tool_result` so a finished call still renders as
    exactly one line, never a start+end duplicate.

    On a TTY (task 004) the line is written WITHOUT a trailing newline
    and `state['open_tool_line']` is set to `tool_call_id` -- the cursor
    is deliberately left sitting at the end of the invocation text so
    `tool_execution_end` can later rewrite it in place (`\r` + ANSI
    erase-line) into the exact same one-liner the buffered/non-live
    renderer would have produced, rather than appending a second line.
    On a non-TTY (piped) stream there is no cursor to rewind, so this
    prints a plain, complete line (with a trailing newline) and leaves
    `open_tool_line` unset -- the matching `tool_execution_end` then
    prints a short, separate completion line (`_render_tool_completion`)
    instead of attempting any in-place rewrite, and the piped bytes never
    contain `\r` or ANSI control sequences."""
    line = f"  → {_fmt_invocation(name, args)}"
    if tty and state is not None:
        sys.stdout.write(line)
        sys.stdout.flush()
        state["open_tool_line"] = tool_call_id
    else:
        print(line)


def _finalize_open_tool_line(state: dict, tty: bool) -> None:
    """If a TTY tool-invocation line is currently left open (cursor still
    sitting at its end, task 004), close it out with a plain newline
    before any other renderable content is printed -- e.g. if streamed
    assistant text or a new iteration boundary interleaves before the
    open call's `tool_execution_end` arrives. The eventual (or already
    happened) completion for that call then renders as its own short
    line via `_render_tool_completion` rather than an in-place rewrite,
    since the cursor has moved on."""
    if tty and state.get("open_tool_line") is not None:
        sys.stdout.write("\n")
        sys.stdout.flush()
        state["open_tool_line"] = None


def _render_tool_completion(ev: dict, tty: bool, args=None, state: dict | None = None) -> None:
    """Render the outcome of a completed tool call in live (follow) mode.

    If `state['open_tool_line']` still matches this call's `toolCallId`
    (i.e. its invocation line is still open on a TTY, cursor sitting at
    its end, nothing else printed in between) this REWRITES that line in
    place -- `\r` + ANSI erase-to-end-of-line, then the full
    `invocation outcome (excerpt)` text -- so the finished TTY stream is
    byte-for-byte identical to the buffered one-line-per-tool rendering
    (`_render_tool_result`). Otherwise (non-TTY, or the line was already
    finalized by an interleaving event) this prints just the outcome as
    a short, separate completion line, never repeating the invocation
    text."""
    outcome, tail = _tool_outcome_and_tail(ev, tty)
    tcid = ev.get("toolCallId")
    if tty and state is not None and state.get("open_tool_line") == tcid:
        invocation = _fmt_invocation(ev.get("toolName", "?"), args or {})
        sys.stdout.write(f"\r\x1b[2K  → {invocation} {outcome}{tail}\n")
        sys.stdout.flush()
        state["open_tool_line"] = None
    else:
        print(f"  ↳ {outcome}{tail}")


def _render_message_end(message: dict, state: dict, tty: bool) -> None:
    for item in message.get("content") or []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "text":
            if not state["text_seen"]:
                print(item.get("text", ""))
        elif kind == "thinking":
            if not state["thinking_seen"]:
                print(_ansi(tty, "2;3", "  [thinking…]"))
        elif kind == "toolCall" and not state["toolcall_seen"]:
            print(f"  → {_fmt_invocation(item.get('name', '?'), item.get('arguments') or {})}")


def new_render_state() -> dict:
    return {"text_open": False, "text_seen": False, "thinking_seen": False,
            "toolcall_seen": False, "tool_args": {}, "open_tool_line": None}


def render_log_line(raw_line: str, tty: bool, state: dict, live: bool = False) -> None:
    """Render a single merged/per-iteration NDJSON line into the pretty
    format (iteration headers, streamed assistant text, compact tool
    one-liners, elided thinking, usage/cost footers, error highlights).
    Shared by every bounded, live-streaming, and rendered-line-capturing
    (`render_to_lines`) path so a line is rendered identically
    regardless of whether the whole response was buffered first or is
    arriving incrementally off an open connection -- with ONE deliberate
    exception: tool-call rendering (task 003). When `live` is true (only
    `_stream_logs`'s genuinely-live follow path passes this) the
    invocation line prints immediately at `tool_execution_start`, before
    the outcome is known, so an operator watching a follow never waits on
    a long-running tool with no feedback; the matching
    `tool_execution_end` then prints a short completion-only line rather
    than repeating the invocation. When `live` is false (the default --
    every buffered/bounded rendering path, e.g. `render_to_lines`, and the
    hub UI's non-follow log tail, task 014) a completed call still
    renders as exactly the single one-liner it always has, since the
    whole transcript is already in hand and there is nothing 'live' to
    show early. Unknown event types are silently skipped; a malformed
    (non-JSON) line prints a one-line marker and is skipped."""
    if not raw_line.strip():
        return
    try:
        ev = json.loads(raw_line)
    except json.JSONDecodeError:
        print(_ansi(tty, "33", f"! [malformed log line, {len(raw_line)} bytes]"))
        return
    if not isinstance(ev, dict):
        print(_ansi(tty, "33", "! [malformed log line: not a JSON object]"))
        return
    etype = ev.get("type")
    # task 004: any event OTHER than the matching tool_execution_end must
    # first close out a still-open live TTY invocation line (cursor left
    # sitting at its end by `_render_tool_start`) with a plain newline --
    # otherwise iteration boundaries / streamed text / a new tool call
    # would print into the middle of that line instead of below it.
    if etype != "tool_execution_end":
        _finalize_open_tool_line(state, tty)
    if etype == "ralphd.iteration":
        _render_boundary(ev, tty)
        state.update(text_open=False, text_seen=False, thinking_seen=False,
                    toolcall_seen=False, tool_args={})
    elif etype == "message_update":
        _render_message_update(ev.get("assistantMessageEvent") or {}, state, tty)
    elif etype == "tool_execution_start":
        # `tool_execution_end` (docs/json.md) carries no `args`/`arguments`
        # field, only `tool_execution_start` does -- stash them here keyed
        # by toolCallId so the end event's one-liner can still show the
        # salient argument (task 001 / PRD req A1). In live rendering
        # (task 003) the invocation line is ALSO printed right now, since
        # there is no guarantee the matching end event ever arrives before
        # the follow connection itself ends (e.g. the process is still
        # mid-tool-call when the operator is watching).
        tcid = ev.get("toolCallId")
        args = ev.get("args") or ev.get("arguments") or {}
        if tcid is not None:
            state["tool_args"][tcid] = args
        if live:
            _render_tool_start(ev.get("toolName", "?"), args, tty, state, tcid)
            state["toolcall_seen"] = True
    elif etype == "tool_execution_end":
        tcid = ev.get("toolCallId")
        args = state["tool_args"].pop(tcid, None) if tcid is not None else None
        if live:
            _render_tool_completion(ev, tty, args, state)
        else:
            _render_tool_result(ev, tty, args)
        state["toolcall_seen"] = True
    elif etype == "message_end":
        _render_message_end(ev.get("message") or {}, state, tty)
    # everything else (unrecognized/future event types) is silently
    # skipped by design.


def render_to_lines(raw_text: str, tty: bool, state: dict) -> list[str]:
    """Render every line of `raw_text` (a merged/per-iteration NDJSON blob)
    through `render_log_line`, capturing the printed output instead of
    writing it directly, and return it split into a list of terminal
    lines -- i.e. exactly what the operator would SEE, one rendered line
    per list entry, as opposed to one raw NDJSON event per entry (task
    057). `state` is mutated in place (boundary/text/thinking/toolcall
    flags) so a caller can keep rendering a live continuation with the
    same running state afterwards (see `_stream_logs_pretty_tailed` in
    main.py). This is the exact function the hub UI server (task 014)
    calls server-side for `GET /api/runs/<id>/logs` so the browser never
    reimplements event-to-text rendering."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for raw_line in raw_text.splitlines():
            render_log_line(raw_line, tty, state)
    text = buf.getvalue()
    if not text:
        return []
    return text.removesuffix("\n").split("\n")
