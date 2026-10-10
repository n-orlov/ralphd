"""The runner scaler's image retention (C.1): `prune-images`.

The fake `docker` lists runner-image tags (`ci-runner:<12 hex>`), CI job-image
tags of the scaler's scheme (`ralphd:test-ci-<run>-<attempt>[-base]`) and
unrelated images. Only scheme tags beyond the cut-off are removed, each by one
`docker rmi <name:tag>`; no argv carries `prune`, `--filter` or a wildcard.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runner_scaler_fakes import FAKE_TOKEN, FakeDocker  # noqa: E402

SCALER = Path(__file__).resolve().parents[1] / "ci" / "runner_scaler.py"
NOW = 1_800_000_000.0
DAY = 86400


def hexid(n: int) -> str:
    return f"{n:012x}"


@pytest.fixture(scope="module")
def scaler():
    spec = importlib.util.spec_from_file_location("runner_scaler_prune_under_test", SCALER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def docker(tmp_path, monkeypatch):
    fake = FakeDocker(tmp_path / "fakedocker")
    monkeypatch.setenv("FAKE_DOCKER_DIR", fake.env()["FAKE_DOCKER_DIR"])
    return fake


@pytest.fixture()
def write_config(tmp_path, docker):
    token = tmp_path / "token"
    token.write_text(FAKE_TOKEN + "\n")
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "Dockerfile").write_text("FROM scratch\n")

    def write(extra: str = "") -> Path:
        path = tmp_path / "scaler.yaml"
        path.write_text(
            f"repo: n-orlov/ralphd\ntoken_file: {token}\nhost_cpus: 64\n"
            f"image: ci-runner:latest\ndocker: {docker.bin_dir / 'docker'}\n"
            f"runner_dir: {recipe}\n{extra}")
        return path
    return write


def seed(docker, now=NOW):
    """Five runner tags (ages 1..5 days), job images, unrelated images."""
    for n in range(1, 6):
        docker.add_image(f"ci-runner:{hexid(n)}", created_epoch=now - n * DAY)
    docker.add_image("ci-runner:latest", created_epoch=now - 90 * DAY)
    docker.add_image("ralphd:test-ci-100-1", created_epoch=now - 30 * DAY)        # old
    docker.add_image("ralphd:test-ci-100-1-base", created_epoch=now - 30 * DAY)   # old
    docker.add_image("ralphd:test-ci-200-1", created_epoch=now - 13 * DAY)        # in window
    docker.add_image("ralphd:test-ci-200-1-base", created_epoch=now - 2 * DAY)    # in window
    docker.add_image("ralphd:test-local-dev", created_epoch=now - 90 * DAY)       # not scheme
    docker.add_image("ralphd:latest", created_epoch=now - 90 * DAY)
    docker.add_image("ubuntu:24.04", created_epoch=now - 400 * DAY)
    docker.add_image("other-runner:" + hexid(9), created_epoch=now - 400 * DAY)
    docker.add_image("ci-runner-extra:" + hexid(9), created_epoch=now - 400 * DAY)


def prune(scaler, docker, write_config, extra="", **kw):
    cfg = scaler.load_config(write_config(extra))
    return scaler.prune_images(cfg, scaler.Docker(cfg), clock=lambda: NOW, **kw)


def rmi_args(docker):
    return [argv[1:] for argv in docker.commands("rmi")]


# ---- (1) what is removed, by exact name ----------------------------------

def test_runner_tags_beyond_the_newest_keep_and_stale_job_tags_are_removed_by_exact_name(
        scaler, docker, write_config):
    seed(docker)
    removed = prune(scaler, docker, write_config)
    expected = {f"ci-runner:{hexid(4)}", f"ci-runner:{hexid(5)}",
                "ralphd:test-ci-100-1", "ralphd:test-ci-100-1-base"}
    assert set(removed) == expected
    assert sorted(rmi_args(docker)) == sorted([[ref] for ref in expected])
    assert expected.isdisjoint(docker.images())


def test_keep_and_retain_days_come_from_the_config(scaler, docker, write_config):
    seed(docker)
    removed = prune(scaler, docker, write_config, "keep: 1\nretain_days: 1\n")
    assert set(removed) == {f"ci-runner:{hexid(n)}" for n in (2, 3, 4, 5)} | {
        "ralphd:test-ci-100-1", "ralphd:test-ci-100-1-base", "ralphd:test-ci-200-1",
        "ralphd:test-ci-200-1-base"}
    assert f"ci-runner:{hexid(1)}" in docker.images()


def test_defaults_are_keep_3_and_retain_days_14(scaler, write_config):
    cfg = scaler.load_config(write_config())
    assert (cfg["keep"], cfg["retain_days"]) == (3, 14)


def test_the_newest_are_ranked_by_creation_time_not_by_name(scaler, docker, write_config):
    for n, age in ((1, 9), (2, 1), (3, 8), (4, 2), (5, 3)):
        docker.add_image(f"ci-runner:{hexid(n)}", created_epoch=NOW - age * DAY)
    removed = prune(scaler, docker, write_config)
    assert set(removed) == {f"ci-runner:{hexid(1)}", f"ci-runner:{hexid(3)}"}


# ---- (2) what is never removed ----------------------------------------------

def test_no_unrelated_and_no_in_window_image_is_removed(scaler, docker, write_config):
    seed(docker)
    prune(scaler, docker, write_config)
    survivors = set(docker.images())
    for ref in ("ci-runner:latest", "ralphd:test-local-dev", "ralphd:latest",
                "ubuntu:24.04", "other-runner:" + hexid(9),
                "ci-runner-extra:" + hexid(9), "ralphd:test-ci-200-1",
                "ralphd:test-ci-200-1-base", *[f"ci-runner:{hexid(n)}" for n in (1, 2, 3)]):
        assert ref in survivors


def test_no_argv_carries_prune_a_filter_or_a_wildcard(scaler, docker, write_config):
    seed(docker)
    prune(scaler, docker, write_config)
    assert docker.commands("rmi")
    for argv in docker.argv_log():
        for arg in argv:
            assert arg != "prune" and "*" not in arg
            assert not arg.startswith("--filter") and arg not in ("-f", "--force")
    for argv in docker.commands("rmi"):
        assert len(argv) == 2 and ":" in argv[1]          # one exact name:tag


def test_an_image_a_container_uses_is_kept(scaler, docker, write_config):
    seed(docker)
    docker.add_container("sibling-1", created_epoch=NOW, image="ralphd:test-ci-100-1")
    docker.add_container("ci-runner-7", created_epoch=NOW, image=f"ci-runner:{hexid(5)}")
    removed = prune(scaler, docker, write_config)
    assert set(removed) == {f"ci-runner:{hexid(4)}", "ralphd:test-ci-100-1-base"}


def test_the_tag_the_current_recipe_builds_is_kept_even_when_old(scaler, docker, write_config):
    cfg = scaler.load_config(write_config())
    current = scaler.image_ref(cfg)
    docker.add_image(current, created_epoch=NOW - 50 * DAY)
    for n in range(1, 5):
        docker.add_image(f"ci-runner:{hexid(n)}", created_epoch=NOW - n * DAY)
    removed = scaler.prune_images(cfg, scaler.Docker(cfg), clock=lambda: NOW)
    assert removed == [f"ci-runner:{hexid(4)}"]
    assert current in docker.images()


def test_a_failed_rmi_is_logged_and_the_rest_go_on(scaler, docker, write_config, monkeypatch):
    seed(docker)
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "rmi")
    assert prune(scaler, docker, write_config) == []
    assert len(docker.commands("rmi")) == 4


def test_remove_image_refuses_a_name_outside_the_scheme(scaler, docker, write_config):
    cfg = scaler.load_config(write_config())
    dock = scaler.Docker(cfg)
    for ref in ("ubuntu:24.04", "ci-runner:latest", "ralphd:latest", "ralphd:test-ci-1-1*",
                "ralphd:test-ci-1"):
        with pytest.raises(scaler.ScalerError, match="not a scaler image"):
            dock.remove_image(cfg, ref)
    assert not docker.commands("rmi")


def test_dry_run_prints_the_removals_and_issues_none(scaler, docker, write_config, capsys):
    seed(docker)
    removed = prune(scaler, docker, write_config, dry_run=True)
    assert len(removed) == 4
    assert not docker.commands("rmi")
    out = capsys.readouterr().out
    assert f"would remove image ci-runner:{hexid(4)}" in out


def test_the_cli_command_prunes_through_the_fake_docker(tmp_path, docker, write_config):
    import time
    seed(docker, now=time.time())
    path = write_config()
    proc = subprocess.run([sys.executable, str(SCALER), "--config", str(path), "prune-images"],
                          env=docker.env(), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert len(docker.commands("rmi")) == 4
    dry = subprocess.run(
        [sys.executable, str(SCALER), "--config", str(path), "prune-images", "--dry-run"],
        env=docker.env(), capture_output=True, text=True, timeout=60)
    assert dry.returncode == 0 and len(docker.commands("rmi")) == 4
