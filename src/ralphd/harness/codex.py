"""The codex harness: SDK-free helpers and the `python -m ralphd.harness.codex` wrapper.

This module never imports `claude_agent_sdk` or `ralphd.harness.claude_code`
(which imports the SDK at its top): the engine and the `codex` wrapper reach
it without the Claude SDK installed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .codex_normalise import (
    CODEX_RAW_ENVELOPE_TYPE, CODEX_STDERR_ENVELOPE_TYPE, CodexNormaliser,
)
from ..cli.versions import compare_versions
from ..engine.config import CODEX_MIN_VERSION
from ..engine.redact import MIN_SECRET_LEN, mask_values
from .skills_index import skills_index

# A hat's thinking/effort level (pi's vocabulary) to the `model_reasoning_effort`
# value sent to `codex`. Only `low`, `medium` and `high` are ever sent: they
# are the only values the gateway accepts, so `off` and `minimal` map to
# `low` and `xhigh` maps to `high`.
CODEX_EFFORT_TABLE: dict[str, str] = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
}

# What an unset or unmapped level becomes.
DEFAULT_CODEX_EFFORT = "medium"


def effort_options(thinking: str | None, effort: str | None) -> dict[str, Any]:
    """The `codex` config for a bundle's level ("thinking wins", as for pi
    and claude-code): `{"model_reasoning_effort": <low|medium|high>}`. A
    missing or unmapped level falls back to `medium`."""
    level = thinking or effort
    return {"model_reasoning_effort":
            CODEX_EFFORT_TABLE.get(level or "", DEFAULT_CODEX_EFFORT)}


# -- the wrapper ---------------------------------------------------------------
#
# `CodexRunner` (engine/runner.py) spawns this module as
# `python -m ralphd.harness.codex --config <scratch>/wrapper.json` through the
# shared child-process path: own session, agent uid, the prompt on stdin, NDJSON
# on stdout. For each call it
#
# 1. never writes `stderr.log` itself: its own Python-level stderr
#    (`sys.stderr`: a traceback, a warning) and `codex`'s stderr both leave as
#    `ralphd.codex_stderr` envelopes on stdout, and anything written below
#    Python straight to fd 2 reaches the runner on the same merged pipe as a
#    non-JSON line; `CodexRunner` is the log's only writer and passes every
#    line through `scrub_text` first,
# 2. writes exactly one file into the scratch `CODEX_HOME` the runner made,
#    `config.toml` (never an `AGENTS.md`), from `build_config`,
# 3. runs `<cli> exec --json --skip-git-repo-check --ephemeral -C <workspace>
#    --dangerously-bypass-approvals-and-sandbox -` in its own process group
#    with the prompt (the skills index first) on stdin, and
# 4. prints each Codex event as a raw envelope
#    (`{"type": "ralphd.codex_raw", "raw": <event>}`) followed by the pi events
#    `CodexNormaliser.feed` makes of it.
#
# The bearer token reaches `codex` only through the environment variable the
# config names as `env_key`; no file and no argument carries its value. It
# prints nothing before `codex` produces something to forward.

CLI_PATH_ENV = "RALPHD_CODEX_CLI_PATH"
BASE_URL_ENV = "AIGW_OPENAI_BASE_URL"
TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"
PROVIDER_ID = "aigw"
GATEWAY_NOT_CONFIGURED = "codex_gateway_not_configured"
VERSION_TOO_OLD = "codex_version_too_old"
CLI_NOT_FOUND = "codex_cli_not_found"
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")

# Codex's own retry loops are kept small so the engine's fault ladder owns
# retrying (the default is about thirty requests).
MAX_RETRIES = 2

# Seconds between the abort signal and the forced exit.
INTERRUPT_GRACE_S = 5.0


def codex_model(ref: str | None) -> str:
    """`ref` with its provider prefix removed and the rest verbatim:
    `aigw-openai/openai.gpt-6.1-sol` -> `openai.gpt-6.1-sol`."""
    ref = ref or ""
    return ref.split("/", 1)[1] if "/" in ref else ref


def _toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def _toml_key(key: str) -> str:
    return key if key and all(c.isalnum() or c in "-_" for c in key) else _toml_str(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return _toml_str(str(value))


def _toml_table(path: list[str], table: dict[str, Any]) -> list[str]:
    """`table` as a TOML table: its scalars first, then each nested dict as a
    sub-table."""
    lines = [f"[{'.'.join(_toml_key(p) for p in path)}]"]
    lines += [f"{_toml_key(k)} = {_toml_value(v)}"
              for k, v in table.items() if not isinstance(v, dict)]
    for k, v in table.items():
        if isinstance(v, dict):
            lines += [""] + _toml_table(path + [k], v)
    return lines


MCP_SERVER_NAME = "ralphd"
MCP_MODULE = "ralphd.harness.codex_mcp"
# Longer than `hat_tools.CLIENT_TIMEOUT_S` (120 s), so the relay's own timeout
# answer reaches the model before codex gives up on the call.
MCP_TOOL_TIMEOUT_S = 150


def mcp_servers(cfg: dict) -> dict[str, Any]:
    """The `mcp_servers` table for the hat's tools: one `ralphd` server, the
    stdio relay `ralphd.harness.codex_mcp`, or nothing when the hat has no
    tools (or no run root to relay through)."""
    names = [str(n) for n in (cfg.get("hat_tools") or ())]
    root = cfg.get("run_root")
    if not names or not root:
        return {}
    env = {"RALPHD_HAT_TOOLS_RUN": str(root)}
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return {MCP_SERVER_NAME: {
        "command": sys.executable,
        "args": ["-m", MCP_MODULE, "--tools", ",".join(names)],
        "tool_timeout_sec": MCP_TOOL_TIMEOUT_S,
        "env": env,
    }}


def hat_tool_prompt(names: list[str]) -> str:
    """The prompt line naming the hat's tools: `ralphd` namespace tools."""
    if not names:
        return ""
    return ("ralphd's tools for this hat (" + ", ".join(names) + ") are `ralphd` "
            "namespace tools (the MCP server `ralphd`); the instructions name "
            "them without the namespace.")


