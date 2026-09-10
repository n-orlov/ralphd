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
# Task 076 (steer 008): this is the mechanism the nightly `dev-layout`
# container matrix (task 077) runs per cell, six times over per push -- so
# the ONE mechanism this script uses to decide what pytest selects, unless
# a caller says otherwise, defaults to the `fast` tier rather than the
# whole suite. The unfiltered whole-suite invocation this script ran through
# task 075 measured 2047.32 s / 2146.54 s per mode (docs/testing.md); six
# cells of that would be ~5 h of runner time across two slots. The content a
# `-m fast` selection drops relative to unfiltered -- the `docker` and
# `browser` tiers -- is already run at the gate-blocking level by
# `.github/workflows/heavy.yml` and does not vary with the interpreter or
# libc a dev-layout cell is exercising, so the matrix loses no coverage that
# varies with the axis under test by dropping it here. That is REASON ONE the
# default is `fast`.
#
# REASON TWO (steer 009): `-m fast` EXCLUDES `-m browser` (tests/conftest.py
# never stamps a `browser`-marked node with `fast`), so no matrix cell ever
# drives a browser -- `.github/workflows/heavy.yml`'s `browser tier` job stays
# the ONLY browser-bearing job on the self-hosted box. That is what makes the
# single `browser-runner` label the operator placed on exactly one of the
# VPS's two runner slots sufficient to serialise browser work across
# *workflows*: two concurrent browser jobs collapse each other's
# `playwright-cli` sessions against real Chrome on a 4-core host, and a
# `concurrency:` group cannot prevent that (it does not know what else is on
# the runner). A dev-layout cell that ran the suite unfiltered would drive
# browsers from `nightly` too, on any slot, and defeat that label.
#
# A caller that wants a
# different selection -- including the retired unfiltered form -- overrides
# it through ONE env var, HOST_LIKE_DEV_LAYOUT_TIER (see below); nothing else
# decides tier selection.
#
# The sweep-sha carve-out (task 053, not yet landed as of this task): the
# gate (`.github/workflows/gate.yml`) still runs the `fast` tier with
# `--deselect 'tests/test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged'`,
# because that node's own HEAD-relative drift check is meaningless run
# against an arbitrary, possibly-stale checkout the way this script's own
# nightly cells are. A caller of this script may pass that exact
# `--deselect '...'` argument the same way the gate does -- it rides through
# to the underlying `pytest` invocation via "$@" like any other pytest
# argument, no separate wiring needed. Once task 053 retires that guard
# module, this carve-out is gone and there is nothing left to deselect for
# this reason.
#
# Exit code is pytest's own, UNLESS the install/import-path setup itself
# fails, in which case it is 2 (venv interpreter refused, an invalid
# HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE, or a HOST_LIKE_DEV_LAYOUT_TIER
# that is not even marker-expression SHAPED -- blank, unbalanced
# parentheses, or a character no marker expression uses; see that entry
# below for what this script does and does not judge) or 3 (import did not
# actually move with $HOME -- the setup did not take).
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
#   HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE
#                        Task 019 (#93): which of two modes decides the
#                        pytest *subprocess*'s import path. The install step
#                        above always exports PYTHONUSERBASE explicitly (it
#                        has to land packages at a known, reused location);
#                        this only controls what the `pytest` invocation
#                        itself sees.
#
#                          exported (default) -- PYTHONUSERBASE=$DEV_USERBASE
#                          is exported to the pytest process, same as every
#                          prior version of this script. This is the mode
#                          that let task 008's own sweep go green without
#                          ever noticing whether tests/conftest.py's autouse
#                          HOME-isolation fixture pins PYTHONUSERBASE itself
#                          (task 018, #67/#83): with this mode, the *outer*
#                          pytest process already has the right value before
#                          any fixture runs, so a fixture that pins nothing
#                          is indistinguishable from one that pins correctly
#                          -- os.environ's PYTHONUSERBASE never leaves the
#                          correct value, fixture or no fixture.
#
#                          unset -- PYTHONUSERBASE is *not* set for the
#                          pytest process at all (`env -u PYTHONUSERBASE`,
#                          which strips it even if some ambient caller
#                          exported one). The interpreter still resolves its
#                          *own* startup user-site correctly, because
#                          `site.getuserbase()` derives the same value from
#                          $HOME alone when PYTHONUSERBASE is unset -- so
#                          collection and ordinary imports are unaffected.
#                          What changes is every test that overrides HOME
#                          in-process and then spawns a child inheriting
#                          `os.environ` as-is: with task 018's fixture fix
#                          reverted, PYTHONUSERBASE is not in os.environ to
#                          begin with, so nothing pins it, and a child that
#                          inherits the fixture's *new*, isolated HOME with
#                          no PYTHONUSERBASE resolves its own user-site
#                          under that empty isolated dir -- the #67 defect,
#                          reproduced in-process. This is the mode that
#                          actually exercises whether the fixture's own pin
#                          matters; "exported" cannot, by construction.
#
#                        See artifacts/reports/task-019-dev-layout-unset-mode.md
#                        for the mutation evidence (fails without task 018's
#                        fixture fix, passes with it) that only the `unset`
#                        mode can produce.
#
#   HOST_LIKE_DEV_LAYOUT_TIER
#                        Task 076 (steer 008): which pytest marker selection
#                        this script's own `pytest` invocation carries, ADDED
#                        to (never instead of) whatever selection the
#                        caller's own pytest args already request. Defaults
#                        to `fast` (see the rationale above the Usage line).
#                        Any other marker expression pytest's own `-m`
#                        accepts is honoured verbatim (e.g. `smoke`,
#                        `docker`, `fast and not docker`).
#
#                        This script judges only the value's SHAPE, and it
#                        does so before the (slow) install step so a typo
#                        costs milliseconds instead of surfacing minutes
#                        later: a blank value, unbalanced parentheses, or a
#                        character outside a marker expression's alphabet
#                        (letters, digits, `_`, whitespace, parentheses, `-`)
#                        is refused with exit 2 and a message naming the
#                        value. It deliberately does NOT judge the
#                        expression's meaning -- it cannot, since pytest's
#                        `-m` grammar and the set of registered markers live
#                        in the interpreter being set up, not in this shell
#                        -- so a value that is shaped like a marker
#                        expression but that pytest rejects anyway (an
#                        unknown operator, a dangling `and`) reaches pytest
#                        and surfaces as pytest's OWN usage error, exit 4.
#
#                        The one explicit "no filter" form is the
#                        literal value `unfiltered`: no `-m` flag is added at
#                        all, reproducing the whole-suite invocation every
#                        version of this script ran before task 076 (the two
#                        measurements in docs/testing.md's dev-layout rows).
#                        Before running pytest, this script echoes exactly
#                        which selection it applied, on its own stdout line
#                        starting `host_like_dev_layout.sh: pytest tier
#                        selection:` -- the same greppable-report convention
#                        as the PYTHONUSERBASE_MODE line above.

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

