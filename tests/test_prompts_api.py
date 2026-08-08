"""Black-box tests: prompts runtime API (PRD req 10).

GET /config/prompts lists every phase prompt with its effective source
(builtin / mounted / api). PUT /config/prompts/{name} overrides a phase
prompt in the writable overlay, effective next iteration (proven via the
stub's prompt-dump marker file); an invalid phase name is rejected 4xx.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_e2e import EngineProc


def _put_text(port: int, path: str, text: str) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="PUT", data=text.encode(),
        headers={"Content-Type": "text/markdown"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def prompts_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "prompts-e2e", "iterations": 3,
                    "max_approaches": 1, "on_complete": "idle"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _write_mounted_prompt(config_dir: Path, name: str, text: str) -> None:
    d = config_dir / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)


def test_list_shows_builtin_then_mounted_then_api(prompts_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    _write_mounted_prompt(tmp_path / "config", "review",
                          "# Role: Review\n\nmounted override.\n")
    e = prompts_engine(job={"iterations": 8},
                       stub_env={"HOME": str(fake_home), "STUB_SLEEP": "1",
                                "STUB_TASKS": "3"})
    e.wait_api()

    status, body = e.api("GET", "/config/prompts")
    assert status == 200
    by_name = {p["name"]: p["source"] for p in body}
    assert by_name == {
        "planning": "builtin",
        "worker": "builtin",
        "review": "mounted",
        "task-verify": "builtin",
    }

    # -- PUT overrides a phase prompt; listing flips that phase to 'api' ----
    body_text = "# Role: Worker\n\nPROMPT-MARKER-abc123 overridden worker.\n"
    req_status = _put_text(e.port, "/config/prompts/worker", body_text)
    assert req_status == 204

    status, body = e.api("GET", "/config/prompts")
    by_name = {p["name"]: p["source"] for p in body}
    assert by_name["worker"] == "api"
    assert by_name["review"] == "mounted"
    assert by_name["planning"] == "builtin"

    # -- takes effect next iteration: the stub's worker prompts carry it ----
    e.wait_state(("succeeded", "failed", "aborted"), timeout=60)
    all_prompts = (e.run_dir / ".stub-all-prompts").read_text()
    chunks = [c for c in all_prompts.split("===PROMPT===") if c.strip()]
    assert any("PROMPT-MARKER-abc123" in c for c in chunks), \
        "override text never reached a worker prompt"

    # never landed under mounted /config or the run dir
    mounted_worker = tmp_path / "config" / "prompts" / "worker.md"
    if mounted_worker.exists():
        assert "PROMPT-MARKER-abc123" not in mounted_worker.read_text()
    overlay_file = fake_home / ".ralphd" / "config-overlay" / "prompts" / "worker.md"
    assert overlay_file.exists()
    assert "PROMPT-MARKER-abc123" in overlay_file.read_text()


def test_put_invalid_phase_name_rejected(prompts_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    e = prompts_engine(stub_env={"HOME": str(fake_home)})
    e.wait_api()
    status = _put_text(e.port, "/config/prompts/not-a-real-phase", "irrelevant\n")
    assert status == 422
    assert not (fake_home / ".ralphd" / "config-overlay" / "prompts"
               / "not-a-real-phase.md").exists()

    status, body = e.api("GET", "/config/prompts")
    assert status == 200
    assert all(p["name"] != "not-a-real-phase" for p in body)