def build_config(cfg: dict, base_url: str) -> str:
    """The `config.toml` text for one call. `cfg` is the wrapper config
    (`model`, `thinking`, `effort`, `headers`). Holds no secret: the token is
    named by `env_key` and each header value by an `env_http_headers` entry;
    both travel in the environment."""
    top: dict[str, Any] = {
        "model": codex_model(cfg.get("model")),
        "model_provider": PROVIDER_ID,
        "service_tier": "auto",
        **effort_options(cfg.get("thinking"), cfg.get("effort")),
        "web_search": "disabled",
        "project_doc_max_bytes": 0,
    }
    provider: dict[str, Any] = {
        "name": "AIGW",
        "base_url": base_url,
        "env_key": TOKEN_ENV,
        "wire_api": "responses",
        "request_max_retries": MAX_RETRIES,
        "stream_max_retries": MAX_RETRIES,
    }
    # `headers` maps a header NAME to the environment variable that carries
    # its value (`header_env`); the value itself never enters this file. A
    # header may come from a `${env:}`/`${file:}`/`${cmd:}` reference, so none
    # is written as a literal `http_headers` entry.
    headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
    if headers:
        provider["env_http_headers"] = headers
    lines = [f"{k} = {_toml_value(v)}" for k, v in top.items()]
    for path, table in (
        (["model_providers", PROVIDER_ID], provider),
        *((["mcp_servers", n], t) for n, t in mcp_servers(cfg).items()),
        (["skills"], {"include_instructions": False, "bundled": {"enabled": False}}),
        (["features"], {"multi_agent": False, "goals": False, "view_image": False}),
    ):
        lines += [""] + _toml_table(path, table)
    return "\n".join(lines) + "\n"


def resolve_cli_path() -> str:
    return os.environ.get(CLI_PATH_ENV) or "codex"


def cli_version(cli: str) -> str | None:
    """`<cli> --version`'s dotted version (`codex-cli 0.156.1` -> `0.156.1`),
    or None when the CLI cannot be run or prints no version."""
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True,
                             timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search(out.stdout or "")
    return m.group(0) if m else None


