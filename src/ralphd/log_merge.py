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


def boundary_line(meta: dict, event: str, collapse_seed: dict | None = None) -> str:
    """Synthesize the `ralphd.iteration` start/end line for one iteration.

    Not written to disk by anyone: it is derived from `meta.json` so that a
    reader can tell iterations apart in a flat stream of transcript lines.

    `collapse_seed` (task 006, PRD F) is an optional
    `{"textSeen": bool, "thinkingSeen": bool, "toolcallSeen": bool}` blob,
    attached only to a "start" line, that tells `log_render.render_log_line`
    the `text_seen`/`thinking_seen`/`toolcall_seen` collapse-guard values a
    REAL iteration start already carries from raw content this boundary's
    OWN emitter chose not to include (see `merge_entries_tail`'s per-
    iteration bounded read). It may also carry `"unresolved"`, the sorted
    names of the flags above whose boolean is a bounded reader's FALLBACK
    rather than a measurement (task cure-01-03-2, PRD Q.1 --
    `_unresolved_seed_flags`), and `"toolArgs"` (task cure-01-02). A genuine, uncut iteration start passes `None`
    (the default) -- the resulting JSON is byte-identical to before this
    task, and `render_log_line` seeds every flag `False` exactly as a fresh
    `new_render_state()` always has, so nothing about an ordinary boundary
    line changes. `event == "end"` never carries a seed: an end boundary's
    own rendering (`_render_boundary`) never reads render-state collapse
    flags, and whatever comes after it is either the run's own end or a
    following iteration's OWN "start" line, which seeds itself."""
    line: dict = {"type": BOUNDARY_TYPE, "event": event,
                  "number": meta.get("number"), "phase": meta.get("phase"),
                  "model": meta.get("model"), "approach": meta.get("approach"),
                  "startedAt": meta.get("startedAt")}
    if event == "end":
        line["exitCode"] = meta.get("exitCode")
        line["error"] = meta.get("error")
        line["usage"] = meta.get("usage")
        line["endedAt"] = meta.get("endedAt")
    elif collapse_seed:
        line["collapseSeed"] = collapse_seed
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

# Task cure-01-02 (#195), widened by cure-01-01-2: once `needed` is
# satisfied, `_tail_lines_of_file` may still grow `prefix_lines` further
# purely to give the collapse-state seed a fighting chance of reaching
# whichever earlier event actually establishes it (a many-delta
# thinking/text run, or a tool call's own start), when that event sits
# further back than the single chunk `needed` alone happened to pull in.
#
# THIS BOUNDS THE CHUNK SIZE, NOT THE LOOK-BACK, AND IS NEVER A STOP
# CONDITION (cure-01-01-2, second attempt): the seed look-back doubles its
# read chunk at most this many times and then keeps reading in
# steady-sized chunks for as long as it still has something concrete to
# resolve, so one `read` syscall can never balloon past
# `_TAIL_READ_CHUNK << _SEED_LOOKBACK_EXTRA_GROWTHS` bytes.
#
# Reaching this many doublings is therefore NOT an argument that the seed
# is complete and NOT a safe restart (E.3): the look-back stops for one
# of exactly two real reasons -- `reached_start` (byte 0 of the file; by
# `_tail_lines_of_file`'s own contract there is provably no unread
# history left) or `_should_extend_seed_lookback` finding nothing left to
# resolve (see that function: it is never fooled merely because the
# single oldest line it happens to have read is an opaque/`"ignored"`
# filler event). Before cure-01-01-2's second attempt this constant WAS
# the loop's third exit, and exhausting it silently published an
# incomplete seed as if it were a genuine iteration start -- a tool call
# whose `tool_execution_start` sat one doubling further back than the cap
# allowed rendered as bare `bash ✓ ok` instead of
# `bash $ printf cap-needle ✓ ok`.
_SEED_LOOKBACK_EXTRA_GROWTHS = 12

# How many BYTES of already-read OLDER lines (`prefix_lines`) the
# look-back's SPECULATIVE trigger alone -- trigger 3 below, "the retained tail's own
# boundary is an event that tells us nothing, so we cannot certify an
# unresolved flag `False` from what we have read" -- may accumulate before
# it stops asking for more (task cure-01-02, PRD P.1/P.5, review's
# `test_P_interleaved_byte_ceiling[one-extra-trailing-ignored-*]`).
#
# WHY THIS ONE TRIGGER AND NOT THE OTHER TWO. Triggers 1 and 2 each name a
# CONCRETE needle whose existence the retained tail itself already
# evidences -- a `_TYPE_FLAG_GROUPS` event of the very type `tail_lines`
# already carries (trigger 1), or the `tool_execution_start` that a
# `tool_execution_end` inside `tail_lines` proves must exist further back
# (trigger 2) -- so reading until it is found terminates on the needle,
# and the shipped scenarios that depend on that are deliberately far back
# (`test_log_render_bounded_tail.py`'s scenario 3: a tool call's own start
# ~9.2 MB / 9,000 opaque events behind its end). Those two stay unbounded.
# Trigger 3 names NO needle: it fires on not-knowing, and "an event that
# might set this flag might sit somewhere further back" stays true, event
# for event, all the way to byte 0 of an arbitrarily long transcript. That
# is precisely review's falsifying measurement: shifting P's interleaved
# 200-line suffix by ONE trailing `ignored` event puts an opaque line at
# `tail_lines[0]`, and the look-back then walked the WHOLE grown file
# TWICE -- 49,430 bytes at 10 older ignored events, 6,826,610 at 100,000
# (files of 24,715 / 3,413,305) -- past P's 1,000,000-byte ceiling,
# chasing a `textSeen` no line anywhere in that file ever sets. Whether
# that walk happened at all depended on nothing but which member of the
# interleaving cycle the retained tail's oldest line happened to be; the
# ceiling now holds for either alignment, because THIS bound, not that
# alignment, is what stops the speculative walk.
#
# WHY THE BUDGET IS COUNTED IN BYTES, NOT LINES (task cure-01-02-2, PRD
# P.1/P.5's own materiality rule, review's
# `test_P_older_padding_cannot_buy_speculative_reads`). A LINE-count
# budget never bounds what this reader is actually measured on -- BYTES
# READ off `output.jsonl` -- because how many bytes an event occupies is
# chosen by whoever wrote the transcript, not by this reader: the SAME
# 200-line rendered tail behind the SAME 100,000 older non-rendering
# events, walked with the SAME line budget, cost 393,216 bytes unpadded,
# 851,968 at 100 bytes of padding per older event and 2,752,512 at 500 --
# one line count, three costs, the last of them over the unmoved
# 1,000,000-byte ceiling. Only a budget counted in BYTES has a cost
# padding cannot move at all.
#
# WHY A BYTE BUDGET IS AFFORDABLE, where the line budget's own comment
# argued it was not. That argument rested on the speculative search
# reading BACKWARDS, chunk by chunk, from the retained tail: the shipped
# gap-after-thinking and Q scenarios hide their genuine needle behind
# 1,000 opaque events padded to 1,000 bytes each, so a BACKWARD search
# has to be allowed >= 1.05 MB to cross that gap -- more than P's whole
# ceiling, read twice. But the state this trigger speculates about is
# `_prior_collapse_state`, and every flag in it is set by its FIRST
# occurrence in the iteration: whatever streamed text/thinking or open
# tool call an omitted prefix establishes, it establishes at the
# iteration's BEGINNING -- not immediately before the retained tail. So
# the speculative extension reads the file's HEAD instead
# (`_tail_lines_of_file`'s head probe: forward from byte 0, in fixed
# `_TAIL_READ_CHUNK` steps), never the arbitrarily long middle. Those
# same scenarios resolve inside the FIRST head chunk (their
# `tool_execution_start`, `text_delta` and `thinking_start` all sit
# within the first ~1 KB of the iteration, AHEAD of the padded gap), so
# this budget never has to be big enough to cross a gap at all -- only
# big enough to read the head, where the answer actually lives, on top of
# whatever `prefix_lines` the backward read for `needed` already pulled
# in for free.
#
# 131,072 bytes (measured, task cure-01-02-2), or the retained tail's own
# byte size when that is larger -- see the constant's own comment below
# for why the budget has to scale with what renders and not merely sit at
# a fixed floor.
#
# STOPPING HERE IS NOT A CLAIM THE SEED IS COMPLETE, and it is not a
# restart: `reached_start` stays False (its own contract is byte 0 of the
# file and nothing else), so no caller may treat the cut point as a
# genuine iteration start on the strength of this bound -- exactly the
# distinction `_SEED_LOOKBACK_EXTRA_GROWTHS`'s own comment above draws.
# What it does mean is that after this many bytes of finding nothing, the
# search stops and the flag is published AS UNRESOLVED (task cure-01-03-2:
# `_unresolved_seed_flags` names it on the seed) beside the
# `_prior_collapse_state` default a renderer then has to draw from -- the
# one thing a bounded reader can do when the transcript format carries no
# record of where an iteration's own streaming began (a format change and a
# side index are both outside this wave's PRD, which is why this is a bound
# and not a lookup). Exhausting the budget is therefore explicitly NOT a
# certification that the omitted prefix set nothing: that distinction is the
# whole of PRD Q.1, and it is why the flag's name travels with the value.
#
# NOR IS RAISING THIS FLOOR THE CURE for a setter that sits deeper than it
# (task cure-01-03-2, second attempt). Review's own silent-prelude variant
# puts the `textSeen` setter 1,067,000 bytes from byte 0 (and 10,720,791 from
# the file's end), so every byte range containing it is larger than P.1/P.5's
# ENTIRE 1,000,000-byte per-call ceiling: a floor big enough to reach it would
# spend more than that whole ceiling on P's own fixtures, whose setter does not
# exist anywhere in the file at all. That is the clause conflict `artifacts/
# reports/v0.22-petitions.md` petitions (task cure-01-01-3 filed it in the
# PRD-designated file there; no adjudication has landed), and it is why the
# answer here is a named unresolved flag rather
# than a bigger number.
#
# 131,072 bytes, OR the retained tail's own byte size when that is larger
# (measured, task cure-01-02-2). The budget is spent on `prefix_lines`
# BYTES -- whatever the backward read for `needed` pulled in for free plus
# whatever the head probe adds -- so it has to clear the free part before
# it can buy a single head chunk, and the free part is itself as large as
# the retained tail (one `_TAIL_READ_CHUNK` beyond it at most): a FIXED
# floor alone would silently starve any request whose own retained tail is
# bigger than the floor (the shipped gap-after-thinking scenario's own
# growing-bound retry asks for 256 raw lines of 1,000-byte filler, 256 KB
# of retained tail, and then never reads the head that resolves it).
# Scaling with the retained tail is the materiality rule itself: what a
# bounded reader may spend is set by the SIZE OF WHAT IT RENDERS, never by
# how much older history happens to sit in the same file.
#
# P's own grown fixture ends at ~192 KB per read pass -- one 64 KB
# backward chunk for its (small, unpadded) 200-line tail, then at most two
# 64 KB head chunks before the floor is spent -- so its two-pass total
# (`ui_server.rendered_log_lines`'s growing-bound retry) stays ~384 KB:
# under the unmoved 1,000,000-byte ceiling and under `large_bytes <
# small_bytes * 10` (the small fixture's own 49,430), at 0, 100 or 500
# bytes of padding per older event alike -- padding changes how many LINES
# those ~192 KB hold, and nothing else.
#
# A fixture that never resolves (P's own; PRD E's older-thinking-marker
# fixture, though that one's `tail_lines` never activates this trigger at
# all -- see `_tail_contains_opaque_filler`'s own docstring) only ever
# gets CHEAPER as this floor shrinks, never wrong: the flag it chases
# stays unresolved however far the search is allowed to run, so lowering
# the cutoff can only reduce bytes read, never change what gets
# published. What shrinking it CAN break is the other direction -- a
# scenario whose first head chunk would have resolved the flag, cut off
# before that chunk is read -- which is why the floor has to stay at least
# one `_TAIL_READ_CHUNK` clear of what the backward read accumulates on
# its own.
_SEED_LOOKBACK_SPECULATIVE_BYTE_FLOOR = 1 << 17

