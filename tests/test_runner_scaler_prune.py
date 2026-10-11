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
            f"runner_dir: {recipe}\nprune_state_file: {tmp_path / 'prune-state.json'}\n{extra}")
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


def prune(scaler, docker, write_config, extra="", *, at=NOW, **kw):
    cfg = scaler.load_config(write_config(extra))
    return scaler.prune_images(cfg, scaler.Docker(cfg), clock=lambda: at, **kw)


def settled(scaler, docker, write_config, extra="", **kw):
    """Two invocations: the first records which job tags are unreferenced (and, by
    the reference-age rule, removes no job tag), the second runs `retain_days`
    later. Returns the second invocation's removals."""
    first = prune(scaler, docker, write_config, extra)
    assert not [r for r in first if r.startswith("ralphd:")]
    return prune(scaler, docker, write_config, extra, at=NOW + 15 * DAY, **kw)


def rmi_args(docker):
    return [argv[1:] for argv in docker.commands("rmi")]


# ---- (1) what is removed, by exact name ----------------------------------

def test_runner_tags_beyond_the_newest_keep_and_stale_job_tags_are_removed_by_exact_name(
        scaler, docker, write_config):
    seed(docker)
    first = prune(scaler, docker, write_config)
    # Runner-image retention is by rank, so it needs no history; a job tag has never
    # been seen unreferenced, so creation age alone removes none.
    assert set(first) == {f"ci-runner:{hexid(4)}", f"ci-runner:{hexid(5)}"}
    second = prune(scaler, docker, write_config, at=NOW + 15 * DAY)
    expected = {"ralphd:test-ci-100-1", "ralphd:test-ci-100-1-base",
                "ralphd:test-ci-200-1", "ralphd:test-ci-200-1-base"}
    assert set(second) == expected      # every job tag was unreferenced for 15 days
    assert sorted(rmi_args(docker)) == sorted(
        [[ref] for ref in expected | set(first)])
    assert expected.isdisjoint(docker.images())


def test_keep_and_retain_days_come_from_the_config(scaler, docker, write_config):
    seed(docker)
    first = prune(scaler, docker, write_config, "keep: 1\nretain_days: 1\n")
    assert set(first) == {f"ci-runner:{hexid(n)}" for n in (2, 3, 4, 5)}
    assert f"ci-runner:{hexid(1)}" in docker.images()
    second = prune(scaler, docker, write_config, "keep: 1\nretain_days: 1\n",
                   at=NOW + 1 * DAY)
    assert set(second) == {"ralphd:test-ci-100-1", "ralphd:test-ci-100-1-base",
                           "ralphd:test-ci-200-1", "ralphd:test-ci-200-1-base"}
    before = prune(scaler, docker, write_config, "keep: 1\nretain_days: 1\n",
                   at=NOW + 1 * DAY)
    assert before == []


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
    prune(scaler, docker, write_config, at=NOW + 15 * DAY)
    survivors = set(docker.images())
    for ref in ("ci-runner:latest", "ralphd:test-local-dev", "ralphd:latest",
                "ubuntu:24.04", "other-runner:" + hexid(9),
                "ci-runner-extra:" + hexid(9), *[f"ci-runner:{hexid(n)}" for n in (1, 2, 3)]):
        assert ref in survivors


def test_no_argv_carries_prune_a_filter_or_a_wildcard(scaler, docker, write_config):
    seed(docker)
    settled(scaler, docker, write_config)
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
    removed = settled(scaler, docker, write_config)
    assert "ralphd:test-ci-100-1" not in removed
    assert "ralphd:test-ci-100-1-base" in removed
    assert f"ci-runner:{hexid(5)}" in docker.images()


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
    assert prune(scaler, docker, write_config, at=NOW + 15 * DAY) == []
    assert len(docker.commands("rmi")) == 2 + 6      # the failed ones are tried again
    monkeypatch.delenv("FAKE_DOCKER_FAIL")
    third = prune(scaler, docker, write_config, at=NOW + 16 * DAY)
    assert "ralphd:test-ci-100-1" in third


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
    prune(scaler, docker, write_config)           # records the history
    capsys.readouterr()
    removed = prune(scaler, docker, write_config, at=NOW + 15 * DAY, dry_run=True)
    assert len(removed) == 4
    assert len(docker.commands("rmi")) == 2         # only the first, real, invocation
    out = capsys.readouterr().out
    assert "would remove image ralphd:test-ci-100-1" in out


