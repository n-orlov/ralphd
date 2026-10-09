"""`ralphd-engine --llm-ping`: one ping through the real runner path.

`ralphctl llm test` runs this inside a throwaway container from the run
image, as the agent uid, once per distinct (harness, model) pair. It builds
the runner an iteration of that harness uses (`PiRunner` or
`ClaudeCodeRunner`) over a scratch workspace, sends `PING_PROMPT`, and judges
the `IterationResult` the way the loop does: `faults.explain_fault` over the
same inputs `LoopSupervisor._classify_result` passes. It prints one JSON line:

    {"harness", "model", "ok", "faultClass", "reason", "message", "exitCode"}

`ok` means no error recorded, not timed out, exit code 0 and non-blank final
text. `message` is the scrubbed error text, at most `MESSAGE_LIMIT`
characters. The process exits 0 when `ok`, else 1; the caller reads the JSON
line, not the exit status.

This module never imports the Claude Agent SDK: `ClaudeCodeRunner` spawns the
wrapper module as a child process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_HARNESS, HARNESSES
from .faults import explain_fault
from .redact import refresh_redaction_map, scrub_text
from .runner import ClaudeCodeRunner, CodexRunner, IterationResult, PiRunner
from .state import billable_tokens

PING_PROMPT = "Reply with exactly the single word: ok"
DEFAULT_PING_TIMEOUT_S = 120
MESSAGE_LIMIT = 400
# The runner's own stderr, as much of it as a FAIL line can use: at most this
# many characters of the scrubbed tail (the rest of `MESSAGE_LIMIT` is the
# `the runner exited N:` lead-in).
STDERR_TAIL_LIMIT = 300
_TAIL_READ_BYTES = 16384


def build_runner(harness: str, workspace: Path):
    """The runner an iteration on `harness` uses, over `workspace`."""
    if harness == "claude-code":
        return ClaudeCodeRunner(workspace)
    if harness == "codex":
        return CodexRunner(workspace)
    return PiRunner(workspace)


def runner_stderr_tail(transcript: Path) -> str:
    """The scrubbed, bounded tail of what the runner's child wrote to stderr.

    The runner merges the child's stderr into its stdout and records every
    line that is not an agent event (pi NDJSON) in the transcript, so those
    lines, plus the claude-code wrapper's own `stderr.log` beside the
    transcript, are the child's stderr. Lines are whitespace-collapsed and
    joined with ` | `; the result is scrubbed (`redact.scrub_text`) and at
    most `STDERR_TAIL_LIMIT` characters, keeping the END. Empty when there is
    nothing, or nothing readable."""
    stderr_log = transcript.with_name(transcript.stem + ".claude-scratch") / "stderr.log"
    lines: list[str] = []
    for path in (transcript, stderr_log):
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - _TAIL_READ_BYTES))
                raw = fh.read().decode(errors="replace")
        except OSError:
            continue
        for line in raw.splitlines():
            line = " ".join(line.split())
            if line and not line.startswith("{"):
                lines.append(line)
    tail = scrub_text(" | ".join(lines))
    return tail[-STDERR_TAIL_LIMIT:].strip()


def judge(harness: str, model: str | None, result: IterationResult,
          stderr_tail: str = "") -> dict:
    """The one-line verdict for `result`, classified exactly as the loop
    classifies an iteration (no abort is ever recorded for a ping).

    `stderr_tail` is the runner's scrubbed stderr tail
    (`runner_stderr_tail`); it is appended to the `the runner exited N`
    message so a resolver or spawn error is visible in the FAIL line."""
    error = result.error_message or ""
    explained = explain_fault(
        error_text=error,
        exit_code=result.exit_code,
        interrupted=result.interrupted,
        timed_out=result.timed_out,
        no_traffic_timeout=result.no_traffic_timeout,
        produced_traffic=billable_tokens(result.usage) > 0,
        rate_limit=result.rate_limit,
        rate_limit_fresh=result.rate_limit_fresh,
        http_status=result.http_status,
        error_type=result.error_type,
    )
    text = (result.final_text or "").strip()
    ok = (not error.strip() and not result.timed_out
          and not result.no_traffic_timeout and result.exit_code == 0
          and bool(text))
    message = error
    if not ok and not message:
        if result.timed_out or result.no_traffic_timeout:
            message = "the ping timed out"
        elif result.exit_code not in (0, None):
            message = f"the runner exited {result.exit_code}"
            tail = scrub_text(stderr_tail or "").strip()
            if tail:
                message += f": {tail[-STDERR_TAIL_LIMIT:]}"
        else:
            message = "the model returned no text"
    fault_class = explained["faultClass"]
    if not ok and fault_class is None:
        # A clean exit with no text is not a fault the ladder knows; it is
        # still a failed ping.
        fault_class = "work"
    return {
        "harness": harness,
        "model": model,
        "ok": ok,
        "faultClass": None if ok else fault_class,
        "reason": None if ok else explained["reason"],
        "message": scrub_text(message)[:MESSAGE_LIMIT] if not ok else "",
        "exitCode": result.exit_code,
    }


async def ping(harness: str, model: str | None,
               timeout_s: int = DEFAULT_PING_TIMEOUT_S) -> dict:
    """Run one ping through the runner for `harness` and judge it."""
    scratch = Path(tempfile.mkdtemp(prefix="ralphd-llm-ping-"))
    try:
        workspace = scratch / "workspace"
        workspace.mkdir()
        # Secrets the profile handed this process through its env are
        # scrubbed from the message; the scratch dir stands in for $HOME so
        # no ambient creds dir is read.
        refresh_redaction_map(home=scratch)
        runner = build_runner(harness, workspace)
        result = await runner.run(PING_PROMPT, scratch / "ping.jsonl",
                                  model=model, timeout_s=timeout_s)
        tail = ""
        if result.exit_code not in (0, None) and not result.error_message:
            tail = runner_stderr_tail(scratch / "ping.jsonl")
        return judge(harness, model, result, tail)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ralphd-engine --llm-ping",
        description="Send one ping through the runner for a harness and "
                    "print the verdict as one JSON line.")
    parser.add_argument("--harness", default=DEFAULT_HARNESS, choices=HARNESSES)
    parser.add_argument("--model", default=None,
                        help="model ref (default: the harness's own default)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_PING_TIMEOUT_S,
                        help="seconds before the ping is abandoned "
                             f"(default {DEFAULT_PING_TIMEOUT_S})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    verdict = asyncio.run(ping(args.harness, args.model or None, args.timeout))
    print(json.dumps(verdict), flush=True)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
