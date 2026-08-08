#!/bin/bash
# ralphd container entrypoint: place injected config, then start the engine.
set -euo pipefail

# pi provider/model config injected by the CLI (llm profile resolution)
if [ -d /config/pi ]; then
    mkdir -p ~/.pi/agent
    cp /config/pi/* ~/.pi/agent/ 2>/dev/null || true
fi

# NOTE: credential placement (*.env -> ~/.creds/, gitconfig, git-credentials,
# netrc, ssh/, setup.sh) is done by the engine itself at startup
# (src/ralphd/engine/creds.py:place_creds), not here -- this keeps secret
# handling inside the process that already promises never to leak values to
# /run, events, stdout, or job.json, instead of a second implementation in
# shell.

# skills -> pi skill discovery: placed by the engine itself at startup and
# kept live by the runtime skills CRUD API (src/ralphd/engine/skills.py:
# place_skills), not here -- mirrors the creds discipline above (one place
# that knows the precedence rules: api overlay > mounted /config/skills,
# tombstones for API deletes), instead of a second implementation in shell
# that can only ever see the mounted set at container start.

exec ralphd-engine