def test_the_cli_command_prunes_through_the_fake_docker(tmp_path, docker, write_config):
    import json
    import time
    now = time.time()
    seed(docker, now=now)
    path = write_config()
    cmd = [sys.executable, str(SCALER), "--config", str(path), "prune-images"]
    proc = subprocess.run(cmd, env=docker.env(), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert len(docker.commands("rmi")) == 2           # runner tags only: no job history yet
    state = tmp_path / "prune-state.json"
    assert "ralphd:test-ci-100-1" in json.loads(state.read_text())["unreferenced"]
    # a later, separate process: age the recorded history by 15 days
    data = json.loads(state.read_text())
    for entry in data["unreferenced"].values():
        entry["unreferenced_since"] = now - 15 * DAY
    state.write_text(json.dumps(data))
    dry = subprocess.run(cmd + ["--dry-run"], env=docker.env(), capture_output=True,
                         text=True, timeout=60)
    assert dry.returncode == 0 and len(docker.commands("rmi")) == 2
    assert dry.stdout.count("would remove image ralphd:test-ci-") == 4
    proc = subprocess.run(cmd, env=docker.env(), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert len(docker.commands("rmi")) == 6


# ---- (3) the reference-age rule ------------------------------------------------

JOB = "ralphd:test-ci-300-1"


def state_of(tmp_path):
    import json
    return json.loads((tmp_path / "prune-state.json").read_text())["unreferenced"]


def test_creation_age_alone_never_removes_a_job_image(scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 400 * DAY)
    docker.add_image("ralphd:test-ci-301-1-base", created_epoch=NOW - 3650 * DAY)
    assert prune(scaler, docker, write_config) == []
    assert prune(scaler, docker, write_config, at=NOW + 1) == []
    assert not docker.commands("rmi")


def test_an_old_image_recently_unreferenced_is_kept_until_the_interval_has_passed(
        scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 60 * DAY)
    docker.add_container("sibling-1", created_epoch=NOW, image=JOB)
    assert prune(scaler, docker, write_config) == []            # referenced
    docker.set_state("sibling-1", "exited")                     # still listed by ps -a
    assert prune(scaler, docker, write_config, at=NOW + 5 * DAY) == []
    data = docker._read()
    del data["containers"]["sibling-1"]                          # the container is removed
    docker._write(data)
    t0 = NOW + 6 * DAY
    assert prune(scaler, docker, write_config, at=t0) == []     # first seen unreferenced
    assert prune(scaler, docker, write_config, at=t0 + 13 * DAY) == []
    assert prune(scaler, docker, write_config, at=t0 + 14 * DAY - 1) == []
    assert JOB in docker.images()
    assert prune(scaler, docker, write_config, at=t0 + 14 * DAY) == [JOB]
    assert rmi_args(docker) == [[JOB]]


def test_an_image_unreferenced_beyond_the_interval_is_removed_in_a_later_invocation(
        scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 1 * DAY)          # young, but unused for long
    assert prune(scaler, docker, write_config) == []
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == [JOB]


def test_a_reference_in_between_restarts_the_clock(scaler, docker, write_config, tmp_path):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    assert prune(scaler, docker, write_config) == []
    docker.add_container("sibling-2", created_epoch=NOW, image=JOB)
    assert prune(scaler, docker, write_config, at=NOW + 10 * DAY) == []   # referenced
    assert JOB not in state_of(tmp_path)
    data = docker._read()
    del data["containers"]["sibling-2"]
    docker._write(data)
    assert prune(scaler, docker, write_config, at=NOW + 12 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 15 * DAY) == []   # 3 days only
    assert prune(scaler, docker, write_config, at=NOW + 26 * DAY) == [JOB]


def test_each_tag_keeps_its_own_clock(scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config)
    docker.add_image("ralphd:test-ci-302-1", created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config, at=NOW + 8 * DAY)
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == [JOB]
    assert prune(scaler, docker, write_config, at=NOW + 22 * DAY) == ["ralphd:test-ci-302-1"]


def test_a_retagged_image_restarts_the_clock(scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config)
    data = docker._read()
    data["image_created"][JOB] = NOW + 13 * DAY                  # same tag, new image
    docker._write(data)
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 27 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 28 * DAY) == [JOB]


def test_a_lost_or_malformed_history_restarts_every_clock(scaler, docker, write_config, tmp_path):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config)
    state = tmp_path / "prune-state.json"
    for broken in ("{not json", "[]", '{"unreferenced": 3}',
                   '{"unreferenced": {"%s": {"created": "x"}}}' % JOB):
        state.write_text(broken)
        assert prune(scaler, docker, write_config, at=NOW + 20 * DAY) == []
    state.unlink()
    assert prune(scaler, docker, write_config, at=NOW + 40 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 54 * DAY) == [JOB]


