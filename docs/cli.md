# ralphctl — CLI Reference

`ralphctl` is the single entry point for operating ralphd jobs. It is designed to be
driven by a **human at a terminal or by an AI agent** — hence:

- every command supports `--json` for machine-readable output (stable schema);
  default output is human-oriented tables/text
- exit codes are meaningful and documented per command
- no command is interactive unless explicitly marked; prompts are suppressed with
  `--yes` or when stdout is not a TTY
- all state is derivable from `~/.ralphd/` + the container APIs; there is no hidden
  session state

Install: `pipx install ralphctl` (or `uvx ralphctl …`). Requires a working
`docker` CLI (or `podman` with `RALPHD_DOCKER=podman`).

## Quick start

```bash
# Run a job against an existing checkout, forwarding the host's LLM config
ralphctl start --prd ./feature.md --workspace ~/src/widget --llm host

# Watch it
ralphctl watch brisk-otter-1408

# Nudge it
ralphctl steer brisk-otter-1408 "Skip the docs task; focus on tests"

# Collect results when done (container idles by default)
ralphctl status brisk-otter-1408
ralphctl artifacts pull brisk-otter-1408 ./out/
ralphctl stop brisk-otter-1408
```

## Global flags

| Flag | Meaning |
|------|---------|
| `--json` | machine-readable output on stdout, logs to stderr |
| `--quiet` | suppress non-essential output |
| `--registry <dir>` | override `~/.ralphd` |
| `--yes` | assume yes on confirmations |

Global exit codes: `0` success · `1` generic error · `2` usage error ·
`3` run not found · `4` container/API unreachable · `5` operation invalid in
current job state.

## Commands

### `ralphctl start`

Create and launch a job container. Prints the run ID (and with `--json`: run ID,
container ID, API URL, token presence).

```
ralphctl start --prd <file|-> [options]
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--prd <file\|->` | required | PRD markdown (`-` = stdin) |
| `--workspace <dir>` | none | bind-mount an existing checkout at `/workspace`; without it the agent clones PRD-listed repos into the run dir |
| `--run-id <id>` | generated | explicit run ID |
| `--iterations <n>` | 25 | shared iteration budget |
| `--max-approaches <n>` | 3 | review-loop approach limit |
| `--vigilant` | off | per-task verification |
| `--model <id>` | profile default | default model (pi model ID) |
| `--model-strategy <s>` | quality-first | `quality-first\|cost-optimized\|balanced\|custom` |
| `--model-<phase> <id>` | — | per-phase override (`planning\|worker\|review\|verify`) |
| `--llm <profile>` | `host` | LLM profile ([llm-profiles.md](llm-profiles.md)) |
| `--llm-env KEY=VAL` | — | ad-hoc env additions to the LLM config (repeatable) |
| `--forward-env NAME\|PREFIX_*` | — | forward host env var(s) into the container, by exact name or prefix glob (repeatable). Required for any non-standard vars — see [llm-profiles.md](llm-profiles.md) |
| `--skills <dir>` | — | mount a skills directory (repeatable) |
| `--creds <dir>` | — | mount a credentials directory (see below) |
| `--allow-docker` | off | mount the host docker socket into the job container — **root-equivalent host access**, see below |
| `--prompt-override <dir>` | — | phase-prompt override directory |
| `--image <ref>` | bundled default | alternative/derived engine image |
| `--on-complete idle\|exit` | idle | post-completion behavior |
| `--timeout <dur>` | 8h | job wall-clock limit (`45m`, `8h`, `2d`) |
| `--iteration-timeout <dur>` | 45m | per-iteration limit |
| `--port <n>` | auto | host port for the API |
| `--api-bind <addr>` | 127.0.0.1 | host interface to publish on |
| `--api-token <t\|auto>` | none | require bearer auth (`auto` generates + stores) |
| `--env KEY=VAL` | — | extra container env (repeatable) |
| `--detach/--no-detach` | detach | `--no-detach` streams events until completion, exit code mirrors job verdict (0 verified / 1 otherwise) |

