"""Engine entrypoint: run the loop + API server in one process (PID 1)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .config import CONFIG_DIR, RUN_DIR, WORKSPACE_DIR, JobConfig
from .loop import LoopSupervisor
from .state import RunDir, utcnow

log = logging.getLogger("ralphd")


async def amain() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = JobConfig.load()
    run = RunDir(Path(os.environ.get("RALPHD_RUN_DIR", str(RUN_DIR))))
    workspace = Path(os.environ.get("RALPHD_WORKSPACE_DIR", str(WORKSPACE_DIR)))
    workspace.mkdir(parents=True, exist_ok=True)

    if not run.prd_file.exists():
        prd_src = CONFIG_DIR / "prd.md"
        if prd_src.exists():
            run.prd_file.write_text(prd_src.read_text())
        else:
            log.error("no PRD at %s or %s", run.prd_file, prd_src)
            return 2

    run.update_status(runId=cfg.run_id, state="starting", createdAt=utcnow())
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
    return 0 if final_state == "succeeded" else 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
