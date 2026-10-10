"""The elastic runner pool's image recipe (v0.34, B.1) and its one-job exit (B.4).

`ci/runner/Dockerfile` is parsed, not built: the base is an Ubuntu 24.04 digest,
the runner archive is checked with `sha256sum -c` against a pinned 64-hex
SHA-256, there is no `latest` tag, and the image ends as `USER 1000`. Each
assertion is run against a mutated copy that lacks exactly that property and
must fail there. The scaler's container-start argv runs the runner once with
the JIT config file and has no `config.sh` registration step."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FakeDocker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "ci" / "runner" / "Dockerfile"
RUN_ONCE = ROOT / "ci" / "runner" / "run-once.sh"
SCALER = ROOT / "ci" / "runner_scaler.py"

DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs: comments dropped, continuations joined."""
    joined = re.sub(r"\\\n", " ", text)
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        word, _, rest = line.partition(" ")
        out.append((word.upper(), rest.strip()))
    return out


def base_images(text: str) -> list[str]:
    return [rest.split()[0] for word, rest in instructions(text) if word == "FROM"]


def check_digest_base(text: str) -> None:
    bases = base_images(text)
    assert bases, "no FROM"
    for base in bases:
        name, sep, digest = base.partition("@sha256:")
        assert name == "ubuntu" and sep and DIGEST_RE.match(digest), base


def check_sha256_verified(text: str) -> None:
    args = dict(rest.split("=", 1) for word, rest in instructions(text)
                if word == "ARG" and "=" in rest)
    assert DIGEST_RE.match(args.get("RUNNER_SHA256", "")), "no pinned 64-hex SHA-256"
    assert "RUNNER_VERSION" in args, "runner version is not a build ARG"
    runs = [rest for word, rest in instructions(text) if word == "RUN"]
    assert any("sha256sum -c" in r and "${RUNNER_SHA256}" in r for r in runs), \
        "the runner archive is not verified with sha256sum -c"
    assert any("actions-runner-linux-x64-${RUNNER_VERSION}" in r for r in runs)


def check_no_latest(text: str) -> None:
    for word, rest in instructions(text):
        assert not re.search(r":latest(\s|$)", rest), (word, rest)
        if word == "FROM":
            assert "@sha256:" in rest and ":latest" not in rest


def check_user_1000(text: str) -> None:
    steps = instructions(text)
    users = [i for i, (word, _) in enumerate(steps) if word == "USER"]
    assert users and steps[users[-1]][1] == "1000", steps[users[-1]:] if users else "no USER"
    assert not any(word == "RUN" for word, _ in steps[users[-1] + 1:]), \
        "a RUN follows the final USER"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text()


CHECKS = {
    "digest-base": check_digest_base,
    "sha256-check": check_sha256_verified,
    "no-latest": check_no_latest,
    "user-1000": check_user_1000,
}


def test_the_dockerfile_satisfies_every_check(dockerfile):
    for name, check in CHECKS.items():
        check(dockerfile)


def test_the_base_is_the_ubuntu_24_04_index_digest_and_noble_is_named(dockerfile):
    check_digest_base(dockerfile)
    assert "noble" in dockerfile or "24.04" in dockerfile


def test_the_image_installs_the_jobs_packages_and_no_docker_daemon(dockerfile):
    runs = " ".join(rest for word, rest in instructions(dockerfile) if word == "RUN")
    for package in ("git", "curl", "jq", "python3", "python3-venv", "docker-ce-cli"):
        assert re.search(rf"(?<![\w-]){re.escape(package)}(?![\w-])", runs), package
    assert not re.search(r"docker\.io|docker-ce(?!-cli)|containerd", runs)


MUTATIONS = {
    "digest-base": lambda t: re.sub(r"ubuntu@sha256:[0-9a-f]{64}", "ubuntu:24.04", t),
    "sha256-check": lambda t: t.replace("sha256sum -c -", "cat"),
    "no-latest": lambda t: re.sub(r"^FROM (\S+)", r"FROM \1:latest", t, flags=re.M),
    "user-1000": lambda t: re.sub(r"^USER 1000$", "USER root", t, flags=re.M),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_a_mutated_copy_lacking_the_property_fails_its_check(dockerfile, name):
    mutated = MUTATIONS[name](dockerfile)
    assert mutated != dockerfile, f"mutation {name} changed nothing"
    with pytest.raises(AssertionError):
        CHECKS[name](mutated)


def test_a_mutation_that_drops_the_pinned_value_fails_the_sha_check(dockerfile):
    mutated = re.sub(r"(ARG RUNNER_SHA256=)[0-9a-f]{64}", r"\1", dockerfile)
    assert mutated != dockerfile
    with pytest.raises(AssertionError):
        check_sha256_verified(mutated)


def test_a_mutated_copy_with_a_run_after_user_fails_the_user_check(dockerfile):
    with pytest.raises(AssertionError):
        check_user_1000(dockerfile + "RUN true\n")


# --- B.4: the container's command runs the runner once, with no registration ---

@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_image_under_test", SCALER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_argv(scaler, tmp_path, monkeypatch):
    docker = FakeDocker(tmp_path / "fakedocker")
    monkeypatch.setenv("PATH", docker.env()["PATH"])
    monkeypatch.setenv("FAKE_DOCKER_DIR", docker.env()["FAKE_DOCKER_DIR"])
    scaler.Docker({"docker": "docker"}).start(
        "ci-runner-101", "ci-runner:test", "FAKE_JIT_VALUE_ci-101",
        {"cpus": 4, "memory": "12g", "pids_limit": 4096})
    runs = docker.commands("create")
    assert len(runs) == 1
    return runs[0]


def test_the_start_argv_runs_the_runner_once_with_the_jit_config_file(
        scaler, tmp_path, monkeypatch):
    argv = start_argv(scaler, tmp_path, monkeypatch)
    image = argv.index("ci-runner:test")
    command = argv[image + 1:]
    assert command == ["/home/ubuntu/run-once.sh", "/run/ci-runner/jitconfig"]
    assert argv.count("/home/ubuntu/run-once.sh") == 1


def test_the_start_argv_has_no_config_sh_registration_step(scaler, tmp_path, monkeypatch):
    argv = start_argv(scaler, tmp_path, monkeypatch)
    assert not any("config.sh" in part for part in argv)
    assert not any("config.sh" in part for part in scaler.RUN_ONCE_COMMAND)


def test_the_run_once_script_runs_the_runner_with_jitconfig_and_never_registers():
    script = RUN_ONCE.read_text()
    code = "\n".join(line for line in script.splitlines()
                     if not line.lstrip().startswith("#"))
    assert re.search(r"exec \./run\.sh --jitconfig ", code)
    assert code.count("run.sh") == 1
    assert "config.sh" not in code
    assert (RUN_ONCE.stat().st_mode & 0o111) != 0
    assert any(w == "COPY" and "run-once.sh" in r
               for w, r in instructions(DOCKERFILE.read_text()))
