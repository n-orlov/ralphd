"""Engine-owned side index that lets a bounded log tail resume mid-transcript
without a full rescan (task 012, PRD v0.26 requirement C.1, #271/#272).

v0.25's `collapse_index.py` (task 020/021) records only the three
prefix-collapse flags, and only ever at ONE moment: the engine's own
iteration-boundary bookkeeping, once the whole transcript is fully
written (`collapse_index.write_index`, called from `engine/loop.py` after
the runner has returned). PRD C names the two gaps that leaves: the index
is absent for the exact case an operator watches most -- a LIVE iteration
-- and it carries nothing a bounded reader needs besides the collapse
flags themselves (rendered-line offsets, the renderer's own running state
at those offsets). The operator's ruling (25 Sep 2026, PRD C): the side
index may record whatever a bounded tail needs, and it may be written
incrementally.

This module is that extension -- a SUCCESSOR file beside
`collapse_index.json`, never a replacement for it (nothing here touches
`collapse_index.py`'s own file, version or read side; only its internal
`_transitions`/`apply_transition`/`new_segments` helpers are reused, so the
two indexes' collapse segments can never drift apart from each other).

Shape (`INDEX_FILENAME`, version `INDEX_VERSION`):
  - `transcriptBytes`: how many bytes of the transcript, from its own
    start, this index actually covers right now. For a live iteration this
    is normally LESS than the transcript's current on-disk size -- see
    `load_index`'s own contract below.
  - `segments`: byte-for-byte the same shape `collapse_index.json` records
    (`startByte`, `textSeenAtByte`, `thinkingSeenAtByte`,
    `toolcallSeenAtByte`), built by the identical `_transitions`/
    `apply_transition` mirror of the renderer's own dispatch.
  - `checkpoints`: one entry per raw NDJSON line consumed so far --
    `byteOffset` (the transcript offset immediately after that line),
    `renderedLines` (the cumulative count of fragments
    `log_render.render_to_lines` returned for each raw line of the prefix
    rendered one raw line at a time -- it rises on exactly the raw lines
    that print anything; a streamed text line spanning several raw lines
    counts once per raw line, so this is not the whole render's line
    count), and
    `state` (`log_render.new_render_state()`'s own dict, JSON-round-
    tripped by `_state_to_json`/`_state_from_json`, as of that same
    point), and `lineCrc32` (`zlib.crc32` of that raw line's own bytes,
    so a reader can tell whether the bytes on disk are still the ones the
    checkpoint describes). A bounded reader can therefore pick any checkpoint at or
    before the byte it wants to resume from, seed a fresh render with its
    `state`, and continue rendering forward -- never from byte 0 -- and
    get output identical to a full render's (task 013's job; this module
    only WRITES the index, per PRD C.1/C.7's own split).

Write side:
  - `compute_render_index` -- the full, from-scratch rebuild (a pure
    function of the transcript file's bytes, like
    `collapse_index.compute_collapse_index`). Used by `write_index` (the
    engine's belt-and-braces call at the END of an iteration, mirroring
    `collapse_index.write_index`'s own call site in `loop.py`) and by
    tests that want to prove the incremental writer agrees with a
    from-scratch render at every prefix.
  - `LiveRenderIndex` -- the INCREMENTAL writer. `engine/runner.py`'s own
    `pump()` (the ONE place the running transcript is written while an
    iteration is still running) feeds it one already-on-disk raw line at
    a time via `feed()`, and calls `flush()` to persist the running
    snapshot to `itdir / INDEX_FILENAME`. `feed()` and
    `compute_render_index` share the exact same per-line step
    (`LiveRenderIndex.feed` IS `compute_render_index`'s inner loop body),
    so an index built line-by-line while an iteration streams can never
    disagree with one built from scratch over the same prefix -- the
    property task 012's own tests (`tests/test_render_index.py`) pin
    directly.

Never written by a render path (`cli/log_render.py`, `log_merge.py`,
`cli/ui_server.py`) -- exactly `collapse_index.py`'s own G.10 rule, carried
over here: rendering never writes an index, so a read-only consumer (a
finished run's directory opened cold) works with whatever it finds,
including none.

Read side (`load_index`): unlike `collapse_index.load_index`, this index's
own `transcriptBytes` is allowed to sit BEHIND the transcript's current
on-disk size -- a live iteration's index is always at least a little
stale, by design (C.1: "written incrementally", not synchronously with
every byte). A reader holding a transcript longer than what the index
covers treats the uncovered suffix as unindexed (falls back to scanning
just that suffix, never the whole file) -- `load_index` itself only
refuses a index that is otherwise malformed, wrong-version, internally
inconsistent, that claims to cover MORE than the transcript's actual
current size (which can only mean a stale index left over from before a
truncation/rewrite, `collapse_index.load_index`'s own one real disagreement
case), or whose bounded verification (`_covered_prefix_matches`: the
leading line plus the covered prefix's last `VERIFY_WINDOW_BYTES`, each
segment's CRC-32, fragment count and resulting state) finds bytes the
index no longer describes. Consuming that "uncovered suffix" bound through the real client-
facing call is task 013's job.
"""

