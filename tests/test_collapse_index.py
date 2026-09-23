"""Task 020 (PRD G.1/G.2/G.4/G.7/G.10, #214): the engine-owned side index
recording prefix-collapse state at iteration boundaries
(`ralphd.collapse_index`).

Four properties, one test class each:

* the WRITE only happens on the engine's own iteration-boundary
  bookkeeping (`LoopSupervisor._run_iteration_once`'s end-of-iteration
  write), never on any render path -- `TestWriteNeverOnRenderPath`;
* the per-iteration transcript file's own schema is untouched, and the
  index is a genuinely separate file -- `TestTranscriptSchemaUntouched`;
* the index is rebuildable from the transcript alone, and a reader falls
  back to the existing scan (never an error, never a wrong render) on
  absence, truncation, and a consistency-check mismatch --
  `TestRebuildableAndFallbackSafe`;
* a run recorded by an earlier engine, with no index at all, renders
  exactly as it does today -- `TestNoIndexRendersLikeToday`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from tests.conftest import supervisor_with_runner

from ralphd import collapse_index
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.engine.runner import IterationResult
from ralphd.log_merge import merged_lines


def _line(ev: dict) -> str:
    return json.dumps(ev)


# A single iteration's transcript: an early tool call, some streamed text,
# a thinking burst, then a second tool call -- enough to exercise every one
# of the three collapse-guard flags at a known, non-zero byte offset each.
def _sample_transcript() -> str:
    return "\n".join([
        _line({"type": "tool_execution_start", "toolCallId": "t1",
              "toolName": "bash", "args": {"command": "echo hi"}}),
        _line({"type": "tool_execution_end", "toolCallId": "t1",
              "toolName": "bash", "result": "hi", "isError": False}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": "hello "}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": "world"}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "contentIndex": 0, "content": "hello world"}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "thinking_start", "contentIndex": 1}}),
        _line({"type": "tool_execution_start", "toolCallId": "t2",
              "toolName": "read", "args": {"path": "/tmp/x"}}),
        _line({"type": "tool_execution_end", "toolCallId": "t2",
              "toolName": "read", "result": "ok", "isError": False}),
    ]) + "\n"


class _ScriptedRunner:
    """Writes a fixed transcript to the iteration's own output file and
    returns a clean result -- the seam `_run_iteration_once` awaits."""

    def __init__(self, transcript: str):
        self.transcript = transcript

    async def run(self, prompt, transcript_path, **kw) -> IterationResult:
        Path(transcript_path).write_text(self.transcript)
        r = IterationResult(exit_code=0)
        r.final_text = "did some work"
        r.usage = {"totalTokens": 3}
        return r


_supervisor = supervisor_with_runner(
    lambda sup, transcript: _ScriptedRunner(transcript),
    run_id="unit", cfg_defaults=False, iterations=4, max_approaches=1,
    vigilant=False, on_complete="exit")


def _run_one_iteration(tmp_path, transcript: str = None):
    sup = _supervisor(tmp_path, transcript or _sample_transcript())
    asyncio.run(sup._run_iteration_once("worker"))
    return sup.run.iteration_dir(1)


# -- write only on the engine's own boundary bookkeeping --------------------


class TestWriteNeverOnRenderPath:
    def test_a_finished_iteration_gets_a_side_index_file(self, tmp_path):
        itdir = _run_one_iteration(tmp_path)
        idx_path = itdir / collapse_index.INDEX_FILENAME
        assert idx_path.exists()
        idx = json.loads(idx_path.read_text())
        # This transcript carries no reset line (no `message_start`, no
        # boundary), so it is exactly ONE segment, opening at byte 0, and
        # every flag's setter offset landed strictly inside the file: the
        # bash call's `tool_execution_end`, then the streamed text, then
        # the thinking burst, in that order.
        assert idx["version"] == collapse_index.INDEX_VERSION
        assert len(idx["segments"]) == 1
        seg = idx["segments"][0]
        assert seg["startByte"] == 0
        assert 0 < seg["toolcallSeenAtByte"] < seg["textSeenAtByte"] \
            < seg["thinkingSeenAtByte"]
        assert idx["transcriptBytes"] == len((itdir / "output.jsonl").read_bytes())

    def test_rendering_repeatedly_never_writes_or_touches_the_index(
            self, tmp_path, monkeypatch):
        itdir = _run_one_iteration(tmp_path)
        idx_path = itdir / collapse_index.INDEX_FILENAME
        before = idx_path.read_bytes()
        before_mtime = idx_path.stat().st_mtime_ns

        # Belt and suspenders: if any render path ever called the writer,
        # this would raise instead of silently re-writing the same bytes.
        def _boom(*a, **kw):
            raise AssertionError("a render path called collapse_index.write_index")
        monkeypatch.setattr(collapse_index, "write_index", _boom)

        for _ in range(5):
            merged_lines(tmp_path)
            state = new_render_state()
            render_to_lines((itdir / "output.jsonl").read_text(), False, state)

        assert idx_path.read_bytes() == before
        assert idx_path.stat().st_mtime_ns == before_mtime

    def test_log_render_and_log_merge_never_import_collapse_index(self):
        """Structural pin, independent of the monkeypatch above: neither
        render module references the write module's name at all, so a
        future edit to either can't accidentally start calling it from a
        render path without touching this module's own import line."""
        import ralphd.cli.log_render as log_render
        import ralphd.log_merge as log_merge
        assert "collapse_index" not in vars(log_render)
        assert "collapse_index" not in vars(log_merge)


