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

# Collect results when done (container exits by default; add --on-complete idle to keep it up for debugging)
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
current job state · `130` follow interrupted by Ctrl+C (see `logs -f` below).

## Commands

### `ralphctl start`

Create and launch a job container. Prints the run ID (and with `--json`: run ID,
container ID, API URL, token presence).

```
ralphctl start --prd <file|-> [options]
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--prd <file\|->` | required* | PRD markdown (`-` = stdin); *not required if `--template` supplies a `prd.md` skeleton |
| `--template <name>` | none | load job defaults + optional `prd.md`/skills/creds from `<registry>/templates/<name>/` (see below); any explicit flag on this command overrides the template's value |
| `--workspace <dir>[:name]` | none | bind-mount an existing checkout at `/workspace` (a bare `<dir>` with no `:name`); without any `--workspace` the agent clones PRD-listed repos into the run dir. Repeatable for multi-repo jobs (PRD req 27): every entry beyond the first must carry a `:name`, mounted at `/workspace/<name>` instead (e.g. `--workspace ~/src/api:api --workspace ~/src/web:web`); prompts list every mounted workspace name/path so the agent doesn't have to guess |
| `--run-id <id>` | generated | explicit run ID |
| `--iterations <n>` | 25 | shared iteration budget |
| `--max-approaches <n>` | 3 | review-loop approach limit |
| `--vigilant` | off | per-task verification |
| `--reflect` | off | run one extra `reflect` iteration after the job reaches a terminal state, writing a report + suggested prompt/skill diff to `artifacts/reflection/` (never touches the workspace or run state) |
| `--model <id>` | profile default | default model (pi model ID) |
| `--model-strategy <s>` | quality-first | `quality-first\|cost-optimized\|balanced\|custom` |
| `--model-<phase> <id>` | — | per-phase override (`planning\|worker\|review\|verify`) |
| `--llm <profile>` | `host` | LLM profile ([llm-profiles.md](llm-profiles.md)); falls back to the registry's `default_llm_profile` (`ralphctl config`) if set
| `--llm-env KEY=VAL` | — | ad-hoc env additions to the LLM config (repeatable) |
| `--forward-env NAME\|PREFIX_*` | — | forward host env var(s) into the container, by exact name or prefix glob (repeatable). Required for any non-standard vars — see [llm-profiles.md](llm-profiles.md) |
| `--skills <dir>` | — | mount a skills directory (repeatable) |
| `--creds <dir>` | — | mount a credentials directory (see below) |
| `--allow-docker` | off | mount the host docker socket into the job container — **root-equivalent host access**, see below |
| `--prompt-override <dir>` | — | phase-prompt override directory |
| `--image <ref>` | bundled default | alternative/derived engine image; falls back to the registry's `image` (`ralphctl config`) if set |
| `--on-complete idle\|exit` | exit | post-completion behavior; `idle` is an explicit debugging opt-in; falls back to the registry's `on_complete` (`ralphctl config`) if set |
| `--on-complete-cmd <cmd>` | — | shell command run once by the engine (in-container) on reaching a terminal state; receives `RALPHD_RUN_ID`/`RALPHD_STATE`/`RALPHD_VERDICT` env vars; failures are logged (`events.jsonl`, `level: error`) but never affect the job's verdict or the engine's exit code |
| `--timeout <dur>` | 8h | job wall-clock limit (`45m`, `8h`, `2d`) |
| `--iteration-timeout <dur>` | 45m | per-iteration limit |
| `--infra-outage-budget <seconds>` | 14400 (4h) | wall-clock budget for riding out one LLM-endpoint outage: infra-classified faults (connection errors, 429/529, overloaded, throttling — see [architecture.md](architecture.md)) keep being retried while one episode's accumulated backoff wait stays under this budget. Waiting costs no iterations and no approaches. Written to the run's `job.yaml` as `infra_outage_budget_s` and visible in `GET /config` (`budgets.infraOutageBudgetS`) |
| `--auto-resume` / `--no-auto-resume` | off | opt this run in to (or explicitly out of) self-recovery: `ralphctl doctor --fix` resumes a run recorded non-terminal whose container has vanished (the dangling-container condition `doctor`/`repair`/`status` report). Host-side setting — recorded with the run's other start-time wiring in `<registry>/configs/<run-id>/auto-resume.json`, never passed into the container, so it survives every later `resume` (the container is replaced, the config dir is not). Falls back to the registry's `auto_resume` (`ralphctl config`) if set; `--no-auto-resume` overrides a template/registry opt-in. The default is deliberately **off** in v0.5 and lives in exactly one place in the source (see [roadmap.md](roadmap.md) for the planned flip) |
| `--port <n>` | auto | host port for the API |
| `--api-bind <addr>` | 127.0.0.1 | host interface to publish on |
| `--network <net>` | docker default (bridge) | docker network for the job container. `host` shares the host network namespace so the job can reach host-only / VPN / tailnet services; with `host` there is no port publishing — the engine itself listens on `--port` bound to `--api-bind` (via `RALPHD_PORT`/`RALPHD_BIND`). Any other value is passed to `docker run --network` with normal `-p` publishing. Recorded in `host.json`; `resume` reuses it. Falls back to the registry's `network` (`ralphctl config`) if set. |
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

Job templates (`--template <name>`, PRD req 25): a directory
`<registry>/templates/<name>/` (e.g. `~/.ralphd/templates/<name>/`, or under
`RALPHD_REGISTRY` if set) may contain:

```
templates/<name>/
├── job.yaml     # optional: scalar job defaults (see below)
├── prd.md       # optional: PRD skeleton, used when --prd is omitted
├── creds/       # optional: default --creds directory (same env-file convention)
└── <skill-dir>/ # optional: default skill(s), referenced by job.yaml's `skills:` list
```

`job.yaml` may set any of: `iterations`, `max_approaches`, `vigilant`,
`reflect`, `on_complete`, `on_complete_cmd`, `timeout`, `iteration_timeout`, `model_strategy`,
`llm`, `model`, `fast_model`, `thinking`, `skills` (a list of directory names
relative to the template dir), `creds` (a directory name relative to the
template dir), and `prd` (a filename relative to the template dir, default
`prd.md`). Every one of these has a corresponding `start` flag; **an explicit
flag on the command line always overrides the template's value** for that
field (`--skills`/`--creds` override wholesale, not merge). For `image`,
`on_complete`, `network`, and `llm`, fields the template doesn't set fall
back next to any matching `ralphctl config` registry default
(`image`/`on_complete`/`network`/`default_llm_profile`), and only then to
the hardcoded default; every other field falls straight back to its
hardcoded default. An unknown `--template`
name exits `3` naming the expected path; a malformed `job.yaml` (not a
mapping) exits `2`.

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
- Siblings must carry **both** `--label ralphd.run=<run-id>` and
  `--label ralphd.role=sibling` (the job container always carries the run
  label plus `--label ralphd.role=job` so the two can be told apart; the job
  also gets `RALPHD_SELF_CONTAINER_ID` naming its own container) —
  `ralphctl stop`/`rm` reap everything with the run label,
  best-effort. `ralphctl doctor` lists stray labeled containers whose run no
  longer exists.