PYTHONUSERBASE_MODE="${HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE:-exported}"
case "$PYTHONUSERBASE_MODE" in
  exported|unset) ;;
  *)
    echo "host_like_dev_layout.sh: HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE" \
         "must be 'exported' or 'unset', got '$PYTHONUSERBASE_MODE'" >&2
    exit 2
    ;;
esac

if ! "$PY" -c "import site,sys; sys.exit(0 if site.ENABLE_USER_SITE else 1)"; then
  echo "host_like_dev_layout.sh: '$PY' has user-site packages disabled (a" \
       "virtualenv?) -- this layout's whole point is a --user editable" \
       "install, so pass PYTHON=<a non-venv interpreter> or leave PYTHON" \
       "unset to use the bare python3 on PATH" >&2
  exit 2
fi

# --- tier selection (task 076, steer 008): one documented mechanism --------
# Defaults to `fast`; the literal value `unfiltered` is the explicit
# "no filter" form. See the header's Usage section and
# HOST_LIKE_DEV_LAYOUT_TIER entry for the rationale.
DEV_LAYOUT_TIER="${HOST_LIKE_DEV_LAYOUT_TIER:-fast}"

# Shape check only, and only what this shell can honestly judge (the header's
# HOST_LIKE_DEV_LAYOUT_TIER entry says so in as many words): a blank value, a
# character no marker expression uses, or unbalanced parentheses is refused
# here -- before the install step below, so a typo fails in milliseconds --
# while an expression that merely MEANS nothing to pytest is left for pytest
# itself to reject with its own usage error.
TIER_INVALID=""
case "$DEV_LAYOUT_TIER" in
  *[![:alnum:][:space:]_\(\)-]*)
    TIER_INVALID="it contains a character no pytest marker expression uses"
    ;;
esac
if [ -z "${DEV_LAYOUT_TIER//[[:space:]]/}" ]; then
  TIER_INVALID="it is blank"
