"""The concurrency cap is a host-wide admission invariant: a pass (or a second
scaler process serving the same pool) admits a container only after reading the
containers again under the shared admission lock, so a stale snapshot -- taken
before an image build, or before a rival pass started something -- never lets
more than `max_concurrency` runner containers be live. Driven in-process with
the fake GitHub API and the fake `docker`; the staleness is forced by hooks that
change the host between the pass's first container read and its starts."""

from __future__ import annotations

import errno
import fcntl
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
T0 = 1_800_000_000.0
JIT_PATH = "/actions/runners/generate-jitconfig"


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_admission", SCALER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Rig:
    def __init__(self, scaler, github, docker, config_path):
        self.scaler = scaler
        self.github = github
        self.docker = docker
        self.config_path = config_path
        self.cfg = scaler.load_config(config_path)
        self.api = scaler.GitHubApi(self.cfg)
        self.dock = scaler.Docker(self.cfg)
        self.docker.set_clock(T0)

    def after_first_snapshot(self, action):
        """Run `action` once, right after the pass's first container read: the
        snapshot the pass keeps is then stale."""
        original = self.dock.containers
        state = {"calls": 0}

        def hooked():
            found = original()
            state["calls"] += 1
            if state["calls"] == 1:
                action()
            return found

        self.dock.containers = hooked

    def rival(self, *job_ids):
        for job_id in job_ids:
            self.docker.add_container(f"ci-runner-{job_id}", created_epoch=T0)

    def run_pass(self):
        return self.scaler.run_pass(self.cfg, self.api, self.dock,
                                    clock=lambda: T0, out=lambda _: None)

    def live(self):
        return [n for n, c in self.docker.containers().items()
                if c["State"] not in ("exited", "dead")]

    def created(self):
        return [argv[argv.index("--name") + 1] for argv in self.docker.commands("create")]

    def jit_requests(self):
        return self.github.requests_to("POST", "/repos/" + self.github.repo + JIT_PATH)


def make_rig(tmp_path, monkeypatch, scaler, github, extra=""):
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    docker = FakeDocker(tmp_path / "fakedocker")
    for key, value in docker.env().items():
        if key in ("PATH", "FAKE_DOCKER_DIR"):
            monkeypatch.setenv(key, value)
    path = tmp_path / "scaler.yaml"
    path.write_text(f"repo: {github.repo}\ntoken_file: {token}\nhost_cpus: 64\n"
                    f"api_url: {github.url}\nimage: ci-runner:test\n"
                    f"admission_lock_file: {tmp_path / 'admission.lock'}\n"
                    f"lock_file: {tmp_path / 'build.lock'}\n" + extra)
    return Rig(scaler, github, docker, path)


@pytest.fixture()
def github():
    with FakeGitHub() as fake:
        yield fake


@pytest.fixture()
def make(tmp_path, monkeypatch, scaler, github):
    def build(max_concurrency, *job_ids, extra=""):
        rig = make_rig(tmp_path, monkeypatch, scaler, github,
                       f"max_concurrency: {max_concurrency}\n" + extra)
        github.add_run(1)
        for job_id in job_ids:
            github.add_job(1, job_id, ["ci-pool"])
        return rig
    return build


def test_a_rival_pass_filling_the_last_slot_after_the_snapshot_stops_admission(make):
    rig = make(2, 101, 102)
    rig.docker.add_image(rig.scaler.image_ref(rig.cfg))
    rig.after_first_snapshot(lambda: rig.rival(900))
    assert rig.run_pass() == 1
    assert rig.created() == ["ci-runner-101"]
    assert len(rig.live()) == 2


def test_containers_started_during_an_image_build_wait_count_against_the_cap(make):
    rig = make(1, 101, 102)  # the image is missing, so the pass builds it first
    real = rig.scaler.ensure_image

    def build_then_rival(cfg, docker, **kwargs):
        ref = real(cfg, docker, **kwargs)
        rig.rival(900)  # another scaler started a runner while this one waited
        return ref

    rig.scaler.ensure_image, saved = build_then_rival, real
    try:
        assert rig.run_pass() == 0
    finally:
        rig.scaler.ensure_image = saved
    assert rig.created() == []
    assert rig.jit_requests() == []
    assert rig.live() == ["ci-runner-900"]


