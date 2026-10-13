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
import queue
import subprocess
import threading
import time

from tests.conftest import RALPHCTL

# task 009 (B.3): STUB_TASKS/STUB_RICH_EVENTS unchanged; the real-time
# STUB_SLEEP padding that used to buy several seconds of job runtime for
# the buffered-vs-live distinguishability below is replaced per-test with
# STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" (see `_assert_liveness_and_drain`'s
# `hold_file` argument): the job is held open, deterministically, at the
# exact instant the immediate liveness check needs it to still be running,
# rather than probabilistically outrunning a fixed real sleep.
_STUB_ENV = {"STUB_RICH_EVENTS": "1", "STUB_TASKS": "3"}


def _stub_env_with_hold(tmp_path, name: str):
    hold_file = tmp_path / name
    env = {**_STUB_ENV, "STUB_HOLD_FILE": str(hold_file),
           "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"}
    return env, hold_file


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


def _assert_liveness_and_drain(run, proc: subprocess.Popen, *, raw: bool,
                               hold_file=None) -> str:
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

        if hold_file is not None:
            # task 009 (B.3): release the held worker iteration now that
            # the immediate liveness check above has already run against a
            # deterministically-still-running job -- the hold, not a real
            # sleep, was what kept it from reaching a terminal state early.
            hold_file.touch()

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


def test_logs_follow_streams_live_pretty(live, tmp_path):
    stub_env, hold_file = _stub_env_with_hold(tmp_path, "hold-pretty")
    run = live(run_id="followtest-pretty", job={"iterations": 20},
               stub_env=stub_env)
    run.wait_api()

    proc = _spawn(run)
    full = _assert_liveness_and_drain(run, proc, raw=False, hold_file=hold_file)

    assert "iteration 1" in full and "phase=planning" in full
    assert "planning done" in full
    assert "everything finished" in full
    assert "tokens=" in full


def test_logs_follow_streams_live_raw(live, tmp_path):
    stub_env, hold_file = _stub_env_with_hold(tmp_path, "hold-raw")
    run = live(run_id="followtest-raw", job={"iterations": 20},
               stub_env=stub_env)
    run.wait_api()

    proc = _spawn(run, "--raw")
    full = _assert_liveness_and_drain(run, proc, raw=True, hold_file=hold_file)

    lines = [l for l in full.splitlines() if l.strip()]
    assert lines, "no raw NDJSON lines received"
    parsed = 0
    for l in lines:
        json.loads(l)  # every raw line must be valid NDJSON (no rendering)
        parsed += 1
    assert parsed == len(lines)
    assert any(json.loads(l).get("type") == "ralphd.iteration" for l in lines)


