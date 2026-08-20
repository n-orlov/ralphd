"""The hub's delete endpoint -- `DELETE /api/runs/<id>` (task 030, #19).

One-command delete from the hub: the same removal `ralphctl rm --force`
performs (job container, siblings, run dir, job config dir), behind a gate
that is deliberately STRICTER than the CLI's -- a recorded terminal state, or
a 409 naming the reason and touching nothing at all.

Three tiers, in the house style:

* an in-process unit tier on `ui_server.deletion_refusal`, the gate's wording
  and its one policy decision;
* the black-box hub tier: the real `ralphctl ui` subprocess pointed at a temp
  registry with the recording stub docker (tests/stub-docker/docker) as
  `RALPHD_DOCKER`, so the precise docker argv, exit codes and what survives a
  refusal are assertions rather than claims -- including that the hub's argv
  is `ralphctl rm --force`'s own argv modulo the run id;
* one real-daemon case (same style as tests/test_cli_rm_force.py): a run
  started for real, then deleted through the hub's HTTP endpoint, leaving no
  container, run dir or config dir.
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

from ralphd.cli import ui_server
from ralphd.engine.state import NONTERMINAL_STATES, TERMINAL_STATES

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import UiServer, ui

# re-exported so the imported `ui` fixture is not flagged as unused
__all__ = ["UiServer", "ui"]

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
RALPHCTL = Path(sys.executable).parent / "ralphctl"
DOCKER_SOCK = "/var/run/docker.sock"
IMAGE = "alpine:3"  # tiny stand-in for the job image


def _seed(registry: Path, run_id: str, state: str | None = "succeeded",
          status: str | None = None) -> Path:
    """A run dir + config dir on disk, as `start` would have left them."""
    rdir = registry / "runs" / run_id
    rdir.mkdir(parents=True)
    (rdir / "iterations").mkdir()
    if status is not None:
        (rdir / "status.json").write_text(status)
    elif state is not None:
        (rdir / "status.json").write_text(
            json.dumps({"runId": run_id, "state": state, "schemaVersion": 1}))
    (rdir / "tasks.json").write_text(json.dumps({"tasks": []}))
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    (cdir / "job.yaml").write_text("iterations: 3\n")
    return rdir


class Hub:
    """A `ralphctl ui` server over a temp registry + the stub docker."""

    def __init__(self, make, tmp_path: Path, **docker_env: str):
        self.registry = tmp_path / "registry"
        (self.registry / "runs").mkdir(parents=True, exist_ok=True)
        self.log = tmp_path / "docker-argv.jsonl"
        self.server = make(self.registry, {
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "STUB_DOCKER_LOG": str(self.log),
            **docker_env,
        })

    def seed(self, run_id: str, **kw) -> Path:
        return _seed(self.registry, run_id, **kw)

    def delete(self, run_id: str) -> tuple[int, dict]:
        return self.server.delete(f"/api/runs/{run_id}")

    def recorded(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run_dir(self, run_id: str) -> Path:
        return self.registry / "runs" / run_id

    def config_dir(self, run_id: str) -> Path:
        return self.registry / "configs" / run_id

    def run_ids(self) -> list[str]:
        _, body = self.server.get("/api/runs")
        return [r["runId"] for r in body["runs"]]


# container "exists" (plain `docker inspect <name>` exits 0)
HAS_CONTAINER = {"STUB_DOCKER_INSPECT_OK": "1"}


def rm_targets(rec: list[list[str]]) -> list[str]:
    return [a[2] for a in rec if a[:2] == ["rm", "-f"]]


# --------------------------------------------------------------------------
# unit tier: the gate and its wording
# --------------------------------------------------------------------------
@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_a_terminal_run_may_be_deleted(state):
    assert ui_server.deletion_refusal({"state": state}) is None


@pytest.mark.parametrize("state", NONTERMINAL_STATES)
def test_an_active_run_is_refused_with_its_state_named(state):
    reason = ui_server.deletion_refusal({"state": state})
    assert reason == ui_server.DELETE_REFUSED_ACTIVE.format(state=state)
    assert state in reason


@pytest.mark.parametrize("status", [
    {},                                 # no status.json / no state key
    {"state": None},
    {"state": ""},
    {"state": "wat"},                   # unrecognized by this build
    {"state": "SUCCEEDED"},             # not the recorded spelling
])
def test_a_state_we_cannot_read_is_not_permission(status):
    """`unknown is not zero` with teeth: an absent, empty or unrecognized
    state is refused in its own words -- neither silently deleted nor
    mislabelled as an active run."""
    reason = ui_server.deletion_refusal(status)
    assert reason
    assert reason != ui_server.DELETE_REFUSED_ACTIVE.format(
        state=status.get("state"))
    assert reason == ui_server.DELETE_REFUSED_UNKNOWN.format(
        state=status.get("state") or ui_server.UNKNOWN_STATE)


def test_the_unknown_refusal_points_at_the_cli_escape_hatch():
    """The hub refuses what the CLI can still do, so it says so."""
    reason = ui_server.deletion_refusal({})
    assert ui_server.UNKNOWN_STATE in reason
    assert "rm --force" in reason


def test_the_gate_is_stricter_than_the_cli_on_a_zombie_run_dir():
    """`ralphctl rm --force` deletes a run dir recording `running` whose
    container is already gone (tests/test_cli_rm_force.py); a one-click
    button in a browser must not, because the hub cannot establish the job
    is over."""
    assert ui_server.deletion_refusal({"state": "running"})


# --------------------------------------------------------------------------
# hub tier: the endpoint, over the recording stub docker
# --------------------------------------------------------------------------
def test_delete_stops_the_container_then_removes_everything(ui, tmp_path):
    """The headline case: a finished run whose container record is still
    around is gone -- container, sibling, run dir, config dir -- in one
    HTTP call, and it leaves the run list."""
    hub = Hub(ui, tmp_path, **HAS_CONTAINER, STUB_DOCKER_PS_IDS="sib1,sib2")
    hub.seed("hub-done", state="succeeded")
    assert "hub-done" in hub.run_ids()

    code, body = hub.delete("hub-done")

    assert code == 200, body
    assert body == {"removed": "hub-done", "stoppedContainer": True}
    rec = hub.recorded()
    assert ["rm", "-f", "ralphd-hub-done"] in rec
    assert ["ps", "-aq", "--filter", "label=ralphd.run=hub-done"] in rec
    assert ["rm", "-f", "sib1"] in rec
    assert ["rm", "-f", "sib2"] in rec
    # #7 discipline unchanged: host-side reaping filters on the run id ALONE
    assert not any("ralphd.role" in a for argv in rec for a in argv)
    assert not hub.run_dir("hub-done").exists()
    assert not hub.config_dir("hub-done").exists()
    assert hub.run_ids() == []


def test_delete_of_a_run_with_no_container_still_reaps_siblings(ui, tmp_path):
    hub = Hub(ui, tmp_path, STUB_DOCKER_PS_IDS="sib7")
    hub.seed("hub-nocont", state="failed")

    code, body = hub.delete("hub-nocont")

    assert code == 200, body
    assert body == {"removed": "hub-nocont", "stoppedContainer": False}
    rec = hub.recorded()
    assert ["rm", "-f", "ralphd-hub-nocont"] not in rec
    assert ["rm", "-f", "sib7"] in rec          # siblings still reaped
    assert not hub.run_dir("hub-nocont").exists()
    assert not hub.config_dir("hub-nocont").exists()


def test_delete_uses_the_cli_removal_sequence(ui, tmp_path):
    """The hub does not grow a second teardown: the docker calls it makes are
    `ralphctl rm --force`'s calls, in `rm --force`'s order (which task 029
    already pins to `stop`'s)."""
    hub = Hub(ui, tmp_path, **HAS_CONTAINER, STUB_DOCKER_PS_IDS="sibA")
    hub.seed("hub-seq", state="succeeded")
    hub.delete("hub-seq")
    hub_argv = hub.recorded()

    cli_log = tmp_path / "cli-argv.jsonl"
    _seed(hub.registry, "cli-seq", state="succeeded")
    res = subprocess.run(
        [str(RALPHCTL), "rm", "cli-seq", "--yes", "--force"],
        env={**os.environ, "RALPHD_REGISTRY": str(hub.registry),
             "RALPHD_DOCKER": str(STUB_DOCKER), "STUB_DOCKER_LOG": str(cli_log),
             "STUB_DOCKER_INSPECT_OK": "1", "STUB_DOCKER_PS_IDS": "sibA"},
        capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    cli_argv = [json.loads(line) for line in cli_log.read_text().splitlines()]

    # identical modulo the run id each surface was given
    assert [[x.replace("hub-seq", "cli-seq") for x in a] for a in hub_argv] \
        == cli_argv


@pytest.mark.parametrize("state", NONTERMINAL_STATES)
def test_delete_refuses_an_active_run_and_touches_nothing(ui, tmp_path, state):
    hub = Hub(ui, tmp_path, **HAS_CONTAINER, STUB_DOCKER_PS_IDS="sib1")
    hub.seed(f"hub-{state}", state=state)

    code, body = hub.delete(f"hub-{state}")

    assert code == 409
    assert body["error"] == ui_server.DELETE_REFUSED_ACTIVE.format(state=state)
    assert body == {"error": body["error"], "runId": f"hub-{state}",
                    "state": state}
    # not even a `docker inspect`: a refusal is decided from the recorded
    # state alone
    assert hub.recorded() == []
    assert (hub.run_dir(f"hub-{state}") / "status.json").exists()
    assert hub.config_dir(f"hub-{state}").exists()
    assert f"hub-{state}" in hub.run_ids()


@pytest.mark.parametrize("kwargs,named", [
    ({"state": None}, ui_server.UNKNOWN_STATE),        # no status.json at all
    ({"status": "{ this is not json"}, ui_server.UNKNOWN_STATE),  # unreadable
    ({"status": json.dumps({"state": "wat"})}, "wat"),  # unrecognized state
])
def test_delete_refuses_when_it_cannot_establish_the_job_is_over(
        ui, tmp_path, kwargs, named):
    hub = Hub(ui, tmp_path, **HAS_CONTAINER)
    hub.seed("hub-mystery", **kwargs)

    code, body = hub.delete("hub-mystery")

    assert code == 409
    assert body["error"] == ui_server.DELETE_REFUSED_UNKNOWN.format(state=named)
    assert hub.recorded() == []
    assert hub.run_dir("hub-mystery").exists()
    assert hub.config_dir("hub-mystery").exists()


def test_delete_of_an_unknown_run_is_404(ui, tmp_path):
    hub = Hub(ui, tmp_path, **HAS_CONTAINER)
    code, body = hub.delete("hub-ghost")
    assert code == 404
    assert "not found" in body["error"]
    assert hub.recorded() == []


@pytest.mark.parametrize("run_id", [
    "..", "%2e%2e", "%2e%2e%2f%2e%2e", "%2fetc", ".",
])
def test_delete_refuses_a_traversal_shaped_run_id(ui, tmp_path, run_id):
    """The id arrives as one URL segment and ends up at `shutil.rmtree`:
    anything that is not the plain name of a direct child of
    `<registry>/runs` is a 404, and the registry survives intact."""
    hub = Hub(ui, tmp_path, **HAS_CONTAINER)
    hub.seed("hub-keep", state="succeeded")

    code, _ = hub.delete(run_id)

    assert code == 404
    assert hub.recorded() == []
    assert hub.run_dir("hub-keep").exists()
    assert (hub.registry / "runs").is_dir()
    assert (hub.registry / "configs").is_dir()


def test_delete_is_not_offered_on_other_paths(ui, tmp_path):
    hub = Hub(ui, tmp_path, **HAS_CONTAINER)
    hub.seed("hub-paths", state="succeeded")
    for path in ("/api/runs", "/api/runs/hub-paths/logs",
                 "/api/runs/hub-paths/iterations/1", "/api/nope"):
        code, body = hub.server.delete(path)
        assert code == 404, (path, body)
    assert hub.run_dir("hub-paths").exists()
    assert hub.recorded() == []


def test_a_deleted_run_is_gone_from_the_detail_view_too(ui, tmp_path):
    hub = Hub(ui, tmp_path)
    hub.seed("hub-detail", state="aborted")
    code, _ = hub.server.get("/api/runs/hub-detail")
    assert code == 200
    assert hub.delete("hub-detail")[0] == 200
    code, body = hub.server.get("/api/runs/hub-detail")
    assert code == 404
    assert "not found" in body["error"]


def test_deleting_one_run_leaves_the_others_alone(ui, tmp_path):
    hub = Hub(ui, tmp_path, STUB_DOCKER_PS_IDS="sib1")
    for rid_ in ("hub-a", "hub-b", "hub-c"):
        hub.seed(rid_, state="succeeded")

    assert hub.delete("hub-b")[0] == 200

    assert sorted(hub.run_ids()) == ["hub-a", "hub-c"]
    assert hub.run_dir("hub-a").exists()
    assert hub.config_dir("hub-c").exists()
    # the reaping filter named the deleted run only
    assert ["ps", "-aq", "--filter", "label=ralphd.run=hub-b"] in hub.recorded()
    assert not any("hub-a" in a or "hub-c" in a
                   for argv in hub.recorded() for a in argv)


# --------------------------------------------------------------------------
# real-daemon tier: the actual effect on a real run
# --------------------------------------------------------------------------
def _docker_available() -> bool:
    if not shutil.which("docker") or not Path(DOCKER_SOCK).is_socket():
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


def docker(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *argv], capture_output=True, text=True,
                          timeout=120)


real_docker = pytest.mark.skipif(not _docker_available(),
                                 reason="real docker daemon not available")


@real_docker
def test_real_hub_delete_leaves_no_container_run_dir_or_config_dir(ui, tmp_path):
    """End to end over the real daemon: a run started by `ralphctl start`,
    its recorded state moved to terminal, then deleted through the hub's HTTP
    endpoint -- container, sibling, run dir and config dir all gone."""
    registry = tmp_path / "registry"
    registry.mkdir()
    prd = tmp_path / "prd.md"
    prd.write_text("# hub delete PRD\n\nDo the thing.\n")
    run_id = f"itg-hubdel-{uuid.uuid4().hex[:8]}"
    sib = f"sib-{run_id}"
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    env.pop("RALPHD_DOCKER", None)
    try:
        res = subprocess.run(
            [str(RALPHCTL), "start", "--prd", str(prd), "--llm", "none",
             "--run-id", run_id, "--image", IMAGE],
            env=env, capture_output=True, text=True, timeout=180)
        assert res.returncode == 0, res.stderr
        assert docker("run", "-d", "--name", sib, "--label",
                      f"ralphd.run={run_id}", "--label", "ralphd.role=sibling",
                      "busybox:stable", "sleep", "300").returncode == 0
        (registry / "runs" / run_id / "status.json").write_text(
            json.dumps({"state": "succeeded", "schemaVersion": 1}))

        server = ui(registry)
        code, body = server.delete(f"/api/runs/{run_id}")

        assert code == 200, body
        assert body == {"removed": run_id, "stoppedContainer": True}
        assert docker("inspect", f"ralphd-{run_id}").returncode != 0
        assert docker("inspect", sib).returncode != 0
        assert not (registry / "runs" / run_id).exists()
        assert not (registry / "configs" / run_id).exists()
    finally:
        docker("rm", "-f", f"ralphd-{run_id}")
        docker("rm", "-f", sib)
