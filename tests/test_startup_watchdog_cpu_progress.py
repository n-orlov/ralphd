"""Task 006 (v0.20 lane-green): the startup window measures AGENT SILENCE,
not the HOST's scheduling delay.

The defect this pins: `PiRunner.run`'s startup-window watchdog (task 001a)
read "no parseable pi event within `startup_timeout_s` of the spawn" as proof
of a hang. That window also contains OS process creation plus the child's own
runtime cold start, whose wall clock is set by how contended the host is. On
the nightly self-hosted box (one 4-core runner, `max-parallel 2` across
`gate`, `heavy` and `nightly`) a healthy invocation that had merely not been
given a CPU slice yet was killed and retried as an `infra` fault -- an extra
`infra_retry:planning:1` round that turned
`tests/flows/test_flow_crash_resume_refunds.py` and
`tests/flows/test_flow_infra_ladder.py` red there, with no defect in either.

The discriminator, and what these two nodes prove, is CPU progress: a
*starved* child still accumulates CPU time whenever it is scheduled, while
the hang the watchdog exists for (a `getaddrinfo` wait, a sleep) is BLOCKED
and consumes none. So a window that expires while the child's own CPU
counter advanced is re-armed; a window that expires with the counter frozen
is still the infra verdict it always was, on the very first such window --
no deadline anywhere is widened, and both nodes below are pinned to the
child's OWN consumed CPU time rather than to any wall clock of this host, so
neither is load-sensitive in the way the defect was.

Mutation case (recorded in the commit message): dropping the re-arm (reading
the first expiry as the verdict again, as before this commit) makes
test_a_child_that_is_starved_but_advancing_is_not_killed_as_an_infra_hang
fail; dropping the frozen-counter branch makes
test_a_child_that_consumes_no_cpu_is_still_killed_inside_its_window fail.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys

from ralphd.engine import runner as runner_mod
from ralphd.engine.runner import PiRunner, process_cpu_seconds


# CPU seconds the "busy" child below burns before it emits its first event --
# comfortably more than the 0.2s startup window, measured on the child's own
# `time.process_time()` so the assertion holds on an idle and on a loaded
# host alike.
BUSY_CHILD_CPU_S = 0.6
STARTUP_WINDOW_S = 0.2


def _busy_fake_pi(tmp_path):
    """An agent that burns its own CPU for a while before its first event.

    Stands in for the real shape of the defect (a healthy `pi` whose cold
    start does not fit in the startup window on a contended host) with a
    child whose CPU consumption is guaranteed rather than dependent on this
    host's load.
    """
    script = tmp_path / "fake-pi-busy"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        f"_target = time.process_time() + {BUSY_CHILD_CPU_S}\n"
        "_wall = time.monotonic() + 60\n"
        "while time.process_time() < _target and time.monotonic() < _wall:\n"
        "    pass\n"
        "print(json.dumps({'type': 'message_end', 'message': {"
        "'role': 'assistant', 'content': [{'type': 'text', 'text': 'hi'}],"
        "'usage': {'input': 1, 'output': 1, 'totalTokens': 2}}}))\n"
        "sys.stdout.flush()\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _blocked_fake_pi(tmp_path):
    """An agent that produces nothing and consumes no CPU: the real hang."""
    script = tmp_path / "fake-pi-blocked"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "sys.stdin.read()\n"
        "time.sleep(9999)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_a_child_that_is_starved_but_advancing_is_not_killed_as_an_infra_hang(
        tmp_path):
    pi = _busy_fake_pi(tmp_path)
    r = PiRunner(tmp_path, pi_bin=str(pi))

    async def go():
        return await r.run("prompt", tmp_path / "output.jsonl",
                           timeout_s=60,
                           startup_timeout_s=STARTUP_WINDOW_S)

    result = asyncio.run(go())
    assert result.no_traffic_timeout is False, (
        "startup watchdog: a child that kept consuming CPU (it just had not "
        "reached its first event yet) must not be classified a no-traffic "
        "infra hang -- that is the host's scheduling delay, not agent silence")
    assert result.interrupted is False
    assert result.exit_code == 0
    assert "message_end" in (tmp_path / "output.jsonl").read_text()
    # The event genuinely arrived later than one window, so the node is not
    # vacuous: the watchdog had to re-arm at least once to let it through.
    assert result.duration_s > STARTUP_WINDOW_S, (
        f"startup watchdog: this node only proves the re-arm if the first "
        f"event lands after the {STARTUP_WINDOW_S}s window, but the whole "
        f"run took {result.duration_s}s")


def test_a_child_that_consumes_no_cpu_is_still_killed_inside_its_window(
        tmp_path, monkeypatch):
    # Compressed grace (task 048's B.5 idiom) so the kill path costs
    # milliseconds instead of the real 30s production default.
    monkeypatch.setattr(runner_mod, "SHUTDOWN_GRACE_S", 0.5)
    pi = _blocked_fake_pi(tmp_path)
    r = PiRunner(tmp_path, pi_bin=str(pi))

    async def go():
        return await r.run("prompt", tmp_path / "output.jsonl",
                           timeout_s=60,
                           startup_timeout_s=STARTUP_WINDOW_S)

    result = asyncio.run(go())
    assert result.no_traffic_timeout is True, (
        "startup watchdog: a child that produced no event AND consumed no "
        "further CPU is blocked -- the fail-fast verdict task 001a exists "
        "for must still fire on the first such window")
    assert result.timed_out is False, (
        "startup watchdog: the 60s iteration timeout must NOT be what ended "
        "this attempt")
    assert r.running is False


def test_the_cpu_counter_reads_a_live_pid_and_declines_a_dead_one():
    mine = process_cpu_seconds(os.getpid())
    assert mine is not None and mine > 0, (
        "startup watchdog: this test process has certainly consumed CPU")
    # A pid whose counter cannot be read is never *reported* as progress:
    # `None` means "no evidence of progress", and the watchdog keeps the
    # verdict this engine always gave in that case.
    assert process_cpu_seconds(-1) is None


class _FakeProc:
    """Enough of asyncio.subprocess.Process for PiRunner.interrupt()."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None


