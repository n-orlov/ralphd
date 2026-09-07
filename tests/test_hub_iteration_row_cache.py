"""Task 004 (A.2): the hub's server-side derivation caches.

`GET /api/runs/<id>` re-builds the whole detail view -- one shaped row per
iteration, plus the shaped tasks/status blocks -- on every request that is
not answered by task 003's `ETag`/`If-None-Match` 304. A run with hundreds of
iterations paid to re-derive every one of them, through
`engine.state.iteration_detail`, on EVERY plain poll, even when nothing about
that iteration had changed since the last one. This module pins the fix:
iteration rows are cached per `(iteration number, meta.json mtime,
transcript size)`, and the disk-shaped tasks/status blocks are cached per
their own file's mtime, so a warm second request -- deliberately sent
WITHOUT `If-None-Match`, so the 304 short-circuit cannot hide whether the
cache actually worked -- derives almost none of what the first, cold request
had to build from scratch.

In-process against the real `Handler` (`ui_server.make_server`, in a
background thread in THIS process), the `test_hub_run_detail_etag.py`
pattern, so a test can seed hundreds of iterations quickly and count
derivations by monkeypatching the module-level names `run_detail` calls
through -- never by timing anything.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ralphd.cli import ui_server

N_ITERATIONS = 300


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
        # Task 004 (A.2): the cache is keyed per run id, at module scope --
        # drop this test's entries so a later test in the same process (a
        # different run id, but sharing the module dicts) starts cold.
        ui_server._ITERATION_ROW_CACHE.clear()
        ui_server._TASKS_BLOCK_CACHE.clear()
        ui_server._STATUS_BLOCK_CACHE.clear()


def _get(base: str, path: str):
    """Plain GET, deliberately never sending `If-None-Match`: a repeated
    request must go all the way through `run_detail` both times, so the
    ETag's own 304 short-circuit (task 003) cannot be mistaken for this
    cache's effect."""
    req = urllib.request.Request(f"{base}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _seed_run(reg: Path, run_id: str, n_iterations: int) -> Path:
    run_dir = reg / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "succeeded", "verdict": "verified",
        "phase": "review", "approach": 1, "iterationsUsed": n_iterations,
        "iterationsBudget": n_iterations, "startedAt": "2026-01-01T00:00:00Z"}))
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": [
        {"id": "001", "status": "completed"}]}))
    for number in range(1, n_iterations + 1):
        idir = run_dir / "iterations" / f"{number:04d}"
        idir.mkdir(parents=True)
        (idir / "meta.json").write_text(json.dumps({
            "number": number,
            "startedAt": "2026-01-01T00:00:00Z",
            "endedAt": "2026-01-01T00:01:00Z",
            "exitCode": 0,
        }))
        (idir / "output.jsonl").write_text(
            json.dumps({"type": "text", "text": f"iteration {number}"}) + "\n")
    return run_dir


def _counting_wrapper(monkeypatch, target_module, name):
    """Patches `target_module.<name>` with a call-counting wrapper around the
    ORIGINAL function, returning a `list` whose single int element is the
    running call count -- a list so the closure can mutate it."""
    original = getattr(target_module, name)
    counter = [0]

    def wrapped(*args, **kwargs):
        counter[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(target_module, name, wrapped)
    return counter


def test_a_warm_second_request_derives_at_most_two_iteration_rows(hub, monkeypatch):
    base, reg = hub
    _seed_run(reg, "cache-run", N_ITERATIONS)
    counter = _counting_wrapper(monkeypatch, ui_server, "iteration_detail")

    status1, body1 = _get(base, "/api/runs/cache-run")
    assert status1 == 200
    assert len(body1["iterations"]) == N_ITERATIONS
    cold_calls = counter[0]
    # The cold request has nothing cached yet: every iteration is derived.
    assert cold_calls == N_ITERATIONS

    status2, body2 = _get(base, "/api/runs/cache-run")
    assert status2 == 200
    assert len(body2["iterations"]) == N_ITERATIONS
    warm_calls = counter[0] - cold_calls
    # The pinned fact: nothing on disk moved between the two requests, so the
    # warm request derives at most two rows -- counting derivations, never
    # elapsed time.
    assert warm_calls <= 2, (
        f"warm second request re-derived {warm_calls} iteration rows, "
        f"expected at most 2 (cold request derived {cold_calls})")


def test_the_warm_rows_are_identical_to_the_cold_ones(hub, monkeypatch):
    """Not merely fewer derivations -- the SAME rows, served from cache."""
    base, reg = hub
    _seed_run(reg, "cache-run", 5)
    _counting_wrapper(monkeypatch, ui_server, "iteration_detail")

    _, body1 = _get(base, "/api/runs/cache-run")
    _, body2 = _get(base, "/api/runs/cache-run")
    assert body1["iterations"] == body2["iterations"]


def test_an_updated_iterations_meta_mtime_is_re_derived_on_the_next_request(hub, monkeypatch):
    """The cache key really is `(number, meta mtime, transcript size)`: an
    iteration whose `meta.json` gets rewritten (a fault re-explained, a still
    -running row growing) is derived again, not served stale from cache."""
    import os

    base, reg = hub
    run_dir = _seed_run(reg, "cache-run", 3)
    counter = _counting_wrapper(monkeypatch, ui_server, "iteration_detail")

    _get(base, "/api/runs/cache-run")
    first_total = counter[0]

    meta_path = run_dir / "iterations" / "0002" / "meta.json"
    new_mtime = meta_path.stat().st_mtime + 5
    meta_path.write_text(json.dumps({
        "number": 2, "startedAt": "2026-01-01T00:00:00Z",
        "endedAt": "2026-01-01T00:02:00Z", "exitCode": 0}))
    os.utime(meta_path, (new_mtime, new_mtime))

    status, body = _get(base, "/api/runs/cache-run")
    assert status == 200
    # Exactly the one changed iteration is re-derived -- the other two, whose
    # files never moved, are still served from cache.
    assert counter[0] - first_total == 1
    row2 = next(r for r in body["iterations"] if r["number"] == 2)
    assert row2["endedAtLocal"] is not None


def test_a_warm_second_request_does_not_reread_an_unchanged_tasks_json(hub, monkeypatch):
    base, reg = hub
    _seed_run(reg, "cache-run", 2)
    counter = _counting_wrapper(monkeypatch, ui_server, "read_tasks_doc")

    status1, _ = _get(base, "/api/runs/cache-run")
    assert status1 == 200
    cold_calls = counter[0]
    assert cold_calls == 1

    status2, body2 = _get(base, "/api/runs/cache-run")
    assert status2 == 200
    assert counter[0] == cold_calls
    assert body2["tasks"]["tasks"] == [{"id": "001", "status": "completed"}]
