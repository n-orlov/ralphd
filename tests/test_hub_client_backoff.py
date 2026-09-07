"""Task 005 (A.3): in-process pin of the header round trip the hub client's
back-off (`app.js`'s `renderRunDetail`) depends on.

`app.js` cannot be exercised without a real browser (see
`tests/test_browser_hub_backoff.py` for the widened-interval/hidden-pause
half of this task, which needs one) -- but the WIRE CONTRACT its poll relies
on is plain HTTP and needs no browser at all: send no `If-None-Match` on the
first request and get `200` with a tag back; resend that tag and get `304`
with an empty body and the SAME tag; change something the tag is derived
from and resend the now-stale tag, and get `200` again with a DIFFERENT tag.
That is the exact three-tick sequence `load()` drives every poll cycle
through (task 003's `run_detail_etag`/`_send_not_modified` on the server
side), pinned here against the real `Handler` in a background thread in THIS
process -- no subprocess, no browser.
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
    """Returns (status, headers, body bytes) -- mirrors a `fetch()` call
    with `headers` set to whatever `app.js`'s `getJSON` would pass."""
    req = urllib.request.Request(f"{base}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _seed_run(reg: Path, run_id: str = "poll-run") -> Path:
    run_dir = reg / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "running", "verdict": None,
        "phase": "worker", "approach": 1, "iterationsUsed": 1,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z"}))
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": []}))
    return run_dir


def test_client_poll_round_trip_first_tick_carries_no_if_none_match(hub):
    """Tick 1 (`lastEtag` starts `null` in `app.js`): the plain, unconditional
    request every poll opens with gets `200` and a quoted tag back."""
    base, reg = hub
    _seed_run(reg)
    status, headers, body = _get(base, "/api/runs/poll-run")
    assert status == 200
    etag = headers.get("ETag")
    assert etag and etag.startswith('"') and etag.endswith('"')
    assert json.loads(body)["runId"] == "poll-run"


def test_client_poll_round_trip_second_tick_with_matching_tag_is_304(hub):
    """Tick 2: nothing on disk moved between polls, so the SAME tag sent
    back as `If-None-Match` (exactly what `app.js` does once it has one)
    gets `304` with an empty body -- the whole point of the round trip: a
    tick that changes nothing costs no parse, no render."""
    base, reg = hub
    _seed_run(reg)
    status1, headers1, _ = _get(base, "/api/runs/poll-run")
    etag = headers1.get("ETag")
    assert status1 == 200 and etag

    status2, headers2, body2 = _get(
        base, "/api/runs/poll-run", {"If-None-Match": etag})
    assert status2 == 304
    assert body2 == b""
    assert headers2.get("ETag") == etag


def test_client_poll_round_trip_third_tick_after_a_change_is_200_with_a_new_tag(hub):
    """Tick 3: something the tag is derived from changes (`tasks.json`
    here) between two polls -- the STALE tag `app.js` is still holding from
    tick 1 no longer matches, so this tick gets a fresh `200` and a
    DIFFERENT tag to carry into its own next `If-None-Match`."""
    base, reg = hub
    run_dir = _seed_run(reg)
    status1, headers1, _ = _get(base, "/api/runs/poll-run")
    stale_etag = headers1.get("ETag")
    assert status1 == 200 and stale_etag

    # Force the mtime forward: some filesystems have coarser resolution
    # than a same-process write-then-read can reliably beat.
    new_mtime = (run_dir / "tasks.json").stat().st_mtime + 2
    (run_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "001", "status": "pending"}]}))
    os.utime(run_dir / "tasks.json", (new_mtime, new_mtime))

    status2, headers2, body2 = _get(
        base, "/api/runs/poll-run", {"If-None-Match": stale_etag})
    assert status2 == 200
    new_etag = headers2.get("ETag")
    assert new_etag is not None and new_etag != stale_etag
    assert json.loads(body2)["tasks"]["tasks"] == [{"id": "001", "status": "pending"}]
