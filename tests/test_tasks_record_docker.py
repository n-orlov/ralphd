"""Docker tier: task 004 (requirement B, #202) proved against a *real*
container -- the standing rule for tasks 003/004/005: a disposable
container, uid separation genuinely active, a child spawned through
`privsep.agent_child_kwargs()` itself (not merely `docker exec --user
1000:1000`, which reaches the same uid by a different route -- this module
exercises the engine's own production call), and both the in-place write
attempt and the directory-entry REPLACEMENT attempt.

Task 105 (#60 requirement D) made `tasks.json` itself root-owned/0644, but
that alone left a hole: the FILE was protected, the DIRECTORY it lived in
(the bind-mounted run root, agent-owned) was not, and a directory-entry
operation -- `os.replace`, `os.rename`, `os.unlink`, `os.link`, `os.symlink`
-- is gated by the PARENT's permissions, never the target's own.

**This task's own first attempt left the same hole one level up**, and was
rejected for it: it put the record in a root-owned 0700 `.tasks-protected/`
*inside* the run dir, where a fully dropped child could
`os.rename('.tasks-protected', ...)`, plant its own directory of that name
and have the engine's next `RunDir.read_tasks()` return the substituted
document. Re-asserting protection on every WRITE cannot close that (a read
between two writes resolves the name the attacker now owns), and no
permission bit can: the run dir is agent-owned by construction -- the agent
writes notes.md, the artifacts directory and every iteration record into it
-- and the engine's own effective uid IS the agent's.

So the authoritative record now lives OUTSIDE the run dir entirely, at
`/var/lib/ralphd/record/tasks.json` (`privsep.task_record_dir()`): a
directory `ralphctl start`/`resume` bind-mount from
`<registry>/configs/<run-id>/record` on the host -- so it still survives a
`resume`'s fresh container, and `ralphctl rm`/the hub's DELETE still remove
it with the rest of the run's state -- under `/var/lib/ralphd`, which is
root-owned 0755 in the running container -- created by the docker daemon as
it materialises this very mount point, not by any image layer (task 019
measured `/var/lib/ralphd` absent from both `python:3.12-slim-bookworm` and
the job image; see `privsep.provision_control_credentials()`). Not one
directory entry on that path is
modifiable at the agent uid, and no agent-writable path aliases it (the host
directory behind the mount lives under `config_root()`, mounted READ-ONLY at
`/config`, never under the run dir).

Four things, each needing a real container:

1. A fully dropped agent-uid child (`privsep.agent_child_kwargs()`, driven
   by a root driver reproducing `privsep.separate_engine_identity()`, the
   engine's own posture) cannot read, truncate, create, unlink, symlink,
   rename, replace or chmod the authoritative record OR ANY ANCESTOR ENTRY
   above it -- requirement B.1's "including every ancestor entry on the
   path, not only the file" -- with nothing in this module patching
   `os.replace`, and with the probe's own negative control (the rejected
   layout: the same file in an agent-writable parent) proving in the SAME
   container that these attempts genuinely succeed when the boundary is
   absent (`test_a_dropped_child_cannot_replace_or_write_the_record`).
2. The exact attack that falsified attempt 1, re-run end to end: the
   dropped child substitutes the run-dir view copy AND plants its own
   `.tasks-protected/tasks.json` in the run root -- both of which still
   succeed, because the run dir is and stays agent-writable -- and the
   engine's own `RunDir.read_tasks()` afterwards returns the AUTHORITATIVE
   document anyway
   (`test_the_ancestor_attack_that_falsified_attempt_1_no_longer_reaches_the_record`).
3. A file precreated at the write's own privileged temporary name
   (`tasks.json.tmp`), and a symlink planted there instead, both fail to
   redirect the write -- proven by running the real `write_protected_tasks_
   json()` (as the engine's own root driver would call it) against a
   directory an attacker got to first, before protection existed
   (`test_a_precreated_file_and_a_symlink_at_the_temp_name_cannot_redirect_
   the_write`).
4. A resumed container, over the exact same run directory and record mount,
   still reads the authoritative record -- continuity across the
   release-at-shutdown / re-protect-on-first-write cycle
   (`test_a_resumed_container_still_reads_the_protected_record`).

Fixtures, cleanup discipline and the two environment-specific wrinkles
(bind-mount path translation, API reachability from inside this job's own
container) are exactly `tests/test_docker_sibling_e2e.py`'s.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from test_docker_sibling_e2e import (  # noqa: F401  (fixtures used by name)
    REAL_DOCKER,
    Ctl,
    _container_running,
    _unique_run_id,
    _wait_status,
    ctl,
    gateway_ip,
    pytestmark,
    test_image,
    work_root,
)

# `privsep.DEFAULT_TASK_RECORD_DIR`, spelled out rather than imported: this
# is the path a real `ralphctl start` bind-mounts inside a real container,
# and a test that read it from the same constant the product does could not
# notice the mount silently moving.
_RECORD_DIR_IN_CONTAINER = "/var/lib/ralphd/record"
_RECORD_IN_CONTAINER = f"{_RECORD_DIR_IN_CONTAINER}/tasks.json"

_DENIED = ("EACCES", "EPERM")

# The probe a fully dropped agent child runs *inside* the job container --
# one attempt per syscall that could reach the authoritative record or any
# ancestor entry above it, each recorded with its errno rather than asserted
# here (a probe that swallowed a success would otherwise read as a pass).
# Deliberately NOT a mock of anything: every line below is a real syscall
# made at uid 1000 against the real layout `ralphctl start` built.
_RECORD_PROBE = r'''
import errno
import json
import os
import sys

record_dir = sys.argv[1].rstrip("/")
scratch = sys.argv[2]
substitute = sys.argv[3]
record = os.path.join(record_dir, "tasks.json")
parent = os.path.dirname(record_dir)

os.makedirs(scratch, exist_ok=True)
out = {
    "childResuid": list(os.getresuid()),
    "childResgid": list(os.getresgid()),
    "attempts": {},
}


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


src = os.path.join(scratch, "substitute.json")
with open(src, "w") as fh:
    fh.write(substitute)
decoy_dir = os.path.join(scratch, "decoy-record-dir")
os.makedirs(decoy_dir, exist_ok=True)

# -- the record's own entry, and every other name inside its directory ----
attempt("record:read", lambda: open(record, "rb").read())
attempt("record:listdir-parent", lambda: os.listdir(record_dir))
attempt("record:open-truncate", lambda: open(record, "w").close())
attempt("record:open-create-sibling",
        lambda: open(os.path.join(record_dir, "planted.json"), "w").close())
attempt("record:os.mkdir-inside",
        lambda: os.mkdir(os.path.join(record_dir, "planted-dir")))
attempt("record:os.symlink", lambda: os.symlink(src, record))
attempt("record:os.unlink", lambda: os.unlink(record))
# A rename/replace whose BOTH ends are inside the protected directory: no
# cross-device shortcut can mask the answer, so an EACCES here is the
# permission boundary talking and nothing else. (The source need not exist:
# resolving a path *through* a 0700 root-owned directory is itself denied at
# the agent uid, which is the point.)
inside = os.path.join(record_dir, "inside-src.json")
attempt("record:os.replace-same-mount", lambda: os.replace(inside, record))
attempt("record:os.rename-same-mount", lambda: os.rename(inside, record))
# ... and the exact operation #202 was reported as -- an os.replace of a
# VALID substituted document (parseable, right shape) from an agent-owned
# scratch file -- which is additionally barred by the record directory
# being a separate mount from anything the agent can write to at all.
attempt("record:os.replace-cross-mount", lambda: os.replace(src, record))
attempt("record:os.link-cross-mount", lambda: os.link(src, record))
attempt("record:os.chmod-dir", lambda: os.chmod(record_dir, 0o777))

# -- every ancestor entry on the path (requirement B.1) ------------------
attempt("dir:os.rename", lambda: os.rename(record_dir, record_dir + "-moved"))
attempt("dir:os.replace-decoy", lambda: os.replace(decoy_dir, record_dir))
attempt("dir:os.rmdir", lambda: os.rmdir(record_dir))
attempt("parent:os.mkdir-sibling",
        lambda: os.mkdir(os.path.join(parent, "record-sibling")))
attempt("parent:os.rename", lambda: os.rename(parent, parent + "-moved"))
attempt("parent:os.chmod", lambda: os.chmod(parent, 0o777))
attempt("grandparent:os.rename",
        lambda: os.rename(os.path.dirname(parent),
                          os.path.dirname(parent) + "-moved"))

sys.stdout.write(json.dumps(out))
'''

# The driver: root inside the container, reproducing the engine's own
# privilege posture (`separate_engine_identity()`) and then spawning the
# probe above exactly the way `engine.runner.PiRunner.run` spawns an
# iteration -- through `privsep.agent_child_kwargs()`.
_RECORD_PROBE_DRIVER = r'''
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

_VALID_SUBSTITUTE = json.dumps({"version": 1, "tasks": [
    {"id": "1", "title": "substituted", "successCriteria": "x",
     "status": "pending"}]})


def _run_record_probe(container: str, record_dir: str, scratch: str) -> dict:
    """Run `_RECORD_PROBE` inside `container` as a child spawned through the
    engine's own `privsep.agent_child_kwargs()`, and return its findings."""
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c",
         "CHILD = " + repr(_RECORD_PROBE) + "\n" + _RECORD_PROBE_DRIVER,
         record_dir, scratch, _VALID_SUBSTITUTE],
        capture_output=True, text=True, timeout=90)
    assert res.returncode == 0, res.stdout + res.stderr
    doc = json.loads(res.stdout)
    assert doc["childResuid"] == [1000, 1000, 1000], (
        "the probe child was not FULLY dropped -- a half-dropped child could "
        f"climb back and proves nothing: {doc}")
    assert doc["separated"] is True and doc["engineResuid"] == [0, 1000, 0], doc
    return doc


