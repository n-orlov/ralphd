"""Task 016 (#17): the hub serves a run's steering history, live and on disk.

Issue #17's complaint is that steering is write-only in every surface: an
operator can post a message and then has no way to see what was queued, what
the loop already applied, or what the text actually said. This module pins the
server half of the fix (the hub's steering panel is task 017, the CLI view is
task 018):

  * `engine.state.steering_entries` is the ONE reader of `<run>/steering/`
    (name, arrival timestamp, pending/applied state, body), used by the
    engine's `GET /steering` *and* by the hub's on-disk fallback, so the two
    answers describe the same run identically rather than in two vocabularies;
  * `GET /api/runs/<id>/steering` is live-first with an on-disk fallback --
    the same shape tasks 038/039 gave the log tail and task 056 the PRD -- so
    a finished or killed run's steering history stays readable;
  * a pre-v0.6 engine (whose `GET /steering` answers with only
    `file`/`consumed`) is completed from the run dir instead of served
    half-empty, and its verdict on applied-ness still wins;
  * a run nobody steered says so (`NO_STEERING`), it does not just look
    broken.

The strongest test here is the last one: a REAL engine's live answer and the
container-gone on-disk answer, for the same run, must be the same entries.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

from ralphd.cli.ui_server import NO_STEERING, _with_steering_display, steering_list
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    STEERING_APPLIED,
    STEERING_CONSUMED_FILE,
    STEERING_PENDING,
    RunDir,
    format_local_time,
    steering_entries,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import (
    StubEngineApi,
    UiServer,
    _write_dead_run,
    _write_run_with_api,
)

ONE = "First message: do <the thing> & keep going.\n"
TWO = "Second message.\n"


@pytest.fixture
def ui():
    servers = []

    def make(registry: Path) -> UiServer:
        s = UiServer(registry)
        s.wait_ready()
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()


def _seed_steering(run_dir: Path, files: dict[str, str],
                   consumed: list[str] | None = None) -> None:
    sdir = run_dir / "steering"
    sdir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (sdir / name).write_text(body)
    if consumed is not None:
        (sdir / STEERING_CONSUMED_FILE).write_text(json.dumps(consumed))


def _hub_entries(run_dir: Path, *, bodies: bool = True) -> list[dict]:
    """What the hub's endpoint must answer for this run dir: the shared
    reader's entries plus the display fields the hub renders server-side
    (task 017's `tsLocal`) -- additive, never a different vocabulary."""
    return [_with_steering_display(e)
            for e in steering_entries(run_dir, bodies=bodies)]


# -- the shared reader ----------------------------------------------------


def test_no_steering_dir_is_no_entries_not_an_error(tmp_path):
    """An operator who never steered is the common case, not a failure."""
    assert steering_entries(tmp_path) == []


def test_entries_carry_name_timestamp_state_and_body(tmp_path):
    _seed_steering(tmp_path, {"001-cost-zero.md": ONE, "002-prefix.md": TWO},
                   consumed=["001-cost-zero.md"])
    entries = steering_entries(tmp_path)
    assert [e["file"] for e in entries] == ["001-cost-zero.md", "002-prefix.md"]
    assert [e["seq"] for e in entries] == [1, 2]
    assert [e["name"] for e in entries] == ["cost-zero", "prefix"]
    assert [e["state"] for e in entries] == [STEERING_APPLIED, STEERING_PENDING]
    assert [e["consumed"] for e in entries] == [True, False]
    assert [e["body"] for e in entries] == [ONE, TWO]
    assert [e["hasBody"] for e in entries] == [True, True]
    # `ts` is the file's mtime, i.e. its arrival time, in the engine's own
    # timestamp format -- no second index to drift out of sync.
    assert all(e["ts"].endswith("Z") and e["ts"][:2] == "20" for e in entries)


def test_bodies_false_omits_the_body_but_not_hasbody(tmp_path):
    _seed_steering(tmp_path, {"001-x.md": ONE})
    (entry,) = steering_entries(tmp_path, bodies=False)
    assert "body" not in entry
    assert entry["hasBody"] is True


def test_an_empty_message_is_flagged_as_bodyless(tmp_path):
    _seed_steering(tmp_path, {"001-x.md": "\n \n"})
    (entry,) = steering_entries(tmp_path)
    assert entry["hasBody"] is False


def test_files_outside_the_naming_scheme_are_ignored(tmp_path):
    """`.consumed.json` and any stray file must not become a message."""
    _seed_steering(tmp_path, {"001-real.md": ONE, "notes.md": "not steering\n",
                              "1-short.md": "not steering\n"},
                   consumed=[])
    assert [e["file"] for e in steering_entries(tmp_path)] == ["001-real.md"]


def test_a_junk_consumed_marker_leaves_everything_pending(tmp_path):
    """Unreadable bookkeeping must not silently mark messages applied."""
    _seed_steering(tmp_path, {"001-x.md": ONE})
    (tmp_path / "steering" / STEERING_CONSUMED_FILE).write_text("{not json")
    (entry,) = steering_entries(tmp_path)
    assert (entry["state"], entry["consumed"]) == (STEERING_PENDING, False)


# -- the engine endpoint --------------------------------------------------


def _engine_get(tmp_path, path: str):
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
    sup = LoopSupervisor(JobConfig(run_id="unit"), run, tmp_path)
    app = create_app(sup.cfg, run, sup)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://engine") as c:
            r = await c.get(path)
            assert r.status_code == 200, r.text
            return r.json()

    return asyncio.run(go())


def test_engine_get_steering_serves_the_full_entries(tmp_path):
    _seed_steering(tmp_path, {"001-a.md": ONE, "002-b.md": TWO},
                   consumed=["001-a.md"])
    body = _engine_get(tmp_path, "/steering")
    assert body == steering_entries(tmp_path)
    # purely additive: the pre-v0.6 keys are still exactly where they were
    assert [(e["file"], e["consumed"]) for e in body] == \
        [("001-a.md", True), ("002-b.md", False)]


# -- the hub endpoint -----------------------------------------------------


def test_hub_proxies_a_live_run(tmp_path, ui):
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"state": "running"})
    run_dir = _write_run_with_api(registry, "run-live", engine, state="running")
    _seed_steering(run_dir, {"001-a.md": ONE, "002-b.md": TWO},
                   consumed=["001-a.md"])
    # what the real engine's route answers with, from the same reader
    engine.steering_body = steering_entries(run_dir)

    server = ui(registry)
    try:
        code, body = server.get("/api/runs/run-live/steering")
        assert code == 200
        assert body["live"] is True
        assert body["notice"] == ""
        assert body["entries"] == _hub_entries(run_dir)
        assert ("GET", "/steering", None) in engine.requests
    finally:
        engine.close()


