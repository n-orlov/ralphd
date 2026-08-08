"""Black-box tests for `ralphctl start --skills <dir>` validation.

Invokes the real `ralphctl` executable with RALPHD_DOCKER pointing at the
recording stub (tests/stub-docker/docker) and RALPHD_REGISTRY at a tmp dir,
then asserts on what landed in the job's config dir (`<registry>/configs/
<run-id>/skills/`).
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


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _make_single_skill(base: Path, name: str = "git") -> Path:
    src = base / name
    src.mkdir()
    (src / "SKILL.md").write_text(f"# {name} skill\n")
    (src / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    return src


def _make_skills_folder(base: Path) -> Path:
    src = base / "skills"
    src.mkdir()
    for name in ("git", "playwright"):
        child = src / name
        child.mkdir()
        (child / "SKILL.md").write_text(f"# {name} skill\n")
    return src


def test_start_skills_single_skill_dir(ctl, tmp_path):
    src = _make_single_skill(tmp_path)
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-skill-one", "--skills", str(src))
    assert res.returncode == 0, res.stderr

    dest = ctl.config_dir("tst-skill-one") / "skills"
    assert {p.name for p in dest.iterdir()} == {"git"}
    assert (dest / "git" / "SKILL.md").is_file()
    assert (dest / "git" / "helper.sh").is_file()


def test_start_skills_directory_of_skills_expands(ctl, tmp_path):
    src = _make_skills_folder(tmp_path)
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-skill-many", "--skills", str(src))
    assert res.returncode == 0, res.stderr

    dest = ctl.config_dir("tst-skill-many") / "skills"
    assert {p.name for p in dest.iterdir()} == {"git", "playwright"}
    assert (dest / "git" / "SKILL.md").is_file()
    assert (dest / "playwright" / "SKILL.md").is_file()


def test_start_skills_invalid_dir_exits_2(ctl, tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    # neither SKILL.md itself, nor every child having one
    (bad / "child_ok").mkdir()
    (bad / "child_ok" / "SKILL.md").write_text("# ok\n")
    (bad / "child_bad").mkdir()
    (bad / "child_bad" / "notes.txt").write_text("no skill here\n")

    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-skill-bad", "--skills", str(bad))
    assert res.returncode == 2
    assert str(bad) in res.stderr


def test_start_skills_repeatable(ctl, tmp_path):
    one = _make_single_skill(tmp_path, "git")
    two = _make_single_skill(tmp_path, "sonar")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-skill-rep", "--skills", str(one),
                  "--skills", str(two))
    assert res.returncode == 0, res.stderr

    dest = ctl.config_dir("tst-skill-rep") / "skills"
    assert {p.name for p in dest.iterdir()} == {"git", "sonar"}
