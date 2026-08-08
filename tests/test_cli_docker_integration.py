"""Real-daemon integration tests for ralphctl's docker support.

Unlike test_cli_docker.py (recording stub — precise argv/exit-code contracts),
these run the real `ralphctl` against the REAL docker daemon and assert on the
actual effects: labels on created containers, socket binds, group-add, env,
and label-based reaping. Skipped automatically when docker is unavailable.

testcontainers-python was considered and deliberately not used: the subject
under test IS a container launcher (ralphctl runs `docker run` itself), so a
container-management library would only wrap the two fixture containers these
tests create — plain docker CLI calls keep the tests dependency-free and
black-box.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

RALPHCTL = Path(sys.executable).parent / "ralphctl"
DOCKER_SOCK = "/var/run/docker.sock"
IMAGE = "alpine:3"  # tiny stand-in for the job image; entrypoint exits at once


def _docker_available() -> bool:
    if not shutil.which("docker") or not Path(DOCKER_SOCK).is_socket():
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_available(),
                                reason="real docker daemon not available")


def docker(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *argv], capture_output=True, text=True,
                          timeout=120)


def inspect(name: str) -> dict:
    res = docker("inspect", name)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)[0]


class Ctl:
    """ralphctl runner bound to a tmp registry, using the real docker."""

    def __init__(self, tmp: Path):
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.prd = tmp / "prd.md"
        self.prd.write_text("# Integration PRD\n\nDo the thing.\n")
        self.cleanup: list[str] = []  # container names/ids to force-remove

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {**os.environ, "RALPHD_REGISTRY": str(self.registry),
                    **(env or {})}
        full_env.pop("RALPHD_DOCKER", None)  # real docker
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=120)

    def start(self, run_id: str, *extra: str) -> subprocess.CompletedProcess:
        self.cleanup.append(f"ralphd-{run_id}")
        return self.run("start", "--prd", str(self.prd), "--llm", "none",
                        "--run-id", run_id, "--image", IMAGE, *extra)


@pytest.fixture
def ctl(tmp_path):
    c = Ctl(tmp_path)
    yield c
    for name in c.cleanup:
        docker("rm", "-f", name)


def rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
def test_start_always_labels_job_container(ctl):
    run_id = rid("itg-plain")
    res = ctl.start(run_id)
    assert res.returncode == 0, res.stderr

    info = inspect(f"ralphd-{run_id}")
    assert info["Config"]["Labels"]["ralphd.run"] == run_id
    # no docker socket, no extra groups, no host-path env without --allow-docker
    assert not any(DOCKER_SOCK in b for b in info["HostConfig"]["Binds"])
    assert not info["HostConfig"]["GroupAdd"]
    assert not any(e.startswith(("RALPHD_HOST_", "RALPHD_RUN_ID="))
                   for e in info["Config"]["Env"])


def test_start_allow_docker_real_injection(ctl, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    run_id = rid("itg-dock")
    res = ctl.start(run_id, "--workspace", str(ws), "--allow-docker")
    assert res.returncode == 0, res.stderr
    assert "ROOT-EQUIVALENT" in res.stderr

    info = inspect(f"ralphd-{run_id}")
    assert info["Config"]["Labels"]["ralphd.run"] == run_id
    assert f"{DOCKER_SOCK}:/var/run/docker.sock" in info["HostConfig"]["Binds"]
    assert info["HostConfig"]["GroupAdd"] == [str(os.stat(DOCKER_SOCK).st_gid)]
    env = info["Config"]["Env"]
    assert f"RALPHD_HOST_WORKSPACE={ws}" in env
    assert f"RALPHD_HOST_RUN_DIR={ctl.registry / 'runs' / run_id}" in env
    assert f"RALPHD_RUN_ID={run_id}" in env


def test_stop_reaps_real_labeled_sibling(ctl):
    run_id = rid("itg-reap")
    res = ctl.start(run_id)
    assert res.returncode == 0, res.stderr

    # simulate a sibling the job left behind (detached, no --rm)
    sib = f"sib-{run_id}"
    ctl.cleanup.append(sib)
    res = docker("run", "-d", "--name", sib, "--label", f"ralphd.run={run_id}",
                 "busybox:stable", "sleep", "300")
    assert res.returncode == 0, res.stderr

    res = ctl.run("stop", run_id, "--force")
    assert res.returncode == 0, res.stderr
    # both the job container and the sibling are gone
    assert docker("inspect", f"ralphd-{run_id}").returncode != 0
    assert docker("inspect", sib).returncode != 0
    # run dir is kept
    assert (ctl.registry / "runs" / run_id).exists()


def test_doctor_lists_real_stray_container(ctl):
    # a labeled container whose run id has no registry dir = stray
    ghost_id = rid("itg-ghost")
    stray = f"stray-{ghost_id}"
    ctl.cleanup.append(stray)
    res = docker("run", "-d", "--name", stray, "--label",
                 f"ralphd.run={ghost_id}", "busybox:stable", "sleep", "60")
    assert res.returncode == 0, res.stderr
    stray_cid = res.stdout.strip()

    res = ctl.run("--json", "doctor", env={"RALPHD_IMAGE": IMAGE})
    doc = json.loads(res.stdout)
    # `docker ps -q` reports short (12-char) ids
    assert {"id": stray_cid[:12], "runId": ghost_id} in doc["strayContainers"]
    # strays never flip the verdict
    assert doc["ok"] == all(doc["checks"].values())
