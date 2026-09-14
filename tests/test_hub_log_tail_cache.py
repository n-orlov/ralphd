"""Task 045 (PRD H.3): `ETag`/`If-None-Match` on `GET /api/runs/<id>/logs`,
and a rendered-tail cache in front of it.

Two things, mirrored on `tests/test_hub_run_detail_etag.py`'s in-process
`Handler` pattern (a real `ui_server.make_server`, in a background thread in
THIS process, so a test can seed a run dir precisely and inspect response
headers directly, with no subprocess and no live container):

1. The route answers a matching `If-None-Match` with `304` and an empty
   body, using the SAME `ETag` `run_detail_etag` already derives for
   `GET /api/runs/<id>` (task 003/A.1) -- `status.json`, `tasks.json`,
   `events.jsonl` and the newest iteration's `meta.json`.
2. A repeated, identical request (same run, same `tail`, nothing on disk
   having moved) is served from `_cached_rendered_log_lines`'s cache without
   a second call to `rendered_log_lines` -- pinned by monkeypatching the
   module-level name and counting calls, the same technique
   `tests/test_log_render_bounded_tail.py` uses for its parse-work probes.
"""

from __future__ import annotations

import json
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
    # Every test in this file reuses the same run id ("log-tail-run") over a
    # freshly seeded but byte-identical fixture, so the module-global
    # `_LOG_TAIL_CACHE` (and its siblings) from an EARLIER test would
    # otherwise still hold a matching key and silently answer this test from
    # someone else's fixture -- clear per run id, same discipline
    # `delete_run`/`_drop_run_detail_caches` already applies on the
    # production delete path.
    ui_server._drop_run_detail_caches("log-tail-run")
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


def _write_dead_run_iteration(run_root: Path, n: int, lines: list[str]) -> None:
    """A finished iteration's `meta.json` + `output.jsonl`, written directly
    to disk -- no `host.json`, so `ui_server._proxy_text` finds no `apiUrl`
    and the route falls back to the on-disk merge with no network attempt
    at all (the same "dead run" shape
    `tests/test_log_render_bounded_tail.py::_write_dead_run_iteration`
    uses)."""
    d = run_root / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    meta = {"number": n, "phase": "worker", "model": "stub-model", "approach": 1,
            "startedAt": f"2025-01-01T00:{n:02d}:00Z",
            "endedAt": f"2025-01-01T00:{n:02d}:30Z",
            "exitCode": 0, "error": None, "usage": {"totalTokens": 10 * n}}
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "output.jsonl").write_text("\n".join(lines) + "\n")


def _seed_dead_run(reg: Path, run_id: str = "log-tail-run") -> Path:
    run_dir = reg / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "succeeded", "verdict": "verified",
        "phase": "review", "approach": 1, "iterationsUsed": 1,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z"}))
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": []}))
    for n in (1, 2):
        _write_dead_run_iteration(run_dir, n, [
            json.dumps({"type": "message_end", "message": {"content": [
                {"type": "text", "text": f"iteration {n} did some work"}]}})])
    return run_dir


def test_repeated_log_tail_request_with_no_disk_change_gets_304(hub):
    base, reg = hub
    _seed_dead_run(reg)

    status1, headers1, body1 = _get(base, "/api/runs/log-tail-run/logs?tail=10")
    assert status1 == 200
    etag = headers1.get("ETag")
    assert etag is not None
    payload1 = json.loads(body1)
    assert payload1["lines"]

    status2, headers2, body2 = _get(
        base, "/api/runs/log-tail-run/logs?tail=10",
        {"If-None-Match": etag})
    assert status2 == 304
    assert body2 == b""
    assert headers2.get("ETag") == etag

    # It is the SAME tag `GET /api/runs/<id>` itself would have served for
    # this run right now -- not a bespoke logs-only derivation.
    detail_status, detail_headers, _ = _get(base, "/api/runs/log-tail-run")
    assert detail_status == 200
    assert detail_headers.get("ETag") == etag


def test_a_disk_change_yields_a_different_etag_and_a_200(hub):
    base, reg = hub
    run_dir = _seed_dead_run(reg)

    status1, headers1, _ = _get(base, "/api/runs/log-tail-run/logs?tail=10")
    etag1 = headers1.get("ETag")
    assert status1 == 200

    import os
    new_mtime = (run_dir / "tasks.json").stat().st_mtime + 2
    (run_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "001", "status": "pending"}]}))
    os.utime(run_dir / "tasks.json", (new_mtime, new_mtime))

    status2, headers2, _ = _get(
        base, "/api/runs/log-tail-run/logs?tail=10",
        {"If-None-Match": etag1})
    assert status2 == 200
    assert headers2.get("ETag") != etag1


def test_repeated_identical_log_tail_request_reuses_the_cache_no_rerender(
        hub, monkeypatch):
    """The cache half, independent of the `ETag`/`304` half above: even a
    request that sends NO `If-None-Match` at all must not pay for a second
    render of the same tail over the same unmoved transcript."""
    base, reg = hub
    _seed_dead_run(reg)

    calls = []
    real = ui_server.rendered_log_lines

    def _counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(ui_server, "rendered_log_lines", _counting)

    status1, _headers1, body1 = _get(base, "/api/runs/log-tail-run/logs?tail=10")
    status2, _headers2, body2 = _get(base, "/api/runs/log-tail-run/logs?tail=10")

    assert status1 == 200 and status2 == 200
    assert body1 == body2
    assert len(calls) == 1


def test_a_different_tail_on_the_same_unmoved_run_is_not_served_from_the_others_slot(
        hub, monkeypatch):
    """The cache is keyed by `tail` too -- a `tail=10` cache hit must never
    answer a `tail=1` request with the wrong slice."""
    base, reg = hub
    _seed_dead_run(reg)

    calls = []
    real = ui_server.rendered_log_lines

    def _counting(*a, **k):
        calls.append(a[2] if len(a) > 2 else k.get("tail"))
        return real(*a, **k)

    monkeypatch.setattr(ui_server, "rendered_log_lines", _counting)

    _status10, _h10, body10 = _get(base, "/api/runs/log-tail-run/logs?tail=10")
    _status1, _h1, body1 = _get(base, "/api/runs/log-tail-run/logs?tail=1")

    assert len(calls) == 2
    lines10 = json.loads(body10)["lines"]
    lines1 = json.loads(body1)["lines"]
    assert len(lines1) == 1
    assert lines1 == lines10[-1:]


def test_unknown_run_gets_404_not_304_for_logs_regardless_of_if_none_match(hub):
    base, _reg = hub
    status, _headers, body = _get(
        base, "/api/runs/nope/logs", {"If-None-Match": '"whatever"'})
    assert status == 404
    assert json.loads(body)["error"] == "run nope not found"
