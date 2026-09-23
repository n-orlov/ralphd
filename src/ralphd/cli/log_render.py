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
            "toolcall_seen": False, "tool_args": {}, "open_tool_line": None,
            "tool_pending": {},
            # Task cure-01-03-2 (PRD Q.1): which of the three collapse flags
            # above hold a bounded reader's FALLBACK rather than a measured
            # value -- empty for a genuine, uncut iteration start, where
            # every flag starts `False` because nothing precedes it. Set from
            # a cut "start" boundary's `collapseSeed["unresolved"]`; see
            # `render_log_line`'s own comment there and
            # `log_merge._unresolved_seed_flags`.
            "collapse_unresolved": set()}


def render_pending_tool_invocations(state: dict, tty: bool) -> None:
    """Print the invocation line for every tool call that is STILL IN
    FLIGHT in `state` -- started, with no matching `tool_execution_end`
    rendered yet -- exactly as live rendering would have printed it at
    `tool_execution_start` time.

    Approach-4 task 010: a follow that ATTACHES while a tool call is
    already running used to lose that call's invocation line entirely. The
    pretty follow path renders its backlog snapshot buffered
    (`render_to_lines`, `live=False`), which deliberately prints nothing
    for a `tool_execution_start` (a finished call must render as exactly
    one line), and then resumes the live stream with `skip_lines` set past
    every raw line that snapshot already consumed -- including that
    `tool_execution_start`. Neither half printed it, so the operator saw
    `[thinking\u2026]` and then, minutes later, a bare `\u21b3 \u2713 ok` for a
    command they were never shown -- the precise blindness tasks 003/004
    exist to prevent. Calling this once, after the backlog and before the
    live continuation, restores the missing line; the matching end event
    then renders through the ordinary live path (in-place rewrite on a
    TTY, completion-only line when piped).
    """
    for tcid, (name, args) in list(state.get("tool_pending", {}).items()):
        _finalize_open_tool_line(state, tty)
        _render_tool_start(name, args, tty, state, tcid)
        state["toolcall_seen"] = True


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
        # Task 006 (PRD F): a "start" line carrying a `collapseSeed` is not
        # a genuine iteration start -- it is `merge_entries_tail`'s stand-
        # in for a cut that lands INSIDE an iteration (see below) -- so it
        # must never print the iteration header `_render_boundary` would
        # otherwise draw: that header is a REAL rendered line, and a
        # bounded/live tail that happens to include this synthetic
        # boundary must render EXACTLY the same lines a full render's
        # matching suffix would, no more (an extra header line here is
        # itself the "spurious line" PRD F.4 rules out, just as much as a
        # duplicated text line would be). A genuine start (no
        # `collapseSeed` key at all) or any "end" boundary always prints,
        # exactly as before this task.
        is_cut_start = ev.get("event") == "start" and "collapseSeed" in ev
        if not is_cut_start:
            _render_boundary(ev, tty)
        # Task 006 (PRD F): a "start" boundary line synthesized by
        # `log_merge.merge_entries_tail` for a raw fetch window that cuts
        # INTO an iteration (rather than beginning at its true start)
        # carries a `collapseSeed` -- the `text_seen`/`thinking_seen`/
        # `toolcall_seen` values that iteration's OMITTED earlier raw
        # lines would already have set, computed by
        # `log_merge._prior_collapse_state` from bytes the bounded reader
        # already had in hand (never a second disk read). Seeding from it
        # here, instead of unconditionally resetting every flag to
        # `False`, is what keeps a bounded tail's collapse state -- and
        # therefore its rendered bytes -- identical to a full render's,
        # across the exact restart this boundary line marks. A GENUINE
        # iteration start (or an "end" boundary, which never carries a
        # seed) has no `collapseSeed` key at all, so `.get(...)` defaults
        # every flag to `False` -- byte-identical to this function's
        # pre-task-006 behavior.
        seed = ev.get("collapseSeed") if ev.get("event") == "start" else None
        seed = seed or {}
        state.update(text_open=False,
                    text_seen=bool(seed.get("textSeen")),
                    thinking_seen=bool(seed.get("thinkingSeen")),
                    toolcall_seen=bool(seed.get("toolcallSeen")),
                    # Task cure-01-02 (#195): a tool call this iteration's
                    # OMITTED earlier raw lines started but never closed
                    # carries its arguments in `seed["toolArgs"]` -- seeded
                    # here instead of unconditionally reset to `{}`, so the
                    # RETAINED tail's own `tool_execution_end` for that same
                    # call can still render its invocation with arguments
                    # rather than bare (the exact PRD F gap: "the renderer
                    # resets tool_args to an empty mapping"). A genuine
                    # start/end boundary carries no `toolArgs` key, so this
                    # defaults to `{}` -- byte-identical to before.
                    # Task cure-01-03-2 (PRD Q.1): the flags named in
                    # `seed["unresolved"]` are the ones whose boolean just
                    # above is `log_merge`'s bounded FALLBACK rather than
                    # knowledge -- their setter was never read, and "the
                    # look-back stopped" is not "the omitted prefix set
                    # nothing" (`log_merge._unresolved_seed_flags`). They are
                    # carried in the render state, distinct from the booleans,
                    # so a consumer that must tell "no state" from "unknown
                    # state" reads this set instead of guessing from a `False`.
                    # This renderer still prints from the fallback, because it
                    # must emit something for the line in front of it; the
                    # alternative fallback (unknown treated as SEEN, so the
                    # line is suppressed) makes every named Q scenario
                    # correct and costs 30,248,729 transcript bytes on PRD P's
                    # own padded fixture against P.1/P.5's unmoved
                    # 1,000,000-byte ceiling, while RESOLVING the flag instead
                    # is not payable either -- review's own silent-prelude
                    # variant puts the setter 1,067,000 bytes from byte 0 and
                    # 10,720,791 from the file's end, both past that whole
                    # ceiling -- so the direction is a clause conflict filed
                    # as a petition with those measurements, never decided
                    # here. What IS decided here: the unknown travels as a
                    # name, so a consumer can take the other fallback itself.
                    collapse_unresolved=set(seed.get("unresolved") or ()),
                    tool_args=dict(seed.get("toolArgs") or {}), tool_pending={})
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
            # Approach-4 task 010: remember the still-unfinished call (name
            # AND args) so a follow that attached mid-call can print its
            # invocation line after the buffered backlog -- see
            # `render_pending_tool_invocations`. Dropped again by the
            # matching end event below and by every iteration boundary.
            state.setdefault("tool_pending", {})[tcid] = (
                ev.get("toolName", "?"), args)
        if live:
            _render_tool_start(ev.get("toolName", "?"), args, tty, state, tcid)
            state["toolcall_seen"] = True
    elif etype == "tool_execution_end":
        tcid = ev.get("toolCallId")
        args = state["tool_args"].pop(tcid, None) if tcid is not None else None
        if tcid is not None:
            state.setdefault("tool_pending", {}).pop(tcid, None)
        if live:
            _render_tool_completion(ev, tty, args, state)
        else:
            _render_tool_result(ev, tty, args)
        state["toolcall_seen"] = True
    elif etype == "message_end":
        _render_message_end(ev.get("message") or {}, state, tty)
    elif etype == "message_start":
        # Task 019 (PRD G.8, #214): a real transcript's `message_start`
        # (docs/json.md's `AgentEvent`) opens a BRAND NEW assistant
        # message -- nothing of ITS content has been shown yet, live or
        # otherwise. Before this branch existed, `text_seen`/
        # `thinking_seen`/`toolcall_seen` were reset ONLY at an
        # `ralphd.iteration` boundary, never between the several messages
        # a single iteration commonly has (one per tool-calling round
        # trip). The confirmable, reproducible defect that leaves: once
        # ANY earlier message in the iteration streamed so much as one
        # character of text, `text_seen` stays `True` for the rest of the
        # iteration, so a LATER message whose text arrives only in its
        # own `message_end` (no preceding `text_delta` -- e.g. a short
        # non-streamed reply) is silently swallowed: `_render_message_end`
        # sees `state["text_seen"]` already `True` and prints nothing at
        # all for it, for every text block that message carries. (The PRD
        # text this task shipped against, written against `af0b57e2`,
        # describes a DIFFERENT shape -- "consumes text_seen at the first
        # substantive text item, so ... prints the second one twice".
        # `git diff af0b57e2 HEAD -- src/ralphd/cli/log_render.py` is
        # empty: this function is byte-identical to that commit, and it
        # never assigns `state["text_seen"]` at all, so nothing in it
        # ever "consumes" the flag or reprints anything -- a literal
        # double-print could not be reproduced against the shipped code
        # under any event sequence tried. What DOES reproduce, on demand,
        # through this exact renderer, is the drop above: a `message_end`
        # with two fresh text blocks after an earlier message's stream
        # renders NEITHER, where it must render both, each exactly once.)
        # Resetting the three collapse flags here -- and ONLY here, never
        # `text_open`/`tool_args`/`tool_pending`, which track state that
        # is not scoped to "has this message's content been shown" --
        # fixes exactly that gap. No existing fixture in this tree emits
        # `message_start` at all (confirmed by grep before this change),
        # so nothing that already passes could regress from adding a new
        # branch for a previously-unrecognized type.
        state.update(text_seen=False, thinking_seen=False, toolcall_seen=False)
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


