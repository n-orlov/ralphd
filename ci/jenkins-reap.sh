#!/usr/bin/env bash
# ci/jenkins-reap.sh -- hand the whole Jenkins workspace back to the agent's
# account. The docker tier's sibling containers and the nightly dev-layout
# cells write as root or as uid 1000 into it; without this, `git clean` and
# Jenkins' own workspace cleanup cannot remove what they left. Root inside a
# throwaway container is the only identity that can, so this runs one, from
# the same pinned image the fast lane uses. Also removes any container this
# build (BUILD_TAG) or its docker tier (RALPHD_RUN_ID) left running.
set -euo pipefail

ws=${WORKSPACE:-$PWD}
image=${PY_IMAGE:?"PY_IMAGE (the pinned python image) is not set"}

for label in ${BUILD_TAG:+"ralphd.ci=$BUILD_TAG"} ${RALPHD_RUN_ID:+"ralphd.run=$RALPHD_RUN_ID"}; do
  ids=$(docker ps -aq --filter "label=$label")
  if [ -n "$ids" ]; then
    echo "removing containers labelled $label"
    # shellcheck disable=SC2086
    docker rm -f $ids >/dev/null || true
  fi
done

docker run --rm --user 0:0 -v "$ws:/reap" "$image" \
  chown -R "$(id -u):$(id -g)" /reap
