"""Black-box tests for task 058 (operator steering 018, defect 1):
`ralphctl resume` must reproduce the *original* run's `--llm` wiring (env
vars forwarded + any extra mounts, e.g. `~/.aws` for `--llm host`), not
whatever the operator's current shell happens to have (or lack) at resume
time.

Uses the same stub-docker recording harness as test_cli_docker.py /
test_cli_llm_profiles.py / test_cli_resume.py -- no real container, no real
engine needed to prove the docker-run argv wiring is reproduced correctly.
"""

from __future__ import annotations

import json
import os

from test_cli_docker import Ctl, ctl, unix_sock

__all__ = ["ctl", "unix_sock"]


def docker_run_argv(ctl: Ctl) -> list[str]:
    runs = [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]
    assert len(runs) == 1, f"expected one docker run, got: {ctl.recorded()}"
    return runs[0]


def env_vars(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


def mount_specs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


def _stop_container(ctl: Ctl) -> None:
    """Erase the recording log from the `start` call so the following
    `resume` call's docker_run_argv() sees exactly one (its own) `run -d`."""
    ctl.log.unlink(missing_ok=True)


# --------------------------------------------------------------------------
def test_resume_reproduces_llm_host_env_absent_from_resuming_shell(ctl, tmp_path):
    """`--llm host` forwards HOST_LLM_ENV vars set at `start` time. A later
    `resume` call made from a shell where those vars are absent (simulating
    a fresh shell/container after a crash) must still see the *original*
    values, not nothing."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-hostenv",
                  env={"ANTHROPIC_API_KEY": "sk-original-secret-value",
                       "AWS_REGION": "us-east-1"})
    assert res.returncode == 0, res.stderr
    start_argv = docker_run_argv(ctl)
    assert "ANTHROPIC_API_KEY=sk-original-secret-value" in env_vars(start_argv)
    assert "AWS_REGION=us-east-1" in env_vars(start_argv)

    wiring_path = ctl.registry / "configs" / "tst-hostenv" / "llm-wiring.json"
    assert wiring_path.is_file()
    assert oct(wiring_path.stat().st_mode)[-3:] == "600"
    doc = json.loads(wiring_path.read_text())
    assert doc["env"]["ANTHROPIC_API_KEY"] == "sk-original-secret-value"

    _stop_container(ctl)
    # Resume from a shell that deliberately has NO key at all for one var
    # and a DIFFERENT value for another -- proves the reproduced env came
    # from the persisted wiring file, not from re-forwarding whatever this
    # resuming shell happens to have (or lack).
    assert "ANTHROPIC_API_KEY" not in os.environ
    res = ctl.run("resume", "tst-hostenv", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-hostenv",
        "STUB_DOCKER_RUNNING": "",
        "AWS_REGION": "eu-west-1",  # different value on this resuming shell
    })
    assert res.returncode == 0, res.stderr
    resume_argv = docker_run_argv(ctl)
    ev = env_vars(resume_argv)
    assert "ANTHROPIC_API_KEY=sk-original-secret-value" in ev
    assert "AWS_REGION=us-east-1" in ev  # original, not the resume shell's
    assert not any(v.startswith("AWS_REGION=eu-west-1") for v in ev)


def test_resume_reproduces_llm_host_aws_mount(ctl, tmp_path):
    fake_home = tmp_path / "homeatstart"
    (fake_home / ".aws").mkdir(parents=True)
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-awsmount",
                  env={"HOME": str(fake_home)})
    assert res.returncode == 0, res.stderr
    start_mounts = mount_specs(docker_run_argv(ctl))
    assert any(m == f"{fake_home / '.aws'}:/home/agent/.aws:ro" for m in start_mounts)

    _stop_container(ctl)
    other_home = tmp_path / "homeatresume"  # no .aws dir here at all
    other_home.mkdir()
    res = ctl.run("resume", "tst-awsmount", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-awsmount",
        "STUB_DOCKER_RUNNING": "",
        "HOME": str(other_home),
    })
    assert res.returncode == 0, res.stderr
    resume_mounts = mount_specs(docker_run_argv(ctl))
    # still the ORIGINAL .aws path, not derived from the resuming shell's
    # (different, .aws-less) HOME
    assert any(m == f"{fake_home / '.aws'}:/home/agent/.aws:ro" for m in resume_mounts)


def test_resume_reproduces_named_profile_env_and_mounts(ctl, tmp_path):
    """A named profile's `${env:...}`-resolved env + mounts, resolved once
    at `start` time, must be reproduced byte-for-byte on resume even when
    the profile-referenced host env var is gone or different by resume
    time."""
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True)
    (d / "acme.yaml").write_text(
        "env:\n  ACME_KEY: \"${env:PROBE_ACME_VAR}\"\n"
        "mounts:\n  - \"~/.acmecreds:/home/agent/.acmecreds:ro\"\n")

    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme",
                  "--run-id", "tst-acmeresume",
                  env={"PROBE_ACME_VAR": "acme-secret-at-start"})
    assert res.returncode == 0, res.stderr
    assert "ACME_KEY=acme-secret-at-start" in env_vars(docker_run_argv(ctl))

    _stop_container(ctl)
    res = ctl.run("resume", "tst-acmeresume", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-acmeresume",
        "STUB_DOCKER_RUNNING": "",
        "PROBE_ACME_VAR": "a-totally-different-value-at-resume-time",
    })
    assert res.returncode == 0, res.stderr
    resume_argv = docker_run_argv(ctl)
    ev = env_vars(resume_argv)
    assert "ACME_KEY=acme-secret-at-start" in ev
    assert not any(v.startswith("ACME_KEY=a-totally-different") for v in ev)
    assert any(m.endswith(":/home/agent/.acmecreds:ro") for m in mount_specs(resume_argv))


def test_resume_of_old_run_without_wiring_file_is_unaffected(ctl):
    """A run started before task 058 (no llm-wiring.json at all) must
    resume exactly as before -- no crash, no spurious env/mounts."""
    rdir = ctl.registry / "runs" / "tst-prewiring"
    cdir = ctl.registry / "configs" / "tst-prewiring"
    rdir.mkdir(parents=True)
    cdir.mkdir(parents=True)
    (cdir / "job.yaml").write_text('run_id: "tst-prewiring"\niterations: 5\n')
    (rdir / "host.json").write_text(json.dumps({
        "runId": "tst-prewiring", "container": "f" * 12, "port": 1234,
        "apiUrl": "http://127.0.0.1:1234", "image": "ralphd:dev",
        "startedAt": "2024-01-01T00:00:00Z"}))
    (rdir / "status.json").write_text(json.dumps({"state": "failed"}))
    assert not (cdir / "llm-wiring.json").exists()

    res = ctl.run("resume", "tst-prewiring")
    assert res.returncode == 0, res.stderr
    assert docker_run_argv(ctl)  # ran fine, no crash from a missing file


def test_llm_none_leaves_no_wiring_file(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-nonewiring")
    assert res.returncode == 0, res.stderr
    assert not (ctl.registry / "configs" / "tst-nonewiring" / "llm-wiring.json").exists()
