"""Black-box tests: mechanical secret-value redaction (operator steering
019 / task 060).

Plants a known secret two ways -- a mounted creds `.env` file (placed by
the engine at `~/.creds/probe.env` at startup) and a `PUT /config/llm` env
override -- then has the stub worker (configured via `STUB_ECHO_SECRET`)
deliberately echo *both* planted values into its final assistant text AND
into a tool call's arguments/result, exactly mirroring the two real
incidents behind this task (a worker `cat`-ing a cred file; `docker
inspect` dumping a container's env). Asserts the persisted
`output.jsonl`/`events.jsonl` and the `GET /logs` response (tail and
follow modes) never contain either secret literal but do contain the
`[REDACTED:...]` marker, while an ordinary non-secret sentinel string
passes through completely untouched (not overzealous).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_e2e import EngineProc

SECRET_CRED = "sekret-cred-value-do-not-leak-redact-001"
SECRET_LLM = "sekret-llm-value-do-not-leak-redact-002"
NONSECRET_MARKER = "NONSECRET_SENTINEL_9f3a"


@pytest.fixture
def redact_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "redact-e2e", "iterations": 12,
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


def _write_mounted_cred(config_dir: Path, name: str, content: str) -> None:
    d = config_dir / "creds"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.env").write_text(content)


def test_secrets_redacted_from_output_events_and_logs(redact_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    # Planted secret #1: a mounted creds env file -- placed at
    # ~/.creds/probe.env by the engine at startup, picked up into the
    # redaction set by the same startup call (main.py: refresh_redaction_map
    # runs right after place_creds()).
    _write_mounted_cred(tmp_path / "config", "probe", f"PROBE_TOKEN={SECRET_CRED}\n")

    e = redact_engine(
        stub_env={"HOME": str(fake_home), "STUB_TASKS": "2", "STUB_SLEEP": "2",
                 "STUB_ECHO_SECRET": f"{SECRET_CRED},{SECRET_LLM}"})
    e.wait_api()

    # Planted secret #2: a PUT /config/llm env override. Fired immediately
    # after the API comes up -- the planning iteration sleeps STUB_SLEEP=2s
    # before doing anything, so this lands well before the worker phase's
    # subprocess (the one that actually echoes the secrets) is spawned,
    # ensuring the redaction map already includes it at write time (not
    # just retroactively at serve time).
    st, _ = _put(e.port, "/config/llm",
                 json.dumps({"env": {"PROBE_LLM_TOKEN": SECRET_LLM}}).encode())
    assert st == 204

    status = e.wait_state(("succeeded", "failed", "aborted"), timeout=60)
    assert status["state"] == "succeeded"

    # -- every file persisted under the run dir: no secret literal, ever ---
    # (except the stub's own test-inspection marker, `.stub-env.json` -- a
    # full env dump used by other tests, not something the engine itself
    # persists or serves)
    for p in e.run_dir.rglob("*"):
        if p.is_file() and p.name != ".stub-env.json":
            text = p.read_text(errors="ignore")
            assert SECRET_CRED not in text, f"leaked in {p}"
            assert SECRET_LLM not in text, f"leaked in {p}"

    # ... but the redaction marker actually fired (proves scrubbing ran,
    # not just "no secret because the stub never echoed one")
    output_texts = []
    for d in sorted((e.run_dir / "iterations").iterdir()):
        out = d / "output.jsonl"
        if out.exists():
            output_texts.append(out.read_text())
    all_output = "".join(output_texts)
    assert "[REDACTED:" in all_output

    # ordinary non-secret text passed through untouched (not overzealous)
    assert NONSECRET_MARKER in all_output

    events_text = (e.run_dir / "events.jsonl").read_text()
    assert SECRET_CRED not in events_text
    assert SECRET_LLM not in events_text

    # -- GET /logs, default (tail) mode: same guarantees ---------------
    logs_tail = e.api_raw("/logs")
    assert SECRET_CRED not in logs_tail
    assert SECRET_LLM not in logs_tail
    assert "[REDACTED:" in logs_tail
    assert NONSECRET_MARKER in logs_tail

    # -- GET /logs?follow=true: replays the same content, same guarantees --
    req = urllib.request.Request(f"http://127.0.0.1:{e.port}/logs?follow=true")
    with urllib.request.urlopen(req, timeout=30) as resp:
        logs_follow = resp.read().decode()
    assert SECRET_CRED not in logs_follow
    assert SECRET_LLM not in logs_follow
    assert "[REDACTED:" in logs_follow
    assert NONSECRET_MARKER in logs_follow

    # -- no API route (GET /config, /config/creds, etc.) ever exposes the
    # redaction set (they already only return names, per earlier tasks) --
    _st, cfg = e.api("GET", "/config")
    dumped_cfg = json.dumps(cfg)
    assert SECRET_CRED not in dumped_cfg
    assert SECRET_LLM not in dumped_cfg
    _st, creds_list = e.api("GET", "/config/creds")
    assert SECRET_CRED not in json.dumps(creds_list)

    # -- captured engine stdout also clean ----------------------------------
    e.stop()
    time.sleep(0.2)
    stdout = e.proc.stdout.read() if e.proc.stdout else ""
    assert SECRET_CRED not in stdout
    assert SECRET_LLM not in stdout
