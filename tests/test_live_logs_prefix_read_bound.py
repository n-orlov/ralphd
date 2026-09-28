"""Task 005 (v0.27 PRD D.1, cure round, #275): the whole-call read bound
`tests/test_tail_whole_call_read_bound.py` pins for a FINISHED (or, via
`tests/test_d1_real_pirunner_active_iteration_reads_within_cap.py`, a
still-active) iteration's own PUBLISHED index must hold for a still-
active iteration cut at an arbitrary PREFIX -- not only at the exact
boundaries a real subprocess happens to publish at -- and it must hold
identically whether the index a reader finds on disk is the LIVE
writer's own (possibly lagging) published state, or a from-scratch
rebuild over that same prefix.

Every prefix below is built by truncating one of the three named PRD Q
fixtures (`_FIXTURES`, its own canonical no-prelude shape and both
silent-prelude depths) to strictly fewer than its own full line count:
one line short of the full transcript, and 35%, 60%, 83% and 97% of its
raw line count. For each prefix, two `render_index.json` (plus, for the
live case, `render_index.tail.ndjson`) documents are built over the exact
same truncated transcript bytes:

  - `index_kind="live"`: fed through the REAL incremental writer
    (`render_index.LiveRenderIndex.append`/`.flush`, exactly the
    append-then-flush-per-line cadence `engine/runner.py`'s `pump()`
    uses), with NO final `force=True` flush -- the iteration genuinely
    has not ended, so the writer's own on-disk state may still lag
    behind a full MAIN republish, extended only by whatever
    `render_index.tail.ndjson` shards its own fixed cadence has appended
    since the last one, exactly as a real still-running iteration's own
    index looks on disk at that exact byte.
  - `index_kind="rebuilt"`: `render_index.write_index`, a full from-
    scratch rebuild over the identical bytes -- what a reader would see
    if the engine's own end-of-iteration bookkeeping had already run
    (even though, for this prefix, nothing downstream says it has).

Both variants describe an iteration whose `meta.json` carries no
`endedAt` -- the prefix is what a reader would see mid-stream, whichever
index happens to be on disk for it -- and both are read through the exact
client-facing call (`ui_server.rendered_log_lines`), on BOTH of its
surfaces (`test_tail_whole_call_read_bound._wire`'s own `"live"`/`"disk"`
wiring -- `bounded_tail`'s own answer, and therefore every byte either
surface reads and every line either surface renders, is identical either
way for a `tail`-bounded request; only the returned `live` flag differs),
first and a repeated request, with every byte read off every file counted
by `test_log_render_bounded_tail._count_transcript_bytes` (index and
side-state bytes included, exactly the counter `test_tail_whole_call_
read_bound.py` itself uses).

Every case above is also built a second way (`collapse="stale"`): the
iteration first FINISHES over the whole fixture, with the engine's own
end-of-iteration `collapse_index.json` written for it, and only then
loses its trailing lines -- so the legacy side index left on disk is
stale for the prefix (its `transcriptBytes` names the whole fixture) and
`collapse_index.load_index` refuses it. The live `/logs` handler's own
bounded reader (`log_merge.merge_entries_tail`) takes an index FILE that
exists but fails to validate as the cue to resolve the cut's collapse
flags exactly rather than guess them; with a validated render index
beside it, that answer comes off the render index's own checkpoints
(`render_index.resolve_flags_at_offset`) instead of a scan of every
transcript byte before the cut -- the scan that used to push the
`minus_final_line`/`live`-surface case at prelude depth 1,000 past twelve
times the cap. `test_invalid_collapse_index_does_not_scan_the_transcript`
holds the same rule for two sibling shapes of an invalid legacy index: a
truncated (malformed) file and one written by a newer engine build.
"""

from __future__ import annotations

import json

import pytest

from ralphd import collapse_index, render_index
from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines

from test_d1_real_pirunner_active_iteration_reads_within_cap import _fixture_lines
from test_log_render_bounded_tail import _count_transcript_bytes  # noqa: F401 (re-exported via _measured_call)
from test_tail_whole_call_read_bound import _FROZEN_CAP, _measured_call

# PRD Q's own canonical fixture, then both of its named silent-prelude
# depths -- byte-for-byte the same three combinations
# `test_tail_whole_call_read_bound.py`'s own `_FIXTURES` names.
_FIXTURES = [(0, 1_000), (1_000, 10_000), (1_700, 10_000)]