# Cheap textual pre-filter for a boundary line (`log_merge.boundary_line`
# always serializes `"type": "ralphd.iteration"` through `json.dumps`, so
# the type value appears verbatim as this quoted token). Used only to
# LOCATE candidate lines while scanning backwards -- every hit is then
# confirmed by actually parsing that one line (`_is_boundary_line`), so a
# transcript that merely mentions the token inside some other event's text
# is rejected rather than treated as a restart point. Pre-filtering with
# `str.rfind` is what keeps the backwards scan proportional to the tail:
# it never parses, or even splits, the history in front of the window.
_BOUNDARY_TOKEN = '"ralphd.iteration"'


def _is_boundary_line(raw_line: str) -> bool:
    """True when this ONE raw line really is a `ralphd.iteration` boundary
    event -- i.e. a safe restart point (see `_previous_boundary_offset`)."""
    if not raw_line.strip():
        return False
    try:
        ev = json.loads(raw_line)
    except json.JSONDecodeError:
        return False
    return isinstance(ev, dict) and ev.get("type") == "ralphd.iteration"


def _previous_boundary_offset(raw_text: str, before: int) -> int | None:
    """Character offset of the START of the newest `ralphd.iteration`
    boundary line that begins strictly before offset `before`, or None when
    there is none -- found by scanning BACKWARDS from `before` with
    `str.rfind`, never by walking the blob forwards.

    Why a boundary line, and ONLY a boundary line, is a safe restart point:
    `log_render`'s per-iteration guard state (`text_open`/`text_seen`/
    `thinking_seen`/`toolcall_seen`/`tool_args`/`tool_pending`) is
    ORDER-DEPENDENT -- `_render_message_end`'s `if not state["text_seen"]`
    and the matching thinking/toolcall guards only print the right thing
    because they know everything since the last reset.
    `render_log_line`'s `ralphd.iteration` branch resets ALL of that state
    UNCONDITIONALLY the moment it sees either boundary event (`event:
    "start"` or `event: "end"`), regardless of what the state held walking
    in. So a `new_render_state()` started fresh at a boundary line renders
    that line and everything after it identically to a full render from
    offset 0 -- nothing before the boundary can leak forward. Any OTHER
    line is unsafe: starting mid-iteration risks e.g. re-printing a
    `[thinking\u2026]` line the real state had already seen, or missing a
    `text_seen`-gated line it hadn't. Offset 0 is the outermost fallback,
    trivially safe because nothing precedes it, and is what a blob with no
    boundary line at all falls back to."""
    end = before
    while end > 0:
        hit = raw_text.rfind(_BOUNDARY_TOKEN, 0, end)
        if hit < 0:
            return None
        start = raw_text.rfind("\n", 0, hit) + 1
        stop = raw_text.find("\n", hit)
        line = raw_text[start:] if stop < 0 else raw_text[start:stop]
        if start < before and _is_boundary_line(line):
            return start
        # A false positive (the token inside some other event) or a hit on
        # the line we already started at: keep walking backwards.
        end = hit
    return None