def _assert_the_probe_can_actually_see_a_hole(container: str) -> None:
    """The probe's own negative control, in the same container, before the
    real check: point `_RECORD_PROBE` at THE REJECTED LAYOUT -- a 0400
    `tasks.json` in a root-owned 0700 directory whose PARENT is
    agent-writable, i.e. exactly what task 004's first attempt built inside
    the run dir -- and require it to report the directory as renameable.

    This is what makes the real assertions evidence rather than ceremony: a
    probe that only tried `open(path, "w")` would call the rejected layout
    secure (truncation is refused by the file's own mode, and by the 0700
    directory) while `os.rename` of the DIRECTORY succeeds through the
    writable parent -- which is precisely how attempt 1 was falsified.
    """
    hole_parent = "/tmp/a202-negative-control"
    hole_dir = f"{hole_parent}/record"
    setup = (
        f"rm -rf {hole_parent} {hole_dir}-moved; mkdir -p {hole_dir}; "
        f"printf '{{\"tasks\": [{{\"id\": \"real\"}}]}}\\n' > {hole_dir}/tasks.json; "
        f"chmod 400 {hole_dir}/tasks.json; chmod 700 {hole_dir}; "
        # the parent is agent-owned (the run dir's own shape); the protected
        # directory and file are root-owned, exactly attempt 1's layout
        f"chown 1000:1000 {hole_parent}")
    res = subprocess.run([REAL_DOCKER, "exec", container, "sh", "-c", setup],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr

    doc = _run_record_probe(container, hole_dir, "/tmp/a202-negative-scratch")
    attempts = doc["attempts"]
    # the file's own mode and the 0700 directory DO refuse the naive attacks
    assert attempts["record:open-truncate"]["ok"] is False, attempts
    assert attempts["record:read"]["ok"] is False, attempts
    # ... and the directory entry one level up is wide open anyway: this is
    # the finding that rejected attempt 1, reproduced here on purpose so the
    # real target's denial below means something.
    assert attempts["dir:os.rename"]["ok"] is True, (
        "the probe could not rename a root-owned protected directory out of "
        "an AGENT-WRITABLE parent -- then its verdict on the real record "
        f"directory proves nothing: {attempts}")
    subprocess.run([REAL_DOCKER, "exec", container, "sh", "-c",
                    f"rm -rf {hole_parent} {hole_dir}-moved"],
                   capture_output=True, timeout=30)


def _read_record_as_root(container: str) -> dict:
    """The authoritative record's content, read through `docker exec` (root
    inside the container) -- never a host-side `Path.read_text()`: this test
    process runs at the same agent uid the protected 0700/0600 pair denies,
    which is the whole point, so a host-side read would hit the identical
    EACCES."""
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "cat", _RECORD_IN_CONTAINER],
        capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    return json.loads(res.stdout)