from __future__ import annotations

import bisect
import copy
import json
import zlib
from pathlib import Path

from . import collapse_index as _collapse_index
from .cli.log_render import (
    new_render_state,
    render_to_lines,
    render_to_text,
    split_rendered_text,
)
from .log_merge import OUTPUT_FILENAME as _OUTPUT_FILENAME
from .log_merge import boundary_line as _boundary_line
from .log_merge import iteration_dir as _iteration_dir
from .log_merge import iteration_numbers as _iteration_numbers
from .log_merge import iteration_output_path as _iteration_output_path

INDEX_FILENAME = "render_index.json"
INDEX_VERSION = 2

_REQUIRED_KEYS = ("version", "transcriptBytes", "segments", "checkpoints")
_SEGMENT_KEYS = _collapse_index._SEGMENT_KEYS
_CHECKPOINT_KEYS = ("byteOffset", "renderedLines", "state", "lineCrc32")

# Task cure-01-03 (PRD C.1/C.3, D.1): the exact key set `new_render_state`
# produces, JSON-round-tripped (`_state_to_json`) -- what a checkpoint's own
# `state` must carry for `_state_from_json` to hydrate it back into
# something `log_render.render_to_text` can actually run against.
# `load_index` rejects a checkpoint whose `state` is missing any of these,
# carries an extra one, or holds a wrong-typed value for one, INSTEAD of
# letting a downstream renderer crash on a missing key the way a bare
# `isinstance(state, dict)` check let a checkpoint like `{}` through
# before this task's cure.
_STATE_SHAPE_KEYS = frozenset(new_render_state())
_STATE_BOOL_KEYS = ("text_open", "text_seen", "thinking_seen", "toolcall_seen")
_STATE_DICT_KEYS = ("tool_args", "tool_pending")


def _valid_checkpoint_state_shape(state: object) -> bool:
    """Structural validity of one checkpoint's `state` value -- cheap
    (no rendering, just key/type checks), and independent of whatever the
    transcript itself says: a checkpoint missing a required key, carrying
    an unknown extra one, or holding a wrong-typed value for one (the
    `malformed-state` case: `state["text_seen"]` absent entirely) is
    refused here, before anything tries to `render_to_text` with it."""
    if not isinstance(state, dict) or set(state) != _STATE_SHAPE_KEYS:
        return False
    if not all(isinstance(state[k], bool) for k in _STATE_BOOL_KEYS):
        return False
    if not all(isinstance(state[k], dict) for k in _STATE_DICT_KEYS):
        return False
    # The NESTED values are checked too, not just their containers: each
    # `tool_pending` entry is hydrated back into a `(name, args)` tuple by
    # `_state_from_json` and later unpacked as exactly that pair, and each
    # `collapse_unresolved` member goes into a `set` -- so a checkpoint
    # like `{"tool_pending": {"call-id": 42}}` or
    # `{"collapse_unresolved": [[1]]}` is structurally malformed and is
    # refused here rather than raising `TypeError` downstream.
    for entry in state["tool_pending"].values():
        if not (isinstance(entry, list) and len(entry) == 2
                and isinstance(entry[0], str)):
            return False
    if state["open_tool_line"] is not None and not isinstance(state["open_tool_line"], str):
        return False
    unresolved = state["collapse_unresolved"]
    return (isinstance(unresolved, list)
            and all(isinstance(flag, str) for flag in unresolved))