Credentials (`--creds <dir>`) — **env-file convention**: prepare every credential
set the job needs as a `<name>.env` file (`KEY=value` lines) in one directory:

```
creds/
├── github.env        # GITHUB_TOKEN=ghp_…
├── jenkins.env       # JENKINS_URL=… JENKINS_USER=… JENKINS_TOKEN=…
└── sonarqube.env     # SONAR_TOKEN=…
```

These land at `~/.creds/*.env` (mode 0600) inside the container, and the phase
prompts tell the agent which files exist and to source the one it needs
(`set -a; . ~/.creds/github.env; set +a`) — values are never auto-exported into
every process. Recognized extras (`gitconfig`, `git-credentials`, `netrc`,
`ssh/`) are placed conventionally; an executable `setup.sh` runs once before the
first iteration. Nothing from this directory is copied into the run dir or logged.

Skills (`--skills <dir>`, repeatable): one directory per skill (must contain
`SKILL.md`). If the given dir has no `SKILL.md` but every immediate child does,
it expands to the children — so both `--skills ./skills/git` and
`--skills ./skills` (a folder of skills) work; anything else is a usage error.
Skills are copied at start (later host edits don't affect the running job) and
scoped per job — there is deliberately no "forward all host skills" mode.

Docker siblings (`--allow-docker`) — default **off**. Mounts the host docker
socket (default `/var/run/docker.sock`; override with `RALPHD_DOCKER_SOCK`)
and adds the socket's group to the container so the agent can run
`docker build` / `docker run`. **Warning: the docker socket is root-equivalent
access to the host** — the job can mount any host path and start privileged
containers; grant it only to PRDs you trust as much as your own shell. ralphctl
prints this warning to stderr at launch. Sibling rules the agent is told (and
you should know):

- Containers the job starts are **siblings on the host daemon** — their `-v`
  paths are *host* paths. ralphctl injects `RALPHD_HOST_WORKSPACE`,
  `RALPHD_HOST_RUN_DIR`, and `RALPHD_RUN_ID` so the agent can mount the right
  dirs; container-local paths mount empty and can litter root-owned dirs on
  the host.
- Siblings should carry `--label ralphd.run=<run-id>` (the job container
  always does) — `ralphctl stop`/`rm` reap everything with that label,
  best-effort. `ralphctl doctor` lists stray labeled containers whose run no
  longer exists.
- Prefer `--rm` for short-lived siblings; detached unlabeled containers,
  built images, and volumes outlive the job.

Exit: `0` started · `1` container failed to start · `2` bad options (including
a missing/invalid docker socket with `--allow-docker`).

### `ralphctl runs`

List all runs (live and historical) from the registry.

```
ralphctl runs [--state running|succeeded|failed|aborted] [--limit n]
```

Columns: run ID, state, verdict, phase, iterations used/budget, started, workspace.
`--json` emits the merged `status.json` array.

### `ralphctl status <run-id>`

Full status (mirrors `GET /status`; falls back to the run dir's `status.json` when
the container is gone — indicated by `"live": false` in `--json` mode).

### `ralphctl watch <run-id>`

Live TUI: task table, phase/approach/iteration header, budget + cost gauges,
scrolling tail of agent output, pending steering. Read-only; `q` quits.
Non-TTY/`--json`: streams SSE events as NDJSON instead (usable by agents).

### `ralphctl logs <run-id> [-N[f]]` / `ralphctl logsf <run-id>`

The **whole-job console** (backed by `GET /logs`): all iterations merged in
order, following across iteration boundaries — the Jenkins-console equivalent.
**Pretty rendering is the default**; `--raw` gives the underlying NDJSON.

Syntax matches `tail`:

```
ralphctl logs <id>            # last 50 rendered lines
ralphctl logs <id> -100       # last 100 lines
ralphctl logs <id> -150f      # last 150 lines, then follow live
ralphctl logs <id> -f         # unbounded backlog, then follow live
ralphctl logsf <id>           # alias for logs -f
```

| Option | Meaning |
|--------|---------|
| `-N` / `-Nf` | tail N lines / tail N then follow (tail-style) |
| `-f`, `--follow` | follow live across iterations until the job ends |
| `--raw` | raw NDJSON passthrough (implies no rendering; for machines) |
| `--iteration n` | restrict to a single iteration's transcript |

Pretty rendering shows: iteration/phase boundary headers (number, phase, model),
assistant text as it streams, tool calls as compact one-liners (name, key args,
outcome), thinking elided to a marker, per-iteration usage/cost footer, agent
errors highlighted. When stdout is not a TTY, output is identical minus color.

### `ralphctl tasks <run-id>`

Task table (or full `tasks.json` with `--json`).

### `ralphctl steer <run-id> [message]`

Send steering. Message from arg, `--file <f>`, or stdin.

| Option | Meaning |
|--------|---------|
| `--now` | also SIGINT the current iteration so guidance applies immediately |
| `--name <slug>` | steering file slug |

Exit `0` accepted · `5` job already finished.

### `ralphctl interrupt <run-id>`

SIGINT the current iteration without adding steering.

### `ralphctl pause <run-id>` / `ralphctl unpause <run-id>`

Hold/release the loop at the next iteration boundary.

### `ralphctl abort <run-id> [--reason <text>]`

Terminate the job (state `aborted`, honors on-complete mode).

### `ralphctl stop <run-id>`

Shut down an **idle finished** container (calls `/shutdown`, then `docker rm`).
For a running job, refuses with exit `5` — use `abort` first, or `--force` to
abort+stop in one step. Run dir is never deleted by `stop`.

### `ralphctl rm <run-id>`

Delete a run's registry dir (history, artifacts, workspace-if-internal). Requires
the container to be gone. Asks confirmation unless `--yes`.

### `ralphctl artifacts <run-id> [ls|pull <dest>]`

List or download artifacts. `pull` copies from the (host-mounted) run dir directly;
works with dead containers.

### `ralphctl skills <run-id> [ls|get <name> <dest>|add <dir>|rm <name>]`

Inspect or hot-swap skills on a running job (API-backed; `add` tars and uploads,
`get` downloads one back). Changes take effect next iteration.

```
ralphctl skills <run-id> ls                 # name, origin (mounted/api), file count
ralphctl skills <run-id> get <name> <dest>  # write the skill dir to <dest>
ralphctl skills <run-id> add <dir>          # <dir> must contain SKILL.md; uploaded as <dir>'s basename
ralphctl skills <run-id> rm <name>
```

`add` validates locally that `<dir>/SKILL.md` exists before tarring/uploading
(exit `2` otherwise, naming the dir); the skill's name is `<dir>`'s basename.
`--json` on `ls` emits the raw `GET /config/skills` list. Exit `3` if `<run-id>`
has no run dir at all; exit `4` if the run exists but its container/API is
unreachable.

### `ralphctl creds <run-id> [ls|get <name>|add <file>.env|rm <name>]`

Runtime credential management (API-backed, env-file convention).

```
ralphctl creds <run-id> ls                # name, size, mtime -- never values
ralphctl creds <run-id> get <name>        # prints the file contents to stdout
ralphctl creds <run-id> add <file>.env    # uploads/replaces; name is <file>'s stem
ralphctl creds <run-id> rm <name>
```

`add` requires a `*.env` file (exit `2` otherwise, naming the file); the
credential's name is the file's stem (`github.env` -> `github`), and it lands
at `~/.creds/<name>.env` (mode 0600) in the container immediately, no restart
needed. `get` prints the file (read-back is by design — the API token *is*
the cred boundary); `rm` deletes. `--json` on `ls` emits the raw
`GET /config/creds` list (never a `value` field). Exit `3` if `<run-id>` has
no run dir at all; exit `4` if the run exists but its container/API is
unreachable. The agent sees the updated inventory at the next iteration.

### `ralphctl prompts <run-id> [ls|set <phase> <file>]`

Inspect or hot-swap phase prompts.

```
ralphctl prompts <run-id> ls                    # name, effective source: builtin/mounted/api
ralphctl prompts <run-id> set <phase> <file>    # override <phase>'s prompt, effective next iteration
```

`<phase>` must be one of `planning`, `worker`, `review`, `task-verify` (exit
`2` otherwise, naming the valid set, checked locally before any HTTP call);
`<file>` must exist and be non-empty (exit `2` otherwise). `set` uploads the
file's raw text to `PUT /config/prompts/{phase}`, which takes effect the next
time that phase's prompt is built (never retroactive to an in-flight
iteration). `ls` reflects the new source as `api` immediately after `set`.
`--json` on `ls` emits the raw `GET /config/prompts` list. Exit `3` if
`<run-id>` has no run dir at all; exit `4` if the run exists but its
container/API is unreachable.

