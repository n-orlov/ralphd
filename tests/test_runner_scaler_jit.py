"""The runner scaler's JIT request (Rule 3): before it starts a container for a
queued job it asks GitHub for a just-in-time runner config with
`POST /repos/<repo>/actions/runners/generate-jitconfig`, naming the runner
`ci-<job id>`, carrying the job's own labels, in runner group 1. Driven against
the fake GitHub API and the fake `docker` in `tests/_runner_scaler_fakes.py`."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
JIT_PATH = "/repos/n-orlov/ralphd/actions/runners/generate-jitconfig"


@pytest.fixture()
def github():
    with FakeGitHub() as fake:
        yield fake


@pytest.fixture()
def docker(tmp_path):
    return FakeDocker(tmp_path / "fakedocker")


@pytest.fixture()
def config(tmp_path, github):
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    path = tmp_path / "scaler.yaml"
    path.write_text(f"repo: {github.repo}\ntoken_file: {token}\nhost_cpus: 64\n"
                    f"api_url: {github.url}\nimage: ci-runner:test\n")
    return path


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_jit_under_test", SCALER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def once(docker, config, *flags, expect=0):
    done = subprocess.run(
        [sys.executable, str(SCALER), "--config", str(config), "once", *flags],
        capture_output=True, text=True, env=docker.env(), timeout=60)
    assert done.returncode == expect, done.stderr
    return done


def queue_job(github, run_id, job_id, labels):
    if not any(r["id"] == run_id for r in github.runs):
        github.add_run(run_id)
    github.add_job(run_id, job_id, labels)


def jit_posts(github):
    return github.requests_to("POST")


def started_names(docker):
    return [argv[argv.index("--name") + 1] for argv in docker.commands("run", "create")]


def test_each_start_is_preceded_by_one_generate_jitconfig_post_with_the_job_fields(
        github, docker, config):
    queue_job(github, 1, 101, ["self-hosted", "ci-pool"])
    queue_job(github, 2, 202, ["ci-pool", "x64", "extra"])
    once(docker, config)
    posts = jit_posts(github)
    assert [p["path"] for p in posts] == [JIT_PATH, JIT_PATH]
    assert [p["body"] for p in posts] == [
        {"name": "ci-101", "labels": ["self-hosted", "ci-pool"], "runner_group_id": 1},
        {"name": "ci-202", "labels": ["ci-pool", "x64", "extra"], "runner_group_id": 1},
    ]
    assert all(p["authorized"] for p in posts)
    assert started_names(docker) == ["ci-runner-101", "ci-runner-202"]


def test_the_request_is_made_before_the_container_starts(
        scaler, github, docker, config, monkeypatch):
    monkeypatch.setenv("PATH", docker.env()["PATH"])
    monkeypatch.setenv("FAKE_DOCKER_DIR", docker.env()["FAKE_DOCKER_DIR"])
    queue_job(github, 1, 101, ["ci-pool"])
    queue_job(github, 1, 102, ["ci-pool"])
    cfg = scaler.load_config(config)
    seen = []

    class Recording(scaler.Docker):
        def start(self, name, image, jit_config, profile, **kwargs):
            seen.append((name, [p["body"]["name"] for p in jit_posts(github)]))
            super().start(name, image, jit_config, profile, **kwargs)

    scaler.run_pass(cfg, scaler.GitHubApi(cfg), Recording(cfg))
    assert seen == [("ci-runner-101", ["ci-101"]),
                    ("ci-runner-102", ["ci-101", "ci-102"])]


def test_a_dry_run_and_a_pass_over_a_served_queue_request_no_jit_config(
        github, docker, config):
    queue_job(github, 1, 101, ["ci-pool"])
    once(docker, config, "--dry-run")
    assert jit_posts(github) == []
    assert docker.commands("run", "create") == []
    once(docker, config)
    assert len(jit_posts(github)) == 1
    once(docker, config)
    once(docker, config, "--dry-run")
    assert len(jit_posts(github)) == 1
    assert started_names(docker) == ["ci-runner-101"]


@pytest.mark.parametrize("status,headers", [
    (500, {}), (502, {}), (403, {"Retry-After": "30"}), (429, {"Retry-After": "7"})])
def test_a_failed_generate_jitconfig_ends_the_pass_and_the_next_pass_retries(
        github, docker, config, status, headers):
    queue_job(github, 1, 101, ["ci-pool"])
    queue_job(github, 1, 102, ["ci-pool"])
    github.fail_next(1, status, headers, path_contains="generate-jitconfig")
    done = once(docker, config, expect=1)
    # The pass ended at the failed call: no container, no second request.
    assert started_names(docker) == []
    assert [p["body"]["name"] for p in jit_posts(github)] == ["ci-101"]
    assert "job 101" in done.stderr
    once(docker, config)
    assert started_names(docker) == ["ci-runner-101", "ci-runner-102"]
    assert [p["body"]["name"] for p in jit_posts(github)] == ["ci-101", "ci-101", "ci-102"]


def test_a_failed_request_does_not_use_up_a_concurrency_slot(github, docker, tmp_path):
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    cfg = tmp_path / "capped.yaml"
    cfg.write_text(f"repo: {github.repo}\ntoken_file: {token}\nhost_cpus: 64\n"
                   f"api_url: {github.url}\nimage: ci-runner:test\nmax_concurrency: 1\n")
    queue_job(github, 1, 101, ["ci-pool"])
    queue_job(github, 1, 102, ["ci-pool"])
    github.fail_next(1, 500, path_contains="generate-jitconfig")
    once(docker, cfg, expect=1)
    assert started_names(docker) == []
    once(docker, cfg)  # the slot was free: the oldest job starts
    assert started_names(docker) == ["ci-runner-101"]


def test_a_request_specific_rejection_skips_only_that_job(github, docker, config):
    # A 422 (say, the runner name is taken) is about this job alone, not an
    # outage: the other jobs of the pass are still served.
    queue_job(github, 1, 101, ["ci-pool"])
    queue_job(github, 1, 102, ["ci-pool"])
    github.fail_next(1, 422, path_contains="generate-jitconfig")
    done = once(docker, config)
    assert started_names(docker) == ["ci-runner-102"]
    assert "job 101" in done.stderr


def test_the_request_goes_to_the_generate_jitconfig_endpoint_of_the_configured_repo(
        github, docker, config):
    queue_job(github, 1, 101, ["ci-pool"])
    once(docker, config)
    (post,) = jit_posts(github)
    assert post["method"] == "POST"
    assert post["path"] == JIT_PATH
    assert post["path"].startswith(f"/repos/{github.repo}/actions/runners/")
    assert post["path"].endswith("/generate-jitconfig")
    assert set(post["body"]) == {"name", "labels", "runner_group_id"}
    assert isinstance(post["body"]["runner_group_id"], int)
    assert post["body"]["runner_group_id"] == 1
    assert post["body"]["name"] == "ci-101"
    assert post["body"]["labels"] == ["ci-pool"]
