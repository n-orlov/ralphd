"""The runner scaler's secret delivery (B.2) and per-container limits (B.3).

* The JIT config is a credential. It reaches the container as the file
  `/run/ci-runner/jitconfig`, through a tar stream on `docker cp -` stdin; it
  appears in no docker argv element, no `--env`/`-e`/`--env-file` value, no
  scaler log line, no `status` output and no host-side file.
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
import io
import json
import os
import subprocess
import sys
import tarfile
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
                    f"max_concurrency: {cap}\n{extra}")
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
    hits = []
    for path in Path(root).rglob("*"):
        if path.is_file() and needle.encode() in path.read_bytes():
            hits.append(path)
    return hits


# ---- (1) the JIT config never leaves the container's file ------------------

def test_the_jit_value_is_in_no_argv_env_log_status_or_host_file(
        github, docker, tmp_path):
    scratch = tmp_path / "scratch-tmp"
    scratch.mkdir()
    config = write_config(tmp_path, github)
    queue_job(github, 1, 101, ["ci-pool"])
    done = run_scaler(docker, config, "once", tmpdir=scratch)
    assert done.returncode == 0, done.stderr
    assert "ci-runner-101" in docker.containers()
    # The value was generated and used: the file inside the container has it.
    assert github.requests_to("POST", "/repos/n-orlov/ralphd/actions/runners")
    # No docker argv element (any verb) carries it; no env flag exists at all.
    for argv in docker.argv_log():
        assert not any(FAKE_JIT in part for part in argv), argv
        assert not any(part in ("-e", "--env", "--env-file") or
                       part.startswith(("--env=", "--env-file=")) for part in argv), argv
    # No scaler log line or output.
    assert FAKE_JIT not in done.stderr and FAKE_JIT not in done.stdout
    status = run_scaler(docker, config, "status", tmpdir=scratch)
    assert status.returncode == 0, status.stderr
    assert "ci-runner-101" in status.stdout
    assert FAKE_JIT not in status.stdout and FAKE_JIT not in status.stderr
    # No host-side file: the scratch dir is empty, and nothing under the scaler's
    # config dir carries it. Any file that did carry it would have to be 0600
    # and gone, so a leftover is a failure either way.
    assert files_containing(scratch, FAKE_JIT) == []
    assert files_containing(config.parent, FAKE_JIT) == []
    assert list(scratch.iterdir()) == []


def test_the_jit_value_reaches_the_container_as_a_0400_file_via_docker_cp_stdin(
        github, docker, tmp_path):
    config = write_config(tmp_path, github)
    queue_job(github, 1, 101, ["ci-pool"])
    assert run_scaler(docker, config, "once").returncode == 0
    verbs = [argv[0] for argv in docker.argv_log() if argv[0] in ("create", "cp", "start")]
    assert verbs == ["create", "cp", "start"]  # delivered before the runner starts
    (cp,) = docker.commands("cp")
    assert cp == ["cp", "-", "ci-runner-101:/run"]
    with tarfile.open(fileobj=io.BytesIO(docker.cp_payload("ci-runner-101"))) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert sorted(members) == ["ci-runner", "ci-runner/jitconfig"]
        jit = members["ci-runner/jitconfig"]
        assert tar.extractfile(jit).read() == FAKE_JIT.encode()
        assert jit.mode == 0o400 and jit.uid == 1000 and jit.gid == 1000
        assert members["ci-runner"].isdir() and members["ci-runner"].mode == 0o500


def test_a_failed_delivery_removes_the_container_by_exact_name_and_leaks_nothing(
        github, docker, tmp_path):
    config = write_config(tmp_path, github)
    queue_job(github, 1, 101, ["ci-pool"])
    failed = run_scaler(docker, config, "once", extra_env={"FAKE_DOCKER_FAIL": "cp"})
    assert failed.returncode == 0, failed.stderr
    assert docker.containers() == {}  # created, then removed: no half-started runner
    assert docker.commands("start") == []
    assert docker.commands("rm") == [["rm", "--force", "ci-runner-101"]]
    assert FAKE_JIT not in failed.stderr and FAKE_JIT not in failed.stdout
    retried = run_scaler(docker, config, "once")  # the next pass starts it cleanly
    assert retried.returncode == 0, retried.stderr
    assert docker.containers()["ci-runner-101"]["State"] == "running"


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