### `ralphctl llm`

LLM profile management + mid-run rotation:

```
ralphctl llm profiles                 # list profiles (~/.ralphd/llm-profiles)
ralphctl llm show <profile>           # resolved (redacted) view
ralphctl llm test <profile>           # spin up a throwaway container, 1-token ping
ralphctl llm set <run-id> --profile <p>   # rotate a running job's endpoint/key
```

`profiles` lists the two built-ins (`host`, `none`, tagged `(builtin)`) followed
by every `<name>.yaml` under `<registry>/llm-profiles/`, in that order.
`--json` emits `[{"name": ..., "builtin": true|false}, ...]`.

`show <profile>` fully resolves the profile (same resolution `start --llm
<profile>` performs) and prints it with every `env:` value replaced by
`***REDACTED***` and every `pi:` field that came from a
`${env:}`/`${file:}`/`${cmd:}` reference likewise masked -- literal `pi:`
fields (e.g. `baseUrl`) stay visible so the resolved shape is still useful
for diagnosis. `host`/`none` have no file to resolve; `show` reports them as
built-in with nothing to redact. Exit `3` for an unknown profile name (no such
`<name>.yaml`); a profile that fails to *resolve* (unset `${env:}` var,
unreadable `${file:}`, failing `${cmd:}`) exits `1` with the same diagnostic
`start` would show. `--json` emits the full resolved (redacted) document.

