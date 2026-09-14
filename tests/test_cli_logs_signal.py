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
import time

import pytest

from tests.conftest import RALPHCTL, sigint_default_for_children

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


def _stacks_of(proc: subprocess.Popen) -> str:
    """Ask a hung `ralphctl` where it is, instead of leaving it to inference.

    The child runs with `PYTHONFAULTHANDLER=1`, so SIGABRT makes CPython dump
    every thread's C and Python stack to stderr before dying. Used only on the
    failure path below, where the alternative is a bare `TimeoutExpired` that
    says the process did not exit but not what it was doing -- which is exactly
    the gap that left this node's intermittent failure undiagnosed for a day.
    """
    proc.send_signal(signal.SIGABRT)
    try:
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate(timeout=20)
    return (f"--- child stdout ({len(out)} chars, undrained): {out[:500]!r}\n"
            f"--- child stderr / faulthandler stacks:\n{err[-4000:]}")


# The two waits below sum to 120s, which is exactly the global `timeout` in
# `pyproject.toml` -- so on the failure path pytest-timeout would kill this
# node before it could report WHY, losing the dump `_stacks_of` exists to
# collect. A per-node bound above that sum keeps the diagnosis (operator steer
# 004 §2: raise a single node with `@pytest.mark.timeout(N)` and say why,
# never the global floor).
@pytest.mark.timeout(240)
def test_sigint_during_follow_exits_clean_no_traceback(live, tmp_path):
    # task 009 (B.3): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" keeps the job
    # genuinely non-terminal for as long as this test needs (bounded, never
    # released -- `live`'s own teardown kills the engine subprocess
    # regardless) instead of racing a fixed STUB_SLEEP window against how
    # fast three tasks with no padding would finish before the signal below
    # could land.
    #
    # STUB_HOLD_TIMEOUT_S must outlive this test's OWN worst case (60s to the
    # first line + 60s for the post-SIGINT exit). It was 30s, i.e. shorter than
    # the window the test allows itself, so on a slow host the hold could
    # expire mid-test, the job would run to completion and the engine would
    # begin shutting down underneath the follow -- making the state under test
    # ("interrupt a follow of a LIVE run") no longer the state being tested.
    hold_file = tmp_path / "hold-sigint-follow"
    run = live(run_id="sigint-follow", job={"iterations": 20},
               stub_env={"STUB_RICH_EVENTS": "1", "STUB_HOLD_FILE": str(hold_file),
                         "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "240",
                         "STUB_TASKS": "3"})
    run.wait_api()

    env = {**os.environ, "RALPHD_REGISTRY": str(run.registry),
           "PYTHONFAULTHANDLER": "1"}
    with sigint_default_for_children():
        proc = subprocess.Popen(
            [str(RALPHCTL), "logs", run.run_id, "--follow"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    try:
        _read_first_nonblank_line(proc, timeout=60)
        proc.send_signal(signal.SIGINT)
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "ralphctl logs --follow did not exit within 60s of SIGINT "
                "(intermittent, ~1 gate run in 8; host load, a full stdout "
                "pipe and getaddrinfo are all ruled out by measurement):\n"
                + _stacks_of(proc)) from None
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
