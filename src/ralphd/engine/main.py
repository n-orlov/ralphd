"""Engine entrypoint: run the loop + API server in one process (PID 1)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import uvicorn

from .api import create_app
from .config import CONFIG_DIR, RUN_DIR, WORKSPACE_DIR, JobConfig
from .creds import place_creds
from .loop import LoopSupervisor
from .skills import place_skills
from .state import CURRENT_SCHEMA_VERSION, RunDir, RunDirLocked, SchemaVersionTooNew, utcnow

log = logging.getLogger("ralphd")

# Distinct, documented exit code for "another live engine already holds this
# run dir's lock" (PRD req 29b). Kept apart from the job-outcome codes used
# by amain()'s normal return (0 succeeded, 1 not-succeeded, 2 missing PRD).
EXIT_RUN_DIR_LOCKED = 3

# Distinct, documented exit code for "this run dir's recorded schemaVersion
# is newer than this engine build knows how to run against" (PRD req 18).
EXIT_SCHEMA_TOO_NEW = 4


def _version() -> str:
    try:
        return version("ralphd")
    except PackageNotFoundError:
        return "unknown"


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


async def amain() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
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

    workspace = Path(os.environ.get("RALPHD_WORKSPACE_DIR", str(WORKSPACE_DIR)))
    workspace.mkdir(parents=True, exist_ok=True)
    place_creds(CONFIG_DIR)
    place_skills(CONFIG_DIR)

    if not run.prd_file.exists():
        prd_src = CONFIG_DIR / "prd.md"
        if prd_src.exists():
            run.prd_file.write_text(prd_src.read_text())
        else:
            log.error("no PRD at %s or %s", run.prd_file, prd_src)
            return 2

    run.update_status(runId=cfg.run_id, state="starting", createdAt=utcnow(),
                      schemaVersion=CURRENT_SCHEMA_VERSION)
    loop = LoopSupervisor(cfg, run, workspace)
    app = create_app(cfg, run, loop)

    port = int(os.environ.get("RALPHD_PORT", "7777"))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port,
                                           log_level="warning"))
    api_task = asyncio.create_task(server.serve())
    log.info("API listening on :%d (auth=%s)", port, bool(cfg.api_token))

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(
            sig, lambda s=sig: (loop.abort(f"signal {s!s}"), stop.set()))

    job_task = asyncio.create_task(loop.run_job())
    final_state = await job_task
    log.info("job finished: %s", final_state)
    run.emit("state", state=final_state)

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
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
