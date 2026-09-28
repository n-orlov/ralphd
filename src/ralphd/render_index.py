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

Task cure-01-01-4 (petition remedy for cure-01-03 criterion (1)): the
bounded window above still cannot see a same-size rewrite of a covered
line it never reads. Every published index therefore also carries an
identity/provenance witness -- `transcriptInode`, `transcriptWitnessBytes`,
`transcriptMtimeNs` and (task cure-01-03-2) `transcriptCtimeNs`, the
transcript's (inode, size, mtime, ctime) as of the moment the writer
vouched for it -- and `poisoned`. The engine's writer provenance-STAMPS
the transcript's mtime after every write it makes (`LiveRenderIndex.
append`, `write_index`: `_stamp`, a marker derived from the transcript's
own inode and size), so `load_index` can decide with one `stat()` and no
read (`_witness_holds`): refused when `poisoned`, when the inode changed
(a replacement), when the transcript is shorter than the witness, when it
is exactly the witness's size but its mtime OR ctime moved (an in-place
rewrite, however far outside the window -- ctime alone catches the case
an ordinary write restores the original mtime via `os.utime` afterwards,
since a normal write cannot restore ctime the same way), and when it has
grown past the witness but its current mtime does not carry the engine's
stamp for its current (inode, size) (the last modification was not the
engine's own append). The writer keeps custody across legitimate appends: before
each write it checks the transcript still looks exactly as its own last
write left it, and the first time it does not, it poisons and republishes
the index at once and never stamps again -- so no later growth, however
long, can bless a previously modified prefix. A reader also re-checks the
witness after its own reads (`_one_iteration_tail_text`), so a transcript
modified while it was being read is refused rather than half-seen, and a
hub-cached index passed in through `bounded_tail(..., index=)` is held to
the same witness. `compute_render_index`'s from-scratch rebuild vouches
only for the exact bytes it read (a `stat()` before and after the read
must agree) and never writes anything.
"""

from __future__ import annotations

import bisect
import copy
import json
import os
import time
import zlib
from collections.abc import Callable
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
INDEX_VERSION = 4

# Task cure-01-01-4 (PRD C.1/C.3, D.1; petition remedy for cure-01-03
# criterion (1)): the identity/provenance witness -- `transcriptInode`
# (`st_ino`, so a same-size REPLACEMENT, e.g. a rename over the path, is
# refused), `transcriptWitnessBytes` and `transcriptMtimeNs` (the size and
# mtime the writer vouched for) -- plus `poisoned`, a sticky flag only
# `LiveRenderIndex` ever sets, once it finds the transcript modified by
# anything but itself. See `_witness_holds` for the whole read-side check.
#
# Task cure-01-03-2 (PRD C.1/C.3, D.1; DoD 3; cure-01-03 and cure-01-01-4
# retry): `transcriptCtimeNs` -- the transcript's `st_ctime_ns` at the same
# moment -- joins the witness as an INDEPENDENT revision signal alongside
# `transcriptMtimeNs`. An ordinary same-size in-place rewrite of a covered
# line, followed by `os.utime` restoring the original mtime, leaves mtime
# alone but always bumps ctime (a normal write cannot set ctime to an
# arbitrary value the way it can mtime): checking mtime alone let such a
# rewrite through at exactly the witnessed size; ctime does not.
_REQUIRED_KEYS = ("version", "transcriptBytes", "segments", "checkpoints",
                 "transcriptInode", "transcriptMtimeNs", "transcriptCtimeNs",
                 "transcriptWitnessBytes", "poisoned")

# Task cure-01-01-4 retry (append provenance): the witness above only says
# what the transcript looked like at ONE moment. Coverage lags, so a reader
# routinely finds a transcript LONGER than `transcriptWitnessBytes`, whose
# mtime has moved on -- and on its own that cannot tell a legitimate engine
# append from a same-size rewrite of a covered line followed by more growth.
# The engine's writer (`LiveRenderIndex.append`, `write_index`) therefore
# STAMPS the transcript's mtime after every write it makes: it sets
# `st_mtime_ns` to a value whose residue modulo `_STAMP_MODULUS` is
# `_stamp_residue(inode, size)` -- a marker that the transcript's LAST
# modification was the engine's own, at exactly this size. Any other write
# (a rewrite, a truncate, a foreign append) sets the kernel's own current
# time instead, which carries that marker only by a ~1-in-4-million
# coincidence. A reader seeing growth past the witness accepts it only
# when the current mtime carries the marker for the current (inode, size),
# and the writer never stamps again once it has itself seen a modification
# it did not make (`poisoned`), so growth cannot launder an earlier
# rewrite. O(1): a `stat()`, never a read of the covered prefix. This is a
# witness against accidental or foreign modification, not against a
# writer that deliberately forges the marker.
_STAMP_MODULUS = 1 << 22


def _stamp_residue(inode: int, size: int) -> int:
    return zlib.crc32(f"{inode}:{size}".encode()) % _STAMP_MODULUS


def _carries_stamp(st: os.stat_result) -> bool:
    """Whether `st`'s mtime is the engine writer's provenance stamp for
    exactly `st`'s own (inode, size)."""
    return st.st_mtime_ns % _STAMP_MODULUS == _stamp_residue(st.st_ino, st.st_size)


def _stamp(path: Path) -> os.stat_result | None:
    """Set `path`'s mtime to the provenance stamp for its current (inode,
    size) -- the most recent such value not after the current time, so the
    mtime moves back by less than `_STAMP_MODULUS` ns -- and return its
    `stat()` afterwards (`None` on any OS error). A filesystem whose
    timestamps cannot hold nanoseconds simply returns an unstamped stat;
    readers then refuse growth past the witness, which costs a fallback,
    never a wrong answer."""
    try:
        st = path.stat()
        now = time.time_ns()
        target = now - (now % _STAMP_MODULUS) + _stamp_residue(st.st_ino, st.st_size)
        if target > now:
            target -= _STAMP_MODULUS
        os.utime(path, ns=(st.st_atime_ns, target))
        return path.stat()
    except OSError:
        return None


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _witness_holds(index: dict, st: os.stat_result | None) -> bool:
    """The O(1) identity/provenance check, against the transcript's
    CURRENT `stat()` `st`: not `poisoned`; the same inode (a replacement,
    e.g. a rename over the path, is refused); never shorter than
    `transcriptWitnessBytes`; at exactly that size, the very mtime AND
    ctime the witness recorded (task cure-01-03-2: an in-place rewrite
    that restores the original mtime via `os.utime` still moves ctime,
    which a normal write can never set back to an arbitrary value the way
    it can mtime -- checking mtime alone let that rewrite through); longer
    than that, an mtime carrying the engine writer's provenance stamp for
    the current (inode, size) -- the last modification was the engine's
    own append."""
    if st is None or index.get("poisoned"):
        return False
    if st.st_ino != index["transcriptInode"]:
        return False
    witness_bytes = index["transcriptWitnessBytes"]
    if st.st_size < witness_bytes:
        return False
    if st.st_size == witness_bytes:
        return (st.st_mtime_ns == index["transcriptMtimeNs"]
                and st.st_ctime_ns == index["transcriptCtimeNs"])
    return _carries_stamp(st)
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
    """Whether a live render `state` equals a checkpoint's stored `state`
    -- both in `_state_to_json`'s one canonical JSON shape, whether the
    stored copy was read back from disk or is still in memory."""
    return _state_to_json(state) == stored


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

# How many freshly-fed raw lines a live iteration accumulates, AT MOST,
# before `LiveRenderIndex.flush` first rewrites `render_index.json` on
# disk. Each rewrite still costs O(total checkpoints so far) -- the whole
# accumulated snapshot, not just the newly-fed batch -- so flushing at a
# FIXED interval (every `LIVE_FLUSH_EVERY_LINES` lines, however batched)
# makes the SUM of every flush's own cost across a whole iteration
# quadratic in the transcript's own line count: doubling the transcript
# roughly doubles both the number of flushes AND the average cost each
# one pays (PRD D.2, #275; the shape `tests/test_render_index_live_write_
# cost.py` used to pin as `docs/architecture.md`'s own "Write timing"
# claim, before this task corrected it).
#
# `flush` now schedules its NEXT publish geometrically instead
# (`_next_flush_threshold`): after publishing at `total` checkpoints, the
# next publish is not due again until `total` has at least DOUBLED (or
# grown by `every`, whichever asks for more lines first, so an iteration
# still short of `LIVE_FLUSH_EVERY_LINES` checkpoints behaves exactly as
# before). Publishing at thresholds `every, 2*every, 4*every, ...` sums to
# a geometric series bounded by `2 * N` for a transcript of `N` lines --
# LINEAR, not quadratic -- while every published snapshot is still a
# complete, independently-valid index (task 012, PRD D.2/D.3: nothing
# about `load_index`'s own contract or an already-published snapshot's
# correctness changes, only how OFTEN a bigger snapshot gets rewritten).
LIVE_FLUSH_EVERY_LINES = 20


