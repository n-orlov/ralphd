---
name: toolchain-sibling
description: Run build/test work that needs a toolchain the job image lacks (Go, Rust, a JDK, tmux, a database) in a sibling container on the host docker daemon, with the host workspace bind-mounted. Use whenever a required tool is missing and `apt-get` is not available.
---

# Toolchain in a sibling container

The ralphd job image is deliberately thin — Python and Node only — and the agent
runs as non-root `agent`, so a missing toolchain cannot be installed in place.
Do not try. Instead build a small image that has the toolchain and run each
command in a throwaway **sibling** container with the workspace bind-mounted.

Requires the job to have been started with `ralphctl start --allow-docker` (the
prompt carries a "Docker siblings" section listing the host paths when it was).
Without that there is no docker socket and this skill does not apply.

## 1. Commit the two files to the target repo

Both belong in the repo being worked on, not in ralphd, so the setup is
reproducible by a human or a later job without this agent.

`ci/Dockerfile` — a base image plus *just* the toolchain this repo needs, e.g.:

```dockerfile
FROM golang:1.25-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
        tmux ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
ENV GOMODCACHE=/cache/gomod GOCACHE=/cache/gobuild
```

`ci/run.sh` — copy `run.sh` from this skill directory verbatim and edit the two
names at the top (image tag, cache volume). It takes the command to run:

```bash
ci/run.sh go build ./...
ci/run.sh go test ./...
ci/run.sh bash -c 'tmux -L t new-session -d -s s "./app"; sleep 1; tmux -L t capture-pane -p -t s'
```

## 2. Build the image, once per iteration is fine (layers cache)

```bash
docker build -t "$CI_IMAGE" --label "ralphd.run=$RALPHD_RUN_ID" ci/
```

## 3. The six rules that make this work

1. **Host paths only.** A sibling's `-v` source is resolved by the *host*
   daemon. Use `$RALPHD_HOST_WORKSPACE` (or the per-name paths in
   `$RALPHD_HOST_WORKSPACES`), never this container's `/workspace` — that
   mounts an empty directory and the failure looks like "my files vanished".
2. **`--user 1000:1000`.** This container's `agent` and the host user are both
   uid 1000. A root sibling leaves root-owned files in the workspace that you
   can then neither edit nor delete.
3. **A named cache volume.** Mount one on the toolchain's download/build dirs
   and point the cache env vars at it, or every run re-downloads its
   dependencies. Name it after the repo + toolchain (`myrepo-gocache`) so runs
   and later jobs share it, and do **not** label it with the run id or gate its
   use on `$RALPHD_RUN_ID` — that would break the very next run of the same
   script. If you genuinely want a per-run volume, name it per run *and*
   `docker volume rm` it before the job finishes.
4. **Label every sibling with BOTH labels** — `ralphd.run=$RALPHD_RUN_ID`
   *and* `ralphd.role=sibling` — and prefer `--rm`, so `ralphctl stop`/`rm`
   reap them (containers) and the operator can find the rest (images). Label
   built images with the run label too.
5. **Never clean up by the run label alone.** The job container the agent runs
   inside carries `ralphd.run=$RALPHD_RUN_ID` as well (plus
   `ralphd.role=job`), so
   `docker rm -f $(docker ps -aq --filter label=ralphd.run=$RALPHD_RUN_ID)`
   deletes the run itself: the agent dies mid-iteration, the iteration's work
   and transcript are lost, and the run dir is left non-terminal. Always add
   the role filter so the query can only match siblings:

   ```bash
   # list siblings
   docker ps -aq --filter "label=ralphd.run=$RALPHD_RUN_ID" \
                 --filter label=ralphd.role=sibling
   # remove siblings only
   docker rm -f $(docker ps -aq --filter "label=ralphd.run=$RALPHD_RUN_ID" \
                                --filter label=ralphd.role=sibling)
   ```

   `$RALPHD_SELF_CONTAINER_ID` names this job's own container: never
   `stop`/`rm`/`kill` it. You do not have to reap anything at the end anyway —
   tearing the run down is `ralphctl`'s job (`ralphctl stop`/`rm` on the host
   may filter on the run label alone, precisely because there it *should* take
   the job container with it). Only remove siblings you are done with mid-run,
   with the two-filter form above.
6. **No credentials.** Siblings get the default bridge network and normal
   internet (image pulls, dependency downloads) whatever network the job
   container is on. They do not need the job's LLM gateway access or any
   credential file — do not pass them in.

## 4. Verified working this way

Go 1.25 `go build`/`go test`; real `tmux` 3.5a on a private `-L` socket with
`capture-pane` reading frames back; a bubbletea TUI spawned in a pty, driven
with keystrokes and asserted on its rendered frames; and immediate,
bidirectional file visibility between the sibling and `/workspace`.

Tests that need a pty or a terminal-sized window run in the sibling too — that
is the point. Keep the sibling's command a single non-interactive invocation so
its exit code is the test result.
