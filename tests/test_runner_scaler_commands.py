"""The runner scaler's commands (`once`, `run`, `status`, `--dry-run`) and its
config loading, driven against the fake GitHub API and the fake `docker` in
`tests/_runner_scaler_fakes.py`. No real network, daemon or credential."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker, FakeGitHub  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("runner_scaler_under_test", SCALER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
    path.write_text(
        f"repo: {github.repo}\n"
        f"token_file: {token}\nhost_cpus: 64\n"
        f"api_url: {github.url}\n"
        "image: ci-runner:test\n"
        "poll_interval_s: 1\n")
    return path


def scaler(docker, config, *args):
    return subprocess.run(
        [sys.executable, str(SCALER), "--config", str(config), *args],
        capture_output=True, text=True, env=docker.env(), timeout=60)


def queue_job(github, run_id, job_id, labels):
    github.add_run(run_id)
    github.add_job(run_id, job_id, labels)


# ---- --help ---------------------------------------------------------------

def test_help_names_the_commands_and_the_dry_run_flag():
    done = subprocess.run([sys.executable, str(SCALER), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    for word in ("once", "run", "status"):
        assert word in done.stdout
    once = subprocess.run([sys.executable, str(SCALER), "once", "--help"],
                          capture_output=True, text=True, timeout=60)
    assert once.returncode == 0 and "--dry-run" in once.stdout


def test_the_scaler_imports_only_the_standard_library():
    import ast
    tree = ast.parse(SCALER.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)


# ---- once -----------------------------------------------------------------

def test_once_exits_zero_on_an_empty_queue(github, docker, config):
    done = scaler(docker, config, "once")
    assert done.returncode == 0, done.stderr
    assert docker.commands("run", "create") == []
    assert github.requests_to("GET", f"/repos/{github.repo}/actions/runs")


def test_once_starts_one_container_for_a_queued_ci_pool_job(github, docker, config):
    queue_job(github, 1, 101, ["self-hosted", "ci-pool"])
    done = scaler(docker, config, "once")
    assert done.returncode == 0, done.stderr
    assert list(docker.containers()) == ["ci-runner-101"]
    runs = docker.commands("create")
    assert len(runs) == 1 and "ci-runner-101" in runs[0]


def test_once_ignores_a_queued_job_without_the_ci_pool_label(github, docker, config):
    queue_job(github, 1, 101, ["self-hosted", "fast-lane"])
    assert scaler(docker, config, "once").returncode == 0
    assert docker.commands("run", "create") == []


def test_once_queries_the_queued_runs_then_each_runs_jobs(github, docker, config):
    queue_job(github, 7, 701, ["ci-pool"])
    assert scaler(docker, config, "once").returncode == 0
    runs = github.requests_to("GET", f"/repos/{github.repo}/actions/runs")
    assert runs[0]["query"]["status"] == "queued"
    jobs = github.requests_to("GET", f"/repos/{github.repo}/actions/runs/7/jobs")
    assert jobs and jobs[0]["query"]["filter"] == "latest"
    assert all(r["authorized"] for r in github.requests)


# ---- --dry-run ------------------------------------------------------------

def test_dry_run_prints_the_start_and_issues_no_docker_run_or_create(
        github, docker, config):
    queue_job(github, 1, 101, ["ci-pool"])
    done = scaler(docker, config, "once", "--dry-run")
    assert done.returncode == 0, done.stderr
    assert "ci-runner-101" in done.stdout and "would start" in done.stdout
    assert docker.commands("run", "create") == []
    assert docker.containers() == {}


def test_run_dry_run_loops_without_starting(github, docker, config):
    queue_job(github, 1, 101, ["ci-pool"])
    done = scaler(docker, config, "run", "--dry-run", "--passes", "1")
    assert done.returncode == 0, done.stderr
    assert "would start ci-runner-101" in done.stdout
    assert docker.commands("run", "create") == []


def test_run_loop_sleeps_poll_interval_between_passes_on_an_injected_clock(
        github, docker, config, monkeypatch):
    for key, value in docker.env().items():
        if key in ("PATH", "FAKE_DOCKER_DIR"):
            monkeypatch.setenv(key, value)  # the pass lists containers first
    module = _load_module()
    cfg = module.load_config(config)
    sleeps = []
    module.run_loop(cfg, module.GitHubApi(cfg), module.Docker(cfg),
                    passes=3, sleep=sleeps.append)
    assert sleeps == [1.0, 1.0]
    assert len(github.requests_to("GET", f"/repos/{github.repo}/actions/runs")) == 3


# ---- status ---------------------------------------------------------------

def test_status_prints_a_json_list_with_container_job_id_and_age(
        github, docker, config):
    now = time.time()
    docker.add_container("ci-runner-202", created_epoch=now - 300)
    docker.add_container("ci-runner-101", created_epoch=now - 60)
    docker.add_container("unrelated-db", created_epoch=now - 9000)
    done = scaler(docker, config, "status")
    assert done.returncode == 0, done.stderr
    entries = json.loads(done.stdout)
    assert [e["container"] for e in entries] == ["ci-runner-101", "ci-runner-202"]
    assert [e["job_id"] for e in entries] == [101, 202]
    assert 55 <= entries[0]["age_s"] <= 120
    assert 295 <= entries[1]["age_s"] <= 360
    assert docker.commands("rm", "stop", "kill") == []


def test_status_is_an_empty_list_with_no_containers(github, docker, config):
    done = scaler(docker, config, "status")
    assert done.returncode == 0 and json.loads(done.stdout) == []


# ---- bad invocations ------------------------------------------------------

def test_an_unknown_command_exits_non_zero(github, docker, config):
    done = scaler(docker, config, "frobnicate")
    assert done.returncode != 0
    assert docker.argv_log() == []


def test_no_command_exits_non_zero(docker, config):
    assert scaler(docker, config).returncode != 0


def test_a_missing_config_exits_non_zero_with_a_clear_message(docker, tmp_path):
    done = scaler(docker, tmp_path / "absent.yaml", "once")
    assert done.returncode == 2 and "config" in done.stderr


# ---- config loading -------------------------------------------------------

def test_config_defaults_and_nested_profiles(tmp_path):
    module = _load_module()
    path = tmp_path / "c.yaml"
    path.write_text(
        "# comment\nrepo: o/r\ntoken_file: ~/t  # trailing\n"
        "max_concurrency: 2\nprofiles:\n  browser-runner:\n    cpus: 4\n"
        "    memory: 16g\n  default:\n    cpus: 2\n    memory: \"8g\"\n")
    cfg = module.load_config(path)
    assert cfg["max_concurrency"] == 2 and cfg["poll_interval_s"] == 5
    assert cfg["host_cpus"] == 12
    assert cfg["start_deadline_s"] == 120 and cfg["job_deadline_s"] == 7800
    assert cfg["profiles"] == {"browser-runner": {"cpus": 4, "memory": "16g"},
                               "default": {"cpus": 2, "memory": "8g"}}
    assert cfg["token_file"].endswith("/t") and "~" not in cfg["token_file"]


@pytest.mark.parametrize("text, fragment", [
    ("repo: o/r\ntoken_file: t\ntoken: ghp_FAKE\n", "inline token"),
    ("token_file: t\n", "repo"),
    ("repo: o/r\n", "token_file"),
    ("repo: o/r\ntoken_file: t\nmax_concurrency: 0\n", "max_concurrency"),
    ("repo: o/r\ntoken_file: t\nbogus_key: 1\n", "bogus_key"),
    ("repo: o/r\ntoken_file: t\nlist: [a, b]\n", "plain scalars"),
    ("repo: o/r\nrepo: o/r\ntoken_file: t\n", "duplicate"),
])
def test_invalid_config_is_rejected_with_a_clear_error(tmp_path, text, fragment):
    module = _load_module()
    path = tmp_path / "c.yaml"
    path.write_text(text)
    with pytest.raises(module.ConfigError, match=fragment):
        module.load_config(path)
