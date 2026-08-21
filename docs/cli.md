# ralphctl — CLI Reference

`ralphctl` is the single entry point for operating ralphd jobs. It is designed to be
driven by a **human at a terminal or by an AI agent** — hence:

- every command supports `--json` for machine-readable output (stable schema);
  default output is human-oriented tables/text
- exit codes are meaningful and documented per command
- no command is interactive unless explicitly marked; the one that is (`rm`)
  takes `--yes`, and every prompt is skipped when stdout is not a TTY
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
ralphctl artifacts brisk-otter-1408 pull ./out/
ralphctl stop brisk-otter-1408
```

## Global flags

Two, and only two — everything else belongs to a subcommand:

| Flag | Meaning |
|------|---------|
| `--json` | machine-readable output on stdout, logs to stderr |
| `--version` | print the ralphd version and exit |

There is no global `--registry`, `--quiet` or `--yes`: point ralphctl at another
registry with the `RALPHD_REGISTRY` environment variable, and see `rm --yes`
for the one confirmation prompt.

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
| `--fast-model <id>` | profile default | model for phases a `cost-optimized`/`balanced` strategy routes to the cheap tier |
| `--model-strategy <s>` | quality-first | `quality-first\|cost-optimized\|balanced\|custom` — which phase gets `--model` and which gets `--fast-model`. A true per-phase override (`model_overrides`, keys `planning\|worker\|review\|verify\|reflect`) is a `job.yaml` key only: no `start` flag sets it |
| `--thinking <level>` | — | pi thinking level |
| `--price-strategy none\|aws` | none | derive a cost for routes the provider does not price (or prices with an implausible `$0`): `aws` uses ralphd's built-in AWS Bedrock rate table, `none` leaves such a cost `unavailable`. Written to the run's `job.yaml` as `price_strategy` (so `resume` replays it) and visible in `GET /config` (`priceStrategy`); falls back to the `--template`'s value, then the registry's `price_strategy` (`ralphctl config`), then the `--llm` profile's own `price_strategy:` — see "Built-in AWS Bedrock rate table" below |
| `--llm <profile>` | `host` | LLM profile ([llm-profiles.md](llm-profiles.md)); falls back to the registry's `default_llm_profile` (`ralphctl config`) if set
| `--llm-env KEY=VAL` | — | ad-hoc env additions to the LLM config (repeatable) |
| `--forward-env NAME\|PREFIX_*` | — | forward host env var(s) into the container, by exact name or prefix glob (repeatable). Required for any non-standard vars — see [llm-profiles.md](llm-profiles.md) |
| `--skills <dir>` | — | mount a skills directory (repeatable) |
| `--creds <dir>` | — | mount a credentials directory (see below) |
| `--allow-docker` | off | mount the host docker socket into the job container — **root-equivalent host access**, see below |
| `--image <ref>` | built from source (`ralphd:<hash>`) | pin an exact engine image instead of building one — see "The job image" below; falls back to the registry's `image` (`ralphctl config`) if set |
| `--base-image <ref>` | — | build the job image **on top of** this one — see "Bring your own base image" below. Mutually exclusive with `--image` |
| `--dockerfile <path>` | — | build **this** Dockerfile (with its own directory as the build context) into that base image instead of naming one that already exists — see "Bring your own base image" below. Mutually exclusive with `--image` and `--base-image` |
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
| `--no-detach` | detached | stream events until completion instead of returning at launch; the exit code mirrors the job verdict (0 verified / 1 otherwise) |

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

The job image (`--image <ref>`) — by default `start` **builds it**, keyed by the
content of its inputs: `container/` (the Dockerfile and its entrypoint),
`pyproject.toml` and `src/ralphd/`. Those hash to a short digest, the image is
tagged `ralphd:<hash>`, and the build runs only when that tag is missing from
the local daemon. So:

- a source change produces a new tag automatically — running a stale engine is
  structurally impossible rather than something you have to remember;
- a repeat `start` on unchanged sources is a single image lookup and no build;
- files that cannot change what the engine does inside the container (`tests/`,
  `docs/`, and `artifacts/`, which a *running job* writes) are outside the hash,
  so they never invalidate a cached image.

Build output streams to stderr, prefixed and bounded — the first 200 lines,
then a notice; if the build fails, the last 40 lines are printed as context and
`start` exits `1` **before creating the run dir or config dir**, so a broken
build never leaves a half-registered run behind (re-run the same `--run-id`
once it is fixed). The build defaults to the legacy builder
(`DOCKER_BUILDKIT=0`) because a build from inside a job container has the
static docker client only; set `DOCKER_BUILDKIT=1` yourself to override that.

An explicit reference — `--image <ref>`, the `RALPHD_IMAGE` environment
variable, a `--template`'s `image:`, or the registry's `image`
(`ralphctl config set image ...`) — **pins exactly that image**: nothing is
hashed and nothing is built, which is how you deliberately run an older
engine.

A `pipx`/wheel install has no checkout next to it, so the wheel **ships the
image inputs as package data** (`ralphd/_image/`: `container/`,
`pyproject.toml` and the two metadata files `pip install` reads). `start` then
stages those, plus the installed engine itself, into a build context laid out
exactly like a checkout — which hashes to the same `ralphd:<hash>` a checkout
of that version builds, so the two share the image cache. It is never silent:
each such build prints

```
ralphctl: job image inputs come from this install's own package data (…/ralphd/_image): …
```

and `doctor` reports it as `imageStaleness.inputs` (`checkout` / `packaged` /
`none`). Only an install with *neither* a checkout nor package data can hash
nothing: that says so on stderr too and falls back to `ralphd:dev`. See
[architecture.md](architecture.md) for why the shipped-inputs option was chosen
over pinning a published tag.

Bring your own base image (`--base-image <ref>`) — when your repo needs a
toolchain the default image does not carry (a JDK, a Go toolchain, a specific
node), hand ralphd that image as a **base**, not as the job image: it is not
run directly (it has no `ralphd-engine` in it). `start` generates a Dockerfile
that layers the engine and pi onto it — pi at the version `container/Dockerfile`
pins, the engine installed from the source tree into its own venv, so your
image's own python installation is left alone — builds it with the ralphd source
root as the build context, and runs the result:

- the derived image is tagged `ralphd-derived:<hash>`, where the hash covers the
  base reference, the same image inputs as above **and** the generated recipe,
  so a new base, a new engine or a new ralphd each produce a new tag;
- it is cached exactly like the default image: one lookup, a build only on a
  miss, so a second `start` from the same base builds nothing;
- your base only has to carry your toolchain. The recipe installs what the
  engine shells out to (`git`, `curl`, `jq`, `rg`, `ps`, `python3`) and a
  new-enough node **only if the base lacks them**, via the base's own
  `apt-get`; a base with neither the tools nor `apt-get` fails the build with a
  message naming what is missing rather than producing a broken job image;
- a failed derived build aborts `start` the same way (exit `1`, no run state).

Build the base yourself (`--dockerfile <path>`) — when the toolchain your repo
needs is a *recipe* rather than an image somebody already pushed, hand ralphd
the Dockerfile instead of the image. It is built **with the Dockerfile's own
directory as the build context** (so a `COPY settings.xml` line in it means what
its author meant), tagged `ralphd-base:<hash>` over that whole context, and then
used exactly as a `--base-image` would be: the engine and pi are layered on top
and the derived image is what runs. Two builds, two cache keys, one rule — hash
the inputs, look the tag up, build only on a miss:

- any change inside the context (the Dockerfile itself, or a file it copies in)
  is a new `ralphd-base:<hash>` **and** a new `ralphd-derived:<hash>`; an
  unchanged context and an unchanged engine is two tag lookups and no build;
- the Dockerfile's own name is part of the hash, so a context carrying both
  `Dockerfile` and `Dockerfile.ci` yields two base images, not one;
- a path that does not exist, a directory, or a file with no `FROM` instruction
  is refused up front (exit `2`) naming the file — not thirty seconds into a
  build. Commit the recipe next to your repo (`ci/Dockerfile`) and the job image
  is reproducible without you;
- the run's `job.yaml` records `dockerfile:` (or `base_image:`), so
  `ralphctl resume` can replay the recipe the run started with rather than
  falling back to `ralphd:dev` — a *fallback*, used only when the image the run
  actually ran is gone (see "Resuming on the same image" below).

**Which image ran, and how it is recorded.** `start` writes the resolution into
the run dir's `host.json` (and `GET /status` / `ralphctl status --json` report
the same fields, see docs/api.md):

| field | meaning |
|-------|---------|
| `image` | the reference the container was started with |
| `imageId` | the daemon's content id for the image the container **actually got**, read from the container itself — so a pinned tag docker pulled, or a tag that moves later, is still identified |
| `imageSource` | `pinned`, `cached`, `built`, `unhashable`, `recorded` (a resume reproducing the record) or `default` (a pre-v0.6 run dir with neither a record nor a recipe) |
| `imageHash` | the content hash the tag was built from; absent for a pin, where staleness is unknowable |
| `imageBase` / `imageDockerfile` | the base and the operator recipe behind a derived image |

`ralphctl status` prints the first two as one line —
`image:     ralphd:9f2c1a4b7d80  (id 0f1e2d3c4b5a)` — and omits the line
entirely for a run dir that records no image.

**Resuming on the same image.** `ralphctl resume` must not swap the engine
mid-run, so it prefers that record over re-resolving anything, ranked:

1. `--image <ref>` pins, as everywhere else;
2. **the image this run started on**, from its own run state — by reference
   while the reference still names the recorded id, by the recorded **id** once
   a mutable tag (`ralphd:dev`) has moved. Nothing is hashed and nothing is
   built on this path, so a resume after you edited the sources (or the
   Dockerfile) continues on the same image it always ran;
3. the recipe from `job.yaml` (`base_image:`/`dockerfile:`), replayed — and
   rebuilt if it now means something else — but only once the recorded image is
   genuinely gone from the daemon;
4. `ralphd:dev`, for a pre-v0.6 run dir that recorded neither.

Every step down from 2 is a warning on stderr naming what could not be
reproduced; none of them refuses the resume.

**Which image runs, and who gets to say.** `image`, `base_image` and
`dockerfile` are three answers to one question, so they are settled as a unit:
the most specific **level** that answers at all wins whole, and the other two
keys are not filled in from further down.

| level | how you set it |
|-------|----------------|
| command line | `--image` / `--base-image` / `--dockerfile` |
| the `--template`'s `job.yaml` | `image:` / `base_image:` / `dockerfile:` |
| the registry's `config.yaml` | `ralphctl config set image` / `config set base_image` / `config set dockerfile` |
| nothing set anywhere | build `ralphd:<hash>` from the source tree |

So `--dockerfile ci/Dockerfile` on the command line **replaces** a standing
`ralphctl config set image ...` pin instead of colliding with it. Setting two of
the three *within one level* is a usage error (exit `2`): there is nothing to
rank them by, so ralphd refuses rather than picking one and being silently
wrong. `RALPHD_IMAGE` is deliberately not a level of its own — it is ambient, so
it pins when nothing else answers and is refused by name when something does.

`--base-image`/`--dockerfile` and `--image` are different things — one supplies
an ingredient, the other pins a finished image — so passing both (including
with `RALPHD_IMAGE` set) is a usage error (exit `2`), as is a base reference
that is not a plain image reference.

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
the hardcoded default (for `image`, to building `ralphd:<hash>` from source
— see "The job image" above); every other field falls straight back to its
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
              [--sort runId|state|verdict|phase|approach|tasks|iterationsUsed|startedAt]
              [--reverse]
```

