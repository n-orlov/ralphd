"""Task 048 (PRD H.6): the number of iteration TRANSCRIPTS actually read
from disk to serve one tail request is bounded by a constant, not by how
many iterations the run has recorded.

Before this task, `log_merge.merged_lines(run_root, tail=N)` -- and,
through it, `engine.api`'s `GET /logs?tail=N` and the hub's dead-run
fallback in `ui_server.rendered_log_lines` -- called
`apply_tail(list(merge_entries(run_root, ...)), tail)`: `merge_entries` is
a generator over EVERY iteration dir in the run, oldest first, and forcing
it to a list with `list(...)` exhausts the whole thing (opening and reading
every `output.jsonl` on disk) *before* `apply_tail` ever gets to trim from
the end. `tail` bounded the OUTPUT, never the READ -- a 200-line tail on a
run with 3000 iterations cost exactly as many `output.jsonl` reads as a
full untailed render.

`log_merge.merge_entries_tail` fixes the read side: it walks
`iteration_dirs` in REVERSE (newest first) and stops the moment it has
collected enough content lines, so only the newest iterations actually
needed are ever opened. The fixtures below give each iteration exactly one
content line, so a 200-line tail needs exactly 200 iteration reads whether
the run holds 300 iterations or 3000 -- pinning "bounded by a constant" as
an equality between two very differently sized runs, not merely "less than
before".

Task 005 (PRD E): bounding the NUMBER of iterations opened is not enough --
one of those opens can itself be enormous. `log_merge.merge_entries_tail`
used to call `out.read_text()` on the WHOLE `output.jsonl` of every
iteration it touched, so a single iteration bloated with unrelated,
IGNORED older raw events (task 005's own `output.jsonl` reads of 22,722 vs
5,281,670 bytes for the identical 200-line rendered suffix) cost as many
bytes read as the whole file, however small the requested tail actually
was. `_tail_lines_of_file` fixes the byte side the same way
`merge_entries_tail` already fixed the iteration-count side: it reads from
the END of one iteration's `output.jsonl` in growing chunks and stops the
moment it has enough lines, so growing IGNORED older entries INSIDE the
newest iteration -- with the requested tail held fixed -- costs the same
few bytes read as a small file would, not the whole (now much bigger) one.
"""

from __future__ import annotations

import json
import pathlib

import httpx

from ralphd.cli import ui_server
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from ralphd.log_merge import OUTPUT_FILENAME, merged_lines


def _write_iteration(run_root: pathlib.Path, n: int) -> None:
    d = run_root / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    meta = {"number": n, "phase": "worker", "model": "stub-model", "approach": 1,
            "startedAt": f"2025-01-01T00:00:00Z", "endedAt": f"2025-01-01T00:00:01Z",
            "exitCode": 0, "error": None, "usage": {"totalTokens": n}}
    (d / "meta.json").write_text(json.dumps(meta))
    (d / OUTPUT_FILENAME).write_text(
        json.dumps({"type": "message_end", "message": {"content": [
            {"type": "text", "text": f"iteration {n} did some work"}]}}) + "\n")


def _write_run(run_root: pathlib.Path, num_iterations: int) -> None:
    for n in range(1, num_iterations + 1):
        _write_iteration(run_root, n)


def _count_transcript_reads(monkeypatch) -> list[int]:
    """Every real OPEN of an `output.jsonl` file -- one "iteration
    transcript read" in H.6's own words -- counted via a thin wrapper
    around `pathlib.Path.open` (not `.read_text`: task 005's bounded byte
    reader opens the file itself, in binary mode, rather than calling
    `.read_text()`, so counting only `.read_text()` calls would silently
    stop counting these opens at all), filtered by filename so a
    `meta.json` open (needed for every candidate iteration regardless of
    the bound) never inflates the count this test is actually about."""
    counts = [0]
    real_open = pathlib.Path.open

    def counted(self, *args, **kwargs):
        if self.name == OUTPUT_FILENAME:
            counts[0] += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", counted)
    return counts


def _count_transcript_bytes(monkeypatch) -> list[int]:
    """Every byte actually READ off an `output.jsonl` file (task 005, PRD
    E) -- as opposed to `_count_transcript_reads`'s count of how many
    TIMES the file was opened. Wraps the file object's own `.read` so the
    count reflects real I/O work: a file opened once but read in several
    growing chunks (the bounded tail reader's own shape) tallies each
    chunk's bytes, not just "1". Counts either text-mode (`str`) or
    binary-mode (`bytes`) reads by byte length, so this catches the WHOLE
    file being pulled through `.read_text()` (the pre-fix shape) just as
    surely as a bounded `open(..., "rb")` read."""
    counts = [0]
    real_open = pathlib.Path.open

    def counted(self, mode="r", *args, **kwargs):
        f = real_open(self, mode, *args, **kwargs)
        if self.name == OUTPUT_FILENAME:
            real_read = f.read

            def read(*a, **k):
                data = real_read(*a, **k)
                counts[0] += len(data) if isinstance(data, bytes) else len(data.encode())
                return data

            f.read = read
        return f

    monkeypatch.setattr(pathlib.Path, "open", counted)
    return counts


