"""The runner scaler's secret delivery (B.2) and per-container limits (B.3).

* The JIT config is a credential. It reaches the container as the file
  `/run/ci-runner/jitconfig`, a read-only bind mount of a mode-0600 file (owned
  by the runner's uid, in a private 0700 directory on a tmpfs-style mount) that
  the scaler removes once `docker start` returns, or when create or start fails;
  it appears in no docker argv element, no `--env`/`-e`/`--env-file` value, no
  scaler log line, no `status` output and no image or container layer.
* `docker create` carries `--cpus`, `--memory` and `--pids-limit` from the
  job's profile: the default profile (4 CPUs, 12 GB) or the `profiles` entry
  keyed by one of the job's labels.
* A config whose `max_concurrency` times its largest profile's CPUs exceeds
  `host_cpus` minus one reserved core is rejected at load.

Driven against the fake GitHub API and the fake `docker` in
`tests/_runner_scaler_fakes.py`; every JIT value is the obviously fake string
`FAKE_JIT_VALUE_ci-<job id>`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
FAKE_JIT = "FAKE_JIT_VALUE_ci-101"


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
        "runner_scaler_secrets_limits_under_test", SCALER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_config(tmp_path, github, extra="", cap=2):
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    token = conf / "token"
    token.write_text(FAKE_TOKEN + "\n")
    path = conf / "scaler.yaml"
    path.write_text(f"repo: {github.repo}\ntoken_file: {token}\n"
                    f"api_url: {github.url}\nimage: ci-runner:test\n"
                    f"max_concurrency: {cap}\nlock_file: {conf / 'image.lock'}\n{extra}")
    return path


def run_scaler(docker, config, *args, extra_env=None, tmpdir=None):
    env = docker.env()
    env.update(extra_env or {})
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return subprocess.run(
        [sys.executable, str(SCALER), "--config", str(config), *args],
        capture_output=True, text=True, env=env, timeout=60)


def queue_job(github, run_id, job_id, labels):
    if not any(r["id"] == run_id for r in github.runs):
        github.add_run(run_id)
    github.add_job(run_id, job_id, labels)


def option(argv, flag):
    assert argv.count(flag) == 1, f"{flag} must appear exactly once in {argv}"
    return argv[argv.index(flag) + 1]


def files_containing(root, needle):
    """Host files under `root` holding `needle`, leaving out the fake docker's own
    state dir (that is the simulated container's view, where the file belongs)."""
    hits = []
    for path in Path(root).rglob("*"):
        if "fakedocker" in path.parts:
            continue
        if path.is_file() and needle.encode() in path.read_bytes():
            hits.append(path)
    return hits


# ---- (1) the JIT config reaches the container through a 0600 file ----------

JIT_DEST = "/run/ci-runner/jitconfig"


def secret_dir(tmp_path):
    path = tmp_path / "secret-root"
    path.mkdir(exist_ok=True)
    return path


def secret_config(tmp_path, github, extra="", cap=2):
    """A config whose JIT files go to an inspectable directory, owned by the
    uid the tests run as (so the runner_uid check does not need root)."""
    return write_config(
        tmp_path, github,
        f"secret_dir: {secret_dir(tmp_path)}\nrunner_uid: {os.getuid()}\n{extra}", cap)


def test_the_jit_value_is_in_no_argv_env_log_status_or_leftover_file(
        github, docker, tmp_path):
    config = secret_config(tmp_path, github)
    queue_job(github, 1, 101, ["ci-pool"])
    done = run_scaler(docker, config, "once")
    assert done.returncode == 0, done.stderr
    assert "ci-runner-101" in docker.containers()
    assert github.requests_to("POST", "/repos/n-orlov/ralphd/actions/runners")
    # No docker argv element (any verb) carries it; no env flag exists at all.
    for argv in docker.argv_log():
        assert not any(FAKE_JIT in part for part in argv), argv
        assert not any(part in ("-e", "--env", "--env-file") or
                       part.startswith(("--env=", "--env-file=")) for part in argv), argv
    # No scaler log line or output.
    assert FAKE_JIT not in done.stderr and FAKE_JIT not in done.stdout
    status = run_scaler(docker, config, "status")
    assert status.returncode == 0, status.stderr
    assert "ci-runner-101" in status.stdout
    assert FAKE_JIT not in status.stdout and FAKE_JIT not in status.stderr
    # The container was started, and nothing on the host keeps the value.
    assert files_containing(tmp_path, FAKE_JIT) == []
    assert list(secret_dir(tmp_path).iterdir()) == []


def test_the_container_sees_a_private_read_only_file_that_is_gone_from_the_host_after_start(
        github, docker, tmp_path):
    config = secret_config(tmp_path, github)
    queue_job(github, 1, 101, ["ci-pool"])
    assert run_scaler(docker, config, "once").returncode == 0
    # Not an image layer: no `docker cp`, no `commit`, a bind mount at the path
    # the run-once command reads.
    assert docker.commands("cp", "commit") == []
    verbs = [argv[0] for argv in docker.argv_log() if argv[0] in ("create", "start")]
    assert verbs == ["create", "start"]
    (create,) = docker.commands("create")
    binds = [create[i + 1] for i, part in enumerate(create) if part == "-v"
             and create[i + 1].endswith(f":{JIT_DEST}:ro")]
    assert len(binds) == 1, create
    host_path = binds[0].split(":")[0]
    assert host_path.startswith(str(secret_dir(tmp_path)) + os.sep)
    assert create[-2:] == ["/home/ubuntu/run-once.sh", JIT_DEST]
    # At the moment of start the file held the value, readable only by the
    # runner's uid (0600 owner), in a directory only that uid can enter (0700).
    seen = docker.mounted_file("ci-runner-101", JIT_DEST)
    assert seen is not None, "the file did not exist when the container started"
    assert seen["content"] == FAKE_JIT
    assert seen["mode"] == 0o600 and seen["uid"] == os.getuid() and seen["ro"]
    assert seen["dir_mode"] == 0o700
    assert seen["src"] == host_path
    # ...and the removal the bind mount tolerates already happened.
    assert not Path(host_path).exists() and not Path(host_path).parent.exists()


def test_each_job_of_a_pass_gets_its_own_file_and_none_survives(
        github, docker, tmp_path):
    config = secret_config(tmp_path, github)
    queue_job(github, 1, 101, ["ci-pool"])
    queue_job(github, 1, 102, ["ci-pool"])
    assert run_scaler(docker, config, "once").returncode == 0
    first = docker.mounted_file("ci-runner-101", JIT_DEST)
    second = docker.mounted_file("ci-runner-102", JIT_DEST)
    assert first["src"] != second["src"]
    assert first["content"] == "FAKE_JIT_VALUE_ci-101"
    assert second["content"] == "FAKE_JIT_VALUE_ci-102"
    assert list(secret_dir(tmp_path).iterdir()) == []


@pytest.mark.parametrize("verb", ["create", "start"])
def test_a_failed_create_or_start_leaves_no_file_no_container_and_no_leak(
        github, docker, tmp_path, verb):
    config = secret_config(tmp_path, github)
    queue_job(github, 1, 101, ["ci-pool"])
    failed = run_scaler(docker, config, "once", extra_env={"FAKE_DOCKER_FAIL": verb})
    assert failed.returncode == 0, failed.stderr
    assert docker.containers() == {}  # no half-started runner
    assert list(secret_dir(tmp_path).iterdir()) == []
    assert files_containing(tmp_path, FAKE_JIT) == []
    if verb == "start":
        assert docker.commands("rm") == [["rm", "--force", "ci-runner-101"]]
    assert FAKE_JIT not in failed.stderr and FAKE_JIT not in failed.stdout
    for argv in docker.argv_log():
        assert not any(FAKE_JIT in part for part in argv), argv
    retried = run_scaler(docker, config, "once")  # the next pass starts it cleanly
    assert retried.returncode == 0, retried.stderr
    assert docker.containers()["ci-runner-101"]["State"] == "running"
    assert list(secret_dir(tmp_path).iterdir()) == []


def test_a_file_the_runner_uid_cannot_own_fails_closed_before_any_container(
        github, docker, tmp_path):
    # Not root, and not the configured uid: chown is refused, so the scaler
    # starts nothing rather than mount a file the runner cannot read.
    other = 0 if os.getuid() != 0 else 1
    config = write_config(tmp_path, github,
                          f"secret_dir: {secret_dir(tmp_path)}\nrunner_uid: {other or 1}\n")
    if os.getuid() == 0:
        pytest.skip("root can chown to any uid")
    queue_job(github, 1, 101, ["ci-pool"])
    done = run_scaler(docker, config, "once")
    assert done.returncode == 0
    assert "uid" in done.stderr and FAKE_JIT not in done.stderr
    assert docker.commands("create", "start") == []
    assert docker.containers() == {}
    assert list(secret_dir(tmp_path).iterdir()) == []


def test_a_missing_secret_dir_is_refused_and_starts_nothing(github, docker, tmp_path):
    config = write_config(tmp_path, github,
                          f"secret_dir: {tmp_path / 'nope'}\nrunner_uid: {os.getuid()}\n")
    queue_job(github, 1, 101, ["ci-pool"])
    done = run_scaler(docker, config, "once")
    assert done.returncode == 0
    assert "secret_dir" in done.stderr
    assert docker.commands("create") == [] and docker.containers() == {}


# ---- in-process: the file helper itself --------------------------------------

def test_jit_file_is_0600_in_a_0700_dir_and_removed_on_normal_exit(scaler, tmp_path):
    with scaler.jit_file(FAKE_JIT, str(tmp_path), os.getuid()) as path:
        assert path.read_text() == FAKE_JIT
        assert (path.stat().st_mode & 0o7777) == 0o600
        assert (path.parent.stat().st_mode & 0o7777) == 0o700
        assert path.parent.parent == tmp_path
    assert not path.exists() and not path.parent.exists()


def test_jit_file_is_removed_when_the_block_raises(scaler, tmp_path):
    with pytest.raises(RuntimeError):
        with scaler.jit_file(FAKE_JIT, str(tmp_path), os.getuid()) as path:
            raise RuntimeError("start failed")
    assert not path.exists() and list(tmp_path.iterdir()) == []


def test_jit_file_hands_the_file_to_the_runner_uid_1000_by_default(
        scaler, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(scaler.os, "getuid", lambda: 4242)
    monkeypatch.setattr(scaler.os, "chown", lambda p, u, g: calls.append((Path(p), u, g)))
    assert scaler.JIT_RUNNER_UID == 1000
    with scaler.jit_file(FAKE_JIT, str(tmp_path)) as path:
        assert calls == [(path, 1000, -1)]
        assert path.read_text() == FAKE_JIT  # written only after the chown


def test_jit_file_writes_nothing_when_the_chown_is_refused(scaler, tmp_path, monkeypatch):
    def refuse(path, uid, gid):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(scaler.os, "getuid", lambda: 4242)
    monkeypatch.setattr(scaler.os, "chown", refuse)
    with pytest.raises(scaler.ScalerError) as err:
        with scaler.jit_file(FAKE_JIT, str(tmp_path)):
            raise AssertionError("must not be reached")
    assert FAKE_JIT not in str(err.value) and "1000" in str(err.value)
    assert list(tmp_path.iterdir()) == []


def test_secret_root_prefers_the_configured_dir_then_a_tmpfs_style_one(
        scaler, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert scaler.secret_root("") == runtime
    assert scaler.secret_root(str(tmp_path)) == tmp_path
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "absent"))
    assert str(scaler.secret_root("")) in ("/dev/shm", scaler.tempfile.gettempdir())
    with pytest.raises(scaler.ScalerError):
        scaler.secret_root(str(tmp_path / "absent"))


# ---- (2) the start carries the job's profile limits --------------------------

def test_the_default_profile_limits_are_on_the_create_argv(github, docker, tmp_path):
    config = write_config(tmp_path, github)
    queue_job(github, 1, 101, ["self-hosted", "ci-pool"])
    assert run_scaler(docker, config, "once").returncode == 0
    (argv,) = docker.commands("create")
    assert option(argv, "--cpus") == "4"
    assert option(argv, "--memory") == "12g"
    assert option(argv, "--pids-limit") == "4096"


def test_a_profile_keyed_by_a_job_label_sets_that_jobs_limits(github, docker, tmp_path):
    config = write_config(
        tmp_path, github,
        "profiles:\n  browser-runner:\n    cpus: 4\n    memory: 16g\n")
    queue_job(github, 1, 101, ["ci-pool", "browser-runner"])
    queue_job(github, 1, 102, ["ci-pool"])
    assert run_scaler(docker, config, "once").returncode == 0
    by_name = {option(a, "--name"): a for a in docker.commands("create")}
    browser, plain = by_name["ci-runner-101"], by_name["ci-runner-102"]
    assert option(browser, "--cpus") == "4"
    assert option(browser, "--memory") == "16g"
    assert option(browser, "--pids-limit") == "4096"  # inherited from the default
    assert option(plain, "--cpus") == "4" and option(plain, "--memory") == "12g"


def test_a_default_entry_in_profiles_overrides_the_built_in_default(
        github, docker, tmp_path):
    config = write_config(tmp_path, github,
                          "profiles:\n  default:\n    cpus: 2\n    memory: 8g\n"
                          "    pids_limit: 512\n"
                          "  browser-runner:\n    memory: 16g\n")
    queue_job(github, 1, 101, ["ci-pool"])
    queue_job(github, 1, 102, ["ci-pool", "browser-runner"])
    assert run_scaler(docker, config, "once").returncode == 0
    by_name = {option(a, "--name"): a for a in docker.commands("create")}
    plain, browser = by_name["ci-runner-101"], by_name["ci-runner-102"]
    assert (option(plain, "--cpus"), option(plain, "--memory"),
            option(plain, "--pids-limit")) == ("2", "8g", "512")
    # A labelled profile inherits every field it omits from the default profile.
    assert (option(browser, "--cpus"), option(browser, "--memory"),
            option(browser, "--pids-limit")) == ("2", "16g", "512")


# ---- (3) a cap that oversubscribes the host is refused at load ---------------

def load(scaler, tmp_path, text):
    path = tmp_path / "c.yaml"
    path.write_text("repo: o/r\ntoken_file: t\n" + text)
    return scaler.load_config(path)


def test_a_cap_times_cpus_above_host_cpus_minus_one_is_rejected_naming_both_numbers(
        scaler, tmp_path):
    with pytest.raises(scaler.ConfigError) as err:
        load(scaler, tmp_path, "max_concurrency: 4\nhost_cpus: 12\n")
    assert "16" in str(err.value) and "11" in str(err.value)
    assert "max_concurrency" in str(err.value) and "host_cpus" in str(err.value)


def test_two_by_four_cpus_on_a_twelve_cpu_host_loads(scaler, tmp_path):
    cfg = load(scaler, tmp_path, "max_concurrency: 2\nhost_cpus: 12\n")
    assert cfg["max_concurrency"] == 2 and cfg["host_cpus"] == 12


def test_the_largest_profile_not_just_the_default_counts_toward_the_check(
        scaler, tmp_path):
    big = "profiles:\n  big:\n    cpus: 8\n"
    with pytest.raises(scaler.ConfigError) as err:
        load(scaler, tmp_path, "max_concurrency: 2\nhost_cpus: 12\n" + big)
    assert "16" in str(err.value) and "11" in str(err.value)
    load(scaler, tmp_path, "max_concurrency: 1\nhost_cpus: 12\n" + big)


def test_the_cli_refuses_an_oversubscribed_config_before_touching_docker(
        github, docker, tmp_path):
    config = write_config(tmp_path, github, "host_cpus: 12\n", cap=4)
    queue_job(github, 1, 101, ["ci-pool"])
    done = run_scaler(docker, config, "once")
    assert done.returncode == 2
    assert "16" in done.stderr and "11" in done.stderr
    assert docker.argv_log() == [] and github.requests == []
