"""Black-box tests for task 001: `ralphctl resume` must reproduce the
resolved `--forward-env`/`--llm-env`/`--env` values from `start` time, not
whatever the resuming shell happens to have (or lack).

Uses the same stub-docker recording harness as test_cli_docker.py /
test_cli_resume_llm_wiring.py -- no real container, no real engine needed
to prove the docker-run argv wiring is reproduced correctly.
"""

from __future__ import annotations

import json
import os

from test_cli_docker import ctl, unix_sock
from test_cli_resume_llm_wiring import _stop_container, docker_run_argv, env_vars

__all__ = ["ctl", "unix_sock"]


def test_start_persists_extra_env_wiring_file(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-extraenv",
                  "--forward-env", "PROBE_FWD_*", "--llm-env", "LLMK=llmv",
                  "--env", "GENERIC=genv",
                  env={"PROBE_FWD_ONE": "fwd-secret-1"})
    assert res.returncode == 0, res.stderr

    wiring_path = ctl.registry / "configs" / "tst-extraenv" / "env-wiring.json"
    assert wiring_path.is_file()
    assert oct(wiring_path.stat().st_mode)[-3:] == "600"
    doc = json.loads(wiring_path.read_text())
    assert doc["extra_env"] == [
        "PROBE_FWD_ONE=fwd-secret-1", "LLMK=llmv", "GENERIC=genv"]

    ev = env_vars(docker_run_argv(ctl))
    assert "PROBE_FWD_ONE=fwd-secret-1" in ev
    assert "LLMK=llmv" in ev
    assert "GENERIC=genv" in ev


def test_resume_reproduces_forward_env_llm_env_and_env(ctl):
    """The exact live-incident scenario from the PRD: `--forward-env
    'AWS_*'` carrying a Bedrock bearer token must survive `resume` even
    when the resuming shell has wiped/changed those vars. Uses a
    `PROBE_AWS_*` prefix (rather than the real `AWS_*`) so this test can't
    accidentally pass/fail depending on genuine AWS_* vars already present
    in whatever environment runs the test suite."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-resumeenv",
                  "--forward-env", "PROBE_AWS_*", "--llm-env", "EXTRA_KEY=extra-orig",
                  "--env", "MY_FLAG=orig-flag",
                  env={"PROBE_AWS_BEARER_TOKEN_BEDROCK": "bedrock-secret-orig",
                       "PROBE_AWS_REGION": "us-east-1"})
    assert res.returncode == 0, res.stderr
    start_ev = env_vars(docker_run_argv(ctl))
    assert "PROBE_AWS_BEARER_TOKEN_BEDROCK=bedrock-secret-orig" in start_ev
    assert "PROBE_AWS_REGION=us-east-1" in start_ev
    assert "EXTRA_KEY=extra-orig" in start_ev
    assert "MY_FLAG=orig-flag" in start_ev

    _stop_container(ctl)
    # Resuming shell has NEITHER PROBE_AWS var at all, and would set a
    # DIFFERENT value for one of them if it were (wrongly) re-forwarded.
    assert "PROBE_AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    res = ctl.run("resume", "tst-resumeenv", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-resumeenv",
        "STUB_DOCKER_RUNNING": "",
        "PROBE_AWS_REGION": "eu-west-1",
    })
    assert res.returncode == 0, res.stderr
    resume_ev = env_vars(docker_run_argv(ctl))
    assert "PROBE_AWS_BEARER_TOKEN_BEDROCK=bedrock-secret-orig" in resume_ev
    assert "PROBE_AWS_REGION=us-east-1" in resume_ev
    assert not any(v.startswith("PROBE_AWS_REGION=eu-west-1") for v in resume_ev)
    assert "EXTRA_KEY=extra-orig" in resume_ev
    assert "MY_FLAG=orig-flag" in resume_ev


def test_resume_of_run_without_env_wiring_file_is_unaffected(ctl):
    """Migration case: a run started before task 001 (no env-wiring.json at
    all, e.g. a run that used no --forward-env/--llm-env/--env, or one
    started by an older ralphctl) must resume exactly as before -- no
    error, no spurious -e flags."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-noextraenv")
    assert res.returncode == 0, res.stderr
    assert not (ctl.registry / "configs" / "tst-noextraenv" / "env-wiring.json").exists()

    _stop_container(ctl)
    res = ctl.run("resume", "tst-noextraenv", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-noextraenv",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    assert docker_run_argv(ctl)  # ran fine, no crash from a missing file


def test_resume_of_prewiring_run_with_neither_file_is_unaffected(ctl):
    """Older-still migration case: no llm-wiring.json AND no
    env-wiring.json at all (a run started before both task 058 and task
    001) -- resumes exactly as it always did."""
    rdir = ctl.registry / "runs" / "tst-prenone"
    cdir = ctl.registry / "configs" / "tst-prenone"
    rdir.mkdir(parents=True)
    cdir.mkdir(parents=True)
    (cdir / "job.yaml").write_text('run_id: "tst-prenone"\niterations: 5\n')
    (rdir / "host.json").write_text(json.dumps({
        "runId": "tst-prenone", "container": "f" * 12, "port": 1234,
        "apiUrl": "http://127.0.0.1:1234", "image": "ralphd:dev",
        "startedAt": "2024-01-01T00:00:00Z"}))
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    assert not (cdir / "llm-wiring.json").exists()
    assert not (cdir / "env-wiring.json").exists()

    res = ctl.run("resume", "tst-prenone")
    assert res.returncode == 0, res.stderr
    assert docker_run_argv(ctl)


def test_no_extra_env_flags_leaves_no_wiring_file(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-noflags")
    assert res.returncode == 0, res.stderr
    assert not (ctl.registry / "configs" / "tst-noflags" / "env-wiring.json").exists()


def test_env_wiring_secret_never_lands_in_run_dir(ctl):
    """Mirrors test_resume_llm_wiring_secret_never_lands_in_run_dir: the
    persisted extra-env secret lands ONLY in the job's CONFIG dir, never
    the run dir, across both `start` and a subsequent `resume`."""
    secret = "sk-extraenv-never-in-rundir-987"
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-extraenvsafe",
                  "--env", f"SOME_SECRET={secret}")
    assert res.returncode == 0, res.stderr
    _stop_container(ctl)
    res = ctl.run("resume", "tst-extraenvsafe", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-extraenvsafe",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr

    run_dir = ctl.registry / "runs" / "tst-extraenvsafe"
    for p in run_dir.rglob("*"):
        if p.is_file():
            assert secret not in p.read_text(errors="ignore"), \
                f"secret leaked into run dir at {p}"
    wiring_path = ctl.registry / "configs" / "tst-extraenvsafe" / "env-wiring.json"
    assert secret in wiring_path.read_text()
    assert oct(wiring_path.stat().st_mode)[-3:] == "600"
