#!/usr/bin/env bash
# Task 054 (K.3, #188/#189/#193/#179), the classifier half of the mechanism.
#
# `scripts/run_bounded.sh` beside this file bounds every wall-clock step from
# the inside and, when one of those bounds expires, leaves a positive
# EVIDENCE FILE in `$RALPHD_TIMEOUT_EVIDENCE_DIR` saying which step's clock
# ran out. It also clamps each step's bound down to the job's own remaining
# `timeout-minutes` budget, so a job that is genuinely running out of wall
# clock always trips one of those inner bounds before the runner's job-level
# kill can land.
#
# This script runs as each job's LAST step under `if: always()` -- Actions
# executes those on a cancelled job as well as a passing or failing one --
# and answers the one question the Checks/Actions API cannot: of the three
# causes that all report `cancelled`, which one happened here.
#
# It reads no exit status of any kind, from this step or any other -- not
# `run_bounded.sh`'s own, and not the raw signal-shaped status (a bare 137,
# say) a command that needed escalating from TERM to KILL can leave behind
# (task 004, K.3). The evidence file is written before that escalation ever
# starts, so by the time this script runs the fact it reads is already
# durable and the exit code that got the command to stop is irrelevant to
# it -- there is no code path here that inspects one at all.
#
# It answers it from two facts, and from NO clock inference at all:
#
#   * `$RALPHD_TIMEOUT_EVIDENCE_DIR` holds at least one `*.expired` file
#     => this job's wall clock ran out inside a bounded step. That is a
#     timeout, and the marker says so:
#
#       RALPHD_TIMEOUT_KILL: <label> ran out of wall clock in <step> ...
#
#   * the job's own status (passed in as `${{ job.status }}`) is `cancelled`
#     and there is NO such file => nothing in this job ran out of wall
#     clock, so the ending is a human cancel or a `cancel-in-progress`
#     supersession:
#
#       RALPHD_CANCEL_NOT_TIMEOUT: <label> was cancelled with no wall-clock
#       bound expiry recorded ...
#
# WHY THIS IS NOT AN ELAPSED-VERSUS-BOUND COMPARISON ANY MORE. It was, and
# that was wrong: a cancel arriving inside a grace window of the declared
# bound (measured: a human cancel 44 minutes into a 45-minute bound) is
# indistinguishable from a timeout by elapsed time alone, so the comparison
# reported cancellations as timeouts precisely in the window where getting it
# right matters most. Elapsed seconds are still PRINTED, as context for a
# reader, but nothing is classified from them: the discriminator is a fact
# one cause produces and the other cannot forge.
#
# Two deliberate properties:
#
#   * It never changes the job's outcome. It exits 0 whatever it finds
#     (including when its inputs are missing, which it reports on stderr): a
#     classifier that could turn a green job red would be a worse trade than
#     the ambiguity it removes.
#   * It never claims a cause it cannot support. With no evidence directory
#     exported it prints NEITHER line and says why on stderr, rather than
#     defaulting to one of the two answers.
#
# Usage:
#   scripts/job_timeout_marker.sh <job-timeout-minutes> <label> [<job-status>]
#
# Env:
#   RALPHD_TIMEOUT_EVIDENCE_DIR  the per-job directory the first step creates
#   RALPHD_JOB_STARTED_AT        epoch seconds, written by the job's first step
#   GITHUB_STEP_SUMMARY          appended to when set
#
# See tests/test_run_bounded_timeout_marker.py for both halves of both
# scripts, exercised directly and never against a live dispatched run.

set -uo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: $0 <job-timeout-minutes> <label> [<job-status>]" >&2
  exit 0
fi

BOUND_MINUTES="$1"
LABEL="$2"
JOB_STATUS="${3:-}"

case "$BOUND_MINUTES" in
  ''|*[!0-9]*)
    echo "job-timeout-minutes must be a positive integer, got: $BOUND_MINUTES" >&2
    exit 0
    ;;
esac

started="${RALPHD_JOB_STARTED_AT:-}"
case "$started" in
  ''|*[!0-9]*)
    echo "RALPHD_JOB_STARTED_AT is unset or not an epoch, reporting elapsed as unknown" >&2
    elapsed="unknown"
    ;;
  *)
    elapsed="$(( $(date +%s) - started ))s"
    ;;
esac

emit() {
  echo "$1"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "$1" >>"$GITHUB_STEP_SUMMARY"
  fi
}

evidence_dir="${RALPHD_TIMEOUT_EVIDENCE_DIR:-}"
if [ -z "$evidence_dir" ]; then
  echo "RALPHD_TIMEOUT_EVIDENCE_DIR is unset: this ending cannot be classified, so no marker is printed either way" >&2
  exit 0
fi

expired=""
for f in "$evidence_dir"/*.expired; do
  [ -e "$f" ] || continue
  step="$(sed -n 's/^label=//p' "$f" | head -1)"
  bound="$(sed -n 's/^effective_bound_s=//p' "$f" | head -1)"
  [ -n "$step" ] || step="$(basename "$f" .expired)"
  entry="${step} (bound ${bound:-unknown}s)"
  if [ -z "$expired" ]; then
    expired="$entry"
  else
    expired="${expired}, ${entry}"
  fi
done

if [ -n "$expired" ]; then
  emit "RALPHD_TIMEOUT_KILL: ${LABEL} ran out of wall clock in ${expired}, inside its job-level bound of ${BOUND_MINUTES} minutes (elapsed ${elapsed})"
  exit 0
fi

if [ "$JOB_STATUS" = "cancelled" ]; then
  emit "RALPHD_CANCEL_NOT_TIMEOUT: ${LABEL} was cancelled with no wall-clock bound expiry recorded, so this is a human cancel or a cancel-in-progress supersession, not a timeout-minutes kill (job-level bound ${BOUND_MINUTES} minutes, elapsed ${elapsed})"
  exit 0
fi

if [ -z "$JOB_STATUS" ]; then
  echo "no job status was passed, so a cancelled ending cannot be reported as a cancel; no wall-clock expiry evidence was found either" >&2
fi

exit 0
