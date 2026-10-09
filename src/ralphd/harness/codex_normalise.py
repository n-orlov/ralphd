"""Pure Codex-event to pi-event normaliser.

The Codex wrapper reads the `codex exec --json` NDJSON stream and must write
the same event stream pi writes, so that `runner._scan_line`, `log_merge`,
`log_render`, `collapse_index` and `count_tool_calls` work unchanged. This
module owns that translation and nothing else: it takes one decoded Codex event
dict at a time and returns a list of pi-shaped event dicts. It never imports
the Claude SDK (or `ralphd.harness.claude_code`, which does) and never does
I/O, so the engine process can import it freely.

Codex has no message ids, no token deltas and no per-message usage, so the
normaliser groups items into assistant messages itself. A reasoning or
agent message that follows a tool item starts a new message; tool items join
the open message. A group closes when the next reasoning or agent message
follows a tool item, on `turn.completed` or `turn.failed`, and at `finish`. A
group with a tool call ends `toolUse`, one without ends `stop`.

Event order for one group: `message_start`, then per item `message_update`
(`thinking_start` + `thinking_delta`, or `text_delta` + `text_end`) and
`tool_execution_start` / `tool_execution_end` (one pair per command, per
file change and per MCP call), then `message_end`.

An event or item type this module does not know produces no pi event, raises
nothing, and is not traffic (`is_traffic`): the wrapper logs it as a raw
envelope only.
"""

from __future__ import annotations

import re
from typing import Any

# The wrapper's stdout carries pi events plus one raw envelope per Codex
# event, `{"type": CODEX_RAW_ENVELOPE_TYPE, "raw": <the Codex event>}`. The
# engine-side runner demuxes them. A raw envelope is agent traffic for the
# stall watchdogs only when `is_traffic` says so.
CODEX_RAW_ENVELOPE_TYPE = "ralphd.codex_raw"

PROVIDER = "openai"
RALPHD_SERVER = "ralphd"

# Item types that produce pi events. Anything else is tolerated, not mapped.
_KNOWN_ITEMS = frozenset(
    {"reasoning", "agent_message", "command_execution", "file_change", "mcp_tool_call"}
)
_KNOWN_EVENTS = frozenset(
    {"item.started", "item.completed", "turn.completed", "turn.failed"}
)

# `add` creates a file; every other change kind rewrites or removes one.
_WRITE_KINDS = frozenset({"add"})

_STATUS_RES = (
    re.compile(r"\b(?:last\s+status|status(?:\s+code)?|http(?:\s+status)?)\W{0,3}([1-5]\d\d)\b", re.I),
    re.compile(r"\"(?:code|status|status_code)\"\s*:\s*\"?([1-5]\d\d)\b", re.I),
)

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
)


def is_traffic(event: Any) -> bool:
    """Does this Codex event count as agent traffic (it resets a stall)?

    `thread.started`, `turn.started`, non-terminal `error` events and any
    event or item type the normaliser does not know are not traffic.
    """
    if not isinstance(event, dict):
        return False
    etype = event.get("type")
    if etype in ("turn.completed", "turn.failed"):
        return True
    if etype in ("item.started", "item.completed"):
        item = event.get("item")
        return isinstance(item, dict) and item.get("type") in _KNOWN_ITEMS
    return False


def pi_usage(usage: Any) -> dict:
    """Map a Codex usage dict to pi's. A missing or non-integer field counts as 0.

    `input_tokens` includes the cached and cache-write tokens, so `input` is
    what is left after both; reasoning tokens are already inside
    `output_tokens`; `totalTokens` is `input_tokens + output_tokens`.
    """
    u = {k: _count(usage, k) for k in _USAGE_FIELDS}
    cached = u["cached_input_tokens"]
    written = u["cache_write_input_tokens"]
    return {
        "input": max(0, u["input_tokens"] - cached - written),
        "output": u["output_tokens"],
        "cacheRead": cached,
        "cacheWrite": written,
        "totalTokens": u["input_tokens"] + u["output_tokens"],
    }


def parse_http_status(text: Any) -> int | None:
    """The HTTP status a Codex error message names, or None (Codex has no structured one)."""
    if not isinstance(text, str):
        return None
    for pattern in _STATUS_RES:
        m = pattern.search(text)
        if m:
            return int(m.group(1))
    return None