def _next_flush_threshold(total: int, every: int) -> int:
    """The checkpoint COUNT `total` (a completed publish, or 0 for a
    writer that has never published) at which `LiveRenderIndex.flush`
    should next actually rewrite the index, given a base batch size
    `every`: whichever of `total + every` (never publish less often than
    the original fixed cadence) and `total * 2` (never publish more often
    than a doubling would need) asks for MORE lines first -- the standard
    amortized-doubling schedule that keeps the sum of every publish's own
    O(checkpoints-at-that-point) cost linear in the FINAL checkpoint
    count, not quadratic."""
    return max(total + every, total * 2)


def _state_to_json(state: dict) -> dict:
    """`log_render`'s render-state dict in exactly the shape a JSON round
    trip gives back: `collapse_unresolved`'s `set` becomes a sorted list
    and `tool_pending`'s `(name, args)` tuples become lists, so an
    in-memory checkpoint and the same checkpoint read back from disk
    compare equal. A DEEP copy: `tool_args`/`tool_pending` are dicts the
    live render state keeps mutating, and a checkpoint sharing them would
    silently describe a later line's state instead of its own."""
    out = dict(state)
    out["collapse_unresolved"] = sorted(state.get("collapse_unresolved") or ())
    return json.loads(json.dumps(out))


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
        # Task cure-01-01-4: `poisoned`, once True, never clears itself --
        # only a fresh `LiveRenderIndex`/a from-scratch `compute_render_index`
        # rebuild starts clean. `_witness_checkpoints` is exactly the
        # `checkpoints` prefix this object last PUBLISHED (via `flush`),
        # never the full current `_checkpoints` -- so the NEXT flush's own
        # bounded self-check verifies precisely the coverage a reader could
        # already have trusted. `_custody` is the transcript's
        # (inode, size, mtime, ctime) exactly as this object's own last write +
        # provenance stamp (`append`), or its first publish, left it: any
        # difference found before the next write or publish is a
        # modification this writer did not make.
        self._poisoned = False
        self._witness_checkpoints: list[dict] = []
        self._custody: tuple[int, int, int] | None = None
        # Task 012 (PRD D.2): the checkpoint COUNT at which `flush` next
        # actually publishes -- `None` until the first `flush` call picks
        # the initial threshold from whatever `every` it is given then.
        self._next_flush_threshold: int | None = None

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

    def snapshot(self, witness: tuple[int, int, int, int] | None = None) -> dict:
        """The index payload as of every line fed so far, plus the given
        identity witness -- `(inode, size, mtime_ns, ctime_ns)` of the
        transcript as of the moment the coverage is vouched for
        (`transcriptInode`/`transcriptWitnessBytes`/`transcriptMtimeNs`/
        `transcriptCtimeNs`; all zero, which no real transcript matches,
        when none is given) -- and `poisoned`."""
        ino, size, mtime_ns, ctime_ns = witness if witness is not None else (0, 0, 0, 0)
        return {"version": INDEX_VERSION, "transcriptBytes": self._offset,
                "segments": [dict(s) for s in self._segments],
                "checkpoints": [dict(c) for c in self._checkpoints],
                "transcriptInode": ino,
                "transcriptMtimeNs": mtime_ns,
                "transcriptCtimeNs": ctime_ns,
                "transcriptWitnessBytes": size,
                "poisoned": self._poisoned}

    def _check_custody(self, output_path: Path) -> None:
        """Poison this writer when the transcript no longer looks exactly
        as its own last write/publish left it (`_custody`) -- a rewrite,
        a replacement, a truncation or a foreign append in between."""
        if self._poisoned or self._custody is None:
            return
        try:
            key = _stat_key(output_path.stat())
        except OSError:
            key = None
        if key != self._custody:
            self._poisoned = True

    def append(self, out, data: str | bytes, output_path: Path) -> None:
        """The engine's own live write path (`engine/runner.py`'s
        `pump()`): check custody, write `data` (one raw transcript line)
        to the already-open append handle `out` and flush it, `feed` it,
        then stamp the transcript's mtime with the provenance marker for
        its new (inode, size) (`_stamp`) and take custody of that state.

        Custody is checked BEFORE the write, because the write itself
        moves the mtime and would erase the evidence: the moment it finds
        a modification this writer did not make, the index is poisoned
        AND republished at once (so no reader trusts it even for a
        moment), and this writer never stamps again -- every later append
        leaves the kernel's own unstamped mtime, so a reader holding an
        older, unpoisoned copy of the index refuses it too."""
        itdir, name = output_path.parent, output_path.name
        was_poisoned = self._poisoned
        self._check_custody(output_path)
        if self._poisoned and not was_poisoned:
            self.flush(itdir, force=True, output_filename=name)
        out.write(data)
        out.flush()
        self.feed(data.encode() if isinstance(data, str) else data)
        if self._poisoned:
            return
        st = _stamp(output_path)
        self._custody = _stat_key(st) if st is not None else None
        if st is None or st.st_size != self._offset:
            # Either unstatable, or the file does not hold exactly what
            # this writer has fed: nothing here can vouch for it.
            self._poisoned = True

    def flush(self, itdir: Path, *, force: bool = False,
              every: int = LIVE_FLUSH_EVERY_LINES,
              output_filename: str = _OUTPUT_FILENAME) -> bool:
        """Persist the current snapshot to `itdir / INDEX_FILENAME` once
        the checkpoint count has reached the next scheduled publish
        threshold (`_next_flush_threshold`, task 012/PRD D.2 -- see
        `_next_flush_threshold`'s own docstring for the geometric schedule
        that keeps the total cost of every publish across a whole
        iteration LINEAR in the transcript's own line count), or always
        when `force` (the pump loop's own end-of-stream call, so the last
        partial batch is never left unwritten). Never raises: exactly
        `collapse_index.write_index`'s own failure contract -- a write
        that fails costs nothing more than a missing/stale index, which
        every reader already treats as an ordinary fall-back case.

        Task cure-01-01-4: BEFORE publishing anything new, checks custody
        (the transcript must still look exactly as this writer's own last
        `append`/publish left it -- a caller that grew it without going
        through `append` has broken the chain of custody too, so this
        writer can no longer vouch for the covered prefix and poisons
        itself), and self-checks whatever it last published
        (`_witness_checkpoints`) against the transcript's current bytes,
        bounded to `VERIFY_WINDOW_BYTES` exactly like a reader's own
        `load_index`. The very first publish of a writer that has never
        appended takes custody of the transcript as it stands (stamping
        it), the way a from-scratch build vouches for what it read.
        `poisoned`, once set, is carried into every later snapshot.
        Returns whether it actually wrote."""
        total = len(self._checkpoints)
        threshold = (every if self._next_flush_threshold is None
                    else self._next_flush_threshold)
        if not force and total < threshold:
            return False
        output_path = itdir / output_filename
        if self._custody is None and not self._poisoned:
            st = _stamp(output_path)
            if st is not None:
                self._custody = _stat_key(st)
        else:
            self._check_custody(output_path)
        if not self._poisoned and self._witness_checkpoints:
            if not _covered_prefix_matches(output_path, self._witness_checkpoints):
                self._poisoned = True
        try:
            tmp = itdir / (INDEX_FILENAME + ".tmp")
            tmp.write_text(json.dumps(self.snapshot(self._custody)))
            tmp.replace(itdir / INDEX_FILENAME)
        except OSError:
            return False
        self._witness_checkpoints = list(self._checkpoints)
        self._next_flush_threshold = _next_flush_threshold(total, every)
        return True