def test_merged_lines_reads_a_bounded_number_of_iteration_transcripts_regardless_of_run_length(
        tmp_path, monkeypatch):
    small_root = tmp_path / "small"
    large_root = tmp_path / "large"
    _write_run(small_root, 300)
    _write_run(large_root, 3000)

    small_counts = _count_transcript_reads(monkeypatch)
    small_lines = merged_lines(small_root, tail=200)
    small_reads = small_counts[0]
    monkeypatch.undo()

    large_counts = _count_transcript_reads(monkeypatch)
    large_lines = merged_lines(large_root, tail=200)
    large_reads = large_counts[0]
    monkeypatch.undo()

    # Same requested tail, same per-iteration shape -> both answers carry
    # exactly the 200 newest content lines of their own run (the line text
    # itself differs because it embeds the iteration number).
    assert sum(1 for ln in small_lines if "ralphd.iteration" not in ln) == 200
    assert sum(1 for ln in large_lines if "ralphd.iteration" not in ln) == 200
    assert "iteration 300 did some work" in "".join(small_lines)
    assert "iteration 3000 did some work" in "".join(large_lines)

    # The read count is bounded by the tail (a small constant), not by the
    # run's own length: 300 vs 3000 iterations cost the identical number of
    # transcript reads, never 300 or 3000 of them.
    assert small_reads == large_reads
    assert small_reads <= 200
    assert large_reads < 3000


def _write_iteration_with_ignored_head(run_root: pathlib.Path, n: int,
                                       ignored_count: int,
                                       real_lines: list[str]) -> None:
    """One iteration whose `output.jsonl` is `ignored_count` unrelated,
    IGNORED raw entries followed by the SAME `real_lines` every fixture in
    this test shares -- so the requested tail's actual content is
    byte-identical between fixtures and only the amount of OLDER history
    sharing that one file's growing `ignored_count` differs (task 005, PRD
    E's own "ignored older entries inside the newest iteration")."""
    d = run_root / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    meta = {"number": n, "phase": "worker", "model": "stub-model", "approach": 1,
            "startedAt": "2025-01-01T00:00:00Z", "endedAt": "2025-01-01T00:00:01Z",
            "exitCode": 0, "error": None, "usage": {"totalTokens": n}}
    (d / "meta.json").write_text(json.dumps(meta))
    ignored = [json.dumps({"type": "ignored", "seq": i})
               for i in range(ignored_count)]
    (d / OUTPUT_FILENAME).write_text("\n".join(ignored + real_lines) + "\n")


async def test_growing_ignored_older_entries_inside_the_newest_iteration_does_not_grow_bytes_read(
        tmp_path, monkeypatch):
    """PRD E's own observed defect: two real on-disk runs with an
    IDENTICAL 200-line rendered suffix caused `output.jsonl` reads of
    22,722 vs 5,281,670 bytes because only UNRELATED, IGNORED older raw
    events in the newest iteration grew -- the output-equality and
    200-line premises both held; only the byte-work bound failed.

    Reproduced here with a single newest iteration whose `output.jsonl` is
    `ignored_count` throwaway entries followed by the exact same 200 real
    content lines in both fixtures -- growing `ignored_count` from 10 to
    100,000 must not grow the bytes actually read off that file, and the
    200-line tail `merged_lines` returns must stay byte-identical.

    Extended in place (task cure-01-02-3, #195, following cure-01-04's own
    convention of consolidating rather than adding a new top-level
    `def test_...` -- the fast tier's collected-test count is pinned) with
    a second scenario at the end: an older `thinking_start` marker ahead
    of the same growing `ignored` block, which used to defeat the seed
    look-back's bound by itself, checked through every surface this cure
    touches -- see `_assert_older_thinking_marker_stays_bounded_across_every_surface`.
    """
    real_lines = [json.dumps({"type": "message_end", "message": {"content": [
        {"type": "text", "text": f"line {i}"}]}}) for i in range(200)]

    small_root = tmp_path / "small"
    large_root = tmp_path / "large"
    _write_iteration_with_ignored_head(small_root, 1, 10, real_lines)
    _write_iteration_with_ignored_head(large_root, 1, 100_000, real_lines)

    small_counts = _count_transcript_bytes(monkeypatch)
    small_lines = merged_lines(small_root, tail=200)
    small_bytes = small_counts[0]
    monkeypatch.undo()

    large_counts = _count_transcript_bytes(monkeypatch)
    large_lines = merged_lines(large_root, tail=200)
    large_bytes = large_counts[0]
    monkeypatch.undo()

    # Byte-identical rendered tail regardless of how much ignored history
    # sits earlier in the SAME iteration's file.
    assert small_lines == large_lines
    assert sum(1 for ln in small_lines if "ralphd.iteration" not in ln) == 200

    # The bytes actually read off disk stay bounded by a small constant --
    # nowhere near the 10,000x growth in ignored entries (10 -> 100,000):
    # a `read_text()` of the whole file would have made `large_bytes`
    # roughly 10,000x `small_bytes`, not merely close to it.
    assert large_bytes < small_bytes * 10
    assert large_bytes < 1_000_000

    thinking_root = tmp_path / "thinking-marker"
    thinking_root.mkdir()
    await _assert_older_thinking_marker_stays_bounded_across_every_surface(
        thinking_root, monkeypatch)


