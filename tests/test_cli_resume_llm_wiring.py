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
from test_cli_start_nodetach import _live_env, _wait_for_supervisor
from test_llm_api import wait_for_env_marker

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


# --------------------------------------------------- --llm-header (task 045)
def test_resume_reproduces_llm_header_without_flag(ctl, tmp_path):
    """A header resolved at `start` time is baked into `<cdir>/pi/
    models.json`, which `resume` never recreates -- so it reproduces for
    free. This test pins that the persisted `llm-wiring.json` also carries
    it, and that a plain `resume` (no `--llm-header`) leaves both files
    exactly as `start` wrote them."""
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True)
    (d / "acme.yaml").write_text(
        "pi:\n  providers:\n    acme:\n      baseUrl: https://gw.example.com\n")
    run_id = "tst-hdrresume"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme",
                  "--llm-header", "X-Jira-ID=EST-1", "--run-id", run_id)
    assert res.returncode == 0, res.stderr
    cdir = ctl.registry / "configs" / run_id
    wiring_doc = json.loads((cdir / "llm-wiring.json").read_text())
    assert wiring_doc["headers"] == {"X-Jira-ID": "EST-1"}

    _stop_container(ctl)
    res = ctl.run("resume", run_id, env={
        "STUB_DOCKER_CONTAINERS": f"ralphd-{run_id}",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    models_doc = json.loads((cdir / "pi" / "models.json").read_text())
    assert models_doc["providers"]["acme"]["headers"] == {"X-Jira-ID": "EST-1"}
    wiring_doc = json.loads((cdir / "llm-wiring.json").read_text())
    assert wiring_doc["headers"] == {"X-Jira-ID": "EST-1"}


def test_resume_llm_header_overrides_persisted(ctl, tmp_path):
    """`resume --llm-header X-Jira-ID=EST-2` overrides the header
    persisted at `start` (EST-1 -> EST-2) in both `pi/models.json` and the
    re-persisted `llm-wiring.json`, while an untouched header from start
    (X-Other) survives unchanged -- task 045, #177."""
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True)
    (d / "acme.yaml").write_text(
        "pi:\n  providers:\n    acme:\n      baseUrl: https://gw.example.com\n")
    run_id = "tst-hdroverride"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme",
                  "--llm-header", "X-Jira-ID=EST-1",
                  "--llm-header", "X-Other=keep-me",
                  "--run-id", run_id)
    assert res.returncode == 0, res.stderr
    cdir = ctl.registry / "configs" / run_id

    _stop_container(ctl)
    res = ctl.run("resume", run_id, "--llm-header", "X-Jira-ID=EST-2", env={
        "STUB_DOCKER_CONTAINERS": f"ralphd-{run_id}",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    models_doc = json.loads((cdir / "pi" / "models.json").read_text())
    headers = models_doc["providers"]["acme"]["headers"]
    assert headers["X-Jira-ID"] == "EST-2"
    assert headers["X-Other"] == "keep-me"
    wiring_doc = json.loads((cdir / "llm-wiring.json").read_text())
    assert wiring_doc["headers"] == {"X-Jira-ID": "EST-2", "X-Other": "keep-me"}

    # a LATER resume with no flag reproduces the OVERRIDDEN value, not the
    # original start-time one -- the re-persisted record is what replays.
    _stop_container(ctl)
    res = ctl.run("resume", run_id, env={
        "STUB_DOCKER_CONTAINERS": f"ralphd-{run_id}",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr
    models_doc = json.loads((cdir / "pi" / "models.json").read_text())
    assert models_doc["providers"]["acme"]["headers"]["X-Jira-ID"] == "EST-2"


def test_resume_llm_header_malformed_value_refused(ctl):
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True)
    (d / "acme.yaml").write_text(
        "pi:\n  providers:\n    acme:\n      baseUrl: https://gw.example.com\n")
    run_id = "tst-hdrbadresume"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme", "--run-id", run_id)
    assert res.returncode == 0, res.stderr
    _stop_container(ctl)
    res = ctl.run("resume", run_id, "--llm-header", "totally-malformed-no-separator",
                  env={"STUB_DOCKER_CONTAINERS": f"ralphd-{run_id}",
                       "STUB_DOCKER_RUNNING": ""})
    assert res.returncode != 0
    assert "totally-malformed-no-separator" in res.stderr


def test_resume_llm_wiring_secret_never_lands_in_run_dir(ctl, tmp_path):
    """Gap #1 from the validation-failed review of this task: a codified,
    repeatable negative proof (mirroring tests/test_secret_redaction.py and
    tests/test_creds_placement.py's rglob-over-run_dir style) that the
    persisted `--llm` wiring secret lands ONLY in the job's CONFIG dir
    (`<cdir>/llm-wiring.json`, mode 0600, mounted read-only at /config --
    already asserted above), never anywhere under the run dir proper --
    across both the original `start` and a subsequent `resume`."""
    secret = "sk-never-in-rundir-zzz789"
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tst-rundirsafe",
                  env={"ANTHROPIC_API_KEY": secret, "AWS_REGION": "us-east-1"})
    assert res.returncode == 0, res.stderr
    _stop_container(ctl)
    res = ctl.run("resume", "tst-rundirsafe", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-rundirsafe",
        "STUB_DOCKER_RUNNING": "",
    })
    assert res.returncode == 0, res.stderr

    run_dir = ctl.registry / "runs" / "tst-rundirsafe"
    assert run_dir.is_dir()
    for p in run_dir.rglob("*"):
        if p.is_file():
            assert secret not in p.read_text(errors="ignore"), \
                f"secret leaked into run dir at {p}"
    # ...and confirm it DID land (mode-0600) in the config dir, so this
    # negative proof isn't vacuously true because nothing was ever written.
    wiring_path = ctl.registry / "configs" / "tst-rundirsafe" / "llm-wiring.json"
    assert secret in wiring_path.read_text()
    assert oct(wiring_path.stat().st_mode)[-3:] == "600"


def test_resume_reproduces_env_seen_by_real_pi_subprocess(ctl):
    """Gap #2 from the validation-failed review of this task: assert the
    reproduced wiring via the stub's `.stub-env.json` marker (mirroring
    tests/test_llm_api.py), i.e. prove a REAL `pi` subprocess invocation
    inside a resumed engine actually observes the original `start`-time
    env -- not just that the recorded `docker run` argv looks right.

    Uses the STUB_DOCKER_LIVE_ENGINE knob (tests/stub-docker/docker +
    live_engine_supervisor.py, same mechanism as
    tests/test_cli_start_nodetach.py): `docker run` really launches a
    `ralphd-engine` process wired to the mounted run/config dirs, and the
    supervisor SIGKILLs it the instant status.json reaches a terminal
    state -- so both the original run and the resumed one terminate
    deterministically without any pattern-based kill of this job's own
    production engine.
    """
    run_id = "tst-liveresume"
    secret = "sk-live-resume-secret-42"
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", run_id,
                  "--llm", "host", "--on-complete", "idle",
                  "--iterations", "10", "--max-approaches", "1",
                  env={**_live_env(), "ANTHROPIC_API_KEY": secret,
                       "STUB_TASKS": "1", "STUB_SLEEP": "0"})
    assert res.returncode == 0, res.stderr
    run_dir = ctl.registry / "runs" / run_id
    sup1 = _wait_for_supervisor(run_dir)
    assert sup1["killed"] is True  # the original run really did reach terminal

    # Wait for a PARSABLE marker, not for mere existence: the stub re-dumps
    # this file on every invocation, so an existence check can be satisfied
    # by a file that has no bytes yet (task 040 / task 018's fast lane).
    env_marker = run_dir / ".stub-env.json"
    started = wait_for_env_marker(env_marker)
    assert started, "stub never dumped its env for the original run"
    assert started.get("ANTHROPIC_API_KEY") == secret

    # Simulate a resume from a shell/environment where the credential is
    # completely absent -- the ONLY source left for it is the persisted
    # llm-wiring.json, reproduced by cmd_resume regardless.
    assert "ANTHROPIC_API_KEY" not in os.environ
    (run_dir / ".stub-supervisor-done").unlink()
    env_marker.unlink()

    res = ctl.run("resume", run_id, "--iterations", "+5",
                  env={**_live_env()})
    assert res.returncode == 0, res.stderr

    _wait_for_supervisor(run_dir)  # resumed engine also reaches a terminal state

    seen = wait_for_env_marker(env_marker, lambda d: d.get("ANTHROPIC_API_KEY"))
    assert seen.get("ANTHROPIC_API_KEY") == secret, (
        "resumed engine's real `pi` subprocess never observed the original "
        f"start-time env (saw: {seen.get('ANTHROPIC_API_KEY')!r})")