def compute_render_index(output_path: Path) -> dict:
    """Full, from-scratch rebuild: render every line of `output_path` once
    through a fresh `LiveRenderIndex`, exactly as `pump()` would have, and
    return its final snapshot. A file that does not exist yet reads as
    empty (matching a freshly begun, not-yet-run iteration, same as
    `collapse_index.compute_collapse_index`).

    Its identity witness is the transcript's `stat()` taken BEFORE the
    read, kept only when a second `stat()` after the read still agrees
    with it and with the byte count actually read; otherwise (the file
    changed underneath the read) the witness is left all-zero, which no
    real transcript matches, so the result is never trusted by a reader.
    Pure: it never writes anything, the transcript's own metadata
    included (`cli/ui_server`'s hub cache builds with it)."""
    try:
        before = output_path.stat()
    except OSError:
        before = None
    try:
        raw = output_path.read_bytes()
    except FileNotFoundError:
        raw = b""
    try:
        after = output_path.stat()
    except OSError:
        after = None
    live = LiveRenderIndex()
    for raw_line in raw.splitlines(keepends=True):
        live.feed(raw_line)
    witness = None
    if (before is not None and after is not None
            and _stat_key(before) == _stat_key(after) and before.st_size == len(raw)):
        witness = _stat_key(before)
    return live.snapshot(witness)


