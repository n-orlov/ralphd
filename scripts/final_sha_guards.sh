#!/usr/bin/env bash
# The guard subset a docs-only commit can break: every test module that
# reads the product docs, run at a final sha without paying for the whole
# fast tier.
#
# There is no module list anywhere in this file. Every invocation greps
# `tests/` (recursively) for test modules whose own source mentions `docs`,
# `README` or `SPEC` as a word -- not just the slash-form `docs/`, because a
# test that reads repository docs just as often builds the path from split
# components (`REPO_ROOT / "docs" / "architecture.md"`), which a
# `docs/`-only pattern would miss. Discovery errs towards over-inclusion: an
# extra module costs seconds, a missed one is a docs change that goes
# unchecked. A new docs-reading module is picked up on the next run with no
# edit here.
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

# Discovery, not enumeration: any test_*.py file anywhere under tests/ whose
# own SOURCE mentions `docs`, `README` or `SPEC` as a word. `\b` keeps
# `docstring`/`docstrings` out. -r = recurse, -l = filenames only,
# -E = extended regex; sort for a deterministic order.
mapfile -t MODULES < <(
  grep -rlE --include='test_*.py' '\b(docs|README|SPEC)\b' tests 2>/dev/null | sort
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

# Selection is by the `smoke` marker: the per-task tier (in-process engine
# flows, in-process CLI, in-process hub), which excludes docker, browser and
# the dev-layout lane by construction and also leaves out modules that spawn
# a real engine or `ralphctl` process -- most of the fast tier's wall clock,
# and nothing a docs-only commit can break. tests/conftest.py derives the
# marker; docs/testing.md describes both tiers. Discovery above still finds
# every guard module; this only deselects their tests outside the smoke
# tier, so the run stays a subset of the fast tier.
exec "$PYTHON_BIN" -m pytest -q -m 'smoke' "${MODULES[@]}"
