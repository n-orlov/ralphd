"""Engine entrypoint: run the loop + API server in one process (PID 1)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from .. import __version__
from .api import create_app
from .config import CONFIG_DIR, RUN_DIR, WORKSPACE_DIR, JobConfig
from .creds import place_creds
from .loop import LoopSupervisor
from .privsep import agent_child_kwargs, separate_engine_identity
from .redact import refresh_redaction_map
from .skills import ambient_home, place_skills, verify_placement_and_report
from .state import CURRENT_SCHEMA_VERSION, RunDir, RunDirLocked, SchemaVersionTooNew, utcnow

log = logging.getLogger("ralphd")

# One log configuration for the whole process, called both from main() -- so
# the uid-boundary line privsep logs before anything else happens is formatted
# and levelled like every other engine line -- and from amain(), which is
# still where an in-process caller (tests, an embedder) enters. basicConfig is
# a no-op once handlers exist, so the second call changes nothing.
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

# Distinct, documented exit code for "another live engine already holds this
# run dir's lock" (PRD req 29b). Kept apart from the job-outcome codes used
# by amain()'s normal return (0 succeeded, 1 not-succeeded, 2 missing PRD).
EXIT_RUN_DIR_LOCKED = 3

# Distinct, documented exit code for "this run dir's recorded schemaVersion
# is newer than this engine build knows how to run against" (PRD req 18).
EXIT_SCHEMA_TOO_NEW = 4


def _version() -> str:
    """The version of the code that is running, not of the metadata beside it.

    This used to read `importlib.metadata.version("ralphd")`, which is the
    version recorded when the distribution was *installed*: in an editable
    checkout whose version literal has moved on (or a run from a source tree
    with no dist-info at all) it disagreed with the very same number
    `GET /version` reports from `ralphd.__version__`, and could answer
    "unknown". One source of truth -- the package literal -- so
    `ralphd-engine --version`, `ralphctl --version` and `GET /version`
    cannot tell three stories (tests/test_packaging_metadata.py).
    """
    return __version__


def build_arg_parser() -> argparse.ArgumentParser:
    """Real argument parsing so --help/--version are self-contained and safe.

    ralphd-engine takes no positional arguments in normal operation (it is
    entirely configured via RALPHD_* environment variables and the mounted
    /config directory), but --help/--version must work with zero side
    effects: no directories created, no server started, no port bound.
    """
    parser = argparse.ArgumentParser(
        prog="ralphd-engine",
        description=(
            "ralphd engine: runs one job's iteration loop and HTTP API "
            "inside the container. Configured entirely via RALPHD_* "
            "environment variables and the mounted /config directory."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"ralphd-engine {_version()}"
    )
    return parser


def _seed_iteration_budget(cfg: JobConfig, run: RunDir) -> None:
    """Task 010 (#51): the effective iteration budget at startup is
    max(status.json's recorded `iterationsBudget`, job.yaml's `iterations`),
    never job.yaml's alone.

    Without this, an operator top-up (`PATCH /config/budget` /
    `ralphctl budget`) is silently undone the moment the container restarts:
    the engine re-reads the read-only-mounted job.yaml from scratch and
    forgets the live change status.json already recorded. A status.json
    figure below job.yaml's (the ordinary case: no top-up ever happened, or
    an operator raised job.yaml itself for a resume) must NOT win -- job.yaml
    is allowed to raise the floor, just never to lower a ceiling the run
    already earned.

    Called once, before the loop starts, so `cfg.iterations` (and thus
    `budget_left()`, `GET /config`, `GET /status`) sees the winner from the
    very first iteration -- and exactly once, so the log/event pair below
    cannot fire twice for one process.
    """
    status_budget = run.read_status().get("iterationsBudget")
    job_budget = cfg.iterations
    if isinstance(status_budget, int) and status_budget > job_budget:
        cfg.iterations = status_budget
        source = "status.json"
    else:
        source = "job.yaml"
    log.info("iteration budget seeded from %s: status.json=%s job.yaml=%s "
             "-> effective=%s", source, status_budget, job_budget, cfg.iterations)
    run.emit("budget_seeded", statusJsonIterations=status_budget,
             jobYamlIterations=job_budget, effectiveIterations=cfg.iterations,
             source=source)


async def _run_on_complete_cmd(cfg: JobConfig, run: RunDir, final_state: str) -> None:
    """Run the operator-supplied `on_complete_cmd` shell hook exactly once,
    after the job has reached a terminal state (PRD req 26). Receives
    RALPHD_RUN_ID / RALPHD_STATE / RALPHD_VERDICT env vars alongside the
    process's own environment. Never raises: any failure (nonzero exit,
    command not found, etc.) is logged as an event and otherwise ignored --
    it must never affect the job's verdict or this engine's own exit code."""
    if not cfg.on_complete_cmd:
        return
    verdict = run.read_status().get("verdict")
    env = dict(os.environ)
    env["RALPHD_RUN_ID"] = cfg.run_id
    env["RALPHD_STATE"] = final_state
    env["RALPHD_VERDICT"] = "" if verdict is None else str(verdict)
    try:
        proc = await asyncio.create_subprocess_shell(
            cfg.on_complete_cmd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # Task 020 (#48): the hook is agent-supplied shell, so it runs
            # under the same uid boundary as an iteration -- inheriting the
            # engine's root real uid would hand it back the ability to
            # signal the engine (and to become root).
            **agent_child_kwargs())
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            log.info("on_complete_cmd finished (rc=0)")
            run.emit("log", message="on_complete_cmd finished (rc=0)")
        else:
            tail = (stderr or stdout or b"").decode(errors="replace")[-500:]
            log.error("on_complete_cmd exited %d: %s", proc.returncode, tail)
            run.emit("log", level="error",
                     message=f"on_complete_cmd exited {proc.returncode}: {tail}")
    except Exception as exc:  # e.g. shell/exec failure -- never propagate
        log.error("on_complete_cmd failed to run: %r", exc)
        run.emit("log", level="error",
                 message=f"on_complete_cmd failed to run: {exc!r}")


async def amain() -> int:
    _configure_logging()
    cfg = JobConfig.load()
    run = RunDir(Path(os.environ.get("RALPHD_RUN_DIR", str(RUN_DIR))))
    try:
        lock_fh = run.acquire_lock()
    except RunDirLocked as exc:
        print(f"ralphd-engine: {exc}", file=sys.stderr)
        log.error("%s", exc)
        return EXIT_RUN_DIR_LOCKED
    try:
        run.check_schema_version()
    except SchemaVersionTooNew as exc:
        print(f"ralphd-engine: {exc}", file=sys.stderr)
        log.error("%s", exc)
        lock_fh.close()
        return EXIT_SCHEMA_TOO_NEW

    _seed_iteration_budget(cfg, run)
    # Task 020 (requirement F bullet 4): fold a pre-0.9 run dir's
    # `vigilant-verified.json` sidecar into the task record before anything
    # else touches tasks.json. A no-op for any run dir that never had
    # vigilant mode on; the sidecar is only deleted once the fold is known
    # to have preserved what it named (task 005/#75) -- otherwise it is left
    # in place rather than destroyed alongside a lost verification.
    run.migrate_legacy_vigilant_verified()

    workspace = Path(os.environ.get("RALPHD_WORKSPACE_DIR", str(WORKSPACE_DIR)))
    workspace.mkdir(parents=True, exist_ok=True)
    place_creds(CONFIG_DIR)
    _skills_home = ambient_home()
    place_skills(CONFIG_DIR, home=_skills_home)
    verify_placement_and_report(run, CONFIG_DIR, _skills_home)
    # Build the in-memory-only secret-redaction set (task 060) now that
    # creds are placed -- covers both process/LLM env and placed cred file
    # values from the very first iteration onward.
    refresh_redaction_map()

    if not run.prd_file.exists():
        prd_src = CONFIG_DIR / "prd.md"
        if prd_src.exists():
            run.prd_file.write_text(prd_src.read_text())
        else:
            log.error("no PRD at %s or %s", run.prd_file, prd_src)
            return 2

    # Task 017 (requirement... reconstruct FSM position from disk on
    # resume): every process start used to write `state="starting"`
    # unconditionally, including over a run dir a prior, crashed episode
    # had already left `finishing` (settled outcome, reflect unwind not
    # yet promoted -- `_finish()`/`run_job()`). That clobbered the one
    # field `run_job()`'s resume check reads BEFORE `LoopSupervisor` is
    # even constructed, so `_promote_recorded_finishing_state` -- code
    # written specifically for this restart -- could never actually run
    # through the real engine binary; only the in-process unit tests that
    # construct `LoopSupervisor` directly and skip `amain()` ever saw it
    # fire. `finishing` is the one prior state worth preserving here:
    # every other state (`running`, a stale terminal state after a budget
    # bump, or none at all for a fresh run dir) is unconditionally
    # overwritten again within moments by `_run_job_core`'s own first
    # statement (`state="running"`) or by `run_job()` itself, so nothing
    # else needs special handling at this one call site.
    _preserved_state = run.read_status().get("state")
    run.update_status(runId=cfg.run_id,
                      state=(_preserved_state if _preserved_state == "finishing"
                             else "starting"),
                      createdAt=utcnow(),
                      schemaVersion=CURRENT_SCHEMA_VERSION,
                      # Task 012 (#5): health/infraWait are part of the status
                      # contract from the very first write, so a consumer never
                      # has to treat their absence as a third case. `state`
                      # deliberately does NOT grow a "degraded" value -- that
                      # would break every consumer's terminal-state logic.
                      health="ok", infraWait=None,
                      # Task 006 (#16): the approach *denominator*, written
                      # with the very first status write rather than only when
                      # the loop reaches `running`, so no surface has to
                      # assemble `approach n/m` from status.json plus a second
                      # `GET /config` call -- and a job that dies before the
                      # loop ever starts still carries its budget.
                      # LoopSupervisor re-asserts it on the move to `running`;
                      # both writes read the same cfg field.
                      maxApproaches=cfg.max_approaches)
    loop = LoopSupervisor(cfg, run, workspace)
    app = create_app(cfg, run, loop)

    port = int(os.environ.get("RALPHD_PORT", "7777"))
    # RALPHD_BIND is set by `ralphctl start --network host`: with the host's
    # network namespace there is no docker port-publish boundary, so binding
    # 0.0.0.0 would expose the API on every host interface -- bind only the
    # address the operator asked for instead.
    bind = os.environ.get("RALPHD_BIND", "0.0.0.0")
    server = uvicorn.Server(uvicorn.Config(app, host=bind, port=port,
                                           log_level="warning"))
    api_task = asyncio.create_task(server.serve())
    log.info("API listening on %s:%d (auth=%s, controlAuth=%s)", bind, port,
              bool(cfg.api_token), bool(cfg.control_token))

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Task 015 (#46): `abort_on_signal`, not `abort` -- a signal nobody
        # claimed through the API is recorded as a *self-inflicted*
        # termination (with the last tool call before it as evidence), so a run
        # shot from inside its own container is still eligible for auto-resume
        # while an operator's abort remains one auto-resume never resurrects.
        asyncio.get_running_loop().add_signal_handler(
            sig, lambda s=sig: (loop.abort_on_signal(s), stop.set()))

    job_task = asyncio.create_task(loop.run_job())
    final_state = await job_task
    log.info("job finished: %s", final_state)
    run.emit("state", state=final_state)

    await _run_on_complete_cmd(cfg, run, final_state)

    if cfg.on_complete == "idle" and not stop.is_set():
        log.info("idling (on_complete=idle); POST /shutdown or stop the container")
        await stop.wait()

    server.should_exit = True
    await api_task
    lock_fh.close()
    return 0 if final_state == "succeeded" else 1


def main() -> None:
    # parse_args() calls sys.exit(0) itself for -h/--help/--version, before
    # amain() (and thus any dir creation / server / lock / config load) runs.
    build_arg_parser().parse_args()
    _configure_logging()
    # Task 020 (#48): establish the uid boundary before anything else runs --
    # before the first file is created, so no run-dir file is ever owned by
    # root, and before the API is listening, so there is no instant in which
    # the engine is signalable by the uid its iterations use. A no-op with one
    # warning when the engine was not started as root (the test suite, or
    # `docker run --user 1000`): see engine/privsep.py.
    separate_engine_identity()
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
