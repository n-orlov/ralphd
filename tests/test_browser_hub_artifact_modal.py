"""#294 (PRD E.7): browser-tier proof of the artifacts "Browse artifacts" modal.

The run page's artifacts section is a count plus a "Browse artifacts" button.
The button opens a modal on the artifacts root; the operator navigates into a
directory, searches by name, previews a text file as text and sees a download
link on it. The test FAILS (never skips, once `playwright-cli` is available)
when the button is absent: it asserts the button exists before clicking it.

Marked `@pytest.mark.browser` for the same reason as `test_browser_hub.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import (  # noqa: F401
    ARTIFACTS_DIR,
    _skip_reason,
    _wait_for,
    pw,
)
from test_cli_ui import UiServer, _write_dead_run

SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


def test_browse_artifacts_modal_navigates_searches_previews_and_links_download(
        tmp_path, pw):
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-modal", state="succeeded",
                              verdict="verified")
    artifacts = run_dir / "artifacts"
    (artifacts / "reports").mkdir(parents=True)
    (artifacts / "reports" / "alpha.md").write_text("# Alpha report\n")
    (artifacts / "reports" / "beta.txt").write_text("beta body line\n")
    (artifacts / "top.txt").write_text("top body\n")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-modal")

        # -- the button must exist: its absence fails the test ----------------
        _wait_for(pw, "String(document.querySelectorAll('#browse-artifacts').length)",
                  "1")
        assert pw.eval_js(
            "document.querySelector('#browse-artifacts').textContent"
        ).strip('"') == "Browse artifacts"
        assert pw.eval_js("String(document.querySelectorAll('dialog[open]').length)"
                          ).strip('"') == "0"

        # -- open the modal on the root ---------------------------------------
        pw.click("#browse-artifacts")
        _wait_for(pw, "document.querySelector('.artifact-browser') "
                      "? document.querySelector('.artifact-rows').textContent : ''",
                  "reports/")
        assert pw.eval_js("String(document.querySelectorAll('dialog[open]').length)"
                          ).strip('"') == "1"
        root_rows = pw.eval_js("document.querySelector('.artifact-rows').textContent")
        assert "top.txt" in root_rows, root_rows
        pw.screenshot(SCREENSHOTS_DIR / "artifact-modal-root.png")

        # -- navigate into a directory ----------------------------------------
        pw.click('button[data-dir="reports"]')
        _wait_for(pw, "document.querySelector('.artifact-rows').textContent",
                  "alpha.md")
        rows = pw.eval_js("document.querySelector('.artifact-rows').textContent")
        assert "beta.txt" in rows and "top.txt" not in rows, rows
        crumbs = pw.eval_js("document.querySelector('.artifact-crumbs').textContent")
        assert "reports" in crumbs, crumbs

        # -- search by name -----------------------------------------------------
        pw.fill(".artifact-search", "beta")
        pw.click(".artifact-search-form button")
        _wait_for(pw, "String(document.querySelectorAll('.artifact-row').length)", "1")
        found = pw.eval_js("document.querySelector('.artifact-rows').textContent")
        assert "beta.txt" in found and "alpha.md" not in found, found

        # -- preview a text file as text, with a download link -----------------
        pw.click('button[data-file="reports/beta.txt"]')
        body = _wait_for(pw, "document.querySelector('.artifact-preview').textContent",
                         "beta body line")
        assert "beta body line" in body, body
        href = pw.eval_js(
            "document.querySelector('.artifact-preview a.artifact-download')"
            ".getAttribute('href')")
        assert "/api/runs/run-modal/artifacts-download/" in href, href
        assert "beta.txt" in href, href
        pw.screenshot(SCREENSHOTS_DIR / "artifact-modal-preview.png")
    finally:
        server.stop()
