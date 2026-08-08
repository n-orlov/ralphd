"""Black-box test: `GET /config` -- effective job config, redacted (PRD
req 10).

Proves the response surfaces budgets, flags, model strategy, prompt
sources, and skills/creds *names* (with skill origin), while never leaking
credential file contents or LLM env values even when both are configured.
"""

from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path

import pytest
from test_e2e import EngineProc

SECRET_CRED_VALUE = "sekret-config-api-do-not-leak-abc123"
SECRET_LLM_VALUE = "sekret-llm-env-do-not-leak-xyz789"


@pytest.fixture
def config_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "config-api-e2e", "iterations": 3,
                    "max_approaches": 1, "on_complete": "idle",
                    "vigilant": True, "model_strategy": "balanced"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _mount_skill(config_dir: Path, name: str) -> None:
    d = config_dir / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n")


def _mount_cred(config_dir: Path, name: str, content: str) -> None:
    d = config_dir / "creds"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.env").write_text(content)


def _skill_tar(name: str) -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = f"# {name}\n".encode()
        info = tarfile.TarInfo(name="SKILL.md")
        info.size = len(data)
        tar.addfile(info, BytesIO(data))
    return buf.getvalue()


def test_get_config_shows_effective_config_no_secrets(config_engine, tmp_path):
    e = config_engine(job={"iterations": 3, "max_approaches": 1,
                            "on_complete": "idle"},
                       stub_env={"HOME": str(tmp_path / "fakehome1")})
    _mount_skill(e.config_dir, "mounted-skill")
    _mount_cred(e.config_dir, "github", f"TOKEN={SECRET_CRED_VALUE}\n")
    e.wait_api()

    # Add an api-origin skill and rotate LLM env via the runtime APIs too.
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{e.port}/config/skills/api-skill",
        method="PUT", data=_skill_tar("api-skill"))
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 204

    llm_status, _ = e.api("PUT", "/config/llm",
                          {"env": {"LLM_KEY": SECRET_LLM_VALUE}})
    assert llm_status == 204

    prompt_req = urllib.request.Request(
        f"http://127.0.0.1:{e.port}/config/prompts/worker",
        method="PUT", data=b"# Role: Worker\n\noverride\n",
        headers={"Content-Type": "text/markdown"})
    with urllib.request.urlopen(prompt_req, timeout=10) as resp:
        assert resp.status == 204

    status_code, doc = e.api("GET", "/config")
    assert status_code == 200

    # Budgets / flags / model strategy.
    assert doc["budgets"]["iterations"] == 3
    assert doc["budgets"]["maxApproaches"] == 1
    assert doc["flags"]["vigilant"] is True
    assert doc["flags"]["onComplete"] == "idle"
    assert doc["model"]["strategy"] == "balanced"

    # Prompt sources: worker is now 'api' after the PUT above; others builtin.
    prompts = {p["name"]: p["source"] for p in doc["prompts"]}
    assert prompts["worker"] == "api"
    assert prompts["planning"] == "builtin"

    # Skills: names + origins only, both mounted and api-added visible.
    skills = {s["name"]: s["origin"] for s in doc["skills"]}
    assert skills["mounted-skill"] == "mounted"
    assert skills["api-skill"] == "api"
    for s in doc["skills"]:
        assert set(s.keys()) == {"name", "origin"}

    # Creds: names only, no size/mtime/value fields, no secret text anywhere.
    assert doc["creds"] == ["github"]

    # LLM env: key names only, never values.
    assert doc["llmEnvKeys"] == ["LLM_KEY"]

    blob = json.dumps(doc)
    assert SECRET_CRED_VALUE not in blob
    assert SECRET_LLM_VALUE not in blob
    assert "TOKEN" not in blob  # the cred's key name isn't surfaced either

    e.wait_state(("succeeded", "failed", "aborted"))


def test_get_config_fieldless_job_defaults(config_engine, tmp_path):
    """A job with no skills/creds/llm/prompt overrides still returns a
    complete, well-shaped document (no crashes, empty lists)."""
    e = config_engine(job={"iterations": 3, "max_approaches": 1,
                           "on_complete": "idle", "vigilant": False},
                       stub_env={"HOME": str(tmp_path / "fakehome2")})
    e.wait_api()

    status_code, doc = e.api("GET", "/config")
    assert status_code == 200
    assert doc["skills"] == []
    assert doc["creds"] == []
    assert doc["llmEnvKeys"] == []
    assert all(p["source"] == "builtin" for p in doc["prompts"])
    assert doc["flags"]["vigilant"] is False

    e.wait_state(("succeeded", "failed", "aborted"))
