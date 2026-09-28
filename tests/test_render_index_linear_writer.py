"""Task 004 (PRD v0.27 D.2/D.3 together, #275): the render index's own
WRITE cost -- every byte the live writer publishes, summed across a whole
iteration, to `render_index.json` AND `render_index.tail.ndjson` together
-- grows LINEARLY in the transcript's own line count, on every event
SHAPE the transcript can carry, not only the text-only control task 012
(`_next_flush_threshold`'s geometric MAIN-publish schedule) already
covered.

Task 012's own fix scheduled MAIN publishes geometrically so the SUM of
every full-snapshot rewrite stayed linear in the CHECKPOINT count. It says
nothing about the SIZE of one checkpoint's own `state`. `new_render_
state()`'s `tool_args`/`tool_pending` dicts hold one entry per in-flight
`toolCallId`; a transcript with many `tool_execution_start` events that
are never matched by their `tool_execution_end` (an operator killing a
long tool call, a crashed worker, a stalled subprocess -- all ordinary)
leaves those dicts growing by one entry per new DISTINCT render state. The
compact on-disk encoding (task 013, D.1) already stores each distinct
state once -- but once, in FULL: a distinct state's own serialised size
grows with however many calls are open AT THAT STATE, so storing N/2 ever-
growing distinct states costs O(1 + 2 + ... + N/2) = O(N^2) bytes, not
O(N) -- confirmed directly at `32352c15` (before this task): doubling the
never-ended sequence's own line count from 200 to 800 (a 4x doubling)
multiplies its published bytes by ~3,984x/3,952x, not the ~4x a linear
writer can cost at most.

The fix (`_encode_states`/`_diff_state`/`_apply_state_patch`): a distinct
state list is stored INCREMENTALLY -- the first entry in full, every
later entry as a diff against the one immediately before it in that same
list, `tool_args`/`tool_pending` diffed key-by-key (`{"add": {...}, "del":
[...]}`) rather than copied whole. One more open tool call now costs one
new dict entry's own bytes, not a fresh copy of every entry already open,
so the total bytes any distinct-state list costs is linear in how many of
them there are -- on every sequence, not only the ones whose state stays
small. `_decode_states` is the exact inverse; `decode_index(encode_index(
index)) == index` still holds (`tests/test_render_index.py`).

Three sequences, each driven through the REAL live writer end to end
(`PiRunner` + `LiveRenderIndex`, exactly `engine/runner.py`'s own
`pump()`/`loop.py`'s end-of-iteration path): a `text_delta` control (no
tool state ever changes -- pins task 012's own geometric MAIN-publish
fix is untouched); N/2 `tool_execution_start` events each immediately
followed by their matching `tool_execution_end` (tool state churns but
never grows past one entry -- already linear before this task, so this
sequence is the "nothing regressed" witness); and N/2 `tool_execution_
start` events that are NEVER matched (tool state grows monotonically --
the sequence that fails at `32352c15`).

Mutation pin: reverting `_encode_checkpoint_columns`'s `_encode_states(
states)` call to a bare `states` (storing each distinct state in full
again) makes the never-ended sequence's own doubling assertions below
fail exactly as they do at `32352c15`, while the text and matched-pairs
sequences would still pass -- this module's whole point is the third
sequence, the other two are the regression witnesses that prove the fix
touches nothing else.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import stat
from pathlib import Path

from ralphd import render_index
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.engine.runner import PiRunner
from ralphd.log_merge import OUTPUT_FILENAME, merged_lines

# Fixed additive slack on top of "at most doubles" -- comfortably bigger
# than the real per-doubling noise every sequence's own geometric publish
# schedule and diff encoding leaves, but far smaller than what a quadratic
# writer's ~4x (or worse) growth would need to slip under.
_RECORD_ALLOWANCE = 400
_BYTE_ALLOWANCE = 20_000


def _text_delta_lines(n: int) -> list[str]:
    return [json.dumps({"type": "message_update", "assistantMessageEvent":
                        {"type": "text_delta", "delta": "x"}})
           for _ in range(n)]


def _matched_tool_lines(n: int) -> list[str]:
    """`n // 2` distinct `tool_execution_start` events, each immediately
    followed by its own matching `tool_execution_end` -- tool state churns
    (one entry added, then removed) but never holds more than one pending
    call at a time."""
    lines = []
    for i in range(n // 2):
        lines.append(json.dumps({"type": "tool_execution_start",
                                 "toolCallId": f"call-{i}", "toolName": "bash",
                                 "args": {"command": f"echo {i}"}}))
        lines.append(json.dumps({"type": "tool_execution_end",
                                 "toolCallId": f"call-{i}", "result": "ok"}))
    return lines


def _unended_tool_lines(n: int) -> list[str]:
    """`n // 2` distinct `tool_execution_start` events, NONE ever matched
    by a `tool_execution_end` -- `tool_pending`/`tool_args` grow by one
    entry per event, so every checkpoint fed after one is its own
    DISTINCT, ever-larger render state."""
    return [json.dumps({"type": "tool_execution_start",
                        "toolCallId": f"call-{i}", "toolName": "bash",
                        "args": {"command": f"echo {i}"}})
           for i in range(n // 2)]


_SEQUENCES = {"text_delta_control": _text_delta_lines,
             "matched_tool_pairs": _matched_tool_lines,
             "unended_tool_starts": _unended_tool_lines}


def _fake_pi_emits(tmp_path: Path, lines: list[str], name: str) -> Path:
    """A `pi_bin` stand-in that consumes stdin, emits exactly the given
    NDJSON `lines`, then exits normally -- no hang, so `PiRunner.run`
    returns on its own once the whole transcript has been fed through the
    real live-write path (`pump()`'s `live_index.append`/`.flush` calls)."""
    script = tmp_path / name
    # Every line is already a JSON string with no embedded single quotes
    # (tool args/ids here are all plain identifiers), so a plain
    # single-quoted echo reproduces it exactly.
    body = "\n".join(f"echo '{line}'" for line in lines)
    script.write_text(f"#!/bin/sh\ncat >/dev/null\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _run_and_measure(tmp_path: Path, n: int, label: str, sequence: str):
    """Run a real `PiRunner` against a script emitting `sequence`'s own
    `n`-line transcript to completion, instrumenting every byte
    `LiveRenderIndex` actually writes -- both `render_index.json`'s own
    (tmp-then-replace) MAIN publishes and `render_index.tail.ndjson`'s own
    appended shards -- and every checkpoint RECORD either kind of write
    carries (the length of that write's own `checkpoints.lineCrc32`
    column, summed across however many shards one tail append's own NDJSON
    line holds -- exactly one, always, but read generically). Returns
    `(total_bytes, total_records, run_root, itdir, lines)`."""
    run_root = tmp_path / label
    itdir = run_root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    (itdir / "meta.json").write_text(json.dumps({
        "number": 1, "phase": "worker", "model": "stub-model", "approach": 1,
        "startedAt": "2025-01-01T00:00:00Z", "endedAt": "2025-01-01T00:01:00Z",
        "exitCode": 0, "error": None, "usage": {"totalTokens": 1}}))
    transcript = itdir / OUTPUT_FILENAME
    lines = _SEQUENCES[sequence](n)
    script = _fake_pi_emits(tmp_path, lines, f"fake-pi-{label}")
    runner = PiRunner(workspace=tmp_path, pi_bin=str(script))

    total_bytes = 0
    total_records = 0
    tmp_name = render_index.INDEX_FILENAME + ".tmp"
    tail_name = render_index.TAIL_FILENAME
    original_write_text = Path.write_text
    original_open = builtins.open

    def spy_write_text(self, data, *a, **kw):
        nonlocal total_bytes, total_records
        if self.name == tmp_name:
            total_bytes += len(data.encode("utf-8"))
            total_records += len(json.loads(data)["checkpoints"]["lineCrc32"])
        return original_write_text(self, data, *a, **kw)

    def spy_open(path, mode="r", *a, **kw):
        f = original_open(path, mode, *a, **kw)
        if "a" in mode and Path(path).name == tail_name:
            original_write = f.write

            def write(data):
                nonlocal total_bytes, total_records
                total_bytes += len(data.encode("utf-8"))
                for shard_line in data.splitlines():
                    if shard_line.strip():
                        total_records += len(
                            json.loads(shard_line)["checkpoints"]["lineCrc32"])
                return original_write(data)

            f.write = write
        return f

    Path.write_text = spy_write_text
    builtins.open = spy_open
    try:
        asyncio.run(runner.run("prompt", transcript, timeout_s=60))
    finally:
        Path.write_text = original_write_text
        builtins.open = original_open
    return total_bytes, total_records, run_root, itdir, lines


class TestRenderStateEncodingIsByteLinear:
    """D.2/D.3: the sum of every MAIN publish's own write plus every tail
    shard's own append, across a whole iteration, grows at most LINEARLY
    (within a fixed additive allowance) in the transcript's own line
    count, on every one of the three sequences -- doubling the line count
    at most doubles both the total checkpoint records and the total bytes
    written, plus that allowance. The `unended_tool_starts` sequence fails
    on the pre-task-004 writer, which stores each distinct, ever-growing
    render state in full."""

    def _assert_linear(self, sequence: str, tmp_path: Path):
        b200, r200, root200, itdir200, lines200 = _run_and_measure(
            tmp_path, 200, f"{sequence}-200", sequence)
        b400, r400, root400, itdir400, lines400 = _run_and_measure(
            tmp_path, 400, f"{sequence}-400", sequence)
        b800, r800, root800, itdir800, lines800 = _run_and_measure(
            tmp_path, 800, f"{sequence}-800", sequence)

        assert r400 <= 2 * r200 + _RECORD_ALLOWANCE, (
            f"{sequence}: records more than doubled: {r200} -> {r400}")
        assert r800 <= 2 * r400 + _RECORD_ALLOWANCE, (
            f"{sequence}: records more than doubled: {r400} -> {r800}")
        assert b400 <= 2 * b200 + _BYTE_ALLOWANCE, (
            f"{sequence}: bytes more than doubled: {b200} -> {b400}")
        assert b800 <= 2 * b400 + _BYTE_ALLOWANCE, (
            f"{sequence}: bytes more than doubled: {b400} -> {b800}")

        # Criterion 2: `output.jsonl`'s own bytes equal exactly the
        # emitted NDJSON, and the published index renders a tail equal to
        # the full renderer's own last N lines, for the LARGEST run of
        # each sequence.
        transcript = itdir800 / OUTPUT_FILENAME
        expected_bytes = sum(len(line.encode("utf-8")) + 1 for line in lines800)
        assert transcript.stat().st_size == expected_bytes

        full = render_to_lines("".join(merged_lines(root800)), tty=False,
                              state=new_render_state())
        for tail_n in (1, 5, 200):
            tail = render_index.bounded_tail(root800, tail_n)
            assert tail is not None, f"{sequence}: bounded_tail must answer"
            assert tail == full[-tail_n:], (
                f"{sequence}: tail({tail_n}) disagrees with the full render")

    def test_text_delta_control(self, tmp_path):
        self._assert_linear("text_delta_control", tmp_path)

    def test_matched_tool_pairs(self, tmp_path):
        self._assert_linear("matched_tool_pairs", tmp_path)

    def test_unended_tool_starts(self, tmp_path):
        self._assert_linear("unended_tool_starts", tmp_path)
