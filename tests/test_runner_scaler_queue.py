"""The runner scaler's queue-to-demand rules: which queued jobs get a container
(label filter), that a job never gets a second one (existing-container check)
and the concurrency cap with oldest-first ordering. Driven against the fake
GitHub API and the fake `docker` in `tests/_runner_scaler_fakes.py`."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"


@pytest.fixture()
def github():
    with FakeGitHub() as fake:
        yield fake


@pytest.fixture()
def docker(tmp_path):
    return FakeDocker(tmp_path / "fakedocker")


def write_config(tmp_path, github, extra=""):
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    path = tmp_path / "scaler.yaml"
    path.write_text(
        f"repo: {github.repo}\n"
        f"token_file: {token}\nhost_cpus: 64\n"
        f"api_url: {github.url}\n"
        "image: ci-runner:test\n" + extra)
    return path


@pytest.fixture()
def config(tmp_path, github):
    return write_config(tmp_path, github)


def once(docker, config):
    done = subprocess.run(
        [sys.executable, str(SCALER), "--config", str(config), "once"],
        capture_output=True, text=True, env=docker.env(), timeout=60)
    assert done.returncode == 0, done.stderr
    return done


def started_names(docker):
    """Container names in the order `docker run` was invoked."""
    return [argv[argv.index("--name") + 1] for argv in docker.commands("run", "create")]


def queue_job(github, run_id, job_id, labels):
    if not any(r["id"] == run_id for r in github.runs):
        github.add_run(run_id)
    github.add_job(run_id, job_id, labels)


def test_only_ci_pool_labelled_queued_jobs_get_a_container(github, docker, config):
    queue_job(github, 1, 101, ["self-hosted", "ci-pool"])
    queue_job(github, 1, 102, ["self-hosted", "fast-lane"])
    queue_job(github, 2, 201, ["ci-pool"])
    once(docker, config)
    assert started_names(docker) == ["ci-runner-101", "ci-runner-201"]
    assert sorted(docker.containers()) == ["ci-runner-101", "ci-runner-201"]


def test_two_passes_over_the_same_queue_start_a_job_once(github, docker, config):
    queue_job(github, 1, 101, ["ci-pool"])
    once(docker, config)
    once(docker, config)
    assert started_names(docker) == ["ci-runner-101"]
    assert list(docker.containers()) == ["ci-runner-101"]


def test_a_pass_skips_a_job_whose_container_has_already_exited(github, docker, config):
    queue_job(github, 1, 101, ["ci-pool"])
    docker.add_container("ci-runner-101", created_epoch=time.time(), state="exited")
    once(docker, config)
    assert docker.commands("run", "create") == []


def test_the_cap_limits_starts_and_freed_slots_are_filled_oldest_job_first(
        github, docker, config):
    for job_id in (110, 103, 107, 101, 109, 102, 108, 104, 106, 105):
        queue_job(github, 1 if job_id % 2 else 2, job_id, ["ci-pool"])
    once(docker, config)
    first = [f"ci-runner-{n}" for n in (101, 102, 103, 104)]
    assert started_names(docker) == first
    assert sorted(docker.containers()) == first

    for name in ("ci-runner-101", "ci-runner-102"):
        subprocess.run([str(docker.bin_dir / "docker"), "stop", name], check=True,
                       env=docker.env(), capture_output=True)
    once(docker, config)
    assert started_names(docker) == first + ["ci-runner-105", "ci-runner-106"]


def test_max_concurrency_is_read_from_the_config(tmp_path, github, docker):
    for job_id in range(1, 6):
        queue_job(github, 1, job_id, ["ci-pool"])
    once(docker, write_config(tmp_path, github, "max_concurrency: 2\n"))
    assert started_names(docker) == ["ci-runner-1", "ci-runner-2"]


def test_a_dry_run_honours_the_cap_and_existing_containers(github, docker, config):
    for job_id in range(1, 9):
        queue_job(github, 1, job_id, ["ci-pool"])
    docker.add_container("ci-runner-1", created_epoch=time.time())
    done = subprocess.run(
        [sys.executable, str(SCALER), "--config", str(config), "once", "--dry-run"],
        capture_output=True, text=True, env=docker.env(), timeout=60)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines == [f"would start ci-runner-{n} for job {n}" for n in (2, 3, 4)]
    assert docker.commands("run", "create") == []