def test_interrupt_targets_the_pid_directly_before_the_child_has_its_own_session(
        tmp_path, monkeypatch):
    """Task 001: the pre-`setsid` window that caused the gate red at
    `472d055` (`test_a_child_that_consumes_no_cpu_is_still_killed_inside_its_
    window` there recorded duration_s=61.06, exit_code=-9, timed_out=True).

    `start_new_session=True` makes the child call `os.setsid()` before it
    execs `pi`, but nothing orders that call against this method's read of
    `os.getpgid(child_pid)`. In the window before `setsid()` has run, the
    child's process group is still the ENGINE's own group (inherited from
    the fork) -- so the old `os.killpg(os.getpgid(pid), SIGINT)` would
    signal the engine's own group, not the child, and the SIGINT never
    reaches it. This drives exactly that window with no real timing race:
    `os.getpgid` is made to report the engine's own pgid for the child pid,
    the shape a genuinely pre-`setsid` child has.

    Mutation case (recorded in the commit message): reverting `interrupt()`
    to the unconditional `os.killpg(os.getpgid(pid), SIGINT)` makes this
    test fail -- `killpg` gets called with the engine's own pgid instead of
    `kill` being called on the child's pid, and the assertions below catch
    that directly rather than through a timing-dependent kill outcome.
    """
    r = PiRunner(tmp_path, pi_bin="unused")
    fake_pid = 999_123
    r._proc = _FakeProc(fake_pid)

    own_pgid = os.getpgid(0)
    calls = {"kill": [], "killpg": []}

    def fake_getpgid(pid):
        if pid == fake_pid:
            # Pre-setsid: the child's group is still ours.
            return own_pgid
        return own_pgid  # os.getpgid(0) also reads as our own pgid

    def fake_kill(pid, sig):
        calls["kill"].append((pid, sig))

    def fake_killpg(pgid, sig):
        calls["killpg"].append((pgid, sig))

    monkeypatch.setattr(runner_mod.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(runner_mod.os, "kill", fake_kill)
    monkeypatch.setattr(runner_mod.os, "killpg", fake_killpg)

    ok = r.interrupt()

    assert ok is True
    assert calls["kill"] == [(fake_pid, runner_mod.signal.SIGINT)], (
        "pre-session: interrupt() must signal the child's own pid directly, "
        "never the group read back as the engine's own")
    assert calls["killpg"] == [], (
        "pre-session: killpg must not be called with the engine's own "
        "pgid -- that would signal the engine's group, not the child, "
        "which is the exact loss that produced the 61.06s/-9/timed_out gate "
        "red at 472d055")
