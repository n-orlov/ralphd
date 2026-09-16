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
import shutil
import stat
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

    Task 019 (STEER 002's generalised rule) confirmed the rest of that
    chain too, not merely the immediate parent: every ancestor of
    `CONTROL_CREDENTIALS_DIR` from the container root down --
    `/var/lib/ralphd`, `/var/lib`, `/var`, `/` -- measures root:root mode
    0755 live (via `docker exec` as root,
    `tests/test_control_plane_auth_docker.py`) and is unreachable by a
    dropped agent-uid child's `os.mkdir`/`os.chmod`/`os.rename` attempts
    against any of them. Every ancestor lies outside every agent-writable
    path, so the rename-a-directory-entry attack that rejected task 004's
    first attempt has no foothold here.

    Three of those four (`/var/lib`, `/var`, `/`) are base-image
    directories no ralphd code creates or chowns. `/var/lib/ralphd` is not:
    it exists in neither `python:3.12-slim-bookworm` nor the job image
    built from it, and is created at runtime, root-owned, by one of two
    creators -- the docker daemon materialising the `record`/`run-secrets`
    bind-mount points beneath it (0755, on the layout `ralphctl start`
    actually builds, before this function ever runs), or the
    `mkdir(parents=True)` below when neither mount is present. In that
    second case its mode is whatever `0o777 & ~umask` gives: 0755 at the
    umask the engine actually inherits in the job container (measured
    0022), but 0777 under `umask 000` -- a world-writable ancestor of this
    0700 directory. That residual is measured and pinned by the same docker
    module; closing it means setting the created parent's mode explicitly
    here (a product change task 019 proposes rather than makes), not
    reading this chain as protected by the image.

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


# --------------------------------------------------------------------------
# Task 004 (requirement B, #202): the authoritative task record, unreplaceable
# at the agent uid -- not merely root-owned (`write_root_owned_json` above
# already made the FILE that), but living in a directory the agent cannot
# write into either. `write_root_owned_json`'s own reproduction is exactly
# this gap: the file is root-owned 0644, but its directory
# (`run.root`, bind-mounted agent-owned) is not, so a dropped child cannot
# open the file for writing but CAN `os.replace()` a whole substituted
# document onto its path -- a directory-entry operation, gated by the
# PARENT's permissions, never the target's own.
#
# Unlike `CONTROL_CREDENTIALS_DIR` above (deliberately never bind-mounted,
# because a credential need not survive a container's death), the task
# record MUST survive a `resume`'s fresh container -- so it cannot live
# purely in this container's own writable layer either.
#
# ## Why a subdirectory of the run dir is NOT enough (this task's first
# ## attempt, and its rejection)
#
# Attempt 1 put the record in `PROTECTED_TASKS_DIRNAME` INSIDE the
# bind-mounted run dir, root-owned 0700, re-asserted on every write. That
# still failed B.1's "including every ancestor entry on the path, not only
# the file": the run root itself is agent-owned (it has to be -- the agent
# writes notes.md, artifacts and the whole iteration record into it), so a
# fully dropped child could `os.rename('.tasks-protected', ...)`, drop its
# OWN `.tasks-protected/tasks.json` in its place, and the engine's next
# `read_tasks()` returned that substituted document. Re-asserting
# protection on every WRITE cannot close that: a read between two writes
# resolves the name the attacker now owns. And no permission bit can
# separate the engine from the agent inside the run dir either -- the
# engine's EFFECTIVE uid *is* the agent's (that is what
# `separate_engine_identity` establishes), so any directory the agent
# cannot write to is one the engine cannot write to unelevated.
#
# ## Where the record lives instead
#
# `TASK_RECORD_DIR` -- a directory `ralphctl start`/`resume` bind-mount
# from the HOST (`cli.main.task_record_dir()`, under the run's own config
# dir, so it survives a `resume`'s fresh container and `ralphctl rm`
# removes it with the rest of the run's state) at a container path whose
# every ancestor is root-owned and agent-unwritable in the container's own
# namespace: `/var/lib/ralphd/record`, parented by the same root-owned
# `/var/lib/ralphd` that already parents `CONTROL_CREDENTIALS_DIR` and the
# run-secrets mount. The engine chowns/chmods the mount point itself
# root-owned 0700 (it is genuinely root here -- no user-namespace remap, so
# a `chown` from inside the container changes the HOST inode's ownership
# too, the same fact that made `tasks.json` root-owned before this task).
#
# So there is no directory entry on the authoritative path that a dropped
# child may modify: not the file, not its directory, and not one ancestor
# above it. It is also not aliased by any agent-writable path -- the host
# directory behind the mount lives under `config_root()`, which is
# bind-mounted READ-ONLY at `/config`, never under the run dir.
#
# The run-dir location stays in the code for exactly two jobs: the
# documented degrade (no mount at all -- a job started by a `ralphctl` that
# predates this task, or the test suite's own unprivileged engine), and the
# one-shot `migrate_task_record()` that lifts a pre-existing run's record
# out of it on the first start/resume under this build.
PROTECTED_TASKS_DIRNAME = ".tasks-protected"
TASK_RECORD_FILENAME = "tasks.json"
TASK_RECORD_DIR_ENV = "RALPHD_TASK_RECORD_DIR"
DEFAULT_TASK_RECORD_DIR = Path("/var/lib/ralphd/record")


def task_record_dir() -> Path | None:
    """The mounted, ancestor-protected directory holding the authoritative
    task record, or None when this container has none.

    Never created here, and that is deliberate: the directory exists only
    because `ralphctl start`/`resume` bind-mounted a host directory onto
    it, so "it exists" and "the record survives this container's death"
    are the same fact. Creating it ourselves would silently produce a
    record that lives in the container's own writable layer and vanishes
    at the next `resume` -- worse than the honest degrade below.
    Correspondingly the agent cannot conjure it either: `/var/lib/ralphd`
    is root-owned 0755, so no path under it can be created at the agent
    uid.

    `$RALPHD_TASK_RECORD_DIR` overrides the container path (the docker-tier
    tests' own seam, and an operator's escape hatch), exactly as
    `$RALPHD_RUN_SECRETS_DIR` does for the MAC key store.
    """
    override = os.environ.get(TASK_RECORD_DIR_ENV)
    candidate = Path(override) if override else DEFAULT_TASK_RECORD_DIR
    try:
        # `is_dir()` needs only `x` on the parent, which is 0755 root-owned
        # -- it answers truthfully even once this directory is 0700 root.
        return candidate if candidate.is_dir() else None
    except OSError:  # pragma: no cover - unreadable ancestor
        return None


def protected_tasks_dir(run_root: Path | str) -> Path:
    """THE directory the authoritative task record lives in: the mounted,
    ancestor-protected `task_record_dir()` when there is one, else the
    pre-mount run-dir location (the documented degrade -- see the module
    comment above). Every reader and writer of the record resolves it
    through this one function, so the two can never disagree."""
    d = task_record_dir()
    return d if d is not None else legacy_protected_tasks_dir(run_root)


def legacy_protected_tasks_dir(run_root: Path | str) -> Path:
    """Where this task's own first attempt put the record: inside the
    bind-mounted, agent-owned run dir. Read by `migrate_task_record()`,
    and still the write target when no mount exists at all."""
    return Path(run_root) / PROTECTED_TASKS_DIRNAME


def migrate_task_record(run) -> None:
    """Lift an existing run's authoritative record into
    `task_record_dir()`, once, at engine startup -- BEFORE the API is
    listening and before any iteration has been spawned, so no agent-uid
    process is alive while the two locations both exist.

    Three run dirs reach this: one whose record is still in the run-dir
    `PROTECTED_TASKS_DIRNAME` (started under this task's first attempt),
    one whose record is still the plain agent-owned `tasks.json` at the run
    root (started before this task at all -- task 105's layout), and a
    fresh one with no record yet (the overwhelmingly common case, a no-op).
    The migrated-from copy is REMOVED rather than left beside the
    authoritative one, so nothing can later read a stale record from a
    location the agent controls; the run-root `tasks.json` is not removed
    but rewritten as the agent-readable view copy every write refreshes.

    Because this runs exactly once per process start and every later read
    goes straight to `task_record_dir()`, the read path itself needs no
    fallback onto an agent-writable location -- the window in which a
    planted `.tasks-protected/tasks.json` could be believed is closed by
    construction rather than by ordering luck.
    """
    record_dir = task_record_dir()
    if record_dir is None:
        return                      # degrade: nothing to migrate into
    target = record_dir / TASK_RECORD_FILENAME
    legacy_dir = legacy_protected_tasks_dir(run.root)
    sources = (legacy_dir / TASK_RECORD_FILENAME,
               Path(run.root) / TASK_RECORD_FILENAME)
    doc: Any = None
    with root_for_write():
        if target.exists():
            doc = None              # already migrated; only sweep below
        else:
            for src in sources:
                try:
                    parsed = json.loads(src.read_text())
                except (OSError, ValueError):
                    continue
                if isinstance(parsed, dict) and parsed.get("tasks") is not None:
                    doc = parsed
                    break
    if doc is not None:
        # Outside the borrow above on purpose: `write_protected_tasks_json`
        # takes its own (`root_for_write` is not re-entrant -- the inner
        # exit would restore the effective uid to the agent's and leave the
        # outer body unelevated).
        write_protected_tasks_json(target, Path(run.root) / TASK_RECORD_FILENAME,
                                    doc)
        log.info("migrated the authoritative task record into %s", target)
    with root_for_write():
        if target.exists() and legacy_dir.exists():
            try:
                shutil.rmtree(legacy_dir)
            except OSError as exc:  # pragma: no cover - best effort
                log.warning("could not remove the superseded record dir %s: %r",
                            legacy_dir, exc)


def _reject_planted_entry(path: Path) -> None:
    """If something already sits at `path` and it is not a genuine
    directory -- a file precreated at this name, or (never followed,
    `lstat` does not dereference) a symlink planted there before the
    boundary could protect it -- discard it rather than trust or open
    through it. Never `Path.mkdir(exist_ok=True)`, whose own existence
    check follows a symlink's target and would silently accept one."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _ensure_protected_tasks_dir(path: Path, *, elevated: bool) -> None:
    """Create `path` if missing, discarding anything planted at that name
    that is not a real directory, then (when `elevated`) pin it root-owned
    0700. Called at the top of every authoritative write -- see the module
    comment above for why this has to be unconditional, not merely
    creation-time.
    """
    _reject_planted_entry(path)
    path.mkdir(parents=True, exist_ok=True)
    if elevated:
        os.chown(path, 0, 0)
        os.chmod(path, 0o700)


def write_protected_tasks_json(protected_path: Path, view_path: Path,
                                obj: Any) -> None:
    """Write `obj` as JSON to `protected_path` -- the authoritative task
    record -- inside `_ensure_protected_tasks_dir`'s self-healing
    directory, root-owned 0600 under the uid boundary; then refresh the
    agent-readable VIEW copy at `view_path` (task 004, requirement B.3),
    root-owned 0644 exactly as `write_root_owned_json` left it before this
    task, for every reader that is not the engine itself:
    `ralphctl tasks`, the hub's task table, the worker/steer prompts, the
    steer hat's `context-read` tool. The engine's own reads
    (`RunDir.read_tasks_result`) never go through `view_path` -- only
    through `protected_path`, elevated the same way this write is.

    Both temp files are created fresh every call and both are
    `_reject_planted_entry`-checked -- B.2's "a precreated path or a
    symlink at the temp name cannot redirect the write": once the
    directory itself denies the agent write access at all, no name inside
    it -- predictable or not -- is a surface the agent can act on, and the
    one window that predates that protection (the very first write ever
    made into a directory an attacker got to before the engine did) is
    closed by `_ensure_protected_tasks_dir` discarding anything not a
    genuine directory before trusting it.
    Degrades to an ordinary, unprotected pair of writes (still two files,
    still atomic) when there is no boundary to raise into
    (`privsep.separated()` is False) -- the test suite's own unprivileged
    engine, exactly like `write_root_owned_json`'s documented degrade.
    """
    global _write_root_owned_json_warned
    text = json.dumps(obj, indent=2) + "\n"
    protected_dir = protected_path.parent
    tmp = protected_path.with_suffix(protected_path.suffix + ".tmp")
    with root_for_write() as elevated:
        if not elevated and not _write_root_owned_json_warned:
            log.warning(
                "no uid boundary: %s is being written owned by the agent's "
                "own uid, not root (#202) -- this is the documented "
                "degrade (privsep.separated() is False here), not a second "
                "code path", protected_path)
            _write_root_owned_json_warned = True
        _ensure_protected_tasks_dir(protected_dir, elevated=elevated)
        _reject_planted_entry(tmp)
        tmp.write_text(text)
        os.replace(tmp, protected_path)
        os.chmod(protected_path, 0o600 if elevated else 0o644)
        # The agent-readable view, refreshed INSIDE the same elevated
        # window so it keeps `write_root_owned_json`'s pre-task-004
        # ownership exactly: root-owned 0644, world-readable and
        # agent-UNWRITABLE. Read-only for the agent is all a view has to
        # be, and keeping that bit costs nothing while preserving two
        # things: task 013's invariant that the steer hat's own uid cannot
        # open `/run/ralphd/tasks.json` for writing at all
        # (`tests/test_steer.py::test_the_hat_uid_cannot_write_tasks_json_
        # and_the_engine_owns_it`), and the weaker but real property that
        # what `ralphctl tasks`/the hub render is the engine's own bytes
        # rather than something an iteration hand-edited. (Only the
        # authoritative record is protected against a directory-entry
        # REPLACEMENT -- the view lives in the agent-owned run root, whose
        # parent permissions no file mode can override; that is precisely
        # why the engine's own reads never touch it.)
        # Best-effort: a viewer that misses one refresh sees
        # `read_tasks_doc()`'s own stale-read fallback on its next poll,
        # never a hard failure of the authoritative write above.
        try:
            view_tmp = view_path.with_suffix(view_path.suffix + ".viewtmp")
            if view_tmp.is_dir() and not view_tmp.is_symlink():
                shutil.rmtree(view_tmp)   # not a name the agent may squat
            else:
                _reject_planted_entry(view_tmp)
            view_tmp.write_text(text)
            os.replace(view_tmp, view_path)
            os.chmod(view_path, 0o644)
        except OSError:
            log.warning("could not refresh the agent-readable view copy "
                        "at %s", view_path)


def release_protected_tasks_dir(run) -> None:
    """Hand the authoritative record's directory (`protected_tasks_dir`,
    i.e. the mounted `task_record_dir()` or -- degraded -- the run-dir
    location) back to the agent's own uid, called once from
    `engine.main.amain()`'s single shutdown funnel, after the job has
    already reached a terminal (or idle-then-signalled) outcome.

    Protection matters while the container is alive -- an iteration might
    still run and attack the record. Once the process is exiting there is
    no further write or read this protection could still be defending,
    so releasing it here is what lets `ralphctl rm`/the hub's `DELETE
    /api/runs/<id>` (both host-side, no root available there) delete a
    finished run's directories exactly as they could before this task --
    the root-owned directory this module created would otherwise be the one
    thing plain `shutil.rmtree(..., ignore_errors=True)` cannot remove. A
    `resume`'s fresh container re-protects it before anything reads or
    writes tasks.json again (every write does, see
    `write_protected_tasks_json`). A no-op when there is no boundary to
    release, or nothing was ever protected.
    """
    if not separated():
        return
    agent = agent_identity()
    if agent is None:
        return
    protected_dir = protected_tasks_dir(run.root)
    if not protected_dir.is_dir():
        return
    with root_for_write():
        try:
            for child in protected_dir.iterdir():
                os.chown(child, agent.uid, agent.gid)
                os.chmod(child, 0o644)
            os.chown(protected_dir, agent.uid, agent.gid)
            os.chmod(protected_dir, 0o755)
        except OSError as exc:  # pragma: no cover - best effort at exit
            log.warning("could not release %s back to the agent uid: %r",
                        protected_dir, exc)


# --------------------------------------------------------------------------
# Task 005 (requirement C, #202's third instance): the petition MAC key
# store's mount point.
#
# `cli.main._bind_run_secrets_mount` binds a HOST directory
# (`cli.main.run_secrets_dir(run_id)`, `mkdir`'d by whatever uid ran
# `ralphctl start`/`resume` -- the agent's own uid on the standard
# uid-1000-both-sides configuration) straight onto `petitions.
# DEFAULT_RUN_SECRETS_DIR` (`/var/lib/ralphd/run-secrets`) inside the
# container. There is no user-namespace remap here, so the directory
# ENTRY presented at that container path is the HOST directory's own
# inode, owned by that uid -- agent-writable, listable and replaceable,
# exactly the hole `_bind_task_record_mount`'s sibling closed for the task
# record, and exactly what PRD requirement C measured live: `docstring`
# claimed the store was "created root-owned 0700 ... because its PARENT
# is root-owned too", which is true of `/var/lib/ralphd` (root-owned 0755
# in the image, and by `provision_control_credentials()`'s own
# `mkdir(parents=True)` before this ever runs) but false of the mount
# point itself, which chmod alone (what `petitions._persist_key` did
# before this task) cannot fix: chmod never changes an owner, and the
# agent remains the OWNER of its own mount point regardless of mode bits.
#
# `protect_run_secrets_dir()` below chowns the mount point root-owned at
# container startup -- before any iteration can run, closing the window
# between container start and the first key being minted -- and
# `petitions._persist_key` does the identical chown on every write, for
# the same self-healing reason `_ensure_protected_tasks_dir` re-asserts
# ownership on every authoritative tasks.json write rather than only at
# creation time.
def protect_run_secrets_dir() -> None:
    """Chown/chmod the petition MAC key store's mount point root-owned,
    called once from `engine.main.amain()` before anything else runs --
    before the API is listening and long before any iteration exists to
    race this call.

    A no-op when there is no boundary to raise into, when an operator's
    own `$RALPHD_RUN_SECRETS_DIR` override is in play (their own choice of
    ownership -- `petitions.key_store_dir()`'s own docstring, never this
    function's to override), or when there is no mount at all (a
    container started by a pre-task-005 build, or the unprivileged test
    suite's own engine -- the documented degrade).
    """
    from ralphd.engine import petitions
    if not separated():
        return
    if os.environ.get(petitions.RUN_SECRETS_DIR_ENV):
        return
    store = petitions.DEFAULT_RUN_SECRETS_DIR
    with root_for_write():
        if not store.is_dir():
            return
        try:
            os.chown(store, 0, 0)
            os.chmod(store, 0o700)
        except OSError as exc:  # pragma: no cover - best effort
            log.warning("could not protect the run-secrets mount at %s: %r",
                        store, exc)