# The raw NDJSON types/subtypes `_prior_collapse_state` actually flips a
# collapse-guard flag on (`message_update`'s `text_delta`/`thinking_start`/
# `thinking_delta`, `tool_execution_start`, `tool_execution_end`) --
# deliberately NOT `message_end` (cure-01-01-2): `message_end` is what a
# flag's value is READ by, never what SETS one (`_prior_collapse_state`
# never touches a flag for it), and E.2's own fixture is built entirely of
# independent `message_end` lines that never set any flag at all -- so
# treating `message_end` as "relevant" here would make trigger 1 below
# fire on every one of E.2's own retained tail lines (unresolved flags
# forever, since nothing in that fixture ever resolves them) and grow all
# the way to `reached_start` regardless of `ignored_count`, exactly the
# unbounded-byte regression E.2 exists to prevent.
#
# A RAW TYPE is shared by several flags -- a `message_update` line can be
# a `text_delta` (-> `textSeen`) or a `thinking_start`/`thinking_delta`
# (-> `thinkingSeen`) -- and seeing ONE subtype of that shared type
# inside the fixed, never-re-read `tail_lines` is real, general evidence
# THIS iteration exercises the streaming mechanism `message_update`
# covers as a WHOLE, wide enough to justify searching further back for
# the OTHER flag that same raw type can also set (task cure-01-02,
# requirement F's own tool-argument/thinking-gap regressions:
# `test_f_text_before_thinking_survives_a_mid_iteration_cut` keeps a run
# of `thinking_delta` events in `tail_lines` itself, and the `text_delta`
# that resolves the STILL-unresolved `textSeen` sits further back, past
# `thinking_start`, with no `text_delta`-specific evidence anywhere
# already read to justify the search on a strictly per-subtype reading
# alone). `_TYPE_FLAG_GROUPS` names that grouping once, at the raw-TYPE
# granularity `render_log_line` itself dispatches on -- an opaque/
# unrelated type (e.g. E.2's `{"type": "ignored"}` filler) has no entry
# here at all, so seeing only that (or only `message_end`) is not, by
# itself, a reason to keep growing.
_TYPE_FLAG_GROUPS = {
    "message_update": {"textSeen", "thinkingSeen"},
    "tool_execution_start": {"toolcallSeen"},
    "tool_execution_end": {"toolcallSeen"},
}


def _tail_active_flag_groups(tail_lines: list[str]) -> set:
    """Which flags are worth searching FURTHER BACK for at all, decided
    from `tail_lines` ALONE -- never from `prefix_lines`, and never from
    an event's bare presence "anywhere already read" the way this
    module's pre-cure-01-02-3 `_has_relevant_type` treated it (task
    cure-01-02-3, #195, the second attempt at this decision).

    `tail_lines` is the one thing this function can trust as GROUND TRUTH
    for what THIS retained tail's own content actually is (it never
    changes across the look-back loop, unlike `prefix_lines`): if it
    contains a `message_update` line of ANY subtype, this iteration
    genuinely streams text/thinking, and the WHOLE `_TYPE_FLAG_GROUPS`
    entry for that type -- both `textSeen` and `thinkingSeen` -- is fair
    game to keep searching for (the counterexample `_TYPE_FLAG_GROUPS`
    documents: a `thinking_delta` run sitting IN `tail_lines` is real
    evidence worth chasing the `text_delta` that resolves a still-
    unresolved `textSeen` further back, even though that specific
    `text_delta` itself has not been read yet). If `tail_lines` contains
    NO `message_update`/`tool_execution_*` line at all -- E.2's own
    fixture, and this task's own `test_e_fixed_200_lines_with_older_
    thinking_marker_has_constant_byte_bound` fixture (`tail_lines` is 200
    independent `message_end` lines, nothing else) -- an UNRELATED
    `thinking_start` sitting arbitrarily far back in `prefix_lines` proves
    nothing about whether this tail's own still-unresolved `textSeen` will
    EVER resolve (it does not, in that fixture: no `text_delta` exists in
    the file at all), so it must not be read as license to keep growing
    towards `reached_start` regardless of how much unrelated `ignored`
    filler sits between them -- exactly the unbounded-byte regression this
    task exists to cure (18,470 of 18,470 bytes at 10 ignored entries grew
    to 3,407,060 of 3,407,060 -- the WHOLE file -- at 100,000)."""
    active: set = set()
    for raw in tail_lines:
        ev = _safe_json(raw)
        if ev is None:
            continue
        active |= _TYPE_FLAG_GROUPS.get(ev.get("type"), set())
    return active


