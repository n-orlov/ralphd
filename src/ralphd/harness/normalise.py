"""Pure SDK-message to pi-event normaliser.

The Claude Code wrapper reads typed SDK messages and must write the same event
stream pi writes, so that `runner._scan_line`, `log_merge`, `log_render`,
`collapse_index` and `count_tool_calls` work unchanged. This module owns that
translation and nothing else: it takes plain dicts (`dataclasses.asdict(msg)`
of an SDK message, or the wire JSON) plus a `kind` tag, and returns lists of
pi-shaped event dicts. It never imports the SDK and never does I/O, so the
engine process can import it freely.

`kind` is one of `assistant`, `user`, `result`, `rate_limit`. Content blocks are
recognised by their keys (an `asdict` block carries no `type` tag), with an
explicit `type` honoured when present.

Event order for one assistant message group (same `message.id`):
`message_start`, then per block `message_update` (text_delta then text_end, or
thinking_start then thinking_delta) and `tool_execution_start` (exactly one per
tool call), then `message_end`. A user `tool_result` becomes
`tool_execution_end`. A group closes when the next message has a different id,
when a user message arrives, or at `finish`.
"""

from __future__ import annotations

from typing import Any

MCP_PREFIX = "mcp__ralphd__"

# The wrapper's stdout carries pi events plus one raw envelope per SDK
# message, `{"type": RAW_ENVELOPE_TYPE, "raw": {"kind": <SDK class name>,
# ...}}`. The engine-side runner demuxes them. A raw envelope counts as agent
# traffic for the startup watchdog only when its kind is in
# RAW_TRAFFIC_KINDS: a `SystemMessage` (the CLI's own `init`) is not.
RAW_ENVELOPE_TYPE = "ralphd.claude_raw"
RAW_TRAFFIC_KINDS = frozenset(
    {"AssistantMessage", "UserMessage", "RateLimitEvent", "ResultMessage", "StreamEvent"}
)

TOOL_NAMES = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Bash": "bash",
    "Grep": "grep",
    "Glob": "glob",
}

_ARG_KEYS = {"file_path": "path"}

_STOP_REASONS = {"end_turn": "stop", "tool_use": "toolUse", "max_tokens": "length"}

_USAGE_KEYS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_input_tokens", "cacheRead"),
    ("cache_creation_input_tokens", "cacheWrite"),
)


def pi_tool_name(name: str) -> str:
    """The pi tool name for an SDK tool name (`Read` -> `read`, `mcp__ralphd__x` -> `x`)."""
    if name.startswith(MCP_PREFIX):
        return name[len(MCP_PREFIX):]
    return TOOL_NAMES.get(name, name)


def pi_tool_args(name: str, args: Any) -> dict:
    """Pi-shaped args: `file_path` becomes `path`; every other key is unchanged."""
    if not isinstance(args, dict):
        return {}
    return {_ARG_KEYS.get(k, k): v for k, v in args.items()}


def pi_usage(usage: Any) -> dict:
    """Map an SDK usage dict to pi's; `totalTokens` is the sum of the four counters."""
    out = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    if isinstance(usage, dict):
        for src, dst in _USAGE_KEYS:
            v = usage.get(src)
            if isinstance(v, int) and not isinstance(v, bool):
                out[dst] = v
    out["totalTokens"] = out["input"] + out["output"] + out["cacheRead"] + out["cacheWrite"]
    return out


def _block_kind(block: dict) -> str | None:
    t = block.get("type")
    if t in ("text", "thinking", "tool_use", "tool_result"):
        return t
    if "tool_use_id" in block:
        return "tool_result"
    if "name" in block and "input" in block:
        return "tool_use"
    if "thinking" in block:
        return "thinking"
    if "text" in block:
        return "text"
    return None


def _tool_result_payload(content: Any) -> Any:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return {"content": content}
    return str(content)


