#!/usr/bin/env bash
# ci/jenkins-lane-entry.sh <command...> -- the first thing a lane container
# runs (ci/jenkins-lane.sh starts it as root). It makes sure an account
# with CI_UID and CI_GID exists (a `ci` one when the image has none), hands
# the lane's directory (CI_LANE_ROOT) to it, adds the docker socket's group
# when CI_DOCKER_GID is set, and runs the command as that account with a
# home of its own.
set -euo pipefail

uid=${CI_UID:?}; gid=${CI_GID:?}
getent group "$gid" >/dev/null || groupadd -o -g "$gid" ci
if ! getent passwd "$uid" >/dev/null; then
  useradd -o -u "$uid" -g "$gid" -m -d /home/ci -s /bin/bash ci
fi
user=$(getent passwd "$uid" | cut -d: -f1)
home=$(getent passwd "$uid" | cut -d: -f6)
mkdir -p "$home"
chown "$uid:$gid" "$home"
if [ -n "${CI_LANE_ROOT:-}" ]; then
  chown -R "$uid:$gid" "$CI_LANE_ROOT"
fi

groups=$gid
if [ -n "${CI_DOCKER_GID:-}" ]; then
  groups="$groups,$CI_DOCKER_GID"
fi
exec setpriv --reuid="$uid" --regid="$gid" --groups="$groups" \
  env HOME="$home" USER="$user" LOGNAME="$user" "$@"
