"""Black-box tests for task 057 (operator steering 017): `ralphctl logs
<id> -N` in pretty (default) mode must mean N RENDERED lines -- what the
operator actually sees -- not N raw NDJSON events.

Before the fix, `-N` was applied engine-side to raw events (`GET
/logs?tail=N`) *before* rendering; the pretty renderer then collapses/skips
many raw event types (e.g. a whole burst of `text_delta`/`toolcall_delta`
events becomes one streamed-text block or one tool one-liner), so
`logs <id> -N` produced a wildly variable, much-smaller-than-N number of
VISIBLE lines. The fix (see `_render_to_lines`/`_stream_logs_pretty_tailed`
in src/ralphd/cli/main.py) always fetches the FULL raw transcript, renders
every line, and trims to exactly N lines AFTER rendering.

`--raw` mode (1 raw line == 1 raw event, tail applied engine-side) is
unchanged and covered by tests/test_cli_logs_tail_syntax.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

RALPHCTL = Path(sys.executable).parent / "ralphctl"

_STUB_ENV = {"STUB_RICH_EVENTS": "1", "STUB_TASKS": "4"}


def test_pretty_tail_n_counts_rendered_lines_not_raw_events(live):
    run = live(run_id="rendtail-basic", stub_env=_STUB_ENV)
    run.wait_terminal()

    # This stub job emits far more raw NDJSON events than any of the N
    # values exercised below -- proven directly via --raw (unaffected by
    # this fix, still 1 line per raw event).
    full_raw = run.ralphctl("logs", run.run_id, "--raw", "--tail", "0")
    assert full_raw.returncode == 0, (full_raw.stdout, full_raw.stderr)
    raw_event_count = len([l for l in full_raw.stdout.splitlines() if l.strip()])

    full_pretty = run.ralphctl("logs", run.run_id, "--tail", "0")
    assert full_pretty.returncode == 0, (full_pretty.stdout, full_pretty.stderr)
    full_rendered_count = len(full_pretty.stdout.splitlines())

    # The whole point of the bug: far fewer rendered lines than raw events.
    assert raw_event_count > full_rendered_count * 2, (
        raw_event_count, full_rendered_count)

    for n in (5, 10, 20):
        assert n < full_rendered_count, (
            "test job doesn't produce enough rendered output to exercise "
            f"N={n} meaningfully (only {full_rendered_count} total)")
        assert n * 3 < raw_event_count, (
            "test job doesn't emit enough raw events to prove N is being "
            f"applied post-render, not pre-render (N={n}, "
            f"raw_event_count={raw_event_count})")
        res = run.ralphctl("logs", run.run_id, "--tail", str(n))
        assert res.returncode == 0, (res.stdout, res.stderr)
        got = res.stdout.splitlines()
        assert len(got) == n, (
            f"-N {n}: expected exactly {n} RENDERED lines, got {len(got)}:\n"
            + res.stdout)
        # sanity: the N lines returned really are the tail of the full
        # rendered output (post-render trim, not some unrelated subset).
        assert got == full_pretty.stdout.splitlines()[-n:]


def test_pretty_dash_n_tail_syntax_also_counts_rendered_lines(live):
    """Same proof via the `-N` tail-style syntax (not just `--tail N`)."""
    run = live(run_id="rendtail-dashn", stub_env=_STUB_ENV)
    run.wait_terminal()

    full_pretty = run.ralphctl("logs", run.run_id, "--tail", "0")
    full_rendered_count = len(full_pretty.stdout.splitlines())
    assert full_rendered_count > 10

    res = run.ralphctl("logs", run.run_id, "-10")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert len(res.stdout.splitlines()) == 10


def test_pretty_iteration_filter_tail_also_counts_rendered_lines(live):
    """`--iteration N` selects a single iteration's transcript through the
    SAME rendering/trim code path -- confirm it gets the identical
    rendered-not-raw treatment (this is the endpoint whose engine-side
    `follow` semantics differ, see `_stream_logs_pretty_tailed`'s
    docstring; the bounded, non-follow path exercised here shares
    `_render_to_lines` either way)."""
    run = live(run_id="rendtail-iter", stub_env={**_STUB_ENV, "STUB_TASKS": "1"})
    run.wait_terminal()

    full_pretty = run.ralphctl("logs", run.run_id, "--iteration", "2", "--tail", "0")
    full_rendered_count = len(full_pretty.stdout.splitlines())
    assert full_rendered_count > 3

    res = run.ralphctl("logs", run.run_id, "--iteration", "2", "--tail", "3")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert len(res.stdout.splitlines()) == 3
    assert res.stdout.splitlines() == full_pretty.stdout.splitlines()[-3:]


def test_follow_pretty_tail_n_shows_exactly_n_backlog_lines_on_terminal_job(live):
    """`-Nf`/`logsf` after the job has ALREADY finished: the follow
    connection just replays the (post-render-trimmed) backlog and then
    closes immediately (nothing more will ever arrive) -- deterministic
    proof that the follow code path (`_stream_logs_pretty_tailed`) applies
    the identical rendered-line trim as the plain bounded path."""
    run = live(run_id="rendtail-follow-term", stub_env=_STUB_ENV)
    run.wait_terminal()

    full_pretty = run.ralphctl("logs", run.run_id, "--tail", "0")
    full_rendered_count = len(full_pretty.stdout.splitlines())
    assert full_rendered_count > 10

    res = run.ralphctl("logs", run.run_id, "-10f")
    assert res.returncode == 0, (res.stdout, res.stderr)
    got = res.stdout.splitlines()
    assert len(got) == 10, (got,)
    assert got == full_pretty.stdout.splitlines()[-10:]


def test_follow_pretty_tail_shows_backlog_live_then_keeps_streaming(live):
    """Genuine liveness proof for the follow+tail combo: once the job has
    already produced more than N rendered lines, spawn `-Nf`, confirm the
    first line arrives while the job is still running (not buffered until
    job end -- same liveness bar as task 056), then let the job finish and
    confirm the total output grew PAST the initial N-line backlog once
    more content arrived live (proving `-Nf` doesn't just stop at N and
    exit -- it shows N lines of backlog, then keeps following)."""
    run = live(run_id="rendtail-follow-live",
              stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "6",
                        "STUB_SLEEP": "0.8"})
    run.wait_api()

    tail_n = 10
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
