"""Task 035 (PRD requirement G.3): a hub run page shows the engine version
the run is executing (F.4's `status.json` field, `engineVersion`), and --
whenever the workspace's own version is knowable, whether or not it matches
the engine's -- both versions on the same row, so the pair E.3 (task 017)
only ever pages the operator about on a MISMATCH is readable at a glance on
every run whose workspace names a version at all.

Both new nodes drive a real Chromium via the SAME `playwright-cli` harness
`tests/test_browser_hub.py` uses, against a real `ralphctl ui` server over a
dead run's on-disk `status.json` -- never a mock of `renderSummary`. The
second node's `status.json` is written by a real `LoopSupervisor(...)`
constructor over a real workspace, so the two version fields it renders come
from the production path itself. Follows
`tests/test_llm_header_visibility.py`'s `_summary_rows` style for reading
the summary card's own rows, top to bottom, as text.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from ralphd import __version__ as ENGINE_VERSION

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import SCREENSHOTS_DIR, _wait_for, pw  # noqa: F401
from test_cli_ui import UiServer, _write_dead_run
from tests.conftest import make_supervisor

__all__ = ["pw"]

PLAYWRIGHT_CLI = shutil.which("playwright-cli")


def _write_workspace_version(workspace: Path, version: str) -> None:
    """A workspace whose own `__version__` literal is readable -- the exact
    shape `loop._read_workspace_version` reads, as
    `tests/test_workspace_version_drift.py` writes it."""
    pkg = workspace / "src" / "ralphd"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n')


def _hub_readable(run_dir: Path, run_id: str) -> dict:
    """Add the run-list/run-page display fields a dead run's `status.json`
    carries, WITHOUT touching either version field the engine itself just
    wrote there -- so what the page renders is the production write, never a
    hand-seeded value. Returns the status dict as it now stands on disk."""
    status = json.loads((run_dir / "status.json").read_text())
    status.update({"runId": run_id, "state": "succeeded",
                   "verdict": "verified", "phase": "review", "approach": 1,
                   "iterationsUsed": 3, "iterationsBudget": 25,
                   "startedAt": "2026-01-01T00:00:00Z"})
    (run_dir / "status.json").write_text(json.dumps(status))
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": []}))
    return status


def _summary_rows(session) -> list[str]:
    """The run-detail summary card's own rows, top to bottom, as text --
    the same helper `test_llm_header_visibility.py` already uses."""
    raw = session.eval_js(
        "[...document.querySelectorAll('div.card p > div')]"
        ".map(d => d.textContent).join('\\n')")
    return [line for line in raw.strip('"').split("\\n") if line]


@pytest.mark.browser
@pytest.mark.skipif(PLAYWRIGHT_CLI is None, reason="playwright-cli not on PATH")
def test_run_page_shows_the_engine_version_it_is_executing(tmp_path, pw):
    registry = tmp_path / "registry"
    run_id = "run-engine-version-only"
    _write_dead_run(registry, run_id, state="succeeded",
                     engineVersion="0.21.0")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run_id}")
        _wait_for(pw, "document.body.innerText", "engine: ")
        rows = _summary_rows(pw)
        engine_row = next(r for r in rows if r.startswith("engine: "))
        assert engine_row == "engine: 0.21.0", engine_row
        # No mismatch was ever recorded for this run -- nothing named to
        # compare, so the row names only the one version it does know.
        assert "workspace" not in engine_row, engine_row
        pw.screenshot(SCREENSHOTS_DIR / "26-run-page-engine-version-only.png")
    finally:
        server.stop()


@pytest.mark.browser
@pytest.mark.skipif(PLAYWRIGHT_CLI is None, reason="playwright-cli not on PATH")
def test_run_page_shows_both_versions_when_the_workspace_version_is_knowable(
        tmp_path, pw):
    """The knowable case that MATTERS, and the one this task's first attempt
    got wrong: a workspace whose version literal is perfectly readable and
    happens to EQUAL the engine's. Nothing is seeded here -- a real
    `LoopSupervisor(...)` constructor writes both fields onto a real run
    dir's `status.json` through the production path
    (`_record_engine_version`, F.4 + `_record_workspace_version`, G.3), and
    the page is then asked what it shows."""
    registry = tmp_path / "registry"
    run_id = "run-engine-and-workspace-version"
    run_dir = registry / "runs" / run_id
    ws = tmp_path / "ws"
    # Knowable AND equal: no drift, no `workspace_version_mismatch` event,
    # nothing to alert about -- and still two versions the page knows.
    _write_workspace_version(ws, ENGINE_VERSION)

    make_supervisor(run_dir, workspace=ws, cfg_defaults=False)

    status = _hub_readable(run_dir, run_id)
    assert status["engineVersion"] == ENGINE_VERSION
    assert status["workspaceVersion"] == ENGINE_VERSION, \
        "the production path must persist a knowable workspace version " \
        "even when it matches the engine's"

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run_id}")
        _wait_for(pw, "document.body.innerText", "engine: ")
        rows = _summary_rows(pw)
        engine_row = next(r for r in rows if r.startswith("engine: "))
        assert engine_row == \
            f"engine: {ENGINE_VERSION}  (workspace: {ENGINE_VERSION})", engine_row
        pw.screenshot(
            SCREENSHOTS_DIR / "27-run-page-engine-and-workspace-version.png")
    finally:
        server.stop()

    # The drifting case reads the same way on the page -- same row, the two
    # versions it names simply differ. Seeded, because the drift half is
    # already driven through the production constructor by
    # `tests/test_workspace_version_drift.py`.
    drift_registry = tmp_path / "registry-drift"
    drift_id = "run-engine-and-workspace-version-drift"
    _write_dead_run(drift_registry, drift_id, state="succeeded",
                    engineVersion="0.21.0",
                    workspaceVersion="0.20.9-not-the-engine")
    drift_server = UiServer(drift_registry)
    drift_server.wait_ready()
    try:
        pw.open(f"{drift_server.base}/#/run/{drift_id}")
        _wait_for(pw, "document.body.innerText", "engine: ")
        rows = _summary_rows(pw)
        engine_row = next(r for r in rows if r.startswith("engine: "))
        assert engine_row == \
            "engine: 0.21.0  (workspace: 0.20.9-not-the-engine)", engine_row
    finally:
        drift_server.stop()