def _start_idle_job(ctl_, prefix: str, **kwargs) -> tuple[str, str]:
    """Start a one-task job that idles when it is done (so the container is
    still up for the probes), waited to a terminal verdict."""
    run_id = _unique_run_id(prefix)
    res = ctl_.start(run_id, extra_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
                     extra=("--on-complete", "idle"), **kwargs)
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"
    _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                 ("succeeded", "failed", "aborted"))
    assert _container_running(container) is True
    return run_id, container


def test_a_dropped_child_cannot_replace_or_write_the_record(ctl):
    """Requirement B/B.1: not the file, not its directory, not one ancestor
    entry above it -- with the probe's own negative control (the rejected
    layout) run first, in the same container, so a pass here means
    something."""
    run_id, container = _start_idle_job(ctl, "e202-tasks-probe")

    _assert_the_probe_can_actually_see_a_hole(container)

    before = _read_record_as_root(container)
    assert before.get("tasks"), (
        "planning never wrote an authoritative record -- nothing to attack")

    doc = _run_record_probe(container, _RECORD_DIR_IN_CONTAINER,
                            "/tmp/a202-probe-scratch")
    attempts = doc["attempts"]

    # Every attempt failed, whatever the errno.
    assert all(a["ok"] is False for a in attempts.values()), attempts

    # The permission-based ones -- the boundary itself talking, no
    # cross-device (EXDEV) accident involved anywhere in this group:
    for op in ("record:read", "record:listdir-parent", "record:open-truncate",
               "record:open-create-sibling", "record:os.mkdir-inside",
               "record:os.symlink", "record:os.unlink",
               "record:os.replace-same-mount", "record:os.rename-same-mount",
               "record:os.chmod-dir",
               # the ancestor clause of B.1: the record DIRECTORY itself and
               # the directory above it, which attempt 1 left renameable
               "dir:os.rename", "dir:os.replace-decoy", "dir:os.rmdir",
               "parent:os.mkdir-sibling", "parent:os.rename", "parent:os.chmod",
               "grandparent:os.rename"):
        got = attempts[op]
        assert got["ok"] is False and got["errno"] in _DENIED, (op, got)

    # The two cross-mount ones -- the literal shape #202 reported, an
    # `os.replace` of a VALID substituted document (never merely malformed
    # content, so the refusal cannot be a parse rejection in disguise).
    # They fail for a second, structural reason on top of the permission
    # one: the record directory is a bind mount of its own, and there is no
    # agent-writable directory anywhere on it, so a same-mount source for a
    # rename/link does not exist to begin with (the kernel answers EXDEV
    # before it ever reaches a permission check). The same-mount pair above
    # is what proves the permission answer itself.
    for op in ("record:os.replace-cross-mount", "record:os.link-cross-mount"):
        got = attempts[op]
        assert got["ok"] is False and got["errno"] in _DENIED + ("EXDEV",), (op, got)

    # and the record on disk is untouched by any of the above
    assert _read_record_as_root(container) == before, (
        "the record changed under the probe")

    # `--on-complete idle` (needed above so the container stayed up for the
    # probe) means `amain()` never returns on its own -- the `ctl` fixture's
    # own teardown is a hard `docker rm -f`, which gives the engine no
    # chance to run `privsep.release_protected_tasks_dir()`. A graceful
    # `ralphctl stop` here (a `/shutdown` request, exactly what SIGTERM
    # drives) lets that funnel run before the container goes away, so this
    # test does not leave a root-owned record directory behind in the
    # host-side registry that a later unprivileged `rmtree` cannot remove.
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


