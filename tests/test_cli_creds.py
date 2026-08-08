"""Black-box tests for `ralphctl start --creds <dir>`.

Invokes the real `ralphctl` executable with RALPHD_DOCKER pointing at the
recording stub (tests/stub-docker/docker) and RALPHD_REGISTRY at a tmp dir,
then asserts on what landed in the job's config dir (`<registry>/configs/
<run-id>/creds/`) and that nothing credential-shaped ever touches the run
dir (`<registry>/runs/<run-id>/`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
RALPHCTL = Path(sys.executable).parent / "ralphctl"

SECRET = "sk-live-DO-NOT-LEAK-9f3a1c"


class Ctl:
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

    def config_dir(self, run_id: str) -> Path:
        return self.registry / "configs" / run_id

    def run_dir(self, run_id: str) -> Path:
        return self.registry / "runs" / run_id


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _make_creds_dir(base: Path) -> Path:
    src = base / "creds-src"
    src.mkdir()
    (src / "github.env").write_text(f"GITHUB_TOKEN={SECRET}\n")
    (src / "sonarqube.env").write_text(f"SONAR_TOKEN={SECRET}\n")
    (src / "gitconfig").write_text("[user]\n\tname = Bot\n")
    (src / "git-credentials").write_text(f"https://x:{SECRET}@example.com\n")
    (src / "netrc").write_text(f"machine example.com login x password {SECRET}\n")
    (src / "setup.sh").write_text("#!/bin/sh\necho setup ran\n")
    (src / "setup.sh").chmod(0o755)
    ssh = src / "ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text(f"-----BEGIN {SECRET}-----\n")
    # noise that must NOT be copied
    (src / "README.md").write_text("not a cred\n")
    (src / "notes.txt").write_text(f"stray secret {SECRET}\n")
    return src


def test_start_creds_copies_env_and_extras_ignores_rest(ctl, tmp_path):
    src = _make_creds_dir(tmp_path)
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-creds", "--creds", str(src))
    assert res.returncode == 0, res.stderr

    dest = ctl.config_dir("tst-creds") / "creds"
    assert (dest / "github.env").read_text() == (src / "github.env").read_text()
    assert (dest / "sonarqube.env").read_text() == (src / "sonarqube.env").read_text()
    assert (dest / "gitconfig").is_file()
    assert (dest / "git-credentials").is_file()
    assert (dest / "netrc").is_file()
    assert (dest / "setup.sh").is_file()
    assert os.access(dest / "setup.sh", os.X_OK)
    assert (dest / "ssh" / "id_ed25519").is_file()

    # exactly the recognized set landed -- noise ignored
    names = {p.name for p in dest.iterdir()}
    assert names == {"github.env", "sonarqube.env", "gitconfig",
                      "git-credentials", "netrc", "setup.sh", "ssh"}

    # nothing credential-shaped in the run dir
    rdir = ctl.run_dir("tst-creds")
    for p in rdir.rglob("*"):
        if p.is_file():
            assert SECRET not in p.read_text(errors="ignore")
    assert not (rdir / "creds").exists()


def test_start_creds_missing_dir_exits_2(ctl, tmp_path):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-nocreds", "--creds", str(tmp_path / "nope"))
    assert res.returncode == 2
    assert "not a directory" in res.stderr
    assert not ctl.config_dir("tst-nocreds").exists() or \
        not (ctl.config_dir("tst-nocreds") / "creds").exists()
