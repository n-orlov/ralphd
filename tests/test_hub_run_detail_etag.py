"""Task 003 (A.1): `ETag` on `GET /api/runs/<id>`, and `If-None-Match`.

The run-detail poll (`app.js`'s refresh loop) re-fetches and re-renders the
whole detail view on a fixed interval even when nothing on disk moved since
the last poll. `run_detail_etag` gives the route a cheap freshness tag --
the max mtime of the four files that can move what `run_detail` reports
(`status.json`, `tasks.json`, `events.jsonl`, the newest iteration's
`meta.json`) -- so a client that already has the current view can say so
with `If-None-Match` and get back `304` with nothing to parse or render.

In-process against the real `Handler` (via `ui_server.make_server`, in a
background thread in THIS process -- no subprocess, unlike `test_cli_ui.py`'s
black-box `UiServer`), so a test can control the run dir's mtimes precisely
and inspect response headers directly.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ralphd.cli import ui_server


@pytest.fixture
def hub(tmp_path):
    reg = tmp_path / "registry"
    (reg / "runs").mkdir(parents=True)
    server = ui_server.make_server(reg, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", reg
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str, headers: dict | None = None):
    """Returns (status, headers, body bytes)."""
    req = urllib.request.Request(f"{base}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _seed_run(reg: Path, run_id: str = "etag-run") -> Path:
    run_dir = reg / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "succeeded", "verdict": "verified",
        "phase": "review", "approach": 1, "iterationsUsed": 1,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z"}))
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": []}))
    return run_dir


# The four files the tag is required to be derived from, plus one that must
# NOT count (an OLDER iteration's meta.json: only the NEWEST one does).
STATUS = "status.json"
TASKS = "tasks.json"
EVENTS = "events.jsonl"
NEWEST_META = "iterations/0002/meta.json"
OLDER_META = "iterations/0001/meta.json"
DERIVED_FROM = (STATUS, TASKS, EVENTS, NEWEST_META)


def _seed_all_four(reg: Path, run_id: str = "etag-run") -> Path:
    """A run dir holding all four derivation inputs, plus an older iteration
    whose `meta.json` is deliberately present so a test can prove it does not
    feed the tag."""
    run_dir = _seed_run(reg, run_id)
    (run_dir / EVENTS).write_text(
        json.dumps({"type": "run.started", "ts": "2026-01-01T00:00:00Z"}) + "\n")
    for rel, number in ((OLDER_META, 1), (NEWEST_META, 2)):
        path = run_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "number": number, "startedAt": "2026-01-01T00:00:00Z",
            "endedAt": "2026-01-01T00:01:00Z", "exitCode": 0}))
    return run_dir


def _set_mtimes(run_dir: Path, mtimes: dict[str, float]) -> None:
    for rel, when in mtimes.items():
        os.utime(run_dir / rel, (when, when))


def _expected_tag(run_dir: Path, run_id: str, rels) -> str:
    """The tag the max-mtime rule demands, computed from what the filesystem
    actually stored (not from the floats we asked for), over `rels` only."""
    newest = max((run_dir / rel).stat().st_mtime for rel in rels)
    return f'"{run_id}-{newest:.6f}"'


def test_get_run_detail_carries_a_quoted_etag(hub):
    base, reg = hub
    _seed_run(reg)
    status, headers, body = _get(base, "/api/runs/etag-run")
    assert status == 200
    etag = headers.get("ETag")
    assert etag is not None
    assert etag.startswith('"') and etag.endswith('"')
    assert json.loads(body)["runId"] == "etag-run"


def test_matching_if_none_match_gets_304_with_empty_body(hub):
    base, reg = hub
    _seed_run(reg)
    status1, headers1, _ = _get(base, "/api/runs/etag-run")
    assert status1 == 200
    etag = headers1.get("ETag")
    assert etag

    status2, headers2, body2 = _get(
        base, "/api/runs/etag-run", {"If-None-Match": etag})
    assert status2 == 304
    assert body2 == b""
    assert headers2.get("ETag") == etag


def test_tasks_json_change_yields_a_different_etag_and_a_200(hub):
    base, reg = hub
    run_dir = _seed_run(reg)
    status1, headers1, _ = _get(base, "/api/runs/etag-run")
    etag1 = headers1.get("ETag")
    assert status1 == 200

    # Force the mtime forward: some filesystems have coarser resolution than
    # a same-process write-then-read can reliably beat.
    new_mtime = (run_dir / "tasks.json").stat().st_mtime + 2
    run_dir.joinpath("tasks.json").write_text(
        json.dumps({"tasks": [{"id": "001", "status": "pending"}]}))
    os.utime(run_dir / "tasks.json", (new_mtime, new_mtime))

    status2, headers2, body2 = _get(
        base, "/api/runs/etag-run", {"If-None-Match": etag1})
    assert status2 == 200
    etag2 = headers2.get("ETag")
    assert etag2 is not None
    assert etag2 != etag1
    payload = json.loads(body2)
    assert payload["tasks"]["tasks"] == [{"id": "001", "status": "pending"}]


def test_the_etag_is_the_max_mtime_of_exactly_the_four_derivation_inputs(hub):
    """A.1's first fact, pinned with controlled mtimes: the served tag is the
    MAXIMUM mtime of `status.json`, `tasks.json`, `events.jsonl` and the
    NEWEST iteration's `meta.json` -- here that maximum is held by
    `events.jsonl`, while the four inputs are spread over four distinct
    mtimes so a tag built from any single one of them, from the minimum, or
    from a sum would differ."""
    base, reg = hub
    run_dir = _seed_all_four(reg)
    base_t = 1_700_000_000.0
    _set_mtimes(run_dir, {
        STATUS: base_t + 10,
        TASKS: base_t + 20,
        EVENTS: base_t + 40,       # the maximum over the four inputs
        NEWEST_META: base_t + 30,
        # Newer than every input, but on an iteration that is NOT the newest,
        # so it must not reach the tag.
        OLDER_META: base_t + 900,
    })

    status, headers, _ = _get(base, "/api/runs/etag-run")
    assert status == 200
    assert headers.get("ETag") == _expected_tag(run_dir, "etag-run", DERIVED_FROM)
    # Same statement negatively: the ignored older meta.json does not set it.
    assert headers.get("ETag") != _expected_tag(run_dir, "etag-run", [OLDER_META])


@pytest.mark.parametrize("newest", DERIVED_FROM)
def test_each_of_the_four_inputs_sets_the_tag_when_it_holds_the_max(hub, newest):
    """Every one of the four files really feeds the derivation: whichever of
    them holds the max mtime is the one the tag reports."""
    base, reg = hub
    run_dir = _seed_all_four(reg)
    base_t = 1_700_000_000.0
    _set_mtimes(run_dir, {rel: base_t for rel in DERIVED_FROM})
    _set_mtimes(run_dir, {OLDER_META: base_t - 100, newest: base_t + 60})

    status, headers, _ = _get(base, "/api/runs/etag-run")
    assert status == 200
    assert headers.get("ETag") == _expected_tag(run_dir, "etag-run", [newest])
    assert headers.get("ETag") == _expected_tag(run_dir, "etag-run", DERIVED_FROM)


@pytest.mark.parametrize("moved", DERIVED_FROM)
def test_moving_an_input_backwards_leaves_the_max_derived_tag_alone(hub, moved):
    """It is the MAX, not the last write or a mix of all four: rewriting one
    input to an OLDER mtime than the maximum cannot change the tag."""
    base, reg = hub
    run_dir = _seed_all_four(reg)
    base_t = 1_700_000_000.0
    _set_mtimes(run_dir, {rel: base_t for rel in DERIVED_FROM})
    _set_mtimes(run_dir, {OLDER_META: base_t, EVENTS: base_t + 300})
    before = _get(base, "/api/runs/etag-run")[1].get("ETag")
    assert before == _expected_tag(run_dir, "etag-run", [EVENTS])

    if moved != EVENTS:
        _set_mtimes(run_dir, {moved: base_t - 50})
        status, headers, body = _get(
            base, "/api/runs/etag-run", {"If-None-Match": before})
        assert (status, body) == (304, b"")
        assert headers.get("ETag") == before
    else:
        # Move the max itself backwards and the tag must follow it down to
        # the new maximum (the other three, still at base_t).
        _set_mtimes(run_dir, {moved: base_t - 50})
        status, headers, _ = _get(
            base, "/api/runs/etag-run", {"If-None-Match": before})
        assert status == 200
        assert headers.get("ETag") == _expected_tag(
            run_dir, "etag-run", DERIVED_FROM)
        assert headers.get("ETag") != before


def test_unknown_run_gets_404_not_304_regardless_of_if_none_match(hub):
    base, _reg = hub
    status, _headers, body = _get(
        base, "/api/runs/nope", {"If-None-Match": '"whatever"'})
    assert status == 404
    assert json.loads(body)["error"] == "run nope not found"
