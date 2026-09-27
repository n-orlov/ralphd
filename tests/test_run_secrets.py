"""Feature module for the subject below (task 028, requirement D.7): the
per-task pin modules listed here are folded in whole -- every test body,
assertion and docstring verbatim, only the module they live in changed.
Sources absorbed (each was `tests/<name>`, deleted in the same commit, with
one `docs/test-suite-audit.md` § "Subsumed" row per node):

- `test_cli_run_secrets_mount_task020.py` -- Black-box tests for the run-secrets MAC-key mount (requirement E.4, task 020, #150/#152). `ralphctl start`/`resume` bind a mode-0700 host directory `<registry>/configs/<run>/run-secrets/` at the fixed container path `/var/lib/ralphd/run-secrets` on every `docker run` they issue, so a per-run MAC key
- `test_run_secrets_key_read_under_privsep_task020.py` -- Task 020 (requirement E.4, #150/#152), attempt 3: the persisted MAC key must be READABLE by the next engine episode in the one configuration that persists one by default -- the privilege-separated engine. Why this module exists. `petitions._persist_key` writes the key inside `privsep.root_for_write(

Renamed to resolve cross-source collisions of module-private names (no test
function was renamed):
- none: no module-level name was defined by two sources.
"""

from __future__ import annotations


# ----------------------------------------------------------------------
# Folded from tests/test_cli_run_secrets_mount_task020.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import json
import shutil
import stat
from pathlib import Path

from ralphd.engine import petitions

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run, _run_argv

__all__ = ["ctl", "unix_sock"]

RUN_SECRETS_TARGET = "/var/lib/ralphd/run-secrets"
# The engine's own run root inside the job container (`-v <rdir>:/run/ralphd`),
# which is what names this run's key file in the store.
CONTAINER_RUN_DIR = "/run/ralphd"


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


def test_status_reports_key_availability_not_the_directory_and_names_the_fallback(ctl):
    """The condition an operator needs is "is this run's MAC key THERE",
    not "did `start` create a directory". A bound-but-empty `run-secrets/`
    is the ordinary single-uid shape (`petitions.key_store_dir()` returns
    None, so nothing is ever persisted), and in it the `events.jsonl`
    witness is the only remaining check -- `status` must say so even while
    the mount itself is bound.
    """
    rdir, cdir = _seed_run(ctl, "tst-secrets-status")
    (rdir / "status.json").write_text(json.dumps({
        "state": "running", "verdict": None, "phase": "worker",
        "approach": 1, "iterationsUsed": 1, "iterationsBudget": 10,
        "startedAt": "2024-01-01T00:00:00Z", "schemaVersion": 1,
    }))
    # the fallback's own witness exists in this run's events log, so the
    # only thing missing is the key itself.
    (rdir / "events.jsonl").write_text(json.dumps({
        "ts": "2024-01-01T00:00:01Z",
        "type": petitions.ADJUDICATIONS_RECORDED_EVENT,
        "mac": "ab" * 32,
    }) + "\n")
    sdir = cdir / "run-secrets"
    sdir.mkdir(mode=0o700)
    assert list(sdir.glob("*.key")) == []

    res = ctl.run("--json", "status", "tst-secrets-status")
    assert res.returncode == 0, res.stderr
    rsm = json.loads(res.stdout)["runSecretsMount"]
    assert rsm["path"] == str(sdir)
    assert rsm["target"] == RUN_SECRETS_TARGET
    assert rsm["bound"] is True
    assert rsm["keyAvailable"] is False
    assert rsm["keyFiles"] == []
    assert "events.jsonl" in rsm["fallbackCheck"]
    assert petitions.ADJUDICATIONS_RECORDED_EVENT in rsm["fallbackCheck"]

    text = ctl.run("status", "tst-secrets-status").stdout
    assert "run-secrets: bound" in text
    assert str(sdir) in text
    assert RUN_SECRETS_TARGET in text
    assert "no MAC key available" in text
    assert "events.jsonl" in text
    assert petitions.ADJUDICATIONS_RECORDED_EVENT in text


