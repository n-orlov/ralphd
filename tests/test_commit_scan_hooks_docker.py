"""Docker tier: task cure-01-02 (PRD requirement I.1, DoD 9, #210 -- review
finding R2) proved against a *real* container -- the same standing rule
tasks 003/004/005 already established for `/var/lib/ralphd`'s other two
protected resources: a disposable container, uid separation genuinely
active, a probe child spawned through `privsep.agent_child_kwargs()` itself
(never merely `docker exec --user`, which reaches the same uid by a
different route), and a negative control proving the probe can actually see
a hole before it is trusted on the real layout.

## The finding, measured live (review resume 0196)

`LoopSupervisor._install_commit_scan_push_gate` installed the git-sink
`pre-push`/`commit-msg`/`pre-commit` hooks into `run.root /
"commit-scan-hooks"` -- inside the bind-mounted, AGENT-OWNED run
directory -- and never raised its own effective uid to write them.
`install_push_gate` already wrote every hook file 0o555 (no write bit for
anyone, owner included) and the `scan-base` note 0o444, but neither mode
bit protects a DIRECTORY ENTRY: `os.unlink`/`os.rename`/`os.replace`
against a name inside a directory is gated by the PARENT directory's own
permissions, never the target file's mode. Since the parent (the run
directory) was agent-owned throughout -- and the engine's own EFFECTIVE
uid under `separate_engine_identity()` IS the agent's, so an un-elevated
`mkdir`/`chmod` produces an agent-owned directory regardless of what mode
it sets -- a dropped agent-uid child could always replace the whole
`commit-scan-hooks` directory (or any file inside it) through that parent,
mode bits notwithstanding. Review's own probe measured exactly that:
`agent_euid=1000 hook_uid=1000`, and both `chmod-and-write` and
`unlink-and-replace` succeeded against the installed hook.

## The fix

`privsep.commit_scan_hooks_dir()` places the hooks under
`/var/lib/ralphd/commit-scan-hooks` -- parented by the same root-owned
`/var/lib/ralphd` that already parents `CONTROL_CREDENTIALS_DIR` and the
run-secrets mount -- and `_install_commit_scan_push_gate` now calls
`commit_scan.install_push_gate` inside `privsep.root_for_write()`, so the
directory and every file `install_push_gate` creates in it are genuinely
root-owned when the boundary is active. The keyed-hash snapshot files
(`KNOWN_SECRETS_FILENAME`/`KNOWN_SECRETS_PEPPER_FILENAME`) are 0o644 rather
than the pre-fix 0o600 so the `pre-push` hook process -- which runs at the
AGENT uid, spawned by the worker's own `git push` -- can still read them
back; read access was never the property under threat, only write.

## What the boundary actually is (and what it deliberately is NOT)

The protected property is every MUTATION of a hook, of the directory
holding it, or of any ancestor entry above it: `open(..., "w")`, `chmod`,
`unlink`, `rename`, `replace`, and creating a new entry inside. READ,
`listdir` and traverse/exec are deliberately still PERMITTED to the agent
uid, and that is not a gap: the hook processes themselves run at the AGENT
uid (git spawns `pre-commit`/`commit-msg`/`pre-push` from the worker's own
`git commit`/`git push`), so the root-owned hook is installed 0o555 inside
a root-owned 0o755 directory precisely so the agent can still read and
execute it. A probe that demanded read be denied too would be demanding
the gate stop working; what makes the directory safe is that neither the
hook's mode nor the directory's grants the agent uid a WRITE, and a
directory-entry operation needs write on the PARENT, which is root's.
So this module's probe classifies every attempt it makes
(`_MUST_BE_DENIED` / `_MUST_STILL_SUCCEED` below, checked exhaustively
against the probe source at import time) and asserts both halves: the
mutations denied, the reads still working.

Four things, each needing a real container:

1. A fully dropped agent-uid child cannot chmod-and-write, unlink-and-
   replace, or rename-and-plant the installed `pre-push` hook, the
   directory holding it, or any ancestor above it, up to the container
   root -- while still being able to read the hook and list its directory,
   which the gate itself depends on -- with the probe's own negative
   control (an unprotected scratch directory, same container) proving the
   mutating attempts genuinely succeed when no boundary is claimed
   (`test_a_dropped_child_cannot_rewrite_the_installed_hook_or_its_directory`).
2. The exact same probe against an UNPROTECTED scratch directory succeeds,
   proving the probe can see a hole (folded into (1) as the negative
   control run first, in the same container).
3. An engine refresh (`_install_commit_scan_push_gate` called a second
   time, exactly as it is on every worker iteration) still succeeds, still
   root-owned, still pointed at the same directory
   (`test_an_engine_refresh_still_succeeds_after_the_first_install`).
4. A clean agent-uid commit (through the local `commit-msg`/`pre-commit`
   hooks) and a clean agent-uid push (through `pre-push`, to a local bare
   remote) both still succeed once the hooks live behind the boundary --
   the fix must not turn "protected" into "unusable"
   (`test_a_clean_agent_commit_and_push_still_succeed_through_the_protected_hooks`).

Fixtures, cleanup discipline and the two environment-specific wrinkles
(bind-mount path translation, API reachability from inside this job's own
container) are exactly `tests/test_docker_sibling_e2e.py`'s, reused here
through `test_tasks_record_docker.py`'s already-proven `_start_idle_job`.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

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

_HOOKS_DIR_IN_CONTAINER = "/var/lib/ralphd/commit-scan-hooks"
_GRANDPARENT_IN_CONTAINER = "/var/lib/ralphd"
_DENIED = ("EACCES", "EPERM")

# Every MUTATION the probe below attempts against the installed hook, the
# directory holding it, or any ancestor up to the container root. All of
# these must be refused with EACCES/EPERM under the real boundary -- this
# is the deliverable of requirement I.1/DoD 9.
_MUST_BE_DENIED = (
    "hook:open-write", "hook:os.chmod", "hook:os.unlink", "hook:os.replace",
    "dir:os.mkdir-inside", "dir:os.chmod", "dir:os.rename",
    "dir:os.replace-decoy", "dir:os.rmdir",
    "grandparent:os.mkdir-sibling", "grandparent:os.rename",
    "grandparent:os.chmod",
    "varlib:os.mkdir-inside", "varlib:os.chmod",
    "var:os.mkdir-inside", "var:os.chmod",
    "root:os.mkdir-inside", "root:os.chmod",
)

# The two NON-mutating attempts, which must keep SUCCEEDING at the agent
# uid: the hook processes run at that uid (git spawns them from the
# worker's own `git commit`/`git push`), so a boundary that denied reading
# or listing would break the gate instead of protecting it. Asserting them
# denied -- as an earlier revision of this module wrongly did -- asserts
# the opposite of what the shipped, deliberately 0o555-in-0o755 layout
# provides and of what test_a_clean_agent_commit_and_push_still_succeed_
# through_the_protected_hooks requires.
_MUST_STILL_SUCCEED = ("hook:read", "dir:listdir")

# A synthetic-only value (PRD requirement I.9): never a real credential,
# never matched by anything real -- this module never even needs the
# redaction map to fire, it only proves the DIRECTORY boundary, so no
# secret value appears anywhere below.
_SYNTH_LEAK = "sk-synthetic-hooks-docker-0123456789abcdefSYNTH"

# Installs the real push/commit-msg/pre-commit gate through the real
# product entry point (`LoopSupervisor._install_commit_scan_push_gate`),
# run as root inside the container so `privsep.separate_engine_identity()`
# reproduces the engine's own posture exactly (real+saved uid 0, effective
# the agent's) before the elevated install call raises back to genuine
# root for the write.
_SETUP_DRIVER = r'''
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from ralphd.engine import privsep
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, atomic_write_json

if os.getuid() != 0:
    sys.exit("this driver must start as real root to reproduce the engine")
agent = privsep.separate_engine_identity()
if agent is None or not privsep.separated():
    sys.exit("no uid boundary in this container: nothing to prove")

workspace = Path(sys.argv[1])
run_root = Path(sys.argv[2])


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True)


workspace.mkdir(parents=True, exist_ok=True)
# Task cure-01-01-2 (review finding R2, upheld cure-01-02): this driver is
# invoked TWICE against the SAME workspace path by
# test_an_engine_refresh_still_succeeds_after_the_first_install, to prove a
# second `_install_commit_scan_push_gate` call (an ordinary engine refresh,
# every worker iteration) still succeeds. `git init` on an existing repo is
# already a no-op, but the original seed step wrote the SAME content and
# then unconditionally ran `git commit`, which fails ("nothing to commit,
# working tree clean") the second time, before the driver ever reaches the
# install call it exists to exercise. Writing a fresh, time-varying seed
# value each call keeps every commit genuinely new -- and gives each call
# its own real head_sha, exactly the "fresh head_before" every worker
# iteration passes in production -- without weakening anything the test
# itself asserts.
if not (workspace / ".git").exists():
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
(workspace / "seed.txt").write_text(f"seed {time.time_ns()}\n")
_git(workspace, "add", "-A")
_git(workspace, "commit", "-q", "-m", "seed")
os.chown(workspace, agent.uid, agent.gid)
for root, dirs, files in os.walk(workspace):
    for name in dirs + files:
        os.chown(os.path.join(root, name), agent.uid, agent.gid)
head = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                      capture_output=True, text=True, check=True).stdout.strip()

run_root.mkdir(parents=True, exist_ok=True)
run = RunDir(root=run_root)
run.update_status(state="running")
atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
cfg = JobConfig(run_id="hooks-docker-c210")
sup = LoopSupervisor(cfg, run, workspace)
hook = sup._install_commit_scan_push_gate(head)
if hook is None:
    sys.exit("install_push_gate returned None -- not installed")
hooks_dir = hook.parent


def stat_of(p):
    st = os.lstat(p)
    return {"uid": st.st_uid, "gid": st.st_gid,
           "mode": oct(stat.S_IMODE(st.st_mode))}


with privsep.root_for_write():
    hooks_dir_stat = stat_of(hooks_dir)
    hook_stat = stat_of(hook)
    ancestors_stat = {}
    for anc in hooks_dir.parents:
        ancestors_stat[str(anc)] = stat_of(anc)

out = {
    "resuid": list(os.getresuid()),
    "separated": privsep.separated(),
    "workspace": str(workspace),
    "headSha": head,
    "hookPath": str(hook),
    "hooksDir": str(hooks_dir),
    "hooksDirStat": hooks_dir_stat,
    "hookStat": hook_stat,
    "ancestorsStat": ancestors_stat,
}
sys.stdout.write(json.dumps(out))
'''

# The probe a fully dropped agent child runs inside the job container: one
# attempt per syscall that could reach the installed hook file, the
# directory holding it, or any ancestor entry above it.
_HOOK_PROBE = r'''
import errno
import json
import os
import sys

hooks_dir = sys.argv[1].rstrip("/")
grandparent = sys.argv[2].rstrip("/")
hook_path = sys.argv[3]
scratch = sys.argv[4]

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


substitute = os.path.join(scratch, "substitute-hook")
with open(substitute, "w") as fh:
    fh.write("#!/bin/sh\nexit 0\n")
os.chmod(substitute, 0o755)
decoy_dir = os.path.join(scratch, "decoy-hooks-dir")
os.makedirs(decoy_dir, exist_ok=True)

# -- the installed hook file itself ---------------------------------------
attempt("hook:read", lambda: open(hook_path, "rb").read())
attempt("hook:open-write", lambda: open(hook_path, "w").write("pwned"))
attempt("hook:os.chmod", lambda: os.chmod(hook_path, 0o777))
attempt("hook:os.unlink", lambda: os.unlink(hook_path))
attempt("hook:os.replace", lambda: os.replace(substitute, hook_path))

# -- the hooks directory itself --------------------------------------------
attempt("dir:listdir", lambda: os.listdir(hooks_dir))
attempt("dir:os.mkdir-inside", lambda: os.mkdir(os.path.join(hooks_dir, "planted")))
attempt("dir:os.chmod", lambda: os.chmod(hooks_dir, 0o777))
attempt("dir:os.rename", lambda: os.rename(hooks_dir, hooks_dir + "-moved"))
attempt("dir:os.replace-decoy", lambda: os.replace(decoy_dir, hooks_dir))
attempt("dir:os.rmdir", lambda: os.rmdir(hooks_dir))

# -- the grandparent (`/var/lib/ralphd`) -----------------------------------
attempt("grandparent:os.mkdir-sibling",
        lambda: os.mkdir(os.path.join(grandparent, "planted-cure0102")))
attempt("grandparent:os.rename",
        lambda: os.rename(grandparent, grandparent + "-moved"))
attempt("grandparent:os.chmod", lambda: os.chmod(grandparent, 0o777))

# -- the remaining ancestors up to the container root, same as the
# run-secrets/task-record probes: `/var/lib`, `/var`, `/` are base-image
# ancestors no ralphd code creates or chowns, but a hole here would defeat
# this store exactly as surely as a hole at the mount point.
attempt("varlib:os.mkdir-inside", lambda: os.mkdir("/var/lib/planted-cure0102"))
attempt("varlib:os.chmod", lambda: os.chmod("/var/lib", 0o777))
attempt("var:os.mkdir-inside", lambda: os.mkdir("/var/planted-cure0102"))
attempt("var:os.chmod", lambda: os.chmod("/var", 0o777))
attempt("root:os.mkdir-inside", lambda: os.mkdir("/planted-cure0102-root"))
attempt("root:os.chmod", lambda: os.chmod("/", 0o777))

sys.stdout.write(json.dumps(out))
'''

# Nothing may escape classification: a later attempt added to the probe
# without a verdict in one of the two tuples above would otherwise be
# asserted about by nobody. Checked at IMPORT time, so it holds wherever
# this module is merely collected (this sandbox has no docker daemon) and
# not only where the probe itself can run.
_PROBE_OPS = tuple(re.findall(r'attempt\(\s*"([^"]+)"', _HOOK_PROBE))
assert len(set(_PROBE_OPS)) == len(_PROBE_OPS), (
    "duplicate probe attempt key", _PROBE_OPS)
assert set(_PROBE_OPS) == set(_MUST_BE_DENIED) | set(_MUST_STILL_SUCCEED), (
    "every probe attempt must be classified exactly once as a mutation that "
    "must be denied or a read that must still succeed",
    sorted(set(_PROBE_OPS) ^ (set(_MUST_BE_DENIED) | set(_MUST_STILL_SUCCEED))))
assert not set(_MUST_BE_DENIED) & set(_MUST_STILL_SUCCEED)

_HOOK_PROBE_DRIVER = r'''
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


def _run_setup(container: str, workspace: str, run_root: str) -> dict:
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c", _SETUP_DRIVER,
         workspace, run_root],
        capture_output=True, text=True, timeout=90)
    assert res.returncode == 0, res.stdout + res.stderr
    doc = json.loads(res.stdout)
    assert doc["separated"] is True and doc["resuid"] == [0, 1000, 0], doc
    return doc


def _run_hook_probe(container: str, hooks_dir: str, hook_path: str,
                    scratch: str) -> dict:
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c",
         "CHILD = " + repr(_HOOK_PROBE) + "\n" + _HOOK_PROBE_DRIVER,
         hooks_dir, _GRANDPARENT_IN_CONTAINER, hook_path, scratch],
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
    agent-owned scratch directory (no boundary claimed at all) must let the
    probe rewrite the "hook" file and rename the "directory" -- otherwise a
    pass on the real hooks dir proves nothing."""
    hole_root = "/tmp/cure0102-negative-control"
    hole_dir = f"{hole_root}/commit-scan-hooks"
    hole_hook = f"{hole_dir}/pre-push"
    setup = (
        f"rm -rf {hole_root} {hole_dir}-moved; mkdir -p {hole_dir}; "
        f"printf '#!/bin/sh\\nexit 0\\n' > {hole_hook}; "
        f"chmod 555 {hole_hook}; chown -R 1000:1000 {hole_root}")
    res = subprocess.run([REAL_DOCKER, "exec", container, "sh", "-c", setup],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr

    doc = _run_hook_probe(container, hole_dir, hole_hook,
                          "/tmp/cure0102-negative-scratch")
    attempts = doc["attempts"]
    for op in _MUST_STILL_SUCCEED:
        assert attempts[op]["ok"] is True, (
            f"the probe could not {op} an UNPROTECTED hook/directory -- then "
            f"its verdict on the real ones proves nothing: {attempts}")
    # Every mutation the real boundary must refuse has to SUCCEED here --
    # against an agent-owned copy of the same 0o555-hook-in-a-directory
    # layout -- or the probe is blind. Two exclusions, both principled:
    # the ancestor attempts (`grandparent:`, `varlib:`, `var:`, `root:`)
    # address real root-owned container paths in this same container, so
    # they are legitimately denied even here; and `hook:open-write` is
    # denied by the 0o555 MODE alone, owner or not, which is precisely why
    # the pre-fix layout was still broken -- `hook:os.chmod` (then write)
    # and `hook:os.replace` are the attempts that carried it, and they are
    # in this control.
    control_ops = [op for op in _MUST_BE_DENIED
                   if op.startswith(("hook:", "dir:"))
                   and op != "hook:open-write"]
    assert len(control_ops) == 8, control_ops
    for op in control_ops:
        assert attempts[op]["ok"] is True, (
            f"the probe could not {op} against an UNPROTECTED copy of the "
            "same layout (a 0o555 hook in an agent-owned directory) -- mode "
            "alone was never the property under test, the DIRECTORY's "
            f"permission is, and this probe cannot see the difference: {attempts}")
    subprocess.run([REAL_DOCKER, "exec", container, "sh", "-c",
                    f"rm -rf {hole_root} {hole_dir}-moved"],
                   capture_output=True, timeout=30)


def test_a_dropped_child_cannot_rewrite_the_installed_hook_or_its_directory(ctl):
    """Requirement I.1/DoD 9 (review finding R2): no MUTATION reaches the
    hook file, the directory holding it, the grandparent above it, or any
    ancestor up to the container root -- while reading the hook and listing
    its directory, which the gate's own agent-uid hook processes need, keep
    working. The probe's own negative control runs first, in the same
    container, so a pass here means something."""
    run_id, container = _start_idle_job(ctl, "cure0102-hookprobe")

    _assert_the_probe_can_actually_see_a_hole(container)

    setup = _run_setup(container, "/tmp/cure0102-ws", "/run/ralphd")
    assert setup["hooksDirStat"]["uid"] == 0, (
        "the installed hooks directory is not root-owned -- the boundary "
        f"is not actually active for this write: {setup}")
    assert setup["hooksDirStat"]["mode"] == "0o755", setup
    assert setup["hookStat"]["uid"] == 0, (
        "the installed pre-push hook is not root-owned even though its "
        f"directory is -- install_push_gate ran unelevated: {setup}")
    assert setup["hookStat"]["mode"] == "0o555", setup
    assert setup["hooksDir"] == _HOOKS_DIR_IN_CONTAINER, (
        "the hooks directory is not the protected, root-owned path this "
        f"task moved it to: {setup}")
    anc = setup["ancestorsStat"]
    for path, expected_mode in (("/var/lib/ralphd", "0o755"),
                                ("/var/lib", "0o755"),
                                ("/var", "0o755"), ("/", "0o755")):
        got = anc.get(path)
        assert got is not None, (path, anc)
        assert got["uid"] == 0, (
            f"ancestor {path} is not root-owned -- the ancestor chain "
            f"above the hooks directory breaks here: {got}")
        assert got["mode"] == expected_mode, (path, got)

    doc = _run_hook_probe(container, setup["hooksDir"], setup["hookPath"],
                          "/tmp/cure0102-probe-scratch")
    attempts = doc["attempts"]
    assert set(attempts) == set(_PROBE_OPS), (
        "the probe reported a different attempt set than it declares",
        sorted(set(attempts) ^ set(_PROBE_OPS)))
    for op in _MUST_BE_DENIED:
        got = attempts[op]
        assert got["ok"] is False and got["errno"] in _DENIED, (
            f"{op} was NOT refused at the agent uid -- the hook or its "
            f"containing path is still mutable from the workspace side: {got}")
    for op in _MUST_STILL_SUCCEED:
        got = attempts[op]
        assert got["ok"] is True, (
            f"{op} was refused -- the agent uid must keep being able to read "
            "and list the hooks it EXECUTES (git spawns them at that uid); a "
            f"boundary that denies this breaks the gate, not an attacker: {got}")

    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


def test_an_engine_refresh_still_succeeds_after_the_first_install(ctl):
    """Every worker iteration calls `_install_commit_scan_push_gate` again,
    with a fresh `head_before` -- the second (and every later) call must
    still succeed and land in the same root-owned place, never regress to
    "unwritable now that it's root-owned"."""
    run_id, container = _start_idle_job(ctl, "cure0102-refresh")

    first = _run_setup(container, "/tmp/cure0102-refresh-ws", "/run/ralphd")
    second = _run_setup(container, "/tmp/cure0102-refresh-ws", "/run/ralphd")

    assert second["hooksDir"] == first["hooksDir"]
    assert second["hookPath"] == first["hookPath"]
    assert second["hooksDirStat"] == first["hooksDirStat"]
    assert second["hookStat"] == first["hookStat"]

    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


def test_a_clean_agent_commit_and_push_still_succeed_through_the_protected_hooks(ctl):
    """The fix must not turn "protected" into "unusable": a clean agent-uid
    commit (through `commit-msg`/`pre-commit`) and a clean agent-uid push
    (through `pre-push`, to a local bare remote) both still succeed once
    the hooks live behind the uid boundary."""
    run_id, container = _start_idle_job(ctl, "cure0102-cleanflow")

    setup = _run_setup(container, "/tmp/cure0102-clean-ws", "/run/ralphd")
    workspace = setup["workspace"]

    driver = f'''
import subprocess
def git(*args):
    return subprocess.run(["git", *args], cwd={workspace!r},
                          capture_output=True, text=True)
subprocess.run(["git", "init", "-q", "--bare", "/tmp/cure0102-remote.git"],
               check=True)
open({workspace!r} + "/clean.txt", "w").write("nothing secret here\\n")
res_add = git("add", "clean.txt")
assert res_add.returncode == 0, res_add.stdout + res_add.stderr
res_commit = git("commit", "-q", "-m", "docs: a clean commit through the protected hooks")
assert res_commit.returncode == 0, (
    "a clean agent commit was refused through the protected local hooks: "
    + res_commit.stdout + res_commit.stderr)
res_remote = git("remote", "add", "origin", "/tmp/cure0102-remote.git")
assert res_remote.returncode == 0, res_remote.stdout + res_remote.stderr
res_push = git("push", "origin", "HEAD:refs/heads/main")
assert res_push.returncode == 0, (
    "a clean agent push was refused through the protected pre-push hook: "
    + res_push.stdout + res_push.stderr)
print("OK")
'''
    res = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", container,
         "python3", "-c", driver],
        capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK" in res.stdout, res.stdout + res.stderr

    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr
