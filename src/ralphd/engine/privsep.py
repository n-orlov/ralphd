"""The uid boundary: an iteration cannot signal its own supervisor (#48).

`container/entrypoint.sh` ends in `exec ralphd-engine`, so the engine is the
container's PID 1 with the argv `ralphd-engine`. Until v0.7 the image also
ended in `USER agent` -- the *same* uid iterations run as -- so a
`pkill -f ralphd-engine` typed inside a task iteration was both a match and
permitted, and it ended a 9h36m run. Requirement I closes that structurally:
`A1`'s prompt rule asks an agent not to do it, and an instruction
demonstrably does not stop one.

## The route taken, and why

The engine now starts as **root** and immediately makes itself unsignalable by
the agent while keeping the agent's *file* identity:

    setresgid(0, agent_gid, 0)      # real+saved root, effective agent
    setresuid(0, agent_uid, 0)

and every child it spawns for an iteration is dropped **all the way** to the
agent uid (`user=`/`group=` on `Popen`, which `setreuid(uid, uid)`s the child
and therefore moves its saved uid too -- no half-dropped child that could
climb back).

Two kernel rules make that the whole mechanism:

* `kill(2)` permission is decided by the *real or saved* uid of the target:
  "the real or effective user ID of the sending process must equal the real
  user ID or saved set-user-ID of the target". The engine's real and saved
  uid are 0, so no process running as `agent` can signal it -- by pid, by
  process group, or by any `pkill`/`killall` pattern. `ptrace` follows the
  same rule.
* File *ownership* is decided by the effective uid. The engine's effective
  uid stays `agent`, so every file it creates under the run dir, the
  workspace and `$HOME` is owned by exactly the uid that owned it in v0.6.
  This is the reason the route is cheap: nothing about ownership,
  the `$HOME` config overlay, the credential files pi must read, or the git
  identity used for commits changes at all.

The alternative (engine as a *third* uid, e.g. 1001) forces the opposite
trade: either the engine can no longer spawn pi as `agent` -- an unprivileged
process cannot change uid -- or every file it writes into the host-mounted run
dir stops being owned by the host user. PID-namespace isolation per iteration
(SPEC 15) remains the general fix and is still deferred; it buys the ability to
tell "stop this iteration" from "the container is being torn down", which the
uid boundary does not, and it is not needed to stop the signal.

## Degrading instead of failing

Separation needs the engine to start as root, which is true of
`container/Dockerfile` and of nothing else: the test suite runs the engine as
an ordinary user, and an operator can always `docker run --user 1000`. Every
function here is therefore a no-op in that case, with one warning logged --
the engine runs exactly as it did in v0.6 (same uid as its agent, signalable)
rather than refusing to start. `separated()` is the one predicate that says
which of the two is in force, so no caller has to re-derive it from
`os.getresuid()`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pwd
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("ralphd.privsep")

# The name of the account iterations run as. Overridable because a *derived*
# job image (`ralphctl start --base`) inherits whatever uid 1000 is called in
# the operator's own base image; resolution falls back to uid 1000 by number,
# so an unnamed or differently-named account still works.
AGENT_USER_ENV = "RALPHD_AGENT_USER"
DEFAULT_AGENT_USER = "agent"
DEFAULT_AGENT_UID = 1000


@dataclass(frozen=True)
class AgentIdentity:
    """The identity iterations run as: name, uid, gid."""

    name: str
    uid: int
    gid: int


def agent_identity() -> AgentIdentity | None:
    """Resolve the account iterations run as, or None if there is none.

    By name first (`$RALPHD_AGENT_USER`, default `agent`), then by uid 1000,
    which is the uid every ralphd image owes the CLI whatever the account is
    called. Root is never a valid answer: it would make the boundary a no-op
    in the one direction that matters.
    """
    name = os.environ.get(AGENT_USER_ENV) or DEFAULT_AGENT_USER
    for lookup in (lambda: pwd.getpwnam(name), lambda: pwd.getpwuid(DEFAULT_AGENT_UID)):
        try:
            pw = lookup()
        except KeyError:
            continue
        if pw.pw_uid == 0:
            continue
        return AgentIdentity(pw.pw_name, pw.pw_uid, pw.pw_gid)
    return None


def separated() -> bool:
    """True when the engine holds a real uid the agent cannot signal.

    The shape `separate_engine_identity()` establishes: real and saved uid 0
    (so `kill(2)` from the agent uid is EPERM), effective uid the agent's (so
    file ownership is unchanged). Read from the live process credentials
    rather than from a flag set at startup, so it cannot claim a boundary the
    kernel is not actually enforcing.
    """
    agent = agent_identity()
    if agent is None:
        return False
    ruid, euid, suid = os.getresuid()
    return ruid == 0 and suid == 0 and euid == agent.uid


def separate_engine_identity() -> AgentIdentity | None:
    """Drop the engine's *effective* identity to the agent's, keeping root
    real and saved. Called once, before anything else in the engine runs.

    Returns the identity now in effect, or None when separation is not
    available (not started as root, or no agent account) -- in which case the
    engine keeps running with the pre-v0.7 arrangement and says so.
    """
    if os.getuid() != 0:
        log.warning("engine is not root (uid %d): no uid boundary -- an "
                    "iteration running as this uid can signal this process "
                    "(#48)", os.getuid())
        return None
    agent = agent_identity()
    if agent is None:
        log.warning("no agent account to drop to (%s / uid %d): no uid "
                    "boundary (#48)", os.environ.get(AGENT_USER_ENV)
                    or DEFAULT_AGENT_USER, DEFAULT_AGENT_UID)
        return None
    # Supplementary groups are taken now, as root, and inherited by every
    # child: the child's own drop runs unprivileged and cannot call
    # setgroups() itself, and leaving root's group set in place would hand
    # each iteration group 0.
    #
    # `initgroups()` alone is wrong here, because it *replaces* the list with
    # what /etc/group says about `agent.name` -- and the groups that matter
    # most are the ones /etc/group has never heard of. `--allow-docker` mounts
    # the host's docker socket and adds the host's docker gid to the container
    # with `--group-add`; that gid has no /etc/group entry inside the image, so
    # initgroups() discards it and every iteration gets "permission denied
    # while trying to connect to the docker API", which for a job whose whole
    # toolchain is sibling containers means it can build and test nothing at
    # all. An inherited supplementary group is an explicit operator grant made
    # at `docker run` time, so it is kept; group 0 is the one thing dropped,
    # because that is the privilege this boundary exists to remove.
    granted = {g for g in os.getgroups() if g != 0}
    os.initgroups(agent.name, agent.gid)
    keep = sorted(granted | {g for g in os.getgroups() if g != 0})
    if keep != sorted(os.getgroups()):
        os.setgroups(keep)
    if granted - {agent.gid}:
        log.info("keeping operator-granted supplementary group(s) %s across "
                 "the uid drop (e.g. --allow-docker's docker gid, which has no "
                 "/etc/group entry in the image)",
                 ",".join(str(g) for g in sorted(granted - {agent.gid})))
    # gid before uid: after the uid drop the gid can no longer be changed.
    os.setresgid(0, agent.gid, 0)
    os.setresuid(0, agent.uid, 0)
    log.info("uid boundary active: engine real/saved uid 0, effective uid %d "
             "(%s); iterations run as %d:%d and cannot signal this process",
             agent.uid, agent.name, agent.uid, agent.gid)
    return agent


def agent_child_kwargs() -> dict:
    """`Popen`/`create_subprocess_*` kwargs that run a child as the agent.

    Empty when there is no boundary, so an unprivileged engine spawns
    iterations exactly as it always did. Under the boundary this is a
    *complete* drop: CPython's `user=`/`group=` do `setreuid(uid, uid)` /
    `setregid(gid, gid)` in the forked child, which moves the saved ids too,
    so an iteration cannot regain the engine's real uid and cannot signal it.
    No privilege is needed for that (each target id equals the child's
    inherited effective id), so the engine never has to raise its own
    effective uid back to 0 -- there is no window in which it would create
    root-owned files in the run dir.
    """
    if not separated():
        return {}
    agent = agent_identity()
    assert agent is not None  # separated() is False without one
    return {"user": agent.uid, "group": agent.gid}


# --------------------------------------------------------------------------
# Task 003 (requirement A, #201): a purely container-internal,
# never-bind-mounted defense-in-depth copy of the control credential (and
# the optional API token).
#
# `cli.main.control_creds_dir` (the HOST side) fixes the actual leak --
# `.control-token`/`.api-token` are no longer written anywhere this module
# bind-mounts into a container at all, so a fully dropped agent child has
# no container-namespace path to either value. Nothing in THIS engine ever
# reads the copy below back (`engine.config.JobConfig.load()` reads the
# same `RALPHD_CONTROL_TOKEN`/`RALPHD_API_TOKEN` env vars directly, once,
# into memory), so this directory's only job is to give the boundary
# something concrete to fail against, on the layout `start` actually
# builds: a directory that is root-owned with a root-owned parent, entirely
# inside this container's own writable layer -- deliberately NOT a
# subdirectory of a host bind mount (`/run/ralphd`, `/config`, or the
# `run-secrets` mount `_bind_run_secrets_mount` builds), where chowning it
# root-owned from inside the container would ALSO chown the *host* file --
# there is no user-namespace remap here, so uid 0 in the container is real
# uid 0 on the host too -- locking the operator's own plain host user out
# of the path it separately needs to keep reading. That mount-overrides-
# intent trap is requirement C's own cautionary tale
# (`petitions.key_store_dir`'s docstring); this directory is never mounted
# at all, so it cannot fall into it.
CONTROL_CREDENTIALS_DIR = Path(os.environ.get(
    "RALPHD_CONTROL_CREDENTIALS_DIR", "/var/lib/ralphd/control"))


def provision_control_credentials() -> None:
    """Write `.control-token`/`.api-token` (whichever `RALPHD_*` env var is
    set) into `CONTROL_CREDENTIALS_DIR`, root-owned 0700 with a root-owned
    parent, entirely inside this container's own filesystem layer.

    Must be called before `separate_engine_identity()` -- while the process
    is still literally root (real, effective AND saved all 0), so the
    mkdir/chmod/write below need no privilege beyond what the process
    already holds and there is no window in which an unprivileged caller
    could race the write. A no-op when not running as root (the test
    suite's unprivileged engine, or `docker run --user 1000`): the
    documented degrade, not a second code path -- there is no boundary for
    this directory to be a defense IN DEPTH of, so skipping it changes
    nothing an unprivileged engine could not already see by reading its own
    (unelevated) `os.environ`.
    """
    if os.getuid() != 0:
        return
    CONTROL_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONTROL_CREDENTIALS_DIR, 0o700)
    for env_name, file_name in (("RALPHD_CONTROL_TOKEN", ".control-token"),
                                 ("RALPHD_API_TOKEN", ".api-token")):
        value = os.environ.get(env_name)
        path = CONTROL_CREDENTIALS_DIR / file_name
        if value:
            path.write_text(value)
            os.chmod(path, 0o600)
        elif path.exists():
            path.unlink()


# --------------------------------------------------------------------------
# Task 105 (#60 requirement D): the authoritative task record, root-owned.
#
# `agent_child_kwargs()` above is one-directional -- it drops a *child*
# process (`pi`) all the way to the agent uid so an iteration can never climb
# back to root. The task record needs the opposite move made safely: the
# engine itself (already living at effective uid = agent, per
# `separate_engine_identity()`) has to write one specific file as *root* so
# that no process running at the agent uid -- including the very iteration
# whose status the file records -- can open it for writing afterwards. The
# engine's real and saved uid are already 0 (that is what `separated()`
# means), so raising the effective uid back to 0 for the duration of one
# write and dropping it again needs no privilege it does not already hold;
# there is no window in which an *iteration* gains anything, because the
# raise and the drop happen entirely inside the engine's own process, never
# crossing a fork.

_write_root_owned_json_warned = False


@contextlib.contextmanager
def root_for_write() -> Iterator[bool]:
    """Momentarily raise the engine's effective uid back to root, then drop
    it back to the agent's, for exactly the duration of one write.

    Yields `True` when the raise (and the guaranteed restore in `finally`)
    actually happened, `False` when there is no boundary to raise into
    (`separated()` is `False` -- an unprivileged engine, or no agent account)
    -- callers use the yielded flag to decide whether to log the documented
    degrade, never to skip the write itself: the file still gets written
    either way, just not root-owned when there was never a boundary raising
    it above the agent's own uid in the first place.

    Named for its first caller, but the window is not write-only: v0.19
    requirement E.4 reads one file through it too
    (`petitions._read_stored_key`, the per-run MAC key this module's own
    `write_root_owned_json` shape made root-owned 0600 in the first place --
    a file the engine's EFFECTIVE uid cannot open either, which is the whole
    reason the read has to borrow the same raise).
    """
    if not separated():
        yield False
        return
    agent = agent_identity()
    assert agent is not None  # separated() is False without one
    os.setresuid(0, 0, 0)
    try:
        yield True
    finally:
        os.setresuid(0, agent.uid, 0)


def write_root_owned_json(path: Path, obj: Any) -> None:
    """Atomically write `obj` as JSON to `path`, root-owned when the uid
    boundary is in force -- this is the one write task 105 asks to be
    unreachable by the agent uid, so that hand-editing a live run's task
    record from inside a job container is no longer possible; `ralphctl
    steer` (a host-side channel) is the replacement for anything an operator
    used to do by hand-editing `tasks.json`.

    Degrades to an ordinary agent-owned write, with exactly one warning
    logged per process (never a second code path, never a hard failure), when
    there is no boundary to raise into (`privsep.separated()` is `False` --
    the test suite's own unprivileged engine, or `docker run --user 1000`
    with no root start). Self-contained (its own tmp-write + rename, not a
    call back into `engine.state.atomic_write_json`) so this module keeps no
    import edge onto `engine.state` -- the uid boundary is meant to be
    reasoned about on its own.
    """
    global _write_root_owned_json_warned
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(obj, indent=2) + "\n"
    with root_for_write() as elevated:
        if not elevated and not _write_root_owned_json_warned:
            log.warning(
                "no uid boundary: %s is being written owned by the agent's "
                "own uid, not root (#60) -- this is the documented degrade "
                "(privsep.separated() is False here), not a second code path",
                path)
            _write_root_owned_json_warned = True
        tmp.write_text(text)
        os.replace(tmp, path)
        os.chmod(path, 0o644)
