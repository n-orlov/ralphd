#!/bin/bash
# ralphd container entrypoint: place injected config, then start the engine.
set -euo pipefail

# Requirement I (#48): this script runs as root in the shipped image, and the
# engine it execs drops its own effective uid to `agent` at startup while
# keeping root as its real/saved uid, so an iteration cannot signal it (see
# src/ralphd/engine/privsep.py for the mechanism and the kill(2) rule it
# rests on). Two consequences here:
#   * $HOME must be the *agent's* home, not root's -- the engine's $HOME
#     config overlay, the credentials it places for pi to read and pi's own
#     settings all have to resolve to the one home the iteration reads;
#   * anything this script places under that home must end up owned by the
#     agent, or the engine (effective uid agent) could not write beside it.
# Everything degrades cleanly when the container was started with
# `--user 1000` (or from a derived image that ends in `USER 1000`): no
# chown is attempted, and the engine logs that it is running without the
# boundary instead of refusing to start.
AGENT_USER="${RALPHD_AGENT_USER:-agent}"
if [ "$(id -u)" = 0 ]; then
    agent_home="$(getent passwd "$AGENT_USER" | cut -d: -f6 || true)"
    export HOME="${agent_home:-/home/${AGENT_USER}}"
fi

# pi provider/model config injected by the CLI (llm profile resolution)
if [ -d /config/pi ]; then
    mkdir -p "$HOME/.pi/agent"
    cp /config/pi/* "$HOME/.pi/agent/" 2>/dev/null || true
    if [ "$(id -u)" = 0 ]; then
        chown -R "$AGENT_USER" "$HOME/.pi" 2>/dev/null || true
    fi
fi

# NOTE: credential placement (*.env -> ~/.creds/, gitconfig, git-credentials,
# netrc, ssh/, setup.sh) is done by the engine itself at startup
# (src/ralphd/engine/creds.py:place_creds), not here -- this keeps secret
# handling inside the process that already promises never to leak values to
# /run, events, stdout, or job.json, instead of a second implementation in
# shell. It lands owned by the agent because the engine's *effective* uid is
# the agent's (#48), so pi can read it exactly as before.

# skills -> pi skill discovery: placed by the engine itself at startup and
# kept live by the runtime skills CRUD API (src/ralphd/engine/skills.py:
# place_skills), not here -- mirrors the creds discipline above (one place
# that knows the precedence rules: api overlay > mounted /config/skills,
# tombstones for API deletes), instead of a second implementation in shell
# that can only ever see the mounted set at container start.

exec ralphd-engine
