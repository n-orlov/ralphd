"""A malformed incremental state encoding is unusable index state, never an
exception (v0.27 PRD D.3, E.1; DoD 4 and 5; verdict-gate finding VG-03).

`render_index.json`'s `states` array and every `render_index.tail.ndjson`
shard's own `states` array store each distinct render state as a patch
against the one before it (`_diff_state`/`_apply_state_patch`). A patch
whose encoding is syntactically valid JSON but not the shape the writer
produces -- a `del` member that is an array or an object (unhashable as a
dictionary key), a first full state whose `tool_args` is not an object, a
patch naming an unknown key -- must be refused as an unusable index, so
the client-facing call (`ui_server.rendered_log_lines`) falls back to the
exact full render on both surfaces, and never raises. A malformed MAIN
index falls back exactly; a malformed TAIL shard drops only the tail
extension and leaves MAIN's own coverage usable.
"""

from __future__ import annotations

import json

import pytest

from ralphd import render_index
from ralphd.cli import ui_server
from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.log_merge import merged_lines

from test_tail_whole_call_read_bound import _wire


def _batched_tool_lines(n):
    starts = [json.dumps({"type": "tool_execution_start", "toolCallId": f"call-{i:04}",
                          "toolName": "bash", "args": {"command": f"echo {i:04}"}})
              for i in range(n // 2)]
    ends = [json.dumps({"type": "tool_execution_end", "toolCallId": f"call-{i:04}",
                        "result": "ok"})
            for i in range(n // 2)]
    return starts + ends


def _iteration(tmp_path):
    reg = tmp_path / "registry"
    run = reg / "runs" / "probe"
    itdir = run / "iterations" / "0001"
    itdir.mkdir(parents=True)
    (itdir / "meta.json").write_text(json.dumps({
        "number": 1, "phase": "worker", "model": "stub", "approach": 1,
        "startedAt": "2026-01-01T00:00:00Z", "endedAt": "2026-01-01T00:01:00Z",
        "exitCode": 0}))
    return reg, run, itdir


def _full(run):
    return render_to_lines("".join(merged_lines(run)), tty=False, state=new_render_state())


def _client_lines(monkeypatch, tmp_path, reg, run, surface, n):
    ui_server._drop_run_detail_caches("probe")
    _wire(monkeypatch, surface, run, tmp_path / "workspace")
    return ui_server.rendered_log_lines(reg, "probe", n)


# The gate's two shapes (an unhashable `del` member), then siblings of the
# same rule: a non-string but hashable `del` member, a non-string
# `tool_pending` add value, an unknown patch key, a wrong-typed whole value,
# and an `add`/`del` object carrying an extra member.
_MAIN_PATCHES = {
    "del-array-key": {"tool_args": {"add": {}, "del": [[]]}},
    "del-object-key": {"tool_args": {"add": {}, "del": [{}]}},
    "del-int-key": {"tool_args": {"add": {}, "del": [7]}},
    "pending-add-not-pair": {"tool_pending": {"add": {"call-x": 42}, "del": []}},
    "unknown-key": {"bogus": True},
    "flag-not-bool": {"text_seen": "yes"},
    "extra-patch-member": {"tool_args": {"add": {}, "del": [], "set": {}}},
}


@pytest.mark.parametrize("surface", ["disk", "live"])
@pytest.mark.parametrize("bad_key", [[], {}], ids=["array-key", "object-key"])
def test_malformed_state_patch_falls_back_exactly(tmp_path, monkeypatch, surface, bad_key):
    """The verdict gate's own reproducer: only the second state's
    `tool_args` patch is changed, to a `del` member that cannot be a
    dictionary key."""
    reg, run, itdir = _iteration(tmp_path)
    (itdir / "output.jsonl").write_text("".join(x + "\n" for x in _batched_tool_lines(2)))
    render_index.write_index(itdir)
    full = _full(run)
    assert render_index.bounded_tail(run, 2) == full[-2:]
    path = itdir / render_index.INDEX_FILENAME
    doc = json.loads(path.read_text())
    assert len(doc["states"]) > 1 and doc["states"][0]["tool_args"]
    doc["states"][1]["tool_args"] = {"add": {}, "del": [bad_key]}
    path.write_text(json.dumps(doc))
    live, lines = _client_lines(monkeypatch, tmp_path, reg, run, surface, 2)
    assert lines == full[-2:]
    assert live is (surface == "live")


@pytest.mark.parametrize("surface", ["disk", "live"])
@pytest.mark.parametrize("patch", list(_MAIN_PATCHES.values()), ids=list(_MAIN_PATCHES))
def test_malformed_main_state_patch_is_refused_and_falls_back(tmp_path, monkeypatch,
                                                              surface, patch):
    reg, run, itdir = _iteration(tmp_path)
    (itdir / "output.jsonl").write_text("".join(x + "\n" for x in _batched_tool_lines(4)))
    render_index.write_index(itdir)
    full = _full(run)
    assert render_index.load_index(itdir) is not None
    path = itdir / render_index.INDEX_FILENAME
    doc = json.loads(path.read_text())
    assert len(doc["states"]) > 1
    doc["states"][1] = patch
    path.write_text(json.dumps(doc))
    assert render_index.load_index(itdir) is None
    for _ in range(2):  # the first request and a repeated one (hub cache)
        live, lines = _client_lines(monkeypatch, tmp_path, reg, run, surface, 2)
        assert lines == full[-2:]
        assert live is (surface == "live")
        monkeypatch.undo()


@pytest.mark.parametrize("first", [
    {"tool_args": [[1]]},                # an array where an object belongs
    {"tool_pending": "abc"},             # a string where an object belongs
], ids=["tool-args-array", "tool-pending-string"])
def test_malformed_first_full_state_is_refused(tmp_path, first):
    """The first `states` entry is a FULL state: one whose `tool_args`/
    `tool_pending` is not an object would crash the next patch applied on
    top of it (`dict([[1]])`, `dict("abc")`), so it is refused up front."""
    _, _, itdir = _iteration(tmp_path)
    (itdir / "output.jsonl").write_text("".join(x + "\n" for x in _batched_tool_lines(4)))
    render_index.write_index(itdir)
    path = itdir / render_index.INDEX_FILENAME
    doc = json.loads(path.read_text())
    doc["states"][0] = {**doc["states"][0], **first}
    path.write_text(json.dumps(doc))
    assert render_index.decode_index(doc) is None
    assert render_index.load_index(itdir) is None


def _live_iteration_with_tail(tmp_path, n_lines):
    """Feed a real `LiveRenderIndex` one raw line at a time with a small
    tail cadence and no final forced flush, so MAIN is behind and the tail
    file carries shards past it."""
    reg, run, itdir = _iteration(tmp_path)
    out_path = itdir / "output.jsonl"
    live = render_index.LiveRenderIndex()
    with open(out_path, "a") as out:
        for line in _batched_tool_lines(n_lines):
            live.append(out, line + "\n", out_path)
            live.flush(itdir, every=2)
    tail = itdir / render_index.TAIL_FILENAME
    assert tail.exists()
    return reg, run, itdir, tail


@pytest.mark.parametrize("surface", ["disk", "live"])
@pytest.mark.parametrize("bad", [
    {"add": {}, "del": [[]]},
    {"add": {}, "del": [{}]},
    {"add": [], "del": []},
], ids=["del-array-key", "del-object-key", "add-not-object"])
def test_malformed_tail_shard_patch_drops_only_the_tail(tmp_path, monkeypatch, surface, bad):
    reg, run, itdir, tail = _live_iteration_with_tail(tmp_path, 12)
    full = _full(run)
    main_only = render_index.load_index(itdir)
    assert main_only is not None
    shards = [json.loads(x) for x in tail.read_text().splitlines() if x.strip()]
    main_doc = json.loads((itdir / render_index.INDEX_FILENAME).read_text())
    main_bytes = main_doc["transcriptBytes"]
    assert main_only["transcriptBytes"] > main_bytes  # the tail extends MAIN
    shards[0]["states"][0]["tool_args"] = bad
    tail.write_text("".join(json.dumps(s) + "\n" for s in shards))
    index = render_index.load_index(itdir)
    assert index is not None and index["transcriptBytes"] == main_bytes
    for _ in range(2):
        live, lines = _client_lines(monkeypatch, tmp_path, reg, run, surface, 2)
        assert lines == full[-2:]
        assert live is (surface == "live")
        monkeypatch.undo()


def test_well_formed_encoding_still_round_trips(tmp_path):
    """The validation refuses nothing the writer itself produces."""
    _, _, itdir = _iteration(tmp_path)
    (itdir / "output.jsonl").write_text("".join(x + "\n" for x in _batched_tool_lines(8)))
    index = render_index.compute_render_index(itdir / "output.jsonl")
    assert render_index.decode_index(render_index.encode_index(index)) == index