def test_a_clock_that_went_backwards_restarts_the_clock(scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config, at=NOW + 100 * DAY)
    assert prune(scaler, docker, write_config, at=NOW + 20 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 34 * DAY) == [JOB]


def test_a_dry_run_neither_starts_nor_changes_any_clock(scaler, docker, write_config, tmp_path):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    assert prune(scaler, docker, write_config, dry_run=True) == []
    assert not (tmp_path / "prune-state.json").exists()
    prune(scaler, docker, write_config)
    before = (tmp_path / "prune-state.json").read_text()
    assert prune(scaler, docker, write_config, at=NOW + 20 * DAY, dry_run=True) == [JOB]
    assert (tmp_path / "prune-state.json").read_text() == before
    assert not docker.commands("rmi")


def test_the_history_forgets_images_that_are_gone_and_the_current_tag(
        scaler, docker, write_config, tmp_path):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config)
    assert JOB in state_of(tmp_path)
    data = docker._read()
    data["images"].remove(JOB)
    docker._write(data)
    prune(scaler, docker, write_config, at=NOW + DAY)
    assert state_of(tmp_path) == {}


def test_runner_image_retention_needs_no_history(scaler, docker, write_config):
    for n in range(1, 6):
        docker.add_image(f"ci-runner:{hexid(n)}", created_epoch=NOW - n * DAY)
    assert set(prune(scaler, docker, write_config)) == {
        f"ci-runner:{hexid(4)}", f"ci-runner:{hexid(5)}"}


def test_a_removed_tag_that_comes_back_starts_a_new_clock(scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config)
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == [JOB]
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)         # pulled again, same image
    assert prune(scaler, docker, write_config, at=NOW + 15 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 29 * DAY) == [JOB]


def test_the_history_file_is_a_config_key_with_a_state_dir_default(scaler, write_config):
    cfg = scaler.load_config(write_config())
    assert cfg["prune_state_file"].endswith("prune-state.json")
    assert "prune_state_file" in scaler.DEFAULTS
    assert scaler.DEFAULTS["prune_state_file"].endswith("ralphd-ci/prune-state.json")


# ---- (4) use that begins and ends between two invocations ---------------------
#
# A container a sibling job creates and removes between two `prune-images`
# invocations is never in any `docker ps -a` snapshot. The daemon's event log is
# the record of it; the log is trusted only when it provably covers the whole
# interval, and otherwise every clock restarts.

CONTROL = "ralphd:test-ci-400-1"


def come_and_go(docker, image, name="sibling-between", at=None):
    """A container that exists only between two invocations."""
    if at is not None:
        docker.set_clock(at)
    docker.add_container(name, created_epoch=at or NOW, image=image)
    docker.remove_container(name)


def test_use_entirely_between_daily_invocations_restarts_the_clock(
        scaler, docker, write_config):
    # The review probe: daily separate invocations, day 0..13; a container uses
    # JOB after the day-13 run and is gone one second before the day-14 run.
    docker.add_image(JOB, created_epoch=NOW - 400 * DAY)
    docker.add_image(CONTROL, created_epoch=NOW - 400 * DAY)
    for day in range(14):
        assert prune(scaler, docker, write_config, at=NOW + day * DAY) == []
    come_and_go(docker, JOB, at=NOW + 14 * DAY - 1)
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == [CONTROL]
    assert JOB in docker.images()
    assert rmi_args(docker) == [[CONTROL]]
    # its clock now runs from the day-14 invocation, not from day 0
    for day in range(15, 28):
        assert prune(scaler, docker, write_config, at=NOW + day * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 28 * DAY) == [JOB]


