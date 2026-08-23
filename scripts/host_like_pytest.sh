#!/usr/bin/env bash
# Task 009 (#57, #50): reproduce a developer host's environment for the
# whole suite from *inside* this job container, so the claim "the fast lane
# is green on a developer host" is checkable without a second machine.
#
# This container is itself a ralphd job container (see notes.md), so it
# starts with several ambient RALPHD_* environment variables a real
# developer host would never have set. "Host-like" here means:
#
#   - every RALPHD_* environment variable unset (discovered dynamically from
#     the current environment, not a fixed list, so a future one added
#     anywhere is covered automatically);
#   - a decoy top-level `tests` package placed on PYTHONPATH *ahead* of this
#     repository (the #50 shadowing scenario -- see /conftest.py and
#     tests/test_tests_conftest_resolution.py for the mechanism this must
#     not regress);
#   - a fresh, empty $HOME (rules out anything cached under the real one).
#
# Runs the PRD's literal acceptance command, `python -m pytest -q`, with no
# `-m` marker filter: the docker- and browser-marked modules are expected to
# skip themselves cleanly here (no docker socket, and Playwright/Chromium
# is present so the browser tier actually runs) -- that self-skipping is
# what makes one bare command comparable across all three environments this
# task records.
#
# Usage: scripts/host_like_pytest.sh [extra pytest args...]
#
# Exit code and stdout/stderr are pytest's own -- pipe to a log and inspect
# pass/fail/skip counts the normal way.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

DECOY_ROOT="$SCRATCH/decoy"
mkdir -p "$DECOY_ROOT/tests"
cat >"$DECOY_ROOT/tests/conftest.py" <<'PYEOF'
DECOY_CONFTEST = True
RALPHCTL = "not-the-real-ralphctl"
PYEOF

CLEAN_HOME="$SCRATCH/home"
mkdir -p "$CLEAN_HOME"

unset_args=()
while IFS='=' read -r name _; do
  case "$name" in
    RALPHD_*) unset_args+=("-u" "$name") ;;
  esac
done < <(env)

cd "$REPO_ROOT"

# Interpreter selection: prefer an explicit override (PYTHON=..., set by a
# caller or a test that wants to exercise a specific interpreter), else this
# repo's own .venv if it exists (this job container's own layout), else fall
# back to whatever `python3` is on PATH -- a developer host's venv may live
# anywhere (~/.virtualenvs/..., uv's default, a system tox env, a plain
# `pip install -e .` into an already-active env), so hard-coding the .venv
# path exits 127 there (steering note 003, task 043).
if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

# GNU env requires -u/-i options ahead of NAME=VALUE assignments.
exec env \
  "${unset_args[@]}" \
  HOME="$CLEAN_HOME" \
  PYTHONPATH="$DECOY_ROOT${PYTHONPATH:+:${PYTHONPATH}}" \
  "$PY" -m pytest -q "$@"
