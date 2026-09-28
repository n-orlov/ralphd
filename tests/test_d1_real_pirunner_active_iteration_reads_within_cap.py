"""Task cure-01-01-4 (v0.27 PRD D.1 and D.2/D.3 together, DoD 4): a whole-
call tail read taken WHILE an iteration is still active -- between two of
the live writer's own geometric MAIN publishes (task 012, PRD D.2) --
must stay inside the same 1,000,000-byte whole-call cap `tests/test_tail_
whole_call_read_bound.py` already pins for a FINISHED iteration's from-
scratch index. Every other test that exercises this cap builds its
fixture by writing a static `output.jsonl` directly and then calling
`render_index.write_index`/`compute_render_index` -- which always
produces the SAME from-scratch, fully-published index a real live writer
only reaches once, at the very end. None of them can see the bug this
task cures: `LiveRenderIndex.flush`'s own incremental publishes, real
because they run inside `engine/runner.py`'s `pump()`, only republish the
whole `render_index.json` geometrically (`_next_flush_threshold`: 20,
40, 80, ..., 10,240, 20,480, ...) -- so a read taken between two of
those, on the named PRD Q silent-prelude fixtures (prelude depths 1,000
and 1,700, `gap_count` 10,000, ~11,504/~12,204 raw lines respectively --
both comfortably inside one such geometric gap), used to fall back to a
raw re-render of the whole uncovered suffix and read well over the
bound (measured directly against this module's own fixture, before the
cure below: 2,538,567 bytes for the `live` surface at prelude 1,700 --
see this test's own history for the exact figure at every depth/surface
combination).

This module is the one place in the suite that drives the REAL
`PiRunner.run` -- a genuine `asyncio.create_subprocess_exec` child, not a
scripted stand-in -- through a fake `pi` executable that streams the
fixture's own exact NDJSON bytes and then blocks before EOF (polling a
sentinel file), so the assertions below run against the live writer's
OWN mid-iteration state: `LiveRenderIndex.append`/`flush` really did run,
one raw line at a time, exactly as `pump()` always calls them, and the
iteration has NOT yet reached its end-of-stream `flush(force=True)` or
`engine/loop.py`'s own end-of-iteration `render_index.write_index`
rebuild. The cure itself (`render_index.TAIL_FILENAME`, see that
constant's own module-level comment) is exercised precisely because the
main file has not republished by the time the read happens.

Reads go through the real client-facing call, `ui_server.
rendered_log_lines` -- both of its surfaces (task 013's own `_wire`/
`_measured_call`, reused unchanged from `tests/test_tail_whole_call_read_
bound.py` -- both surfaces resolve to the very same on-disk `render_index.
bounded_tail` call for a `tail`-bounded request, so both exercise the
exact code path this task changes) -- on the first and a repeated call,
and the rendered lines must equal the FULL renderer's own last two lines
exactly, never a hand-picked string."""

from __future__ import annotations

import asyncio
import json
import stat
import textwrap
import threading
import time

import pytest

from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.engine.runner import PiRunner
from ralphd.log_merge import merged_lines

from test_tail_whole_call_read_bound import _FROZEN_CAP, _measured_call

# Same event shapes `_write_iteration_with_recognized_nonrendering_gap`
# builds, kept as a local copy here (not imported) because that helper
# WRITES straight to a file; this module needs the individual lines
# themselves, to stream them through a real `pi` subprocess instead.
_GAP_LINE = json.dumps(
    {"type": "message_end", "message": {"content": []}, "padding": "x" * 1000})


def _fixture_lines(text: str, gap_line: str, gap_count: int, prelude_count: int) -> list[str]:
    """Byte-for-byte the same construction
    `_write_iteration_with_recognized_nonrendering_gap` uses for the named
    PRD Q silent-prelude scenario (prelude BEFORE the streamed text, so
    the setter sits far past any bounded look-back at either named
    depth)."""
    prelude = [gap_line for _ in range(prelude_count)]
    lines = prelude + [
        json.dumps({"type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": text}}),
        json.dumps({"type": "message_update",
                    "assistantMessageEvent": {"type": "text_end"}}),
        json.dumps({"type": "message_update",
                    "assistantMessageEvent": {"type": "thinking_start"}}),
    ]
    lines += [json.dumps({"type": "message_update",
                         "assistantMessageEvent": {"type": "thinking_delta",
                                                    "delta": "thinking"}})
              for _ in range(500)]
    lines += [gap_line for _ in range(gap_count)]
    lines.append(json.dumps({"type": "message_end", "message": {"content": [
        {"type": "text", "text": text}]}}))
    return lines


# A minimal `pi` stand-in: streams every line given to it (via
# `FAKE_PI_LINES`, a JSON array on disk -- kept off argv so an arbitrarily
# large fixture never has to fit a command line), then blocks -- BEFORE
# ever closing stdout -- until `FAKE_PI_SENTINEL` exists. That is the
# whole of "keeps the child alive before EOF": the real
# `asyncio.create_subprocess_exec` child genuinely has not exited (and its
# stdout genuinely has not hit EOF) for as long as the test withholds the
# sentinel, so `PiRunner.running` is genuinely `True` and `pump()` has
# genuinely not run its end-of-stream `flush(force=True)` while the test
# reads.
_FAKE_PI = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys, time
    with open(os.environ["FAKE_PI_LINES"], "r") as f:
        lines = json.load(f)
    for line in lines:
        sys.stdout.write(line)
        sys.stdout.write("\\n")
    sys.stdout.flush()
    sentinel = os.environ["FAKE_PI_SENTINEL"]
    while not os.path.exists(sentinel):
        time.sleep(0.02)
    sys.exit(0)