`test <profile>` first resolves the profile on the host exactly like `start`
/ `show` (same exit codes: `3` unknown name, `1` unresolvable reference,
unredacted since nothing is printed) -- no docker needed for this part. If
resolution succeeds and a docker daemon answers `docker version`, it follows
up with a real one-token completion in a throwaway container: `docker run
--rm --label ralphd.llm-test=<name> --entrypoint pi <resolved env/mounts>
<image> -p --mode json --no-session [--model <model>]`, piping a one-line
prompt on stdin (entrypoint overridden straight to `pi`, bypassing
`ralphd-engine` entirely -- this never touches a run dir). Exit `1` with the
container's stderr/stdout on a failed ping. When docker isn't reachable (or
`--no-ping` is given), the ping is skipped and resolution success alone is
reported (exit `0`). `--image`/`--model` override the image/model used for
the ping.

### `ralphctl resume <run-id> [--iterations +N]`

Start a fresh container against an existing run dir (PRD req 16). The engine
detects pre-existing `tasks.json`/completed iterations on startup and
continues the job instead of re-planning; `resume` just has to reproduce
`start`'s docker-run wiring for the *same* mounts:

- `<run-dir>` and `<config-dir>` (creds/skills/pi config already staged
  there from the original `start` survive as-is — nothing to re-derive).
- The workspace, if the original `start` used `--workspace` (the host path
  is recorded in `host.json` at `start` time and reused verbatim; the
  positional resume command never needs `--workspace` itself).
