"""#292 (PRD C.3, C.4): the operator surfaces of `POST /tasks/{id}/undefer` --
`ralphctl undefer <run> <task>`, the hub's proxy route
`POST /api/runs/<id>/tasks/<task>/undefer`, and app.js's "Undefer now" button.

The CLI is driven in-process against a stub engine API (it must write nothing
host-side); the hub route runs against a real `ralphctl ui` subprocess; the
button is checked as text against app.js here and driven in a real browser in
`test_browser_hub_undefer.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from test_cli_ui import StubEngineApi, _write_dead_run, _write_run_with_api, ui  # noqa: F401

from tests.conftest import call_ralphctl_in_process

APP_JS = (Path(__file__).resolve().parent.parent / "src" / "ralphd" / "cli"
          / "web" / "app.js").read_text()

UNTIL = "2099-01-01T00:00:00Z"
DEFERRED_TASKS = {"tasks": [
    {"id": "002", "title": "gated work", "status": "pending",
     "deferredUntil": UNTIL, "deferredReason": "window closed"},
]}


@pytest.fixture
def ralphctl(tmp_path, capsys):
    registry = tmp_path / "registry"
    registry.mkdir()

    def run(*argv: str):
        env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
        return call_ralphctl_in_process(list(argv), env=env, capsys=capsys)

    run.registry = registry
    return run


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- the CLI

def test_undefer_posts_to_the_task_route_and_exits_0_writing_nothing_host_side(ralphctl):
    engine = StubEngineApi(tasks=DEFERRED_TASKS)
    try:
        run_dir = _write_run_with_api(ralphctl.registry, "run-u", engine, state="running")
        before = _snapshot(ralphctl.registry)
        res = ralphctl("undefer", "run-u", "002")
        assert res.returncode == 0, res.stderr
        assert [(m, p) for m, p, _ in engine.requests] == [
            ("POST", "/tasks/002/undefer")], engine.requests
        assert "undeferring task 002" in res.stdout, res.stdout
        assert _snapshot(ralphctl.registry) == before, "undefer wrote host-side"
        assert run_dir.is_dir()
    finally:
        engine.close()


def test_undefer_json_prints_the_engine_body(ralphctl):
    engine = StubEngineApi()
    try:
        _write_run_with_api(ralphctl.registry, "run-uj", engine, state="running")
        res = ralphctl("--json", "undefer", "run-uj", "002")
        assert res.returncode == 0, res.stderr
        assert json.loads(res.stdout) == {"undeferring": True, "taskId": "002"}
    finally:
        engine.close()


def test_undefer_exits_5_with_the_servers_message_on_a_409(ralphctl):
    engine = StubEngineApi(undefer_status=409, undefer_body={
        "title": "task not deferred", "detail": "task '002' is not deferred"})
    try:
        _write_run_with_api(ralphctl.registry, "run-u409", engine, state="running")
        res = ralphctl("undefer", "run-u409", "002")
        assert res.returncode == 5, (res.returncode, res.stderr)
        assert "task '002' is not deferred" in res.stderr, res.stderr
    finally:
        engine.close()


def test_undefer_exits_4_with_a_message_when_the_api_is_unreachable(ralphctl):
    engine = StubEngineApi()
    _write_run_with_api(ralphctl.registry, "run-u4", engine, state="running")
    engine.close()  # the recorded apiUrl now refuses connections
    res = ralphctl("undefer", "run-u4", "002")
    assert res.returncode == 4, (res.returncode, res.stderr)
    assert "API unreachable" in res.stderr, res.stderr


def test_undefer_exits_3_for_an_unknown_run(ralphctl):
    res = ralphctl("undefer", "no-such-run", "002")
    assert res.returncode == 3, (res.returncode, res.stderr)


# ------------------------------------------------------------- the hub route

def test_hub_undefer_proxies_to_the_run_engine(tmp_path, ui):
    engine = StubEngineApi(tasks=DEFERRED_TASKS)
    try:
        _write_run_with_api(tmp_path / "registry", "run-h", engine, state="running")
        server = ui(tmp_path / "registry")
        code, body = server.post("/api/runs/run-h/tasks/002/undefer", {})
        assert code == 200, (code, body)
        assert body == {"undeferring": True, "taskId": "002"}
        assert ("POST", "/tasks/002/undefer") in [(m, p) for m, p, _ in engine.requests]
    finally:
        engine.close()


def test_hub_undefer_passes_the_engines_409_message_through(tmp_path, ui):
    engine = StubEngineApi(undefer_status=409, undefer_body={
        "title": "task not deferred", "detail": "task '002' is not deferred"})
    try:
        _write_run_with_api(tmp_path / "registry", "run-h409", engine, state="running")
        server = ui(tmp_path / "registry")
        code, body = server.post("/api/runs/run-h409/tasks/002/undefer", {})
        assert code == 409, (code, body)
        assert "task '002' is not deferred" in json.dumps(body)
    finally:
        engine.close()


def test_hub_undefer_answers_503_for_a_run_whose_api_is_unreachable(tmp_path, ui):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "gone", state="running")  # no host.json at all
    engine = StubEngineApi()
    _write_run_with_api(registry, "dead-port", engine, state="running")
    engine.close()
    server = ui(registry)
    for run_id in ("gone", "dead-port"):
        code, body = server.post(f"/api/runs/{run_id}/tasks/002/undefer", {})
        assert code == 503, (run_id, code, body)
        assert "unreachable" in body["error"], body


def test_hub_undefer_unknown_run_404(tmp_path, ui):
    server = ui(tmp_path / "registry")
    code, body = server.post("/api/runs/nope/tasks/002/undefer", {})
    assert code == 404, (code, body)


def test_hub_undefer_writes_nothing_host_side(tmp_path, ui):
    engine = StubEngineApi(tasks=DEFERRED_TASKS)
    try:
        registry = tmp_path / "registry"
        _write_run_with_api(registry, "run-hw", engine, state="running")
        server = ui(registry)
        before = _snapshot(registry)
        server.post("/api/runs/run-hw/tasks/002/undefer", {})
        assert _snapshot(registry) == before
    finally:
        engine.close()


# --------------------------------------------------------------- app.js text

def _block(name: str) -> str:
    return APP_JS.split(f"function {name}(")[1].split("\nfunction ")[0]


def test_app_js_undefer_button_posts_through_the_hub_proxy_and_shows_the_refusal():
    block = _block("undeferButton")
    assert '"Undefer now"' in block, block
    assert "/tasks/" in block and "/undefer" in block and 'method: "POST"' in block, block
    # the server's own 409/503 message, never a generic one
    assert "innermostReason(j, resp.status)" in block, block


def test_app_js_task_row_button_only_for_a_deferred_task_while_live():
    block = _block("renderTasks")
    assert "t.deferredUntil && undefer && undefer.live" in block, block
    assert "undeferButton(undefer.runId, t.id)" in block, block
    # the live run page passes the payload's own `live` flag in; the
    # approach archive's renderTasks call passes nothing, so it has no button
    assert "lastLive = body.live === true" in APP_JS
    assert "{ runId, live: lastLive }" in APP_JS


def test_app_js_current_iteration_deferred_entry_button_only_while_live():
    block = _block("renderCurrentIteration")
    assert "live === true && runId" in block, block
    assert "undeferButton(runId, d.taskId)" in block, block
    assert "lastCurrentIteration, runId, lastLive)" in APP_JS
