"""Black-box tests for the run-secrets MAC-key mount (requirement E.4,
task 020, #150/#152).

`ralphctl start`/`resume` bind a mode-0700 host directory
`<registry>/configs/<run>/run-secrets/` at the fixed container path
`/var/lib/ralphd/run-secrets` on every `docker run` they issue, so a
per-run MAC key an earlier engine episode minted (see
`ralphd.engine.petitions.key_store_dir`) survives a `resume`'s fresh
container. `resume`'s tests reuse `test_cli_resume.py`'s own
stub-docker/`_seed_run` harness (a manually seeded run+config dir, no real
`start` needed) -- the same approach that module's other mount-reproduction
tests use.
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run, _run_argv

__all__ = ["ctl", "unix_sock"]

RUN_SECRETS_TARGET = "/var/lib/ralphd/run-secrets"


def _mount_pairs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]


def test_start_binds_a_mode_0700_run_secrets_dir_at_the_fixed_container_path(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-secrets-start")
    assert res.returncode == 0, res.stderr

    sdir = ctl.registry / "configs" / "tst-secrets-start" / "run-secrets"
    assert sdir.is_dir()
    assert stat.S_IMODE(sdir.stat().st_mode) == 0o700

    argv = _run_argv(ctl)
    assert f"{sdir}:{RUN_SECRETS_TARGET}" in _mount_pairs(argv)


def test_resume_rebinds_the_same_pre_existing_host_directory(ctl):
    rdir, cdir = _seed_run(ctl, "tst-secrets-resume")
    sdir = cdir / "run-secrets"
    sdir.mkdir(mode=0o700)

    res = ctl.run("resume", "tst-secrets-resume")
    assert res.returncode == 0, res.stderr

    argv = _run_argv(ctl)
    assert f"{sdir}:{RUN_SECRETS_TARGET}" in _mount_pairs(argv)
    assert stat.S_IMODE(sdir.stat().st_mode) == 0o700


def test_resume_self_heals_a_pre_task020_run_with_no_run_secrets_dir_yet(ctl):
    """A run/config dir seeded before this task landed has no
    `run-secrets/` directory at all -- `resume` creates and mode-fixes it
    rather than assuming it already exists."""
    rdir, cdir = _seed_run(ctl, "tst-secrets-heal")
    sdir = cdir / "run-secrets"
    assert not sdir.exists()

    res = ctl.run("resume", "tst-secrets-heal")
    assert res.returncode == 0, res.stderr

    assert sdir.is_dir()
    assert stat.S_IMODE(sdir.stat().st_mode) == 0o700
    argv = _run_argv(ctl)
    assert f"{sdir}:{RUN_SECRETS_TARGET}" in _mount_pairs(argv)


def test_status_names_the_mount_and_the_fallback_check_when_unbound(ctl):
    rdir, cdir = _seed_run(ctl, "tst-secrets-status")
    (rdir / "status.json").write_text(json.dumps({
        "state": "running", "verdict": None, "phase": "worker",
        "approach": 1, "iterationsUsed": 1, "iterationsBudget": 10,
        "startedAt": "2024-01-01T00:00:00Z", "schemaVersion": 1,
    }))
    sdir = cdir / "run-secrets"
    sdir.mkdir(mode=0o700)

    res = ctl.run("--json", "status", "tst-secrets-status")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["runSecretsMount"] == {
        "path": str(sdir), "target": RUN_SECRETS_TARGET, "bound": True}

    text = ctl.run("status", "tst-secrets-status").stdout
    assert "run-secrets: bound" in text
    assert str(sdir) in text
    assert RUN_SECRETS_TARGET in text

    # remove the host directory out from under the run: `status` names the
    # unbound condition and the documented fallback in the same line.
    shutil.rmtree(sdir)
    res = ctl.run("--json", "status", "tst-secrets-status")
    doc = json.loads(res.stdout)
    assert doc["runSecretsMount"]["bound"] is False
    text = ctl.run("status", "tst-secrets-status").stdout
    assert "run-secrets: not bound" in text
    assert "events.jsonl" in text
    assert "petition_adjudications_recorded" in text


def test_doctor_names_the_mount_and_the_fallback_check(ctl):
    res = ctl.run("doctor")
    assert res.returncode in (0, 1)
    assert "run-secrets" in res.stdout
    assert RUN_SECRETS_TARGET in res.stdout
    assert "events.jsonl" in res.stdout
    assert "petition_adjudications_recorded" in res.stdout

    doc = ctl.run("--json", "doctor")
    payload = json.loads(doc.stdout)
    assert payload["runSecretsMountTarget"] == RUN_SECRETS_TARGET