_GAP_LINE = json.dumps(
    {"type": "message_end", "message": {"content": []}, "padding": "x" * 1000})

_PERCENTAGES = (35, 60, 83, 97)
_PREFIX_NAMES = ("minus_final_line",) + tuple(f"{p}pct" for p in _PERCENTAGES)
_TAIL_N = 2

# How the legacy `collapse_index.json` sits beside each prefix: "absent"
# (never written -- the iteration never finished), or left over from the
# iteration having finished over the WHOLE fixture before it was cut back
# ("stale"; the siblings "malformed" and "future_version" are the same
# finished index, then truncated mid-file or re-versioned).
_COLLAPSE_KINDS = ("absent", "stale")
_INVALID_COLLAPSE_SIBLINGS = ("malformed", "future_version")


def _prefix_count(total: int, prefix_name: str) -> int:
    """The raw line count `prefix_name` truncates `total` lines to --
    strictly fewer than the fixture's own full line count in every case,
    per this task's own criterion (1)."""
    if prefix_name == "minus_final_line":
        count = total - 1
    else:
        pct = int(prefix_name[:-3])
        count = int(total * pct / 100)
    assert 1 <= count < total, (prefix_name, total, count)
    return count


def _write_meta_still_running(itdir) -> None:
    """No `endedAt` at all -- the exact shape `engine/loop.py` leaves on
    disk before the runner has returned (see `test_d1_real_pirunner_
    active_iteration_reads_within_cap.py`'s own identical `meta`), so
    both index variants below describe a genuinely still-active
    iteration, never a finished one."""
    meta = {"number": 1, "phase": "worker", "model": "stub-model", "approach": 1,
           "startedAt": "2025-01-01T00:00:00Z"}
    (itdir / "meta.json").write_text(json.dumps(meta))


def _write_live_published_index(itdir, transcript, prefix_lines: list[str]) -> None:
    """Feed `prefix_lines` through the real incremental writer exactly as
    `engine/runner.py`'s `pump()` does for every raw line -- append then
    flush, one line at a time -- with NO final forced flush: this
    prefix's own writer state is left exactly as a genuinely still-
    running iteration's own on-disk index would be at this exact byte,
    lagging MAIN's own geometric publish schedule by however much
    `render_index.tail.ndjson`'s fixed cadence has not yet closed."""
    transcript.touch()
    live = render_index.LiveRenderIndex()
    with open(transcript, "a") as out:
        for line in prefix_lines:
            live.append(out, line + "\n", transcript)
            live.flush(itdir)


def _write_rebuilt_index(itdir, transcript, prefix_lines: list[str]) -> None:
    """The from-scratch rebuild over the identical bytes -- what a reader
    would see had the engine's end-of-iteration bookkeeping already run
    for this exact prefix."""
    transcript.write_text("".join(line + "\n" for line in prefix_lines))
    render_index.write_index(itdir)


def _write_finished_collapse_index(itdir, transcript, full_lines: list[str],
                                   collapse: str) -> None:
    """The engine's own end-of-iteration `collapse_index.json`, written
    over the WHOLE fixture as if the iteration had finished, then (for
    the sibling kinds) damaged the way `collapse` names. The transcript is
    then cut back to the prefix by the caller, so every kind leaves an
    index FILE on disk that `collapse_index.load_index` refuses."""
    transcript.write_text("".join(line + "\n" for line in full_lines))
    collapse_index.write_index(itdir)
    path = itdir / collapse_index.INDEX_FILENAME
    if collapse == "malformed":
        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) // 2])
    elif collapse == "future_version":
        doc = json.loads(path.read_text())
        doc["version"] = collapse_index.INDEX_VERSION + 1
        path.write_text(json.dumps(doc))
    else:
        assert collapse == "stale", collapse
    transcript.write_text("")