def release_run_secrets_dir() -> None:
    """The run-secrets mount's own release-at-shutdown, symmetric with
    `release_protected_tasks_dir` above and for the identical reason:
    `ralphctl rm`/the hub's `DELETE /api/runs/<id>` (both host-side,
    unprivileged) must still be able to remove `cli.main.run_secrets_dir
    (run_id)` -- this mount's own host source, under `config_root(run_id)`
    -- after the run ends. A resumed container's own `protect_run_secrets_
    dir()` call (and every later `_persist_key` write) re-protects it on
    its very next start, exactly like the task record's own
    re-protect-on-first-write.

    A no-op with no boundary, an operator override in play (never touched
    by this module either way), or nothing mounted at all.
    """
    from ralphd.engine import petitions
    if not separated():
        return
    if os.environ.get(petitions.RUN_SECRETS_DIR_ENV):
        return
    agent = agent_identity()
    if agent is None:
        return
    store = petitions.DEFAULT_RUN_SECRETS_DIR
    with root_for_write():
        if not store.is_dir():
            return
        try:
            for child in store.iterdir():
                os.chown(child, agent.uid, agent.gid)
                os.chmod(child, 0o600)
            os.chown(store, agent.uid, agent.gid)
            os.chmod(store, 0o755)
        except OSError as exc:  # pragma: no cover - best effort at exit
            log.warning("could not release %s back to the agent uid: %r",
                        store, exc)