def write_index(itdir: Path, output_filename: str = _OUTPUT_FILENAME) -> None:
    """Full rebuild + atomic write for one iteration's transcript --
    `engine/loop.py`'s own end-of-iteration bookkeeping call, mirroring
    `collapse_index.write_index`'s call at the same call site. The engine
    owns this transcript, so -- unlike a hub-side `compute_render_index`
    -- it also stamps the transcript's mtime with the provenance marker
    (`_stamp`) once the rebuild's own witness is known to describe exactly
    the bytes it read, and publishes that stamped witness (mtime AND
    ctime, task cure-01-03-2). Never raises."""
    try:
        output_path = itdir / output_filename
        index = compute_render_index(output_path)
        key = (index["transcriptInode"], index["transcriptWitnessBytes"],
               index["transcriptMtimeNs"], index["transcriptCtimeNs"])
        if index["transcriptInode"]:
            try:
                current = _stat_key(output_path.stat())
            except OSError:
                current = None
            if current == key:
                st = _stamp(output_path)
                if st is not None and (st.st_ino, st.st_size) == key[:2]:
                    index["transcriptMtimeNs"] = st.st_mtime_ns
                    index["transcriptCtimeNs"] = st.st_ctime_ns
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
    a covered prefix no larger than the window is verified in full);
    and the identity/provenance witness holds (`_witness_holds`, task
    cure-01-01-4, the petition remedy for cure-01-03 criterion (1)): not
    `poisoned`, the same inode, and either the witnessed size with the
    witnessed mtime AND ctime, or a longer transcript whose mtime carries
    the engine writer's provenance stamp for its current (inode, size) --
    so a same-size rewrite or replacement of a covered line is refused
    even OUTSIDE the window and the tail walk, whether or not the
    transcript has grown since, at the O(1) cost of one `stat()`. Task
    cure-01-03-2 (the petition remedy for cure-01-03 and cure-01-01-4
    alike): the witnessed-size case checks ctime alongside mtime, so an
    ordinary write that restores the original mtime via `os.utime` after
    an in-place rewrite -- which cannot restore ctime the same way -- is
    refused too, not only a rewrite that leaves mtime moved.

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
    # Task cure-01-01-4 (petition remedy for cure-01-03 criterion (1)):
    # the identity/provenance witness is checked before anything else
    # costs a byte (`_witness_holds`, a single `stat()`) -- a same-size
    # rewrite or replacement of ANY covered line, however far outside
    # `VERIFY_WINDOW_BYTES` and the tail walk it sits, and whether or not
    # the transcript has grown since, is refused here.
    for key in ("transcriptInode", "transcriptMtimeNs", "transcriptCtimeNs",
                "transcriptWitnessBytes"):
        value = index.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
    if not isinstance(index.get("poisoned"), bool) or index["poisoned"]:
        return None
    if index["transcriptWitnessBytes"] < transcript_bytes:
        return None
    try:
        actual_stat = (itdir / output_filename).stat()
        actual_size = actual_stat.st_size
    except OSError:
        actual_stat = None
        actual_size = 0
    if transcript_bytes > actual_size:
        return None
    if checkpoints and not _witness_holds(index, actual_stat):
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
    checkpoints = index["checkpoints"]
    covered = index["transcriptBytes"]
    # Task cure-01-01-4: ONE consistent transcript/index snapshot. The
    # witness is (re)checked against this very `stat()` -- a passed-in
    # (hub-cached) index never went through `load_index`, and an on-disk
    # one may have been loaded a moment ago -- everything below reads only
    # up to this `stat()`'s size, and the witness is checked once more
    # after the reads: a modification that landed while this call was
    # reading is refused rather than half-seen, while the engine's own
    # stamped appends in the meantime (bytes past `size`, never read here)
    # are not.
    try:
        before = out_path.stat()
    except OSError:
        before = None
    if checkpoints and not _witness_holds(index, before):
        return None
    size = before.st_size if before is not None else 0
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
    if checkpoints:
        try:
            after = out_path.stat()
        except OSError:
            return None
        if (after.st_ino != before.st_ino or after.st_size < before.st_size
                or not _witness_holds(index, after)):
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