""")


def _write_fake_pi(path) -> None:
    path.write_text(_FAKE_PI)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_pirunner_in_background(runner, prompt, transcript, extra_env, timeout_s):
    """Runs the real `PiRunner.run` to completion on a dedicated thread
    with its OWN event loop (`asyncio.run`), so the calling (test) thread
    stays fully synchronous and can drive `ui_server.rendered_log_lines`
    (which itself uses `asyncio.run` internally for the 'live' surface,
    `test_tail_whole_call_read_bound._wire`) without ever nesting one
    event loop inside another. The child subprocess this starts is a real
    OS process either way -- it keeps writing/blocking on its own,
    independent of which thread is currently pumping its pipes, which is
    exactly why the main thread can poll+assert for as long as it likes
    before the background thread ever resumes draining it."""
    box: dict = {}

    def _target():
        try:
            box["result"] = asyncio.run(
                runner.run(prompt, transcript, timeout_s=timeout_s, extra_env=extra_env))
        except BaseException as exc:
            box["exc"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, box


def _wait_until_exact_size(path, expected_size: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size >= expected_size:
            break
        time.sleep(0.02)
    return path.exists() and path.stat().st_size == expected_size


@pytest.mark.parametrize("prelude_count", [1000, 1700])
@pytest.mark.parametrize("surface", ["disk", "live"])
def test_d1_real_pirunner_active_iteration_reads_within_cap(
        tmp_path, monkeypatch, prelude_count, surface):
    text = ("real-pirunner active-iteration streamed text must render exactly "
           f"and stay within the whole-call cap (prelude={prelude_count})")
    lines = _fixture_lines(text, _GAP_LINE, 10_000, prelude_count)
    expected_size = sum(len(line.encode()) + 1 for line in lines)

    reg = tmp_path / "registry"
    run_root = reg / "runs" / "probe"
    itdir = run_root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    # The same shape `engine/loop.py`'s own dispatch write leaves before it
    # ever invokes the runner (`meta.json`, no `endedAt` -- this iteration
    # genuinely has not ended yet): `log_merge.merge_entries` skips an
    # iteration with no readable `meta.json` at all, and only emits the
    # "end" boundary once `endedAt` is recorded, so this is what a real
    # live (never-yet-finished) iteration looks like on disk.
    meta = {"number": 1, "phase": "worker", "model": "stub-model", "approach": 1,
           "startedAt": "2025-01-01T00:00:00Z"}
    (itdir / "meta.json").write_text(json.dumps(meta))
    transcript = itdir / "output.jsonl"

    lines_path = tmp_path / f"fixture-lines-{prelude_count}.json"
    lines_path.write_text(json.dumps(lines))
    sentinel = tmp_path / f"sentinel-{prelude_count}-{surface}"
    fake_pi = tmp_path / f"fake_pi-{prelude_count}-{surface}.py"
    _write_fake_pi(fake_pi)

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    runner = PiRunner(workspace_root, pi_bin=str(fake_pi))
    thread, box = _run_pirunner_in_background(
        runner, "probe prompt", transcript,
        {"FAKE_PI_LINES": str(lines_path), "FAKE_PI_SENTINEL": str(sentinel)},
        timeout_s=60)

    try:
        assert _wait_until_exact_size(transcript, expected_size), (
            "fake pi never streamed the full fixture", prelude_count,
            transcript.stat().st_size if transcript.exists() else None, expected_size)
        # The child is genuinely still alive -- blocked on the sentinel,
        # BEFORE its stdout has hit EOF -- for every assertion below.
        assert runner.running

        full = render_to_lines("".join(merged_lines(run_root)), tty=False,
                               state=new_render_state())
        assert len(full) >= 2, full
        assert text in full[-2], full[-2:]
        assert full[-1].strip() == "[thinking\u2026]", full[-2:]

        for attempt in ("first", "repeated"):
            read, live, rendered = _measured_call(
                monkeypatch, surface, reg, run_root, workspace_root, 2)
            assert rendered == full[-2:], (attempt, surface, rendered, full[-2:])
            assert live is (surface == "live")
            assert read <= _FROZEN_CAP, (prelude_count, surface, attempt, read)
    finally:
        sentinel.write_text("go")
        thread.join(timeout=30)

    assert "exc" not in box, box.get("exc")
    assert "result" in box, "PiRunner.run never finished"
    result = box["result"]
    assert result.exit_code == 0, result
    assert not result.timed_out, result
    assert not result.interrupted, result

    # After the iteration genuinely ends -- the pump's own end-of-stream
    # `flush(force=True)` already ran inside `PiRunner.run` itself, ahead
    # of `engine/loop.py`'s own end-of-iteration `render_index.write_index`
    # rebuild -- the very same reads still stay within the cap, on a MAIN
    # index that now covers everything by itself.
    for attempt in ("post-first", "post-repeated"):
        read, live, rendered = _measured_call(
            monkeypatch, surface, reg, run_root, workspace_root, 2)
        assert rendered == full[-2:], (attempt, surface, rendered, full[-2:])
        assert read <= _FROZEN_CAP, (prelude_count, surface, attempt, read)