# The attack that falsified attempt 1, made by a fully dropped child: the
# run dir is agent-writable (it must be -- the agent owns notes.md, the
# artifacts dir and every iteration record), so BOTH of these still
# succeed. What must no longer follow is the engine believing either one.
_ANCESTOR_ATTACK_CHILD = r'''
import errno
import json
import os
import sys

run_root = sys.argv[1]
substitute = sys.argv[2]
out = {"childResuid": list(os.getresuid()), "attempts": {}}


def attempt(key, fn):
    try:
        fn()
    except OSError as exc:
        out["attempts"][key] = {"ok": False,
                                "errno": errno.errorcode.get(exc.errno, exc.errno)}
    else:
        out["attempts"][key] = {"ok": True}


legacy = os.path.join(run_root, ".tasks-protected")
# 1. if anything of that name is there, move it aside (attempt 1's hole)
attempt("rename-legacy-dir-aside",
        lambda: os.rename(legacy, legacy + "-stolen"))
# 2. plant an agent-owned replacement holding a valid substituted document
def plant():
    os.makedirs(legacy, exist_ok=True)
    with open(os.path.join(legacy, "tasks.json"), "w") as fh:
        fh.write(substitute)
attempt("plant-replacement-record", plant)
# 3. and substitute the agent-readable VIEW copy too. In place this is
#    refused (the view keeps task 105's root-owned 0644), but its
#    directory ENTRY is replaceable -- its parent is the agent-owned run
#    root, and that is precisely why the record no longer lives here.
attempt("overwrite-view-in-place",
        lambda: open(os.path.join(run_root, "tasks.json"), "w").write(substitute))
def replace_view():
    tmp = os.path.join(run_root, ".view-substitute.json")
    with open(tmp, "w") as fh:
        fh.write(substitute)
    os.replace(tmp, os.path.join(run_root, "tasks.json"))
attempt("replace-view-entry", replace_view)
sys.stdout.write(json.dumps(out))
'''

