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
# for a *word-boundary* mention of `docs`, `artifacts` or `README` -- not
# just the slash-form `docs/`, because a test that reads repository content
# just as often builds the path from split components (`REPO_ROOT / "docs" /
# "architecture.md"`, `REPO_ROOT / "artifacts" / "reports" / ...`), which a
# `docs/`-only pattern silently misses. Discovery deliberately errs towards
# over-inclusion: running a handful of extra modules costs seconds, while
# omitting one is exactly the v0.13 failure this script exists to kill. Add
# a brand-new test module tomorrow that reads docs/ -- with no edit to this
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
# mentions `docs`, `artifacts` or `README` as a word is a guard for this
# subset -- a test cannot read one of those off disk without naming it,
# whether as `docs/architecture.md`, as `REPO_ROOT / "docs" / "cli.md"`, or
# as a bare `README` reference. `\b` keeps `docstring`/`docstrings` out.
# -l = filenames only, -E = extended regex, sort for a deterministic order.
mapfile -t MODULES < <(
  grep -lE '\b(docs|artifacts|README)\b' tests/test_*.py 2>/dev/null | sort
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
# that tier meaningless here). Selection is by the `smoke` marker (task 004,
# requirement G.8): the per-task tier -- in-process engine flows, in-process
# CLI, records lint, in-process hub -- which excludes docker, browser and the
# dev-layout lane by construction, and additionally leaves out the modules
# that spawn a real engine or `ralphctl` process, since those cost ~75 % of
# the fast tier's wall clock (docs/test-suite-audit.md section 2) and a
# docs- or report-only commit cannot break them. tests/conftest.py derives
# the marker; docs/testing.md records both tiers with their budgets.
# Discovery above still finds every guard module; this only deselects the
# tests of theirs that are outside the smoke tier, so the run stays a
# *subset* of the fast tier rather than a third, uncomparable lane.
exec "$PYTHON_BIN" -m pytest -q -m 'smoke' "${MODULES[@]}"
