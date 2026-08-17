#!/usr/bin/env bash
# Run one command in a sibling container that has the toolchain this image
# lacks. Copy into the target repo as ci/run.sh and edit the two names below.
#
#   ci/run.sh go test ./...
#
# Siblings run on the HOST docker daemon, so every -v source must be a HOST
# path ($RALPHD_HOST_WORKSPACE), and the container must run as uid 1000 or it
# litters the workspace with root-owned files.
set -euo pipefail

IMAGE="${CI_IMAGE:-myrepo-ci}"
# Deliberately shared across runs and jobs: the cache is the whole point, and a
# run-id-scoped name (or a run-id check) breaks every subsequent run.
CACHE_VOL="${CI_CACHE_VOL:-myrepo-ci-cache}"

WS="${RALPHD_HOST_WORKSPACE:-$PWD}"   # host path, not this container's view
LABEL=()
[ -n "${RALPHD_RUN_ID:-}" ] && LABEL=(--label "ralphd.run=$RALPHD_RUN_ID")

docker volume create "$CACHE_VOL" >/dev/null   # idempotent, unlabeled on purpose

exec docker run --rm --user 1000:1000 "${LABEL[@]}" \
  -v "$WS:/workspace" -w /workspace \
  -v "$CACHE_VOL:/cache" \
  -e HOME=/tmp -e GOMODCACHE=/cache/gomod -e GOCACHE=/cache/gobuild \
  "$IMAGE" "$@"
