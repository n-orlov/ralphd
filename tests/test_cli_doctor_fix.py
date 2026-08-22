"""Black-box tests for `ralphctl doctor --fix`'s self-recovery sweep (task
027, issue #8, PRD req F).

`--fix` resumes every run matching the dangling-container condition (recorded
non-terminal, container gone) that is opted in to `auto_resume`, and leaves
the opted-out ones alone while still reporting them. The restart goes through
the same `resume` code path an operator would type, so the assertions here are
on the *recorded* `docker run` argv: mounts, env and labels must reproduce the
original run's wiring.

Uses the recording stub docker from test_cli_docker.py -- no real container,
no real engine.
"""

from __future__ import annotations

import json

import pytest
from test_cli_docker import Ctl

REPO_MOUNT = ":/run/ralphd"


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _base_env(**extra) -> dict:
    """Docker/image checks satisfied so the sweep is the only thing under
    test (see test_cli_doctor_enriched._base_env)."""
    return {"STUB_DOCKER_INSPECT_OK": "1", **extra}


def _start(ctl: Ctl, run_id: str, *extra: str, workspace=None) -> list[str]:
    """Start a run for real (so its wiring on disk is real) and return the
    recorded `docker run` argv."""
    argv = ["start", "--prd", str(ctl.prd), "--llm", "none",
            "--run-id", run_id, *extra]
    if workspace is not None:
        workspace.mkdir(exist_ok=True)
        argv += ["--workspace", str(workspace)]
    res = ctl.run(*argv)
    assert res.returncode == 0, res.stderr
    return _docker_runs(ctl)[-1]


def _docker_runs(ctl: Ctl) -> list[list[str]]:
    return [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]


def _kill_container(ctl: Ctl, run_id: str) -> None:
    """Simulate the container vanishing mid-run: status.json still records
    a non-terminal state, no container by that name exists (the stub's
    STUB_DOCKER_CONTAINERS never lists it)."""
    (ctl.registry / "runs" / run_id / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1,
                    "iterationsUsed": 3}))


def _doctor_fix(ctl: Ctl, *extra: str) -> dict:
    res = ctl.run("--json", "doctor", *extra, env=_base_env(
        STUB_DOCKER_CONTAINERS="some-unrelated-container"))
    assert res.stdout, res.stderr
    return json.loads(res.stdout)


def _mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


