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

# The two per-iteration file names, spelled ONCE (task 020's #81 grep guard,
# tests/test_log_merge.py, pins `output.jsonl` to this module and its four
# allowed readers -- a second literal elsewhere would be a second place that
# could drift if the name ever changed).
OUTPUT_FILENAME = "output.jsonl"
PROMPT_FILENAME = "prompt.md"


# The run dir's per-iteration layout, spelled in ONE place: `<run>/iterations/
# NNNN/` holding `prompt.md`, `output.jsonl` and `meta.json`. Every reader of a
# single iteration (this module's `iteration_lines`, `engine.state.
# iteration_detail` for the CLI/hub iteration-detail views, task 019) goes
# through the two helpers below rather than re-spelling the zero padding.
ITERATIONS_DIR = "iterations"

# A slot can be attempted twice: an engine killed mid-iteration leaves a
# half-written `iterations/NNNN/` behind, and the resumed engine reuses that
# number (`RunDir.max_iteration_number()` counts only finished iterations). The
# crashed attempt's files are moved into `iterations/NNNN/attempts/NN/` first --
# same 2-digit, oldest-first shape as `approaches/NN/` -- so nothing the dead
# process recorded is overwritten (task 019, #44). Spelled here with the rest of
# the layout; `engine.state` archives and counts through the helper below.
ITERATION_ATTEMPTS_DIR = "attempts"


def iteration_attempt_dirs(run_root: Path, number: int) -> list[Path]:
    """Archived earlier attempts at iteration `number`, oldest first (`[]` when
    the slot was only ever attempted once). A name that is not a number is
    ignored, like `iteration_numbers`: a stray file cannot break a reader."""
    root = iteration_dir(run_root, number) / ITERATION_ATTEMPTS_DIR
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and d.name.isdigit())


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
    return iteration_dir(run_root, number) / OUTPUT_FILENAME


def iteration_prompt_path(run_root: Path, number: int) -> Path:
    """Where iteration `number`'s prompt lives -- the `iteration_output_path`
    role for the "input" region of task 020's iteration detail (#81):
    `engine.state.iteration_detail`'s `hasPrompt`/`promptBytes` and
    `read_iteration_prompt` ask here instead of spelling the name a second
    time. Written once by `engine.loop._run_iteration_once`, never rewritten."""
    return iteration_dir(run_root, number) / PROMPT_FILENAME


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


# Grown one doubling step at a time from this floor (task 005, PRD E): big
# enough that a normal iteration's whole transcript is read in one shot (no
# doubling at all -- the common case costs exactly one `read` syscall), small
# enough that reading this much off the tail of a truly enormous single
# iteration is still a rounding error next to the bug this bounds (a full
# `read_text()` of a multi-megabyte file for a 200-line tail).
_TAIL_READ_CHUNK = 1 << 16


def _tail_lines_of_file(path: Path, needed: int) -> list[str]:
    """The last `needed` newline-terminated lines of `path`, read from the
    END of the file in growing chunks -- never the whole file -- so bytes
    read are bounded by `needed`, not by how much unrelated OLDER content
    happens to sit earlier in the same file (task 005, PRD E: two on-disk
    runs with an identical rendered 200-line suffix cost 22,722 vs
    5,281,670 `output.jsonl` bytes read, because the newest iteration's
    WHOLE transcript was `read_text()`-ed regardless of how much of it the
    requested tail actually needed).

    Reads in binary mode and only decodes once a run of whole lines has
    been isolated: every chunk boundary this function ever stops at lands
    immediately after a `\\n` byte (or at byte 0), and `\\n` is itself
    always a line boundary in UTF-8, so a multi-byte character can never be
    split by where a chunk starts.
    """
    if needed <= 0:
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        chunk = _TAIL_READ_CHUNK
        data = b""
        while pos > 0 and data.count(b"\n") < needed:
            read_size = min(chunk, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
            chunk *= 2
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    return lines[-needed:] if len(lines) > needed else lines


def merge_entries_tail(run_root: Path, tail: int,
                       scrub: Callable[[str], str] | None = None) -> list[Entry]:
    """Like `merge_entries`, but reads only as many of the NEWEST iteration
    directories as are needed to reach `tail` content (non-boundary) lines,
    instead of every iteration the run has ever recorded (task 048, PRD
    H.6).

    `merge_entries` is a plain generator over `iteration_dirs` in execution
    order, oldest first -- every caller that wants a tail (`merged_lines`,
    `engine.api`'s `GET /logs?tail=N`) had to force it to a full list
    (`list(merge_entries(...))`) before `apply_tail` could trim from the
    END, so a tiny tail on a run with thousands of iterations still opened
    and read every `output.jsonl` on disk. This function walks the SAME
    `iteration_dirs` list in REVERSE (newest first) instead, reading one
    iteration's `meta.json` + `output.jsonl` per step -- one "iteration
    transcript read" in H.6's own words -- and stops the moment it has
    collected at least `tail` content lines (or runs out of iterations).
    `apply_tail` still does the exact trim afterwards, so a step that
    overshoots (one iteration alone can hold more than `tail` lines) still
    yields exactly the same lines full `merge_entries` + `apply_tail` would
    have, because only STRICTLY OLDER iterations are ever left unread.

    Task 005 (PRD E): the per-iteration read itself is now bounded too --
    `_tail_lines_of_file` reads only the `tail - content_count` lines this
    step still needs off the END of that iteration's `output.jsonl`,
    instead of the whole file, so unrelated OLDER raw entries sharing the
    NEWEST iteration (growth `apply_tail` would have discarded anyway) are
    never read off disk at all.
    """
    scrub = scrub or (lambda text: text)
    newest_first_chunks: list[list[Entry]] = []
    content_count = 0
    for d in reversed(iteration_dirs(run_root)):
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        chunk: list[Entry] = []
        if meta.get("endedAt"):
            chunk.append((True, scrub(boundary_line(meta, "end"))))
        out = d / OUTPUT_FILENAME
        if out.exists():
            remaining = max(tail - content_count, 1)
            tail_lines = _tail_lines_of_file(out, remaining)
            for line in reversed(tail_lines):
                line = line if line.endswith("\n") else line + "\n"
                chunk.append((False, scrub(line)))
            content_count += len(tail_lines)
        chunk.append((True, scrub(boundary_line(meta, "start"))))
        chunk.reverse()
        newest_first_chunks.append(chunk)
        if content_count >= tail:
            break
    out_entries: list[Entry] = []
    for chunk in reversed(newest_first_chunks):
        out_entries.extend(chunk)
    return out_entries


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
    exactly what `GET /logs?tail=N` serves for the same run dir.

    Task 048 (PRD H.6): a nonzero `tail` goes through `merge_entries_tail`,
    which reads only the iterations needed to satisfy it, instead of
    `merge_entries`'s every-iteration generator -- forcing the LATTER to a
    list (the shape `apply_tail` needs to trim from the end) is exactly
    what used to make this function's own `tail` a no-op on the READ cost,
    however small the request. `tail=0` (the whole transcript) still goes
    through the unbounded `merge_entries`, since there is no tail to bound
    the read by."""
    entries = merge_entries_tail(run_root, tail, scrub=scrub) if tail \
        else list(merge_entries(run_root, scrub=scrub))
    return [line for _, line in apply_tail(entries, tail)]


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
