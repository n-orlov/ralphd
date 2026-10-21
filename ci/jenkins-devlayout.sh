#!/usr/bin/env bash
# ci/jenkins-devlayout.sh <cell-image> <exported|unset> -- one nightly
# dev-layout cell (docs/testing.md): scripts/host_like_dev_layout.sh in a bare
# <cell-image> container as uid 1000, with PYTHONUSERBASE <mode>, from a
# read-only copy of the commit under test.
#
# Runs on the agent itself. The cells share one checkout, .lanes/devlayout/src
# (made by the first cell, ci/jenkins-lane.sh's way); each cell copies it
# inside its own container, writes its Allure results to
# .lanes/devlayout/results/<cell>/ and its run_bounded evidence to
# .lanes/devlayout/evidence/. CELL_MINUTES (default 120) is the cell's
# Jenkins timeout, which scripts/run_bounded.sh clamps to.
set -euo pipefail

image=${1:?"usage: ci/jenkins-devlayout.sh <cell-image> <exported|unset>"}
mode=${2:?"usage: ci/jenkins-devlayout.sh <cell-image> <exported|unset>"}
ws=${WORKSPACE:-$PWD}
root="$ws/.lanes/devlayout"
src="$root/src"
slug=$(printf '%s-%s' "$image" "$mode" | tr -c 'A-Za-z0-9_.-' '-')
results="$root/results/$slug"

# The cells start in parallel: whoever takes the lock first makes the checkout.
mkdir -p "$root/evidence" "$root/results"
(
  flock 9
  if [ ! -d "$src/.git" ]; then
    commit=${GIT_COMMIT:-$(git -C "$ws" rev-parse HEAD)}
    # --no-hardlinks, as in ci/jenkins-lane.sh: the lanes chown their trees.
    git clone --quiet --local --no-hardlinks --no-checkout "$ws" "$src"
    git -C "$src" checkout --quiet --detach "$commit"
  fi
) 9>"$root/.checkout.lock"
rm -rf "$results"
mkdir -p "$results"

export RALPHD_JOB_STARTED_AT
RALPHD_JOB_STARTED_AT=$(date +%s)
export RALPHD_JOB_BOUND_MINUTES=${CELL_MINUTES:-120}
export RALPHD_TIMEOUT_EVIDENCE_DIR="$root/evidence"
export CELL_IMAGE=$image CELL_MODE=$mode WS=$src
tag=$(printf '%s' "${BUILD_TAG:-local}" | tr -c 'A-Za-z0-9_.-' '-')
name="ralphd-ci-$tag-cell-$slug"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; exit 143' TERM INT

pulled=0
for attempt in 1 2 3; do
  if "$src/scripts/run_bounded.sh" 300 "cell image pull" -- docker pull -q "$CELL_IMAGE"; then
    pulled=1
    break
  fi
  echo "pull attempt $attempt failed; retrying" >&2
  sleep 10
done
[ "$pulled" -eq 1 ] || { echo "could not pull $CELL_IMAGE" >&2; exit 1; }

"$src/scripts/run_bounded.sh" 6300 "dev-layout cell" -- docker run --rm --cpus="2" \
  --name "$name" --label "ralphd.ci=${BUILD_TAG:-local}" \
  -e HOST_LIKE_DEV_LAYOUT_ALLURE_DIR=/allure-results \
  -v "$results:/allure-results" \
  -e PIP_DEFAULT_TIMEOUT=60 \
  -e PIP_RETRIES=10 \
  -e HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR=/pip-cache \
  -v ci-pip-cache:/pip-cache \
  -e HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE="$CELL_MODE" \
  -e CELL_IMAGE="$CELL_IMAGE" \
  -v "$WS:/src-ro:ro" \
  "$CELL_IMAGE" \
  bash -c '
    set -euo pipefail
    cp -a /src-ro /src
    apt-get update -qq
    apt-get install -y -qq git curl >/dev/null
    if [ "$CELL_IMAGE" = "ubuntu:24.04" ]; then
      apt-get install -y -qq python3 python3-pip >/dev/null
    fi
    getent passwd 1000 >/dev/null || useradd -u 1000 -m -s /bin/bash dev
    CELL_USER="$(getent passwd 1000 | cut -d: -f1)"
    CELL_HOME="$(getent passwd 1000 | cut -d: -f6)"
    CELL_GID="$(getent passwd 1000 | cut -d: -f4)"
    mkdir -p "$CELL_HOME"
    chown -R 1000:"$CELL_GID" "$CELL_HOME" /src /pip-cache /allure-results
    cd /src
    setpriv --reuid=1000 --regid="$CELL_GID" --init-groups env HOME="$CELL_HOME" scripts/host_like_dev_layout.sh
  '