def _env_vars(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


# ------------------------------------------------------------ opted in
def test_fix_resumes_an_opted_in_dangling_run(ctl, tmp_path):
    start_argv = _start(ctl, "tst-fix-on", "--auto-resume",
                        workspace=tmp_path / "ws")
    _kill_container(ctl, "tst-fix-on")

    doc = _doctor_fix(ctl, "--fix")
    assert doc["autoResume"] == {"resumed": ["tst-fix-on"], "skipped": [],
                                "failed": [], "waiting": [], "gaveUp": [],
                                "operatorTerminated": [], "recovered": []}
    assert doc["danglingRegistryEntries"] == [
        {"runId": "tst-fix-on", "container": "ralphd-tst-fix-on",
         "liveness": "absent"}]  # task 021 (#31): the dangling shape

    runs = _docker_runs(ctl)
    assert len(runs) == 2, runs          # start + the auto-resume
    resume_argv = runs[1]

    # ... and it reproduces the original wiring: same mounts, same label,
    # same container name, same env (module the port/name-independent bits).
    assert set(_mounts(start_argv)) <= set(_mounts(resume_argv)), (
        _mounts(start_argv), _mounts(resume_argv))
    assert f"{ctl.registry / 'runs' / 'tst-fix-on'}:/run/ralphd" in _mounts(resume_argv)
    assert f"{ctl.registry / 'configs' / 'tst-fix-on'}:/config:ro" in _mounts(resume_argv)
    assert f"{tmp_path / 'ws'}:/workspace" in _mounts(resume_argv)
    assert "ralphd.run=tst-fix-on" in resume_argv
    assert "ralphd-tst-fix-on" in resume_argv
    for entry in _env_vars(start_argv):
        assert entry in _env_vars(resume_argv), entry

    # host.json now points at the fresh container (resume rewrote it)
    meta = json.loads((ctl.registry / "runs" / "tst-fix-on" / "host.json").read_text())
    assert meta["container"] == "f" * 64


def test_fix_reports_the_resumed_run_in_the_human_report(ctl):
    _start(ctl, "tst-fix-report", "--auto-resume")
    _kill_container(ctl, "tst-fix-report")
    res = ctl.run("doctor", "--fix", env=_base_env(
        STUB_DOCKER_CONTAINERS="none"))
    assert "tst-fix-report" in res.stdout
    assert "auto-resumed" in res.stdout, res.stdout


# ------------------------------------------------------------ opted out
def test_fix_leaves_an_opted_out_run_untouched_but_reported(ctl):
    _start(ctl, "tst-fix-off")          # default is off
    _kill_container(ctl, "tst-fix-off")

    doc = _doctor_fix(ctl, "--fix")
    assert doc["autoResume"] == {"resumed": [], "skipped": ["tst-fix-off"],
                                "failed": [], "waiting": [], "gaveUp": [],
                                "operatorTerminated": [], "recovered": []}
    # still reported as dangling, with the manual remedy
    assert doc["danglingRegistryEntries"] == [
        {"runId": "tst-fix-off", "container": "ralphd-tst-fix-off",
         "liveness": "absent"}]
    assert len(_docker_runs(ctl)) == 1, "opted-out run must not be resumed"


def test_fix_sweeps_a_mixed_registry(ctl):
    """One opted-in and one opted-out dangling run in the same sweep:
    exactly one `docker run`, both reported."""
    _start(ctl, "tst-mixed-on", "--auto-resume")
    _start(ctl, "tst-mixed-off")
    _kill_container(ctl, "tst-mixed-on")
    _kill_container(ctl, "tst-mixed-off")

    doc = _doctor_fix(ctl, "--fix")
    assert doc["autoResume"]["resumed"] == ["tst-mixed-on"]
    assert doc["autoResume"]["skipped"] == ["tst-mixed-off"]
    runs = _docker_runs(ctl)
    assert len(runs) == 3, runs         # two starts + one auto-resume
    assert "ralphd-tst-mixed-on" in runs[2]
    assert not any("ralphd-tst-mixed-off" in a for a in runs[2])
    assert {d["runId"] for d in doc["danglingRegistryEntries"]} == {
        "tst-mixed-on", "tst-mixed-off"}


# --------------------------------------------------------- not dangling
def test_fix_does_not_touch_a_run_whose_container_is_alive(ctl):
    _start(ctl, "tst-fix-alive", "--auto-resume")
    (ctl.registry / "runs" / "tst-fix-alive" / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1}))
    res = ctl.run("--json", "doctor", "--fix", env=_base_env(
        STUB_DOCKER_CONTAINERS="ralphd-tst-fix-alive",
        STUB_DOCKER_RUNNING="ralphd-tst-fix-alive"))
    doc = json.loads(res.stdout)
    assert doc["danglingRegistryEntries"] == []
    assert doc["autoResume"] == {"resumed": [], "skipped": [], "failed": [],
                                "waiting": [], "gaveUp": [],
                                "operatorTerminated": [], "recovered": []}
    assert len(_docker_runs(ctl)) == 1


def test_plain_doctor_never_resumes_anything(ctl):
    """Without `--fix` the sweep is report-only (unchanged behaviour):
    `autoResume` is null and no container is started."""
    _start(ctl, "tst-nofix", "--auto-resume")
    _kill_container(ctl, "tst-nofix")
    doc = _doctor_fix(ctl)
    assert doc["autoResume"] is None
    assert doc["danglingRegistryEntries"] == [
        {"runId": "tst-nofix", "container": "ralphd-tst-nofix",
         "liveness": "absent"}]
    assert len(_docker_runs(ctl)) == 1


def test_fix_appears_in_doctor_help(ctl):
    res = ctl.run("doctor", "--help")
    assert res.returncode == 0, res.stderr
    assert "--fix" in res.stdout
    assert "auto_resume" in res.stdout


def test_docs_document_cron_deployment():
    from pathlib import Path
    doc = (Path(__file__).parent.parent / "docs" / "cli.md").read_text()
    assert "doctor --fix" in doc
    lowered = doc.lower()
    assert "cron" in lowered and "systemd" in lowered