def test_hub_falls_back_to_disk_for_a_dead_run(tmp_path, ui):
    """The point of #17's fallback: the run is over, the history is not."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-gone", state="failed")
    _seed_steering(run_dir, {"001-a.md": ONE, "002-b.md": TWO},
                   consumed=["001-a.md", "002-b.md"])

    server = ui(registry)
    code, body = server.get("/api/runs/run-gone/steering")
    assert code == 200
    assert body["live"] is False          # so the UI can label it a snapshot
    assert body["entries"] == _hub_entries(run_dir)
    assert [e["state"] for e in body["entries"]] == [STEERING_APPLIED] * 2
    assert [e["body"] for e in body["entries"]] == [ONE, TWO]


def test_live_and_on_disk_answers_are_the_same_entries(tmp_path, ui):
    """Same run, both code paths, byte-identical payload -- the criterion
    that keeps the hub from growing a second steering vocabulary."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"state": "running"})
    run_dir = _write_run_with_api(registry, "run-both", engine, state="running")
    _seed_steering(run_dir, {"001-a.md": ONE, "002-b.md": TWO},
                   consumed=["001-a.md"])
    engine.steering_body = steering_entries(run_dir)

    server = ui(registry)
    try:
        _, live_body = server.get("/api/runs/run-both/steering")
    finally:
        engine.close()
    (run_dir / "host.json").unlink()       # the container is gone
    _, disk_body = server.get("/api/runs/run-both/steering")

    assert live_body["live"] is True and disk_body["live"] is False
    assert live_body["entries"] == disk_body["entries"]