def test_status_names_the_key_once_one_survives_in_the_bound_directory(ctl):
    rdir, cdir = _seed_run(ctl, "tst-secrets-key")
    (rdir / "status.json").write_text(json.dumps({
        "state": "running", "verdict": None, "phase": "worker",
        "approach": 1, "iterationsUsed": 1, "iterationsBudget": 10,
        "startedAt": "2024-01-01T00:00:00Z", "schemaVersion": 1,
    }))
    sdir = cdir / "run-secrets"
    sdir.mkdir(mode=0o700)
    keyfile = sdir / petitions.stored_key_filename(CONTAINER_RUN_DIR)
    keyfile.write_text("cd" * 32)

    rsm = json.loads(ctl.run("--json", "status",
                             "tst-secrets-key").stdout)["runSecretsMount"]
    assert rsm["bound"] is True
    assert rsm["keyAvailable"] is True
    assert rsm["keyFiles"] == [keyfile.name]
    assert rsm["fallbackCheck"] is None

    text = ctl.run("status", "tst-secrets-key").stdout
    assert "run-secrets: bound" in text
    assert f"MAC key present: {keyfile.name}" in text
    assert "no MAC key available" not in text

    # an empty key file is no key: truncating it puts the run back in the
    # fallback condition rather than leaving a false "present".
    keyfile.write_text("")
    rsm = json.loads(ctl.run("--json", "status",
                             "tst-secrets-key").stdout)["runSecretsMount"]
    assert rsm["keyAvailable"] is False
    assert "events.jsonl" in rsm["fallbackCheck"]


def test_status_names_the_mount_and_the_fallback_check_when_unbound(ctl):
    rdir, cdir = _seed_run(ctl, "tst-secrets-gone")
    (rdir / "status.json").write_text(json.dumps({
        "state": "running", "verdict": None, "phase": "worker",
        "approach": 1, "iterationsUsed": 1, "iterationsBudget": 10,
        "startedAt": "2024-01-01T00:00:00Z", "schemaVersion": 1,
    }))
    sdir = cdir / "run-secrets"
    sdir.mkdir(mode=0o700)

    # remove the host directory out from under the run: `status` names the
    # unbound condition and the documented fallback in the same line.
    shutil.rmtree(sdir)
    doc = json.loads(ctl.run("--json", "status", "tst-secrets-gone").stdout)
    assert doc["runSecretsMount"]["bound"] is False
    assert doc["runSecretsMount"]["keyAvailable"] is False
    text = ctl.run("status", "tst-secrets-gone").stdout
    assert "run-secrets: not bound" in text
    assert "no MAC key available" in text
    assert "events.jsonl" in text
    assert petitions.ADJUDICATIONS_RECORDED_EVENT in text


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


# ----------------------------------------------------------------------
# Folded from tests/test_run_secrets_key_read_under_privsep_task020.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import contextlib
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from ralphd.engine import petitions, privsep
from ralphd.engine.state import RunDir

OBLIGATION = "Requirement E.4, DoD item 3"

# A root-only file cannot be modelled at all when the tests themselves run
# as root: mode 0o000 would still be readable and the "unreadable at my own
# uid" premise every test below rests on would be vacuous.
#
# Task 028 (requirement D.7): this was `pytestmark` in the source module, i.e.
# module-scoped. In this merged module a module-scoped mark would also skip the
# seven folded `ralphctl` mount tests above, which have no such premise -- so
# the SAME mark is bound to a name and applied, unchanged (same predicate, same
# reason string), as a decorator on exactly the five tests it governed there.
_privsep_root_skip = pytest.mark.skipif(
    os.geteuid() == 0, reason="models a file root can read and this uid cannot")


@contextlib.contextmanager
def _stand_in_root_window(paths, entered: list[str]):
    """Stand-in for `privsep.root_for_write()`: records that the window was
    entered and, for its duration, makes `paths` readable -- modelling
    root's exemption from the mode check, not re-implementing anything the
    production path does."""
    entered.append("raised")
    restore: dict[Path, int] = {}
    for p in paths:
        if p.exists():
            restore[p] = p.stat().st_mode & 0o777
            os.chmod(p, 0o600)
    try:
        yield True
    finally:
        for p, mode in restore.items():
            if p.exists():
                os.chmod(p, mode)


