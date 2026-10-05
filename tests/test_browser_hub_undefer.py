"""#292 (PRD C.3, C.4): browser-tier proof of the hub's "Undefer now" button.

A running fixture whose live API reports one deferred task shows the button
on the task-table row and on the Current iteration panel's deferred entry;
clicking it POSTs through the hub's proxy to the run's
`POST /tasks/<id>/undefer`, the (stub) engine releases the task, and the
`deferred until` pill disappears. A refusal shows the server's message, and a
run with no live API offers no button at all.

Marked `@pytest.mark.browser` for the same reason as `test_browser_hub.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import (  # noqa: F401
    ARTIFACTS_DIR,
    Pw,
    _skip_reason,
    _wait_for,
    pw,
)
from test_cli_ui import StubEngineApi, UiServer, _write_dead_run, _write_run_with_api

SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"

pytestmark = [pytest.mark.browser, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]

UNTIL = "2099-01-01T00:00:00Z"


def _tasks() -> dict:
    return {"tasks": [
        {"id": "001", "title": "done work", "status": "completed"},
        {"id": "002", "title": "gated work", "status": "pending",
         "deferredUntil": UNTIL, "deferredReason": "window closed"},
    ]}


def _deferred_pill_count(pw) -> int:
    return int(pw.eval_js(
        "[...document.querySelectorAll('#task-box .pill')]"
        ".filter((p) => p.textContent.startsWith('deferred until')).length"))


def test_undefer_now_button_releases_the_task_and_the_pill_disappears(tmp_path, pw):
    registry = tmp_path / "registry"
    state: dict = {}

    def release(task_id):
        # What the real loop thread does: clear the deferral and rewrite
        # tasks.json (which also moves the run-detail ETag).
        doc = _tasks()
        for t in doc["tasks"]:
            if t["id"] == task_id:
                t.pop("deferredUntil"), t.pop("deferredReason")
        engine.tasks_body = doc
        (state["run_dir"] / "tasks.json").write_text(json.dumps(doc))

    engine = StubEngineApi(status={"runId": "run-undefer", "state": "running"},
                           tasks=_tasks(), on_undefer=release)
    try:
        state["run_dir"] = _write_run_with_api(registry, "run-undefer", engine,
                                               state="running")
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-undefer")
            _wait_for(pw, "document.getElementById('task-box').innerText", "gated work")
            assert _deferred_pill_count(pw) == 1
            # both placements: the task row and the Current iteration panel
            assert int(pw.eval_js(
                "document.querySelectorAll('#task-box .undefer-now').length")) == 1
            _wait_for(pw, "document.getElementById('current-iteration-box').innerText",
                      "Undefer now")
            pw.screenshot(SCREENSHOTS_DIR / "undefer-now-before.png")

            pw.click("#task-box .undefer-now")

            _wait_for(
                pw, "String([...document.querySelectorAll('#task-box .pill')]"
                    ".filter((p) => p.textContent.startsWith('deferred until')).length)",
                "0")
            assert _deferred_pill_count(pw) == 0
            assert int(pw.eval_js("document.querySelectorAll('.undefer-now').length")) == 0
            assert ("POST", "/tasks/002/undefer") in [(m, p) for m, p, _ in engine.requests]
            # the dialog behind the row was not opened by the button click
            assert pw.eval_js("document.querySelectorAll('dialog[open]').length") == "0"
            pw.screenshot(SCREENSHOTS_DIR / "undefer-now-after.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_undefer_now_refusal_shows_the_servers_message(tmp_path, pw):
    registry = tmp_path / "registry"
    engine = StubEngineApi(
        status={"runId": "run-undefer-409", "state": "running"}, tasks=_tasks(),
        undefer_status=409,
        undefer_body={"title": "task not deferred", "detail": "task '002' is not deferred"})
    try:
        _write_run_with_api(registry, "run-undefer-409", engine, state="running")
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-undefer-409")
            _wait_for(pw, "document.getElementById('task-box').innerText", "Undefer now")
            pw.click("#task-box .undefer-now")
            _wait_for(pw, "document.getElementById('task-box').innerText",
                      "failed: task '002' is not deferred")
        finally:
            server.stop()
    finally:
        engine.close()


def test_a_run_with_no_live_api_offers_no_undefer_button(tmp_path, pw):
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-undefer-dead", state="aborted", verdict="unverified")
    (run_dir / "tasks.json").write_text(json.dumps(_tasks()))
    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-undefer-dead")
        _wait_for(pw, "document.getElementById('task-box').innerText", "gated work")
        assert _deferred_pill_count(pw) == 1
        assert int(pw.eval_js("document.querySelectorAll('.undefer-now').length")) == 0
    finally:
        server.stop()
