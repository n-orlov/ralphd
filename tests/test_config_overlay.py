"""Black-box test: writable config overlay layered over a read-only-mounted
/config (PRD req 11).

The config dir is chmod'd read-only (555) *before* the engine starts (real
containers mount /config `ro`). A config mutation via the API (a prompt
override PUT — a minimal, generic slice of the future config-CRUD API) must
still succeed and take effect on the next iteration, by landing in a
container-local writable overlay path — never under /config (still read-only
afterwards) and never under the run dir.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_e2e import STUB_PI, EngineProc, free_port


def _chmod_tree_ro(root: Path) -> None:
    for p in root.rglob("*"):
        if p.is_file():
            p.chmod(0o444)
    root.chmod(0o555)


@pytest.fixture
def overlay_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict, stub_env: dict, readonly_config: bool = True) -> EngineProc:
        run_dir = tmp_path / "run"
        config_dir = tmp_path / "config"
        workspace = tmp_path / "ws"
        for d in (run_dir, config_dir, workspace):
            d.mkdir(parents=True, exist_ok=True)
        (config_dir / "prd.md").write_text("# overlay test PRD\n\nDo the thing.\n")
        (config_dir / "job.yaml").write_text(
            "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items()))
        if readonly_config:
            _chmod_tree_ro(config_dir)

        port = free_port()
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir(exist_ok=True)
        base_env = {
            k: v for k, v in os.environ.items()
            if k not in ("RALPHD_HOST_WORKSPACE", "RALPHD_HOST_RUN_DIR", "RALPHD_RUN_ID")
        }
        env = {
            **base_env,
            "PATH": f"{STUB_PI}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "STUB_RUN_DIR": str(run_dir),
            "RALPHD_RUN_DIR": str(run_dir),
            "RALPHD_CONFIG_DIR": str(config_dir),
            "RALPHD_WORKSPACE_DIR": str(workspace),
            "RALPHD_PORT": str(port),
            "HOME": str(fake_home),
            **stub_env,
        }
        e = EngineProc.__new__(EngineProc)
        e.run_dir = run_dir
        e.config_dir = config_dir
        e.workspace = workspace
        e.port = port
        e.proc = subprocess.Popen(
            ["ralphd-engine"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        e.fake_home = fake_home
        procs.append(e)
        return e

    yield make
    for e in procs:
        # restore writability so tmp_path cleanup doesn't choke on the RO tree
        e.stop()
        for p in e.config_dir.rglob("*"):
            try:
                p.chmod(0o755)
            except OSError:
                pass
        e.config_dir.chmod(0o755)


def put_raw(port: int, path: str, body: bytes) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="PUT", data=body,
        headers={"Content-Type": "text/markdown"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


# Keep the "Role: Worker" marker so the stub still recognizes the phase and
# makes real task progress -- the point here is proving the *content* the
# stub receives is the override, not breaking the phase detection.
OVERRIDE_TEXT = (
    "# Role: Worker\n\nOVERLAY-MARKER-xyz987 overridden worker instructions.\n")


def test_config_mutation_persists_in_overlay_not_readonly_config(overlay_engine):
    e = overlay_engine(
        job={"run_id": "overlay-e2e", "iterations": 8, "max_approaches": 1,
             "on_complete": "exit"},
        stub_env={"STUB_SLEEP": "2"})
    e.wait_api()

    # /config is genuinely read-only: confirm the mount mode before we rely on it.
    mode = stat.S_IMODE(e.config_dir.stat().st_mode)
    assert mode == 0o555

    status = put_raw(e.port, "/config/prompts/worker", OVERRIDE_TEXT.encode())
    assert status == 204

    # /config itself must remain untouched: still read-only, no new prompts/ dir.
    assert stat.S_IMODE(e.config_dir.stat().st_mode) == 0o555
    assert not (e.config_dir / "prompts").exists()

    assert e.proc.wait(timeout=60) == 0
    status_json = json.loads((e.run_dir / "status.json").read_text())
    assert status_json["state"] == "succeeded"

    all_prompts = (e.run_dir / ".stub-all-prompts").read_text()
    prompts = [p for p in all_prompts.split("===PROMPT===") if p.strip()]
    worker_prompts = [p for p in prompts if "OVERLAY-MARKER-xyz987" in p]
    assert worker_prompts, "override text never reached a worker prompt"

    # The overlay itself lives under $HOME (container-local), never in the
    # run dir or under /config -- this is the actual proof of req 11.
    overlay_file = e.fake_home / ".ralphd" / "config-overlay" / "prompts" / "worker.md"
    assert overlay_file.exists()
    assert "OVERLAY-MARKER-xyz987" in overlay_file.read_text()
    assert not overlay_file.is_relative_to(e.run_dir)
    assert not overlay_file.is_relative_to(e.config_dir)
    # /config itself: no file anywhere under it was created/changed by the PUT.
    for f in e.config_dir.rglob("*"):
        if f.is_file():
            assert "OVERLAY-MARKER-xyz987" not in f.read_text(errors="ignore")


def test_prompt_override_rejects_empty_body(overlay_engine):
    e = overlay_engine(
        job={"run_id": "overlay-empty", "iterations": 3, "max_approaches": 1,
             "on_complete": "exit"},
        stub_env={"STUB_SLEEP": "2"})
    e.wait_api()
    req = urllib.request.Request(
        f"http://127.0.0.1:{e.port}/config/prompts/worker", method="PUT", data=b"")
    try:
        urllib.request.urlopen(req, timeout=10)
        raised = False
    except urllib.error.HTTPError as exc:
        raised = True
        assert exc.code == 422
    assert raised
    e.proc.wait(timeout=60)
