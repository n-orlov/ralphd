"""Black-box tests for `ralphctl resume <run-id> [--iterations +N]` (PRD req
16, CLI side / task 029) and for the `pause`/`unpause` rename that freed up
the `resume` verb for this feature.

`resume` reuses the stub-docker recording harness from test_cli_docker.py
(no real container, no real engine needed to prove the docker-run wiring);
the `unpause` rename is proven against a real (no-Docker) engine via the
`live` fixture in conftest.py, since it talks to a live API endpoint.
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock

__all__ = ["ctl", "unix_sock"]


def _seed_run(c: Ctl, run_id: str, *, iterations: int = 5, workspace=None,
              token: str | None = None) -> tuple:
    rdir = c.registry / "runs" / run_id
    cdir = c.registry / "configs" / run_id
    rdir.mkdir(parents=True)
    cdir.mkdir(parents=True)
    (cdir / "job.yaml").write_text(
        f"run_id: {json.dumps(run_id)}\niterations: {iterations}\n"
        "max_approaches: 1\n")
    host_meta = {"runId": run_id, "container": "f" * 12, "port": 1234,
                 "apiUrl": "http://127.0.0.1:1234", "image": "ralphd:dev",
                 "startedAt": "2024-01-01T00:00:00Z"}
    if workspace is not None:
        host_meta["workspace"] = str(workspace)
    (rdir / "host.json").write_text(json.dumps(host_meta))
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    if token is not None:
        (rdir / ".api-token").write_text(token)
    return rdir, cdir


def _run_argv(c: Ctl) -> list[str]:
    runs = [a for a in c.recorded() if a[:2] == ["run", "-d"]]
    assert len(runs) == 1, f"expected one docker run, got: {c.recorded()}"
    return runs[0]


def env_vars(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


# --------------------------------------------------------------------------
def test_resume_unknown_run_exits_3(ctl):
    res = ctl.run("resume", "no-such-run")
    assert res.returncode == 3, res.stderr
    assert "not found" in res.stderr
    assert ctl.recorded() == []


def test_resume_refuses_when_container_still_running(ctl):
    _seed_run(ctl, "tst-alive")
    res = ctl.run("resume", "tst-alive", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-alive",
        "STUB_DOCKER_RUNNING": "ralphd-tst-alive",
    })
    assert res.returncode == 5, res.stderr
    assert "still running" in res.stderr
    # never even attempted a docker run
    assert not any(a[:2] == ["run", "-d"] for a in ctl.recorded())


def test_resume_removes_stopped_container_and_reuses_mounts(ctl, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rdir, cdir = _seed_run(ctl, "tst-dead", workspace=ws, token="secret-tok")
    res = ctl.run("resume", "tst-dead", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-dead",
        "STUB_DOCKER_RUNNING": "",  # exists but stopped
    })
    assert res.returncode == 0, res.stderr
    rec = ctl.recorded()
    # the stale (stopped) container is removed before the new one starts
    rm_idx = rec.index(["rm", "-f", "ralphd-tst-dead"])
    run_idx = next(i for i, a in enumerate(rec) if a[:2] == ["run", "-d"])
    assert rm_idx < run_idx

    argv = _run_argv(ctl)
    assert f"{rdir}:/run/ralphd" in argv
    assert f"{cdir}:/config:ro" in argv
    assert f"{ws}:/workspace" in argv
    ev = env_vars(argv)
    assert "RALPHD_API_TOKEN=secret-tok" in ev
    # a fresh host.json was written, workspace preserved
    meta = json.loads((rdir / "host.json").read_text())
    assert meta["workspace"] == str(ws)
    assert meta["container"] == "f" * 64  # stub's fake id


def test_resume_without_prior_container_skips_rm(ctl):
    _seed_run(ctl, "tst-fresh")
    res = ctl.run("resume", "tst-fresh")  # no STUB_DOCKER_CONTAINERS at all
    assert res.returncode == 0, res.stderr
    rec = ctl.recorded()
    assert not any(a[:2] == ["rm", "-f"] for a in rec)
    assert any(a[:2] == ["run", "-d"] for a in rec)


def test_resume_iterations_topup_bumps_job_yaml(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-topup", iterations=5)
    res = ctl.run("resume", "tst-topup", "--iterations", "+10")
    assert res.returncode == 0, res.stderr
    job_text = (cdir / "job.yaml").read_text()
    lines = dict(line.split(": ", 1) for line in job_text.splitlines() if line)
    assert json.loads(lines["iterations"]) == 15


def test_resume_iterations_absolute_value(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-abs", iterations=5)
    res = ctl.run("resume", "tst-abs", "--iterations", "30")
    assert res.returncode == 0, res.stderr
    job_text = (cdir / "job.yaml").read_text()
    lines = dict(line.split(": ", 1) for line in job_text.splitlines() if line)
    assert json.loads(lines["iterations"]) == 30


def test_resume_bad_iterations_value_exits_2(ctl):
    _seed_run(ctl, "tst-bad")
    res = ctl.run("resume", "tst-bad", "--iterations", "banana")
    assert res.returncode == 2, res.stderr
    assert "invalid value" in res.stderr
    assert not any(a[:2] == ["run", "-d"] for a in ctl.recorded())


def test_resume_no_workspace_recorded_omits_workspace_mount(ctl):
    _seed_run(ctl, "tst-nows")  # no workspace kwarg
    res = ctl.run("resume", "tst-nows")
    assert res.returncode == 0, res.stderr
    argv = _run_argv(ctl)
    assert not any(v.endswith(":/workspace") for v in argv)


def test_resume_allow_docker_reinjects_socket(ctl, unix_sock):
    _seed_run(ctl, "tst-dock2")
    res = ctl.run("resume", "tst-dock2", "--allow-docker",
                  env={"RALPHD_DOCKER_SOCK": str(unix_sock)})
    assert res.returncode == 0, res.stderr
    argv = _run_argv(ctl)
    assert f"{unix_sock}:/var/run/docker.sock" in argv
    assert "ROOT-EQUIVALENT" in res.stderr


# --------------------------------------------------------------------------
def test_unpause_hits_resume_endpoint(live):
    """The rename (old `resume` -> `unpause`) must still hit the engine's
    `POST /resume` (not a no-op / not renamed to a nonexistent route):
    the response body's `resumed: true` only comes from that handler."""
    run = live(run_id="unpause-test",
              job={"iterations": 2, "max_approaches": 1, "on_complete": "idle"})
    run.wait_terminal()
    res = run.ralphctl("--json", "unpause", run.run_id)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert json.loads(res.stdout) == {"resumed": True}