def _build_case(tmp_path, prelude_count: int, gap_count: int, prefix_name: str,
                index_kind: str, collapse: str = "absent"):
    """Builds one (fixture, prefix, index_kind) case's own run directory
    and returns `(reg, run_root, workspace_root, full)`, `full` being the
    full renderer's own lines over the EXACT SAME truncated prefix (no
    merge boundaries: this iteration has no `endedAt`, and none of these
    prefixes are short enough for `bounded_tail`'s own backward walk to
    reach byte 0 and synthesize a "start" boundary either, so a plain
    `render_to_lines` over the prefix's own bytes is exactly what
    `bounded_tail` must agree with)."""
    text = f"live-logs-prefix prelude={prelude_count} gap={gap_count}"
    full_lines = _fixture_lines(text, _GAP_LINE, gap_count, prelude_count)
    count = _prefix_count(len(full_lines), prefix_name)
    prefix_lines = full_lines[:count]

    label = f"{prelude_count}-{gap_count}-{prefix_name}-{index_kind}-{collapse}"
    reg = tmp_path / f"registry-{label}"
    run_root = reg / "runs" / "probe"
    itdir = run_root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    _write_meta_still_running(itdir)
    transcript = itdir / "output.jsonl"
    if collapse != "absent":
        _write_finished_collapse_index(itdir, transcript, full_lines, collapse)

    if index_kind == "live":
        _write_live_published_index(itdir, transcript, prefix_lines)
    else:
        _write_rebuilt_index(itdir, transcript, prefix_lines)

    assert render_index.load_index(itdir) is not None, (prelude_count, gap_count,
                                                        prefix_name, index_kind)
    collapse_file = itdir / collapse_index.INDEX_FILENAME
    assert collapse_file.exists() is (collapse != "absent"), collapse
    assert collapse_index.load_index(itdir) is None, collapse

    full_text = "".join(line + "\n" for line in prefix_lines)
    full = render_to_lines(full_text, tty=False, state=new_render_state())
    workspace_root = tmp_path / f"workspace-{label}"
    return reg, run_root, workspace_root, full


def _assert_bounded_and_exact(tmp_path, monkeypatch, prelude_count, gap_count,
                              prefix_name, index_kind, surface, collapse):
    reg, run_root, workspace_root, full = _build_case(
        tmp_path, prelude_count, gap_count, prefix_name, index_kind, collapse)
    assert len(full) >= _TAIL_N, full
    ui_server._drop_run_detail_caches("probe")

    for attempt in ("first", "repeated"):
        read, live, lines = _measured_call(
            monkeypatch, surface, reg, run_root, workspace_root, _TAIL_N)
        case = (prelude_count, gap_count, prefix_name, index_kind, surface,
                collapse, attempt)
        assert lines == full[-_TAIL_N:], (case, lines, full[-_TAIL_N:])
        assert live is (surface == "live"), case
        assert read <= _FROZEN_CAP, (case, read)


@pytest.mark.parametrize("collapse", _COLLAPSE_KINDS)
@pytest.mark.parametrize("surface", ["live", "disk"])
@pytest.mark.parametrize("index_kind", ["live", "rebuilt"])
@pytest.mark.parametrize("prefix_name", _PREFIX_NAMES)
@pytest.mark.parametrize("prelude_count,gap_count", _FIXTURES)
def test_live_logs_prefix_stays_within_the_bound_and_renders_exactly(
        tmp_path, monkeypatch, prelude_count, gap_count, prefix_name, index_kind,
        surface, collapse):
    """Every named prefix of every named fixture, on both `/logs` surfaces,
    first and a repeated request, for both the live writer's own published
    index and a fresh rebuild over the identical bytes, with and without
    the finished iteration's now-stale `collapse_index.json` left on disk:
    at most 1,000,000 bytes read per call (index and side-state bytes
    included), and the rendered lines equal the full renderer's own last
    `N` lines of that SAME truncated prefix exactly."""
    _assert_bounded_and_exact(tmp_path, monkeypatch, prelude_count, gap_count,
                              prefix_name, index_kind, surface, collapse)


@pytest.mark.parametrize("collapse", _INVALID_COLLAPSE_SIBLINGS)
@pytest.mark.parametrize("index_kind", ["live", "rebuilt"])
@pytest.mark.parametrize("prefix_name", ("minus_final_line", "60pct"))
@pytest.mark.parametrize("prelude_count,gap_count", _FIXTURES[1:])
def test_invalid_collapse_index_does_not_scan_the_transcript(
        tmp_path, monkeypatch, prelude_count, gap_count, prefix_name, index_kind,
        collapse):
    """The same rule for two sibling shapes of an invalid legacy index --
    a finished iteration's `collapse_index.json` truncated mid-file, or
    re-versioned by a newer engine build -- on the live `/logs` surface
    (the one whose handler resolves collapse flags from it): a validated
    render index answers the flags, so the call stays within the bound
    and renders exactly."""
    _assert_bounded_and_exact(tmp_path, monkeypatch, prelude_count, gap_count,
                              prefix_name, index_kind, "live", collapse)