def _state_matches(state: dict, stored: dict) -> bool:
    """Whether a live render `state` equals a checkpoint's stored (JSON-
    round-tripped) `state` -- compared through the same JSON round trip
    the stored copy went through, so `tool_pending`'s tuples and
    `collapse_unresolved`'s set compare equal to their JSON arrays."""
    return json.loads(json.dumps(_state_to_json(state))) == stored


def _render_segment(raw: bytes, cp: dict, state: dict,
                    prev_lines: int) -> str | None:
    """Verify ONE covered segment -- the raw line(s) between the preceding
    checkpoint and `cp` -- against the transcript bytes actually on disk,
    and return what it renders. `state` is the preceding checkpoint's
    state (already hydrated; advanced in place) and `prev_lines` its
    `renderedLines`. `None` -- the index no longer describes these bytes
    -- unless all three hold: the bytes' CRC-32 equals `cp["lineCrc32"]`
    (catches a same-size rewrite even when it renders identically), the
    render's fragment count equals `cp`'s `renderedLines` delta, and the
    state it leaves behind equals `cp["state"]` (so a checkpoint's stored
    state is re-derived from its predecessor's, never trusted blind)."""
    if zlib.crc32(raw) != cp["lineCrc32"]:
        return None
    text = render_to_text(raw.decode("utf-8", errors="replace"), tty=False,
                          state=state)
    if len(split_rendered_text(text)) != cp["renderedLines"] - prev_lines:
        return None
    if not _state_matches(state, cp["state"]):
        return None
    return text


def _covered_prefix_matches(output_path: Path, checkpoints: list[dict],
                            window: int | None = None) -> bool:
    """Whether the covered prefix's bytes on disk still match what the
    index recorded, checked over a BOUNDED set of segments (never the
    whole covered prefix, whose silent bytes a tail must not pay for --
    PRD C.2):

      - `checkpoints[0]`'s own segment (the transcript's first raw line),
        rendered from a genuinely fresh `new_render_state()` -- the root
        of trust the backward walk (`_prepend_covered`) never re-derives
        from anything earlier;
      - every segment lying wholly inside the LAST `window` bytes of the
        covered prefix (`VERIFY_WINDOW_BYTES`), rendered in order from the
        stored state of the checkpoint just before that window. When the
        whole covered prefix fits in the window, every covered byte is
        verified.

    Each segment goes through `_render_segment` (CRC-32, fragment count,
    resulting state). A read error or short read is a mismatch."""
    if window is None:
        window = VERIFY_WINDOW_BYTES
    covered = checkpoints[-1]["byteOffset"]
    window_start = max(0, covered - window)
    if window_start == 0:
        first = 0
    else:
        i = bisect.bisect_left(checkpoints, window_start,
                               key=lambda cp: cp["byteOffset"])
        first = i + 1
    try:
        with output_path.open("rb") as f:
            if first > 0:
                lead = checkpoints[0]
                raw = f.read(lead["byteOffset"])
                if (len(raw) != lead["byteOffset"]
                        or _render_segment(raw, lead, new_render_state(), 0) is None):
                    return False
            if first >= len(checkpoints):
                return True
            prev = checkpoints[first - 1] if first > 0 else None
            start = prev["byteOffset"] if prev else 0
            f.seek(start)
            data = f.read(covered - start)
    except OSError:
        return False
    if len(data) != covered - start:
        return False
    state = (_state_from_json(copy.deepcopy(prev["state"])) if prev
             else new_render_state())
    prev_lines = prev["renderedLines"] if prev else 0
    for cp in checkpoints[first:]:
        end = cp["byteOffset"] - start
        seg_start = (prev["byteOffset"] - start) if prev else 0
        if _render_segment(data[seg_start:end], cp, state, prev_lines) is None:
            return False
        prev, prev_lines = cp, cp["renderedLines"]
    return True