def test_use_between_invocations_seen_by_a_separate_cli_process(tmp_path, docker,
                                                                 write_config):
    import json
    import time
    docker.add_image(JOB, created_epoch=time.time() - 400 * DAY)
    docker.add_image(CONTROL, created_epoch=time.time() - 400 * DAY)
    path = write_config()
    cmd = [sys.executable, str(SCALER), "--config", str(path), "prune-images"]
    first = subprocess.run(cmd, env=docker.env(), capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    state = tmp_path / "prune-state.json"
    data = json.loads(state.read_text())
    for entry in data["unreferenced"].values():          # 15 days unreferenced so far
        entry["unreferenced_since"] = time.time() - 15 * DAY
    state.write_text(json.dumps(data))
    come_and_go(docker, JOB)
    second = subprocess.run(cmd, env=docker.env(), capture_output=True, text=True,
                            timeout=60)
    assert second.returncode == 0, second.stderr
    assert rmi_args(docker) == [[CONTROL]]
    assert JOB in docker.images()


def test_use_by_image_id_between_invocations_protects_every_tag_of_that_image(
        scaler, docker, write_config):
    twin = "ralphd:test-ci-300-1-base"
    for ref in (JOB, twin, CONTROL):
        docker.add_image(ref, created_epoch=NOW - 90 * DAY)
    docker.set_image_id(JOB, "sha256:" + "ab" * 32)
    docker.set_image_id(twin, "sha256:" + "ab" * 32)
    assert prune(scaler, docker, write_config) == []
    come_and_go(docker, "abababababab")                  # run by short image id
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == [CONTROL]
    assert prune(scaler, docker, write_config, at=NOW + 27 * DAY) == []
    assert set(prune(scaler, docker, write_config, at=NOW + 28 * DAY)) == {JOB, twin}


def test_an_event_log_that_rolled_over_restarts_every_clock(scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    docker.add_image(CONTROL, created_epoch=NOW - 90 * DAY)
    docker.set_events_limit(8)
    assert prune(scaler, docker, write_config) == []
    come_and_go(docker, JOB)                              # pushed out of the log by ...
    for n in range(6):                                    # ... later unrelated events
        come_and_go(docker, "ubuntu:24.04", name=f"other-{n}")
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == []
    assert not docker.commands("rmi")                     # neither tag: coverage unproven
    assert prune(scaler, docker, write_config, at=NOW + 27 * DAY) == []
    assert set(prune(scaler, docker, write_config, at=NOW + 28 * DAY)) == {JOB, CONTROL}


def test_a_daemon_restart_between_invocations_restarts_every_clock(
        scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    assert prune(scaler, docker, write_config) == []
    docker.restart_daemon()
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 28 * DAY) == [JOB]


@pytest.mark.parametrize("verb", ["events", "info", "volume"])
def test_an_unreadable_event_log_removes_no_job_tag(scaler, docker, write_config,
                                                    monkeypatch, verb):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    for n in range(1, 5):
        docker.add_image(f"ci-runner:{hexid(n)}", created_epoch=NOW - n * DAY)
    assert prune(scaler, docker, write_config) == [f"ci-runner:{hexid(4)}"]
    monkeypatch.setenv("FAKE_DOCKER_FAIL", verb)
    assert prune(scaler, docker, write_config, at=NOW + 30 * DAY) == []
    monkeypatch.delenv("FAKE_DOCKER_FAIL")
    # the failed invocation left no marker, so the next one cannot prove coverage
    assert prune(scaler, docker, write_config, at=NOW + 31 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 45 * DAY) == [JOB]


def test_a_history_without_a_marker_restarts_every_clock(scaler, docker, write_config,
                                                         tmp_path):
    import json
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config)
    state = tmp_path / "prune-state.json"
    data = json.loads(state.read_text())
    assert data["marker"]["name"].startswith(scaler.RETENTION_MARKER_PREFIX)
    del data["marker"]                                    # e.g. a history written before markers
    state.write_text(json.dumps(data))
    assert prune(scaler, docker, write_config, at=NOW + 20 * DAY) == []
    assert prune(scaler, docker, write_config, at=NOW + 34 * DAY) == [JOB]


def test_the_marker_is_removed_by_exact_name_and_a_dry_run_sets_none(
        scaler, docker, write_config):
    docker.add_image(JOB, created_epoch=NOW - 90 * DAY)
    prune(scaler, docker, write_config)
    prune(scaler, docker, write_config, at=NOW + 7 * DAY, dry_run=True)
    assert len(docker.commands("volume")) == 2           # the real run only
    created, removed = docker.commands("volume")
    assert created[:2] == ["volume", "create"] and removed[:2] == ["volume", "rm"]
    assert created[2] == removed[2] and created[2].startswith(scaler.RETENTION_MARKER_PREFIX)
    assert len(created) == 3 and len(removed) == 3
    assert docker.volumes() == []
    # the dry run in between did not break coverage
    assert prune(scaler, docker, write_config, at=NOW + 14 * DAY) == [JOB]
    for argv in docker.commands("events"):
        assert "--until" in argv                          # bounded: it never streams