Columns: run ID, state, verdict, phase, approach, tasks, iterations
used/budget, started (absolute local time via the shared formatter, see
`status` below).
The **approach** column renders the counter against its limit (task 007,
issue #16): `2/3` for a run with `maxApproaches` recorded, a bare `2` for a
pre-v0.6 run dir where the limit is unknown (the live config's limit is never
guessed in), and **blank** for a run that has not entered the review ladder
yet — never `/3`, never the literal `None`. Same renderer as `ralphctl
status` and the hub (`ralphd.engine.state.format_approach`).

The **tasks** column (task 015, issue #21) is the hub run list's TASKS cell
flattened to one string by `ralphd.engine.state.format_task_column`:

```
RUN                      STATE      VERDICT    PHASE     APPROACH TASKS        ITER    STARTED
selfdev-v06              running    None       worker    1/3      5/7 ⚠        41/250  2026-09-02 11:04:07 +0000
nightly-docs             succeeded  verified   review    2/3      7/7          18/60   2026-09-01 22:10:00 +0000
fresh-start              starting   None       None                            0/250   2026-09-02 12:00:00 +0000
```

- `5/7` — completed/total, counted from that run's own `tasks.json` with the
  engine's `state.task_counts`, through the **hardened last-good reader**
  (issue #15) with `persist=False`: a poll landing inside the agent's rewrite
  of the plan shows the last plan that parsed rather than blinking to nothing,
  and the CLI never writes a cache into somebody else's run dir.
- **blank**, never `0/0`, for a run whose agent has not written a plan yet —
  and equally for a `tasks.json` that will not parse with no last-good payload
  behind it (that is ignorance, not a plan of zero tasks).
- `⚠` marks a plan with a **validation-failed** or **in-progress** task (the
  same two states the hub flags; pending work is not trouble). The flag
  *sentences* do not fit a column, so they are not abbreviated into a private
  wording here: `--json` carries them verbatim in `tasksTrouble`
  (`["1 validation-failed", "1 in-progress"]`) and `ralphctl status <run>`
  prints the full summary.
- `stale` after the fraction means the number came from the last-good payload
  (`tasksSource: "last-good"`), the same label the hub shows as a pill.
- One **local** read per listed row (after `--state` filters), never a `GET
  /tasks` proxy call — so a run whose container is long gone shows its
  fraction exactly like a live one, and listing N runs costs no round trips.

The row itself is built once, by `TasksRead.row_fields`, and shared with the
hub's `/api/runs`: the hub cell, `ralphctl runs` and `ralphctl status` cannot
disagree about a run's progress.

`--json` emits the merged `status.json` array — in the **same order** as the
human table, with the raw ISO `startedAt` and the numeric `iterationsUsed`/
`iterationsBudget` fields kept alongside the rendered `"7/250"` string, and
both raw `approach` and `maxApproaches` numbers (either may be `null`)
alongside the rendered `approachDisplay` string. Task progress travels the
same way: raw `tasksTotal`/`tasksCompleted`/`tasksInProgress`/
`tasksValidationFailed` beside the rendered `tasksDisplay` (`5/7`),
`tasksColumn` (the terminal cell), `tasksSummary`, `tasksTrouble` and issue
#15's `tasksStale`/`tasksSource` — the same field set the hub's run-list rows
carry.

**Sorting** (task 055, issue #9) is the CLI half of the hub run list's
click-to-sort (see “Sorting” under `ralphctl ui` below) and uses the *same*
keys, the same lifecycle orders and the same raw payload values:

- default `--sort startedAt`, i.e. **newest first** — not the run-id
  alphabetical order the registry directory listing yields;
- `startedAt`, `iterationsUsed` and `approach` sort biggest/newest first;
  the text keys (`runId`, `phase`) sort A→Z; `state`/`verdict` sort in
  lifecycle order (`starting → running → succeeded → failed → aborted`, and
  no-verdict → `unverified` → `verified`); `tasks` sorts on the completion
  **ratio** `tasksCompleted / tasksTotal` (so `5/7` outranks `100/250`) and
  starts **ascending**, least-complete first — the runs that still owe work;
- `--reverse` flips whichever direction the key starts with;
- rows with a missing value for the key (no `startedAt`/`approach`/plan yet)
  sort last under an ascending key and first under a descending one, so a
  just-started run appears at the top of the default view;
- ties break on run id ascending, so the order is stable;
- `--sort`/`--reverse` compose with `--state`, which filters first.

An unrecognised `--sort` key is a usage error (exit `2`).

### `ralphctl status <run-id>`

Full status (mirrors `GET /status`; falls back to the run dir's `status.json` when
the container is gone — indicated by `"live": false` in `--json` mode).

The `phase:` line carries the approach counter against its limit (task 007,
issue #16): `phase:     worker  approach 2/3`. With no `maxApproaches`
recorded (a pre-v0.6 run dir, where `GET /status` publishes an explicit
`null`) it degrades to a bare `approach 2` rather than inventing a
denominator; for a run that has not entered the review ladder yet the
approach segment is omitted entirely (it used to read `approach None`).
`--json` always carries both raw numbers, `approach` and `maxApproaches`,
the latter `null` when unknown.

A `model:` line names the model the run is actually talking to (task 012, issue
#14) — the id **pi resolved**, as observed in its own message stream, not the
ref the operator asked for (which is `null` whenever nothing was pinned):

```
model:     amazon-bedrock/eu.anthropic.claude-opus-5  (gateway id: eu.anthropic.claude-opus-5)
```

The `(gateway id: …)` suffix appears only when the provider's own id differs
from the pi-style ref, and the whole line is omitted for a run that has not
observed a model yet (never `model: None`, same discipline as the approach
segment). `--json` carries `model` and `modelRaw`, both explicitly `null` for a
pre-v0.6 run dir — see “`model` and `modelRaw`” in docs/api.md for why the
observed id is the honest one.

An `image:` line names the job image the run is running it on (task 036, issue
#20) — the reference plus the daemon's short id for the image the container
actually got:

```
image:     ralphd:9f2c1a4b7d80  (id 0f1e2d3c4b5a)
```

The `(id …)` suffix appears only when an id was recorded, and the whole line is
omitted for a run dir that records no image (pre-v0.6), never `image: None`.
`--json` carries `image`, `imageId`, `imageSource`, `imageHash`, `imageBase` and
`imageDockerfile`, all explicitly `null` when unrecorded — the same six fields
`GET /status` serves, read from the same `host.json` record, and the same record
`resume` reproduces.

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
iteration's tokens without quoting a usable price (`usage.costPriced: false`,
including an implausible `$0` quote -- `usage.costZeroQuoted`, see
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

### `ralphctl iteration <run-id> <n>`

One iteration's own story: what it was, how long it took, why it ended, what it
cost, and its full transcript. `logs --iteration n` answers *what did the agent
do*; this answers *what happened to iteration n* (task 019, #18.1).

```
ralphctl iteration <run-id> <n> [--no-log]
```

| Option | Meaning |
|--------|---------|
| `<n>` | iteration number, as printed by `status` (`iteration: 17/250`), the `logs` boundary lines and the hub timeline |
| `--no-log` | header only — skip the transcript |

```
$ ralphctl iteration brisk-otter-1408 2
run:       brisk-otter-1408
iteration: 2  phase worker  approach 1
started:   2026-09-02 10:00:00 +0200
ended:     2026-09-02 10:17:51 +0200
duration:  17m 51s  (total)
exit:      clean exit
model:     amazon-bedrock/eu.anthropic.claude-opus-5  (gateway id: eu.anthropic.claude-opus-5)
tokens:    180,661 total (in 18, out 2,118, cache read 136,849, cache write 41,676)
cost:      $0.4231
steering:  001-focus.md
--- log (169 lines) ---
…
```

- **`exit:` is the one-line verdict**, ranked from the raw signals the engine
  records (they overlap — a timed-out iteration also has an exit code): `still
  running` · `interrupted (a signal ended the iteration)` · `no-traffic
  timeout (the model never answered)` · `iteration timeout` · `error (exit N):
  <message>` · `clean exit`
  · `exit N` · `unknown`. A non-null fault classification
  (`engine/faults.py`, the reason an attempt was retried and refunded) is
  appended as `[infra fault]`/`[work fault]` — alongside the signal, never
  instead of it. One definition (`engine.state.format_exit_reason`), shared with
  the hub's iteration dialog.
- **Purely on-disk, no container needed and no snapshot notice.**
  `iterations/NNNN/meta.json` is written by the engine itself, atomically, at
  the start and the end of every iteration, so the run dir is authoritative for
  a running job and for one whose container is long gone alike. `duration:`
  therefore says `(elapsed)` instead of `(total)` for an iteration still in
  flight.
- **Unknown is not zero.** An iteration dir whose `meta.json` is absent or
  truncated (a crash mid-write) prints `!! no readable meta.json for this
  iteration` and `exit: unknown` — not a row of `None`s and not a clean exit —
  while still printing the transcript that *is* there. A cost the provider
  never priced (including a quote of `$0` next to billable tokens, see
  `status`' cost rules) renders `unavailable`, never `$0.0000`.
- **`cost:`/`tokens:`** are that iteration alone, through the shared
  `format_cost` (4 decimals — one iteration is small money) and token
  formatter; only counters the provider actually reported are named.
- The transcript is rendered by the same merge and renderer
  `logs --iteration n` uses, so the two commands cannot show the same events
  differently; an iteration that wrote none prints `(no transcript yet)`.
- `--json` prints the whole `meta.json` verbatim (`exitCode`, `interrupted`,
  `timedOut`, `noTrafficTimeout`, `error`, `faultClass`, `usage`,
  `steeringConsumed`, `modelResolved`/`modelRaw`, `verifiedTask`/`verifyOutcome`)
  plus the derived fields the human view shows: `exitReason`, `durationS`,
  `durationDisplay`, `durationLabel`, `startedAtLocal`/`endedAtLocal`,
  `tokensDisplay`, `costDisplay`, `costStatus`, `hasMeta`, `hasTranscript`,
  `transcriptBytes`, and `log` (the rendered lines, never ANSI). With
  `--no-log` the `log` key is **absent** rather than empty — an empty list
  would claim the iteration produced no transcript.
- Exit codes: `0` · `3` run not found · `1` no such iteration in that run,
  naming the ones on disk (`run X has no iteration 47 (iterations on disk:
  1..12)`), the same code the live `logs --iteration` path returns for the
  engine's 404. Pinned by `tests/test_cli_iteration_detail.py`.
- The header block below the `run:` line is worded ONCE, by
  `ralphd.engine.state.iteration_summary_lines`, and the hub's iteration
  dialog (task 020, `GET /api/runs/<id>/iterations/<n>`) shows those very
  lines — asserted line-for-line by
  `tests/test_hub_iteration_dialog.py`, so the two surfaces cannot grow two
  vocabularies for the same `meta.json`.

### `ralphctl fault <run-id>`

Why this run is (or last was) in trouble — the fault **explained**, not just
classified (task 025, #18.4).

```
ralphctl fault <run-id>
```

```
$ ralphctl fault brisk-otter-1408
run:       brisk-otter-1408
fault:     infra (iteration 7, phase worker)
because:   the error text matched a known infra signature
signature: dns -- the endpoint's name did not resolve (pattern EAI_AGAIN, matched "EAI_AGAIN")
exit:      error (exit 0): request to https://aigw.internal/v1 failed, reason: getaddrinfo EAI_AGAIN aigw.internal [infra fault]
ladder:    attempt 3 (no cap: the outage budget is the stopping rule), waits so far 30s, 1m, 2m, next attempt at 2026-09-04 13:14:03 +0200
budget:    3m 30s of 4h spent waiting (3h 56m left); 5m 30s of infra waits in this run
health:    degraded (sitting out a backoff wait right now)
```

Every fact above was already on disk and nothing joined it up: `status`
printed `degraded:` with the countdown, `iteration <n>` printed `[infra
fault]`, and *which* signature fired, *how far* up the retry ladder the run
has climbed and *how much* of the outage budget is gone had to be
reconstructed by knowing `engine/faults.py`' table by heart and grepping
`events.jsonl`.

- **`fault:`** is the class the engine recorded and acted on (`infra` ·
  `work`), with the iteration and phase it happened in — read from the newest
  `iterations/NNNN/meta.json` that carries a `faultClass`.
- **`because:`** names which branch of the classifier decided it, in the
  classifier's own words: the startup watchdog fired (no LLM traffic at all) ·
  the error text matched a known infra signature · the agent reached the model
  and then failed · a signal ended the iteration after it had reached the model
  · no traffic and no recognized signature (an unclassifiable no-traffic failure
  is treated as infra) · an abort/interrupt recorded for the run, which is never
  retried as an outage.
  The abort branch is worded by **what the engine can establish** (steering 004):
  `operator_abort_requested` is equally true for a `POST /abort`, a `POST
  /interrupt` and the engine giving up on its own (an exhausted outage budget, a
  `SIGTERM` from anywhere), so it says *operator-requested* only when the abort
  demonstrably arrived from outside (`_operator_abort_recorded`); otherwise it
  says an abort/interrupt is recorded and that who asked for it is not
  established, and `gave up:` quotes the recorded reason verbatim (`signal 15`)
  instead of attributing it to a person who may not exist. The *classification*
  of these shapes is unchanged and still coarse — an engine-side give-up is
  still `work` — which is issue #23, not this surface's business.
- **`signature:`** is the row of `engine/faults.py`'s `INFRA_SIGNATURES` table
  that matched: its family (`dns` · `tcp` · `stream` · `tls` · `sdk` ·
  `http-5xx` · `backpressure` · `bedrock-stream` · `capacity`), what that
  family means, the pattern, and the exact substring it matched in the error.
  `(no signature matched)` for a work fault or a watchdog kill — never a guess.
- **`ladder:`** is the run's *own* recorded retry attempts (one `infra_retry`
  event each), not `infra_retry_backoff_s` re-simulated: a wait cut short by
  `ralphctl retry`, a wait clamped by what was left of the budget and reflect's
  own shorter budget all read truthfully. `attempt 3 of 6` when
  `infra_retry_max` is set, otherwise `no cap: the outage budget is the
  stopping rule` (the real stopping rule, see `status`' `degraded:` line). The
  pre-reflect delay is reported as what it is, not as a retry.
- **`budget:`** is that outage episode's spend against `infra_outage_budget_s`,
  plus the run-wide `infraWaitTotalS` when earlier outages added to it. Infra
  waits extend the job deadline, so this time never counts against
  `job_timeout_s`.
- **`recovered:`** appears when the engine emitted `infra_recovered` — a later
  iteration reached the model, so the episode is over and the ladder is not a
  live one. **`gave up:`** prints `status.json`'s `abortReason` when the run
  stopped on the fault.
- **Purely on-disk, no container needed and no snapshot notice**, like
  `iteration`/`docs`/`artifacts`: `status.json`, `events.jsonl` and the
  iteration metas are all the engine's own writes, so a live run and one whose
  container is long gone read identically.
- **Unknown is not zero.** A run that never faulted prints `(no fault
  recorded)` rather than an empty block. A fault whose `meta.json` is
  unreadable (mid-write, or an iteration dir removed by hand) is still
  explained from the run's own retry events, saying where the verdict came
  from instead of inventing a branch. And when the class the engine recorded
  differs from what the error text alone implies — an abort/interrupt recorded
  for the run (by the operator, or by the engine giving up) is the usual,
  legitimate cause — the divergence is printed (`!! the engine
  recorded a different class than this error alone implies`); the engine's
  verdict is what the run acted on and is never overwritten.
- `--json` carries the whole shaping: `faultClass`, `reason`, `signature`
  (`family`/`description`/`pattern`/`match`), `iteration`, `phase`, `error`
  (full text, untruncated), `iterationDetail` (`iteration`'s own dict),
  `ladder` (`attempt`, `maxAttempts`, `attempts`, `backoffsS`,
  `nextAttemptAt`/`nextAttemptAtLocal`, `display`), `budget` (`waitedS`,
  `budgetS`, `remainingS`, `totalWaitedS`, `display`), `state`, `health`,
  `waiting`, `recovered`, `abortReason`, `notices`, `hasFault`, plus
  `summaryLines` and `text` (the human block, verbatim).
- Exit codes: `0` · `3` run not found. A run with no fault is **not** an error:
  "nothing went wrong" is an answer. Pinned by
  `tests/test_cli_fault_explanation.py`.
- The block below the `run:` line is worded ONCE, by
  `ralphd.engine.state.fault_summary_lines`, so the hub's fault dialog (task
  026) explains the same fault in the same words: the hub's `GET
  /api/runs/<id>/fault` serves this exact document (`--json`'s shape, `text`
  included) and the badge on a degraded or failed run's card renders that `text`
  verbatim — see the `ralphctl ui` section's run-detail description.

### `ralphctl cost <run-id>`

What this run spent, **per phase and per approach**, with every kind of money
labelled (task 027, #18.5).

```
ralphctl cost <run-id>
```

```
$ ralphctl cost brisk-otter-1408
run:       brisk-otter-1408
cost:      $0.5000 + ~$1.2500 derived, partial (rest unavailable)
tokens:    40,000 total (in 1,200, out 3,400)
model:     amazon-bedrock/eu.anthropic.claude-opus-5  (gateway id: eu.anthropic.claude-opus-5)
by phase:
  planning  $0.5000           10,000 tokens
  worker    ~$1.2500 derived  20,000 tokens
  verify    unavailable       10,000 tokens
  reflect   (none)
by approach:
  1  $0.5000+ (partial, rest unavailable)  30,000 tokens
  2  ~$1.2500 derived                      10,000 tokens
legend:    a bare amount was quoted by the provider; ~ marks money derived from the host-side rate table; unavailable means tokens were billed that nothing priced
```

`status.json`'s `usage` has carried `byPhase`/`byApproach` buckets since
day one (`loop._accumulate_usage`, PRD req 19) and nothing rendered them:
`status` printed one headline plus a hard-coded `planning/worker/review`
parenthetical (two of which a vigilant run does not even use), and the hub's
usage card showed the raw numbers. "Which phase burned the tokens" and "how
much of this figure is actually known" meant reading JSON by hand.

- **`cost:`** is the headline — the *same* string `ralphctl status` and the
  hub's usage card show, produced by the one shared formatter
  (`ralphd.engine.state.format_cost`, `decimals=4` here), so a breakdown can
  never disagree with the number printed beside it.
- **`tokens:`** is the run total in full (`format_tokens`, the counters the
  provider actually reported — no zeroed cache fields are invented). Each table
  row carries that bucket's one-number token count instead.
- **`source:`** appears only when the money string does not already say where
  the money came from: `provider-priced` for a real quote, `declared free` for a
  route your `pricing.free:` patterns declare free, `no traffic` for the
  historical `$0.00`-with-no-tokens sentinel. `derived`, `partial` and
  `unavailable` are spelled by the amount itself, so they are not repeated.
- **`model:`** is the id recorded in run state (task 012), with the raw gateway
  id in brackets when it differs — i.e. the id the rate table was asked about.
  Omitted entirely for a run that never observed one.
- **`by phase:` / `by approach:`** are the run's own buckets: phases in the
  engine's order, approaches numerically (`10` after `2`). A bucket that
  recorded nothing renders `(none)` — never `$0.00`, which would read like a
  phase that ran for free.
- **`!!` notices** name the anomaly behind a wall of `unavailable`: a provider
  that quoted `$0` for billable tokens (task 049) is reported as unpriced, and
  the notice points at `artifacts/reports/pricing-anomaly.md` — the same
  sentence every other cost surface uses.
- **`legend:`** is printed only when `derived`/`partial`/`unavailable` actually
  occur, so a fully priced run's breakdown is not padded with an explanation of
  vocabulary it never uses.
- **Purely on-disk, no container needed and no snapshot notice**, like
  `iteration`/`docs`/`artifacts`/`fault`: `status.json` is the engine's own
  atomic write, so a live run and one whose container is long gone read
  identically. A forged `costDisplay`/`tokensDisplay` in `status.json` is
  always recomputed from the numbers beside it.
- **Unknown is not zero.** A run that recorded no usage at all prints `(no
  usage recorded)` rather than a table of zeros, and is **not** an error.
- `--json` carries the raw numbers plus the rendered strings: `total` and the
  `byPhase`/`byApproach` **lists** (each entry = that bucket's own counters plus
  `key`, `tokens`, `tokensDisplay`, `tokensTotalDisplay`, `costDisplay`,
  `costSource`), `costDisplay`/`costStatus`/`costSource` for the run,
  `model`/`modelRaw`, `sources`, `notices`, `hasUsage`, plus `summaryLines` and
  `text` (the human block, verbatim, minus the `run:` line).
- Exit codes: `0` · `3` run not found. Pinned by
  `tests/test_cli_cost_breakdown.py`.
- The block below the `run:` line is worded ONCE, by
  `ralphd.engine.state.cost_breakdown_lines`, so the hub's cost-breakdown
  dialog (task 028, `GET /api/runs/<id>/cost`, opened by the usage card's cost
  cell) shows the same numbers in the same words.

### `ralphctl docs <run-id> [name]`

A run's own **state documents** — the prose a run leaves behind, plus the config
it was launched with (task 021, #18.2):

| Key | File | What it is |
|-----|------|------------|
| `notes` | `notes.md` (run dir) | handoff notes the worker rewrites every iteration |
| `findings` | `review-findings.md` (run dir) | the reviewer's findings that sent the run into another approach |
| `composite-prd` | `composite-prd.md` (run dir) | the PRD text the agent works from once an approach restarts |
| `job` | `job.yaml` (config dir) | effective job config as inlined at `start`, **secret values redacted** |

```
ralphctl docs <run-id>            # which documents exist, with sizes
ralphctl docs <run-id> <name>     # one document: header block + full body
```

`<name>` is a key or the file name (`notes` and `notes.md` both work,
case-insensitively). The original `prd.md` is not in this list — it has its own
surface (the hub's PRD dialog, `GET /prd`).

```
$ ralphctl docs brisk-otter-1408
run:       brisk-otter-1408
DOCUMENT      FILE                         SIZE  DESCRIPTION
notes         notes.md                    2,914  handoff notes the worker rewrites every iteration
findings      review-findings.md          1,102  the reviewer's findings that sent the run into another approach
composite-prd composite-prd.md    (not written)  the PRD text the agent works from (written when an approach restarts)
job           job.yaml                      412  effective job config as inlined at start, secret values redacted

$ ralphctl docs brisk-otter-1408 job
run:       brisk-otter-1408
document:  job  (job.yaml)
purpose:   effective job config as inlined at start, secret values redacted
size:      412 bytes
note:      secret values redacted
--- job.yaml ---
run_id: "brisk-otter-1408"
iterations: 250
api_token: "***REDACTED***"
on_complete_cmd: "curl -H 'Authorization: Bearer [REDACTED:github.env:GITHUB_TOKEN]' https://ci"
model: "amazon-bedrock/eu.anthropic.claude-opus-5"
price_strategy: "aws"
```

- **`job.yaml` is redacted mechanically, on two independent bounds**, because
  the alternative — remembering not to `cat` it — has already failed twice in
  this project (see `src/ralphd/engine/redact.py`): every value under a
  secret-shaped key **name** (`api_token`, a nested `AWS_SECRET_ACCESS_KEY`,
  anything matching `TOKEN|KEY|SECRET|PASSWORD`) is replaced with
  `***REDACTED***`, and every **value** this host knows to be a secret — from
  its own environment, `~/.creds`, and the run's own staged config dir
  (`creds/*.env`, `llm-wiring.json`, `env-wiring.json`, `pi/models.json`) — is
  scrubbed as `[REDACTED:<label>]` wherever it appears, including inside an
  innocently-named key like `on_complete_cmd`. Key order, structure and every
  non-secret value survive verbatim, so the output is still the file. Output of
  this command is safe to paste into an issue; `cat`-ing the file is not.
- **Which documents exist is part of the answer.** A document this run never
  wrote is a listed row saying `(not written)`, never a dropped line; asking for
  it exits `1` and names the documents that *are* on disk. A file that exists
  but is blank prints `(empty)` — two different facts, two different words.
- **Purely on-disk, no container needed and no snapshot notice**, like
  `ralphctl iteration`: these files are written into the run dir and the config
  dir by the agent, the engine and `start` itself, so a live run and one whose
  container is long gone read identically. Nothing is written or created by
  reading (no `RunDir` construction — a viewer must not mkdir into somebody
  else's run dir).
- `--json` on the listing prints `{runId, documents: [...]}` with
  `key`/`name`/`where`/`title`/`path`/`available`/`exists`/`bytes`/`redacted`
  and **no bodies**; `--json` with a name adds `body` (already redacted — there
  is no raw back door) and `text`, the complete rendering the human view prints.
  `available: false` means the reader had no config dir to look in, which is
  not the same as the document being missing.
- Exit codes: `0` · `1` the named document is not there · `2` unknown document
  name (the message lists the keys) · `3` run not found. Pinned by
  `tests/test_cli_run_documents.py`.
- The header block and the listing are worded ONCE, by
  `ralphd.engine.state.run_document_summary_lines` /
  `format_run_document_listing` / `format_run_document_size`, so the hub's
  **State documents** panel and its dialogs (task 022, `GET
  /api/runs/<id>/documents[/<name>]`) show the very same lines and the very
  same size/absence cells.

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
| `--list` | **show** this run's steering messages instead of sending one (task 018, #17) |

The on-disk filename is always `NNN-<slug>.md`, where `NNN` is an
engine-assigned monotonic sequence (never supplied by the caller). If
`--name` already carries its own `NNN-` prefix (e.g. copy-pasted from a
prior steering filename), that prefix is stripped before appending the
engine's own, so the result is never doubled (e.g. `--name 019-steering`
does not yield `022-019-steering.md`).

Exit `0` accepted · `5` job already finished.

#### `ralphctl steer <run-id> --list`

Steering used to be **write-only**: after posting a message there was no way to
see what was queued, what the loop had already applied, or what the text said.
`--list` is the terminal view of the same history the hub's run-detail
"Steering history" panel shows — literally the same code
(`ui_server.steering_list`), so the two surfaces cannot drift:

```console
$ ralphctl steer brisk-otter-1408 --list
SEQ  STATE    ARRIVED                    NAME                MESSAGE
  1  applied  2026-09-03 11:04:07 +0000  cost-zero-quote     A quoted cost of 0 beside 500k tokens is…
  2  pending  2026-09-03 12:31:55 +0000  dont-pattern-kill   Never signal a process by pattern from…
```

* **Live-first, with an on-disk fallback.** A running job's own
  `GET /steering` answers (it is the process that decides when an entry
  becomes `applied`); when the container is gone the run dir's `steering/`
  directory is read directly through the one shared reader
  (`engine.state.steering_entries`), and stderr carries
  `on-disk snapshot: the run's API is not reachable, showing the steering
  messages recorded in the run dir` — the same phrase `logs` and `tasks` use.
* `MESSAGE` is a one-line preview (whitespace collapsed, truncated with `…`);
  `--json` carries every entry in full — `file`, `seq`, `name`, `ts`,
  `tsLocal`, `state`, `consumed`, `bytes`, `hasBody`, `body` — plus
  `live: true|false`, exactly the shape
  `GET /api/runs/<id>/steering` serves.
* A run nobody ever steered prints `(no steering messages)`
  (`ui_server.NO_STEERING`, the hub's own wording), not zero bytes.
* `--list` is a **read**: it never touches stdin, and combining it with a
  message / `--file` / `--name` / `--now` is exit `2` rather than a surprise
  POST. Unknown run id is exit `3`; an unreachable API is *not* an error here.

Pinned by `tests/test_cli_steer_list.py` (including a real engine steered from
the CLI: pending → applied → container gone, agreeing with the hub at every
step).

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

### `ralphctl rm <run-id> [--force]`

Delete a run's registry dir (history, artifacts, workspace-if-internal) and its
persisted config dir, and reap any containers still labeled `ralphd.run=<run-id>`.
Asks confirmation unless `--yes`.

Plain `rm` requires the container to be gone: while a container record exists it
exits `5` with "container still exists — `stop` first (or `rm --force`)".

`--force` (task 029, #19) stops that container first and then deletes, so
disposing of a finished run is one command instead of `stop` + `rm`. It runs
exactly `stop`'s teardown (`/shutdown`, `docker rm -f` the job container, reap
the run's siblings, record the operator-termination marker), so the sibling and
label discipline is the same one `stop` uses.

`--force` is a shortcut past a **stale** container, not a way to kill live work:
it deletes only when the run's recorded state is terminal (`succeeded`,
`failed`, `aborted`). Anything else — `starting`/`running`, an unrecognized
state, or a `status.json` that is missing or unreadable — exits `5` with
"job still running (state: …) — `abort` first, then `rm --force`" and touches
nothing at all: no container is removed and neither directory is deleted.
Killing a live job stays explicit (`abort`, or `stop --force`).

A run with no container record is unaffected by `--force`: it takes the plain
path (siblings reaped, both directories deleted), so a zombie run dir still
recording `running` remains deletable as before. `--json` prints
`{"removed": "<run-id>", "stoppedContainer": true|false}`.

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

### `ralphctl artifacts <run-id> [ls|show <artifact>|pull <dest>]`

What the job left behind in the run dir's `artifacts/` — above all the `reflect`
phase's post-mortem and the prompt/skill diff it proposes (task 023, #18.3):

```
ralphctl artifacts <run-id>                # same as `ls`
ralphctl artifacts <run-id> ls             # the whole tree: size, name, path
ralphctl artifacts <run-id> show report    # one artifact: header block + body
ralphctl artifacts <run-id> pull ./out/    # copy the tree out (default: ./artifacts)
```

`show` takes a well-known name or any path under `artifacts/`:

| Name | Path | What it is |
|------|------|------------|
| `report` | `reflection/report.md` | the reflect phase's post-mortem report |
| `suggestions` | `reflection/suggestions.diff` | the prompt/skill diff the reflect phase proposes (never applied) |
| `reflect-failed` | `reflection/FAILED.md` | why the reflect phase left no report |

```
$ ralphctl artifacts brisk-otter-1408 ls
run:       brisk-otter-1408
      SIZE  NAME            PATH
     3,120  report          reflection/report.md
       844  suggestions     reflection/suggestions.diff
     1,905                  reports/pricing-anomaly.md
   142,338                  screenshots/hub/24-document-dialog.png

$ ralphctl artifacts brisk-otter-1408 show suggestions
run:       brisk-otter-1408
artifact:  reflection/suggestions.diff  (suggestions)
purpose:   the prompt/skill diff the reflect phase proposes (never applied)
size:      844 bytes
--- reflection/suggestions.diff ---
--- a/prompts/worker.md
+++ b/prompts/worker.md
@@
-...
+...
```

- **One resolver, one traversal guard.** `report`,
  `reflection/report.md` and `artifacts/reflection/report.md` are the same
  artifact (so every spelling the listing shows also works as an argument), and
  a name that is not addressing an artifact at all — empty, absolute, or
  containing `..` — is a usage error (`2`), decided once in
  `ralphd.engine.state.artifact_relpath` because the hub (task 024) puts that
  string in a URL.
- **A binary artifact is described, never printed**: `show` on a screenshot
  prints `(binary file -- copy it out with ralphctl artifacts <run> pull)`
  instead of spraying the terminal. A file that exists but is blank prints
  `(empty)`; one that was never written exits `1` and names what *is* on disk —
  the `ralphctl docs` rule, same words.
- **Purely on-disk, no container needed and no snapshot notice**, like
  `ralphctl docs`/`iteration`: the agent writes these files into a directory the
  host holds, so a live run and one whose container is long gone read
  identically. `pull` has always worked that way and still does.
- `--json` on the listing prints `{runId, artifacts: [...]}` with
  `path`/`key`/`title`/`file`/`available`/`exists`/`bytes`/`isText` and **no
  bodies** (a listing must not ship the artifacts themselves — the hub polls
  it); `--json show <artifact>` adds `body` and `text`, the complete rendering
  the human view prints; `--json pull` prints `{pulled}`.
- Exit codes: `0` · `1` the named artifact is not there · `2` not an artifact
  name (the message lists the well-known ones) · `3` run not found. Pinned by
  `tests/test_cli_artifacts.py`.
- The listing and the header block are worded ONCE, by
  `ralphd.engine.state.format_artifact_listing` / `artifact_summary_lines` /
  `format_artifact_size`, so the hub's artifacts panel and dialog (task 024,
  `GET /api/runs/<id>/artifacts[/<path>]`) show the very same lines and the very
  same size cells — an artifact cannot be described as missing in one surface
  and empty in the other.

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

There is no start-time `--prompt-override` flag: a prompt is either overridden
live with `set` (the run's own config dir is a read-only mount, written by
`start`) or replaced in the source tree of the image being built.

### `ralphctl llm`

LLM profile inspection:

```
ralphctl llm profiles                 # list profiles (~/.ralphd/llm-profiles)
ralphctl llm show <profile>           # resolved (redacted) view
ralphctl llm test <profile>           # spin up a throwaway container, 1-token ping
```

Mid-run rotation of a live job's endpoint/key is an **API-only** capability in
v0.6 (`PUT /config/llm`, docs/api.md): there is no `ralphctl llm set` wrapper
yet.

`profiles` lists the two built-ins (`host`, `none`, tagged `(builtin)`) followed
by every `<name>.yaml` under `<registry>/llm-profiles/`, in that order.
`--json` emits `[{"name": ..., "builtin": true|false}, ...]`.

`show <profile>` fully resolves the profile (same resolution `start --llm
<profile>` performs) and prints it with every `env:` value replaced by
`***REDACTED***` and every `pi:` field that came from a
`${env:}`/`${file:}`/`${cmd:}` reference likewise masked -- literal `pi:`
fields (e.g. `baseUrl`) stay visible so the resolved shape is still useful
for diagnosis. `model`/`fast_model`/`price_strategy` are printed as declared
(`(unset)` when the profile has no opinion). `host`/`none` have no file to
resolve; `show` reports them as
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
`start`'s flags of the same name. `--image` on `resume` pins as always; with no
flag, `resume` **reproduces the image this run started on** — the reference and
content id recorded in the run's `host.json` (`imageId`), reused without hashing
or building anything, even after the sources or the Dockerfile changed. Only
when that image is gone from the daemon does it fall back to replaying the
`base_image:`/`dockerfile:` recipe recorded in `job.yaml`, and to `ralphd:dev`
for a run that recorded neither; every step down is a warning on stderr. The
full ranking is under "Resuming on the same image" in `start` above.
The resolved `pi` config and creds/skills
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
(created on first `set`). Recognized keys: `image`, `base_image`, `dockerfile`
(the three image supply points, ranked as one unit — see "The job image"),
`on_complete`
(`idle`/`exit` — validated on `set`), `default_llm_profile` (any string;
`ralphctl doctor` resolves it as an LLM profile name, see below), `network`
(any string; same values `--network` accepts, e.g. `host`), `auto_resume`
(`true`/`false` — validated on `set` and stored as a real boolean; the
registry-wide default for `start --auto-resume`), `price_strategy`
(`none`/`aws` — validated on `set`; the registry-wide default for
`start --price-strategy`).

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
same-named flags (`--image`, `--base-image`, `--dockerfile`, `--on-complete`,
`--network`, `--auto-resume`,
`--price-strategy`)
and for `--llm`
(via `default_llm_profile`), between an explicit flag/`--template` value and
the hardcoded built-in default: explicit flag > `--template` > `ralphctl
config` default > hardcoded default. `llm test`/`doctor`'s own
`--image` flags are unaffected (still default to the hardcoded image), and
`resume`'s prefers the image the run recorded (see `resume` below). Note
that for the three image keys there is no hardcoded final fallback on `start`:
with nothing set anywhere, `start` builds `ralphd:<hash>` from source instead of
selecting a tag, and the three are ranked *as one unit* rather than key by key
(see "The job image").

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
  free:
    - "ollama/*"                           # declared free: a $0 here is real
```

Rates are USD per **million** tokens, keyed like the usage counters
(`input`, `output`, `cacheRead`, `cacheWrite`); an absent cache rate falls back
to the `input` rate rather than to a silent `$0`. An exact model key beats a
wildcard one, and the longest wildcard prefix wins.

`free:` (v0.6) is a list of model-id patterns you **declare** cost nothing,
matched with the same rules after aliasing. It exists because a provider
quoting exactly `$0` for billable tokens is not evidence of a free route --
some gateways quote zero when their model definition carries no rates, so an
undeclared zero is treated as *unknown* (`costZeroQuoted`, see `docs/api.md`
and `artifacts/reports/pricing-anomaly.md`). A declared-free route keeps
printing `$0.00`. A `pricing:` map may consist of `free:` alone.

- `ralphctl start` **inlines** the map into the run's `job.yaml` (`pricing`),
  so the rates a run uses are the ones it started with and survive every later
  `resume`; a single run can also be pointed at a map with
  `RALPHD_PRICING='{"models": ...}'`. The resolved table is visible in
  `GET /config` (`pricing`).
- It is consulted **only** when the provider quoted no price (which since v0.6
  includes a quote of exactly `$0` over billable tokens, unless the route is
  declared `free`), and the result is
  published separately as `costDerivedUSD` (never merged into `costUSD`) and
  rendered as `~$0.45 derived` everywhere (`status`, the `logs` footer, the
  hub) -- a derived cost is never passed off as a provider-reported one.
- No map configured (the default) changes nothing: unpriced traffic stays
  `unavailable`, never a guessed number.

#### Built-in AWS Bedrock rate table (`builtin-aws-bedrock`)

Hand-writing `pricing:` is why it is usually unset, and it is unnecessary for
the most common unpriced route: an AIGW-style gateway in front of Bedrock bills
exactly Bedrock list price, so ralphd ships those rates itself
(`src/ralphd/engine/pricing_aws.py`, v0.6, #14). A cost computed from it is
still **derived** (`costDerivedUSD`, `~$0.45 derived`) -- a built-in table is
not a provider quote -- and an operator `pricing:` map always wins over it.

**Selecting it: the `price_strategy` knob (v0.6).** The table is consulted only
when a run opts in, because a number ralphd invented must never appear beside a
run that never asked for one:

```console
$ ralphctl start --prd prd.md --price-strategy aws     # this job
$ ralphctl config set price_strategy aws               # every job on this host
```

Accepted values are `none` (the default: an unpriced route stays
`unavailable`) and `aws` (the built-in Bedrock table may derive a cost). The
resolution order is the usual one — explicit `--price-strategy` >
`--template`'s `job.yaml` > `ralphctl config set price_strategy` >
the `--llm` profile's own `price_strategy:` (see `docs/llm-profiles.md`: a
gateway profile is what knows which table bills its routes) > the engine
default `none`. `start` writes the resolved value into the run's `job.yaml`
(`price_strategy`), so `ralphctl resume` keeps the strategy the run started
with instead of re-deriving it from the resuming shell. A single run can also
be pointed at a strategy with `RALPHD_PRICE_STRATEGY=aws`, and the effective
value is visible in `GET /config` (`priceStrategy`). An unrecognised value
in `job.yaml`/the env degrades to `none` with a warning in the engine log
rather than failing the job — the fallback can only withhold a derived number,
never invent one — while `--price-strategy`/`config set` reject a bad value up
front (exit `2`).

What the table does, and deliberately does not, resolve:

- The `<provider>/` segment pi and the gateways prepend
  (`amazon-bedrock/`, `aigw-openai/`, `bedrock-mantle/`, ...) is **aliased
  away**: it carries no pricing information, and the same
  `openai.gpt-5.6-sol` costs the same through either gateway.
- The region segment (`eu.`, `us.`, `jp.`, `au.`, `global.`) is **kept**: EU
  sits ~10% above us-east and some ids differ far more, so
  `eu.anthropic.claude-opus-5` has its own entry rather than borrowing
  us-east's price.
- An id the table does not know resolves to **nothing**, so the cost stays
  `unavailable` instead of borrowing a neighbouring model's or region's rate.
- A `0` cache rate is never stored; as with an operator map, an absent cache
  rate falls back to the `input` rate rather than to a silent `$0`.
- Lookups go through the same `PricingMap` rules as an operator map (one alias
  hop, exact key beats wildcard, longest wildcard prefix wins).

**Which table produced a rate (v0.6, #14).** With `price_strategy: aws` the two
tables are *layered*, never merged: the operator's `pricing:` map is consulted
first and the built-in table only answers ids the operator map does not cover,
so exactly one table prices any given message (never a sum of both) and its
identity stays visible. `GET /config` reports it as `priceTables`:

```json
{"names": ["operator map", "builtin-aws-bedrock"],
 "answers": "operator map, then builtin-aws-bedrock",
 "tables": [{"name": "operator map", "models": 2, "aliases": 1, "free": 0},
            {"name": "builtin-aws-bedrock", "asOf": "...", "stale": false, "models": 114}]}
```

`answers` reads `neither` when nothing can price the run's routes -- which is
precisely why such a cost renders `unavailable` rather than `$0.00`. Both
triggers reach the derivation: a provider that quotes **no** cost block, and
one that quotes an implausible `$0` beside billable tokens (the live AIGW case,
`artifacts/reports/pricing-anomaly.md`). A route declared free in `pricing:`
`free:` is still free: a declaration outranks every rate table, built-in ones
included.

**Which id gets priced (v0.6, #14).** The ref the job pinned (`model:`, or a
per-phase `models:` entry) if there is one -- an operator naming a ref is
choosing which rate applies, so an unknown pinned ref reports `unavailable`
rather than borrowing the rate of whatever the gateway routed to. When nothing
is pinned, pi picks its own model and the rate is looked up against the id pi
*reported* using (`status.json`'s `model`, `meta.json`'s `modelResolved`) -- so
`price_strategy: aws` derives money for an unpinned run too, which is the shape
most real runs have.

**Provenance, as-of date and refresh.** The rates mirror pi-ai's bundled
Bedrock provider data (`@earendil-works/pi-ai/.../data/amazon-bedrock.json`,
the same numbers pi itself prices a request with), cross-checkable against
<https://aws.amazon.com/bedrock/pricing/>. The mirror is generated, carries a
machine-readable as-of date (`pricing_aws.AS_OF`) and reports its own
staleness (`pricing_aws.staleness()`: `asOf`, `ageDays`, `staleAfterDays`,
`stale`, `source`, `sourceVersion`, `refresh`) -- a rate table with no as-of
date is a future lie, so building the map logs a warning once it is older than
`staleAfterDays`. AWS changes prices; refresh with:

```console
$ python tools/refresh_bedrock_rates.py            # rewrite the table + as-of date
$ python tools/refresh_bedrock_rates.py --check    # non-zero if out of date
```

### `ralphctl doctor`

Preflight checks (`checks` in `--json` output; overall `ok` is the AND of all
of them, exit code `0`/`1` accordingly):

- `docker` — the docker daemon is reachable.
- `image` — the job image is **available**: present on this daemon, or
  something ralphd can build from this source tree. (Before v0.6 this was
  bare presence, which failed a fresh checkout for an image `start` would
  have built anyway and passed for a pinned reference years older than the
  source.) A pinned reference that is missing still fails the check — a pin is
  run as-is, never built, so a run on it cannot start.
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

#### Job-image staleness (#20 H4)

Beyond "does an image exist", `doctor` answers *is the image this host runs
jobs on the one this source tree builds?* — it hashes the image inputs
(`container/`, `pyproject.toml`, `src/ralphd/`; see
[architecture.md](architecture.md)) and compares that hash with the tag in use.
One line in the human report, `imageStaleness` in `--json`:

```
! job image (stale, from RALPHD_IMAGE): ralphd:aaaaaaaaaaaa was built from different image inputs than this source tree, whose job image is ralphd:867111e69aec -- a run on ralphd:aaaaaaaaaaaa executes an engine that is not this source
```

The line is prefixed `!` only when it is news to act on (`stale`/`missing`).
Four verdicts, because two would have to lie:

| `staleness` | meaning |
| --- | --- |
| `fresh` | the reference is `ralphd:<hash>` and the hash **is** this source tree's |
| `stale` | it is `ralphd:<hash>` from some *other* source tree — a run on it executes an engine that is not this checkout |
| `missing` | not on this daemon. If it is this tree's own tag, the next `start` builds it (the check still passes); otherwise nothing here builds it |
| `unknowable` | it cannot be compared to a source hash at all — and is therefore never reported as up to date |

`unknowable` covers, deliberately: an operator pin / registry reference /
hand-built tag (`ralphd:dev`); a **derived** `ralphd-derived:<hash>`, whose hash
covers its base image as well as ralphd's source; a **base**
`ralphd-base:<hash>`, whose hash covers an operator's build context; and an
install with nothing to hash — neither a checkout (no `container/Dockerfile`)
nor packaged image inputs. The three tag namespaces are never compared with
each other. A `pipx` install *is* comparable: its package data hashes to the
same source hash a checkout does, so it gets a real verdict, and `inputs` says
where that hash came from.

`imageStaleness` fields: `image` (the reference reported on, `null` when the
registry supplies a `base_image:`/`dockerfile:` and the job image is therefore
a derived tag that does not exist until `start` derives it — the base is named
in `imageBase` instead of a tag being guessed), `imageKind`
(`default`/`derived`/`base`/`unhashed`/`none`), `imageHash` (the comparable
hash, `null` when there is none), `imageSource` (task 036's provenance word for
a run's recorded image), `sourceHash`/`sourceImage`/`sourceRoot` (this tree's
hash, the tag it produces, and where it was hashed), `inputs` (where this
install's image inputs came from: `checkout`, `packaged` — a wheel/pipx
install's own package data, also named in an extra report line — or `none`),
`present`, `staleness`,
`where` (which level supplied the reference: `--image` > `RALPHD_IMAGE` > the
registry's `image:` > this source tree's inputs) and `note` (the sentence the
human report prints — worded once, so text and JSON cannot disagree).

`--json`'s `runImageStaleness` applies the same verdict per **run recorded
non-terminal**, against the image that run's own `host.json` records (see
`resume` above): the literal "tag in use", and the case this check exists for —
a live run executing an engine that predates the fix it is watching for. It is
a hash comparison over run state, so it costs one file read per run and no
`docker` call, and it never claims an image is gone. Terminal runs are left out
(an image that was current while the run happened is not stale afterwards).
Stale runs — and only those — are listed in the human report, report-only:

```
! runs recorded non-terminal whose own job image is not this source tree's:
    myrun  ralphd:aaaaaaaaaaaa was built from different image inputs than this source tree, whose job image is ralphd:867111e69aec -- ...
```

Staleness never affects `ok`: running an old engine on purpose is supported
(`--image`), being unable to tell is not.

`--image REF` reports on `REF` instead of the reference this host would resolve;
with `--fix` it also pins the image auto-resumed runs restart on (by default
each run restarts on the image it recorded at start time, see `resume`).

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
  verdict, phase, approach, maxApproaches, approachDisplay, iterationsUsed,
  iterationsBudget, startedAt, containerGone, tasksTotal, tasksCompleted,
  tasksInProgress, tasksValidationFailed, tasksDisplay, tasksSummary,
  tasksTrouble, tasksColumn, tasksStale, tasksSource, deletable,
  deleteRefusal}, ...]}`, read straight from every
  `runs/*/status.json` (no
  live proxy calls, so listing stays cheap regardless of how many runs are
  dead). `containerGone` (task 024) is `true` only for a run whose recorded
  state is non-terminal (`starting`/`running`) while its API port does not
  accept a connection — i.e. the container died without recording a terminal
  state. Only those runs are probed, with a concurrent loopback TCP connect
  (~0.3s worst case for the whole sweep, no docker CLI involved); a terminal
  run is unreachable by design and always reports `false`. Task 008 (issue
  #16) adds `approachDisplay` — the counter rendered as `2/3` by the one shared
  formatter `ralphctl runs`/`status` print through
  (`ralphd.engine.state.format_approach`) — next to (never replacing) the raw
  `approach`/`maxApproaches` numbers the hub sorts the APPROACH column on.
  Either raw number may be `null`: no `maxApproaches` renders a bare `2`
  rather than borrowing this host's configured limit, and no `approach` at all
  renders an empty string rather than `/3`.
  Task 013 (issue #21) adds the task-progress fields, from **one local read of
  that run's `tasks.json` per row** through the engine's hardened reader
  (`read_tasks_doc(..., persist=False)`) and `task_counts` — still no live
  proxy call, so a finished run whose container is gone reports its progress
  exactly like a live one. `tasksTotal`/`tasksCompleted`/`tasksInProgress`/
  `tasksValidationFailed` are the raw counts (what the hub sorts on);
  `tasksDisplay` is the rendered `5/7`, `tasksSummary` is the same sentence
  `ralphctl status` prints (`5/7 completed (1 in-progress, 1
  validation-failed)`) and `tasksTrouble` is the list of trouble flags
  (`["1 validation-failed", "1 in-progress"]`) worded by that same renderer
  (`ralphd.engine.state.format_task_counts`/`format_task_fraction`/
  `format_task_trouble`) — one vocabulary for CLI and hub. A run with no plan
  (none written yet, an empty plan, or a `tasks.json` that will not parse and
  has no last-good payload) gets an **empty** `tasksDisplay`/`tasksSummary`
  rather than `0/0`: there is no denominator anybody stated. `tasksStale`/
  `tasksSource` are the same two fields documented in docs/api.md, so a row
  served from the last-good plan keeps its fraction and says where it came
  from instead of blinking blank for a poll cycle.
  Task 015 (issue #21) moved the whole field set into one builder,
  `TasksRead.row_fields`, shared with `ralphctl runs` — which is why the rows
  also carry `tasksColumn`, the same cell flattened to one string for a
  terminal (`5/7 ⚠ stale`). The hub composes its own cell from the parts
  instead (styled spans), but neither surface can word the counts differently:
  there is exactly one place a run-list row is built.
  Task 031 (issue #19) adds `deletable` and `deleteRefusal`: whether
  `DELETE /api/runs/<id>` (below) would delete this run, and — when it would
  not — the very sentence that endpoint answers with. Both are computed from
  the same gate over the same on-disk `status.json` the endpoint reads, so the
  hub's delete button is enabled exactly when clicking it works, and a
  disabled one shows the endpoint's own reason instead of a second wording
  invented in the browser. `deleteRefusal` is `null` exactly when `deletable`
  is `true`; a forged `deletable` in somebody's `status.json` is ignored (the
  row is built from the gate, like `approachDisplay`).
- `GET /api/runs/<id>` — run detail: `{runId, live, containerGone, deletable,
  deleteRefusal, status, tasks, iterations}`. `status`/`tasks` are proxied live from the run's
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
  does not invent `tasksStale: false` on its behalf). Task 005 (#15) adds the
  two *display* strings the browser shows for such a read: `tasksLabel` (the
  short badge, `stale`) and `tasksNotice` (the full sentence), rendered
  server-side from the engine's single copy of that wording
  (`ralphd.engine.state.tasks_read_notice` / `TASKS_STALE_LABEL`), exactly
  like `usage.costDisplay` and `startedAtLocal` — `app.js` never re-spells
  engine vocabulary. Both keys are present only when the read really was
  stale (and are stripped if the plan file forged them), so their absence
  means "nothing to warn about", never "an old hub". Task 008 (#16) adds
  `status.approachDisplay` the same way (see `GET /api/runs` above); it is
  always recomputed from the payload's own `approach`/`maxApproaches`, so a
  status doc carrying a forged `approachDisplay` cannot claim a ladder
  position its counter fields do not support, and a live pre-v0.6 engine's
  answer (no `maxApproaches`) renders a bare `2`.
  Task 031 (#19) adds `deletable`/`deleteRefusal`, the same two fields the run
  list carries — deliberately **top level, not inside `status`**: they state a
  fact about the run's removability, not a rendering of the status doc, and
  `status` here may be a live payload from another process that must not be
  able to claim it. They are decided from the run dir's **recorded**
  `status.json` even while the card above shows a live proxied status, because
  that is what `DELETE /api/runs/<id>` gates on: a live answer claiming the
  job finished cannot unlock a button the endpoint would refuse.
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
- `GET /api/runs/<id>/steering` — the run's steering history, for the hub's
  steering panel (task 016, issue #17): `{"live": bool, "entries": [...],
  "notice": ""}`. Each entry is one `steering/NNN-<slug>.md` message with
  `file`, `seq`, `name`, `ts` (arrival time), `state` (`pending`/`applied`),
  `consumed`, `bytes`/`hasBody` and `body` — the exact shape the engine's own
  `GET /steering` returns (docs/api.md), because both sides read it with the
  same helper, `ralphd.engine.state.steering_entries` — plus `tsLocal`, that
  arrival time rendered server-side by the shared
  `ralphd.engine.state.format_local_time` (task 017, exactly like
  `startedAtLocal` on the detail payload; recomputed from `ts`, and absent
  when there is no `ts` to render). Live-first with an
  **on-disk fallback**, the same shape as the log tail and the PRD above: the
  running job's API is asked first (it is the process that decides when an
  entry becomes *applied*), and when it does not answer, `<run>/steering/` is
  read straight off disk with `live: false`, so a finished or killed run's
  steering history stays readable. An old engine that answers with only
  `file`/`consumed` still wins on applied-ness, and the missing name,
  timestamp and body are filled in from the run dir rather than served empty
  (nothing is invented for a file the hub cannot see). A run nobody steered
  answers `entries: []` with `notice` set to `(no steering messages)`
  (constant `ui_server.NO_STEERING`, wording server-side like `NO_PRD`);
  `404` for an unknown run id.
- `GET /api/runs/<id>/documents` and `GET /api/runs/<id>/documents/<name>` —
  the run's **state documents** for the hub's document panel and its dialogs
  (task 022, issue #18.2): the same shaping `ralphctl docs` prints from
  (`ralphd.engine.state.run_documents`/`run_document`). The listing answers
  `{runId, documents: [...], notice}` with one entry per *known* document
  (`notes`, `findings`, `composite-prd`, `job`) whether or not this run wrote
  it — `key`, `name`, `where` (`run` or `config`), `title`, `path`,
  `available`, `exists`, `bytes` and `sizeDisplay`, the byte count or the one
  absence wording (`(not written)`/`(unreadable)`) rendered server-side by
  `state.format_run_document_size`, so app.js words nothing. `notice` is
  `(no state documents on disk)` (constant `ui_server.NO_DOCUMENTS`, the
  `NO_PRD`/`NO_STEERING` discipline) when a run wrote none of them, else `""`.
  The listing carries **no bodies**: the panel only needs labels and a 4s poll
  must not ship the whole run's prose. `GET .../documents/<name>` (key or file
  name, e.g. `notes` or `notes.md`, case-insensitively) adds `body`,
  `redacted`, `summaryLines` and `text` — the complete dialog body, i.e. the
  header block + `--- <file> ---` + the body, exactly what `ralphctl docs <run>
  <name>` prints. `job.yaml` arrives **already redacted** (masked by key name
  and scrubbed by value, `engine.redact.redact_job_yaml`): there is no raw back
  door, so the dialog is as safe to screenshot as `ralphctl docs` output is to
  paste. Like the iteration endpoint above these are **on-disk only, with no
  `live` flag**: these files are written into the run dir and the job config
  dir by the agent, the engine and `start` itself, so there is nothing to fall
  back *from*. A document this run never wrote is not an error — its `text` is
  the `(not written)` wording. `404` for an unknown run id or a name that
  matches no known document.
- `GET /api/runs/<id>/artifacts` and `GET /api/runs/<id>/artifacts/<path>` —
  what the job left behind in `artifacts/`, for the hub's **Artifacts** panel and
  its dialogs (task 024, issue #18.3): the same shaping `ralphctl artifacts`
  prints from (`ralphd.engine.state.artifact_entries`/`artifact`). The listing
  answers `{runId, artifacts: [...], notice}` with one entry per *file* under
  `artifacts/` in path order (an artifact tree is whatever the agent wrote, so
  unlike the document listing there is no fixed set of rows): `path` (relative
  to `artifacts/`, and what you pass back in the URL), `key` (the well-known
  name — `report`, `suggestions`, `reflect-failed` — or `null`), `title`,
  `file`, `available`, `exists`, `bytes`, `isText` and `sizeDisplay`, rendered
  server-side by `state.format_artifact_size` (the same file-size vocabulary as
  the documents above). `notice` is `(no artifacts)` — `state.NO_ARTIFACTS`, the
  very line `ralphctl artifacts <run> ls` prints — for a run that produced
  nothing, else `""`. The listing carries **no bodies**: a 4s poll must not ship
  a whole reflection report. `GET .../artifacts/<path>` adds `body`,
  `summaryLines` and `text` — the complete dialog body, exactly what `ralphctl
  artifacts <run> show <name>` prints. The path may be a well-known key
  (`report`), a path relative to `artifacts/`, or that path with the directory
  (`artifacts/reflection/report.md`), spelled either as one percent-encoded
  segment or with real slashes. Resolution *and* the traversal guard are the
  shaping's single `state.artifact_relpath` — an absolute path, a `..` segment
  or a NUL is not an artifact and gets a `404`, never a file from elsewhere on
  the host. On-disk only, with no `live` flag (the agent writes these files into
  a directory this host holds); a binary artifact answers with the `(binary file
  …)` wording rather than bytes, and one that is no longer there with `(not
  written)` rather than a `404`. `404` for an unknown run id or a name that
  cannot address an artifact at all.
- `GET /api/runs/<id>/fault` — why this run is (or last was) in trouble, for
  the hub's fault dialog behind the failure / infra-wait badge (task 026, issue
  #18.4). Byte-for-byte the document `ralphctl fault <run> --json` prints: the
  shared join `ralphd.engine.state.fault_explanation` produces (`hasFault`,
  `faultClass`, `reason`, `signature`/`signatureDisplay`, `ladder`, `budget`,
  `health`, `waiting`, `recovered`, `abortReason`, the failing iteration's own
  `iterationDetail`, `notices`, `summaryLines`) plus `runId` and `text` — the
  complete dialog body, i.e. exactly the block `ralphctl fault <run>` prints
  below its `run:` line. So the hub cannot explain a fault differently from the
  CLI. A run that never faulted is **not** an error: it answers `hasFault:
  false` with `(no fault recorded)` as its `text`, so the badge that opens the
  dialog never has to lie about having something to say. On-disk only, with no
  `live` flag (status.json, `events.jsonl` and the iteration metas are the
  engine's own writes), like the iteration/document/artifact endpoints above.
  `404` for an unknown run id.
- `GET /api/runs/<id>/cost` — what this run spent, per phase and per approach,
  for the hub's cost-breakdown dialog behind the usage card's cost cell (task
  028, issue #18.5). Byte-for-byte the document `ralphctl cost <run> --json`
  prints: the shared shaping `ralphd.engine.state.cost_breakdown` produces
  (`hasUsage`, `total`, the `byPhase`/`byApproach` lists, `costDisplay`/
  `costStatus`/`costSource`, `model`/`modelRaw`, `sources`, `notices`,
  `summaryLines`) plus `runId` and `text` — the complete dialog body, i.e.
  exactly the block `ralphctl cost <run>` prints below its `run:` line. So the
  hub cannot label money differently from the CLI, and `costDisplay` is the very
  string the card's cost cell already shows — opening the breakdown can never
  contradict the number that was clicked. A run that recorded no usage is **not**
  an error: it answers `hasUsage: false` with `(no usage recorded)` as its `text`.
  On-disk only, with no `live` flag (status.json is the engine's own atomic
  write), like the fault/iteration/document/artifact endpoints. `404` for an
  unknown run id.
- `GET /api/runs/<id>/iterations/<n>` — one iteration's whole story, for the
  hub's iteration dialog (task 020, issue #18.1): the exact dict `ralphctl
  iteration` prints from (`ralphd.engine.state.iteration_detail` — `number`,
  `phase`, `approach`, `hasMeta`, `startedAt`/`endedAt` plus their `*Local`
  renderings, `durationS`/`durationDisplay`/`durationLabel`, `exitReason`,
  `tokensDisplay`, `costDisplay`/`costStatus`, `hasTranscript`/
  `transcriptBytes` and everything else `meta.json` records) plus `runId`,
  `summaryLines` (that header block as labelled text lines, worded once by
  `state.iteration_summary_lines`), `log` (the transcript rendered by the same
  `log_render` pass the log tail above uses) and `text` — the complete dialog
  body, i.e. `summaryLines` + the `--- log (N lines) ---` separator + `log`.
  So every string the browser shows was formatted in Python, and the hub
  cannot word an exit reason, duration or token count differently from
  `ralphctl iteration <run> <n>`.
  Unlike the log tail, the PRD and the steering history, this endpoint is
  **on-disk only and has no `live` flag**: `iterations/NNNN/meta.json` and the
  per-iteration transcript are the engine's own atomic writes into the run
  dir, so there is nothing better a live container could say and nothing to
  fall back *from* (the same reasoning as `ralphctl iteration`'s missing
  snapshot notice). `?log=0` omits the transcript — the `log` key is then
  *absent*, never an empty list, exactly like `ralphctl iteration --no-log`.
  An iteration with no transcript answers with the single `(no transcript
  yet)` line, and an unknown run id, an unknown iteration number or a
  non-numeric one all answer `404` with a naming `error`.
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
- `DELETE /api/runs/<id>` — delete a **finished** run's state (task 030, issue
  #19): exactly what `ralphctl rm <id> --force --yes` does, in one HTTP call —
  the leftover job container is stopped and removed, the run's labeled
  siblings are reaped, and the run dir and job config dir are deleted. Answers
  `200 {"removed": "<id>", "stoppedContainer": true|false}` (the shape
  `ralphctl rm --json` prints, because it is the same act: the hub calls the
  CLI's own removal sequence rather than a second one of its own).

  The gate is deliberately **stricter** than `rm --force`'s: the run's
  recorded `state` in status.json must be one of `succeeded`, `failed`,
  `aborted`. Anything else answers `409` with `{error, runId, state}` and
  touches **nothing at all** — not even a `docker inspect`:

  | recorded state | answer |
  | --- | --- |
  | `succeeded` / `failed` / `aborted` | deleted (`200`) |
  | `starting` / `running` | `409` "run is still active (state: …) — abort or stop it first" |
  | absent, unreadable or unrecognized | `409` "cannot establish that this run has finished …" |

  A run dir recording `running` whose container is already gone (a zombie) is
  therefore refused here even though `ralphctl rm --force` would delete it:
  the hub cannot tell a zombie from a live run whose port is merely filtered,
  and an unreachable API is not the same fact as a finished job. The refusal
  names the escape hatch (`ralphctl repair`, then `ralphctl rm --force`), so a
  browser is never the only way to make progress. An unknown run id — and any
  id that is not the plain name of a directory directly under
  `<registry>/runs/`, e.g. a percent-encoded `..` — answers `404`.

  Which of the two answers a run would get is also *published*, as
  `deletable`/`deleteRefusal` on `GET /api/runs`' rows and on
  `GET /api/runs/<id>` (task 031, above), so the hub's delete button is
  enabled exactly when it works and a disabled one shows this endpoint's own
  refusal sentence.
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
  verdict, phase, approach, task progress, iteration count and start time, auto-refreshed
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
  rebuild preserves it. The **APPROACH** cell shows the server-rendered
  `approachDisplay` (`10/12`, a bare `2` with no recorded limit, empty for a
  run that never entered the ladder — task 008, issue #16), while the column
  still sorts on the raw `approach` number, so approach 10 never sorts as the
  string `"10/12"`.
  The **TASKS** cell (task 014, issue #21) shows the server-rendered
  `tasksDisplay` (`5/7`) plus the trouble flags from `tasksTrouble`
  (`⚠ 1 validation-failed`, `⚠ 1 in-progress` — the same wording
  `ralphctl status` uses), with the full sentence (`tasksSummary`) as the
  cell's hover title. A run with no plan on disk gets a **blank** cell, never
  `0/0`. The column sorts on the completion **ratio** `tasksCompleted /
  tasksTotal` — so `5/7` outranks `100/250`, which neither the rendered text
  nor the bare numerator would get right — and a plan-less run has no ratio at
  all, so it sorts **last ascending** rather than pretending to be 0% done.
  First click on the header is therefore *ascending* (least-complete first:
  the runs that still owe work).
  A trailing **delete** button per row (task 031, issue #19) removes a
  finished run without leaving the hub — see the delete affordance below. It
  is deliberately not a sortable column: it renders an action, not a value.
- **Run detail** (`#/run/<id>`) — summary card (state/verdict/phase/
  approach `n/m` (task 008, issue #16: the same `approachDisplay` string the
  run list and `ralphctl status` show)/iterations/live-vs-snapshot/duration), a usage/cost panel
  (total tokens+cost plus the `byPhase`/`byApproach` breakdowns from PRD
  req 19 when present; an unknown/partial cost shows the shared
  `unavailable` wording, computed server-side by `ui_server` and delivered
  as `usage.costDisplay` exactly like `startedAtLocal`, never re-derived
  from `costUSD` in the browser), a task table, an iteration timeline (number,
  phase, model, duration once ended — each row clickable, see below), a live log tail rendered with the
  *same* pretty rules as `ralphctl logs` (iteration boundaries, streamed
  text, compact tool one-liners, elided thinking, malformed-line
  markers — reimplemented in `app.js`, not shared code, since the CLI is
  Python and the bundle is browser JS), and a steering form that `POST`s
  to `/api/runs/<id>/steer` and reports the created file name back, plus the
  **steering history** panel below it (task 017, issue #17, described below).
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
  A **State documents** panel (task 022, issue #18.2) sits under the summary
  card and lists what `GET /api/runs/<id>/documents` reports: the worker's
  `notes.md`, the reviewer's `review-findings.md`, the `composite-prd.md` an
  approach restart wrote and the effective `job.yaml`. A document that exists
  is a `<button class="document-item" data-document="<key>">` (keyboard
  reachability and Enter/Space come from the platform) opening it in the same
  single `<dialog>`, showing the server's `text` as text nodes only — the very
  lines `ralphctl docs <run> <name>` prints, `job.yaml` included and redacted.
  A document this run never wrote is listed too, as a non-clickable
  `.document-absent` row carrying the server's `(not written)` wording: which
  documents exist is itself part of the answer, so absence is stated rather
  than hidden. The endpoint is on-disk, so the panel works for a run whose
  container is long gone — hence no snapshot label here either.
  An **Artifacts** panel (task 024, issue #18.3) follows it and lists what `GET
  /api/runs/<id>/artifacts` reports — every file under the run's `artifacts/`,
  the reflect phase's `reflection/report.md` and `reflection/suggestions.diff`
  above all, which until now could only be read by knowing the registry layout
  and `cat`-ing files on the host. Each row is a `<button class="artifact-item"
  data-artifact="<path>" data-artifact-key="<key>">` showing the well-known name
  (when the file has one), the path and the server's `sizeDisplay`, and opens the
  file in the same single `<dialog>` as the server's `text` — the very lines
  `ralphctl artifacts <run> show <name>` prints — again as text nodes only: a
  post-mortem report is agent-authored markdown and a suggestions diff is
  nothing but `<`, `>` and context lines. A binary artifact stays clickable and
  its dialog carries the server's `(binary file …)` wording, which is a better
  answer than an unexplained dead row; a run that left nothing behind gets the
  `(no artifacts)` notice instead of an empty panel. On-disk like the documents,
  so it works with the container long gone.
  Each row of the **task table** is clickable (and keyboard-reachable:
  `tabindex=0`, Enter/Space) and opens that task's detail in the same
  `<dialog>` (task 057, issue #2): its `status`, `priority` and `dependsOn`
  when set, its `successCriteria` — the text the task is actually judged
  against — and any `validationNotes`. The task record is already in the
  run-detail payload (`tasks`), so no extra request is made, and the same
  text-nodes-only discipline applies: criteria are agent-authored prose full
  of backticks, `<` and fenced snippets.
  When the task read was served from the last-good cache (task 005, #15) the
  table is preceded by a `#tasks-stale` line — a `stale` pill plus the
  `tasksNotice` sentence, `data-tasks-source` carrying `last-good` or
  `unreadable` — so rows that are true-but-old are never shown as current.
  The rows themselves stay put: a poll landing inside pi's non-atomic rewrite
  of `tasks.json` no longer blinks the table to `(no tasks)`, and an
  `unreadable` read (no last-good anywhere) shows the notice *instead of*
  `(no tasks)`, since that emptiness is ignorance rather than an empty plan.
  The **steering history** panel (task 017, issue #17) lists every message
  `GET /api/runs/<id>/steering` reports, oldest first, one `.steering-item`
  row each: a `pending`/`applied` pill (the fact the panel exists to state —
  a pending message is one the agent has *not* read yet), the operator's own
  `--name`, the arrival time as the server-formatted `tsLocal` string (the
  same `engine.state.format_local_time` `ralphctl status` uses, so "local"
  means the host running ralphd, not the browser's timezone) and the file
  name. Each row is clickable and keyboard-reachable (`tabindex=0`,
  Enter/Space) and opens that message's full text in the **same single
  `<dialog>`** the PRD and task dialogs use — text nodes only, since a
  steering message is operator prose from outside the page's trust boundary;
  the dialog's note line repeats the state and arrival time. Because the
  endpoint is live-first with an on-disk fallback, the panel works for a
  finished or killed run too, and then adds the `(on-disk snapshot — the
  run's API is not reachable)` label (both in the list and in the dialog). A
  run nobody steered shows `#steering-notice` with the server's `(no steering
  messages)` wording rather than an empty box, and an entry the hub holds no
  body for says so instead of showing an empty message. Sending through the
  form refreshes the panel immediately, so an operator sees their own message
  without waiting out the 4s poll.
  The **iteration timeline** rows are clickable and keyboard-reachable
  (`tabindex=0`, Enter/Space, `.timeline-clickable[role=button]`,
  `data-iteration="<n>"`) and open that iteration's own story in the **same
  single `<dialog>`** (task 020, issue #18.1): phase and approach, the
  absolute start/end instants, the duration (labelled `total` or `elapsed`),
  the exit reason (`clean exit`, `exit 7`, `error (exit N): …`, `iteration
  timeout`, `no-traffic timeout`, `interrupted (a signal ended the
  iteration)`, with an
  `[infra fault]` marker when the attempt was refunded), the model pi actually
  used, that iteration's tokens and cost, the steering it consumed, the task it
  verified — and then its full transcript. The whole body is the `text` string
  `GET /api/runs/<id>/iterations/<n>` formatted (see above), inserted as text
  nodes only, so a transcript full of `<` survives as text and the hub says
  exactly what `ralphctl iteration <run> <n>` says. The endpoint is on-disk
  only, so this works for a run whose container is long gone — hence no
  snapshot label on this dialog.
  A **degraded** run (`health: degraded`/`infraWait` set — the run is
  sitting out an endpoint outage; see docs/api.md) gets a visually
  distinct card (`.card.degraded`) carrying the attempt number, phase,
  error, episode wait against the outage budget, a countdown to
  `nextAttemptAt` that ticks every second, and a **retry now** button
  posting to `/api/runs/<id>/retry`. The button appears only while a
  backoff wait is actually pending and only when the run's API is
  reachable: on a dead run the card says `read-only on-disk snapshot` and
  offers no button.
  Both the degraded block and a **failed**/**aborted** run's `state:` pill carry
  a **fault badge** (task 026, issue #18.4): a `<button class="fault-badge"
  data-fault-badge="infra-wait|state">` — keyboard-reachable, Enter/Space from
  the platform — opening the run's fault explanation in the same single
  `<dialog>`. The body is the `text` string `GET /api/runs/<id>/fault` formatted
  (see above), i.e. exactly what `ralphctl fault <run>` prints, as text nodes
  only: the classification the engine acted on, which row of `engine/faults.py`'
  signature table matched (family, pattern and the substring that matched), the
  run's position on the retry ladder with its own recorded backoffs, and how much
  of the outage budget is spent. Because the endpoint is on-disk this works with
  the container long gone — and a run that never faulted carries no badge, so
  there is nothing to click and nothing to explain.
  The **usage card's cost cell** is a `<button class="cost-cell"
  data-cost-cell="total">` (task 028, issue #18.5) — keyboard-reachable,
  Enter/Space from the platform — opening this run's cost breakdown in the same
  single `<dialog>`. The headline stays exactly the `costDisplay` string the card
  always showed; the dialog body is the `text` string `GET /api/runs/<id>/cost`
  formatted (see above), i.e. exactly what `ralphctl cost <run>` prints, as text
  nodes only: the per-phase and per-approach buckets, and whether each figure was
  quoted by the provider, derived from the host-side rate table, a partial
  subtotal or `unavailable`. Because the endpoint is on-disk this works with the
  container long gone — and a run that reported no usage at all has no cost cell
  to click.
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
  A **delete** control (task 031, issue #19) sits in the run-detail action bar
  next to **view PRD**, and in every run-list row: a `<button
  class="delete-run" data-delete-run="<id>">` that opens a confirmation naming
  the run id and listing what will be removed (container, siblings, run dir,
  job config — `ralphctl rm --force`'s own removal), with **cancel** and
  **delete** buttons. The confirmation is a `<dialog id="delete-dialog">` and
  goes through the *same* single-dialog invariant as every text dialog above,
  so it cannot stack on one of them or on the 4s refresh behind it. Whether the
  button is offered at all is the server's `deletable` answer (see
  `DELETE /api/runs/<id>`), never a state comparison re-spelled in JS: for an
  active run — or one whose state the hub cannot establish — it renders
  **disabled** (`data-delete-refused="1"`) with the server's own
  `deleteRefusal` sentence shown beside it, so an operator learns *why* instead
  of finding a missing button. Confirming removes the row from the list
  immediately (the list reloads rather than waiting out the poll); on the
  detail page there is no run left to show, so the hub returns to the run list.
  A refusal that arrives anyway (the run started again between the poll and the
  click) is shown in the dialog verbatim rather than swallowed.

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