- **Never clean up by the run label alone** — and the agent is told so in
  every prompt. From inside
  the job, `docker rm -f $(docker ps -aq --filter
  label=ralphd.run=$RALPHD_RUN_ID)` also matches the job container: the run
  dies mid-iteration, that iteration's work and transcript are lost and the run
  dir is left non-terminal (issue #7). The sanctioned form adds the role
  filter — `docker ps -aq --filter label=ralphd.run=$RALPHD_RUN_ID --filter
  label=ralphd.role=sibling` — and end-of-run reaping is ralphctl's job, not
  the agent's. Host-side, `stop`/`rm` filter on the run label alone on purpose
  (they *should* take the job container too).
- Prefer `--rm` for short-lived siblings; detached unlabeled containers,
  built images, and volumes outlive the job (only *containers* are reaped).
- **Toolchain in a sibling** — the intended answer whenever a job needs a
  toolchain the engine image doesn't ship (Go, Rust, a JDK, tmux, a database):
  the job builds a small image from a `ci/Dockerfile` it commits to the target
  repo and runs each command in a `--rm --user 1000:1000` sibling with the
  *host* workspace path bind-mounted. The prompts spell the recipe out; see
  [architecture.md §6](architecture.md#toolchain-in-a-sibling) for the
  verified facts and failure modes, and `examples/skills/toolchain-sibling/`
  for a ready-made skill (`--skills examples/skills/toolchain-sibling`) with a
  copy-pasteable `run.sh` wrapper.

Exit: `0` started · `1` container failed to start · `2` bad options (including
a missing/invalid docker socket with `--allow-docker`).

### `ralphctl runs`

List all runs (live and historical) from the registry, **newest first**.

```
ralphctl runs [--state running|succeeded|failed|aborted]
              [--sort runId|state|verdict|phase|approach|iterationsUsed|startedAt]
              [--reverse]
```

Columns: run ID, state, verdict, phase, approach, iterations used/budget,
started (absolute local time via the shared formatter, see `status` below).
`--json` emits the merged `status.json` array — in the **same order** as the
human table, with the raw ISO `startedAt` and the numeric `iterationsUsed`/
`iterationsBudget` fields kept alongside the rendered `"7/250"` string.

**Sorting** (task 055, issue #9) is the CLI half of the hub run list's
click-to-sort (see “Sorting” under `ralphctl ui` below) and uses the *same*
keys, the same lifecycle orders and the same raw payload values:

- default `--sort startedAt`, i.e. **newest first** — not the run-id
  alphabetical order the registry directory listing yields;
- `startedAt`, `iterationsUsed` and `approach` sort biggest/newest first;
  the text keys (`runId`, `phase`) sort A→Z; `state`/`verdict` sort in
  lifecycle order (`starting → running → succeeded → failed → aborted`, and
  no-verdict → `unverified` → `verified`);
- `--reverse` flips whichever direction the key starts with;
- rows with a missing value for the key (no `startedAt`/`approach` yet) sort
  last under an ascending key and first under a descending one, so a
  just-started run appears at the top of the default view;
- ties break on run id ascending, so the order is stable;
- `--sort`/`--reverse` compose with `--state`, which filters first.

An unrecognised `--sort` key is a usage error (exit `2`).

### `ralphctl status <run-id>`

Full status (mirrors `GET /status`; falls back to the run dir's `status.json` when
the container is gone — indicated by `"live": false` in `--json` mode).

Human output includes a `duration:` line: while the job is still running this
is the **elapsed-so-far** time since `startedAt` (labeled `(elapsed)`); once
the job has reached a terminal state it is the **total run time** from
`startedAt` to `endedAt` (labeled `(total)`). While an iteration is in flight,
an additional `iteration elapsed:` line shows that iteration's own elapsed
time. Both are rendered in a compact human format (`45s`, `3h 12m`, `2d 1h` —
no millisecond noise) via one shared formatting helper used everywhere
durations are shown.

Alongside the durations (task 048, issue #4) come the **absolute** instants:
a `started:` line always, an `ended:` line once the run is terminal, and — for
a vanished-container run — a `last update:` line instead. All three are local
wall clock plus the UTC offset (`2026-08-18 11:04:07 +0300`), rendered by the
same shared formatter (`ralphd.engine.state.format_local_time`) the `logs`
iteration boundaries and the hub use. `--json` is unaffected: it keeps the raw
ISO `startedAt`/`endedAt` fields for machine consumers.

`--json` adds machine-usable duration fields alongside the existing timestamp
fields (nothing existing is removed or renamed): a top-level `durationSeconds`
(elapsed-so-far or total, numeric seconds, same rule as the human line above),
and, when `currentIteration` is present, an `elapsedSeconds` field nested
inside it for that iteration's own elapsed time. Task 022 adds
`containerGone` (always present: `true` only for the vanished-container case
described under `container:` below) and, for such a run, a
`sinceLastUpdateSeconds` field; `durationSeconds` and
`currentIteration.elapsedSeconds` are then measured to the last `status.json`
write instead of to *now*, so they stop growing for a run that stopped.

A terminal run (`succeeded`/`failed`/`aborted`) also carries an
`unconsumedSteering` field: a list of steering filenames that were still
pending when the run went terminal (task 006 -- empty in the common,
fully-consumed case, since a terminal run never reads pending steering
again). The human output does not bury this in the JSON: if the list is
non-empty, `ralphctl status` prints an extra `!! UNCONSUMED STEERING: ...`
line (bold red on a TTY) naming the stranded file(s), so an operator running
the plain human command still notices without having to remember `--json`.
The hub run-detail view shows the same fact as a `.steering-warning` banner
on the run summary card.

Human output also renders (task 003):

- `reason:` -- shown only when status.json carries a non-empty `reason`
  (the engine sets this on terminal `failed`/`aborted` states, e.g. an
  infra-fault exhaustion or an engine bug, and on budget-exhaustion grace
  reviews per task 002). Long reasons wrap across multiple lines rather
  than one unreadable line. Omitted entirely when there is no reason to
  show (still running, or a terminal state that never set one). Task 004:
  the hub run-detail view surfaces the same `reason` prominently for
  terminal `failed`/`aborted` runs as a `.run-reason` banner on the run
  summary card (mirroring the `.steering-warning` banner above), so an
  operator watching a run through the hub sees why it failed without
  fetching `--json`.
- `tasks:` -- a one-line summary of the `tasks` counts dict, e.g.
  `7/7 completed` when everything is done, or `5/7 completed (1
  in-progress, 1 pending)` when it is not, instead of a raw JSON dump of
  the counts.
- `usage:` -- a one-line summary of the `usage` dict, e.g. `$0.56, 625k
  tokens (planning $0.10 / worker $0.40 / review $0.06)`, instead of a raw
  JSON dump. The per-phase breakdown only lists phases that actually
  accrued usage. A cost the provider never quoted is **never** printed as
  `$0.00` (task 051, #10): a bucket marked `costStatus: "unknown"` renders
  `unavailable`, and `costStatus: "partial"` renders the priced subtotal as
  a lower bound, `$0.12+ (partial, rest unavailable)` -- the same wording
  the `ralphctl logs` footer and the hub use, because all three go through
  one formatter (`engine/state.format_cost`). A cost computed from the
  optional host-side pricing map (task 052, #10, see `ralphctl config`
  below) is marked as such and never presented as a provider-quoted price:
  `~$0.45 derived`, or `$0.56 + ~$0.45 derived` when both kinds are present.
- `degraded:` (task 013) -- shown only while the run is `health: "degraded"`,
  i.e. sitting out an infra outage (see `health`/`infraWait` in
  [api.md](api.md)). While a backoff wait is pending it names the attempt
  number, the phase, the **countdown to `nextAttemptAt`** and how much of the
  outage budget the episode has spent, with the classified error on
  continuation line(s):

  ```
  degraded:  infra outage: attempt 4 (phase worker), next attempt in 58s (at 2026-08-18T09:15:02Z), waited 52s of 4h outage budget
             error: getaddrinfo EAI_AGAIN aigw.example.internal
  ```

  Between two waits (`infraWait` back to `null` while the retry attempt
  itself runs, `health` still `degraded`) it reports the ongoing episode
  without a countdown. A healthy run prints no such line -- its output is
  byte-identical to before this line existed.
- `reflection:` (task 020) -- shown only when the post-terminal `reflect`
  iteration **failed** (status.json's `reflect.ok` is `false`, see
  [api.md](api.md)):

  ```
  reflection: failed (Connection error.)
  ```

  A failed reflection never changes the run's `state`/`verdict`/`reason` --
  the job is already over when `reflect` runs -- so without this line the
  only trace is `artifacts/reflection/FAILED.md` and the run dir looks
  exactly like one that never enabled reflect. A *successful* reflection
  (its `artifacts/reflection/report.md` is the signal) and a run that never
  ran one print nothing, keeping their output byte-identical to before.
  The hub run-detail card carries the same line (`.reflect-failed`).
- `container:` (task 022, issue #8) -- shown only for an **unreachable** run
  whose status.json still records a non-terminal state (`starting`/`running`)
  while no container by that name exists at all: the zombie condition
  `ralphctl doctor`/`repair` report. Printed right under the `state:` line
  (bold red on a TTY) so the operator does not have to join `state: running`
  with `(live api: False)` themselves:

  ```
  container: ralphd-myrun appears gone (no such container) -- status.json still
             records state 'running', so this run stopped without recording a terminal
             state; diagnose with `ralphctl repair myrun`
  ```

  For such a run the `duration:` line stops showing an ever-growing
  `(elapsed)` value -- nothing is elapsing -- and instead shows the
  **staleness**: the time since the last `status.json` write, labelled
  `(since last update)`. Any `iteration elapsed:` line is frozen at that same
  last write (`, at last update`). A run whose container still *exists*
  (merely exited) and every live or terminal run print none of this, keeping
  their output unchanged.
- `auto-resume:` (task 028, issue #8) -- shown only when the auto-resume
  crash-loop guard has **given up** on this run (the run dir's
  `auto-resume.json` records `gaveUp: true`, see `doctor --fix` below):

  ```
  auto-resume: gave up after 5 attempts (max 5, last attempt
               2026-08-18T09:15:02Z): the run's container keeps dying
               without the run making progress, ...
  ```

  `--json` carries the whole record as `autoResume` (`null` for a run that
  never needed recovery, which also prints nothing here -- as does a run
  still inside the guard's backoff, since that one is still being recovered).

For an unreachable run the `tasks:` counts are computed **CLI-side** from the
run dir's `tasks.json` (task 023, issue #8): status.json itself never stores
them -- `GET /status` synthesises them from the plan -- so without this the
fallback printed `tasks: (none)` for a dead run with a perfectly readable
plan, exactly when an operator most wants to know how far it got. The same
key mapping the engine uses is applied (`total`/`completed`/`inProgress`/
`pending`/`validationFailed`, shared code in `engine/state.py:task_counts`),
and `--json` carries the identical numbers under `tasks`. A run dir with no
`tasks.json`, or an empty plan, still prints `tasks: (none)`. Task 004 (#15):
that read goes through the hardened reader too, so a plan caught mid-rewrite
is reconstructed from the last payload that parsed and `--json` carries the
same `tasksStale`/`tasksSource` fields a live `GET /status` does (docs/api.md,
"Stale task reads") — the counts never drop to `(none)` for a file that exists
and previously parsed.

`--json` output is untouched by any of this: it still carries the full,
unsummarized `reason`/`tasks`/`usage` detail straight from status.json, plus
`health` and `infraWait` verbatim (the on-disk fallback for an unreachable
run defaults them to `"ok"`/`null`, matching `GET /status`), and `reflect`
(defaulted to `null` the same way).

### `ralphctl watch <run-id>`

Live TUI: task table, phase/approach/iteration header, budget + cost gauges,
scrolling tail of agent output, pending steering. Read-only; `q` quits.
Non-TTY/`--json`: streams SSE events as NDJSON instead (usable by agents).
The cost gauge renders through the one shared cost formatter
(`engine/state.format_cost`, task 051), so an unpriced/mixed total reads
`unavailable` there too rather than `$0.0000`.

The stream is replayed from the start of the run's `events.jsonl`, which is
append-only **across resumes** — so it can contain a terminal `state` event
from an earlier episode. `watch` ends only on a terminal `state` event that
nothing in the log supersedes *and* that the live `GET /status` agrees with,
so watching a resumed run blocks until that run's real terminus instead of
exiting on the historical marker (a finished run whose engine is gone or
idling still ends immediately).

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

With `--follow`, `ralphctl` reads and renders/prints each line off the open
connection as it arrives (both `--raw` and pretty modes) — it does not wait
for the underlying HTTP response to close, which for a running job only
happens once the job itself terminates. `ralphctl watch` streams the same
way (it never buffered). Redirect to a file/pager as usual if you want to
capture the live output while still watching it (e.g. `ralphctl logs <id> -f
| tee out.ndjson`).

A follow ends when the run's **current** state is terminal and the whole
transcript has been delivered — never on a marker left in the run dir by an
earlier episode. So `logs -f` on a *resumed* run (whose `events.jsonl` still
carries the previous episode's `succeeded`/`failed`/`aborted` event, and whose
`status.json` said so until the resuming engine rewrote it) keeps streaming
through the old transcript and on into the new iterations, right up to the
real terminus — the same liveness contract `ralphctl watch` follows. Unlike
`watch`, the logs path never inspected the event log for that decision, so it
was never subject to the stale-terminal early close (#13); the behaviour is
pinned by `tests/test_cli_logs_resumed_run.py`.

Interactive exit from `-f`/`--follow` (task 002):
- On a TTY, press **`q`** to stop following and return — no error, no
  extra output, exit code `0`. This never touches stdin on a non-TTY
  stdin (piped/redirected): a piped `logs -f` is never blocked waiting
  for a key that will never arrive. (The key-watcher thread closes the
  open HTTP response to unblock the main thread's blocking read; the
  main thread's exception handling around that read treats the resulting
  connection-closed error — including a bare `AttributeError` from
  Python's chunked-transfer decoder if the close lands mid-chunk — as
  the expected, non-error `q` outcome, never a traceback.)
- **Ctrl+C** (SIGINT) during a follow always exits cleanly — no
  Python traceback on stderr — at the single documented exit code
  **`130`** (the standard `128+SIGINT` shell convention), whether or not
  stdin is a TTY.
- **SIGTERM** (e.g. a plain `kill <pid>`) during a follow is handled the
  same way: no traceback, terminal left exactly as it was found, exit
  code `128+SIGTERM` (task 016).

Terminal-mode ownership (task 016): on a TTY, `logs -f` puts stdin into
cbreak mode (no echo, single-keypress reads, so `q` doesn't need Enter)
for the duration of the follow, and restores the terminal's prior mode on
*every* exit path — normal completion, `q`, Ctrl+C, SIGTERM, or any other
exception — from a context manager that wraps the whole follow loop in
the main thread. (An earlier version of this saved/restored termios state
from the background key-watcher thread instead; that thread can be torn
down by a main-thread `KeyboardInterrupt` before its own cleanup runs,
which could strand the terminal in no-echo mode after Ctrl+C. Restoration
now happens from code guaranteed to run to completion around the follow,
not from the thread that merely reads keys.)

Pretty rendering shows: iteration/phase boundary headers (number, phase, model),
assistant text as it streams, tool calls as compact one-liners, thinking
elided to a marker, per-iteration usage/cost footer, agent errors
highlighted. When stdout is not a TTY, output is identical minus color.
The footer's `cost=` field prints `unavailable` when the provider billed the
iteration's tokens without quoting a price (`usage.costPriced: false`, see
[api.md](api.md)) instead of dropping the field or showing `$0`.

Each tool one-liner shows the tool's salient argument (task 001), not just
its name -- generously truncated (~300 chars) so the operator can actually
tell one `bash` call from another instead of seeing nine identical
`bash() ✓ ok` lines:
- `bash` → `→ bash $ <command>` -- newlines in the command are collapsed so
  one invocation always stays one line.
- `read`/`write`/`edit` → `→ <tool> <path>`.
- `grep`/`glob`/`find`-style tools → `→ <tool> <pattern>`.
- any other/unknown tool → `→ <tool> <first scalar argument value>`
  (best-effort; whatever the first plain string/number/bool argument is).
The `✓ ok` / `✗ error` outcome is unchanged; on `✗` a short excerpt of the
tool's result is shown when one can be extracted, same as on success
(errors get a bit more room -- ~120 chars -- than success excerpts,
~60 chars, since the detail matters more when something failed).
A plain-string result is used verbatim (as before). A STRUCTURED
(non-string) result -- e.g. the standard
`{"content": [{"type": "text", "text": ...}]}` shape tool results
commonly use, including structured error payloads under an `error`/
`detail` key -- is walked for its first non-empty `text` item to produce
the same short excerpt (task 015). An unrecognized/unknown structured
shape yields NO excerpt at all: this deliberately never falls back to
stringifying or JSON-dumping the whole result object, which would dump
arbitrary structured noise into the pretty renderer instead of a short,
readable line.
This does not change what secrets can leak: redaction
(`src/ralphd/engine/redact.py`) scrubs known secret values out of the
transcript at write/serve time, upstream of rendering, so a full `bash`
command or file path shown here is exactly what a `--raw` reader already
sees -- rendering it is not a new exposure surface (see
tests/test_secret_redaction.py, which asserts this stays true).

**Live tool start-lines (task 003) and in-place TTY rewrite (task 004).**
In a `-f`/`--follow` (live) session the invocation line above prints the
MOMENT the tool call starts (`→ bash $ <command>`, no outcome yet) rather
than only once it finishes -- a long-running tool (e.g. a multi-minute
`bash` command) is no longer silent for its whole duration.

- **On a TTY**, that invocation line is left open with no trailing
  newline (the cursor sits right after it), and once the matching
  `tool_execution_end` arrives the SAME line is rewritten in place
  (`\r` + ANSI erase-to-end-of-line, then the full text again) into the
  final `→ bash $ <cmd> ✓ ok (<excerpt>)` form -- so a finished TTY
  follow session ends up byte-for-byte identical (once those control
  bytes are accounted for) to the buffered one-line-per-tool rendering.
  If any other renderable event (streamed text, a new iteration boundary,
  another tool call) arrives before the outcome does, the open line is
  finalized with a plain newline first and the eventual outcome instead
  prints as its own short completion line (`↳ ✓ ok`), never mid-line.
- **On a non-TTY (piped) stream** there is no cursor to rewind: the
  invocation prints as a plain, complete line immediately, and the
  outcome later prints as its own short completion line (`↳ ✓ ok`,
  plus error excerpt on `✗`) -- piped `logs -f` output never contains a
  `\r` or an ANSI control byte.

In non-follow (buffered) rendering -- `ralphctl logs <id>` without
`--follow`, where the whole transcript is already in hand -- a completed
tool call still renders as exactly the single one-liner it always has;
only a call with no matching end yet (still running when the transcript
was fetched) would show just the invocation line with no completion.
Once an iteration has ended, its "done" summary line includes a `took <duration>`
field (same compact human format as `status`, computed from that iteration's
`startedAt`/`endedAt`); an iteration still in flight has no "done" line yet
(it only appears once the iteration boundary's `end` event exists), so there is
nothing to omit or fake an in-progress duration for. `--raw` mode is unchanged
by this: the underlying `ralphd.iteration` boundary events simply carry the raw
`startedAt`/(on `end`) `endedAt` fields the pretty renderer derives the duration
from.

Both boundary lines also carry an **absolute local-time timestamp** (task 048,
issue #4): the start header ends with `· started 2026-08-18 11:04:07 +0300`
and the "done" line carries `at <same format>` for `endedAt`, alongside (never
instead of) the relative `took`. A relative duration alone cannot be lined up
with anything outside the run -- an upstream outage window, a host reboot,
another run's log -- which is what these are for. "Local" means *the host's*
timezone, and the UTC offset is always printed so a pasted timestamp stays
unambiguous; one shared formatter
(`ralphd.engine.state.format_local_time`) renders every absolute timestamp
ralphd shows (this renderer, `ralphctl status`, and the hub -- which gets the
formatted string from the server rather than reimplementing the format in
JavaScript). `--raw` still emits only the ISO wire values.

**`-N` means N RENDERED lines in pretty mode, N raw events in `--raw` mode.**
The engine's `GET /logs?tail=N` (and `GET /iterations/{n}/output?tail=N`)
always trims RAW NDJSON events — that's the wire contract and it never
changes. In pretty mode a raw `tail=N` would be the wrong thing to hand the
renderer: the renderer collapses/skips many raw event types (e.g. a whole
burst of `text_delta`/`toolcall_delta` events becomes one streamed-text
block or one tool one-liner), so trimming raw events *before* rendering
produces a wildly variable, much-smaller-than-N number of *visible* lines.
So the trim is owned by different layers depending on mode:
- `--raw`: the **engine** trims (raw `tail=N`, 1 raw line == 1 line of
  output, unchanged from the wire contract).
- pretty (default): **`ralphctl` itself** trims. It always fetches the full
  untailed transcript from the engine, renders every line, and only then
  keeps the last N *rendered* lines — so `logs <id> -100` always means
  "the last 100 lines you'd actually see", full stop.
- Iteration/phase boundary headers (`── iteration N · ... ──` / the "done"
  summary line) count toward N like any other rendered line — they are not
  given special exemption.
- `-Nf`/`logsf -N` (follow + pretty with a tail): shows exactly N rendered
  lines of backlog first, then keeps following live as normal — the
  backlog fetch/render/trim happens the same way as the non-follow case,
  then a live connection picks up from there without re-showing or
  dropping a line.

**Unreachable run — on-disk snapshot (task 040).** A run's transcript lives
in its run dir (`iterations/NNNN/output.jsonl`), not only inside the
container, so `logs` no longer fails with exit `4` once the container is
gone (crashed, removed, or a finished run whose engine has exited). All
modes fall back to the same on-disk merge the engine's `GET /logs` serves
from the inside (`ralphd.log_merge`, shared with the hub's log tail), exit
`0`, and print a one-line notice on **stderr**:

```
ralphctl: on-disk snapshot: the run's API is not reachable, showing the transcript recorded in the run dir
```

- stdout is unaffected — `--raw` keeps its 1:1 wire contract (stdout is
  byte-for-byte the merge, tailed with the engine's own raw `tail=N`
  semantics), so piping into `jq`/`tee` works exactly as for a live run.
- `-f`/`--follow` (and `logsf`) print that snapshot and return cleanly with
  `… (nothing to follow)` appended to the notice, instead of hanging on a
  container that will never answer or dying on connection-refused.
- `--iteration n` reads that iteration's transcript from disk the same way.
- A run id with no run dir is still an error: exit `3` ("run not found").
- A reachable run's behaviour and output are unchanged, and nothing is ever
  written to stderr for it. Pinned by `tests/test_cli_logs_dead_run.py`.

**Empty transcript (task 041).** A run whose `iterations/` dir is empty (it
just started, or it died before its first iteration was recorded) prints the
explicit line

```
(no transcript yet)
```

and exits `0`, rather than zero bytes of output — which is
indistinguishable from a broken command. The hub's log tail shows the same
wording for the same run (one definition, `ralphd.log_merge.NO_TRANSCRIPT`).
`--raw` is excluded on purpose: it is a machine contract, and an empty
transcript honestly is zero events, so its stdout stays byte-empty. Pinned
by `tests/test_no_transcript_message.py`.

### `ralphctl tasks <run-id>`

Task table (or full `tasks.json` with `--json`).

**Never prints an empty plan it is not sure about (task 004, #15).**
`tasks.json` is written by the agent, not the engine, so a read can land
inside a non-atomic rewrite. Two cases used to print nothing at all; both now
print the plan plus a marker on **stderr** (stdout keeps the plain
`[status] id title` format, `--json` stays a clean document):

| Case | stdout | stderr marker |
|------|--------|---------------|
| API unreachable (container gone) | the plan from the run dir | `on-disk snapshot: the run's API is not reachable, showing the plan recorded in the run dir` |
| `tasks.json` caught mid-rewrite / corrupt | the last plan that parsed | `!! stale task list: …` |
| `tasks.json` unparseable and never read before | nothing | `!! unreadable task list: …` |

The read goes through the engine's hardened reader
(`engine.state.read_tasks_doc`, `docs/architecture.md` §3), so the same
`tasksStale` / `tasksSource` fields a live `GET /tasks` carries (see
`docs/api.md`, "Stale task reads") appear in `--json` on the on-disk path too,
alongside `live: true|false`. A run id with no run dir is still exit `3`.
A reachable run with a healthy plan prints exactly what it always did, with
nothing on stderr. Pinned by `tests/test_tasks_stale_cli.py`.

### `ralphctl steer <run-id> [message]`

Send steering. Message from arg, `--file <f>`, or stdin.

| Option | Meaning |
|--------|---------|
| `--now` | also SIGINT the current iteration so guidance applies immediately |
| `--name <slug>` | steering file slug |

The on-disk filename is always `NNN-<slug>.md`, where `NNN` is an
engine-assigned monotonic sequence (never supplied by the caller). If
`--name` already carries its own `NNN-` prefix (e.g. copy-pasted from a
prior steering filename), that prefix is stripped before appending the
engine's own, so the result is never doubled (e.g. `--name 019-steering`
does not yield `022-019-steering.md`).

Exit `0` accepted · `5` job already finished.

### `ralphctl interrupt <run-id>`

SIGINT the current iteration without adding steering.

### `ralphctl pause <run-id>` / `ralphctl unpause <run-id>`

Hold/release the loop at the next iteration boundary.

### `ralphctl retry <run-id>`

Wake a **degraded** run (one whose `status` shows `health: degraded` with a
populated `infraWait` — it is sitting out an LLM-endpoint outage) instead of
letting the escalating backoff run its course: posts `POST /retry`, which cuts
the current wait short and attempts the phase again immediately.

A manual retry also **resets the outage-budget episode clock**, so the wait
accumulated so far stops counting against `infra_outage_budget_s` (the attempt
number keeps escalating, so the *next* automatic backoff is unchanged). It
never unpauses a paused run and never touches steering — use `unpause` to
release an operator pause.

Exit `0` woken · `5` the run is not in an infra wait (or already finished) ·
`3` unknown run · `4` API unreachable. `--json` prints the engine's
`{"retrying": true}`.

### `ralphctl budget <run-id> <+N|N>`

Change a **running** job's iteration budget in flight (`PATCH /config/budget`)
without restarting the container — the operator-facing answer to "the job is
about to run out of iterations and it is nearly there":

```console
$ ralphctl budget my-run +10      # top up: current budget + 10
$ ralphctl budget my-run 40       # absolute new budget
```

Same spec syntax as `resume --iterations`: `+N` is relative, a bare integer is
absolute — so a bare `-5` is an *absolute* -5 and is rejected, never read as a
decrement (lower a budget by passing the absolute value you want). The new
value is live at the next iteration boundary and immediately visible in
`status` (`iterationsBudget`) and `GET /config`; every accepted change emits a
`budget_changed` audit event.

This is a **live-engine** change only: `/config/job.yaml` is a read-only mount,
so the engine never rewrites it. Use `resume <run-id> --iterations +N` when the
new budget has to survive a fresh container.

Exit `0` applied · `5` the engine refused (resulting budget below
`iterationsUsed`, or the job already finished — resume instead) · `1` the
engine rejected the value (`422`) · `2` locally malformed spec · `3` unknown
run · `4` API unreachable. `--json` prints the engine's
`{"iterations": 40, "previous": 25, "iterationsUsed": 17}`.

### `ralphctl abort <run-id> [--reason <text>]`

Terminate the job (state `aborted`, honors on-complete mode).

### `ralphctl stop <run-id>`

Shut down an **idle finished** container (calls `/shutdown`, then `docker rm`).
For a running job, refuses with exit `5` — use `abort` first, or `--force` to
abort+stop in one step. Run dir is never deleted by `stop`.

### `ralphctl rm <run-id>`

Delete a run's registry dir (history, artifacts, workspace-if-internal). Requires
the container to be gone. Asks confirmation unless `--yes`.

### `ralphctl repair <run-id>`

Non-interactive diagnosis (task 008; PRD requirement E) for a run dir left in
an inconsistent shape by a crash outside the paths the engine's own
crash-consistency handling already covers. Validates `status.json`,
`tasks.json`, and `host.json` against their expected schemas
(docs/architecture.md's "State model" / "tasks.json schema (v1)") and
reports every issue found (malformed JSON, missing required fields,
unrecognized `state`/task `status` values, a `schemaVersion` newer than
this build knows, duplicate task ids) -- it never guesses at a fix on its
own; that's what the (separate, guarded) `--set-state`/`--env` flags are
for.

It also checks the **dangling-container condition** (task 021): a run whose
`status.json` records a non-terminal state (`starting`/`running`) but whose
container no longer exists at all. This is the same check `doctor` reports
globally as `danglingRegistryEntries` (one implementation, shared), so the
two can never disagree; the *remedy* is shared too (task 025, one string in
one place), so both commands tell **one story** for the same run:

1. `ralphctl resume <run-id>` — continue it. Resume-first is the default
   advice because the container died but the run dir (plan, notes,
   artifacts, transcripts) is intact, so the work is still there to finish.
   Opt-in per-run auto-resume (`ralphctl doctor --fix`, see
   docs/roadmap.md) automates exactly this step.
2. `ralphctl repair <run-id> --set-state aborted` — declare it over,
   recording a `reason` naming the vanished container.

`repair` prints that remedy as part of the issue text, plus a `dangling`
field (`{runId, container}` or `null`) in `--json`. A run whose container
merely *exited* (it still exists) is not this condition.

- Refuses to touch a run whose container is currently running (a live
  engine already owns that run dir's on-disk state) -- exit `5`, nothing
  written, same as `resume`'s refusal.
- Every invocation appends a `type: repair` audit line to the run's
  `events.jsonl` (`action`, what was `checked`, the issue count/
  list) -- never a secret value, since diagnosis only ever names files,
  fields, and task ids.
- `--json` prints `{"runId", "checked", "issues", "ok", "dangling"}`;
  `checked` is `["status.json", "tasks.json", "host.json", "container"]`.
  Plain output prints a readable one-issue-per-line summary. Exit `0` if no
  issues were found, `1` otherwise (mirrors `doctor`'s `ok`-based exit
  code).

Exit codes: `3` unknown run, `5` container still running.

**`--set-state <state>`** (task 009) is a guarded escape hatch for a run
whose container died without the engine ever writing a terminal state to
`status.json`. It skips diagnosis and directly overwrites `status.json`'s
`state` field, after validating the requested value against the same
recognized-state list diagnosis checks (`starting`, `running`, `succeeded`,
`failed`, `aborted`) and after the same refuse-while-running check. Every
other field in `status.json` is left untouched -- except that when the run
was in fact a zombie (the dangling-container condition above), a `reason`
is written alongside the new state saying the container no longer exists
(died or was removed outside `ralphctl`), so the terminal state on disk
explains itself; for an already-terminal run no such reason is invented.
`--json` prints `{"runId", "action": "set-state", "old", "new", "reason"}`
(`reason` is `null` when nothing vanished); the audit event
(`type: repair`, `action: "set-state"`) records the `old`/`new` state
values and the same `reason`. Exit `2` for an unrecognized state value (no
write, no audit event), `5` if the container is running.

**`--env KEY=VAL`** (task 010, repeatable) adds or updates a recorded
value in the persisted env wiring (`env-wiring.json` under the job's
config dir -- the mechanism from task 001/requirement A that lets
`resume` reproduce `--forward-env`/`--llm-env`/`--env` byte-for-byte).
This is the exact hand-edit the operator performed live before this
feature existed, done safely: an existing key is replaced in place
(preserving the file's key order), a new key is appended, the file stays
mode `0600`, and the value is never echoed to stdout/stderr or written
into the audit event -- only the `KEY` name is recorded. A subsequent
`resume` carries the updated value into the container exactly like any
other recorded env wiring. `--json` prints `{"runId", "action": "env",
"keys"}` (the list of key names touched, never values); the audit event
(`type: repair`, `action: "env"`) records the same `keys` list. Exit `2`
for a malformed `KEY=VAL` argument (no `=`; no write, no audit event),
`5` if the container is running.

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
- The workspace(s), if the original `start` used `--workspace` (the host
  path(s) are recorded in `host.json` at `start` time — as `workspace` for a
  single unnamed mount, `workspaces` (name→path) for the multi-repo case —
  and reused verbatim; the positional resume command never needs
  `--workspace` itself).
- The recorded `.api-token`, if any (`-e RALPHD_API_TOKEN=...`), so the
  same client-side token keeps working against the new container.
- The `--llm` wiring resolved at `start` time (task 058, operator steering
  018): whichever env vars and extra mounts `start` added on top of the
  base wiring above — `--llm host`'s forwarded `HOST_LLM_ENV` vars
  (`ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`, ...) and its `~/.aws` mount if
  present, or a named profile's fully-resolved `env:`/`mounts:` (including
  anything that came from a `${env:}`/`${file:}`/`${cmd:}` reference) — are
  persisted at `start` time to `<config-dir>/llm-wiring.json` (mode `0600`,
  never under the run dir proper, never returned by any HTTP route — the
  same at-rest pattern already used for `<config-dir>/pi/models.json` and
  `<run-dir>/.api-token`) and reproduced byte-for-byte by `resume`,
  regardless of what the operator's *current* shell has (or lacks) at
  resume time. A run started before this existed (no `llm-wiring.json` on
  disk) resumes exactly as it did before — no error, just nothing extra to
  reproduce.
- The resolved `--forward-env`/`--llm-env`/`--env` pairs from `start` time
  (task 001): these three generic, non-`--llm`-derived flags are resolved
  once at `start` and persisted, in the exact order applied, to
  `<config-dir>/env-wiring.json` (mode `0600`, same at-rest pattern as
  `llm-wiring.json`) — so a job whose LLM credentials arrived via e.g.
  `--forward-env 'AWS_*'` resumes with those *original* values, never
  re-read from the resuming shell's own (possibly absent or different)
  environment. `resume` replays `llm-wiring.json`'s env/mounts first, then
  `env-wiring.json`'s pairs, matching `start`'s own precedence (a later
  duplicate name wins). A run started before this existed (no
  `env-wiring.json`) resumes exactly as before — no error, no extra `-e`
  flags. `resume` has no override flags of its own for these yet; only the
  recorded values are replayed.

`--iterations +10` adds 10 to the existing budget in `job.yaml` before the
container starts (a bare integer, e.g. `--iterations 30`, sets it
absolutely instead); omit it to just continue with whatever budget remains.
`--allow-docker`, `--image`, `--port`, `--api-bind`, `--network` (defaults
to the network recorded at start time), `--no-detach` mirror
`start`'s flags of the same name. The resolved `pi` config and creds/skills
are restored too, since the container entrypoint re-copies `/config/pi`
and the engine re-places `/config/creds` + `/config/skills` on every
startup. Anything from a bare `--forward-env`/`--llm-env`/`--env` (as
opposed to `--llm` itself) at the *original* `start` is not re-derived by
`resume` — pass those again explicitly on the `resume` invocation if
needed (`resume` has no such flags of its own; use `start`'s wiring only
for `--llm`-derived env/mounts, which now survives automatically).

Exit codes: `3` unknown run, `5` the run's container is still alive (a live
engine already holds the run dir's flock; `abort`/`stop` it first), `2` a
malformed `--iterations` value, `1` the underlying `docker run` failed.

### `ralphctl config get <key>` / `ralphctl config set <key> <value>`

Registry-wide defaults (PRD req 25), persisted at `<registry>/config.yaml`
(created on first `set`). Recognized keys: `image`, `on_complete`
(`idle`/`exit` — validated on `set`), `default_llm_profile` (any string;
`ralphctl doctor` resolves it as an LLM profile name, see below), `network`
(any string; same values `--network` accepts, e.g. `host`), `auto_resume`
(`true`/`false` — validated on `set` and stored as a real boolean; the
registry-wide default for `start --auto-resume`).

- `get` prints `<key>: <value>`, or `<key>: (unset)` if the key has never
  been `set` — this is not an error (exit `0` either way).
- `set` overwrites just that one key in `config.yaml`, leaving any other
  keys already set untouched.
- An unrecognized key exits `2` (both `get` and `set`) naming the expected
  keys; an invalid `on_complete`/`auto_resume` value on `set` also exits `2`
  and leaves `config.yaml` unchanged.
- `--json` on `get`/`set` prints `{"key": ..., "value": ...}` (`value` is
  `null` for an unset `get`).

`ralphctl start` layers these in as the **registry-wide fallback** for the
same-named flags (`--image`, `--on-complete`, `--network`, `--auto-resume`)
and for `--llm`
(via `default_llm_profile`), between an explicit flag/`--template` value and
the hardcoded built-in default: explicit flag > `--template` > `ralphctl
config` default > hardcoded default. `resume`/`llm test`/`doctor`'s own
`--image` flags are unaffected (still default to the hardcoded image).

#### Optional host-side pricing map (`pricing:`)

Some gateways bill tokens and report no price at all, which ralphd records as
*unknown* rather than `$0` (#10). `pricing:` in `<registry>/config.yaml` lets
you supply the missing rates yourself -- the only way to get a real number for
a gateway-local alias like `aigw-openai/gpt-5`, which no upstream pricing table
can know. It is a nested mapping, so it is edited in `config.yaml` directly
rather than through `config set` (which only takes scalar keys):

```yaml
pricing:
  aliases:
    "aigw-openai/*": "openai/*"            # trailing-* keeps the tail
    eu.anthropic.claude-opus-5: anthropic/claude-opus-5
  models:
    "openai/gpt-5": {input: 1.25, output: 10.0, cacheRead: 0.125}
    "anthropic/*":  {input: 3.0, output: 15.0}   # family default
```

Rates are USD per **million** tokens, keyed like the usage counters
(`input`, `output`, `cacheRead`, `cacheWrite`); an absent cache rate falls back
to the `input` rate rather than to a silent `$0`. An exact model key beats a
wildcard one, and the longest wildcard prefix wins.

- `ralphctl start` **inlines** the map into the run's `job.yaml` (`pricing`),
  so the rates a run uses are the ones it started with and survive every later
  `resume`; a single run can also be pointed at a map with
  `RALPHD_PRICING='{"models": ...}'`. The resolved table is visible in
  `GET /config` (`pricing`).
- It is consulted **only** when the provider quoted no price, and the result is
  published separately as `costDerivedUSD` (never merged into `costUSD`) and
  rendered as `~$0.45 derived` everywhere (`status`, the `logs` footer, the
  hub) -- a derived cost is never passed off as a provider-reported one.
- No map configured (the default) changes nothing: unpriced traffic stays
  `unavailable`, never a guessed number.

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
  records a non-terminal state (`starting`/`running`) but whose container no
  longer exists at all (killed or `docker rm`'d outside `ralphctl`).
  Reported as `{runId, container}`; the human report prints the **shared
  remedy line** described under `repair` below (task 025) — resume first,
  `repair --set-state aborted` as the alternative — naming the actual run
  id, so `doctor` and `repair` can never recommend different next commands
  for the same run. The same check (one implementation) backs `repair`'s
  per-run dangling-container diagnosis.

  ```
  ! registry entries recorded running with no matching container:
      myrun  container=ralphd-myrun
        the container died or was removed outside ralphctl; continue it with `ralphctl resume myrun`, or record it as over with `ralphctl repair myrun --set-state aborted` (writes a reason naming the vanished container)
  ```

  (one line per entry; wrapped here for the page width)

#### `ralphctl doctor --fix` (self-recovery sweep)

`--fix` turns the `danglingRegistryEntries` report into an action: every run
recorded non-terminal whose container has vanished **and** which is opted in
to `auto_resume` (`start --auto-resume`, a template's `auto_resume: true`, or
`ralphctl config set auto_resume true`; default **off**) is resumed through
exactly the same code path as an operator-typed `ralphctl resume <id>` — so
the fresh container reproduces the run's original wiring (run-dir/config-dir/
workspace mounts, the `--llm`/`--env` wiring recorded at start time, the
`ralphd.run` label) and cannot drift from it. Opted-out runs are still
**reported** but never touched.

`--json` adds `autoResume: {resumed: [...], skipped: [...], failed:
[{runId, error}], waiting: [{runId, attempts, nextAttemptAt}], gaveUp:
[{runId, attempts, reason}], operatorTerminated: [{runId, action, at,
reason}], recovered: [...]}` (`null` without `--fix`); the human report
annotates each dangling entry with `auto-resumed (auto_resume enabled)`, a
`not auto-resumed: auto_resume is off for this run` note above the usual
manual remedy line, the crash-loop guard's `not auto-resumed yet: crash-loop
backoff …` / give-up reason (below), or `auto-resume FAILED: <error>`.
A run whose container is still alive is not dangling and is never touched.
One broken run cannot abort the sweep. `--fix` never changes the exit code:
it stays the AND of the preflight `checks` above.

**Never resurrects a run you killed.** Self-recovery only ever restarts a run
whose container *vanished on its own*. Two carve-outs make that true:

* runs in a terminal state (`succeeded`/`failed`/`aborted`) are not dangling
  by definition and never enter the sweep — and the dangling condition is
  re-checked immediately before each resume, so a run that finished (or whose
  container came back) between the registry scan and the restart is reported
  as `recovered` and left alone;
* `ralphctl abort` and `ralphctl stop` record the operator's intent in the run
  dir as `operator-termination.json` (`{action, at, reason, source}`; the
  engine writes the same file the moment `POST /abort` arrives, so an abort
  whose container dies before it can write a terminal state is still marked).
  A run carrying that marker is reported under `operatorTerminated` with `not
  auto-resumed: terminated by the operator …` and is never restarted, even
  with `auto_resume` on. `ralphctl stop --force` is the sharp case it exists
  for: it removes the container while `status.json` may still say `running`,
  which on disk is otherwise indistinguishable from a crash.

**Crash-loop guard.** A run whose container dies seconds after every resume
(broken image, missing credential, corrupt run dir) must not be resurrected
forever, so each run's attempts are recorded in its run dir at
`auto-resume.json` as `{attempts, lastAt, maxAttempts, iterationsUsed,
gaveUp, reason}` and:

* consecutive attempts are spaced by an escalating backoff (30s, 2m, 10m,
  30m, 1h — the last value repeating); a sweep inside the backoff reports
  `waiting` and starts no container;
* after `maxAttempts` (5) attempts that never made progress the sweep gives
  up: the run is left alone (still reported as dangling) with a `reason`
  naming the crash loop, which `ralphctl status` prints as an
  `auto-resume: gave up after N attempts …` line and `--json` exposes as
  `autoResume`. Investigate, then delete the run dir's `auto-resume.json`
  to re-arm auto-recovery (or `ralphctl resume <id>` by hand);
* progress resets the counter: if the run's `iterationsUsed` advanced since
  the recorded attempt, the next death is a new incident and is recovered
  immediately — a long-lived job is never refused recovery just because it
  was recovered a few times over its lifetime.

The sweep is idempotent and cheap (one registry scan plus one `docker
inspect` per run), so **the intended deployment is a periodic `ralphctl
doctor --fix` from cron or a systemd timer** — deliberately *not* a new
ralphd daemon, which keeps the process model at "one container per job,
nothing long-lived on the host":

```cron
* * * * * ralphctl doctor --fix >/dev/null 2>&1
```

```ini
# /etc/systemd/system/ralphd-doctor.service  (+ .timer, OnUnitActiveSec=60)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/ralphctl doctor --fix
```

Designed as the first command an AI agent should run.

### `ralphctl ui [--port N] [--bind ADDR]`

Starts the local web hub server (PRD reqs 21-22) in the foreground: a
stdlib-only HTTP server (`http.server`/`urllib`; no `fastapi`/`uvicorn` on
this path, even though those are dependencies of the engine side of this
same package) that reads `~/.ralphd/runs/*` and proxies each run's *live*
container API when reachable. `--port` defaults to a free ephemeral port;
prints `serving hub at http://<bind>:<port>` on startup. Ctrl-C to stop.

JSON endpoints served under `/api/`:

- `GET /api/runs` — run list (PRD req 21): `{"runs": [{runId, state,
  verdict, phase, approach, iterationsUsed, iterationsBudget, startedAt,
  containerGone}, ...]}`, read straight from every `runs/*/status.json` (no
  live proxy calls, so listing stays cheap regardless of how many runs are
  dead). `containerGone` (task 024) is `true` only for a run whose recorded
  state is non-terminal (`starting`/`running`) while its API port does not
  accept a connection — i.e. the container died without recording a terminal
  state. Only those runs are probed, with a concurrent loopback TCP connect
  (~0.3s worst case for the whole sweep, no docker CLI involved); a terminal
  run is unreachable by design and always reports `false`.
- `GET /api/runs/<id>` — run detail: `{runId, live, containerGone, status,
  tasks, iterations}`. `status`/`tasks` are proxied live from the run's
  container API (`GET /status`/`GET /tasks`) when its `apiUrl` (recorded in
  `host.json`) answers; otherwise falls back to the on-disk
  `status.json`/`tasks.json` snapshot with `live: false` — a dead run never
  produces an error, just stale-but-valid data. `containerGone` is the same
  condition as in the run list, decided here by the real proxy call rather
  than a port probe. `iterations` is always read from disk
  (`iterations/*/meta.json`). `404` for an unknown run id. Task 048 (issue
  #4) adds `startedAtLocal`/`endedAtLocal`/`updatedAtLocal` strings next to
  (never replacing) the ISO `startedAt`/`endedAt`/`updatedAt` fields of both
  `status` and each iteration: absolute local-time renderings produced by the
  one shared server-side formatter (`ralphd.engine.state.format_local_time`),
  which is what the iteration timeline and the summary card display. "Local"
  is the *hub host's* timezone (the offset is included), and clients keep the
  raw ISO values for sorting and machine use. Task 004 (#15): the on-disk
  `tasks` snapshot is read through the engine's hardened reader
  (`read_tasks_doc(..., persist=False)` — the hub is a read-only viewer of
  another process's run dir and never writes a last-good cache into it), so a
  poll that lands inside an agent's rewrite of `tasks.json` serves the last
  plan that parsed, carrying the same `tasksStale`/`tasksSource` fields
  documented in docs/api.md instead of an empty table. A live answer is passed
  through verbatim, flags included (a pre-0.6 engine sends none, and the hub
  does not invent `tasksStale: false` on its behalf).
- `GET /api/runs/<id>/logs?tail=N` — server-rendered log tail (task 014):
  fetches the run's FULL raw NDJSON backlog from the live container API
  (`GET /logs`, no `tail` param there), renders it through the exact same
  `ralphd.cli.log_render.render_to_lines` function `ralphctl logs` uses
  (plain text, no ANSI — `tty=False`), THEN trims to the last `tail`
  *rendered* lines (matching the `ralphctl logs` non-follow tail contract:
  `N` means N rendered lines, not N raw events, same as the `-N`/`--tail N`
  syntax below). Returns `{"live": bool, "lines": ["<rendered line>", ...]}`
  — never an error. When the run's API isn't reachable (task 039: its
  container died, or the run finished long ago) `live` is `false` and the
  lines come from the **on-disk transcript merge** instead
  (`ralphd.log_merge.merged_lines` — the very same merge the engine serves
  from inside the container, so the rendering is identical), which means a
  dead run's log is still readable in the hub; only *following* needs a
  live container. `app.js` labels such a tail as `(on-disk snapshot — the
  run's API is not reachable, not following)`, in the same wording style as
  the detail card's `live: no (on-disk snapshot)` row. When there is no
  transcript at all (an empty `iterations/` dir), `lines` is the single
  line `(no transcript yet)` — the same wording `ralphctl logs` prints,
  from the same constant (`ralphd.log_merge.NO_TRANSCRIPT`, task 041) —
  never `[]`. The static hub bundle's `app.js` just displays these
  lines (one per DOM element, via `textContent`); it does not reimplement
  any event-to-text rendering rules of its own, so it always renders
  identically to `ralphctl logs` (including collapsing a many-delta
  thinking block to exactly one `[thinking…]` line, the defect this task
  fixed — the pre-014 client-side renderer appended one element per
  `thinking_delta` event with no dedup).
- `GET /api/runs/<id>/prd` — the run's PRD for the hub's PRD dialog (task
  056, issue #1): `{"live": bool, "text": "<markdown>"}`. Proxies the run's
  live `GET /prd` and, when that API doesn't answer, falls back to reading
  the run dir on disk — the same live-first/on-disk shape as the log tail
  above, so a finished or killed run's PRD stays readable in the hub. Which
  file counts as "the PRD" (`composite-prd.md` when the engine composed one,
  else `prd.md`) is decided by the single shared helper
  `ralphd.engine.state.prd_path`, the same one the engine route uses, so the
  live and on-disk answers can never disagree. A run dir with no PRD at all
  answers with the single line `(no PRD recorded)` (constant
  `ui_server.NO_PRD`) rather than an empty string, and `404` for an unknown
  run id.
- `POST /api/runs/<id>/steer` — body `{"message": ..., "name": ...}`,
  forwarded to the run's live `POST /steering`. Returns the API's own
  response (`202 {"file": ...}`) on success; `503` with an `error`/`detail`
  if the run's API is unreachable or rejects it; `404` for an unknown run id.
- `POST /api/runs/<id>/retry` — the hub's "retry now" button (task 017):
  forwarded (empty body) to the run's live `POST /retry`, i.e. the same
  thing `ralphctl retry <run-id>` does — wake the pending infra backoff
  wait immediately and reset the outage-budget episode clock (docs/api.md).
  The engine's status code is passed **through**, notably its `409 not
  waiting on an infra fault` refusal, so the UI can say "nothing to wake"
  instead of reporting a generic failure; `503` (with an `error`) only when
  the run's API is unreachable; `404` for an unknown run id.
- Any other path is served from the static hub bundle packaged in the
  wheel (`src/ralphd/cli/web/`: `index.html`, `app.js`, `style.css` — plain
  HTML/JS/CSS, no npm/node build step). A path that doesn't match a real
  file under `web/` falls back to `index.html` (SPA-style client-side
  routing via `location.hash`), so e.g. a raw browser refresh on a run's
  detail view still loads the app shell. If the bundle is somehow absent
  from the installed build, non-`/api` paths `404` with a plain-text
  "static hub bundle not installed in this build" message instead of
  crashing.

The bundle itself (open `http://<bind>:<port>/` in a browser):

- **Run list** (`#/`) — table of every run under the registry with state,
  verdict, phase, approach, iteration count and start time, auto-refreshed
  every 4s; click a run id to open its detail view. A run flagged
  `containerGone` gets a highlighted row (`tr.row-warning`) and a
  `⚠ container gone` marker next to its state pill, so a zombie never
  looks like a healthy `running` run in the list either.
  **Sorting** (task 054, issue #9): every column header is click-to-sort,
  clicking the active column reverses it, and the active column carries a
  `▲`/`▼` indicator (plus `aria-sort`). The default is **STARTED
  descending** — newest first, not the run-id alphabetical order the
  registry directory listing yields. Keys are the *raw payload values*, not
  the rendered cell text: `ITERATIONS` sorts numerically on
  `iterationsUsed` (not the `"17/250"` string), `STARTED` on the parsed
  instant (so ISO values with different UTC offsets order correctly), and
  `STATE`/`VERDICT` in lifecycle order (`starting → running → succeeded →
  failed → aborted`, and no-verdict → `unverified` → `verified`) rather than
  alphabetically. The chosen sort lives outside the DOM, so the 4s refresh
  rebuild preserves it.
- **Run detail** (`#/run/<id>`) — summary card (state/verdict/phase/
  approach/iterations/live-vs-snapshot/duration), a usage/cost panel
  (total tokens+cost plus the `byPhase`/`byApproach` breakdowns from PRD
  req 19 when present; an unknown/partial cost shows the shared
  `unavailable` wording, computed server-side by `ui_server` and delivered
  as `usage.costDisplay` exactly like `startedAtLocal`, never re-derived
  from `costUSD` in the browser), a task table, an iteration timeline (number,
  phase, model, duration once ended), a live log tail rendered with the
  *same* pretty rules as `ralphctl logs` (iteration boundaries, streamed
  text, compact tool one-liners, elided thinking, malformed-line
  markers — reimplemented in `app.js`, not shared code, since the CLI is
  Python and the bundle is browser JS), and a steering form that `POST`s
  to `/api/runs/<id>/steer` and reports the created file name back.
  A **view PRD** button (task 056, issue #1) opens the run's PRD in a modal
  `<dialog>` fed by `GET /api/runs/<id>/prd`, so it works for a dead run too
  (the dialog then says `(on-disk snapshot — the run's API is not
  reachable)`, the same wording style as the log tail's snapshot label). The
  PRD is inserted as **text nodes only** (`textContent`, never `innerHTML`
  — the task-014 rendering discipline): agent/operator-authored markdown is
  outside the page's trust boundary, and rendering it as HTML would both
  invite injection and mangle the `<`-heavy text the operator came to read.
  Only one dialog exists at a time; closing it removes it, so the 4s refresh
  behind it cannot accumulate copies.
  Each row of the **task table** is clickable (and keyboard-reachable:
  `tabindex=0`, Enter/Space) and opens that task's detail in the same
  `<dialog>` (task 057, issue #2): its `status`, `priority` and `dependsOn`
  when set, its `successCriteria` — the text the task is actually judged
  against — and any `validationNotes`. The task record is already in the
  run-detail payload (`tasks`), so no extra request is made, and the same
  text-nodes-only discipline applies: criteria are agent-authored prose full
  of backticks, `<` and fenced snippets.
  A **degraded** run (`health: degraded`/`infraWait` set — the run is
  sitting out an endpoint outage; see docs/api.md) gets a visually
  distinct card (`.card.degraded`) carrying the attempt number, phase,
  error, episode wait against the outage budget, a countdown to
  `nextAttemptAt` that ticks every second, and a **retry now** button
  posting to `/api/runs/<id>/retry`. The button appears only while a
  backoff wait is actually pending and only when the run's API is
  reachable: on a dead run the card says `read-only on-disk snapshot` and
  offers no button.
  A run whose post-terminal **reflection failed** (`reflect.ok: false`)
  gets a `.reflect-failed` line -- `reflection: failed (<error>)`, the same
  wording `ralphctl status` prints -- since the failure deliberately leaves
  `state`/`verdict`/`reason` untouched; a successful or absent reflection
  adds nothing.
  A run flagged **`containerGone`** (task 024: recorded `starting`/`running`,
  API gone) gets the same warning treatment as a degraded card
  (`.card.warning`, sharing one CSS rule with `.card.degraded`) plus a
  `.container-gone` block saying the container appears gone and pointing at
  `ralphctl repair <run-id>` for the authoritative docker-side diagnosis —
  the hub only knows the API stopped answering. Without it the only hint was
  the `live: no (on-disk snapshot)` row, which reads identically for a
  finished run that is unreachable by design.

Browser e2e coverage (PRD req 23a): `tests/test_browser_hub.py`, marked
`@pytest.mark.browser`, drives a real Chromium via the external
`playwright-cli` tool (shelled out to, never imported) against a real
`ralphctl ui` server, proving the run list renders fixture runs, the run
detail view renders a real task table and iteration timeline from a live
test engine, and submitting the steering form creates a real file under
the run's `steering/` directory. Screenshots of each view are saved to
`artifacts/screenshots/hub/`. The whole module skips cleanly (all tests
`SKIPPED`, not errored) if the `playwright-cli` binary isn't on `PATH`.
- No build step: the JS is hand-written, talks only to the JSON endpoints
  above via `fetch()`, and ships as-is inside the wheel (verified by
  building the wheel and listing its contents — no `package.json`/
  `node_modules`/bundler output anywhere in the tree).

## Notes for AI agents driving ralphctl

- Always pass `--json`; parse stdout only.
- `start` is asynchronous by default; poll `status` or stream `--json watch`.
  A simple completion wait: `ralphctl --json watch <id> | jq -c 'select(.type=="state")'`
  — it exits at the run's real terminus, including on a resumed run whose log
  still carries the previous episode's terminal event.
- Steering is cheap and safe (applies at iteration boundary); `--now` interrupts
  work in progress — use only to stop active harm, not to reprioritize.
- Do not edit files under `~/.ralphd/runs/<id>/` except `steering/`; everything
  else is engine-owned.
- Treat `verdict: "verified"` as the only success signal; `state: "succeeded"`
  without it cannot occur, but `failed` still leaves useful state — read
  `review-findings.md` and `notes.md` before retrying with `resume` or a new job.
