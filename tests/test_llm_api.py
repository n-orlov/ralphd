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
from tests.conftest import LiveRun

SECRET_VALUE = "sekret-llm-key-do-not-leak-abc123"

ENV_MARKER_TIMEOUT = 20


def wait_for_env_marker(path, predicate=None, timeout=ENV_MARKER_TIMEOUT):
    """Poll the stub's `.stub-env.json` until it holds a PARSABLE document
    satisfying `predicate` (default: any document at all) and return that
    document -- {} if the deadline passes first, so the caller's own
    assertion is what reports the failure.

    Waiting for mere existence is what reddened task 018's fast lane: the
    marker is re-dumped by every `pi` invocation while this poller reads it,
    and a zero-byte read raised JSONDecodeError inside the test body. The
    stub now writes it atomically (temp file + os.replace); this reader
    additionally treats an absent or unparsable document as "not there yet"
    rather than as an error, which is also the only shape that stays correct
    if a future stub knob writes the marker some other way. No assertion is
    relaxed by this: every caller still asserts exactly the same thing about
    the document returned here.
    """
    deadline = time.time() + timeout
    seen: dict = {}
    while True:
        try:
            seen = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            seen = {}
        else:
            if predicate is None or predicate(seen):
                return seen
        if time.time() >= deadline:
            return seen
        time.sleep(0.2)


@pytest.fixture
def llm_engine(tmp_path):
    procs: list[LiveRun] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> LiveRun:
        defaults = {"run_id": "llm-api-e2e", "iterations": 20,
                    "max_approaches": 1, "on_complete": "idle"}
        e = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env)
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
    # task 010 (B.4): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" +
    # STUB_HOLD_REARM_MAX="1" holds the first TWO worker iterations open
    # (one per PUT below) instead of racing a real STUB_SLEEP window to
    # land each PUT before the next iteration's env dump -- released once
    # per checkpoint, and the stub itself re-arms the hold for the second
    # checkpoint the instant the first is released (see the stub's own
    # STUB_HOLD_REARM_MAX doc).
    hold_file = tmp_path / "hold-llmapi"
    e = llm_engine(stub_env={"HOME": str(fake_home),
                             "STUB_HOLD_FILE": str(hold_file),
                             "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30",
                             "STUB_HOLD_REARM_MAX": "1", "STUB_TASKS": "5"})
    e.wait_api()

    # -- no override yet: the stub's env has no trace of our key -----------
    env_marker = e.run_dir / ".stub-env.json"
    first = wait_for_env_marker(env_marker)
    assert first, "stub never ran a first iteration"
    assert "PROBE_LLM_KEY" not in first

    # -- PUT replaces the env-override set; effective next iteration -------
    body = json.dumps({"env": {"PROBE_LLM_KEY": SECRET_VALUE,
                               "PROBE_LLM_ENDPOINT": "https://gw.example.com"}}).encode()
    st, _ = _put(e.port, "/config/llm", body)
    assert st == 204
    hold_file.touch()  # release checkpoint 1; the stub re-arms it for #2

    seen = wait_for_env_marker(
        env_marker, lambda d: d.get("PROBE_LLM_KEY") == SECRET_VALUE)
    assert seen.get("PROBE_LLM_KEY") == SECRET_VALUE, \
        f"PROBE_LLM_KEY never observed by a later iteration; last seen keys: {list(seen)}"
    assert seen.get("PROBE_LLM_ENDPOINT") == "https://gw.example.com"

    # -- a second PUT *replaces* the set (old key gone) ---------------------
    st, _ = _put(e.port, "/config/llm",
                 json.dumps({"env": {"OTHER_KEY": "v2"}}).encode())
    assert st == 204
    hold_file.touch()  # release checkpoint 2 (last -- no more re-arming)
    seen = wait_for_env_marker(env_marker, lambda d: d.get("OTHER_KEY") == "v2")
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
