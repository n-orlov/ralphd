"""The runner scaler's start deadline and reaping: a container whose job the
API keeps `queued` past `start_deadline_s`, a container whose job is
`completed`, one older than `job_deadline_s` and one that has exited are each
removed by exact name
(`docker rm --force ci-runner-<job id>`), never by filter, prune or pattern.
Time is an injected clock: the fake docker stamps containers from it and the
scaler's passes read it, so nothing really waits."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
T0 = 1_800_000_000.0


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_reaping", SCALER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Rig:
    def __init__(self, scaler, github, docker, config_path):
        self.scaler = scaler
        self.github = github
        self.docker = docker
        self.cfg = scaler.load_config(config_path)
        self.api = scaler.GitHubApi(self.cfg)
        self.dock = scaler.Docker(self.cfg)
        self.now = T0
        self.docker.set_clock(self.now)

    def advance(self, seconds):
        self.now += seconds
        self.docker.set_clock(self.now)

    def run_pass(self, **kwargs):
        return self.scaler.run_pass(self.cfg, self.api, self.dock,
                                    clock=lambda: self.now, out=lambda _: None,
                                    **kwargs)


@pytest.fixture()
def rig(tmp_path, monkeypatch, scaler):
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    docker = FakeDocker(tmp_path / "fakedocker")
    for key, value in docker.env().items():
        if key in ("PATH", "FAKE_DOCKER_DIR"):
            monkeypatch.setenv(key, value)
    with FakeGitHub() as github:
        path = tmp_path / "scaler.yaml"
        path.write_text(f"repo: {github.repo}\ntoken_file: {token}\nhost_cpus: 64\n"
                        f"api_url: {github.url}\nimage: ci-runner:test\n")
        yield Rig(scaler, github, docker, path)


def queue_job(github, run_id, job_id, status="queued"):
    if not any(r["id"] == run_id for r in github.runs):
        github.add_run(run_id)
    github.add_job(run_id, job_id, ["ci-pool"], status=status)


def removals(docker):
    return docker.commands("rm")


def test_defaults_come_from_the_loaded_default_config(tmp_path, scaler):
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    path = tmp_path / "scaler.yaml"
    path.write_text(f"repo: n-orlov/ralphd\ntoken_file: {token}\nhost_cpus: 64\n")
    cfg = scaler.load_config(path)
    assert cfg["start_deadline_s"] == 120
    assert cfg["job_deadline_s"] == 7800


def test_a_job_queued_past_start_deadline_s_loses_its_container_and_is_restarted(rig):
    queue_job(rig.github, 1, 101)
    assert rig.run_pass() == 1
    assert sorted(rig.docker.containers()) == ["ci-runner-101"]
    rig.advance(rig.cfg["start_deadline_s"] - 1)
    rig.run_pass()
    assert removals(rig.docker) == []

    rig.advance(2)  # now past the start deadline, job still queued
    rig.run_pass()
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-101"]]
    assert rig.docker.containers() == {}

    assert rig.run_pass() == 1  # the next pass starts a new container
    assert sorted(rig.docker.containers()) == ["ci-runner-101"]
    runs = [argv for argv in rig.docker.commands("create") if "ci-runner-101" in argv]
    assert len(runs) == 2


def test_a_container_whose_job_completed_is_removed_by_exact_name(rig):
    queue_job(rig.github, 1, 101)
    queue_job(rig.github, 1, 102)
    rig.run_pass()
    rig.github.set_job_status(101, "completed")
    rig.github.set_job_status(102, "in_progress")
    rig.advance(30)
    rig.run_pass()
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-101"]]
    assert sorted(rig.docker.containers()) == ["ci-runner-102"]


def test_a_container_older_than_job_deadline_s_is_removed_whatever_its_job_says(rig):
    queue_job(rig.github, 1, 101, status="in_progress")
    rig.docker.add_container("ci-runner-101", created_epoch=T0)
    rig.advance(rig.cfg["job_deadline_s"] - 1)
    rig.run_pass()
    assert removals(rig.docker) == []
    rig.advance(2)
    rig.run_pass()
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-101"]]
    assert rig.docker.containers() == {}


def test_a_running_job_and_foreign_containers_are_left_alone(rig):
    queue_job(rig.github, 1, 101, status="in_progress")
    rig.docker.add_container("ci-runner-101", created_epoch=T0)
    rig.docker.add_container("standing-runner", created_epoch=T0 - 10**6)
    rig.advance(rig.cfg["start_deadline_s"] * 5)
    rig.run_pass()
    assert removals(rig.docker) == []
    assert sorted(rig.docker.containers()) == ["ci-runner-101", "standing-runner"]


def test_a_failing_job_lookup_keeps_the_container(rig):
    queue_job(rig.github, 1, 101)
    rig.docker.add_container("ci-runner-101", created_epoch=T0)
    rig.advance(rig.cfg["start_deadline_s"] + 10)
    rig.github.jobs[1].clear()  # the API no longer knows the job: 404
    rig.run_pass()
    assert removals(rig.docker) == []


def test_a_dry_run_names_the_removal_and_issues_none(rig):
    queue_job(rig.github, 1, 101)
    rig.docker.add_container("ci-runner-101", created_epoch=T0)
    rig.advance(rig.cfg["start_deadline_s"] + 10)
    lines = []
    rig.scaler.run_pass(rig.cfg, rig.api, rig.dock, dry_run=True,
                        clock=lambda: rig.now, out=lines.append)
    assert any(line.startswith("would remove ci-runner-101") for line in lines)
    assert removals(rig.docker) == []
    assert sorted(rig.docker.containers()) == ["ci-runner-101"]


def test_every_removal_argv_names_one_exact_container_and_no_selector(rig):
    for job_id in (101, 102, 103):
        queue_job(rig.github, 1, job_id)
    rig.run_pass()
    rig.github.set_job_status(101, "completed")
    rig.advance(rig.cfg["start_deadline_s"] + 1)  # 102 and 103 are overdue starts
    rig.run_pass()
    rig.run_pass()  # restarts 102 and 103
    rig.github.set_job_status(102, "in_progress")
    rig.advance(rig.cfg["job_deadline_s"] + 1)  # 102 is over job_deadline_s
    rig.run_pass()

    argv_log = rig.docker.argv_log()
    removed = [argv for argv in argv_log if argv[0] in ("rm", "stop", "kill", "rmi")
               or argv[:2] in (["container", "rm"], ["container", "prune"])]
    assert len(removed) >= 4
    for argv in removed:
        assert argv[0] == "rm"
        operands = [a for a in argv[1:] if not a.startswith("-")]
        assert len(operands) == 1
        assert re.fullmatch(r"ci-runner-\d+", operands[0])
        assert set(argv[1:]) - set(operands) <= {"--force"}
    for argv in argv_log:
        for arg in argv:
            assert "--filter" not in arg and "*" not in arg
            assert "prune" not in arg and "label" not in arg
            assert arg != "-f"


# ---- the hard deadline when job-status reads are unavailable ---------------

OUTAGES = [(503, {}), (403, {"x-ratelimit-reset": "1"}), (429, {"Retry-After": "7"})]


def job_reads(github):
    return github.requests_to("GET", f"/repos/{github.repo}/actions/jobs/")


@pytest.mark.parametrize("status,headers", OUTAGES)
def test_job_deadline_s_removes_an_overdue_container_while_job_reads_fail(
        rig, status, headers):
    queue_job(rig.github, 1, 101, status="in_progress")
    queue_job(rig.github, 1, 102, status="queued")
    rig.docker.add_container("ci-runner-101", created_epoch=T0)
    rig.advance(rig.cfg["job_deadline_s"] + 1)
    rig.docker.add_container("ci-runner-102", created_epoch=T0)  # same age: overdue too
    rig.github.fail_next(100, status, headers, path_contains="/actions/jobs/")
    rig.run_pass()
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-101"],
                                    ["rm", "--force", "ci-runner-102"]]
    assert job_reads(rig.github) == []  # the hard deadline asked the API nothing


@pytest.mark.parametrize("status,headers", OUTAGES)
def test_an_unavailable_job_read_alone_never_removes_a_container_under_the_deadline(
        rig, status, headers):
    queue_job(rig.github, 1, 101, status="in_progress")
    queue_job(rig.github, 1, 102, status="queued")
    queue_job(rig.github, 1, 103, status="completed")
    for job in (101, 102, 103):
        rig.docker.add_container(f"ci-runner-{job}", created_epoch=T0)
    # Past start_deadline_s and job 103 is completed, but job_deadline_s is far off.
    rig.advance(rig.cfg["job_deadline_s"] - 1)
    rig.github.fail_next(100, status, headers, path_contains="/actions/jobs/")
    rig.run_pass()
    assert removals(rig.docker) == []
    assert sorted(rig.docker.containers()) == [
        "ci-runner-101", "ci-runner-102", "ci-runner-103"]
    # The API returns: the same containers are then reaped on the read.
    rig.github._failures.clear()
    rig.run_pass()
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-102"],
                                    ["rm", "--force", "ci-runner-103"]]
    assert sorted(rig.docker.containers()) == ["ci-runner-101"]


def test_the_hard_deadline_holds_when_the_queue_itself_cannot_be_read(rig, scaler):
    queue_job(rig.github, 1, 101, status="in_progress")
    queue_job(rig.github, 1, 102, status="in_progress")
    rig.docker.add_container("ci-runner-101", created_epoch=T0)
    rig.advance(rig.cfg["job_deadline_s"] - 5)
    rig.docker.add_container("ci-runner-102", created_epoch=T0 + 100)  # younger
    rig.advance(10)
    rig.github.fail_next(100, 503)
    with pytest.raises(scaler.ScalerError):
        rig.run_pass()
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-101"]]
    assert sorted(rig.docker.containers()) == ["ci-runner-102"]
    assert rig.docker.commands("create") == []  # an outage starts nothing


def test_a_dry_run_during_a_job_read_outage_names_only_the_overdue_container(rig):
    queue_job(rig.github, 1, 101, status="in_progress")
    queue_job(rig.github, 1, 102, status="in_progress")
    rig.docker.add_container("ci-runner-101", created_epoch=T0)
    rig.advance(rig.cfg["job_deadline_s"] - 5)
    rig.docker.add_container("ci-runner-102", created_epoch=T0 + 100)
    rig.advance(10)
    rig.github.fail_next(100, 503, path_contains="/actions/jobs/")
    lines = []
    rig.scaler.run_pass(rig.cfg, rig.api, rig.dock, dry_run=True,
                        clock=lambda: rig.now, out=lines.append)
    assert [ln for ln in lines if ln.startswith("would remove")] == [
        "would remove ci-runner-101: past job_deadline_s"]
    assert removals(rig.docker) == []


# ---- exit cleanup: unconditional, needs no job read (B.4) -------------------

ALL_JOB_STATES = ["queued", "in_progress", "completed"]


@pytest.mark.parametrize("job_state", ALL_JOB_STATES)
@pytest.mark.parametrize("container_state", ["exited", "dead"])
def test_an_exited_container_is_removed_at_once_whatever_its_job_says(
        rig, job_state, container_state):
    queue_job(rig.github, 1, 101, status=job_state)
    rig.docker.add_container("ci-runner-101", created_epoch=T0, state=container_state)
    rig.advance(1)  # far below start_deadline_s and job_deadline_s
    rig.run_pass()
    assert removals(rig.docker)[0] == ["rm", "--force", "ci-runner-101"]
    assert all(argv[-1] != "ci-runner-101" for argv in removals(rig.docker)[1:])


@pytest.mark.parametrize("status,headers", OUTAGES)
def test_exit_cleanup_holds_while_job_reads_fail_and_reads_no_job(
        rig, status, headers):
    queue_job(rig.github, 1, 101, status="in_progress")
    queue_job(rig.github, 1, 102, status="in_progress")
    rig.docker.add_container("ci-runner-101", created_epoch=T0, state="exited")
    rig.docker.add_container("ci-runner-102", created_epoch=T0)  # running, young
    rig.advance(1)
    rig.github.fail_next(100, status, headers, path_contains="/actions/jobs/")
    rig.run_pass()
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-101"]]
    assert sorted(rig.docker.containers()) == ["ci-runner-102"]
    # The only job read was for the running container, and it failed: exit
    # cleanup itself read nothing, and the outage removed nothing.
    assert [r["path"].rsplit("/", 1)[-1] for r in job_reads(rig.github)] == ["102"]


@pytest.mark.parametrize("status,headers", OUTAGES)
def test_a_queue_outage_removes_overdue_and_exited_containers_and_keeps_running_ones(
        rig, scaler, status, headers):
    for job in (801, 802, 803):
        queue_job(rig.github, 8, job, status="in_progress")
    rig.docker.add_container("ci-runner-803", created_epoch=T0)  # will be overdue
    rig.advance(rig.cfg["job_deadline_s"] + 1)
    rig.docker.add_container("ci-runner-801", created_epoch=rig.now, state="exited")
    rig.docker.add_container("ci-runner-802", created_epoch=rig.now)  # running, young
    rig.docker.add_container("ci-runner-foreign", created_epoch=T0, state="exited")
    rig.docker.add_container("buildkit", created_epoch=T0, state="exited")
    rig.advance(1)
    rig.github.fail_next(100, status, headers)  # every API call fails
    with pytest.raises(scaler.ScalerError):
        rig.run_pass()
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-801"],
                                    ["rm", "--force", "ci-runner-803"]]
    assert sorted(rig.docker.containers()) == [
        "buildkit", "ci-runner-802", "ci-runner-foreign"]
    assert job_reads(rig.github) == []
    assert rig.docker.commands("create") == []


@pytest.mark.parametrize("container_state", ["created", "paused", "restarting"])
def test_a_container_that_has_not_exited_is_kept_through_a_job_read_outage(
        rig, container_state):
    queue_job(rig.github, 1, 101, status="completed")
    rig.docker.add_container("ci-runner-101", created_epoch=T0, state=container_state)
    rig.advance(rig.cfg["job_deadline_s"] - 1)
    rig.github.fail_next(100, 503, path_contains="/actions/jobs/")
    rig.run_pass()
    assert removals(rig.docker) == []
    assert sorted(rig.docker.containers()) == ["ci-runner-101"]


def test_an_exited_container_of_a_queued_job_is_replaced_on_the_next_pass(rig):
    queue_job(rig.github, 1, 101, status="queued")
    rig.docker.add_container("ci-runner-101", created_epoch=T0, state="exited")
    rig.advance(1)
    assert rig.run_pass() == 0  # removed, but never restarted in the same pass
    assert removals(rig.docker) == [["rm", "--force", "ci-runner-101"]]
    assert rig.docker.containers() == {}
    assert rig.run_pass() == 1
    assert sorted(rig.docker.containers()) == ["ci-runner-101"]
    assert len(rig.docker.commands("create")) == 1


def test_a_dry_run_names_the_exit_cleanup_during_an_outage_and_issues_none(rig):
    queue_job(rig.github, 1, 101, status="in_progress")
    queue_job(rig.github, 1, 102, status="in_progress")
    rig.docker.add_container("ci-runner-101", created_epoch=T0, state="exited")
    rig.docker.add_container("ci-runner-102", created_epoch=T0)
    rig.advance(1)
    rig.github.fail_next(100, 429, {"Retry-After": "7"}, path_contains="/actions/jobs/")
    lines = []
    rig.scaler.run_pass(rig.cfg, rig.api, rig.dock, dry_run=True,
                        clock=lambda: rig.now, out=lines.append)
    assert [ln for ln in lines if ln.startswith("would remove")] == [
        "would remove ci-runner-101: exited"]
    assert removals(rig.docker) == []
    assert sorted(rig.docker.containers()) == ["ci-runner-101", "ci-runner-102"]
