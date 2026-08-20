"""`ralphctl rm --force` -- stop then delete in one command (task 029, #19).

Two tiers, both black-box over the real `ralphctl` executable:

* the recording stub docker (tests/stub-docker/docker), for the precise
  argv/exit-code contracts: which containers get removed, in which order
  relative to `stop`'s own sequence, and what is left untouched on a refusal;
* the real docker daemon (same style as test_cli_docker_integration.py), for
  the actual effect: a started run's job container, sibling, run dir and
  config dir are all gone afterwards -- and none of them is touched when the
  run's recorded state says the job is still working.

The invariant under test is deliberately narrow: `--force` is a shortcut past
a *stale* container, never a way to kill live work (PRD G / issue #19).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
RALPHCTL = Path(sys.executable).parent / "ralphctl"
DOCKER_SOCK = "/var/run/docker.sock"
IMAGE = "alpine:3"  # tiny stand-in for the job image


class Ctl:
    """ralphctl runner bound to a tmp registry + the recording stub docker."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.log = tmp / "docker-argv.jsonl"

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {
            **os.environ,
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            **(env or {}),
        }
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=60)

    def recorded(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def forget_docker(self) -> None:
        self.log.unlink(missing_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.registry / "runs" / run_id

    def config_dir(self, run_id: str) -> Path:
        return self.registry / "configs" / run_id

    def seed(self, run_id: str, state: str | None = "succeeded",
             status: str | None = None) -> None:
        """A run dir + config dir on disk, as `start` would have left them."""
        rdir = self.run_dir(run_id)
        rdir.mkdir(parents=True)
        (rdir / "iterations").mkdir()
        if status is not None:
            (rdir / "status.json").write_text(status)
        elif state is not None:
            (rdir / "status.json").write_text(json.dumps({"state": state}))
        cdir = self.config_dir(run_id)
        cdir.mkdir(parents=True)
        (cdir / "job.yaml").write_text("iterations: 3\n")


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


# container "exists" (plain `docker inspect <name>` exits 0)
HAS_CONTAINER = {"STUB_DOCKER_INSPECT_OK": "1"}


def rm_targets(rec: list[list[str]]) -> list[str]:
    return [a[2] for a in rec if a[:2] == ["rm", "-f"]]


# --------------------------------------------------------------------------
# stub-docker tier: exit codes, docker argv, what survives a refusal
# --------------------------------------------------------------------------
def test_plain_rm_still_refuses_a_leftover_container_and_touches_nothing(ctl):
    """The safe default is unchanged: plain `rm` says `stop` first, exits 5,
    removes no container and deletes neither directory."""
    ctl.seed("tst-plain")
    res = ctl.run("rm", "tst-plain", "--yes", env=HAS_CONTAINER)

    assert res.returncode == 5
    assert "container still exists" in res.stderr
    assert "`stop` first" in res.stderr
    assert rm_targets(ctl.recorded()) == []
    assert ctl.run_dir("tst-plain").exists()
    assert ctl.config_dir("tst-plain").exists()


def test_plain_rm_error_now_mentions_the_force_shortcut(ctl):
    """Discoverability: the refusal names the one-command alternative."""
    ctl.seed("tst-hint")
    res = ctl.run("rm", "tst-hint", "--yes", env=HAS_CONTAINER)
    assert "rm --force" in res.stderr


def test_rm_force_stops_the_container_then_deletes_everything(ctl):
    """The headline case: a finished run whose container record is still
    around is gone -- container, sibling, run dir, config dir -- in one
    command, where plain `rm` exits 5."""
    ctl.seed("tst-force", state="succeeded")
    res = ctl.run("rm", "tst-force", "--yes", "--force",
                  env={**HAS_CONTAINER, "STUB_DOCKER_PS_IDS": "sib1,sib2"})

    assert res.returncode == 0, res.stderr
    rec = ctl.recorded()
    assert ["rm", "-f", "ralphd-tst-force"] in rec
    assert ["ps", "-aq", "--filter", "label=ralphd.run=tst-force"] in rec
    assert ["rm", "-f", "sib1"] in rec
    assert ["rm", "-f", "sib2"] in rec
    # #7 discipline unchanged: host-side reaping filters on the run id ALONE
    assert not any("ralphd.role" in a for argv in rec for a in argv)
    assert not ctl.run_dir("tst-force").exists()
    assert not ctl.config_dir("tst-force").exists()
    assert "removed tst-force" in res.stdout


def test_rm_force_reuses_stops_container_teardown_sequence(ctl):
    """PRD G: `--force` reuses `stop`'s path rather than re-implementing it,
    so the docker calls it makes are `stop`'s, in `stop`'s order."""
    ctl.seed("tst-seq", state="failed")
    ctl.run("stop", "tst-seq", env={**HAS_CONTAINER,
                                    "STUB_DOCKER_PS_IDS": "sibA"})
    stop_argv = [a for a in ctl.recorded() if a[0] != "inspect"]
    ctl.forget_docker()

    ctl.seed("tst-seq2", state="failed")
    ctl.run("rm", "tst-seq2", "--yes", "--force",
            env={**HAS_CONTAINER, "STUB_DOCKER_PS_IDS": "sibA"})
    rm_argv = [a for a in ctl.recorded() if a[0] != "inspect"]

    # identical modulo the run id the two commands were given
    assert [[x.replace("tst-seq2", "tst-seq") for x in a] for a in rm_argv] \
        == stop_argv


def test_rm_force_refuses_a_running_job_and_touches_nothing(ctl):
    """`--force` is not a kill switch: a job whose recorded state is still
    non-terminal survives untouched, container included."""
    ctl.seed("tst-live", state="running")
    res = ctl.run("rm", "tst-live", "--yes", "--force",
                  env={**HAS_CONTAINER, "STUB_DOCKER_PS_IDS": "sib1"})

    assert res.returncode != 0
    assert res.returncode == 5
    assert "job still running" in res.stderr
    assert "abort" in res.stderr
    assert rm_targets(ctl.recorded()) == []     # no container removed
    assert ctl.recorded() == [["inspect", "ralphd-tst-live"]]  # not even a ps
    assert ctl.run_dir("tst-live").exists()
    assert (ctl.run_dir("tst-live") / "status.json").exists()
    assert ctl.config_dir("tst-live").exists()


def test_rm_force_refuses_a_starting_job(ctl):
    ctl.seed("tst-start", state="starting")
    res = ctl.run("rm", "tst-start", "--yes", "--force", env=HAS_CONTAINER)
    assert res.returncode == 5
    assert "state: starting" in res.stderr
    assert ctl.run_dir("tst-start").exists()


@pytest.mark.parametrize("kwargs", [
    {"state": None},                              # no status.json at all
    {"status": "{ this is not json"},              # unreadable
    {"status": json.dumps({"state": "wat"})},      # unrecognized state
])
def test_rm_force_refuses_when_it_cannot_establish_the_job_is_over(ctl, kwargs):
    """Absent/unreadable/unrecognized state is not permission: we cannot
    establish the job finished, so `--force` declines instead of guessing
    (the `unknown is not zero` rule applied to a destructive action)."""
    ctl.seed("tst-mystery", **kwargs)
    res = ctl.run("rm", "tst-mystery", "--yes", "--force", env=HAS_CONTAINER)
    assert res.returncode == 5
    assert "job still running" in res.stderr
    assert rm_targets(ctl.recorded()) == []
    assert ctl.run_dir("tst-mystery").exists()
    assert ctl.config_dir("tst-mystery").exists()


def test_rm_force_on_a_run_with_no_container_is_plain_rm(ctl):
    """With no container record there is nothing to stop: `--force` neither
    adds a docker removal nor changes the refusal-free plain-rm path (and a
    zombie run dir left saying `running` is still deletable, as before)."""
    ctl.seed("tst-nocont", state="running")
    res = ctl.run("rm", "tst-nocont", "--yes", "--force",
                  env={"STUB_DOCKER_PS_IDS": "sib7"})

    assert res.returncode == 0, res.stderr
    rec = ctl.recorded()
    assert ["rm", "-f", "ralphd-tst-nocont"] not in rec
    assert ["rm", "-f", "sib7"] in rec          # siblings still reaped
    assert not ctl.run_dir("tst-nocont").exists()
    assert not ctl.config_dir("tst-nocont").exists()
    assert res.stdout.strip().endswith("removed tst-nocont")


def test_rm_force_unknown_run_exits_3_without_removing_anything(ctl):
    res = ctl.run("rm", "tst-ghost", "--yes", "--force", env=HAS_CONTAINER)
    assert res.returncode == 3
    assert "not found" in res.stderr
    assert rm_targets(ctl.recorded()) == []


def test_rm_json_reports_whether_a_container_was_stopped(ctl):
    ctl.seed("tst-json", state="aborted")
    res = ctl.run("--json", "rm", "tst-json", "--yes", "--force",
                  env=HAS_CONTAINER)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"removed": "tst-json",
                                      "stoppedContainer": True}

    ctl.seed("tst-json2", state="succeeded")
    res = ctl.run("--json", "rm", "tst-json2", "--yes")
    assert json.loads(res.stdout) == {"removed": "tst-json2",
                                      "stoppedContainer": False}


def test_rm_force_is_advertised_in_help(ctl):
    res = ctl.run("rm", "--help")
    assert "--force" in res.stdout
    assert res.returncode == 0


# --------------------------------------------------------------------------
# real-daemon tier: the actual effect on real containers
# --------------------------------------------------------------------------
def _docker_available() -> bool:
    if not shutil.which("docker") or not Path(DOCKER_SOCK).is_socket():
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


def docker(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *argv], capture_output=True, text=True,
                          timeout=120)


