"""Black-box tests for multi-workspace support (PRD req 27):
`ralphctl start` repeatable `--workspace <dir>[:name]`.

CLI-side mount correctness reuses tests/test_cli_docker.py's stub-docker
recording harness (Ctl/docker_run_argv/env_vars). The engine-side test
(prompts list the mounted workspace names) reuses tests/test_e2e.py's
no-Docker EngineProc/engine_factory harness.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_docker import Ctl, docker_run_argv, env_vars
from test_e2e import engine_factory

__all__ = ["engine_factory"]


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def docker_run_argv_nth(ctl: Ctl, idx: int) -> list[str]:
    runs = [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]
    assert len(runs) >= 1
    return runs[idx]


def _wait_for_prompt(run_dir: Path, timeout: float = 15) -> Path:
    deadline = time.time() + timeout
    prompt_path = None
    while time.time() < deadline:
        it_dirs = sorted((run_dir / "iterations").glob("*")) \
            if (run_dir / "iterations").is_dir() else []
        for d in it_dirs:
            p = d / "prompt.md"
            if p.exists() and p.stat().st_size > 0:
                prompt_path = p
                break
        if prompt_path:
            break
        time.sleep(0.2)
    assert prompt_path is not None, "no prompt.md ever appeared"
    return prompt_path


# --------------------------------------------------------------------------
# CLI mount correctness
def test_single_unnamed_workspace_mounts_at_workspace_root(ctl, tmp_path):
    """Existing/unchanged behavior: one --workspace, no :name, mounts at
    /workspace exactly as before."""
    ws = tmp_path / "solo-repo"
    ws.mkdir()
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-single", "--workspace", str(ws))
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)
    assert f"{ws}:/workspace" in argv
    assert not any(a.endswith(f":/workspace/{ws.name}") for a in argv)
    ev = env_vars(argv)
    assert not any(v.startswith("RALPHD_WORKSPACES=") for v in ev)

    host = json.loads((ctl.registry / "runs" / "tst-single" / "host.json").read_text())
    assert host["workspace"] == str(ws)
    assert "workspaces" not in host


def test_two_named_workspaces_mount_at_workspace_name(ctl, tmp_path):
    a = tmp_path / "repo-a"
    b = tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-multi",
                  "--workspace", f"{a}:alpha",
                  "--workspace", f"{b}:beta")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)
    assert f"{a}:/workspace/alpha" in argv
    assert f"{b}:/workspace/beta" in argv
    assert not any(x == f"{a}:/workspace" for x in argv)

    ev = env_vars(argv)
    assert "RALPHD_WORKSPACES=alpha,beta" in ev

    host = json.loads((ctl.registry / "runs" / "tst-multi" / "host.json").read_text())
    assert host["workspaces"] == {"alpha": str(a), "beta": str(b)}
    assert "workspace" not in host


def test_multiple_workspaces_without_all_names_exits_2(ctl, tmp_path):
    a = tmp_path / "repo-a"
    b = tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-badmulti",
                  "--workspace", str(a),
                  "--workspace", f"{b}:beta")
    assert res.returncode == 2
    assert "needs a name" in res.stderr


def test_allow_docker_multi_workspace_sets_host_workspaces_json(ctl, tmp_path):
    sock_path = tmp_path / "docker.sock"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock_path))
    try:
        a = tmp_path / "repo-a"
        b = tmp_path / "repo-b"
        a.mkdir()
        b.mkdir()
        res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                      "--run-id", "tst-multidock",
                      "--workspace", f"{a}:alpha", "--workspace", f"{b}:beta",
                      "--allow-docker", env={"RALPHD_DOCKER_SOCK": str(sock_path)})
        assert res.returncode == 0, res.stderr
        ev = env_vars(docker_run_argv(ctl))
        hostwss = [v for v in ev if v.startswith("RALPHD_HOST_WORKSPACES=")]
        assert len(hostwss) == 1
        payload = json.loads(hostwss[0][len("RALPHD_HOST_WORKSPACES="):])
        assert payload == {"alpha": str(a), "beta": str(b)}
        assert not any(v.startswith("RALPHD_HOST_WORKSPACE=") for v in ev)
    finally:
        s.close()


def test_resume_remounts_named_workspaces(ctl, tmp_path):
    a = tmp_path / "repo-a"
    b = tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-resume-multi",
                  "--workspace", f"{a}:alpha", "--workspace", f"{b}:beta")
    assert res.returncode == 0, res.stderr

    res2 = ctl.run("resume", "tst-resume-multi")
    assert res2.returncode == 0, res2.stderr
    argv = docker_run_argv_nth(ctl, -1)
    assert f"{a}:/workspace/alpha" in argv
    assert f"{b}:/workspace/beta" in argv
    ev = env_vars(argv)
    assert "RALPHD_WORKSPACES=alpha,beta" in ev


# --------------------------------------------------------------------------
# Engine-side: prompts list the mounted workspace names
def test_prompt_lists_mounted_workspace_names(engine_factory):
    e = engine_factory(job={"on_complete": "idle", "iterations": 1},
                        stub_env={"RALPHD_WORKSPACES": "app,lib"})
    e.wait_api()
    prompt_path = _wait_for_prompt(e.run_dir)
    text = prompt_path.read_text()
    assert "app" in text
    assert "lib" in text
    assert "Workspaces (code directories), 2 mounted" in text


def test_prompt_single_workspace_line_unchanged_without_env(engine_factory):
    e = engine_factory(job={"on_complete": "idle", "iterations": 1})
    e.wait_api()
    prompt_path = _wait_for_prompt(e.run_dir)
    text = prompt_path.read_text()
    assert "- Workspace (code) directory:" in text
    assert "Workspaces (code directories)" not in text
