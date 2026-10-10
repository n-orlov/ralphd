"""The runner scaler's trust rule (A.8): `is_trusted(run)` is the only decision
about whose jobs get a container. A fork's pull request, `pull_request_target`
and any other event get none (and a logged skip); a same-repo run of an allowed
event gets one. Driven end to end against the fake GitHub API and fake `docker`,
and directly on the predicate."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
OWN = "n-orlov/ralphd"
FORK = "someone-else/ralphd"


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_trust", SCALER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def github():
    with FakeGitHub(OWN) as fake:
        yield fake


@pytest.fixture()
def docker(tmp_path):
    return FakeDocker(tmp_path / "fakedocker")


def once(tmp_path, github, docker):
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    config = tmp_path / "scaler.yaml"
    config.write_text(f"repo: {github.repo}\ntoken_file: {token}\n"
                      f"api_url: {github.url}\nimage: ci-runner:test\n")
    done = subprocess.run(
        [sys.executable, str(SCALER), "--config", str(config), "once"],
        capture_output=True, text=True, env=docker.env(), timeout=60)
    assert done.returncode == 0, done.stderr
    return done


def started(docker):
    return [argv[argv.index("--name") + 1] for argv in docker.commands("run", "create")]


def run_of(event, head=OWN):
    return {"id": 1, "event": event, "repository": {"full_name": OWN},
            "head_repository": {"full_name": head}}


def test_a_fork_pull_request_job_gets_no_container_and_a_logged_skip(
        tmp_path, github, docker):
    github.add_run(7, event="pull_request", head_repo=FORK)
    github.add_job(7, 701, ["ci-pool"])
    done = once(tmp_path, github, docker)
    assert started(docker) == []
    assert github.requests_to("POST", "/repos") == []  # no JIT config either
    assert "skipping run 7" in done.stderr and "not trusted" in done.stderr
    assert FORK in done.stderr


def test_a_same_repo_pull_request_job_gets_a_container(tmp_path, github, docker):
    github.add_run(8, event="pull_request", head_repo=OWN)
    github.add_job(8, 801, ["ci-pool"])
    once(tmp_path, github, docker)
    assert started(docker) == ["ci-runner-801"]


def test_only_the_trusted_run_of_a_mixed_queue_is_served(tmp_path, github, docker):
    github.add_run(7, event="pull_request", head_repo=FORK)
    github.add_job(7, 701, ["ci-pool"])
    github.add_run(8, event="pull_request")
    github.add_job(8, 801, ["ci-pool"])
    once(tmp_path, github, docker)
    assert started(docker) == ["ci-runner-801"]


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule",
                                   "pull_request"])
def test_own_repo_runs_of_the_allowed_events_are_trusted(scaler, event):
    assert scaler.is_trusted(run_of(event)) is True


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_end_to_end_own_repo_push_dispatch_and_schedule_get_a_container(
        tmp_path, github, docker, event):
    github.add_run(9, event=event)
    github.add_job(9, 901, ["ci-pool"])
    once(tmp_path, github, docker)
    assert started(docker) == ["ci-runner-901"]


def test_pull_request_target_is_not_trusted_even_from_the_own_repo(
        scaler, tmp_path, github, docker):
    assert scaler.is_trusted(run_of("pull_request_target")) is False
    github.add_run(10, event="pull_request_target")
    github.add_job(10, 1001, ["ci-pool"])
    once(tmp_path, github, docker)
    assert started(docker) == []


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule",
                                   "pull_request"])
def test_a_run_from_another_head_repository_is_never_trusted(scaler, event):
    assert scaler.is_trusted(run_of(event, head=FORK)) is False


@pytest.mark.parametrize("event", ["issues", "workflow_run", "release",
                                   "issue_comment", "", None])
def test_other_events_are_not_trusted(scaler, event):
    assert scaler.is_trusted(run_of(event)) is False


def test_a_run_without_repository_information_fails_closed(scaler):
    assert scaler.is_trusted({"id": 1, "event": "push"}) is False
    assert scaler.is_trusted({"id": 1, "event": "push", "repository": None,
                              "head_repository": None}) is False
    assert scaler.is_trusted({"id": 1, "event": "pull_request",
                              "repository": {"full_name": OWN},
                              "head_repository": None}) is False
