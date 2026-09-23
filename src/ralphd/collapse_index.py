"""Engine-owned side index recording prefix-collapse state at iteration
boundaries (task 020, PRD G.1/G.2/G.4/G.7/G.10).

PRD G names the fact this module writes down: "has text already been shown
at this point, or is this new?" -- for `text_seen`/`thinking_seen`/
`toolcall_seen`, the three collapse guards `log_render.render_log_line`
and `log_merge._prior_collapse_state` both maintain, whose deciding setter
can sit past `log_merge`'s own per-call read budget (PRD P.1/P.5, 1,000,000
transcript bytes). G.1 authorises exactly one new side-index file so a
bounded reader can resolve the flag by O(1) lookup instead of by re-reading
the transcript; this module is that file's whole read/write surface.

Write side (`write_index`): called ONLY from `engine/loop.py`'s
iteration-boundary bookkeeping, once an iteration's own per-iteration
transcript file (`log_merge.OUTPUT_FILENAME`) is fully written and its
`meta.json` has recorded `endedAt` -- never from any render path
(`log_render.py`, this package's own `log_merge.py` readers). G.10's whole
point: rendering never writes the index, and a read-only consumer
(`ralphctl logs` against a finished run, the hub against someone else's
run dir) works with whatever index it finds, including none.

Read side (`load_index`): validates the on-disk file against the ACTUAL
transcript it claims to describe (size), and against this build's own
`INDEX_VERSION`, before trusting it at all. Absence,
truncation/malformed JSON, a mismatched consistency check or an unknown
version all return `None` -- the caller's signal to fall back to a full
scan of the transcript (G.4: never an error, never a wrong render, only
ever slower). `resolve_flags_at_offset` is the one fallback-safe
consumer this task ships: given a byte offset into one iteration's
transcript, it answers the three collapse flags AS OF that offset --
exactly what a fresh `log_render.new_render_state()` holds once the
renderer has consumed the transcript's first `cut_offset` bytes -- by a
binary search over the index's segments when the index validates, or by
scanning the transcript up to that offset when it does not.

The flags are NOT monotone within one iteration, which is the whole
reason this file records segments rather than one offset per flag:
`render_log_line` resets all three at every `message_start` (task 019,
PRD G.8) and at every `ralphd.iteration` boundary. Both the index and
the fallback scan take their transitions from the single `_transitions`
generator below, so they cannot disagree with each other; that generator
is the one place the renderer's dispatch is mirrored. G.6's growth-ladder replacement in the engine's two real render
paths (the `/logs` handler and the on-disk fallback) is task 021's job,
built on top of this same `resolve_flags_at_offset`/`load_index` pair.

G.7: the index is a SEPARATE file (`INDEX_FILENAME`, sitting beside
`meta.json` and the transcript inside one `iterations/NNNN/` dir) -- this
module never opens the transcript file for writing, so its own schema is
untouched by this task, at every call site.

The transcript filename itself is deliberately never spelled as a literal
string here (see `_OUTPUT_FILENAME` below, imported from `log_merge`
rather than re-typed) -- `tests/test_log_merge.py`'s own guard pins that
literal to `log_merge.py` and a short, named list of readers, and this
module has no business joining that list by accident.
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path

from .log_merge import OUTPUT_FILENAME as _OUTPUT_FILENAME

INDEX_FILENAME = "collapse_index.json"

# Bumped from 1 to 2 by task 020's cure: version 1 recorded ONE first-setter
# offset per flag for the whole file, which silently assumed the three flags
# only ever go from false to true inside one iteration. They do not --
# `log_render.render_log_line`'s `message_start` branch (task 019, PRD G.8)
# RESETS all three at every new assistant message, and its
# `ralphd.iteration` branch resets (or seeds) them too. A monotone
# first-setter index therefore answered `textSeen=true` for offsets inside a
# LATER message that had shown nothing yet, and a reader seeded from it
# dropped that message's un-streamed `message_end` text -- the exact defect
# G.8 just fixed, re-introduced through the index. Version 2 records one
# SEGMENT per reset instead (see `compute_collapse_index`), and `load_index`
# refuses any version it was not built to read, falling back to the scan.
INDEX_VERSION = 2

_REQUIRED_KEYS = ("version", "transcriptBytes", "segments")
_SEGMENT_KEYS = ("startByte", "textSeenAtByte", "thinkingSeenAtByte",
                 "toolcallSeenAtByte")

# flag name -> the segment key recording where it first fires in a segment
_FLAG_KEY = {"text": "textSeenAtByte", "thinking": "thinkingSeenAtByte",
             "toolcall": "toolcallSeenAtByte"}
_FLAG_OUT = {"text": "textSeen", "thinking": "thinkingSeen",
             "toolcall": "toolcallSeen"}


def _transitions(raw: bytes):
    """Yield `(line_start_offset, action)` for every line of `raw` that
    changes `log_render.render_log_line`'s three collapse guards, in file
    order. `action` is either `("set", <flag name>)` or `("reset", <dict
    of seeded flags>)`.

    This is the ONE place the renderer's own dispatch is mirrored, so the
    index (`compute_collapse_index`) and the fallback scan
    (`resolve_flags_at_offset`) can never drift apart from each other:

    * `message_update` / `text_delta` sets `text_seen`;
    * `message_update` / `thinking_start`|`thinking_delta` sets
      `thinking_seen`;
    * `tool_execution_end` sets `toolcall_seen`. A bare
      `tool_execution_start` deliberately does NOT: `render_log_line`
      sets the flag there only when `live=True`, and every consumer of
      this index renders buffered (`render_to_lines`, `live=False`) --
      the hub's `/logs` rendering and `ralphctl logs`' backlog alike --
      so a `True` here between a start and its matching end would
      suppress a `toolCall` line a full render does print. (This is the
      one deliberate difference from `log_merge._prior_collapse_state`,
      which sets the flag on either event; that function's own callers
      are task 021/G.6's business, not this module's.)
    * `message_start` RESETS all three (task 019, PRD G.8: a brand new
      assistant message has shown nothing yet);
    * `ralphd.iteration` resets all three too, seeded from a cut "start"
      boundary's `collapseSeed` when it carries one -- byte-for-byte the
      state transition `render_log_line`'s boundary branch applies.

    A line that does not parse, or parses to something other than a JSON
    object, changes nothing -- exactly like the renderer's own
    unrecognized-event handling.
    """
    offset = 0
    for raw_line in raw.splitlines(keepends=True):
        stripped = raw_line.strip()
        if stripped:
            try:
                ev = json.loads(stripped)
            except (json.JSONDecodeError, UnicodeDecodeError):
                ev = None
            if isinstance(ev, dict):
                etype = ev.get("type")
                if etype == "message_update":
                    sub = (ev.get("assistantMessageEvent") or {}).get("type")
                    if sub == "text_delta":
                        yield offset, ("set", "text")
                    elif sub in ("thinking_start", "thinking_delta"):
                        yield offset, ("set", "thinking")
                elif etype == "tool_execution_end":
                    yield offset, ("set", "toolcall")
                elif etype == "message_start":
                    yield offset, ("reset", {})
                elif etype == "ralphd.iteration":
                    seed = (ev.get("collapseSeed")
                            if ev.get("event") == "start" else None)
                    yield offset, ("reset", seed or {})
        offset += len(raw_line)


def compute_collapse_index(output_path: Path) -> dict:
    """Scan one iteration's ENTIRE transcript file exactly once and return
    the side-index payload.

    Shape (version 2): `segments`, one entry per stretch of the file over
    which the three collapse flags are monotone -- i.e. the file's start
    plus one new segment at every reset line (`message_start`,
    `ralphd.iteration`; see `_transitions`). Each segment records its own
    `startByte` (the byte offset of the reset line that opened it, 0 for
    the first) and, for each flag, the byte offset of the FIRST line
    inside that segment which sets it, or `None` when the flag never
    fires in that segment at all. A seeded boundary records its seeded
    flags at the segment's own `startByte`. Plus the file's size, for the
    read side's consistency check (`load_index`).

    Within one segment the flags really are monotone (nothing between two
    resets ever clears one), so one offset per flag per segment is the
    whole truth, and `resolve_flags_at_offset` answers any offset with
    one binary search over `segments` plus three comparisons -- never a
    cost that grows with the distance to the deciding setter (G.3/G.6).

    This is the WHOLE rebuild -- called both by `write_index` (engine
    boundary write) and directly by anything that wants to prove the
    index is rebuildable from the transcript alone (PRD G.4/G.7), since
    it reads nothing but `output_path` itself and no other engine state.
    A file that does not exist yet reads as empty (no flags ever set),
    matching a freshly-`begin_iteration_dir`-ed, not-yet-run iteration.
    """
    try:
        raw = output_path.read_bytes()
    except FileNotFoundError:
        raw = b""
    segments = [{"startByte": 0, "textSeenAtByte": None,
                 "thinkingSeenAtByte": None, "toolcallSeenAtByte": None}]
    for offset, action in _transitions(raw):
        if action[0] == "reset":
            seed = action[1]
            # A reset line at the very first byte (a transcript that opens
            # with `message_start`, the common live shape) resets a segment
            # that spans no cut point at all -- `resolve_flags_at_offset`
            # only ever selects a segment whose `startByte` is strictly
            # below the cut. Replace it rather than appending a second
            # segment at the same offset, so `startByte`s stay strictly
            # ascending and `load_index` can check that as an invariant.
            if segments[-1]["startByte"] == offset:
                segments.pop()
            segments.append({
                "startByte": offset,
                "textSeenAtByte": offset if seed.get("textSeen") else None,
                "thinkingSeenAtByte": (offset if seed.get("thinkingSeen")
                                       else None),
                "toolcallSeenAtByte": (offset if seed.get("toolcallSeen")
                                       else None)})
        else:
            key = _FLAG_KEY[action[1]]
            if segments[-1][key] is None:
                segments[-1][key] = offset
    return {"version": INDEX_VERSION, "transcriptBytes": len(raw),
            "segments": segments}


def write_index(itdir: Path, output_filename: str = _OUTPUT_FILENAME) -> None:
    """Compute and atomically write the side index for one iteration whose
    transcript file is finished (the engine's own iteration-boundary
    bookkeeping -- `engine/loop.py`'s end-of-iteration `meta.json` write --
    is the only caller). Never raises: a failure here must cost nothing
    more than a missing/stale index, which `load_index` already treats as
    an ordinary fall-back-to-scan case (G.4), never a lost iteration.
    """
    try:
        index = compute_collapse_index(itdir / output_filename)
        tmp = itdir / (INDEX_FILENAME + ".tmp")
        tmp.write_text(json.dumps(index))
        tmp.replace(itdir / INDEX_FILENAME)
    except OSError:
        pass


def load_index(itdir: Path, output_filename: str = _OUTPUT_FILENAME) -> dict | None:
    """Read and validate one iteration's side index against its ACTUAL
    transcript file on disk right now. Returns the parsed dict only when
    every one of these holds: the file exists, parses as a JSON object,
    carries every required key, records exactly `INDEX_VERSION` (an index
    written by some other engine build is not second-guessed, it is
    ignored), carries a well-formed non-empty `segments` list whose
    `startByte`s ascend from 0, and records a `transcriptBytes` still
    equal to the transcript's current size. Otherwise returns `None` --
    absence, truncation/malformed JSON, or a stale/mismatched consistency
    check are the exact three fallback cases G.4 names, and none of them
    is ever raised as an error here.

    The size check alone (not a full re-read/hash of the transcript) is
    deliberate: the transcript never changes again once its iteration has
    ended (the one writer is the engine's own boundary bookkeeping, which
    writes this index immediately after that file's last byte), so a
    stat-cost comparison is enough to catch the only way the two could
    ever legitimately disagree -- an index left over from before some
    later truncation/rewrite of the transcript -- without re-reading the
    very bytes this index exists so a bounded reader does not have to."""
    path = itdir / INDEX_FILENAME
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        index = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(index, dict) or any(k not in index for k in _REQUIRED_KEYS):
        return None
    if index.get("version") != INDEX_VERSION:
        return None
    segments = index.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    previous = -1
    for seg in segments:
        if not isinstance(seg, dict) or any(k not in seg for k in _SEGMENT_KEYS):
            return None
        start = seg.get("startByte")
        if not isinstance(start, int) or start <= previous:
            return None
        previous = start
        for key in _SEGMENT_KEYS[1:]:
            at = seg.get(key)
            if at is not None and not isinstance(at, int):
                return None
    if segments[0].get("startByte") != 0:
        return None
    try:
        actual_size = (itdir / output_filename).stat().st_size
    except OSError:
        actual_size = 0
    if index.get("transcriptBytes") != actual_size:
        return None
    return index


def resolve_flags_at_offset(itdir: Path, cut_offset: int,
                            output_filename: str = _OUTPUT_FILENAME) -> dict:
    """`{"textSeen": bool, "thinkingSeen": bool, "toolcallSeen": bool}` as
    of byte offset `cut_offset` into this iteration's transcript file --
    i.e. exactly the three collapse guards a fresh `new_render_state()`
    holds after `log_render.render_to_lines` has rendered the transcript's
    first `cut_offset` bytes and nothing more -- resolved against
    `load_index`'s validated side index with one binary search over its
    segments (no cost in the distance to the deciding setter), or by a
    plain scan of those same bytes when no valid index exists.

    Both branches read their transitions from the same `_transitions`
    generator, so the index answer and the fallback answer cannot drift
    apart from each other, and neither can drift from the renderer
    without `_transitions` itself being wrong.

    This is the one consumer this task wires up: absence, truncation, a
    consistency-check mismatch, or an index written by a different engine
    build (`load_index` returning `None`) all fall through to the scan
    below rather than raising or guessing -- so every one of those cases
    still renders correctly, only slower (G.4). Eliminating the scan's
    OWN cost in the two real render paths (the `/logs` handler and the
    on-disk fallback), so the fallback itself is never paid on the hot
    path either, is G.6/task 021's job -- this function's fallback branch
    is deliberately the plain, obviously correct scan it replaces, not a
    second optimisation.
    """
    index = load_index(itdir, output_filename)
    if index is not None:
        segments = index["segments"]
        # The newest segment that OPENED strictly before the cut: a reset
        # line starting exactly at `cut_offset` has not been rendered yet,
        # so it must not be applied (its own segment is excluded by the
        # strict `<`). Nothing opened before the cut at all means nothing
        # has been rendered: every flag false.
        i = bisect.bisect_left([s["startByte"] for s in segments],
                               cut_offset) - 1
        if i < 0:
            return {out: False for out in _FLAG_OUT.values()}
        seg = segments[i]
        return {_FLAG_OUT[flag]: (seg[_FLAG_KEY[flag]] is not None
                                  and seg[_FLAG_KEY[flag]] < cut_offset)
                for flag in _FLAG_OUT}
    try:
        with (itdir / output_filename).open("rb") as f:
            raw = f.read(max(cut_offset, 0))
    except OSError:
        raw = b""
    seen = {"text": False, "thinking": False, "toolcall": False}
    for _offset, action in _transitions(raw):
        if action[0] == "reset":
            seed = action[1]
            seen = {flag: bool(seed.get(_FLAG_OUT[flag])) for flag in seen}
        else:
            seen[action[1]] = True
    return {_FLAG_OUT[flag]: seen[flag] for flag in seen}