def test_a_pre_v06_live_answer_is_completed_from_disk(tmp_path, ui):
    """An old engine answers `file`/`consumed` only. The panel still needs a
    name, a timestamp and the text -- all of which are in the run dir the hub
    is running on."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"state": "running"},
                           steering=[{"file": "001-a.md", "consumed": True},
                                     {"file": "002-b.md", "consumed": False}])
    run_dir = _write_run_with_api(registry, "run-old", engine, state="running")
    # on disk NOTHING is consumed yet -- the LIVE side decides applied-ness
    _seed_steering(run_dir, {"001-a.md": ONE, "002-b.md": TWO}, consumed=[])

    server = ui(registry)
    try:
        code, body = server.get("/api/runs/run-old/steering")
    finally:
        engine.close()
    assert code == 200 and body["live"] is True
    first, second = body["entries"]
    assert (first["state"], first["consumed"]) == (STEERING_APPLIED, True)
    assert (second["state"], second["consumed"]) == (STEERING_PENDING, False)
    assert (first["name"], first["body"]) == ("a", ONE)
    assert second["body"] == TWO
    assert first["ts"] == steering_entries(run_dir)[0]["ts"]


def test_a_live_entry_with_no_file_on_disk_invents_nothing(tmp_path, ui):
    """A file the hub cannot see (a run dir it does not hold) is still listed
    with its state -- but no timestamp or body is guessed for it."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"state": "running"},
                           steering=[{"file": "007-ghost.md", "consumed": False}])
    _write_run_with_api(registry, "run-ghost", engine, state="running")

    server = ui(registry)
    try:
        _, body = server.get("/api/runs/run-ghost/steering")
    finally:
        engine.close()
    (entry,) = body["entries"]
    assert entry["state"] == STEERING_PENDING
    assert entry["name"] == "007-ghost.md"     # nothing better is known
    assert "ts" not in entry and "body" not in entry


def test_a_run_nobody_steered_says_so(tmp_path, ui):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-quiet", state="succeeded")

    server = ui(registry)
    code, body = server.get("/api/runs/run-quiet/steering")
    assert code == 200
    assert body["entries"] == []
    assert body["notice"] == NO_STEERING == "(no steering messages)"


def test_unknown_run_is_404(tmp_path, ui):
    registry = tmp_path / "registry"
    registry.mkdir()
    server = ui(registry)
    code, body = server.get("/api/runs/nope/steering")
    assert code == 404
    assert "not found" in body["error"]


def test_a_nonsense_live_answer_falls_back_to_disk(tmp_path, ui):
    """A garbled/unexpected live payload must not blank the history."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"state": "running"}, steering=None)  # 404s
    run_dir = _write_run_with_api(registry, "run-404", engine, state="running")
    _seed_steering(run_dir, {"001-a.md": ONE}, consumed=[])

    server = ui(registry)
    try:
        _, body = server.get("/api/runs/run-404/steering")
    finally:
        engine.close()
    assert body["live"] is False
    assert [e["file"] for e in body["entries"]] == ["001-a.md"]


def test_steering_list_can_skip_bodies(tmp_path):
    """The list view (task 017's header rows) does not need to ship every
    message body; `hasBody` still tells it there is one to open."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-nb", state="succeeded")
    _seed_steering(run_dir, {"001-a.md": ONE}, consumed=[])
    live, entries = steering_list(registry, "run-nb", bodies=False)
    assert live is False
    assert "body" not in entries[0] and entries[0]["hasBody"] is True


# -- the display fields the panel renders (task 017) ----------------------


