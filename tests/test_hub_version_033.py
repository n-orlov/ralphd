"""Task 033 (G.1): the hub header shows the hub's OWN package version
(`ralphd.__version__`), served from the hub rather than hard-coded into
the static bundle, and `server_version` -- the wire-protocol tag -- stays
exactly `ralphctl-ui/1`. The two are different numbers answering different
questions (which package is running the hub vs. which wire shape it
speaks), and conflating them was the defect this task fixes.

Follows the source-inspection style `tests/test_hub_features.py`'s folded
`test_hub_task_counts.py`/`test_hub_task_dialog.py` nodes use for anything
rendered client-side: a real (subprocess) hub server proves the SERVED
value, and a string check on `app.js`/`index.html` proves the header
actually renders whatever that route answers -- no browser needed, unlike
the heavier `browser`-tier nodes in `tests/test_browser_hub.py`.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import ralphd
from ralphd.cli import ui_server

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import UiServer, ui  # noqa: E402

__all__ = ["UiServer", "ui"]

APP_JS = (Path(ui_server.__file__).parent / "web" / "app.js").read_text()
INDEX_HTML = (Path(ui_server.__file__).parent / "web" / "index.html").read_text()


def test_the_wire_protocol_tag_is_unchanged():
    """`server_version` names the wire shape, not the package -- G.1
    requires it stay exactly as it was before this task."""
    assert ui_server.Handler.server_version == "ralphctl-ui/1"


def test_get_api_version_serves_the_running_packages_own_version(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, body = server.get("/api/version")
    assert code == 200
    # Served from the package, never a literal duplicated into the route.
    assert body == {"version": ralphd.__version__}


def test_the_version_route_answers_something_that_is_not_the_protocol_tag(
        tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    _, body = server.get("/api/version")
    assert body["version"] != ui_server.Handler.server_version


def test_the_static_page_carries_a_header_slot_for_the_served_version():
    assert '<span id="hub-version" class="muted"></span>' in INDEX_HTML
    # Sits beside the existing clock span in the same <header>, not
    # replacing anything already there.
    header = INDEX_HTML.split("<header>")[1].split("</header>")[0]
    assert 'id="hub-version"' in header
    assert 'id="clock"' in header


def test_app_js_fetches_the_hub_version_and_renders_it_in_that_slot():
    assert "function loadHubVersion(" in APP_JS
    body = APP_JS.split("async function loadHubVersion(")[1].split("\n}")[0]
    assert 'getJSON("/api/version")' in body
    assert 'getElementById("hub-version")' in body
    assert "body.version" in body
    # Called unconditionally on load, independent of which route the
    # SPA-style router renders (the header sits outside `#app`).
    boot = APP_JS.split('window.addEventListener("DOMContentLoaded", () => {')[1]
    boot = boot.split("});")[0]
    assert "loadHubVersion()" in boot


def test_the_live_page_html_still_serves_the_header_slot_over_http(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    with urllib.request.urlopen(f"{server.base}/", timeout=5) as resp:
        page = resp.read().decode()
    assert '<span id="hub-version" class="muted"></span>' in page
