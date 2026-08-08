"""Black-box tests: credential placement is done by the engine itself
(engine/creds.py), not the container entrypoint, and secret *values* never
leak into the run dir, events, or captured engine stdout (PRD reqs 6, 8).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from test_e2e import EngineProc

SECRET_VALUE = "sekret-token-do-not-leak-xyz789"


@pytest.fixture
def creds_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "creds-e2e", "iterations": 12,
                    "max_approaches": 3, "on_complete": "exit"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _write_creds(config_dir: Path) -> None:
    creds = config_dir / "creds"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / "github.env").write_text(f"GITHUB_TOKEN={SECRET_VALUE}\n")
    (creds / "jenkins.env").write_text("JENKINS_URL=https://example.com\n")
    (creds / "gitconfig").write_text("[user]\n\tname = Bot\n")
    (creds / "git-credentials").write_text(
        f"https://bot:{SECRET_VALUE}@example.com\n")
    (creds / "netrc").write_text(f"machine example.com login bot password {SECRET_VALUE}\n")
    ssh = creds / "ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text(f"-----BEGIN KEY-----\n{SECRET_VALUE}\n-----END KEY-----\n")
    setup = creds / "setup.sh"
    setup.write_text("#!/bin/sh\ntouch \"$HOME/.creds-setup-ran\"\n")
    setup.chmod(0o755)
    # a file that must be ignored (not *.env, not recognized extra)
    (creds / "readme.txt").write_text("ignore me\n")


def test_creds_placed_by_engine_not_entrypoint_no_value_leak(creds_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    # EngineProc starts the process synchronously in __init__, so the creds
    # dir must exist under tmp_path/"config" (the path EngineProc uses)
    # *before* the factory instantiates it -- mkdir(exist_ok=True) there
    # won't disturb files already present.
    _write_creds(tmp_path / "config")
    e = creds_engine(stub_env={"HOME": str(fake_home)})
    # entrypoint.sh is not run in this harness (we exec ralphd-engine
    # directly) -- proving placement happens without it.
    assert e.proc.wait(timeout=30) == 0
    out = e.proc.stdout.read()

    # *.env files placed at $HOME/.creds/<name>.env, mode 0600
    github_env = fake_home / ".creds" / "github.env"
    assert github_env.read_text() == f"GITHUB_TOKEN={SECRET_VALUE}\n"
    assert stat.S_IMODE(github_env.stat().st_mode) == 0o600
    jenkins_env = fake_home / ".creds" / "jenkins.env"
    assert jenkins_env.exists()
    assert stat.S_IMODE(jenkins_env.stat().st_mode) == 0o600

    # recognized extras placed conventionally
    assert (fake_home / ".gitconfig").exists()
    git_creds = fake_home / ".git-credentials"
    assert git_creds.exists()
    assert stat.S_IMODE(git_creds.stat().st_mode) == 0o600
    netrc = fake_home / ".netrc"
    assert netrc.exists()
    assert stat.S_IMODE(netrc.stat().st_mode) == 0o600
    ssh_key = fake_home / ".ssh" / "id_ed25519"
    assert ssh_key.exists()
    assert stat.S_IMODE(ssh_key.stat().st_mode) == 0o600

    # setup.sh ran once
    assert (fake_home / ".creds-setup-ran").exists()

    # ignored file not copied anywhere useful
    assert not (fake_home / "readme.txt").exists()
    assert not (fake_home / ".creds" / "readme.txt").exists()

    # --- negative proof: SECRET_VALUE appears nowhere it must not ---
    run_dir_text = []
    for p in e.run_dir.rglob("*"):
        if p.is_file():
            try:
                run_dir_text.append(p.read_text(errors="ignore"))
            except (UnicodeDecodeError, OSError):
                pass
    assert SECRET_VALUE not in "\n".join(run_dir_text)

    events_path = e.run_dir / "events.jsonl"
    if events_path.exists():
        assert SECRET_VALUE not in events_path.read_text()

    assert SECRET_VALUE not in out

    job_json = e.run_dir / "job.json"
    if job_json.exists():
        assert SECRET_VALUE not in job_json.read_text()

    # but the *name* github.env is fine to have surfaced (e.g. in logs) --
    # this is a name-only sanity check, not a requirement either way.
    assert "GITHUB_TOKEN" not in out


def test_no_creds_dir_is_a_clean_noop(creds_engine, tmp_path):
    """Absence of /config/creds must not error or create ~/.creds."""
    fake_home = tmp_path / "fakehome2"
    fake_home.mkdir()
    e = creds_engine(stub_env={"HOME": str(fake_home)})
    assert e.proc.wait(timeout=30) == 0
    assert not (fake_home / ".creds").exists()