# -- output.jsonl's own schema is untouched ----------------------------------


class TestTranscriptSchemaUntouched:
    def test_the_recorded_transcript_is_byte_for_byte_what_the_agent_wrote(
            self, tmp_path):
        transcript = _sample_transcript()
        itdir = _run_one_iteration(tmp_path, transcript)
        assert (itdir / "output.jsonl").read_text() == transcript

    def test_the_index_is_a_separate_file_next_to_the_transcript(self, tmp_path):
        itdir = _run_one_iteration(tmp_path)
        assert collapse_index.INDEX_FILENAME != "output.jsonl"
        assert (itdir / collapse_index.INDEX_FILENAME).exists()
        assert (itdir / "output.jsonl").exists()
        assert (itdir / collapse_index.INDEX_FILENAME) != (itdir / "output.jsonl")


# -- rebuildable from the transcript alone, and a safe fallback -------------


class TestRebuildableAndFallbackSafe:
    def test_a_fresh_rebuild_from_only_the_transcript_matches_the_written_index(
            self, tmp_path):
        itdir = _run_one_iteration(tmp_path)
        written = json.loads((itdir / collapse_index.INDEX_FILENAME).read_text())
        rebuilt = collapse_index.compute_collapse_index(itdir / "output.jsonl")
        assert rebuilt == {k: written[k] for k in rebuilt}

    def _expected_flags(self, transcript: str, cut_offset: int) -> dict:
        """The plain, obviously-correct answer: render the WHOLE transcript
        and read off `new_render_state()`'s flags right after the boundary
        line that ends at `cut_offset` -- computed independently of
        `collapse_index` itself, by feeding the renderer only the prefix
        bytes and inspecting its own mutated state."""
        state = new_render_state()
        prefix = transcript.encode("utf-8")[:cut_offset].decode(
            "utf-8", errors="replace")
        render_to_lines(prefix, False, state)
        return {"textSeen": state["text_seen"],
                "thinkingSeen": state["thinking_seen"],
                "toolcallSeen": state["toolcall_seen"]}

    @pytest.mark.parametrize("scenario", ["valid_index", "absent", "truncated",
                                          "size_mismatch"])
    def test_absence_truncation_and_mismatch_all_fall_back_and_still_render_correctly(
            self, tmp_path, scenario):
        transcript = _sample_transcript()
        itdir = _run_one_iteration(tmp_path, transcript)
        idx_path = itdir / collapse_index.INDEX_FILENAME
        assert idx_path.exists()  # the engine wrote it; now perturb it

        if scenario == "absent":
            idx_path.unlink()
        elif scenario == "truncated":
            raw = idx_path.read_bytes()
            idx_path.write_bytes(raw[: len(raw) // 2])  # cuts the JSON mid-token
        elif scenario == "size_mismatch":
            idx = json.loads(idx_path.read_text())
            idx["transcriptBytes"] += 1  # stale relative to the real file now
            idx_path.write_text(json.dumps(idx))
        # else "valid_index": leave it exactly as the engine wrote it

        # A real cut point always lands on a LINE boundary (a bounded
        # reader only ever keeps whole NDJSON lines) -- here, right before
        # the last two lines of the transcript (the second tool call).
        raw_lines = transcript.splitlines(keepends=True)
        kept_suffix_lines = raw_lines[-2:]
        cut_offset = len(transcript.encode("utf-8")) - sum(
            len(l.encode("utf-8")) for l in kept_suffix_lines)
        flags = collapse_index.resolve_flags_at_offset(itdir, cut_offset)
        assert flags == self._expected_flags(transcript, cut_offset)

        # and the actual renderer, seeded from those flags, renders the
        # retained suffix exactly like a full render's own matching tail --
        # never an error, never a wrong render, in any of the four scenarios.
        seed_state = new_render_state()
        seed_state.update(text_seen=flags["textSeen"],
                          thinking_seen=flags["thinkingSeen"],
                          toolcall_seen=flags["toolcallSeen"])
        suffix = "".join(kept_suffix_lines)
        seeded_lines = render_to_lines(suffix, False, seed_state)

        full_state = new_render_state()
        full_lines = render_to_lines(transcript, False, full_state)
        assert seeded_lines == full_lines[-len(seeded_lines):]


# -- an earlier engine's run, with no index at all, renders like today ------


class TestNoIndexRendersLikeToday:
    def test_a_run_with_no_index_file_renders_exactly_as_a_run_with_one(
            self, tmp_path):
        transcript = _sample_transcript()
        with_index = _run_one_iteration(tmp_path / "with-index", transcript)
        without_index_root = tmp_path / "without-index"
        sup = _supervisor(without_index_root, transcript)
        # Simulate "an earlier engine" by never letting the write happen at
        # all, rather than writing then deleting -- proves the render path
        # never depended on this task's write ever having run.
        import ralphd.engine.loop as loop_mod
        orig = loop_mod.collapse_index.write_index
        loop_mod.collapse_index.write_index = lambda *a, **kw: None
        try:
            asyncio.run(sup._run_iteration_once("worker"))
        finally:
            loop_mod.collapse_index.write_index = orig
        without_index = sup.run.iteration_dir(1)

        assert not (without_index / collapse_index.INDEX_FILENAME).exists()
        assert (with_index / collapse_index.INDEX_FILENAME).exists()

        lines_with = merged_lines(tmp_path / "with-index")
        lines_without = merged_lines(without_index_root)
        # Both runs share the identical transcript and meta shape (same
        # scripted result) -- only the presence of the side index differs,
        # and the rendered transcript is byte-identical either way.
        assert lines_with == lines_without


# -- the flags are NOT monotone: every message_start resets all three -------


def _multi_message_transcript() -> str:
    """One iteration, TWO assistant messages -- the shape task 019 (PRD
    G.8) fixed in the renderer and the shape a monotone, one-offset-per-
    flag index gets wrong. Message 1 streams its text ("first") and makes
    a tool call; message 2 does NOT stream at all, carrying its text
    ("second") only in its own `message_end`. A full render prints both
    texts; an index that thinks `text_seen` stays true for the rest of the
    iteration makes a bounded reader seed message 2 with `textSeen=True`,
    and `_render_message_end` then prints nothing for it."""
    return "\n".join([
        _line({"type": "message_start", "message": {"role": "assistant"}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": "first"}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "contentIndex": 0, "content": "first"}}),
        _line({"type": "message_update", "assistantMessageEvent": {
            "type": "thinking_start", "contentIndex": 1}}),
        _line({"type": "tool_execution_start", "toolCallId": "t1",
              "toolName": "bash", "args": {"command": "echo hi"}}),
        _line({"type": "tool_execution_end", "toolCallId": "t1",
              "toolName": "bash", "result": "hi", "isError": False}),
        _line({"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "first"}]}}),
        _line({"type": "message_start", "message": {"role": "assistant"}}),
        _line({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "second"},
                        {"type": "thinking", "thinking": "hmm"},
                        {"type": "toolCall", "name": "read",
                         "arguments": {"path": "/tmp/x"}}]}}),
    ]) + "\n"


def _render_probe(transcript: str, cut_offset: int) -> tuple[list[str], dict]:
    """`(lines, state)` from rendering ONLY the transcript's first
    `cut_offset` bytes through the real renderer -- the independent,
    obviously-correct answer this module's index and fallback scan are
    measured against, computed without touching `collapse_index` at all."""
    state = new_render_state()
    prefix = transcript.encode("utf-8")[:cut_offset].decode(
        "utf-8", errors="replace")
    return render_to_lines(prefix, False, state), state


def _line_offsets(transcript: str) -> list[int]:
    """Every byte offset a real cut point can land on: the start of each
    line, plus end-of-file (a bounded reader only ever keeps whole NDJSON
    lines)."""
    offsets, at = [], 0
    for raw_line in transcript.encode("utf-8").splitlines(keepends=True):
        offsets.append(at)
        at += len(raw_line)
    offsets.append(at)
    return offsets


def _perturb(idx_path: Path, scenario: str) -> None:
    if scenario == "valid_index":
        return
    if scenario == "absent":
        idx_path.unlink()
    elif scenario == "truncated":
        raw = idx_path.read_bytes()
        idx_path.write_bytes(raw[: len(raw) // 2])  # cuts the JSON mid-token
    elif scenario == "size_mismatch":
        idx = json.loads(idx_path.read_text())
        idx["transcriptBytes"] += 1  # stale relative to the real file now
        idx_path.write_text(json.dumps(idx))
    elif scenario == "unknown_version":
        idx = json.loads(idx_path.read_text())
        idx["version"] = collapse_index.INDEX_VERSION + 7
        idx_path.write_text(json.dumps(idx))
    else:  # pragma: no cover - a typo in a parametrization must not pass
        raise AssertionError(f"unknown scenario {scenario!r}")


_SCENARIOS = ["valid_index", "absent", "truncated", "size_mismatch",
              "unknown_version"]


class TestFlagsResetAtEveryMessageStart:
    """The cure for task 020's first rejection: a valid multi-message
    transcript. `render_log_line` resets all three collapse guards at
    `message_start` (task 019, PRD G.8), so neither the index nor the
    fallback scan may treat them as monotone within one iteration."""

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    def test_the_flags_match_the_renderer_at_every_cut_point(
            self, tmp_path, scenario):
        transcript = _multi_message_transcript()
        itdir = _run_one_iteration(tmp_path, transcript)
        _perturb(itdir / collapse_index.INDEX_FILENAME, scenario)
        # the parametrization is not vacuous: only the untouched index is
        # actually trusted, the other four really do fall back to the scan
        assert (collapse_index.load_index(itdir) is not None) == (
            scenario == "valid_index")
        for cut in _line_offsets(transcript):
            _lines, state = _render_probe(transcript, cut)
            expected = {"textSeen": state["text_seen"],
                        "thinkingSeen": state["thinking_seen"],
                        "toolcallSeen": state["toolcall_seen"]}
            got = collapse_index.resolve_flags_at_offset(itdir, cut)
            assert got == expected, f"{scenario} disagreed at byte {cut}"

    def test_the_second_message_starts_with_text_unseen(self, tmp_path):
        """The single offset the first attempt got wrong, pinned on its
        own: immediately after the SECOND `message_start`, nothing of
        message 2 has been shown, so `textSeen` is False -- even though
        message 1 streamed text earlier in the same iteration."""
        transcript = _multi_message_transcript()
        itdir = _run_one_iteration(tmp_path, transcript)
        raw = transcript.encode("utf-8").splitlines(keepends=True)
        second_start = [i for i, l in enumerate(raw)
                        if json.loads(l).get("type") == "message_start"][1]
        cut = sum(len(l) for l in raw[:second_start + 1])
        assert collapse_index.resolve_flags_at_offset(itdir, cut) == {
            "textSeen": False, "thinkingSeen": False, "toolcallSeen": False}

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    def test_a_seeded_render_of_the_suffix_completes_the_full_render(
            self, tmp_path, scenario):
        """End to end, on the rendered bytes rather than the flags: at
        every cut point where the renderer holds no half-finished state
        besides the three collapse flags (no open text block, no open tool
        call), a fresh state seeded from `resolve_flags_at_offset` renders
        the retained suffix into exactly the lines a full render produces
        after the prefix's own -- in all five index scenarios, never an
        error and never a wrong render."""
        transcript = _multi_message_transcript()
        itdir = _run_one_iteration(tmp_path, transcript)
        _perturb(itdir / collapse_index.INDEX_FILENAME, scenario)
        full_lines, _full_state = _render_probe(
            transcript, len(transcript.encode("utf-8")))
        assert full_lines[0] == "first" and "second" in full_lines
        checked = 0
        for cut in _line_offsets(transcript):
            prefix_lines, state = _render_probe(transcript, cut)
            if state["text_open"] or state["tool_args"] or state["tool_pending"]:
                continue  # mid-stream cut: not a seedable restart point
            flags = collapse_index.resolve_flags_at_offset(itdir, cut)
            seeded = new_render_state()
            seeded.update(text_seen=flags["textSeen"],
                          thinking_seen=flags["thinkingSeen"],
                          toolcall_seen=flags["toolcallSeen"])
            suffix = transcript.encode("utf-8")[cut:].decode("utf-8")
            assert prefix_lines + render_to_lines(suffix, False, seeded) \
                == full_lines, f"{scenario} rendered wrong from byte {cut}"
            checked += 1
        assert checked >= 5  # the sweep really did exercise several cuts

    def test_the_index_records_one_segment_per_message(self, tmp_path):
        transcript = _multi_message_transcript()
        itdir = _run_one_iteration(tmp_path, transcript)
        idx = json.loads(
            (itdir / collapse_index.INDEX_FILENAME).read_text())
        # One segment per `message_start`: this transcript opens with one,
        # so byte 0's implicit segment is that same segment rather than a
        # second one at the same offset (`startByte`s ascend strictly).
        assert len(idx["segments"]) == 2
        assert [s["startByte"] for s in idx["segments"]] == sorted(
            s["startByte"] for s in idx["segments"])
        # message 2 (the last segment) sets nothing at all: it streams
        # nothing, and its `message_end` is not a setter for any flag.
        assert idx["segments"][-1]["textSeenAtByte"] is None
        assert idx["segments"][-1]["thinkingSeenAtByte"] is None
        assert idx["segments"][-1]["toolcallSeenAtByte"] is None

    def test_a_cut_boundary_carrying_a_collapse_seed_is_honoured(self, tmp_path):
        """`log_merge.merge_entries_tail` synthesizes a `ralphd.iteration`
        start line carrying a `collapseSeed` for a window that cuts INTO an
        iteration, and `render_log_line` seeds the three flags from it
        instead of resetting them to False. The index and the fallback scan
        mirror that, so a transcript blob containing such a boundary
        resolves the same way the renderer does."""
        blob = "\n".join([
            _line({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "contentIndex": 0, "delta": "x"}}),
            _line({"type": "ralphd.iteration", "event": "start", "n": 2,
                   "collapseSeed": {"textSeen": True, "thinkingSeen": False,
                                    "toolcallSeen": True}}),
            _line({"type": "message_end", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "kept"}]}}),
        ]) + "\n"
        itdir = tmp_path / "iter"
        itdir.mkdir()
        (itdir / "output.jsonl").write_text(blob)
        collapse_index.write_index(itdir)
        assert collapse_index.load_index(itdir) is not None
        for cut in _line_offsets(blob):
            _lines, state = _render_probe(blob, cut)
            expected = {"textSeen": state["text_seen"],
                        "thinkingSeen": state["thinking_seen"],
                        "toolcallSeen": state["toolcall_seen"]}
            assert collapse_index.resolve_flags_at_offset(itdir, cut) == expected
            (itdir / collapse_index.INDEX_FILENAME).rename(
                itdir / "stashed.json")
            assert collapse_index.resolve_flags_at_offset(itdir, cut) == expected
            (itdir / "stashed.json").rename(
                itdir / collapse_index.INDEX_FILENAME)

    def test_resolution_does_not_read_the_transcript_when_the_index_is_valid(
            self, tmp_path, monkeypatch):
        """The index's whole point (G.3/G.6): with a valid index, the
        answer costs a binary search over the segments and no re-read of
        the transcript at any distance -- so a deciding setter 100x
        further back costs the same, and the transcript file is not opened
        for reading at all beyond `load_index`'s own size stat."""
        transcript = _multi_message_transcript()
        itdir = _run_one_iteration(tmp_path, transcript)
        opened: list[str] = []
        real_open = Path.open

        def _watch(self, *a, **kw):
            opened.append(self.name)
            return real_open(self, *a, **kw)

        monkeypatch.setattr(Path, "open", _watch)
        collapse_index.resolve_flags_at_offset(
            itdir, len(transcript.encode("utf-8")))
        assert "output.jsonl" not in opened
