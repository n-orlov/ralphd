"""Task 056 (#1): the hub's PRD dialog -- server side + rendering discipline.

The browser half (the dialog actually opening and showing the text for a live
AND a dead run) lives in tests/test_browser_hub.py; this module covers the
endpoint that feeds it and the greppable invariants:

  * `GET /api/runs/<id>/prd` proxies the run's live `GET /prd` and falls back
    to the ON-DISK run dir when that API does not answer -- the same
    live-first/on-disk shape tasks 038/039 gave the log tail, so an operator
    investigating a dead run can still read what it was asked to do;
  * which file *is* the PRD (`composite-prd.md` when present, else `prd.md`)
    is decided in exactly ONE place (`engine.state.prd_path`), used by the
    engine route, the hub fallback and the prompt builder;
  * `app.js` renders the PRD as text nodes only -- never `innerHTML`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ralphd.cli.ui_server import NO_PRD, prd_text
from ralphd.engine.state import prd_path

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import (
    StubEngineApi,
    UiServer,
    _write_dead_run,
    _write_run_with_api,
)

LIVE_PRD = "# Live PRD\n\nBuild the <thing> & ship it.\n"
DISK_PRD = "# On-disk PRD\n\nRead me from the run dir.\n"


@pytest.fixture
def ui():
    """Same shape as tests/test_cli_ui.py's `ui` fixture: a real `ralphctl
    ui` server over a fixture registry, stopped at teardown."""
    servers = []

    def make(registry: Path) -> UiServer:
        s = UiServer(registry)
        s.wait_ready()
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()


# -- the endpoint -------------------------------------------------------


def test_prd_endpoint_proxies_a_live_run(tmp_path, ui):
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"state": "running"}, prd=LIVE_PRD)
    run_dir = _write_run_with_api(registry, "run-live", engine, state="running")
    # a *stale* on-disk copy proves the live answer wins
    (run_dir / "prd.md").write_text("stale on-disk copy\n")

    server = ui(registry)
    try:
        code, body = server.get("/api/runs/run-live/prd")
        assert code == 200
        assert body["live"] is True
        assert body["text"] == LIVE_PRD
        assert ("GET", "/prd", None) in engine.requests
    finally:
        engine.close()


def test_prd_endpoint_falls_back_to_on_disk_for_a_dead_run(tmp_path, ui):
    """The whole point of #1's on-disk fallback: a finished or killed run has
    no API to proxy, but its PRD is sitting in the run dir."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-gone", state="failed")
    (run_dir / "prd.md").write_text(DISK_PRD)

    server = ui(registry)
    code, body = server.get("/api/runs/run-gone/prd")
    assert code == 200
    assert body["live"] is False          # so the UI can label it a snapshot
    assert body["text"] == DISK_PRD


def test_prd_endpoint_prefers_the_composite_prd(tmp_path, ui):
    """`composite-prd.md` is what the agent actually works from, so it is
    what "the run's PRD" means -- on the on-disk path too."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-composite", state="succeeded")
    (run_dir / "prd.md").write_text(DISK_PRD)
    (run_dir / "composite-prd.md").write_text("composite: prd + approach note\n")

    server = ui(registry)
    code, body = server.get("/api/runs/run-composite/prd")
    assert code == 200
    assert body["text"] == "composite: prd + approach note\n"


def test_prd_endpoint_says_no_prd_instead_of_empty_text(tmp_path, ui):
    """Same discipline as `log_merge.NO_TRANSCRIPT` (task 041): an empty
    answer explains itself, and the wording lives server-side."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-nothing", state="failed")

    server = ui(registry)
    code, body = server.get("/api/runs/run-nothing/prd")
    assert code == 200
    assert body["live"] is False
    assert body["text"] == NO_PRD == "(no PRD recorded)"


def test_prd_endpoint_404_for_unknown_run(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, body = server.get("/api/runs/does-not-exist/prd")
    assert code == 404
    assert "not found" in body["error"]


def test_prd_text_never_writes_into_the_run_dir(tmp_path):
    """The hub is a read-only viewer: reading a PRD must not create a
    `RunDir` (whose __post_init__ makes steering/iterations/... dirs)."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-ro", state="failed")
    (run_dir / "prd.md").write_text(DISK_PRD)
    before = sorted(p.name for p in run_dir.iterdir())

    live, text = prd_text(registry, "run-ro")
    assert (live, text) == (False, DISK_PRD)
    assert sorted(p.name for p in run_dir.iterdir()) == before


# -- one implementation of "which file is the PRD" ----------------------


def test_prd_path_is_the_single_implementation(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    assert prd_path(run_root) is None
    (run_root / "prd.md").write_text("a")
    assert prd_path(run_root) == run_root / "prd.md"
    (run_root / "composite-prd.md").write_text("b")
    assert prd_path(run_root) == run_root / "composite-prd.md"
    # ?original=True forces the operator's own file
    assert prd_path(run_root, original=True) == run_root / "prd.md"

    src = Path(__file__).resolve().parents[1] / "src" / "ralphd"
    hits = sorted(
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if 'composite-prd.md"' in p.read_text()
    )
    assert hits == ["engine/state.py"], \
        f"the composite-PRD filename must live only in state.py, found in {hits}"


def test_engine_route_and_hub_fallback_agree_for_the_same_run_dir(tmp_path, ui):
    """The engine's `GET /prd` and the hub's on-disk fallback must serve the
    same bytes for the same run dir -- asserted through the real ASGI app."""
    from fastapi.testclient import TestClient

    from ralphd.engine.api import create_app
    from ralphd.engine.config import JobConfig
    from ralphd.engine.state import RunDir

    root = tmp_path / "runroot"
    root.mkdir()
    (root / "prd.md").write_text(DISK_PRD)
    (root / "composite-prd.md").write_text("composite wins\n")
    (root / "status.json").write_text(json.dumps({"runId": "r", "state": "running"}))
    (root / "tasks.json").write_text(json.dumps({"tasks": []}))

    app = create_app(JobConfig(run_id="r"), RunDir(root=root), None)
    with TestClient(app) as client:
        live_text = client.get("/prd").text
        original_text = client.get("/prd?original=true").text

    registry = tmp_path / "registry"
    (registry / "runs").mkdir(parents=True)
    (registry / "runs" / "r").symlink_to(root)
    server = ui(registry)
    code, body = server.get("/api/runs/r/prd")

    assert code == 200
    assert body["text"] == live_text == "composite wins\n"
    assert original_text == DISK_PRD


# -- rendering discipline (task 014 style grep) -------------------------


def test_app_js_renders_dialog_text_with_text_nodes_only():
    """Rendering-discipline grep (same style as the cost/textContent checks):
    the PRD dialog is built by `openTextDialog`, which must pass the text
    through `h()`'s text nodes -- no `innerHTML`, no `html:` attribute (the
    `h()` escape hatch), inside the dialog code."""
    import ralphd.cli.ui_server as ui_mod

    app_js = (Path(ui_mod.__file__).parent / "web" / "app.js").read_text()
    assert "function openTextDialog(" in app_js
    assert "function openPrdDialog(" in app_js
    block = app_js.split("function openTextDialog(")[1].split("\n// ----")[0]
    for forbidden in ("innerHTML", "html:", 'insertAdjacentHTML'):
        assert forbidden not in block, \
            f"PRD dialog rendering must not use {forbidden}: {block}"
    # and it must not spell the "no PRD" wording itself -- that lives in
    # ui_server.NO_PRD, like log_merge.NO_TRANSCRIPT
    assert "no PRD recorded" not in app_js
