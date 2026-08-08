#!/usr/bin/env python3
"""Supervisor used by the STUB_DOCKER_LIVE_ENGINE knob (see ./docker).

Launches a real `ralphd-engine` process wired to the given run/config/
workspace dirs and port (simulating what the container entrypoint would set
up), then watches the run dir's status.json. The instant the job reaches a
terminal state, it SIGKILLs the engine immediately — deterministically
reproducing "the API dies right at job completion" so tests of
`ralphctl start --no-detach` don't depend on a real timing race.

Runs detached (its own session) so the `docker run` stub that spawns it can
return immediately, the way `docker run -d` does.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

TERMINAL = ("succeeded", "failed", "aborted")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--config-dir", required=True)
    p.add_argument("--port", required=True)
    p.add_argument("--workspace-dir")
    p.add_argument("--kill-grace", type=float, default=0.2,
                    help="seconds to wait after the terminal state event is "
                         "written before SIGKILLing the engine, to let an "
                         "already-open SSE reader see it (simulates the "
                         "client's follow-up request racing container exit)")
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    workspace_dir = args.workspace_dir or str(run_dir / ".workspace")
    Path(workspace_dir).mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "RALPHD_RUN_DIR": str(run_dir),
        "RALPHD_CONFIG_DIR": args.config_dir,
        "RALPHD_WORKSPACE_DIR": workspace_dir,
        "RALPHD_PORT": args.port,
        # tests/stub-pi/pi (spawned by the engine, inheriting this process's
        # env) needs this to know which run dir to mutate.
        "STUB_RUN_DIR": str(run_dir),
    }
    proc = subprocess.Popen(["ralphd-engine"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)

    events_file = run_dir / "events.jsonl"
    killed = False
    deadline = time.monotonic() + 120
    seen = 0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            lines = events_file.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        terminal_seen = False
        for line in lines[seen:]:
            seen += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "state" and ev.get("state") in TERMINAL:
                terminal_seen = True
        if terminal_seen:
            # give an already-open SSE reader a moment to receive this exact
            # line before we pull the rug out from under the API.
            time.sleep(args.kill_grace)
            proc.send_signal(signal.SIGKILL)
            killed = True
            break
        time.sleep(0.01)
    if killed:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    else:
        proc.wait(timeout=30)
    (run_dir / ".stub-supervisor-done").write_text(
        json.dumps({"killed": killed, "returncode": proc.returncode}))


if __name__ == "__main__":
    main()
