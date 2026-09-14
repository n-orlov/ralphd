"""Task 035 (PRD requirement G.3): a hub run page shows the engine version
the run is executing (F.4's `status.json` field, `engineVersion`), and --
where the workspace's own version is knowable -- both versions on the same
row, so the drift alert E.3 (task 017) already compiles into
`workspace_version_mismatch` is readable at a glance instead of only ever
reaching the operator through a page.

Both new nodes drive a real Chromium via the SAME `playwright-cli` harness
`tests/test_browser_hub.py` uses, against a real `ralphctl ui` server over a
dead run's on-disk `status.json` -- never a mock of `renderSummary`. Follows
`tests/test_llm_header_visibility.py`'s `_summary_rows` style for reading
the summary card's own rows, top to bottom, as text.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import SCREENSHOTS_DIR, _wait_for, pw  # noqa: F401
from test_cli_ui import UiServer, _write_dead_run

__all__ = ["pw"]

PLAYWRIGHT_CLI = shutil.which("playwright-cli")


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
    registry = tmp_path / "registry"
    run_id = "run-engine-and-workspace-version"
    _write_dead_run(registry, run_id, state="succeeded",
                     engineVersion="0.21.0",
                     workspaceVersion="0.20.9-not-the-engine")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run_id}")
        _wait_for(pw, "document.body.innerText", "engine: ")
        rows = _summary_rows(pw)
        engine_row = next(r for r in rows if r.startswith("engine: "))
        assert engine_row == \
            "engine: 0.21.0  (workspace: 0.20.9-not-the-engine)", engine_row
        pw.screenshot(SCREENSHOTS_DIR / "27-run-page-engine-and-workspace-version.png")
    finally:
        server.stop()