def test_entries_carry_a_server_formatted_arrival_time(tmp_path, ui):
    """Task 017 (#17): the panel shows `tsLocal`, formatted by the ONE shared
    absolute-time formatter -- so "local" means the host running ralphd and
    app.js never re-implements a timestamp format."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-ts", state="succeeded")
    _seed_steering(run_dir, {"001-a.md": ONE}, consumed=[])

    server = ui(registry)
    _, body = server.get("/api/runs/run-ts/steering")
    (entry,) = body["entries"]
    assert entry["tsLocal"] == format_local_time(entry["ts"])
    assert entry["ts"] == steering_entries(run_dir)[0]["ts"]   # raw value untouched


def test_an_entry_with_no_timestamp_claims_no_arrival_time(tmp_path, ui):
    """`format_local_time(None)` renders `n/a`, which in a row would read like
    a real value -- a ghost entry (task 016) gets no `tsLocal` at all."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"state": "running"},
                           steering=[{"file": "007-ghost.md", "consumed": False}])
    _write_run_with_api(registry, "run-ghost-ts", engine, state="running")

    server = ui(registry)
    try:
        _, body = server.get("/api/runs/run-ghost-ts/steering")
    finally:
        engine.close()
    (entry,) = body["entries"]
    assert "ts" not in entry and "tsLocal" not in entry


def test_a_forged_tslocal_from_a_live_answer_is_recomputed(tmp_path, ui):
    """Same discipline as `_with_approach_display`: the rendered string is
    always derived from the entry's own `ts`, never trusted as sent."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(
        status={"state": "running"},
        steering=[{"file": "001-a.md", "ts": "2026-01-02T03:04:05Z",
                   "state": STEERING_PENDING, "consumed": False,
                   "tsLocal": "whenever I say"}])
    _write_run_with_api(registry, "run-forged", engine, state="running")

    server = ui(registry)
    try:
        _, body = server.get("/api/runs/run-forged/steering")
    finally:
        engine.close()
    (entry,) = body["entries"]
    assert entry["tsLocal"] == format_local_time("2026-01-02T03:04:05Z")


def test_app_js_renders_the_server_side_arrival_string(tmp_path):
    """The panel must not grow a second timestamp vocabulary in JS: it renders
    `tsLocal` and never parses the raw `ts` itself."""
    app_js = (Path(__file__).parents[1] / "src" / "ralphd" / "cli" / "web"
              / "app.js").read_text()
    assert "e.tsLocal" in app_js
    steering_part = app_js[app_js.index("function renderSteering"):]
    steering_part = steering_part[:steering_part.index("\nasync function loadSteering")]
    assert "Date" not in steering_part and "fmtDuration" not in steering_part


# -- against a real engine ------------------------------------------------


def _wait_for(fn, timeout=30, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.2)
    raise TimeoutError(f"{what} never happened; last: {last!r}")


def test_real_engine_live_then_container_gone_agree(tmp_path, live, ui):
    """End to end: steer a REAL running engine, watch the entry flip from
    pending to applied through the hub, then kill the engine and assert the
    on-disk fallback reports the very same entries."""
    # STUB_SLEEP/STUB_TASKS keep the loop busy long enough to be steered
    # *while running* (a finished job refuses steering, 409).
    run = live(run_id="steerhub", job={"iterations": 8, "on_complete": "idle"},
               stub_env={"STUB_SLEEP": "1", "STUB_TASKS": "4"})
    run.wait_api()
    server = ui(run.registry)

    def entries():
        code, body = server.get("/api/runs/steerhub/steering")
        assert code == 200
        return body

    posted = server.post("/api/runs/steerhub/steer",
                         {"message": ONE, "name": "from-hub"})
    assert posted[0] == 202, posted

    pending = _wait_for(lambda: entries() if entries()["entries"] else None,
                        what="the queued entry appearing")
    assert pending["live"] is True
    (entry,) = pending["entries"]
    assert entry["name"] == "from-hub"
    assert entry["body"].strip() == ONE.strip()
    assert entry["state"] == STEERING_PENDING

    def applied_yet():
        body = entries()
        return body if body["entries"][0]["state"] == STEERING_APPLIED else None

    applied = _wait_for(applied_yet, what="the loop consuming the entry")
    assert applied["entries"][0]["file"] == entry["file"]

    live_entries = applied["entries"]
    run.stop()
    _wait_for(lambda: entries()["live"] is False, what="the API going away")
    assert entries()["entries"] == live_entries