# How many bytes, at the END of the covered prefix, `load_index` re-reads
# and verifies segment by segment on every load (`_covered_prefix_
# matches`) -- the fixed allowance PRD C.2 permits on top of the tail's
# own lines. A covered prefix no larger than this is verified in full.
# Beyond it, a same-size rewrite is caught only in the leading line, the
# window, and the lines the tail itself reads (`_prepend_covered`): a
# rewrite anywhere else means bytes nobody reads, and reading them all
# would cost the whole silent prefix the C.2 bound exists to skip.
VERIFY_WINDOW_BYTES = 1 << 17

# How many freshly-fed raw lines a live iteration accumulates before
# `LiveRenderIndex.flush` actually rewrites `render_index.json` on disk --
# the write itself is O(total checkpoints so far), so paying it on every
# single line would make a long iteration's own logging cost grow
# quadratically in its own line count. Batching it costs nothing in
# CORRECTNESS (PRD C.1's own "written incrementally", not "written
# synchronously") -- only in how stale the "uncovered suffix" a reader
# falls back to scanning can be, which `load_index`'s whole contract
# already treats as ordinary, never as an error.
LIVE_FLUSH_EVERY_LINES = 20


def _state_to_json(state: dict) -> dict:
    """`log_render`'s render-state dict, JSON-round-trippable: its one
    non-JSON member (`collapse_unresolved`, a `set`) becomes a sorted
    list; everything else (`tool_pending`'s tuples included -- `json.dumps`
    already serializes a tuple as a JSON array) survives `json.dumps`
    unchanged. A DEEP copy: `tool_args`/`tool_pending` are dicts the live
    render state keeps mutating, and a checkpoint sharing them would
    silently describe a later line's state instead of its own."""
    out = copy.deepcopy(state)
    out["collapse_unresolved"] = sorted(state.get("collapse_unresolved") or ())
    return out


def _state_from_json(d: dict) -> dict:
    """The inverse of `_state_to_json`: restores the `set` and the
    `tool_pending` tuples a freshly-rendered `new_render_state()` would
    hold, so a checkpoint's stored state can seed a real continuation
    (task 013), not just be inspected as JSON."""
    state = dict(d)
    state["collapse_unresolved"] = set(d.get("collapse_unresolved") or ())
    state["tool_pending"] = {k: tuple(v) for k, v in
                             (d.get("tool_pending") or {}).items()}
    return state


class LiveRenderIndex:
    """Incrementally builds one iteration's render index, one already-
    on-disk raw transcript line at a time.

    The ONLY two callers this is meant for: `engine/runner.py`'s `pump()`
    (fed every line the moment it is written to disk, i.e. the engine's
    own LIVE write path -- PRD C.1's own requirement) and
    `compute_render_index` below (fed every line of a finished transcript
    in one pass, so the incremental and from-scratch code paths are
    provably the same algorithm rather than two that could drift apart).
    """

    def __init__(self):
        self._state = new_render_state()
        self._segments = _collapse_index.new_segments()
        self._checkpoints: list[dict] = []
        self._rendered_lines = 0
        self._offset = 0
        self._lines_since_flush = 0

    @property
    def transcript_bytes(self) -> int:
        return self._offset

    def feed(self, raw_line: bytes) -> None:
        """Consume exactly one raw transcript line (bytes, as already
        written to disk, trailing newline included) and extend the
        running segments/checkpoints by whatever it contributes."""
        for rel_offset, action in _collapse_index._transitions(raw_line):
            _collapse_index.apply_transition(
                self._segments, self._offset + rel_offset, action)
        text = raw_line.decode("utf-8", errors="replace")
        produced = render_to_lines(text, tty=False, state=self._state)
        self._rendered_lines += len(produced)
        self._offset += len(raw_line)
        self._checkpoints.append({
            "byteOffset": self._offset,
            "renderedLines": self._rendered_lines,
            "state": _state_to_json(self._state),
            "lineCrc32": zlib.crc32(raw_line)})
        self._lines_since_flush += 1

    def snapshot(self) -> dict:
        """The index payload as of every line fed so far."""
        return {"version": INDEX_VERSION, "transcriptBytes": self._offset,
                "segments": [dict(s) for s in self._segments],
                "checkpoints": [dict(c) for c in self._checkpoints]}

    def flush(self, itdir: Path, *, force: bool = False,
              every: int = LIVE_FLUSH_EVERY_LINES) -> bool:
        """Persist the current snapshot to `itdir / INDEX_FILENAME` when at
        least `every` new lines were fed since the last flush, or always
        when `force` (the pump loop's own end-of-stream call, so the last
        partial batch is never left unwritten). Never raises: exactly
        `collapse_index.write_index`'s own failure contract -- a write
        that fails costs nothing more than a missing/stale index, which
        every reader already treats as an ordinary fall-back case.
        Returns whether it actually wrote."""
        if not force and self._lines_since_flush < every:
            return False
        try:
            tmp = itdir / (INDEX_FILENAME + ".tmp")
            tmp.write_text(json.dumps(self.snapshot()))
            tmp.replace(itdir / INDEX_FILENAME)
        except OSError:
            return False
        self._lines_since_flush = 0
        return True