class Normaliser:
    """Stateful per-iteration translator. One instance per wrapper run."""

    def __init__(self, *, model_ref: str) -> None:
        self._model_ref = model_ref.split("/", 1)[1] if model_ref.startswith("anthropic/") else model_ref
        self._group: dict | None = None
        self._tool_names: dict[str, str] = {}
        self._model: str | None = None
        self._last_text: str = ""
        self._result: dict | None = None

    # -- public API --------------------------------------------------------

    def feed(self, kind: str, msg: dict) -> list[dict]:
        if not isinstance(msg, dict):
            return []
        if kind == "assistant":
            return self._assistant(msg)
        if kind == "user":
            return self._user(msg)
        if kind == "result":
            return self._record_result(msg)
        return []

    def finish(self, error: dict | None) -> list[dict]:
        """Close the stream. `error` is `{status, type, message}` (any key optional)."""
        events = self._close_group()
        result = self._result
        if error is None and result is not None and result.get("is_error"):
            error = {
                "status": result.get("api_error_status"),
                "type": result.get("terminal_reason"),
                "message": result.get("result") or "; ".join(map(str, result.get("errors") or [])),
            }
        if error is not None:
            events.append(self._error_event(error))
        elif result is not None:
            text = result.get("result")
            if isinstance(text, str) and text.strip() and text.strip() != self._last_text.strip():
                events.extend(self._trailing(text))
        return events

    # -- assistant ---------------------------------------------------------

    def _assistant(self, msg: dict) -> list[dict]:
        events: list[dict] = []
        mid = msg.get("message_id") or msg.get("id")
        group = self._group
        if group is None or mid is None or group["id"] != mid:
            events.extend(self._close_group())
            group = self._group = {
                "id": mid,
                "content": [],
                "usage": None,
                "stop_reason": None,
                "model": msg.get("model"),
            }
            events.append({"type": "message_start", "message": {"role": "assistant"}})
        if msg.get("model"):
            group["model"] = msg["model"]
            self._model = msg["model"]
        if msg.get("usage") is not None:
            group["usage"] = msg["usage"]
        if msg.get("stop_reason"):
            group["stop_reason"] = msg["stop_reason"]
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = _block_kind(block)
            if kind == "text":
                text = block.get("text") or ""
                group["content"].append({"type": "text", "text": text})
                events.append(_update("text_delta", delta=text))
                events.append(_update("text_end", content=text))
            elif kind == "thinking":
                text = block.get("thinking") or ""
                group["content"].append({"type": "thinking", "thinking": text})
                events.append(_update("thinking_start"))
                events.append(_update("thinking_delta", delta=text))
            elif kind == "tool_use":
                call_id = str(block.get("id") or "")
                name = pi_tool_name(str(block.get("name") or ""))
                args = pi_tool_args(name, block.get("input"))
                self._tool_names[call_id] = name
                group["content"].append({"type": "toolCall", "id": call_id, "name": name, "arguments": args})
                events.append(
                    {"type": "tool_execution_start", "toolCallId": call_id, "toolName": name, "args": args}
                )
        return events

    def _close_group(self) -> list[dict]:
        group, self._group = self._group, None
        if group is None:
            return []
        content = group["content"]
        for item in content:
            if item["type"] == "text" and item["text"].strip():
                self._last_text = item["text"]
        if any(i["type"] == "toolCall" for i in content):
            stop = "toolUse"
        else:
            stop = _STOP_REASONS.get(group["stop_reason"] or "", "stop")
        message = {
            "role": "assistant",
            "content": content,
            "usage": pi_usage(group["usage"]),
            "provider": "anthropic",
            "model": group["model"] or self._model or self._model_ref,
            "stopReason": stop,
        }
        return [{"type": "message_end", "message": message}]

    # -- user / result -----------------------------------------------------

    def _user(self, msg: dict) -> list[dict]:
        events = self._close_group()
        content = msg.get("content")
        if not isinstance(content, list):
            return events
        for block in content:
            if not isinstance(block, dict) or _block_kind(block) != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            events.append(
                {
                    "type": "tool_execution_end",
                    "toolCallId": call_id,
                    "toolName": self._tool_names.get(call_id, ""),
                    "isError": bool(block.get("is_error")),
                    "result": _tool_result_payload(block.get("content")),
                }
            )
        return events

    def _record_result(self, msg: dict) -> list[dict]:
        events = self._close_group()
        self._result = msg
        return events

    # -- synthesised messages ---------------------------------------------

    def _trailing(self, text: str) -> list[dict]:
        model = self._model or self._model_ref
        return [
            {"type": "message_start", "message": {"role": "assistant"}},
            _update("text_delta", delta=text),
            _update("text_end", content=text),
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": pi_usage(None),
                    "provider": "anthropic",
                    "model": model,
                    "stopReason": "stop",
                },
            },
        ]

    def _error_event(self, error: dict) -> dict:
        status = error.get("status")
        etype = error.get("type")
        text = error.get("message")
        head = " ".join(str(p) for p in (status, etype) if p not in (None, ""))
        body = str(text) if text not in (None, "") else ""
        if head and body:
            message = f"{head}: {body}"
        else:
            message = head or body or "unknown agent error"
        hints = {k: v for k, v in (("httpStatus", status), ("errorType", etype)) if v not in (None, "")}
        event: dict = {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "usage": pi_usage(None),
                "provider": "anthropic",
                "model": self._model or self._model_ref,
                "stopReason": "error",
                "errorMessage": message,
            },
        }
        if hints:
            event["ralphd"] = hints
        return event


def _update(kind: str, **fields: Any) -> dict:
    return {"type": "message_update", "assistantMessageEvent": {"type": kind, **fields}}
