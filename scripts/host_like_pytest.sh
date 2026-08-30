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
#     anywhere is covered automatically) *except* RALPHD_RUN_ID, which is
#     deliberately preserved unchanged if inherited, or minted fresh and
#     unique if not. Scrubbing RALPHD_RUN_ID away collapses the docker
#     tier's per-run tag isolation (`ralphd:test-<RALPHD_RUN_ID>`, see
#     tests/test_docker_sibling_e2e.py) to the shared fallback
#     `ralphd:test-local-dev` -- exactly the collision the docker tier's
#     tag-per-run scheme exists to prevent (F1, #73);
#   - a decoy top-level `tests` package placed on PYTHONPATH *ahead* of this
#     repository (the #50 shadowing scenario -- see tests_package_pin.py and
#     tests/test_tests_conftest_resolution.py for the mechanism this must
#     not regress). The decoy is a **regular** package (it has an
#     `__init__.py`), which is the shape that actually ships in the wild and
#     the only shape that is hard to beat: a regular package found anywhere on
#     sys.path pre-empts this repository's namespace portion no matter how
#     early that portion sits (#64). A namespace-portion decoy -- what this
#     script used to plant -- loses to plain sys.path ordering and so proved
#     almost nothing;
#   - a fresh, empty $HOME (rules out anything cached under the real one).
#
# Runs the PRD's literal acceptance command, `python -m pytest -q`. The
# browser-marked modules are left alone -- Playwright/Chromium is present in
# this job container, so that tier actually runs here, and stays comparable
# with a bare developer host.
#
# The docker tier is different, and this script's actual behaviour toward it
# genuinely branches on whether a usable docker socket is present at
# invocation time -- it does not universally assume one:
#
#   - A ralphd job container talks to its own sibling containers over a
#     bind-mounted docker socket, so *some* ralphd job containers do have a
#     usable socket at invocation time -- but not every one does, and not
#     always: this very container has none right now (`ls
#     /var/run/docker.sock` fails with "No such file or directory",
#     `DOCKER_SOCK_PATH` is unset, and the `docker` CLI's `version`
#     round-trip (the probe spelled out below) exits 1 with "failed to
#     connect to the docker API at unix:///var/run/docker.sock").
#     When a usable socket *is* present, running the docker-marked modules
#     anyway would not exercise the tier the way it runs on a bare host: it
#     would run *nested* (a sibling of a sibling), and the sibling's
#     published port is on a network namespace this container does not
#     share, so every docker test would time out with `[Errno 111]
#     Connection refused` against the bridge gateway instead of skipping or
#     passing cleanly. That false "it self-skips" premise in an earlier
#     version of this header cost a real 57-minute sweep (8 failed, 6
#     errors, all in the five docker-marked modules) before anyone noticed
#     the failures were an artefact of nesting, not a real regression
#     (F2, #74). So: whenever this script detects a usable socket (a `docker`
#     binary on PATH, a real socket file, and a working `docker version`
#     round-trip), it filters the docker tier out with `-m "not docker"`
#     instead of attempting to run it nested.
#   - When no usable socket is present -- as in this container right now, and
#     as on a bare developer host that lacks docker entirely -- the script
#     injects nothing extra for the docker tier: the docker-marked modules'
#     own module-level `skipif` (see tests/test_docker_sibling_e2e.py's
#     `_docker_available()`) is what makes them self-skip cleanly there, the
#     same as it would for a developer running the bare acceptance command
#     by hand.
#
# Making the nested docker tier actually pass is explicitly out of scope
# (#74) -- that tier still needs a real, unnested host run to mean anything.
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
# `__init__.py` is what makes this a *regular* package, and therefore a decoy
# worth planting -- see the header and #64.
cat >"$DECOY_ROOT/tests/__init__.py" <<'PYEOF'
DECOY_PACKAGE = True
PYEOF
cat >"$DECOY_ROOT/tests/conftest.py" <<'PYEOF'
DECOY_CONFTEST = True
RALPHCTL = "not-the-real-ralphctl"
PYEOF

CLEAN_HOME="$SCRATCH/home"
mkdir -p "$CLEAN_HOME"

# F1 (#73): capture (or mint) RALPHD_RUN_ID *before* scrubbing RALPHD_* vars,
# and re-inject it below -- never let it fall through to the docker tier's
# own "local-dev" fallback, which is shared across every invocation that
# lacks it and therefore unsafe to collide on. PID + nanosecond clock keeps
# repeated invocations on the same host apart even within the same second.
HOST_LIKE_RUN_ID="${RALPHD_RUN_ID:-}"
if [ -z "$HOST_LIKE_RUN_ID" ]; then
  HOST_LIKE_RUN_ID="host-like-$$-$(date +%s%N)"
fi

unset_args=()
while IFS='=' read -r name _; do
  case "$name" in
    RALPHD_RUN_ID) ;; # preserved deliberately, see above (F1, #73)
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

# F2 (#74): a usable docker socket means the docker-marked tests would run
# *nested* here (see header) instead of self-skipping -- filter the whole
# tier out with `-m "not docker"` rather than let it attempt that and time
# out against a bridge-gateway port this container cannot reach. The check
# mirrors tests/test_docker_sibling_e2e.py's own `_docker_available()`:
# a `docker` binary on PATH, a real socket file, and a working
# `docker version` round-trip. A caller who passes their own `-m` in the
# extra args after this script's name overrides this default, same as any
# other pytest argument would.
marker_args=()
if command -v docker >/dev/null 2>&1 \
   && [ -S "${DOCKER_SOCK_PATH:-/var/run/docker.sock}" ] \
   && docker version >/dev/null 2>&1; then
  marker_args=("-m" "not docker")
fi

# `-p tests_package_pin` is load-bearing, not belt-and-braces. The repo-root
# conftest.py also pins the `tests` package, but pytest only loads the conftests
# on the path to the arguments it is given: point this script at a probe file
# outside the repository (which tests/test_host_like_pytest_script.py does, and
# which is the only way to observe the child's environment from inside a test)
# and that conftest never loads, leaving the regular-package decoy above --
# or a `tests` package installed in site-packages, which is how #50 was found --
# free to win. Loading the pin as a plugin runs it before any conftest and
# regardless of what is being collected. Importable because `-m pytest` puts
# the working directory, `cd`'d to REPO_ROOT above, on sys.path.
#
# GNU env requires -u/-i options ahead of NAME=VALUE assignments.
exec env \
  "${unset_args[@]}" \
  HOME="$CLEAN_HOME" \
  RALPHD_RUN_ID="$HOST_LIKE_RUN_ID" \
  PYTHONPATH="$DECOY_ROOT${PYTHONPATH:+:${PYTHONPATH}}" \
  "$PY" -m pytest -q -p tests_package_pin "${marker_args[@]}" "$@"
