"""Black-box tests: creds runtime CRUD API (PRD req 10).

GET /config/creds (list, no values), GET/PUT/DELETE /config/creds/{name}.
PUT bodies are `text/plain` env-file content; mutations re-run
`place_creds()` immediately so `$HOME/.creds/{name}.env` reflects the change
without waiting for a container restart. Secret *values* must never appear
in the run dir, events.jsonl, or captured engine stdout -- only names, sizes
and mtimes leave the engine via the list route.
"""

from __future__ import annotations

import json
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_e2e import EngineProc

SECRET_VALUE = "sekret-token-do-not-leak-creds-api-abc123"


@pytest.fixture
def creds_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "creds-api-e2e", "iterations": 3,
                    "max_approaches": 1, "on_complete": "idle"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _write_mounted_cred(config_dir: Path, name: str, content: str) -> None:
    d = config_dir / "creds"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.env").write_text(content)


def _put(port: int, path: str, body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="PUT", data=body,
        headers={"Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get(port: int, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _delete(port: int, path: str) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_creds_crud_no_leak(creds_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    _write_mounted_cred(tmp_path / "config", "jenkins", "JENKINS_URL=https://example.com\n")
    e = creds_engine(stub_env={"HOME": str(fake_home)})
    e.wait_api()

    # -- mounted cred visible from the start, list has no values -----------
    status, body = json_get(e.port, "/config/creds")
    assert status == 200
    names = [c["name"] for c in body]
    assert names == ["jenkins"]
    assert set(body[0]) == {"name", "size", "mtime"}
    dumped = json.dumps(body)
    assert "example.com" not in dumped  # value never in the list route

    placed = fake_home / ".creds" / "jenkins.env"
    assert placed.is_file()
    assert stat.S_IMODE(placed.stat().st_mode) == 0o600

    # -- PUT a new secret env cred: placement re-run immediately -----------
    body_text = f"GITHUB_TOKEN={SECRET_VALUE}\n"
    st, _ = _put(e.port, "/config/creds/github", body_text.encode())
    assert st == 204

    github_path = fake_home / ".creds" / "github.env"
    assert github_path.is_file()
    assert stat.S_IMODE(github_path.stat().st_mode) == 0o600
    assert github_path.read_text() == body_text

    status, body = json_get(e.port, "/config/creds")
    names = sorted(c["name"] for c in body)
    assert names == ["github", "jenkins"]

    # -- GET /config/creds/{name} returns the contents (read-back allowed) -
    st, content = _get(e.port, "/config/creds/github")
    assert st == 200
    assert content.decode() == body_text

    # -- DELETE removes the file from ~/.creds, then 404 on repeat ---------
    assert _delete(e.port, "/config/creds/github") == 204
    assert not github_path.exists()
    assert _delete(e.port, "/config/creds/github") == 404
    st, _ = _get(e.port, "/config/creds/github")
    assert st == 404
    status, body = json_get(e.port, "/config/creds")
    assert [c["name"] for c in body] == ["jenkins"]

    # -- deleting a mounted cred tombstones it (doesn't resurrect) ----------
    assert _delete(e.port, "/config/creds/jenkins") == 204
    assert not (fake_home / ".creds" / "jenkins.env").exists()
    status, body = json_get(e.port, "/config/creds")
    assert body == []

    # -- no value leakage anywhere under the run dir / events / stdout -----
    e.stop()
    time.sleep(0.2)
    for p in e.run_dir.rglob("*"):
        if p.is_file():
            assert SECRET_VALUE not in p.read_text(errors="ignore"), p
    stdout = e.proc.stdout.read() if e.proc.stdout else ""
    assert SECRET_VALUE not in stdout


def test_put_empty_body_rejected(creds_engine, tmp_path):
    e = creds_engine(stub_env={"HOME": str(tmp_path / "fakehome2")})
    e.wait_api()
    st, _ = _put(e.port, "/config/creds/empty", b"")
    assert st == 422


def json_get(port: int, path: str) -> tuple[int, list]:
    st, raw = _get(port, path)
    return st, (json.loads(raw) if raw else None)
