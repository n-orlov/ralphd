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
transcript it claims to describe (size) before trusting it at all.
Absence, truncation/malformed JSON, or a mismatched consistency check all
return `None` -- the caller's signal to fall back to a full scan of the
transcript (G.4: never an error, never a wrong render, only ever slower).
`resolve_flags_at_offset` is the one fallback-safe consumer this task
ships: given a byte offset into one iteration's transcript, it answers
the three collapse flags AS OF that offset, from the index in O(1)
comparisons when the index validates, or by scanning the transcript up to
that offset (byte-for-byte the same computation `log_merge.
_prior_collapse_state` already does over an in-memory line list) when it
does not. G.6's growth-ladder replacement in the engine's two real render
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

import json
from pathlib import Path

from .log_merge import OUTPUT_FILENAME as _OUTPUT_FILENAME

INDEX_FILENAME = "collapse_index.json"
INDEX_VERSION = 1

# Mirrors `log_render.render_log_line`'s own event dispatch (and
# `log_merge._prior_collapse_state`'s already-in-memory-line version of the
# same three transitions): a `message_update` event's `text_delta` sets
# `text_seen`, `thinking_start`/`thinking_delta` sets `thinking_seen`; a
# `tool_execution_start` OR `tool_execution_end` sets `toolcall_seen`.
_REQUIRED_KEYS = ("version", "transcriptBytes",
                  "textSeenAtByte", "thinkingSeenAtByte", "toolcallSeenAtByte")


def compute_collapse_index(output_path: Path) -> dict:
    """Scan one iteration's ENTIRE transcript file exactly once and return
    the side-index payload: the byte offset (start of the line that set
    it) at which each of the three collapse flags first becomes true
    within this one file, or `None` when that flag never fires in this
    iteration at all -- plus the file's own size, recorded for the
    read side's consistency check (`load_index`).

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
    text_at = thinking_at = toolcall_at = None
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
                    if sub == "text_delta" and text_at is None:
                        text_at = offset
                    elif (sub in ("thinking_start", "thinking_delta")
                          and thinking_at is None):
                        thinking_at = offset
                elif (etype in ("tool_execution_start", "tool_execution_end")
                      and toolcall_at is None):
                    toolcall_at = offset
        offset += len(raw_line)
    return {
        "version": INDEX_VERSION,
        "transcriptBytes": len(raw),
        "textSeenAtByte": text_at,
        "thinkingSeenAtByte": thinking_at,
        "toolcallSeenAtByte": toolcall_at,
    }


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
    carries every required key, and its recorded `transcriptBytes` still
    equals the transcript's current size. Otherwise returns `None` --
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
    i.e. exactly what `log_merge._prior_collapse_state` would compute by
    scanning the transcript up to `cut_offset` line by line, resolved in
    O(1) offset comparisons against `load_index`'s validated side index
    when one exists, or by that same scan (read here, not from an
    already-in-memory line list) when it does not.

    This is the one consumer this task wires up: absence, truncation, or a
    consistency-check mismatch (`load_index` returning `None`) all fall
    through to the scan below rather than raising or guessing -- so every
    one of those three cases still renders correctly, only slower (G.4).
    Eliminating the scan's OWN cost in the two real render paths (the
    `/logs` handler and the on-disk fallback), so the fallback itself is
    never paid on the hot path either, is G.6/task 021's job -- this
    function's fallback branch is deliberately the plain, obviously
    correct scan it replaces, not a second optimisation.
    """
    index = load_index(itdir, output_filename)
    if index is not None:
        def _seen(key: str) -> bool:
            at = index.get(key)
            return at is not None and at < cut_offset
        return {"textSeen": _seen("textSeenAtByte"),
                "thinkingSeen": _seen("thinkingSeenAtByte"),
                "toolcallSeen": _seen("toolcallSeenAtByte")}
    try:
        with (itdir / output_filename).open("rb") as f:
            raw = f.read(max(cut_offset, 0))
    except OSError:
        raw = b""
    text_seen = thinking_seen = toolcall_seen = False
    for raw_line in raw.splitlines(keepends=True):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "message_update":
            sub = (ev.get("assistantMessageEvent") or {}).get("type")
            if sub == "text_delta":
                text_seen = True
            elif sub in ("thinking_start", "thinking_delta"):
                thinking_seen = True
        elif etype in ("tool_execution_start", "tool_execution_end"):
            toolcall_seen = True
    return {"textSeen": text_seen, "thinkingSeen": thinking_seen,
            "toolcallSeen": toolcall_seen}
