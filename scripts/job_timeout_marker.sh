#!/usr/bin/env bash
# Task 054 (K.3, #188/#189/#193/#179), second half of the mechanism.
#
# `scripts/run_bounded.sh` beside this file removes the ambiguity for the
# cause a *step* controls: it bounds a long-running command from the inside,
# so an overrun becomes an ordinary `failure` carrying a greppable marker
# rather than the job-level bound's `cancelled`. That is prevention, and it
# cannot cover everything: a job also spends wall clock in steps no wrapper
# owns (`actions/checkout`, the environment report, a cleanup step), and a
# runner can be slow enough that the sum of those pushes the job past its
# `timeout-minutes` even with every wrapped bound respected.
#
# This script is the classifier for exactly that residual case. It runs as
# each job's LAST step under `if: always()`, which Actions executes on a
# cancelled job as well as a passing or failing one, and it answers the one
# question the Checks/Actions API cannot: of the three causes that all
# report `cancelled` -- a human pressing Cancel, `cancel-in-progress`
# superseding this run, and the job's own `timeout-minutes` firing -- which
# one happened here.
#
# How it can tell: a `timeout-minutes` kill can only ever land when the job
# has been alive for its whole declared bound, whereas a human cancel or a
# supersession lands whenever the human or the newer push happens to arrive,
# which is almost never within seconds of that bound. So the job stamps its
# own start time into `$GITHUB_ENV` as its first step
# (`RALPHD_JOB_STARTED_AT`), and this step compares elapsed wall time
# against the bound the workflow declared for that job. Within the grace
# window of the bound, it prints -- to the step log and to
# `$GITHUB_STEP_SUMMARY`, the two surfaces a judging hat already fetches
# fresh -- the same fixed marker prefix the step wrapper uses:
#
#   RALPHD_TIMEOUT_KILL: <label> reached its job-level bound of <N> minutes (elapsed <E>s)
#
# Short of that window it prints nothing at all, so a genuine cancel still
# leaves a bare `cancelled` conclusion -- unchanged, and correctly so.
#
# Two deliberate properties:
#
#   * It never changes the job's outcome. It exits 0 whatever it finds
#     (including when the start stamp is missing or unparseable, which it
#     reports on stderr): a classifier that could turn a green job red would
#     be a worse trade than the ambiguity it removes.
#   * The window is a window, not an equality test. A cancel that arrives
#     inside it is reported as a timeout, and that direction is chosen on
#     purpose: the marker's claim is "this job was still running when its
#     declared bound came due", which is the fact a judging hat needs in
#     order to not read the `cancelled` as a verdict either way.
#
# Usage:
#   scripts/job_timeout_marker.sh <job-timeout-minutes> <label>
#
# Env:
#   RALPHD_JOB_STARTED_AT           epoch seconds, written by the job's first step
#   RALPHD_JOB_TIMEOUT_GRACE_S      window width in seconds (default 90)
#   GITHUB_STEP_SUMMARY             appended to when set
#
# See tests/test_run_bounded_timeout_marker.py for both halves of both
# scripts, exercised directly and never against a live dispatched run.

set -uo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <job-timeout-minutes> <label>" >&2
  exit 0
fi

BOUND_MINUTES="$1"
LABEL="$2"
GRACE_S="${RALPHD_JOB_TIMEOUT_GRACE_S:-90}"

case "$BOUND_MINUTES" in
  ''|*[!0-9]*)
    echo "job-timeout-minutes must be a positive integer, got: $BOUND_MINUTES" >&2
    exit 0
    ;;
esac

started="${RALPHD_JOB_STARTED_AT:-}"
case "$started" in
  ''|*[!0-9]*)
    echo "RALPHD_JOB_STARTED_AT is unset or not an epoch, cannot classify this ending" >&2
    exit 0
    ;;
esac

now="$(date +%s)"
elapsed=$((now - started))
threshold=$((BOUND_MINUTES * 60 - GRACE_S))

if [ "$elapsed" -ge "$threshold" ]; then
  marker="RALPHD_TIMEOUT_KILL: ${LABEL} reached its job-level bound of ${BOUND_MINUTES} minutes (elapsed ${elapsed}s)"
  echo "$marker"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "$marker" >>"$GITHUB_STEP_SUMMARY"
  fi
fi

exit 0