def compute_render_index(output_path: Path) -> dict:
    """Full, from-scratch rebuild: render every line of `output_path` once
    through a fresh `LiveRenderIndex`, exactly as `pump()` would have, and
    return its final snapshot. A file that does not exist yet reads as
    empty (matching a freshly begun, not-yet-run iteration, same as
    `collapse_index.compute_collapse_index`)."""
    try:
        raw = output_path.read_bytes()
    except FileNotFoundError:
        raw = b""
    live = LiveRenderIndex()
    for raw_line in raw.splitlines(keepends=True):
        live.feed(raw_line)
    return live.snapshot()


def write_index(itdir: Path, output_filename: str = _OUTPUT_FILENAME) -> None:
    """Full rebuild + atomic write for one iteration's transcript --
    `engine/loop.py`'s own end-of-iteration bookkeeping call, mirroring
    `collapse_index.write_index`'s call at the same call site. Never
    raises."""
    try:
        index = compute_render_index(itdir / output_filename)
        tmp = itdir / (INDEX_FILENAME + ".tmp")
        tmp.write_text(json.dumps(index))
        tmp.replace(itdir / INDEX_FILENAME)
    except OSError:
        pass


def load_index(itdir: Path, output_filename: str = _OUTPUT_FILENAME) -> dict | None:
    """Read and validate one iteration's render index against its transcript
    file's CURRENT size on disk. Returns the parsed dict only when: the
    file exists and parses as a JSON object; carries every required key;
    records exactly `INDEX_VERSION`; `segments` is well-formed (the same
    shape/ascending-`startByte` check `collapse_index.load_index` applies);
    `checkpoints` is a well-formed list whose `byteOffset`/`renderedLines`
    both ascend (non-strictly) and whose last `byteOffset` equals
    `transcriptBytes` (0 when there are no checkpoints at all); and
    `transcriptBytes` is no GREATER than the transcript's actual current
    size (never stale in the direction of overclaiming coverage -- the one
    disagreement that can only mean a truncated/rewritten transcript, the
    exact case `collapse_index.load_index` also refuses); every checkpoint
    carries an unsigned 32-bit integer `lineCrc32`; and
    `_covered_prefix_matches` finds the leading segment and every segment
    inside the covered prefix's last `VERIFY_WINDOW_BYTES` still matching
    the transcript's current bytes (so any same-size rewrite or
    replacement inside that window, or of the first line, is refused;
    a covered prefix no larger than the window is verified in full).

    Deliberately NOT required: `transcriptBytes == actual size`. A live
    iteration's index is normally behind the transcript's current size
    (PRD C.1's own "written incrementally"); the caller (task 013) treats
    that gap as an unindexed suffix to scan directly, never as reason to
    discard the whole index. Absence, truncation/malformed JSON, or any
    other inconsistency returns `None`, never raises."""
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
    transcript_bytes = index.get("transcriptBytes")
    if not isinstance(transcript_bytes, int) or transcript_bytes < 0:
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
    checkpoints = index.get("checkpoints")
    if not isinstance(checkpoints, list):
        return None
    prev_offset, prev_lines = -1, -1
    for cp in checkpoints:
        if not isinstance(cp, dict) or any(k not in cp for k in _CHECKPOINT_KEYS):
            return None
        offset = cp.get("byteOffset")
        lines = cp.get("renderedLines")
        state = cp.get("state")
        if not isinstance(offset, int) or offset <= prev_offset:
            return None
        if not isinstance(lines, int) or lines < prev_lines:
            return None
        if not _valid_checkpoint_state_shape(state):
            return None
        crc = cp.get("lineCrc32")
        if (not isinstance(crc, int) or isinstance(crc, bool)
                or not 0 <= crc <= 0xFFFFFFFF):
            return None
        prev_offset, prev_lines = offset, lines
    last_offset = checkpoints[-1]["byteOffset"] if checkpoints else 0
    if transcript_bytes != last_offset:
        return None
    try:
        actual_size = (itdir / output_filename).stat().st_size
    except OSError:
        actual_size = 0
    if transcript_bytes > actual_size:
        return None
    if checkpoints and not _covered_prefix_matches(
            itdir / output_filename, checkpoints):
        return None
    return index


