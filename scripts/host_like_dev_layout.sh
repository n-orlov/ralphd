#!/usr/bin/env bash
# Task 008 (v0.12 requirement D, #84): a THIRD host-shape layout, sibling to
# scripts/host_like_pytest.sh, not a mode of it.
#
# host_like_pytest.sh (see its own header) reproduces a developer host's
# *environment* -- stray RALPHD_* vars, a shadowing `tests` package, a fresh
# $HOME -- but never its *install layout and dotfiles*. The v0.12 PRD's
# "read this before anything else" section names exactly the two blind spots
# that leaves:
#
#   1. This job container installs ralphd system-wide
#      (`container/Dockerfile:42`: `pip install --no-cache-dir /opt/ralphd`),
#      so $HOME never decides whether `ralphd` imports. On a real developer
#      host's `pip install --user -e .` it does.
#   2. host_like_pytest.sh's own $HOME is a fresh, EMPTY directory. An empty
#      home has no ~/.pi/agent/models.json, and `check_hat_routes` documents
#      an empty provider list as "this profile hasn't declared anything yet"
#      and skips the whole check -- so that script's green is silent about
#      the exact ambient-$HOME read this wave is about (#85).
#
# SIBLING SCRIPT, not a new host_like_pytest.sh mode, because the two have
# different lifecycles and different failure surfaces. A `pip install --user
# -e .` is a mutating, first-run setup step (needs `pip`, a working build
# backend, and -- the one time per cache miss -- network to fetch it) that
# every host_like_pytest.sh invocation would otherwise pay for even when the
# thing THIS script exists to prove ("the import path moves with $HOME") is
# not what that invocation is testing. Keeping them separate also keeps
# host_like_pytest.sh's own header, already dense, about one subject.
#
# What this script builds, and why each piece:
#
#   (a) User-base editable install, wired through PYTHONUSERBASE explicitly
#       rather than left to each platform's own $HOME-derived default (which
#       differs: `<home>/.local` on Linux, `<home>/Library/Python/X.Y` on
#       macOS) -- so "HOME moves where ralphd is imported from" is a literal,
#       portable claim: PYTHONUSERBASE is computed FROM the new $HOME once
#       (`site.getuserbase()` under that $HOME), then exported for the
#       install and every subsequent invocation, so moving $HOME again would
#       move it again.
#
#       This requires a NON-venv interpreter: a virtualenv disables user-site
#       packages outright (`site.ENABLE_USER_SITE` is False inside one --
#       verified live in this repo's own .venv), which would make PYTHONUSERBASE
#       inert and the whole layout's claim (a) vacuous. So, unlike
#       host_like_pytest.sh (which prefers this repo's own .venv), this script
#       defaults to the bare `python3` on PATH and refuses to run under a
#       PYTHON= override that turns out to be a venv.
#
#   (b) A *populated, hostile* $HOME, built before the install: a
#       ~/.pi/agent/models.json declaring a provider no fixture in this repo
#       ever names (grep tells you so -- see the check below), and a skill
#       placed under ~/.pi/agent/skills/ the way `ralphctl start --skills`
#       would mount one.
#
#   (c) After the pytest run, re-read ~/.pi/agent/skills/<name>/SKILL.md and
#       report (stdout, one greppable line) whether the placed skill
#       survived -- the #83 class this whole wave is about is exactly a
#       suite run silently deleting it.
#
# Usage: scripts/host_like_dev_layout.sh [pytest args...]
#
# Exit code is pytest's own, UNLESS the install/import-path setup itself
# fails, in which case it is 2 (venv interpreter refused) or 3 (import did
# not actually move with $HOME -- the setup did not take).
#
# Env overrides:
#   PYTHON              interpreter to use instead of the bare `python3` on
#                        PATH. Must NOT be a virtualenv interpreter (checked).
#   HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR
#                        pip cache dir to reuse across invocations (each
#                        invocation's $HOME is a fresh scratch dir, so pip's
#                        own default `$HOME/.cache/pip` would never be
#                        reused otherwise). Defaults to
#                        /tmp/host-like-dev-layout-pip-cache.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

DEV_HOME="$SCRATCH/home"
mkdir -p "$DEV_HOME"

