"""v0.38 E (#319 items 1 and 3): a run that ends on its own leaves no siblings.

E.1: when the engine reaches a terminal state (`succeeded`, `failed`,
`exhausted`, `timed_out`, `aborted`) and the job has docker access, it removes
the containers labelled BOTH `ralphd.run=<run>` and `ralphd.role=sibling`
(best effort) and emits `siblings.reaped` with the count and any per-container
error. A run without docker access emits nothing and touches nothing.

E.2: `ralphctl status` of a run that is not running warns with the count of
running `ralphd.run=<run>` containers, and `--json` carries it as
`liveSiblings`.

Nothing here talks to a docker daemon: the engine side drives a fake docker
client through `LoopSupervisor.docker_runner`, the CLI side uses the recording
stub docker binary (tests/stub-docker/docker) behind RALPHD_DOCKER.

Which nodes are controls: the unlabelled-container, other-run, no-docker-access
and job-container nodes pass on the code before this change too (they pin what
must NOT be touched); the rest fail with the behaviour reverted.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
from test_cli_docker import ctl
from test_engine_finishing_state import (
    _abort,
    _events,
    _finish_task,
    _ok,
    _plan,
    _supervisor,
)

from ralphd import siblings
from ralphd.engine import findings
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


__all__ = ["ctl"]

RUN = "v08-finishing"  # the run id `_supervisor` gives its config


class FakeDocker:
    """A fake docker client: `containers` is a list of `{id, labels, running}`;
    `ps` honours repeated `--filter label=k=v` (ANDed) and `-a`; `rm -f`
    removes. Every call's argv is recorded."""

    def __init__(self, containers, fail_rm=()):
        self.containers = list(containers)
        self.fail_rm = set(fail_rm)
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "ps":
            wanted = [argv[i + 1][len("label="):]
                      for i, a in enumerate(argv) if a == "--filter"]
            show_all = any(a.startswith("-") and not a.startswith("--")
                           and "a" in a for a in argv[1:])
            ids = [c["id"] for c in self.containers
                   if (show_all or c.get("running", True))
                   and all(f"{k}={v}" in [f"{kk}={vv}" for kk, vv
                                          in c["labels"].items()]
                           for k, v in (w.split("=", 1) for w in wanted))]
            return subprocess.CompletedProcess(argv, 0, "\n".join(ids), "")
        if argv[0] == "rm":
            cid = argv[-1]
            if cid in self.fail_rm:
                return subprocess.CompletedProcess(argv, 1, "", "boom: busy")
            self.containers = [c for c in self.containers if c["id"] != cid]
            return subprocess.CompletedProcess(argv, 0, cid, "")
        return subprocess.CompletedProcess(argv, 1, "", "unsupported")

    def ids(self):
        return {c["id"] for c in self.containers}


def _c(cid, run=RUN, role="sibling", **extra):
    labels = {} if run is None else {"ralphd.run": run}
    if role:
        labels["ralphd.role"] = role
    return {"id": cid, "labels": {**labels, **extra}, "running": True}


def _world():
    return FakeDocker([
        _c("sibling-1"),                          # ours: must go
        _c("unlabelled", run=None, role=None),    # no labels: must stay
        _c("run-label-only", role=None),          # run label, no role: stay
        _c("other-run", run="someone-else"),      # another run's sibling: stay
        _c("job", role="job"),                    # our own job container: stay
    ])


@pytest.fixture
def docker_access(monkeypatch):
    for k in ("RALPHD_HOST_WORKSPACE", "RALPHD_HOST_WORKSPACES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RALPHD_HOST_RUN_DIR", "/host/run")


@pytest.fixture
def no_docker_access(monkeypatch):
    for k in ("RALPHD_HOST_WORKSPACE", "RALPHD_HOST_WORKSPACES",
              "RALPHD_HOST_RUN_DIR"):
        monkeypatch.delenv(k, raising=False)


def _review_rejects_incurable(sup) -> IterationResult:
    findings.write_findings_json(sup.run.findings_json_file, [
        {"severity": "blocking", "curable": False,
         "clause": "requirement X", "evidence": "never fixable this approach"}])
    return _ok("rejected\n" + json.dumps({
        "verdict": "rejected", "reason": "incurable finding",
        "findings_path": str(sup.run.findings_json_file)}))


def _failed_run(tmp_path, fake):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, [_plan, _finish_task, _review_rejects_incurable])
    sup.docker_runner = fake
    final = _bounded(sup.run_job())
    return sup, final


# -- E.1: the engine reaps on the way out -----------------------------------

def test_failed_run_with_a_labelled_sibling_leaves_none(
        tmp_path, docker_access):
    fake = _world()
    _sup, final = _failed_run(tmp_path, fake)
    assert final == "failed"
    assert "sibling-1" not in fake.ids()


def test_failed_run_emits_siblings_reaped_with_count_one(
        tmp_path, docker_access):
    fake = FakeDocker([_c("sibling-1"), _c("job", role="job")])
    sup, final = _failed_run(tmp_path, fake)
    assert final == "failed"
    evs = _events(sup.run.root, "siblings.reaped")
    assert len(evs) == 1
    assert evs[0]["count"] == 1
    assert evs[0]["errors"] == {}


def test_reap_is_after_the_terminal_state_is_on_disk(tmp_path, docker_access):
    fake = FakeDocker([_c("sibling-1")])
    seen = []
    orig = fake.__call__

    def spying(argv):
        seen.append(RunDir(root=tmp_path / "run").read_status().get("state"))
        return orig(argv)

    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, [_plan, _finish_task, _review_rejects_incurable])
    sup.docker_runner = spying
    _bounded(sup.run_job())
    assert seen and set(seen) == {"failed"}