fi
if [ -z "$TIER_INVALID" ]; then
  PAREN_DEPTH=0
  for (( TIER_I = 0; TIER_I < ${#DEV_LAYOUT_TIER}; TIER_I++ )); do
    case "${DEV_LAYOUT_TIER:$TIER_I:1}" in
      "(") PAREN_DEPTH=$((PAREN_DEPTH + 1)) ;;
      ")") PAREN_DEPTH=$((PAREN_DEPTH - 1)) ;;
    esac
    if [ "$PAREN_DEPTH" -lt 0 ]; then break; fi
  done
  if [ "$PAREN_DEPTH" -ne 0 ]; then
    TIER_INVALID="its parentheses are unbalanced"
  fi
fi
if [ -n "$TIER_INVALID" ]; then
  echo "host_like_dev_layout.sh: HOST_LIKE_DEV_LAYOUT_TIER" \
       "'$DEV_LAYOUT_TIER' is not shaped like a pytest marker expression:" \
       "$TIER_INVALID. Pass an expression built from marker names, 'not'," \
       "'and', 'or' and parentheses (e.g. 'fast', 'smoke'," \
       "'fast and not docker'), or the literal 'unfiltered' to add no -m" \
       "flag at all." >&2
  exit 2
fi

PYTEST_TIER_ARGS=()
if [ "$DEV_LAYOUT_TIER" = "unfiltered" ]; then
  echo "host_like_dev_layout.sh: pytest tier selection: no marker filter" \
       "(unfiltered) -- HOST_LIKE_DEV_LAYOUT_TIER=unfiltered"
else
  PYTEST_TIER_ARGS=(-m "$DEV_LAYOUT_TIER")
  echo "host_like_dev_layout.sh: pytest tier selection: -m $DEV_LAYOUT_TIER"
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
# `env -u PYTHONUSERBASE` here matters even in the default "exported" mode:
# without it, a caller of THIS script that already has PYTHONUSERBASE set
# ambiently (any nested invocation -- e.g. a test that shells out to this
# script while running under this repo's own conftest.py autouse fixture,
# task 018 -- or simply an operator's shell with one exported) would make
# `site.getuserbase()` return that inherited value verbatim, ignoring
# $DEV_HOME entirely (PYTHONUSERBASE always wins over HOME when set). That
# silently derives DEV_USERBASE from the wrong home and is invisible in
# "exported" mode (install and run both use the same, wrongly-derived
# value, so they still agree) -- exactly the failure the `unset` mode
# exists to expose (see tests/test_host_like_dev_layout_script.py, which
# invokes this script from inside a pytest process where task 018's fixture
# has already pinned an ambient PYTHONUSERBASE).
DEV_USERBASE="$(env -u PYTHONUSERBASE HOME="$DEV_HOME" "$PY" -c 'import site; print(site.getuserbase())')"

PIP_CACHE_DIR="${HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR:-/tmp/host-like-dev-layout-pip-cache}"
mkdir -p "$PIP_CACHE_DIR"

# Task 066: a developer host's system python3 is frequently PEP 668
# "externally managed" (Debian/Ubuntu ship an EXTERNALLY-MANAGED marker file
# in the stdlib dir since python3.11), which makes a bare `pip install --user`
# against it fail outright with "externally-managed-environment" -- the
# nightly dev-layout lane's own host hits exactly this. Detect the marker on
# the SELECTED interpreter ($PY, already checked above to be a non-venv
# interpreter with user-site enabled) rather than assuming either way, and
# pass --break-system-packages ONLY when it is present; an interpreter with
# no marker is invoked exactly as before, with no extra flag.
#
# Task 076 pins this branch with named nodes in
# tests/test_host_like_dev_layout_script.py:
# test_break_system_packages_is_passed_exactly_when_the_marker_is_present
# (both directions), test_the_pep668_install_targets_only_the_throwaway_user_base
# and test_the_marker_probe_reads_the_selected_interpreters_own_stdlib. They
# drive this script under a stub interpreter with a synthetic marker, since
# whether the ambient python3 carries one is not something a test can choose;
# a genuinely externally-managed interpreter is the `ubuntu:24.04` cell of
# task 077's matrix.
EXTERNALLY_MANAGED_MARKER="$("$PY" -c \
  'import os, sysconfig; print(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED"))')"