def _boundary(monkeypatch, paths, entered):
    """Put the uid boundary in force for `petitions`: `separated()` true and
    an elevation window that behaves like root's."""
    monkeypatch.setattr(privsep, "separated", lambda: True)
    monkeypatch.setattr(
        privsep, "root_for_write",
        lambda: _stand_in_root_window(paths, entered))


def _store(monkeypatch, tmp_path) -> Path:
    store = tmp_path / "run-secrets"
    monkeypatch.setenv(petitions.RUN_SECRETS_DIR_ENV, str(store))
    return store


@_privsep_root_skip
def test_a_key_only_root_can_open_is_read_back_inside_the_raise(
        tmp_path, monkeypatch):
    store = _store(monkeypatch, tmp_path)
    run = RunDir(root=tmp_path / "run")
    entered: list[str] = []

    # Episode 1 mints and persists the key.
    minted = petitions._authority(run, create=True).key
    key_path = petitions._stored_key_path(run)
    assert key_path is not None and key_path.parent == store
    assert key_path.read_bytes().strip()

    # Model what `_persist_key` leaves behind under the boundary: a file
    # the engine's effective (agent) uid cannot open.
    os.chmod(key_path, 0o000)
    with pytest.raises(OSError):
        key_path.read_bytes()
    _boundary(monkeypatch, [key_path], entered)

    # Episode 2: the process is gone, the in-memory authority with it.
    petitions.forget_run_authority(run)
    later = RunDir(root=run.root)
    assert petitions._read_stored_key(later) == minted
    assert entered, "the read never entered the elevation window"
    auth = petitions._authority(later, create=False)
    assert auth is not None and auth.key == minted

    # ... and the window closed again: nothing left the file readable.
    assert key_path.stat().st_mode & 0o777 == 0o000


@_privsep_root_skip
def test_with_no_boundary_an_unreadable_key_is_the_fail_closed_degrade(
        tmp_path, monkeypatch):
    """The retry is scoped to the boundary: where there is none there is no
    privilege to raise into, and an unreadable key stays unread -- the
    documented degrade, never an exception and never a fabricated key."""
    _store(monkeypatch, tmp_path)
    run = RunDir(root=tmp_path / "run")
    petitions._authority(run, create=True)
    key_path = petitions._stored_key_path(run)
    os.chmod(key_path, 0o000)
    monkeypatch.setattr(privsep, "separated", lambda: False)

    petitions.forget_run_authority(run)
    later = RunDir(root=run.root)
    assert petitions._read_stored_key(later) is None
    assert petitions._authority(later, create=False) is None


@_privsep_root_skip
def test_an_agent_readable_store_is_read_without_raising_at_all(
        tmp_path, monkeypatch):
    """The plain read comes first, so an operator's own
    `$RALPHD_RUN_SECRETS_DIR` store (and a key file an older, pre-boundary
    build wrote agent-owned) needs no elevation -- the raise is a fallback,
    not the new default."""
    _store(monkeypatch, tmp_path)
    run = RunDir(root=tmp_path / "run")
    entered: list[str] = []
    minted = petitions._authority(run, create=True).key
    _boundary(monkeypatch, [petitions._stored_key_path(run)], entered)

    petitions.forget_run_authority(run)
    assert petitions._read_stored_key(RunDir(root=run.root)) == minted
    assert entered == []


@_privsep_root_skip
def test_a_later_episode_authenticates_the_earlier_confirmed_record(
        tmp_path, monkeypatch):
    """The property E.4 exists for, end to end: a CONFIRMED adjudication
    written by episode 1 still authenticates in episode 2 when the only
    thing surviving in between is a key file the agent uid cannot read."""
    _store(monkeypatch, tmp_path)
    run = RunDir(root=tmp_path / "run")
    entered: list[str] = []

    petitions.record_confirmed_adjudications(
        run, [{"obligation": OBLIGATION, "adjudication": "CONFIRMED"}])
    entries, tampered = petitions.read_confirmed_adjudications(run)
    assert [e["obligation"] for e in entries] == [OBLIGATION]
    assert tampered is False

    key_path = petitions._stored_key_path(run)
    os.chmod(key_path, 0o000)
    _boundary(monkeypatch, [key_path], entered)

    petitions.forget_run_authority(run)
    later = RunDir(root=run.root)
    entries2, tampered2 = petitions.read_confirmed_adjudications(later)
    assert [e["obligation"] for e in entries2] == [OBLIGATION]
    assert tampered2 is False
    assert entered