# Root inside the container: reproduce the engine's posture, spawn the
# attack above as a fully dropped child, then read the task record back
# through the engine's OWN production read path and report what it returned.
_ANCESTOR_ATTACK_DRIVER = r'''
import json
import os
import subprocess
import sys
from pathlib import Path

from ralphd.engine import privsep
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
run = RunDir(Path(sys.argv[1]))
read = run.read_tasks_result()
doc["engineRead"] = read.doc
doc["engineReadSource"] = read.source
doc["protectedDir"] = str(run.protected_tasks_dir)
doc["viewOnDisk"] = json.loads(run.tasks_file.read_text())
sys.stdout.write(json.dumps(doc))
'''


def test_the_ancestor_attack_that_falsified_attempt_1_no_longer_reaches_the_record(ctl):
    """The rejection, re-run: a fully dropped child renames any protected
    directory in the run root aside, plants its own with a valid substituted
    task document, and substitutes the view copy in place and by
    directory-entry replacement. All of that still SUCCEEDS -- the run dir
    is agent-writable and must stay so -- and the engine's own
    `RunDir.read_tasks_result()` afterwards returns the authoritative
    record regardless, because the record does not live there any more."""
    run_id, container = _start_idle_job(ctl, "e202-ancestor")

    authoritative = _read_record_as_root(container)
    assert authoritative.get("tasks"), (
        "planning never wrote an authoritative record -- nothing to attack")

    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c",
         "CHILD = " + repr(_ANCESTOR_ATTACK_CHILD) + "\n"
         + _ANCESTOR_ATTACK_DRIVER,
         "/run/ralphd", _VALID_SUBSTITUTE],
        capture_output=True, text=True, timeout=90)
    assert res.returncode == 0, res.stdout + res.stderr
    doc = json.loads(res.stdout)
    assert doc["childResuid"] == [1000, 1000, 1000], doc

    # the attack apparatus genuinely worked: the child DID plant a record of
    # its own in the run root and DID substitute the view copy. (Renaming a
    # `.tasks-protected` aside is recorded but not required to succeed --
    # under this layout there is no such directory left to rename, which is
    # itself the fix.)
    assert doc["attempts"]["plant-replacement-record"]["ok"] is True, doc
    assert doc["attempts"]["overwrite-view-in-place"]["ok"] is False, (
        "an in-place write to the VIEW copy should have been refused -- it "
        f"is root-owned 0644 (task 105's own posture): {doc}")
    assert doc["attempts"]["overwrite-view-in-place"]["errno"] == "EACCES", doc
    assert doc["attempts"]["replace-view-entry"]["ok"] is True, (
        "replacing the VIEW copy's directory ENTRY must succeed -- its "
        "parent is the agent-owned run root, no file mode overrides that, "
        f"and that asymmetry is why the record moved out: {doc}")
    assert doc["viewOnDisk"]["tasks"][0]["title"] == "substituted", (
        "the view copy was not actually substituted -- the attack did not "
        f"happen, so nothing below is evidence: {doc}")

    # and the engine read the AUTHORITATIVE record anyway
    assert doc["protectedDir"] == _RECORD_DIR_IN_CONTAINER, doc
    assert doc["engineReadSource"] == "file", doc
    assert doc["engineRead"] == authoritative, (
        "the engine's own read returned a document the agent planted -- "
        f"requirement B/B.1 unmet, exactly attempt 1's rejection: {doc}")
    assert "substituted" not in json.dumps(doc["engineRead"]), doc

    # the record on disk is still the authoritative one, too
    assert _read_record_as_root(container) == authoritative

    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