PIP_EXTRA_ARGS=()
if [ -f "$EXTERNALLY_MANAGED_MARKER" ]; then
  # PEP 668's marker refuses the pip install *action* for this interpreter
  # no matter what target it is pointed at -- it has no notion of "this
  # target is a throwaway dir, not the system site-packages", so it blocks
  # `--user` here exactly as it would block a plain, unqualified install.
  # --break-system-packages' name describes what it does to the SYSTEM
  # python (nothing stops you clobbering /usr/lib/python3/dist-packages);
  # it does the opposite of its name in this script specifically, because
  # this install's *target* is never the system site-packages: $DEV_USERBASE
  # was computed above from `site.getuserbase()` evaluated under $DEV_HOME,
  # and $DEV_HOME is this script's own `mktemp -d` scratch dir (`$SCRATCH`,
  # removed by the `trap ... EXIT` at the top) -- never `/usr`, and never the
  # invoking user's real $HOME. So the flag only lifts a blanket refusal that
  # was never protecting anything reachable from that throwaway path.
  PIP_EXTRA_ARGS+=(--break-system-packages)
  echo "host_like_dev_layout.sh: '$PY' is externally managed" \
       "($EXTERNALLY_MANAGED_MARKER exists) -- installing with" \
       "--break-system-packages into the throwaway user base" \
       "$DEV_USERBASE (never the system site-packages)"
fi

# `.[dev]` pulls in ralphd's own runtime deps (fastapi/uvicorn/pyyaml) AND
# the test-time deps (pytest, pytest-asyncio, httpx, ...) in one step -- a
# real developer host's `pip install --user -e ".[dev]"`, not a hand-picked
# subset that could quietly drift from pyproject.toml's own `dev` extra.
HOME="$DEV_HOME" PYTHONUSERBASE="$DEV_USERBASE" PIP_CACHE_DIR="$PIP_CACHE_DIR" \
  "$PY" -m pip install --user -q --no-warn-script-location "${PIP_EXTRA_ARGS[@]}" -e "$REPO_ROOT[dev]"

# Verify the install actually did what (a) claims: `ralphd` must now import
# from under $DEV_HOME's user base, not from wherever this container's own
# system-wide install lives.
#
# Task 068 (steer 005 defect 1): `pip install --user` writes its console
# scripts (ralphctl, ralphd-engine) into $DEV_USERBASE/bin, a directory that
# is on no process's PATH by default -- a real developer host gets it onto
# PATH via ~/.profile (Debian's stock one appends ~/.local/bin), which this
# throwaway, non-interactive $HOME never sources. Every invocation below that
# is meant to resolve those console scripts (this import-path check included,
# since it shares $DEV_HOME/$DEV_USERBASE with both pytest modes and is the
# cheapest place to prove the directory is findable) must therefore put
# $DEV_USERBASE/bin first on PATH itself, exactly once, computed here and
# reused below.
DEV_USERBASE_BIN_PATH="$DEV_USERBASE/bin:$PATH"
IMPORT_PATH="$(HOME="$DEV_HOME" PYTHONUSERBASE="$DEV_USERBASE" PATH="$DEV_USERBASE_BIN_PATH" \
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
# Mode decides whether the pytest *invocation itself* sees PYTHONUSERBASE.
# The install above always needed it explicit; this is deliberately a
# separate decision (see HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE above).
cd "$REPO_ROOT"
set +e
if [ "$PYTHONUSERBASE_MODE" = "unset" ]; then
  echo "host_like_dev_layout.sh: PYTHONUSERBASE_MODE=unset -- running pytest" \
       "with PYTHONUSERBASE unset; \$HOME alone decides the import path"
  # `unset` mode is ONLY about which env var decides the pytest subprocess's
  # own IMPORT path ($HOME alone, via env -u PYTHONUSERBASE, per the header
  # above) -- it says nothing about where `pip install --user` put the
  # console scripts this suite shells out to. Those landed in the SAME
  # $DEV_USERBASE/bin in both modes (the install step above always exports
  # PYTHONUSERBASE explicitly), so both modes need that directory on PATH;
  # putting it on PATH here does not re-introduce PYTHONUSERBASE into the
  # subprocess's environment and does not weaken this mode's own claim.
  HOME="$DEV_HOME" PIP_CACHE_DIR="$PIP_CACHE_DIR" PATH="$DEV_USERBASE_BIN_PATH" \
    env -u PYTHONUSERBASE "$PY" -m pytest -q "${PYTEST_TIER_ARGS[@]}" "$@"
else
  HOME="$DEV_HOME" PYTHONUSERBASE="$DEV_USERBASE" PIP_CACHE_DIR="$PIP_CACHE_DIR" \
    PATH="$DEV_USERBASE_BIN_PATH" "$PY" -m pytest -q "${PYTEST_TIER_ARGS[@]}" "$@"
fi
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
