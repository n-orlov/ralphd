"""Task 016: `ralphctl logs -f` must leave the terminal in the SAME
termios mode it found it in, on every exit path from a follow -- not just
the happy path.

Before task 016, save/restore of termios state lived inside the
`_QuitWatcher` background daemon thread. That is provably unsafe: a
main-thread `KeyboardInterrupt` (Ctrl+C -> SIGINT) unwinds and can
terminate the process before the watcher thread's own `finally` block
ever gets scheduled by the interpreter, stranding the real terminal in
cbreak/no-echo mode after `logs -f` exits. These tests drive the actual
CLI end-to-end under a REAL pty (the `pty` module, not a pipe) -- a pipe
has no termios state at all, so exercising the bug (and its fix) requires
a genuine pseudo-terminal -- and assert the slave side's termios settings
are bit-for-bit identical before spawn and after the process has exited,
for both the SIGINT path and the pre-existing 'q'-quit path.

Traceability: BOTH signal paths (SIGINT and 'q') are verified at the full
end-to-end level (real `ralphctl logs -f` subprocess, real pty, real
signal delivery) -- not merely against the `_TerminalModeGuard` context
manager in isolation -- because driving the whole CLI under a pty here
turned out not to be disproportionate: the existing `live` test-engine
fixture already gives a real running job to follow, and `pty.openpty()`
is stdlib and cheap.
"""

from __future__ import annotations

import os
import pty
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

RALPHCTL = Path(sys.executable).parent / "ralphctl"


def _read_until(fd: int, timeout: float, min_bytes: int = 1) -> bytes:
    """Read from `fd` (non-blocking-tolerant) until at least `min_bytes`
    have arrived or `timeout` elapses. Used to confirm the follow has
    actually started producing output -- i.e. the signal below really
    lands "mid-stream", not before the process has even entered its
    follow loop."""
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline and len(buf) < min_bytes:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _spawn_under_pty(run, extra_args: list[str]):
    """Open a real pty, spawn `ralphctl logs -f <extra_args>` with its
    stdio attached to the slave end, and return
    (proc, master_fd, pre_spawn_attrs) -- `pre_spawn_attrs` is the
    slave's termios state captured BEFORE the child ever touches it, the
    baseline every assertion below compares against."""
    master_fd, slave_fd = pty.openpty()
    pre_attrs = termios.tcgetattr(slave_fd)
    env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
    proc = subprocess.Popen(
        [str(RALPHCTL), "logs", run.run_id, "--follow", *extra_args],
        env=env, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)  # child holds its own dup'd copies; the pty stays
                        # alive via master_fd for the parent to inspect.
    return proc, master_fd, pre_attrs


def test_sigint_restores_termios_mode(live):
    run = live(run_id="pty-sigint", job={"iterations": 20},
               stub_env={"STUB_RICH_EVENTS": "1", "STUB_SLEEP": "1.5",
                         "STUB_TASKS": "3"})
    run.wait_api()

    proc, master_fd, pre_attrs = _spawn_under_pty(run, [])
    try:
        # Confirm the follow is genuinely live (producing output) before
        # the signal lands, per the "mid-stream" test bar.
        out = _read_until(master_fd, timeout=30, min_bytes=1)
        assert out, "ralphctl logs --follow produced no output under pty"

        proc.send_signal(signal.SIGINT)
        rc = proc.wait(timeout=15)

        # Drain remaining output looking for a traceback, and to let the
        # pty settle before re-checking its mode.
        tail = _read_until(master_fd, timeout=2, min_bytes=0)
        assert b"Traceback" not in tail

        assert rc == 130, rc

        # Re-open termios on the SAME pty (master reflects the live
        # slave-side state) and assert it matches the pre-spawn baseline
        # exactly -- iflag/oflag/cflag/lflag/cc all restored, not merely
        # ECHO/ICANON.
        post_attrs = termios.tcgetattr(master_fd)
        assert post_attrs == pre_attrs, (post_attrs, pre_attrs)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        os.close(master_fd)


def test_quit_keypress_restores_termios_mode(live):
    run = live(run_id="pty-quit", job={"iterations": 20},
               stub_env={"STUB_RICH_EVENTS": "1", "STUB_SLEEP": "1.5",
                         "STUB_TASKS": "3"})
    run.wait_api()

    proc, master_fd, pre_attrs = _spawn_under_pty(run, [])
    try:
        out = _read_until(master_fd, timeout=30, min_bytes=1)
        assert out, "ralphctl logs --follow produced no output under pty"

        os.write(master_fd, b"q")
        rc = proc.wait(timeout=15)

        tail = _read_until(master_fd, timeout=2, min_bytes=0)
        assert b"Traceback" not in tail
        assert rc == 0, rc

        post_attrs = termios.tcgetattr(master_fd)
        assert post_attrs == pre_attrs, (post_attrs, pre_attrs)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        os.close(master_fd)


def test_sigterm_exits_clean_and_restores_termios_mode(live):
    run = live(run_id="pty-sigterm", job={"iterations": 20},
               stub_env={"STUB_RICH_EVENTS": "1", "STUB_SLEEP": "1.5",
                         "STUB_TASKS": "3"})
    run.wait_api()

    proc, master_fd, pre_attrs = _spawn_under_pty(run, [])
    try:
        out = _read_until(master_fd, timeout=30, min_bytes=1)
        assert out, "ralphctl logs --follow produced no output under pty"

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)

        tail = _read_until(master_fd, timeout=2, min_bytes=0)
        assert b"Traceback" not in tail
        assert rc == 128 + signal.SIGTERM, rc

        post_attrs = termios.tcgetattr(master_fd)
        assert post_attrs == pre_attrs, (post_attrs, pre_attrs)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        os.close(master_fd)