# Task 013: the read side. `checkpoints[i]["renderedLines"]` counts the
# per-raw-line fragments `render_to_lines` returned while the index was fed
# one raw line at a time -- NOT the whole render's line count (a streamed
# text line spans many `text_delta` raw lines, each its own fragment) -- but
# it rises by at least one on exactly the raw lines that print anything and
# stays flat across every silent one. That is all the reader needs: a
# binary search over it lands on the PRINTING raw lines, newest first,
# without walking a single silent byte in between, and each one is then
# re-rendered from its predecessor's saved `state`, so the exact characters
# it printed -- newlines included -- are recovered and concatenated the way
# a whole render would have printed them.
_SUFFIX_READ_CHUNK = 1 << 16  # the unindexed suffix is streamed in these.


def _enough(text: str, n: int) -> bool:
    """True when `text` -- a suffix of the whole render's own output, not
    necessarily starting at a line start -- already fixes the whole
    render's last `n` lines: it holds `n` newlines besides a trailing one,
    so even a partial first line leaves `n` complete lines after it."""
    return text.removesuffix("\n").count("\n") >= n


def _keep_tail(text: str, n: int) -> str:
    """Drop whole leading lines of `text` it no longer needs, keeping it
    `_enough` for `n` -- bounds memory while the unindexed suffix streams."""
    body = text.removesuffix("\n")
    extra = body.count("\n") - (n + 1)
    if extra <= 0:
        return text
    cut = -1
    for _ in range(extra):
        cut = text.index("\n", cut + 1)
    return text[cut + 1:]


