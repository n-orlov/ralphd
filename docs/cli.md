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
| `--skills <dir>` | — | mount a skills directory (repeatable) |
| `--creds <dir>` | — | mount a credentials directory (see below) |
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

Credentials (`--creds <dir>`): the directory is mounted read-only; recognized names
(`gitconfig`, `git-credentials`, `netrc`, `ssh/`) are placed conventionally inside
the container; an executable `setup.sh` is run once before the first iteration.
Nothing from this directory is copied into the run dir or logged.

Exit: `0` started · `1` container failed to start · `2` bad options.

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

### `ralphctl logs <run-id> [--iteration n] [--follow] [--tail n]`

Agent transcript(s). Default: current/last iteration.

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

### `ralphctl pause <run-id>` / `ralphctl resume <run-id>`

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

### `ralphctl skills <run-id> [ls|add <dir>|rm <name>]`

Inspect or hot-swap skills on a running job (API-backed; `add` tars and uploads).

### `ralphctl prompts <run-id> [ls|set <phase> <file>]`

Inspect or hot-swap phase prompts.

### `ralphctl llm`

LLM profile management + mid-run rotation:

```
ralphctl llm profiles                 # list profiles (~/.ralphd/llm-profiles)
ralphctl llm show <profile>           # resolved (redacted) view
ralphctl llm test <profile>           # spin up a throwaway container, 1-token ping
ralphctl llm set <run-id> --profile <p>   # rotate a running job's endpoint/key
```

### `ralphctl resume <run-id>` *(v0.2)*

Start a fresh container against an existing run dir; the loop continues from
`tasks.json` (crash recovery, or continuing an exited job with more budget via
`--iterations +10`).

### `ralphctl config`

Get/set registry defaults (`ralphctl config set image ghcr.io/...`,
`default_llm_profile`, `on_complete`, …).

### `ralphctl doctor`

Preflight: docker reachable, image present/pullable, registry writable, default LLM
profile resolves and (with `--ping`) answers. Designed as the first command an AI
agent should run.

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
