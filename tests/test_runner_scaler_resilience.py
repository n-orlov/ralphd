"""The runner scaler survives GitHub API failures (A.6): a pass whose call
failed (connection error, timeout, 5xx, 403, 429) is skipped, the loop waits
longer (exponential from `poll_interval_s`, capped at 60 s, or as long as
`Retry-After` / `x-ratelimit-reset` say), and it neither starts a job twice,
misses one, removes a running container, nor lets an exception out of the
`run` loop. Sleep and the clock are injected; nothing really waits."""

from __future__ import annotations

import importlib.util
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
T0 = 1_800_000_000.0
POLL = 5


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_resilience", SCALER)
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

    def run(self, passes):
        self.scaler.run_loop(self.cfg, self.api, self.dock, passes=passes,
                             sleep=self.waits.append, clock=lambda: T0)


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
                        f"api_url: {github.url}\nimage: ci-runner:test\n"
                        f"poll_interval_s: {POLL}\n")
        yield Rig(scaler, github, docker, path)


def fail_first_call_with(monkeypatch, scaler, exc):
    """Make the next urlopen call raise `exc`, then behave normally."""
    real = scaler.urllib.request.urlopen
    state = {"left": 1}

    def flaky(*args, **kwargs):
        if state["left"]:
            state["left"] -= 1
            raise exc
        return real(*args, **kwargs)

    monkeypatch.setattr(scaler.urllib.request, "urlopen", flaky)


def test_four_failures_then_recovery_starts_each_queued_job_exactly_once(
        rig, scaler, monkeypatch):
    rig.github.add_run(1)
    rig.github.add_job(1, 101, ["ci-pool"], status="in_progress")
    rig.github.add_job(1, 102, ["ci-pool"])
    rig.github.add_job(1, 103, ["ci-pool"])
    rig.docker.add_container("ci-runner-101", created_epoch=T0 - 30)
    # Four failed passes: a timeout, a 5xx, a 403 and a 429.
    fail_first_call_with(monkeypatch, scaler, socket.timeout("timed out"))
    rig.github.fail_next(1, 503)
    rig.github.fail_next(1, 403, {"x-ratelimit-reset": str(int(T0) + 30)})
    rig.github.fail_next(1, 429, {"Retry-After": "11"})

    rig.run(passes=4)
    assert rig.docker.commands("rm") == []
    assert rig.docker.commands("create") == []
    assert "ci-runner-101" in rig.docker.containers()

    rig.run(passes=3)  # recovered, then two idle passes
    runs = rig.docker.commands("create")
    started = sorted(argv[argv.index("--name") + 1] for argv in runs)
    assert started == ["ci-runner-102", "ci-runner-103"]  # one each, none doubled
    assert rig.docker.commands("rm") == []  # job 101 still runs: nothing reaped
    assert len(rig.github.requests_to("POST", "/repos/n-orlov/ralphd/actions/runners")) == 2


def test_waits_grow_exponentially_from_poll_interval_and_cap_at_60(rig):
    rig.github.fail_next(7, 503)
    rig.run(passes=9)
    assert rig.waits[:7] == [5, 10, 20, 40, 60, 60, 60]
    assert rig.waits[7] == POLL  # recovered: back to the normal poll interval


def test_waits_are_the_poll_interval_when_nothing_fails(rig):
    rig.run(passes=3)
    assert rig.waits == [POLL, POLL]


def test_retry_after_on_429_and_403_sets_the_next_wait(rig):
    rig.github.fail_next(1, 429, {"Retry-After": "17"})
    rig.github.fail_next(1, 403, {"Retry-After": "23"})
    rig.run(passes=3)
    assert rig.waits == [17, 23]


def test_ratelimit_reset_on_403_sets_the_next_wait(rig):
    rig.github.fail_next(1, 403, {"x-ratelimit-reset": str(int(T0) + 42)})
    rig.run(passes=2)
    assert rig.waits == [42]


def test_retry_after_wins_over_ratelimit_reset(rig):
    rig.github.fail_next(1, 429, {"Retry-After": "9",
                                  "x-ratelimit-reset": str(int(T0) + 500)})
    rig.run(passes=2)
    assert rig.waits == [9]


def test_a_bogus_huge_rate_limit_header_cannot_park_the_loop(rig, scaler):
    rig.github.fail_next(1, 429, {"Retry-After": "999999"})
    rig.run(passes=2)
    assert rig.waits == [scaler.RATE_LIMIT_MAX_WAIT_S]


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_a_4xx_reply_backs_off_like_any_failed_call(rig, status):
    rig.github.fail_next(2, status)
    rig.run(passes=4)
    assert rig.waits == [POLL, 2 * POLL, POLL]  # one clean pass resets it


