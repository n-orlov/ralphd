#!/usr/bin/env bash
# ci/jenkins-lane.sh <lane> <minutes> <image> [docker run options...] -- <command...>
#
# Run one CI lane of the Jenkinsfile in its own container on the agent.
#
#   * The lane gets its own checkout of the commit under test at
#     .lanes/<lane>/src (a local clone of the workspace), so lanes running in
#     parallel never share a .venv, an egg-info or a test's litter.
#   * .lanes/<lane> is mounted at its own host path and the command runs in
#     src/, so a sibling container the lane starts resolves every bind mount
#     under it (the docker tier needs that).
#   * The command runs as uid/gid 1000, as the tiers always have
#     (ci/jenkins-lane-entry.sh hands .lanes/<lane> to it and creates a `ci`
#     account when the image has none): several tests expect a uid-1000
#     account, the agent identity a ralphd job image carries. The agent's own
#     uid gets the files back from ci/jenkins-reap.sh.
#   * <minutes> is the lane's Jenkins stage timeout. It is stamped as
#     RALPHD_JOB_STARTED_AT / RALPHD_JOB_BOUND_MINUTES, so scripts/run_bounded.sh
#     clamps every inner bound to what is left of it, and its expiry evidence
#     lands in .lanes/<lane>/evidence (ci/jenkins-report.sh lists it).
#   * The container is labelled ralphd.ci=$BUILD_TAG; the Jenkinsfile removes
#     whatever is left with that label when the build ends, aborts included.
#
# Extra options before `--` go to `docker run` as they are (CPU, memory and
# pid limits, the docker socket).
set -euo pipefail

lane=${1:?usage}; minutes=${2:?usage}; image=${3:?usage}; shift 3
opts=()
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do opts+=("$1"); shift; done
[ "${1:-}" = "--" ] || { echo "ci/jenkins-lane.sh: missing -- before the command" >&2; exit 2; }
shift
[ "$#" -gt 0 ] || { echo "ci/jenkins-lane.sh: no command" >&2; exit 2; }

ws=${WORKSPACE:-$PWD}
commit=${GIT_COMMIT:-$(git -C "$ws" rev-parse HEAD)}
root="$ws/.lanes/$lane"
rm -rf "$root"
mkdir -p "$root/evidence"
git clone --quiet --local --no-checkout "$ws" "$root/src"
git -C "$root/src" checkout --quiet --detach "$commit"

tag=$(printf '%s' "${BUILD_TAG:-local}" | tr -c 'A-Za-z0-9_.-' '-')
name="ralphd-ci-$tag-$lane"
docker_gid=""
if [ -S /var/run/docker.sock ]; then
  docker_gid=$(stat -c %g /var/run/docker.sock)
fi

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap 'cleanup; exit 143' TERM INT

echo "lane $lane: $image, ${opts[*]:-no limits}, bound ${minutes} min, commit $commit"
docker run --rm --init --name "$name" --label "ralphd.ci=${BUILD_TAG:-local}" \
  --user 0:0 \
  -v "$root:$root" -w "$root/src" \
  -e CI_UID=1000 -e CI_GID=1000 -e CI_DOCKER_GID="$docker_gid" -e CI_LANE_ROOT="$root" \
  -e RALPHD_JOB_STARTED_AT="$(date +%s)" -e RALPHD_JOB_BOUND_MINUTES="$minutes" \
  -e RALPHD_TIMEOUT_EVIDENCE_DIR="$root/evidence" \
  -e ALLURE_RUNNER_IMAGE="$image" \
  -e GIT_COMMIT="$commit" -e BRANCH_NAME -e JOB_NAME -e BUILD_NUMBER -e BUILD_URL \
  -e RALPHD_RUN_ID \
  "${opts[@]}" \
  "$image" bash "$root/src/ci/jenkins-lane-entry.sh" "$@" &
wait $!
