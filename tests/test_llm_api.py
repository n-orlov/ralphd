"""Black-box tests: runtime LLM env + pi config fragment CRUD (PRD req 10,
`PUT /config/llm`).

`env` fully replaces the env-override set applied to every subsequent `pi`
subprocess (proven via the stub's `.stub-env.json` marker, dumped on every
invocation); `pi` is deep-merged into `~/.pi/agent/models.json` immediately
-- the same file `pi` itself reads for provider/model config -- without
requiring a container restart. Secret values must never land in the run
dir, events.jsonl, or captured engine stdout.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest
from test_e2e import EngineProc

SECRET_VALUE = "sekret-llm-key-do-not-leak-abc123"


@pytest.fixture
def llm_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "llm-api-e2e", "iterations": 20,
                    "max_approaches": 1, "on_complete": "idle"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _put(port: int, path: str, body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="PUT", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_llm_env_reaches_next_pi_invocation_no_leak(llm_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    e = llm_engine(stub_env={"HOME": str(fake_home), "STUB_SLEEP": "1",
                             "STUB_TASKS": "5"})
    e.wait_api()

    # -- no override yet: the stub's env has no trace of our key -----------
    env_marker = e.run_dir / ".stub-env.json"
    deadline = time.time() + 20
    while time.time() < deadline and not env_marker.exists():
        time.sleep(0.2)
    assert env_marker.exists(), "stub never ran a first iteration"
    assert "PROBE_LLM_KEY" not in json.loads(env_marker.read_text())

    # -- PUT replaces the env-override set; effective next iteration -------
    body = json.dumps({"env": {"PROBE_LLM_KEY": SECRET_VALUE,
                               "PROBE_LLM_ENDPOINT": "https://gw.example.com"}}).encode()
    st, _ = _put(e.port, "/config/llm", body)
    assert st == 204

    deadline = time.time() + 20
    seen = {}
    while time.time() < deadline:
        seen = json.loads(env_marker.read_text())
        if seen.get("PROBE_LLM_KEY") == SECRET_VALUE:
            break
        time.sleep(0.3)
    assert seen.get("PROBE_LLM_KEY") == SECRET_VALUE, \
        f"PROBE_LLM_KEY never observed by a later iteration; last seen keys: {list(seen)}"
    assert seen.get("PROBE_LLM_ENDPOINT") == "https://gw.example.com"

    # -- a second PUT *replaces* the set (old key gone) ---------------------
    st, _ = _put(e.port, "/config/llm",
                 json.dumps({"env": {"OTHER_KEY": "v2"}}).encode())
    assert st == 204
    deadline = time.time() + 20
    seen = {}
    while time.time() < deadline:
        seen = json.loads(env_marker.read_text())
        if seen.get("OTHER_KEY") == "v2":
            break
        time.sleep(0.3)
    assert seen.get("OTHER_KEY") == "v2"
    assert "PROBE_LLM_KEY" not in seen, "env PUT must replace, not merge"

    # -- no leakage anywhere host-visible -----------------------------------
    e.stop()
    time.sleep(0.2)
    for p in e.run_dir.rglob("*"):
        if p.is_file() and p.name != ".stub-env.json":
            assert SECRET_VALUE not in p.read_text(errors="ignore"), p
    stdout = e.proc.stdout.read() if e.proc.stdout else ""
    assert SECRET_VALUE not in stdout


def test_llm_pi_fragment_merges_into_models_json(llm_engine, tmp_path):
    fake_home = tmp_path / "fakehome2"
    (fake_home / ".pi" / "agent").mkdir(parents=True)
    existing = {"providers": {"anthropic": {"apiKey": "existing-value",
                                            "models": [{"id": "opus"}]}}}
    models_path = fake_home / ".pi" / "agent" / "models.json"
    models_path.write_text(json.dumps(existing))

    e = llm_engine(job={"iterations": 3, "max_approaches": 1},
                   stub_env={"HOME": str(fake_home)})
    e.wait_api()

    fragment = {"providers": {"my-gateway": {
        "baseUrl": "https://gw.example.com/api/v1",
        "api": "openai-completions",
        "apiKey": SECRET_VALUE,
        "models": [{"id": "big-model"}]}}}
    st, _ = _put(e.port, "/config/llm", json.dumps({"pi": fragment}).encode())
    assert st == 204

    doc = json.loads(models_path.read_text())
    # pre-existing provider preserved (merge, not replace) ...
    assert doc["providers"]["anthropic"]["apiKey"] == "existing-value"
    # ... new provider merged in immediately, no restart needed
    assert doc["providers"]["my-gateway"]["baseUrl"] == "https://gw.example.com/api/v1"
    assert doc["providers"]["my-gateway"]["apiKey"] == SECRET_VALUE

    e.stop()
    time.sleep(0.2)
    for p in e.run_dir.rglob("*"):
        if p.is_file():
            assert SECRET_VALUE not in p.read_text(errors="ignore"), p


def test_put_llm_validation(llm_engine, tmp_path):
    e = llm_engine(job={"iterations": 3, "max_approaches": 1},
                   stub_env={"HOME": str(tmp_path / "fakehome3")})
    e.wait_api()

    st, _ = _put(e.port, "/config/llm", b"{}")
    assert st == 422

    st, _ = _put(e.port, "/config/llm", json.dumps({"env": "not-a-dict"}).encode())
    assert st == 422

    st, _ = _put(e.port, "/config/llm", json.dumps({"pi": ["not", "a", "dict"]}).encode())
    assert st == 422