_SELF_HEAL_DRIVER = r'''
import json
import os
import sys
from pathlib import Path

from ralphd.engine import privsep

privsep.separate_engine_identity()

protected_path = Path(sys.argv[1])
view_path = Path(sys.argv[2])
doc = {"version": 1, "tasks": [{"id": "1", "title": "real", "successCriteria": "x",
                                "status": "pending"}]}
privsep.write_protected_tasks_json(protected_path, view_path, doc)

out = {}
with privsep.root_for_write():
    with open(protected_path) as fh:
        out["protectedContent"] = json.load(fh)
    st = os.lstat(protected_path)
    out["protectedIsRegularFile"] = not os.path.islink(protected_path)
    out["protectedMode"] = oct(st.st_mode & 0o777)
    out["protectedUid"] = st.st_uid
sys.stdout.write(json.dumps(out))
'''


def _run_self_heal_driver(container: str, scratch: str, plant: str) -> dict:
    """Set up `scratch/record/` as an agent-writable directory (the shape it
    has before protection is ever established -- the exact window a planted
    entry would need to matter), plant something hostile at the write's own
    temp name inside it, then call `privsep.write_protected_tasks_json()`
    for real (as root, reproducing the engine's own call site) and report
    what actually landed.

    `plant`: "file" precreates a plain file at the temp name; "symlink"
    plants a symlink pointing at an agent-writable decoy elsewhere, so a
    write that naively followed it would land the real content somewhere
    an agent could read it back out unprotected -- and the decoy's own
    content proves whether that happened.
    """
    protected_dir = f"{scratch}/record"
    protected_path = f"{protected_dir}/tasks.json"
    view_path = f"{scratch}/tasks.json"
    decoy = f"{scratch}/decoy-elsewhere.json"
    setup = [
        f"rm -rf {scratch}", f"mkdir -p {protected_dir}",
        f"chown -R 1000:1000 {scratch}",
    ]
    if plant == "symlink":
        setup.append(f"printf 'decoy-untouched\\n' > {decoy}")
        setup.append(f"chown 1000:1000 {decoy}")
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "sh", "-c", " && ".join(setup)],
        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr

    # the plant itself is done AS THE AGENT UID (`docker exec --user
    # 1000:1000`) -- the identity that would genuinely have this window,
    # not root setting up its own trap.
    if plant == "file":
        plant_cmd = f"printf 'PLANTED-FILE\\n' > {protected_path}.tmp"
    elif plant == "symlink":
        plant_cmd = f"ln -s {decoy} {protected_path}.tmp"
    else:
        raise AssertionError(plant)
    plant_res = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", container, "sh", "-c", plant_cmd],
        capture_output=True, text=True, timeout=15)
    assert plant_res.returncode == 0, plant_res.stdout + plant_res.stderr

    driver_res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c", _SELF_HEAL_DRIVER,
         protected_path, view_path],
        capture_output=True, text=True, timeout=30)
    assert driver_res.returncode == 0, driver_res.stdout + driver_res.stderr
    out = json.loads(driver_res.stdout)

    if plant == "symlink":
        decoy_res = subprocess.run(
            [REAL_DOCKER, "exec", container, "cat", decoy],
            capture_output=True, text=True, timeout=15)
        out["decoyContent"] = decoy_res.stdout
    subprocess.run([REAL_DOCKER, "exec", container, "rm", "-rf", scratch],
                   capture_output=True, timeout=30)
    return out


