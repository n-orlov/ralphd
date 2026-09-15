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
# not a verdict"); this script is the half of the mechanism that produces
# the POSITIVE fact the other half classifies on.
#
# The mechanism: wrap every step that can consume real wall clock in this
# repo's own inner `timeout(1)` bound, set SHORTER than the job's own
# `timeout-minutes`. If the wrapped command overruns that inner bound, this
# script -- not the runner's job-level timeout -- is what kills it, with
# time left to report before the job-level bound could ever fire. It then
# does two things:
#
#   1. prints, and (under Actions) appends to `$GITHUB_STEP_SUMMARY`, a
#      single fixed, greppable marker line:
#
#        RALPHD_TIMEOUT_KILL: <label> exceeded its internal bound of <N>s
#
#      and exits 1 -- an ordinary step `failure`, never `cancelled`;
#   2. writes one EVIDENCE FILE into `$RALPHD_TIMEOUT_EVIDENCE_DIR` (the
#      job's first step creates that directory fresh and exports it). That
#      file is the durable, positive record that this job's wall clock
#      genuinely ran out, and it is the only thing
#      `scripts/job_timeout_marker.sh` will call a timeout on. Nothing else
#      in this repo writes one.
#
# Why the evidence file rather than a clock comparison: this script runs the
# wrapped command in the background beside its OWN bound timer (a plain
# `sleep`, also backgrounded) and blocks on whichever of the two finishes
# first. If the command wins, that is an ordinary completion (or an outside
# signal that reached the command directly), never a self-inflicted timeout,
# and its real exit status is propagated untouched. If the timer wins, the
# bound has expired -- and the marker line and the `*.expired` evidence file
# are both written IMMEDIATELY, before this script sends the command so much
# as a TERM. Only once that record is durable does it escalate: TERM first,
# then (task 004, K.3) KILL after `KILL_AFTER_S` if the command ignores TERM.
# A command that dies to the first TERM and one that needs the KILL are
# therefore reported identically -- both already recorded, both exit 1 -- so
# a TERM-ignoring child can no longer hide a genuine expiry behind whatever
# raw signal finally worked. A cancel delivered from outside this script's
# own bound -- a human, or `cancel-in-progress` -- lands on THIS SCRIPT
# itself (it has no trap, so the shell's default disposition applies) or on
# the command directly, before the bound's own timer could ever fire, so
# neither the marker nor the evidence file is ever written and the script
# dies by that raw signal (128+signum) -- including when that outside cancel
# itself needed an outside escalation from TERM to KILL: this script never
# runs its own escalation for a bound that has not expired, so there is
# nothing here that could turn that KILL into a recorded timeout either. The
# two causes are therefore separated by a fact each one either did or did not
# produce, not by how close to the deadline the ending happened to be, and
# not by which raw signal happened to finish the command off.
#
# THE REMAINING-BUDGET CLAMP. A step's declared inner bound is an upper
# bound, not a promise: if the steps before it were slow, its declared
# bound can still outlive the job's own `timeout-minutes`, and then the
# runner's job-level kill lands first and the ending is ambiguous again.
# So when the job exports both `RALPHD_JOB_STARTED_AT` and
# `RALPHD_JOB_BOUND_MINUTES`, this script clamps its effective bound down to
# whatever wall clock the job has left minus a tail
# (`RALPHD_JOB_BOUND_TAIL_S`, default 120s, which also pays for the gap
# between the job's real start and the stamp). Whichever wrapped step is
# running when the job's budget runs out therefore expires FIRST, from the
# inside, leaving the marker and the evidence file -- so a wall-clock
# exhaustion reports as a `failure` that says why, and the job-level
# `cancelled` ending is left to mean cancel.
#
# See tests/test_run_bounded_timeout_marker.py for both halves, exercised
# directly against these scripts (never against a live dispatched workflow
# run -- that would need a >45-minute wait to prove the negative half).
#
# Usage:
#   scripts/run_bounded.sh <inner-timeout-seconds> <label> -- <command...>
#
# `<label>` is free text (quote it if it has spaces) and appears verbatim in
# the marker line and in the evidence file, so a job with more than one
# wrapped step can tell them apart without re-deriving which step's command
# was running.
#
# Env:
#   RALPHD_TIMEOUT_EVIDENCE_DIR  where to write the expiry evidence file
#   RALPHD_JOB_STARTED_AT        epoch seconds, stamped by the job's first step
#   RALPHD_JOB_BOUND_MINUTES     the job's own `timeout-minutes`
#   RALPHD_JOB_BOUND_TAIL_S      clamp tail in seconds (default 120)
#   GITHUB_STEP_SUMMARY          appended to when set

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

DECLARED_TIMEOUT_S="$INNER_TIMEOUT_S"