class RealCtl(Ctl):
    """Same runner, real docker daemon (RALPHD_DOCKER unset)."""

    def __init__(self, tmp: Path):
        super().__init__(tmp)
        self.prd = tmp / "prd.md"
        self.prd.write_text("# rm --force PRD\n\nDo the thing.\n")
        self.cleanup: list[str] = []

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {**os.environ, "RALPHD_REGISTRY": str(self.registry),
                    **(env or {})}
        full_env.pop("RALPHD_DOCKER", None)
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=120)

    def start(self, run_id: str, *extra: str) -> subprocess.CompletedProcess:
        self.cleanup.append(f"ralphd-{run_id}")
        return self.run("start", "--prd", str(self.prd), "--llm", "none",
                        "--run-id", run_id, "--image", IMAGE, *extra)

    def record_state(self, run_id: str, state: str) -> None:
        (self.run_dir(run_id) / "status.json").write_text(
            json.dumps({"state": state, "schemaVersion": 1}))


@pytest.fixture
def real_ctl(tmp_path):
    c = RealCtl(tmp_path)
    yield c
    for name in c.cleanup:
        docker("rm", "-f", name)


real_docker = pytest.mark.skipif(not _docker_available(),
                                 reason="real docker daemon not available")


def rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@real_docker
def test_real_rm_force_leaves_no_container_run_dir_or_config_dir(real_ctl):
    run_id = rid("itg-rmf")
    assert real_ctl.start(run_id).returncode == 0
    sib = f"sib-{run_id}"
    real_ctl.cleanup.append(sib)
    res = docker("run", "-d", "--name", sib, "--label", f"ralphd.run={run_id}",
                 "busybox:stable", "sleep", "300")
    assert res.returncode == 0, res.stderr
    real_ctl.record_state(run_id, "succeeded")

    # plain rm refuses while the container record is there
    res = real_ctl.run("rm", run_id, "--yes")
    assert res.returncode == 5, res.stdout
    assert docker("inspect", f"ralphd-{run_id}").returncode == 0

    res = real_ctl.run("rm", run_id, "--yes", "--force")
    assert res.returncode == 0, res.stderr
    assert docker("inspect", f"ralphd-{run_id}").returncode != 0
    assert docker("inspect", sib).returncode != 0
    assert not real_ctl.run_dir(run_id).exists()
    assert not real_ctl.config_dir(run_id).exists()


@real_docker
def test_real_rm_force_on_a_working_job_touches_nothing(real_ctl):
    run_id = rid("itg-rmlive")
    assert real_ctl.start(run_id).returncode == 0
    real_ctl.record_state(run_id, "running")

    res = real_ctl.run("rm", run_id, "--yes", "--force")
    assert res.returncode == 5, res.stdout
    assert "job still running" in res.stderr
    assert docker("inspect", f"ralphd-{run_id}").returncode == 0
    assert real_ctl.run_dir(run_id).exists()
    assert real_ctl.config_dir(run_id).exists()