def test_a_container_name_taken_after_the_snapshot_is_skipped_without_a_jit_request(make):
    rig = make(3, 101, 102)
    rig.docker.add_image(rig.scaler.image_ref(rig.cfg))
    rig.after_first_snapshot(lambda: rig.rival(101))
    assert rig.run_pass() == 1
    assert rig.created() == ["ci-runner-102"]  # 101 never even attempted
    assert [r["body"]["name"] for r in rig.jit_requests()] == ["ci-102"]
    assert sorted(rig.live()) == ["ci-runner-101", "ci-runner-102"]


def test_a_conflicting_name_does_not_free_a_slot_for_a_younger_job(make):
    rig = make(2, 101, 102, 103)
    rig.docker.add_image(rig.scaler.image_ref(rig.cfg))
    rig.after_first_snapshot(lambda: rig.rival(101, 900))
    assert rig.run_pass() == 0  # 101 and 900 fill the cap
    assert rig.created() == []
    assert rig.jit_requests() == []
    assert len(rig.live()) == 2


def test_oldest_first_holds_when_a_rival_takes_a_slot(make):
    rig = make(2, 103, 101, 102)
    rig.docker.add_image(rig.scaler.image_ref(rig.cfg))
    rig.after_first_snapshot(lambda: rig.rival(900))
    rig.run_pass()
    assert rig.created() == ["ci-runner-101"]  # not 102 or 103


def test_a_failed_jit_request_still_leaves_the_recheck_in_force(make):
    rig = make(2, 101, 102, 103)
    rig.docker.add_image(rig.scaler.image_ref(rig.cfg))
    rig.github.fail_next(1, 500, path_contains="generate-jitconfig")
    rig.after_first_snapshot(lambda: rig.rival(900))
    rig.run_pass()
    assert rig.created() == ["ci-runner-102"]  # 101's request failed, then the cap
    assert len(rig.live()) == 2


def test_an_exited_container_does_not_hold_a_slot_at_the_recheck(make):
    rig = make(2, 101, 102)
    rig.docker.add_image(rig.scaler.image_ref(rig.cfg))
    rig.docker.add_container("ci-runner-900", created_epoch=T0)
    rig.docker.add_container("ci-runner-901", created_epoch=T0, state="exited")
    rig.run_pass()
    assert rig.created() == ["ci-runner-101"]  # one slot free, oldest job first
    assert len(rig.live()) == 2


def test_every_started_job_is_admitted_while_the_admission_lock_is_held(make):
    rig = make(3, 101, 102)
    rig.docker.add_image(rig.scaler.image_ref(rig.cfg))
    lock_path = Path(rig.cfg["admission_lock_file"])
    probes = []

    def held(label):
        with open(lock_path, "a+") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                probes.append((label, exc.errno in (errno.EAGAIN, errno.EACCES)))
            else:
                probes.append((label, False))
                fcntl.flock(other, fcntl.LOCK_UN)

    jit, start = rig.api.generate_jitconfig, rig.dock.start
    rig.api.generate_jitconfig = lambda job: (held("jit"), jit(job))[1]
    rig.dock.start = lambda *a, **k: (held("start"), start(*a, **k))[1]
    rig.run_pass()
    assert probes == [("jit", True), ("start", True)] * 2
    held("after")  # released between starts and after the pass
    assert probes[-1] == ("after", False)


def test_two_scaler_processes_on_one_pool_never_exceed_the_cap(make, tmp_path):
    rig = make(1, 101, 102, 103)
    gate = tmp_path / "build.gate"
    env = rig.docker.env()
    env["FAKE_DOCKER_BUILD_GATE"] = str(gate)
    procs = [subprocess.Popen(
        [sys.executable, str(SCALER), "--config", str(rig.config_path), "once"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(2)]
    try:
        while not rig.docker.commands("build"):
            assert all(p.poll() is None for p in procs), "a pass ended before building"
        gate.write_text("go")
        results = [p.communicate(timeout=60) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
    assert all(p.returncode == 0 for p in procs), results
    assert len(rig.created()) == 1
    assert len(rig.live()) == 1