# The remaining-budget clamp (see the header). Both inputs must be usable
# integers; anything else leaves the declared bound alone rather than
# guessing.
job_started="${RALPHD_JOB_STARTED_AT:-}"
job_bound_minutes="${RALPHD_JOB_BOUND_MINUTES:-}"
tail_s="${RALPHD_JOB_BOUND_TAIL_S:-120}"
case "$job_started$job_bound_minutes$tail_s" in
  *[!0-9]*) ;;
  '') ;;
  *)
    if [ -n "$job_started" ] && [ -n "$job_bound_minutes" ] && [ -n "$tail_s" ]; then
      remaining=$(( job_bound_minutes * 60 - ( $(date +%s) - job_started ) - tail_s ))
      if [ "$remaining" -lt 1 ]; then
        remaining=1
      fi
      if [ "$remaining" -lt "$INNER_TIMEOUT_S" ]; then
        echo "clamping ${LABEL}'s inner bound from ${INNER_TIMEOUT_S}s to ${remaining}s: the job's ${job_bound_minutes}-minute bound has less left than that" >&2
        INNER_TIMEOUT_S="$remaining"
      fi
    fi
    ;;
esac

# KILL_AFTER_S is the grace period this script itself gives the command
# between TERM and KILL once the bound has expired (task 004, K.3): the
# expiry is recorded before either signal goes out, so which of the two
# ends up killing the command changes nothing about how this run is
# reported.
KILL_AFTER_S=5

record_expiry() {
  marker="RALPHD_TIMEOUT_KILL: ${LABEL} exceeded its internal bound of ${INNER_TIMEOUT_S}s"
  echo "$marker"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "$marker" >>"$GITHUB_STEP_SUMMARY"
  fi
  evidence_dir="${RALPHD_TIMEOUT_EVIDENCE_DIR:-}"
  if [ -n "$evidence_dir" ]; then
    if mkdir -p "$evidence_dir" 2>/dev/null; then
      slug="$(printf '%s' "$LABEL" | tr -c 'A-Za-z0-9._-' '-')"
      {
        echo "label=${LABEL}"
        echo "effective_bound_s=${INNER_TIMEOUT_S}"
        echo "declared_bound_s=${DECLARED_TIMEOUT_S}"
        echo "expired_at=$(date +%s)"
      } >"${evidence_dir}/${slug}.expired" 2>/dev/null ||
        echo "could not write timeout evidence into ${evidence_dir}" >&2
    else
      echo "could not create the timeout evidence directory ${evidence_dir}" >&2
    fi
  else
    echo "RALPHD_TIMEOUT_EVIDENCE_DIR is unset: this expiry leaves no evidence for the job-level classifier" >&2
  fi
}

# Run the command and this script's own bound timer side by side, and block
# on whichever finishes first. No trap is installed on this script for TERM
# or INT: a cancel delivered from outside (a human, or `cancel-in-progress`)
# hits this shell's default disposition -- or the command directly -- and
# this script dies by that raw signal before the code below ever runs,
# which is exactly why that path records neither marker nor evidence however
# close to the bound it lands, and whatever escalation the OUTSIDE canceller
# itself needed to reach TERM to KILL.
# `kill_tree` reaches every descendant of a pid THIS SCRIPT ITSELF spawned
# (walked via `pgrep -P`, never a name pattern), not just that one pid: a
# wrapped command that is a script spawning its own children (`sleep`,
# a real subprocess, ...) would otherwise leave a grandchild behind when
# only its direct child is signalled, which then keeps this step's own
# stdout pipe open past the point this script has already reported an
# ending. Children are signalled before their parent (post-order) so none
# of them can become an orphan mid-walk.
kill_tree() {
  sig="$1"
  pid="$2"
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$sig" "$child"
  done
  kill -s "$sig" "$pid" 2>/dev/null
}

"$@" &
cmd_pid=$!

( exec sleep "$INNER_TIMEOUT_S" ) &
timer_pid=$!

wait -n "$cmd_pid" "$timer_pid" 2>/dev/null

if kill -0 "$cmd_pid" 2>/dev/null; then
  # The timer won: the bound has expired while the command is still
  # running. Record it now, before sending the command anything at all.
  record_expiry

  kill_tree TERM "$cmd_pid"
  waited=0
  while [ "$waited" -lt "$KILL_AFTER_S" ] && kill -0 "$cmd_pid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$cmd_pid" 2>/dev/null; then
    kill_tree KILL "$cmd_pid"
  fi
  wait "$cmd_pid" 2>/dev/null
  kill "$timer_pid" 2>/dev/null
  wait "$timer_pid" 2>/dev/null
  # Documented failure status, whichever signal the command actually died
  # to -- the recorded expiry above is what this run is reported from, not
  # the raw exit code of a TERM vs. a KILL.
  exit 1
fi

# The command finished on its own before the bound's timer fired: an
# ordinary completion, never a timeout this script inflicted. Propagate its
# real exit status untouched.
status=0
wait "$cmd_pid" 2>/dev/null || status=$?
kill "$timer_pid" 2>/dev/null
wait "$timer_pid" 2>/dev/null
exit "$status"
