#!/bin/bash
# ralphd container entrypoint: place injected config, then start the engine.
set -euo pipefail

# pi provider/model config injected by the CLI (llm profile resolution)
if [ -d /config/pi ]; then
    mkdir -p ~/.pi/agent
    cp /config/pi/* ~/.pi/agent/ 2>/dev/null || true
fi

# recognized credential files
if [ -d /config/creds ]; then
    [ -f /config/creds/gitconfig ] && cp /config/creds/gitconfig ~/.gitconfig
    [ -f /config/creds/git-credentials ] && {
        cp /config/creds/git-credentials ~/.git-credentials
        chmod 600 ~/.git-credentials
        git config --global credential.helper store
    }
    [ -f /config/creds/netrc ] && { cp /config/creds/netrc ~/.netrc; chmod 600 ~/.netrc; }
    [ -d /config/creds/ssh ] && { mkdir -p ~/.ssh; cp -r /config/creds/ssh/* ~/.ssh/; chmod -R go-rwx ~/.ssh; }
    [ -x /config/creds/setup.sh ] && /config/creds/setup.sh
fi

# skills → pi skill discovery location
if [ -d /config/skills ]; then
    mkdir -p ~/.pi/agent/skills
    for s in /config/skills/*/; do
        [ -d "$s" ] && ln -sfn "$s" ~/.pi/agent/skills/"$(basename "$s")"
    done
fi

exec ralphd-engine