def test_logsf_alias_also_streams_live(live, tmp_path):
    """`logsf` is a pure argv-rewrite alias for `logs --follow` (see
    _preprocess_logs_argv) -- confirm the alias path is covered too."""
    stub_env, hold_file = _stub_env_with_hold(tmp_path, "hold-alias")
    run = live(run_id="followtest-alias", job={"iterations": 20},
               stub_env=stub_env)
    run.wait_api()

    env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
    proc = subprocess.Popen(
        [str(RALPHCTL), "logsf", run.run_id],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    full = _assert_liveness_and_drain(run, proc, raw=False, hold_file=hold_file)
    assert "iteration 1" in full and "phase=planning" in full


def _try_read_line(proc: subprocess.Popen, timeout: float) -> str | None:
    """Non-blocking (up to `timeout`) readline off `proc.stdout` -- `None`
    means nothing arrived within the window, as opposed to `readline()`
    itself which blocks indefinitely. Used by the task-004 test below to
    prove a real timing gap rather than just an event ORDER.

    approach-4 task 010: this used to `select.select([proc.stdout], ...)`,
    which asks the KERNEL whether the pipe's fd has bytes -- while
    `readline()` reads out of the `TextIOWrapper`'s own buffer above it. A
    burst of lines (the follow stream replays its whole backlog snapshot
    the moment it attaches) lands in that buffer in ONE read: the first
    `readline()` returns line 1 and the remaining lines become invisible
    to `select`, so every later poll reported "nothing arrived" until the
    child happened to write again -- and then returned lines that had in
    truth arrived seconds earlier. Both directions of that error were
    live: a real line went unseen, and a line's apparent arrival time was
    the time of the NEXT write. Draining the pipe in a thread and taking
    lines off a queue measures arrival where it actually happens, with no
    fd-versus-buffer split.
    """
    return _reader_for(proc).next_line(timeout)


class _LineReader:
    """Background drain of one `Popen.stdout` into a queue (see
    `_try_read_line`). `next_line(timeout)` returns the next line, `None`
    if none arrived within `timeout`, or `""` once the stream is at EOF --
    the same three outcomes the old select-based helper reported, minus
    the buffered-line blind spot."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._q: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(proc,),
                                        daemon=True)
        self._thread.start()

    def _pump(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:  # blocking readline, in this thread only
                self._q.put(line)
        except (ValueError, OSError):
            pass  # pipe closed under us by the test's own cleanup
        self._q.put("")

    def next_line(self, timeout: float) -> str | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


_READERS: dict[int, _LineReader] = {}


def _reader_for(proc: subprocess.Popen) -> _LineReader:
    reader = _READERS.get(id(proc))
    if reader is None:
        reader = _READERS[id(proc)] = _LineReader(proc)
    return reader


def test_logs_follow_shows_tool_invocation_before_tool_ends(live, tmp_path):
    """task 004: the live invocation line for a tool call must be visible
    in the follow stream strictly BEFORE the matching `tool_execution_end`
    is even emitted by the stub job, i.e. while the tool call is still
    genuinely in flight -- not merely rendered-before-printed-completion
    due to buffering.

    approach-4 task 010: the window that proves this is now opened
    DETERMINISTICALLY by `STUB_TOOL_HOLD_FILE` (tests/stub-pi/pi), which
    blocks the stub between `tool_execution_start` and `tool_execution_end`
    until this test releases it, instead of by a fixed `STUB_TOOL_SLEEP`
    wall-clock gap. The old shape measured its 2.0s gap from the stub's own
    emission but started its 1.5s no-completion check only once the
    invocation line had traversed pump -> output.jsonl -> the engine's
    0.3s-poll follower -> the CLI's renderer, so any delivery/scheduling
    lag above 0.5s put `tool_execution_end` inside the check window and the
    node went red with `'  \u21b3 \u2713 ok (ok)'` -- observed on the
    `ubuntu:24.04, PYTHONUSERBASE unset` nightly cell at 7b0860b (check run
    103003362434, a 19-minute serial fast tier). With the hold there is no
    margin to lose: the completion event cannot exist until this test says
    so, and the assertions below are unchanged in strength."""
    hold_file = tmp_path / "hold-toolopen"
    stub_env = {"STUB_RICH_EVENTS": "1",
                "STUB_TOOL_HOLD_FILE": str(hold_file),
                "STUB_TOOL_HOLD_TIMEOUT_S": "30"}
    # Task 015 (requirement E.1, #169) retired the off-budget grace review
    # this fixture's `iterations: 3` used to lean on: the stub planner's
    # default two tasks cost planning(1) + worker(1) + worker(1) = 3,
    # leaving no budget for the review that turns the run terminal --
    # the retired grace review used to supply that review off-budget for
    # free once both tasks were complete. Without it the run held
    # `timed_out` with no review ever run, so `run.wait_terminal` below
    # never saw `succeeded`. One more iteration covers the ordinary
    # planning+worker+worker+review path outright. v0.36 requirement B adds
    # one spare iteration past the verdict: a verdict on the last charged
    # iteration holds `exhausted`.
    run = live(run_id="followtest-toolopen", job={"iterations": 5},
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

        # The tool call is provably STILL OPEN at this instant: the stub is
        # parked inside its hold, and the hold's duration marker (written
        # only when the hold ends, i.e. strictly before `tool_execution_end`
        # is printed) does not exist yet. This replaces the old shape's
        # implicit "the 2.0s sleep probably hasn't elapsed" assumption with
        # a fact read off disk.
        assert not (tmp_path / "hold-toolopen.stub-tool-hold-duration").exists(), (
            "the stub's tool hold had already ended when the invocation line "
            "arrived -- tool_execution_end may already have been emitted, so "
            "this line does not prove the call was in flight")

        # The real proof: while the tool call is held open, no completion
        # line may arrive -- if the renderer (or the stub) were instead
        # buffering until the tool call ended, this window would show the
        # outcome too.
        next_line = _try_read_line(proc, 1.5)
        assert next_line is None or (
            "✓" not in next_line and "✗" not in next_line and "↳" not in next_line
        ), f"tool completion appeared while the tool call was still held open: {next_line!r}"

        # And the invocation line really came from a call the stub had
        # entered and not yet finished, not from a replayed backlog.
        assert (tmp_path / "hold-toolopen.stub-tool-hold-entered").exists(), (
            "the stub never entered the tool hold, so the invocation line "
            "cannot have come from a tool call that was still in flight")

        # Release the held tool call now that the proof above has run
        # against a deterministically-still-in-flight invocation.
        hold_file.touch()

        run.wait_terminal(timeout=60)
        rc = proc.wait(timeout=15)
        assert rc == 0, (rc, proc.stderr.read())
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# --- moved here verbatim from tests/test_cli_logs_rendered_tail.py by task
# cure-01-03 (requirement F): it is the only node of the rendered-tail family
# that needs a real `subprocess.Popen([RALPHCTL, ...])` pipe, and requirement
# F confines the CLI process boundary to the modules docs/test-suite-audit.md
# names for it (this one among them). Body, docstring and assertions are
# unchanged; see that file's § "Subsumed" for the row.
def test_follow_pretty_tail_shows_backlog_live_then_keeps_streaming(live, tmp_path):
    """Genuine liveness proof for the follow+tail combo: once the job has
    already produced more than N rendered lines, spawn `-Nf`, confirm the
    first line arrives while the job is still running (not buffered until
    job end -- same liveness bar as task 056), then let the job finish and
    confirm the total output grew PAST the initial N-line backlog once
    more content arrived live (proving `-Nf` doesn't just stop at N and
    exit -- it shows N lines of backlog, then keeps following).

    task 010 (B.4): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" holds the
    first worker iteration open (mid-preamble) instead of racing a real
    STUB_SLEEP window to catch the job with backlog already produced but
    not yet terminal -- a held first iteration's own rich-event preamble
    (planning + partial worker transcript) already exceeds the smaller
    `tail_n` used here (confirmed: 6 lines while held vs 3 threshold),
    so no further real wait is needed before spawning `-Nf`."""
    hold_file = tmp_path / "hold-rendtail-live"
    run = live(run_id="rendtail-follow-live",
              stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "6",
                        "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()

    tail_n = 3
    deadline = time.time() + 30
    while time.time() < deadline:
        full = run.ralphctl("logs", run.run_id, "--tail", "0")
        if len(full.stdout.splitlines()) > tail_n:
            break
        time.sleep(0.3)
    else:
        raise TimeoutError("job never produced enough backlog to exercise tail_n")

    env = {**os.environ, "RALPHD_REGISTRY": str(run.registry)}
    proc = subprocess.Popen(
        [str(RALPHCTL), "logs", run.run_id, f"-{tail_n}f"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    try:
        deadline = time.time() + 15
        first_line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if line.strip():
                first_line = line
                break
        assert first_line, "ralphctl logs -Nf produced no output in time"

        # No sleep/poll loop between readline() and this check -- a
        # buffered implementation could only deliver this line after the
        # job had already reached a terminal state.
        status = run.run_dir / "status.json"
        state = None
        if status.exists():
            import json
            state = json.loads(status.read_text()).get("state")
        assert state not in ("succeeded", "failed", "aborted"), (
            f"first line only arrived after the job reached a terminal "
            f"state ({state!r}) -- -Nf buffered instead of following live")

        # Release the held first worker iteration now that the immediate
        # liveness check above has already run against a deterministically
        # still-running job -- the hold, not a real sleep, kept it open.
        hold_file.touch()

        run.wait_terminal(timeout=60)
        rest = proc.stdout.read()
        rc = proc.wait(timeout=15)
        assert rc == 0, (rc, proc.stderr.read())

        total_lines = (first_line + rest).splitlines()
        # more than tail_n lines total: the backlog (tail_n lines) plus at
        # least some genuinely new lines that arrived live afterwards.
        assert len(total_lines) > tail_n, total_lines
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
