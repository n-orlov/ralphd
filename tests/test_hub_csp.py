"""Task 024 (#54): `Content-Security-Policy` on every hub response.

The hub serves files an agent inside the job container wrote (artifacts,
notes, PRD text, reflection reports) back to a browser at the operator's
own origin. `ui_server.HUB_CSP` is sent on EVERY response -- the static
bundle (`index.html`/`app.js`/`style.css`), every JSON API route and the
raw artifact byte route (task 023) -- via the two response helpers
(`_send_json`/`_send_bytes`), so a new route can never forget it.

No `'unsafe-inline'` anywhere in the policy: the static bundle has no
inline `<script>`, no `onclick="..."` attributes and no inline `style=`
attributes, so `script-src 'self'` and `style-src 'self'` cost nothing.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ralphd.cli import ui_server
from ralphd.cli.ui_server import HUB_CSP

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import UiServer, ui  # noqa: E402

__all__ = ["UiServer", "ui"]

PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00"


def _headers(server, path):
    """Fetch a path, returning (status, headers, body bytes) -- unlike
    `test_cli_ui._get_raw`, this keeps the full header set so a test can
    assert on `Content-Security-Policy` specifically."""
    try:
        with urllib.request.urlopen(f"{server.base}{path}", timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _seed_run_with_artifact(tmp_path: Path, run_id="csp-run") -> Path:
    registry = tmp_path / "registry"
    rdir = registry / "runs" / run_id
    adir = rdir / "artifacts"
    adir.mkdir(parents=True)
    (rdir / "status.json").write_text(
        json.dumps({"runId": run_id, "state": "succeeded"}))
    (adir / "shot.png").write_bytes(PNG_HEADER + b"rest of the file")
    return registry


# ------------------------------------------------------------------ policy shape


def test_the_policy_carries_no_unsafe_inline():
    assert "unsafe-inline" not in HUB_CSP
    assert "unsafe-eval" not in HUB_CSP


def test_the_policy_defaults_and_scripts_and_images_to_self():
    directives = dict(
        part.strip().split(" ", 1)
        for part in HUB_CSP.split(";") if part.strip())
    assert directives["default-src"] == "'self'"
    assert directives["script-src"] == "'self'"
    assert directives["img-src"] == "'self'"


# --------------------------------------------------------------------- HTML page


def test_csp_header_present_on_the_static_html_page(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, headers, body = _headers(server, "/")
    assert code == 200
    assert headers.get("Content-Security-Policy") == HUB_CSP
    assert b"<html" in body.lower()


def test_csp_header_present_on_js_and_css_assets(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    for path in ("/app.js", "/style.css"):
        code, headers, _body = _headers(server, path)
        assert code == 200
        assert headers.get("Content-Security-Policy") == HUB_CSP


# ---------------------------------------------------------------- JSON API route


def test_csp_header_present_on_a_json_api_response(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, headers, body = _headers(server, "/api/runs")
    assert code == 200
    assert headers.get("Content-Security-Policy") == HUB_CSP
    assert json.loads(body) == {"runs": []}


def test_csp_header_present_on_a_json_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, headers, _body = _headers(server, "/api/runs/nope/logs")
    assert code == 404
    assert headers.get("Content-Security-Policy") == HUB_CSP


# ---------------------------------------------------------------- raw artifact route


def test_csp_header_present_on_the_raw_artifact_route(tmp_path, ui):
    registry = _seed_run_with_artifact(tmp_path)
    server = ui(registry)
    code, headers, body = _headers(server, "/api/runs/csp-run/artifacts-raw/shot.png")
    assert code == 200
    assert headers.get("Content-Security-Policy") == HUB_CSP
    assert body.startswith(PNG_HEADER)


def test_csp_header_present_on_a_refused_raw_artifact_request(tmp_path, ui):
    registry = _seed_run_with_artifact(tmp_path)
    server = ui(registry)
    code, headers, _body = _headers(
        server, "/api/runs/csp-run/artifacts-raw/..%2F..%2Fetc%2Fpasswd")
    assert code == 404
    assert headers.get("Content-Security-Policy") == HUB_CSP


# ------------------------------------------------------------ mutation-derived

def test_the_header_is_sent_from_the_two_shared_send_helpers_not_per_route(monkeypatch):
    """Guards against a future route bypassing `_send_json`/`_send_bytes` and
    writing its own headers directly -- if that ever happens this whole
    module's black-box assertions would still catch it (the header would go
    missing on that one route), but this unit-level check pins the actual
    mechanism: both helpers pass `HUB_CSP` to `send_header`, not a per-route
    constant that could drift out of sync."""
    import inspect
    src_json = inspect.getsource(ui_server.Handler._send_json)
    src_bytes = inspect.getsource(ui_server.Handler._send_bytes)
    assert "HUB_CSP" in src_json
    assert "HUB_CSP" in src_bytes