# --- interpreter selection: must have user-site enabled ---------------------
if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON:-}" ]; then
  PY="$PYTHON"
else
  PY="$(command -v python3)"
fi

if ! "$PY" -c "import site,sys; sys.exit(0 if site.ENABLE_USER_SITE else 1)"; then
  echo "host_like_dev_layout.sh: '$PY' has user-site packages disabled (a" \
       "virtualenv?) -- this layout's whole point is a --user editable" \
       "install, so pass PYTHON=<a non-venv interpreter> or leave PYTHON" \
       "unset to use the bare python3 on PATH" >&2
  exit 2
fi

# --- (b) populate the hostile $HOME BEFORE the install ----------------------
mkdir -p "$DEV_HOME/.pi/agent/skills"
cat >"$DEV_HOME/.pi/agent/models.json" <<'JSON'
{
  "providers": {
    "host-layout-decoy-provider-no-fixture-uses": {
      "baseUrl": "https://example.invalid/decoy",
      "apiKey": "no-fixture-should-ever-read-this-value"
    }
  }
}
JSON

SKILL_NAME="host-layout-smoke-skill"
mkdir -p "$DEV_HOME/.pi/agent/skills/$SKILL_NAME"
cat >"$DEV_HOME/.pi/agent/skills/$SKILL_NAME/SKILL.md" <<MD
# $SKILL_NAME

Placed by scripts/host_like_dev_layout.sh (task 008, v0.12 requirement D)
before the pytest run, to prove a skill placed the way \`ralphctl start
--skills\` mounts one survives a full suite run under a populated,
non-isolating \$HOME (the #83 class).
MD

# --- (a) PYTHONUSERBASE computed FROM the new $HOME, once -------------------
DEV_USERBASE="$(HOME="$DEV_HOME" "$PY" -c 'import site; print(site.getuserbase())')"

PIP_CACHE_DIR="${HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR:-/tmp/host-like-dev-layout-pip-cache}"
mkdir -p "$PIP_CACHE_DIR"

# `.[dev]` pulls in ralphd's own runtime deps (fastapi/uvicorn/pyyaml) AND
# the test-time deps (pytest, pytest-asyncio, httpx, ...) in one step -- a
# real developer host's `pip install --user -e ".[dev]"`, not a hand-picked
# subset that could quietly drift from pyproject.toml's own `dev` extra.
HOME="$DEV_HOME" PYTHONUSERBASE="$DEV_USERBASE" PIP_CACHE_DIR="$PIP_CACHE_DIR" \
  "$PY" -m pip install --user -q --no-warn-script-location -e "$REPO_ROOT[dev]"

# Verify the install actually did what (a) claims: `ralphd` must now import
# from under $DEV_HOME's user base, not from wherever this container's own
# system-wide install lives.
IMPORT_PATH="$(HOME="$DEV_HOME" PYTHONUSERBASE="$DEV_USERBASE" \
  "$PY" -c 'import ralphd; print(ralphd.__file__)')"
case "$IMPORT_PATH" in
  "$REPO_ROOT"/*) ;; # editable install resolves into this checkout's src/
  *)
    echo "host_like_dev_layout.sh: ralphd imported from '$IMPORT_PATH'," \
         "not from this checkout ($REPO_ROOT) -- the PYTHONUSERBASE/\$HOME" \
         "wiring did not take" >&2
    exit 3
    ;;
esac

# --- run the pytest smoke subset under the hostile, populated $HOME ---------
cd "$REPO_ROOT"
set +e
HOME="$DEV_HOME" PYTHONUSERBASE="$DEV_USERBASE" PIP_CACHE_DIR="$PIP_CACHE_DIR" \
  "$PY" -m pytest -q "$@"
STATUS=$?
set -e

# --- (c) re-check the placed skill's survival, and report it ----------------
if [ -f "$DEV_HOME/.pi/agent/skills/$SKILL_NAME/SKILL.md" ]; then
  echo "host_like_dev_layout.sh: placed skill '$SKILL_NAME' SURVIVED the run"
else
  echo "host_like_dev_layout.sh: placed skill '$SKILL_NAME' DID NOT SURVIVE" \
       "the run (lost under ~/.pi/agent/skills)"
fi

exit "$STATUS"
