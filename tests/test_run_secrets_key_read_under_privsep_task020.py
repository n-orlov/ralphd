"""Task 020 (requirement E.4, #150/#152), attempt 3: the persisted MAC key
must be READABLE by the next engine episode in the one configuration that
persists one by default -- the privilege-separated engine.

Why this module exists. `petitions._persist_key` writes the key inside
`privsep.root_for_write()`, so under the uid boundary the key file is
root-owned mode 0600 in a mode-0700 store outside the run dir; that is the
property making it a secret from every hat process. But the engine's own
EFFECTIVE uid is the agent's (`privsep.separate_engine_identity`: real and
saved uid 0, effective uid `agent`), so the plain `path.read_bytes()` the
read used to be could not open that file either: episode N persisted a key
episode N+1 could not read, the record read as unauthenticated, and the
store bought nothing at all. `_read_stored_key` therefore retries the read
inside the same momentary raise back to root the write already used.

What is modelled here, and how honestly. The suite runs single-uid and
unprivileged, so it cannot mint a genuinely root-owned file. These tests
model only the KERNEL's half -- root reads a file its mode forbids the
owning uid -- by making the key file mode 0o000 (unreadable at the uid
running the tests) and having the stand-in elevation window lift the mode
for exactly the duration of the raise. Everything else is the production
code path: the real `_read_stored_key`, the real `_authority`, the real
`record_confirmed_adjudications`/`read_confirmed_adjudications`.

The genuinely privileged proof is not modelled: it is a real root engine
in a sibling container (`test_the_real_privsep_engine_reads_back_its_own_
root_owned_key`, marked `docker`), plus the recorded reproduction in
`artifacts/reports/v0.19-e4-privsep-key-read.md`, which shows the same
scenario failing before this fix and passing after it.
"""

from __future__ import annotations

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
pytestmark = pytest.mark.skipif(
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
