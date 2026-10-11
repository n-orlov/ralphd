"""A failed or timed-out GitHub API call ends the scaler's current pass (A.6).

Whatever the call is -- the queue discovery, one run's job list, a job read for
reaping, or the JIT request -- and whatever the failure is (a timeout, a 5xx, a
403 or a 429), the pass stops there: it makes no further API request, starts no
further container, and keeps every container already running. The loop retries
on its next pass after the failure backoff, and that pass serves the whole queue
once each. These tests assert the stop and the retry, never work after the
failed call. Driven in-process against the fake GitHub API and the fake
`docker`, with an injected clock and sleep (nothing really waits)."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
T0 = 1_800_000_000.0
POLL = 5
JIT = "generate-jitconfig"
FAILURES = [(500, {}), (502, {}), (503, {}), (403, {"x-ratelimit-reset": "1"}),
            (429, {"Retry-After": "7"})]


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_pass_failures", SCALER)
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
        self.waits: list[float] = []
        docker.set_clock(T0)
        docker.add_image(scaler.image_ref(self.cfg))

    def run_pass(self, **kwargs):
        return self.scaler.run_pass(self.cfg, self.api, self.dock, clock=lambda: T0,
                                    out=lambda _: None, **kwargs)

    def loop(self, passes):
        self.scaler.run_loop(self.cfg, self.api, self.dock, passes=passes,
                             sleep=self.waits.append, clock=lambda: T0)

    def created(self):
        return [argv[argv.index("--name") + 1] for argv in self.docker.commands("create")]

    def calls(self):
        return [(r["method"], r["path"]) for r in self.github.requests]

    def heal(self):
        self.github._failures.clear()


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
        path.write_text(
            f"repo: {github.repo}\ntoken_file: {token}\nhost_cpus: 64\n"
            f"api_url: {github.url}\nimage: ci-runner:test\npoll_interval_s: {POLL}\n"
            f"admission_lock_file: {tmp_path / 'admission.lock'}\n"
            f"lock_file: {tmp_path / 'build.lock'}\nmax_concurrency: 8\n")
        yield Rig(scaler, github, docker, path)


def queue(github, *jobs, status="queued"):
    """A multi-job queue: `jobs` is (run id, job id) pairs."""
    for run_id, job_id in jobs:
        if not any(r["id"] == run_id for r in github.runs):
            github.add_run(run_id)
        github.add_job(run_id, job_id, ["ci-pool"], status=status)


def assert_next_pass_serves_everything_once(rig, jobs):
    rig.heal()
    rig.run_pass()
    rig.run_pass()  # idempotent: no job is started twice
    assert rig.created() == [f"ci-runner-{j}" for j in jobs]


# ---- queue discovery -------------------------------------------------------

@pytest.mark.parametrize("status,headers", FAILURES)
def test_a_failed_run_listing_ends_the_pass_with_no_job_work(
        rig, scaler, status, headers):
    queue(rig.github, (1, 101), (2, 102), (3, 103))
    rig.docker.add_container("ci-runner-900", created_epoch=T0)
    rig.github.fail_next(100, status, headers, path_contains="/actions/runs")
    with pytest.raises(scaler.ApiError):
        rig.run_pass()
    assert len(rig.calls()) == 1  # the listing; nothing after it
    assert rig.created() == []
    assert sorted(rig.docker.containers()) == ["ci-runner-900"]
    assert_next_pass_serves_everything_once(rig, [101, 102, 103])


@pytest.mark.parametrize("status,headers", FAILURES)
def test_a_failed_job_list_of_the_first_run_skips_the_other_runs(
        rig, scaler, status, headers):
    queue(rig.github, (1, 101), (2, 102), (3, 103))
    rig.github.fail_next(100, status, headers, path_contains="/actions/runs/1/jobs")
    with pytest.raises(scaler.ApiError):
        rig.run_pass()
    job_lists = [p for _, p in rig.calls() if "/jobs" in p]
    assert job_lists == [f"/repos/{rig.github.repo}/actions/runs/1/jobs"]
    assert rig.created() == []
    assert_next_pass_serves_everything_once(rig, [101, 102, 103])


@pytest.mark.parametrize("failing_run", [2, 3])
def test_a_failed_job_list_in_the_middle_of_the_queue_starts_nothing_even_for_earlier_runs(
        rig, scaler, failing_run):
    queue(rig.github, (1, 101), (2, 102), (3, 103))
    rig.github.fail_next(100, 503, path_contains=f"/actions/runs/{failing_run}/jobs")
    with pytest.raises(scaler.ApiError):
        rig.run_pass()
    assert rig.created() == []  # the queue was never complete, so nothing started
    assert not any(m == "POST" for m, _ in rig.calls())
    assert_next_pass_serves_everything_once(rig, [101, 102, 103])


# ---- job reads (reaping) ---------------------------------------------------

@pytest.mark.parametrize("status,headers", FAILURES)
def test_a_failed_job_read_ends_the_pass_before_any_start_and_keeps_containers(
        rig, scaler, status, headers):
    queue(rig.github, (1, 201), (1, 202), (2, 203))
    queue(rig.github, (9, 101), (9, 102), status="in_progress")
    for job in (101, 102):
        rig.docker.add_container(f"ci-runner-{job}", created_epoch=T0)
    rig.github.fail_next(100, status, headers, path_contains="/actions/jobs/")
    with pytest.raises(scaler.ApiError):
        rig.run_pass()
    reads = [p for _, p in rig.calls() if "/actions/jobs/" in p]
    assert len(reads) == 1  # the second container's job was not read
    assert rig.created() == []
    assert rig.docker.commands("rm") == []
    assert sorted(rig.docker.containers()) == ["ci-runner-101", "ci-runner-102"]
    rig.heal()
    rig.run_pass()
    assert rig.created() == ["ci-runner-201", "ci-runner-202", "ci-runner-203"]


# ---- JIT generation --------------------------------------------------------

@pytest.mark.parametrize("status,headers", FAILURES)
def test_a_failed_jit_request_on_the_first_job_of_three_ends_the_pass(
        rig, scaler, status, headers):
    queue(rig.github, (1, 101), (1, 102), (2, 103))
    rig.github.fail_next(100, status, headers, path_contains=JIT)
    with pytest.raises(scaler.ApiError):
        rig.run_pass()
    posts = rig.github.requests_to("POST")
    assert [p["body"]["name"] for p in posts] == ["ci-101"]
    assert rig.created() == []
    assert_next_pass_serves_everything_once(rig, [101, 102, 103])


@pytest.mark.parametrize("status,headers", FAILURES)
def test_a_jit_failure_after_a_start_keeps_that_container_and_asks_for_no_more(
        rig, scaler, status, headers):
    queue(rig.github, (1, 101), (1, 102), (2, 103))
    original = rig.github._route
    seen = {"posts": 0}

    def route(method, path, query, body):
        if method == "POST":
            seen["posts"] += 1
            if seen["posts"] == 2:
                return status, {"message": "fake failure"}
        return original(method, path, query, body)

    rig.github._route = route
    with pytest.raises(scaler.ApiError):
        rig.run_pass()
    assert rig.created() == ["ci-runner-101"]
    assert [p["body"]["name"] for p in rig.github.requests_to("POST")] == [
        "ci-101", "ci-102"]
    assert sorted(rig.docker.containers()) == ["ci-runner-101"]
    rig.heal()
    rig.run_pass()
    rig.run_pass()
    assert rig.created() == ["ci-runner-101", "ci-runner-102", "ci-runner-103"]


# ---- a call that times out or never gets a reply ---------------------------

@pytest.mark.parametrize("exc", [socket.timeout("timed out"),
                                 ConnectionResetError("reset by peer")])
@pytest.mark.parametrize("after_calls", [0, 1, 2, 3])
def test_a_timeout_at_any_call_of_a_pass_ends_it_with_no_further_api_work(
        rig, scaler, monkeypatch, exc, after_calls):
    # Calls of the pass over this queue: runs list, run 1 jobs, run 2 jobs, JIT.
    queue(rig.github, (1, 101), (2, 102))
    real = scaler.urllib.request.urlopen
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] > after_calls:
            raise exc
        return real(*args, **kwargs)

    monkeypatch.setattr(scaler.urllib.request, "urlopen", flaky)
    with pytest.raises(scaler.ApiError) as caught:
        rig.run_pass()
    assert caught.value.status is None and caught.value.retryable
    assert state["calls"] == after_calls + 1  # the timed-out call was the last
    assert rig.created() == []
    monkeypatch.setattr(scaler.urllib.request, "urlopen", real)
    rig.run_pass()
    assert rig.created() == ["ci-runner-101", "ci-runner-102"]


# ---- the loop retries on the next pass using the failure backoff ------------

def test_the_loop_retries_a_failed_pass_after_the_failure_backoff(rig):
    queue(rig.github, (1, 101), (1, 102))
    rig.github.fail_next(2, 503, path_contains=JIT)
    rig.loop(passes=3)
    assert rig.waits == [POLL, 2 * POLL]  # one wait after each failed pass
    assert [p["body"]["name"] for p in rig.github.requests_to("POST")] == [
        "ci-101", "ci-101", "ci-101", "ci-102"]
    assert rig.created() == ["ci-runner-101", "ci-runner-102"]


def test_a_rate_limit_at_a_job_read_makes_the_loop_wait_as_long_as_the_reply_says(rig):
    queue(rig.github, (9, 101), status="in_progress")
    rig.docker.add_container("ci-runner-101", created_epoch=T0)
    rig.github.fail_next(1, 429, {"Retry-After": "17"}, path_contains="/actions/jobs/")
    rig.loop(passes=2)
    assert rig.waits == [17]
    assert sorted(rig.docker.containers()) == ["ci-runner-101"]


# ---- a reply that is about one request only does not end the pass ----------

def test_a_404_on_one_job_read_keeps_that_container_and_is_not_an_outage(rig):
    queue(rig.github, (9, 101), (9, 102), status="completed")
    queue(rig.github, (1, 201))
    for job in (101, 102):
        rig.docker.add_container(f"ci-runner-{job}", created_epoch=T0)
    rig.github.fail_next(1, 404, path_contains="/actions/jobs/")
    rig.run_pass()
    assert rig.api.last_failure is None  # not outage-shaped: no backoff
    assert rig.docker.commands("rm") == [["rm", "--force", "ci-runner-102"]]
    assert rig.created() == ["ci-runner-201"]