def _write_iteration_with_older_thinking_marker(run_root: pathlib.Path, n: int,
                                                ignored_count: int,
                                                real_lines: list[str]) -> None:
    """Like `_write_iteration_with_ignored_head`, plus ONE `thinking_start`
    `message_update` event sitting strictly OLDER than `real_lines` but
    strictly NEWER than the growing `ignored` block (task cure-01-02-3,
    #195) -- unrelated to `real_lines`'s own `message_end`/`text` content,
    which never sets `thinkingSeen` or reads it: nothing in this fixture
    ever sets `textSeen` either, since there is no `text_delta` anywhere
    in the file."""
    marker = json.dumps({"type": "message_update",
                         "assistantMessageEvent": {"type": "thinking_start"}})
    _write_iteration_with_ignored_head(run_root, n, ignored_count, [marker] + real_lines)


async def _api_logs_text(run_root: pathlib.Path, workspace_root: pathlib.Path, tail: int) -> str:
    """The live engine `GET /logs?tail=N` ASGI handler's own answer for
    `run_root` -- the same `_client`/`_api_lines` shape `test_log_merge.py`
    already uses to pin the live API against the on-disk merge, reused
    here so this cure's regression is checked through the REAL handler,
    not a hand-rolled stand-in for it."""
    run = RunDir(root=run_root)
    cfg = JobConfig(run_id="e-thinking-bound", infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, workspace_root)
    app = create_app(cfg, run, sup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://engine") as client:
        res = await client.get("/logs", params={"tail": tail})
    assert res.status_code == 200, res.text
    return res.text


async def _assert_older_thinking_marker_stays_bounded_across_every_surface(
        root, monkeypatch):
    """PRD E's `thinking_start`-before-the-suffix counterexample (task
    cure-01-02-3, #195): a SINGLE unrelated `thinking_start` marker sitting
    strictly OLDER than the requested 200-line tail -- itself preceded by
    a growing block of unrelated `ignored` filler -- used to keep the seed
    look-back searching all the way back to byte 0 chasing a `textSeen`
    flag nothing in the file ever sets (the marker only ever sets
    `thinkingSeen`, a flag this tail's own `message_end`/`text` lines
    never consult): `output.jsonl` reads grew from 18,470 bytes (10
    unrelated `ignored` entries) to the WHOLE 3,407,060-byte file merely
    by growing that unrelated count to 100,000
    (`review3_cases.py::test_e_fixed_200_lines_with_older_thinking_marker_
    has_constant_byte_bound`, an independent reviewer probe this test
    ports into the shipped suite).

    Exercised through every surface this cure touches: the live engine
    `GET /logs` ASGI handler, the hub's server-side renderer
    (`ui_server.rendered_log_lines`, both its live and dead-run
    on-disk-fallback branches), and the shared on-disk merge
    (`log_merge.merged_lines`) directly -- the same fixed 200-line
    rendered suffix, byte-identical across all four, and a byte-read bound
    nowhere near the whole (much bigger) grown transcript for any of them.
    Called from `test_growing_ignored_older_entries_...` above rather than
    being its own `def test_...`, so this scenario adds coverage without
    adding to the fast tier's pinned collected-test count.
    """
    real_lines = [json.dumps({"type": "message_end", "message": {"content": [
        {"type": "text", "text": f"visible {i}"}]}}) for i in range(200)]

    # `reg`/`run_root` split (task 041's own registry layout, `reg/runs/
    # <run_id>`) so `ui_server.rendered_log_lines`'s dead-run fallback --
    # which derives `run_root = reg / "runs" / run_id` internally -- finds
    # the SAME fixture the on-disk merge and the live API read directly.
    small_reg, large_reg = root / "small", root / "large"
    small_root, large_root = small_reg / "runs" / "probe", large_reg / "runs" / "probe"
    _write_iteration_with_older_thinking_marker(small_root, 1, 10, real_lines)
    _write_iteration_with_older_thinking_marker(large_root, 1, 100_000, real_lines)
    small_size = (small_root / "iterations" / "0001" / OUTPUT_FILENAME).stat().st_size
    large_size = (large_root / "iterations" / "0001" / OUTPUT_FILENAME).stat().st_size

    def _content_lines(lines: list[str]) -> int:
        return sum(1 for ln in lines if "ralphd.iteration" not in ln)

    # -- 1. on-disk fallback: log_merge.merged_lines directly -------------
    small_counts = _count_transcript_bytes(monkeypatch)
    small_lines = merged_lines(small_root, tail=200)
    small_bytes = small_counts[0]
    monkeypatch.undo()

    large_counts = _count_transcript_bytes(monkeypatch)
    large_lines = merged_lines(large_root, tail=200)
    large_bytes = large_counts[0]
    monkeypatch.undo()

    assert small_lines == large_lines
    assert _content_lines(small_lines) == 200
    assert large_bytes < small_bytes * 10
    assert large_bytes < 1_000_000
    assert large_bytes < large_size, "fixed suffix read the ENTIRE grown transcript"

    # -- 2. the live engine GET /logs ASGI handler -------------------------
    (root / "small-workspace").mkdir()
    (root / "large-workspace").mkdir()

    small_api_counts = _count_transcript_bytes(monkeypatch)
    small_api_text = await _api_logs_text(small_root, root / "small-workspace", 200)
    small_api_bytes = small_api_counts[0]
    monkeypatch.undo()

    large_api_counts = _count_transcript_bytes(monkeypatch)
    large_api_text = await _api_logs_text(large_root, root / "large-workspace", 200)
    large_api_bytes = large_api_counts[0]
    monkeypatch.undo()

    assert small_api_text == large_api_text
    assert small_api_text.splitlines(keepends=True) == small_lines
    assert large_api_bytes < small_api_bytes * 10
    assert large_api_bytes < 1_000_000
    assert large_api_bytes < large_size, "the live handler read the ENTIRE grown transcript"

    # -- 3. the hub's server-side renderer, live AND dead-run fallback ----
    #
    # The live branch's `_proxy_text` stand-in mirrors the real container's
    # `GET /logs?tail=N` contract by reading the SAME on-disk merge the
    # live ASGI handler above already reads from -- `_merge_logs` inside
    # `engine/api.py`'s own `/logs` route calls exactly this same
    # `log_merge.merged_lines` (`test_log_merge.py::
    # test_api_and_on_disk_merge_are_identical` pins the two byte-
    # identical), and doing it synchronously here (rather than re-entering
    # the ASGI app from inside a callback the hub renderer calls
    # synchronously, while this very test is itself an `async def` already
    # running inside pytest-asyncio's own event loop) needs no second event
    # loop at all.
    def _wire_hub(mp, root, live):
        def _proxy(reg, run_id, path, **kw):
            if not live:
                return False, ""
            import re
            m = re.search(r"tail=(\d+)", path)
            tail = int(m.group(1)) if m else 0
            return True, "".join(merged_lines(root, tail=tail))
        mp.setattr(ui_server, "_proxy_text", _proxy)

    for live in (False, True):
        with monkeypatch.context() as m:
            _wire_hub(m, small_root, live)
            small_hub_counts = _count_transcript_bytes(m)
            small_hub_live, small_hub_lines = ui_server.rendered_log_lines(
                small_reg, "probe", 200)
            small_hub_bytes = small_hub_counts[0]

        with monkeypatch.context() as m:
            _wire_hub(m, large_root, live)
            large_hub_counts = _count_transcript_bytes(m)
            large_hub_live, large_hub_lines = ui_server.rendered_log_lines(
                large_reg, "probe", 200)
            large_hub_bytes = large_hub_counts[0]

        assert small_hub_live == live and large_hub_live == live
        assert len(small_hub_lines) == 200 and len(large_hub_lines) == 200
        assert small_hub_lines == large_hub_lines
        assert large_hub_bytes < small_hub_bytes * 10
        assert large_hub_bytes < 1_000_000
        assert large_hub_bytes < large_size, (
            f"hub renderer (live={live}) read the ENTIRE grown transcript")

