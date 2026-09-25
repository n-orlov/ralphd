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
    `renderedLines` (how many PRETTY lines `log_render.render_to_lines`
    has produced for the whole prefix up to and including that line), and
    `state` (`log_render.new_render_state()`'s own dict, JSON-round-
    tripped by `_state_to_json`/`_state_from_json`, as of that same
    point). A bounded reader can therefore pick any checkpoint at or
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
inconsistent, or that claims to cover MORE than the transcript's actual
current size (which can only mean a stale index left over from before a
truncation/rewrite, `collapse_index.load_index`'s own one real disagreement
case). Consuming that "uncovered suffix" bound through the real client-
facing call is task 013's job.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from . import collapse_index as _collapse_index
from .cli.log_render import new_render_state, render_to_lines
from .log_merge import OUTPUT_FILENAME as _OUTPUT_FILENAME
from .log_merge import boundary_line as _boundary_line
from .log_merge import iteration_dir as _iteration_dir
from .log_merge import iteration_numbers as _iteration_numbers
from .log_merge import iteration_output_path as _iteration_output_path

INDEX_FILENAME = "render_index.json"
INDEX_VERSION = 1

_REQUIRED_KEYS = ("version", "transcriptBytes", "segments", "checkpoints")
_SEGMENT_KEYS = _collapse_index._SEGMENT_KEYS
_CHECKPOINT_KEYS = ("byteOffset", "renderedLines", "state")

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
    unchanged."""
    out = dict(state)
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
            "state": _state_to_json(self._state)})
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
    exact case `collapse_index.load_index` also refuses).

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
        if not isinstance(state, dict):
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
    return index


# Task 013: how far short of "enough" the index's OWN covered prefix can
# see the RENDER growing, without walking any of the SILENT bytes in
# between -- the whole point of writing `checkpoints` at all (see this
# module's own top-of-file rationale). One entry per raw transcript line
# (never coarser), so a binary search over `renderedLines` finds the
# LATEST line whose cumulative count still falls `content_needed` or more
# short of the index's own covered total in O(log checkpoints) -- no disk
# read at all, since `checkpoints` is already the parsed JSON in hand.
_FORWARD_READ_CHUNK = 1 << 12  # 4096 -- see `_render_forward_from_seed`.


def _seed_checkpoint_for_tail(index: dict, content_needed: int) -> tuple[int, dict] | None:
    """`(byte_offset, deserialized_state)` to resume rendering FROM so that
    continuing forward reproduces exactly the last `content_needed`
    RENDERED lines the index's own covered prefix would render -- the
    LATEST checkpoint whose own cumulative `renderedLines` still falls
    `content_needed` or more short of the covered total. `None` when the
    covered prefix cannot promise `content_needed` lines at all (fewer
    lines exist in the whole prefix it covers): the caller falls back to
    an older iteration, or the growth-based outer fetch."""
    checkpoints = index.get("checkpoints") or []
    if not checkpoints:
        return None
    total = checkpoints[-1]["renderedLines"]
    if total < content_needed:
        return None
    threshold = total - content_needed
    lo, hi = 0, len(checkpoints)
    while lo < hi:
        mid = (lo + hi) // 2
        if checkpoints[mid]["renderedLines"] <= threshold:
            lo = mid + 1
        else:
            hi = mid
    idx = lo - 1
    if idx < 0:
        return 0, new_render_state()
    cp = checkpoints[idx]
    return cp["byteOffset"], _state_from_json(cp["state"])


def _render_forward_from_seed(output_path: Path, seed_offset: int, seed_state: dict,
                              content_needed: int, size_cap: int) -> list[str]:
    """The forward, SEEDED sibling of `log_render.render_tail_lines`'s own
    backward growth (task 013): read `output_path` from `seed_offset`
    onward, in growing chunks (`_FORWARD_READ_CHUNK`, doubling), re-
    rendering a FRESH copy of `seed_state` each time, until the render has
    produced at least `content_needed` new lines or `size_cap` (the
    transcript's own current size) is exhausted. Bytes read stay
    proportional to `content_needed` plus a fixed allowance -- never to how
    far `seed_offset` sits from `size_cap` -- because `seed_offset` is
    itself already the LATEST checkpoint the index's own per-line
    granularity could offer short of the answer
    (`_seed_checkpoint_for_tail`), so the very next few raw lines are
    always exactly where the missing render happens, however much silent
    transcript follows them all the way to `size_cap`."""
    if content_needed <= 0 or seed_offset >= size_cap:
        return []
    chunk = _FORWARD_READ_CHUNK
    lines: list[str] = []
    with output_path.open("rb") as f:
        f.seek(seed_offset)
        data = b""
        while True:
            target = min(seed_offset + chunk, size_cap)
            to_read = target - (seed_offset + len(data))
            if to_read > 0:
                data += f.read(to_read)
            at_cap = seed_offset + len(data) >= size_cap
            # A chunk that has not yet reached `size_cap` may stop mid-line
            # -- render only whatever WHOLE lines it holds so far (never a
            # truncated trailing line, which `render_log_line` would report
            # as malformed) and let the next, larger chunk pick that partial
            # line back up from its own start (never re-reading bytes this
            # one already has: `data` keeps growing, never resets).
            usable = data if at_cap else data[:data.rfind(b"\n") + 1]
            text = usable.decode("utf-8", errors="replace")
            lines = render_to_lines(text, tty=False, state=copy.deepcopy(seed_state))
            if len(lines) >= content_needed or at_cap:
                return lines
            chunk *= 2


def bounded_tail(run_root: Path, n: int) -> list[str] | None:
    """The exact tail of the last `n` rendered lines for `run_root`'s MOST
    RECENT iteration alone, served entirely through its own render index
    (task 013) -- bytes read off the transcript stay proportional to `n`
    plus a fixed allowance, regardless of however much silent transcript
    sits between the deciding line and the transcript's own end (exactly
    the gap `tests/test_hub_real_render_cap_whole_call.py`'s PRD Q fixture
    -- and its silent-prelude variants -- exposed against the outer
    doubling fetch this bypasses).

    `None` -- never a wrong-but-plausible answer -- whenever this one
    iteration's own render index cannot promise `n` lines by itself: no
    iteration at all, no validated index for the latest one, or its own
    covered prefix falling short. The caller (`ui_server.rendered_log_lines`,
    on both its live and dead-run surfaces) falls back to the older,
    growth-based outer fetch in every such case -- an OLDER iteration
    supplying the remainder is a later task's job, not this one's.

    A finished iteration's own trailing `ralphd.iteration` "end" line is
    never part of the transcript file itself -- `log_merge.boundary_line`
    synthesizes it -- so it is rendered separately, through the very same
    `log_render.render_to_lines` every other reader uses (never hand-
    rolled here), and counted against `n` before any transcript content is
    even looked at, since it is unconditionally the newest line whenever
    it exists."""
    numbers = _iteration_numbers(run_root)
    if not numbers:
        return None
    last = numbers[-1]
    itdir = _iteration_dir(run_root, last)
    index = load_index(itdir)
    if index is None:
        return None
    meta_path = itdir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    boundary_rendered: list[str] = []
    if meta.get("endedAt"):
        boundary_rendered = render_to_lines(_boundary_line(meta, "end"), tty=False,
                                            state=new_render_state())
    content_needed = max(n - len(boundary_rendered), 0)
    content_lines: list[str] = []
    if content_needed > 0:
        seed = _seed_checkpoint_for_tail(index, content_needed)
        if seed is None:
            return None
        seed_offset, seed_state = seed
        out_path = _iteration_output_path(run_root, last)
        try:
            size = out_path.stat().st_size
        except OSError:
            return None
        content_lines = _render_forward_from_seed(out_path, seed_offset, seed_state,
                                                   content_needed, size)
        if len(content_lines) < content_needed:
            return None
    combined = content_lines + boundary_rendered
    if len(combined) < n:
        return None
    return combined[-n:]
