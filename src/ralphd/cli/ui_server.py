"""`ralphctl ui` — local hub HTTP server (PRD reqs 21-22).

Deliberately stdlib-only (`http.server` + `urllib`), same spirit as
`main.py`'s doc string: this is a CLI-side feature and must not force
`fastapi`/`uvicorn` (already dependencies of the engine side, but not
needed here) onto the `ralphctl ui` path.

Serves two things:
  - JSON endpoints under `/api/...` reading `<registry>/runs/*` and
    proxying a run's *live* container API when it is reachable, degrading
    gracefully (never raising into a 500, never hanging past a short
    timeout) when it is not.
  - The static hub bundle (plain HTML/JS/CSS, no build step) from the
    `web/` directory next to this file (task 034: run list, run detail
    with task table/iteration timeline/live log tail/steering
    form/usage-cost, packaged in the wheel). Non-`/api` paths that don't
    match a real file fall back to `index.html` (SPA-style client-side
    routing).

See docs/cli.md's "ralphctl ui" section for the exact endpoint shapes.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATIC_DIR = Path(__file__).parent / "web"

DEFAULT_LOG_TAIL = 200


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def host_meta(reg: Path, run_id: str) -> dict:
    return _read_json(reg / "runs" / run_id / "host.json", {}) or {}


def run_list(reg: Path) -> list[dict]:
    """Run list view (PRD req 21): state/verdict/phase/iterations per run,
    read from `<registry>/runs/*/status.json` only -- no live proxy calls
    (would make listing N runs take N round trips)."""
    rows = []
    runs_dir = reg / "runs"
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            if not d.is_dir():
                continue
            status = _read_json(d / "status.json", {}) or {}
            rows.append({
                "runId": d.name,
                "state": status.get("state"),
                "verdict": status.get("verdict"),
                "phase": status.get("phase"),
                "approach": status.get("approach"),
                "iterationsUsed": status.get("iterationsUsed"),
                "iterationsBudget": status.get("iterationsBudget"),
                "startedAt": status.get("startedAt"),
            })
    return rows


def _proxy_json(reg: Path, run_id: str, method: str, path: str,
                 body: bytes | None = None, timeout: float = 5.0):
    """Forward a request to the run's live container API, expecting a JSON
    response. Returns (ok, status_code, obj). Never raises -- a dead/gone
    run degrades to (False, 0, None) so callers can fall back to on-disk
    state instead of the hub crashing."""
    meta = host_meta(reg, run_id)
    api_url = meta.get("apiUrl")
    if not api_url:
        return False, 0, None
    req = urllib.request.Request(api_url + path, method=method, data=body)
    token_file = reg / "runs" / run_id / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return True, resp.status, (json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read())
        except (json.JSONDecodeError, ValueError):
            detail = {"detail": str(e)}
        return False, e.code, detail
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, 0, None


def _proxy_text(reg: Path, run_id: str, path: str, timeout: float = 5.0):
    """Same as `_proxy_json` but for the (text/plain NDJSON) `/logs`
    endpoint. Returns (ok, text)."""
    meta = host_meta(reg, run_id)
    api_url = meta.get("apiUrl")
    if not api_url:
        return False, ""
    req = urllib.request.Request(api_url + path, method="GET")
    token_file = reg / "runs" / run_id / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode(errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False, ""


def run_detail(reg: Path, run_id: str) -> dict | None:
    """Run detail view (PRD req 21): task table + iteration timeline data,
    live where possible, falling back to the on-disk snapshot for a dead
    run. Returns None if the run doesn't exist at all (caller -> 404)."""
    run_dir = reg / "runs" / run_id
    if not run_dir.is_dir():
        return None
    status = _read_json(run_dir / "status.json", {}) or {}
    ok_s, _, live_status = _proxy_json(reg, run_id, "GET", "/status")
    if ok_s and live_status:
        status = live_status

    tasks = _read_json(run_dir / "tasks.json", {"tasks": []}) or {"tasks": []}
    ok_t, _, live_tasks = _proxy_json(reg, run_id, "GET", "/tasks")
    if ok_t and live_tasks is not None:
        tasks = live_tasks

    iterations = []
    itroot = run_dir / "iterations"
    if itroot.is_dir():
        for d in sorted(itroot.iterdir()):
            meta = _read_json(d / "meta.json")
            if meta is not None:
                iterations.append(meta)

    return {
        "runId": run_id,
        "live": ok_s,
        "status": status,
        "tasks": tasks,
        "iterations": iterations,
    }


class Handler(BaseHTTPRequestHandler):
    """Bound to a registry path via `make_handler_class()`."""

    registry: Path = Path.home() / ".ralphd"
    server_version = "ralphctl-ui/1"

    def log_message(self, fmt, *args):  # quiet by default -- no per-request spam
        pass

    # -- response helpers -------------------------------------------------

    def _send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, code=200, content_type="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        qs = parse_qs(parts.query)
        reg = self.registry
        try:
            if segs == ["api", "runs"]:
                self._send_json({"runs": run_list(reg)})
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "logs":
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                tail = qs.get("tail", [str(DEFAULT_LOG_TAIL)])[0]
                ok, text = _proxy_text(reg, run_id, f"/logs?tail={tail}")
                self._send_json({"live": ok, "text": text})
                return
            if len(segs) == 3 and segs[:2] == ["api", "runs"]:
                run_id = segs[2]
                detail = run_detail(reg, run_id)
                if detail is None:
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                self._send_json(detail)
                return
            if segs and segs[0] == "api":
                self._send_json({"error": "no such endpoint"}, 404)
                return
            self._serve_static(parts.path)
        except Exception as e:  # defensive: never let one bad request kill the hub
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        reg = self.registry
        try:
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "steer":
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                raw = self._read_body()
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    self._send_json({"error": "invalid JSON body"}, 400)
                    return
                ok, code, resp = _proxy_json(reg, run_id, "POST", "/steering",
                                              body=json.dumps(body).encode())
                if not ok:
                    self._send_json(
                        {"error": "run's API unreachable or rejected the request",
                         "detail": resp}, code or 503)
                    return
                self._send_json(resp, code)
                return
            self._send_json({"error": "no such endpoint"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # -- static bundle (populated by task 034) ------------------------------

    def _serve_static(self, url_path: str) -> None:
        if not STATIC_DIR.is_dir():
            self._send_bytes(
                b"ralphctl ui: static hub bundle not installed in this build\n",
                404, "text/plain")
            return
        rel = url_path.lstrip("/") or "index.html"
        candidate = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            self._send_bytes(b"forbidden\n", 403, "text/plain")
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            candidate = STATIC_DIR / "index.html"
            if not candidate.is_file():
                self._send_bytes(b"not found\n", 404, "text/plain")
                return
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self._send_bytes(candidate.read_bytes(), 200, content_type)


def make_handler_class(reg: Path) -> type[Handler]:
    return type("BoundHandler", (Handler,), {"registry": reg})


def make_server(reg: Path, bind: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((bind, port), make_handler_class(reg))
