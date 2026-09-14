#!/usr/bin/env bash
# Task 054 (K.3, #188/#189/#193/#179): make a `timeout-minutes` kill
# distinguishable from a human cancel and from a `cancel-in-progress`
# cancellation, on the surfaces a judging hat reads.
#
# The problem this exists for: a GitHub Actions job's own `conclusion` is
# `cancelled` for THREE different causes -- a human clicking "Cancel
# workflow", a `cancel-in-progress: true` group superseding an older run,
# and the job exceeding its `timeout-minutes` -- and the Checks API gives no
# field that tells them apart. `gate.yml`'s own comment already names the
# consequence ("A `timeout-minutes` kill reports as `cancelled`, which is
# not a verdict"); this script is the mechanism that removes the ambiguity
# for the one cause this job controls: its own long-running step.
#
# The mechanism: wrap the long-running command in this repo's own inner
# `timeout(1)` bound, set a little SHORTER than the job's own
# `timeout-minutes`. If the wrapped command overruns *that* inner bound,
# this script -- not the runner's job-level timeout -- is what kills it, and
# it does so with time left to report before the job-level bound could ever
# fire. It prints, and (when running under Actions) also appends to
# `$GITHUB_STEP_SUMMARY`, a single fixed, greppable marker line:
#
#   RALPHD_TIMEOUT_KILL: <label> exceeded its internal bound of <N>s
#
# and then exits 1 -- an ordinary step `failure`, not `cancelled`. Both
# surfaces a judging hat reads survive to be read: the step's own log text
# (fetched fresh via the Checks/Actions API, same as any other log) and the
# job summary blob (`GET .../actions/runs/{run_id}/... /jobs/{job_id}`'s
# sibling summary artifact). Either one already answers "was this job's own
# step still running when it died, or did it die some OTHER way" the moment
# it is read.
#
# The disambiguation is the other half, and it needs no code: if the
# wrapped command is killed from OUTSIDE this script instead -- a human
# cancel, or `cancel-in-progress` superseding this run -- the signal lands
# on `timeout(1)` itself before ITS internal alarm ever fires, so GNU
# `timeout` reports its own external-signal exit status (128+signum), never
# its self-timeout status of exactly 124. This script only ever prints the
# marker on that one specific 124, so an externally-cancelled run prints
# nothing and reports its ordinary `cancelled` conclusion -- unchanged, and
# correctly so, because that IS what happened. See
# tests/test_run_bounded_timeout_marker.py for both halves, exercised
# directly against this script (never against a live dispatched workflow
# run -- that would need a >45-minute wait to prove the negative half).
#
# Usage:
#   scripts/run_bounded.sh <inner-timeout-seconds> <label> -- <command...>
#
# `<label>` is free text (quote it if it has spaces) and appears verbatim in
# the marker line, so a job with more than one wrapped step can tell them
# apart without re-deriving which step's command was running.

set -uo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: $0 <inner-timeout-seconds> <label> -- <command...>" >&2
  exit 2
fi

INNER_TIMEOUT_S="$1"
shift
LABEL="$1"
shift

if [ "$1" != "--" ]; then
  echo "usage: $0 <inner-timeout-seconds> <label> -- <command...> (missing '--')" >&2
  exit 2
fi
shift

case "$INNER_TIMEOUT_S" in
  ''|*[!0-9]*)
    echo "inner-timeout-seconds must be a positive integer, got: $INNER_TIMEOUT_S" >&2
    exit 2
    ;;
esac

# `--kill-after` sends a KILL a few seconds after the initial TERM, in case
# the wrapped command ignores TERM -- still 124, still self-inflicted, still
# the marker case.
timeout --signal=TERM --kill-after=5s "${INNER_TIMEOUT_S}s" "$@"
status=$?

if [ "$status" -eq 124 ]; then
  marker="RALPHD_TIMEOUT_KILL: ${LABEL} exceeded its internal bound of ${INNER_TIMEOUT_S}s"
  echo "$marker"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "$marker" >>"$GITHUB_STEP_SUMMARY"
  fi
  exit 1
fi

exit "$status"