def _render_unindexed_suffix(output_path: Path, start: int, size: int,
                             state: dict, n: int) -> str | None:
    """Render `[start, size)` of the transcript -- the part a lagging live
    index does not cover yet -- continuing `state` in place, and return
    the captured output (trimmed to what the last `n` lines need). Every
    byte of it is read: nothing records what it prints but itself. `None`
    on a read error."""
    if start >= size:
        return ""
    out = ""
    pending = b""
    try:
        with output_path.open("rb") as f:
            f.seek(start)
            remaining = size - start
            while remaining > 0:
                data = f.read(min(_SUFFIX_READ_CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                pending += data
                cut = pending.rfind(b"\n") + 1
                if cut:
                    out += render_to_text(pending[:cut].decode("utf-8", errors="replace"),
                                          tty=False, state=state)
                    pending = pending[cut:]
                    out = _keep_tail(out, n)
    except OSError:
        return None
    if pending:
        out += render_to_text(pending.decode("utf-8", errors="replace"),
                              tty=False, state=state)
    return _keep_tail(out, n)


def _prepend_covered(output_path: Path, checkpoints: list[dict], acc: str,
                     n: int) -> tuple[str, bool] | None:
    """Walk the index's covered prefix backward over its PRINTING raw lines
    only (binary search on `renderedLines`), re-rendering each from its
    predecessor checkpoint's saved `state` and prepending what it printed
    to `acc`, until `acc` is `_enough` for `n` or the prefix is exhausted.
    Returns `(acc, exhausted)`; `None` when the transcript disagrees with
    the index for any segment it reads (`_render_segment`: a CRC-32
    mismatch, a different fragment count, or a resulting state that is
    not the checkpoint's stored one) -- never a wrong-but-plausible
    render."""
    if not checkpoints:
        return acc, True
    k = checkpoints[-1]["renderedLines"]
    try:
        with output_path.open("rb") as f:
            while not _enough(acc, n):
                if k <= 0:
                    return acc, True
                j = bisect.bisect_left(checkpoints, k,
                                       key=lambda cp: cp["renderedLines"])
                prev = checkpoints[j - 1] if j > 0 else None
                start = prev["byteOffset"] if prev else 0
                prev_lines = prev["renderedLines"] if prev else 0
                state = (_state_from_json(copy.deepcopy(prev["state"])) if prev
                         else new_render_state())
                f.seek(start)
                raw = f.read(checkpoints[j]["byteOffset"] - start)
                if len(raw) != checkpoints[j]["byteOffset"] - start:
                    return None
                text = _render_segment(raw, checkpoints[j], state, prev_lines)
                if text is None:
                    return None
                acc = text + acc
                k = prev_lines
    except OSError:
        return None
    return acc, False


def _one_iteration_tail_text(run_root: Path, number: int, n: int,
                             *, index: dict | None = None) -> tuple[str, bool] | None:
    """One iteration's own contribution to a merged bounded tail: its
    covered prefix's printing lines (walked backward, newest first) plus
    its unindexed suffix and, once it has ended, its own `ralphd.iteration`
    "end" boundary line -- exactly what `bounded_tail` used to compute
    for the LATEST iteration alone, factored out so `bounded_tail` (task
    cure-01-04, PRD C.2) can call it for however many EARLIER iterations a
    tail spanning indexed iterations needs too.

    Returns `(text, exhausted)`. `exhausted` is True exactly when the
    backward walk reached this iteration's own byte 0 -- i.e. `text`
    already starts at the true beginning of this iteration's own render,
    so a caller that still needs more lines may prepend this iteration's
    own `ralphd.iteration` "start" boundary line and move on to the
    PRECEDING iteration, never re-reading anything already covered here.
    `None` -- never a wrong-but-plausible answer -- when this one
    iteration cannot answer by itself: no valid index (on disk or
    passed in), a transcript that disagrees with it, or any other reason
    to fall back."""
    itdir = _iteration_dir(run_root, number)
    if index is None:
        index = load_index(itdir)
        if index is None:
            return None
    try:
        meta = json.loads((itdir / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    out_path = _iteration_output_path(run_root, number)
    try:
        size = out_path.stat().st_size
    except OSError:
        size = 0
    checkpoints = index["checkpoints"]
    covered = index["transcriptBytes"]
    # Defence in depth behind `load_index`'s own shape check: a stored
    # checkpoint state that still makes hydration or rendering raise (a
    # passed-in `index` that never went through `load_index`, or a shape
    # this module's validator does not anticipate) makes the index
    # unusable -- `None`, so the caller falls back to the full render --
    # never an exception escaping to `ui_server.rendered_log_lines`.
    try:
        state = (_state_from_json(copy.deepcopy(checkpoints[-1]["state"]))
                 if checkpoints else new_render_state())
        acc = _render_unindexed_suffix(out_path, covered, size, state, n)
        if acc is None:
            return None
        if meta.get("endedAt"):
            acc += render_to_text(_boundary_line(meta, "end"), tty=False, state=state)
        walked = _prepend_covered(out_path, checkpoints, acc, n)
    except (TypeError, KeyError, ValueError, AttributeError, IndexError):
        return None
    if walked is None:
        return None
    acc, exhausted = walked
    if exhausted:
        # A genuine iteration start always renders its header line
        # (`log_render.render_log_line`'s `ralphd.iteration` branch, no
        # `collapseSeed`) -- this walk reached byte 0 of THIS iteration's
        # own transcript, so that header belongs immediately before `acc`.
        # Every field the boundary line prints comes from `meta` alone, so
        # rendering it from a fresh state is exactly what a full render's
        # own pass over this iteration's start would do (the boundary
        # branch resets every collapse flag regardless of what came in).
        acc = render_to_text(_boundary_line(meta, "start"), tty=False,
                             state=new_render_state()) + acc
    return acc, exhausted


def bounded_tail(run_root: Path, n: int, *, index: dict | None = None) -> list[str] | None:
    """The exact last `n` rendered lines of `run_root`'s merged render,
    served from its iterations' own render indexes alone (task 013,
    extended across iteration boundaries by task cure-01-04, PRD C.2) --
    bytes read off any one transcript stay proportional to the lines it
    actually contributes (the printing raw lines that produced them) plus
    that iteration's own unindexed suffix, regardless of however many
    silent bytes sit between them and that transcript's end.

    Tries the MOST RECENT iteration first (`_one_iteration_tail_text`):
    whatever its covered prefix prints, then its unindexed suffix, then
    its own "end" boundary line once it has ended. When that iteration's
    OWN render, walked all the way back to its own byte 0, still falls
    short of `n` lines, this no longer gives up the way task 013 did --
    it prepends that iteration's own "start" boundary line and tries the
    PRECEDING iteration the exact same way, and keeps walking backward
    through however many iterations this run has, each purely off its own
    render index, never rescanning a byte of silent history any of them
    already answered from. Reaching the run's very first iteration's own
    byte 0 with still fewer than `n` lines total means there is genuinely
    no more history anywhere, indexed or not -- PRD C.2's own "N exceeds
    the latest iteration's rendered output" case, extended across however
    many earlier iterations this run has -- so this returns every line
    the run ever rendered instead of falling back to a scan that could
    only ever reach the same, already-known answer at unbounded cost.

    `index`, when given, is used INSTEAD OF loading `INDEX_FILENAME` from
    the LATEST iteration's own directory only -- task 015's hub-owned
    cache (`cli/ui_server._absent_index_cache_lookup`) passes in an index
    it built itself, off the on-disk transcript, for a run whose own
    latest-iteration directory carries no such file (or an invalid one) at
    all; `load_index` on that same directory would just answer `None`
    again. Every EARLIER iteration this function may still need to consult
    is always read from its own on-disk `render_index.json`: the absent-
    index cache's own key (`render_index.latest_iteration_cache_key`) only
    ever describes the latest iteration, so a cached stand-in has nothing
    to offer any iteration before it. The caller (never this function) is
    responsible for knowing the cached index still describes the latest
    transcript's CURRENT size -- see the cache's own key, `(iteration
    number, transcript size)`.

    `None` -- never a wrong-but-plausible answer -- when some iteration in
    the backward walk cannot answer by itself: no iteration at all, no
    valid index (on disk or passed in) for an iteration the walk actually
    needs, or a transcript that disagrees with its index. The caller
    (`ui_server.rendered_log_lines`, on both its live and dead-run
    surfaces) then falls back to the growth-based fetch."""
    if n <= 0:
        return None
    numbers = _iteration_numbers(run_root)
    if not numbers:
        return None
    acc = ""
    for pos in range(len(numbers) - 1, -1, -1):
        number = numbers[pos]
        result = _one_iteration_tail_text(
            run_root, number, n,
            index=index if pos == len(numbers) - 1 else None)
        if result is None:
            return None
        text, exhausted = result
        acc = text + acc
        if _enough(acc, n):
            return split_rendered_text(acc)[-n:]
        if not exhausted:
            return None
    # Walked every iteration this run has, from the latest back to the
    # very first one's own byte 0, purely off each one's own render index
    # -- and the run's WHOLE rendered output still has fewer than `n`
    # lines. There is no more history left to consult anywhere, so this
    # genuinely is the answer.
    return split_rendered_text(acc)


def latest_iteration_cache_key(run_root: Path) -> tuple[int, int] | None:
    """`(iteration number, transcript byte size)` for `run_root`'s latest
    iteration -- the two components, beyond a run id, of task 015's
    hub-owned absent-index cache key (PRD C.4). `None` when the run has no
    iterations yet, or its latest one's transcript cannot be stat'd (a
    pure cache-miss signal, never an error the caller need handle
    specially: both cases just fall back to the growth-based scan)."""
    numbers = _iteration_numbers(run_root)
    if not numbers:
        return None
    last = numbers[-1]
    try:
        size = _iteration_output_path(run_root, last).stat().st_size
    except OSError:
        return None
    return last, size
