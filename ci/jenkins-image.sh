#!/usr/bin/env bash
# ci/jenkins-image.sh <browser|docker-tier> -- build one lane image on the
# Jenkins agent and print its tag.
#
# The tag is ralphd-<name>-ci:<hash>, where <hash> is taken from the git blob
# ids of everything tracked under ci/<name>/ (the Dockerfile is the image's
# only input), so an unchanged directory reuses the image the agent already
# has and an edited one builds a new one. Nothing is pulled from or pushed to
# a registry except the pinned base images. Build output goes to stderr;
# stdout is the tag alone, for the Jenkinsfile to read.
set -euo pipefail

name=${1:?"usage: ci/jenkins-image.sh <browser|docker-tier>"}
dir="ci/$name"
[ -f "$dir/Dockerfile" ] || { echo "ci/jenkins-image.sh: no $dir/Dockerfile" >&2; exit 2; }

hash=$(git ls-files -s -- "$dir" | sha256sum | cut -c1-16)
tag="ralphd-$name-ci:$hash"

if docker image inspect "$tag" >/dev/null 2>&1; then
  echo "ci/jenkins-image.sh: $tag already built" >&2
else
  echo "ci/jenkins-image.sh: building $tag from $dir" >&2
  docker build --pull -t "$tag" \
    --label "org.opencontainers.image.source=https://github.com/n-orlov/ralphd" \
    "$dir" >&2
fi
echo "$tag"
