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
from tests.conftest import LiveRun


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
    procs: list[LiveRun] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> LiveRun:
        defaults = {"run_id": "prompts-e2e", "iterations": 8,
                    "max_approaches": 1, "on_complete": "idle"}
        e = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env)
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
    # GH #209 defect 2: the heading must spell `Role: Reviewer` -- the stub
    # `pi` dispatches on exactly that string, and a mounted review prompt
    # without it sends every review invocation down the stub's prose `else`
    # branch, so review never produces a parseable verdict. That used to be
    # scored as a rejection and the run ended `failed`; it now HOLDS on
    # `review_contract_unrepaired` and never reaches a terminal state, which
    # this node's own wait below would sit out. Nothing about this module's
    # subject (the prompts listing/override API) depends on which branch the
    # stub takes, only that the mounted file exists and the run finishes.
    _write_mounted_prompt(tmp_path / "config", "review",
                          "# Role: Reviewer\n\nmounted override.\n")
    # task 010 (B.4): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" holds the
    # first worker iteration open instead of racing a real STUB_SLEEP
    # window to land the PUT before any worker prompt is built -- it
    # doesn't matter that the held iteration's own prompt predates the
    # PUT, only that a LATER one (of the remaining STUB_TASKS) does not.
    hold_file = tmp_path / "hold-promptslist"
    e = prompts_engine(job={"iterations": 8},
                       stub_env={"HOME": str(fake_home),
                                "STUB_HOLD_FILE": str(hold_file),
                                "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30",
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
    hold_file.touch()
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
