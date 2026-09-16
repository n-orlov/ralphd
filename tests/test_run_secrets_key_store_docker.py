"""Docker tier: task 005 (requirement C, #202's third instance) proved
against a *real* container -- the standing rule for tasks 003/004/005: a
disposable container, uid separation genuinely active, a child spawned
through `privsep.agent_child_kwargs()` itself, both the ancestor-directory
attack and the key-file attack, and a negative control that proves the
probe can actually see a hole.

## The finding (PRD requirement C, measured live 15 Sep 2026)

`petitions.key_store_dir()`'s docstring claimed `/var/lib/ralphd/run-secrets`
is "created root-owned 0700 -- neither readable nor replaceable at the
agent uid, because its PARENT is root-owned too." True of the parent
(`/var/lib/ralphd`); false of the store itself. `cli.main._bind_run_secrets_
mount` binds a HOST directory -- `mkdir`'d by whatever uid ran `ralphctl
start`/`resume`, the agent's own uid on the standard uid-1000-both-sides
configuration -- straight onto that container path. There is no
user-namespace remap here, so the directory ENTRY the container sees at
that path is the HOST directory's own inode: agent-owned, hence
agent-writable and agent-replaceable, exactly the ancestor hole B.1 closed
for the task record, one level up from where the docstring claimed
protection already held. `petitions._persist_key` chmod'd the key file and
its parent but never chowned either -- chmod never changes an owner, and
the agent stayed the OWNER of its own mount point regardless of mode bits.

This module's own pre-existing sibling, `test_run_secrets.py`'s
`test_the_real_privsep_engine_reads_back_its_own_root_owned_key`, is the
exact "acknowledges an attack it does not run" shape STEER 002 named: its
setup is `mkdir -p .../run-secrets; chown 1000:1000 ...` -- a SYNTHETIC
directory the test creates itself, in a bare `python:3.12-slim` sibling,
never the real bind mount `_bind_run_secrets_mount` builds -- and it only
ever inspects the key FILE's ownership, never the directory's. It is left
as-is (it still proves the *file* is root-owned inside the raise), but this
module is what proves the directory.

Fix landed alongside this module: `privsep.protect_run_secrets_dir()`
(called once at container startup, mirroring `migrate_task_record`) and
`petitions._persist_key` (self-healing, on every key-mint write) now
`os.chown` the store's mount point root-owned, not merely `os.chmod` it;
`privsep.release_run_secrets_dir()` hands it back at shutdown so an
unprivileged host-side `rmtree` of `cli.main.run_secrets_dir(run_id)`
(under `config_root(run_id)`) still works, symmetric with
`release_protected_tasks_dir`.

Three things, each needing a real container:

1. A fully dropped agent-uid child cannot read, unlink, symlink, rename,
   replace or chmod the key file OR the store's mount-point directory OR
   the grandparent `/var/lib/ralphd` -- with the probe's own negative
   control (an unprotected scratch directory, same container) proving
   these attempts genuinely succeed when no boundary is claimed
   (`test_a_dropped_child_cannot_touch_the_run_secrets_store_or_its_key`).
2. The exact ancestor-rename-and-plant attack `docs/`/`notes.md`'s "THE
   ancestor rule" describes, run for real: rename the store's mount point
   aside, plant an agent-owned replacement holding a substituted key --
   both attempted by the dropped child -- and the authoritative
   `petitions.read_confirmed_adjudications` read afterwards, from a
   simulated LATER episode (`forget_run_authority`), still returns the
   TRUE CONFIRMED entry, never a forged one and never silently accepting
   the plant (`test_the_ancestor_attack_does_not_reach_the_authoritative_key`).
3. The documented degrade is unchanged (C.5): with no uid boundary and no
   `$RALPHD_RUN_SECRETS_DIR` override there is still no on-disk key, and a
   later episode still fails closed -- proved in-process, no container
   needed, alongside the container tests here so the whole requirement's
   evidence lives in one module
   (`test_with_no_boundary_and_no_override_there_is_still_no_on_disk_key`).

Fixtures, cleanup discipline and the two environment-specific wrinkles
(bind-mount path translation, API reachability from inside this job's own
container) are exactly `tests/test_docker_sibling_e2e.py`'s, imported by
name from `tests/test_tasks_record_docker.py`'s own already-proven
`_start_idle_job` helper.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from ralphd.engine import petitions, privsep
from ralphd.engine.state import RunDir

from test_docker_sibling_e2e import (  # noqa: F401  (fixtures used by name)
    REAL_DOCKER,
    Ctl,
    ctl,
    gateway_ip,
    pytestmark,
    test_image,
    work_root,
)
from test_tasks_record_docker import _start_idle_job  # noqa: F401

_STORE_DIR_IN_CONTAINER = "/var/lib/ralphd/run-secrets"
_GRANDPARENT_IN_CONTAINER = "/var/lib/ralphd"
_DENIED = ("EACCES", "EPERM")

_OBLIGATION = "requirement C: the petition MAC key store is protected in fact"

# Mint a real key through the REAL product write path (`_persist_key`, via
# `petitions._authority(run, create=True)`), record one CONFIRMED
# adjudication authenticated under it, and report everything a later
# assertion needs: uid/mode of the store directory, its grandparent, and
# the key file, plus the resulting entries and key material -- run as
# root inside the container, against the REAL mount `_bind_run_secrets_
# mount` bound via the real `ralphctl start` this test drove.
_SETUP_DRIVER = r'''
import json
import os
import stat
import sys
from pathlib import Path

from ralphd.engine import petitions, privsep
from ralphd.engine.state import RunDir

if os.getuid() != 0:
    sys.exit("this driver must start as real root to reproduce the engine")
agent = privsep.separate_engine_identity()
if agent is None or not privsep.separated():
    sys.exit("no uid boundary in this container: nothing to prove")

run = RunDir(Path(sys.argv[1]))
minted = petitions._authority(run, create=True).key
petitions.record_confirmed_adjudications(
    run, [{"obligation": sys.argv[2], "adjudication": "CONFIRMED"}])
entries, tampered = petitions.read_confirmed_adjudications(run)

key_path = petitions._stored_key_path(run)
store = key_path.parent


def stat_of(p):
    st = os.lstat(p)
    return {"uid": st.st_uid, "gid": st.st_gid,
           "mode": oct(stat.S_IMODE(st.st_mode))}


# `os.lstat` needs SEARCH permission on every directory in the path, not
# merely read permission on the target -- and the whole point proved below
# is that the agent's own effective uid (what this process holds right
# now, outside any `root_for_write()` window) has NONE on the store's
# 0700 root-owned mount point. So these introspection stats borrow the
# identical elevation `_persist_key`/`_read_stored_key` themselves use --
# proving the LAYOUT, not merely reproducing the same EACCES the probe
# below is supposed to be the one to hit.
with privsep.root_for_write():
    store_stat = stat_of(store)
    key_stat = stat_of(key_path)
    grandparent_stat = stat_of(Path("/var/lib/ralphd"))

out = {
    "resuid": list(os.getresuid()),
    "separated": privsep.separated(),
    "keyPath": str(key_path),
    "storeDir": str(store),
    "storeStat": store_stat,
    "keyStat": key_stat,
    "grandparentStat": grandparent_stat,
    "entries": entries,
    "tampered": tampered,
    "mintedHex": minted.hex(),
}
sys.stdout.write(json.dumps(out))
'''

# The probe a fully dropped agent child runs inside the job container: one
# attempt per syscall that could reach the key file or any ancestor entry
# above it, each recorded with its errno rather than asserted here.
_KEY_PROBE = r'''
import errno
import json
import os
import sys

store = sys.argv[1].rstrip("/")
grandparent = sys.argv[2].rstrip("/")
key_path = sys.argv[3]
scratch = sys.argv[4]
substitute_hex = sys.argv[5]

os.makedirs(scratch, exist_ok=True)
out = {"childResuid": list(os.getresuid()), "attempts": {}}


def attempt(key, fn):
    try:
        fn()
    except OSError as exc:
        out["attempts"][key] = {
            "ok": False,
            "exc": type(exc).__name__,
            "errno": errno.errorcode.get(exc.errno, exc.errno),
        }
    else:
        out["attempts"][key] = {"ok": True}


src = os.path.join(scratch, "substitute.key")
with open(src, "w") as fh:
    fh.write(substitute_hex)
decoy_dir = os.path.join(scratch, "decoy-store-dir")
os.makedirs(decoy_dir, exist_ok=True)

# -- the key file itself ---------------------------------------------------
attempt("key:read", lambda: open(key_path, "rb").read())
attempt("key:open-truncate", lambda: open(key_path, "w").close())
attempt("key:os.unlink", lambda: os.unlink(key_path))
attempt("key:os.symlink", lambda: os.symlink(src, key_path))
attempt("key:os.chmod", lambda: os.chmod(key_path, 0o666))
inside = os.path.join(store, "inside-src.key")
attempt("key:os.replace-same-mount", lambda: os.replace(inside, key_path))
attempt("key:os.replace-cross-mount", lambda: os.replace(src, key_path))
attempt("key:os.link-cross-mount", lambda: os.link(src, key_path))

# -- the store directory (the mount point) and everything inside it -------
attempt("store:listdir", lambda: os.listdir(store))
attempt("store:os.mkdir-inside", lambda: os.mkdir(os.path.join(store, "planted")))
attempt("store:os.chmod", lambda: os.chmod(store, 0o777))
attempt("store:os.rename", lambda: os.rename(store, store + "-moved"))
attempt("store:os.replace-decoy", lambda: os.replace(decoy_dir, store))
attempt("store:os.rmdir", lambda: os.rmdir(store))

# -- the grandparent (`/var/lib/ralphd`) -----------------------------------
attempt("grandparent:os.mkdir-sibling",
        lambda: os.mkdir(os.path.join(grandparent, "planted-sibling")))
attempt("grandparent:os.rename",
        lambda: os.rename(grandparent, grandparent + "-moved"))
attempt("grandparent:os.chmod", lambda: os.chmod(grandparent, 0o777))

sys.stdout.write(json.dumps(out))
'''

# Root inside the container, reproducing the engine's own posture
# (`separate_engine_identity()`), spawning the probe above through
# `privsep.agent_child_kwargs()` -- the engine's own production call, not
# merely `docker exec --user`.
_KEY_PROBE_DRIVER = r'''
import json
import os
import subprocess
import sys

from ralphd.engine import privsep

if os.getuid() != 0:
    sys.exit("this driver must start as real root to reproduce the engine")
agent = privsep.separate_engine_identity()
if agent is None:
    sys.exit("no agent identity in this container: no boundary to prove")
if not privsep.separated():
    sys.exit("uid separation is not active after separate_engine_identity()")
kwargs = privsep.agent_child_kwargs()
if kwargs != {"user": agent.uid, "group": agent.gid}:
    sys.exit("agent_child_kwargs() did not ask for the agent identity: %r"
             % (kwargs,))
res = subprocess.run([sys.executable, "-c", CHILD, *sys.argv[1:]],
                     capture_output=True, text=True, timeout=60, **kwargs)
if res.returncode != 0:
    sys.exit("the dropped probe child failed (rc=%d): %s%s"
             % (res.returncode, res.stdout, res.stderr))
doc = json.loads(res.stdout)
doc["engineResuid"] = list(os.getresuid())
doc["agentChildKwargs"] = kwargs
doc["separated"] = privsep.separated()
sys.stdout.write(json.dumps(doc))
'''

_ANCESTOR_ATTACK_HEX = (b"\x00" * 32).hex()


def _run_setup(container: str, run_root: str) -> dict:
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c", _SETUP_DRIVER,
         run_root, _OBLIGATION],
        capture_output=True, text=True, timeout=90)
    assert res.returncode == 0, res.stdout + res.stderr
    doc = json.loads(res.stdout)
    assert doc["separated"] is True and doc["resuid"] == [0, 1000, 0], doc
    return doc


def _run_key_probe(container: str, store: str, key_path: str,
                   scratch: str) -> dict:
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c",
         "CHILD = " + repr(_KEY_PROBE) + "\n" + _KEY_PROBE_DRIVER,
         store, _GRANDPARENT_IN_CONTAINER, key_path, scratch,
         _ANCESTOR_ATTACK_HEX],
        capture_output=True, text=True, timeout=90)
    assert res.returncode == 0, res.stdout + res.stderr
    doc = json.loads(res.stdout)
    assert doc["childResuid"] == [1000, 1000, 1000], (
        "the probe child was not FULLY dropped -- a half-dropped child "
        f"could climb back and proves nothing: {doc}")
    assert doc["separated"] is True and doc["engineResuid"] == [0, 1000, 0], doc
    return doc


def _assert_the_probe_can_actually_see_a_hole(container: str) -> None:
    """Negative control, in the SAME container, before the real check: an
    agent-owned scratch directory (no boundary claimed at all) must let
    the probe rename the "store" directory and read/replace the "key"
    file -- otherwise a pass on the real store proves nothing."""
    hole_root = "/tmp/c202-negative-control"
    hole_store = f"{hole_root}/run-secrets"
    hole_key = f"{hole_store}/fake.key"
    setup = (
        f"rm -rf {hole_root} {hole_store}-moved; mkdir -p {hole_store}; "
        f"printf 'realkeybytes\\n' > {hole_key}; "
        f"chown -R 1000:1000 {hole_root}")
    res = subprocess.run([REAL_DOCKER, "exec", container, "sh", "-c", setup],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr

    doc = _run_key_probe(container, hole_store, hole_key,
                         "/tmp/c202-negative-scratch")
    attempts = doc["attempts"]
    assert attempts["key:read"]["ok"] is True, (
        "the probe could not read an UNPROTECTED key file -- then its "
        f"verdict on the real store proves nothing: {attempts}")
    assert attempts["store:os.rename"]["ok"] is True, (
        "the probe could not rename an UNPROTECTED store directory -- "
        f"then its verdict on the real mount point proves nothing: {attempts}")
    assert attempts["key:os.replace-cross-mount"]["ok"] is True, attempts
    subprocess.run([REAL_DOCKER, "exec", container, "sh", "-c",
                    f"rm -rf {hole_root} {hole_store}-moved"],
                   capture_output=True, timeout=30)


def test_a_dropped_child_cannot_touch_the_run_secrets_store_or_its_key(ctl):
    """Requirement C.2/C.4: not the key file, not the store's mount-point
    directory, not the grandparent above it -- with the probe's own
    negative control run first, in the same container, so a pass here
    means something."""
    run_id, container = _start_idle_job(ctl, "c202-keyprobe")

    _assert_the_probe_can_actually_see_a_hole(container)

    setup = _run_setup(container, "/run/ralphd")
    assert setup["entries"] and setup["entries"][0]["adjudication"] == "CONFIRMED"
    assert setup["storeStat"]["uid"] == 0, (
        "the store's mount point is not root-owned -- C.2 unmet on the "
        f"real layout `_bind_run_secrets_mount` builds: {setup}")
    assert setup["storeStat"]["mode"] == "0o700", setup
    assert setup["keyStat"]["uid"] == 0 and setup["keyStat"]["mode"] == "0o600", (
        "the key file is not root-owned mode 0600 -- its own gid is not "
        "asserted: `root_for_write()` raises only the effective UID (real+"
        "saved uid 0, effective the agent's), never the GID, so a file "
        "created inside that window inherits the process's own effective "
        "GID (the agent's) -- harmless here because mode 0600 grants "
        f"nothing to any group at all: {setup}")
    assert setup["grandparentStat"]["uid"] == 0, (
        "`/var/lib/ralphd` itself is not root-owned -- the ancestor chain "
        f"above the store breaks here: {setup}")

    doc = _run_key_probe(container, setup["storeDir"], setup["keyPath"],
                         "/tmp/c202-probe-scratch")
    attempts = doc["attempts"]
    assert all(a["ok"] is False for a in attempts.values()), attempts
    for op in ("key:read", "key:open-truncate", "key:os.unlink",
               "key:os.symlink", "key:os.chmod", "key:os.replace-same-mount",
               "store:listdir", "store:os.mkdir-inside", "store:os.chmod",
               "store:os.rename", "store:os.replace-decoy", "store:os.rmdir",
               "grandparent:os.mkdir-sibling", "grandparent:os.rename",
               "grandparent:os.chmod"):
        got = attempts[op]
        assert got["ok"] is False and got["errno"] in _DENIED, (op, got)
    # the two cross-mount ones -- same structural reason
    # `test_tasks_record_docker.py` documents: no agent-writable directory
    # shares the store's mount, so EACCES/EPERM and EXDEV are both denials.
    for op in ("key:os.replace-cross-mount", "key:os.link-cross-mount"):
        got = attempts[op]
        assert got["ok"] is False and got["errno"] in _DENIED + ("EXDEV",), (op, got)

    # and the key/record on disk are untouched by any of the above
    after = _run_setup(container, "/run/ralphd")
    assert after["mintedHex"] == setup["mintedHex"], "the key material changed"
    assert after["entries"] == setup["entries"], "the record changed"
    assert after["tampered"] is False, after

    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


# The ancestor-rename-and-plant attack, run for real, by a fully dropped
# child: rename the store's mount point aside, then plant an agent-owned
# replacement directory holding a SUBSTITUTED key -- exactly the shape
# that falsified task 004's first attempt, aimed at requirement C's own
# store instead of the task record.
_ANCESTOR_ATTACK_CHILD = r'''
import errno
import json
import os
import sys

store = sys.argv[1]
key_name = sys.argv[2]
substitute_hex = sys.argv[3]
out = {"childResuid": list(os.getresuid()), "attempts": {}}


def attempt(key, fn):
    try:
        fn()
    except OSError as exc:
        out["attempts"][key] = {"ok": False,
                                "errno": errno.errorcode.get(exc.errno, exc.errno)}
    else:
        out["attempts"][key] = {"ok": True}


attempt("rename-store-aside", lambda: os.rename(store, store + "-stolen"))


def plant():
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, key_name), "w") as fh:
        fh.write(substitute_hex)


attempt("plant-replacement-store", plant)
sys.stdout.write(json.dumps(out))
'''

_ANCESTOR_ATTACK_DRIVER = r'''
import json
import os
import subprocess
import sys
from pathlib import Path

from ralphd.engine import petitions, privsep
from ralphd.engine.state import RunDir

if os.getuid() != 0:
    sys.exit("this driver must start as real root to reproduce the engine")
agent = privsep.separate_engine_identity()
if agent is None or not privsep.separated():
    sys.exit("no uid boundary in this container: nothing to prove")
res = subprocess.run([sys.executable, "-c", CHILD, *sys.argv[1:]],
                     capture_output=True, text=True, timeout=60,
                     **privsep.agent_child_kwargs())
if res.returncode != 0:
    sys.exit("the attack child failed (rc=%d): %s%s"
             % (res.returncode, res.stdout, res.stderr))
doc = json.loads(res.stdout)

# The "later episode" this run's own process still is, in memory -- drop
# the in-memory authority the way a fresh engine process would have none
# of, then re-read through the ONLY authoritative path,
# `read_confirmed_adjudications`.
run = RunDir(Path(sys.argv[4]))
petitions.forget_run_authority(run)
entries, tampered = petitions.read_confirmed_adjudications(run)
doc["entriesAfter"] = entries
doc["tamperedAfter"] = tampered
sys.stdout.write(json.dumps(doc))
'''


def test_the_ancestor_attack_does_not_reach_the_authoritative_key(ctl):
    """The falsifying attack, aimed at requirement C's own store: a fully
    dropped child renames the run-secrets mount point aside and plants an
    agent-owned replacement holding a substituted key. Both attempts are
    recorded (not required to succeed -- the FIRST is the actual product
    property; the second is reported for completeness), and the
    authoritative read afterwards -- from a simulated later episode that
    holds no in-memory authority -- still returns the TRUE CONFIRMED entry,
    never a forged one, and is never tampered."""
    run_id, container = _start_idle_job(ctl, "c202-ancestor")

    setup = _run_setup(container, "/run/ralphd")
    assert setup["entries"], "nothing was recorded -- nothing to attack"
    key_name = setup["keyPath"].rsplit("/", 1)[-1]

    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c",
         "CHILD = " + repr(_ANCESTOR_ATTACK_CHILD) + "\n"
         + _ANCESTOR_ATTACK_DRIVER,
         setup["storeDir"], key_name, _ANCESTOR_ATTACK_HEX, "/run/ralphd"],
        capture_output=True, text=True, timeout=90)
    assert res.returncode == 0, res.stdout + res.stderr
    doc = json.loads(res.stdout)
    assert doc["childResuid"] == [1000, 1000, 1000], doc

    # The property this task closes: the rename itself must be denied --
    # if this were ever True the store is exactly attempt 004-1's hole,
    # one requirement over.
    assert doc["attempts"]["rename-store-aside"]["ok"] is False, (
        "the run-secrets mount point could be renamed aside by a dropped "
        f"agent child -- requirement C.2 unmet: {doc}")
    assert doc["attempts"]["rename-store-aside"]["errno"] in _DENIED, doc

    # The authoritative read, from a process holding no in-memory
    # authority (a genuine later-episode simulation), still authenticates
    # the TRUE record -- never a forged one, and never silently empty.
    assert doc["tamperedAfter"] is False, doc
    assert doc["entriesAfter"] == setup["entries"], (
        "the authoritative read did not return the true CONFIRMED entry "
        f"after the attack: {doc}")
    assert doc["entriesAfter"][0]["obligation"] == _OBLIGATION, doc

    # and the key file on disk is still the one that was minted -- the
    # plant never displaced it
    key_check = subprocess.run(
        [REAL_DOCKER, "exec", container, "cat", setup["keyPath"]],
        capture_output=True, text=True, timeout=15)
    assert key_check.returncode == 0, key_check.stdout + key_check.stderr
    assert key_check.stdout.strip() == setup["mintedHex"], (
        "the key file's content changed -- the plant reached the "
        f"authoritative path: {key_check.stdout!r}")

    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


# ---------------------------------------------------------------------------
# C.5: the documented degrade is unchanged. No container needed -- this is
# the in-process half, kept in the same module as the container proof
# above so this requirement's whole evidence lives in one place.
# ---------------------------------------------------------------------------

def test_with_no_boundary_and_no_override_there_is_still_no_on_disk_key(
        tmp_path, monkeypatch):
    monkeypatch.delenv(petitions.RUN_SECRETS_DIR_ENV, raising=False)
    monkeypatch.setattr(privsep, "separated", lambda: False)
    assert petitions.key_store_dir() is None

    run = RunDir(root=tmp_path / "run")
    auth = petitions._authority(run, create=True)
    assert auth is not None, "an authority is still minted in memory"
    assert petitions._stored_key_path(run) is None, (
        "a key path exists with no boundary and no override -- the "
        "documented degrade must not persist anything to disk")

    # a "later episode" (fresh process, no in-memory authority) cannot
    # authenticate anything this one wrote -- fails closed, not open.
    petitions.record_confirmed_adjudications(
        run, [{"obligation": _OBLIGATION, "adjudication": "CONFIRMED"}])
    petitions.forget_run_authority(run)
    later = RunDir(root=run.root)
    entries, tampered = petitions.read_confirmed_adjudications(later)
    assert entries == [] and tampered is False, (
        "a later episode with no key store must read the record as "
        f"EMPTY, never as tampered and never as trusted: {(entries, tampered)}")