def version_gate(cli: str, version: str | None) -> dict | None:
    """None when `version` meets `CODEX_MIN_VERSION`, else the in-band error.
    The comparison is `ralphd.cli.versions.compare_versions`, the helper the
    claude-code gate shares; a version it cannot parse is refused."""
    if version is None:
        if not (os.path.isfile(cli) or shutil.which(cli)):
            return {"type": CLI_NOT_FOUND,
                    "message": f"Codex CLI not found at {cli}"}
        return {"type": VERSION_TOO_OLD,
                "message": f"could not read the Codex version from {cli} --version; "
                           f"ralphd needs {CODEX_MIN_VERSION} or later"}
    if compare_versions(version, CODEX_MIN_VERSION) in (-1, None):
        return {"type": VERSION_TOO_OLD,
                "message": f"Codex {version} at {cli} is older than "
                           f"{CODEX_MIN_VERSION}, the minimum ralphd runs"}
    return None


def build_argv(cli: str, workspace: str) -> list[str]:
    """The `codex` command line. The prompt is never an argument: the final `-`
    makes it read the prompt from stdin."""
    return [cli, "exec", "--json", "--skip-git-repo-check", "--ephemeral",
            "-C", workspace, "--dangerously-bypass-approvals-and-sandbox", "-"]


# Environment the `codex` child must not inherit: the OpenAI credentials and
# routing (`codex` would prefer them to the gateway), every `CODEX_*` and
# `OPENAI_*` variable (the wrapper sets `CODEX_HOME` itself after the scrub),
# and the claude-code gateway routing, mirrored from the claude-code wrapper's
# list as data because that module imports the Claude SDK. The bearer token
# `AWS_BEARER_TOKEN_BEDROCK` and `AIGW_OPENAI_BASE_URL` are kept: they are the
# gateway.
STRIPPED_ENV = frozenset({
    "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_EFFORT",
    "CLAUDE_PID", "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL",
})
STRIPPED_ENV_PREFIXES = ("CODEX_", "OPENAI_", "CLAUDE_CODE_",
                         "ANTHROPIC_BEDROCK_", "ANTHROPIC_VERTEX_")
KEPT_ENV = frozenset({TOKEN_ENV, BASE_URL_ENV})


def scrub_environ(env: Any) -> list[str]:
    """Remove from `env` (in place) what the `codex` child must not inherit.
    Returns the removed names, sorted."""
    removed = []
    for name in list(env):
        if name in KEPT_ENV:
            continue
        if name in STRIPPED_ENV or name.startswith(STRIPPED_ENV_PREFIXES):
            del env[name]
            removed.append(name)
    return sorted(removed)


HEADER_ENV_PREFIX = "RALPHD_CODEX_HEADER_"


