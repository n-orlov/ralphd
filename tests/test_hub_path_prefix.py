"""The hub behind a path-mounting reverse proxy (`https://host/ralph/`).

A proxy that mounts the hub under a prefix strips that prefix before
proxying and forwards no header naming it -- `tailscale serve
--set-path=/ralph` is the case this was written for: the backend sees `/`
for a request to `/ralph/`, and `X-Forwarded-{For,Host,Proto}` say nothing
about the prefix. So the server cannot rewrite anything, and the two halves
that make it work are both client-side:

1. `index.html` references its assets relatively (`style.css`, `app.js`),
   so a document at `/ralph/` loads `/ralph/style.css` and `/ralph/app.js`;
2. `app.js` recovers the prefix from `document.currentScript.src`
   (`HUB_MOUNT`) and prepends it to every request, so `/api/runs` goes out
   as `/ralph/api/runs` -- and, at the root, as `/api/runs` exactly as
   before.

`_StripPrefixProxy` below is a real prefix-stripping proxy, so the browser
test drives the same shape of deployment rather than a simulation of one.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.conftest import free_port

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import Pw  # noqa: E402  (its own pytestmark stays there)
from test_cli_ui import UiServer, _write_dead_run  # noqa: E402

APP_JS = (Path(__file__).resolve().parents[1]
          / "src" / "ralphd" / "cli" / "web" / "app.js").read_text()
INDEX_HTML = (Path(__file__).resolve().parents[1]
              / "src" / "ralphd" / "cli" / "web" / "index.html").read_text()

MOUNT = "/ralph"


class _StripPrefixProxy:
    """Mounts `http://127.0.0.1:<upstream>` under `MOUNT`, stripping it.

    Deliberately as dumb as `tailscale serve --set-path` is: the prefix is
    removed and nothing is added in its place, so anything that depends on
    knowing the prefix has to work it out on the client.
    """

    def __init__(self, upstream: str, mount: str = MOUNT):
        self.upstream = upstream.rstrip("/")
        self.mount = mount
        self.port = free_port()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def _forward(self, method: str) -> None:
                if not self.path.startswith(proxy.mount):
                    self.send_error(404, "not mounted here")
                    return
                rest = self.path[len(proxy.mount):] or "/"
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length) if length else None
                req = urllib.request.Request(
                    proxy.upstream + rest, method=method, data=data)
                for h in ("Content-Type",):
                    if self.headers.get(h):
                        req.add_header(h, self.headers[h])
                # The three headers tailscale does forward, and no more.
                req.add_header("X-Forwarded-For", "127.0.0.1")
                req.add_header("X-Forwarded-Host", self.headers.get("Host", ""))
                req.add_header("X-Forwarded-Proto", "https")
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        status, headers, body = (
                            resp.status, resp.headers, resp.read())
                except urllib.error.HTTPError as e:
                    status, headers, body = e.code, e.headers, e.read()
                self.send_response(status)
                for k, v in headers.items():
                    if k.lower() in ("transfer-encoding", "connection",
                                     "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._forward("GET")

            def do_POST(self):
                self._forward("POST")

            def do_DELETE(self):
                self._forward("DELETE")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path: str) -> tuple[int, str, bytes]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()


@pytest.fixture
def proxied(tmp_path):
    """A hub with one run in it, reached only through the prefix proxy."""
    registry = tmp_path / "registry"
    (registry / "runs").mkdir(parents=True)
    _write_dead_run(registry, "run-behind-proxy")
    server = UiServer(registry)
    try:
        server.wait_ready()
    except Exception:
        server.stop()
        raise
    proxy = _StripPrefixProxy(server.base)
    try:
        yield proxy
    finally:
        proxy.stop()
        server.stop()


# -- the two client-side halves, read off the bundle ----------------------

def test_every_request_goes_through_the_mount_helper():
    """No `fetch()` may take a raw path: at the root `hubURL()` is the
    identity, so a bare `fetch("/api/...")` looks correct in every test that
    serves the hub at `/` and 404s only behind a mount."""
    calls = re.findall(r"fetch\(([^,]*)", APP_JS)
    assert calls, "no fetch() calls found -- has app.js moved?"
    for call in calls:
        assert call.startswith("hubURL("), (
            f"fetch() argument does not go through hubURL(): {call}")
    # The `<img src>` route is not a fetch() but is just as absolute.
    raw = re.search(r"function artifactRawPath\(.*?\n}", APP_JS, re.S)
    assert raw and "hubURL(" in raw.group(0), raw and raw.group(0)


def test_index_html_references_its_assets_relatively():
    """`href="/style.css"` would resolve above the mount and 404."""
    for asset in ("style.css", "app.js"):
        assert f'"{asset}"' in INDEX_HTML, INDEX_HTML
        assert f'"/{asset}"' not in INDEX_HTML, INDEX_HTML


# -- the server, behind a real stripping proxy ----------------------------

def test_shell_and_assets_load_under_the_mount(proxied):
    code, ctype, body = proxied.get(MOUNT + "/")
    assert code == 200 and "text/html" in ctype, (code, ctype)
    assert b"ralphd hub" in body

    for asset, expect in (("style.css", "css"), ("app.js", "javascript")):
        code, ctype, body = proxied.get(f"{MOUNT}/{asset}")
        assert code == 200 and expect in ctype, (asset, code, ctype)
        assert body, asset


def test_api_answers_under_the_mount(proxied):
    """The URL `app.js` builds once `HUB_MOUNT` is `/ralph`."""
    code, ctype, body = proxied.get(MOUNT + "/api/runs")
    assert code == 200 and "application/json" in ctype, (code, ctype)
    runs = json.loads(body)["runs"]
    assert [r["runId"] for r in runs] == ["run-behind-proxy"]


def test_client_side_routing_still_falls_back_to_the_shell(proxied):
    """A refresh on a deep path behind the mount must reach `index.html`,
    the same fallback the hub gives at the root."""
    code, ctype, body = proxied.get(MOUNT + "/run/run-behind-proxy")
    assert code == 200 and "text/html" in ctype, (code, ctype)
    assert b"ralphd hub" in body


# -- and the whole thing in a real browser --------------------------------

@pytest.mark.browser
@pytest.mark.skipif(shutil.which("playwright-cli") is None,
                    reason="playwright-cli not on PATH")
def test_browser_renders_the_run_list_under_the_mount(proxied):
    """The end-to-end claim: open `http://.../ralph/` in Chromium and the
    run list is populated, which can only happen if the bundle loaded from
    the mount and its `/api/runs` request was addressed through it."""
    pw = Pw(f"hub-prefix-{time.time_ns()}")
    try:
        pw.open(proxied.base + MOUNT + "/")
        deadline = time.time() + 20
        rows = "0"
        while time.time() < deadline:
            rows = pw.eval_js("document.querySelectorAll('tbody tr').length")
            if rows not in ("0", ""):
                break
            time.sleep(0.3)
        assert int(rows) >= 1, f"run list never rendered behind {MOUNT}"
        # `--raw` still quotes a string result, hence the strip.
        assert pw.eval_js("window.__hubMount()").strip('"') == MOUNT
        assert "run-behind-proxy" in pw.eval_js("document.body.textContent")
    finally:
        pw.close()