def test_a_failed_job_read_never_removes_a_running_container(rig):
    # The reap reads the job; with the API failing that read, the container of
    # a job GitHub says is completed must stay until the read succeeds.
    rig.github.add_run(1)
    rig.github.add_job(1, 101, ["ci-pool"], status="completed")
    rig.docker.add_container("ci-runner-101", created_epoch=T0 - 30)
    rig.github.fail_next(3, 503, path_contains="/actions/jobs/")
    rig.run(passes=3)
    assert rig.docker.commands("rm") == []
    assert rig.waits == [5, 10]  # the failed reads back the loop off
    rig.run(passes=1)
    assert [argv[-1] for argv in rig.docker.commands("rm")] == ["ci-runner-101"]


def test_an_unreachable_api_never_lets_an_exception_out_of_the_loop(
        rig, scaler, monkeypatch):
    def refuse(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(scaler.urllib.request, "urlopen", refuse)
    rig.docker.add_container("ci-runner-101", created_epoch=T0 - 30)
    rig.run(passes=5)
    assert rig.waits == [5, 10, 20, 40]
    assert rig.docker.commands("rm") == []
    assert rig.docker.commands("create") == []


def test_retry_after_on_a_503_sets_the_next_wait(rig):
    rig.github.fail_next(1, 503, {"Retry-After": "27"})
    rig.run(passes=2)
    assert rig.waits == [27]


@pytest.mark.parametrize("status", [500, 502, 504])
def test_retry_after_on_any_5xx_sets_the_next_wait(rig, status):
    rig.github.fail_next(1, status, {"Retry-After": "13"})
    rig.run(passes=2)
    assert rig.waits == [13]


def test_ratelimit_reset_on_a_5xx_sets_the_next_wait_when_the_limit_is_spent(rig):
    rig.github.fail_next(1, 502, {"x-ratelimit-reset": str(int(T0) + 33),
                                  "x-ratelimit-remaining": "0"})
    rig.run(passes=2)
    assert rig.waits == [33]


def test_a_5xx_ignores_a_reset_while_requests_remain(rig):
    rig.github.fail_next(1, 503, {"x-ratelimit-reset": str(int(T0) + 900),
                                  "x-ratelimit-remaining": "4000"})
    rig.run(passes=2)
    assert rig.waits == [POLL]  # plain backoff, not the unrelated reset


def test_retry_after_as_an_http_date_on_a_503(rig):
    from email.utils import formatdate
    rig.github.fail_next(1, 503, {"Retry-After": formatdate(T0 + 21, usegmt=True)})
    rig.run(passes=2)
    assert rig.waits == [21]


@pytest.mark.parametrize("value", ["nan", "-5", "soon", ""])
def test_an_unusable_retry_after_falls_back_to_the_backoff(rig, value):
    rig.github.fail_next(2, 503, {"Retry-After": value})
    rig.run(passes=3)
    assert rig.waits == [POLL, 2 * POLL]


def test_a_4xx_other_than_403_429_ignores_retry_after(rig):
    rig.github.fail_next(1, 404, {"Retry-After": "50"})
    rig.run(passes=2)
    assert rig.waits == [POLL]


def test_a_503_with_retry_after_then_recovery_starts_the_queued_job(rig):
    rig.github.add_run(1)
    rig.github.add_job(1, 101, ["ci-pool"])
    rig.github.fail_next(1, 503, {"Retry-After": "19"})
    rig.run(passes=3)
    assert rig.waits == [19, POLL]
    assert len(rig.docker.commands("create")) == 1


def test_a_sustained_failure_streak_recovers_with_the_wait_capped_at_60(rig):
    rig.github.fail_next(40, 503)
    rig.run(passes=42)
    assert rig.waits[:4] == [5, 10, 20, 40]
    assert set(rig.waits[4:40]) == {60}
    assert rig.waits[40] == POLL


@pytest.mark.parametrize("failures", [10, 100, 1023, 1024, 5000, 10**9])
def test_next_wait_is_capped_for_arbitrarily_long_streaks(scaler, failures):
    err = scaler.ApiError("down", status=503)
    assert scaler.next_wait(POLL, failures, err, T0) == 60
    assert scaler.next_wait(0.5, failures, None, T0) == 0.5
    assert scaler.next_wait(1, failures, scaler.ApiError("x"), T0) == 60


def test_next_wait_never_exceeds_the_cap_nor_drops_below_the_poll(scaler):
    for failures in range(1, 30):
        wait = scaler.next_wait(POLL, failures, scaler.ApiError("x"), T0)
        assert POLL <= wait <= 60
