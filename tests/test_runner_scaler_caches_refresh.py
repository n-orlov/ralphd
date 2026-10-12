"""The runner scaler's shared caches (B.5) and its build-once image refresh (B.6).

* A container mounts the named volume `ci-pip-cache` writable as the pip cache
  dir, and binds the host's `/opt/ms-playwright` read-only only for a
  `browser-runner` job. A volume the host lacks is created by its exact name.
* The docker socket is mounted for a job labelled `docker-host`, and only then.
* The image tag is `<repository>:<hash of the recipe>`. A pass that finds the
  tag missing builds it once, under a file lock: a second pass (or process)
  that meets the lock waits for it and builds nothing. A changed recipe is a
  new tag, built before the next start.

Driven against the fake GitHub API and the fake `docker` in
`tests/_runner_scaler_fakes.py`, in process, with an injected hook where a test
has to know that a pass is waiting on the lock.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
PIP_MOUNT = "ci-pip-cache:/home/ubuntu/.cache/pip"
PLAYWRIGHT_MOUNT = "/opt/ms-playwright:/opt/ms-playwright:ro"
SOCKET_MOUNT = "/var/run/docker.sock:/var/run/docker.sock"


@pytest.fixture()
def github():
    with FakeGitHub() as fake:
        yield fake


@pytest.fixture()
def docker(tmp_path):
    return FakeDocker(tmp_path / "fakedocker")


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location(
        "runner_scaler_caches_refresh_under_test", SCALER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rig(tmp_path, github, docker, monkeypatch, scaler):
    """A loaded config, a recipe dir (`runner_dir`) the test can edit, and the fake docker."""
    monkeypatch.setenv("FAKE_DOCKER_DIR", docker.env()["FAKE_DOCKER_DIR"])
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "Dockerfile").write_text("FROM scratch\n# recipe one\n")
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    path = tmp_path / "scaler.yaml"
    path.write_text(
        f"repo: {github.repo}\ntoken_file: {token}\nhost_cpus: 64\n"
        f"api_url: {github.url}\nimage: ci-runner:test\nmax_concurrency: 4\n"
        f"docker: {docker.bin_dir / 'docker'}\nrunner_dir: {recipe}\n"
        f"lock_file: {tmp_path / 'build.lock'}\n")
    cfg = scaler.load_config(path)

    class Rig:
        pass

    r = Rig()
    r.cfg, r.recipe, r.github, r.docker, r.tmp = cfg, recipe, github, docker, tmp_path
    r.api, r.dock = scaler.GitHubApi(cfg), scaler.Docker(cfg)
    r.pass_ = lambda **kw: scaler.run_pass(cfg, r.api, r.dock, **kw)
    return r


def queue_job(github, run_id, job_id, labels):
    github.add_run(run_id)
    github.add_job(run_id, job_id, labels)


def create_argv(docker, job_id):
    (argv,) = [a for a in docker.commands("create")
               if f"ci-runner-{job_id}" in a]
    return argv


def mounts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


def builds(docker):
    return docker.commands("build")


# ---- B.5: the shared caches ------------------------------------------------

def test_the_pip_cache_volume_is_mounted_writable_and_the_playwright_dir_only_for_browser_jobs(rig):
    queue_job(rig.github, 1, 101, ["ci-pool"])
    queue_job(rig.github, 2, 102, ["ci-pool", "browser-runner"])
    assert rig.pass_() == 2
    plain, browser = (mounts(create_argv(rig.docker, n)) for n in (101, 102))
    assert PIP_MOUNT in plain and PIP_MOUNT in browser       # writable: no ":ro"
    assert not any(m.startswith("/opt/ms-playwright") for m in plain)
    assert PLAYWRIGHT_MOUNT in browser                       # read-only bind
    # The only read-only mount of a plain job is the JIT file (B.2), which is
    # not a cache; covered by tests/test_runner_scaler_secrets_limits.py.
    assert [m for m in plain if m.endswith(":ro")] == [
        m for m in plain if m.endswith(":/run/ci-runner/jitconfig:ro")]
    assert len([m for m in plain if m.endswith(":ro")]) == 1


def test_a_missing_cache_volume_is_created_by_its_exact_name_and_an_existing_one_is_not(rig):
    queue_job(rig.github, 1, 101, ["ci-pool"])
    assert rig.pass_() == 1
    assert rig.docker.commands("volume") == [
        ["volume", "inspect", "ci-pip-cache"], ["volume", "create", "ci-pip-cache"]]
    assert rig.docker.volumes() == ["ci-pip-cache"]
    queue_job(rig.github, 2, 102, ["ci-pool"])
    assert rig.pass_() == 1
    assert [a for a in rig.docker.commands("volume") if a[1] == "create"] == [
        ["volume", "create", "ci-pip-cache"]]


def test_a_volume_that_already_exists_is_not_created(rig):
    rig.docker.add_volume("ci-pip-cache")
    queue_job(rig.github, 1, 101, ["ci-pool"])
    assert rig.pass_() == 1
    assert [a for a in rig.docker.commands("volume") if a[1] == "create"] == []


# ---- B.5 / PRD A.4: the docker socket --------------------------------------

def test_the_docker_socket_is_mounted_for_docker_host_jobs_only(rig):
    queue_job(rig.github, 1, 101, ["ci-pool", "docker-host"])
    queue_job(rig.github, 2, 102, ["ci-pool"])
    queue_job(rig.github, 3, 103, ["ci-pool", "browser-runner"])
    assert rig.pass_() == 3
    assert SOCKET_MOUNT in mounts(create_argv(rig.docker, 101))
    assert not any("docker.sock" in a for a in create_argv(rig.docker, 102))
    assert not any("docker.sock" in a for a in create_argv(rig.docker, 103))


def test_a_docker_host_job_joins_the_socket_group_and_other_jobs_do_not(rig, scaler, monkeypatch):
    monkeypatch.setattr(scaler, "docker_socket_gid", lambda: 971)
    queue_job(rig.github, 1, 101, ["ci-pool", "docker-host"])
    queue_job(rig.github, 2, 102, ["ci-pool"])
    assert rig.pass_() == 2
    argv = create_argv(rig.docker, 101)
    assert argv[argv.index("--group-add") + 1] == "971"
    assert "--group-add" not in create_argv(rig.docker, 102)


def test_a_missing_docker_socket_adds_no_group(rig, scaler, monkeypatch):
    monkeypatch.setattr(scaler, "docker_socket_gid", lambda: None)
    queue_job(rig.github, 1, 101, ["ci-pool", "docker-host"])
    assert rig.pass_() == 1
    assert "--group-add" not in create_argv(rig.docker, 101)


# ---- B.6: build once, under a lock; a changed recipe is a new tag ----------

def test_a_missing_runner_image_is_built_exactly_once_across_passes(rig, scaler):
    queue_job(rig.github, 1, 101, ["ci-pool"])
    queue_job(rig.github, 2, 102, ["ci-pool"])
    assert rig.pass_() == 2
    assert len(builds(rig.docker)) == 1
    tag = f"ci-runner:{scaler.recipe_hash(rig.recipe)}"
    assert builds(rig.docker)[0][:3] == ["build", "-t", tag]
    assert tag in rig.docker.images()
    queue_job(rig.github, 3, 103, ["ci-pool"])
    assert rig.pass_() == 1
    assert len(builds(rig.docker)) == 1                      # the tag is there now
    for job in (101, 102, 103):
        assert tag in create_argv(rig.docker, job)


def test_the_image_is_built_before_the_first_start_and_not_in_a_dry_run(rig):
    queue_job(rig.github, 1, 101, ["ci-pool"])
    assert rig.pass_(dry_run=True, out=lambda line: None) == 1
    assert builds(rig.docker) == []
    assert rig.pass_() == 1
    verbs = [a[0] for a in rig.docker.argv_log() if a[0] in ("build", "create")]
    assert verbs == ["build", "create"]


def test_a_second_concurrent_pass_waits_on_the_lock_and_builds_nothing(
        rig, scaler, monkeypatch):
    gate = rig.tmp / "release-build"
    monkeypatch.setenv("FAKE_DOCKER_BUILD_GATE", str(gate))
    waiting = threading.Event()
    results = {}

    def first():
        results["first"] = scaler.ensure_image(rig.cfg, rig.dock)

    def second():
        results["second"] = scaler.ensure_image(rig.cfg, rig.dock,
                                                on_wait=waiting.set)

    a = threading.Thread(target=first)
    a.start()
    deadline = time.monotonic() + 30
    while not builds(rig.docker) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(builds(rig.docker)) == 1                      # the first build is in flight
    b = threading.Thread(target=second)
    b.start()
    assert waiting.wait(30), "the second pass never met the lock"
    assert len(builds(rig.docker)) == 1                      # it is waiting, not building
    gate.write_text("go")
    a.join(60)
    b.join(60)
    assert not a.is_alive() and not b.is_alive()
    assert len(builds(rig.docker)) == 1                      # and built nothing after
    assert results["first"] == results["second"]


def test_a_second_concurrent_scaler_pass_records_no_second_build(
        rig, scaler, monkeypatch):
    gate = rig.tmp / "release-build"
    monkeypatch.setenv("FAKE_DOCKER_BUILD_GATE", str(gate))
    queue_job(rig.github, 1, 101, ["ci-pool"])
    waiting = threading.Event()
    real_log = scaler.log
    monkeypatch.setattr(scaler, "log", lambda m: (
        waiting.set() if "waiting for another build" in m else None, real_log(m))[1])
    a = threading.Thread(target=rig.pass_)
    a.start()
    deadline = time.monotonic() + 30
    while not builds(rig.docker) and time.monotonic() < deadline:
        time.sleep(0.01)
    b = threading.Thread(target=rig.pass_)
    b.start()
    assert waiting.wait(30), "the second pass never met the lock"
    gate.write_text("go")
    a.join(60)
    b.join(60)
    assert not a.is_alive() and not b.is_alive()
    assert len(builds(rig.docker)) == 1


def test_a_changed_recipe_is_a_new_tag_built_before_the_next_start(rig, scaler):
    queue_job(rig.github, 1, 101, ["ci-pool"])
    assert rig.pass_() == 1
    old_tag = f"ci-runner:{scaler.recipe_hash(rig.recipe)}"
    (rig.recipe / "Dockerfile").write_text("FROM scratch\n# recipe two\n")
    new_tag = f"ci-runner:{scaler.recipe_hash(rig.recipe)}"
    assert new_tag != old_tag
    queue_job(rig.github, 2, 102, ["ci-pool"])
    assert rig.pass_() == 1
    assert [b[2] for b in builds(rig.docker)] == [old_tag, new_tag]
    log = rig.docker.argv_log()
    new_build = next(i for i, a in enumerate(log) if a[:1] == ["build"] and new_tag in a)
    second_start = next(i for i, a in enumerate(log)
                        if a[:1] == ["create"] and "ci-runner-102" in a)
    assert new_build < second_start
    assert new_tag in create_argv(rig.docker, 102)
    assert old_tag in create_argv(rig.docker, 101)


def test_an_unchanged_recipe_keeps_its_tag(rig, scaler):
    first = scaler.recipe_hash(rig.recipe)
    assert scaler.recipe_hash(rig.recipe) == first
    (rig.recipe / "run-once.sh").write_text("#!/bin/sh\n")
    assert scaler.recipe_hash(rig.recipe) != first           # a copied file counts too
