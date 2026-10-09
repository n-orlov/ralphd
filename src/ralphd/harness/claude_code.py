"""The `claude-code` harness wrapper: one iteration on the Claude Agent SDK.

`ClaudeCodeRunner` (engine/runner.py) spawns this module as
`python -m ralphd.harness.claude_code --config <wrapper.json>` through the
same child-process path as pi: own session, agent uid, prompt on stdin. This
is the only module in ralphd that imports `claude_agent_sdk`; the engine
process never does.

What it does, in order:

1. Redirects its own stderr to the config's `stderr_log`, so stdout stays
   NDJSON-only.
2. Scrubs its own `os.environ` (the SDK hands the CLI child the whole of it
   and `options.env` can only add): `ANTHROPIC_API_KEY`,
   `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_BEDROCK_*`,
   `ANTHROPIC_VERTEX_*`, `AWS_BEARER_TOKEN_BEDROCK`, `CLAUDECODE`,
   `CLAUDE_EFFORT`, `CLAUDE_PID`, `CLAUDE_CONFIG_DIR` and every `CLAUDE_CODE_*` except
   `CLAUDE_CODE_OAUTH_TOKEN` are removed. The SDK itself then sets
   `CLAUDE_CODE_ENTRYPOINT=sdk-py`, `CLAUDE_AGENT_SDK_VERSION` and
   `CLAUDE_CODE_SDK_READS_SESSION_STATE`.
3. Runs `<cli> -v` and refuses to go on below
   `config.CLAUDE_CODE_MIN_VERSION` (an in-band `claude_code_version_too_old`
   error, exit 1). The CLI is `$RALPHD_CLAUDE_CLI_PATH`, else the SDK's
   bundled binary.
4. Calls `query()` with an isolated Claude Code: a fresh `CLAUDE_CONFIG_DIR`
   from the scratch area (`HOME` is inherited: the agent's real home, as
   for pi), `setting_sources=[]`, no persisted
   session, `skills=[]` (the CLI's bundled skills off), `bypassPermissions`,
   cwd = the workspace, exactly the six built-in tools in `BUILTIN_TOOLS`, no
   `max_turns`, and the effort row of `EFFORT_TABLE`.
5. For every SDK message prints one raw envelope
   (`{"type": "ralphd.claude_raw", "raw": {"kind": ..., ...}}`) and then the
   pi events `Normaliser.feed` makes of it. SDK errors end the stream with an
   in-band error `message_end` (`Normaliser.finish`).

It prints nothing before the first SDK message, so the engine's startup
watchdog sees no traffic until Claude Code produces some. On SIGINT it
cancels the query, ends the stream with an `aborted` error and exits 130.
The engine scrubs every line it writes; this process holds no secret map.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    ProcessError,
    ResultError,
    create_sdk_mcp_server,
    query,
    tool as sdk_tool,
)

from ..cli.versions import compare_versions
from ..engine import hat_tools as hat_tools_mod
from ..engine.config import CLAUDE_CODE_MIN_VERSION
from .normalise import RAW_ENVELOPE_TYPE, Normaliser
from .skills_index import skills_index  # noqa: F401  (re-exported)

# The pi-parity built-in tool set (PRD B.3). Passed as the SDK's `tools`
# list, so the CLI loads nothing else; ralphd's hat tools are added as an
# SDK MCP server, never through this list.
BUILTIN_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Bash", "Grep", "Glob")

# A hat's thinking/effort level (pi's vocabulary) to the nearest
# `ClaudeAgentOptions` keywords the installed SDK accepts. Its `effort` takes
# only low|medium|high|xhigh|max, so `minimal` maps to `low`; `off` turns
# thinking off and sets no effort. A level not in the table sets nothing.
EFFORT_TABLE: dict[str, dict[str, Any]] = {
    "off": {"thinking": {"type": "disabled"}},
    "minimal": {"effort": "low"},
    "low": {"effort": "low"},
    "medium": {"effort": "medium"},
    "high": {"effort": "high"},
    "xhigh": {"effort": "xhigh"},
}

CLI_PATH_ENV = "RALPHD_CLAUDE_CLI_PATH"
KEPT_CLAUDE_CODE_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
STRIPPED_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_EFFORT",
    "CLAUDE_PID", "CLAUDE_CONFIG_DIR",
    # Gateway / cloud-provider routing: a claude-code hat always talks to
    # api.anthropic.com with CLAUDE_CODE_OAUTH_TOKEN, however the profile or
    # forwarded env (which pi hats keep) is set up. CLAUDE_CODE_USE_BEDROCK /
    # CLAUDE_CODE_USE_VERTEX are covered by the CLAUDE_CODE_* rule.
    "ANTHROPIC_BASE_URL", "AWS_BEARER_TOKEN_BEDROCK",
})
STRIPPED_ENV_PREFIXES = ("ANTHROPIC_BEDROCK_", "ANTHROPIC_VERTEX_")

VERSION_TOO_OLD = "claude_code_version_too_old"
CLI_NOT_FOUND = "claude_code_cli_not_found"

# The skills index goes in the system prompt. Flip to False to send an empty
# system prompt and put the index at the head of the user prompt instead.
SKILLS_INDEX_IN_SYSTEM_PROMPT = True

# Seconds between SIGINT and a forced exit when the SDK's own shutdown of
# the CLI child does not finish.
INTERRUPT_GRACE_S = 5.0

_NORMALISER_KINDS = {
    "AssistantMessage": "assistant",
    "UserMessage": "user",
    "ResultMessage": "result",
    "RateLimitEvent": "rate_limit",
}

_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)")


# -- pure helpers ------------------------------------------------------------

def effort_options(thinking: str | None, effort: str | None) -> dict[str, Any]:
    """The `ClaudeAgentOptions` keywords for a bundle's level ("thinking
    wins", as for pi)."""
    level = thinking or effort
    if not level:
        return {}
    return dict(EFFORT_TABLE.get(level, {}))