def render_tail_lines(raw_text: str, tty: bool, n: int) -> list[str]:
    """The bounded-cost sibling of `render_to_lines` (PRD H.1): render only
    the last `n` RENDERED lines of `raw_text`, at a cost bounded by the
    tail rather than by the whole blob, by reading backwards from the
    newest events in growing chunks instead of rendering everything and
    trimming (what `rendered_log_lines` in `ui_server.py` does today).

    RESTART POINT chosen: every `ralphd.iteration` boundary line -- see
    `_previous_boundary_offset`'s docstring for exactly why that, and
    only that, is a safe place to start a fresh `new_render_state()` and
    still get output byte-identical to a full render from line 0 (pinned
    by task 044/H.2's byte-identical test). A naive fixed-line or
    fixed-byte reversed scan would instead land mid-iteration and render
    from an arbitrary, wrong guard state -- exactly the "changes what the
    operator sees" naive-reversed-scan defect H.1 calls out.

    CHUNK GROWTH: starting from the single most recent boundary, each
    retry that still renders fewer than `n` lines DOUBLES how many
    boundaries back the window restarts from (1, 2, 4, 8, ...) and
    re-renders from there, until either the render yields at least `n`
    lines or the window has grown all the way back to offset 0 (nothing
    further back to include). Growing by boundary COUNT rather than a
    fixed byte/line size means a run with a few huge iterations doesn't
    thrash on an undersized window, and a small tail on a run with many
    small iterations stops after a couple of small chunks rather than
    always falling back to offset 0.

    BOUNDED IN THE HISTORY, not just in the output: the window's start is
    located by scanning BACKWARDS from the end of `raw_text`
    (`_previous_boundary_offset`, `str.rfind`), and only the window itself
    is ever split into lines or JSON-parsed. Prepending an arbitrary amount
    of older history in front of the same tail therefore does not add
    `json.loads` calls or rendered lines -- pinned by
    `tests/test_log_render_bounded_tail.py`'s parse-count test. The
    earlier shape of this function walked the whole blob forwards to index
    every boundary first, which made its cost proportional to the history
    after all; that is the defect this docstring's promise now holds it to.
    """
    if n <= 0 or not raw_text:
        return []
    # Boundary offsets already located, newest-first; grown lazily so a
    # tail satisfied by the first chunk never looks further back.
    found: list[int] = []
    oldest_searched = len(raw_text)
    exhausted = False
    chunk = 1
    while True:
        while not exhausted and len(found) < chunk:
            offset = _previous_boundary_offset(raw_text, oldest_searched)
            if offset is None:
                exhausted = True
                break
            found.append(offset)
            oldest_searched = offset
        start = found[chunk - 1] if len(found) >= chunk else 0
        rendered = render_to_lines(raw_text[start:], tty, new_render_state())
        if len(rendered) >= n or start == 0:
            return rendered[-n:] if rendered else []
        chunk *= 2
