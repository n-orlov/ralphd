# ralphd

**A self-contained, locally runnable autonomous coding loop.**

ralphd runs an AI coding agent ([pi](https://pi.dev), or optionally the Claude Code
SDK per hat) in a plan → work → review loop inside a Docker container until a task
is verifiably complete — the
[Ralph Wiggum technique](https://www.aihero.dev/tips-for-ai-coding-with-ralph-wiggum),
packaged as an engine you can run on a laptop with no cloud dependencies.

One container per job. The container exposes an HTTP API for observing progress and
steering the agent mid-flight. A CLI (`ralphctl`) does all the heavy lifting: starting
jobs, injecting credentials and skills, watching progress, steering, and collecting
outputs. The CLI is documented to be driven by a human **or** by another AI agent.

- **ralphd** is the engine (`ralphd-engine`) that runs inside the job container.
- **ralphctl** is the host-side CLI: it builds the image, starts and resumes
  containers, and reads and steers runs. It also serves the local web hub
  (`ralphctl ui`).
- Both ship in one Python distribution, `ralphd` (version 0.32.2, Python >= 3.11).

```
┌─────────────────────────────┐        ┌──────────────────────────────────────┐
│ operator laptop             │        │ docker container (one per job)       │
│                             │        │                                      │
│  ralphctl ──── docker ──────┼──────► │  ralphd  ┌─────────────────────────┐ │
│     │                       │        │  engine  │ loop supervisor         │ │
│     │        HTTP API       │        │          │  plan → work → review   │ │
│     └───────────────────────┼──────► │  :7777   │  spawns `pi` per iter   │ │
│                             │        │          └─────────────────────────┘ │
│  ~/.ralphd/runs/<id>/  ◄────┼────────┼── bind-mounted run dir (history)     │
│  ~/.ralphd/llm-profiles/    │        │  /workspace (bind-mount or clone)    │
└─────────────────────────────┘        └──────────────────────────────────────┘
```

## Key properties

- **Zero environment coupling.** No AWS, no Jenkins, no corporate anything required.
  Everything a job needs — prompts, skills, credentials, LLM endpoint config — is
  mapped into the container by the CLI, upfront or at runtime via the API.
- **Any LLM pi supports.** Anthropic/OpenAI/Google APIs, AWS Bedrock via standard AWS
  credentials, and any OpenAI-/Anthropic-compatible gateway (custom base URL + API
  key). The CLI can forward the host's existing LLM configuration or apply a named
  *LLM profile* (see [docs/llm-profiles.md](docs/llm-profiles.md)).
- **Observable and steerable.** The container API serves job status, task state,
  iteration logs, an SSE event stream, a steering inbox (picked up at iteration
  boundaries), and an interrupt endpoint (SIGINT to the running agent for immediate
  course correction).
- **Survivable outputs.** Run state and artifacts live in a host-mounted run
  directory (`~/.ralphd/runs/<run-id>/`), so history outlives the container — a
  finished container **exits** by default and everything worth keeping is already
  on the host. `on_complete: idle` keeps it up with its API live for debugging.
- **Cost reported by payer.** Each LLM profile can carry a `billing:` block
  (`payer` label, `basis` of `metered|subscription|chargeback|free`). `ralphctl
  cost`/`status`, the hub and the run record group spend by payer: metered and
  chargeback rows show the priced cost, subscription and free rows show `$0` with
  the list price kept apart as list-equivalent. Derived costs are always marked
  as derived.
- **Verified, not just "done".** The worker claiming `COMPLETE` only triggers an
  independent review pass; the job succeeds only when the reviewer emits `VERIFIED`.
  Failed reviews start a new approach with the findings folded into a composite PRD.

## Quickstart

Requirements: Python >= 3.11, `pipx` (or `pip`), and a working `docker` CLI (or
`podman` with `RALPHD_DOCKER=podman`). Nothing is published to PyPI or a registry yet,
so install from a checkout:

```bash
git clone https://github.com/n-orlov/ralphd.git && cd ralphd
pipx install .                 # installs the CLI and the engine entry point
ralphctl doctor                # docker, image, registry and default LLM profile checks
ralphctl llm profiles          # built-in (host, none) plus ~/.ralphd/llm-profiles/*.yaml

# Run a job against an existing checkout, forwarding the host's pi LLM config.
# The job image is built from this source tree on first use (ralphd:<hash>).
ralphctl start --prd ./feature.md --workspace ~/src/widget --llm host
# prints the run id, e.g. brisk-otter-1408

ralphctl watch brisk-otter-1408                      # follow progress
ralphctl steer brisk-otter-1408 "Focus on tests"     # picked up at the next iteration (--now interrupts)
ralphctl status brisk-otter-1408                     # state, tasks, budgets
ralphctl cost brisk-otter-1408                       # spend per phase, approach and payer
ralphctl artifacts brisk-otter-1408 pull ./out/      # copy out what the job left
ralphctl stop brisk-otter-1408                       # shut a finished container down
ralphctl ui                                          # local web hub on 127.0.0.1
```

`ralphctl resume <run-id>` starts a fresh container over an existing run directory
(crash recovery, or `--iterations +N` / `--timeout +N` to top up a budget).
`ralphctl --help` lists every subcommand; [docs/tutorial.md](docs/tutorial.md) is a
full walkthrough and [docs/cli.md](docs/cli.md) is the flag reference.

## Run lifecycle

```
approach 1..max_approaches (shared iteration budget)
  PLANNING  1 iteration   read the PRD, write tasks.json + notes.md
  WORKER    n iterations  one task at a time (vigilant mode adds a verify pass per task)
  REVIEW    1 iteration   re-check every PRD requirement independently
     verified  -> job succeeds
     rejected  -> findings fold into a composite PRD, next approach
budget or approaches exhausted -> job fails (state preserved)
```

Run states are `starting`, `running`, `paused`, `finishing` and `exhausted`
(non-terminal; a paused run waits for the operator, an exhausted run names the
budget it spent -- iterations, time or approaches -- and resumes with a top-up of it),
then `succeeded`, `failed` or `aborted`. Optional gates: `--peer-plan-review`,
`--peer-verdict-gate`, `--mid-plan-review`, and `--reflect` for a post-mortem
iteration. By default a rejection that would archive the approach pauses the run for an
operator ruling (`ralphctl rule`, or `unpause`). See
[docs/architecture.md](docs/architecture.md) for the full state model.

## LLM profiles, harnesses and cost

- **Profiles** are named YAML files in `~/.ralphd/llm-profiles/` (examples in
  [examples/llm-profiles/](examples/llm-profiles/): Bedrock, a generic gateway, a
  cross-family gateway, a Claude subscription). `--llm host` forwards the host's
  pi config; `--llm none` forwards nothing. Inspect with `ralphctl llm profiles|show|test`.
- **Models per hat.** Hats are `planning`, `worker`, `verify`, `review`, `reflect`
  and `router`. Override with `--model-for HAT=REF` and
  `--model-for-tier HAT:TIER=REF` (tiers `trivial|normal|hard`).
- **Harnesses.** `pi` is the default; `claude-code` drives the Claude Code SDK and
  always bills as a subscription; `codex` runs an OpenAI-family hat on the Codex CLI. Choose per hat with `--harness-for HAT=NAME` or a
  profile's `harness:`.
- **Payer and basis.** `--billing-payer TEXT` and `--billing-basis
  metered|subscription|chargeback|free` override the profile's `billing:` block for
  one run. `ralphctl cost <run-id>` prints the `spend:` headline and the by-payer
  table. Runs recorded before 0.32.1 keep their old cost layout.

## Web hub

`ralphctl ui [--port N] [--bind ADDR]` serves a stdlib-only local hub (loopback by
default) that reads `~/.ralphd/runs/*` and proxies each live run's API: a sortable
run list with cost, run detail (tasks, iterations with thinking/effort and router
calls, original start and resume times, per-payer cost, hat model map), artifact
browsing, and steer, retry, undefer and delete actions. It has no authentication, so
a non-loopback bind needs `--allow-non-loopback-bind`.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/solution-tech-spec.md](docs/solution-tech-spec.md) | Source-grounded harness flows and capabilities, with Mermaid diagrams, operating limits and verified boundary gaps |
| [docs/architecture.md](docs/architecture.md) | System design: engine, loop, state model, container layout, lifecycle, security |
| [docs/api.md](docs/api.md) | Container HTTP API specification |
| [docs/cli.md](docs/cli.md) | `ralphctl` reference — written for humans and AI agents |
| [docs/llm-profiles.md](docs/llm-profiles.md) | LLM auth: profile format, host forwarding, Bedrock and generic-gateway presets |
| [docs/tutorial.md](docs/tutorial.md) | Start-to-finish walkthrough: install, doctor, profile, launch, steer, resume, hub |
| [docs/testing.md](docs/testing.md) | Test tiers (smoke, fast, nightly), their recipes and time budgets |
| [docs/ci.md](docs/ci.md) | CI lanes: who runs which tier, where, and what each attests |
| [docs/review-economics.md](docs/review-economics.md) | Reusable PRD fragment: materiality rubric, petition protocol, late-cure rule |
| [docs/petition-format.md](docs/petition-format.md) | Format of an impossibility petition a run files with review |
| [docs/roadmap.md](docs/roadmap.md) | Versioned delivery plan |
| [docs/prds/](docs/prds/) | The PRDs ralphd was built from, verbatim |

For a single full snapshot of the product — requirements, architecture, data
model, fault model, both operator surfaces, testing — see [SPEC.md](SPEC.md).

## Status

Implemented and in use: the engine loop, the container API, `ralphctl`, the web
hub, LLM profiles, and the resilience/self-recovery layer, covered by a test
suite that includes a real docker-sibling tier and a real browser tier. What is
not done: **publishing** — no Docker image is pushed to a registry and the
`ralphd` distribution is not on PyPI, so both are installed from a checkout
(`pipx install .`, `docker build`). See
[docs/roadmap.md](docs/roadmap.md) for the version-by-version state and
[SPEC.md](SPEC.md#15-deferred) for what is deliberately deferred.

## Tests

```bash
pip install -e '.[dev]'
python -m pytest -q -m smoke -n 8 --dist loadfile   # per-task tier, in-process
python -m pytest -q -m fast -n 8 --dist loadfile    # final-sha tier, adds stub-pi flows and CLI subprocess tests
python -m pytest -q -m "docker or browser"          # docker-sibling and real-browser tiers (skip if unavailable)
```

About 7,600 tests are collected. Tiers, budgets and the nightly matrix are in
[docs/testing.md](docs/testing.md).

## Provenance

ralphd is a from-scratch design and implementation of the Ralph loop concept
(planning/worker/review phases, task-state files, mid-flight steering). It shares
ideas — not code or prompts — with a prior internal implementation.

## License

Apache-2.0 (see [LICENSE](LICENSE)).
