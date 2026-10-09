"""`python -m ralphd.harness.codex_mcp`: ralphd's hat tools as a stdio MCP server.

`codex` starts this process as the `ralphd` MCP server (see `codex.build_config`)
and speaks newline-delimited JSON-RPC 2.0 to it. The server answers
`initialize`, `ping`, `tools/list` and `tools/call`. `tools/list` advertises
`hat_tools.sdk_tool_specs` for the hat's tools (the names come from `--tools`),
and `tools/call` relays through `hat_tools.call` -- the same request/response
spool pi's extension and the claude-code SDK server use, rooted at
`RALPHD_HAT_TOOLS_RUN` -- and returns the response JSON as one text content
block, never flagged as an error.

The module imports no `claude_agent_sdk` and no `ralphd.engine` module other
than `hat_tools`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..engine import hat_tools

SERVER_NAME = "ralphd"
PROTOCOL_VERSION = "2025-06-18"
RUN_ENV = hat_tools.RUN_DIR_ENV

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def tool_list(names: list[str]) -> list[dict]:
    """The MCP `tools/list` entries for `names`, in the order given."""
    return [{"name": s["name"], "description": s["description"],
             "inputSchema": s["input_schema"]}
            for s in hat_tools.sdk_tool_specs(names)]


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def call_tool(run_root: Path, tool: str, args: dict) -> dict:
    """One relayed call: the broker's response as a single text block."""
    resp = hat_tools.call(run_root, tool, dict(args or {}))
    return {"content": [{"type": "text", "text": json.dumps(resp)}]}


def handle(msg: Any, names: list[str], run_root: Path | None) -> dict | None:
    """The response to one decoded JSON-RPC message, or None for a notification."""
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None,
                      INVALID_REQUEST, "invalid request")
    method = msg["method"]
    if "id" not in msg:
        return None  # a notification (e.g. notifications/initialized)
    rid = msg["id"]
    params = msg.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        version = params.get("protocolVersion")
        return _result(rid, {
            "protocolVersion": version if isinstance(version, str) and version
            else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        })
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": tool_list(names)})
    if method == "tools/call":
        tool = params.get("name")
        if tool not in names:
            return _error(rid, INVALID_PARAMS, f"unknown tool: {tool}")
        if run_root is None:
            return _error(rid, INVALID_PARAMS, f"{RUN_ENV} is not set")
        args = params.get("arguments")
        return _result(rid, call_tool(run_root, tool,
                                      args if isinstance(args, dict) else {}))
    return _error(rid, METHOD_NOT_FOUND, f"method not found: {method}")


def serve(names: list[str], run_root: Path | None, stdin=None, stdout=None) -> int:
    """Read newline-delimited JSON-RPC from `stdin` until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: dict | None = _error(None, PARSE_ERROR, "parse error")
        else:
            reply = handle(msg, names, run_root)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ralphd.harness.codex_mcp")
    ap.add_argument("--tools", default="",
                    help="comma-separated hat tool names to serve")
    ns = ap.parse_args(argv)
    names = [n for n in ns.tools.split(",") if n]
    hat_tools.sdk_tool_specs(names)  # an unknown name fails at start-up
    root = os.environ.get(RUN_ENV)
    return serve(names, Path(root) if root else None)


if __name__ == "__main__":
    sys.exit(main())
