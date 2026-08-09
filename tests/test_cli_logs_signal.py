"""Task 002: `ralphctl logs -f` must exit cleanly on user interrupt.

Two behaviors, two tests:

(a) Ctrl+C (SIGINT) during a follow must exit with NO traceback on stderr
    and the single documented exit code (`_SIGINT_EXIT_CODE` = 130, see
    docs/cli.md) -- a user-interrupted follow is a normal exit, not a
    crash.
(b) A piped (non-TTY) follow must NEVER block waiting for a keypress --
    proven here the hard way: stdin is an open `subprocess.PIPE` that is
    never written to and never closed (the worst case a naive
    `sys.stdin.read(1)` could hang on), and the follow still completes
    normally once the job itself finishes.

Both use the same "live test engine" fixture as
tests/test_cli_logs_follow_liveness.py.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

RALPHCTL = Path(sys.executable).parent / "ralphctl"

_SIGINT_EXIT_CODE = 130


def _read_first_nonblank_line(proc: subprocess.Popen, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line == "" and proc.poll() is not None:
            break
        if line.strip():
            return line
    raise TimeoutError("ralphctl logs --follow produced no output in time")


def test_sigint_during_follow_exits_clean_no_traceback(live):
    # A job with a handful of slow-ish iterations so the follow is still
    # live (not already exited) when the signal arrives.
    run = live(run_id="sigint-follow", job={"iterations": 20},
               stub_env={"STUB_RICH_EVENTS": "1", "STUB_SLEEP": "1.5",
                         "STUB_TASKS": "3"})
    run.wait_api()

    env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
    proc = subprocess.Popen(
        [str(RALPHCTL), "logs", run.run_id, "--follow"],
        env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        _read_first_nonblank_line(proc, timeout=30)
        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=15)
        stderr = proc.stderr.read()
        assert rc == _SIGINT_EXIT_CODE, (rc, stderr)
        assert "Traceback" not in stderr, stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_piped_follow_never_blocks_on_open_unwritten_stdin(live):
    # Short job so the assertion ("the follow completed") is reached
    # quickly if (and only if) the CLI never actually tried to read a key
    # from stdin -- an open, never-written-to, never-closed PIPE is
    # exactly the case a stray `sys.stdin.read(1)` would block on forever.
    run = live(run_id="piped-follow-noblock", job={"iterations": 3},
               stub_env={"STUB_RICH_EVENTS": "1"})
    run.wait_api()

    env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
    proc = subprocess.Popen(
        [str(RALPHCTL), "logs", run.run_id, "--follow"],
        env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        rc = proc.wait(timeout=30)
        stderr = proc.stderr.read()
        assert rc == 0, (rc, stderr)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdin:
            proc.stdin.close()
