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
import time
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


def test_unknown_run_gets_404_not_304_regardless_of_if_none_match(hub):
    base, _reg = hub
    status, _headers, body = _get(
        base, "/api/runs/nope", {"If-None-Match": '"whatever"'})
    assert status == 404
    assert json.loads(body)["error"] == "run nope not found"
