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

import logging
import os
import pwd
from dataclasses import dataclass

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
    os.initgroups(agent.name, agent.gid)
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
