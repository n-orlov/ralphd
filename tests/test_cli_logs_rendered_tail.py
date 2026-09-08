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

task cure-01-03 (requirement F): every node here drives the CLI through the
`live` fixture's own helper. The one node of this family that additionally
needed a REAL `subprocess.Popen([ralphctl, ...])` pipe of its own -- the
`-Nf` "backlog first, then keeps streaming" liveness proof -- was moved
verbatim into the canonical process-boundary module
`tests/test_cli_logs_follow_liveness.py`, so the process boundary lives only
in the modules `docs/test-suite-audit.md` names for it (see that file's
§ "Subsumed" for the node's row). Nothing about the proof changed: same
body, same assertions, same stub hold mechanism.

task 002 (requirement F): the four nodes remaining here (the liveness proof
above having already left) now drive `ralphctl` in-process through
`tests/conftest.py`'s `call_ralphctl_in_process` -- none of the four needs a
second real OS process, since each is a single bounded (non-follow, or
follow-on-an-already-terminal-job) call, not an interleaved
still-running-process proof.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import call_ralphctl_in_process

_STUB_ENV = {"STUB_RICH_EVENTS": "1", "STUB_TASKS": "4"}


def _ctl(registry, *argv: str):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return call_ralphctl_in_process(list(argv), env=env, capsys=_CAPSYS)


@pytest.fixture(autouse=True)
def _capture_cli_output(capsys):
    """Task 002 (requirement F): module-global capsys, never concurrent --
    pytest runs one test at a time even under `-n 8`, each worker its own
    process (same pattern as tests/test_cli_fault_explanation.py)."""
    global _CAPSYS
    _CAPSYS = capsys
    yield
    _CAPSYS = None


_CAPSYS = None


def _in_process_ralphctl(run):
    def ralphctl(*argv: str):
        return _ctl(run.registry, *argv)
    run.ralphctl = ralphctl
    return run


def test_pretty_tail_n_counts_rendered_lines_not_raw_events(live):
    run = _in_process_ralphctl(live(run_id="rendtail-basic", stub_env=_STUB_ENV))
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
    run = _in_process_ralphctl(live(run_id="rendtail-dashn", stub_env=_STUB_ENV))
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
    run = _in_process_ralphctl(
        live(run_id="rendtail-iter", stub_env={**_STUB_ENV, "STUB_TASKS": "1"}))
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
    run = _in_process_ralphctl(
        live(run_id="rendtail-follow-term", stub_env=_STUB_ENV))
    run.wait_terminal()

    full_pretty = run.ralphctl("logs", run.run_id, "--tail", "0")
    full_rendered_count = len(full_pretty.stdout.splitlines())
    assert full_rendered_count > 10

    res = run.ralphctl("logs", run.run_id, "-10f")
    assert res.returncode == 0, (res.stdout, res.stderr)
    got = res.stdout.splitlines()
    assert len(got) == 10, (got,)
    assert got == full_pretty.stdout.splitlines()[-10:]
