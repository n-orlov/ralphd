"""The codex harness: SDK-free helpers and the `python -m ralphd.harness.codex` wrapper.

This module never imports `claude_agent_sdk` or `ralphd.harness.claude_code`
(which imports the SDK at its top): the engine and the `codex` wrapper reach
it without the Claude SDK installed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .codex_normalise import CODEX_RAW_ENVELOPE_TYPE, CodexNormaliser
from ..engine.redact import MIN_SECRET_LEN
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
# 1. redirects its own stderr to the config's `stderr_log` (stdout stays
#    NDJSON-only),
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


def build_config(cfg: dict, base_url: str) -> str:
    """The `config.toml` text for one call. `cfg` is the wrapper config
    (`model`, `thinking`, `effort`, `headers`). Holds no secret: the token is
    named by `env_key` and travels in the environment."""
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
    headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
    if headers:
        provider["http_headers"] = headers
    lines = [f"{k} = {_toml_value(v)}" for k, v in top.items()]
    for path, table in (
        (["model_providers", PROVIDER_ID], provider),
        (["skills"], {"include_instructions": False, "bundled": {"enabled": False}}),
        (["features"], {"multi_agent": False, "goals": False, "view_image": False}),
    ):
        lines += [""] + _toml_table(path, table)
    return "\n".join(lines) + "\n"


def resolve_cli_path() -> str:
    return os.environ.get(CLI_PATH_ENV) or "codex"


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


def mask_token(text: str, token: str | None) -> str:
    """`text` with the bearer token's value replaced by the label the engine's
    own `scrub_text` uses for it. A no-op for an empty or short token (the
    engine's `MIN_SECRET_LEN`)."""
    if token and len(token) >= MIN_SECRET_LEN and token in text:
        return text.replace(token, f"[REDACTED:env:{TOKEN_ENV}]")
    return text


def gateway_missing(env: Any) -> str | None:
    """The name of the first gateway variable `env` lacks, else None."""
    for name in (BASE_URL_ENV, TOKEN_ENV):
        if not env.get(name):
            return name
    return None


class _Out:
    """NDJSON writer for stdout: one object per line, flushed per line."""

    def __init__(self) -> None:
        self._buf = sys.stdout.buffer

    def emit(self, obj: dict) -> None:
        data = mask_token(json.dumps(obj, default=str, ensure_ascii=False),
                          os.environ.get(TOKEN_ENV)) + "\n"
        self._buf.write(data.encode("utf-8", errors="replace"))
        self._buf.flush()


def _redirect_stderr(path: str | None) -> None:
    if not path:
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return
    os.dup2(fd, 2)
    os.close(fd)


def _pump_stderr(src, token: str | None) -> None:
    """Copy `codex`'s stderr to this process's stderr (the runner's
    `stderr.log`), masking the bearer token on the way."""
    try:
        for chunk in iter(src.readline, b""):
            text = mask_token(chunk.decode("utf-8", errors="replace"), token)
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
    home = Path(cfg["codex_home"])
    (home / "config.toml").write_text(build_config(cfg, os.environ[BASE_URL_ENV]))
    os.environ["CODEX_HOME"] = str(home)
    index = skills_index(list(cfg.get("skills") or []))
    if index:
        prompt = f"{index}\n\n{prompt}"
    argv = build_argv(resolve_cli_path(), cfg["workspace"])
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cfg["workspace"], env=os.environ.copy(),
                                start_new_session=True)
    except OSError as e:
        _finish(norm, out, {"type": "codex_cli_not_found", "message": str(e)})
        return 1

    pump = threading.Thread(target=_pump_stderr,
                            args=(proc.stderr, os.environ.get(TOKEN_ENV)),
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
        _finish(norm, out, {"message": "aborted"})
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
        _finish(norm, out, {"message": "aborted"})
        return 130
    if completed and code == 0:
        _finish(norm, out, None)
        return 0
    if failed:
        _finish(norm, out, None)
        return 1
    _finish(norm, out, {"message": (f"codex exited {code} without a turn.completed event"
                                    if code == 0 else f"codex exited {code}")})
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ralphd.harness.codex")
    ap.add_argument("--config", required=True,
                    help="path of the wrapper.json the runner wrote")
    args = ap.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text())
    _redirect_stderr(cfg.get("stderr_log"))
    prompt = sys.stdin.read()
    norm = CodexNormaliser(model_ref=cfg.get("model") or "")
    return _run(cfg, prompt, norm, _Out())


if __name__ == "__main__":
    sys.exit(main())
