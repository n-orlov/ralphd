"""Sibling-container reaping, shared by `ralphctl` and the engine (v0.38 E).

A job that was granted the host docker socket starts SIBLING containers on the
host daemon. Each carries `ralphd.run=<run>` and `ralphd.role=sibling`; the job
container itself carries `ralphd.run=<run>` and `ralphd.role=job`. Two callers
reap them, with two different label filters on purpose:

* `ralphctl stop`/`rm` (host side) take the WHOLE run down, job container
  included, so they filter on the run label alone (`sibling_only=False`).
* The engine, when its run reaches a terminal state, runs INSIDE the job
  container: the same query would remove the container it is running in. It
  filters on BOTH labels (`sibling_only=True`).

Every function takes a `runner`: a callable `(docker_argv) -> result` where the
result has `returncode`, `stdout` and `stderr` (a `subprocess.CompletedProcess`
fits). The CLI passes its recording-friendly `sh([DOCKER, ...])`, the engine
passes `docker_runner()`, and a test passes a fake docker client, so nothing in
here talks to a daemon by itself.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

RUN_LABEL = "ralphd.run"
ROLE_LABEL = "ralphd.role"
SIBLING_ROLE = "sibling"

#: How long one docker call may take inside the engine before it counts as a
#: per-container error. The reap is best effort and must never hold a finished
#: run up for long.
DOCKER_CALL_TIMEOUT_S = 30.0

Runner = Callable[[list], Any]


def docker_runner(docker: str | None = None,
                  timeout: float = DOCKER_CALL_TIMEOUT_S) -> Runner:
    """The real runner: `<docker> <argv>` through subprocess, never raising.

    A missing binary answers rc 127 and a timeout rc 124, both with a stderr
    line, so a caller reads one shape of failure.
    """
    binary = docker or os.environ.get("RALPHD_DOCKER", "docker")

    def run(argv: list) -> subprocess.CompletedProcess:
        cmd = [binary, *argv]
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except FileNotFoundError:
            return subprocess.CompletedProcess(
                cmd, 127, stdout="", stderr=f"{binary}: command not found")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                cmd, 124, stdout="",
                stderr=f"{binary} {argv[0] if argv else ''}: timed out")
        except OSError as exc:
            return subprocess.CompletedProcess(cmd, 126, stdout="",
                                               stderr=str(exc))

    return run


def run_filter_args(run_id: str, *, sibling_only: bool) -> list[str]:
    """The `--filter` arguments selecting a run's containers."""
    args = ["--filter", f"label={RUN_LABEL}={run_id}"]
    if sibling_only:
        args += ["--filter", f"label={ROLE_LABEL}={SIBLING_ROLE}"]
    return args


def list_containers(run_id: str, runner: Runner, *, sibling_only: bool,
                    all_states: bool = True) -> list[str] | None:
    """Container ids labelled for this run; None when the listing failed.

    `all_states` includes stopped containers (`ps -a`); the live-sibling
    warning asks for running ones only.
    """
    res = runner(["ps", "-aq" if all_states else "-q",
                  *run_filter_args(run_id, sibling_only=sibling_only)])
    if res.returncode != 0:
        return None
    return str(res.stdout).split()


@dataclass
class ReapResult:
    """Outcome of one reap: how many containers were removed, and a
    `{container id: message}` entry for every one that was not."""
    count: int = 0
    errors: dict = field(default_factory=dict)
    listing_error: str | None = None

    def event_fields(self) -> dict:
        """The payload of the engine's `siblings.reaped` event."""
        out: dict = {"count": self.count, "errors": dict(self.errors)}
        if self.listing_error:
            out["error"] = self.listing_error
        return out


def reap_siblings(run_id: str, runner: Runner, *,
                  sibling_only: bool = False) -> ReapResult:
    """Best-effort removal of a run's containers; never raises.

    `sibling_only=False` (the host's `stop`/`rm`) filters on the run label
    alone; `sibling_only=True` (the engine) also requires
    `ralphd.role=sibling`, so the job container survives.
    """
    result = ReapResult()
    try:
        res = runner(["ps", "-aq",
                      *run_filter_args(run_id, sibling_only=sibling_only)])
    except Exception as exc:  # a fake or odd runner: still best effort
        result.listing_error = f"{type(exc).__name__}: {exc}"
        return result
    if res.returncode != 0:
        result.listing_error = (str(res.stderr).strip()
                                or f"docker ps exited {res.returncode}")
        return result
    for cid in str(res.stdout).split():
        try:
            rm = runner(["rm", "-f", cid])
        except Exception as exc:
            result.errors[cid] = f"{type(exc).__name__}: {exc}"
            continue
        if rm.returncode == 0:
            result.count += 1
        else:
            result.errors[cid] = (str(rm.stderr).strip()
                                  or f"docker rm exited {rm.returncode}")
    return result


def live_container_count(run_id: str, runner: Runner) -> int | None:
    """How many RUNNING containers carry `ralphd.run=<run_id>` (any role);
    None when docker could not answer."""
    try:
        ids = list_containers(run_id, runner, sibling_only=False,
                              all_states=False)
    except Exception:
        return None
    return None if ids is None else len(ids)


def engine_has_docker_access(environ=None) -> bool:
    """Does this job container have the host docker socket?

    `ralphctl start --allow-docker` exports the `RALPHD_HOST_*` coordinates
    only when it mounts the socket (the engine's own prompt guidance keys off
    the same variables), so their presence is the engine-side record of the
    grant; no docker call is needed to ask.
    """
    env = os.environ if environ is None else environ
    return any(env.get(k) for k in ("RALPHD_HOST_WORKSPACE",
                                    "RALPHD_HOST_WORKSPACES",
                                    "RALPHD_HOST_RUN_DIR"))