# --------------------------------------------------- builtin_providers (I.2)
def _builtin_profile(ctl):
    d = ctl.registry / "llm-profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bi.yaml").write_text("builtin_providers: [anthropic]\n")


def test_builtin_only_profile_still_writes_wiring_and_resume_keeps_it(ctl):
    _builtin_profile(ctl)
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "bi",
                  "--run-id", "tst-builtin")
    assert res.returncode == 0, res.stderr
    path = ctl.registry / "configs" / "tst-builtin" / "llm-wiring.json"
    assert json.loads(path.read_text())["builtinProviders"] == ["anthropic"]

    _stop_container(ctl)
    res = ctl.run("resume", "tst-builtin", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-builtin",
        "STUB_DOCKER_RUNNING": ""})
    assert res.returncode == 0, res.stderr
    assert json.loads(path.read_text())["builtinProviders"] == ["anthropic"]


def test_builtin_providers_survive_resume_llm_header(ctl):
    _builtin_profile(ctl)
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "bi",
                  "--run-id", "tst-builtin-hdr")
    assert res.returncode == 0, res.stderr
    _stop_container(ctl)
    res = ctl.run("resume", "tst-builtin-hdr", "--llm-header", "X-Test: 1", env={
        "STUB_DOCKER_CONTAINERS": "ralphd-tst-builtin-hdr",
        "STUB_DOCKER_RUNNING": ""})
    assert res.returncode == 0, res.stderr
    doc = json.loads((ctl.registry / "configs" / "tst-builtin-hdr"
                      / "llm-wiring.json").read_text())
    assert doc["builtinProviders"] == ["anthropic"]
    assert doc["headers"] == {"X-Test": "1"}