def _safe_json(raw: str) -> dict | None:
    """Parsed JSON object for one raw NDJSON line, or `None` for anything
    blank, malformed, or not an object -- the same tolerant parsing
    `render_log_line` and `_prior_collapse_state` already apply, pulled out
    once so the seed-lookback decision below can share it."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) else None


def _dangling_tool_call_ids(tail_lines: list[str], prefix_lines: list[str]) -> set:
    """`toolCallId`s with a `tool_execution_end` in the RETAINED `tail_lines`
    but no matching `tool_execution_start` anywhere already read -- exactly
    the case PRD F's tool-argument gap names: a tool call whose start (and
    therefore its arguments) sits far enough back that a single chunk never
    reached it. Non-empty here is the second, independent trigger for
    `_should_extend_seed_lookback`: unlike a many-delta run, the intervening
    history can be arbitrary unrelated events (ignored/other tool calls),
    so this is checked by TARGETED id lookup rather than by the type of
    whatever line happens to be oldest."""
    ends: set = set()
    starts: set = set()
    for raw in tail_lines:
        ev = _safe_json(raw)
        if not ev:
            continue
        tcid = ev.get("toolCallId")
        if tcid is None:
            continue
        if ev.get("type") == "tool_execution_end":
            ends.add(tcid)
        elif ev.get("type") == "tool_execution_start":
            starts.add(tcid)
    for raw in prefix_lines:
        ev = _safe_json(raw)
        if ev and ev.get("type") == "tool_execution_start" and ev.get("toolCallId") is not None:
            starts.add(ev["toolCallId"])
    return ends - starts


# Every raw event type this module has a DEDICATED meaning for -- either
# a `_TYPE_FLAG_GROUPS` member (sets a collapse-guard flag) or one of the
# two structural types `_prior_collapse_state`/`render_log_line` both key
# off directly. Anything else (this task's own `{"type": "ignored"}`
# filler, or any other type a future wire event might introduce) is
# OPAQUE to this module -- `_tail_contains_opaque_filler` below is what
# tells `_should_extend_seed_lookback` such a line sitting INSIDE
# `tail_lines` itself carries no information either way, unlike a
# `tail_lines` built entirely of recognized types (E.2's own fixture).
_KNOWN_EVENT_TYPES = set(_TYPE_FLAG_GROUPS) | {"message_end", "ralphd.iteration"}


def _message_end_is_uninformative(ev: dict) -> bool:
    """True when a `message_end` event's own `content` holds no item that
    ACTUALLY RENDERS anything or establishes any collapse-guard state at
    all (task 007, PRD Q). An EMPTY content list (Q.2's own falsifying
    scenario: 1,000 `message_end` events padded to 1,000 bytes each via an
    unrelated top-level field, `content: []`) is the narrowest case; a
    NON-empty content list built entirely of item kinds `_render_
    message_end`/`_flags_needed_by_tail` do not recognize (Q.6's variant:
    1,000 `message_end` events each carrying one `{"type": "citation",
    ...}` item padded to 1,000 bytes) is exactly the same shape; and so,
    one level subtler (cure-01-02's own falsifying scenario), is a `text`
    item this module DOES recognize whose own `text` field is empty --
    `_render_message_end` only ever prints something for a `text` item
    when `state["text_seen"]` is still False, and an empty string is
    nothing to print either way, so such an item is silence dressed as a
    KNOWN kind. Checking `item.get("type") in (...)` alone -- kind, not
    substance -- treated that silence as fully resolved, informative
    content: exactly review's own falsifying probe (`test_Q_silent_
    message_preserves_state[empty-text-variant-*]`), a `message_end`
    carrying `content=[{"type": "text", "text": ""}]` in place of Q.2's
    `content=[]`, which renders nothing in the full transcript (the real
    stream's own earlier `text_delta` already set `text_seen`) yet made
    the bounded two-line tail on both the disk fallback and the real
    `/logs`-backed hub path replay the streamed text instead of returning
    `[thinking\u2026]` plus the iteration-end line, because a KNOWN,
    'resolved' `text` item at `tail_lines[0]` stopped `_tail_contains_
    opaque_filler` from ever admitting it does not know whether `textSeen`
    was true before it.

    A `thinking` or `toolCall` item is NOT given the same substance check:
    `_render_message_end` renders a `thinking` item's fixed `[thinking\u2026]`
    marker, and a `toolCall` item's invocation line, unconditionally once
    `state["thinking_seen"]`/`state["toolcall_seen"]` is False, regardless
    of what (if anything) the item's own payload carries -- so seeing
    EITHER kind at all is real, substantive evidence this message_end
    would render something, exactly as before this task."""
    content = (ev.get("message") or {}).get("content") or []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("thinking", "toolCall"):
            return False
        if kind == "text" and item.get("text"):
            return False
    return True


def _tail_contains_opaque_filler(tail_lines: list[str]) -> bool:
    """True when the OLDEST line in `tail_lines` -- `tail_lines[0]`, the
    retained tail's own boundary against whatever `prefix_lines` will
    grow into next -- is EITHER a raw event of a type this module has no
    dedicated meaning for at all (task cure-01-03-2, F.1/F.2/F.4, E.3,
    narrowed by task 006, PRD P: checked at the boundary alone, never
    `tail_lines` as a whole) OR a `message_end` this module recognizes
    perfectly well but that itself carries no state information either
    way (task 007, PRD Q -- see `_message_end_is_uninformative`).

    PRD Q.1's own point is exactly this pairing: an event the renderer
    *recognizes* but that produces no output is precisely as uninformative
    about the state before it as one this module cannot parse the
    meaning of at all -- recognition is not the discriminator; only
    whether the event actually SETS one of the three collapse-guard
    flags (`_TYPE_FLAG_GROUPS`) is. Before this task, an uninformative
    `message_end` sitting at `tail_lines[0]` was treated as a fully
    KNOWN, RESOLVED boundary (`_KNOWN_EVENT_TYPES` lists `message_end`
    unconditionally) -- so with no `_TYPE_FLAG_GROUPS` member anywhere in
    `tail_lines` either (trigger 1's own gate, already empty for a
    fixture built of nothing but `message_end`), growth stopped
    immediately and a still-genuinely-unresolved `textSeen`/`thinkingSeen`
    defaulted to `False` -- publishing "never streamed" for a message
    that HAD already streamed, minutes and 1,000 padded events earlier,
    and printing its replayed text where `[thinking…]` belonged (PRD Q's
    own falsifying probe, `e7072b4b`: `test_f_recognized_nonrendering_gap_
    preserves_state`). A `message_end` that DOES carry a `text`/
    `thinking`/`toolCall` item is deliberately excluded from this new
    half -- it is a READ of a flag (`_flags_needed_by_tail` already names
    it as `needed`), never silence, and trigger 1's own `tail_lines`-
    grounded evidence check is what decides whether ITS unresolved flag
    is worth searching for; conflating the two would regrow exactly the
    unbounded walk P's own `_write_iteration_with_ignored_head`/
    interleaved-suffix fixtures exist to bound (every one of THEIR
    `message_end` lines carries real `text`/`toolCall` content, so this
    new half never fires for them at all).

    cure-01-03-2's own counterexample is exactly a boundary case: 1,000
    opaque 1,000-byte `{"type": "ignored"}` filler events sit directly
    between a `message_end` (needing `textSeen`/`thinkingSeen` resolved)
    and the actual streamed text/thinking further back, so a `needed=2`
    request's own `tail_lines` is `['ignored', 'message_end']` --
    `tail_lines[0]` IS the opaque line. Every flag `tail_lines` needs is
    read by a `message_end`/`thinking_start` inside it that itself sets
    nothing (`_TYPE_FLAG_GROUPS` never names `message_end`) -- so with no
    `_TYPE_FLAG_GROUPS` member anywhere in `tail_lines` (trigger 1's own
    gate, `_tail_active_flag_groups`, already empty), NOTHING inside
    `tail_lines` ever changes an unresolved flag's value; the only
    question a still-unresolved flag poses is what the seed carried in
    at `tail_lines[0]` -- what came after it, however many further
    `message_end`/opaque lines, inherits that same answer unchanged.
    `tail_lines[0]` being unrecognized is exactly "we do not even know
    what we are missing, and must look further back to find out";
    `tail_lines[0]` being a plain, understood type (E.2's fixture:
    `tail_lines` is 200 independent `message_end` lines with no filler
    sharing the window at all; this task's own PRD-P fixture: 200
    `message_end`/`ignored` pairs, `tail_lines[0]` is the OLDEST pair's
    `message_end`, never the `ignored` beside it) answers that same
    question with a NAME (`_flags_needed_by_tail`) whose resolution
    trigger 1 already owns -- an opaque line elsewhere in `tail_lines`
    (never at the boundary) changes that answer for nothing further back
    than what is already fully in memory, so scanning past `tail_lines[0]`
    here regressed exactly the unbounded read PRD P's own fixture exists
    to catch: 100,000 unrelated interleaved `ignored` events walked the
    WHOLE transcript (49,360 -> 6,826,540 `output.jsonl` bytes read)
    chasing a `textSeen` no line anywhere in the file ever sets, merely
    because SOME `ignored` line shared `tail_lines` with the 200
    `message_end` lines that do need it -- never because the boundary
    itself was unrecognized.

    Gated on `tail_lines[0]` alone, mirroring trigger 1's own grounding:
    an opaque line found only in `prefix_lines` (already read while
    chasing trigger 1 or 2), or anywhere in `tail_lines` OTHER than its
    own oldest member, changes nothing here, since `tail_lines` itself
    never changes across the look-back loop.
    """
    if not tail_lines:
        return False
    ev = _safe_json(tail_lines[0])
    if ev is None:
        return False
    etype = ev.get("type")
    if etype not in _KNOWN_EVENT_TYPES:
        return True
    # Task 007, PRD Q: a recognized `message_end` at the boundary that
    # itself carries no text/thinking/toolCall item is exactly as opaque
    # to this decision as a wholly unrecognized type -- see this
    # function's own docstring and `_message_end_is_uninformative`.
    return etype == "message_end" and _message_end_is_uninformative(ev)


def _has_relevant_type(lines: list[str], flags: set) -> bool:
    """Whether ANY line in `lines` -- anywhere in it, not merely as the
    single OLDEST one -- carries a raw TYPE whose `_TYPE_FLAG_GROUPS`
    entry intersects `flags` (cure-01-01-2's original growing-scan shape,
    narrowed by cure-01-02-3 to a caller-supplied `flags` set rather than
    the fixed `_TYPE_FLAG_GROUPS`-membership-regardless-of-flag test it
    used to run unconditionally). Checking only the oldest already-read line (this
    module's pre-cure-01-01-2 shape) is unsound: an opaque/`"ignored"`
    filler event sitting at the very EDGE of the current read proves
    nothing either way about what is further back, so treating it as a
    reason to stop growing wrongly stopped short whenever a genuinely
    relevant run (streamed text, say) was separated from the content that
    still needs it by enough ignored filler to reach past a single read.

    `_should_extend_seed_lookback` calls this with `flags` set to
    `_tail_active_flag_groups(tail_lines)` -- i.e. restricted to the
    mechanism(s) `tail_lines` ITSELF already exercises -- never with the
    raw `unresolved` set on its own; see that function's own docstring
    for why an unrestricted check regressed E.2's byte bound."""
    for raw in lines:
        ev = _safe_json(raw)
        if ev is not None and _TYPE_FLAG_GROUPS.get(ev.get("type"), set()) & flags:
            return True
    return False


def _flags_needed_by_tail(tail_lines: list[str]) -> set:
    """Which of `"textSeen"`/`"thinkingSeen"`/`"toolcallSeen"` the RETAINED
    `tail_lines` actually READ when rendered -- i.e. which seed flags could
    change this tail's bytes at all (cure-01-01-2, second attempt).
    Mirrors `log_render`'s own two readers exactly: `_render_message_end`
    consults `text_seen` for a `text` content item, `thinking_seen` for a
    `thinking` item and `toolcall_seen` for a `toolCall` item, and
    `_render_message_update` consults `thinking_seen` for
    `thinking_start`/`thinking_delta`. Nothing else in that module gates
    output on one of the three.

    Used to keep the look-back's first trigger honest now that no
    doubling cap terminates it: growth continues only while a flag THIS
    tail would actually read is still unresolved, never for a flag whose
    value this tail could not observe -- so an iteration that merely
    happens to lack (say) a thinking block never walks its whole
    transcript looking for one."""
    needed: set = set()
    for raw in tail_lines:
        ev = _safe_json(raw)
        if not ev:
            continue
        etype = ev.get("type")
        if etype == "message_update":
            sub = (ev.get("assistantMessageEvent") or {}).get("type")
            if sub in ("thinking_start", "thinking_delta"):
                needed.add("thinkingSeen")
        elif etype == "message_end":
            for item in (ev.get("message") or {}).get("content") or []:
                kind = item.get("type") if isinstance(item, dict) else None
                if kind == "text":
                    needed.add("textSeen")
                elif kind == "thinking":
                    needed.add("thinkingSeen")
                elif kind == "toolCall":
                    needed.add("toolcallSeen")
    return needed


def _unresolved_seed_flags(tail_lines: list[str], seed_flags: dict,
                           reached_start: bool) -> set:
    """The subset of `{"textSeen", "thinkingSeen", "toolcallSeen"}` this
    cut's collapse seed carries WITHOUT having read the evidence that would
    settle it -- empty when there is no such flag (task cure-01-03-2, PRD
    Q.1).

    THE PROPERTY, not a case list (PRD Q.5). A seed flag's value is
    KNOWLEDGE in exactly two situations, and both are about which bytes were
    read, never about which event types happened to be seen:

      * something already read SETS it (`_prior_collapse_state` returned
        True). Every flag is set by its FIRST occurrence and none is ever
        unset, so `True` is monotone: no unread history can take it back.
      * the backward read walked to byte 0 of the transcript
        (`reached_start`), so by `_tail_lines_of_file`'s own contract there
        is provably NO unread history left and `False` is a measurement of
        the whole prefix.

    A flag in neither situation, but which the retained tail actually READS
    (`_flags_needed_by_tail`), is a DEFAULT and not a measurement: the search
    for its setter stopped on a BOUND -- `_SEED_LOOKBACK_SPECULATIVE_BYTE_
    FLOOR`, or the retained tail's own byte size -- rather than on evidence,
    and `False` is then merely what a bounded reader has to render from.
    Naming those flags on the seed, next to the boolean, is what keeps "we
    did not find a setter" distinct from "there is no setter": the ONE
    thing PRD Q.1 forbids is letting the second stand in for the first.
    Neither of the two things a reader is tempted to read the difference off
    is the discriminator here, which is why neither appears in this function:
    not RECOGNITION (a parsed `message_end` with empty content is exactly as
    uninformative about the state before it as an unknown `{"type":
    "ignored"}` event -- both are absent from `_TYPE_FLAG_GROUPS` because
    neither can set a flag), and not EXHAUSTION of the look-back (running out
    of budget is a fact about this reader, not about the transcript).

    A flag the retained tail never reads is NOT listed even when nothing
    resolved it: its value cannot change one rendered byte of this tail, so
    there is no uncertainty for a consumer to act on -- the same materiality
    line `_flags_needed_by_tail` already draws for the look-back itself.

    WHAT A CONSUMER MAY DO WITH IT, and the one thing this disclosure does
    NOT buy (task cure-01-03-2's petition against PRD Q.1's arbitrary-depth
    half). `log_render` still renders an unresolved flag from its `False`
    fallback -- a renderer has to emit something -- so a `message_end` that
    repeats text streamed FURTHER back than the look-back's budget still
    prints that text once more. Neither direction of that fallback is
    reachable from state, and the arithmetic says why rather than the wording:

      * suppressing whenever a material flag is unresolved makes every named
        Q scenario and review's own silent-prelude variant correct, and was
        measured to cost 7,345,430 / 7,607,609 / 30,248,729 transcript bytes
        on PRD P's own original, shifted and padded fixtures -- against
        P.1/P.5's unmoved 1,000,000-byte ceiling, on files of 3.4 MB / 12 MB
        whose omitted prefix cannot be certified without reading all of it;
      * reading far enough to RESOLVE the flag instead is not a fallback
        choice but a bigger budget, and the depth is not payable either: in
        review's own variant the setter's first byte is 1,067,000 bytes from
        byte 0 and 10,720,791 from the file's end (measured; the shipped
        guard `_assert_silent_prelude_publishes_unknown_state_not_empty_state`
        asserts that offset off the fixture), so EVERY byte range containing
        it is larger than P's entire per-call ceiling. Only an index would
        seek to it, and this PRD puts a side index and a transcript-format
        change both out of scope.

    Which fallback is right is therefore not a question this reader can decide
    from state; it is a conflict between two clauses of the same contract,
    filed as a petition (`artifacts/reports/v0.22-petitions.md`, "PRD requirement
    Q's fallback direction ...") with those measurements rather than settled
    here by guessing or by this module calling either direction a residual --
    only review's own adjudication may do that, and none has landed yet
    (task cure-01-01-3) -- and NOT something exhaustion may quietly decide by
    publishing a `False` that looks measured -- which is the part this
    function does fix."""
    if reached_start:
        return set()
    return {flag for flag in _flags_needed_by_tail(tail_lines)
            if not seed_flags.get(flag)}


def _seed_lookback_extension_kind(tail_lines: list[str], prefix_lines: list[str]) -> str | None:
    """Which of the two independent, concrete signals below wants one more
    (bounded) chunk read further back purely to widen the collapse-state
    seed, given what has been read so far -- `"needle"` for trigger 1 or
    2 (each names a CONCRETE needle whose existence the retained tail
    itself already evidences, so growth is safe to keep doubling
    aggressively: it terminates on the needle, never on a cap alone),
    `"speculative"` for trigger 3 alone (names no needle -- see that
    trigger's own comment below for why `_tail_lines_of_file` must NOT
    let it read with the same doubled step a needle-grounded search
    uses), or `None` if neither wants more.

    `_should_extend_seed_lookback` is the plain boolean callers that only
    need the yes/no (the two `test_log_render_bounded_tail.py` stopping-
    condition assertions, `_should_extend_seed_lookback(kept, prefix)`)
    still use; `_tail_lines_of_file` calls THIS function instead so it
    can choose a materially different read-step size for the
    `"speculative"` case (task cure-01-02-2, PRD P.1/P.5's own
    materiality rule) without changing when growth stops.

    Returns a non-`None` value on either of two independent, concrete
    signals -- never merely because more history happens to exist, and
    never merely because a lookback cap or a single opaque edge event
    lets it stop (cure-01-01-2: `_SEED_LOOKBACK_EXTRA_GROWTHS` caps only
    how large one read chunk may grow and no longer ends the look-back
    at all, so THIS function and `reached_start` are its only two
    exits):

    1. `_prior_collapse_state` over `prefix_lines + tail_lines` -- i.e.
       EVERY line read so far, in chronological order, not `prefix_lines`
       alone -- has not yet resolved a flag the RETAINED tail's own lines
       actually read (`_flags_needed_by_tail`; a flag no line in this tail
       consults cannot change one rendered byte, so an unresolved one of
       those is no reason to read further back), AND that unresolved flag
       is one `_tail_active_flag_groups(tail_lines)` names -- i.e.
       `tail_lines` ITSELF (never merely something read further back)
       already carries a raw event of the TYPE that flag belongs to
       (cure-01-02-3, #195; see that function's own docstring for the
       narrowing this replaces and why the broader one regressed). This
       still covers cure-01-01-2's own counterexample (`prefix_lines`
       alone stayed pure `"ignored"` filler for several growths in a row
       while the fixed `tail_lines` already held a thinking delta the
       whole time): `tail_lines` holding that thinking delta is exactly
       the ground truth `_tail_active_flag_groups` reads, with no need for
       `prefix_lines` to also show anything before growth is justified.
       `tail_lines` never changes across this loop, so this check is not a
       second disk read, only a wider scan of bytes already in memory.
    2. `_dangling_tool_call_ids` is non-empty -- the retained tail keeps a
       `tool_execution_end` this seed cannot yet supply arguments for.

    Neither signal fires for E.2's own fixture (`{"type": "ignored"}`
    filler around plain, independent `message_end` lines): `tail_lines`
    there is exactly the requested `message_end` lines with no ignored
    filler mixed in (the ignored history sits strictly OLDER, outside the
    requested tail), and `tail_lines` never itself contains a
    `_TYPE_FLAG_GROUPS` member for that fixture (`message_end` is not
    itself one -- only the streaming/tool-call events under it are) -- so
    trigger 1 never fires there regardless of how far growth would
    otherwise be allowed to run, and there is no `tool_execution_end` in
    its tail at all for trigger 2 -- the byte bound E.2 pins stays exactly
    as it was, not because of a cap and not because the oldest event
    happens to be `"ignored"`, but because there is provably nothing
    relevant to find anywhere in what has been (or could cheaply be) read.

    Trigger 1's evidence check is grounded in `tail_lines` alone
    (cure-01-02-3, #195, its SECOND attempt at this decision): its first
    attempt asked only whether SOME relevant type -- of any kind, found
    ANYWHERE already read, including deep in `prefix_lines` -- was
    positive evidence for the exact unresolved flag it happened to name by
    subtype. That is unsound in the OTHER direction from the broadest
    pre-cure-01-02-3 shape: a `thinking_start` sitting arbitrarily far back
    in `prefix_lines`, entirely unconnected to a tail that needs only
    `textSeen` (all `message_end` with `text` content, no thinking
    anywhere in `tail_lines` at all), is not evidence that tail's own
    `textSeen` will EVER resolve -- and in `test_e_fixed_200_lines_with_
    older_thinking_marker_has_constant_byte_bound`'s fixture it never does
    (no `text_delta` exists in the file at all), so growth walked the
    ENTIRE transcript chasing it: `output.jsonl` bytes read grew from
    18,470 (10 unrelated `ignored` entries) to 3,407,060 -- the WHOLE file
    -- at 100,000, regardless of how much unrelated filler sat between the
    marker and the tail. Grounding the check in `tail_lines` instead --
    what THIS retained tail is actually built from, never what merely sits
    somewhere further back -- rules that out while still finding the
    genuine `test_f_text_before_thinking_survives_a_mid_iteration_cut`
    case: there, `tail_lines` itself holds a `thinking_delta`, so BOTH
    `_TYPE_FLAG_GROUPS["message_update"]` flags (`textSeen` and
    `thinkingSeen`) stay fair game, and the `text_delta` that resolves the
    still-unresolved `textSeen` is duly found further back.
    """
    combined = prefix_lines + tail_lines
    seed_flags, _ = _prior_collapse_state(combined)
    unresolved = {flag for flag in _flags_needed_by_tail(tail_lines)
                  if not seed_flags[flag]}
    active = _tail_active_flag_groups(tail_lines)
    if unresolved & active and _has_relevant_type(combined, unresolved & active):
        return "needle"
    if _dangling_tool_call_ids(tail_lines, prefix_lines):
        return "needle"
    # Trigger 3 (task cure-01-03-2, F.1/F.2/F.4, E.3): `tail_lines` itself
    # carries an OPAQUE event this module cannot read at all (never merely
    # a recognized-but-irrelevant one, which is what E.2's own fixture
    # keeps entirely OUT of `tail_lines`) -- so an unresolved flag `tail_
    # lines` still needs cannot be certified `False` from what has been
    # read so far; see `_tail_contains_opaque_filler`'s own docstring for
    # exactly why this is safe against E.2's byte bound (its `tail_lines`
    # never contains this signal at all, homogeneous `message_end` only).
    #
    # Bounded, ALONE of the three, by a byte budget --
    # `_SEED_LOOKBACK_SPECULATIVE_BYTE_FLOOR` or the retained tail's own
    # byte size, whichever is larger (task cure-01-02, PRD P.1/P.5,
    # recounted in bytes by cure-01-02-2): this trigger names no needle
    # to terminate on, so without a
    # bound it reads to byte 0 of any transcript whose tail boundary
    # happens to be opaque -- review's shifted-interleaving measurement,
    # see that constant's own comment. THIS ALONE is also the trigger
    # `_tail_lines_of_file` serves by reading the file's HEAD instead of
    # extending the backward walk (task cure-01-02-2, PRD P.1/P.5, the
    # materiality rule review's padded-older-history variant demands):
    # triggers 1/2 each name a needle whose existence the retained tail
    # itself evidences, so walking BACKWARDS terminates on that needle,
    # while THIS trigger's only question -- did this iteration EVER stream
    # text/thinking, or open a tool call, before the retained tail? -- is
    # answered by the iteration's own FIRST events, at the head of the
    # file, whatever sits in between. Reading the head rather than the
    # middle is what makes this bound affordable in BYTES: the line-count
    # budget it replaces cost 393,216 bytes unpadded, 851,968 at 100
    # bytes of padding per older `ignored` event and 2,752,512 at 500 --
    # the last well over the unmoved 1,000,000-byte ceiling -- for one and
    # the same walk, and the byte cost of finding out this trigger has
    # nothing to resolve must not be movable by padding at all. See
    # `_tail_lines_of_file`'s own read loop.
    if (unresolved and _tail_contains_opaque_filler(tail_lines)
            and (sum(len(line) for line in prefix_lines)
                 < max(_SEED_LOOKBACK_SPECULATIVE_BYTE_FLOOR,
                       sum(len(line) for line in tail_lines)))):
        return "speculative"
    return None


def _should_extend_seed_lookback(tail_lines: list[str], prefix_lines: list[str]) -> bool:
    """Plain yes/no wrapper around `_seed_lookback_extension_kind` for the
    callers that only need to know WHETHER `_tail_lines_of_file` should
    read one more (bounded) chunk further back -- never which of the two
    reasons it is (only `_tail_lines_of_file`'s own read loop needs
    that, to size a purely speculative step differently; see that
    function's `_seed_lookback_extension_kind` call)."""
    return _seed_lookback_extension_kind(tail_lines, prefix_lines) is not None


def _head_seed_lines(head_lines: list[str]) -> list[str]:
    """The head-probe lines (task cure-01-02-2) that are SAFE to seed a
    collapse state with across a non-contiguous read -- every line except
    a `tool_execution_start` the head window itself never closes.

    The three collapse-guard flags are MONOTONE: a `text_delta`,
    `thinking_start`/`thinking_delta`, `tool_execution_start` or
    `tool_execution_end` anywhere earlier in the iteration sets its flag
    for good, so reading such a line at the file's head establishes
    exactly what a full render would have established there, whatever
    sits in the unread middle -- an unread middle can only COST this seed
    a flag, never invent one.

    `_prior_collapse_state`'s OTHER half is not monotone: `tool_args`
    holds calls still OPEN at the cut point, and a call the head window
    opens may well have been closed by a `tool_execution_end` in the very
    middle this probe skipped. Seeding it would then claim a call is
    still in flight that is not, and `render_pending_tool_invocations`
    prints a phantom invocation line for exactly that claim (measured on
    `test_collapse_state_carries_across_a_cut_mid_iteration_direct_dead_
    run_and_live`'s wide-tool-gap scenario: `  -> bash $ echo hello`
    printed on its own, where the full render shows the finished
    `  -> bash $ echo hello / ok (ok)` line and nothing else). So a start
    left open at the head window's edge contributes nothing at all --
    conservative in the one direction that only ever costs a suppression
    this reader cannot prove, never a line it invents. A call the head
    window opens AND closes keeps both of its lines: together they set
    `toolcallSeen` and leave `tool_args` empty, which is precisely the
    state a full render would carry (the shipped post-thinking-gap
    scenarios' own tool call, opened and closed in the iteration's first
    ~200 bytes).

    A backward-read `prefix` is contiguous with the retained tail, so it
    is never filtered here -- only what the head probe adds is.
    """
    open_ids: set = set()
    for raw in head_lines:
        ev = _safe_json(raw)
        if not ev:
            continue
        tcid = ev.get("toolCallId")
        if tcid is None:
            continue
        if ev.get("type") == "tool_execution_start":
            open_ids.add(tcid)
        elif ev.get("type") == "tool_execution_end":
            open_ids.discard(tcid)
    if not open_ids:
        return head_lines
    kept = []
    for raw in head_lines:
        ev = _safe_json(raw)
        if (ev and ev.get("type") == "tool_execution_start"
                and ev.get("toolCallId") in open_ids):
            continue
        kept.append(raw)
    return kept


def _whole_lines(raw: bytes) -> list[str]:
    """The WHOLE newline-terminated lines `raw` holds, dropping a trailing
    partial one (task cure-01-02-2): the head probe in
    `_tail_lines_of_file` reads forward from byte 0 in fixed-size chunks,
    so its last line is cut mid-event unless the chunk boundary happened
    to land exactly after a `\\n`. Every kept line ends after a `\\n`
    byte, and `\\n` is always a line boundary in UTF-8, so no multi-byte
    character is ever split by where a chunk ends."""
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines.pop()
    return lines


def _tail_lines_of_file(path: Path, needed: int, *,
                        skip_seed_lookback: bool = False) -> tuple[list[str], bool, list[str]]:
    """The last `needed` newline-terminated lines of `path`, read from the
    END of the file in growing chunks -- never the whole file -- so bytes
    read are bounded by `needed`, not by how much unrelated OLDER content
    happens to sit earlier in the same file (task 005, PRD E: two on-disk
    runs with an identical rendered 200-line suffix cost 22,722 vs
    5,281,670 `output.jsonl` bytes read, because the newest iteration's
    WHOLE transcript was `read_text()`-ed regardless of how much of it the
    requested tail actually needed).

    `skip_seed_lookback` (task 021, PRD G.3/G.6): when the caller already
    has a validated `collapse_index` for this same file, it can resolve
    the collapse-state seed by an O(1) lookup (`collapse_index.
    resolve_flags_at_offset`) instead of by this function's own bounded
    LOOK-BACK GROWTH below -- so that growth (bytes read proportional to
    how far back the deciding setter sits) becomes pure waste: it would
    read real bytes only to have their answer discarded in favour of the
    index's. Passing `True` here skips straight past the loop below,
    leaving `reached_start`/`prefix_lines` exactly as the single, needed-
    bounded read above already computed them -- `merge_entries_tail` is
    the only caller that ever passes it, and only once `collapse_index.
    load_index` has already validated an index for this same file.

    Reads in binary mode and only decodes once a run of whole lines has
    been isolated: every chunk boundary this function ever stops at lands
    immediately after a `\\n` byte (or at byte 0), and `\\n` is itself
    always a line boundary in UTF-8, so a multi-byte character can never be
    split by where a chunk starts.

    Returns `(kept, reached_start, prefix)` (task 006, PRD F):
      `kept`          the last `needed` lines -- what this function has
                      always returned.
      `reached_start` whether the backward read actually walked all the
                      way to byte 0 of the file. When true, `kept` (plus
                      `prefix`) is EVERY line the file has -- there is no
                      unread history this read could be missing, so a
                      caller may safely treat the point just before `kept`
                      as a genuine restart (matches this function's
                      pre-task-006 contract exactly).
      `prefix`        whatever OLDER lines the same growing-chunk read
                      happened to pull in beyond `needed` (e.g. because
                      the read floor is `_TAIL_READ_CHUNK` bytes, not
                      `needed` lines), plus -- when the speculative
                      look-back trigger fired and the file's HEAD was
                      probed for the collapse state's own first
                      occurrences (task cure-01-02-2) -- those head lines
                      ahead of them, in chronological order. Read
                      already, at no further disk cost, so a caller can
                      inspect them for the raw event types that flip a
                      render collapse guard (task 006) without any
                      further I/O. Empty exactly when the read needed no
                      more than `needed` lines' worth of bytes and no
                      head probe happened. NOT necessarily contiguous
                      with `kept`: when a head probe fired without
                      meeting the backward read, the middle of the file
                      stays unread -- which is precisely why
                      `reached_start` stays False in that case, so no
                      caller may treat the cut point as a genuine restart
                      (E.3). An unread middle can only cost this prefix a
                      flag it would otherwise have carried, never invent
                      one: every line in it is a genuinely older line of
                      the same iteration.
    """
    if needed <= 0:
        return [], True, []
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

        def _split(raw: bytes) -> tuple[list[str], list[str], list[str]]:
            ls = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
            if len(ls) > needed:
                return ls, ls[-needed:], ls[:-needed]
            return ls, ls, []

        reached_start = pos <= 0
        _, tail_lines, prefix_lines = _split(data)

        # Task cure-01-02 (#195): bounded extra look-back purely to widen
        # `prefix_lines` for the collapse-state seed -- `tail_lines` never
        # changes from here on (it stays exactly what `needed` selected
        # above; every extra chunk only PREPENDS strictly older bytes, so
        # the last `needed` lines of the growing decoded line list are
        # the same lines every time `_split` recomputes them).
        #
        # The loop's exit conditions are exactly two, both of them REAL
        # (cure-01-01-2, second attempt): `reached_start` -- byte 0, no
        # unread history left at all -- or `_seed_lookback_extension_kind`
        # finding nothing further back that could still resolve what the
        # retained tail actually reads. `_SEED_LOOKBACK_EXTRA_GROWTHS` is
        # NOT a third exit: it only caps how large a single NEEDLE-
        # grounded chunk may grow, after which the read continues in
        # steady-sized chunks (see that constant's own comment for the
        # `bash ✓ ok` counterexample its old stop-condition shape
        # produced).
        #
        # A PURELY SPECULATIVE step (task cure-01-02-2, PRD P.1/P.5's own
        # materiality rule) does not extend the BACKWARD read at all: it
        # reads the file's HEAD instead, forward from byte 0, one fixed
        # `_TAIL_READ_CHUNK` at a time, and never touches `chunk` (so a
        # later needle-grounded step in the SAME call still doubles from
        # wherever it last legitimately grew). The reason is where the
        # answer lives, not merely how big a step is: every flag
        # `_prior_collapse_state` carries is set by its FIRST occurrence
        # in the iteration, so "did this iteration stream text/thinking or
        # open a tool call before the retained tail?" is answered at the
        # iteration's beginning -- while a BACKWARD speculative walk pays
        # for every byte of unrelated middle history on the way there, an
        # amount padding alone can multiply at will (review's
        # `test_P_older_padding_cannot_buy_speculative_reads`: 393,216
        # bytes unpadded, 2,752,512 at 500 bytes of padding per older
        # event, one and the same walk). Head bytes are counted against
        # `_SEED_LOOKBACK_SPECULATIVE_BYTE_FLOOR`-or-retained-tail budget
        # by the very same
        # `prefix_lines` measurement trigger 3 already stops on, so the
        # loop's stop conditions stay exactly the two REAL ones
        # (`reached_start`, or `_seed_lookback_extension_kind` finding
        # nothing left to resolve) -- the head probe adds no third exit.
        #
        # `head_lines` are genuinely OLDER lines of this same iteration,
        # so every flag they set is one a full render would also have set
        # before `tail_lines` (never the reverse: an unread middle can
        # only cost this seed a flag, never invent one). They are NOT
        # contiguous with `prefix_lines` when the middle stays unread --
        # which is exactly why `reached_start` stays False in that case,
        # so no caller may read the cut point as a genuine iteration
        # start (E.3). When the head probe MEETS the backward read, the
        # two halves join into one contiguous buffer and `reached_start`
        # becomes true on its own byte-0 terms.
        head_buf = b""
        head_lines: list[str] = []
        head_pos = 0
        extra_growths = 0
        while not reached_start and not skip_seed_lookback:
            kind = _seed_lookback_extension_kind(tail_lines,
                                                 head_lines + prefix_lines)
            if kind is None:
                break
            if kind == "speculative":
                read_size = max(1, min(_TAIL_READ_CHUNK, pos - head_pos))
                f.seek(head_pos)
                head_buf += f.read(read_size)
                head_pos += read_size
                if head_pos >= pos:
                    # The head probe read every byte the backward read had
                    # not: one contiguous buffer, and a real byte-0 stop.
                    data = head_buf + data
                    pos = 0
                    head_buf = b""
                    head_lines = []
                    reached_start = True
                else:
                    head_lines = _head_seed_lines(_whole_lines(head_buf))
                    continue
            else:
                read_size = max(1, min(chunk, pos))
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size) + data
                if extra_growths < _SEED_LOOKBACK_EXTRA_GROWTHS:
                    chunk *= 2
                    extra_growths += 1
                reached_start = pos <= 0
            _, tail_lines, prefix_lines = _split(data)

    return tail_lines, reached_start, head_lines + prefix_lines