# ---------------------------------------------------------------------------
# The unmodelled half: a real root engine, a real uid drop, a real
# root-owned key file. Sibling container, so it runs in the docker lane
# rather than the fast tier.
# ---------------------------------------------------------------------------

REPO = Path(__file__).parent.parent
DOCKER = shutil.which("docker")
HOST_WORKSPACE = os.environ.get("RALPHD_HOST_WORKSPACE")
PY_IMAGE = os.environ.get("RALPHD_E4_PYTHON_IMAGE", "python:3.12-slim")

REAL_PRIVSEP_SCRIPT = textwrap.dedent(
    """
    set -e
    useradd -u 1000 -m agent 2>/dev/null || true
    mkdir -p /var/lib/ralphd/run-secrets /run/ralphd
    chown 1000:1000 /var/lib/ralphd/run-secrets /run/ralphd
    chmod 700 /var/lib/ralphd/run-secrets
    python - <<'PY'
    import json, os, subprocess, sys
    from pathlib import Path
    from types import SimpleNamespace
    sys.path.insert(0, "/src")
    from ralphd.engine import petitions, privsep

    run = SimpleNamespace(root=Path("/run/ralphd"))
    out = {}
    privsep.separate_engine_identity()
    out["separated"] = privsep.separated()
    out["resuid"] = list(os.getresuid())
    minted = petitions._authority(run, create=True).key
    kp = petitions._stored_key_path(run)
    # The store dir is root-owned 0700 (`_persist_key` chowns it on every
    # write), so even a stat needs the same elevation the engine reads with.
    with privsep.root_for_write():
        st = kp.stat()
    out["keyFile"] = {"uid": st.st_uid, "mode": oct(st.st_mode & 0o777)}
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys\\n"
         "try:\\n"
         "    open(sys.argv[1], 'rb').read()\\n"
         "    print('READABLE')\\n"
         "except OSError as e:\\n"
         "    print('EACCES' if e.errno == 13 else 'ERR')\\n",
         str(kp)],
        user=1000, group=1000, capture_output=True, text=True)
    out["agentUidRead"] = probe.stdout.strip()
    petitions.forget_run_authority(run)
    recovered = petitions._authority(run, create=False)
    out["recovered"] = bool(recovered and recovered.key == minted)
    print("VERDICT " + json.dumps(out))
    PY
    """
).strip()


@_privsep_root_skip
@pytest.mark.docker
@pytest.mark.skipif(
    not DOCKER or not HOST_WORKSPACE or not Path("/var/run/docker.sock").is_socket(),
    reason="needs the host docker socket and $RALPHD_HOST_WORKSPACE")
def test_the_real_privsep_engine_reads_back_its_own_root_owned_key():
    """Root engine, real `separate_engine_identity()`, real root-owned 0600
    key file: a later episode recovers the key, and a process at the agent
    uid still cannot read it. This is the exact path task 020's second
    attempt was rejected for -- it failed here before the elevated read."""
    label_run = os.environ.get("RALPHD_RUN_ID", "local-dev")
    res = subprocess.run(
        [DOCKER, "run", "--rm", "-i",
         "--label", f"ralphd.run={label_run}",
         "--label", "ralphd.role=sibling",
         "-v", f"{HOST_WORKSPACE}/src:/src:ro",
         PY_IMAGE, "bash", "-s"],
        input=REAL_PRIVSEP_SCRIPT, capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, (res.stdout[-4000:] + res.stderr[-4000:])
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("VERDICT ")]
    assert line, res.stdout[-4000:] + res.stderr[-4000:]
    verdict = json.loads(line[-1][len("VERDICT "):])
    assert verdict["separated"] is True, verdict
    assert verdict["resuid"] == [0, 1000, 0], verdict
    assert verdict["keyFile"] == {"uid": 0, "mode": "0o600"}, verdict
    assert verdict["agentUidRead"] == "EACCES", verdict
    assert verdict["recovered"] is True, verdict