def header_env(headers: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Split header NAME -> VALUE into (NAME -> env var name, env var name ->
    VALUE). The first goes into `wrapper.json` and `config.toml`, the second
    into the wrapper's environment only."""
    names: dict[str, str] = {}
    values: dict[str, str] = {}
    for i, (name, value) in enumerate(headers.items()):
        var = f"{HEADER_ENV_PREFIX}{i}"
        names[str(name)] = var
        values[var] = str(value)
    return names, values


# Values (beyond the bearer token) the wrapper masks in everything it emits:
# the profile-header values the wrapper read from its environment.
_extra_masks: dict[str, str] = {}


def _load_header_masks(cfg: dict) -> None:
    _extra_masks.clear()
    for name, var in (cfg.get("headers") or {}).items():
        value = os.environ.get(str(var))
        if value and len(value) >= MIN_SECRET_LEN:
            _extra_masks[value] = f"[REDACTED:codex-header:{name}]"


def mask_all(text: str, token: str | None) -> str:
    """The bearer token (as `mask_token` labels it) and every profile-header
    value in `_extra_masks`, masked through the engine's one routine,
    `redact.mask_values`: however JSON escaping spells a value, at any nesting
    depth, it cannot be recovered by decoding what the wrapper prints."""
    values = dict(_extra_masks)
    if token and len(token) >= MIN_SECRET_LEN:
        values[token] = f"[REDACTED:env:{TOKEN_ENV}]"
    return mask_values(text, values)


def mask_token(text: str, token: str | None) -> str:
    """`text` with the bearer token's value (in every JSON spelling, through
    `redact.mask_values`) replaced by the label the engine's own `scrub_text`
    uses for it. A no-op for an empty or short token (the engine's
    `MIN_SECRET_LEN`)."""
    if token and len(token) >= MIN_SECRET_LEN:
        return mask_values(text, {token: f"[REDACTED:env:{TOKEN_ENV}]"})
    return text


def gateway_missing(env: Any) -> str | None:
    """The name of the first gateway variable `env` lacks, else None."""
    for name in (BASE_URL_ENV, TOKEN_ENV):
        if not env.get(name):
            return name
    return None


# The terminal `message_end` of an interrupted iteration.
ABORTED = {"message": "aborted", "stopReason": "aborted"}


class _Out:
    """NDJSON writer for stdout: one object per line, flushed per line."""

    def __init__(self) -> None:
        self._buf = sys.stdout.buffer
        self._lock = threading.Lock()  # the stderr pump emits from a thread

    def emit(self, obj: dict) -> None:
        data = mask_all(json.dumps(obj, default=str, ensure_ascii=False),
                        os.environ.get(TOKEN_ENV)) + "\n"
        with self._lock:
            self._buf.write(data.encode("utf-8", errors="replace"))
            self._buf.flush()


class _StderrEnvelopes(io.TextIOBase):
    """`sys.stderr` for the wrapper: each complete line leaves as a
    `ralphd.codex_stderr` envelope through `_Out` (under its lock, so it never
    splits a stdout line), and `flush` sends a trailing partial line. The
    wrapper never opens `stderr.log`; the runner writes each envelope's line
    there through `scrub_text`."""

    def __init__(self, out: "_Out") -> None:
        super().__init__()
        self._out = out
        self._buf = ""

    @property
    def encoding(self) -> str:  # traceback/warnings consult it
        return "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text) -> int:
        text = str(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line + "\n")
        return len(text)

    def flush(self) -> None:
        if self._buf:
            line, self._buf = self._buf, ""
            self._emit(line)

    def _emit(self, line: str) -> None:
        try:
            self._out.emit({"type": CODEX_STDERR_ENVELOPE_TYPE, "line": line})
        except (OSError, ValueError):
            pass


def _capture_stderr(out: "_Out") -> None:
    """Route the wrapper's own Python-level stderr into envelopes."""
    sys.stderr = _StderrEnvelopes(out)


def _pump_stderr(src, token: str | None, last: list | None = None,
                 out: "_Out | None" = None) -> None:
    """Hand `codex`'s stderr on, masking the bearer token on the way. With
    `out`, each line leaves as a stderr envelope on stdout, and the runner
    writes it to `stderr.log` through the engine's `scrub_text` (the only
    place the live redaction map exists). Without `out` it is copied to this
    process's fd 2, which the runner reads on the same merged pipe and
    scrubs the same way. The last non-blank (masked) line lands in `last[0]`
    when a list is given."""
    try:
        for chunk in iter(src.readline, b""):
            text = mask_all(chunk.decode("utf-8", errors="replace"), token)
            if last is not None and text.strip():
                last[:] = [text.strip()]
            if out is not None:
                out.emit({"type": CODEX_STDERR_ENVELOPE_TYPE, "line": text})
            else:
                os.write(2, text.encode("utf-8", errors="replace"))
    except (OSError, ValueError):
        pass


def _finish(norm: CodexNormaliser, out: _Out, error: dict | None) -> None:
    for ev in norm.finish(error):
        out.emit(ev)


def _run(cfg: dict, prompt: str, norm: CodexNormaliser, out: _Out) -> int:
    scrub_environ(os.environ)
    missing = gateway_missing(os.environ)
    if missing:
        _finish(norm, out, {"type": GATEWAY_NOT_CONFIGURED,
                            "message": f"{missing} is not set; the codex harness "
                                       "needs an OpenAI-gateway LLM profile"})
        return 1
    cli = resolve_cli_path()
    refusal = version_gate(cli, cli_version(cli))
    if refusal:
        _finish(norm, out, refusal)
        return 1
    _load_header_masks(cfg)
    home = Path(cfg["codex_home"])
    (home / "config.toml").write_text(build_config(cfg, os.environ[BASE_URL_ENV]))
    os.environ["CODEX_HOME"] = str(home)
    index = skills_index(list(cfg.get("skills") or []))
    if index:
        prompt = f"{index}\n\n{prompt}"
    if mcp_servers(cfg):
        prompt = f"{hat_tool_prompt(list(cfg['hat_tools']))}\n\n{prompt}"
    argv = build_argv(cli, cfg["workspace"])
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cfg["workspace"], env=os.environ.copy(),
                                start_new_session=True)
    except OSError as e:
        _finish(norm, out, {"type": CLI_NOT_FOUND, "message": str(e)})
        return 1

    last_stderr: list[str] = []
    pump = threading.Thread(target=_pump_stderr,
                            args=(proc.stderr, os.environ.get(TOKEN_ENV), last_stderr, out),
                            daemon=True)
    pump.start()
    state = {"interrupted": False}

    def _abort_group(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _on_sigint(_signum, _frame) -> None:
        if state["interrupted"]:
            return
        state["interrupted"] = True
        _abort_group(signal.SIGINT)
        signal.signal(signal.SIGALRM, lambda *_: (_abort_group(signal.SIGKILL), _force()))
        signal.setitimer(signal.ITIMER_REAL, INTERRUPT_GRACE_S)

    def _force() -> None:
        _finish(norm, out, ABORTED)
        os._exit(130)

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    completed = False
    failed = False
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        out.emit({"type": CODEX_RAW_ENVELOPE_TYPE, "raw": event})
        completed = completed or event.get("type") == "turn.completed"
        failed = failed or event.get("type") == "turn.failed"
        for ev in norm.feed(event):
            out.emit(ev)
    code = proc.wait()
    pump.join(timeout=2.0)
    if state["interrupted"]:
        _abort_group(signal.SIGKILL)
        _finish(norm, out, ABORTED)
        return 130
    if completed and code == 0:
        _finish(norm, out, None)
        return 0
    if failed:
        _finish(norm, out, None)
        return 1
    if completed:
        # a turn.completed followed by a non-zero exit is not a clean iteration
        message = f"codex exited {code} after turn.completed"
    elif code == 0:
        # what SIGTERM produces: exit 0 and no terminal event
        message = "codex exited 0 without a turn.completed event"
    else:
        message = f"codex exited {code} without a turn.failed event"
    if code != 0 and last_stderr:
        message = f"{message}: {last_stderr[0]}"
    _finish(norm, out, {"message": message})
    return 1


def probe() -> dict:
    """`--probe-version`: the Codex CLI's version and path, whether that CLI
    meets `CODEX_MIN_VERSION`, and the gate's message when it does not."""
    cli = resolve_cli_path()
    version = cli_version(cli)
    gate = version_gate(cli, version)
    doc = {"cli": version, "cliPath": cli, "ok": gate is None}
    if gate is not None:
        doc["error"] = gate["message"]
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ralphd.harness.codex")
    ap.add_argument("--config", help="path of the wrapper.json the runner wrote")
    ap.add_argument("--probe-version", action="store_true",
                    help="print the Codex CLI version as JSON and exit")
    args = ap.parse_args(argv)
    if args.probe_version:
        print(json.dumps(probe()))
        return 0
    if not args.config:
        ap.error("--config is required")
    out = _Out()
    _capture_stderr(out)
    cfg = json.loads(Path(args.config).read_text())
    prompt = sys.stdin.read()
    norm = CodexNormaliser(model_ref=cfg.get("model") or "")
    return _run(cfg, prompt, norm, out)


if __name__ == "__main__":
    sys.exit(main())
