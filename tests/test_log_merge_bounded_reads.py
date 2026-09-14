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
"""

from __future__ import annotations

import json
import pathlib

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
    """Every real read of an `output.jsonl` file -- one "iteration
    transcript read" in H.6's own words -- counted via a thin wrapper
    around `pathlib.Path.read_text`, filtered by filename so a `meta.json`
    read (needed for every candidate iteration regardless of the bound)
    never inflates the count this test is actually about."""
    counts = [0]
    real_read_text = pathlib.Path.read_text

    def counted(self, *args, **kwargs):
        if self.name == OUTPUT_FILENAME:
            counts[0] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", counted)
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