def _count(usage: Any, key: str) -> int:
    v = usage.get(key) if isinstance(usage, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _update(kind: str, **fields: Any) -> dict:
    return {"type": "message_update", "assistantMessageEvent": {"type": kind, **fields}}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class CodexNormaliser:
    """Stateful per-wrapper-run translator. One instance per `codex` run."""

    def __init__(self, *, model_ref: str) -> None:
        self._model_ref = model_ref
        self._group: dict | None = None
        self._started: set[str] = set()
        self._seq = 0
        self._prev_usage: dict[str, int] = {}

    # -- public API --------------------------------------------------------

    def feed(self, event: dict) -> list[dict]:
        """Translate one Codex event. Never raises; unknown events give `[]`."""
        if not isinstance(event, dict):
            return []
        etype = event.get("type")
        if etype in ("item.started", "item.completed"):
            item = event.get("item")
            if not isinstance(item, dict):
                return []
            return self._item(etype == "item.completed", item)
        if etype == "turn.completed":
            return self._completed(event)
        if etype == "turn.failed":
            return self._failed(event)
        return []

    def finish(self, error: dict | None) -> list[dict]:
        """Close the stream. `error` is `{status, type, message}` (any key optional).

        With no error the open group (if any) is closed normally. A stream that
        ended without a terminal event passes an error, which closes the open
        group as an `error` message_end.
        """
        if error is None:
            return self._close_group()
        status = error.get("status")
        etype = error.get("type")
        head = " ".join(str(p) for p in (status, etype) if p not in (None, ""))
        body = str(error["message"]) if error.get("message") not in (None, "") else ""
        message = f"{head}: {body}" if head and body else (head or body or "unknown agent error")
        hints = {k: v for k, v in (("httpStatus", status), ("errorType", etype)) if v not in (None, "")}
        stop = error.get("stopReason")
        return self._error_end(message, stop if isinstance(stop, str) and stop else "error", hints)

    # -- items -------------------------------------------------------------

    def _item(self, done: bool, item: dict) -> list[dict]:
        kind = item.get("type")
        if kind == "reasoning" and done:
            return self._block(
                {"type": "thinking", "thinking": _text(item.get("text"))},
                lambda t: [_update("thinking_start"), _update("thinking_delta", delta=t)],
                _text(item.get("text")),
            )
        if kind == "agent_message" and done:
            return self._block(
                {"type": "text", "text": _text(item.get("text"))},
                lambda t: [_update("text_delta", delta=t), _update("text_end", content=t)],
                _text(item.get("text")),
            )
        if kind == "command_execution":
            return self._command(done, item)
        if kind == "file_change":
            return self._file_change(item) if done else []
        if kind == "mcp_tool_call":
            return self._mcp(done, item)
        return []

    def _block(self, content: dict, updates, text: str) -> list[dict]:
        events: list[dict] = []
        group = self._group
        if group is not None and group["tool"]:
            events.extend(self._close_group())
        events.extend(self._open_group())
        self._group["content"].append(content)
        events.extend(updates(text))
        return events

    def _open_group(self) -> list[dict]:
        if self._group is not None:
            return []
        self._group = {"content": [], "tool": False}
        return [{"type": "message_start", "message": {"role": "assistant"}}]

    def _call_id(self, item: dict, suffix: str = "") -> str:
        iid = item.get("id")
        if iid in (None, ""):
            self._seq += 1
            iid = f"item_{self._seq}"
        return f"{iid}{suffix}"

    def _tool_start(self, call_id: str, name: str, args: dict) -> list[dict]:
        events = self._open_group()
        self._group["tool"] = True
        self._group["content"].append(
            {"type": "toolCall", "id": call_id, "name": name, "arguments": args}
        )
        self._started.add(call_id)
        events.append({"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": args})
        return events

    def _tool_end(self, call_id: str, name: str, is_error: bool, result: Any) -> dict:
        self._started.discard(call_id)
        return {
            "type": "tool_execution_end",
            "toolCallId": call_id,
            "toolName": name,
            "isError": is_error,
            "result": result,
        }

    def _command(self, done: bool, item: dict) -> list[dict]:
        call_id = self._call_id(item)
        args = {"command": _text(item.get("command"))}
        events: list[dict] = []
        if call_id not in self._started:
            events.extend(self._tool_start(call_id, "bash", args))
        if done:
            code = item.get("exit_code")
            if isinstance(code, int) and not isinstance(code, bool):
                is_error = code != 0
            else:
                is_error = item.get("status") in ("failed", "declined")
            events.append(self._tool_end(call_id, "bash", is_error, _text(item.get("aggregated_output"))))
        return events

    def _file_change(self, item: dict) -> list[dict]:
        changes = item.get("changes")
        if not isinstance(changes, list):
            return []
        base = self._call_id(item)
        is_error = item.get("status") == "failed"
        events: list[dict] = []
        for i, change in enumerate(changes):
            if not isinstance(change, dict):
                continue
            kind = _text(change.get("kind"))
            name = "write" if kind in _WRITE_KINDS else "edit"
            args = {"path": _text(change.get("path")), "kind": kind}
            call_id = f"{base}:{i}"
            events.extend(self._tool_start(call_id, name, args))
            events.append(self._tool_end(call_id, name, is_error, f"{kind} {args['path']}".strip()))
        return events

    def _mcp(self, done: bool, item: dict) -> list[dict]:
        server = _text(item.get("server"))
        tool = _text(item.get("tool"))
        name = tool if server == RALPHD_SERVER else f"mcp__{server}__{tool}"
        args = item.get("arguments")
        args = dict(args) if isinstance(args, dict) else {}
        call_id = self._call_id(item)
        events: list[dict] = []
        if call_id not in self._started:
            events.extend(self._tool_start(call_id, name, args))
        if done:
            error = item.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else error
                events.append(self._tool_end(call_id, name, True, _text(message) or str(error)))
            else:
                is_error = item.get("status") == "failed"
                events.append(self._tool_end(call_id, name, is_error, _mcp_result(item.get("result"))))
        return events

    # -- terminal events ---------------------------------------------------

    def _completed(self, event: dict) -> list[dict]:
        usage = self._delta(event.get("usage"))
        events: list[dict] = []
        if self._group is None:
            events.extend(self._open_group())
        events.extend(self._close_group(usage=usage))
        return events

    def _delta(self, usage: Any) -> dict:
        """Usage since the previous `turn.completed`: Codex's `usage` is cumulative for the thread."""
        cur = {k: _count(usage, k) for k in _USAGE_FIELDS}
        delta = {k: (cur[k] - self._prev_usage.get(k, 0) if cur[k] >= self._prev_usage.get(k, 0) else cur[k])
                 for k in _USAGE_FIELDS}
        self._prev_usage = cur
        return delta

    def _failed(self, event: dict) -> list[dict]:
        error = event.get("error")
        text = error.get("message") if isinstance(error, dict) else error
        message = _text(text) or "unknown agent error"
        stop = "length" if "max_output_tokens" in message else "error"
        status = parse_http_status(message)
        return self._error_end(message, stop, {"httpStatus": status} if status is not None else {})

    # -- message_end -------------------------------------------------------

    def _close_group(self, *, usage: dict | None = None) -> list[dict]:
        group, self._group = self._group, None
        if group is None:
            return []
        return [{"type": "message_end", "message": self._message(group, usage, "toolUse" if group["tool"] else "stop")}]

    def _message(self, group: dict, usage: dict | None, stop: str) -> dict:
        return {
            "role": "assistant",
            "content": group["content"],
            "usage": pi_usage(usage),
            "provider": PROVIDER,
            "model": self._model_ref,
            "stopReason": stop,
        }

    def _error_end(self, message: str, stop: str, hints: dict) -> list[dict]:
        events: list[dict] = []
        if self._group is None:
            events.extend(self._open_group())
        group, self._group = self._group, None
        end = {"type": "message_end", "message": self._message(group, None, stop)}
        end["message"]["errorMessage"] = message
        if hints:
            end["ralphd"] = hints
        events.append(end)
        return events


def _mcp_result(result: Any) -> Any:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return {"content": result["content"]}
    return str(result)
