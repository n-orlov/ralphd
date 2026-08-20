"""On-disk iteration-transcript merge, shared by the engine and the host CLI
(task 038, #6).

A run's transcript is not one file: it is `iterations/NNNN/output.jsonl` per
iteration plus the surrounding `meta.json`, and the *rendered* log is those
transcripts concatenated in iteration order with a synthesized
`ralphd.iteration` boundary line (`event: "start"` / `"end"`) around each
one. That merge used to live inside `engine/api.py`'s `GET /logs` closure,
which meant the host side (`ralphctl logs`, the hub's log tail) could only
see a transcript while the run's container was alive to serve it.

This module is the single implementation. `engine/api.py` imports it for the
snapshot part of `GET /logs` and keeps only its *live follow* logic on top
(tailing the newest `output.jsonl`, waiting for the next iteration dir);
host-side readers call `merged_lines()` directly against the run dir. Both
paths therefore emit byte-identical lines for the same run dir (pinned by
tests/test_log_merge.py).

Scrubbing is injected, not assumed: the engine passes
`engine.redact.scrub_text` so serving re-scrubs with whatever the redaction
set currently is (defense-in-depth on top of the write-time scrub in
`runner.py`). Host-side callers pass nothing and get the bytes as written --
see docs/architecture.md's redaction section for that decision.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

# Type of a rendered entry: (is_boundary, line-with-trailing-newline).
Entry = tuple[bool, str]

BOUNDARY_TYPE = "ralphd.iteration"

# Rendered when a run dir has no iteration transcripts at all (task 041 uses
# the same wording on every surface).
NO_TRANSCRIPT = "(no transcript yet)"


# The run dir's per-iteration layout, spelled in ONE place: `<run>/iterations/
# NNNN/` holding `prompt.md`, `output.jsonl` and `meta.json`. Every reader of a
# single iteration (this module's `iteration_lines`, `engine.state.
# iteration_detail` for the CLI/hub iteration-detail views, task 019) goes
# through the two helpers below rather than re-spelling the zero padding.
ITERATIONS_DIR = "iterations"


def iteration_dirs(run_root: Path) -> list[Path]:
    """Iteration directories in execution order (`0001`, `0002`, ...)."""
    itroot = Path(run_root) / ITERATIONS_DIR
    if not itroot.exists():
        return []
    return sorted(itroot.iterdir())


def iteration_dir(run_root: Path, number: int) -> Path:
    """The directory holding iteration `number`'s transcript and `meta.json`
    (not necessarily existing -- callers check)."""
    return Path(run_root) / ITERATIONS_DIR / f"{number:04d}"


def iteration_output_path(run_root: Path, number: int) -> Path:
    """Where iteration `number`'s raw transcript lives. Which file *is* the
    transcript is this module's business (see the grep guard in
    tests/test_log_merge.py), so readers that only need its size or existence
    -- `engine.state.iteration_detail`'s `hasTranscript`/`transcriptBytes` --
    ask here instead of spelling the name a second time."""
    return iteration_dir(run_root, number) / "output.jsonl"


def iteration_numbers(run_root: Path) -> list[int]:
    """The iteration numbers a run dir actually holds, in execution order.

    Derived from the directory names (`0001` -> 1), not from any index: the
    dirs ARE the record. A name that is not a number is ignored rather than
    raising, so a stray file in `iterations/` cannot break a reader (task 019,
    which uses this to tell an operator which iterations exist).
    """
    out = []
    for d in iteration_dirs(run_root):
        try:
            out.append(int(d.name))
        except ValueError:
            continue
    return out


def boundary_line(meta: dict, event: str) -> str:
    """Synthesize the `ralphd.iteration` start/end line for one iteration.

    Not written to disk by anyone: it is derived from `meta.json` so that a
    reader can tell iterations apart in a flat stream of transcript lines.
    """
    line: dict = {"type": BOUNDARY_TYPE, "event": event,
                  "number": meta.get("number"), "phase": meta.get("phase"),
                  "model": meta.get("model"), "approach": meta.get("approach"),
                  "startedAt": meta.get("startedAt")}
    if event == "end":
        line["exitCode"] = meta.get("exitCode")
        line["error"] = meta.get("error")
        line["usage"] = meta.get("usage")
        line["endedAt"] = meta.get("endedAt")
    return json.dumps(line) + "\n"


def merge_entries(run_root: Path,
                  scrub: Callable[[str], str] | None = None) -> Iterator[Entry]:
    """Yield `(is_boundary, line)` for every iteration transcript in order.

    An iteration without a readable `meta.json` is skipped (it is being
    created right now, or was truncated by a crash); the `end` boundary is
    emitted only once `endedAt` is recorded, so a live iteration renders
    open-ended.
    """
    scrub = scrub or (lambda text: text)
    for d in iteration_dirs(run_root):
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        yield True, scrub(boundary_line(meta, "start"))
        out = d / "output.jsonl"
        if out.exists():
            for line in out.read_text().splitlines(keepends=True):
                line = line if line.endswith("\n") else line + "\n"
                yield False, scrub(line)
        if meta.get("endedAt"):
            yield True, scrub(boundary_line(meta, "end"))


def apply_tail(entries: list[Entry], tail: int) -> list[Entry]:
    """Keep only the last `tail` non-boundary (transcript) lines, plus any
    boundary lines that fall within that window."""
    if not tail:
        return entries
    selected: list[Entry] = []
    content_count = 0
    for is_boundary, line in reversed(entries):
        selected.append((is_boundary, line))
        if not is_boundary:
            content_count += 1
            if content_count >= tail:
                break
    return list(reversed(selected))


def merged_lines(run_root: Path, tail: int = 0,
                 scrub: Callable[[str], str] | None = None) -> list[str]:
    """The rendered transcript of `run_root` as newline-terminated lines --
    exactly what `GET /logs?tail=N` serves for the same run dir."""
    return [line for _, line in
            apply_tail(list(merge_entries(run_root, scrub=scrub)), tail)]


def iteration_lines(run_root: Path, number: int, tail: int = 0,
                    scrub: Callable[[str], str] | None = None) -> list[str]:
    """One iteration's raw transcript lines -- what `GET
    /iterations/{n}/output?tail=N` serves for the same run dir (no
    synthesized boundaries: that route is the per-iteration passthrough).

    Lives here rather than in the host CLI so that reading a run's
    `output.jsonl` files stays the single responsibility of this module
    (task 040, #6; pinned by tests/test_log_merge.py's grep test).
    Returns `[]` for an iteration that has no transcript on disk.
    """
    scrub = scrub or (lambda text: text)
    path = iteration_output_path(run_root, number)
    if not path.exists():
        return []
    lines = [scrub(line if line.endswith("\n") else line + "\n")
             for line in path.read_text().splitlines(keepends=True)]
    return lines[-tail:] if tail else lines
