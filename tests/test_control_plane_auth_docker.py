"""Docker tier: task 004 (#62 candidate 1, requirement A) proved against a
*real* container, not only the ASGI-level `tests/test_control_plane_auth.py`.

Five things only a real container can show:

1. `ralphctl start` provisions `.control-token` unconditionally (no
   `--api-token` passed below) and every mutating command this file drives
   -- `pause`, `budget`, `unpause`, `abort` -- still works exactly as an
   operator's `ralphctl` always could, over the surface task 004 moved.
2. A raw HTTP client that presents no `X-Ralphd-Control-Token` header --
   exactly what a spawned iteration's own client would be, since
   `RALPHD_CONTROL_TOKEN` is stripped from its environment (task 003,
   extended) -- is refused on the real, running container's mutating
   routes, while `GET /status` still succeeds for it.
3. Task 003 (requirement A, #201): the token is read back, host-side, from
   `<registry>/control/<run-id>/.control-token` -- NOT from anywhere under
   the bind-mounted `<registry>/runs/<run-id>/` (`/run/ralphd` in the
   container) -- because that is now the only place `start`/`resume` ever
   write it.
4. Task 003 (requirement A.6, #201), the regression this module exists to
   pin: a process running at the agent's own uid *inside* the job's own
   container -- exactly the identity a dropped iteration runs as -- can
   find the control credential in none of its plausible leak routes:
   (a) the REAL running iteration process's own environ, read directly
   from `/proc/<pid>/environ` by pid (not via `docker exec`'s own
   synthesised environment, which always carries the container's
   *launch-time* `-e` values regardless of `--user` and would otherwise be
   a false positive with nothing to do with the boundary), (b) the run
   dir's own bind-mounted, agent-writable path (`/run/ralphd`, host uid
   1000 == container uid 1000), (c) the read-only config mount (`/config`),
   and (d) the in-container, root-owned defense-in-depth copy
   (`engine.privsep.provision_control_credentials`) -- neither readable
   NOR replaceable at the dropped agent uid, the SAME kernel permission
   check `tests/test_engine_uid_docker.py` proves for `tasks.json`. The
   SAME (d) check runs against `.api-token`'s own directory entry too,
   whenever it exists -- success criteria's "so does its attempt to
   replace the optional .api-token's directory entry" is not a separate
   mechanism from `.control-token`'s, so it is not a separate check either.
   Proved on BOTH the layout a fresh `start` builds and the layout a
   `resume` reproduces (a second, independent container, same run id).

   The (d) probe's *child* is the real thing, not an approximation, and
   this is the whole point of the check: a driver running as root inside
   the container calls `privsep.separate_engine_identity()` (the engine's
   own posture: real/saved uid 0, effective uid the agent's) and then
   spawns the probe through `privsep.agent_child_kwargs()` itself, so the
   attempts below are made by a genuinely forked, fully dropped child
   (`getresuid() == (1000, 1000, 1000)`) -- exactly what an iteration is.
   And the *replacement* attempts are real directory-entry replacements:
   `os.replace`, `os.rename`, `os.unlink`, `os.link` and `os.symlink`
   against the entry, plus a whole-directory `os.replace` of the control
   dir itself. An `open(path, "w")` truncate is kept as one more attempt
   among them, but it is deliberately NOT the evidence: truncation is
   refused by the FILE's mode alone and would keep passing even if the
   parent directory became agent-writable, which is precisely the hole a
   rename/unlink walks through. The probe also proves the rename attempts
   failed on permission and not on `EXDEV` by comparing `st_dev` of its
   own scratch dir against the control dir's. And the probe carries its own
   negative control, run in the same container immediately before the real
   check (`_assert_the_probe_can_actually_see_a_hole`): pointed at a
   deliberately agent-writable parent holding an agent-owned 0400
   `.control-token`, the SAME probe must report the entry as replaceable --
   otherwise its verdict on the real directory would prove nothing.

   Task 019 (STEER 002's generalised rule -- a protected path is only as
   protected as its least-protected ancestor): the (d) check does not stop
   at the control dir's own directory entry. Every ancestor above it, from
   the container root down -- `/var/lib/ralphd` (its parent), `/var/lib`,
   `/var`, `/` -- is measured (uid/mode, via `docker exec` as root, never a
   host-side `Path.stat()`) and attacked by the SAME dropped child
   (`os.mkdir`-inside, `os.chmod`, `os.rename`, with `/`'s own rename
   accepting the structural `EBUSY` a mount-namespace root returns instead
   of a permission errno) before the confirmation below is written down.
   These four are base-image ancestors no ralphd code creates or chowns --
   they were never task 003's own fix, and this is proof, not a new one.
5. With `--api-token` enabled, the SAME dropped-agent-uid probe (4) above
   applied to `.api-token` finds it in none of the same leak routes
   either -- a separate, minimal test (enabling `--api-token` makes
   `cfg.api_token` gate every route unconditionally, not just the
   mutating half `.control-token` gates, which would otherwise dilute the
   "unconditional" claim in (1) and the tokenless-401 claim in (2)).

Fixtures, cleanup discipline and the two environment-specific wrinkles
(bind-mount path translation, API reachability from inside this job's own
container) are exactly `tests/test_docker_sibling_e2e.py`'s; this module
imports them by name instead of re-deriving a second copy.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

import pytest

from test_docker_sibling_e2e import (  # noqa: F401  (fixtures used by name)
    REAL_DOCKER,
    Ctl,
    _unique_run_id,
    _wait_status,
    ctl,
    gateway_ip,
    pytestmark,
    test_image,
    work_root,
)


def _read_control_token(ctl_obj: Ctl, run_id: str) -> str:
    # Task 003 (requirement A, #201): the ONLY place `start`/`resume` ever
    # write the control token now -- host-only, never bind-mounted into any
    # container, a sibling of `runs/`/`configs/` in the registry.
    ccfile = ctl_obj.registry / "control" / run_id / ".control-token"
    assert ccfile.is_file(), (
        "ralphctl start did not provision a control token unconditionally "
        f"at its new host-only location ({ccfile})")
    token = ccfile.read_text().strip()
    assert token
    return token


# The probe a fully dropped agent child runs *inside* the job container.
# Spawned by `_PROBE_DRIVER` below through the engine's own
# `privsep.agent_child_kwargs()`, so `os.getresuid()` here is
# `(1000, 1000, 1000)`: real, effective AND saved, with no way back.
# Every attempt is recorded with its errno rather than asserted here, so
# the assertions live in the test (a probe that silently swallowed a
# success would otherwise read as a pass).
_CHILD_PROBE = r'''
import errno
import json
import os
import sys

names = json.loads(sys.argv[1])
control_dir = sys.argv[2]
# Task 004 (#202): an optional fourth arg overrides the scratch dir,
# needed the moment a caller's control_dir is itself bind-mounted (task
# 004's record directory, unlike this module's own never-bind-mounted
# `.control-token` dir) -- `/tmp` inside the container is a different
# device from a bind mount, so a rename/link/replace FROM there would fail
# EXDEV regardless of any permission bit, proving nothing about the
# boundary this probe exists to demonstrate. Every existing caller omits
# the arg and keeps the original same-device (both container-local) pair.
scratch = sys.argv[3] if len(sys.argv) > 3 else "/tmp/ralphd-a6-probe"
os.makedirs(scratch, exist_ok=True)

out = {
    "childResuid": list(os.getresuid()),
    "childResgid": list(os.getresgid()),
    "childEnvDump": "\n".join("%s=%s" % kv for kv in sorted(os.environ.items())),
    "attempts": {},
}
try:
    out["sameDevice"] = os.stat(scratch).st_dev == os.stat(control_dir).st_dev
except OSError as exc:
    out["sameDevice"] = "stat failed: %s" % (exc,)


def record(key, fn):
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


for name in names:
    path = os.path.join(control_dir, name)
    for op in ("replace", "rename", "link", "symlink"):
        src = os.path.join(scratch, "%s.%s" % (name.lstrip("."), op))
        with open(src, "w") as fh:
            fh.write("pwned-by-the-dropped-child\n")
    def src_for(name=name, op=None):
        return os.path.join(scratch, "%s.%s" % (name.lstrip("."), op))
    # the read half
    record("%s:read" % name, lambda p=path: open(p, "rb").read())
    record("%s:listdir-parent" % name, lambda: os.listdir(control_dir))
    # the in-place write half -- refused by the FILE's own mode, and NOT
    # the evidence this probe exists for (see the module docstring)
    record("%s:open-truncate" % name, lambda p=path: open(p, "w").close())
    # the directory-entry replacement half: every syscall that would swap,
    # remove or re-point the entry itself, each needing write permission on
    # the PARENT directory, none of which the file's own mode can grant
    record("%s:os.replace" % name,
           lambda p=path, s=src_for(op="replace"): os.replace(s, p))
    record("%s:os.rename" % name,
           lambda p=path, s=src_for(op="rename"): os.rename(s, p))
    record("%s:os.unlink" % name, lambda p=path: os.unlink(p))
    record("%s:os.link" % name,
           lambda p=path, s=src_for(op="link"): os.link(s, p))
    record("%s:os.symlink" % name,
           lambda p=path, s=src_for(op="symlink"): os.symlink(s, p))

# and the whole directory: swapping the control dir itself for one the
# child owns would replace every entry inside it at once
decoy = os.path.join(scratch, "decoy-control-dir")
os.makedirs(decoy, exist_ok=True)
record("<dir>:os.replace", lambda: os.replace(decoy, control_dir))
record("<dir>:os.rmdir", lambda: os.rmdir(control_dir))

# Task 019 (STEER 002's generalised rule -- "walk EVERY ancestor from the
# container root down ... run the attack yourself ... for each
# agent-reachable ancestor in turn"): the attempts above stop at the
# control dir's own directory entry inside its parent. Everything above
# that -- `/var/lib/ralphd` (the control dir's parent), `/var/lib`, `/var`
# and `/` itself -- is walked and attacked too, exactly the same way task
# 005 (requirement C) walked the run-secrets store's own ancestor chain.
# These are the container's base-image ancestors regardless of which
# control_dir was passed in (the negative control below points control_dir
# at a /tmp scratch dir, but these four paths are always the real ones),
# so an attack here proves the SAME boundary every other protected leaf in
# this run relies on, not a boundary specific to this probe's own target.
attempt = record  # same shape, named to read like task 005's own probe
attempt("ancestor:/var/lib/ralphd:os.mkdir-inside",
        lambda: os.mkdir("/var/lib/ralphd/planted-a201"))
attempt("ancestor:/var/lib/ralphd:os.chmod",
        lambda: os.chmod("/var/lib/ralphd", 0o777))
attempt("ancestor:/var/lib/ralphd:os.rename",
        lambda: os.rename("/var/lib/ralphd", "/var/lib/ralphd-moved"))
attempt("ancestor:/var/lib:os.mkdir-inside",
        lambda: os.mkdir("/var/lib/planted-a201"))
attempt("ancestor:/var/lib:os.chmod", lambda: os.chmod("/var/lib", 0o777))
attempt("ancestor:/var/lib:os.rename",
        lambda: os.rename("/var/lib", "/var/lib-moved"))
attempt("ancestor:/var:os.mkdir-inside",
        lambda: os.mkdir("/var/planted-a201"))
attempt("ancestor:/var:os.chmod", lambda: os.chmod("/var", 0o777))
attempt("ancestor:/var:os.rename", lambda: os.rename("/var", "/var-moved"))
attempt("ancestor:/:os.mkdir-inside",
        lambda: os.mkdir("/planted-a201-root"))
attempt("ancestor:/:os.chmod", lambda: os.chmod("/", 0o777))
# renaming `/` itself is a structural question, not a permission one --
# there is no parent dentry within this mount namespace to rename it IN,
# so the kernel refuses with EBUSY before it ever consults the agent
# uid's permissions. Attempted anyway (never skipped: an omitted attempt
# is exactly the "acknowledges an attack it does not run" shape STEER 002
# named), with EBUSY accepted alongside EACCES/EPERM in the assertion.
attempt("ancestor:/:os.rename", lambda: os.rename("/", "/-moved"))

sys.stdout.write(json.dumps(out))
'''

# The driver: root inside the container, reproducing the engine's own
# privilege posture and then spawning the probe above exactly the way
# `engine.runner.PiRunner.run` spawns an iteration -- through
# `privsep.agent_child_kwargs()`, with `CONTROL_PLANE_ENV_VARS` stripped
# from the child's environment by the production constant itself.
_PROBE_DRIVER = r'''
import json
import os
import subprocess
import sys

from ralphd.engine import privsep
from ralphd.engine.config import CONTROL_PLANE_ENV_VARS

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

env = {k: v for k, v in os.environ.items() if k not in CONTROL_PLANE_ENV_VARS}
res = subprocess.run([sys.executable, "-c", CHILD, *sys.argv[1:]],
                     capture_output=True, text=True, timeout=60, env=env,
                     **kwargs)
if res.returncode != 0:
    sys.exit("the dropped probe child failed (rc=%d): %s%s"
             % (res.returncode, res.stdout, res.stderr))
doc = json.loads(res.stdout)
doc["engineResuid"] = list(os.getresuid())
doc["agentChildKwargs"] = kwargs
doc["separated"] = privsep.separated()
sys.stdout.write(json.dumps(doc))
'''

_CONTROL_DIR_IN_CONTAINER = "/var/lib/ralphd/control"

_DENIED = ("EACCES", "EPERM")


def _run_dropped_child_probe(container: str, names: list[str],
                             control_dir: str = _CONTROL_DIR_IN_CONTAINER,
                             strict: bool = True,
                             scratch: str | None = None) -> dict:
    """Run `_CHILD_PROBE` inside `container` as a child spawned through
    `privsep.agent_child_kwargs()`, and return its findings.

    `strict` (the default) additionally asserts the boundary-shape
    invariants below; the negative control turns them off only for its own
    `sameDevice` check, which is about a different directory.

    `scratch`, when given, overrides the probe's own scratch dir (task 004,
    #202) -- needed only when `control_dir` is itself bind-mounted, so the
    directory-entry attempts stay same-device with their target and the
    refusal proves the permission boundary rather than an unrelated EXDEV.

    `docker exec` (no `--user`) enters as the container's default identity,
    real root -- which is what the driver needs to reproduce
    `separate_engine_identity()` and then drop a genuine child, rather than
    merely running a command AT uid 1000 the way `docker exec --user
    1000:1000` does. The uid the attempts below are made at is therefore
    established by the engine's own production code path, and is asserted
    from the child's own `getresuid()`.
    """
    driver = "CHILD = " + repr(_CHILD_PROBE) + "\n" + _PROBE_DRIVER
    argv = [json.dumps(names), control_dir]
    if scratch is not None:
        argv.append(scratch)
    res = subprocess.run(
        [REAL_DOCKER, "exec", container, "python3", "-c", driver, *argv],
        capture_output=True, text=True, timeout=90)
    assert res.returncode == 0, res.stdout + res.stderr
    doc = json.loads(res.stdout)
    # the child really was fully dropped: real, effective AND saved uid the
    # agent's, so it can neither climb back nor signal the engine
    assert doc["childResuid"] == [1000, 1000, 1000], doc
    assert doc["childResgid"][:2] == [1000, 1000], doc
    assert doc["separated"] is True, doc
    assert doc["agentChildKwargs"] == {"user": 1000, "group": 1000}, doc
    assert doc["engineResuid"] == [0, 1000, 0], doc  # the engine's own shape
    # ... and the rename attempts below were refused on PERMISSION, not on
    # EXDEV: the child's scratch dir and the control dir are one filesystem.
    if strict:
        assert doc["sameDevice"] is True, doc
    return doc


_NEGATIVE_CONTROL_DIR = "/tmp/a6-negative-control"


def _assert_the_probe_can_actually_see_a_hole(container: str) -> None:
    """The probe's own negative control, run in the same container, right
    before the real check: point `_CHILD_PROBE` at a directory that HAS the
    hole -- an agent-owned parent holding an agent-owned 0400
    `.control-token` -- and require it to report the entry as replaceable.

    This is what makes the real assertions below evidence rather than
    ceremony. A probe that only tried `open(path, "w")` would call this
    layout secure (truncation is refused by the file's own 0400 mode) while
    `os.replace`/`os.rename`/`os.unlink`/`os.link` all succeed through the
    writable parent -- so the truncate attempt alone cannot distinguish a
    root-owned 0700 parent from an agent-writable one, and every
    directory-entry attempt below can.
    """
    setup = (
        f"rm -rf {_NEGATIVE_CONTROL_DIR}; mkdir -p {_NEGATIVE_CONTROL_DIR}; "
        f"printf 'decoy\\n' > {_NEGATIVE_CONTROL_DIR}/.control-token; "
        f"chmod 400 {_NEGATIVE_CONTROL_DIR}/.control-token; "
        f"chmod 700 {_NEGATIVE_CONTROL_DIR}; "
        f"chown -R 1000:1000 {_NEGATIVE_CONTROL_DIR}")
    res = subprocess.run([REAL_DOCKER, "exec", container, "sh", "-c", setup],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr
    try:
        doc = _run_dropped_child_probe(
            container, [".control-token"],
            control_dir=_NEGATIVE_CONTROL_DIR, strict=False)
        attempts = doc["attempts"]
        # the weak attempt is refused here too -- by the FILE's mode alone
        weak = attempts[".control-token:open-truncate"]
        assert weak["ok"] is False and weak["errno"] in _DENIED, weak
        # ... while every directory-entry attempt goes straight through the
        # writable parent. If any of these were refused, the probe below
        # would be unable to tell a protected layout from this one.
        for op in ("os.replace", "os.rename", "os.unlink", "os.link"):
            got = attempts[f".control-token:{op}"]
            assert got["ok"] is True, (
                f"the probe's {op} attempt failed even against a deliberately "
                f"agent-writable parent ({got}) -- it cannot distinguish a "
                "protected control dir from an exposed one, so its verdict on "
                "the real one below would prove nothing")
    finally:
        subprocess.run(
            [REAL_DOCKER, "exec", container, "rm", "-rf",
             _NEGATIVE_CONTROL_DIR], capture_output=True, timeout=30)


def _assert_doctor_is_happy_with_the_new_layout(ctl_obj: Ctl, scratch,
                                                run_id: str) -> dict:
    """`ralphctl doctor` against the very registry this test's `start` just
    built with the new control-creds layout.

    Two runs, because one check in `doctor` is about the OPERATOR's host and
    nothing else: `pi_host_config` is `(~/.pi/agent/settings.json).exists()`
    (`cli/main.py`), which is false in the container this test suite itself
    runs inside (its HOME carries `models.json`/`auth.json` only) and has
    nothing to do with this task's change. So:

    * with the ambient HOME, every OTHER check must be true, no registry
      schema issue and no dangling entry may be reported -- i.e. the new
      `<registry>/control/<run-id>/` sibling breaks nothing `doctor` reads;
    * with a HOME that satisfies that one host precondition (an operator's
      does), the SAME doctor against the SAME registry must return
      `ok: true` and exit 0.
    """
    res = ctl_obj.run("doctor", timeout=180)
    doc = json.loads(res.stdout)
    for name, value in doc["checks"].items():
        if name == "pi_host_config":
            continue
        assert value is True, (name, doc["checks"])
    assert doc["registryIssues"] == [], doc["registryIssues"]
    assert run_id not in [e.get("runId") for e in doc["danglingRegistryEntries"]], \
        doc["danglingRegistryEntries"]
    # the run's own control-creds dir is real, and is a sibling of runs/ --
    # not something under it that a bind mount would carry into a container
    assert (ctl_obj.registry / "control" / run_id).is_dir()

    home = scratch / "doctor-home"
    (home / ".pi" / "agent").mkdir(parents=True, exist_ok=True)
    (home / ".pi" / "agent" / "settings.json").write_text("{}\n")
    res2 = ctl_obj.run("doctor", timeout=180,
                       env={**ctl_obj.env, "HOME": str(home)})
    doc2 = json.loads(res2.stdout)
    assert doc2["checks"]["pi_host_config"] is True, doc2["checks"]
    assert doc2["ok"] is True, doc2["checks"]
    assert res2.returncode == 0, res2.stderr
    return doc2


def _assert_dropped_child_cannot_reach_control_credential(
        container: str, control_token: str, api_token: str | None = None) -> None:
    """Requirement A.6: probe the running `container` exactly as a fully
    dropped agent-uid process inside it would see the world -- `docker exec
    --user 1000:1000` needs no docker-socket access from inside the
    container itself, it only reproduces the identity a spawned iteration
    already runs as (`agent_child_kwargs()`, `engine/privsep.py`).

    `api_token`, when given (`--api-token` was enabled at `start`), gets
    every check `control_token` gets: success criteria's "With --api-token
    enabled that same dropped child cannot read .api-token either, so the
    extra gate reopens no path" is not a separate claim about a separate
    mechanism, it is the SAME claim about a second value the SAME code path
    now protects."""
    tokens = {".control-token": control_token}
    if api_token is not None:
        tokens[".api-token"] = api_token

    def probe(*cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [REAL_DOCKER, "exec", "--user", "1000:1000", container, *cmd],
            capture_output=True, text=True, timeout=15)

    # (a) no readable process environ mentions either token value. Checked
    # against the REAL running iteration process specifically (`pgrep -f
    # /usr/bin/pi`, the stub's own path in this image) rather than a blanket
    # `/proc/[0-9]*/environ` scan: `docker exec` synthesises its OWN
    # process's environment from the container's *launch-time* `-e` values
    # regardless of `--user` (a Docker property, not a fork/exec from this
    # container's own process tree), so a blanket scan would always "find"
    # the token in the exec session's own environ -- a false positive with
    # nothing to do with the boundary. The iteration process, in contrast,
    # was genuinely forked/exec'd by the engine through
    # `PiRunner.run`/`agent_child_kwargs()`, so its environ is the real
    # thing (confirmed live: pid 13 in a manual repro had
    # `RALPHD_RUN_DIR`/`RALPHD_WORKSPACE_DIR`/etc. but none of the four
    # `CONTROL_PLANE_ENV_VARS`). The pattern is `[/]usr/bin/pi`, not
    # `/usr/bin/pi`, on purpose -- the plain string is a classic `pgrep -f`
    # self-match: it appears verbatim in the invoking `sh -c "...pgrep -f
    # '/usr/bin/pi'..."` script's OWN argv, so `pgrep` finds itself (and
    # inherits the exec session's launch-time env, the very false positive
    # this whole probe exists to avoid); the bracket expression matches the
    # real process's `/usr/bin/pi` identically while no longer appearing as
    # that literal substring in this script's own command line.
    script_a = (
        "for p in $(pgrep -f '[/]usr/bin/pi'); do "
        "tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null; done"
    )
    res = probe("sh", "-c", script_a)
    assert res.returncode == 0, res.stderr
    for name, value in tokens.items():
        assert value not in res.stdout, (
            f"an iteration process's own environ names the {name} value: "
            + repr(res.stdout))

    # (b) no file under the bind-mounted run dir (host-writable by the same
    # uid, /run/ralphd in the container) is even named like a token.
    script_b = "find /run/ralphd -iname '*token*' 2>/dev/null; true"
    res = probe("sh", "-c", script_b)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", (
        "a token-named file exists under the bind-mounted run dir: "
        + repr(res.stdout))

    # (c) the read-only config mount contains neither token value. Excludes
    # `/config/run-secrets` -- the SAME host directory `run_secrets_dir()`
    # bind-mounts a second time at `/var/lib/ralphd/run-secrets`, chowned
    # root-owned 0700 from inside the container by task 005's own
    # `protect_run_secrets_dir()` (requirement C) -- which surfaces here too
    # since it lives under `config_root()`. That boundary is proved by
    # `test_run_secrets_key_store_docker.py`, not this check; without the
    # `-prune`, `find /config` would legitimately hit `EACCES` descending
    # into it at the agent uid, making `find`'s own exit status nonzero for
    # a reason this check was never about -- caught live, pre-existing
    # (same red at HEAD `b48489cd`, before any change task 019 made).
    script_c = "cat /config/job.yaml /config/*.json /config/**/*.json 2>/dev/null; " \
               "find /config -path /config/run-secrets -prune -o " \
               "-type f -exec cat {} + 2>/dev/null"
    res = probe("sh", "-c", script_c)
    assert res.returncode == 0, res.stderr
    for name, value in tokens.items():
        assert value not in res.stdout, (
            f"the {name} value is readable somewhere under /config: "
            + repr(res.stdout))

    # (d) the in-container, root-owned defense-in-depth copy
    # (`engine.privsep.provision_control_credentials`,
    # `/var/lib/ralphd/control`, never bind-mounted) is neither readable NOR
    # replaceable by a child the engine's own `agent_child_kwargs()` spawns
    # -- the read half AND the real directory-entry replacement half of the
    # boundary (`os.replace`/`os.rename`/`os.unlink`/`os.link`/`os.symlink`,
    # plus a whole-directory swap), every one against a real kernel
    # permission check, exactly `test_engine_uid_docker.py`'s `tasks.json`
    # proof for the SAME uid -- for `.control-token` AND (when enabled) the
    # optional `.api-token` beside it.
    _assert_the_probe_can_actually_see_a_hole(container)
    doc = _run_dropped_child_probe(container, list(tokens))
    attempts = doc["attempts"]

    # the child's own environment, built by the production
    # `CONTROL_PLANE_ENV_VARS` strip the engine applies to every iteration,
    # carries neither value either
    for name, value in tokens.items():
        assert value not in doc["childEnvDump"], (
            f"the dropped child's own environment names the {name} value")

    for name in tokens:
        # the read half
        for op in ("read", "listdir-parent"):
            got = attempts[f"{name}:{op}"]
            assert got["ok"] is False and got["errno"] in _DENIED, (
                f"the dropped child could {op} the in-container {name} "
                f"copy: {got}")
        # the directory-entry replacement half: the entry itself cannot be
        # swapped, removed or re-pointed. `open-truncate` is asserted too,
        # but as the weakest of the six -- the file's own mode refuses it,
        # so on its own it would still pass with an agent-writable parent.
        for op in ("open-truncate", "os.replace", "os.rename", "os.unlink",
                   "os.link", "os.symlink"):
            got = attempts[f"{name}:{op}"]
            assert got["ok"] is False and got["errno"] in _DENIED, (
                f"the dropped child's {op} against the in-container {name} "
                f"directory entry SUCCEEDED (or failed for an unrelated "
                f"reason): {got}")

    # and the control directory itself cannot be swapped out or removed
    for op in ("os.replace", "os.rmdir"):
        got = attempts[f"<dir>:{op}"]
        assert got["ok"] is False and got["errno"] in _DENIED, (
            f"the dropped child's {op} against the control directory itself "
            f"SUCCEEDED (or failed for an unrelated reason): {got}")

    # Task 019 (STEER 002): confirm the rest of the ancestor chain -- above
    # the control dir's own directory entry, up to the container root --
    # is measured and attacked too, not merely reasoned about. `docker exec`
    # (default identity, real root) reads uid/mode directly; a host-side
    # `Path.stat()` is never used (it would raise `PermissionError` walking
    # through a 0700 ancestor on some other protected leaf's layout, and
    # this repo's standing rule is to never rely on it for this class of
    # check regardless).
    for anc_path, expected_mode in (("/var/lib/ralphd", "755"),
                                    ("/var/lib", "755"),
                                    ("/var", "755"), ("/", "755")):
        stat = subprocess.run(
            [REAL_DOCKER, "exec", container, "stat", "-c", "%u:%g:%a",
             anc_path], capture_output=True, text=True, timeout=15)
        assert stat.returncode == 0, stat.stderr
        uid, gid, mode = stat.stdout.strip().split(":")
        assert uid == "0", (
            f"ancestor {anc_path} is not root-owned ({stat.stdout.strip()}) "
            "-- the ancestor chain above the control credential breaks here")
        assert mode == expected_mode, (anc_path, stat.stdout.strip())

    # ... and every attack against those same ancestors, run by the SAME
    # dropped child that just failed against the control dir's own entry,
    # is denied too (EBUSY accepted alongside EACCES/EPERM only for `/`
    # itself, a structural refusal -- there is no parent dentry within this
    # mount namespace to rename `/` IN -- never a silently broadened set).
    for anc_key in ("/var/lib/ralphd", "/var/lib", "/var", "/"):
        for op in ("os.mkdir-inside", "os.chmod", "os.rename"):
            got = attempts[f"ancestor:{anc_key}:{op}"]
            allowed = _DENIED if not (anc_key == "/" and op == "os.rename") \
                else _DENIED + ("EBUSY",)
            assert got["ok"] is False and got["errno"] in allowed, (
                f"the dropped child's {op} against ancestor {anc_key} "
                f"SUCCEEDED (or failed for an unrelated reason): {got}")


def _assert_root_owned_credential_copy_matches_and_survived(
        container: str, control_token: str, api_token: str | None = None) -> None:
    """Root-side sanity for the (d) probe above: the in-container copy this
    task provisions is not a stray, unrelated artifact (it holds the SAME
    token(s) `ralphctl` uses) and the dropped agent uid's failed write
    attempt(s) truly changed nothing on disk -- checked as root (uid 0, the
    container's default identity per `container/Dockerfile`'s deliberate
    absence of a trailing `USER agent`, task 020/requirement I), the only
    identity able to read it at all."""
    tokens = {".control-token": control_token}
    if api_token is not None:
        tokens[".api-token"] = api_token
    for name, expected in tokens.items():
        stat = subprocess.run(
            [REAL_DOCKER, "exec", container, "stat", "-c", "%u:%g:%a",
             f"/var/lib/ralphd/control/{name}"],
            capture_output=True, text=True, timeout=15)
        assert stat.returncode == 0, stat.stderr
        # still root-owned and still 0600: the dropped child's unlink/
        # rename/link attempts left the entry exactly as provisioned
        assert stat.stdout.strip() == "0:0:600", (
            f"the in-container {name} entry is now {stat.stdout.strip()!r} "
            "-- one of the dropped child's attempts changed it")
        res = subprocess.run(
            [REAL_DOCKER, "exec", container, "cat",
             f"/var/lib/ralphd/control/{name}"],
            capture_output=True, text=True, timeout=15)
        assert res.returncode == 0, res.stderr
        assert res.stdout == expected, (
            f"the in-container {name} copy does not match the real "
            f"token (got {res.stdout!r}, want {expected!r}) -- either "
            "provisioning is wrong or the dropped-uid write attempt "
            "silently succeeded")


def test_mutating_ralphctl_commands_still_work_and_the_raw_control_gate_holds(ctl, work_root):
    run_id = _unique_run_id("e004-ctlauth")
    # task 009 (B.3): STUB_HOLD_FILE (a container-side path under
    # /workspace, mounted from work_root/ws below) blocks the job
    # indefinitely -- deterministically wide enough to pause/budget/unpause
    # a running job -- instead of racing a fixed STUB_SLEEP window against
    # how fast the container reaches a terminal state on its own. Released
    # (touched) right before the final wait below, same pattern as
    # test_docker_sibling_e2e.py's test_resume_continues_after_container_stop.
    hold_rel = ".hold-ctlauth"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1",
                                       "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                                       "STUB_HOLD_TIMEOUT_S": "60"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    api_url = meta["apiUrl"]
    container = f"ralphd-{run_id}"

    control_token = _read_control_token(ctl, run_id)

    _wait_status(api_url, lambda s: True, timeout=30)

    # -- A.6: the credential is unreachable to a dropped agent child, on
    # the layout a fresh `start` actually built ----------------------------
    _assert_dropped_child_cannot_reach_control_credential(container, control_token)
    _assert_root_owned_credential_copy_matches_and_survived(container, control_token)

    # -- `ralphctl doctor` still passes against this very layout -----------
    _assert_doctor_is_happy_with_the_new_layout(ctl, work_root, run_id)

    # -- 1. the raw boundary, against the real running container ----------
    # A client presenting no X-Ralphd-Control-Token header at all -- exactly
    # what a spawned iteration's own HTTP client would be able to build
    # (task 003 strips RALPHD_CONTROL_TOKEN from its environment) -- is
    # refused on a mutating route.
    req = urllib.request.Request(api_url + "/pause", method="POST", data=b"")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 401, exc.value.read()

    # The observation half is untouched by the same tokenless client.
    req = urllib.request.Request(api_url + "/status")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200

    # The correct header passes the same route.
    req = urllib.request.Request(api_url + "/pause", method="POST", data=b"")
    req.add_header("X-Ralphd-Control-Token", control_token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200

    # -- 2. the operator's own front door: ralphctl still does everything --
    # These four commands are the A.4 evidence: `ralphctl` (host-side,
    # reading the host-only control-creds dir this task moved the token
    # into) keeps authenticating to every mutating route exactly as before.
    res = ctl.run("budget", run_id, "+3", timeout=30)
    assert res.returncode == 0, res.stderr

    res = ctl.run("unpause", run_id, timeout=30)
    assert res.returncode == 0, res.stderr

    hold_file_host.touch()  # release: let the held phase proceed and finish

    final = _wait_status(api_url, lambda s: s.get("state") in
                         ("succeeded", "failed", "aborted"), timeout=60)
    assert final["verdict"] == "verified", final

    # task 004 (#202): planning above wrote the authoritative task record
    # into the root-owned, bind-mounted record directory -- `--on-complete idle` otherwise leaves the
    # container (and that directory) up past this test's own assertions,
    # which the `ctl` fixture's hard `docker rm -f` teardown cannot remove
    # afterward. A graceful `ralphctl stop` (the same `/shutdown` path
    # SIGTERM drives) releases it first.
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


def test_dropped_agent_child_cannot_read_the_optional_api_token_either(ctl, work_root):
    """Success criteria's own second clause: "With --api-token enabled that
    same dropped child cannot read .api-token either, so the extra gate
    reopens no path." A separate, minimal test rather than folding
    `--api-token` into the test above: enabling it makes `cfg.api_token`
    gate EVERY route (`engine/api.py`'s `auth` middleware, unconditionally,
    not just the mutating half `control_token` gates), so the tokenless-
    401/authenticated-200 raw-HTTP probe above would need an `Authorization`
    header added to distinguish the two independent gates -- kept separate
    here so neither test's own claim ("unconditional" above, "the extra
    gate reopens no path" here) is diluted by the other's setup."""
    run_id = _unique_run_id("e004-apitoken")
    hold_rel = ".hold-apitoken"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "1",
                                       "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                                       "STUB_HOLD_TIMEOUT_S": "60"},
                    extra=("--on-complete", "idle", "--api-token", "auto"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    control_token = _read_control_token(ctl, run_id)
    api_token_file = ctl.registry / "control" / run_id / ".api-token"
    assert api_token_file.is_file(), (
        "--api-token auto did not provision .api-token at its new "
        f"host-only location ({api_token_file})")
    api_token = api_token_file.read_text().strip()
    assert api_token
    assert api_token != control_token

    _wait_status(meta["apiUrl"], lambda s: True, timeout=30,
                 headers={"Authorization": f"Bearer {api_token}"})

    _assert_dropped_child_cannot_reach_control_credential(
        container, control_token, api_token=api_token)
    _assert_root_owned_credential_copy_matches_and_survived(
        container, control_token, api_token=api_token)

    # The operator's own front door still works with both gates enabled:
    # `ralphctl` reads BOTH tokens back from the same host-only directory
    # and presents both headers automatically.
    res = ctl.run("budget", run_id, "+3", timeout=30)
    assert res.returncode == 0, res.stderr
    res = ctl.run("unpause", run_id, timeout=30)
    assert res.returncode == 0, res.stderr

    hold_file_host.touch()

    final = _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                         ("succeeded", "failed", "aborted"), timeout=60,
                         headers={"Authorization": f"Bearer {api_token}"})
    assert final["verdict"] == "verified", final

    # same litter hygiene as the test above -- see its own comment.
    stop_res = ctl.stop(run_id, force=True)
    assert stop_res.returncode == 0, stop_res.stderr


def test_dropped_agent_child_cannot_reach_the_control_credential_after_resume(ctl):
    """Requirement A.6, resumed layout: the SAME three-route probe as the
    fresh-start test above, against a second, independent container that
    `ralphctl resume` brings up for the same run id. Also confirms `resume`
    reproduces `RALPHD_CONTROL_TOKEN` into the new container's own engine
    process without ever re-exposing it anywhere the run dir's bind mount
    (or the config mount) makes visible to an agent-uid process."""
    run_id = _unique_run_id("e004-ctlauth-resume")
    hold_rel = ".hold-ctlauth-resume"
    res = ctl.start(run_id, extra_env={"STUB_TASKS": "2",
                                       "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                                       "STUB_HOLD_TIMEOUT_S": "60"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    container = f"ralphd-{run_id}"

    _wait_status(meta["apiUrl"], lambda s: s.get("state") == "running", timeout=30)

    res = subprocess.run([REAL_DOCKER, "stop", "-t", "5", container],
                        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr

    # Deliberately NOT releasing the hold before resume this time (unlike
    # test_docker_sibling_e2e.py's own resume test): `_read_extra_env_wiring`
    # reproduces the SAME STUB_HOLD_FILE/STUB_HOLD_TIMEOUT_S pair into the
    # resumed container, so it re-blocks -- which is exactly the wide,
    # deterministic "still running" window the pause/abort probe below
    # needs, with no race against how fast the stub finishes on its own.
    res = ctl.resume(run_id, extra=("--iterations", "+10"))
    assert res.returncode == 0, res.stderr
    meta2 = json.loads(res.stdout)
    assert meta2["container"] != meta["container"]  # a genuinely fresh container

    control_token = _read_control_token(ctl, run_id)
    _wait_status(meta2["apiUrl"], lambda s: s.get("state") == "running", timeout=30)

    _assert_dropped_child_cannot_reach_control_credential(container, control_token)
    _assert_root_owned_credential_copy_matches_and_survived(container, control_token)

    # The resumed container's own mutating gate still authenticates too:
    # `pause` via the raw header, then `ralphctl abort` (the fourth mutating
    # command named in this test's own commit message) while genuinely
    # paused -- proving `abort` itself still authenticates, distinctly from
    # `pause`/`unpause`/`budget` already proven by the fresh-start test
    # above.
    req = urllib.request.Request(meta2["apiUrl"] + "/pause", method="POST", data=b"")
    req.add_header("X-Ralphd-Control-Token", control_token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200

    res = ctl.run("abort", run_id, timeout=30)
    assert res.returncode == 0, res.stderr

    final = _wait_status(meta2["apiUrl"], lambda s: s.get("state") == "aborted",
                         timeout=30)
    assert final["state"] == "aborted", final
