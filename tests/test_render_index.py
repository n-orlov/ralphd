"""Task 012 (PRD v0.26 requirement C.1, #271): `ralphd.render_index`, the
engine-owned side index maintained INCREMENTALLY while an iteration runs,
never only at its end.

v0.25's `collapse_index.json` (task 020/021) is written exactly once, from
`engine/loop.py`'s own iteration-boundary bookkeeping, after the runner has
already returned and `output.jsonl` is fully written -- the case an
operator actually watches, a LIVE iteration, always has no index at all.
PRD C.1's ruling: the side index may record whatever a bounded tail needs
(rendered-line byte offsets, the renderer's own state at those offsets)
beside the collapse segments, and it may be written incrementally.

Two properties, one test class each:

* the engine's own live write path -- `engine/runner.py`'s `pump()`, the
  ONE place `output.jsonl` is written while an iteration is still running
  -- extends `render_index.json` as it goes, not only once the transcript
  is finished; a reader that opens it mid-iteration finds a file that
  validates against the bytes actually on disk right then --
  `TestWrittenWhileLive`;
* every checkpoint the index ever records, on both PRD Q's own canonical
  fixture and the silent-prelude variant, equals what an independent full
  `log_render` render of the transcript's prefix up to that same byte
  produces -- `TestEntriesEqualAFullRenderOfTheirOwnPrefix`.

Mutation case (recorded in the commit message): removing `runner.py`'s
`live_index.feed`/`flush` calls from `pump()` (leaving only `loop.py`'s
end-of-iteration `render_index.write_index` call) makes
`test_the_index_exists_and_validates_before_the_iteration_ends` fail --
this pins the fact this task's own criterion 3 exists to fail at `9b72b700`
(no `render_index` module existed there at all).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import stat
import sys
import time
import zlib

import pytest

from ralphd import render_index
from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.engine.runner import PiRunner
from ralphd.log_merge import OUTPUT_FILENAME, merged_lines
from test_log_merge_bounded_reads import (
    _count_transcript_bytes,
    _write_iteration_with_recognized_nonrendering_gap,
)


def _independent_checkpoints(raw: bytes) -> list[dict]:
    """A from-scratch, single forward pass over `raw` using ONLY
    `log_render`'s own public primitives (`new_render_state`,
    `render_to_lines`) -- never `render_index.LiveRenderIndex` -- so
    comparing its output against a written index proves that index's own
    checkpoints really do describe a full render of every prefix they
    claim to cover, not merely that `render_index.py` agrees with itself.
    Each entry's `lineCrc32` is `zlib.crc32` of that one raw line's own
    bytes, computed here directly off the transcript."""
    state = new_render_state()
    rendered_lines = 0
    offset = 0
    out = []
    for raw_line in raw.splitlines(keepends=True):
        text = raw_line.decode("utf-8", errors="replace")
        produced = render_to_lines(text, tty=False, state=state)
        rendered_lines += len(produced)
        offset += len(raw_line)
        out.append({"byteOffset": offset, "renderedLines": rendered_lines,
                    "state": render_index._state_to_json(state),
                    "lineCrc32": zlib.crc32(raw_line)})
    return out


class TestEntriesEqualAFullRenderOfTheirOwnPrefix:
    """Success criterion 4: on PRD Q's own canonical fixture (`gap_count=
    1,000`, no prelude) and the silent-prelude variant (`gap_count=
    10,000`, `prelude_count=1,000`), at EVERY recorded offset the index's
    own entry equals what a full `log_render` render of the transcript's
    prefix up to that offset produces."""

    @pytest.mark.parametrize("gap_count,prelude_count,label", [
        (1_000, 0, "prd-q-canonical"),
        (10_000, 1_000, "silent-prelude"),
    ])
    def test_every_checkpoint_matches_an_independent_full_render(
            self, tmp_path, gap_count, prelude_count, label):
        run_root = tmp_path / label / "registry" / "runs" / "probe"
        gap = json.dumps({"type": "message_end", "message": {"content": []},
                          "padding": "x" * 1000})
        _write_iteration_with_recognized_nonrendering_gap(
            run_root, 1, "streamed text", gap, gap_count,
            prelude_count=prelude_count)
        itdir = run_root / "iterations" / "0001"
        output_path = itdir / OUTPUT_FILENAME

        index = render_index.compute_render_index(output_path)
        expected = _independent_checkpoints(output_path.read_bytes())

        assert index["transcriptBytes"] == output_path.stat().st_size
        assert len(index["checkpoints"]) == len(expected) > 0
        assert index["checkpoints"] == expected, label

    def test_write_index_persists_the_same_checkpoints_computed_in_memory(
            self, tmp_path):
        """`write_index` (the engine's iteration-boundary call) is not a
        second, drifting implementation of `compute_render_index` -- it
        writes exactly that function's own return value, byte for byte."""
        run_root = tmp_path / "registry" / "runs" / "probe"
        gap = json.dumps({"type": "message_end", "message": {"content": []}})
        _write_iteration_with_recognized_nonrendering_gap(
            run_root, 1, "hi", gap, 50)
        itdir = run_root / "iterations" / "0001"
        # `write_index` first: it provenance-stamps the transcript's mtime
        # (task cure-01-01-4), so a rebuild computed AFTER it sees the very
        # witness it published.
        render_index.write_index(itdir)
        in_memory = render_index.compute_render_index(itdir / OUTPUT_FILENAME)
        on_disk = json.loads((itdir / render_index.INDEX_FILENAME).read_text())
        assert on_disk == in_memory


def _fake_pi_emits_then_hangs(tmp_path, n: int):
    """A `pi` stand-in that streams `n` distinct, recognized NDJSON events
    -- comfortably more than `render_index.LIVE_FLUSH_EVERY_LINES` -- and
    then hangs forever, never closing its own stdout. Stands in for the
    real shape task 012's own criterion 3 needs: an iteration that is
    genuinely still RUNNING (never reaches EOF on its own) when a reader
    looks at its render index."""
    script = tmp_path / "fake-pi-emits-then-hangs"
    lines = "\n".join(
        "echo '{\"type\": \"message_update\", \"assistantMessageEvent\": "
        "{\"type\": \"text_delta\", \"delta\": \"x\"}}'"
        for _ in range(n))
    script.write_text(f"#!/bin/sh\ncat >/dev/null\n{lines}\nexec sleep 9999\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _run_and_peek_mid_flight(tmp_path, itdir, transcript, script,
                              poll_predicate):
    """Start `PiRunner.run` against `script` (which never exits on its
    own), poll `render_index.load_index(itdir)` until `poll_predicate`
    accepts it or a bound is hit, then cancel the still-running call and
    let `PiRunner.run`'s own `finally` clean up the child -- never letting
    the fake pi's own hang, or the runner's ordinary SIGINT/grace/SIGKILL
    sequence, cost this test any real wall-clock time. Returns the index
    dict observed while `PiRunner.run` had NOT yet returned -- i.e.
    genuinely mid-iteration, never a post-hoc rebuild after the fact."""
    runner = PiRunner(workspace=tmp_path, pi_bin=str(script))

    async def go():
        task = asyncio.ensure_future(
            runner.run("prompt", transcript, timeout_s=60))
        seen = None
        for _ in range(500):
            await asyncio.sleep(0.01)
            index = render_index.load_index(itdir)
            if index is not None and poll_predicate(index):
                seen = index
                break
        assert not task.done(), "the runner call must still be mid-flight"
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return seen

    return asyncio.run(go())


class TestWrittenWhileLive:
    """Success criteria 1-3: the render index is extended by the engine's
    own live write path while `output.jsonl` is still being written, and a
    reader mid-iteration finds a file that validates against exactly the
    bytes on disk right then -- never only a post-hoc rebuild once the
    iteration (and the whole transcript) is already finished."""

    def test_the_index_exists_and_validates_before_the_iteration_ends(
            self, tmp_path):
        n = render_index.LIVE_FLUSH_EVERY_LINES + 10
        script = _fake_pi_emits_then_hangs(tmp_path, n)
        itdir = tmp_path / "iterations" / "0001"
        itdir.mkdir(parents=True)
        transcript = itdir / OUTPUT_FILENAME

        index = _run_and_peek_mid_flight(
            tmp_path, itdir, transcript, script, lambda idx: True)

        assert index is not None, "the live-written index must validate"
        actual_size = transcript.stat().st_size
        assert actual_size > 0
        # It was written WHILE running, not only by some final rebuild
        # after the fact: it covers strictly fewer bytes than the whole
        # transcript's own `n` lines (the last partial batch under
        # `LIVE_FLUSH_EVERY_LINES` is still on disk, just not yet flushed
        # into the index) -- proof the periodic in-`pump()` flush, never
        # `loop.py`'s end-of-iteration call (never reached here -- this
        # runner call is `PiRunner.run` in isolation, cancelled before it
        # ever returns), is what produced it.
        assert 0 < index["transcriptBytes"] < actual_size

        # And what it DOES cover really does equal a full render of that
        # exact prefix.
        covered = transcript.read_bytes()[: index["transcriptBytes"]]
        assert index["checkpoints"] == _independent_checkpoints(covered)

    def test_the_uncovered_suffix_is_unindexed_not_invalid(self, tmp_path):
        """PRD C.1's own reader contract: a transcript that has grown past
        what the index covers does not make the index INVALID -- it stays
        a validated index for a shorter prefix, and the difference between
        `transcriptBytes` and the transcript's actual current size is
        exactly the suffix a bounded reader must treat as unindexed."""
        n = render_index.LIVE_FLUSH_EVERY_LINES + 5
        script = _fake_pi_emits_then_hangs(tmp_path, n)
        itdir = tmp_path / "iterations" / "0001"
        itdir.mkdir(parents=True)
        transcript = itdir / OUTPUT_FILENAME

        index = _run_and_peek_mid_flight(
            tmp_path, itdir, transcript, script, lambda idx: True)

        assert index is not None
        actual_size = transcript.stat().st_size
        uncovered = actual_size - index["transcriptBytes"]
        assert uncovered > 0
        # An index that claimed MORE than the transcript's current size
        # would instead be refused outright (the one real disagreement --
        # a truncated/rewritten transcript, `collapse_index.load_index`'s
        # own equivalent case).
        doc = dict(index)
        doc["transcriptBytes"] = actual_size + 1
        (itdir / render_index.INDEX_FILENAME).write_text(json.dumps(doc))
        assert render_index.load_index(itdir) is None


def _said(text):
    return {"type": "message_end", "message": {"content": [
        {"type": "text", "text": text}]}}


def _fake_pi_calls_a_tool_then_waits(tmp_path, pad: int, go):
    """A `pi` stand-in that starts a tool call, streams `pad` silent ~1 KB
    lines while that call is still pending -- many live flushes, and well
    over a megabyte of transcript -- says 'during', then waits for `go` to
    exist before ending the call, saying 'after' and exiting."""
    script = tmp_path / "fake-pi-tool-call"
    events = {
        "before": _said("before"),
        "start": {"type": "tool_execution_start", "toolCallId": "call-1",
                  "toolName": "bash", "args": {"command": "sleep 1"}},
        "pad": {"type": "message_end", "message": {"content": []},
                "padding": "x" * 1000},
        "during": _said("during"),
        "end": {"type": "tool_execution_end", "toolCallId": "call-1",
                "toolName": "bash", "isError": False,
                "result": {"content": [{"type": "text", "text": "ok"}]}},
        "after": _said("after"),
    }
    script.write_text(f"""#!{sys.executable}
import json, os, sys, time
E = {events!r}
sys.stdin.read()
def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\\n")
    sys.stdout.flush()
emit(E["before"])
emit(E["start"])
for _ in range({pad}):
    emit(E["pad"])
emit(E["during"])
while not os.path.exists({str(go)!r}):
    time.sleep(0.01)
emit(E["end"])
emit(E["after"])
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class TestPendingToolCallAcrossFlushes:
    """A live index stays valid across every flush made while a tool call
    is pending: `tool_pending` holds a `(name, args)` pair, and the flush's
    own self-check of what it last published must compare that state in
    the same (JSON) shape it was stored in, or the writer poisons itself
    and every live tail falls back to reading the whole transcript."""

    PAD = 1500

    def test_index_stays_valid_and_the_hub_tail_bounded(self, tmp_path,
                                                         monkeypatch):
        reg = tmp_path / "registry"
        run_root = reg / "runs" / "probe"
        itdir = run_root / "iterations" / "0001"
        itdir.mkdir(parents=True)
        (itdir / "meta.json").write_text(json.dumps({
            "number": 1, "phase": "worker", "model": "stub-model",
            "approach": 1, "startedAt": "2025-01-01T00:00:00Z",
            "exitCode": None, "error": None, "usage": {"totalTokens": 1}}))
        transcript = itdir / OUTPUT_FILENAME
        go = tmp_path / "go"
        script = _fake_pi_calls_a_tool_then_waits(tmp_path, self.PAD, go)
        (tmp_path / "workspace").mkdir()
        runner = PiRunner(workspace=tmp_path / "workspace", pi_bin=str(script))
        # The dead-run on-disk surface; both surfaces try the same
        # `render_index.bounded_tail` first.
        monkeypatch.setattr(ui_server, "_proxy_text",
                            lambda *a, **kw: (False, ""))

        def tail_two():
            full = render_to_lines("".join(merged_lines(run_root)),
                                   tty=False, state=new_render_state())
            with monkeypatch.context() as mp:
                counts = _count_transcript_bytes(mp)
                _, rendered = ui_server.rendered_log_lines(reg, "probe", 2)
            return counts[0], rendered, full[-2:]

        def assert_index_valid():
            index = render_index.load_index(itdir)
            assert index is not None, "the live index must validate"
            assert index["poisoned"] is False
            return index

        async def go_run():
            task = asyncio.ensure_future(
                runner.run("prompt", transcript, timeout_s=120))
            deadline = time.monotonic() + 60
            while b"during" not in (transcript.read_bytes()[-200:]
                                    if transcript.exists() else b""):
                assert not task.done(), "the fake pi ended early"
                assert time.monotonic() < deadline, "the fake pi never said 'during'"
                await asyncio.sleep(0.01)
            # Mid-iteration, the tool call still pending: the writer has
            # flushed many times since `tool_execution_start`.
            mid = assert_index_valid()
            assert mid["checkpoints"][-1]["state"]["tool_pending"] == {
                "call-1": ["bash", {"command": "sleep 1"}]}
            size = transcript.stat().st_size
            assert size > 1_000_000
            mid_tail = tail_two()
            go.touch()
            await asyncio.wait_for(task, 60)
            return mid_tail, size

        (mid_bytes, mid_rendered, mid_full), mid_size = asyncio.run(go_run())
        assert mid_rendered == mid_full
        assert mid_full[-1].endswith("during")
        assert mid_bytes < 1_000_000, (mid_bytes, mid_size)

        final = assert_index_valid()
        assert final["transcriptBytes"] == transcript.stat().st_size
        end_bytes, end_rendered, end_full = tail_two()
        assert end_rendered == end_full
        assert end_full[-1].endswith("after")
        assert end_bytes < 1_000_000, end_bytes