def test_aborted_run_reaps_too(tmp_path, docker_access):
    fake = FakeDocker([_c("sibling-1")])
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, [_plan, _abort])
    sup.docker_runner = fake
    final = _bounded(sup.run_job())
    assert final == "aborted"
    assert fake.ids() == set()
    assert _events(sup.run.root, "siblings.reaped")[0]["count"] == 1


def test_a_per_container_error_is_reported_and_does_not_stop_the_rest(
        tmp_path, docker_access):
    fake = FakeDocker([_c("stuck"), _c("fine")], fail_rm={"stuck"})
    sup, final = _failed_run(tmp_path, fake)
    assert final == "failed"
    ev = _events(sup.run.root, "siblings.reaped")[0]
    assert ev["count"] == 1
    assert "stuck" in ev["errors"] and "busy" in ev["errors"]["stuck"]
    assert fake.ids() == {"stuck"}


def test_engine_reap_filters_on_both_labels(tmp_path, docker_access):
    fake = _world()
    _failed_run(tmp_path, fake)
    ps = [c for c in fake.calls if c[0] == "ps"]
    assert ps and all("label=ralphd.run=" + RUN in c
                      and "label=ralphd.role=sibling" in c for c in ps)


# controls: pass before and after this change ------------------------------

def test_control_unlabelled_other_run_and_job_containers_are_left_alone(
        tmp_path, docker_access):
    fake = _world()
    _failed_run(tmp_path, fake)
    assert {"unlabelled", "run-label-only", "other-run", "job"} <= fake.ids()
    assert not [c for c in fake.calls if c[0] == "rm"
                and c[-1] in {"unlabelled", "run-label-only", "other-run",
                              "job"}]


def test_control_a_run_without_docker_access_makes_no_docker_call(
        tmp_path, no_docker_access):
    fake = _world()
    sup, final = _failed_run(tmp_path, fake)
    assert final == "failed"
    assert fake.calls == []
    assert _events(sup.run.root, "siblings.reaped") == []
    assert "sibling-1" in fake.ids()


# -- the shared module ------------------------------------------------------

def test_cli_reap_logic_is_the_shared_module_and_keeps_run_label_only():
    from ralphd.cli import main as cli_main
    assert cli_main.siblings_mod is siblings
    fake = _world()
    result = siblings.reap_siblings(RUN, fake, sibling_only=False)
    # host-side stop/rm take the whole run down: job container included
    assert result.count == 3
    assert fake.ids() == {"unlabelled", "other-run"}


# -- E.2: `ralphctl status` warns about live containers ---------------------

def _seed(ctl, run, state):
    run_dir = ctl.registry / "runs" / run
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        json.dumps({"runId": run, "state": state}))


def test_status_of_a_finished_run_warns_with_the_live_container_count(ctl):
    _seed(ctl, "tst-warn", "failed")
    res = ctl.run("status", "tst-warn", env={"STUB_DOCKER_PS_IDS": "a1,b2"})
    assert res.returncode == 0, res.stderr
    warn = [ln for ln in res.stdout.splitlines()
            if ln.startswith("warning:")]
    assert len(warn) == 1
    assert "2 container(s)" in warn[0]
    assert "ralphd.run=tst-warn" in warn[0]
    assert ["ps", "-q", "--filter", "label=ralphd.run=tst-warn"] in ctl.recorded()


def test_status_json_carries_live_siblings(ctl):
    _seed(ctl, "tst-json", "succeeded")
    res = ctl.run("--json", "status", "tst-json",
                  env={"STUB_DOCKER_PS_IDS": "a1,b2,c3"})
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["liveSiblings"] == 3


def test_status_json_live_siblings_zero_when_none_remain(ctl):
    _seed(ctl, "tst-none", "failed")
    res = ctl.run("--json", "status", "tst-none")
    assert json.loads(res.stdout)["liveSiblings"] == 0
    assert "warning:" not in res.stdout


def test_status_warns_for_an_exhausted_hold_too(ctl):
    _seed(ctl, "tst-held", "exhausted")
    res = ctl.run("--json", "status", "tst-held",
                  env={"STUB_DOCKER_PS_IDS": "a1"})
    assert json.loads(res.stdout)["liveSiblings"] == 1


def test_control_status_of_a_running_run_asks_docker_nothing(ctl):
    _seed(ctl, "tst-live", "running")
    res = ctl.run("--json", "status", "tst-live",
                  env={"STUB_DOCKER_PS_IDS": "a1,b2"})
    doc = json.loads(res.stdout)
    assert doc["liveSiblings"] is None
    assert not [c for c in ctl.recorded() if c[:1] == ["ps"]]


@pytest.mark.parametrize("state", ["succeeded", "failed", "exhausted",
                                   "timed_out", "aborted"])
def test_every_terminal_state_reaps(tmp_path, docker_access, state):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, [_plan])
    sup.docker_runner = FakeDocker([_c("sibling-1")])
    sup._reap_siblings_at_terminal(state)
    assert sup.docker_runner.ids() == set()
    assert _events(root, "siblings.reaped")[0]["count"] == 1


def test_control_a_non_terminal_state_reaps_nothing(tmp_path, docker_access):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, [_plan])
    sup.docker_runner = FakeDocker([_c("sibling-1")])
    sup._reap_siblings_at_terminal("running")
    assert sup.docker_runner.calls == []