- The recorded `.api-token`, if any (`-e RALPHD_API_TOKEN=...`), so the
  same client-side token keeps working against the new container.

`--iterations +10` adds 10 to the existing budget in `job.yaml` before the
container starts (a bare integer, e.g. `--iterations 30`, sets it
absolutely instead); omit it to just continue with whatever budget remains.
`--allow-docker`, `--image`, `--port`, `--api-bind`, `--no-detach` mirror
`start`'s flags of the same name (docker-sibling access, host env forwarding
for LLM auth, and the `-e`/mount wiring those don't touch are **not**
automatically restored — only what's durably staged on disk via the config
dir; the resolved `pi` config and creds/skills already are, since the
container entrypoint re-copies `/config/pi` and the engine re-places
`/config/creds` + `/config/skills` on every startup).

Exit codes: `3` unknown run, `5` the run's container is still alive (a live
engine already holds the run dir's flock; `abort`/`stop` it first), `2` a
malformed `--iterations` value, `1` the underlying `docker run` failed.

### `ralphctl config`

Get/set registry defaults (`ralphctl config set image ghcr.io/...`,
`default_llm_profile`, `on_complete`, …).

### `ralphctl doctor`

Preflight checks (`checks` in `--json` output; overall `ok` is the AND of all
of them, exit code `0`/`1` accordingly):

- `docker` — the docker daemon is reachable.
- `image` — the job image (`--image`, default `ghcr.io/.../ralphd:latest`) is
  present locally.
- `registry` — `~/.ralphd` (or `$RALPHD_REGISTRY`) is writable.
- `pi_host_config` — `~/.pi/agent/settings.json` exists (needed for `--llm host`).
- `default_llm_profile` — the registry's `default_llm_profile` (set via
  `ralphctl config set default_llm_profile <name>`; defaults to the builtin
  `host`, which — like `none` — always trivially "resolves") resolves
  cleanly on the host: every `${env:}`/`${file:}`/`${cmd:}` reference in
  `<registry>/llm-profiles/<name>.yaml` succeeds. On failure, `--json`'s
  `defaultLlmProfileError` names the profile and the offending reference.
- `registry_schema` — no malformed registry entries: every
  `<registry>/llm-profiles/*.yaml` parses as YAML, and every run's
  `status.json` parses as JSON with a `schemaVersion` this build recognizes
  (not newer than the engine's own, PRD req 18). Failures are listed in
  `--json`'s `registryIssues` (also the human report, under `! registry
  schema issues:`).

Two dangling-container checks, in both directions — always **non-fatal**
(report-only; never affect `ok`/the exit code):

- `strayContainers` — containers labeled `ralphd.run=<id>` with no matching
  run dir at all (leftovers from `--allow-docker` jobs that were never
  reaped, or a manually deleted run dir).
- `danglingRegistryEntries` — the reverse: a run dir whose `status.json`
  says `state: running` but whose container no longer exists at all (killed
  or `docker rm`'d outside `ralphctl`). Reported as `{runId, container}`;
  suggested remedy is `ralphctl resume <run-id>`.

Designed as the first command an AI agent should run.

## Notes for AI agents driving ralphctl

- Always pass `--json`; parse stdout only.
- `start` is asynchronous by default; poll `status` or stream `watch --json`.
  A simple completion wait: `ralphctl watch <id> --json | jq -c 'select(.type=="state")'`.
- Steering is cheap and safe (applies at iteration boundary); `--now` interrupts
  work in progress — use only to stop active harm, not to reprioritize.
- Do not edit files under `~/.ralphd/runs/<id>/` except `steering/`; everything
  else is engine-owned.
- Treat `verdict: "verified"` as the only success signal; `state: "succeeded"`
  without it cannot occur, but `failed` still leaves useful state — read
  `review-findings.md` and `notes.md` before retrying with `resume` or a new job.
