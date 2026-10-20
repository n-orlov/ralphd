"""scripts/allure_env.sh describes a Jenkins build to the Allure report.

It writes `environment.properties` (sha, branch, job, build number, python
version, runner image) and `executor.json` (the build URL) into a results
directory. The
nodes run the real script with fixed environment variables and compare both
files exactly, so a missing or renamed key fails.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "allure_env.sh"

_ENV = {
    "GIT_COMMIT": "0123456789abcdef0123456789abcdef01234567",
    "BRANCH_NAME": "main",
    "JOB_NAME": "ralphd/ci/main",
    "BUILD_NUMBER": "42",
    "BUILD_URL": "https://jenkins.example/job/ralphd/job/ci/job/main/42/",
    "ALLURE_RUNNER_IMAGE": "python:3.12.3",
}


def _run(directory: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ["PATH"], "PYTHON": sys.executable, **env}
    return subprocess.run(
        ["bash", str(SCRIPT), str(directory)],
        env=full, capture_output=True, text=True, timeout=60,
    )


def test_allure_env_writes_the_exact_environment_properties(tmp_path):
    out = tmp_path / "allure-results-fast"
    done = _run(out, _ENV)
    assert done.returncode == 0, done.stderr
    assert (out / "environment.properties").read_text() == (
        "sha=0123456789abcdef0123456789abcdef01234567\n"
        "branch=main\n"
        "job=ralphd/ci/main\n"
        "build=42\n"
        f"python={platform.python_version()}\n"
        "runner_image=python:3.12.3\n"
    )


def test_allure_env_writes_the_exact_executor_json_with_the_build_url(tmp_path):
    out = tmp_path / "results"
    out.mkdir()
    assert _run(out, _ENV).returncode == 0
    text = (out / "executor.json").read_text()
    assert json.loads(text) == {
        "name": "Jenkins",
        "type": "jenkins",
        "buildOrder": "42",
        "buildName": "ralphd/ci/main #42",
        "buildUrl": "https://jenkins.example/job/ralphd/job/ci/job/main/42/",
        "url": "https://jenkins.example/job/ralphd/job/ci/job/main/42/",
    }


def test_allure_env_keeps_every_key_when_the_environment_is_empty(tmp_path):
    out = tmp_path / "results"
    done = _run(out, {})
    assert done.returncode == 0, done.stderr
    props = dict(
        line.split("=", 1)
        for line in (out / "environment.properties").read_text().splitlines()
    )
    assert set(props) == {"sha", "branch", "job", "build", "python", "runner_image"}
    assert props["sha"] == "unknown" and props["runner_image"] == "unknown"
    executor = json.loads((out / "executor.json").read_text())
    assert executor["buildUrl"] == "unknown"


def test_allure_env_without_a_directory_argument_fails(tmp_path):
    done = subprocess.run(
        ["bash", str(SCRIPT)], env={"PATH": os.environ["PATH"]},
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert done.returncode != 0
    assert "usage" in done.stderr
