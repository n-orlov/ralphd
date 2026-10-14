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

Task 048 (#177, requirement J.5) added one more piece of the simulation:
before launching, it copies `<config-dir>/pi/*` into `$HOME/.pi/agent/`,
the same step `container/entrypoint.sh` performs for a real container, so
the spawned engine's `pi` invocation sees the same ambient models.json a
real run would (see main() below).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

# Task 011 (v0.24 requirement C.1, #169): the budget hold joins this tuple
# too -- recorded as `timed_out` before v0.36 and as `exhausted` since (v0.36
# requirement A.1); both spellings stay so a 0.35 run dir still reads as held.
# It is deliberately not one of `state.TERMINAL_STATES` in the engine itself
# (a future `resume` can still revive the run dir), but the *simulation*
# this supervisor exists to reproduce -- "the container's API dies right as
# the job stops making progress" -- applies just as much to a run that
# freezes on an exhausted iteration budget as to one that succeeds, fails,
# or aborts outright (see api.py's own `finished()`, task 010/task 011,
# which now agrees the hold means "no more work is coming from this
# process"). Without this, a `--no-detach` run that lands on `timed_out`
# never gets its terminal event caught here, and the real engine (which now
# also shuts its own API down gracefully once `finished()` says so) is left
# to exit on its own instead of being pulled out from under an open reader
# the way the other three endings are -- a live scenario this task's own
# coverage exercises, not just a stub-supervisor nicety.
TERMINAL = ("succeeded", "failed", "aborted", "exhausted", "timed_out")


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

    # Task 048 (#177, requirement J.5): mirror `container/entrypoint.sh`'s
    # own "pi provider/model config injected by the CLI" step here, before
    # the engine (and the `pi` it spawns) ever runs -- otherwise a test
    # driving `ralphctl start --llm-header`/`--llm <profile>` through this
    # knob would see the config dir's own `pi/models.json` (already
    # asserted by tests/test_cli_llm_profiles.py and friends) but never the
    # ambient-home copy the real container places for `pi` itself to read,
    # so an e2e assertion that the spawned process actually observes a
    # header would be checking nothing this stub ever exercised. Same
    # source, same `[ -d ... ]` guard, same destination as the shell step;
    # `shutil.copy` in place of `cp *` since we already have the file list.
    pi_src = Path(args.config_dir) / "pi"
    if pi_src.is_dir():
        home_agent_dir = (Path(os.environ.get("HOME", os.path.expanduser("~")))
                          / ".pi" / "agent")
        home_agent_dir.mkdir(parents=True, exist_ok=True)
        for item in pi_src.iterdir():
            if item.is_file():
                shutil.copy(item, home_agent_dir / item.name)

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
    events_file = run_dir / "events.jsonl"
    # A `resume`-style invocation over an existing run dir can already have
    # a terminal `state` event in events.jsonl from a PRIOR engine process
    # (e.g. the run already succeeded once, then got its iteration budget
    # topped up). Only lines appended AFTER this specific engine process
    # starts count as "this invocation reached a terminal state" -- start
    # `seen` at the pre-existing line count, not 0, so a stale old terminal
    # event never causes an instant false-positive kill before the new
    # engine process has done any work at all.
    try:
        seen = len(events_file.read_text().splitlines())
    except FileNotFoundError:
        seen = 0

    proc = subprocess.Popen(["ralphd-engine"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)

    killed = False
    deadline = time.monotonic() + 120
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
