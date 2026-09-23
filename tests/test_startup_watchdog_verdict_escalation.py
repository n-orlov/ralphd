"""Task 004 (B.1, #215): the no-traffic verdict path escalates to SIGKILL
through `SHUTDOWN_GRACE_S`, the same as the iteration-timeout path already
does, instead of falling through to the outer `timeout_s` wait.

Before this task, `startup_watchdog()`'s verdict branch
(`src/ralphd/engine/runner.py`) did exactly two things after deciding the
child was silently blocked: set `result.no_traffic_timeout = True` and call
`self.interrupt()` (one SIGINT). It then returned, leaving control inside the
caller's `await asyncio.wait({pump_task}, timeout=timeout_s)` -- so whenever
the child ignores that SIGINT, the fail-fast guard task 001a exists to end a
hang in ~0.2s degenerated into no guard at all: the only thing left to end
the attempt was the *other* timer, `timeout_s`
(`min(iteration_timeout_s, max(60, deadline - now))`, up to
`iteration_timeout_s` itself -- 7200s in this run's own configuration).

Honest scope, restated here as required by the commit: this has **never
fired in production**. All 166 iteration records of the v0.22 run carry
`noTrafficTimeout: false` (see PRD B.5). The defect is structural -- only a
child that ignores SIGINT ever reaches this path -- and nothing below claims
a production saving; it proves the fix against a deliberately-arranged
pathological child instead.
"""

from __future__ import annotations

import asyncio
import stat
import sys

from ralphd.engine import runner as runner_mod
from ralphd.engine.faults import classify_fault
from ralphd.engine.runner import PiRunner
from ralphd.engine.state import billable_tokens

# Compressed grace (the same idiom used in
# tests/test_startup_watchdog_cpu_progress.py) so the escalation costs
# fractions of a second rather than the real 30s production default.
SHUTDOWN_GRACE_S = 0.4
STARTUP_WINDOW_S = 0.15
# Deliberately much larger than any plausible escalation time, so a
# regression that falls back through to the outer `timeout_s` wait (the
# defect this task fixes) is caught by the duration assertion below rather
# than by the test simply taking `TIMEOUT_S` seconds to finish.
TIMEOUT_S = 20


def _sigint_ignoring_fake_pi(tmp_path):
    """An agent that ignores SIGINT ITSELF, unconditionally.

    Deliberate, and the OPPOSITE precondition from
    `tests/conftest.py::sigint_default_for_children` -- that fixture exists
    to force SIG_DFL onto a child so an inherited SIG_IGN from this host's
    own process tree (self-hosted runners, `scripts/run_bounded.sh`'s
    background-job wrapper -- see PRD requirement B and
    `test_a_child_that_consumes_no_cpu_is_still_killed_inside_its_window`'s
    own comment) does not make an unrelated test flaky. This test wants a
    child that ignores SIGINT NO MATTER what disposition it inherited, so it
    sets SIG_IGN explicitly in the child's own body -- do not "fix" this
    back to a plain child or wrap it in `sigint_default_for_children()`,
    that would defeat the entire point of this node, which is to prove the
    escalation fires even when the ordinary SIGINT never lands.
    """
    script = tmp_path / "fake-pi-sigint-ignoring"
    script.write_text(
        f"#!{sys.executable}\n"
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "sys.stdin.read()\n"
        "time.sleep(9999)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_a_child_that_ignores_sigint_ends_within_shutdown_grace_not_timeout_s(
        tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "SHUTDOWN_GRACE_S", SHUTDOWN_GRACE_S)
    pi = _sigint_ignoring_fake_pi(tmp_path)
    r = PiRunner(tmp_path, pi_bin=str(pi))

    async def go():
        return await r.run("prompt", tmp_path / "output.jsonl",
                           timeout_s=TIMEOUT_S,
                           startup_timeout_s=STARTUP_WINDOW_S)

    result = asyncio.run(go())

    # B.1: the attempt ends within SHUTDOWN_GRACE_S plus a margin -- the
    # watchdog's own grace wait, plus the second SHUTDOWN_GRACE_S wait/kill
    # stage shared with every other exit path, plus process spawn/kill
    # overhead -- never anywhere near TIMEOUT_S. Before this task's fix this
    # assertion failed at ~TIMEOUT_S (the process was only ever reaped by
    # the outer `timeout_s` wait's `finally`, since the pump never observed
    # EOF and the child never exited on its own).
    margin_s = 4 * SHUTDOWN_GRACE_S + 2.0
    assert result.duration_s < margin_s, (
        f"startup watchdog verdict path: took {result.duration_s}s, expected "
        f"under {margin_s}s (2x SHUTDOWN_GRACE_S={SHUTDOWN_GRACE_S}s plus "
        f"overhead) -- it must not fall through to the outer timeout_s="
        f"{TIMEOUT_S}s wait")
    assert result.duration_s < TIMEOUT_S / 2, (
        "startup watchdog verdict path: ended too close to timeout_s to "
        "prove the escalation (rather than the outer wait) ended it")

    # B.3: the two verdicts stay distinguishable in the record -- a
    # no-traffic kill is not additionally marked timed_out.
    assert result.no_traffic_timeout is True, (
        "startup watchdog: the no-traffic verdict must still be recorded")
    assert result.timed_out is False, (
        "startup watchdog: a no-traffic kill must NOT also be recorded as "
        "the (unrelated) iteration-timeout verdict")
    assert r.running is False

    # B.4: the zero-traffic refund path is unaffected by B.1's escalation --
    # this iteration produced no billable traffic, and classify_fault still
    # reads that combination as "infra" (refunded), exactly as it did before
    # this task touched runner.py at all.
    assert billable_tokens(result.usage) == 0, (
        "this pathological child never emitted any pi event, so it must "
        "not be billed")
    produced_traffic = billable_tokens(result.usage) > 0
    assert classify_fault(
        error_text="",
        exit_code=result.exit_code,
        interrupted=result.interrupted,
        timed_out=result.timed_out,
        no_traffic_timeout=result.no_traffic_timeout,
        produced_traffic=produced_traffic,
    ) == "infra", (
        "B.4: a zero-traffic no-traffic-timeout failure must still classify "
        "'infra' (refunded, never charged) -- B.1's escalation changes WHEN "
        "the process dies, not who pays")
