"""Black-box liveness test for `ralphctl logs --follow`/`logsf` (task 056,
operator steering 016).

`cmd_logs` used to call `api(..., raw=True, timeout=3600)`, which reads the
ENTIRE HTTP response body via `.read()` before returning control to the
caller. With `follow=true` the engine's response body only ends once the
job terminates, so `ralphctl logs --follow` buffered everything and dumped
it all at once at job end instead of streaming live, even though the
engine's `GET /logs?follow=true` itself streams fine on its own (see
tests/test_e2e.py::test_get_logs_follow_streams_across_iteration_boundaries,
which talks to the engine directly with urllib, bypassing the CLI's
buggy call path entirely).

A naive "assert output eventually appears" test would pass on the buggy
code too, since a buffered read still delivers the full body once the
process's underlying HTTP connection closes at job end. The genuine
liveness proof here is: read a line from the CLI subprocess's stdout, then
IMMEDIATELY (before the job has had any further chance to progress) check
that the job's own status.json still says the job is running -- something
only possible if that line arrived while the job was still alive, not
after it had already finished and the whole response got flushed at once.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

RALPHCTL = Path(sys.executable).parent / "ralphctl"

# Slow enough (several seconds of total job runtime) that a buffered
# implementation's "first" line -- which would only arrive at job end --
# is trivially distinguishable from a genuinely live first line, which
# arrives while iterations are still in flight.
_STUB_ENV = {"STUB_RICH_EVENTS": "1", "STUB_SLEEP": "1.5", "STUB_TASKS": "3"}


def _spawn(run, *extra_args: str) -> subprocess.Popen:
    env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
    return subprocess.Popen(
        [str(RALPHCTL), "logs", run.run_id, "--follow", *extra_args],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)


def _read_first_nonblank_line(proc: subprocess.Popen, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line == "" and proc.poll() is not None:
            break
        if line.strip():
            return line
    raise TimeoutError("ralphctl logs --follow produced no output in time")


def _job_state(run) -> str | None:
    sf = run.run_dir / "status.json"
    if not sf.exists():
        return None
    try:
        return json.loads(sf.read_text()).get("state")
    except json.JSONDecodeError:
        return None


def _assert_liveness_and_drain(run, proc: subprocess.Popen, *, raw: bool) -> str:
    try:
        first_line = _read_first_nonblank_line(proc, timeout=30)
        # The check happens as close as possible to the moment the line
        # arrived -- there is no sleep/poll loop between readline() and
        # this assertion, so a state of "running" here is only possible if
        # the CLI genuinely delivered the line mid-job.
        state = _job_state(run)
        assert state not in ("succeeded", "failed", "aborted"), (
            f"first {'raw' if raw else 'rendered'} line only arrived after "
            f"the job had already reached a terminal state ({state!r}) -- "
            "ralphctl buffered the whole response instead of following it "
            "live"
        )

        # Let the job finish naturally, then confirm the CLI itself exits
        # cleanly once the underlying stream closes, with the full expected
        # content present.
        run.wait_terminal(timeout=60)
        rest = proc.stdout.read()
        rc = proc.wait(timeout=15)
        assert rc == 0, (rc, proc.stderr.read())
        return first_line + rest
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_logs_follow_streams_live_pretty(live):
    run = live(run_id="followtest-pretty", job={"iterations": 20},
               stub_env=_STUB_ENV)
    run.wait_api()

    proc = _spawn(run)
    full = _assert_liveness_and_drain(run, proc, raw=False)

    assert "iteration 1" in full and "phase=planning" in full
    assert "planning done" in full
    assert "everything finished" in full
    assert "tokens=" in full


def test_logs_follow_streams_live_raw(live):
    run = live(run_id="followtest-raw", job={"iterations": 20},
               stub_env=_STUB_ENV)
    run.wait_api()

    proc = _spawn(run, "--raw")
    full = _assert_liveness_and_drain(run, proc, raw=True)

    lines = [l for l in full.splitlines() if l.strip()]
    assert lines, "no raw NDJSON lines received"
    parsed = 0
    for l in lines:
        json.loads(l)  # every raw line must be valid NDJSON (no rendering)
        parsed += 1
    assert parsed == len(lines)
    assert any(json.loads(l).get("type") == "ralphd.iteration" for l in lines)


def test_logsf_alias_also_streams_live(live):
    """`logsf` is a pure argv-rewrite alias for `logs --follow` (see
    _preprocess_logs_argv) -- confirm the alias path is covered too."""
    run = live(run_id="followtest-alias", job={"iterations": 20},
               stub_env=_STUB_ENV)
    run.wait_api()

    env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
    proc = subprocess.Popen(
        [str(RALPHCTL), "logsf", run.run_id],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    full = _assert_liveness_and_drain(run, proc, raw=False)
    assert "iteration 1" in full and "phase=planning" in full


def _try_read_line(proc: subprocess.Popen, timeout: float) -> str | None:
    """Non-blocking (up to `timeout`) readline off `proc.stdout` -- `None`
    means nothing arrived within the window, as opposed to `readline()`
    itself which blocks indefinitely. Used by the task-004 test below to
    prove a real timing gap rather than just an event ORDER."""
    r, _, _ = select.select([proc.stdout], [], [], timeout)
    if not r:
        return None
    return proc.stdout.readline()


def test_logs_follow_shows_tool_invocation_before_tool_ends(live):
    """task 004: the live invocation line for a tool call must be visible
    in the follow stream strictly BEFORE the matching `tool_execution_end`
    is even emitted by the stub job, i.e. while the tool call is still
    genuinely in flight -- not merely rendered-before-printed-completion
    due to buffering. `STUB_TOOL_SLEEP` (tests/stub-pi/pi) opens a real,
    multi-second wall-clock window between `tool_execution_start` and
    `tool_execution_end` so this is a genuine timing proof: after seeing
    the invocation line we assert no completion/outcome line arrives
    within a window comfortably shorter than STUB_TOOL_SLEEP."""
    stub_env = {"STUB_RICH_EVENTS": "1", "STUB_TOOL_SLEEP": "4"}
    run = live(run_id="followtest-toolopen", job={"iterations": 3},
               stub_env=stub_env)
    run.wait_api()

    proc = _spawn(run)
    try:
        invocation_line = None
        deadline = time.time() + 30
        while time.time() < deadline:
            line = _try_read_line(proc, 1.0)
            if line is None:
                if proc.poll() is not None:
                    break
                continue
            if line == "" and proc.poll() is not None:
                break
            if "bash $" in line and "→" in line:
                invocation_line = line
                break
        assert invocation_line is not None, "tool invocation line never appeared"
        # No outcome yet -- the matching tool_execution_end has not been
        # emitted (this is the plain-non-TTY start line, task 004).
        assert "✓" not in invocation_line and "✗" not in invocation_line

        # The real proof: give the stub comfortably less time than
        # STUB_TOOL_SLEEP to produce a completion line -- if the renderer
        # (or the stub) were instead buffering until the tool call ended,
        # this window would consistently show the outcome too.
        next_line = _try_read_line(proc, 1.5)
        assert next_line is None or (
            "✓" not in next_line and "✗" not in next_line and "↳" not in next_line
        ), f"tool completion appeared before STUB_TOOL_SLEEP elapsed: {next_line!r}"

        run.wait_terminal(timeout=60)
        rc = proc.wait(timeout=15)
        assert rc == 0, (rc, proc.stderr.read())
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