def scrub_environ(env: Any) -> list[str]:
    """Remove from `env` (in place) what the CLI child must not inherit.
    Returns the removed names, sorted."""
    removed = []
    for name in list(env):
        if (name in STRIPPED_ENV or name.startswith(STRIPPED_ENV_PREFIXES)
                or (name.startswith("CLAUDE_CODE_") and name != KEPT_CLAUDE_CODE_ENV)):
            del env[name]
            removed.append(name)
    return sorted(removed)


def read_version(text: str | None) -> str | None:
    """The leading `x.y.z` of `<cli> -v`'s output (`2.1.280 (Claude Code)` ->
    `2.1.280`), or None. Extraction only: comparison is
    `ralphd.cli.versions.compare_versions`."""
    m = _VERSION_RE.match(text or "")
    return f"{int(m[1])}.{int(m[2])}.{int(m[3])}" if m else None


def bundled_cli_path() -> str:
    return str(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")


def resolve_cli_path() -> str:
    return os.environ.get(CLI_PATH_ENV) or bundled_cli_path()


def cli_version(cli: str) -> str | None:
    """`<cli> -v`'s `x.y.z`, or None when the CLI cannot be run or prints
    no version."""
    try:
        out = subprocess.run([cli, "-v"], capture_output=True, text=True,
                             timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return read_version(out.stdout)


def version_gate(cli: str, version: str | None) -> dict | None:
    """None when `version` meets the minimum, else the in-band error. The
    comparison is `ralphd.cli.versions.compare_versions`, the helper the
    `codex` gate shares; a version it cannot parse is refused."""
    if version is None:
        if not os.path.exists(cli):
            return {"type": CLI_NOT_FOUND,
                    "message": f"Claude Code CLI not found at {cli}"}
        return {"type": VERSION_TOO_OLD,
                "message": f"could not read the Claude Code version from {cli} -v; "
                           f"ralphd needs {CLAUDE_CODE_MIN_VERSION} or later"}
    if compare_versions(version, CLAUDE_CODE_MIN_VERSION) in (-1, None):
        return {"type": VERSION_TOO_OLD,
                "message": f"Claude Code {version} at {cli} is older than "
                           f"{CLAUDE_CODE_MIN_VERSION}, the minimum ralphd runs"}
    return None


def probe() -> dict:
    """`--probe-version`: the SDK version, the CLI's version and path, and
    whether that CLI meets the minimum."""
    cli = resolve_cli_path()
    version = cli_version(cli)
    return {"sdk": claude_agent_sdk.__version__, "cli": version, "cliPath": cli,
            "ok": version_gate(cli, version) is None}


def system_prompt(skills: list[dict], hat_tools: list[str]) -> str:
    """ralphd's system prompt for the CLI: the hat-tool naming line and the
    skills index, each only when there is something to say."""
    parts = []
    if hat_tools:
        names = ", ".join(f"mcp__ralphd__{n}" for n in hat_tools)
        parts.append("ralphd's tools for this hat appear as mcp__ralphd__<name> "
                     f"({names}); the instructions name them without the prefix.")
    if SKILLS_INDEX_IN_SYSTEM_PROMPT:
        index = skills_index(skills)
        if index:
            parts.append(index)
    return "\n\n".join(parts)


MCP_SERVER_NAME = "ralphd"


def hat_tool_server(names: list[str], run_root: str):
    """The in-process MCP server carrying ralphd's hat tools for this hat.
    Each handler relays through the same request/response spool pi's
    extension uses (`hat_tools.call`, run in a thread because it blocks) and
    returns the response JSON as one text block, never flagged as an error,
    exactly what pi's extension returns."""
    tools = []
    for spec in hat_tools_mod.sdk_tool_specs(names):
        def make(tool_name: str):
            async def handler(args: dict) -> dict:
                resp = await asyncio.to_thread(
                    hat_tools_mod.call, Path(run_root), tool_name, dict(args or {}))
                return {"content": [{"type": "text", "text": json.dumps(resp)}]}
            return handler
        tools.append(sdk_tool(spec["name"], spec["description"],
                              spec["input_schema"])(make(spec["name"])))
    return create_sdk_mcp_server(name=MCP_SERVER_NAME, tools=tools)


def build_options(cfg: dict, cli: str, stderr_cb) -> ClaudeAgentOptions:
    hat_tools = list(cfg.get("hat_tools") or [])
    mcp_servers = ({MCP_SERVER_NAME: hat_tool_server(hat_tools, cfg["run_root"])}
                   if hat_tools and cfg.get("run_root") else {})
    sp = system_prompt(list(cfg.get("skills") or []), hat_tools)
    return ClaudeAgentOptions(
        cli_path=cli,
        tools=list(BUILTIN_TOOLS),
        permission_mode="bypassPermissions",
        setting_sources=[],
        # The SDK's documented "no skills" filter (initialize `skills: []`):
        # the CLI's bundled skills never reach the model, now or if the Skill
        # tool is ever added. ralphd's own skills ride the system prompt index.
        skills=[],
        cwd=cfg["workspace"],
        model=cfg.get("model") or None,
        extra_args={"no-session-persistence": None},
        system_prompt=sp or None,
        mcp_servers=mcp_servers,
        max_turns=None,
        stderr=stderr_cb,
        env={"CLAUDE_CONFIG_DIR": cfg["config_dir"]},
        **effort_options(cfg.get("thinking"), cfg.get("effort")),
    )


def raw_envelope(msg: Any) -> dict:
    """One SDK message as the raw envelope the engine writes, unwrapped, to
    `output.claude.jsonl`. A `RateLimitEvent` carries the CLI's own
    `rate_limit_info` dict (with `unifiedWindows`), not the typed subset."""
    kind = type(msg).__name__
    body = dataclasses.asdict(msg) if dataclasses.is_dataclass(msg) else {"repr": repr(msg)}
    if kind == "RateLimitEvent":
        info_raw = getattr(msg.rate_limit_info, "raw", None)
        if info_raw:
            body["rate_limit_info"] = dict(info_raw)
    return {"type": RAW_ENVELOPE_TYPE, "raw": {"kind": kind, **body}}


def error_from_result(result: dict, assistant_error: str | None) -> dict:
    return {
        "status": result.get("api_error_status"),
        "type": assistant_error or result.get("terminal_reason") or result.get("subtype"),
        "message": result.get("result") or "; ".join(map(str, result.get("errors") or [])),
    }


# -- I/O ---------------------------------------------------------------------

class _Out:
    """NDJSON writer for stdout: one object per line, flushed per line."""

    def __init__(self) -> None:
        self._buf = sys.stdout.buffer
        self.finished = False

    def emit(self, obj: dict) -> None:
        data = json.dumps(obj, default=str, ensure_ascii=False) + "\n"
        self._buf.write(data.encode("utf-8", errors="replace"))
        self._buf.flush()

    def finish(self, norm: Normaliser, error: dict | None) -> None:
        if self.finished:
            return
        self.finished = True
        for ev in norm.finish(error):
            self.emit(ev)


def _redirect_stderr(path: str | None) -> None:
    if not path:
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return
    os.dup2(fd, 2)
    os.close(fd)


def _log(text: str) -> None:
    try:
        sys.stderr.write(text.rstrip("\n") + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


async def _drive(prompt: str, options: ClaudeAgentOptions, norm: Normaliser,
                 out: _Out) -> dict | None:
    """Stream one query. Returns the in-band error to end with, or None."""
    assistant_error: str | None = None
    last_result: dict | None = None
    error: dict | None = None
    try:
        async for msg in query(prompt=prompt, options=options):
            envelope = raw_envelope(msg)
            out.emit(envelope)
            raw = envelope["raw"]
            kind = raw["kind"]
            if kind == "AssistantMessage" and raw.get("error"):
                assistant_error = str(raw["error"])
            nkind = _NORMALISER_KINDS.get(kind)
            if nkind is None:
                continue
            body = {k: v for k, v in raw.items() if k != "kind"}
            if kind == "ResultMessage":
                last_result = body
            for ev in norm.feed(nkind, body):
                out.emit(ev)
    except ResultError as e:
        error = error_from_result(e.data or {}, assistant_error)
        if not error["message"]:
            error["message"] = str(e)
        if error["status"] is None:
            error["status"] = e.api_error_status
    except CLINotFoundError as e:
        error = {"type": CLI_NOT_FOUND, "message": str(e)}
    except ProcessError as e:
        error = {"type": "process_error", "message": str(e)}
    except ClaudeSDKError as e:
        error = {"type": type(e).__name__, "message": str(e)}
    except Exception as e:  # every failure ends in-band
        error = {"type": type(e).__name__, "message": str(e) or repr(e)}
    if error is None and last_result is not None and last_result.get("is_error"):
        error = error_from_result(last_result, assistant_error)
    return error


async def _amain(prompt: str, options: ClaudeAgentOptions, norm: Normaliser,
                 out: _Out) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(_drive(prompt, options, norm, out))
    interrupted = False

    def _force_exit() -> None:
        out.finish(norm, {"message": "aborted"})
        os._exit(130)

    def _on_sigint() -> None:
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        task.cancel()
        loop.call_later(INTERRUPT_GRACE_S, _force_exit)

    loop.add_signal_handler(signal.SIGINT, _on_sigint)
    try:
        error = await task
    except asyncio.CancelledError:
        error = None
    if interrupted:
        out.finish(norm, {"message": "aborted"})
        return 130
    out.finish(norm, error)
    return 1 if error else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ralphd.harness.claude_code")
    ap.add_argument("--config", help="path of the wrapper.json the runner wrote")
    ap.add_argument("--probe-version", action="store_true",
                    help="print the SDK and CLI versions as JSON and exit")
    args = ap.parse_args(argv)
    if args.probe_version:
        print(json.dumps(probe()))
        return 0
    if not args.config:
        ap.error("--config is required")
    cfg = json.loads(Path(args.config).read_text())
    _redirect_stderr(cfg.get("stderr_log"))
    prompt = sys.stdin.read()
    removed = scrub_environ(os.environ)
    if removed:
        _log(f"ralphd: removed from the CLI env: {', '.join(removed)}")
    os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"
    if not SKILLS_INDEX_IN_SYSTEM_PROMPT:
        index = skills_index(list(cfg.get("skills") or []))
        if index:
            prompt = f"{index}\n\n{prompt}"
    norm = Normaliser(model_ref=cfg.get("model") or "")
    out = _Out()
    cli = resolve_cli_path()
    refusal = version_gate(cli, cli_version(cli))
    if refusal is not None:
        out.finish(norm, refusal)
        return 1
    options = build_options(cfg, cli, _log)
    return asyncio.run(_amain(prompt, options, norm, out))


if __name__ == "__main__":
    sys.exit(main())