def bounded_tail(run_root: Path, n: int, *, index: dict | None = None,
                 on_missing: Callable[[int], dict | None] | None = None) -> list[str] | None:
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

    `on_missing` (task cure-01-02-2, PRD C.4, D.1; DoD 3): called with an
    iteration NUMBER, at most once per number per call, exactly when that
    one iteration's own attempt above -- the passed-in `index` for the
    latest one, its own on-disk `render_index.json` for every earlier one
    -- could not answer by itself (missing, refused, or disagreeing with
    the transcript). Lets a caller (`ui_server.
    _absent_index_cache_lookup_or_rebuild_refused` and `ui_server.
    _absent_index_cache_fill`, its own two callback-builders) build and
    cache that ONE iteration's own index, off its own on-disk transcript,
    and hand it straight back for an immediate retry
    -- so a tail whose backward walk needs several COLD iterations pays
    each of their scan costs at most once per call, and never again once
    cached, instead of this function simply giving up on the first cold
    one it meets and forcing the caller back to an unbounded, un-cached,
    whole-transcript growth scan. Returning `None` from `on_missing` means
    that iteration genuinely cannot be resolved right now (e.g. its
    transcript could not be read) -- the walk still ends in `None`
    exactly as it would with no callback at all.

    `None` -- never a wrong-but-plausible answer -- when some iteration in
    the backward walk cannot answer by itself even after `on_missing`: no
    iteration at all, no valid index (on disk, passed in, or freshly
    built) for an iteration the walk actually needs, or a transcript that
    disagrees with its index. The caller (`ui_server.rendered_log_lines`,
    on both its live and dead-run surfaces) then falls back to the
    growth-based fetch."""
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
        if result is None and on_missing is not None:
            fresh = on_missing(number)
            if fresh is not None:
                result = _one_iteration_tail_text(run_root, number, n, index=fresh)
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
