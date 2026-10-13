#!/usr/bin/env bash
# Prints, on stdout, the HOST-side absolute path that backs a given absolute
# path INSIDE THIS running container -- resolved by asking the docker daemon
# itself for this container's own `Mounts` table, never assumed.
#
# Task 012 (requirement I.2): GitHub Actions' self-hosted `container:`
# runtime does NOT bind-mount `$GITHUB_WORKSPACE` at an identical host path
# -- measured directly (a CI run 35163222049, job 105018454092: the
# runner mounts its own `_work` directory at `/__w` inside the job's
# container regardless of the runner host's own absolute path, so
# `docker run -v "$GITHUB_WORKSPACE":/check:ro ...` mounted an empty
# directory and the assertion step that caught it failed loudly). The one
# thing every self-hosted `container:` job's own docker socket can always
# answer, with no runner-specific path hardcoded anywhere, is "what host
# path backs a given path inside me" -- this script asks exactly that, by
# inspecting its own container's `Mounts` (each: `{Source, Destination}`,
# `Source` already the HOST'S OWN path -- the same field `docker inspect`
# reports for any container, sibling or self) and picking the entry whose
# `Destination` most specifically covers the requested path, then
# appending whatever tail of the requested path falls past that
# `Destination` (a workspace one directory level under the runner's own
# `_work` mount, as `$GITHUB_WORKSPACE` always is, has no `Destination`
# entry of its own -- only its ancestor `/__w` does).
set -euo pipefail

container_path="${1:?usage: resolve_container_host_path.sh <absolute-path-inside-this-container>}"

# Docker sets a new container's hostname to its own short container id
# unless something overrides it (`--hostname`, `--network host`) -- neither
# applies to a GitHub Actions self-hosted `container:` job, which always
# creates its own bridge network per job (see the `docker network create`
# line preceding every job's own container in its own log).
self_id="$(cat /etc/hostname)"

python3 - "$self_id" "$container_path" <<'PYEOF'
import json
import subprocess
import sys

self_id, container_path = sys.argv[1], sys.argv[2]
out = subprocess.check_output(
    ["docker", "inspect", "--format", "{{json .Mounts}}", self_id]
)
mounts = json.loads(out)

best = None
for m in mounts:
    dest = m["Destination"].rstrip("/")
    if container_path == dest or container_path.startswith(dest + "/"):
        if best is None or len(dest) > len(best["Destination"].rstrip("/")):
            best = m

if best is None:
    sys.exit(
        f"no mount on container {self_id!r} covers {container_path!r} -- "
        f"mounts were {mounts!r}"
    )

dest = best["Destination"].rstrip("/")
rel = container_path[len(dest):]
print(best["Source"] + rel)
PYEOF
