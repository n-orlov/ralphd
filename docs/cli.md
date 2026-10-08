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

Install: the distribution is **`ralphd`** (it provides the `ralphctl` console
script, plus `ralphd-engine` for the in-container engine) and is not on PyPI in
v0.6 — install it from a checkout with `pipx install .` (or `pip install -e .`
for development). Requires a working `docker` CLI (or `podman` with
`RALPHD_DOCKER=podman`).

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
container ID, API URL, token presence). Before that, it prints exactly one
`rubric: ...` line (task 005, PRD requirement A.3) naming which materiality
rubric this PRD resolves to -- `rubric: inline`, `rubric: by reference
(<path>)` or `rubric: engine default` (`findings.resolve_materiality_rubric`,
the same resolver review uses at review time, [architecture.md](architecture.md));
a referenced rubric path this checkout cannot resolve is warned on stderr
instead of silently falling back.

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
| `--reflect` | off | run one extra `reflect` iteration after the job reaches a terminal state, writing a report + suggested prompt/skill diff to `artifacts/reflection/` (never touches the workspace or run state). That one attempt gets its own timeout floor (`REFLECT_TAIL_TIMEOUT_S`, 600s, still capped by `--iteration-timeout`) instead of the plain 60s floor every other phase gets once `--timeout` has already elapsed -- requirement C (#72): reflect is the run's own post-mortem and a deadline that has already ended the job must not starve the one attempt left to write it down. See [SPEC.md](../SPEC.md) §4.5 |
| `--peer-plan-review` | off | opt-in peer plan-review gate (task 018, PRD requirement K, #101): a peer model approves or objects to the plan before `worker` starts, bounded by `peer_plan_review_max_revisions` revision round trips. Written to the run's `job.yaml` as `peer_plan_review` and visible in `GET /config` (`flags.peerPlanReview`); no `resume` equivalent -- like `--vigilant`/`--reflect`, it is a `start`-time choice only, replayed unchanged by `job.yaml` on every later `resume` |
| `--peer-verdict-gate` | off | opt-in, genuinely blind peer verdict gate (task 020, PRD requirement L, #101): a second, independent peer model re-verifies completed work before terminal success. Written to the run's `job.yaml` as `peer_verdict_gate` and visible in `GET /config` (`flags.peerVerdictGate`); no `resume` equivalent, for the same reason `--peer-plan-review` has none |
| `--mid-plan-review` | off | opt-in mid-plan review episode (task 022, PRD requirement K.2, #169): once per approach, once a declared fraction (`loop.MID_PLAN_REVIEW_FRACTION`) of the plan has already reached terminal-success, run one review iteration scoped to those requirements, opening a cure task per blocking finding through the existing cure machinery (`state.insert_cure_tasks`, the same path an ordinary review's own cure pass takes). Written to the run's `job.yaml` as `mid_plan_review` and visible in `GET /config` (`flags.midPlanReview`); no `resume` equivalent, for the same reason the two peer gates above have none |
| `--no-operator-input-needed-pause` | on (this is the disable flag) | task 014's pause-before-archive gate (PRD requirement A.1, #224): ON by default, unlike the two peer gates above -- a rejection that would otherwise archive the approach (whether the next step would have been a fresh `replan` or the terminal `rejected_no_approach_left` outcome) holds instead, with `pauseReason: "operator_input_needed"`, so an operator can answer -- with a ruling (`ralphctl rule <run-id> --file <ruling.md> --name <slug>`, task 013) or a plain `ralphctl unpause <run-id>` -- before that spend happens. This flag restores the pre-014 behaviour of archiving immediately. The SAME flag also gates the peer verdict gate's own mirror (v0.27 requirement C.1, task 008; widened by cure task 002, this wave, to every blocking-set shape): a `verdict_gate` rejection holds instead with `pauseReason: "verdict_gate_operator_input_needed"` whenever the run-wide `cure_pass_ceiling` is already spent -- whether the rejection's blocking findings are all curable, mixed, or all non-curable -- disabled by this same flag; a parked mixed/all-incurable rejection's own unanswered resume still ends the run on the merits immediately, never a fresh `replan`, whatever approaches remain. Written to the run's `job.yaml` as `operator_input_needed_pause` (always an explicit `true`/`false`, unlike the two peer gates) and visible in `GET /config` (`flags.operatorInputNeededPause`); a `--template`'s own `operator_input_needed_pause:` key (SPEC.md §6.7) supplies the default when the flag is absent; no `resume` equivalent, for the same reason `--peer-plan-review`/`--peer-verdict-gate` have none |
| `--model <id>` | profile default | model used by any hat with no per-hat override; falls back to the `--template`'s value, then the registry's `model` (`ralphctl config`), then the `--llm` profile's own `model:`. `router` (task 018, requirement E; rung order corrected task 003, #278 B.6) resolves `router_model` first, then the dedicated per-hat table entry (`model_overrides["router"]`/`models.router`), then the worker hat's own `trivial` tier (`model_tiers["worker"]["trivial"]`) when configured, then `model`; see `routerModel`/`routerTimeoutS` under `GET /config` (`docs/api.md`). `--fast-model` and `--model-strategy` (and `ralphctl config set model_strategy`/`fast_model`) are retired (task 006): the parser still recognises the flag names (hidden from `--help`) only so `start` can refuse them with exit `2` naming this `models:`/`model_overrides:` replacement, never a silent no-op; a job.yaml still carrying either legacy key from before task 006 is translated into an equivalent per-hat table at engine load time, never ignored |
| `--no-route-tasks` | on (this is the disable flag) | task 003's router-assisted task-selection opt-out (#278 B.5): by default, once `engine.scheduler.pending_candidates` finds 2+ runnable tasks, the router picks which one to work next (`LoopSupervisor._route_task_selection`); this flag (or a `job.yaml` `route_task_selection: false` key) restores the fully deterministic pick, `engine.scheduler.select_next_task`'s own order, unconditionally. Written to the run's `job.yaml` as `route_task_selection` (always an explicit `true`/`false`) and visible in `GET /config`; no `resume` equivalent -- `resume` never rewrites this key, so the value a run started with is replayed unchanged on every later resume |
| `--model-for HAT=REF` | — | per-hat model override (task 008, #60 pillar 4 requirement A), repeatable — e.g. `--model-for worker=amazon-bedrock/eu.anthropic.claude-sonnet-5`. Known hats: `planning\|worker\|verify\|review\|reflect\|router` (`MODEL_HATS`; an unrecognised name still resolves via `model_for`'s anti-downgrade rule rather than erroring). Writes/merges into the run's `job.yaml` `models:` table — the mechanism `model_overrides`/`models:` always was ([llm-profiles.md](llm-profiles.md) unaffected, `docs/api.md`'s `GET /config` `model.overrides`) — the ONLY thing this flag adds is a way to *reach* that table from `start` without hand-editing `job.yaml` and `resume`-ing. Unlike every other `start` scalar above, the table is **merged key by key across levels, not replaced whole**: explicit `--model-for` flags (last one wins for a repeated hat) > the `--template`'s own `job.yaml` `models:`/`model_overrides:` key > `<registry>/config.yaml`'s own `models:`/`model_overrides:` key (a nested mapping, edited directly like `pricing:` below — there is no scalar `ralphctl config set` for a table) > (a named LLM profile has no per-hat table of its own yet, so this rung is presently always empty) > absent, i.e. each hat falls through to `model_for`'s other rungs exactly as before. A malformed value (no `=`) exits `2` naming the expected `HAT=REF` shape |
| `--harness-for HAT=NAME` | — | per-hat agent harness, repeatable — e.g. `--harness-for worker=claude-code`. `NAME` is `pi` or `claude-code`; `HAT` is one of the `MODEL_HATS`. Merges per hat into the run's `job.yaml` `harnesses:` table, weakest to strongest: the LLM profile's `harness:` < `<registry>/config.yaml`'s `harnesses:` < the `--template`'s `harnesses:` < this flag. `start` writes every hat's resolved harness, and `resume` replays it. An unknown hat or harness name, or a value without `=`, exits `2`; see "Per-hat harness" below |
| `--model-for-tier HAT:TIER=REF` | — | per-(hat, tier) rung override (task 002/004, #68), repeatable — e.g. `--model-for-tier worker:hard=amazon-bedrock/eu.anthropic.claude-opus-5`. `TIER` is one of the three declared tiers, `trivial\|normal\|hard` (`tiers.TIERS`); `HAT` is the same `MODEL_HATS` vocabulary `--model-for` uses. Merges rung by rung — hat AND tier both have to match for one flag to overwrite another — into the run's `job.yaml` `model_tiers:` table, over the `--template`'s and `<registry>/config.yaml`'s own `model_tiers:` key, the identical three-level order `--model-for` merges `models:` across; see "Per-(hat, tier) override table" below. A hat/tier this flag never names falls through to that hat's plain `--model-for`/`models:` answer (`bundle_for`), not to some cheaper tier — there is no second per-hat table this could conflict with. A malformed value (no `:` before the `=`, or no `=` at all) exits `2` naming the expected `HAT:TIER=REF` shape |
| `--thinking <level>` | — | pi thinking level; falls back to the `--template`'s value, then the registry's `thinking` (`ralphctl config`) |
| `--price-strategy none\|aws` | none | derive a cost for routes the provider does not price (or prices with an implausible `$0`): `aws` uses ralphd's built-in AWS Bedrock rate table, `none` leaves such a cost `unavailable`. Written to the run's `job.yaml` as `price_strategy` (so `resume` replays it) and visible in `GET /config` (`priceStrategy`); falls back to the `--template`'s value, then the registry's `price_strategy` (`ralphctl config`), then the `--llm` profile's own `price_strategy:` — see "Built-in AWS Bedrock rate table" below |
| `--notifier-url <url>` | unset | endpoint the engine's notify hat tool POSTs operator updates to (task 010, PRD requirement B.2, #197) -- configuring the channel no longer requires `start` -> `docker stop` -> hand-edit `job.yaml` -> `resume`. Written to the run's `job.yaml` as `notifier_url` (so `resume` replays it) exactly like `--price-strategy` above; when the flag is absent it falls back to the `--template`'s own `notifier_url:` key (`notifier_url` is one of the template scalar fields, see "Templates" below), and a `notifier_url:` key hand-added to a started run's own `job.yaml` reaches the config the same way. It has no registry-wide level of its own (`ralphctl config`), unlike `--price-strategy`: an endpoint is one operator's channel for one job, not a fleet-wide default. Precedence: `RALPHD_NOTIFIER_URL`, read by the engine inside the container at load time, overrides everything below unconditionally (a single-run override with no job.yaml edit); then the flag, then the template's key; unset means unconfigured, and the notify hat tool answers "no notifier URL configured" instead of raising |
| `--review-clone-protocol <command>` | unset | the command line -- a workspace-relative script, plus any arguments it needs -- the `review` and `verdict_gate` hats run inside their disposable clone to prove that clone is what they are measuring, in place of the Python `pip install -e .` plus `__file__` assertion those prompts otherwise spell out (#208). A product with no `pyproject.toml` cannot satisfy that recipe, so a hat honouring its prompt measures nothing and rejects `curable: false` on every approach; naming the product's own equivalent here authorizes it at launch, where it is auditable, instead of through a mid-run `ralphctl amend --wave`. **The value is the command the hat runs, as written** (#265): the whole string is rendered **verbatim** into the `## Disposable-clone protocol (operator-authorized)` block -- the engine introduces no shell, performs no argv parsing on it and invents no quoting convention, so a script that itself requires an argument can be named through this same flag exactly as it is invoked anywhere else. Worked example: a Go product's `ci/review.sh` requires at least one argument and exits 2 with none, so `--review-clone-protocol 'ci/review.sh smoke'` names it complete with the argument it needs, and the hat sees `` `ci/review.sh smoke` `` in its prompt unchanged. A bare path with no arguments (the only shape used before #265) renders exactly as it always has. Written to the run's `job.yaml` as `review_clone_protocol` (so `resume` replays it) exactly like `--notifier-url` above, and when the flag is absent it falls back to the `--template`'s own `review_clone_protocol:` key (one of the template scalar fields, see "Templates" below); a key hand-added to a started run's own `job.yaml` reaches the config the same way. No registry-wide level (`ralphctl config`): the command is a property of one product's repository, not a fleet-wide default. The engine neither runs nor validates it -- it renders it into every hat's prompt as above, and the judging hat requires exit 0 before citing any measurement from that clone. Unset leaves the prompts' Python recipe as the protocol, so an existing run is unaffected |
| `--llm <profile>` | `host` | LLM profile ([llm-profiles.md](llm-profiles.md)); falls back to the registry's `default_llm_profile` (`ralphctl config`) if set
| `--billing-payer TEXT` | the `--llm` profile's `billing.payer` | who pays for this run's tokens, a free-text label (#298) written into the run's `job.yaml` `billing` block and shown by `ralphctl cost`/`status` and the hub's by-payer table. Falls back to the profile's `billing:` block (for `--llm host`, the registry `config.yaml`'s `billing:` mapping, see "Who pays" below), then to the profile name (`host models.json` for host). A blank value, or one holding a control character such as a newline, exits `2`; so does either billing flag with `--llm none` |
| `--billing-basis metered\|subscription\|chargeback\|free` | the profile's `billing.basis`, else `metered` | how those tokens are billed (#298): `metered`/`chargeback` report the priced cost, `subscription`/`free` report `$0` with the list price apart as the list-equivalent. The `claude-code` harness always bills `subscription`, so a different basis on a `claude-code` profile exits `2` |
| `--llm-env KEY=VAL` | — | ad-hoc env additions to the LLM config (repeatable) |
| `--llm-header NAME:VALUE` | none | set this HTTP header on every provider of the resolved pi config (`NAME=VALUE` also accepted; repeatable; a same-named header the profile already carries -- whether set per-provider or via the profile's own top-level `headers:` shorthand (#177) -- is overridden, others are kept; `${env:}`/`${file:}`/`${cmd:}` resolve on the host); falls back to a `--template`'s own `llm_header:` list (task 045, #177) — see [llm-profiles.md](llm-profiles.md) for the full flag > template > per-provider profile headers > top-level profile `headers:` precedence, and the `ralphctl resume` section below for `resume --llm-header`'s override |
| `--forward-env NAME\|PREFIX_*` | — | forward host env var(s) into the container, by exact name or prefix glob (repeatable). Required for any non-standard vars — see [llm-profiles.md](llm-profiles.md) |
| `--skills <dir>` | — | mount a skills directory (repeatable) |
| `--creds <dir>` | — | mount a credentials directory (see below) |
| `--allow-docker` | off | mount the host docker socket into the job container — **root-equivalent host access**, see below. Recorded (on or off) for the run, so every later resume and auto-resume re-applies it (#297) |
| `--image <ref>` | built from source (`ralphd:<hash>`) | pin an exact engine image instead of building one — see "The job image" below; falls back to the registry's `image` (`ralphctl config`) if set |
| `--base-image <ref>` | — | build the job image **on top of** this one — see "Bring your own base image" below. Mutually exclusive with `--image` |
| `--dockerfile <path>` | — | build **this** Dockerfile (with its own directory as the build context) into that base image instead of naming one that already exists — see "Bring your own base image" below. Mutually exclusive with `--image` and `--base-image` |
| `--on-complete idle\|exit` | exit | post-completion behavior; `idle` is an explicit debugging opt-in; falls back to the registry's `on_complete` (`ralphctl config`) if set |
| `--on-complete-cmd <cmd>` | — | shell command run once by the engine (in-container) on reaching a terminal state; receives `RALPHD_RUN_ID`/`RALPHD_STATE`/`RALPHD_VERDICT` env vars; failures are logged (`events.jsonl`, `level: error`) but never affect the job's verdict or the engine's exit code |
| `--timeout <dur>` | 8h | job wall-clock limit (`45m`, `8h`, `2d`) for `planning`/`worker`/`review`/`verify` -- the post-terminal `reflect` iteration (`--reflect`) is exempt from being squeezed down once this limit is already behind the run; see the `--reflect` row |
| `--iteration-timeout <dur>` | 45m | per-iteration limit |
| `--infra-outage-budget <seconds>` | 14400 (4h) | wall-clock budget for riding out one LLM-endpoint outage: infra-classified faults (connection errors, 429/529, overloaded, throttling — see [architecture.md](architecture.md)) keep being retried while one episode's accumulated backoff wait stays under this budget. Waiting costs no iterations and no approaches. Written to the run's `job.yaml` as `infra_outage_budget_s` and visible in `GET /config` (`budgets.infraOutageBudgetS`) |
| `--auto-resume` / `--no-auto-resume` | on | opt this run in to (or explicitly out of) self-recovery: `ralphctl doctor --fix` resumes a run recorded non-terminal whose container has vanished (the dangling-container condition `doctor`/`repair`/`status` report). Host-side setting — recorded with the run's other start-time wiring in `<registry>/configs/<run-id>/auto-resume.json`, never passed into the container, so it survives every later `resume` (the container is replaced, the config dir is not). Falls back to the registry's `auto_resume` (`ralphctl config`) if set; `--no-auto-resume` overrides a template/registry opt-in. The default is **on** since v0.7 — a dangling unattended run is worth more resumed than left until someone notices, and the crash-loop guard bounds the cost of a run that cannot start (see `doctor --fix` below). To keep a run out of the sweep, start it with `--no-auto-resume`; to opt the whole registry out, `ralphctl config set auto_resume false`. The default lives in exactly one place in the source (`AUTO_RESUME_DEFAULT` in `src/ralphd/cli/main.py`) |
| `--port <n>` | auto | host port for the API. When ralphctl picks the port and docker reports it already allocated (another container took it between the probe and the publish), `start`/`resume` retry on a fresh port, up to five tries; an explicit `--port` is never replaced. |
| `--api-bind <addr>` | 127.0.0.1 | host interface to publish on |
| `--network <net>` | docker default (bridge) | docker network for the job container. `host` shares the host network namespace so the job can reach host-only / VPN / tailnet services; with `host` there is no port publishing — the engine itself listens on `--port` bound to `--api-bind` (via `RALPHD_PORT`/`RALPHD_BIND`). Any other value is passed to `docker run --network` with normal `-p` publishing. Recorded in `host.json`; `resume` reuses it. Falls back to the registry's `network` (`ralphctl config`) if set. |
| `--api-token <t\|auto>` | none | require bearer auth on **every** route (`auto` generates + stores); a **network control** (who on the network the operator exposed the port to may call the API at all) — it was **never a container boundary** and is orthogonal to the mutating-only control token `start` always provisions regardless of this flag (task 005/#62, requirement A — see `docs/api.md`'s "container boundary" section for the code-derived detail) |
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
entirely for a run dir that records no image. Task 032 (F.6) grows that same
line with the running engine's own version, whenever `status.json` carries
one (task 030's `engineVersion`) — `image:     ralphd:9f2c1a4b7d80  (engine
0.20.2, id 0f1e2d3c4b5a)` — right beside the content-hash tag, never as a
separate line, and never changing the tag's own spelling.

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
`reflect`, `on_complete`, `on_complete_cmd`, `timeout`, `iteration_timeout`,
`llm`, `model`, `thinking`, `notifier_url` (task 010, requirement B.2 --
the `--notifier-url` endpoint), `skills` (a list of directory names
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
hardcoded default. `job.yaml` may also set `llm_header` (task 045, #177): a
list of `--llm-header`-shaped `Name: value`/`Name=value` strings. This one
is NOT override-wholesale like the fields above — an explicit `--llm-header`
flag is appended AFTER the template's own list, so a later spec for the same
name wins: flag > template > the profile's own `pi.providers.<p>.headers:` >
the profile's own top-level `headers:` shorthand (#177), with no
registry-wide level. An unknown `--template`
name exits `3` naming the expected path; a malformed `job.yaml` (not a
mapping) exits `2`.

A started run's OWN `job.yaml` (as opposed to a template's) may also carry
`llm_headers` (task 047, #177, plural -- distinct from the template input
key above): the DISPLAY copy of that run's resolved `--llm-header` map,
written once by `start` right beside `model`, with every value that came
from a `${env:}`/`${file:}`/`${cmd:}` reference already replaced by `***`.
It is informational only -- the functional (unmasked) values `resume`
replays live in the run's own `<cdir>/llm-wiring.json`, never in `job.yaml`
itself -- which is why `ralphctl docs <run> job.yaml` (redacted the same
mechanical way as everything else in that file) is where an operator reads
it, not `JobConfig`.

Docker siblings (`--allow-docker`) — default **off**. Mounts the host docker
socket (default `/var/run/docker.sock`; override with `RALPHD_DOCKER_SOCK`)
and adds the socket's group to the container so the agent can run
`docker build` / `docker run`. **Warning: the docker socket is root-equivalent
access to the host** — the job can mount any host path and start privileged
containers; grant it only to PRDs you trust as much as your own shell. ralphctl
prints this warning to stderr at launch. Sibling rules the agent is told (and
you should know):

- **Docker access belongs to the run, not to one container (#297).** `start`
  records it — on or off — in `<registry>/configs/<run>/docker-access.json`
  (`{"allow_docker", "setBy", "at"}`, mounted read-only into the container),
  and every later container for the run re-applies it: a bare `resume`, a
  `doctor --fix` auto-resume, the abort → edit `job.yaml` → resume recipe.
  The socket is re-validated and the warning re-printed on each of them.
  `resume --no-allow-docker` drops it and records that, so it stays dropped;
  a run started without the flag never gains it unless a resume passes
  `--allow-docker` explicitly. `ralphctl status` and the hub show it as a
  `docker:` line.

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
ralphctl runs [--state starting|running|paused|finishing|timed_out|succeeded|failed|aborted]
              [--sort runId|state|verdict|phase|approach|tasks|iterationsUsed|startedAt|cost]
              [--reverse]
```

`--state` is an **exact match** on the recorded `state` — every state a run
dir can hold, `starting` included — and not a constrained choice set: an
unrecognised value simply matches nothing (exit `0`, empty table), whereas an
unrecognised `--sort` key is a usage error (exit `2`, below).

Columns: run ID, state, verdict, phase, approach, tasks, iterations
used/budget, started (absolute local time via the shared formatter, see
`status` below), cost (the run header's total string, e.g. `$0.5600`,
`$0.2500+ (partial, rest unavailable)` or `unavailable`; blank for a run with
no `usage`; `--json` carries it as `costDisplay` beside the raw `costUSD`).
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
- `⚠` marks a plan with a **failed** (under either meaning — see below),
  **validation-failed** or **in-progress** task (the same states the hub flags;
  pending work is not trouble). The flag
  *sentences* do not fit a column, so they are not abbreviated into a private
  wording here: `--json` carries them verbatim in `tasksTrouble`
  (`["1 failed (requirement-unmet)", "1 validation-failed", "1 in-progress"]`)
  and `ralphctl status <run>`
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
`tasksValidationFailed`/`tasksFailed`/`tasksFailedValidationExhausted`/
`tasksFailedRequirementUnmet` beside the rendered `tasksDisplay` (`5/7`),
`tasksColumn` (the terminal cell), `tasksSummary`, `tasksTrouble` and issue
#15's `tasksStale`/`tasksSource` — the same field set the hub's run-list rows
carry.

#### Both meanings of a `failed` task (task 025, issue #33)

`failed` says two different things — *a verifier judged this requirement
unmet* and *the engine spent this task's validation rounds* — told apart by
the `failureKind` label the engine writes (`requirement-unmet` /
`validation-exhausted`, SPEC §5.3). Every counting surface reports which:

- the counts a summary is rendered from keep counting both under `failed`
  (so the status counts still sum to `total`, exactly as before) and carry
  the breakdown as the **sub-counts** `failedValidationExhausted` /
  `failedRequirementUnmet`, which sum to `failed`;
- the wording is one vocabulary — `1 failed (validation-exhausted)` — in
  `ralphctl status`' `tasks:` line, `ralphctl runs --json`'s `tasksTrouble`,
  the hub's TASKS cell tooltip and `ralphctl tasks`' status column;
- the plain `2 failed` phrasing is what a summary falls back to when the
  counts carry no sub-counts at all (a run whose engine predates v0.7): one
  failed task is never both counted and flagged twice;
- for a `tasks.json` written before v0.7 (no `failureKind` key anywhere) the
  kind is **derived** from the record — at/past three recorded validation
  attempts it is `validation-exhausted`, otherwise `requirement-unmet` — so
  an old plan reads the same as a labelled one instead of showing nothing.

**Sorting** (task 055, issue #9) is the CLI half of the hub run list's
click-to-sort (see “Sorting” under `ralphctl ui` below) and uses the *same*
keys, the same lifecycle orders and the same raw payload values:

- default `--sort startedAt`, i.e. **newest first** — not the run-id
  alphabetical order the registry directory listing yields;
- `startedAt`, `iterationsUsed` and `approach` sort biggest/newest first;
  the text keys (`runId`, `phase`) sort A→Z; `state`/`verdict` sort in
  lifecycle order (`starting → running → paused → finishing → succeeded →
  failed → aborted`, and no-verdict → `unverified` → `verified`); `tasks`
  sorts on the completion
  **ratio** `tasksCompleted / tasksTotal` (so `5/7` outranks `100/250`) and
  starts **ascending**, least-complete first — the runs that still owe work;
  `cost` sorts on the raw `costUSD` number, ascending first, and a run whose
  cost is unknown (no `usage` in `status.json`, nothing priced) has no number
  and sorts last;
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

The `state:` line names up to two markers, space-separated after the raw
state word, whenever they apply (issue #269). The first is the pause
marker (the same one `ralphctl runs` and the hub run list render): `⏸
parked for input` marks the one `pauseReason` (`operator_input_needed`)
whose bare `ralphctl unpause`, with no ruling ever filed, releases the
run straight back into `review` over the EXACT SAME approach with
nothing new to go on — likely to re-raise the identical rejection rather
than progress (a plain unpause DOES release this pause; it just does not
change the underlying determination). `⏸ held on rejected_incurable
escalation` marks a run the engine is holding on a `rejected_incurable`
escalation — a pause kept in memory only, so its `state` still reads
`running` and it records no `pauseReason`; a ruling filed against this
hold is recorded but never releases it on its own (cure task cure-01-01-9)
— only `ralphctl unpause`/`POST /resume` does. The second, `❗ escalation
undelivered`, names a run whose most recently recorded engine escalation
never reached the operator at all — every bounded delivery retry failed —
and which is still paused on that exact escalation, persisted or held in
memory. Both disappear once `ralphctl unpause`/`POST /resume` releases the
hold — for `operator_input_needed`/`rejected_repeated_signature` a `ralphctl
rule` alone does too, but for every other reason that waits on a ruling
(`rejected_last_approach`, `rejected_incurable`, `stagnation`) a rule
alone leaves the hold in place; it takes a separate unpause afterward. `--json` carries the same facts
as `pauseMarker` (the pause marker string, `""` when none applies),
`escalationUndelivered` (boolean) and `escalationUndeliveredMarker` (the
marker string, `""` when it does not apply), on the live and on-disk
fallback paths alike.

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

A `docker:` line (issue #297) says whether the run's containers get the host
docker socket, read from the run's `docker-access.json` record (see
`start --allow-docker`), always printed so a lost mount is visible:

```
docker:    on (host docker socket mounted -- ROOT-EQUIVALENT)
```

The other values are `off`, `off (dropped by resume --no-allow-docker)`,
`not recorded (run predates #297; a resume mounts the socket only with
--allow-docker)`, and `unreadable (docker-access.json is damaged; a resume
refuses until --allow-docker or --no-allow-docker)`. `--json` carries
`dockerAccess: {enabled, setBy, at, line}`, `enabled` `null` when nothing is
recorded, plus `unreadable: true` when a record exists but cannot be read; the hub's run header shows the same
line as its `docker` row.

Task 032 (F.6): the running engine's own version (task 030's `status.json`
`engineVersion`) rides beside the content-hash tag on this same line, tag
spelling unchanged — `image:     ralphd:9f2c1a4b7d80  (engine 0.20.2, id
0f1e2d3c4b5a)` — never a new line, and omitted the same way the `(id …)`
suffix is when there is nothing recorded to show.

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

The header carries four timing lines, all from one shared helper
(`ralphd.engine.state.run_timing`) that the hub's run header also renders:

```
started:   2026-09-02 09:00:00 +0200
resumed:   2026-09-02 11:00:00 +0200
wall:      3h
active:    1h 50m
```

- `started:` is the run's **original** start: `firstStartedAt`, or — for a run
  recorded before that field existed — the `ts` of the first line of
  `events.jsonl` (that one line is read, never the whole file), else the
  earliest episode start on record.
- `resumed:` is the current episode's start and is printed only when the run
  has more than one episode.
- `wall:` is the original start to `endedAt` (to now while the run is live).
- `active:` is every episode's own wall clock (each `previousEndings[]` entry's
  `startedAt` to `endedAt`, plus the current episode) summed, minus
  `pausedTotalS`. It is omitted when any episode's start is unknown.

`ralphctl runs` keeps its columns; its `STARTED` column is unchanged.

`--json` adds machine-usable duration fields alongside the existing timestamp
fields (nothing existing is removed or renamed): a top-level `durationSeconds`
(elapsed-so-far or total, numeric seconds, same rule as the human line above),
and, when `currentIteration` is present, an `elapsedSeconds` field nested
inside it for that iteration's own elapsed time. Task 022 adds
`containerGone` and, for such a run, a `sinceLastUpdateSeconds` field;
`durationSeconds` and `currentIteration.elapsedSeconds` are then measured to
the last `status.json` write instead of to *now*, so they stop growing for a
run that stopped. Task 021 (issue #31) adds the two fields the widened
dangling condition needs: `dangling` (always present — `true` for **either**
dangling shape: no container at all, or one that exists and has exited) and
`containerLiveness` (`"absent"`, `"exited"`, `"unknown"`, or `null` when the
run is live or not dangling). Task 026 added the `"unknown"` reading: with no
`docker` client on `PATH` at all the liveness probe cannot decide anything, so
it says so instead of reporting docker's own `"absent"` answer ("looked, found
no such container") for a container nothing ever looked at — `status` still
exits 0 and renders the run from disk, `containerGone` stays `false`, and the
human warning reads "has unknown liveness (no docker client here to ask)". `containerGone` keeps its original narrower meaning — `true`
only for the vanished-container case — so a machine consumer written against
v0.6 reads the same value it always did; `dangling` is the field to ask about
the condition `doctor`/`repair`/auto-resume act on.

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
  fetching `--json`. Task 012 (#92): when the reason is a hat's contract
  ladder running out (no budget left to re-ask again), the text is exactly
  `'<hat>' contract violation: <errors>` -- the validator's own error(s),
  joined verbatim -- built by the ONE shared sentence-builder
  (`contract_enforcement.violation_message`) the engine itself writes
  `status.json`'s `reason` from, so this line and `ralphctl iteration
  <n>`'s `contract:` line below can never disagree about the wording.
- `deadline:` (task 007, #72) -- shown only when `status.json` carries the
  `deadlineBeatVerdict` marker task 006 added: every task in the run's plan
  was completed, the worker's own `COMPLETE` was already honoured, and the
  wall-clock deadline took the last review attempt before review ever
  rendered a verdict. `state`/`verdict` above still read the ordinary
  `failed` / `unverified` -- `verdict: verified` stays grantable by the
  review hat alone, this line does not change that ruling -- so without it
  this run is indistinguishable on screen from an ordinary failure. It
  names both timestamps the marker carries and says outright that the
  ending is not a judgement on the work:

  ```
  deadline:  the clock beat the verdict, not the work (#72): every task was
             completed and COMPLETE was honoured at 2026-08-26T06:53:15Z, but
             the deadline (2026-08-26T06:53:21Z) took the last review attempt
             before it could ever render one -- this is not a failure
             judgement.
  ```

  `--json` carries the same `deadlineBeatVerdict: {deadline,
  completeHonouredAt}` object through untouched (SPEC.md §4.2/§10.4). Absent
  entirely for every other terminal shape, including an ordinary failed run
  and a genuinely rejected last approach that happens to look similar on
  disk (see `LoopSupervisor._deadline_beat_verdict_patch()`'s own docstring
  for why that one is deliberately excluded).
- `provisional:` (task 026, PRD requirement H.2/H.3, #264) -- shown only
  when `status.json` carries the `provisionalVerdict` object task 026
  added, with `provisional: true`: review recorded an early verdict
  (`record-verdict`, task 025) and was then cut short by
  `iteration_timeout_s` with no budget or wall clock left to ask it again,
  so the run concludes decisively on THAT verdict instead of holding with
  a null verdict. `state`/`verdict` above already read the checkpoint's
  own outcome (`succeeded`/`verified` or `failed`/`rejected`) -- this line
  is what tells an operator, reading cold, that the verdict never got its
  refinement pass, and names what that refinement would have covered:

  ```
  provisional: the recorded verdict (verified) is PROVISIONAL (#264): review
               recorded it early (at 2026-09-23T15:00:00Z) and never got to
               refine it -- review's own refinement pass over its reason and
               findings before ending the iteration -- neither could run
               because the engine's iteration budget or wall clock was
               spent before review could be asked again (requirement H.2,
               #264).
  ```

  `--json` carries the same `provisionalVerdict: {verdict, provisional,
  reason, recordedAt, refinementScope}` object through untouched, and the
  hub's run-detail card renders the identical wording as a
  `.provisional-verdict` warning banner; the terminal record
  (`run-record.json`/`.md`, task 006/B.1) carries the same object under its
  own `provisionalVerdict` field. Absent entirely for every ordinary,
  fully-refined terminal verdict (see
  `LoopSupervisor._conclude_on_provisional_verdict()`'s own docstring).
- `termination:` (task 015, #46) -- shown only for a run a **signal** ended with
  no operator abort behind it, i.e. `status.json`'s `termination.class` is
  `self-inflicted`:

  ```
  termination: self-inflicted (signal 15) — no operator abort was recorded; eligible for auto-resume
               last tool call before the signal: bash({"command": "pkill -f
               ralphd-engine"}) in iteration 37
               (iterations/0037/output.jsonl)
  ```

  The evidence line is the last tool call recorded in the run's transcripts
  (`iterations/NNNN/output.jsonl`), which for the motivating case -- the agent
  killing its own supervisor from inside the container -- is the answer itself.
  This exists because the bare `reason: signal 15` this class used to print told
  an operator nothing they could act on; the `reason` now says the same thing in
  words, and this line names the class and the evidence as fields. An
  **operator** abort prints no such line: `reason: aborted by operator` already
  says it, and the operator is the one who asked. So every run that is not a
  self-inflicted termination keeps byte-identical human output. A self-inflicted
  termination is also *eligible for auto-resume*, unlike an operator abort --
  see `doctor --fix` below.
- `tasks:` -- a one-line summary of the `tasks` counts dict, e.g.
  `7/7 completed` when everything is done, or `5/7 completed (1
  in-progress, 1 pending)` when it is not, instead of a raw JSON dump of
  the counts.
- `list-equiv:` -- printed after `usage:` when the run has subscription
  traffic (a `claude-code` hat): the SDK's list price for it, e.g.
  `list-equiv: $0.4000  (subscription list price, not billed)`. It is never
  part of the `usage:` cost. On a payer-stamped run (#298) its note reads
  `(list price of subscription/free usage, not billed)`.
- `spend:` -- printed on a payer-stamped run (#298) after `usage:`, and after
  `list-equiv:` when that line is printed (a run with only metered or
  chargeback traffic has none): the same headline `ralphctl cost` opens
  with, e.g. `spend:     ~$58.65
  (chargeback, estimated); subscription: $0.00 (would be $27.17 at list)`.
  When `by payer` does not cover the whole token total (a run resumed
  across the upgrade) a `!! by payer covers N of M tokens ...` notice
  replaces it. A run without payers prints neither line.
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
- `waiting:` -- shown only while the run waits for a provider's rate-limit
  window to reset (`rateLimitWait` in [api.md](api.md); a `claude-code`
  iteration classified `rate_limit`). It names the window type and when it
  resets, in local time:

  ```
  waiting:   rate limit (five_hour) until 2026-10-02 17:00:00 +0200
  ```

  The run is not degraded: the iteration is refunded, the deadline moves by
  the time waited and the same work re-runs at the reset (or at once, after
  `ralphctl retry`).
- `paused:` -- shown only while the run is paused on a provider refusal
  (`pauseDetail` in [api.md](api.md); an iteration classified `refused`). It
  names the refusal family and quotes the provider's error, scrubbed:

  ```
  paused:    provider refused (extra-usage): 400 {"type":"error","error":{"type":"invalid_request_error","message":"Third-party apps now draw from your extra usage, not your plan limits"}} -- fix the profile or credentials (PUT /config/llm), then ralphctl unpause
  ```

  It is also shown while the run is paused on the rate-limit ceiling
  (`rate_limit_max_wait_s`): it names the waiting done, the ceiling and the
  window's reset in local time:

  ```
  paused:    rate-limit ceiling reached (1d waited of 1d); the five_hour window resets at 2026-10-03 09:00:00 +0200 -- ralphctl unpause resets the count, ralphctl budget --rate-limit-wait raises the ceiling
  ```

  Nothing is retried. Fix the LLM profile or credentials, then `ralphctl
  unpause <run-id>` re-runs the same work.

  It is also shown while the run is parked because a judging hat produced no
  verdict (`pauseDetail.reason: judge_no_verdict`): the peer `verdict_gate`'s
  reply was cut off by the provider or came back empty, and every
  `judge_retry_max` retry failed the same way. The `review` hat parks the same
  way when its reply was cut off, or empty after the contract ladder, on every
  attempt, and the line then names `review`. It names the hat and the cause:

  ```
  paused:    verdict_gate: no verdict: output truncated -- ralphctl unpause re-dispatches it
  gate:      no verdict (output truncated)
  ```

  The `gate:` line is the fourth `gateRejectionEnding` value
  (`no_verdict_output_truncated`). It is not a rejection and `status` never
  says `rejected on the merits` for it: nothing is archived, no composite PRD is
  written, and the recorded review verdict stands (a review park records none
  for that approach). A review whose contract ladder ended on empty replies is
  retried and then parked the same way, reading `review: no verdict: empty
  reply`. An unpause re-dispatches the hat that had no verdict.
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

  Two more wordings (task 016, issue #47) cover a reflection that produced
  **no verdict** because a signal was already taking the engine down
  (`reflect.ok` is `null` with a `skipped` reason):

  ```
  reflection: not attempted (signal 15 ended the engine before the reflect
              phase could start, so no reflect iteration was attempted)
  reflection: not completed (signal 15 ended the engine during the reflect
              phase, ...)
  ```

  Neither is a failure and neither leaves an `artifacts/reflection/FAILED.md`
  behind: `ralphctl stop` must not make a cleanly stopped run look like one
  whose post-mortem broke. `reflect: null`, and `ok: null` with no reason
  recorded (a pre-v0.7 run dir), still print nothing.
- `container:` (task 022, issue #8) -- shown only for an **unreachable** run
  whose status.json still records a non-terminal state (`starting`/`running`)
  while **no container is running** for it: the zombie condition
  `ralphctl doctor`/`repair` report. Printed right under the `state:` line
  (bold red on a TTY) so the operator does not have to join `state: running`
  with `(live api: False)` themselves:

  ```
  container: ralphd-myrun appears gone (no such container) -- status.json still
             records state 'running', so this run stopped without recording a terminal
             state; diagnose with `ralphctl repair myrun`
  ```

  Task 021 (issue #31) words the second dangling shape apart, because an
  exited container is still there to be inspected (`docker logs`) while a
  vanished one is not:

  ```
  container: ralphd-myrun still exists but has exited (nothing is running in it) -- status.json still
             records state 'running', so this run stopped without recording a terminal
             state; diagnose with `ralphctl repair myrun`
  ```

  For such a run the `duration:` line stops showing an ever-growing
  `(elapsed)` value -- nothing is elapsing -- and instead shows the
  **staleness**: the time since the last `status.json` write, labelled
  `(since last update)`. Any `iteration elapsed:` line is frozen at that same
  last write (`, at last update`). Every live or terminal run prints none of
  this, keeping its output unchanged.
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
`pending`/`validationFailed`, plus task 025's `failed` sub-counts, shared code
in `engine/state.py:task_counts`),
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

Live **event stream**, not a TUI: `watch` subscribes to the run's
`GET /events?since=0` and prints one line per event as it arrives —
`[<ts>] <type> <the event's remaining fields as JSON>` — replaying the whole
run from event 0 first. With the global `--json` flag (`ralphctl --json watch
<run-id>` — it belongs to the top-level parser, so it goes *before* the verb)
each line is the raw event object instead, i.e. NDJSON a script can parse.
That flag is the only switch: the rendering does **not** depend on whether
stdout is a terminal, so piping the default output to a file or another
process still gives you the `[<ts>] <type> {…}` lines, byte for byte, and a
script that wants objects must ask for them. Read-only; Ctrl+C
stops it. There is deliberately no curses/framework UI on this path (the CLI
is stdlib-only, see SPEC § "Packaging"); the run's live task table, gauges and
cost figures are `ralphctl status`, `ralphctl tasks` and the hub (`ralphctl
ui`), which render through the shared formatters (`engine/state.format_cost`
and friends, so an unpriced total reads `unavailable` rather than `$0.0000`).

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
=== outcome (meta.json) ===
iteration: 2  phase worker  approach 1
started:   2026-09-02 10:00:00 +0200
ended:     2026-09-02 10:17:51 +0200
duration:  17m 51s  (total)
exit:      clean exit
model:     amazon-bedrock/eu.anthropic.claude-opus-5  (gateway id: eu.anthropic.claude-opus-5)
tokens:    180,661 total (in 18, out 2,118, cache read 136,849, cache write 41,676)
cost:      $0.4231
steering:  001-focus.md

=== input (prompt.md) ===
…

=== logs (output.jsonl) ===
--- log (169 lines) ---
…
```

- **Task 020 (#81): three distinctly LABELLED regions, outcome FIRST** --
  `=== outcome (meta.json) ===`, `=== input (prompt.md) ===`,
  `=== logs (output.jsonl) ===` -- so an iteration that ended `exitCode: -2,
  interrupted: true` is readable without scrolling past the transcript. The
  hub's iteration dialog renders the identical three regions from the same
  server-side shaping (`state.iteration_detail_text`), so a fact cannot be
  labelled, ordered or worded differently on the two surfaces
  (`tests/test_hub_iteration_dialog.py`).
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
- **The `model:` line names the harness too.** When `meta.json` records a
  `harness` (every iteration from v0.30 on, `pi` included), the `model:` line
  ends with `  · <harness>`, for example `model:     amazon-bedrock/eu.anthropic.claude-opus-5  · claude-code`; an iteration that recorded a harness but no
  model prints `harness:   <h>` instead. An iteration from before v0.30 has no
  `harness` field and prints exactly as it always did. The hub's iteration
  timeline row shows `<model> · <harness>` the same way, and its dialog and
  this command share the line (`state.iteration_summary_lines`).
- **The thinking or effort level rides beside the model.** When `meta.json`
  records `thinking` or `effort` (written only when the hat's bundle carries
  one), the `model:` line ends with `  · thinking: <level>` or
  `  · effort: <level>` after the harness (on the `harness:` line when there is
  no model, on its own line when there is neither). An iteration that recorded
  neither prints nothing extra. `--json` carries `thinking`, `effort` and
  `thinkingDisplay` (`""` when neither was recorded); the hub's timeline row
  shows `thinkingDisplay` beside the model and harness.
- **The log region is the pi-shaped `output.jsonl`.** For a `claude-code`
  iteration that is the wrapper's normalised events, not the raw SDK stream:
  `output.claude.jsonl` is a forensic sidecar that neither this command, the
  hub's log view nor `ralphctl logs` reads.
- **Purely on-disk, no container needed and no snapshot notice.**
  `iterations/NNNN/meta.json`, `prompt.md` and the transcript are written by
  the engine itself, atomically, at the start and the end of every iteration,
  so the run dir is authoritative for a running job and for one whose
  container is long gone alike. `duration:`
  therefore says `(elapsed)` instead of `(total)` for an iteration still in
  flight.
- **The `input` region is that iteration's own `prompt.md`**, read verbatim;
  an iteration dir that never got one (or whose file is blank) prints `(no
  prompt.md recorded for this iteration)` instead of a blank region.
- **Unknown is not zero.** An iteration dir whose `meta.json` is absent or
  truncated (a crash mid-write) prints `!! no readable meta.json for this
  iteration` and `exit: unknown` — not a row of `None`s and not a clean exit —
  while still printing the transcript that *is* there. A cost the provider
  never priced (including a quote of `$0` next to billable tokens, see
  `status`' cost rules) renders `unavailable`, never `$0.0000`.
- **`exit: error: engine iteration failure: ...`** is an iteration the *engine*
  could not carry out (a stream/OS error in the event pump, a stray
  cancellation): the loop's containment boundary records it as one ordinary
  failed iteration — an `endedAt`, no exit code, and its fault verdict appended
  as `[<class> fault]` — and the loop carries on, so this line is how an
  engine-side explosion is read back and the run itself did not end because of
  it (issue #28, `docs/architecture.md` §9). `exit: iteration timeout` is the
  same guarantee for an iteration that ran past `iteration_timeout_s`: it costs
  one iteration, never the run.
- **`cost:`/`tokens:`** are that iteration alone, through the shared
  `format_cost` (4 decimals — one iteration is small money) and token
  formatter; only counters the provider actually reported are named.
- **`steering:`** names the steering files this iteration *applied*. A note the
  iteration was handed but did not earn (it failed, was interrupted or timed out,
  so the note stays pending for the next actionable iteration — docs/api.md
  `GET /steering`) gets its own line, `<file>  (delivered, not consumed -- still
  pending)`; a clean iteration's line is the plain one above.
- **`attempts:`** appears only for a slot that was attempted more than once —
  an engine was killed mid-iteration and the resumed engine reused the number.
  The dead attempt's `prompt.md`, transcript and partial `meta.json` are kept
  under `iterations/NNNN/attempts/NN/` (oldest first) instead of being
  overwritten, and the line says how many are there so the crash is readable
  after the resume. `--json`'s `archivedAttempts` is the same count (`0`
  normally).
- **`contract:`** (task 012, #92) appears only when this iteration's own
  window in `events.jsonl` (between its `iteration.end` and the next
  `iteration.start`, or the end of the log) holds a `contract.violation`/
  `contract.agent_fault` event — a hat's payload was refused by
  `enforce_contract` at least once during this iteration's slot, whether or
  not a later re-ask within the same ladder went on to recover it:

  ```
  contract:  'worker' contract violation: $.task_id: expected string, got int
  ```

  The text is produced by the exact same shared sentence-builder
  (`contract_enforcement.violation_message`) `status.json`'s `reason` line
  is built from when the ladder never recovers and the run aborts on it —
  so a recovered iteration and a run-ending one are never worded two
  different ways for the same underlying validation error; only
  `--json`'s `contractFault.agentFault` (below) tells them apart. Absent
  entirely for an iteration whose hat never had a payload refused.
  `--json` carries the full derivation as a `contractFault` object:
  `hat`, `errors` (the validator's own list, verbatim), `attempts`,
  `maxAttempts`, `agentFault` (`true` only if the ladder ran out inside
  this iteration's window) and `text` (the same rendered sentence the
  human `contract:` line prints) — or `null` when there was no violation.
- The transcript is rendered by the same merge and renderer
  `logs --iteration n` uses, so the two commands cannot show the same events
  differently; an iteration that wrote none prints `(no transcript yet)`.
- `--json` prints the whole `meta.json` verbatim (`exitCode`, `interrupted`,
  `timedOut`, `noTrafficTimeout`, `error`, `faultClass`, `usage`,
  `steeringDelivered`/`steeringConsumed`, `modelResolved`/`modelRaw`, `verifiedTask`/`verifyOutcome`)
  plus the derived fields the human view shows: `exitReason`, `durationS`,
  `durationDisplay`, `durationLabel`, `startedAtLocal`/`endedAtLocal`,
  `tokensDisplay`, `costDisplay`, `costStatus`, `hasMeta`, `hasTranscript`,
  `transcriptBytes`, `hasPrompt`, `promptBytes`, `promptText` (the "input"
  region's raw `prompt.md` body), `archivedAttempts`, `contractFault`
  (task 012, above) and `log` (the
  rendered lines, never ANSI). With
  `--no-log` the `log` key is **absent** rather than empty — an empty list
  would claim the iteration produced no transcript.
- Exit codes: `0` · `3` run not found · `1` no such iteration in that run,
  naming the ones on disk (`run X has no iteration 47 (iterations on disk:
  1..12)`), the same code the live `logs --iteration` path returns for the
  engine's 404. Pinned by `tests/test_cli_iteration_detail.py`.
- The three region bodies are worded ONCE, by
  `ralphd.engine.state.iteration_detail_text` (the outcome region is
  `iteration_summary_lines`, the input region is `read_iteration_prompt`, the
  logs region is the shared `log_render` output), and the hub's iteration
  dialog (task 020, `GET /api/runs/<id>/iterations/<n>`) shows those very
  regions — asserted line-for-line by
  `tests/test_hub_iteration_dialog.py`, so the two surfaces cannot grow two
  vocabularies for the same `meta.json`/`prompt.md`.

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
  `work` · `signal` · `rate_limit` · `refused` · `output_truncated`), with the iteration and phase it happened
  in — read from the newest `iterations/NNNN/meta.json` that carries a
  `faultClass`.
- **`because:`** names which branch of the classifier decided it, in the
  classifier's own words: the startup watchdog fired (no LLM traffic at all) ·
  the error text matched a known infra signature · the agent reached the model
  and then failed · a signal ended the iteration after it had reached the model
  · a bare `aborted` with a clean exit ended it after real traffic, with no
  abort recorded for the run (the provider hung up mid-stream)
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
  of the abort/interrupt shapes stays deliberately coarse — an engine-side
  give-up is still `work`, because that branch's job is "never retry this as an
  outage" — which is issue #49, not this surface's business. A signal that ended
  an iteration with **no** abort recorded for the run is its own `signal` class
  (#49 part 1, task 013). A bare `aborted` after traffic with a clean exit and
  no abort recorded is `infra` — a stream the provider hung up on, retried and
  refunded, at **any** duration (#49 part 2, task 014; the duration bound this
  used to carry was retired by task 117 / steering 005 after a run's worth of
  real stream aborts landed above it and failed an approach for it).
  The provider reporting its rate-limit window as reached (a `claude-code`
  iteration's structured evidence, kept in `meta.json`) is `rate_limit`:
  waited out until the window resets, without spending an iteration or the
  outage budget. A non-transient refusal from either harness (an HTTP 400,
  401, 403 or 404 naming a bad request, rejected credentials, a missing
  permission or model, or the subscription's extra-usage refusal; or the
  `claude-code` harness refusing to start) is `refused`: not retried, and the
  run pauses for the operator.
  A final reply the provider cut off at its output-token limit
  (`stopReason: length`, or a claude-code `max_tokens`) before it carried any
  text is `output_truncated`, and `because:` says the provider cut the final
  reply off, not that the work failed. A cut-off reply that did carry text is a
  usable reply and is not a fault. The class adds no retry and no refund of its
  own.
- **`signature:`** is the row of `engine/faults.py`'s `INFRA_SIGNATURES` table
  that matched: its family (`dns` · `tcp` · `stream` · `tls` · `sdk` ·
  `http-5xx` · `backpressure` · `bedrock-stream` · `capacity`), what that
  family means, the pattern, and the exact substring it matched in the error.
  For a `refused` fault it is the row of `REFUSED_SIGNATURES` that matched,
  printed the same way (`extra-usage` · `invalid-request` · `authentication` ·
  `permission` · `not-found` · `model-not-found` · `cc-version` · `cc-cli-missing` · `cc-root`):
  `signature: authentication -- the provider rejected the credentials
  (pattern authentication_(?:error|failed), matched "authentication_error")`.
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
- **`waiting:`** appears while the run waits for a rate-limit window to reset
  (status.json's `rateLimitWait`), with the same wording as `status`:
  `waiting:   rate limit (five_hour) until <local time>`.
- **`paused:`** appears while the run is paused on a provider refusal or on
  the rate-limit ceiling (status.json's `pauseDetail`), with the same wording
  as `status`; a ceiling pause explains itself as `fault:     rate_limit`.
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

A run started on 0.32.1 or later with a `billing` block (#298, see `ralphctl
start --billing-payer`) also groups the cost **by payer** (abridged):

```
$ ralphctl cost deck-phase4k
run:       deck-phase4k
spend:     ~$58.65 (chargeback, estimated); subscription: $0.00 (would be $27.17 at list)
cost:      $1.2140 + ~$57.4329 derived
list-equiv: $27.1744  (list price of subscription/free usage, not billed)
tokens:    106,200,000 total (in 700,000, out 1,500,000, cache read 104,000,000)
by payer:
  Claude subscription                              subscription  57,400,000 tokens  $0.0000    list-equiv: $27.1744
    anthropic/claude-sonnet-5-5                    subscription  57,400,000 tokens  $0.0000    list-equiv: $27.1744
  AIGW gateway                                     chargeback    48,800,000 tokens  ~$58.6469  ($1.2140 quoted + ~$57.4329 derived)
    aigw-openai/openai.gpt-6-astra                 chargeback    41,700,000 tokens  ~$57.4329  (~$57.4329 derived)
    amazon-bedrock/eu.anthropic.claude-sonnet-5-5  chargeback     7,100,000 tokens  $1.2140
note:      by phase, by approach and by tier are alternative breakdowns of the same total -- do not add them up
by phase:
  ...
```

`status.json`'s `usage` has carried `byPhase`/`byApproach` buckets since
day one (`loop._accumulate_usage`, PRD req 19) and nothing rendered them:
`status` printed one headline plus a hard-coded `planning/worker/review`
parenthetical (two of which a vigilant run does not even use), and the hub's
usage card showed the raw numbers. "Which phase burned the tokens" and "how
much of this figure is actually known" meant reading JSON by hand.

- **`spend:`** (#298) comes first on a payer-stamped run: the metered and
  chargeback money as one figure (marked `~` and `estimated` when part of it
  was derived; the quoted/derived split stays in each payer row's footnote and
  in the `cost:` line), then the subscription/free bases at `$0.00` with what
  they would cost at list. A payer label used under both kinds of basis
  (its row reads `mixed`) feeds both halves: its money is spend and its
  list-equivalent goes to the `$0.00` clause. `mixed` is never printed as a
  basis there; the bases its model rows name are, else `zero-priced` for the
  `$0.00` clause. It is withheld, with a `!!` notice saying how many
  tokens `by payer` covers, when the run was resumed across the upgrade and
  its earlier iterations were never stamped. "Spend", not "you pay": a
  chargeback is not necessarily personal money.
- **`cost:`** is the headline — the *same* string `ralphctl status` and the
  hub's usage card show, produced by the one shared formatter
  (`ralphd.engine.state.format_cost`, `decimals=4` here), so a breakdown can
  never disagree with the number printed beside it.
- **`list-equiv:`** appears after `cost:` when the run has subscription
  traffic (a `claude-code` hat): the SDK's list price for it, apart from the
  cost, which stays `$0` for that traffic. The phase and approach tables gain a
  `list-equiv:` column on the rows that have one. On a payer-stamped run it
  also covers `pi` routes billed on a `subscription`/`free` basis, and its
  note reads `(list price of subscription/free usage, not billed)`.
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
- **`by phase:` / `by approach:` / `by tier:`** are the run's own buckets:
  phases in the engine's order, approaches numerically (`10` after `2`),
  tiers in ladder order (`trivial` < `normal` < `hard`, task 017, #68 half
  2). The tier bucket also carries an `iterations` count (how many
  iterations dispatched at that tier), the one field no other bucket has.
  A bucket that recorded nothing renders `(none)` — never `$0.00`, which
  would read like a phase that ran for free.
- **`by payer:`** (#298) groups the same total by whatever payer labels the
  run's profiles declared — never by model prefix, so two gateways' routes or
  a subscription and a gateway on the same `anthropic/` model land in their
  own rows. Columns: payer, basis, tokens, cost, then the list-equivalent and
  a footnote when present; each payer's model rows (keyed by the requested
  model ref) follow it, indented. A zero-priced row's cost is `$0.0000` and
  its list-equivalent says `unavailable` (or `$X+ (partial, rest
  unavailable)`) when some of its tokens could not be list-priced — what a
  `claude-code` iteration interrupted before the SDK's own figure looks like.
  The closing `note:` says the breakdowns are alternatives, not addends. A
  run recorded before payers keeps exactly the layout above.
- **`!!` notices** name the anomaly behind a wall of `unavailable`: a provider
  that quoted `$0` for billable tokens (task 049) is reported as unpriced, and
  the notice names the `pricing:` map as the remedy — the same sentence every
  other cost surface uses.
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
  `byPhase`/`byApproach`/`byTier` **lists** (each entry = that bucket's own
  counters plus `key`, `tokens`, `tokensDisplay`, `tokensTotalDisplay`,
  `costDisplay`, `costSource`; `byTier` entries additionally carry
  `iterations`), `costDisplay`/`costStatus`/`costSource` for the run,
  `model`/`modelRaw`, `sources`, `notices`, `hasUsage`, plus `summaryLines` and
  `text` (the human block, verbatim, minus the `run:` line). A payer-stamped
  run adds `payerView` (`headline`, `note`, `notices`, `columns`, `rows`, each
  row with `basis`, `costDisplay`, `footnote`, `listEquivalentDisplay` and its
  `models`); the key is absent otherwise.
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
| `run-record` | `run-record.md` (`artifacts/`) | the engine's own terminal record, human-rendered (task 006, B.1) |
| `run-record-json` | `run-record.json` (`artifacts/`) | the JSON `run-record.md` renders from (task 006, B.1) |

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

The listing also carries `run-record`/`run-record-json` rows once the run has
reached `finishing` (task 006, B.1) -- absent before that, same
`(not written)` wording as `composite-prd` above.

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

**A failed task says which kind of failed it is (task 025, issue #33).** The
status column prints `failed (validation-exhausted)` or `failed
(requirement-unmet)` — the same words the counting surfaces use, derived for a
plan written before the label existed — and the column is as wide as its widest
entry, so a plan with no failed task prints byte-identically to before.
`--json` carries the same answer per task id in `taskFailureKinds`
(`{"014": "validation-exhausted"}`, omitted entirely when nothing failed),
exactly as `GET /tasks` serves it.

**`--approach N` / `--all` read an archived approach's OWN task table (task
019, #24).** Every time the review ladder starts a new approach, the engine
copies the finished approach's `tasks.json`/`notes.md`/`review-findings.md`
into `approaches/NN/` (`LoopSupervisor._archive_approach()`,
`engine/loop.py`) before wiping the live plan for the next attempt — so the
plain command above only ever shows the CURRENT approach.

- `ralphctl tasks <run-id> --approach N` prints (and, with `--json`, carries)
  approach `N`'s own task table, read straight from
  `approaches/NN/tasks.json` through the same hardened reader as the live
  path — never recomputed from the live plan, which may be several
  approaches further on. `N` is zero-padded to two digits on disk
  (`01`, `02`, …) but taken as a plain integer on the command line. An
  approach that was never archived (including the CURRENT, still-live one)
  is a clean, non-zero-exit error naming the missing file, never a
  traceback.
- `ralphctl tasks <run-id> --all` lists every archived approach with its own
  `completed/total` summary, each counted from THAT approach's own archived
  file (never the live plan's counts repeated under a different label).
  `--json` carries one entry per approach with its raw `counts` dict plus
  the same `tasksStale`/`tasksSource` contract as any other read. A run with
  no archived approaches yet (including one still on its first attempt)
  prints nothing and exits `0`, with a notice on stderr — not an error: it
  has not failed an approach yet, which is good news, not a fault.
- `--approach` and `--all` are mutually exclusive with each other (each
  names a different document to read) and, unlike the plain command, always
  read the run dir directly rather than trying the live API first: an
  archived approach's files never change once written, live or dead run.

### `ralphctl steer <run-id> [message]`

Send steering. Message from arg, `--file <f>`, or stdin.

| Option | Meaning |
|--------|---------|
| `--now` | also SIGINT the current iteration so guidance applies immediately — or, if the loop is idle (between iterations, in a deferred wait, in a backoff wait), wake it instead (v0.25 task 028b, PRD requirement K.3/K.4, #267); either way this exits `0` and the printed line says which of the two happened |
| `--name <slug>` | steering file slug |
| `--list` | **show** this run's steering messages instead of sending one (task 018, #17) |

**Which engine states wake, which signal, and what each answers (v0.25 task
028c, PRD requirement K.6/K.7, #267).** `--now`'s two outcomes above are
exhaustive: **every** running iteration is SIGNALLED, whatever phase it is
in -- `planning`, `plan_review` and `review` included, exactly the same as
`worker`/`verify`/`reflect`/`steer`/`verdict_gate` always were. K introduces
**no new interrupt semantics for these three judging hats**: signalling a
running `plan_review` iteration is re-asked in place with no revision spent
(a SECOND signal in a row still falls to the pre-existing #161 policy --
a revision spent, the plan re-ingested); signalling `planning` retries in
place (`_one_planning_attempt`'s own inner loop); signalling `review`
self-loops via the pre-existing `review_interrupted` guard (requirement A,
#72). None of that is new, none of it is widened, and `--now` against one
of these three costs exactly what an interrupted iteration always cost
(#161's own accounting, PRD non-negotiable 10) -- it does **not** reach the
idle-wake path at all. Only a genuinely idle loop -- between iterations, in
a deferred wait (K.1/K.2), or in a backoff wait (K.5) -- is WOKEN instead.
Both outcomes answer `200`; `409` is reserved for a run that has already
finished (`state` terminal), checked before either outcome and before any
steering in the same request is enqueued.

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
  becomes `applied` — once an actionable iteration that carried the note has
  finished cleanly, never merely because it was delivered, see docs/api.md); when the container is gone the run dir's `steering/`
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

### `ralphctl rule <run-id> --file <path> [--name <slug>]`

Record an operator **ruling** — the answer channel (task 013, v0.23
requirement A.2, #224) — in response to a rejection the engine is about to
spend an approach on. A distinct command from both `steer` (discounted by
review by design) and `amend` (binds review but not verify, and is orphaned
by a replan): a ruling reaches BOTH judging hats and is keyed to the
rejection's own signature (`_review_rejection_signature`, persisted as
`status.json`'s `rejectionSignatures`), never a task id, so it survives a
replan that renumbers every task back to `001`.

| Option | Meaning |
|--------|---------|
| `--file <path>` | **required.** The ruling text. There is no positional or stdin form for this command. |
| `--name <slug>` | ruling file slug |

**`--file` is the only path in; a bare positional message is refused.** A
positional message slot has previously swallowed a ruling silently while
`--file` was ignored (#224's own named incident), so `ralphctl rule <run-id>
<text>` is exit `2`, naming `--file`, before any request reaches the network
— unlike `steer`/`amend`, which both accept a positional message.

Posts through the run's own live API (`POST /rule`), exactly the way `steer`
posts through `POST /steering` — never a second, host-side writer — so the
CLI, the hub's run-page textarea and a direct `POST /rule` all land the same
ruling into the same run-scoped, cumulative record
(`engine.state.write_ruling`/`ruling_entries`, under `<run-root>/rulings/`,
deliberately NOT the same `<config-root>/amendments/` directory `amend`
writes to — `POST /rule` is handled in-container and needs write access
there, unlike an amendment's host-only write).

Exit `0` accepted · `2` usage error (no `--file`, a bare positional message,
or an empty file) · `3` unknown run · `4` API unreachable · `5` job already
finished.

### `ralphctl amend <run-id> <task-id> [text]`

Record an **operator ruling** — a waiver or a replacement of one task's
`successCriteria` — that the review phase must honour (task 028, #26). For
when a PRD criterion turns out to be impossible mid-run, or a scope call only
the operator can make, and the run has no other way to change a verdict:
steering can redirect what the *worker* does next, but review and verify are
deliberately outside `STEERING_ACTIONABLE_PHASES` — a steering message never
reaches them — so without this verb a run just burns every remaining approach
re-failing the same finding.

| Option | Meaning |
|--------|---------|
| `--waive` | a full waiver: that task's criteria are satisfied by operator decision, not a text replacement |
| `--status <status>` | record a status ruling instead (task 006/029, #26 requirement E; every declared status except the engine-forced `superseded`) — see below |
| `--approach N` | stamp this ruling as recorded against approach N (task 036, #168) instead of the run's own current approach — see below |
| `--force` | write even though `task-id` is not in this run's current plan (task 036, #168) — see below |
| `--wave` | record a **wave-scoped** ruling instead of a task-scoped one — carries no task id at all; see below (task 017, D.7, #196) |
| `--repoint SEQ` | re-point an already-recorded ruling (its `SEQ`, as shown by `--list`) to a new target instead of recording a new one — see below (task 021, D.12, #196) |
| `--to TASK_ID` | the new task-id target for `--repoint`; combine with `--wave` instead to re-point onto the wave scope |
| `--file <f>` | read the ruling text from a file instead of the trailing argument |
| `--list` | **show** this run's recorded rulings instead of recording one |

Text comes from the trailing positional argument, `--file <f>`, or stdin — the
same three-way choice as `steer`. For a criteria ruling (the default, or
`--waive`), that text is required and non-empty.

**Before writing anything** (task 036, #168, closing the mis-addressed-ruling
gap #168 diagnosed), the command reads (never writes) `tasks.json` under the
run's OWN run dir and echoes `task-id`'s current title and status to stderr —
so a typo in the id, or a ruling meant for a different task, is visible at
write time instead of only at review time. When `task-id` is not in the
current plan at all, the command refuses (exit `2`) unless `--force` is
given; a run with no `tasks.json` at all yet is not treated as a refusal —
there is no plan for the id to be absent FROM. The echo/refusal is read-only
and does not touch the design's unforgeability guarantee below, which is
entirely about where a ruling gets WRITTEN.

`--approach N` (task 036, #168) stamps `approach: N` in the amendment file's
fixed header; omitted, the command defaults to the run's own current approach
read straight from `status.json` (never a value it invents), and if neither
is available the header simply carries no `approach:` line at all — an old
entry, or one written before any approach had committed, reads as "the
approach is unknown" rather than as approach 0. This is the raw materials a
later review pass uses to scope a ruling to the approach it was actually
recorded in, rather than reapplying it verbatim to a same-numbered task in a
fresh replan (#168's incident).

#### `ralphctl amend <run-id> --wave [text]`

A **wave-scoped** ruling (task 017, D.7, #196): the reserved target
`engine.state.AMENDMENT_WAVE_TARGET` (`"WAVE"`) is written as the ruling's
`task:` header in place of a real task id, through the exact same
`write_amendment` writer as every other ruling kind — there is no second
on-disk shape, just a task id that is never a real one. #196 is the incident
this closes: a replan restarts task ids at `001`, so a ruling written against
a task id can be orphaned the moment the id it named stops existing; a
wave-scoped ruling has no task id for a replan to make disappear in the first
place. `ralphctl amend <run-id> --list` renders a `SCOPE` column reading
`wave` for this kind and `task` for every ordinary task-scoped ruling (and
`"scope": "wave"`/`"scope": "task"` in `--json`), the same `amendment_entries`
reader every other listing column comes from.

`--wave` is a criteria-ruling target only — it carries the ruling text (or
`--file`/stdin) exactly like the default form above, `--waive` still means a
full waiver rather than a text replacement, and `--approach` still stamps or
defaults exactly as described above. Combining `--wave` with a task id,
`--force` (there is no plan-membership check to override, since there is no
task id) or `--status` (a status ruling names one task's `status` field,
which a wave has none of) is a usage error, exit `2`, before any file is
written. Binding this scope to review and the peer verdict gate
unconditionally, whatever the current approach, is D.8 (task 018) — this
command only records the ruling and shows its scope; see
`LoopSupervisor._operator_amendment_review_context` for the binding logic
itself once that task lands.

**An orphaned ruling escalates, and shows without reading `events.jsonl`
(task 020, D.11, #196).** When a replan removes a task-scoped ruling's own
target from the current plan, `_operator_amendment_review_context` still
renders it BINDING (D.9/D.10) but also emits `amendment_target_not_in_plan`
and, whenever a notifier is configured, sends one notification naming the
affected ruling(s), their old target task id(s) and the current approach
number — the same `self.notifier` path E.3/E.5 already use, never a second
channel. `ralphctl amend <run-id> --list` shows the same fact directly, with
no event log to parse: a `STATE` column (`_amend_entry_is_orphaned`) reads
`ORPHANED` for a task-scoped ruling whose `taskId` is absent from this run's
current `tasks.json` (`_amend_orphaned_task_ids`, the same read-only,
host-side `read_tasks_doc(persist=False)` `_amend_echo_target_or_die` already
uses) and `-` otherwise — a wave-scoped ruling is never orphaned (D.7/D.8),
and a run with no `tasks.json` at all yet shows `-` for everything, exactly
the review-time carve-out. `--json` carries the same fact as a boolean
`"orphaned"` key on each entry.

#### `ralphctl amend <run-id> --repoint <seq> (--to <task-id> | --wave)`

Re-address an ALREADY-RECORDED ruling — found by the `SEQ` number `--list`
already shows — to a live task id or to the wave scope, with **no hand-edit**
of the file under `config_root(run_id)/amendments/` (task 021, D.12, #196):
the amendment-analogue of `steer task-update`'s `dependsOn` rewrite, so an
orphaning D.11 escalates to the operator has a one-command fix rather than a
text editor. Goes through `engine.state.repoint_amendment` — the same file's
`task:` header line is rewritten in place and the file itself is renamed
(`NNN-<old-target>.md` → `NNN-<new-target>.md`, same `NNN`) — never a second
writer alongside `write_amendment`; `mode:`/`status:`/`approach:`/`ts:` and
the ruling text are all untouched.

`--repoint` is a distinct mode of its own, like `--list`: combining it with a
task id, text, `--file`, `--waive`, `--status`, `--force` or `--list` is a
usage error, exit `2`, before any file is touched. It needs EXACTLY one of
`--to <task-id>`/`--wave` as the new target — neither, or both, is a usage
error. `--to <task-id>` is refused, exit `2`, naming the id, when that id is
not in this run's CURRENT plan (`tasks.json`, read the same read-only,
host-side way `_amend_echo_target_or_die` does) — a re-point must not itself
manufacture a second orphaning; `--wave` carries no such check, mirroring the
ordinary wave-scoped write path. A `SEQ` with no recorded ruling at all is
refused, exit `2`, naming the missing sequence number.

#### `ralphctl amend <run-id> <task-id> --status <status> [reason]`

A THIRD ruling kind (task 006/029, #26 requirement E), alongside the
waiver/replacement above: a typed status ruling the ENGINE itself applies to
that task's `status` field in `tasks.json`, at load time and at every later
worker/review step boundary, rather than one that only binds review's
judgement of successCriteria. Goes through the exact same `write_amendment`
writer as every other ruling — `status=` instead of `waive=` — there is no
second writer and no second on-disk shape; the file gets `mode: status` and a
`status: <status>` header line instead of `mode: waive`/`mode: replace`.

`<status>` must be one of `engine.transitions.OPERATOR_RULEABLE_TASK_STATUSES`
(`pending`, `in-progress`, `awaiting-validation`, `validated`,
`validation-failed`, `completed`, `failed`, `skipped`, `petitioned`) —
every declared task status EXCEPT the engine-forced ones
(`transitions.ENGINE_FORCED_TASK_STATUSES`, today just `superseded`, which
`state.supersede_task()` writes itself at a spent progress budget; v0.20
requirement B.1, #156). Anything else is
rejected right here, on the host, with the full list of legal statuses in the
error, exit `2` — no file is ever written for a bad status, so a typo can
never land as an inert file the engine silently ignores. The trailing
text/`--file`/stdin argument becomes the operator's OPTIONAL reason for a
status ruling — never required, and never read from stdin when omitted (unlike
the criteria-ruling path, which blocks on stdin when nothing else is given).
Combining `--status` with `--waive` is a usage error (exit `2`): a ruling is
exactly one kind, never two at once.

Applying the ruling to `tasks.json` is NOT this command's job —
`ralphctl amend` never WRITES to the run dir at all, only to `config_root` (it
does read `tasks.json`/`status.json` there, read-only, for the echo and
`--approach` default described above — task 036, #168). The engine applies it
(`LoopSupervisor._apply_operator_status_rulings` →
`engine.state.apply_amendment_status_rulings`) at the next worker or review
step boundary of the LIVE run — and again at the top of every later episode —
so a ruling recorded while the container is up takes effect without a restart
(GH #209 defect 6, which is what made this true: before it, the applier ran
once per episode and a ruling on a live run changed nothing at all, since
`mode: status` entries are deliberately kept out of review's prompt context
too). It is checked against the same declared task-status transition table
every other status move is checked against; an illegal transition for the
CURRENT status is refused there, not here, since this command has no way to
know the task's current status at write time. `ralphctl amend <run-id> --list`
shows a recorded status ruling as `status  -> <status> (<reason>)` in the
table (and `mode: "status"` / `status: "<status>"` in `--json`), the same
reader as every other ruling kind.

**Why this is unforgeable, and why that's the whole design.** `ralphctl amend`
runs on the HOST and writes directly into `config_root(run_id)`
(`~/.ralphd/configs/<run-id>/`) — the exact directory `ralphctl start`/`resume`
bind-mount **read-only** as `/config` into the job container. Nothing that
runs inside that container — the worker's own shell, the API's config-CRUD
routes, the writable config overlay under `$HOME` — can write there; a `:ro`
bind mount is a kernel-enforced boundary at the mount-table level, and the job
container gets no elevated docker capabilities that could defeat it. So a
ruling's *authenticity* is a fact about where the file physically lives, never
about its wording or about a signature review would have to verify — the same property
`job.yaml`/`prd.md` already lean on for "the operator configured this, the run
didn't." See `src/ralphd/prompts/review.md`'s "Operator rulings" section for the rule review
is told to follow.

On-disk shape: `config_root(run_id)/amendments/NNN-<task-id>.md`, `NNN`
assigned monotonically by `ralphctl amend` itself (never supplied by the
caller), each file a small `task:`/`mode:`/`ts:` header followed by a blank
line and the ruling text verbatim (`engine.state.write_amendment`/
`amendment_entries`, the ONE reader/writer pair, mirroring the `steering/`
convention). `engine.loop`'s `_operator_amendment_review_context()` reads this
directory directly (never the writable overlay) at every point a review-phase
prompt is built, and appends every *criteria* ruling found
(`mode: waive`/`mode: replace`) as a mandatory, unconditional instruction —
review never has to (and must not) ask why the operator is entitled to grant
one. A `mode: status` ruling (v0.14, requirement E) is deliberately excluded
from that context: it changes a task's `status` in `tasks.json`, applied by
the ENGINE at load time and at every later worker/review step boundary
(`LoopSupervisor._apply_operator_status_rulings` →
`engine.state.apply_amendment_status_rulings`, validated against the
task-status table, leaving `validationAttempts`, `tierLadderExhausted` and the
criteria text untouched and emitting an `amendment_status_applied` event with
`source: operator`), and it binds review's judgement of successCriteria not at
all — a status ruling is not a claim the criteria were met and must not be
able to read like one.

`--list` has no live half at all: `ralphctl amend` never talks to the engine,
so there is nothing for a live container to answer differently than the file
on disk — it always reads `config_root(run_id)/amendments/` directly, live or
dead run, with a `(no operator rulings recorded)` notice when there are none.
Unknown run id is exit `3`; combining `--list` with a task id, text, `--file`,
`--waive` or `--status` is exit `2`, mirroring `steer --list`'s own mutual
exclusion.

### `ralphctl interrupt <run-id>`

v0.25 task 028b (PRD requirement K.3/K.4, #267): SIGINTs the current
iteration if one is running, without adding steering; if none is running
(the loop is idle between iterations, in a deferred wait, or in a backoff
wait) it wakes that idle wait instead, so an operator is never told the
request failed while the loop was simply idle. Exit `0` either way, with the
printed line naming which happened (`a running iteration was signalled` /
`an idle loop was woken (nothing was running to signal)`); exit `5` only for
a job that has already finished.

"A running iteration" above means any phase at all, `planning`/
`plan_review`/`review` included -- task 028c (PRD requirement K.6, #267)
names this explicitly because it is the one thing K does **not** touch: no
new interrupt semantics reach these three judging hats, and interrupting one
of them keeps exactly its pre-existing cost (`plan_review`'s own #161 policy:
one signal is re-asked in place for free, a second in a row over a fresh
plan spends a revision and re-ingests the plan) -- see docs/api.md's
`POST /interrupt` for the fuller statement.

### `ralphctl pause <run-id>` / `ralphctl unpause <run-id>`

Hold/release the loop at the next iteration boundary.

`unpause` is also how you proceed past an engine-recorded pause
(`pauseReason` in `status.json`): it POSTs `/resume` to the live engine and
clears the reason, and it ALWAYS releases the pause on its own, whichever
reason set it. Whether a ruling should come first depends on the reason:
for `operator_input_needed`/`rejected_repeated_signature`, `ralphctl rule
<run-id> --file <ruling.md> --name <slug>` ALONE already releases the run
(the two commands are interchangeable single-step alternatives); for every
other reason that waits on a ruling (`rejected_last_approach`, `rejected_
incurable`, `stagnation`), a `rule` alone records the ruling but leaves the
run paused -- it takes the explicit sequence, `ralphctl rule <run-id>
--file <ruling.md> --name <slug>` then `ralphctl unpause <run-id>`, to
record a ruling before resuming; `ralphctl unpause <run-id>` alone still
works too, resuming with no ruling recorded. The full `operator_escalation.escalation` text follows the same rule (cure task cure-01-01-9): for `rejected_last_approach`, `rejected_incurable` and `stagnation` (both variants) it says only `ralphctl unpause <run>` releases the pause, that filing a ruling records it but leaves the run paused, and gives the `ralphctl rule ... then ralphctl unpause ...` sequence beside the unpause-alone alternative, and for `rejected_repeated_signature` it says a ruling releases the pause on its own, or `ralphctl unpause <run>` resumes with none recorded to accept the default. Every pause and escalation
text the engine produces names both live-run levers, never `ralphctl
resume` -- `resume` starts a fresh container over an existing run dir
(crash recovery, budget top-up) and is not for a run that is still alive.

A provider refusal (`pauseReason: operator_input_needed` with `pauseDetail.
reason: provider_refused`, printed by `status` as `paused:    provider
refused (<family>): ...`) is answered by fixing the cause, not by a ruling: a
ruling never releases it. Fix the LLM profile or credentials (`ralphctl llm`,
`PUT /config/llm`), then `ralphctl unpause <run-id>` re-runs the refused work.
An unpause before the fix re-runs it too, and pauses again if it is refused
again.

A no-verdict park (`pauseReason: operator_input_needed` with `pauseDetail.
reason: judge_no_verdict`, printed as `paused:    verdict_gate: no verdict:
output truncated`, or `review: no verdict: output truncated`) is not answered by
a ruling and an unpause grants it no `proceed`: `ralphctl unpause <run-id>`
re-dispatches the hat (the gate, or review for a review park) as a fresh first
attempt, with a fresh retry allowance, and its verdict then decides the run. With `judge_retry_max: 0` the park follows the first failed attempt. When
the iteration budget or the deadline is already spent at the park, the run does
not park: it ends `unreviewed` with review's verdict intact, never `rejected`.

A rate-limit ceiling pause (`pauseDetail.reason: rate_limit_ceiling`, printed
as `paused:    rate-limit ceiling reached (...)`) is not answered by a ruling
either. `ralphctl unpause <run-id>` resets the run's rate-limit waiting count
and re-probes the same work; `ralphctl budget <run-id> --rate-limit-wait +N`
raises the ceiling first if the run should keep waiting on its own.

With a notifier configured (`--notifier-url`), every engine-recorded pause
also POSTs the operator a bounded digest (at most `MAX_NOTIFY_CHARS`) instead
of the raw diagnostic text: run id, `pauseReason`, `approach n of m`,
blocking/residual counts, tasks reopened/inserted, spend this approach, the
hub URL when `hub_base_url` is configured, and the exact answer command(s)
for that reason, phrased the way the paragraph above describes. That covers
every pause reason, including the task-boundary commit-secret-scan hold
(`pauseReason: operator_input_needed` raised from `worker`, which resumes
into `worker`, not `review`): its digest names the offending commit's
`sha`, the `part` and the variable NAME, never the matched value, reads
`unavailable` for the findings and reopen/insert fields it has no channel
for, and its own answer command depends on whether the CURRENT approach
already carries a recorded rejection signature from an EARLIER `operator_
input_needed`/`rejected_repeated_signature` pause on that same approach,
already answered by a ruling before this hold ever landed: with none (the
common case), a `ralphctl rule` filed against it does not release it, so it
names only `ralphctl unpause <run>`; with one, `resume_if_ruling_answers_
pause` releases this hold too, so it names both levers as interchangeable
alternatives, exactly like `operator_input_needed`'s own review-side
pause. docs/api.md's `POST /pause` section lists the
full coverage.

A bare `ralphctl unpause <run-id>` on review's own `operator_input_needed`
pause, or on the peer verdict gate's `verdict_gate_operator_input_needed`
sibling (v0.27 requirement C.1), with NO ruling filed to answer it,
records a disk-backed one-shot 'proceed' for the exact rejection signature
that pause was raised over (v0.27 requirement C.2, #274; `status.json`'s
`noRulingProceed`, SPEC.md §5.2). The very next rejection with the
IDENTICAL signature consumes the proceed, whichever edge it takes, and
raises no same-signature operator gate while doing so. After a
`ralphctl budget <run-id> --cure-passes +1` grant, that rejection takes the
cure edge and the proceed is spent there: it never survives the cure to
authorise a later archive, so the next same-signature rejection after the
cure parks afresh. With no cure pass available, it takes the ordinary
archive-and-replan edge (or finishes, when no approach is left) --
or, for the gate's own mixed/all-incurable blocking-set rejection (cure
task 002, this wave), that rejection's own immediate terminal tail, never
a fresh replan, whatever approaches remain -- instead of parking a second
time, mirroring the one-re-park bound a filed ruling already gets
(`rulingReparked`). A DIFFERENT rejection never
matches the grant and still parks; filing a ruling that
answers the signature (review's own pause only) takes the existing
re-park path instead of granting this proceed at all. Without a matching
grant, a bare unpause with no ruling re-enters `review`/`verdict_gate` over
the SAME approach with nothing new to go on and is likely to re-raise the
identical rejection and re-park forever.

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

It also wakes a run waiting for a rate-limit window to reset (`status` shows
`waiting:   rate limit (...) until ...`): the same work re-runs at once. That
wait has no outage budget to reset.

Exit `0` woken · `5` the run is in neither wait (or already finished) ·
`3` unknown run · `4` API unreachable. `--json` prints the engine's
`{"retrying": true}`.

### `ralphctl undefer <run-id> <task-id>`

Release a **deferred** task now instead of waiting out its `deferredUntil`
(a worker's `verdict: "deferred"` report, or the engine's own re-poll
backoff). Posts `POST /tasks/<task-id>/undefer` through the same `api()` path
as `steer` and `retry`, and writes nothing host-side: the engine's loop
thread clears `deferredUntil`/`deferredReason` at its next selection boundary
and emits `task.undeferred`, ending a deferred wait at once (its
`deferred_wait` event reads `boundedBy: "undefer"`). Prints
`undeferring task <id> (released at the loop's next selection)`.

Exit `0` accepted · `5` the engine's `409` (the job has finished, or the task
is not deferred), printed with the server's message · `3` unknown run · `4`
API unreachable (`API unreachable: …`). An unknown task id is the engine's
`404`, which exits `1`. `--json` prints the engine's
`{"undeferring": true, "taskId": ...}`.

The hub's **Undefer now** button does the same through
`POST /api/runs/<id>/tasks/<task>/undefer` (see the hub section below).

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

This is a **live-engine** change to `/config/job.yaml` (read-only, so the
engine never rewrites it) but it now persists across a resume: a fresh
container's own startup seeds its budget from `max(status.json's
iterationsBudget, job.yaml's iterations)` (task 010, #51), so a plain
`resume <run-id>` with no flags already carries this top-up forward.
`resume <run-id> --iterations +N` is only needed to add *more* on top, and
it adds to that same effective (seeded) number, never to job.yaml's alone
(task 011).

Task 004 (#94 (c), operator-added scope) adds three more flags, each
independent and optional — any non-empty subset of the four (the bare
positional plus these three) reaches the engine in ONE request:

```console
$ ralphctl budget my-run --approaches +1        # top up the approaches budget
$ ralphctl budget my-run --timeout +3600        # push the job deadline out 1h
$ ralphctl budget my-run --iteration-timeout 900  # absolute per-iteration cap
$ ralphctl budget my-run +5 --approaches +1     # both in one request
```

`--approaches` maps to `maxApproaches` (refused with `5` if below the
current approach), `--timeout` to `jobDeadlineEpochS` (refused with `5` if
the resolved value is at or before now — `+N` extends the deadline by N
seconds, an absolute value is a Unix epoch second), `--iteration-timeout` to
`iterationTimeoutS` (seconds; applies to the *next* iteration, never one
already running). Same `+N|N` grammar as `--iterations` for every one of
them.

Task 010 (v0.27 requirement C.3) adds a fifth flag, `--cure-passes`, mapped
to `curePassCeiling` — the run-wide cure-pass ceiling both cure channels
(review's own and the peer verdict gate's) spend from a single shared pot
(see `curePasses`/`gateCurePasses` in [api.md](api.md)):

```console
$ ralphctl budget my-run --cure-passes +1       # top up the run-wide cure-pass ceiling
```

Refused with `5` if the resolved value is below the cure passes this run
has already taken (`curePasses`' and `gateCurePasses`' combined total).
Same `+N|N` grammar as every other knob. Unlike the three knobs above,
this one PERSISTS across a resume, exactly like `iterations` (see the
table below).

`--rate-limit-wait` maps to `rateLimitMaxWaitS`, the ceiling in seconds on
the run's total rate-limit waiting (`rate_limit_max_wait_s`, default 86400).
A rate-limit wait that would pass it does not start: the run pauses
`operator_input_needed` with `paused:    rate-limit ceiling reached (…)` in
`ralphctl status` and `ralphctl fault`, and the notifier names the window's
reset. Raise the ceiling, or just `ralphctl unpause` (which resets the count
and re-probes the same work):

```console
$ ralphctl budget my-run --rate-limit-wait +43200   # half a day more of waiting
```

The new ceiling is read before the next rate-limit wait. It persists across a
resume like `--cure-passes`.

**Persistence across a resume — one row per knob (#94 (d); task 010, v0.27
requirement C.3 adds the cure-pass row):**

| knob | flag | job.yaml key | across a plain `resume` (no flag) |
| --- | --- | --- | --- |
| iterations | `--iterations`/bare positional | `iterations` | **live**: persists across a resume — task 010/#51's startup seeding carries the top-up forward automatically |
| approaches budget | `--approaches` | `max_approaches` | **reverts**: process-only while the container is live; a plain resume reverts to job.yaml's own value unless this resume itself passes `--approaches` |
| job deadline | `--timeout` | `job_timeout_s` | **reverts**: process-only while the container is live; a plain resume reverts to job.yaml's own value unless this resume itself passes `--timeout` |
| iteration timeout | `--iteration-timeout` | `iteration_timeout_s` | **reverts**: process-only while the container is live; a plain resume reverts to job.yaml's own value unless this resume itself passes `--iteration-timeout` |
| cure-pass ceiling | `--cure-passes` | `cure_pass_ceiling` | **live**: persists across a resume — the engine's own startup seeds it from `max(status.json's curePassCeiling, job.yaml's cure_pass_ceiling)`, mirroring `iterations`' own seeding (task 010, v0.27 requirement C.3) |
| rate-limit wait ceiling | `--rate-limit-wait` | `rate_limit_max_wait_s` | **live**: persists across a resume — the engine's own startup seeds it from `max(status.json's rateLimitMaxWaitS, job.yaml's rate_limit_max_wait_s)` |

`iterations` and `curePassCeiling` are seeded from the LIVE-set value on
every resume because the engine's own startup already reads
`max(status.json's iterationsBudget, job.yaml's iterations)` (task
010/#51) and, since v0.27 requirement C.3,
`max(status.json's curePassCeiling, job.yaml's cure_pass_ceiling)`. The
other three have no such seeding: they are read from
`cfg.max_approaches`/`cfg.job_timeout_s`
(indirectly, via `loop.deadline_epoch`, which `job_timeout_s` only seeds at
*startup*)/`cfg.iteration_timeout_s` — i.e. from job.yaml — every time a
container starts, live-set values included. A `budget --approaches +1` an
operator applied mid-run is gone the moment that container is replaced by a
plain `resume`; carrying it forward needs `resume --approaches +1` (or
whatever absolute value) on that same resume, which writes job.yaml's
`max_approaches` directly (see `ralphctl resume` below).

Exit `0` applied · `5` the engine refused (resulting budget below
`iterationsUsed`/the current approach/the cure passes already taken, a
deadline at or before now, or the job already finished — resume instead) ·
`1` the engine rejected a value (`422`) · `2` locally malformed spec, or no
knob given at all · `3` unknown run · `4` API unreachable. `--json` prints
the engine's `{"iterations": 40, "previous": 25, "iterationsUsed": 17}`
(plus `maxApproaches`/`iterationTimeoutS`/`jobDeadlineEpochS`/
`curePassCeiling`/`rateLimitMaxWaitS` and their `previous*` counterparts for
whichever of the other five were included).

### `ralphctl abort <run-id> [--reason <text>]`

Terminate the job (state `aborted`, honors on-complete mode).

### `ralphctl stop <run-id>`

Shut down an **idle finished** container (calls `/shutdown`, then `docker rm`).
For a running job, refuses with exit `5` — use `abort` first, or `--force` to
abort+stop in one step. A `timed_out` run (the deadline hold, v0.24
requirement B.1) is accepted with no `--force` and no `abort`, exactly as a
stopped run in a terminal state is: the hold means the deadline expired and the
iteration in flight already finished, so there is nothing live to interrupt.
Run dir is never deleted by `stop`.

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
`failed`, `aborted`) **or `finishing`** (task 014, #56) **or `timed_out`**
(v0.24 requirement B.1 — the deadline hold has no iteration in flight at all,
so it is accepted like a stopped run; plain `rm` gives it no refusal of its own
either, only the generic container-still-exists one). Anything else —
`starting`/`running`, an unrecognized state, or a `status.json` that is
missing or unreadable — exits `5` with
"job still running (state: …) — `abort` first, then `rm --force`" and touches
nothing at all: no container is removed and neither directory is deleted.
Killing a live job stays explicit (`abort`, or `stop --force`).

The reflect unwind's `finishing` state (task 012, #56; a settled verdict not
yet written as a terminal state, sorted between `running` and `succeeded` in
the lifecycle order above) is neither of those cases: plain `rm`
refuses it too, but with its own wording naming the unwind —
"run is finishing (reflect unwind in progress) — wait for it to reach a
terminal state, or use `rm --force`" — checked before the generic
container-still-exists refusal, and regardless of whether a container record
exists (a crashed-mid-unwind run dir with no container left still gets the
accurate reason rather than silently becoming plain-`rm`-deletable).
`--force`'s terminal-state guard is deliberately widened to accept
`finishing`, matching the hub's `ui_server.deletion_refusal` reasoning for the
same state (task 013): the verdict is already settled, so this is exactly the
case `--force` exists to let an operator past — it costs nothing extra beyond
what `--force` already costs for a terminal run (the container is stopped via
`stop`'s own teardown sequence before the directories are deleted).

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
`status.json` records a non-terminal state (`starting`/`running`) while no
container is running for it -- either none exists at all, or one exists and
has **exited** (issue #31: liveness has three states, and the exited one is
what an engine death leaves behind). This is the same check `doctor` reports
globally as `danglingRegistryEntries` (one implementation, shared), so the
two can never disagree; the *remedy* is shared too (task 025, one string in
one place), so both commands tell **one story** for the same run:

1. `ralphctl resume <run-id>` — continue it. Resume-first is the default
   advice because the container died but the run dir (plan, notes,
   artifacts, transcripts) is intact, so the work is still there to finish.
   Per-run auto-resume (`ralphctl doctor --fix`, on unless the run was
   started with `--no-auto-resume`) automates exactly this step.
2. `ralphctl repair <run-id> --set-state aborted` — declare it over,
   recording a `reason` naming the vanished container.

`repair` prints that remedy as part of the issue text, plus a `dangling`
field (`{runId, container, liveness}` or `null`) in `--json`, where
`liveness` is `"absent"` (no such container) or `"exited"` (it is still
there, stopped) -- the issue text is worded per shape, since where to look
for the cause differs even though the remedy does not.

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
is written alongside the new state naming the container and what happened to
it -- it no longer exists (died or was removed outside `ralphctl`), or it
exists and has exited without the engine recording a terminal state -- so the
terminal state on disk explains itself; for an already-terminal run no such
reason is invented.
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
| `suggestions` | `reflection/suggestions.diff` | the prompt/skill diff the reflect phase proposes (applied only by `ralphctl suggestions <run> apply --yes`) |
| `reflect-failed` | `reflection/FAILED.md` | why the reflect phase left no report |
| `run-record` | `run-record.md` | the engine's own terminal record, human-rendered (task 006, B.1) |
| `run-record-json` | `run-record.json` | the JSON `run-record.md` renders from (task 006, B.1) |

`reflect-failed` is listed only while the file is on disk, and the engine
removes it the moment a reflect attempt succeeds (task 017, issue #43): a run
whose reflection failed, was resumed and reflected successfully the second time
lists `report` alone, and `show reflect-failed` exits non-zero — no stale
tombstone advertised beside a report that exists.

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
purpose:   the prompt/skill diff the reflect phase proposes (applied only by `ralphctl suggestions <run> apply --yes`)
size:      844 bytes
--- reflection/suggestions.diff ---
--- a/prompts/worker.md
+++ b/prompts/worker.md
@@
-...
+...
```

The same tree can also carry `run-record.md`/`run-record.json` once the run
reaches `finishing` (task 006, B.1) -- generic tree entries like the two above
until then, well-known ones (`run-record`/`run-record-json`) once written.

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

### `ralphctl router <run-id> [ls|show <index>]`

This run's own router calls (task 022, P, #80) -- the harness's per-state
fallback question, asked ONLY when a state's next legal edge is not decidable
from disk alone (`transitions.ROUTER_FALLBACK` is `{}` by design, so every
shipped run today answers with none):

```
ralphctl router <run-id>                  # same as `ls`
ralphctl router <run-id> ls                # every recorded decision: state, chosen edge, fallback used, attempts, model
ralphctl router <run-id> show 1            # one decision's full header block
```

- **Purely on-disk, like `ralphctl artifacts`**: `state.router_call_entries`
  reads `artifacts/router/decisions.jsonl`, the engine's own append-only write
  (`RunDir.record_router_decision`), so a live run and one whose container is
  long gone read identically.
- **Never a work iteration.** Every entry carries `chargesIteration: false`
  and the note `state.ROUTER_CALL_NOTE` says explicitly that a router call
  consumes no iteration and no approach and opens no `iterations/NNNN/`
  directory of its own -- `ls`/`show` print that same note, so nobody reading
  either surface mistakes a router decision for a worker attempt.
  `show`'s header also lists the candidate edges, the chosen target and
  guard, whether the declared fallback was used, attempts, model, and the
  same `tokensDisplay`/`costDisplay` vocabulary every other usage surface
  uses (`cost_bucket`).
- `--json ls` prints `{runId, routerCalls: [...]}`; `--json show <index>`
  prints `{runId, ...entry}`. Exit codes: `0` · `1` no router call at that
  index · `2` a non-numeric index · run-not-found per the shared `_require_run`
  gate.
- The listing and the header block are worded ONCE, by
  `ralphd.engine.state.format_router_calls_listing` / `router_call_summary_lines`,
  so the hub's router panel (`GET /api/runs/<id>/router`, below) and this verb
  show the very same lines -- a decision cannot be described one way on the CLI
  and another in the browser.

### `ralphctl suggestions <run-id> [show|apply] [--target DIR] [--only PATH] [--yes] [--ignore-conflicts]`

A finished run's reflect phase may leave `artifacts/reflection/suggestions.diff`:
a unified diff proposing edits to the loop's own material (`src/ralphd/prompts/`,
skills). Until v0.7 nothing could apply it — the reflect prompt forbids the
agent from doing so, and no host-side surface could either — so a wave's
self-improvement was an operator reading the diff and retyping it by hand. This
verb closes that loop **without ever writing something unseen** (task 041,
requirement P):

```
ralphctl suggestions <run-id>                    # same as `show`
ralphctl suggestions <run-id> show               # the diff + what it would touch; writes nothing
ralphctl suggestions <run-id> apply              # the same, then stops: nothing written (exit 2)
ralphctl suggestions <run-id> apply --yes        # the same, then writes, all-or-nothing
ralphctl suggestions <run-id> apply --yes --target ~/src/ralphd
ralphctl suggestions <run-id> apply --yes --only src/ralphd/prompts/worker.md   # take one file out of the diff
ralphctl suggestions <run-id> apply --yes --ignore-conflicts  # another run's diff also fits these files: this one wins
```

```
$ ralphctl suggestions brisk-otter-1408 apply --yes
run:       brisk-otter-1408
diff:      artifacts/reflection/suggestions.diff (13945 bytes)
target:    /home/you/src/ralphd
image:     5adb7924fa1b
--- the diff as proposed ---
--- a/src/ralphd/prompts/worker.md
+++ b/src/ralphd/prompts/worker.md
@@ -50,7 +50,13 @@
...
--- what it would touch ---
modify  src/ralphd/prompts/planning.md  (3 hunks)
modify  src/ralphd/prompts/worker.md  (3 hunks)
--- what was written ---
wrote   src/ralphd/prompts/planning.md
wrote   src/ralphd/prompts/worker.md
image:     5adb7924fa1b -> 63644df115a2  (the next `start` builds and runs it)
```

- **Nothing is written unseen.** Every invocation prints the diff and the exact
  list of files it would touch *before* anything is written — the write is the
  last thing that happens, after the plan the operator is looking at. `apply`
  with no `--yes` is therefore a dry run that ends in
  `nothing written -- re-run the same command with --yes to apply this plan`
  and exit `2`: an `apply` that wrote nothing is not reported as a success.
- **The target is a checkout, never a live run.** A job runs the prompts baked
  into its image, so applying a diff to the tree changes what the **next**
  `start` builds: the content hash of `container/`, `pyproject.toml` and
  `src/ralphd/` moves, and the human output shows it moving. The default target
  is the ralphd checkout this `ralphctl` runs out of; a wheel/`pipx` install has
  no checkout beside it, which is a usage error (`2`) naming `--target`, not a
  guess.
- **All-or-nothing, and a rejection names the hunk.** The matcher is
  `ralphd.cli.patch` (no `git`/`patch` subprocess, no fuzz, no renames): a diff
  whose context no longer matches is refused with one line per failing hunk —
  the file, its `@@` header and why (`no such context anywhere in the file`, `the
  change is already present in the file`, `the context appears N times, so its
  position is ambiguous`, a missing file) — exit `1`, and **not one byte** of
  even the hunks that did apply is written. `show` refuses the same way, so the
  check is scriptable with no write path involved.
- Re-running `apply --yes` on a diff that already landed is refused as `the
  change is already present in the file` rather than applied twice.
- **Never mid-flight** (requirement P part 2). An `apply` is refused, exit `1`,
  while a run that is *really running* is tied to the target tree — either its
  `host.json` `imageHash` says it is executing the image built from exactly this
  tree, or the tree is (contains, or sits inside) its mounted workspace. Both
  runs and reasons are named. The image tie is the one worth spelling out: the
  write cannot reach the live container, whose prompts are baked into its image —
  and that *is* the damage, because the checkout would silently stop describing
  the run you are watching. There is no override flag; apply to a copy
  (`--target DIR`) or wait for the run to finish, since a diff reaches a job only
  through the next `start` either way. A run recorded non-terminal whose
  container is gone does not block anything (that is `doctor`'s condition, and
  liveness is asked with the same three-valued docker question `resume` uses).
- **Two runs proposing edits to the same prompt are refused, not merged.** While
  another run's `suggestions.diff` *also still applies* to a file in this plan,
  `apply` exits `1` naming that run, its diff and the shared paths (`show` lists
  them under `--- other runs proposing edits to the same files ---`). Applying
  both would corrupt nothing — the matcher is context-exact — it would produce a
  third version of the prompt that neither run proposed, and nothing would say
  so. `--ignore-conflicts` is the explicit "this run's suggestions win"; the
  other diff is then re-planned against what was written and refused by the
  matcher rather than landing quietly on top. A sibling diff that no longer
  applies is not a conflict — it can never be written.
- **A subset is selected by file, up front** (requirement P part 3). A reflect
  diff is a batch of independent suggestions — v0.6's became eight issues, two of
  them wrong — so `--only PATH` (repeatable) takes just the files you name:

  ```
  $ ralphctl suggestions brisk-otter-1408 apply --yes --only src/ralphd/prompts/worker.md
  ...
  selected:  1 of 2 files (--only)
  --- what it would touch ---
  modify  src/ralphd/prompts/worker.md  (3 hunks)
  --- left out by --only ---
    skip  src/ralphd/prompts/planning.md  (3 hunks)
  ```

  The selection is made on the parsed diff **before the plan is built**, so
  everything downstream sees only what was selected: the plan that is printed is
  the plan that is written, still all-or-nothing, and the whole diff is still
  printed above it so nothing is hidden. What is left out is listed under `---
  left out by --only ---`, because a partial apply has to announce itself as one
  — including in `--json`, which grows `only` and `skipped` (and `paths` is then
  the selected subset). This is the opposite of writing the hunks that happened
  to apply: the tree that lands is one an operator named and was shown.
- **Hunks are not separately selectable**, and a `--only` path the diff does not
  touch is a usage error (`2`) listing the paths it does touch. A `@@` header is
  not a name you can select by — the same file's hunks renumber the moment one of
  them applies — while a path is exactly what the plan prints. To take part of
  one file: take the whole file and edit the rest back out, or apply to a scratch
  copy (`--target DIR`) and carry across what you wanted.
- Selection narrows the conflict check but never the live-run refusal: another
  run's diff that only touches a file you left out is no longer a conflict for
  *this* apply (nothing shared can be overwritten), while a run executing the
  target tree still refuses the write however few files you selected — the
  refusal is about the tree, not about which of its files move.
- **The hub does not surface this verb at all**, and that is deliberate CLI
  parity: the browser can read a run's `suggestions.diff` as an artifact
  (`GET /api/runs/<id>/artifacts/<path>`, read-only) and
  nothing more — there is no endpoint that plans, applies or selects, so the hub
  can only ever expose *less* than the CLI here. Writing the material the loop
  runs on stays an act at a terminal, with `--yes` typed by a person.
- **Almost purely on-disk**, like `ralphctl docs`/`artifacts`: the diff is a file
  in the run dir and the target is a directory on this host, so this verb never
  talks to a run's API and works identically for a run whose container is long
  gone. The one exception is the mid-flight refusal above: one `docker inspect`
  per run that is both recorded non-terminal and tied to this tree, and none at
  all otherwise.
- `--json` prints `{runId, diff, target, paths, applies, failures, imageHash,
  written, applied, only, skipped, liveRuns, conflicts}` (plus `imageHashBefore`
  after a write, and `reason` for the `apply`-without-`--yes` stop), so the plan
  — including both refusal conditions and any `--only` selection — can be
  inspected programmatically before an operator decides.
- Exit codes: `0` shown or applied · `1` no `suggestions.diff` in that run, an
  unreadable diff (the message names the offending line), a diff that does not
  apply, a live run tied to the target tree, or an unresolved conflict with
  another run's diff · `2` `apply` without `--yes`, a `--target` that is not a
  directory, a `--only` path the diff does not touch, or no checkout to default
  to · `3` run not found. Pinned by
  `tests/test_cli_suggestions_verb.py`,
  `tests/test_cli_suggestions_guards.py` and
  `tests/test_cli_suggestions_subset.py`.

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

### `ralphctl skills install [--target DIR] [--force]`

The ONE `skills` action that takes no run id (task 022, requirement R, #88):
it installs `skills/ralphd/SKILL.md` -- this repo's own guide for a coding
agent driving `ralphctl` -- onto the local machine, so a coding agent's own
skills directory can pick it up. Everything above (`ls`/`get`/`add`/`rm`)
manages a *running job's* skill inventory over the API; this one never
touches a run at all.

```
ralphctl skills install                    # auto-detect the target, copy, verify
ralphctl skills install --target DIR       # install under DIR/ralphd instead
ralphctl skills install --force            # overwrite a locally-modified install
```

- **Target auto-detection**: without `--target`, checks (in this order)
  `~/.pi/agent/skills`, then `~/.claude/skills`; the first one that already
  exists as a directory wins, then the first whose *parent* agent config dir
  (`~/.pi/agent` or `~/.claude`) exists even without a `skills/` subdirectory
  yet, and only when neither agent has ever been set up on this machine does
  it default to creating `~/.pi/agent/skills`. `--target DIR` names the
  parent directory directly (the skill lands at `DIR/ralphd`), overriding
  auto-detection entirely.
- **Copy, not symlink**, so it works the same whether this install is a
  checkout or (once packaged) a wheel, and so a later `install` can tell
  *unchanged, upgrade it* from *the operator edited it*.
- **Version stamp**: every placed copy gets a `.ralphd-skill-stamp.json`
  recording this build's `__version__` and a content hash of what was placed.
  A later `install` recomputes the target's current hash (stamp file
  excluded) and compares it to the recorded one: unchanged means "safe to
  overwrite with the current source" (this is also how an upgrade lands
  without `--force` -- only the *source* changed, not the target) --
  changed means a local edit, refused with exit `2` naming the target and
  `--force`, unless `--force` is given.
- **Verifies, rather than trusting the copy returned**: after placing, it
  re-checks the agent's own discoverability rule -- a directory containing
  `SKILL.md` -- and reports exactly that (`--json`: `{"target": ..., "verified":
  true}`; human: "installed '...' to ... -- verified discoverable (directory
  contains SKILL.md)"). A placement that somehow didn't leave a discoverable
  `SKILL.md` behind exits `1` rather than reporting success.
- **Wheel installs** (task 023): `skills/` is shipped as package data the same
  way `container/` etc. already are (`pyproject.toml`'s `force-include`,
  `cli/image.py: image.packaged_skill_dir`), so a real `pipx install ralphd`
  now has a real source instead of the checkout-only refusal task 022
  shipped: a checkout's `skills/ralphd` wins when there is one,
  `<installed ralphd>/_image/skills/ralphd` otherwise, and exit `2` only if
  somehow neither exists.
- **`ralphctl doctor`** reports whether this operator skill is installed at
  all (task 023): an `operator skill: installed` / `missing` / `stale (built
  from <old version>)` line, driven by the same install stamp -- `stale`
  means the discoverable copy's stamped version differs from this
  `ralphctl`'s own; report-only, like the job-image staleness line, so it
  never flips the overall verdict.

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
ralphctl llm test <profile>           # ping each (harness, model) pair through the runner
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
(`(unset)` when the profile has no opinion), and the line after
`price_strategy` is `billing: <payer> (<basis>)` (#298) -- the profile's
`billing:` block with its fallbacks applied, e.g. `billing: AIGW gateway
(chargeback)`; billing values are literal, never masked. `host`/`none` have no file to
resolve; `show` reports them as
built-in with nothing to redact. Exit `3` for an unknown profile name (no such
`<name>.yaml`); a profile that fails to *resolve* (unset `${env:}` var,
unreadable `${file:}`, failing `${cmd:}`) exits `1` with the same diagnostic
`start` would show. `--json` emits the full resolved (redacted) document.

`test <profile>` first resolves the profile on the host exactly like `start`
(same exit codes: `3` unknown name, `1` unresolvable reference, unredacted
since nothing is printed) -- no docker needed for this part. It resolves the
env, mounts, `pi:` fragment and headers, `harness:` and `builtin_providers:`;
for `host` it stages the host's `~/.pi/agent` files with `!command` apiKeys
resolved, as `start --llm host` does. If resolution succeeds and a docker
daemon answers `docker version`, it pings every distinct (harness, model)
pair the run would use, each through the runner an iteration uses, in a
throwaway container from the run image, as the agent uid:

```
docker run --rm -i --user 1000 --label ralphd.llm-test=<name> <resolved env/mounts> \
  --entrypoint ralphd-engine <image> --llm-ping --harness <h> [--model <m>]
```

Each pair is judged by its `IterationResult`, not the container's exit
status, so an in-band provider error fails it. The output is one line per
pair:

```
ok  pi  acme/m1
FAIL claude-code anthropic/claude-sonnet-5  refused: 400 invalid_request: ...
```

The failure line carries the fault class (`refused`, `rate_limit`, `infra`,
...; `no-verdict` when the container printed none) and the scrubbed provider
message. When the runner exits non-zero without an error of its own, the
message is `the runner exited N: <tail>`: the tail is the last of the child's
stderr (the ping transcript's non-JSON lines and the claude-code wrapper's
`stderr.log`), whitespace collapsed, scrubbed of every known secret value and
kept to its last 300 characters. A `getaddrinfo ENOTFOUND` exit stays `infra`
and now names the resolver error in the line. A `claude-code` provider `404` whose
error is `model_not_found` is `refused`, as `not_found_error` is; a pi gateway
`404 model_not_found` stays `infra`, as in a run. `--json` prints a list with one verdict per pair (`profile`,
`image`, `harness`, `model`, `ok`, `faultClass`, `reason`, `message`,
`exitCode`). The exit status is `1` if any pair fails, else `0`.

| Option | Default | Meaning |
|--------|---------|---------|
| `--image <ref>` | the run image | image to ping in; by default the one `start` would run (template, registry, `RALPHD_IMAGE`, else the content-hashed image, built on a cache miss) |
| `--model <ref>` | the profile's `model` | model to ping; with `--hats`, the job's base `model` |
| `--hats` | off | ping every hat's (harness, model) pair and every configured `model_tiers` leaf, merged as `start` merges them: registry `config.yaml` < `--template` < the flags below |
| `--template <name>` | none | the template whose `job.yaml` `model:`/`models:`/`model_tiers:`/`harnesses:` (and image keys) `start --template` would use |
| `--network <net>` | `--template` `job.yaml` `network:`, else the registry's `network`, else none | docker network for the ping container, resolved as `start` resolves it; every ping, including each `--hats` ping, gets `--network <net>`. None means no `--network` argument (the default bridge). Pass `host` for a gateway that resolves only on the host (a tailnet or VPN name) |
| `--model-for HAT=REF` | none | as on `start`; repeatable; needs `--hats` |
| `--model-for-tier HAT:TIER=REF` | none | as on `start`; repeatable; needs `--hats` |
| `--harness-for HAT=NAME` | none | as on `start`; repeatable; needs `--hats` |
| `--no-ping` | off | resolve only; skip the container ping even when docker answers |

With `--hats`, the merged job also goes through the route check `start` runs
before iteration 1, before the ping and with or without `--no-ping`: a
`claude-code` hat whose model or tier reference is not `anthropic/*` is
refused with `start`'s message and exit `1`, and no container runs
(`hat 'planning' runs on the claude-code harness but is configured with model
'X', whose provider 'Y' is not 'anthropic'; ...`). Only that rule applies:
the pi provider-key rule reads the staged `~/.pi/agent/models.json`, which
`llm test` stages only when it pings. Without `--hats` there is no route
check.

When docker isn't reachable (or `--no-ping` is given), the ping is skipped
and resolution success alone is reported (exit `0`).

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
  same client-side token keeps working against the new container. Read from
  `<control-creds-dir>` (task 003, requirement A, #201 -- host-only, never
  bind-mounted into any container; self-healed off `<run-dir>/.api-token`
  on the first resume of a run a pre-task-003 build started).
- **The recorded `.control-token` (task 004, #62 candidate 1, requirement
  A), unconditionally** -- unlike `.api-token`, this one exists on every
  run `start` created after task 004 landed, whether or not `--api-token`
  was ever passed, because it is what gates the mutating half of the API
  (`/abort`, `/pause`, `/resume`, `/retry`, `/shutdown`,
  `PATCH /config/budget`, every `PUT`/`DELETE` under `/config`) closed to
  an iteration by default -- see `docs/api.md`'s "container boundary"
  section. Read from the same `<control-creds-dir>` as `.api-token`
  (task 003). A run started before task 004 (no `.control-token` anywhere)
  resumes exactly as it did before: the mutating gate stays open, the same
  as `.api-token`'s own absent-file behaviour, never retrofitted.
- The `--llm` wiring resolved at `start` time (task 058, operator steering
  018): whichever env vars and extra mounts `start` added on top of the
  base wiring above — `--llm host`'s forwarded `HOST_LLM_ENV` vars
  (`ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`, ...) and its `~/.aws` mount if
  present, or a named profile's fully-resolved `env:`/`mounts:` (including
  anything that came from a `${env:}`/`${file:}`/`${cmd:}` reference) --
  are persisted at `start` time to `<config-dir>/llm-wiring.json` (mode
  `0600`, never under the run dir proper, never returned by any HTTP
  route -- the same at-rest pattern already used for
  `<config-dir>/pi/models.json` and `<control-creds-dir>/.api-token`) and
  reproduced byte-for-byte by `resume`,
  regardless of what the operator's *current* shell has (or lacks) at
  resume time. A run started before this existed (no `llm-wiring.json` on
  disk) resumes exactly as it did before — no error, just nothing extra to
  reproduce.
- The resolved `--llm-header` pairs (task 045, #177): since `resume` never
  recreates `<config-dir>/pi/models.json`, a header merged in at `start`
  reproduces for free just by the config dir surviving — nothing extra to
  do with no flag. `resume --llm-header NAME:VALUE` (repeatable, same
  spec shape as `start`'s own flag, `${env:}`/`${file:}`/`${cmd:}` resolved
  on THIS resuming host) OVERRIDES: it merges over whichever headers were
  persisted (a same-named header wins, any other persisted header is
  kept), re-merges the result into `pi/models.json`, and re-persists the
  new effective map to `llm-wiring.json`'s `headers` field — so a LATER
  resume with no flag replays the overridden value, not the original
  `start`'s.
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

`--iterations +10` adds 10 to the *effective* budget -- `max(status.json's
iterationsBudget, job.yaml's iterations)`, the same number the engine's own
startup seeding (task 010, #51) would pick -- before the container starts (a
bare integer, e.g. `--iterations 30`, sets it absolutely instead); omit it to
just continue with whatever budget remains (a plain resume already carries a
live `ralphctl budget` top-up forward without this flag).

Task 004 (#94 (c)+(d), operator-added scope) adds three more `+N|N` flags,
each independent and optional, written straight into job.yaml *before* the
container starts, right next to `--iterations`'s write above:

```console
$ ralphctl resume my-run --approaches +1            # job.yaml max_approaches += 1
$ ralphctl resume my-run --timeout +3600            # job.yaml job_timeout_s += 3600
$ ralphctl resume my-run --iteration-timeout 900    # job.yaml iteration_timeout_s = 900
```

`--approaches` writes `max_approaches`, `--timeout` writes `job_timeout_s`,
`--iteration-timeout` writes `iteration_timeout_s` -- the base a `+N` tops
up is job.yaml's OWN current value for that key (never a live-engine
value: unlike `iterations`, there is no status.json seeding for these
three, by design -- see the budget table below).

**None of the three persists from a live container the way `iterations`
does (#94 (d)):** a live `ralphctl budget --approaches`/`--timeout`/
`--iteration-timeout` change only ever lives in that container's own
process (`PATCH /config/budget`'s `maxApproaches`/`iterationTimeoutS`/
`jobDeadlineEpochS` are process-only, never seeded from status.json), so a
PLAIN `resume` with none of these three flags REVERTS every one of them to
whatever job.yaml already said -- this run's `start`-time value, or an
earlier `resume --approaches`/`--timeout`/`--iteration-timeout` that itself
passed a flag. Carrying a live change forward needs passing the same flag
(with whatever value you want to keep) on the resume that follows it. See
`ralphctl budget`'s section above for the full per-knob table.

**Docker access (#297).** With neither flag, `resume` re-applies the docker
access recorded for the run in `<registry>/configs/<run>/docker-access.json`
(written by `start`, and by any resume that passed a flag): the socket is
re-validated, the ROOT-EQUIVALENT warning is printed again, and a one-line
`docker access: on (recorded at start; ...)` notice says where it came from.
Precedence is explicit flag > record > off:

| flag | effect | recorded |
|---|---|---|
| (none) | the recorded value; **off** for a run with no record (one started before 0.32.2) | no |
| `--allow-docker` | mount the socket | yes, `setBy: resume` |
| `--no-allow-docker` | no socket, no `RALPHD_HOST_*` env | yes, so later bare resumes keep it dropped |

The two flags are mutually exclusive (exit `2`). When the record says on but
the socket is missing or not a socket, `resume` **refuses** with exit `2`
before it changes anything (no container removed, no `job.yaml` top-up
written) and names the record and `--no-allow-docker` — it never silently
drops the socket. A record that exists but cannot be read (truncated, not
JSON) is refused the same way, naming the file: it may have been a grant, so
it is never read as off — pass `--allow-docker` or `--no-allow-docker` to
decide, and that flag rewrites the record. A `doctor --fix` auto-resume replays the record the same way
and reports such a refusal's reason in `autoResume.failed[].error`. The record
lives in the config dir, never in the run dir's `host.json`: the run dir is
writable from inside the container, so the agent cannot grant itself the
socket for its next resume.

`--image`, `--port`, `--api-bind`, `--network` (defaults
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
startup. A bare `--forward-env`/`--llm-env`/`--env` from the *original*
`start` is replayed too, from `env-wiring.json` (the bullet above) — and it
has to be, since `resume` has no such flags of its own to pass those values
again with.

Exit codes: `3` unknown run, `5` the run's container is still alive (a live
engine already holds the run dir's flock; `abort`/`stop` it first), `2` a
malformed `--iterations` value, both docker flags, or recorded/requested docker
access whose socket is missing or not a socket, `1` the underlying
`docker run` failed.

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
`start --price-strategy`), and the two keys that decide which models a job
runs (#52): `model` (any pi `provider/model-id` string, used by any hat with
no per-hat override) and `thinking` (any string `pi --thinking` accepts).
`model_strategy`/`fast_model` are retired (task 006): `config set` refuses
both with exit `2` naming the job.yaml `models:`/`model_overrides:`
replacement, never a silent no-op.

The per-hat table itself (task 008) is **not** a `config set` key -- it is a
table, not a scalar, so (like `pricing:` below) it is edited directly as a
nested `models:` (or `model_overrides:`) mapping in `<registry>/config.yaml`:

```yaml
models:
  planning: amazon-bedrock/eu.anthropic.claude-opus-5
  worker:   amazon-bedrock/eu.anthropic.claude-sonnet-5
```

`ralphctl start` merges this in as the registry-wide fallback for `--model-for`
(see above): explicit `--model-for` flag > `--template`'s own `models:` key >
this registry `models:` key, merged key by key rather than replaced whole.

For `model` and `price_strategy` a **named LLM profile** can
state a value too (see [llm-profiles.md](llm-profiles.md)); a registry-wide
value beats it: a profile answers *where do I route and with what
credentials*.

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
`--price-strategy`, `--model`,
`--thinking`)
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

#### Per-hat model table (`models:`/`model_overrides:`, task 008)

Unlike the scalar keys above, the per-hat table is not one flag/`config
set`/hardcoded-default ladder: it is a table that **merges key by key**
across three levels (a hat one level names and a lower level does not still
survives) rather than one level replacing the last outright --
`--model-for HAT=REF` (`start`, repeatable) > a `--template`'s own `job.yaml`
`models:`/`model_overrides:` key > `<registry>/config.yaml`'s own `models:`/
`model_overrides:` key (edited directly, like `pricing:` below -- there is
no scalar `config set` for a table) > absent, i.e. each hat falls through to
`model_for`'s other rungs (the bare `model` field, then pi's own default).
See `--model-for` above and "`ralphctl config get/set`" above for both
settable levels.

#### Per-hat harness (`harnesses:`)

Each hat runs on one agent harness, `pi` (the default) or `claude-code`. A hat's
tiers share its harness. `start` resolves the table in this order, weakest to
strongest, merging per hat: the LLM profile's `harness:` (every hat), then
`<registry>/config.yaml`'s `harnesses:`, then the `--template`'s `harnesses:`,
then each `--harness-for HAT=NAME`. The result, with all nine hats, is written
to `job.yaml`; `ralphctl docs <run> job.yaml` shows it unmasked.

```yaml
# job.yaml (or --harness-for HAT=NAME / a --template / <registry>/config.yaml)
harnesses:
  worker: claude-code
  review: pi
```

`ralphctl status` and `status --json` name each hat's harness at the end of
its provisioning line (the hub shows it in the `harness` column of its
provisioning table), for example `worker    anthropic/claude-sonnet-5  [claude-code]`;
each tier rung line (`worker.hard`, ...) carries the same `[harness]` suffix,
the hat's, since a tier has no harness of its own.
A `claude-code` hat must use an `anthropic/*` model ref (or none); the engine
refuses any other ref at start with exit `5`, naming the hat, the tier, the ref
and the harness.

#### Per-(hat, tier) override table (`model_tiers:`, task 002)

One axis over from the per-hat table: `model_tiers:` names one bundle per
`(hat, tier)` rung instead of one bundle per hat, so a `worker` task escalated
to `hard` can provision a different model than that same hat's `trivial`/
`normal` rungs -- see [SPEC.md](../SPEC.md) §4.7 for the resolver
(`tiers.resolve_bundle`) this table feeds. Same merge discipline as `models:`,
one level over: `--model-for-tier HAT:TIER=REF` (`start`, repeatable) > a
`--template`'s own `job.yaml` `model_tiers:` key > `<registry>/config.yaml`'s
own `model_tiers:` key (edited directly, like `pricing:` below) > absent,
i.e. that rung falls through to `models:`/`model_overrides:`'s plain per-hat
answer, never to some other tier's rung. A `job.yaml` naming only `model`/
`models:` needs no `model_tiers:` entry at all -- every rung of every hat's
ladder collapses to that one bundle, and `ralphctl status`/`status --json`
render the collapsed single-rung row exactly as they did before this table
existed; a hat with at least one configured rung instead renders one line per
tier, labelled `<hat>.<tier>` (`state.format_provisioning_lines`, the one
formatter `ralphctl status`/`status --json` call; the hub renders the same
rows as a table with `trivial`/`normal`/`hard` columns). A leaf
is either a plain model ref string or a mapping naming `model`/`thinking`/
`effort` explicitly, the same shape family `thinking_overrides:` speaks:

```yaml
# job.yaml (or --model-for-tier HAT:TIER=REF / a --template / <registry>/config.yaml)
model_tiers:
  worker:
    hard: amazon-bedrock/eu.anthropic.claude-opus-5   # escalated rung only
  verify:
    trivial: {model: aigw-openai/openai.gpt-5.6-sol, effort: low}
    hard:    {model: aigw-openai/openai.gpt-5.6-terra, effort: high}
```

See `--model-for-tier` above and "`ralphctl config get/set`" above for both
settable levels.

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
undeclared zero is treated as *unknown* (`costZeroQuoted`, see `docs/api.md`). A declared-free route keeps
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
- A few gateway-routed ids that pi-ai's Bedrock data does not list yet
  (`openai.gpt-6-sol`, `openai.gpt-6-luna`) are priced from a short hand-kept
  `GATEWAY_RATES` table beside the generated one, at OpenAI list price × 1.1
  (the markup the generated `openai.gpt-6-astra` rate carries). A generated
  entry for the same id wins, so a refresh that starts listing one retires it.

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
one that quotes an implausible `$0` beside billable tokens (the live AIGW case). A route declared free in `pricing:`
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

#### Who pays (`billing:`, #298)

`ralphctl cost`, `ralphctl status` and the hub group a run's cost **by payer**
when its `job.yaml` carries a `billing` block. `start` writes that block from,
for each of payer and basis, the first of: `--billing-payer`/`--billing-basis`;
the `--llm` profile's own `billing:` (see `docs/llm-profiles.md`); the
fallback (the profile name, `metered`). For `--llm host` the middle rung is a
`billing:` mapping in `<registry>/config.yaml`, edited directly like
`pricing:` above (there is no `config set` key for it):

```yaml
billing:
  payer: Home subscription          # payer for --llm host [default: host models.json]
  basis: subscription               # metered|subscription|chargeback|free [default: metered]
  subscription_payer: Claude Max    # label for claude-code hats under a non-claude-code
                                    # profile or host [default: claude-code subscription]
```

Set `basis: subscription` when the host's `models.json` logs in with a
subscription OAuth, so its traffic reports `$0` with a list-equivalent. The
block is validated at every `start`, whatever `--llm` says; an unknown key or
basis, or a blank label, exits `2`. `subscription_payer` applies to every
named profile too: a `claude-code` hat always bills the subscription, so a
`claude-code` hat in a run on a gateway profile is stamped with this label.
`--llm none` writes no `billing` and is never grouped; giving
`--billing-payer`/`--billing-basis` with it exits `2`. Like `pricing`, the
block is inlined into `job.yaml`, so `resume` replays it and a later profile
edit never rewrites a run's history; it is never written to
`llm-wiring.json`. A run started before 0.32.1 has no block and keeps its old
layout, including after a resume on 0.32.1.

### `ralphctl doctor`

Preflight checks (`checks` in `--json` output; overall `ok` is the AND of all
of them, exit code `0`/`1` accordingly):

- `docker` — the docker daemon is reachable.
- `--prd <file>` (optional) — also prints the same `rubric: ...` line
  `start` prints (task 005, PRD requirement A.3), resolved against the
  current directory, for a PRD file this checkout can read; silently
  omitted when `--prd` is absent or unreadable. Warns on stderr on a
  referenced rubric path that does not resolve, exactly like `start`.
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

- `claude_code_runtime` — the job image carries the Claude Agent SDK and a
  Claude Code CLI at or above `CLAUDE_CODE_MIN_VERSION` (`2.1.280`). `doctor`
  runs `python -m ralphd.harness.claude_code --probe-version` in the image
  (`docker run --rm --entrypoint python <image>`; `python` is the engine's
  interpreter in both the base and the derived image) and compares the CLI
  version it reports. An SDK or wrapper import failure, or a CLI below the
  minimum, fails the check and the report line names the minimum. When the
  image cannot be probed (docker down, image absent, a derived supply with no
  single tag, an unparseable answer) the check is report-only: the line
  `! claude_code_runtime: not probed (<why>)` appears and `checks` carries no
  `claude_code_runtime` key.

Two dangling-container checks, in both directions — always **non-fatal**
(report-only; never affect `ok`/the exit code):

- `strayContainers` — containers labeled `ralphd.run=<id>` with no matching
  run dir at all (leftovers from `--allow-docker` jobs that were never
  reaped, or a manually deleted run dir).
- `danglingRegistryEntries` — the reverse: a run dir whose `status.json`
  records a non-terminal state (`starting`/`running`) while no container is
  running for it — either none exists at all (killed or `docker rm`'d outside
  `ralphctl`) or one exists that has **exited** (issue #31).
  Reported as `{runId, container, liveness}` (`liveness`: `"absent"` or
  `"exited"`); the human report prints the **shared
  remedy line** described under `repair` below (task 025) — resume first,
  `repair --set-state aborted` as the alternative — naming the actual run
  id, so `doctor` and `repair` can never recommend different next commands
  for the same run. The same check (one implementation) backs `repair`'s
  per-run dangling-container diagnosis.

  ```
  ! registry entries recorded running with no live container:
      myrun  container=ralphd-myrun (absent)
        the container died or was removed outside ralphctl; continue it with `ralphctl resume myrun`, or record it as over with `ralphctl repair myrun --set-state aborted` (writes a reason naming the vanished container)
  ```

  (one line per entry; wrapped here for the page width)

#### Operator skill (task 023, requirement R)

Also always report-only (never affects `ok`/the exit code): whether
`skills/ralphd` (the shipped operator skill `ralphctl skills install` places)
is actually on this machine, checked the same two agent layouts `install`
auto-detects (pi first) via the same discoverability rule `install` verifies
its own copy against.

```
  operator skill: installed
! operator skill: stale (built from 0.12.0)
  operator skill: missing
```

`--json`'s `operatorSkill` is `{"status": ..., "path": ..., "installedVersion": ...}`:

| `status` | meaning |
| --- | --- |
| `installed` | discoverable, and either its `.ralphd-skill-stamp.json` names this `ralphctl`'s own `__version__`, or there is no stamp at all (placed some other way -- there is then no version claim to call stale) |
| `stale` | discoverable, and its stamp names an **older** version than this `ralphctl` -- `installedVersion` in `--json`, `(built from <version>)` in the human line |
| `missing` | not discoverable at either agent layout |

Marked `!` only for `stale` (news to act on, matching the job-image line's
rule) -- `missing` is not a fault, since not every host running `ralphctl`
also drives a coding agent through it.

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

#### Notifier-URL configuration (task 011, PRD requirement B.3, #197)

The `notify` hat tool is wired for every run — both hats carry it
unconditionally — so a run with no `notifier_url` configured (no
`--notifier-url` at `start`, no `job.yaml` `notifier_url:` key, no
`RALPHD_NOTIFIER_URL`) is one whose prompts can still reach the tool while
every call it makes is a guaranteed no-op (`hat_tools.send_notification`'s
own "no notifier URL configured" branch, which answers `ok=False` and emits
`notification.failed` rather than raising). The engine itself warns about
this exactly once, at startup, into that run's own `events.jsonl`; `doctor`
mirrors the same fact registry-wide, report-only, one entry per **run
recorded non-terminal**, exactly like `runImageStaleness` above it:

```
! runs recorded non-terminal with no notifier URL configured (`notify` calls are no-ops -- `ralphctl start --notifier-url`, the job.yaml `notifier_url` key, or RALPHD_NOTIFIER_URL):
    myrun
```

`--json`'s `unconfiguredNotifierRuns` is the plain list of those run ids.
Each run is judged on **its own persisted start-time state**, never on the
shell `doctor` runs in: the run's `job.yaml` `notifier_url:` key, or a
`RALPHD_NOTIFIER_URL=<value>` pair in the `--env`/`--forward-env` wiring
`start` recorded for that run (which is what actually reaches its engine
process as an environment variable). So exporting `RALPHD_NOTIFIER_URL` in
your own shell before typing `ralphctl doctor` changes nothing about what is
reported — it configured your shell, not any run. Never affects `ok`: an
operator who never wanted notifications configured is not broken.

#### `ralphctl doctor --fix` (self-recovery sweep)

`--fix` turns the `danglingRegistryEntries` report into an action: every run
recorded non-terminal whose container has vanished **and** which is opted in
to `auto_resume` (the **default since v0.7**, or explicitly via `start
--auto-resume`, a template's `auto_resume: true`, or `ralphctl config set
auto_resume true`) is resumed through
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
  dir as `operator-termination.json` (`{action, at, reason, source, class}`; the
  engine writes the same file the moment `POST /abort` arrives, so an abort
  whose container dies before it can write a terminal state is still marked).
  A run carrying that marker with `class: "operator"` is reported under
  `operatorTerminated` with `not auto-resumed: terminated by the operator …`
  and is never restarted, even with `auto_resume` on. `ralphctl stop --force`
  is the sharp case it exists for: it removes the container while `status.json`
  may still say `running`, which on disk is otherwise indistinguishable from a
  crash.

**But a self-inflicted kill IS resumed** (task 015, #46). A `SIGTERM`/`SIGINT`
that reaches the engine with no `POST /abort` behind it is recorded with
`class: "self-inflicted"` instead, and the refusal above asks the class, not
merely whether the file exists. The case this is for: an iteration that
`pkill`s its own supervisor, or takes down the job container from a sibling it
started -- an accident, and precisely what self-recovery should recover from,
whereas the pre-v0.7 reading stranded such a run forever. A marker with no
`class` field (written before v0.7) reads as `operator`, so an old run dir can
only ever be treated more conservatively, never less. Two consequences worth
knowing: the crash-loop guard below is what stops a run that kills itself every
time from looping, and **a raw `docker stop` of a job container** (bypassing
`ralphctl stop`, which posts `/abort` first) is indistinguishable from a
self-inflicted signal and will therefore be resumed -- use `ralphctl stop` if you
mean it to stay stopped.

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

### `ralphctl ui [--port N] [--bind ADDR] [--allow-non-loopback-bind]`

Starts the local web hub server (PRD reqs 21-22) in the foreground: a
stdlib-only HTTP server (`http.server`/`urllib`; no `fastapi`/`uvicorn` on
this path, even though those are dependencies of the engine side of this
same package) that reads `~/.ralphd/runs/*` and proxies each run's *live*
container API when reachable. `--port` defaults to a free ephemeral port;
`--bind` defaults to `127.0.0.1`. Ctrl-C to stop.

**The hub has no authentication of its own** (PRD requirement H, operator
ruling 4 -- no hub-level auth token, no new secret) and it publishes six
mutating routes: `DELETE /api/runs/<id>` (deletes a run), `POST
/api/runs/<id>/steer` (steers a run), `POST /api/runs/<id>/retry` (retries
a degraded run now), (task 013, v0.23 requirement A.2, #224) `POST
/api/runs/<id>/rule` (records an operator ruling) and (task 006) `POST
/api/runs/<id>/unpause` (unpauses a run -- the SAME `/resume` route
`ralphctl unpause` calls, so there is no second unpause mechanism) and
(#292) `POST /api/runs/<id>/tasks/<task>/undefer` (releases a deferred
task now -- the SAME route `ralphctl undefer` calls).
`--bind` accepting any
address with no warning would let `--bind 0.0.0.0` publish all of those to
the LAN silently, so **`ralphctl ui` refuses a non-loopback `--bind`**
(anything outside `127.0.0.0/8`/`::1`/`localhost`) **unless
`--allow-non-loopback-bind` is also passed** -- the refusal names the
address and the flag and exits non-zero without starting the server. A
loopback bind is unaffected and needs no flag. On every successful start
-- loopback, or non-loopback with the opt-in flag -- **the startup line
names every mutating route and states that the hub has no authentication
of its own**, e.g.:

```
ralphctl: serving hub at http://127.0.0.1:PORT (registry: ~/.ralphd) --
mutating routes published: DELETE /api/runs/<id> (delete a run), POST
/api/runs/<id>/steer (steer a run), POST /api/runs/<id>/retry (retry a
degraded run now), POST /api/runs/<id>/rule (record an operator ruling),
POST /api/runs/<id>/unpause (unpause a run), POST
/api/runs/<id>/tasks/<task>/undefer (release a deferred task now) --
the hub has no authentication of its own
```

**Behind a reverse proxy, under a path prefix.** The hub can be mounted at a
sub-path of another origin — `tailscale serve --bg --set-path=/ralph
http://127.0.0.1:8420` publishes it as `https://<host>.<tailnet>.ts.net/ralph`
— and needs no configuration to do it. Such a proxy strips the prefix before
proxying (the server sees `/api/runs` for a request to `/ralph/api/runs`) and
forwards no header naming it, so nothing server-side could know the prefix
even in principle: `index.html` therefore loads its two assets relatively, and
`app.js` recovers the prefix from `document.currentScript.src` (`HUB_MOUNT`)
and prepends it to every request it makes. At the root the prefix is `""` and
every request is byte-identical to an unproxied one.

One constraint the client cannot lift: **the URL must end in a slash.**
`https://host/ralph/` works; `https://host/ralph` makes the browser resolve
`app.js` and `style.css` against `/`, so neither loads and there is no client
left to correct anything. Link the trailing-slash form.
`tests/test_hub_path_prefix.py` holds both halves, and drives a real Chromium
against a real prefix-stripping proxy.

Every response — the static bundle, every JSON route below and the raw
artifact byte route — carries a `Content-Security-Policy` header (task 024,
issue #54; constant `ui_server.HUB_CSP`), sent from the two shared response
helpers (`_send_json`/`_send_bytes`) so no route can forget it:
`default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self';
connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors
'none'`. No `'unsafe-inline'`/`'unsafe-eval'` anywhere — the static bundle
has no inline `<script>`, no `onclick="..."` attribute and no inline
`style="..."` attribute or `<style>` block, so the tight policy costs the
hub nothing. The point is the same one `X-Content-Type-Options: nosniff`
serves on the raw artifact route below: a file an agent inside the job
container wrote (an artifact, a note, a PRD, a reflection report) is served
back to a browser at the operator's own origin, and this is the barrier
that stops one of those files turning into script that acts against that
origin.

JSON endpoints served under `/api/`:

- `GET /api/version` — the hub's OWN package version (task 033, requirement
  G.1): `{"version": "<ralphd.__version__>"}`. Read straight from the
  `ralphd` package the hub process is running, never a literal duplicated
  into the route or the static bundle — `app.js` fetches this once on page
  load and renders it in the page header (`#hub-version`, beside the clock).
  This is a DIFFERENT number from `Handler.server_version` (`"ralphctl-ui/1"`,
  the wire-protocol tag sent on every response's `Server` header, unchanged
  by this task): one says which package is running the hub, the other says
  which wire shape it speaks.
- `GET /api/runs` — run list (PRD req 21): `{"runs": [{runId, state,
  verdict, phase, approach, maxApproaches, approachDisplay, iterationsUsed,
  iterationsBudget, startedAt, containerGone, tasksTotal, tasksCompleted,
  tasksInProgress, tasksValidationFailed, tasksFailed,
  tasksFailedValidationExhausted, tasksFailedRequirementUnmet, tasksDisplay,
  tasksSummary, tasksTrouble, tasksColumn, tasksStale, tasksSource, deletable,
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
  `tasksValidationFailed` are the raw counts (what the hub sorts on), joined by
  task 025's `tasksFailed`/`tasksFailedValidationExhausted`/
  `tasksFailedRequirementUnmet` (issue #33: the two meanings of `failed`, raw,
  with the kind counts summing to `tasksFailed`);
  `tasksDisplay` is the rendered `5/7`, `tasksSummary` is the same sentence
  `ralphctl status` prints (`5/7 completed (1 in-progress, 1
  validation-failed)`) and `tasksTrouble` is the list of trouble flags
  (`["1 failed (requirement-unmet)", "1 validation-failed", "1 in-progress"]`)
  worded by that same renderer
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
  deleteRefusal, status, tasks, iterations, parkedQuestion}`. `status`/`tasks` are proxied live from the run's
  container API (`GET /status`/`GET /tasks`) when its `apiUrl` (recorded in
  `host.json`) answers; otherwise falls back to the on-disk
  `status.json`/`tasks.json` snapshot with `live: false` — a dead run never
  produces an error, just stale-but-valid data. `containerGone` is the same
  condition as in the run list, decided here by the real proxy call rather
  than a port probe. `iterations` is always read from disk
  (`iterations/*/meta.json`). `404` for an unknown run id. Task 007 (v0.26,
  #269) adds `parkedQuestion` — `null`/absent unless the (possibly live-
  proxied) `status.pauseReason` is `operator_input_needed` or
  `rejected_repeated_signature`, the two pause reasons a ruling can actually
  answer; built entirely on disk (`ralphd.engine.state.parked_question`, off
  the most recent matching `operator_escalation` event NOT YET ANSWERED by a
  later resume with no fresher matching escalation since, in `events.jsonl`
  plus the on-disk structured findings sidecar), never a second live-only
  path -- that answered-since check (cure task cure-01-02-4) is what keeps
  a later task-boundary commit-secret-scan hold, which persists the SAME
  `operator_input_needed` reason string with no fresh `operator_escalation`
  of its own, from resurrecting an already-answered gate's stale question.
  Carries `reason`, `approach`/`maxApproaches`/`approachOfMax`, `parkedAt`,
  `replanAvailable` (present only for `operator_input_needed`),
  `blockingCount`/`residualCount`, one `findings` entry per finding —
  every blocking finding (`id: "F<n>"`, the escalation's own numbering)
  followed by every residual finding off the sidecar (`id: "R<n>"`), each
  `{id, severity, clause, evidence, cured_when, invalidates}` with the whole
  evidence — and
  `consequence` — `taskIdsValidatedToPending`, `insertCount`,
  `costSpentThisApproach`, `tokensSpentThisApproach`, and
  `nextStepIfUnanswered` (`"cure_pass"` \| `"archive_and_replan"` \|
  `"finishing"`, docs/api.md §`operator_escalation`) naming which of the
  three the pause is about to take if it is never answered — `"finishing"`
  (cure task cure-01-05, PRD v0.27 requirement C.4, DoD 3) only when this
  approach is also the run's last (`replanAvailable: false`): the pause
  archives this approach either way, but ends the run instead of
  replanning a fresh one. Task 011 (v0.27 requirement C.4, #275), extended
  by cure task cure-01-05 and widened once more by task 003 (this wave):
  the preview itself always names that SAME edge, `insertCount: 0`/
  `taskIdsValidatedToPending: []` whenever a cure pass would otherwise
  show but no cure pass will actually run — the run-wide `cure_pass_
  ceiling` is spent, (cure-01-05) this approach's own no-progress
  comparison finds the rejection re-files a previously targeted finding
  with nothing new to reopen, OR (task 003) the findings were never
  curable at all (mixed, all-incurable, a criteria-tamper finding) — the
  zeroing now applies to EVERY shape that earns no cure pass, never only
  a rejection that would be curable if not for the ceiling or the lack of
  progress. The run-detail `ETag` below moves whenever
  `events.jsonl` gains a new escalation, so a fresh one is never served under
  an unchanged tag. Task 008 (v0.26, #269) renders this field as a full-width
  **parked panel** at the top of `#/run/<id>` -- every field above, plus each
  finding expandable to its evidence/`cured_when`/`invalidates` (the evidence
  scrolls rather than truncating) -- absent when `parkedQuestion` is
  `null`/absent, with the Steer/Rule sections staying their own sections
  below it. cure-01-01-5 places the findings list in a LEFT column and the
  answer box/actions in a RIGHT column at desktop widths (stacked
  vertically, findings first, on a narrower viewport), and the poll loop
  never rebuilds the panel over an UNRELATED move of the run-detail `ETag`
  (an unrelated event, another iteration's write) -- only when
  `parkedQuestion` itself actually differs, so an operator's in-progress
  draft name/ruling text survives an unrelated refresh untouched, while a
  genuinely new question (a re-park, or the pause getting
  answered/resumed) always gets a fresh, empty box. The panel carries its
  own answer box beside the findings: an
  editable ruling textarea (`#parked-rule-text`, where `Enter` is a
  newline and each finding's waive checkbox adds or removes only its own
  line -- located by its own line boundaries, so a waive line's text
  embedded inside an unrelated operator sentence is never mistaken for
  it -- sending nothing, byte-identical on a check-then-uncheck round trip
  whatever the prose's own trailing-newline shape, including a hand edit
  typed directly into the textarea between two toggles (cure-01-01-6),
  and checking several findings then unchecking them in either order,
  which always restores the exact original bytes once every composed
  waiver is gone and keeps every still-active one on its own removable
  line throughout (cure-01-01-7)), a name field (`#parked-rule-name`) and both
  actions -- *Submit ruling and resume* posts the name and the textarea
  body byte-identical to `POST /api/runs/<id>/rule`, *Resume without a
  ruling* posts `unpause`; the runs list's `\u23F8 parked for input` marker links straight
  to it. See SPEC.md §11.3 for the full shape. Task 048 (issue
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
  means "nothing to warn about", never "an old hub". Task 025 (#33) adds
  `tasks.taskFailureKinds` — `{task id: kind}` for the failed tasks of that
  plan — re-derived here from the payload's own task list by the engine's
  `task_failure_kinds`, whichever side served it, so a live pre-v0.7 engine's
  answer and a forged key in `tasks.json` are both replaced by the derived
  truth (and the key is absent when nothing failed, exactly as `GET /tasks`
  omits it). Task 008 (#16) adds
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
  the last `N` *rendered* lines (matching the `ralphctl logs` non-follow
  tail contract: `N` means N rendered lines, not N raw events, same as the
  `-N`/`--tail N` syntax below), rendered by the exact same
  `ralphd.cli.log_render` renderer `ralphctl logs` uses (plain text, no
  ANSI — `tty=False`). This is a **bounded indexed read** whenever a usable
  index covers it. The hub first reads the run's own per-iteration render
  indexes (`render_index.json`, via `ralphd.render_index.bounded_tail`)
  straight off disk, walking backward across as many earlier indexed
  iterations as `N` needs; the rendering and walk-back work is proportional
  to `N`, not to the run's history or to silent transcript, but each
  per-iteration `render_index.json` is read whole (`load_index` validates
  the whole document before trusting any of it), so the index-read cost is
  proportional to the number of raw transcript lines before the tail in
  every iteration the walk touches, silent lines included, at about 20
  bytes per raw line — not to `N` alone. A cheap `GET /logs?tail=1` probe of
  the live container API then only decides the `live` flag. When no usable
  index covers the request — a cold run with no index yet, or an index the
  witness refuses — an uncapped **correctness scan** takes over instead: it
  asks the live API for a bounded `GET /logs?tail=<bound>` (the on-disk
  merge with the same bound when the API is unreachable), re-asking with a
  growing bound until enough rendered lines exist. This fallback has no byte
  cap and, unlike the indexed path, can end up fetching and rendering the
  *entire* available transcript even for a small positive `tail` — the same
  outcome `tail=0` always produces — when enough of the transcript is silent
  or unindexable that no smaller bound yields the requested rendered-line
  count; the render is still exact either way. It caches an index it builds
  in the hub's own memory (never in the run dir) whenever that scan was
  expensive, so a second request against the same iteration with an
  unchanged transcript identity (the same inode, size, `mtime` and `ctime`)
  takes the bounded indexed read instead of repeating the scan; a same-size
  rewrite or replacement keeps the size but changes that identity, so it
  invalidates that iteration's entry and is rescanned. This
  cache is kept per iteration, not just for the newest one: when a tail
  spans several such cold iterations and only one of them later grows, is
  rewritten in place, or is replaced, only THAT iteration's own cache entry
  is invalidated and rescanned -- every other iteration the same tail needs,
  unchanged since its own last scan, is served from cache untouched, never
  rescanned merely because a different iteration in the same tail changed.
  `tail=0`
  is an explicit request for the *whole* transcript (`GET /logs`,
  unconditionally) — it is not the only path that can read all of it, since
  the cold-scan fallback above can reach the same result for a positive
  `tail` too. Returns `{"live": bool, "lines": ["<rendered line>", ...]}`
  — never an error. **A whole indexed tail call stays at or under 1,000,000
  bytes for the fixtures this is measured against (task 013, v0.27
  requirement D.1, #275)**, counting every byte it reads off any file — the
  transcript, `render_index.json` and any other side state — on the live and
  the on-disk surface, on the first and a repeated request, for PRD Q's
  canonical fixture and both of its silent-prelude depths, with the render
  still equal to the full renderer's last `N` lines; the bound is not stated
  for transcripts or histories beyond what that test measures. It holds
  just as well while the iteration this tail is served against is still
  RUNNING, between two of the live writer's own geometric publishes (task
  cure-01-01-4, v0.27 PRD D.1 and D.2/D.3 together, DoD 4,
  `tests/test_d1_real_pirunner_active_iteration_reads_within_cap.py`,
  which drives the real engine subprocess, kept alive before its own
  EOF), and just as well at an arbitrary PREFIX of either named fixture --
  not only wherever a real subprocess happens to publish at
  (`tests/test_live_logs_prefix_read_bound.py`: PRD Q's canonical
  fixture and both silent-prelude depths, each truncated one line short
  of its own full transcript and to 35%, 60%, 83% and 97% of its own raw
  line count, on both surfaces, first and a repeated request, once
  against the live writer's own published index for that exact prefix
  and once against a fresh from-scratch rebuild of it, each both with no
  `collapse_index.json` and with a finished iteration's
  `collapse_index.json` left on disk stale for the prefix -- the engine's
  `/logs` handler then resolves its cut's collapse seed off the validated
  render index instead of scanning every transcript byte before the
  cut): `render_index.py`'s live writer appends a small extra shard to a
  side file every `LIVE_FLUSH_EVERY_LINES` checkpoints in between its own
  much-rarer full republishes, so the two named silent-prelude depths --
  large enough to blow past this bound if the whole gap since the last
  full republish had to be scanned raw -- stay inside it instead.
  `render_index.json` is read whole on every call, so the index-read cost is
  proportional to the number of raw transcript lines before the tail, silent
  lines included, not to `N` — the file is therefore stored compactly: each
  distinct render state once, the per-line
  checkpoints as parallel integer arrays (about 20 bytes per raw transcript
  line; see SPEC.md section 11.4). When the run's API isn't reachable (task 039: its
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
  `file`, `seq`, `name`, `ts` (arrival time), `state`
  (`pending`/`outstanding`/`applied` — task 007, #122), `consumed`,
  `bytes`/`hasBody` and `body` — the exact shape the engine's own
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
- `GET /api/runs/<id>/artifacts-summary` — the run page's **Artifacts**
  section: `{runId, count, capped, aliases, notice}`, the bounded file count
  (a walk that stops at 5000 files) and the well-known artifacts that exist.
  It opens no file, and it is the only artifact call the run-detail poll makes;
  the section's "Browse artifacts" button opens a modal over the listing below
  (directory navigation, a name search backed by `q`, paging, a preview pane
  and a download link per file; an html file previews in an `<iframe sandbox>`
  with no `allow-scripts` and no `allow-same-origin`).
- `GET /api/runs/<id>/artifacts?offset=&limit=&q=&dir=&top=` and `GET
  /api/runs/<id>/artifacts/<path>` — what the job left behind in `artifacts/`,
  for the hub's **Artifacts** panel and its dialogs (issue #18.3, #294). The
  listing is one page, `{runId, total, offset, limit, entries, notice}`
  (`ralphd.engine.state.artifact_listing`): `limit` defaults to 200 and is
  clamped to 1000, and each entry has `path` (relative to `artifacts/`, and
  what you pass back in the URL), `name`, `size`, `mtime`, `key` (the
  well-known name — `report`, `suggestions`, `reflect-failed` — or `null`),
  `title`, `sizeDisplay` and a `kind` derived from the extension alone
  (`image`, `markdown`, `json`, `html`, `text`, else `binary`). `q` is a
  case-insensitive substring match on the relative path; `dir` lists one
  directory's direct children, subdirectories first as entries of kind `dir`
  carrying `fileCount` (`top=1` is the same one-level view of the artifacts
  root). The listing stats files and never opens one, so a poll never ships or
  sniffs a body. `notice` is `(no artifacts)` —
  `state.NO_ARTIFACTS`, the line `ralphctl artifacts <run> ls` prints — for a
  run that produced nothing, else `""`. `GET .../artifacts/<path>` is the text
  view: it reads at most `state.ARTIFACT_PREVIEW_MAX_BYTES` (1 MiB) and adds
  `content` (the text), `truncated` (`true` when the file is larger; `bytes` is
  always the full size), `summaryLines` and `text` — the dialog body, what
  `ralphctl artifacts <run> show <name>` prints. A file no preview handles
  (binary, image) answers its sniffed `kind` and size and no `content`. The
  path may be a well-known key (`report`), a path relative to `artifacts/`, or
  that path with the directory (`artifacts/reflection/report.md`), spelled
  either as one percent-encoded segment or with real slashes. Resolution *and*
  the traversal guard are the single `state.artifact_relpath` — an absolute
  path, a `..` segment (also `%2e%2e`) or a NUL is not an artifact and gets a
  `404` on the listing's `dir`, the text view and the download route alike,
  never a file from elsewhere on the host. On-disk only, with no `live` flag
  (the agent writes these files into a directory this host holds); a binary
  artifact answers with the `(binary file …)` wording, and one that is no
  longer there with `(not written)` rather than a `404`. `404` for an unknown
  run id or a name that cannot address an artifact at all.
- `GET /api/runs/<id>/artifacts-download/<path...>` — the whole file, of any
  size or kind, streamed as `Content-Disposition: attachment` with
  `Content-Type: application/octet-stream` and `X-Content-Type-Options:
  nosniff`. The name goes through `state.artifact_relpath` (via
  `state.artifact_download`), like every artifact route; a refused name, a
  missing file or an unknown run id is a `404`.
- `GET /api/runs/<id>/artifacts-raw/<path>` — the raw byte route (task 023,
  issue #54) an `<img src>` can point at directly, since a dialog cannot embed
  an image from a JSON endpoint. Same name shape as `artifacts` above (one
  percent-encoded segment, or several real ones) and the SAME single
  resolver, `state.artifact_relpath` — no second, looser path check lives on
  this route (`state.artifact_raw` does the resolving; a test asserts it is
  the only one). The `Content-Type` is **never guessed from the URL or
  filename** (no `mimetypes.guess_type`): only a file whose sniffed magic
  bytes are a previewable image (`state.ARTIFACT_KIND_IMAGE`, task 022) *and*
  whose size is at or under `state.ARTIFACT_INLINE_MAX_BYTES` is ever answered
  with its real bytes and a `Content-Type` drawn from `state.
  ARTIFACT_IMAGE_MIME_TYPES` (`image/png`/`image/jpeg`/`image/gif`/
  `image/webp`); every answer carries `X-Content-Type-Options: nosniff`.
  Anything else — text, opaque binary, a name that was never written, or an
  image over the size cap — answers `200` with today's `pull` wording
  (`state.ARTIFACT_BINARY`) as plain text instead of a stream of bytes a
  browser cannot make sense of; only a name `state.artifact_relpath` refuses
  outright (traversal, an absolute path) or an unknown run id gets a `404`.
- `GET /api/runs/<id>/approaches` and `GET /api/runs/<id>/approaches/<n>` —
  the run's **archived approach history** for the hub's approach-history panel
  and dialogs (task 020, issue #24): the same reader `ralphctl tasks <run>
  --approach N|--all` (task 019) uses
  (`ralphd.engine.state.archived_approaches_list`/`archived_approach_view`),
  never a second resolver, so the CLI and the hub can never list a different
  set of archived approaches or disagree about one's counts. The listing
  answers `{runId, approaches: [{approach, counts, summary, tasksStale,
  tasksSource}, ...]}`, one entry per approach `LoopSupervisor._archive_approach`
  (`engine/loop.py`) copied into `approaches/NN/` before starting the next one
  — `counts`/`summary` are that approach's **own** archived `tasks.json`, read
  through the same hardened reader (`read_tasks_doc(..., persist=False)`) every
  other tasks surface uses, never recomputed from the live plan. A run still on
  its first approach (nothing archived yet) answers `{runId, approaches: []}`
  rather than an error. `GET .../approaches/<n>` answers `{runId, approach,
  tasks, notice, notes, findings}`: `tasks` is that approach's task table —
  `TasksRead.payload`, byte-for-byte what `GET /tasks` and `ralphctl tasks
  --approach --json` serve for a live plan, including `taskFailureKinds` (issue
  #33) — and `notes`/`findings` are that approach's frozen `notes.md`/
  `review-findings.md`, shaped exactly like the document dialog's entries
  (task 022: `exists`, `bytes`, `sizeDisplay`, `body`, `text`) since
  `_archive_approach` never copies `composite-prd.md`/`job.yaml`, so those two
  keys are simply not part of this view. A document an approach never wrote
  (e.g. a first approach that left no findings) is not an error — its `text`
  is the `(not written)` wording, same as the live document dialogs. On-disk
  only, with no `live` flag, like the document/artifact/fault endpoints above:
  these files are the engine's own copies, written before the archived
  approach is ever superseded, so a dead run's history reads exactly like a
  live one's. `404` for an unknown run id, a non-numeric approach segment, or
  an approach number that was never archived.
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
- `GET /api/runs/<id>/router` — this run's own router calls (task 022, P,
  #80), for the hub's router panel behind a native `<details>`/`<summary>`
  disclosure: `{runId, routerCalls: [...]}`, byte-for-byte
  `state.router_call_entries` read straight off `artifacts/router/
  decisions.jsonl` — the very entries `ralphctl router <run> ls` prints,
  each carrying `chargesIteration: false` and `note` (`state.
  ROUTER_CALL_NOTE`) so the panel cannot present a router decision as a work
  iteration. On-disk only, like the fault/cost/iteration endpoints — a run
  whose container is gone still answers from `artifacts/router/`. A run that
  never reached a router-ambiguous state (every shipped run, today —
  `transitions.ROUTER_FALLBACK` is `{}` by design) answers `routerCalls: []`,
  which `app.js` renders as the same `state.NO_ROUTER_CALLS` sentence
  `ralphctl router` prints. `404` for an unknown run id.
- `GET /api/runs/<id>/iterations/<n>` — one iteration's whole story, in three
  distinctly LABELLED regions matching the on-disk layout (task 020, issue
  #18.1, restructured by #81): outcome (`meta.json`) FIRST, then input
  (`prompt.md`), then logs (`output.jsonl`) — the exact dict `ralphctl
  iteration` prints from (`ralphd.engine.state.iteration_detail` — `number`,
  `phase`, `approach`, `hasMeta`, `startedAt`/`endedAt` plus their `*Local`
  renderings, `durationS`/`durationDisplay`/`durationLabel`, `exitReason`,
  `tokensDisplay`, `costDisplay`/`costStatus`, `hasTranscript`/
  `transcriptBytes`, `hasPrompt`/`promptBytes` and everything else
  `meta.json` records) plus `runId`,
  `summaryLines` (the outcome region's body as labelled text lines, worded
  once by `state.iteration_summary_lines`), `outcomeText` (those same lines
  joined — exactly what `ralphctl iteration`'s outcome region prints, so a
  test can diff the two surfaces for zero drift), `promptText` (the input
  region's raw `prompt.md` body, `state.read_iteration_prompt` — the one
  wording for an iteration that never got one is `(no prompt.md recorded for
  this iteration)`), `log` (the transcript rendered by the same `log_render`
  pass the log tail above uses), `logsText` (that same transcript with its
  `--- log (N lines) ---` separator, as one string) and `text` — the complete
  dialog body, all three regions labelled and in outcome-first order
  (`state.iteration_detail_text`).
  So every string the browser shows was formatted in Python, and the hub
  cannot word an exit reason, duration or token count differently from
  `ralphctl iteration <run> <n>`.
  Unlike the log tail, the PRD and the steering history, this endpoint is
  **on-disk only and has no `live` flag**: `iterations/NNNN/meta.json`,
  `prompt.md` and the
  per-iteration transcript are the engine's own atomic writes into the run
  dir, so there is nothing better a live container could say and nothing to
  fall back *from* (the same reasoning as `ralphctl iteration`'s missing
  snapshot notice). `?log=0` omits the transcript — the `log`/`logsText` keys
  are then
  *absent*, never an empty list/string, exactly like `ralphctl iteration --no-log`.
  An iteration with no transcript answers with the single `(no transcript
  yet)` line, and an unknown run id, an unknown iteration number or a
  non-numeric one all answer `404` with a naming `error`.
- `POST /api/runs/<id>/steer` — body `{"message": ..., "name": ...}`.
  `now` absent or `false` forwards `{"message": ..., "name": ...}` to the
  run's live `POST /steering`, returning the API's own response (`202
  {"file": ...}`) on success. `now: true` is the hub's own "apply now"
  checkbox — the SAME mechanism `ralphctl steer --now`/`ralphctl
  interrupt` drive — and forwards only `{"message": ..., "name": ...}` to
  the run's live `POST /interrupt` instead, passing that route's own
  status code and body (including `target`) through unchanged: `200
  {"interrupted": true, "target": "iteration", ...}` when a running
  iteration was signalled, the same shape naming `"idle"` when nothing was
  running to signal, or the engine's own rejection (`409` once the job has
  already finished, `400`, `401`) with its own body, never wrapped. Either
  branch answers `503` with an `error` if the run's API is unreachable;
  `404` for an unknown run id.
- `POST /api/runs/<id>/retry` — the hub's "retry now" button (task 017):
  forwarded (empty body) to the run's live `POST /retry`, i.e. the same
  thing `ralphctl retry <run-id>` does — wake the pending infra backoff
  wait immediately and reset the outage-budget episode clock (docs/api.md).
  The engine's status code is passed **through**, notably its `409 not
  waiting on an infra fault` refusal, so the UI can say "nothing to wake"
  instead of reporting a generic failure; `503` (with an `error`) only when
  the run's API is unreachable; `404` for an unknown run id.
- `POST /api/runs/<id>/rule` — the run page's operator-ruling textarea (task
  013, v0.23 requirement A.2, #224): body `{"text": ..., "name": ...}`,
  forwarded to the run's live `POST /rule`. Returns the API's own response
  (`202 {"file": ...}`) on success; `503` with an `error`/`detail` if the
  run's API is unreachable or rejects it; `404` for an unknown run id. A
  distinct proxy from `/steer` above, landing in a distinct on-disk record
  (`rulings/`, never `steering/`) — never a variant of steering.
- `POST /api/runs/<id>/unpause` — the run page's "unpause" button (task
  006): forwarded (empty body) to the run's live `POST /resume` — the SAME
  engine route `ralphctl unpause <run-id>` calls, so there is no second
  unpause mechanism. Returns the API's own response (`200 {"resumed":
  true}` in the ordinary case) with its status code passed through
  unedited; `503` (with an `error`) only when the run's API is
  unreachable; `404` for an unknown run id.
- `POST /api/runs/<id>/tasks/<task>/undefer` — the **Undefer now** button
  (#292): forwarded (empty body) to the run's live `POST
  /tasks/<task>/undefer`, the SAME engine route `ralphctl undefer` calls,
  and never a host-side write. The engine's status and body pass through
  (its `404` unknown task, `409` job finished or task not deferred) so the
  button shows the server's own message. A `404` with the engine framework's
  bare `{"detail": "Not Found"}` body means the run's engine has no undefer
  route, so the hub answers it with a `detail` saying the run's engine
  (naming `engineVersion` from `status.json` when it is a well-formed version
  older than 0.31.0) predates undefer (0.31+) and to resume it on a newer
  image; `503` (with an `error`) only when
  the run's API is unreachable; `404` for an unknown run id. The button
  appears on a deferred task's task-table row and on its entry in the Current
  iteration panel's deferred list, only while the run's API answers (a
  run's on-disk snapshot offers none). A successful click reloads the open
  run-detail view at once rather than waiting for the next poll, even while
  that poll is backed off.
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
  (`⚠ 1 failed (requirement-unmet)`, `⚠ 1 validation-failed`,
  `⚠ 1 in-progress` — the same wording
  `ralphctl status` uses, including task 025's two meanings of `failed`), with
  the full sentence (`tasksSummary`) as the
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
  phase, model, duration once ended — each row clickable, see below; newest
  first, collapsed to the 3 newest behind a `show all N iterations`
  disclosure), a **Current iteration** panel beside the log (the in-flight
  iteration's number, phase, model/harness, tier, task, elapsed vs.
  `iteration_timeout_s`, waits and deferrals — or why nothing is running —
  built server-side as `run_detail`'s `currentIteration`), a live log tail rendered with the
  *same* pretty rules as `ralphctl logs` (iteration boundaries, streamed
  text, compact tool one-liners, elided thinking, malformed-line
  markers — rendered **server-side** by the shared `ralphd.cli.log_render`
  module, see `GET /api/runs/<id>/logs` above: `app.js` prints the lines it
  is handed and reimplements none of those rules), and a steering form that `POST`s
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
  A **failed** row carries a second pill naming which kind of failed it is
  (task 025, issue #33: `requirement-unmet` or `validation-exhausted`, also on
  the row as `data-failure-kind`), and its dialog's `status:` line reads
  `failed (validation-exhausted)` — the words
  `ralphctl tasks` prints for the same task. The kind comes from the payload's
  server-derived `taskFailureKinds` map, so the browser never re-implements the
  rule that reads a plan written before the label existed.
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
  single `<dialog>`** as three distinctly LABELLED regions matching the
  on-disk layout (task 020, issue #18.1, restructured by #81), **outcome
  FIRST**: `#iteration-region-outcome` — phase and approach, the absolute
  start/end instants, the duration (labelled `total` or `elapsed`), the exit
  reason (`clean exit`, `exit 7`, `error (exit N): …`, `iteration
  timeout`, `no-traffic timeout`, `interrupted (a signal ended the
  iteration)`, with an
  `[infra fault]` marker when the attempt was refunded), the model pi actually
  used, that iteration's tokens and cost, the steering it consumed, the task it
  verified; then `#iteration-region-input` — that iteration's own `prompt.md`,
  verbatim; then `#iteration-region-logs` — its full transcript. Outcome
  leading means `exitCode: -2, interrupted: true` is readable without
  scrolling past the transcript. Each region's body is one of the `outcomeText`/
  `promptText`/`logsText` strings
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