@pytest.mark.parametrize("plant", ["file", "symlink"])
def test_a_precreated_file_and_a_symlink_at_the_temp_name_cannot_redirect_the_write(
        ctl, plant):
    run_id, container = _start_idle_job(ctl, f"e202-tmp-{plant}")

    out = _run_self_heal_driver(container, "/tmp/a202-self-heal", plant)
    # the real document landed -- the planted entry was discarded, never
    # trusted, and the write was never redirected through it
    assert out["protectedContent"]["tasks"][0]["title"] == "real", out
    assert out["protectedIsRegularFile"] is True, out  # never a symlink
    assert out["protectedMode"] == "0o600", out
    assert out["protectedUid"] == 0, out
    if plant == "symlink":
        # the decoy the symlink pointed at was never written through --
        # if the write had followed the symlink, the real content would
        # have landed THERE instead, agent-readable, which is the whole
        # point of planting it
        assert out["decoyContent"] == "decoy-untouched\n", out

    # `_start_idle_job` above also started a REAL job, whose real planning
    # wrote a real authoritative record into this run's own record mount --
    # independent of `_run_self_heal_driver`'s scratch dir, which lives
    # inside the container's own writable layer and needs no cleanup. A
    # graceful stop here (see the sibling comment in
    # `test_a_dropped_child_cannot_replace_or_write_the_record`) lets
    # `privsep.release_protected_tasks_dir()` run before the `ctl` fixture's
    # hard `docker rm -f` teardown.
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


