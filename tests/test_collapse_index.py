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
        # every flag's setter offset landed strictly inside the transcript,
        # each one recorded once and never at the very first byte (0) --
        # this transcript's setters are the bash tool call, the streamed
        # text, and the thinking burst, in that order.
        assert idx["toolcallSeenAtByte"] == 0
        assert 0 < idx["textSeenAtByte"] < idx["thinkingSeenAtByte"]
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
