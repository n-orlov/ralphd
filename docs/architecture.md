# ralphd Architecture

Status: design, targets v0.1 unless marked otherwise.

## 1. Overview

ralphd is a job-scoped autonomous coding loop. A **job** is one PRD (product
requirements document / task description) executed to verified completion. Each job
runs in its own Docker container. The container holds:

- the **engine** (`ralphd`, Python): loop supervisor + HTTP API server, PID 1
- the **agent runtime**: [pi](https://pi.dev) CLI, spawned as a subprocess once per
  iteration
- a **workspace**: the code being worked on (host bind-mount or named volume)

The host side is driven entirely by **`ralphctl`** (Python CLI): it prepares config,
starts the container, talks to the API, and maintains the run registry at
`~/.ralphd/`. Nothing in the engine assumes any particular cloud, CI system, or
network beyond reaching the configured LLM endpoint.

## 2. The loop

The loop has three phase types drawing on one shared **iteration budget** (`max_iterations`):

```
job start
  └─ approach 1..max_approaches (shared budget):
       ├─ PLANNING   (1 iteration)  read PRD → write tasks.json + notes.md
       ├─ WORKER     (n iterations) execute tasks one at a time until it
       │                            emits <promise>COMPLETE</promise>
       │    └─ [vigilant mode] VERIFY after each newly completed task;
       │                       failure ⇒ status=validation-failed, back to worker
       └─ REVIEW     (1 iteration)  independently re-check EVERY PRD requirement
            ├─ satisfied  ⇒ <promise>VERIFIED</promise> ⇒ job succeeds
            └─ not        ⇒ write review-findings.md ⇒ next approach with a
                            composite PRD (original + findings + attempt history)
budget exhausted or max_approaches reached ⇒ job fails (state preserved)
```

Design invariants:

- **The worker's word is never trusted.** `COMPLETE` only gates entry to review.
  Only the reviewer's `VERIFIED` ends the job successfully.
- **Fresh context per iteration.** Each iteration is a new `pi` process; continuity
  flows exclusively through state files (below). This bounds context growth and makes
  every iteration resumable and interruptible.
- **`tasks.json` is the source of truth**, written atomically (write-temp + rename).
  Killing an iteration mid-flight loses at most that iteration's uncommitted
  reasoning, never task state.
- **Signals are exact sentinels** (`<promise>COMPLETE</promise>`,
  `<promise>VERIFIED</promise>`, `<task-verified>id</task-verified>`) scanned from
  the agent's final output; prompts instruct the agent to emit them only as its
  last line.

### Phase prompts

Prompt templates live in the image at `/opt/ralphd/prompts/` — one per phase
(`planning.md`, `worker.md`, `review.md`, `task-verify.md`, plus the workspace-level
agent instructions file `AGENT.md`). All are **overridable**: a file of the same name
in `/config/prompts/` (mapped by the CLI) takes precedence, and can be replaced at
runtime via the API (`PUT /config/prompts/{name}`), taking effect next iteration.
Prompts are authored fresh for this project.

### Vigilant mode

Optional (`vigilant: true` in job config). After a worker iteration, the engine
diffs task statuses; each newly `completed` task gets a verification iteration
against its `successCriteria`. Failures set `status: validation-failed` with
`validationNotes`; the worker treats those like `pending` and reads the notes. Three
failed validations of the same task mark it `failed` (budget protection).

### Model strategy

Each phase resolves its model independently: per-phase override → strategy preset →
job default. Presets: `quality-first` (default; one strong model everywhere),
`cost-optimized` (strong model for planning only), `balanced` (strong for planning +
review). "Strong" and "fast" tiers are just two model IDs in job config — any model
pi can reach is valid in either slot. The engine sets the model per-iteration via
pi's model selection flag/env on the subprocess.

## 3. State model

All run state lives in `/run` inside the container, which is **always a host
bind-mount** of `~/.ralphd/runs/<run-id>/`. This is what makes history survive the
container and lets the CLI read state even when the container is dead.

```
~/.ralphd/runs/<run-id>/          # mounted at /run in the container
├── job.json            # immutable job config as launched (redacted: no secrets)
├── status.json         # engine-maintained: phase, iteration, approach, verdict
├── prd.md              # original PRD
├── composite-prd.md    # approach ≥2 only
├── tasks.json          # task state (source of truth)
├── notes.md            # agent handoff notes, ≤50 lines enforced by prompt
├── review-findings.md  # written by failed reviews
├── steering/           # steering inbox: 001-*.md, 002-*.md …
├── iterations/         # per-iteration record
│   └── 0007/
│       ├── meta.json   # phase, model, start/end, exit code, signal seen, usage
│       └── output.jsonl# full agent transcript (pi session log)
├── approaches/         # archived tasks.json/notes.md per finished approach
├── artifacts/          # anything the agent is told to persist (reports, screenshots)
└── events.jsonl        # append-only event log (also fed to the SSE stream)
```

`tasks.json` schema (v1):

```json
{
  "version": 1,
  "runId": "brisk-otter-1408",
  "goal": "one-line goal distilled from the PRD",
  "scope": {"level": "single-repo", "reasoning": "..."},
  "repositories": ["https://github.com/acme/widget"],
  "tasks": [
    {
      "id": "001",
      "title": "Add JWT middleware",
      "status": "pending|in-progress|completed|validation-failed|failed|skipped",
      "successCriteria": "natural-language, independently checkable",
      "validationNotes": "present when validation-failed",
      "validationAttempts": 0
    }
  ],
  "discovered": {}
}
```

The **workspace** (`/workspace`) is separate from `/run`: it is the repo checkout the
agent edits. Two modes, chosen per job:

- `--workspace <host-dir>` — bind-mount an existing checkout (preferred; the
  operator's normal working copy or a dedicated clone)
- no workspace flag — the engine creates `/workspace` on the run dir
  (`runs/<id>/workspace/`) and the planning iteration clones the PRD-listed repos
  using whatever git credentials were injected

## 4. Container engine

Single Python process (`ralphd.engine`), PID 1 in the container, two concerns:

1. **Supervisor** — an asyncio task running the loop: builds each iteration's
   prompt, spawns `pi` with the right env (model, workspace cwd), streams its
   output to `iterations/N/output.jsonl`, scans for sentinels, updates
   `status.json`/`events.jsonl`, applies steering, enforces budgets and timeouts.
2. **API server** — FastAPI on `:7777` (uvicorn in the same process), serving the
   endpoints in [api.md](api.md). Reads state files; writes only to the steering
   inbox, config drop-ins, and the control channel to the supervisor (pause /
   interrupt / abort / resume).

Because both share a process, "interrupt" is a plain `SIGINT` to the `pi` child
process group; the supervisor records the iteration as interrupted and proceeds to
the next one (which will see any newly arrived steering).

### Diagnosability requirements

Failures must be visible without digging into transcripts:

- **Agent-level errors surface upward.** When an iteration's agent reports an
  error (pi `message_end` with `stopReason: "error"`), the engine records the
  `errorMessage` in the iteration's `meta.json`, in the `iteration.end` event,
  and as an error-level `log` event. A job that failed because of provider/auth
  errors must say so in `/status`-adjacent surfaces, not just exit silently.
- **`docker logs` is informative on its own**: the engine logs each iteration's
  start (phase, model) and end (exit code, sentinels, error summary) to stdout,
  so a dead-in-2-seconds job is diagnosable from container logs alone.

### Container lifecycle

```
created ─ starting ─ running ─┬─ succeeded ─┐
                              ├─ failed ────┼─ idle (default) … until stopped
                              └─ aborted ───┘        └─ or exit (on_complete: exit)
```

- `on_complete: idle` (default) — engine stays up, API remains queryable, agent is
  never spawned again. The operator collects outputs / post-mortems, then
  `ralphctl stop`.
- `on_complete: exit` — container exits with 0 (succeeded) / 1 (failed/aborted).
  Suits scripted/batch use; state remains in the run dir either way.

A **job timeout** (wall clock, default 8h) and per-iteration timeout (default 45m)
bound runaway runs; hitting either aborts the current iteration and, for the job
timeout, fails the job.

### Steering

Steering files are markdown notes dropped into `steering/` (via API or by writing
the mounted dir directly). At each iteration start the engine includes all *unconsumed*
steering files in the prompt and marks them consumed (recorded in `meta.json`).
`POST /interrupt` gives the "right now" variant: SIGINT the current iteration so the
next one starts immediately with the new guidance. Steering is guidance for the
agent; it does not mutate `tasks.json` directly.

## 5. Configuration & injection

Everything the job needs is mapped in by the CLI. Three mount points:

| Mount | Contents | Writable at runtime |
|-------|----------|--------------------|
| `/run` | run state (always host-mounted) | engine-owned |
| `/workspace` | code | agent-owned |
| `/config` | prompts overrides, skills, creds, pi settings | via API (`/config/*`) |

`/config` layout:

```
/config/
├── job.yaml            # job config (PRD ref, budgets, mode flags, model strategy)
├── prompts/            # optional phase-prompt overrides
├── skills/             # agent skills, exposed to pi as workspace skills
├── creds/              # operator-provided credential files (git creds, netrc, …)
│   └── setup.sh        # optional: sourced/run before first iteration
└── pi/                 # pi settings fragments (models.json, provider config)
```

- **Skills**: directories of instruction files (`SKILL.md` convention). The engine
  links `/config/skills/*` into the location pi discovers skills from. New skills
  can be added mid-run via `PUT /config/skills/{name}` (tar upload); they appear at
  the next iteration.
- **Credentials**: opaque files; the engine copies them to conventional locations
  when recognized (`gitconfig`, `git-credentials`, `netrc`, `ssh/`) and otherwise
  leaves them for `setup.sh` / prompts to reference. Nothing credential-shaped is
  ever written to `/run` (which is host-visible history) or logged.
- **LLM config**: env vars + pi config fragments produced by the CLI's LLM-profile
  resolution — see [llm-profiles.md](llm-profiles.md). The engine merges
  `/config/pi/*` into the container-local pi settings at startup and again on API
  update.

## 6. API security

- Default: container port published to `127.0.0.1` only, **no auth**.
- Optional: `--api-token <t>` (or auto-generate with `--api-token auto`) makes the
  engine require `Authorization: Bearer <t>` on every route; combine with
  `--api-bind 0.0.0.0` for LAN/remote access. The CLI stores the token in the run
  registry (`~/.ralphd/runs/<id>/.api-token`, mode 0600) and sends it automatically.
- The API never returns credential material; `job.json` is stored redacted.

## 7. Host-side: CLI and registry

`ralphctl` (see [cli.md](cli.md)) is stateless except for `~/.ralphd/`:

```
~/.ralphd/
├── config.yaml         # defaults: image, ports, on_complete, default llm profile
├── llm-profiles/       # named LLM profiles (see llm-profiles.md)
└── runs/<run-id>/      # one dir per job, bind-mounted as /run (above)
```

Run IDs are generated (`adjective-animal-HHMM` style) or supplied with `--run-id`.
`ralphctl runs` lists history by scanning `runs/*/status.json`; live status merges
in the container API when the container is up. `ralphctl watch` renders a TUI:
task table, current phase/iteration, tail of agent output, budget gauge. A web hub
UI reading the same registry is planned (v0.3, [roadmap.md](roadmap.md)).

## 8. Docker image

`ghcr.io/…/ralphd` (multi-arch), containing: Python 3.12 (engine), Node 20 + pi CLI,
git, ripgrep, curl/jq, build essentials, and a non-root `agent` user. Deliberately
**thin on toolchains** — language runtimes beyond Python/Node are the operator's
business via `--image` (derived images `FROM ralphd`) or a job-level
`setup.sh`. Engine and API run as the non-root user; no docker socket, no host
network.

## 9. Failure containment

- Agent process crash / nonzero exit → iteration recorded as failed, loop continues
  (budget permitting). Repeated immediate failures (3 consecutive iterations with no
  task-state change) → job fails fast with a diagnostic event.
- Engine crash → container exits; run dir remains consistent (atomic writes);
  `ralphctl resume <run-id>` starts a fresh container against the same run dir and
  the loop continues from `tasks.json` (v0.2).
- Host reboot ⇒ same as engine crash; nothing lives only in container memory.