def _mixed_collapse_lines() -> list[str]:
    """A transcript exercising every transition the collapse flags have:
    streamed text, thinking, a finished tool call, an unfinished one, an
    unparseable line, and `message_start` resets between messages."""
    def upd(sub, **kw):
        return json.dumps({"type": "message_update",
                           "assistantMessageEvent": {"type": sub, **kw}})
    lines: list[str] = []
    for n in range(6):
        lines.append(json.dumps({"type": "message_start"}))
        if n % 3 != 1:
            lines.append(upd("thinking_start"))
            lines.append(upd("thinking_delta", delta="hmm"))
        if n % 2 == 0:
            lines += [upd("text_delta", delta=f"part {n}"), upd("text_end")]
        lines.append(json.dumps({"type": "tool_execution_start",
                                 "toolCallId": f"c{n}", "toolName": "bash",
                                 "args": {"command": f"echo {n}"}}))
        if n != 4:
            lines.append(json.dumps({"type": "tool_execution_end",
                                     "toolCallId": f"c{n}", "result": "ok"}))
        lines.append("not json at all")
        lines.append(json.dumps({"type": "message_end", "message": {"content": [
            {"type": "text", "text": f"message {n} done"}]}}))
        lines.append(_GAP_LINE)
    return lines


@pytest.mark.parametrize("index_kind", ["live", "rebuilt"])
def test_render_index_flags_agree_with_the_legacy_scan_at_every_line_start(
        tmp_path, index_kind):
    """`render_index.resolve_flags_at_offset` answers exactly what the
    legacy side index's own scan fallback answers, at every line start of
    a transcript that sets and resets each flag -- for a fresh rebuild and
    for the live writer's lagging index, whose uncovered suffix is replayed
    from the last checkpoint at or before the cut."""
    lines = _mixed_collapse_lines()
    itdir = tmp_path / "iterations" / "0001"
    itdir.mkdir(parents=True)
    transcript = itdir / "output.jsonl"
    if index_kind == "live":
        _write_live_published_index(itdir, transcript, lines)
    else:
        _write_rebuilt_index(itdir, transcript, lines)
    assert collapse_index.load_index(itdir) is None
    offsets, at = [], 0
    for line in lines:
        offsets.append(at)
        at += len(line) + 1
    offsets.append(at)
    answers = set()
    for offset in offsets:
        flags = render_index.resolve_flags_at_offset(itdir, offset)
        assert flags == collapse_index.resolve_flags_at_offset(itdir, offset), offset
        answers.add(tuple(sorted(flags.items())))
    assert len(answers) >= 4, answers


@pytest.mark.parametrize("tail", [1, 3, 7, 20])
def test_logs_handler_answer_with_a_stale_collapse_index_matches_a_valid_one(
        tmp_path, tail):
    """The engine's own `/logs?tail=N` merge over a still-running prefix
    is the same text whether the finished iteration's `collapse_index.json`
    is left stale beside a validated render index or rewritten for the
    prefix: the render index resolves the cut's collapse seed to the value
    the valid legacy index records."""
    from ralphd.log_merge import merged_lines

    full = _mixed_collapse_lines()
    answers = []
    for variant in ("stale", "rewritten"):
        run_root = tmp_path / variant / "runs" / "probe"
        itdir = run_root / "iterations" / "0001"
        itdir.mkdir(parents=True)
        _write_meta_still_running(itdir)
        transcript = itdir / "output.jsonl"
        _write_finished_collapse_index(itdir, transcript, full, "stale")
        _write_rebuilt_index(itdir, transcript, full[:-1])
        if variant == "rewritten":
            collapse_index.write_index(itdir)
            assert collapse_index.load_index(itdir) is not None
        else:
            assert collapse_index.load_index(itdir) is None
        answers.append(merged_lines(run_root, tail=tail))
    assert answers[0] == answers[1], answers
    assert any("collapseSeed" in line for line in answers[0]), answers[0]
