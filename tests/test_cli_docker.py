"""Black-box tests for ralphctl's docker sibling-container support.

Each test invokes the real `ralphctl` executable as a subprocess with
RALPHD_DOCKER pointing at a recording stub (tests/stub-docker/docker) and
RALPHD_REGISTRY at a tmp dir, then asserts on the recorded docker argv,
stdout/stderr, and exit codes. No CLI internals are imported.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
RALPHCTL = Path(sys.executable).parent / "ralphctl"


class Ctl:
    """ralphctl runner bound to a tmp registry + recording stub docker."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.log = tmp / "docker-argv.jsonl"
        self.prd = tmp / "prd.md"
        self.prd.write_text("# Test PRD\n\nDo the thing.\n")

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {
            **os.environ,
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            **(env or {}),
        }
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=60)

    def recorded(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


@pytest.fixture
def unix_sock(tmp_path):
    """A real unix socket standing in for the host docker socket."""
    path = tmp_path / "docker.sock"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    yield path
    s.close()


def docker_run_argv(ctl: Ctl) -> list[str]:
    runs = [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]
    assert len(runs) == 1, f"expected one docker run, got: {ctl.recorded()}"
    return runs[0]


def env_vars(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


def labels(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]


# --------------------------------------------------------------------------
def test_start_labels_job_role_and_exports_self_container_id(ctl):
    """Task 034 (#7): the job container is distinguishable from the siblings
    the job starts (ralphd.role=job) and knows its own identifier, so the
    cleanup idiom handed to the agent can exclude it."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-role")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)

    assert labels(argv) == ["ralphd.run=tst-role", "ralphd.role=job"]
    assert "RALPHD_SELF_CONTAINER_ID=ralphd-tst-role" in env_vars(argv)
    # the exported id is the identifier docker was given for this container
    ni = argv.index("--name")
    assert argv[ni + 1] == "ralphd-tst-role"


def test_start_without_allow_docker_has_label_but_no_socket(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none", "--run-id", "tst-run")
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)

    # label is always present (uniform reaping)
    li = argv.index("--label")
    assert argv[li + 1] == "ralphd.run=tst-run"

    # no socket mount, no group-add, no host-path env vars
    assert "--group-add" not in argv
    assert not any(v.endswith(":/var/run/docker.sock") for v in argv)
    assert not any(v.startswith(("RALPHD_HOST_", "RALPHD_RUN_ID="))
                   for v in env_vars(argv))
    assert "root-equivalent" not in res.stderr.lower()


def test_start_allow_docker_injects_socket_group_and_env(ctl, tmp_path, unix_sock):
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none", "--run-id", "tst-dock",
                  "--workspace", str(ws), "--allow-docker",
                  env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    assert res.returncode == 0, res.stderr
    argv = docker_run_argv(ctl)

    # socket mount + group-add with the socket's actual gid
    mi = argv.index(f"{unix_sock}:/var/run/docker.sock")
    assert argv[mi - 1] == "-v"
    gi = argv.index("--group-add")
    assert argv[gi + 1] == str(os.stat(unix_sock).st_gid)

    # host-path env vars + run id + label
    ev = env_vars(argv)
    assert f"RALPHD_HOST_WORKSPACE={ws}" in ev
    assert f"RALPHD_HOST_RUN_DIR={ctl.registry / 'runs' / 'tst-dock'}" in ev
    assert "RALPHD_RUN_ID=tst-dock" in ev
    li = argv.index("--label")
    assert argv[li + 1] == "ralphd.run=tst-dock"

    # loud warning on stderr
    assert "ROOT-EQUIVALENT" in res.stderr


def test_start_allow_docker_without_workspace_omits_host_workspace(ctl, unix_sock):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none", "--run-id", "tst-nows",
                  "--allow-docker", env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    assert res.returncode == 0, res.stderr
    ev = env_vars(docker_run_argv(ctl))
    assert not any(v.startswith("RALPHD_HOST_WORKSPACE=") for v in ev)
    assert any(v.startswith("RALPHD_HOST_RUN_DIR=") for v in ev)
    assert "RALPHD_RUN_ID=tst-nows" in ev


def test_start_allow_docker_missing_socket_exits_2(ctl, tmp_path):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none", "--run-id", "tst-nosock",
                  "--allow-docker",
                  env={"RALPHD_DOCKER_SOCK": str(tmp_path / "absent.sock")})
    assert res.returncode == 2
    assert "not found" in res.stderr
    # never reached docker run. (The job-image cache probe -- `image inspect`,
    # task 033 -- legitimately precedes this check, so the invariant is about
    # `run`, not about the log being empty.)
    assert [a for a in ctl.recorded() if a[:1] == ["run"]] == []


def test_start_allow_docker_non_socket_path_exits_2(ctl, tmp_path):
    plain = tmp_path / "not-a-socket"
    plain.write_text("x")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none", "--run-id", "tst-plain",
                  "--allow-docker", env={"RALPHD_DOCKER_SOCK": str(plain)})
    assert res.returncode == 2
    assert "not a socket" in res.stderr


def test_stop_reaps_labeled_siblings(ctl):
    run_dir = ctl.registry / "runs" / "tst-stop"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"state": "succeeded"}))

    res = ctl.run("stop", "tst-stop",
                  env={"STUB_DOCKER_PS_IDS": "sib1,sib2"})
    assert res.returncode == 0, res.stderr
    rec = ctl.recorded()
    assert ["rm", "-f", "ralphd-tst-stop"] in rec
    assert ["ps", "-aq", "--filter", "label=ralphd.run=tst-stop"] in rec
    assert ["rm", "-f", "sib1"] in rec
    assert ["rm", "-f", "sib2"] in rec
    # task 034: _reap_siblings() keeps its run-id-ONLY filter -- host-side
    # `stop` takes the whole run down, job container included; the
    # role=sibling narrowing is only for cleanup run from inside the job.
    assert not any("ralphd.role" in a for argv in rec for a in argv)


def test_stop_reap_failure_is_non_fatal(ctl):
    """If `docker ps` finds nothing (or fails), stop still succeeds."""
    run_dir = ctl.registry / "runs" / "tst-quiet"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"state": "failed"}))
    res = ctl.run("stop", "tst-quiet")  # STUB_DOCKER_PS_IDS unset -> no ids
    assert res.returncode == 0, res.stderr
    rec = ctl.recorded()
    assert ["ps", "-aq", "--filter", "label=ralphd.run=tst-quiet"] in rec
    # only the job container itself was rm'd
    rms = [a for a in rec if a[0] == "rm"]
    assert rms == [["rm", "-f", "ralphd-tst-quiet"]]


def test_rm_reaps_labeled_siblings(ctl):
    run_dir = ctl.registry / "runs" / "tst-rm"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"state": "succeeded"}))

    res = ctl.run("rm", "tst-rm", "--yes",
                  env={"STUB_DOCKER_PS_IDS": "sib9"})
    assert res.returncode == 0, res.stderr
    rec = ctl.recorded()
    assert ["ps", "-aq", "--filter", "label=ralphd.run=tst-rm"] in rec
    assert ["rm", "-f", "sib9"] in rec
    assert not any("ralphd.role" in a for argv in rec for a in argv)
    assert not run_dir.exists()


def test_doctor_reports_stray_containers(ctl):
    """Containers labeled ralphd.run=<id> with no registry run dir are listed
    (report-only): the ok verdict comes from the regular checks alone."""
    # one live run (has a dir) and one stray (does not)
    (ctl.registry / "runs" / "live-run").mkdir(parents=True)
    labels = {"c-live": "live-run", "c-stray": "gone-run"}
    res = ctl.run("--json", "doctor",
                  env={"STUB_DOCKER_PS_IDS": "c-live,c-stray",
                       "STUB_DOCKER_INSPECT_LABELS": json.dumps(labels),
                       "STUB_DOCKER_INSPECT_OK": "1"})
    doc = json.loads(res.stdout)
    assert doc["strayContainers"] == [{"id": "c-stray", "runId": "gone-run"}]
    # verdict is computed from checks only, never from strays
    assert doc["ok"] == all(doc["checks"].values())