# The three raw NDJSON shapes (task 006, PRD F) whose FIRST occurrence in an
# iteration flips one of `log_render`'s collapse guards (`text_seen`,
# `thinking_seen`, `toolcall_seen`) -- used only to scan the `prefix` lines
# `_tail_lines_of_file` already read (never a fresh disk read) for whichever
# of the three flags a bounded tail's cut point needs to carry forward, so a
# `message_end`/`thinking_delta` line INSIDE the kept tail renders exactly as
# it would have from a genuine, uncut iteration start (`_prior_collapse_state`
# below). Mirrors `log_render.render_log_line`'s own dispatch: a
# `message_update` event's `text_delta` sets `text_seen`, `thinking_start`/
# `thinking_delta` sets `thinking_seen`; a `tool_execution_start`/
# `tool_execution_end` sets `toolcall_seen`.
def _prior_collapse_state(prefix_lines: list[str]) -> tuple[dict, dict]:
    """`({"textSeen": bool, "thinkingSeen": bool, "toolcallSeen": bool},
    tool_args)` for whatever collapse-guard state the (already in-memory)
    `prefix_lines` establish -- i.e. what a fresh `new_render_state()`
    would hold after rendering them, without actually rendering (no
    output, just the state transitions `log_render._render_message_update`
    and `render_log_line`'s tool-call branches apply). A line that fails
    to parse, or is unrecognized, changes nothing -- exactly like
    `render_log_line`'s own unknown-event handling.

    `tool_args` (task cure-01-02, #195) is `{toolCallId: args}` for every
    tool call `prefix_lines` started but did not also close (pop on a
    matching `tool_execution_end`, mirroring `render_log_line`'s own
    `state['tool_args']` bookkeeping exactly) -- i.e. calls still open as
    of the retained tail's cut point, whose `tool_execution_end` the
    RETAINED tail may still need to render with its invocation's
    arguments (PRD F's tool-argument gap). Never early-exits once all
    three flags are known True (unlike this function's pre-cure-01-02
    shape): a `tool_execution_end` closing an ALREADY-open call still has
    to be seen to pop that call back out of `tool_args`."""
    text_seen = thinking_seen = toolcall_seen = False
    tool_args: dict = {}
    for raw in prefix_lines:
        ev = _safe_json(raw)
        if not ev:
            continue
        etype = ev.get("type")
        if etype == "message_update":
            sub = (ev.get("assistantMessageEvent") or {}).get("type")
            if sub == "text_delta":
                text_seen = True
            elif sub in ("thinking_start", "thinking_delta"):
                thinking_seen = True
        elif etype == "tool_execution_start":
            toolcall_seen = True
            tcid = ev.get("toolCallId")
            if tcid is not None:
                tool_args[tcid] = ev.get("args") or ev.get("arguments") or {}
        elif etype == "tool_execution_end":
            toolcall_seen = True
            tcid = ev.get("toolCallId")
            if tcid is not None:
                tool_args.pop(tcid, None)
    return ({"textSeen": text_seen, "thinkingSeen": thinking_seen,
            "toolcallSeen": toolcall_seen}, tool_args)


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
        collapse_seed = None
        if out.exists():
            remaining = max(tail - content_count, 1)
            # Task 021 (PRD G.3/G.6): a validated side index (task 020,
            # PRD G.1) resolves the collapse-state seed by an O(1) lookup
            # -- one `bisect` over a handful of segments, no re-read of
            # `out` at all -- so the bounded look-back growth
            # `_tail_lines_of_file` would otherwise pay for, proportional
            # to how far back the deciding setter sits, is skipped
            # entirely (`skip_seed_lookback=True` below). A truncated or
            # mismatched index (task cure-01-09, PRD G.4/DoD 7, below) is
            # likewise skipped past the growth-based look-back, in favour
            # of `resolve_flags_at_offset`'s own scan fallback; only a
            # GENUINELY ABSENT index file still runs the growth-based
            # look-back exactly as it always has, unaffected -- imported
            # locally, not at module scope, so this module never has to
            # order itself around `collapse_index`'s own module-level
            # `from .log_merge import OUTPUT_FILENAME` -- the two modules
            # would otherwise import each other.
            from .collapse_index import INDEX_FILENAME as _INDEX_FILENAME
            from .collapse_index import load_index as _load_collapse_index
            from .collapse_index import resolve_flags_at_offset as _resolve_flags_at_offset
            index = _load_collapse_index(d, output_filename=OUTPUT_FILENAME)
            # Task cure-01-09 (PRD G.4, DoD 7): a validated index resolves
            # the seed by O(1) lookup exactly as before. An index that
            # fails to validate (`load_index` returning `None`) splits into
            # two genuinely different cases `load_index` cannot itself tell
            # apart, but this call site can, from one extra `stat`: the
            # index FILE EXISTS (truncated, malformed, a stale/mismatched
            # `transcriptBytes`, or a version this build does not read) --
            # proof an index-write attempt genuinely happened for this
            # iteration -- versus no file at all, which is indistinguishable
            # from an iteration recorded by an engine build that never had
            # this side index (`TestNoIndexRendersLikeToday`'s own
            # contract, `tests/test_collapse_index.py`) and from every
            # shipped materiality fixture in this module that never writes
            # one by construction (`_write_iteration_with_older_thinking_
            # marker` and friends -- PRD P.1/P.5's own petitioned, still-
            # open cost tension: forcing THOSE fixtures to resolve a flag
            # they can safely leave disclosed-but-unresolved would cost
            # exactly what that petition already measured as unpayable).
            # An index file that exists but fails to validate carries no
            # such ambiguity and no such petitioned cost history, so it
            # goes through `resolve_flags_at_offset`'s OWN scan fallback --
            # G.4's own promised contract for "the index does not
            # validate": never a wrong render, only ever slower -- instead
            # of the older growth-based look-back's bounded, sometimes-
            # wrong guess.
            index_file_present = (d / _INDEX_FILENAME).exists()
            tail_lines, reached_start, prefix_lines = _tail_lines_of_file(
                out, remaining,
                skip_seed_lookback=index is not None or index_file_present)
            if index is not None or index_file_present:
                # The cut point is wherever `tail_lines` -- exactly the
                # tail this call retains, whether or not the growth
                # loop above ran -- begins in the file: the file's
                # CURRENT size minus the bytes those lines re-encode to.
                # `tail_lines` is always a literal suffix of `out`'s own
                # bytes (task 005's own contract), so this needs no
                # further disk read -- one `stat()` plus arithmetic on
                # lines already in hand.
                try:
                    file_size = out.stat().st_size
                except OSError:
                    file_size = 0
                tail_bytes = len("".join(tail_lines).encode("utf-8"))
                cut_offset = max(file_size - tail_bytes, 0)
                if cut_offset > 0:
                    # An index FILE that exists but fails to validate
                    # (typically a finished iteration's own index left
                    # stale by a later truncation of its transcript) is
                    # answered off a validated render index first, when
                    # one sits beside it: that costs the render index's
                    # own read plus any unindexed gap before the cut,
                    # never `resolve_flags_at_offset`'s scan of every
                    # byte before the cut (v0.27 PRD D.1's whole-call
                    # bound). Only with no usable render index either
                    # does the scan below still run.
                    flags = None
                    if index is None:
                        from .render_index import resolve_flags_at_offset as _render_index_flags
                        flags = _render_index_flags(d, cut_offset,
                                                    output_filename=OUTPUT_FILENAME)
                    if flags is None:
                        flags = _resolve_flags_at_offset(d, cut_offset,
                                                         output_filename=OUTPUT_FILENAME)
                    collapse_seed = {"textSeen": flags["textSeen"],
                                     "thinkingSeen": flags["thinkingSeen"],
                                     "toolcallSeen": flags["toolcallSeen"]}
            # Task 006 (PRD F): whether or not this read reached byte 0 of
            # the file (`reached_start`), the fabricated "start" boundary
            # below is NOT a real iteration start whenever `prefix_lines`
            # is non-empty -- `prefix_lines` is raw content that came
            # BEFORE the retained `tail_lines`, so whatever `text_seen`/
            # `thinking_seen`/`toolcall_seen` state a real start would
            # already carry must ride along on that same boundary line
            # instead of being silently dropped (the exact defect PRD F
            # names: "the raw-tail fetch drops the preceding text_seen
            # state before render_tail_lines selects its restart"). This
            # costs no extra disk read either way -- `prefix_lines` is
            # whatever `_tail_lines_of_file`'s OWN growing chunk already
            # pulled in beyond `remaining` lines (often the whole file, if
            # it is smaller than one growth chunk, in which case this seed
            # is EXACT rather than merely best-effort). Task cure-01-02
            # (#195): `_tail_lines_of_file` may have grown `prefix_lines`
            # a few chunks further back than `remaining` alone needed, so
            # this seed also carries `toolArgs` -- the arguments of any
            # tool call `prefix_lines` opened but did not also close --
            # for a `tool_execution_end` kept in `tail_lines` whose own
            # `tool_execution_start` fell outside even that widened read.
            if collapse_seed is None and prefix_lines:
                seed_flags, pending_tool_args = _prior_collapse_state(prefix_lines)
                collapse_seed = dict(seed_flags)
                # Task cure-01-03-2 (PRD Q.1): a flag whose setter was never
                # read is published AS UNRESOLVED, beside its fallback
                # boolean, instead of silently as `False` -- "we stopped
                # looking" is not "there is nothing there". See
                # `_unresolved_seed_flags` for the property, for why neither
                # recognition nor exhaustion discriminates, and for the one
                # thing the disclosure deliberately does not decide.
                unresolved = _unresolved_seed_flags(tail_lines, seed_flags,
                                                    reached_start)
                if unresolved:
                    collapse_seed["unresolved"] = sorted(unresolved)
                if pending_tool_args:
                    collapse_seed["toolArgs"] = pending_tool_args
            for line in reversed(tail_lines):
                line = line if line.endswith("\n") else line + "\n"
                chunk.append((False, scrub(line)))
            content_count += len(tail_lines)
        chunk.append((True, scrub(boundary_line(meta, "start", collapse_seed))))
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
    boundary lines that fall within that window.

    Task 006 (PRD F): a boundary line immediately preceding the exact
    content line that satisfies `tail` is kept too, not dropped -- a
    `merge_entries_tail` "start" boundary for a cut iteration carries the
    `collapseSeed` (task 006, PRD F) that lets the renderer pick up
    `text_seen`/`thinking_seen`/`toolcall_seen` state the omitted earlier
    raw lines already set; that boundary is invariably the VERY NEXT entry
    once the content quota is met (it is `merge_entries_tail`'s per-
    iteration read that sizes the kept raw lines to land exactly on the
    quota), so silently stopping the instant the quota is met -- this
    function's behavior before this task -- always discarded it. Only
    CONTIGUOUS boundary lines right at that edge are kept this way; the
    next actual content line past the quota still ends the scan exactly
    as before.
    """
    if not tail:
        return entries
    selected: list[Entry] = []
    content_count = 0
    met = False
    for is_boundary, line in reversed(entries):
        if met and not is_boundary:
            break
        selected.append((is_boundary, line))
        if not is_boundary:
            content_count += 1
            if content_count >= tail:
                met = True
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