def test_a_resumed_container_still_reads_the_protected_record(ctl, work_root):
    """A resumed container, over the exact same run dir and record mount,
    still reads the authoritative record -- across the release-at-shutdown /
    re-protect-on-first-write cycle `privsep.release_protected_tasks_dir`/
    `_ensure_protected_tasks_dir` describe."""
    run_id = _unique_run_id("e202-resume")
    hold_rel = ".hold-e202-resume"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id, extra_env={
        "STUB_TASKS": "2",
        "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
        # Task 008 (B.2): without STUB_HOLD_PHASE the hold catches EVERY
        # invocation, planning included -- which would mean the record never
        # exists at all while this first container is up (exactly
        # `test_resume_continues_after_container_stop`'s own pattern, which
        # never inspects the record for that reason). This test needs the
        # opposite: planning through immediately, so the authoritative copy
        # already exists before the stop, and only the first WORKER
        # iteration blocked -- naming the target phase is what buys that.
        "STUB_HOLD_PHASE": "worker",
        "STUB_HOLD_TIMEOUT_S": "60"},
        extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    # planning (unheld) writes the record deterministically fast; poll the
    # run-root agent-readable VIEW copy (`tasks.json`, host-readable,
    # unlike the protected copy) rather than trust `state == "running"`
    # (set at the very top of `_run_job_core`, before planning has run at
    # all) or the first readable `/status` (same problem, one layer up).
    rdir = ctl.registry / "runs" / run_id
    deadline = time.time() + 30
    tasks_doc = None
    while time.time() < deadline:
        try:
            tasks_doc = json.loads((rdir / "tasks.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            tasks_doc = None
        if tasks_doc and len(tasks_doc.get("tasks", [])) >= 2:
            break
        time.sleep(0.4)
    assert tasks_doc and len(tasks_doc.get("tasks", [])) >= 2, (
        "planning's first write never landed within the deadline -- not a "
        f"genuine mid-run proof: {tasks_doc}")

    # while the first container is still up: the authoritative copy already
    # exists (planning's own first write), root-owned inside a root-owned
    # 0700 directory, with the run dir holding no protected directory at all
    check = subprocess.run(
        [REAL_DOCKER, "exec", container, "sh", "-c",
         f"stat -c '%U %a' {_RECORD_DIR_IN_CONTAINER} && "
         f"stat -c '%U %a' {_RECORD_IN_CONTAINER} && "
         "stat -c '%U %a' /var/lib/ralphd && "
         "ls -a /run/ralphd | grep -c tasks-protected || true"],
        capture_output=True, text=True, timeout=15)
    assert check.returncode == 0, check.stdout + check.stderr
    lines = check.stdout.splitlines()
    assert lines[0] == "root 700", lines
    assert lines[1] == "root 600", lines
    assert lines[2] == "root 755", (
        "the record mount's own parent is not root-owned -- the ancestor "
        f"clause of B.1 rests on it: {lines}")
    assert lines[3] == "0", (
        f"the run dir still carries a protected record directory: {lines}")

    res = subprocess.run([REAL_DOCKER, "stop", "-t", "5", container],
                        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert _container_running(container) is False

    hold_file_host.touch()  # release, exactly test_resume_continues...'s pattern

    res = ctl.resume(run_id, extra=("--iterations", "+10"))
    assert res.returncode == 0, res.stderr
    meta2 = json.loads(res.stdout)
    container2 = f"ralphd-{run_id}"
    assert meta2["container"] != meta["container"]

    final = _wait_status(meta2["apiUrl"], lambda s: s.get("state") in
                         ("succeeded", "failed", "aborted"), timeout=90)
    assert final["verdict"] == "verified", final

    # the resumed container's own engine re-established protection on its
    # first write, and the AUTHORITATIVE copy inside it carries the real,
    # continued task history -- both tasks completed, not reset
    check2 = subprocess.run(
        [REAL_DOCKER, "exec", container2, "sh", "-c",
         f"stat -c '%U %a' {_RECORD_DIR_IN_CONTAINER} && "
         f"cat {_RECORD_IN_CONTAINER}"],
        capture_output=True, text=True, timeout=15)
    assert check2.returncode == 0, check2.stdout + check2.stderr
    first_line, _, rest = check2.stdout.partition("\n")
    assert first_line == "root 700", first_line
    protected_doc = json.loads(rest)
    assert len(protected_doc["tasks"]) == 2
    assert all(t["status"] == "completed" for t in protected_doc["tasks"]), protected_doc

    # and every ordinary reader -- `ralphctl tasks`, host-side, unprivileged
    # -- agrees, reading the agent-readable view, never needing root itself
    res = ctl.run("tasks", run_id)
    assert res.returncode == 0, res.stderr
    view_doc = json.loads(res.stdout)
    assert len(view_doc["tasks"]) == 2
    assert all(t["status"] == "completed" for t in view_doc["tasks"]), view_doc

    # `resume` inherits `--on-complete idle` from the original `start` (it
    # is persisted job config, not a fresh `--env` pair), so container2 is
    # still up right now -- graceful `ralphctl stop` lets
    # `privsep.release_protected_tasks_dir()` run before the `ctl` fixture's
    # hard `docker rm -f` teardown, same as the first container above.
    stop_res2 = ctl.stop(run_id, force=True)
    assert stop_res2.returncode == 0, stop_res2.stderr
