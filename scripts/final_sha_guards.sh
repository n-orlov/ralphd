#!/usr/bin/env bash
# Task 003 (requirement B, #103): the guard subset a docs- or report-only
# commit can break, run at every "final sha" without paying for the whole
# fast tier (~14 minutes, 4237+ tests).
#
# v0.13 approach 3 task 012 tried to name that subset with a hand-typed
# module list; the list omitted a module and the task exhausted its
# validation ladder ("failed" -- see docs/prds and notes.md for the record).
# This script removes the failure mode instead of patching the list: there
# is no list anywhere in this file. Every invocation grep's `tests/` fresh
# for the literal strings a test would need to reference `docs/`,
# `artifacts/` or `README` content, and runs exactly what it finds. Add a
# brand-new test module tomorrow that reads docs/ -- with no edit to this
# script -- and the very next run picks it up, because the discovery step
# runs then, not now.
#
# Usage:
#   scripts/final_sha_guards.sh            # discover, print, run pytest
#   scripts/final_sha_guards.sh --list      # discovery only, one path per line

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

# Discovery, not enumeration: any tests/test_*.py file whose own SOURCE
# mentions docs/, artifacts/ or README is a guard for this subset -- that
# is the literal shape a test has to take to read one of those on disk.
# -l = filenames only, -E = extended regex, sort for a deterministic order.
mapfile -t MODULES < <(
  grep -lE 'docs/|artifacts/|README' tests/test_*.py 2>/dev/null | sort
)

if [[ "${1:-}" == "--list" ]]; then
  printf '%s\n' "${MODULES[@]}"
  exit 0
fi

if [[ "${#MODULES[@]}" -eq 0 ]]; then
  echo "final_sha_guards: discovery found no modules -- refusing to report a false green" >&2
  exit 1
fi

echo "final_sha_guards: discovered ${#MODULES[@]} module(s) by grepping tests/:"
printf '  %s\n' "${MODULES[@]}"
echo

# The docker/browser tiers are separate, deliberately excluded lanes (see
# scripts/host_like_pytest.sh's header for why a nested docker socket makes
# that tier meaningless here) -- same filter the fast tier already uses, so
# this stays a *subset* of the fast tier rather than a third, uncomparable
# lane. Discovery still finds those modules; this only skips their marked
# tests, so the deselect is fast rather than a real docker/browser run.
exec "$PYTHON_BIN" -m pytest -q -m 'not docker and not browser' "${MODULES[@]}"
