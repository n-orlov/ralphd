# ralphd — product spec

An autonomous coding loop you can run on a laptop. One Docker container per job,
an AI coding agent looped over a PRD until the work is verifiably complete, and a
CLI plus local web hub to start, watch, steer and post-mortem it.

Interaction model: **`ralphctl` is the product surface.** Every operator action
is a CLI command, and every one of them is documented to be driven by a human or
by another agent. The web hub is a read-mostly window onto the same state; it
adds no capability the CLI lacks.

**How this document works.** It describes the product as it is meant to be, in
the present tense: one design, with the reasons that constrain it. It does
**not** narrate its own revisions — no "changed from", no "previously", no task
or issue numbers as provenance, no phase numbers. Three other places carry that
record and are the only ones that should: `git log` for how the design changed
and why, `docs/roadmap.md` for what gets built when, and `docs/prds/` for the
briefs individual waves of work were built from. **A sentence that only makes
sense if you already know what the spec used to say, or which version is in
flight, is a defect here** — it rots on its own schedule, and it teaches the
reader to trust the least reliable copy of the history. Reasons are not history:
"the first retry waits 2 seconds because most gateway blips clear inside one
attempt" is a constraint and belongs; "the backoff was made more aggressive"
does not.

Scope note: this file is the snapshot. `docs/architecture.md`, `docs/cli.md`,
`docs/api.md` and `docs/llm-profiles.md` remain the long-form references and go
deeper than this document does on their own subjects; where one of them
disagrees with the code, the code wins and the doc is wrong.

1. [Why](#1-why)
2. [Stack](#2-stack)
3. [Architecture](#3-architecture)
4. [The loop](#4-the-loop)
5. [Data model](#5-data-model)
6. [Job configuration](#6-job-configuration)
7. [LLM profiles](#7-llm-profiles)
8. [Fault model and resilience](#8-fault-model-and-resilience)
9. [HTTP API](#9-http-api)
10. [ralphctl](#10-ralphctl)
11. [Hub UI](#11-hub-ui)
12. [Artifacts and notifications](#12-artifacts-and-notifications)
13. [Security](#13-security)
14. [Testing](#14-testing)
15. [Deferred](#15-deferred)
16. [Open questions](#16-open-questions)

---
## 1. Why

ralphd runs the **Ralph technique** — an autonomous coding agent looped over a PRD
until the work is verifiably done — as a managed job instead of a shell session. A
**job** is one PRD executed to a verdict. It gets its own Docker container, its own
run directory on the host, an HTTP API for observation and mid-flight steering, and a
terminal state that is recorded whether or not anybody was watching when it happened.
Two executables carry the whole product: `ralphctl` on the host (start, observe,
steer, collect, resume, repair) and `ralphd-engine` inside the container (the loop
supervisor plus the API, PID 1, one process).

The loop itself is small: planning writes a task list, a worker executes one task per
iteration, an independent review re-checks every PRD requirement, and only the
reviewer's `VERIFIED` ends the job successfully (§4). Every iteration is a fresh `pi`
process; continuity flows through files in the run directory, never through a live
process's memory. That is what makes an iteration interruptible, a job resumable, and
a container disposable.

The reason this is a daemon and a CLI rather than a shell loop is that the jobs are
long. A run that spends 22 hours and 100M tokens on a PRD cannot depend on a laptop
lid staying open, on a terminal session staying attached, on a `tmux` server
surviving a reboot, or on the operator being present at the moment something goes
wrong. Nor can it depend on the network: the agent talks to an LLM endpoint over the
whole of those 22 hours, and that endpoint will not be up for all of them. Run the
loop by hand and every one of those events costs the work done so far, because the
only place the loop's state existed was the process you just lost.

So the design is shaped by a handful of properties of this problem domain, all of
them mundane and all of them expensive:

- **A transient endpoint fault is indistinguishable, at the call site, from the
  agent failing.** Both are a subprocess that ends badly. A loop that scores them the
  same lets four minutes of DNS trouble eat iterations, approaches and a task's
  validation attempts — the budget meant for *thinking* is spent on failures that
  never reached a model. Worse, the failure shape that actually happens is an in-band
  provider error at **exit code 0** with zero tokens billed, which a naive
  "did it exit nonzero?" check reads as success.
- **The most valuable output of a failed job is the explanation, and it is produced
  last.** A post-mortem iteration that runs immediately after an endpoint outage
  fires into the same dead endpoint, gets nothing, and — if nobody checks its result
  — leaves a run with no record of why it ended. A hundred iterations of work then
  answer no questions at all.
- **The agent has hands.** With a docker socket mounted it can build images and run
  containers, which means it can also `docker rm -f` the container it is running
  inside. A cleanup idiom that selects containers by the run label alone selects the
  job container too, and the agent following that instruction kills its own run
  mid-iteration.
- **Everything the agent touches is transcribed.** Tool arguments and command output
  land verbatim in an iteration transcript that lives on the host forever. One
  `cat ~/.git-credentials` is a permanent credential leak in a file the operator
  will later paste into a bug report.
- **Money is only visible if it is measured honestly.** A gateway that bills tokens
  and reports no price turns into `$0.0000` the moment any code writes
  `cost or 0` — and a cost-optimising model strategy then has an all-zero signal to
  optimise against.

The operator is a human at a terminal or another AI agent driving the same commands;
the CLI is written for both, so every command takes `--json`, exit codes are
documented per command, and there is no hidden session state. The mental model is
deliberately close to a container runtime's: you start a job, you list jobs, you look
at one, you nudge it, you stop it, and if the machine dies you resume it. The
authoritative artifact is never the container — it is `~/.ralphd/runs/<run-id>/`,
which the container merely writes into. `ralphctl` reads that directory when the
container is alive *and* when it is long gone, so a dead run is still a readable run.

Nothing about a job assumes a cloud, a CI system, a secret manager, or a corporate
network. What it needs is a docker daemon, an LLM endpoint it can reach, and a
directory to write into. The credentials for anything else — git forges, Jenkins,
SonarQube — arrive as operator-prepared env files that the agent sources on demand.

### Hard requirements (the acceptance bar)

| # | Requirement | Consequence if broken |
|---|---|---|
| **R1** | **The environment must not be able to destroy a job.** A fault in the LLM endpoint, the network, or the gateway costs a job wall-clock time and nothing else: it is classified (`faults.classify_fault`), retried in place with escalating backoff against a wall-clock **outage budget**, refunded so it never counts against `iterations`, and it never advances an approach, the no-progress stagnation guard, or a task's `validationAttempts`. | Otherwise a four-minute DNS wobble spends ~40 iterations and 4 of 8 approaches, and the job dies with a timeout message that names nothing. Every phase must be covered (`INFRA_RETRY_PHASES` is all five), because an outage does not care which prompt is running. |
| **R2** | **Operator-initiated termination is never undone.** An `abort`/`stop` is recorded durably (`operator-termination.json` in the run dir) and is authoritative over every automatic mechanism: `classify_fault(operator_abort=...)` can never call it `"infra"`, the backoff wrapper never re-runs the iteration the operator just stopped, and `doctor --fix` never auto-resumes such a run. | Otherwise a `SIGINT` — which `pi` reports as the bare in-band error `aborted`, textually identical to a provider aborting a stream — reads as a transient fault, and the run the operator deliberately killed sits in backoff or comes back as a fresh container. |
| **R3** | **The run directory on the host is the source of truth and outlives the container.** `~/.ralphd/runs/<run-id>/` is *always* a bind-mount (at `/run/ralphd`); `status.json`/`tasks.json` are written temp-then-rename; every host-side surface — `status`, `logs`, `runs`, the hub, `repair` — falls back to reading that directory when the API is unreachable, and says so (`"live": false`) rather than erroring. | Otherwise the container's death takes the run's observability with it: 29,720 lines of on-disk transcript with an empty log tail in the UI, `tasks: (none)` next to a 25 KB `tasks.json`, and every post-mortem done by hand in `output.jsonl`. |
| **R4** | **Every terminal state produces a post-mortem or an explicit record of why it could not.** The optional `reflect` iteration runs exactly once, strictly after the terminal state, through the same retry wrapper; a failure is recorded as `status.json`'s `reflect: {ok: false, error: …}` plus `artifacts/reflection/FAILED.md` and surfaced by `ralphctl status` and the hub. A signal taking the engine down instead records `reflect: {ok: null, attempted, skipped}` — no attempt, no tombstone (§8.4). | Otherwise the analysis of the run silently produces nothing — the one outcome indistinguishable, from outside, from never having asked for it. |
| **R5** | **"Done" is a claim; only verification is a verdict.** The worker's `<promise>COMPLETE</promise>` gates entry to review and nothing else; the job succeeds only on the reviewer's `<promise>VERIFIED</promise>`, and only when no steering is left unconsumed. A budget exhausted with every task `completed` still gets one off-budget **grace review** per approach. | Otherwise a confident agent ends the job, and the two shapes that actually cost operators time recur: a "successful" run nobody re-checked, and a `failed/unverified` run whose work was in fact complete but whose last budgeted iteration went to finishing it instead of reviewing it. |
| **R6** | **Secrets never reach disk or a transcript.** Credentials arrive as `<name>.env` files, are placed at `~/.creds/*.env` (mode `0600`) by the engine itself, and are never auto-exported into the agent's environment; only file *names* ever reach the run dir, the persisted job config, or a log line. On top of the prompt rule, redaction is **mechanical**: known secret values are scrubbed from `output.jsonl` at write time, from `events.jsonl` at emit time, and again as `GET /logs` serves. The redaction set is memory-only and no route returns it. | Otherwise one `cat` of a cred file, or one `docker inspect` of the engine container, writes a live token into host-visible history permanently — which prompt guidance alone demonstrably does not prevent. Persisting the redaction map to enable a second pass would put every secret on disk next to the transcript it protects, so that is forbidden too. |
| **R7** | **Unknown cost reads as unknown, never as `$0.0000`.** An iteration with tokens but no provider-quoted price is marked `costPriced: false`; a run total mixing priced and unpriced iterations reads *partial*; an optional host-side pricing map may supply a *derived* cost, which lives in its own `costDerivedUSD` field with its own marker and is never merged into provider-reported `costUSD`. | Otherwise a 102M-token run reads as free, `--model-strategy cost-optimized` optimises against zeros, and no surface can distinguish "free" from "unmeasured". |
| **R8** | **The engine cannot be killed by the agent it supervises.** The job container carries `ralphd.role=job` alongside `ralphd.run=<run-id>`, siblings carry `ralphd.role=sibling`, and the container is told its own name via `RALPHD_SELF_CONTAINER_ID`. Every documented cleanup command — prompt, skill example, docs — is the two-filter, sibling-only form. | Otherwise the idiom ralphd itself teaches deletes the job container mid-iteration: that iteration's work and transcript are lost and the run dir is left non-terminal, which is exactly how a run becomes a zombie that `status` reports as healthy and still-running. |
| **R9** | **A job is resumable from its own run directory.** A fresh `ralphd-engine` over an existing run dir seeds `iterations_used` from the highest *completed* iteration, resumes the recorded approach, skips planning, and treats a topped-up `iterations` as remaining budget. `resume` reproduces the original `docker run` wiring from files (`llm-wiring.json`, `env-wiring.json`, `host.json`), not from the resuming shell. Concurrency is bounded by an exclusive `flock` on `<run-dir>/.lock`; an on-disk `schemaVersion` newer than the build refuses to run. | Otherwise recovery re-plans from scratch, or — worse — resumes into a credential-less container and fails instantly on every iteration because the `-e` flags only ever existed in the shell that typed `start`. Two engines over one run dir would interleave writes into `status.json` and `events.jsonl`. |
| **R10** | **The CLI's `--json` output is a stable machine interface.** Every command supports it, the schema is stable, human text goes to stderr on that path, and exit codes are documented (`0` success · `1` error · `2` usage · `3` unknown run · `4` API unreachable · `5` invalid in current state · `130` follow interrupted). Followers reconcile against live state: a terminal-state event ends a stream only when it is the log's last event *and* the engine is not live. | Otherwise the documented agent completion-wait (`ralphctl watch <id> --json \| jq …`) returns a *historical* `aborted` from before a resume and exits `0` while the job is still running — failing silently, successfully, and precisely after a crash recovery, which is when supervision matters most. |
| **R11** | **Steering is never silently discarded.** A steering file is only marked consumed by a phase whose prompt tells the agent to act on it (`planning`, `worker`); a `VERIFIED` verdict observed with steering still pending is discarded in favour of one more worker iteration and a re-review; and every terminal `status.json` carries `unconsumedSteering`. | Otherwise a note accepted with `202` while the worker was in flight is recorded as consumed by a review iteration that was never going to act on it, or is stranded forever by a run that went terminal — and a terminal run never reads pending steering again. |
| **R12** | **Zero environment coupling.** A job needs a docker daemon, a reachable LLM endpoint, and a writable directory. No cloud service, no CI, no secret manager, no corporate network; the container has no docker socket and no host network unless explicitly opted in. | Otherwise the thing cannot be run on a laptop, which is the only deployment target it has. |

A misconfiguration must still fail fast — R1 and that requirement pull against each
other, and the tension is resolved explicitly rather than by tuning: a broken
credential fails identically in 0.6 s every time, an outage varies and takes time, so
shape-over-time (not error text) decides which one is in front of the loop (§8.4).

### Non-goals (out — do not add)

Multi-job orchestration, queueing, or parallel-job scheduling — **the `--json` CLI is
deliberately sufficient for an external orchestrator**, human, script, or agent · a
daemon or supervisor process of any kind (self-recovery rides on `ralphctl doctor
--fix` invoked from the operator's own cron/systemd) · remote/daemon mode as a
*product* — the `--api-token` + `--api-bind` options already make it possible; TLS,
discovery and making it *nice* are out · agent runtimes other than `pi` (no adapter
interface, no Claude Code / Codex / opencode backend) · provider or model failover on
outage (retrying the same endpoint is this version's answer; failover needs a policy
design of its own) · an npm/node build step for the hub (the bundle is static files
served by the CLI) · a separate hub server process to manage · a cloud service, a
hosted control plane, or any multi-tenant story · CI, published images, or PyPI
releases (nothing in this repo builds or publishes anything) · a `"degraded"` value
in `state` (that case is `health` + `infraWait`, so terminal-state consumers keep
working) · PID-namespace isolation of an iteration from in-container signals ·
inbound remote control beyond the documented API · web-scale anything.

The boundary is **one job, one container, one PRD, one verdict**. Anything that would
make ralphd reason about *several jobs at once* — priorities, queues, fair-share of an
endpoint — belongs to whatever drives `ralphctl`, which is why the machine-readable
surface exists. Anything that would make it reason about *several machines* is deferred
rather than rejected, but no part of the current design may assume it arrives.

---

## 2. Stack

- **Python ≥ 3.11** (`requires-python`), one distribution named `ralphd`, two console
  scripts: `ralphctl` → `ralphd.cli.main:main`, `ralphd-engine` →
  `ralphd.engine.main:main`. Built with `hatchling` from `src/ralphd`.
- **Engine runtime dependencies, all three of them:** `fastapi>=0.110`,
  `uvicorn>=0.29`, `pyyaml>=6.0`. Nothing else is imported at runtime.
- **The CLI is stdlib-only** — `argparse` + `urllib.request`, no HTTP client library,
  no TUI framework — so `pipx install` stays light and the host side has no
  dependency on the engine's web stack. `ralphctl ui` follows the same rule: the hub
  is `http.server`/`ThreadingHTTPServer`, deliberately not `fastapi`/`uvicorn`.
- **The hub bundle is static.** `src/ralphd/cli/web/{index.html,app.js,style.css}`,
  plain HTML/JS/CSS, packaged into the wheel and served by the CLI. **No npm, no
  node, no build step, no bundler** — the price is no framework, and the return is
  that a `pipx`-installed `ralphctl` can serve the UI with nothing else present.
- **Docker is driven through the `docker` CLI as a subprocess**, never a Python SDK:
  one module-level constant, `DOCKER = os.environ.get("RALPHD_DOCKER", "docker")`, is
  the whole integration. That buys `RALPHD_DOCKER=podman` for free, makes every
  container operation reproducible by hand from the recorded argv, and lets the test
  suite substitute a stub `docker` on `PATH` with no in-product test hooks.
- **Agent runtime: `pi`** (`@earendil-works/pi-coding-agent`), spawned once per
  iteration as a subprocess emitting NDJSON events on stdout. Both it and Node are
  **pinned** in the image (`PI_VERSION=0.84.1`, Node 22 via NodeSource) because npm
  silently resolves an ancient `pi` when the node engine requirement is unmet — pin
  both and the failure is loud instead of subtle. Single NDJSON lines routinely
  exceed asyncio's 64 KiB default, so the reader is opened with
  `STREAM_LIMIT = 16 MiB`.
- **Image base:** `python:3.12-slim-bookworm`, plus `git`, `curl`, `ca-certificates`,
  `jq`, `ripgrep`, `procps`; a non-root `agent` user at uid 1000; the static docker
  **client** (`DOCKER_VERSION=29.7.2`, inert without a socket); and
  `@playwright/cli` (`PLAYWRIGHT_CLI_VERSION=0.1.14`) with headless Google Chrome —
  the `chrome` channel specifically, because that is playwright-cli's default and
  chromium would add ~500 MB for no default-path gain. `PI_OFFLINE=1`.
- **Tests:** `pytest>=8` with `pytest-asyncio` in `asyncio_mode = "auto"`, and two
  opt-in tiers marked in `pyproject.toml` — `docker` (real docker-sibling e2e, skips
  cleanly with no socket) and `browser` (real playwright-cli e2e for the hub, skips
  cleanly when absent). Everything is black-box: tests drive the real `ralphctl` and
  a real `ralphd-engine` process. The harness is three stub binaries put on `PATH`
  (`tests/stub-pi/pi`, `tests/stub-docker/docker`,
  `tests/docker-hostpath-wrapper/docker`) plus a `conftest.py` "live test engine"
  fixture that launches `ralphd-engine` directly against a temporary registry so
  `ralphctl` talks to it as if a container had started normally. Lint is `ruff`
  (`line-length = 100`, `target-version = "py311"`). There is no CI: the suite is run
  locally.

---

## 3. Architecture

```
 host                                        │ job container (one per job)
                                             │
 ralphctl start/resume ──── docker CLI ───────► ralphd-engine  (PID 1, one process)
 ralphctl status/logs/steer/retry/abort       │   ├─ LoopSupervisor  (asyncio task)
        │                                    │   │    plan → work → review
        │   HTTP  :<ephemeral> → :7777        │   │    spawns one `pi` per iteration
        └────────────────────────────────────────►  └─ uvicorn/FastAPI  :7777
                                             │        status·tasks·iterations·events
 ralphctl ui  (http.server + static bundle)   │        logs·steering·retry·abort·config
        │ proxies live APIs, falls back to disk
        ▼                                    │
 ~/.ralphd/runs/<id>/    ─── bind-mount ──────►  /run/ralphd     (engine-owned)
 ~/.ralphd/configs/<id>/ ─── bind-mount ro ───►  /config         (job input)
 <workspace host dir>    ─── bind-mount ──────►  /workspace[/<name>]  (agent-owned)
                                             │  ~/.creds/*.env  (0600, placed by engine)
 docker daemon ◄── optional --allow-docker ───┤  ~/.ralphd/config-overlay/ (writable)
        └─ sibling containers (ralphd.role=sibling)
```

Three properties fall out of that picture. **The container is disposable and the run
dir is not** — every durable fact about a job is a file under `/run/ralphd`, which is a
host directory. **The engine is one process**, so "interrupt this iteration" is a plain
`SIGINT` to the `pi` child's process group and needs no IPC. And **the host side is
stateless** apart from `~/.ralphd/`, so any number of invocations from any number of
terminals see the same truth.

### 3.1 Processes and where they run

| process | where | lifetime | role |
|---|---|---|---|
| `ralphctl <cmd>` | host | one command | prepares the config dir, invokes `docker run`, talks HTTP to a run's API, reads the run dir. Stateless except `~/.ralphd/`. |
| `ralphd-engine` | container, PID 1 | the job | `amain()`: takes the run-dir lock, checks `schemaVersion`, places creds and skills, builds the redaction set, copies the PRD if needed, then runs `LoopSupervisor.run_job()` and `uvicorn` as two asyncio tasks in one event loop. |
| `pi` | container, child of the engine | one iteration | the coding agent. Fresh process per iteration; NDJSON on stdout is scrubbed and appended to `iterations/NNNN/output.jsonl`. Killed by process group on every exit path — never orphaned. |
| `ralphctl ui` | host, foreground | until Ctrl-C | the hub: `/api/...` JSON over the registry, proxying each run's live API and degrading to the run dir; the static bundle on every other path, with unmatched paths falling back to `index.html`. |
| sibling containers | host docker daemon | as the job decides | toolchains the thin image lacks (§3.4). Siblings of the job container, not children — the daemon knows no parentage between them. |

`ralphd-engine` is configured entirely by `RALPHD_*` environment variables and the
mounted `/config`; it takes no positional arguments. `--help`/`--version` are parsed
by `argparse` in `build_arg_parser()` and exit before `amain()` runs — no config load,
no directory creation, no port bound, no lock taken — so they are safe to run bare
against a host with live jobs. Exit codes are disjoint and documented: `0` job
succeeded, `1` job did not succeed, `2` no PRD at `/run/ralphd/prd.md` or
`/config/prd.md`, `3` `EXIT_RUN_DIR_LOCKED` (another live engine holds
`<run-dir>/.lock`), `4` `EXIT_SCHEMA_TOO_NEW`. `SIGTERM`/`SIGINT` to the engine abort
the job and let the process wind down through the normal terminal path rather than
dying mid-write.

The API is FastAPI on `:7777` in the same process as the loop. It reads state files
and writes only to the steering inbox, the config overlay, and the supervisor's
control channel: `GET` for `/healthz`, `/version`, `/status`, `/tasks`, `/prd`,
`/notes`, `/iterations[/{n}[/output]]`, `/logs`, `/events` (SSE), `/artifacts`,
`/config`; `POST` for `/steering`, `/interrupt`, `/pause`, `/resume`, `/retry`,
`/abort`, `/shutdown`; `PATCH /config/budget`; and `GET`/`PUT`/`DELETE` under
`/config/{prompts,skills,creds,llm}`. Default posture is loopback-only with **no
auth**; `--api-token` makes `Authorization: Bearer` mandatory on every route (§9.1, §13.3).

### 3.2 The container model

`ralphctl start` assembles one `docker run` invocation and records enough to
reproduce it:

```
docker run -d --name ralphd-<run-id> --init \
  -p 127.0.0.1:<port>:7777 \
  --label ralphd.run=<run-id> --label ralphd.role=job \
  -v <registry>/runs/<run-id>:/run/ralphd \
  -v <registry>/configs/<run-id>:/config:ro \
  -v <host workspace>:/workspace \
  -e RALPHD_SELF_CONTAINER_ID=ralphd-<run-id> [... env wiring ...] \
  <image>
```

- **The container name is chosen by the CLI**, in one place
  (`job_container_name()` → `ralphd-<run-id>`), and doubles as
  `RALPHD_SELF_CONTAINER_ID`: docker accepts a name anywhere it accepts an id, and
  unlike the 64-hex id the name is known *before* `docker run` returns, so the agent
  can be told which container never to touch.
- **`/run/ralphd` is always a bind-mount** of `<registry>/runs/<run-id>/`, and is
  engine-owned. **`/config` is mounted read-only**: operator-provided config is
  immutable job input. **`/workspace`** is agent-owned — a single bare
  `--workspace <dir>` mounts there directly; two or more each require a `:name` and
  mount at `/workspace/<name>` side by side, with `RALPHD_WORKSPACES=<names>` telling
  the prompts what is where so the agent never has to guess from a listing. With no
  `--workspace` at all the engine creates `/workspace` inside the run dir and
  planning clones the PRD's repositories itself.
- **Credentials are placed by the engine, not the entrypoint** —
  `creds.place_creds()` copies `/config/creds/*.env` to `~/.creds/*.env` at mode
  `0600`, keeping secret handling inside the same process that guarantees values never
  reach `/run/ralphd`, `events.jsonl`, stdout or the persisted `job.yaml`. Values
  are *not* exported into the engine's or the agent's environment: a credential is
  visible only to a command that sources its file.
- **Runtime config mutations land in a container-local writable overlay**,
  `$HOME/.ralphd/config-overlay/` (`RALPHD_CONFIG_OVERLAY_DIR`) — neither `/config`
  (read-only, and host-visible input should not be mutated by the running job) nor the
  run dir (host-visible history, and where creds must never appear). Resolution order
  for every config-relative read, implemented once in `engine/config.py`: overlay →
  `/config` → builtin default. The overlay dies with the container, by design.
- **Networking.** Default is the bridge network with `-p <api-bind>:<port>:7777`, so
  docker's publish rule is a second isolation layer independent of what the engine
  binds internally. `--network host` removes that layer — port publishing is
  meaningless in the host namespace — so instead the engine is told
  `RALPHD_PORT`/`RALPHD_BIND` and binds the host port itself. There, `--api-bind` is
  the *only* boundary between the job's API and the network; `doctor` flags the
  caveat rather than making the operator reconstruct it.
- **Labels.** Both labels are always applied, with or without `--allow-docker`:
  `ralphd.run=<run-id>` identifies the run, and `ralphd.role=job` distinguishes *this*
  container from the siblings a job may start (§3.4).
- **Environment wiring** injected by the CLI: `RALPHD_RUN_DIR`, `RALPHD_CONFIG_DIR`,
  `RALPHD_WORKSPACE_DIR` (baked into the image), `RALPHD_WORKSPACES`,
  `RALPHD_SELF_CONTAINER_ID`, `RALPHD_API_TOKEN`, `RALPHD_PORT`/`RALPHD_BIND` for
  host-network jobs, `RALPHD_RUN_ID` + `RALPHD_HOST_RUN_DIR` +
  `RALPHD_HOST_WORKSPACE`/`RALPHD_HOST_WORKSPACES` for docker-socket jobs, plus
  whatever the LLM profile and `--forward-env`/`--llm-env`/`--env` resolved to.
- **Completion.** `on_complete: exit` (the default) exits the container `0` on
  success and `1` otherwise, leaving state in the run dir either way;
  `on_complete: idle` keeps the engine and its API up with the agent never spawned
  again — an explicit debugging opt-in the operator ends with `ralphctl stop`.
- **Self-protection.** The engine takes an exclusive non-blocking `flock` on
  `<run-dir>/.lock` at startup and exits `3` if another live engine holds it, without
  touching any other file. Because a `flock` is kernel-held and process-lifetime, a
  `SIGKILL`ed engine leaves no stale lock and a fresh one can start immediately.

#### The job image

Until v0.6 `container/Dockerfile` existed and nothing built it: `--image` only
ever *selected* a tag somebody had built by hand, so a run could quietly execute
a ten-day-old engine against today's source. The image is now **a function of
its inputs**, resolved and built by `ralphctl start` itself. Three tag
namespaces, because the three hashes are not comparable and a staleness check
must not confuse them:

| reference | means | hash covers |
|---|---|---|
| `ralphd:<hash>` | the default job image, built from this checkout | `container/`, `pyproject.toml`, `src/ralphd/` (`cli/image.py: IMAGE_INPUTS`) |
| `ralphd-base:<hash>` | an operator's own Dockerfile, built as a **base** | that Dockerfile's name plus its whole build context |
| `ralphd-derived:<hash>` | the engine and `pi` layered onto a base | the base reference, the image inputs and the generated recipe |

- **A supplied image or Dockerfile is an ingredient, never the job image.** It
  has no `ralphd-engine` in it, so `render_derived_dockerfile()` generates a
  recipe layering the engine and `pi` — at the version `container/Dockerfile`
  pins, copied rather than restated — onto it, and the derived tag is what runs.
  The base itself is neither probed nor run.
- **Three keys, one unit.** `image` (pin a finished image: no hash, no build),
  `base_image` and `dockerfile` are three answers to one question, so the most
  specific level that answers *any* of them settles all three — command line,
  then the `--template`'s `job.yaml`, then the registry `config.yaml`, then
  "build `ralphd:<hash>` from source". Two of the three at one level is a usage
  error (§10.7), since there is nothing to rank them by.
- **Build on a cache miss only.** The tag is `HASH_LENGTH` hex of a content
  digest over the inputs (`hash_image_inputs()`), so each build is one
  `docker image inspect` and a build only when that tag is absent; a repeat
  `start` on unchanged sources is a lookup and nothing else, and nothing is ever
  tagged `latest`.
- **Recorded, then reproduced.** `host.json` carries `IMAGE_RECORD_KEYS` — the
  `image` reference, the daemon's observed `imageId`, `imageSource`
  (`pinned` \| `cached` \| `built` \| `unhashable` \| `recorded` \| `default`),
  the `imageHash` it was tagged by, and `imageBase`/`imageDockerfile` for a
  derived image. That record is what lets `GET /status` and `ralphctl status` say
  which engine a run is *actually* on, and lets `resume` reproduce the image
  instead of re-resolving it from sources that have since changed (§10.2).
- **Staleness has four answers, not two.** `ralphctl doctor` reports `fresh`,
  `stale`, `missing` or `unknowable` — the last for a pin, for either of the
  other two namespaces, and for an install with nothing to hash — because a
  reference that cannot be compared to a source hash must never be reported as
  up to date. The same verdict is applied per live run against the image its own
  `host.json` records, which is the case the mechanism exists for: a job
  executing an engine that predates the fix it is being watched for.
- **An install with no checkout next to it still hashes.** The wheel ships the
  image inputs as package data (`ralphd/_image/`, `cli/image.py:
  PACKAGED_FILES`) and a build stages them plus the *installed* package into a
  temporary context laid out exactly like a checkout, producing the same
  `ralphd:<hash>` a checkout of that version builds. `doctor` names which of the
  two it found as `imageStaleness.inputs` (`checkout` \| `packaged` \| `none`);
  with neither, `start` warns and staleness stays `unknowable`.
- **`cli/image.py` is docker-free by construction.** It owns the declarative
  half — which files are inputs, what they hash to, the text of the generated
  recipe — while running builds, cache lookups and precedence live in
  `cli/main.py` (§3.5).

### 3.3 Host layout

Everything host-side lives under one directory, `~/.ralphd/`, overridable in full via
`RALPHD_REGISTRY` (which is how the test suite gets a throwaway registry):

```
~/.ralphd/                       # the "registry" — RALPHD_REGISTRY overrides
├── config.yaml                  # registry-wide defaults: image, network, on_complete,
│                                #   default_llm_profile, auto_resume, pricing map
├── llm-profiles/<name>.yaml     # named LLM profiles (env/mounts/pi fragments)
├── templates/<name>/            # job templates: job.yaml + optional prd/skills/creds
├── runs/<run-id>/               # bind-mounted at /run/ralphd — the source of truth
│   ├── status.json              # engine-maintained: state, phase, approach, verdict,
│   │                            #   health, infraWait, schemaVersion, usage, reflect
│   ├── tasks.json               # the plan; agent-written, atomically
│   ├── prd.md · composite-prd.md · notes.md · review-findings.md
│   ├── events.jsonl             # append-only event log, also fed to GET /events
│   ├── iterations/NNNN/{meta.json,output.jsonl}
│   ├── approaches/ · artifacts/ · steering/{NNN-*.md,.consumed.json}
│   ├── vigilant-verified.json   # engine-owned record of tasks that passed verify
│   ├── operator-termination.json# durable record of an operator abort/stop
│   ├── auto-resume.json         # crash-loop guard: attempts, lastAt, maxAttempts
│   ├── host.json                # container id, port, apiUrl, the image record (§3.2), network, workspace
│   └── .api-token (0600) · .lock
└── configs/<run-id>/            # bind-mounted at /config, read-only
    ├── job.yaml                 # the job config as launched
    ├── prd.md                   # as handed to `start`
    ├── prompts/ · skills/<name>/ · creds/<name>.env · pi/{models.json,…}
    ├── llm-wiring.json (0600)   # resolved --llm env + mounts, for `resume`
    ├── env-wiring.json (0600)   # resolved --forward-env/--llm-env/--env pairs, ordered
    └── auto-resume.json         # host-side opt-in; never passed into the container
```

The split between `runs/<id>/` and `configs/<id>/` is load-bearing rather than
cosmetic: the run dir is history, it is mounted writable, and it must never contain
anything credential-shaped, while the config dir is job *input*, is mounted read-only,
and is exactly where the resolved-secret files (`pi/models.json`,
`llm-wiring.json`, `env-wiring.json`) live at `0600`. Because the config dir survives
a container's death untouched, `ralphctl resume` can reproduce the original wiring
byte-for-byte from files instead of from whatever the resuming shell happens to have
in its environment — replaying `llm-wiring.json`'s `env`/`mounts` first, then
`env-wiring.json`'s pairs in their original order, so a later duplicate name still
wins exactly as the original `-e` flags did.

Run ids are `adjective-animal-HHMM` by default (`gen_run_id()`) or supplied with
`--run-id`. `ralphctl runs` is a scan of `runs/*/status.json`, newest first, with live
status merged in per run when a container answers. Nothing in the registry is a
database: every file is independently readable, and a missing or malformed one
degrades to a diagnostic rather than an error.

### 3.4 Sibling containers and the toolchain-in-a-sibling pattern

The image is deliberately thin on toolchains and the agent runs as non-root `agent`,
so a job cannot `apt-get install` a missing compiler. The heavyweight answer is a
derived image per job; the general answer is **run the toolchain work in a sibling
container with the host workspace bind-mounted**, which keeps the shipped image
unchanged. `ralphctl start --allow-docker` enables it by mounting the host docker
socket (`RALPHD_DOCKER_SOCK`, default `/var/run/docker.sock`) and adding its group via
`--group-add <gid>` computed at launch.

**That opt-in is root-equivalent, and there is no partial-trust variant.** A container
holding the socket can `docker run --privileged -v /:/host` and own the machine; the
non-root user and every other hardening measure stop mattering at that point. Socket
proxies that permit `run` at all permit arbitrary mounts, so the design does not
pretend otherwise: `ralphctl` prints a loud warning at launch, and the flag is for
PRDs trusted as much as an unsupervised shell on the same host.

The pattern the prompts teach whenever `--allow-docker` is in effect puts both files
in the *target* repo — `ci/Dockerfile` (a base image plus just that toolchain) and
`ci/run.sh` (a wrapper running an arbitrary command in a `--rm` sibling) — so the
setup is reproducible without the agent. `examples/skills/toolchain-sibling/` ships a
generic version as a mountable skill. Five details are load-bearing, each of them a
failure mode when omitted:

1. **Host paths only.** A sibling's `-v` is resolved by the *host* daemon, so every
   mount source must be `$RALPHD_HOST_WORKSPACE` / `$RALPHD_HOST_WORKSPACES` /
   `$RALPHD_HOST_RUN_DIR`. Mounting the container-local `/workspace` silently mounts
   an **empty** directory, and the daemon may auto-create it root-owned on the host.
   This is the most common mistake. `docker build` contexts are exempt — the CLI
   streams the context itself.
2. **`--user 1000:1000`.** The job container's `agent` and the host user are both uid
   1000; a default-root sibling leaves root-owned files in the workspace that the
   agent can afterwards neither modify nor clean up.
3. **A named cache volume** for the toolchain's download/build directories
   (`GOMODCACHE`/`GOCACHE`, `~/.cargo`, `~/.m2`), named after repo+toolchain and
   deliberately *without* the run label so it is shared across runs. The trade-off is
   "shared and long-lived" versus "per-run and explicitly deleted", never "shared but
   run-id-gated" — gating a shared volume on `$RALPHD_RUN_ID` makes the repo's own
   `run.sh` fail for every subsequent run.
4. **Networking.** Siblings get docker's default bridge network and ordinary internet
   for pulls and dependency downloads, regardless of the job container's own
   `--network` (which may be `host`). They neither need nor should be given the job's
   LLM gateway access.
5. **Sibling-only cleanup.** Every sibling carries `ralphd.role=sibling` in addition
   to the run label, which is the entire point of the `role` label:

```bash
# the only in-container cleanup form, ever:
docker rm -f $(docker ps -aq \
  --filter label=ralphd.run=$RALPHD_RUN_ID \
  --filter label=ralphd.role=sibling)
```

**Filtering on `label=ralphd.run=<id>` alone is forbidden inside the container.** The
job container carries that label too, so the one-filter form selects the container the
agent is running in: `docker rm -f` over it kills the run mid-iteration, discards that
iteration's work and transcript, and leaves the run dir recorded non-terminal with no
process to advance it. `$RALPHD_SELF_CONTAINER_ID` names the one id never to touch,
and the prohibition ships with the *reason* attached in every copy — prompt, skill
example, and docs — because an unexplained rule reads as optional advice to a capable
model. In-container cleanup is optional anyway: **reaping is `ralphctl`'s job.**

Host-side, the filter is the opposite by design. `_reap_siblings()` — used by
`ralphctl stop` and `ralphctl rm` — matches the **run label alone**, because there
"take this run down" genuinely includes the job container. It is best-effort and never
fails the command. `ralphctl doctor` reports stray labelled containers whose run id
has no registry directory, report-only. Reaping is containers only: labelled *images*
and *volumes* survive `stop`/`rm` deliberately, and anything the job left unlabelled
and detached outlives it — the daemon has no parentage notion to fall back on.

### 3.5 Module map

| path | responsibility |
|---|---|
| `src/ralphd/__init__.py` | `__version__` and `API_VERSION` — nothing else. |
| `src/ralphd/log_merge.py` | The one implementation of "a run's transcript": `iterations/NNNN/output.jsonl` in order, wrapped in synthesized `ralphd.iteration` boundary lines. Imported by the engine's `GET /logs` snapshot path *and* by host-side readers straight off the run dir, so both emit byte-identical lines. Scrubbing is an injected callback, never assumed. |
| `src/ralphd/prompts/planning.md` | Planning phase: read the PRD, write `tasks.json` + `notes.md`, decide scope and optional `dependsOn`/`priority`. |
| `src/ralphd/prompts/worker.md` | Worker phase: the one-task-per-iteration rule with its reasons, the task pick rule, credential handling, and the sibling-cleanup prohibition. |
| `src/ralphd/prompts/review.md` | Review phase: independently re-check every PRD requirement, re-verify criteria flagged as edited after a validation failure, emit `VERIFIED` or write findings. Never fix anything. |
| `src/ralphd/prompts/task-verify.md` | Vigilant-mode per-task verification against `successCriteria`, emitting `<task-verified>id</task-verified>`. |
| `src/ralphd/prompts/reflect.md` | Post-terminal self-reflection: analyse the run and write a report plus an optional prompt/skill diff to `artifacts/reflection/`, touching nothing else. Builtin-only — not in `PROMPT_NAMES`, so not overridable via the prompts API. |
| `src/ralphd/engine/main.py` | Entrypoint and startup order: argparse-only `--help`/`--version`, run-dir `flock`, `schemaVersion` check, creds/skills placement, redaction-set build, PRD copy, then `LoopSupervisor.run_job()` + `uvicorn` in one event loop. Owns the `on_complete_cmd` hook and the process exit codes. |
| `src/ralphd/engine/config.py` | `JobConfig` (every budget, flag, timeout and retry knob, with its default and `RALPHD_*` override) and its `effective()` view for `GET /config`; the container path constants; the overlay → `/config` → builtin resolution order (`overlay_or_config`, `overlay_write_path`); phase→model resolution via `STRATEGY_TIERS`. |
| `src/ralphd/engine/state.py` | The run directory as an object: paths, atomic write-then-rename JSON, `status.json` merge-patch updates, `events.jsonl` emit (scrubbed), the `.lock` flock, iteration directories and `max_iteration_number()`, steering inbox and consumed marker, the vigilant-verified record, operator-termination record, schema-version policy, the hardened `tasks.json` read (`read_tasks_doc()` → `TasksRead`, §5.3), and the shared duration/time/cost/approach/task-count/image formatters both sides render with. |
| `src/ralphd/engine/loop.py` | `LoopSupervisor`: the whole loop. Approaches and composite PRDs, per-phase prompt assembly, budget accounting and refunds, vigilant verification, criteria fingerprinting, the stagnation and instant-failure guards, the infra-retry wrapper and outage-budget episode clock, grace review, steering application, pause/interrupt/abort/retry gates, the live `tasks.json` poller, and reflection. |
| `src/ralphd/engine/runner.py` | `PiRunner.run()` + `IterationResult`: spawn one `pi` process, pump and scrub its NDJSON into `output.jsonl`, parse events and usage, scan for the exact sentinels, enforce the iteration timeout and the startup-window watchdog, and kill the process group on every exit path. |
| `src/ralphd/engine/api.py` | The FastAPI app: every route in §3.1, bearer-token enforcement, the SSE event stream, `GET /logs` tail and live-follow on top of `log_merge`, `PATCH /config/budget`, and the config CRUD that writes into the overlay and re-places creds/skills immediately. |
| `src/ralphd/engine/faults.py` | `classify_fault()` — a pure function mapping a finished iteration to `"infra"`, `"work"`, or `None`, with `_INFRA_TEXT_PATTERNS` as the single reviewable signature table (one commented line per family) and the `aborted`/operator carve-out. No engine state. |
| `src/ralphd/engine/pricing.py` | `PricingMap`: the optional host-side per-million-token rate table with gateway alias globs and declared-free patterns, used *only* when the provider quoted no usable price, producing a cost marked derived and never merged into `costUSD`. `PricingChain` layers the operator map over a built-in table so exactly one of them prices any message, and `resolve_pricing()` builds whichever of the two `price_strategy` asks for (§8.6). |
| `src/ralphd/engine/pricing_aws.py` | The shipped AWS Bedrock rate table: canonical per-model USD/Mtok rates, a generated alias map for the gateway spellings, and a machine-readable `AS_OF` date with a `staleness()` verdict. Exposed as a `PricingMap` through `pricing_map()`, so there is one matcher rather than a second resolver, and refreshed by `tools/refresh_bedrock_rates.py`. |
| `src/ralphd/engine/redact.py` | The mechanical secret scrubber: build the value set from env vars and placed cred files, rebuild it after any creds/llm mutation, keep it in memory only, and expose `scrub_text` to the three write/serve points. Enforces the ≥8-character floor. |
| `src/ralphd/engine/creds.py` | `place_creds()`: `/config/creds` (plus overlay and tombstones) → `~/.creds/*.env` at `0600`, conventional extras (`gitconfig`, `git-credentials`, `netrc`, `ssh/`), and the one-shot `setup.sh` escape hatch. Logs names, never contents. |
| `src/ralphd/engine/skills.py` | `place_skills()`: rebuild `~/.pi/agent/skills/` from scratch (symlinks) out of mounted skills, API-origin overlay skills that win over same-named mounted ones, and tombstones — cheap enough to re-run after every mutation, so skill CRUD needs no restart. |
| `src/ralphd/engine/llm.py` | The mid-run LLM path: `PUT /config/llm` writes the env-override set to the overlay (read fresh each iteration) and deep-merges a `models.json` fragment into the file `pi` itself reads, so a rotated key applies to the next invocation. |
| `src/ralphd/cli/main.py` | `ralphctl`: every subcommand, the `docker run` assembly for `start`/`resume`, the registry and its defaults, templates, run-id generation, the API client, `--json` output and exit codes, `logs`/`watch` streaming and `tail`-style argv preprocessing, `doctor`/`doctor --fix`/`repair`, the auto-resume opt-in and its crash-loop guard, and `AUTO_RESUME_DEFAULT` as the single default literal. |
| `src/ralphd/cli/log_render.py` | The shared NDJSON→lines pretty renderer: iteration/phase headers, streamed assistant text, tool calls as one-liners, thinking collapsed to a single marker per block, per-iteration usage/cost footers, errors highlighted. `tty=False` yields plain text with no ANSI and no `\r`. Lives here, not in `main.py`, because the hub must import it without a cycle. |
| `src/ralphd/cli/image.py` | The job image, declaratively and docker-free: which files are inputs (`IMAGE_INPUTS`), how they hash to a tag, the packaged-inputs fallback for an install with no checkout (`PACKAGED_FILES`), and the text of the generated derived recipe (§3.2). Runs no builds — `cli/main.py` does that. |
| `src/ralphd/cli/ui_server.py` | The hub server: `/api/runs` and `/api/runs/<id>[/logs,/prd,/steering,/documents,/artifacts,/fault,/cost,/iterations/<n>,/steer,/retry]` over the registry (`DELETE /api/runs/<id>` for a terminal run), proxying a run's live API with a short timeout and falling back to the run dir (`"live": false`) rather than 500-ing on the four views that have a live answer at all; renders log tails, cost breakdowns, fault explanations and iteration detail through the same modules `ralphctl` prints, server-side, so the browser only displays strings; serves the static bundle with `index.html` fallback. |
| `src/ralphd/cli/llm_profiles.py` | Named-profile loading and host-side resolution: `${env:}`/`${file:}`/`${cmd:}` references evaluated exactly once, before the container starts, into `env`/`mounts`/`pi`. The `host` and `none` built-ins never reach this module. `MASK` is what `llm show` prints instead of a value. |
| `src/ralphd/cli/web/index.html` | The hub shell — one page, no framework, no build step. |
| `src/ralphd/cli/web/app.js` | Hub behaviour: run list with client-side sorting whose state lives outside the DOM (the table is rebuilt every few seconds), run detail with task table, iteration timeline, log tail, steering form and history, state-document/artifact/fault/cost/iteration dialogs, the delete affordance, degraded card with countdown and retry-now. Agent-authored text is rendered with `textContent` only, never `innerHTML`. |
| `src/ralphd/cli/web/style.css` | Hub styling, including the warning/degraded/snapshot treatments the JS keys off. |
| `src/ralphd/{cli,engine}/__init__.py` | Empty package markers. |
## 4. The loop

One job is one loop, supervised by `LoopSupervisor` in
`src/ralphd/engine/loop.py`: which phase runs next, on which model, whether an
attempt cost the job an iteration, when an approach is abandoned, when the run
goes terminal. The agent is stateless across iterations — each one is a fresh `pi`
subprocess (`src/ralphd/engine/runner.py`) fed a prompt on stdin, whose only
memory of the run is what earlier iterations wrote to the run directory and the
workspace.

**The loop never trusts a claim it can check.** A worker's "done" is a sentinel
string that buys an independent review; a review's verdict is the only thing that
can make a run succeed.

### 4.1 Phases

An iteration runs exactly one phase. There are five.

| phase | prompt | given | must produce |
|---|---|---|---|
| `planning` | `planning.md` | PRD, workspace | `tasks.json` (the plan) plus `notes.md` handoff notes; implements nothing |
| `worker` | `worker.md` | `tasks.json`, `notes.md`, workspace | exactly one task advanced to `completed`, `notes.md` refreshed; `<promise>COMPLETE</promise>` when every task is done |
| `verify` | `task-verify.md` | one task's `id`/`title`/`successCriteria` | `<task-verified>{id}</task-verified>`, or `status: validation-failed` + `validationNotes` on that task in `tasks.json` |
| `review` | `review.md` | PRD (composite when present), `tasks.json`, workspace | `<promise>VERIFIED</promise>`, or `review-findings.md` listing every unmet requirement |
| `reflect` | `reflect.md` | the finished run's own records | `artifacts/reflection/report.md`, optionally `suggestions.diff` |

`planning` runs once per approach, then the `worker` loop (with `verify`
iterations interleaved in vigilant mode, §4.4), then `review` once the worker
signals completion; `reflect` runs after the run is already terminal and cannot
change how it ended. What runs next is decided from disk state, not from agent
narration: `_resume_point()` skips `planning` when the run dir already holds a
`tasks.json` with tasks and at least one completed iteration, continuing the
existing approach's worker loop; `planning` that produces no tasks abandons the
approach; the `worker` loop runs while budget remains, ending on
`<promise>COMPLETE</promise>`, on three consecutive iterations that changed
nothing, or on exhaustion; `review` then decides the run.

Prompt text resolves in one order: runtime overlay (written through the config
API) over the mounted `/config/prompts/<name>.md` over the built-in
`src/ralphd/prompts/<name>.md`. `PROMPT_NAMES` in `engine/config.py` names the
four the config surfaces enumerate: `planning`, `worker`, `review`,
`task-verify`.

`build_prompt()` appends a `## Job context` block naming the run state directory,
the workspace (or every mounted workspace name and path for a multi-workspace
job), the PRD file, `tasks.json`, `notes.md` and the artifacts directory, then two
conditional blocks: `## Docker siblings` when the host docker socket is mounted
(host-side paths, the mandatory two-label cleanup filter, the rule that the job's
own container is never removed) and `## Credentials` listing the `*.env` file
*names* placed at `~/.creds` with the sourcing rule — **never a value**. Pending
steering comes last (§4.6). The rendered prompt is written to
`iterations/NNNN/prompt.md`, so every iteration's exact input is recoverable.

`JobConfig.model_for(phase)` resolves each phase's model: an explicit
per-phase override (`model_overrides`), else the phase's tier under the
configured strategy. `None` means "whatever `pi` defaults to".

| strategy | planning | worker | review | verify | reflect |
|---|---|---|---|---|---|
| `quality-first` (default) | strong | strong | strong | strong | strong |
| `cost-optimized` | strong | fast | fast | fast | fast |
| `balanced` | strong | fast | strong | fast | strong |

The strong tier is `model`, the fast tier is `fast_model` falling back to
`model`. `reflect` mirrors `review` in every strategy — the same post-hoc
analysis role. The resolved reference is recorded per iteration (`meta.json`'s
`model`, the `iteration.start` event), so a run's model mix is auditable.

### 4.2 Iterations, approaches and the composite PRD

An iteration is one agent subprocess and one directory, `iterations/NNNN/`
(4-digit, zero-padded, monotonically increasing). A restarted engine seeds its
counter from `RunDir.max_iteration_number()`, which counts only iteration
directories whose `meta.json` has an `endedAt`, so a slot left half-written by
a killed process is reused and a finished one is never renumbered.

An *approach* is one full attempt at the PRD: plan, work, review. Approaches
run sequentially from 1 to `max_approaches` (`--max-approaches`, default `3`)
and **share one iteration budget** — approaches bound how many times the job
may start over, not how much work it may do.

| an approach ends because | archived | composite PRD rewritten |
|---|---|---|
| `planning` produced no tasks | no | no |
| three consecutive worker iterations changed nothing (stagnation guard) | yes | no |
| `review` rejected it | yes | yes |

Archiving is `_archive_approach()`: copy `tasks.json`, `notes.md` and
`review-findings.md` into `approaches/NN/`. The next approach starts from a
fresh plan.

**A rejected review — and only a rejected review — regenerates the composite
PRD.** `_write_composite_prd()` writes `composite-prd.md`: the original
`prd.md` verbatim, then a `# Previous attempt history` section appending, per
archived approach, its `notes.md` under `### Final notes` and its
`review-findings.md` under `### Review findings (unmet requirements)`, closing
with an instruction to address all findings and not blindly repeat the previous
approach. `state.prd_path()` is the single decision of which file *is* the run's
PRD — the composite when it exists, the original otherwise — shared by the
prompt builder and every PRD reader, so the next approach plans against the
accumulated history rather than the original text alone.

### 4.3 Sentinels and completion

Three exact strings carry agent claims. The engine matches them in the final
assistant text of the iteration, not in tool output.

| sentinel | emitted by | meaning to the engine |
|---|---|---|
| `<promise>COMPLETE</promise>` | `worker` | every task is `completed` (or justified `skipped`); end the worker loop and run `review` |
| `<promise>VERIFIED</promise>` | `review` | every PRD requirement independently verified; the run is `succeeded`/`verified` |
| `<task-verified>{id}</task-verified>` | `verify` | that one task's success criteria hold |

`COMPLETE` and `VERIFIED` are matched in `engine/runner.py`
(`IterationResult.saw_complete` / `saw_verified`); the per-task sentinel is built
from the task id in `_verify_task()`. Each match emits a `signal` event. A
`VERIFIED` verdict is refused while operator steering is still unconsumed (§4.6).
The reflect prompt ends on `<promise>REFLECTED</promise>`, which the engine does
not score: the report on disk is the deliverable (`_reflect_failure()`).

### 4.4 Vigilant mode

Vigilant mode (`vigilant: true`) inserts an independent `verify` iteration per
completed task: after each worker iteration the supervisor lists every task
currently `completed` that is not in `vigilant-verified.json` and runs one
`verify` iteration for each, budget permitting.

`vigilant-verified.json` is engine-owned and never touched by the agent. A
per-process before/after diff of `tasks.json` cannot tell "still `completed`
because it was already verified" from "still `completed` because the previous
process was killed between the worker iteration and its verify iteration"; the
disk record survives crash and resume, so a missed verification is never skipped
forever.

Task-failure bookkeeping, all in `_verify_task()`:

| condition | effect |
|---|---|
| sentinel emitted | task id added to `vigilant-verified.json`; `signal` event `taskVerified` |
| verdict miss (a verifier ran to completion, no sentinel, no error) | `validationAttempts` incremented; `status` forced to `validation-failed` and a default `validationNotes` added if the verifier wrote none |
| `validationAttempts` reaches 3 | `status` forced to `failed` |
| task already at `validationAttempts >= 3` | verification skipped |
| the verify iteration reached **no verdict at all** — an in-band agent/provider error, the full `iteration_timeout_s`, the startup-window watchdog, or a signal (`error_message` / `timed_out` / `no_traffic_timeout` / `interrupted`) | retried up to `MAX_VERIFY_ERROR_RETRIES` (3) **without** consuming a validation attempt; if no attempt ever reaches a verdict, `status`, `validationAttempts` and `validationNotes` are left byte-for-byte as they were |
| no verify iteration ran at all (budget gone) | same: nothing observed, nothing recorded against the task |

The last two rows are the load-bearing ones (task 012, #45): absence of a
verdict is not a negative verdict, so neither an infrastructure fault nor one of
the engine's own timeouts/interrupts may be recorded as a failed validation, and
the note written on those paths says the verifier never reached a verdict rather
than quoting the missing sentinel. The verify iteration's `meta.json` keeps the
distinction on disk: `verifyOutcome: "error"` means no verdict was reached,
`"fail"` means a verifier judged the criteria unmet (`verifiedTask`,
`verifyOutcome`).

Independently of vigilant mode, every worker pass fingerprints success criteria:
`_ensure_criteria_baseline()` stores a `criteriaFingerprint` (sha256 of
`successCriteria`) per task and `_check_criteria_edits()` compares it afterwards.
A rewrite seen while `validationAttempts` is still 0 just re-baselines; a
rewrite seen after at least one validation failure sets the sticky
`criteriaEditedAfterValidationFailure` marker, and every subsequent `review`
prompt gains a section naming those tasks and demanding an explicit per-task
pass/fail conclusion against the *current* criteria text. Otherwise a worker
could dodge every automated check by moving the bar after failing it.

### 4.5 Budgets, deadlines and timeouts

| limit | field / constant | default | meaning |
|---|---|---|---|
| iteration budget | `iterations` | `25` | charged iterations the job may spend, across all approaches |
| approaches | `max_approaches` | `3` | how many times the job may start over |
| wall-clock deadline | `job_timeout_s` | `8h` | total run time available to the agent |
| per-iteration timeout | `iteration_timeout_s` | `45m` | one agent subprocess's ceiling |
| startup (no-traffic) window | `infra_startup_timeout_s` | `150s` | how long an iteration may run with zero observed LLM traffic |
| outage budget | `infra_outage_budget_s` | `4h` | cumulative backoff wait one outage episode may spend |
| reflect outage budget | `REFLECT_OUTAGE_BUDGET_S` | `300s` | the same, for the post-terminal reflect iteration |
| instant-failure fail-fast | `INSTANT_FAILURE_MAX_DURATION_S`, `MAX_CONSECUTIVE_INSTANT_FAILURES` | `5.0s`, `3` | a run of identical sub-5s zero-work failures aborts the job |
| stagnation guard | — | `3` | consecutive no-change worker iterations that abandon an approach |

`budget_left()` is the single gate, evaluated at every turn of the loop:

```python
charged = self.iterations_used - self._infra_refunded - self._grace_refunded
return (charged < self.cfg.iterations
        and time.monotonic() < self.deadline
        and self._abort_reason is None)
```

Raw attempts always increase — every attempt gets its own iteration number and
directory — but two kinds are refunded and never charged: an attempt retried
after a fault `classify_fault()` scored as `infra`, and an off-budget grace
review (`_maybe_grace_review()`). `status.json`'s `iterationsUsed` publishes raw
attempts minus infra refunds, so a refund an operator was shown is never quietly
re-charged.

The per-iteration timeout is clamped by what is left of the deadline:

```python
timeout = min(self.cfg.iteration_timeout_s,
              max(60, int(self.deadline - time.monotonic())))
startup_timeout_s = (min(self.cfg.infra_startup_timeout_s, timeout)
                     if phase in self.INFRA_RETRY_PHASES else None)
```

The startup watchdog exists because a hang with *nothing* coming back — a DNS or
gateway glitch the agent process blocks on internally — otherwise consumes the
full 45-minute iteration timeout before dying. It fires on the absence of any
parseable agent event, kills the process group, and reports `no_traffic_timeout`
distinctly from a real timeout, across all five phases (`INFRA_RETRY_PHASES`).

**An outage must not eat the job's working time.** The deadline exists twice, as a
monotonic `self.deadline` and its published wall-clock twin `deadlineAt`, and
`_account_infra_wait()` pushes *both* out by exactly the seconds spent in each
infra backoff wait, adds them to `infraWaitTotalS` and emits a `deadline_extended`
event so the adjustment is auditable rather than silent. `infraWaitTotalS` is
re-read from `status.json` at startup, so it survives a resume. Without it a
four-hour outage would silently consume half an eight-hour job.

`set_iteration_budget()` tops the budget up in flight, rebinding `cfg.iterations`
and rewriting `iterationsBudget`; because `budget_left()` re-reads the field every
turn the new budget applies at the next iteration boundary, with no container
restart and no re-read of the read-only `job.yaml`. A budget can only be raised to
the current usage or above, and the change is recorded as a `budget_changed` event.

When the budget runs out with every task already `completed`, the approach gets
exactly one **off-budget grace review** rather than going terminal
`failed`/`unverified` with all the work done and nobody having looked at it. It is
granted at most once per approach, never loops back into the worker, and is
refunded from the charged count; a run that succeeds this way records
`graceReview: true`.

### 4.6 Steering, pause, interrupt, abort

Steering is a file, not a message queue. Each message lands in
`steering/NNN-<name>.md` with an engine-assigned sequence prefix;
`steering/.consumed.json` lists the names already actioned, and everything else
is pending.

- Pending steering is injected into the next `planning` or `worker` prompt under
  `## Operator steering (MUST take priority)` and marked consumed at that
  iteration's start (`steering.consumed` names the file and the iteration).
- `review` and `verify` are pure verification roles whose prompts carry no
  steering instructions, so they must not consume it
  (`STEERING_ACTIONABLE_PHASES`): they get a read-only note that steering is
  waiting for the next actionable phase. Marking it consumed there would record
  it as actioned and silently discard it.
- A `VERIFIED` verdict with steering still pending is deferred: one more `worker`
  iteration actions it, then the review is re-run. Nothing reads pending steering
  after a terminal state, so going terminal there would strand it forever.
- Every terminal write records `unconsumedSteering`, so stranded steering is
  visible from `status.json` alone.

Pause takes effect at iteration boundaries only: `pause()` clears an event that
`_gate()` awaits before each iteration, so the running agent finishes undisturbed,
and `resume()` releases it. Interrupt is `SIGINT` to the agent's process group —
the subprocess runs in its own session precisely so the signal cannot hit the
engine — and ends the current iteration and nothing else. Abort is sticky:
`abort(reason)` records the reason, interrupts the agent, and makes
`budget_left()` false from then on, so every phase's existing "out of budget" exit
path unwinds the run to terminal `aborted` with `reason` set. `SIGTERM`/`SIGINT`
to the engine itself become `abort_on_signal(sig)` — the same unwinding, but a
different *termination class* (below). Shutdown follows `on_complete`:
`exit` tears the container down, `idle` keeps the API alive until the container is
stopped or `POST /shutdown` arrives — an explicit debugging opt-in.

**Operator-initiated termination is recorded as such, and is never retried or
resurrected.** Three mechanisms enforce it:

- `abort()` writes `operator-termination.json` *before* the loop unwinds, so a
  container killed mid-abort still leaves the operator's intent on disk instead
  of looking exactly like a crash.
- `classify_fault(operator_abort=True)` can never return `"infra"`, so no retry
  episode fights an operator who asked the run to stop — the agent reports an
  operator `SIGINT` with the same bare error text a provider-side stream abort
  produces, and only this flag tells them apart.
- Host-side auto-resume buckets a run with an **operator-class** marker as
  `operator_terminated` and refuses to resume it.

**A self-inflicted termination is a different class, and is recoverable.** The
marker carries `class`: `operator` when the abort was claimed through
`POST /abort` (`ralphctl abort`, `ralphctl stop`, the hub's button), and
`self-inflicted` when a signal reached the engine with no such request behind it
— the agent `pkill`ing its own supervisor from inside the container being the
motivating case. A process cannot see who signalled it, so the class is decided
by **attribution, not provenance** (`state.TERMINATION_CLASS_SELF`); a marker
with no `class` field at all (every marker written before v0.7) reads as
`operator`, the only default that cannot resurrect a run somebody killed on
purpose. The self-inflicted record also carries `evidence` — the last tool call
before the signal, read back out of `iterations/NNNN/output.jsonl` — and a
`reason` that says all of that instead of a bare `signal 15`, both mirrored into
`status.json`'s `termination` field (§5.2) for every reader that is not
auto-resume. The residual: a raw `docker stop` by an operator who bypassed
`ralphctl` is indistinguishable from a self-inflicted signal and is therefore
resumable.

## 5. Data model

Everything about a run lives in two host directories: `~/.ralphd/runs/<run-id>/`
(mutable run state, mounted into the container as `/run/ralphd`) and
`~/.ralphd/configs/<run-id>/` (the start config, mounted read-only as `/config`).
Both hold plain files — JSON, JSONL and Markdown — readable with `jq` and a text
editor, with no database and no running server needed to interpret them. Every
JSON write is atomic (`atomic_write`/`atomic_write_json`: temp file in the same
directory, then rename), so a reader never sees a partial document and a crash
never leaves a truncated one; `events.jsonl` and the iteration transcripts are
strictly append-only.

### 5.1 Run directory

```
~/.ralphd/runs/<run-id>/
├── status.json              # the run's whole live state (§5.2)
├── tasks.json               # the plan and every task's status (§5.3)
├── events.jsonl             # append-only lifecycle log, monotonic ids (§5.4)
├── prd.md                   # the PRD exactly as handed to `ralphctl start`
├── composite-prd.md         # PRD + previous-approach history (§4.2), when written
├── notes.md                 # the agent's cross-iteration handoff notes
├── review-findings.md       # the reviewer's unmet-requirement list, when rejected
├── vigilant-verified.json   # engine-owned ids of tasks that passed verification
├── operator-termination.json # the operator's abort/stop intent (§4.6), when recorded
├── auto-resume.json         # host-side crash-loop guard bookkeeping
├── host.json                # host-side container/registry metadata
├── .tasks-last-good.json    # last `tasks.json` that parsed; written only on a failed read (§5.3)
├── .api-token               # the run's API bearer token, mode 0600
├── .lock                    # exclusive flock held by the live engine; holds its pid
├── steering/
│   ├── NNN-<name>.md        # one operator steering message
│   └── .consumed.json       # names already actioned by an iteration
├── iterations/
│   └── NNNN/                # one iteration (§5.5)
│       ├── meta.json        # what ran, how it ended, what it cost
│       ├── prompt.md        # the exact prompt the agent was given
│       └── output.jsonl     # the agent's raw NDJSON transcript
├── approaches/
│   └── NN/                  # archived tasks.json/notes.md/review-findings.md
└── artifacts/               # agent-produced deliverables; reflection/ for §4.1
```

Everything above is engine-written except `host.json`, `auto-resume.json` and
`.api-token`, which `ralphctl` writes on the host; `operator-termination.json` is
written by whichever side recorded the intent (its `source` field says which).
`steering/`, `iterations/`, `approaches/` and `artifacts/` are created when
`RunDir` is constructed, so they exist even for a run that never got anywhere.

The engine holds an exclusive non-blocking `flock` on `.lock` for its whole
lifetime and writes its pid there. A second engine over the same run dir fails to
start with a distinct exit code rather than corrupting the state; because it is
an flock, a `SIGKILL`ed engine leaves no stale lock behind.

### 5.2 status.json

The single answer to "what is this run doing". One document, patched field by field
(`update_status()` reads, merges, rewrites, and always refreshes `updatedAt`).

| field | type | meaning |
|---|---|---|
| `runId` | string | the run id |
| `state` | string | `starting` \| `running` \| `succeeded` \| `failed` \| `aborted` |
| `schemaVersion` | int | run-dir schema this state was written under (§5.7) |
| `health` | string | `ok` \| `degraded` — degraded while sitting out an infra outage |
| `infraWait` | object \| null | the backoff wait in progress: `since`, `attempt`, `error`, `phase`, `nextAttemptAt`, `waitedS`, `budgetS`, `remainingS`; `null` whenever nothing is being waited on |
| `infraWaitTotalS` | float | cumulative seconds this run spent in infra backoff waits |
| `phase` | string \| null | the phase running now; `null` between phases and once terminal |
| `iteration` | int | the current (raw) iteration number |
| `iterationsUsed` | int | charged iterations: raw attempts minus infra refunds |
| `iterationsBudget` | int | the current iteration budget, including in-flight top-ups |
| `currentIteration` | object \| null | `{number, phase, model, startedAt}` while an iteration runs, or `{phase, note}` while retrying after an infra fault; `null` otherwise |
| `approach` | int | the approach running now |
| `maxApproaches` | int | configured approach ceiling, so every surface can render `approach 2/3` rather than a bare number |
| `model` | string \| null | the model reference the run's iterations actually reported, `provider/model`; `null` until an iteration observes one |
| `modelRaw` | string \| null | the provider's own id when it differs from the resolved reference (a gateway-local spelling); `null` when it does not |
| `verdict` | string \| null | `verified` \| `unverified`; `null` until terminal |
| `reason` | string | why a non-succeeded run ended, or the grace-review note |
| `termination` | object \| null | present once the run was told to stop: `{class, action, at, signal, reason, evidence}`, where `class` is `operator` (someone asked through `POST /abort`) or `self-inflicted` (a signal reached the engine and nobody claimed it — §4.6). `evidence` is the last tool call before the signal (`{iteration, tool, args, transcript}`) or `null` |
| `graceReview` | bool | present and `true` when an off-budget grace review verified the run |
| `reflect` | object | `{ok, error, endedAt}` — the post-terminal reflect phase's own verdict; `error` is `null` when it produced a report, and a failure also leaves `artifacts/reflection/FAILED.md`. `ok: null` with `{attempted, skipped}` instead means a signal was already taking the engine down, so the phase produced no verdict and no tombstone was written (§8.4) |
| `unconsumedSteering` | array | steering files still pending at the terminal write |
| `onComplete` | string | `exit` \| `idle` |
| `usage` | object | token and cost totals, plus `byPhase` and `byApproach` buckets |
| `createdAt` | string | UTC ISO-8601, first status write |
| `startedAt` | string | when the loop entered `running` |
| `endedAt` | string | terminal write |
| `updatedAt` | string | last write of any field |
| `deadlineAt` | string | wall-clock deadline, extended by every infra wait |

Cost fields deserve their own contract, because **a missing price is not a
price of zero**:

| field | where | meaning |
|---|---|---|
| `costUSD` | iteration usage and every bucket | money the provider actually quoted |
| `costPriced` | iteration usage | `false` when tokens were billed that the provider quoted no usable price for |
| `costZeroQuoted` | iteration usage | `true` when the provider quoted exactly `0` for non-zero billable tokens — an *implausible zero*, recorded as unknown rather than as free (§8.6) |
| `costFree` | iteration usage and buckets | `true` only when a route the operator **declared** free priced this traffic, which is the one way a `$0.00` over billable tokens is believed |
| `costDerivedUSD` / `costDerived` | iteration usage and buckets | money computed from a rate table (operator map or built-in), kept in its own field so it can never be summed with a quoted price |
| `costStatus` | buckets only | absent (fully priced) \| `partial` (`costUSD` is a lower bound) \| `unknown` (no price at all) \| `derived` |
| `costDisplay` | added by readers | the rendered string from `state.format_cost()` — e.g. `unavailable`, `$0.56+ (partial, rest unavailable)`, `~$0.45 derived` |

`costDisplay` is not stored: the hub's server attaches it next to the raw numbers
so every surface words an unknown cost identically. `autoResume` is likewise not
engine-written — the host merges `{attempts, lastAt, maxAttempts}` (plus
`iterationsUsed`, `gaveUp`, `reason`) from `auto-resume.json` into the status
payload it serves, and the `tasks` counts and `steering: {pending, consumed}`
summary are synthesized from `tasks.json` and `steering/` on read — the counts
through the hardened reader of §5.3, which also attaches its
`tasksStale`/`tasksSource` contract beside them.

`model` is the model *observed* in an iteration's own `message_end`, not the
reference the engine requested: the requested one is `null` exactly when the
agent picks its own model, which is the case a reader most needs an answer for.
It is written only when an iteration observes one, so a zero-traffic attempt
cannot erase a known id, and the per-iteration ids stay in `meta.json` (§5.5)
while `status.json` answers "which model is this run using".

```json
{
  "runId": "v05-smoke", "state": "succeeded", "schemaVersion": 1,
  "health": "ok", "infraWait": null, "infraWaitTotalS": 0.0,
  "createdAt": "2026-08-19T16:43:46Z", "startedAt": "2026-08-19T16:43:46Z",
  "deadlineAt": "2026-08-19T17:03:46Z", "endedAt": "2026-08-19T16:45:24Z",
  "updatedAt": "2026-08-19T16:45:24Z",
  "iterationsBudget": 8, "maxApproaches": 2, "onComplete": "exit",
  "verdict": "verified", "approach": 1, "phase": null,
  "iteration": 5, "iterationsUsed": 5, "currentIteration": null,
  "usage": {
    "input": 54, "output": 6027, "cacheRead": 136143,
    "cacheWrite": 20434, "totalTokens": 162658, "costUSD": 0,
    "byPhase": {
      "planning": {"totalTokens": 19204, "costUSD": 0},
      "worker":   {"totalTokens": 124960, "costUSD": 0},
      "review":   {"totalTokens": 18494, "costUSD": 0}
    },
    "byApproach": {"1": {"totalTokens": 162658, "costUSD": 0}}
  },
  "unconsumedSteering": []
}
```

A run sitting out a gateway outage keeps `state: "running"` — adding a
`degraded` state value would break every consumer's terminal-state logic —
and carries the condition in the two dedicated fields instead:

```json
{
  "state": "running", "health": "degraded", "infraWaitTotalS": 7.0,
  "infraWait": {
    "since": "2026-08-19T18:02:11Z", "attempt": 3, "phase": "worker",
    "error": "no LLM traffic within startup window",
    "nextAttemptAt": "2026-08-19T18:02:26Z",
    "waitedS": 7.0, "budgetS": 14400.0, "remainingS": 14393.0
  },
  "currentIteration": {"phase": "worker",
                       "note": "retrying after infra fault (attempt 3, next in 15s): ..."}
}
```

### 5.3 tasks.json

Written by `planning`, mutated by `worker` and `verify`, read by everything: the
agent-facing source of truth for what is left to do. The engine only ever adds
its own bookkeeping fields to it.

| field | type | meaning |
|---|---|---|
| `version` | int | plan schema version (`1`) |
| `goal` | string | one-line goal distilled from the PRD |
| `scope` | object | `{level: no-repo\|single-repo\|multi-repo, reasoning}` |
| `repositories` | array | repositories the plan works in |
| `discovered` | object | facts the planner learned about the workspace |
| `tasks[]` | array | the plan, in intended execution order |
| `tasks[].id` | string | zero-padded ordinal, e.g. `"001"` |
| `tasks[].title` | string | imperative, one deliverable |
| `tasks[].status` | string | `pending` \| `in-progress` \| `completed` \| `validation-failed` \| `failed` \| `skipped` |
| `tasks[].successCriteria` | string | independently checkable, in natural language |
| `tasks[].priority` | int | optional scheduler hint, missing means `0` |
| `tasks[].dependsOn` | array | optional task ids that must be `completed` first |
| `tasks[].validationAttempts` | int | failed verification verdicts so far; `3` forces `failed` |
| `tasks[].validationNotes` | string | what the verifier observed |
| `tasks[].criteriaFingerprint` | string | engine-written sha256 of `successCriteria` |
| `tasks[].criteriaEditedAfterValidationFailure` | bool | engine-written, sticky (§4.4) |

The worker picks the first `validation-failed` task, else the first
`in-progress` one, else the first `pending` task whose `dependsOn` are all
`completed`, breaking ties by highest `priority` then list order. A plain
linear plan omits both optional fields entirely.

```json
{
  "version": 1, "repositories": [], "discovered": {},
  "goal": "Make infra faults never cost the job an iteration",
  "scope": {"level": "single-repo", "reasoning": "one python package"},
  "tasks": [
    {"id": "001", "status": "completed", "priority": 0,
     "title": "Classify a non-empty error_message as a failure regardless of exit code",
     "successCriteria": "classify_fault() no longer returns None when error_text is non-empty and exit_code is 0; new tests in tests/test_fault_classifier.py green.",
     "criteriaFingerprint": "adda90a2529488a8138181f79e1f49964451e6e73facbcdf4ba726f29bcb786f"}
  ]
}
```

Task status counts are derived, never stored: `state.task_counts()` maps statuses
to the published keys (`in-progress` → `inProgress`, `validation-failed` →
`validationFailed`) in one place, so the engine and the host-side fallback can
never disagree about the same file.

**Reading it: unknown is not zero.** This file is written by the *agent*, with
whatever atomicity its tooling happens to have, and a plan is rewritten many
times per iteration. A reader that catches `JSONDecodeError` and falls back to a
default therefore reports `0/0 tasks` for a run with a full plan, for as long as
the write window lasts — a lie the engine cannot fix in the writer, only in the
read path. So there is exactly one read path, `state.read_tasks_doc()`, returning
a `TasksRead` whose `source` distinguishes the four cases that used to collapse
into one:

| `tasksSource` | means | `tasksStale` |
|---|---|---|
| `absent` | no `tasks.json` yet — an empty plan really is the truth | `false` |
| `file` | parsed straight off disk, empty plan included | `false` |
| `last-good` | it would not parse; this is the last payload that did | `true` |
| `unreadable` | it would not parse and nothing ever did — ignorance, not an empty plan | `true` |

A parse failure is re-read a bounded number of times first
(`TASKS_READ_ATTEMPTS` attempts `TASKS_READ_DELAY` apart, ~30ms worst case),
which is longer than an agent's rewrite and shorter than any request budget.
The last-good payload is held in memory and mirrored to
`<run-dir>/.tasks-last-good.json` **only at the moment a read actually fails**,
never on the happy path: the file exists so the fallback survives an engine
restart, not as a second copy of the plan that could drift from `tasks.json` or
be mistaken for it. A read-only viewer of somebody else's run dir passes
`persist=False` and writes nothing at all.

`TasksRead.contract` — `{tasksStale, tasksSource}` — travels with every payload
that carries such a read: `GET /tasks`, `GET /status` (as siblings of the `tasks`
counts, never keys inside them, which summarisers iterate as statuses), the hub's
run payloads, `ralphctl tasks --json`. `tasksStale` is always present, so its
absence means "an older engine wrote this" and never "fresh", and
`TasksRead.notice` words the condition once for every surface. Progress cells are
rendered from the same read too (`TasksRead.row_fields`: raw counts plus
`tasksDisplay`/`tasksSummary`/`tasksTrouble`/`tasksColumn`), which is how
`ralphctl runs`' and the hub's `TASKS` columns cannot disagree — and why a
plan-less run shows an empty cell rather than `0/0`.

### 5.4 events.jsonl

Append-only, one JSON object per line, `id` monotonically increasing from 1 and
`ts` in UTC. `RunDir.emit()` assigns the id under a lock and scrubs known secret
values out of the line before it touches disk. The file is never rewritten or
rotated and ids continue across resumes, so a follower can replay from id 0 and
reconstruct the whole run, including its restarts.

| type | fields | emitted when |
|---|---|---|
| `state` | `state`, `resumed` | the loop enters `running` (with `resumed`), and once with the terminal state |
| `phase` | `phase`, `approach` | a `planning`/`worker`/`review` phase starts; `reflect` carries no approach |
| `iteration.start` | `number`, `phase`, `model` | an iteration's agent process is about to start |
| `iteration.end` | `number`, `phase`, `exitCode`, `interrupted`, `sawComplete`, `sawVerified`, `error`, `faultClass` | that iteration finished |
| `task` | `taskId`, `oldStatus`, `newStatus` | a task's status changed in `tasks.json` (polled while the agent runs, so it lands mid-iteration) |
| `signal` | `signal` (`COMPLETE` \| `VERIFIED` \| `taskVerified`), `taskId` | a sentinel was accepted |
| `steering.received` | `file` | a steering message was stored |
| `steering.consumed` | `file`, `iteration` | an actionable phase took it |
| `infra_wait` | the whole `infraWait` payload plus `backoffS` | a backoff wait starts |
| `infra_retry` | `phase`, `attempt`, `maxAttempts`, `error`, `noTrafficTimeout`, `instantFailure`, `backoffS`, `waitedS`, `budgetS` | an attempt classified as an infra fault |
| `infra_retry_now` | `phase`, `attempt`, `error`, `source`, `message` | the operator woke a backoff wait |
| `infra_recovered` | `health`, `infraWaitTotalS` | an iteration reached the model again |
| `deadline_extended` | `phase`, `attempt`, `waitedS`, `infraWaitTotalS`, `deadlineAt`, `reason` | a finished wait pushed the deadline out |
| `reflect_infra_delay` | `phase`, `delayS`, `error`, `budgetS` | reflect waits before its first attempt because the job just died on an infra fault |
| `reflect_done` | `ok`, `error` | the reflect phase's verdict |
| `reflect_skipped` | `attempted`, `signal`, `reason` | a signal was taking the engine down, so the reflect phase rendered no verdict (§8.4) |
| `budget_changed` | `field`, `previous`, `iterations`, `delta`, `iterationsUsed`, `source` | the iteration budget was changed in flight |
| `log` | `level`, `message` | anything the loop wants an operator to see: stagnation, batching violations, instant-failure streaks, abort diagnostics |
| `repair` | `action`, plus the names/ids involved | appended host-side by `ralphctl repair`, which records only names, never values |

```json
{"id": 1, "ts": "2026-08-18T18:01:26Z", "type": "phase", "phase": "planning", "approach": 1}
{"id": 2, "ts": "2026-08-18T18:01:26Z", "type": "iteration.start", "number": 1, "phase": "planning", "model": "amazon-bedrock/eu.anthropic.claude-opus-5"}
{"id": 3, "ts": "2026-08-18T18:05:40Z", "type": "task", "taskId": "001", "oldStatus": null, "newStatus": "pending"}
```

### 5.5 Iteration records

`iterations/NNNN/meta.json` is written twice: once before the agent starts (so a
killed iteration still says what it was doing) and once when it ends.

| field | type | meaning |
|---|---|---|
| `number` | int | iteration number, matching the directory name |
| `phase` | string | `planning` \| `worker` \| `verify` \| `review` \| `reflect` |
| `model` | string \| null | the model reference the engine *requested*; `null` means the agent's own default |
| `modelResolved` | string \| null | the `provider/model` reference the iteration's own messages reported — the answer when `model` is `null` |
| `modelRaw` | string \| null | the provider's own id, when it differs from the resolved reference |
| `approach` | int \| null | the approach this iteration belongs to |
| `startedAt` / `endedAt` | string | UTC ISO-8601; a missing `endedAt` means the iteration never finished |
| `steeringConsumed` | array | steering file names this iteration took |
| `exitCode` | int \| null | agent process exit code; negative for a signal, `null` if it never spawned |
| `interrupted` | bool | ended by `SIGINT` (operator, timeout or watchdog) |
| `timedOut` | bool | the full per-iteration timeout fired |
| `noTrafficTimeout` | bool | the startup watchdog fired: zero observed agent traffic |
| `sawComplete` / `sawVerified` | bool | sentinel found in the final assistant text |
| `error` | string \| null | the agent's own error text, or the engine's |
| `faultClass` | string \| null | the engine's verdict: `null` (no failure), `"infra"` or `"work"` |
| `usage` | object | this iteration's tokens and cost markers |
| `verifiedTask` | string | verify iterations only: the task under verification |
| `verifyOutcome` | string | verify iterations only: `pass` \| `fail` \| `error` |

`faultClass` sits next to the raw signals it was derived from, so an operator can
see *why* an attempt was retried and refunded without re-deriving the
classification from exit codes and token counts.

```json
{
  "number": 1, "phase": "planning", "approach": 1,
  "model": "amazon-bedrock/eu.anthropic.claude-sonnet-5",
  "startedAt": "2026-08-19T16:43:46Z", "endedAt": "2026-08-19T16:44:08Z",
  "steeringConsumed": [], "exitCode": 0, "interrupted": false,
  "timedOut": false, "noTrafficTimeout": false,
  "sawComplete": false, "sawVerified": false,
  "error": null, "faultClass": null,
  "usage": {"input": 8, "output": 1774, "cacheRead": 11991,
            "cacheWrite": 5431, "totalTokens": 19204,
            "costUSD": 0, "costPriced": true}
}
```

`output.jsonl` beside it is the agent's raw NDJSON transcript, appended as the
process streams (`message_start`, `message_update`, `message_end`,
`turn_start`/`turn_end`, `tool_execution_*`, `agent_start`/`agent_end`, ...).
Lines are large — full message snapshots — so the stream reader is configured for
16 MiB lines, and every line is scrubbed of known secret values before it is
written.

**`src/ralphd/log_merge.py` is the single reader of these files.** A run's
transcript is not one file, so `merge_entries()` concatenates every iteration's
`output.jsonl` in order and synthesizes a `ralphd.iteration` boundary line
(`event: "start"` / `"end"`, carrying `number`, `phase`, `model`, `approach`,
`startedAt`, and on the end boundary `exitCode`, `error`, `usage`, `endedAt`)
around each one. That boundary is derived from `meta.json` and never stored; the
end boundary appears only once `endedAt` exists, so a live iteration renders
open-ended, and an iteration with an unreadable `meta.json` is skipped rather
than half-rendered. Both the engine's log route and the host-side CLI and hub
call this one module, which is why they emit byte-identical lines for the same
run dir. Scrubbing is injected rather than assumed: the engine re-scrubs on
serve, a host-side reader gets the bytes as written.

### 5.6 The persisted start config

`~/.ralphd/configs/<run-id>/` is everything the run was started *with*, separated
from everything it has *done*, and is mounted read-only at `/config`.

```
~/.ralphd/configs/<run-id>/
├── job.yaml           # the JobConfig document: budgets, flags, models, pricing
├── prd.md             # the PRD, copied into the run dir on first start
├── creds/             # *.env credential files, placed at ~/.creds in-container
├── skills/            # skill directories made available to the agent
├── pi/                # agent configuration, e.g. models.json
├── prompts/           # optional operator overrides of the phase prompts
├── llm-wiring.json    # resolved LLM env + mounts for this run, mode 0600
├── env-wiring.json    # resolved --forward-env/--llm-env/--env pairs, mode 0600
└── auto-resume.json   # the start-time auto-resume opt-in
```

**This directory is what `resume` re-reads.** A resume replaces the container,
never the config dir, so the run continues under exactly the configuration it
started with. That only works because the *resolved* wiring was recorded rather
than re-derived: `llm-wiring.json` holds the LLM `mode`, the resolved `env` map
and the `mounts` list, and `env-wiring.json` holds the `extra_env` list of
resolved `name=value` pairs in the exact order the flags were applied at start
(forward-env, then llm-env, then env), so a later duplicate name wins on replay
exactly as it did the first time. Both are mode `0600`, live outside the run dir
and are served by no route; only their key *names* are ever surfaced.

Because `/config` is read-only in a real container, runtime configuration changes
go to a container-local writable overlay instead (`overlay_or_config()`: overlay
first, then `/config`, then the built-in default). Nothing in the overlay
survives the container, which is the point — `job.yaml` and the wiring files stay
the record of how the run was started.

### 5.7 Schema version and migration

`status.json` carries a `schemaVersion`; the engine build carries
`CURRENT_SCHEMA_VERSION` (`1`). At startup, after taking the run-dir lock and
before touching anything else, `RunDir.check_schema_version()` reads the recorded
version — treating an absent one as `0` — and refuses to start when it is *newer*
than the build knows, exiting with a distinct documented code and touching
nothing on disk. An older engine must never interpret a newer run dir by
guessing.

Reading in the other direction is a compatibility requirement, not a migration
step: a build reads any run dir recorded at or below its own version, directly and
without rewriting it. A field this contract lists may be absent from such a run
dir, and readers treat that absence as "not known for this run" rather than as a
third state. `health` and `infraWait`, the two that would otherwise force a third
case, are written on the very first status write of every run, so a consumer of a
live run never has to.
## 6. Job configuration

A job's configuration is a directory on the host — `<registry>/configs/<run-id>/`
(default registry `~/.ralphd`, `$RALPHD_REGISTRY` overrides it) — bind-mounted
**read-only** at `/config` in the job container. `ralphctl start` materialises it
once; every later `ralphctl resume` reuses the same directory over a fresh
container, which is why the config dir, not the container and not the operator's
shell, is the durable record of how a job was wired.

```
<registry>/configs/<run-id>/     ->  /config  (ro)
├── prd.md              the brief, verbatim as handed to `start --prd`
├── job.yaml            budgets, flags, model tiers, retry policy, pricing
├── prompts/<name>.md   optional phase-prompt overrides (operator-placed)
├── skills/<name>/      one directory per skill, each with a SKILL.md
├── creds/              <name>.env files + recognized extras
├── pi/                 pi provider config (models.json, settings.json, auth.json)
├── llm-wiring.json     resolved `--llm` env + mounts        (0600, host-only)
├── env-wiring.json     resolved --forward-env/--llm-env/--env (0600, host-only)
└── auto-resume.json    the run's `auto_resume` opt-in       (plain, host-only)
```

The last three are **host-side only**: `ralphctl` reads them, nothing in the
engine opens them, and no HTTP route returns them. Everything else is job input
the engine reads at startup and — for prompts, skills and creds — again on every
iteration.

Because `/config` is read-only, runtime mutations pushed through the API land in a
container-local **writable overlay**, `$HOME/.ralphd/config-overlay/` by default
(`RALPHD_CONFIG_OVERLAY_DIR`), and every config-relative read resolves in one
order: overlay → `/config` → builtin default. The overlay disappears with the
container, which is deliberate — it can never mutate the operator's input, and it
is never where a secret becomes durable.

### 6.1 The PRD

The PRD is the job's contract with the agent, and it carries three things — a weak
PRD is weak in one of them:

- **The brief** — what to build, against which workspace, at which commit.
- **The standing rules** — git policy, credential handling, testing bar, what the
  agent may not touch, what is out of scope. Re-read by every iteration; the only
  project-specific policy the loop has.
- **The definition of done** — the checkable conditions under which review may
  return `VERIFIED`. Without them, review has nothing to hold an approach against.

`ralphctl start --prd <file|->` reads the file (or stdin) and writes it
**verbatim** to `<config-dir>/prd.md`; the engine copies it once to
`<run-dir>/prd.md` at startup if that file does not already exist, so a resume
keeps the run dir's copy. Every phase prompt names the PRD's path rather than
inlining it, and the agent reads the file. From the second approach onward the
prompt points at `<run-dir>/composite-prd.md` instead — the PRD plus the history
of rejected approaches, engine-generated, never an authored input.

**Operator prohibitions belong in the PRD**, not in the prompts, which are generic
and shared by every job. The PRD is where "do not `git push`", "do not touch the
host user's `~/.ralphd`" and "no hardcoded endpoints or org names in code or
examples" live. The most important class is about the engine itself, because the
agent runs inside it:

> **You are running INSIDE a ralphd engine.** The engine driving this job is
> PID 1 of this container; its live run dir is `/run/ralphd`, its config is
> `/config`. NEVER kill by pattern — `pkill`/`killall` matching
> `ralphd-engine` (or anything broad) SIGTERMs PID 1 and aborts this job.
> Kill only specific PIDs of processes you started. Any engine you launch for
> testing must get `RALPHD_RUN_DIR`, `RALPHD_CONFIG_DIR`,
> `RALPHD_WORKSPACE_DIR`, `RALPHD_PORT` pointed at temp dirs.

The engine has its own self-protection — argument parsing with no side effects, an
exclusive `flock` on the run dir — but the prohibition is what stops the agent
trying in the first place; a job's own verifier killing its engine by pattern is
an observed failure, not a hypothetical one. `docs/prds/` holds the real,
byte-identical PRDs ralphd was built with, including a pair that differ only by
these rules.

### 6.2 job.yaml

`job.yaml` is the engine's whole scalar configuration. `ralphctl start` composes
it from flags, template and registry defaults and writes it once; the engine
loads it with `JobConfig.load()` (`engine/config.py`), which is `yaml.safe_load`
plus a per-field env override pass. `ralphctl`'s writer emits one
`key: <json-value>` line per non-null field, which is why a real file looks like
this:

```yaml
run_id: "est6534-impl-phase3-models"
iterations: 500                 # shared budget across planning/worker/review/verify
max_approaches: 40              # review may reject and restart this many times
vigilant: true                  # verify every completed task before the job can pass
on_complete: "exit"             # tear the container down at a terminal state
reflect: true                   # one extra `reflect` iteration after the verdict
model: "amazon-bedrock/eu.anthropic.claude-opus-5"          # strong tier
fast_model: "amazon-bedrock/eu.anthropic.claude-sonnet-5"   # fast tier
model_strategy: "balanced"      # strong for planning + review, fast elsewhere
thinking: "high"                # passed to pi as --thinking
iteration_timeout_s: 3600
job_timeout_s: 86400
```

A hand-written or template `job.yaml` is ordinary YAML (comments and block
structure are fine) — the JSON-per-line shape is just what the writer produces.
Every field:

| key | type | default | meaning |
|---|---|---|---|
| `run_id` | string | `unnamed-run` | run identity; echoed in status, events and the `on_complete_cmd` env |
| `iterations` | int | `25` | total iteration budget shared by all phases; infra-fault retries are refunded and cost none of it |
| `max_approaches` | int | `3` | how many times review may reject an approach and have the loop start a new one |
| `vigilant` | bool | `false` | verify each `completed` task against its `successCriteria` before the job may reach a verdict |
| `on_complete` | `exit` \| `idle` | `exit` | what the engine does at a terminal state; `idle` keeps the API up for debugging and is an explicit opt-in |
| `on_complete_cmd` | string \| null | `null` | shell hook run once, in-container, after the terminal state, with `RALPHD_RUN_ID`/`RALPHD_STATE`/`RALPHD_VERDICT` set; a nonzero exit is logged as an `error` event and changes neither the verdict nor the engine's exit code |
| `reflect` | bool | `false` | run exactly one extra `reflect` iteration after the verdict, writing a report and an optional prompt/skill diff to `artifacts/reflection/`; outside the budget gate, so it runs even on an exhausted budget |
| `job_timeout_s` | int | `28800` (8h) | wall-clock limit for the whole job |
| `iteration_timeout_s` | int | `2700` (45m) | wall-clock limit for one iteration |
| `model` | string \| null | `null` | strong-tier pi model ref (`provider/model-id`); `null` means pi's own default |
| `fast_model` | string \| null | `null` | fast-tier ref; falls back to `model` when unset |
| `model_strategy` | string | `quality-first` | which tier each phase gets (table below); an unrecognised value silently resolves as `quality-first` |
| `model_overrides` | map phase→ref | `{}` | per-phase model, beating the strategy; keys are `planning`, `worker`, `review`, `verify`, `reflect`. `job.yaml`-only — no `start` flag sets it |
| `thinking` | string \| null | `null` | pi `--thinking` level, e.g. `medium`, `high` |
| `api_token` | string \| null | `null` | bearer token the API requires; `RALPHD_API_TOKEN` (how `start --api-token` delivers it) overrides |
| `infra_startup_timeout_s` | float | `150.0` | how long a phase may run with **zero** parseable pi events before the engine kills it as an infra fault instead of waiting out `iteration_timeout_s` |
| `infra_retry_backoff_s` | list[float] | `[2, 5, 15, 30, 60, 120, 300]` | sleep before each retry of the same phase after an infra-classified failure; the last value repeats. Fast at the start on purpose — a 30-second gateway blip should cost seconds, not minutes |
| `infra_retry_backoff_max_s` | float | `300.0` | cap on one wait, so the repeating tail polls at a 5-minute cadence through a long outage |
| `infra_retry_max` | int \| null | `null` | back-compat attempt cap, honoured **only** when set explicitly; `null` means "no cap — retry as long as the outage budget allows" |
| `infra_outage_budget_s` | float | `14400.0` (4h) | the real stopping rule: retries continue while one fault episode's accumulated wait stays under this. The episode clock resets on any successful iteration |
| `pricing` | map | `{}` | host-side rate table, consulted only when the provider quoted no price (§7.3) |
| *(anything else)* | — | — | unknown keys are preserved on `JobConfig.extra`, never rejected, and never surfaced by `GET /config` (they may contain operator-supplied secret-shaped values) |

Strategy tiers (`JobConfig.STRATEGY_TIERS`); `reflect` deliberately mirrors
`review`, being the same kind of post-hoc analysis:

| strategy | planning | worker | review | verify | reflect |
|---|---|---|---|---|---|
| `quality-first` (default) | strong | strong | strong | strong | strong |
| `cost-optimized` | strong | fast | fast | fast | fast |
| `balanced` | strong | fast | strong | fast | strong |

Seven fields also take an env override at engine load, so a run (or a test) can be
retuned without editing `job.yaml`: `RALPHD_API_TOKEN`,
`RALPHD_INFRA_STARTUP_TIMEOUT`, `RALPHD_INFRA_RETRY_BACKOFF_S` (comma-separated
seconds), `RALPHD_INFRA_RETRY_BACKOFF_MAX_S`, `RALPHD_INFRA_RETRY_MAX`,
`RALPHD_INFRA_OUTAGE_BUDGET_S` and `RALPHD_PRICING` (JSON of the same shape as
`pricing:`; invalid JSON logs a warning and is ignored).

**Precedence, stated once.** For every scalar `start` can set:

1. an explicit flag on `ralphctl start`;
2. the `--template`'s `job.yaml` value (§6.7);
3. the registry default in `<registry>/config.yaml` — only for the eight keys
   that have one (`ralphctl config set`): the three image supply keys `image`,
   `base_image` and `dockerfile`, plus `on_complete`, `network`, `auto_resume`,
   `default_llm_profile` (the fallback for `--llm`) and `price_strategy`;
4. the hardcoded default in `ralphctl` (`_TEMPLATE_SCALAR_FIELDS`).

The winner is written into `job.yaml`, and **only non-null keys are written** — an
absent key means the engine's own `JobConfig` default applies, so every default
lives in `engine/config.py` alone and is never duplicated host-side. Inside the
container, `job.yaml` loses to the `RALPHD_*` overrides above and to nothing else.
Two edits happen after the fact: `ralphctl resume --iterations +N` rewrites
`job.yaml` on the host before the new container starts, while `ralphctl budget
<run-id> +N` changes only the **live** engine's counter — `/config/job.yaml` is a
read-only mount, so making an increase outlive the container needs the resume form.

`GET /config` reports the effective view — budgets, flags, model block, resolved
pricing table — and deliberately excludes `api_token` and `extra`.

### 6.3 Workspaces

`--workspace` decides what code the agent sees, and there are three shapes.

| form | mount | recorded in `host.json` | container env |
|---|---|---|---|
| none | `/workspace` is the image's own directory: container-local, gone with the container | — | — |
| `--workspace DIR` (one, unnamed) | `DIR` → `/workspace` | `workspace: <host path>` | — |
| `--workspace DIR:NAME` (repeatable) | each `DIR` → `/workspace/<NAME>` | `workspaces: {NAME: <host path>}` | `RALPHD_WORKSPACES=<names, comma-separated>` |

A name must match `[A-Za-z0-9_.-]+`; the spec is split on the **last** colon, so
`~/src/api:api` works and a path containing a colon is left alone. A single
*named* workspace is legal; but as soon as there are two or more entries,
**every** one needs a name (exit `2` otherwise) — there is no "first at the root,
the rest named" mode, because then the agent could not tell which repo is which
from a directory listing. It never has to guess:
`LoopSupervisor._workspace_note()` puts the mounted set — one line, or a
name→path list — into every phase prompt's job-context block.

Multi-workspace exists for the unit of work that spans repositories which must
change together: an API and its client, a library and its consumer, a spec repo
and the implementation it governs. One job per repo cannot make that change,
because neither job can see the other's tree or run the cross-repo test.

Two consequences worth designing for. With `--allow-docker`, sibling containers
run on the *host* daemon, so their `-v` sources must be host paths: `ralphctl`
injects `RALPHD_HOST_WORKSPACE` for the single case and `RALPHD_HOST_WORKSPACES`
(a JSON name→path object) for the multi case, and the prompt lists them per name.
And `ralphctl resume` remounts from `host.json` verbatim, so a resumed run never
needs `--workspace` repeated — nor can it be quietly pointed somewhere else.

### 6.4 Prompts

One prompt file per phase ships in the image (`src/ralphd/prompts/`:
`planning.md`, `worker.md`, `review.md`, `task-verify.md`, `reflect.md`;
`RALPHD_PROMPTS_DIR` relocates the builtin set). Each iteration composes its
prompt fresh, in `LoopSupervisor.build_prompt()`:

1. the phase's prompt text, resolved overlay → `/config/prompts/<name>.md` →
   builtin (`prompt_text()`, evaluated per iteration, so an override never
   requires a restart);
2. a `## Job context` block: run state directory, the workspace note (§6.3), and
   the paths of the PRD, `tasks.json`, the handoff notes and the artifacts dir;
3. a `## Docker siblings` section, only when `--allow-docker` set the
   `RALPHD_HOST_*` vars — the host-path rule, the two labels every sibling must
   carry (`ralphd.run=<run-id>` **and** `ralphd.role=sibling`), and why a cleanup
   sweep must filter on both;
4. a `## Credentials` section, only when `~/.creds/*.env` is non-empty (§7.4);
5. pending operator steering — as a MUST-take-priority block in phases that may
   consume it, or as an explicitly read-only "not for this phase" note in
   review/verify, so a non-actionable phase cannot swallow it;
6. the phase's own `extra` (the task under verification, the review findings, and
   so on).

The composed text is written to `<run-dir>/iterations/<n>/prompt.md`, so the
exact prompt of any iteration is recoverable after the fact.

Overrides come from two places. An operator-provided file at
`<config-dir>/prompts/<name>.md` reports as source `mounted`; a runtime
`ralphctl prompts <run-id> set <phase> <file>` uploads the raw text into the
overlay and reports as `api`. `ralphctl prompts <run-id> ls` shows the effective
source per phase (`builtin` / `mounted` / `api`). The CRUD set is the four
loop phases (`planning`, `worker`, `review`, `task-verify`); `reflect` has a
builtin prompt and resolves through the same order, but is not accepted by the
CRUD route. An override takes effect the next time that phase builds a prompt and
is never retroactive to an in-flight iteration.

### 6.5 Skills

A skill is a directory containing `SKILL.md`. They are supplied per job and
explicitly — `ralphctl start --skills <dir>`, repeatable. There is deliberately
no "forward all host skills" mode: the operator picks exactly what a job needs.

`--skills` accepts either one skill directory (it has a `SKILL.md`) or a
directory of skills (every immediate child has one), in which case it expands to
the children; anything else is a usage error naming the path. Each is **copied**
into `<config-dir>/skills/<name>` at start, so later edits on the host cannot
change what a running job sees.

Inside the container the engine — not the entrypoint script — owns placement:
`engine/skills.py:place_skills()` runs at startup and again after every API
mutation, rebuilding `~/.pi/agent/skills/` from scratch each time as symlinks into
the effective sources. Two sources feed it, `mounted` (`/config/skills/<name>`)
and `api` (`<overlay>/skills/<name>`, always winning a name collision), with
`<overlay>/skills-deleted/<name>` tombstones so a deleted mounted skill is not
resurrected on the next rebuild. That the rebuild is cheap and idempotent is what
makes runtime CRUD possible without a container restart: `ralphctl skills <run-id>
ls | get <name> <dest> | add <dir> | rm <name>` (tar bodies over the API) is live
for the very next iteration.

Skills are advertised to the agent by pi's own discovery of `~/.pi/agent/skills/`,
not by the job-context block — which is why a skill's `SKILL.md` front matter
(`name`, `description`) is what decides whether it gets used.
`examples/skills/toolchain-sibling/` is the shipped example: a description written
to fire on a missing-toolchain situation, plus a copy-pasteable `run.sh`.

### 6.6 Environment wiring

Three `start` flags put environment variables into the container, applied in this
order — so a later duplicate name wins, exactly as `docker run -e` semantics
dictate:

| flag | resolution | typical use |
|---|---|---|
| `--forward-env NAME` \| `PREFIX_*` | read from the host env at `start`; an exact name that is unset prints a warning and forwards nothing; a trailing-`*` pattern forwards every matching var | non-standard credentials and endpoint overrides |
| `--llm-env KEY=VAL` | literal, layered on top of the `--llm` profile | one-off endpoint/key tweak |
| `--env KEY=VAL` | literal | anything the job itself needs |

All three exist rather than a longer built-in allowlist because of one design rule:
**no environment-specific variable is ever baked into ralphd code.** `--llm host`
forwards exactly ten standard, vendor-documented names (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `AWS_REGION`,
`AWS_DEFAULT_REGION`, `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`) and nothing else; everything past that — gateway bearer
tokens, endpoint overrides, SDK knobs — is the operator's business, per job.

**Forward related variables together.** A bearer token whose endpoint-override
variable is left behind gets sent to the vendor's real endpoint and rejected (the
observed failure: `AccessDeniedException: Invalid API Key format`). A prefix glob
(`--forward-env 'AWS_*'`) is the safe way to keep a family intact.

Everything resolved at `start` is persisted per run, so a `resume` reproduces it
byte-for-byte whatever the resuming shell happens to hold:

| file (in the config dir) | contents | mode |
|---|---|---|
| `llm-wiring.json` | `{mode, env, mounts}` from the `--llm` resolution — `host`'s forwarded vars and `~/.aws` mount, or a named profile's fully-resolved `env:`/`mounts:` including `${env:}`/`${file:}`/`${cmd:}` results | `0600` |
| `env-wiring.json` | `{extra_env: [...]}` — the resolved `name=value` pairs from the three flags above, in the exact order applied | `0600` |
| `auto-resume.json` | `{auto_resume: bool}` — a host-side setting, never passed into the container | plain |

`resume` replays `llm-wiring.json`'s env and mounts first, then
`env-wiring.json`'s pairs, matching `start`'s own precedence. A run whose config
dir has no such file resumes with nothing extra and no error. Neither secret file
is under the run dir (host-visible history) and neither is returned by any HTTP
route — the same at-rest pattern as `<config-dir>/pi/models.json` and
`<run-dir>/.api-token`.

**`ralphctl repair <run-id> --env KEY=VAL`** is the sanctioned editor for
`env-wiring.json` — the alternative being a hand-edit of a `0600` JSON file, done
live, when a resumed run comes up with no LLM credentials at all. It replaces an
existing key in place (preserving key order), appends a new one, keeps the mode,
and never echoes the value: stdout, `--json` (`{"runId", "action": "env",
"keys"}`) and the `type: repair` audit line in `events.jsonl` carry key **names**
only. A malformed argument (no `=`) exits `2` with nothing written; a run whose
container is still alive is refused with exit `5`, since a live engine owns that
run. The next `resume` carries the value in like any other recorded wiring.

Mid-run, the container's LLM environment is rotated through `PUT /config/llm`
instead: the `env` object *replaces* the whole override set, written to
`<overlay>/llm/env.json` (`0600`) and re-read by `engine/llm.py:current_env()`
before every iteration, while an optional `pi` fragment is deep-merged into
`~/.pi/agent/models.json` immediately, so rotating one provider's key does not
wipe out the rest of what a profile placed there. Neither destination is
host-visible.

### 6.7 Templates

A job template is a directory `<registry>/templates/<name>/` that bundles the
recurring parts of a `start` invocation:

```
templates/<name>/
├── job.yaml      # scalar job defaults (below)
├── prd.md        # PRD skeleton, used when --prd is omitted
├── creds/        # default --creds directory (same env-file convention)
└── <skill-dir>/  # default skill(s), named in job.yaml's `skills:` list
```

`start --template <name>` fills in every flag the caller left unset. The template's
`job.yaml` may set `iterations`, `max_approaches`, `vigilant`, `reflect`,
`on_complete`, `on_complete_cmd`, `timeout` and `iteration_timeout` (both in
**minutes**, defaults `480` and `45`), `model_strategy`, `llm`, `model`,
`fast_model`, `thinking`, `price_strategy`, `network` and `auto_resume`, plus the
three image supply keys `image`, `base_image` and `dockerfile` (settled as one
unit, §3.2), plus three
path-valued keys resolved relative to the template directory: `skills` (a list of
directory names), `creds` (one directory name) and `prd` (a filename, default
`prd.md`).

Every one of those has a corresponding `start` flag, and the flag always wins
(§6.2's chain). `--skills` and `--creds` override **wholesale**, not by merge — a
template's set and an explicit one never mix. An unknown `--template` exits `3`
naming the path it looked for; a `job.yaml` that is not a mapping exits `2`; a
template with no `job.yaml` contributes only its `prd.md`/`creds/`/skill
directories, and every scalar falls back down the chain.

---

## 7. LLM profiles

ralphd delegates all model access to `pi`, so any provider pi supports is usable.
The CLI's only job is to get the right **env vars, files and pi config
fragments** into the container; that bundle, named and reusable, is an LLM
profile. Profiles live at `<registry>/llm-profiles/<name>.yaml` and are selected
with `start --llm <name>` (falling back to the registry's
`default_llm_profile`, then to the built-in `host`).

A profile is resolved **once, on the host, at container start**. Secrets travel
as container env and mounted files; nothing profile-derived is ever written into
the run dir, an event, a transcript, or the image.

### 7.1 Profile format

```yaml
description: what this points at
model: provider/model-id             # strong tier for jobs using this profile
fast_model: provider/cheap-model-id  # fast tier under the cost strategies
env:                                 # env vars set in the container
  SOME_API_KEY: "literal-value"
  OTHER_KEY: ${env:HOST_VAR}              # from the host env at start time
  THIRD_KEY: ${file:~/.config/thing/key}  # from a host file (stripped)
  FOURTH_KEY: ${cmd:pass show thing/key}  # from a command's stdout (stripped)
mounts:                              # host:container[:ro], host side ~-expanded
  - ~/.aws:/home/agent/.aws:ro
pi:                                  # pi provider config for this container
  providers:
    my-gateway:
      baseUrl: https://gw.example.com/api/v1
      api: openai-completions        # or anthropic-messages
      apiKey: ${env:GW_API_KEY}      # resolved against `env:` above
      models: [{id: some-model}]
```

| key | type | meaning |
|---|---|---|
| `description` | string | free text, shown by `ralphctl llm show` |
| `model` | string | default strong-tier model ref for a job started with this profile; an explicit `--model` beats it |
| `fast_model` | string | default fast-tier ref; `--fast-model` beats it |
| `price_strategy` | string | which built-in rate table may derive a cost for this profile's routes (§8.6); one of `PRICE_STRATEGIES`, validated at resolve time so a typo is a `ProfileError` rather than a silent `none`. `--price-strategy` and a template/registry value beat it |
| `env` | mapping | env vars for the container. Entries resolve top to bottom and each one joins the resolution environment, so a later entry — and `mounts`/`pi` — may reference an earlier one |
| `mounts` | list | `host:container[:ro]` specs added to `docker run`; the host side is `~`-expanded |
| `pi` | mapping | written to `<config-dir>/pi/models.json` (`0600`) and copied to `~/.pi/agent/` by the entrypoint |

Three reference forms are supported, and only as a **whole** value: `${env:NAME}`
(the host environment layered with the profile's own already-resolved `env:`),
`${file:PATH}` (read, `~`-expanded, stripped), and `${cmd:...}` (run through
`bash -lc`, stdout stripped). Every reference is resolved even when the output
will be masked, so a broken one is always reported rather than hidden. Any failure
— missing profile file, non-mapping document, non-mapping `env:`/`pi:`, unset var,
unreadable file, nonzero command, unsupported `${kind:...}` — raises
`ProfileError` with a diagnostic naming the profile and the offending key.

`ralphctl llm profiles` lists the two built-ins first, then every `<name>.yaml` in
the directory. `ralphctl llm show <name>` prints the fully resolved profile with
every `env` value masked as `***REDACTED***`, and every `pi` field that came from
a reference masked too — literal `pi` fields such as `baseUrl` and the model ids
stay visible, because the resolved *shape* is what diagnosis needs. Mount paths
are never masked.

### 7.2 Built-in profiles

Two names always work with no file at all, and neither can fail to resolve.

**`host`** (the default) forwards the host's existing LLM setup:

1. copies `~/.pi/agent/settings.json`, `models.json` and `auth.json` (whichever
   exist) into `<config-dir>/pi/`, which the entrypoint copies to `~/.pi/agent/`
   inside the container;
2. **resolves `!command` apiKey references.** pi supports `apiKey: "!some-command
   args"`, shelling out per request — such helpers exist on the host, not in the
   container, so `ralphctl start` runs them host-side and writes the literal value
   into the copied `models.json` (`0600`, in the config dir, never the run dir).
   An unresolvable helper is a warning, not a failure. The trade is that the value
   is frozen at start time: on a long job with short-lived tokens, rotate mid-run
   through `PUT /config/llm`;
3. forwards the ten standard credential env vars listed in §6.6, when set;
4. mounts `~/.aws` read-only if it exists.

`host` sets no `model`/`fast_model`, so pi's own default applies unless `--model`
says otherwise. Anything beyond step 3 needs an explicit `--forward-env`, and a
recurring set of those flags is the signal to promote them into a named profile.

**`none`** injects nothing at all: no pi config, no env, no mounts. The operator
supplies everything through `--llm-env`/`--env`/mounts. For fully custom setups,
and for isolating a wiring problem.

### 7.3 Bedrock and gateway profiles

Two example profiles ship in `examples/llm-profiles/`. They exist because they
prove the two mechanisms every other setup is a variation of — an SDK credential
chain, and a bearer-token endpoint. **The engine has no code specific to
either**; they are plain profiles.

```yaml
# examples/llm-profiles/bedrock.yaml — SDK credential chain
description: AWS Bedrock, auth via host AWS CLI credentials/SSO
model: amazon-bedrock/anthropic.claude-opus-5
env:
  AWS_REGION: ${env:AWS_REGION}
  AWS_PROFILE: ${env:AWS_PROFILE}    # optional; omit to use the default chain
mounts:
  - ~/.aws:/home/agent/.aws:ro
```

pi's Bedrock provider uses the standard AWS SDK credential chain, so mounting
`~/.aws` (config plus the SSO/credential cache) and setting the region is enough —
static keys, SSO sessions and assumed roles all work. An SSO session that expires
mid-run surfaces as iteration failures and is fixable live.

```yaml
# examples/llm-profiles/gateway.yaml — endpoint + rotating key
description: any bearer-token gateway exposing an Anthropic- or OpenAI-style API
model: my-gateway/big-model
fast_model: my-gateway/small-model
env:
  GW_API_KEY: ${cmd:aws secretsmanager get-secret-value --secret-id my-gw-key
               --query SecretString --output text}
pi:
  providers:
    my-gateway:
      baseUrl: https://my-gateway.example.com/api/v1
      api: anthropic-messages          # or openai-completions
      apiKey: ${env:GW_API_KEY}
      models: [{id: big-model}, {id: small-model}]
```

This is the shape for a corporate AI gateway: point `baseUrl` at it, declare
which wire API it speaks, and list the model ids it fronts. The key is never a
literal in the file — `${cmd:...}` keeps the profile safe to commit and share.

**Gateway routes are commonly unpriced.** A gateway's model ids are local to it —
`aigw-openai/gpt-5` is a route name, not a catalogue entry — so such a gateway
bills plenty of tokens while reporting no cost block at all, and ralphd records
that as *unknown* rather than `$0`. `job.yaml`'s optional `pricing:` map (§6.2)
is the answer: it supplies rates in USD per **million** tokens, keyed like the
usage counters, plus a one-hop `aliases:` table that rewrites gateway ids onto
the priced ones with a trailing `*` preserving the tail:

```yaml
pricing:
  aliases:
    "aigw-openai/*": "openai/*"
    eu.anthropic.claude-opus-5: anthropic/claude-opus-5
  models:
    "openai/gpt-5": {input: 1.25, output: 10.0, cacheRead: 0.125}
    "anthropic/*":  {input: 3.0, output: 15.0}          # family default
```

The map is consulted **only** when the provider quoted nothing, and what it
produces is published separately as `costDerivedUSD` — never merged into
`costUSD`, so "the provider quoted this" and "a local rate table computed this"
stay distinguishable. With no map, unpriced traffic stays unknown rather than
becoming a guess.

### 7.4 Credentials

Model access is a profile's job; everything *else* the job needs to authenticate
against — git hosts, CI, issue trackers, artifact stores — goes through the
credentials convention, which is deliberately not part of a profile.

**One `<name>.env` file per credential set**, all in one directory:

```
creds/
├── github.env        # GITHUB_TOKEN=…
├── jenkins.env       # JENKINS_URL=… JENKINS_USER=… JENKINS_TOKEN=…
└── sonarqube.env     # SONAR_TOKEN=…
```

`start --creds <dir>` copies `*.env` plus four recognized extras (`gitconfig`,
`git-credentials`, `netrc`, an executable `setup.sh`) and an `ssh/` subdirectory
into `<config-dir>/creds/`; anything else in the directory is ignored.

Placement inside the container is the **engine's** job
(`engine/creds.py:place_creds()`, at startup and after every mutation), not the
entrypoint shell's — so secret handling stays inside the one process that already
promises never to write a value to `/run`, `events.jsonl` or stdout, and logs file
*names* only. It writes `~/.creds/<name>.env` at mode `0600`, rebuilding that
directory on every call so a delete actually removes a file; `gitconfig` →
`~/.gitconfig`; `git-credentials` → `~/.git-credentials` (`0600`) plus `git config
--global credential.helper store`; `netrc` → `~/.netrc` (`0600`); `ssh/` →
`~/.ssh` (`0700` dirs, `0600` files); and an executable `setup.sh` is run once,
from `$HOME`, before the first iteration — the escape hatch for anything the
convention does not cover.

**The inventory is advertised, the values are not.** When `~/.creds/*.env` is
non-empty, every phase prompt gets a `## Credentials` section listing the file
names — read fresh each iteration, so runtime CRUD shows up immediately — plus
the usage rule and the prohibition that makes it hold:

- source only what you need, where you need it:
  `set -a; . ~/.creds/<name>.env; set +a`;
- never print, `cat`, `echo` or otherwise dump a credential file, and never paste a
  value into a command's arguments (a URL query string, a `--token` flag, an inline
  `Authorization:` header) — **every tool call's arguments and stdout are recorded
  verbatim in the run's transcript**, which is host-visible and permanent, so
  either mistake persists the secret outside its file;
- refer to credentials only as `$VARNAME` after sourcing, letting the tool read its
  own environment — which also rules out token-bearing git remote URLs
  (`https://<token>@host/…`, leaked by `git remote -v` and `.git/config`) in favour
  of the credential helper.

Values are never auto-exported into the engine's or the agent's environment: a
credential is visible only to a command that sources its file. Nothing
credential-shaped is baked into the image, and nothing from the creds directory is
copied into the run dir or logged. The engine's mechanical redaction
(`engine/redact.py`) scrubs known values out of transcripts as a safety net; the
prompt rule is the actual mechanism, and the net exists because the rule has been
violated in practice.

Runtime CRUD mirrors skills: `ralphctl creds <run-id> ls | get <name> | add
<file>.env | rm <name>`. `ls` reports name, size and mtime, never values; `add`
takes a `*.env` file (its stem is the credential name) and lands it at
`~/.creds/<name>.env` immediately, no restart, reflected in the next iteration's
prompt. `get` reads a value back, which is deliberate: **holding the API bearer
token is defined as equivalent to holding the job's credentials.**

### 7.5 Verifying a profile

`ralphctl llm test <profile>` is the pre-flight check, and it never touches a run
dir — no registry entry is created, no run is started.

It runs in two stages. First it resolves the profile on the host exactly as `start`
would: exit `3` for an unknown name, exit `1` with the same diagnostic for an
unresolvable `${env:}`/`${file:}`/`${cmd:}` reference. No resolved value is
printed, so this stage needs neither redaction nor docker. Then, if resolution
succeeded and `docker version` answers, it performs a real one-token completion:

```
docker run --rm -i --entrypoint pi --label ralphd.llm-test=<name> \
  <resolved env> <resolved mounts> <image> -p --mode json --no-session [--model <m>]
```

with a one-line prompt on stdin. The entrypoint goes straight to `pi`, so
`ralphd-engine` is bypassed entirely; the profile's `pi:` fragment is written to a
temporary `models.json`, mounted read-only, and deleted afterwards. `--model`
overrides the pinged model (default: the profile's `model`), `--image` the image;
a failed ping exits `1` with the container's output. With `--no-ping`, or when no
docker daemon answers, the ping is skipped and successful resolution alone is
reported, which the output says explicitly.

A green `llm test` proves the profile parses, every reference resolves on *this*
host right now, and the resulting env, mounts and pi config are enough for pi to
reach the endpoint, authenticate and get a completion back for that model id. It
does not prove that a short-lived token will still be valid an hour into a job
(rotate through `PUT /config/llm`), that any *other* model id in the profile works,
or anything about quota and throttling under a real job's load.
## 8. Fault model and resilience

A long autonomous run does not fail cleanly. It fails because a gateway returns
`503` for ninety seconds, because DNS resolution wobbles, because the provider's
stream is reset mid-token, or because a credential was rotated out from under
the container. None of those are the agent's fault, and none should cost the job
an iteration, an approach, or a task attempt.

`ralphd` therefore treats *whether* an iteration failed and *whose fault* it was
as two independent questions, answered by two independent mechanisms:

- `src/ralphd/engine/faults.py` holds `classify_fault()`, a pure function over
  one iteration's failure signals. It answers `None` (not a failure),
  `"infra"` (the LLM endpoint or the network between it and the container) or
  `"work"` (the agent's own doing).
- `src/ralphd/engine/loop.py` holds the machinery that acts on that verdict:
  retry in place, refund the iteration, escalate the backoff, publish a
  degraded status, and give up on a wall-clock budget rather than an attempt
  count.

The verdict is recorded, not just acted on: it lands in the iteration's
`meta.json` and on the `iteration.end` event as `faultClass`, derived from the
same `_classify_result()` call the retry wrapper consumes. An operator reading
`faultClass: "infra"` on an iteration can be certain that is why the attempt
was retried and refunded.

### 8.1 What counts as a failure

Five signals, read off `IterationResult` (`src/ralphd/engine/runner.py`), make
an iteration a failure. Any one of them is sufficient.

| signal | source | meaning |
|---|---|---|
| non-empty `error_message` | a `message_end` assistant message with `stopReason: "error"` | the agent runtime recorded an error in band |
| `no_traffic_timeout` | the engine's startup-window watchdog | no parseable agent event at all within the window |
| `timed_out` | the full `iteration_timeout_s` firing | the iteration ran past its wall-clock ceiling |
| `interrupted` | a negative exit code, `SIGINT`, or exit `130` | the process was signalled |
| `exit_code` not `None` and not `0` | process exit | the agent process failed |

**Non-empty error text is a failure signal in its own right, whatever the exit
code says.** The agent runtime can report an in-band provider error — a gateway
reset surfaced as an assistant error message with zero token usage — and then
shut down cleanly with `exit_code: 0`. Keying the failure predicate off the exit
code alone makes that iteration look successful: the loop charges it to the
iteration budget, advances its progress bookkeeping, and moves on having
accomplished nothing, so a run of them burns the whole budget on iterations that
never ran. The predicate is therefore a disjunction over all five signals, and
`classify_fault()` returns a class for every cell of the
`{exit 0, exit nonzero} x {traffic, no traffic}` grid whenever error text is
present. The guard against over-reaching is equally load-bearing: exit `0` with
error text that is empty or whitespace-only is `None`, traffic or not, so
successes are never retried and never refunded.

`no_traffic_timeout` is deliberately distinct from `timed_out`. The watchdog in
`PiRunner.run()` waits `infra_startup_timeout_s` (default `150.0`) for the
*first* parseable event and then `SIGINT`s the process group. Without it, a
transient name-resolution failure that the agent blocks on internally consumes
the entire iteration timeout — up to forty-five minutes of wall clock — before
producing an error anyone can classify.

### 8.2 Infra versus work

`classify_fault()` takes `error_text`, `exit_code`, `interrupted`,
`timed_out`, `no_traffic_timeout`, `produced_traffic`, `operator_abort` and —
since task 014 (#49 part 2) — `duration_s`,
and applies exactly this order (the verdicts are `None`, `"infra"`, `"work"`
and — since task 013, #49 — `"signal"`, the tuple `faults.FAULT_CLASSES`):

1. If none of the failure signals in §8.1 is present, return `None`. Nothing
   downstream runs for a successful iteration.
2. If `operator_abort` is set, return `"work"`. An operator-initiated stop is
   never an infra fault, so `POST /abort` and `POST /interrupt` take effect at
   once instead of being fought by a retry episode that keeps relaunching the
   thing the operator just killed.
3. If `no_traffic_timeout` is set, return `"infra"`. The watchdog only fires
   when nothing was ever observed; there is no work to blame.
4. If the error text matches the infra signature table (§8.3), return
   `"infra"`. The signature wins over traffic: a gateway-level DNS or
   connection failure that the agent surfaced after streaming partway is still
   the provider's fault.
5. Otherwise, if `produced_traffic` is true and `interrupted` is set, return
   `"signal"` (task 013, #49). A signal ended an iteration that had already
   reached the model, with no abort recorded for this run: it was terminated
   before it could fail on its own, so it is not the agent's `work` failure
   (`work` is the class that burns approach and task-failure bookkeeping), and
   nothing about it looks like an outage, so it is not retried either — the
   retry wrapper acts on `"infra"` alone. `interrupted` is set from the agent
   process's own exit status (a negative code, or 130) and from nothing else.
6. Otherwise, if `produced_traffic` is true, return `"work"` — **unless** the
   error is a bare `aborted` and the iteration ran for no longer than
   `faults.ABORTED_STREAM_MAX_DURATION_S` (120s), in which case return
   `"infra"` (task 014, #49 part 2, the answer to what was §16's open question
   1 — see §17). A one-word `aborted` that arrived within two minutes, after
   traffic, with a clean exit status and no abort recorded for the run is the
   provider hanging up mid-stream: no iteration that did real work finishes,
   let alone fails with a one-word error, that fast. Past the threshold, or
   with the duration unknown (`duration_s=None`: nothing is known, so nothing
   is reclassified), it stays `"work"`.
7. Otherwise return `"infra"`.

`produced_traffic` is derived in `LoopSupervisor._classify_result()` as
`bool(result.final_text) or bool(result.usage)` — the agent said something, or
tokens were accounted for. `duration_s` is `IterationResult.duration_s`, the
agent subprocess's own wall-clock; the on-disk re-derivation in
`state.fault_explanation` passes the iteration record's `durationS` instead, so
the explanation reaches the same verdict the engine did. The threshold is
absolute rather than a fraction of `iteration_timeout_s` precisely because the
cap is not part of an iteration's `meta.json`, so a fraction could not be
re-derived from the record.

Step 7 is the interesting default. **An unclassifiable failure that produced
no traffic at all is treated as infra, because nothing ran.** The alternative
default charges the job for an iteration in which the agent never reached the
model, which is precisely the accounting error §8.1 exists to prevent. Step 6
is the counterweight: once traffic exists, an unrecognised error is the
agent's own problem and stays `"work"`, so it keeps consuming approach and
task-failure bookkeeping instead of being retried forever.

A signal-terminated iteration that never reached the model deliberately keeps
falling through to step 7 (`"infra"`) rather than to step 5's `"signal"`: with
zero traffic it is textually indistinguishable from a provider-side stream
abort, and "nothing ran, so nothing is charged" is the rule step 7 exists to
enforce.

A bare `aborted` error is genuinely ambiguous and is therefore decided from
inputs rather than from the signature table: the agent runtime reports an
operator `/abort` or `/interrupt` and a provider-side stream abort with the
*same* text, so no regex can separate them. `operator_abort` (step 2),
`interrupted` (step 5), `duration_s` (step 6) and `produced_traffic` (steps 6
and 7) decide it instead — a bare `aborted` with a recorded operator abort is
`"work"`; with no traffic and no recorded abort it is `"infra"`; with traffic
and no recorded abort it is `"signal"` when a signal killed the process,
`"infra"` when it arrived inside `ABORTED_STREAM_MAX_DURATION_S`, and `"work"`
only past that threshold.

**What is left, deliberately.** A bare `aborted` arriving after real traffic
*more* than `ABORTED_STREAM_MAX_DURATION_S` into an iteration still classifies
as `"work"` (step 6), so a provider-side abort late in a long iteration can
still cost an approach. That is the residual half of the trade-off task 014
took: past two minutes the shape is no longer distinguishable from an agent
that ran, worked and then aborted, and the alternative — adding `aborted` to
the signature table — would reclassify every operator interrupt that had
already produced traffic as an infra fault and retry against the operator's
explicit instruction.

### 8.3 The infra signature table

`faults._INFRA_TEXT_PATTERNS` is compiled case-insensitively into one
alternation. It is the entire contract for "is this the provider's fault"
when traffic exists, so it is specified family by family.

| family | patterns | what it means |
|---|---|---|
| DNS | `ENOTFOUND`, `EAI_AGAIN`, `getaddrinfo` | the gateway hostname did not resolve, permanently or temporarily |
| TCP | `ECONNREFUSED`, `ECONNRESET`, `ETIMEDOUT`, `EHOSTUNREACH`, `ENETUNREACH` | no usable connection to the endpoint |
| broken stream | `EPIPE`, `socket hang up`, `premature close` | the response stream died mid-flight |
| TLS | `TLS handshake`, `SSL handshake`, `certificate verify failed` | the transport could not be secured |
| opaque transport | `connection error` | the SDK's own catch-all, reported as a bare `Connection error.` |
| gateway 5xx | `bad gateway`, `gateway timeout`, `service unavailable`, `ServiceUnavailable`, `internal server error`, `50[234]` | the endpoint answered, with a server-side failure |
| back-pressure | `429`, `529`, `rate[ _-]?limit`, `throttl`, `overloaded` | the endpoint is refusing load, and waiting is the correct response |
| Bedrock stream | `ModelStreamErrorException` | the upstream terminated a Bedrock response stream |
| capacity and quota | `quota`, `capacity` | the account or region has nothing left to serve |

The numeric patterns are word-bounded (`\b50[234]\b`, `\b429\b`, `\b529\b`) so a
request id or a token count containing those digits cannot trigger a match. The
opaque `connection error` row earns its place because an SDK that swallows the
underlying `errno` and reports only `Connection error.` produces the single most
common live shape; without it, the most frequent real-world infra fault falls
through to step 5 and is scored as agent failure whenever traffic preceded it.

The table is asserted family by family in `tests/test_fault_classifier.py`, with
every family case passed as `produced_traffic=True, exit_code=1` so that only a
signature match can produce `"infra"` (a no-traffic case would be `"infra"` via
step 6 and the assertion would be vacuous). The negative cases matter as much:
ordinary agent failure text — `pytest exited 1: 3 failed, 12 passed`, an
`AssertionError`, a `ruff check failed`, an agent giving up on a merge conflict
— must stay `"work"` with traffic observed, so the table cannot quietly swallow
genuine work failures into an infinite retry.

### 8.4 Retry, backoff and the outage budget

`LoopSupervisor.run_iteration()` routes the phases in `INFRA_RETRY_PHASES` —
`("planning", "worker", "review", "verify", "reflect")` — through
`_run_iteration_with_infra_retry()`, which re-runs *the same* phase and
iteration while the verdict is `"infra"`. Any other phase runs once via
`_run_iteration_once()`.

Each infra-classified attempt increments `_infra_refunded`, which
`budget_left()` subtracts from `iterations_used`, so a retried attempt never
costs the job an iteration — while `iterations_used` itself keeps rising, so
every attempt still gets its own iteration directory, number and transcript on
disk. `status.json`'s `iterationsUsed` publishes the charged figure
(`iterations_used - _infra_refunded`).

The backoff is `infra_retry_backoff_s`, defaulting to
`DEFAULT_INFRA_RETRY_BACKOFF_S` in `src/ralphd/engine/config.py`:

    [2.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0]

Attempt *n* takes element `min(n - 1, len - 1)`, so **the last value repeats
for every further attempt** — an outage that outlives the schedule settles into
a five-minute poll rather than escalating without bound or resetting to the
bottom. The first step is two seconds because most gateway blips clear inside
one retry, and paying two seconds to find that out is cheaper than publishing a
degraded status. Each backoff is additionally clamped by
`infra_retry_backoff_max_s` (default `300.0`) and by whatever remains of the
outage budget, so one episode's cumulative wait can never overshoot the budget.

**The stopping rule is wall clock, not an attempt count.** An *episode* is one
continuous outage: consecutive infra-classified attempts with no iteration
reaching the model in between. Retries continue while the episode's cumulative
backoff wait stays under `infra_outage_budget_s` (default
`DEFAULT_INFRA_OUTAGE_BUDGET_S`, four hours). A gateway can be down for an
hour, and the correct response is to keep waiting, not to give up after three
tries. When the budget is spent, the run terminates with a reason naming the
outage duration, the attempt count, the seconds waited and the last error:

    infra fault: worker iteration failed throughout a 14403s infra outage
    (37 attempts, 14400s of the 14400s outage budget spent waiting):
    Connection error.

`infra_retry_max` is an explicit hard cap, **honoured only when set**: its
default is `None`, meaning no attempt cap at all, and when an operator sets it,
reaching it terminates the episode with a reason naming the attempt count.
Each of these knobs has an environment override
(`RALPHD_INFRA_STARTUP_TIMEOUT`, `RALPHD_INFRA_RETRY_BACKOFF_S`,
`RALPHD_INFRA_RETRY_BACKOFF_MAX_S`, `RALPHD_INFRA_RETRY_MAX`,
`RALPHD_INFRA_OUTAGE_BUDGET_S`), so changing one needs no `job.yaml` edit.

`_reset_infra_episode()` ends the episode as soon as any iteration produces a
non-infra verdict, and the next outage gets the full schedule and the full
budget again — a job that hits a thirty-second glitch every hour is never
slowly starved of retry budget by the earlier ones.

**Phase-local error budgets are never charged for an infra fault.** The
precedence is that the wrapper handles an infra verdict entirely in place and
only returns to the phase's own logic once the result is no longer an infra
fault, or once the wrapper gave up — in which case `_abort_reason` is set and
`budget_left()` is already `False`, so `_verify_task`'s
`MAX_VERIFY_ERROR_RETRIES` loop (three) and the review steering loop both exit
at once rather than re-charging the same outage against their own counters.
Nothing in this path touches a task's `validationAttempts`; only an explicit
non-error verdict miss does that.

Waiting is not working time. `job_timeout_s` is wall clock, so a four-hour
outage would silently consume half of an eight-hour job and the run would die
of "timeout" having done nothing wrong. `_account_infra_wait()` therefore
extends both the internal deadline and its published twin `deadlineAt` by
exactly the seconds waited, adds them to `infraWaitTotalS`, and emits
`deadline_extended` so an extension is auditable rather than a silent clock
adjustment. One number drives the episode clock, `infraWaitTotalS` and the
deadline extension, so the three can never disagree. `infraWaitTotalS` is
seeded from `status.json` at startup and therefore survives `resume`.

The degraded status contract is two fields, not a new state. **`state` stays
`running` throughout an outage**, because adding a `degraded` state value
would break every consumer's terminal-state logic. Instead:

| field | values | meaning |
|---|---|---|
| `health` | `"ok"` \| `"degraded"` | whether the endpoint is currently believed usable |
| `infraWait` | object or `null` | `null` unless the run is sitting in a backoff wait right now |

`infraWait` carries `since`, `attempt`, `error`, `phase`, `nextAttemptAt`,
`waitedS`, `budgetS` and `remainingS`. The same payload is emitted as an
`infra_wait` event, so the wait is visible to a follower of the event stream
and not only to whoever polls `/status` at the right moment. `infraWait`
returns to `null` when a wait ends, but `health` stays `"degraded"` until an
iteration actually reaches the model again: a run between two backoffs has not
recovered. Recovery is a single `infra_recovered` event that sets `health`
back to `"ok"`, emitted exactly once per episode.

**The fail-fast path survives all of this.** Unbounded patience is correct for
an outage and wrong for a broken environment: a malformed credential fails
instantly, identically and without traffic every time, and sitting out four
hours of backoff on it is useless. `_instant_failure_signature()` tells the two
apart with a stable signature — the exit code plus the error text with every
run of digits replaced by `N`, truncated at 200 characters — because transient
faults vary and take time, whereas a broken credential fails identically in
0.6s. An attempt counts toward the streak only while it runs under
`INSTANT_FAILURE_MAX_DURATION_S` (`5.0`), produces no assistant text and no
billed tokens, and carries the *same* signature as the streak. At
`MAX_CONSECUTIVE_INSTANT_FAILURES` (`3`) the wrapper stops with the
broken-environment diagnosis instead of the outage budget, so a broken
credential is diagnosed in seconds; the attempts are still refunded. The
verdict is memoised per result object, because the wrapper and the phase call
site both score the same result and double-scoring would inflate the streak. An
instant failure that *did* reach the model — a 0.3s Bedrock `502` with tokens
billed — goes back to the phase's own bounded error retry, since the
environment is demonstrably working.

The post-terminal `reflect` iteration is retried too, and reports its own
failure. It runs after the job already has a terminal state, so
`_outage_budget_for()` caps its episode at `REFLECT_OUTAGE_BUDGET_S` (`300.0`):
long enough to ride out the wobble that killed the job seconds ago, short
enough that a dead endpoint does not hold a finished run's container open for
hours. Two pieces of the job's own ending would otherwise make the wrapper a
no-op here, and `_begin_reflect_retry_window()` handles both — the episode
clock starts clean (a job that died of an outage arrives with the whole budget
spent), and an *engine*-recorded abort reason is parked for the duration and
restored afterwards, since `operator_abort_requested` is true for any recorded
reason and step 2 of §8.2 would otherwise score every reflect fault as
`"work"`. An *operator*-initiated abort keeps its veto in full: one attempt,
whatever it returns. When the job just ended on an infra fault,
`_reflect_pre_attempt_wait()` waits one backoff step first — emitting
`reflect_infra_delay` and then an `infra_wait` with `attempt: 0`, since the
delay precedes the episode's retries, which number from 1 — rather than firing
into the same dead endpoint in the same second the job died.

The outcome is recorded either way. `_record_reflect_outcome()` writes
`reflect: {ok, error, endedAt}` into `status.json` and emits `reflect_done`; on
failure it also writes `artifacts/reflection/FAILED.md` naming the error, the
time, the run id and the terminal state, because a silently swallowed reflect
failure is indistinguishable from `reflect` never having been enabled. "Ran but
produced no report" counts as failure: a missing
`artifacts/reflection/report.md` is a reflect failure even on a clean exit,
since the report on disk is the deliverable. Reflect can never rewrite the
job's terminal state, verdict or reason.

One path renders **no verdict**, and says so rather than inventing one: a
`SIGTERM`/`SIGINT` reaching the engine (`ralphctl stop`, a raw `docker stop`, a
host shutdown) runs `abort_on_signal()`, which fires the child killer and hands
the job a terminal state — with `SIGKILL` already counting down. Attempting
reflect there manufactures a failure out of the engine's own teardown, so
instead `_record_reflect_not_attempted()` writes `reflect: {ok: null,
attempted, skipped}`, emits `reflect_skipped`, spawns no iteration and
deliberately writes **no** `FAILED.md`: the tombstone asserts the reflection was
tried and failed, and a stopped run must not look like one whose post-mortem
broke. `attempted: false` is the signal-before-the-phase case, `attempted: true`
the signal-mid-attempt one; `ok: null` is what keeps `ralphctl status` and the
hub, which both gate on `ok === false`, from reporting a failure that did not
happen (they print `reflection: not attempted (…)` instead). An *API* abort is
not a signal: the engine is still alive and still owes a post-mortem, so it
keeps its one attempt.

### 8.5 Skipping the wait

An operator watching a five-minute backoff often knows something the engine does
not — that the gateway is back, that the credential has been rotated.
`POST /retry` acts on that, and the host CLI and the hub both drive this same
route rather than a side channel of their own.

The backoff is not `asyncio.sleep()`. `_wait_out_backoff()` awaits an
`asyncio.Event` under a timeout, so setting the event releases the loop task
immediately. It returns the seconds *actually* spent, clamped to the planned
backoff, and those are the seconds booked into the episode clock,
`infraWaitTotalS` and the deadline extension — a wait cut short is never
accounted as a full one.

Waking the wait also **restarts the outage-budget episode clock**
(`_infra_episode_waited_s = 0.0`). The operator asserting "it is back" is new
information, and a run that has already sat out most of its budget must not
die of budget exhaustion one attempt after being told to try again. The
attempt counter is deliberately *kept*, so repeated impatient retries keep
escalating the backoff instead of hammering a still-broken endpoint.

The route is narrow by design. It does not unpause a paused run — that is
`POST /resume` — and it does not touch steering. A degraded run is not paused;
it is waiting out an outage, and the two conditions are independent. When the
run is not in a backoff wait there is nothing to wake, and the API answers
`409` rather than silently pretending to have acted. An `infra_retry_now`
event records that the operator asked.

### 8.6 Cost and usage accounting

The agent runtime reports per-message token counters (`input`, `output`,
`cacheRead`, `cacheWrite`, `totalTokens`) and, sometimes, a `usage.cost.total`
price. `_accumulate_cost()` in `src/ralphd/engine/runner.py` folds those into
the iteration's `usage` under one rule: **a missing price is not a price of
zero.**

| observed | recorded |
|---|---|
| price reported | added to `costUSD`; `costPriced` set to `true` unless already `false` |
| no price, tokens billed | `costPriced: false`, and **nothing** added to `costUSD` |
| no price, nothing billed | `costUSD` accumulates an integer `0` — `$0` is the truth |
| a price of exactly `0`, tokens billed | treated as *no price*: `costPriced: false`, nothing added to `costUSD`, plus `costZeroQuoted: true` and a warning naming the model |
| a price of `0` on a route declared free | `costUSD: 0.0`, `costPriced: true`, `costFree: true` — the one believable `$0.00` |

A fully unpriced iteration therefore has no `costUSD` key at all — unknown, not
zero — and a mixed one keeps its priced subtotal flagged. Real gateways bill
plenty of tokens while reporting no cost block, and coercing that to zero
reports a whole run as `$0.0000` with no way to tell "free" from "unknown".

**An implausible zero is not a price either.** `pi` zero-fills its cost block
when the resolved model definition carries no rates, so a gateway can quote
`costUSD: 0` with `costPriced: true` over half a million billable tokens — which
is exactly what happened to the run that produced this section
(`artifacts/reports/pricing-anomaly.md`). `state.is_zero_quote()` recognises the
shape — a quoted zero alongside non-zero `billable_tokens()`, with the
no-traffic sentinel and a declared-free route exempted — and `cost_status()`
applies it **on read as well as on write**, so a run dir written by an older
engine reports honestly rather than confidently. Freeness is only ever
*declared*, never inferred from a zero: `pricing.free` patterns mark the usage
`costFree: true`, the only bool carried up into the `byPhase`/`byApproach`
rollups.

`cost_status()` in `src/ralphd/engine/state.py` collapses a usage dict to
`None` (fully priced, or nothing billed), `"derived"`, `"partial"` or
`"unknown"`. Run totals mix priced and unpriced iterations, and the mix reads
as *partial*: a known subtotal plus an explicit admission that the rest is not
known. `format_cost()` is the one shared formatter, and its vocabulary is fixed
by `COST_UNAVAILABLE = "unavailable"`,
`COST_PARTIAL_SUFFIX = "+ (partial, rest unavailable)"` and
`COST_DERIVED_WORD = "derived"`:

    $0.56                                  fully priced
    $0.56+ (partial, rest unavailable)     some iterations unpriced
    unavailable                            nothing priced at all
    ~$0.45 derived                          host-side rate table only
    $0.56 + ~$0.45 derived                  quoted plus derived
    $0.56 + ~$0.45 derived, partial (rest unavailable)

**An unknown cost renders as `unavailable` and never as `$0.0000`.** Only a
float `costUSD` counts as a quote (`_has_reported_price`); the integer `0` a
no-traffic iteration contributes is not one, and neither is an implausible zero.

An optional host-side pricing map fills the gap the provider leaves — for a
gateway-local model id, no upstream table can ever know the rate. It lives
under the registry `config.yaml`'s `pricing:` key, is inlined into the run's
`job.yaml` by `ralphctl start`, and is also settable directly or via
`RALPHD_PRICING` as JSON. Rates are USD **per million tokens** keyed exactly
like the usage counters (`RATE_KEYS = ("input", "output", "cacheRead",
"cacheWrite")`); an absent cache rate falls back to the *input* rate rather than
to zero, since pricing cached tokens at `$0` is the same class of lie this
feature exists to remove. `aliases:` resolves a local model id to a canonical
one in exactly one hop, longest pattern first, with a trailing `*` preserving
the matched tail (`"aigw-openai/*"` to `"openai/*"`); `models:` then prefers an
exact key over the longest wildcard; `free:` lists the patterns whose `$0.00` is
to be believed. A malformed entry is logged and ignored rather than fatal — a
typo in an optional cost annotation must never stop a job from running — and an
empty map derives nothing.

**A shipped rate table is opt-in, and says how old it is.** Writing a map by
hand is the wrong ask for a route whose public rates are published, so
`engine/pricing_aws.py` ships the AWS Bedrock rates (USD/Mtok, one entry per
model id) with a generated alias map for the gateway spellings, exposed as an
ordinary `PricingMap` through `pricing_map()` so there is one matcher rather
than a second resolver. Region prefixes are deliberately *not* collapsed — an
EU or AU route is not priced at the US rate — and an unknown id resolves to
nothing, because `unavailable` beats a guessed neighbour. The table carries a
machine-readable `AS_OF` date, `STALE_AFTER_DAYS`, and a `staleness()` verdict
surfaced through `GET /config`; `tools/refresh_bedrock_rates.py` regenerates and
re-checks it.

Which tables may price a run is one knob, `price_strategy`
(`PRICE_STRATEGIES = ("none", "aws")`, default `none`; settable in `job.yaml`, an
LLM profile, the registry config, `RALPHD_PRICE_STRATEGY`, or
`ralphctl start --price-strategy`, and reported by `GET /config` as the
**effective** value, so an unrecognised setting shows as the `none` it behaves
as). `resolve_pricing()` builds either the operator map alone or a
`PricingChain` of operator map then built-in table; a chain never merges rate
dicts — the first layer that can price an id answers for it, so exactly one
table prices any message and an operator's typed rate always wins over a shipped
one. `GET /config`'s `priceTables` names the layers in precedence order (`"operator
map"`, the built-in table's `TABLE_NAME`, or `NO_TABLE` when nothing can price
this run), which is the difference between "this run's cost is unknown" and
"nothing here could ever have priced it".

**A derived cost is never conflated with a provider-reported one.** It
accumulates into its own `costDerivedUSD` field, carries its own `costDerived`
marker, is never folded into `costUSD`, and is always rendered with a `~` and
the word `derived`. `costPriced` stays `false` either way, because the provider
still quoted nothing usable, and `costDerived: false` means at least one unpriced
message had no rate, so part of the cost remains genuinely unknown. Derivation
fires for an implausible zero exactly as it does for an absent price — otherwise
the feature would miss the route it was written for — and it prices the id the
iteration *observed* when the operator pinned no model reference, since an
unpinned run is precisely the one whose id the engine never chose.
`GET /config` reports the map through `PricingMap.describe()`.

Rendering happens once, on the server. `costDisplay` — the output of
`format_cost()` — is attached to the usage total and to every `byPhase` and
`byApproach` bucket, and omitted when there is nothing to say. **Surfaces
consume `costDisplay` rather than re-deriving a string from the numeric
fields**, so a browser tab, a terminal and an API client cannot disagree about
whether a figure is partial or derived.

### 8.7 Vanished containers, repair and doctor

A run whose recorded state is non-terminal (`NONTERMINAL_STATES` is
`("starting", "running")`) but whose container no longer exists is a dangling
run. It is the shape a host reboot, an OOM kill or a `docker rm` leaves
behind, and the run dir alone cannot distinguish it from a run that is working
normally.

`status` detects it by pairing the recorded state with a container lookup by
name and surfaces it explicitly:

- a warning line stating that the container is gone, backed by a
  `containerGone` field, so the condition is machine-readable and not only a
  rendering flourish;
- task counts read from the run dir rather than from the engine, since there
  is no engine left to ask;
- **staleness instead of a growing live elapsed time.** A dangling run's
  duration is frozen and the display switches to a "since last update"
  reading derived from `sinceLastUpdateSeconds`. A ticking elapsed counter on
  a dead run is an actively misleading display.

`doctor` sweeps the whole registry rather than one run and reports two
findings without changing anything: `danglingRegistryEntries` (recorded
non-terminal, no container) and `strayContainers` (a container with no
registry entry). Report-only is the default because both findings have benign
explanations, and each carries a suggested remedy — resume first for a
dangling run, or record the truth if resuming is not wanted.

`repair` is the guarded editor for the run dir. It refuses to touch a run
whose container is actually running, exiting `5`, because editing state under
a live engine produces a run dir that contradicts itself. `--set-state`
validates the target state, writes a `reason` recording that this was an
operator repair of a run whose container vanished, and appends a `repair`
audit event, so a hand-edited terminal state is never indistinguishable from
one the engine wrote. `--env KEY=VAL` updates the persisted env wiring
(`env-wiring.json`) in place for the next resume; only the key *names* are
recorded in the audit trail, never the values.

### 8.8 Opt-in self-recovery

Automatic resurrection of a job is a policy decision, not a default. A host
that reboots nightly wants dangling runs picked back up; a host where a
container died because the job was making things worse does not.

**Self-recovery is off by default**, and the default literal is written in
exactly one place: `AUTO_RESUME_DEFAULT = False` in
`src/ralphd/cli/main.py`. A default that appears in two places is a default
that will eventually disagree with itself — one copy in a flag definition, one
in a config loader, and no way to tell which one a given run obeyed. Every
consumer reads the single constant.

Opting in is per run or per registry: `start --auto-resume` writes an
`auto-resume.json` marker in the run's config dir, and a registry-level
default supplies the same for runs that do not say otherwise. The marker in
the *config* dir is the immutable opt-in; a separate `auto-resume.json` in the
*run* dir holds mutable guard state, so the operator's intent and the
machine's bookkeeping cannot overwrite each other.

Execution is `doctor --fix`, driven from `cron` or a systemd timer. **There is
no new daemon.** A sweep that runs on a schedule the operator already
understands, using the same detection logic as the report-only sweep, is
easier to reason about and to switch off than a resident process with its own
lifecycle. Each pass buckets what it did — `resumed`, `skipped`, `failed`,
`waiting`, `gaveUp`, `operatorTerminated`, `recovered` — and re-checks the
dangling condition immediately before each individual resume, because the
sweep is not atomic and a run may have come back on its own since detection.

A crash loop is guarded rather than retried forever.
`AUTO_RESUME_MAX_ATTEMPTS` is `5` and `AUTO_RESUME_BACKOFF_S` is
`[30, 120, 600, 1800, 3600]` seconds, so a run that dies on startup is
retried on an escalating schedule and then stops with a readable give-up
reason recorded in its guard state. Progress resets the counter: the guard
keys on `iterationsUsed`, so a run that got further than last time is treated
as making progress and is not penalised for an earlier crash. Re-arming is a
deliberate operator act — deleting the run dir's `auto-resume.json`.

**A terminal run is never resurrected, and neither is one the operator
stopped.** Self-recovery only ever acts on the dangling condition: a
non-terminal recorded state with no container. Operator intent is recorded
*before* the loop unwinds, in `operator-termination.json`
(`OPERATOR_TERMINATION_FILE`), written by `abort()` at the moment the abort is
requested. Without it, a container killed mid-abort — `SIGKILL`, a forced
stop, a host reboot — leaves a run dir indistinguishable from a crash, and the
sweep would helpfully restart the job the operator just killed. The marker
makes that impossible, and such runs land in the `operatorTerminated` bucket.
The refusal asks `is_operator_termination()`, not "does the file exist": a
**self-inflicted** termination (§4.6) is an ordinary zombie from here on and is
resumed like any other, which is what makes the agent accidentally killing its
own supervisor a recoverable event rather than a permanent one.

## 9. HTTP API

Every engine capability is an HTTP endpoint. The CLI and the hub are clients
of this API and have no privileged side channel into a running job, which is
what makes a third client — a script, a CI step, `curl` — a first-class
consumer rather than an afterthought.

### 9.1 Transport and auth

The engine serves FastAPI under `uvicorn` inside the job's container
(`src/ralphd/engine/main.py`). The port is `RALPHD_PORT`, default `7777`; the
bind address is `RALPHD_BIND`, default `0.0.0.0`. That default is safe under the
normal deployment, where the container's network namespace is its own and only
published ports are reachable, but the argument fails under host networking —
there is no port-publish boundary and `0.0.0.0` would expose the API on every
host interface — so the host side sets `RALPHD_BIND` explicitly to the address
the operator asked for.

Authentication is a bearer token. `api_token` on `JobConfig` (settable via
`RALPHD_API_TOKEN`) enables one middleware check:

```python
if cfg.api_token and request.url.path != "/healthz":
    if request.headers.get("authorization", "") != f"Bearer {cfg.api_token}":
        return JSONResponse({"title": "unauthorized", "status": 401},
                            status_code=401)
```

`GET /healthz` is the single exemption, so a container health probe never
needs the job's credential. When `api_token` is unset the API is unauthenticated
and relies entirely on the network boundary. The token is excluded from
`JobConfig.effective()` and therefore from `GET /config`.

`GET /version` returns `{"ralphd": <version>, "api": <API_VERSION>}` —
`API_VERSION` is currently `1`. A client that needs to know whether a field
exists asks this route rather than probing.

### 9.2 Endpoints

| method | path | purpose |
|---|---|---|
| `GET` | `/healthz` | liveness; auth-exempt; `{"ok": true}` |
| `GET` | `/version` | engine version and API version |
| `GET` | `/status` | the whole run state — see §9.3 |
| `GET` | `/tasks` | the task list as recorded on disk, plus the `tasksStale`/`tasksSource` read contract (§5.3) |
| `GET` | `/prd` | the PRD as markdown; `?original=true` for the pre-composite text; `404` when absent |
| `GET` | `/notes` | the run's accumulated notes as markdown |
| `GET` | `/iterations` | every iteration's `meta.json`, in order |
| `GET` | `/iterations/{n}` | one iteration's `meta.json`; `404` when unknown |
| `GET` | `/iterations/{n}/output` | that iteration's raw NDJSON transcript; `?tail=N`, `?follow=true`; `404` when it has no transcript |
| `GET` | `/logs` | the merged transcript across all iterations; `?tail=N`, `?follow=true` — see §9.4 |
| `GET` | `/events` | SSE event stream; `?since=<id>` replays from after that id |
| `GET` | `/artifacts` | every artifact as `{path, size}` |
| `GET` | `/artifacts/{path}` | one artifact's bytes; `404` outside the artifacts root |
| `GET` | `/config` | effective config, redacted |
| `PATCH` | `/config/budget` | raise or lower the iteration budget of a live run |
| `GET` | `/config/prompts` | every phase prompt name with its effective source |
| `PUT` | `/config/prompts/{name}` | override one phase prompt; `204` |
| `GET` | `/config/skills` | skill inventory as `{name, origin, fileCount}` |
| `GET` | `/config/skills/{name}` | one skill as a tar archive; `404` when unknown |
| `PUT` | `/config/skills/{name}` | install or replace one skill from a tar body; `204` |
| `DELETE` | `/config/skills/{name}` | tombstone one skill; `204`; `404` when unknown |
| `GET` | `/config/creds` | credential inventory as `{name, size, mtime}` — never values |
| `GET` | `/config/creds/{name}` | one credential's bytes; `404` when unknown |
| `PUT` | `/config/creds/{name}` | install or replace one credential; `204` |
| `DELETE` | `/config/creds/{name}` | tombstone one credential; `204`; `404` when unknown |
| `PUT` | `/config/llm` | mid-run endpoint or key rotation: `{"env": {...}, "pi": {...}}`; `204` |
| `POST` | `/steering` | queue a steering message; `202` with the written filename |
| `GET` | `/steering` | every steering file with a `consumed` flag |
| `POST` | `/interrupt` | signal the running iteration, optionally queueing a message first |
| `POST` | `/pause` | stop at the next iteration boundary |
| `POST` | `/resume` | release an operator pause |
| `POST` | `/retry` | wake an infra backoff wait now — see §8.5 |
| `POST` | `/abort` | terminate the run, with an optional `reason` |
| `POST` | `/shutdown` | exit the engine process; only once the job is terminal |

`GET /config` returns `JobConfig.effective()` plus prompt sources, skills as
`{name, origin}`, credential *names* only, and `llmEnvKeys` — the sorted key
names of the LLM env overrides. **No credential value and no LLM env value is
ever served**, by any route, at any verbosity.

`PATCH /config/budget` takes `{"iterations": "+10"}` to top up or
`{"iterations": 40}` to set absolutely. The new value is live at the next
iteration boundary, since the loop reads the budget every turn, and is
immediately visible as `iterationsBudget` in `/status` and
`budgets.iterations` in `/config`. It emits `budget_changed` with the previous
value, the delta and the usage at the time. It is a live-engine change only:
`/config/job.yaml` is a read-only mount, so the engine cannot persist it
there, and carrying a larger budget into a fresh container is a `resume`
concern. Two rejections are specified: `422` when `iterations` is missing or
unparseable, and `409` when the requested value is below
`iterations_used_charged`, because a budget may be set to current usage or
above but never retroactively below what has already been spent.

`POST /shutdown` inverts the usual guard: it answers `409` **while the job is
still running** ("abort first") and succeeds only once the state is terminal.
It emits a log event and schedules `SIGTERM` to its own process shortly after
responding, so the client gets an answer rather than a dropped connection.

`POST /abort` calls `resume()` immediately after recording the abort, so a
*paused* run is unblocked and can actually wind down rather than sitting in a
pause the operator can no longer see the point of.

### 9.3 Status payload

`GET /status` is *the* wire contract. Every other surface — terminal, browser,
script — renders this document, so anything true about a run has to be
expressible here.

The route reads `status.json` and then guarantees three things about the
result:

- `health` defaults to `"ok"` and `infraWait` to `null`;
- `reflect` defaults to `null`, meaning "no reflect iteration has finished";
- `maxApproaches`, `model` and `modelRaw` default to `null`, so a pre-v0.6 run
  dir yields an explicit "not known for this run" rather than a denominator or a
  model id guessed in from the live config;
- `tasks` is replaced with computed counts via `task_counts()`, carrying
  `tasksStale`/`tasksSource` beside them (§5.3), and `steering`
  with `{"pending": N, "consumed": N}`.

**The absence of a field is never a third case a consumer has to handle.** A
run dir written before a field existed, or one whose loop has not started yet,
still produces the same shape, so a client can read `status.health` without a
null guard and `status.reflect` without distinguishing "reflect is off" from
"this engine does not report reflect". `task_counts()` is shared with the
host-side on-disk fallback in `src/ralphd/engine/state.py`, so the counts a
dangling run shows (§8.7) are computed by the same code as the counts a live
engine serves.

The resilience-specific fields are `health`, `infraWait` and `infraWaitTotalS`
(§8.4), `deadlineAt` (extended by every infra wait), `reflect`
(`{ok, error, endedAt}`), and `iterationsUsed`, which publishes the *charged*
figure — raw attempts minus infra refunds (`iterations_used_charged`), which is
also the figure `PATCH /config/budget` validates against, so the number an
operator reads is the number the engine enforces.

### 9.4 Streaming logs

A run's transcript is not one file. It is `iterations/NNNN/output.jsonl` per
iteration, and the rendered log is those transcripts concatenated in iteration
order with a synthesized boundary line around each one.
`src/ralphd/log_merge.py` is the single implementation of that merge, imported
by the engine for the snapshot half of `GET /logs` and called directly by
host-side readers against the run dir. Both paths therefore emit
byte-identical lines for the same run dir. This is why a transcript is
readable after the container is gone: reading a run's logs is a function of
the run dir, not a service the container provides.

The boundary line has `"type": "ralphd.iteration"` and `"event": "start"` or
`"end"`, carrying the iteration number, phase, model, approach and start time;
the `end` form adds `exitCode`, `error`, `usage` and `endedAt`. It is never
written to disk — it is derived from `meta.json` so a reader can tell iterations
apart in a flat stream. An iteration with no readable `meta.json` is skipped,
because it is either being created right now or was truncated by a crash, and
the `end` boundary appears only once `endedAt` is recorded, so a live iteration
renders open-ended rather than falsely closed. `?tail=N` counts *transcript*
lines only and keeps whatever boundaries fall inside that window. A run dir with
no transcripts at all renders `(no transcript yet)`.

`?follow=true` replays the (possibly tail-limited) snapshot and then continues
live from where that snapshot's *true, untailed* end was — the byte offset of
the last iteration's `output.jsonl` at snapshot time, plus whether that
iteration had already ended. Anchoring on the untailed end is what stops a
`tail`ed follow from re-emitting or skipping lines at the handover. From there
the follower walks forward: emit the `start` boundary for an iteration it has
not opened, stream new bytes as they land, emit the `end` boundary once
`endedAt` appears, move on. It breaks only when there is no further iteration
directory *and* the job is terminal.

Scrubbing is applied again at serving time on top of the write-time scrub, so a
value only *recognised* as a secret after a transcript line was written — a
credential added mid-run whose literal value an earlier iteration echoed — is
still redacted on the way out. Host-side readers pass no scrubber and get the
bytes as written.

`GET /events` is Server-Sent Events over `events.jsonl`, with one frame per
event carrying `id`, `event` (the event type) and the whole event as `data`,
plus a comment keepalive every second so an idle stream is not mistaken for a
dead one. Ids are monotonic and the file is append-only *across resumes*, so
`?since=<id>` is a reliable resume point and a follower can reconcile a
restarted run against a log it already has. That append-only property is also
why a run's move to `running` is emitted as a `state` event unconditionally,
not only on resume: otherwise a resumed run's log would still end on the
previous episode's terminal `state` event.

That sets up the one rule a naive follower gets wrong. A terminal `state` event
in an append-only log is not proof that the run is over: a resumed run's log
contains the *previous* episode's terminal event followed by a fresh `running`.
**A terminal event ends a follow only when it is the log's last event and the
engine is not live.** Both halves are necessary — without the "last event" test,
replaying history closes the stream at the first historical terminal marker;
without the liveness test, a race between the terminal event and the engine's
actual exit closes a stream that is about to produce more. A historical terminal
marker can therefore never close a live follow.

### 9.5 Errors

Errors are `application/problem+json`. `problem()` builds
`{"title", "status", "detail"}`, with `detail` carrying the actionable half —
what to do instead, not merely what went wrong. The auth middleware returns
the same shape with `title: "unauthorized"`.

| status | when |
|---|---|
| `401` | a token is configured and the `Authorization` header does not match; every path except `/healthz` |
| `404` | a named iteration, artifact, skill, credential or PRD does not exist, including a path that escapes the artifacts root |
| `409` | the request is well-formed but the run is in the wrong state for it |
| `422` | the body is missing, empty, or semantically invalid |

`204` is the success code for every write that has nothing to return: prompt,
skill, credential and LLM-config mutations. `202` is `POST /steering`'s, since
a steering message is queued for the next iteration boundary rather than
applied on the spot.

The `409` cases are worth enumerating, because each one distinguishes an
operation that would be a no-op from one that would be a lie:

| route | `409` condition |
|---|---|
| `POST /steering` | the job is finished; steering has no effect |
| `POST /interrupt` | no iteration is running, so no signal was delivered |
| `POST /pause` | the job is finished |
| `POST /retry` | the job is finished, or the run is not in an infra backoff wait |
| `POST /abort` | the job is already finished |
| `POST /shutdown` | the job is still running |
| `PATCH /config/budget` | the job is finished, or the value is below iterations already used |

`POST /interrupt` returning `409` rather than `200` matters to §8.2: an
interrupt that reached nothing changes no iteration's outcome, and must not be
recorded as operator intent, because doing so would shield the *next*
iteration's failure from infra retry.
## 10. ralphctl

`ralphctl` is the host-side control surface: one command that starts job
containers, follows them, steers them, and reads their run dirs. It lives in
`src/ralphd/cli/` — `main.py` (the parser and every command), plus
`log_render.py` for the transcript, `ui_server.py` for the hub (§11),
`image.py` for the job-image hash and `llm_profiles.py` for the profiles — on
the standard library alone
(`argparse`, `urllib.request`, `json`, `subprocess`, plus `yaml` for the
registry config) — no HTTP client library, no curses dependency, no framework.
It also reuses the engine's own reader/formatter helpers (`engine/state.py`)
rather than restating them, which is why a run dir renders identically in the
terminal and in the browser.
That keeps `pipx install ralphd` small on a machine that only ever *drives*
jobs; the server dependencies stay on the engine side of the same wheel,
inside the job image.

Everything `ralphctl` knows lives in two places: the **registry** on disk
(`~/.ralphd`, or `$RALPHD_REGISTRY`), holding `runs/<run-id>/` and
`configs/<run-id>/`; and the **run's container API**, at the `apiUrl` recorded
in `runs/<run-id>/host.json`, authenticated with `runs/<run-id>/.api-token`
when one exists. Commands that report history read the run dir; commands that
change a live job's behaviour call the API; several do both in that order of
preference — ask the engine, fall back to disk when the container is gone
(§10.3, §10.5).

Two global flags exist, and only two: `--version` and `--json`. There is no
global `--registry` (use `RALPHD_REGISTRY`), `--quiet` or `--yes`. A
subcommand is mandatory.

### 10.1 Command surface

| command | what it does |
|---------|--------------|
| `start` | launch a new job container from a PRD (and optional template) |
| `runs` | list every run in the registry as a table, newest first |
| `status <id>` | one-screen state of a run: state, verdict, phase, approach, tasks, usage |
| `tasks <id>` | the run's task list with per-task status |
| `watch <id>` | follow the run's `events.jsonl` stream until its real terminus |
| `interrupt <id>` | SIGINT the current iteration, adding no guidance |
| `pause <id>` | hold the loop at the next iteration boundary |
| `unpause <id>` | release an operator pause |
| `resume <id>` | start a fresh container over an existing run dir |
| `logs <id>` | the agent transcript, pretty-rendered, tail- and follow-capable |
| `iteration <id> <n>` | one iteration's own story: phase, timing, exit reason, its tokens and cost, its transcript |
| `fault <id>` | why the run is (or last was) in trouble: class, matched signature, retry ladder, outage budget |
| `cost <id>` | what the run spent, per phase and per approach, with priced, derived and unavailable money labelled |
| `docs <id> [name]` | the run's state documents: notes, review findings, composite PRD, the redacted effective job config |
| `skills <id> …` | `ls`/`get`/`add`/`rm` skills on a live job |
| `creds <id> …` | `ls`/`get`/`add`/`rm` runtime credentials on a live job |
| `prompts <id> …` | `ls`/`set` phase prompt overrides on a live job |
| `llm …` | `profiles`/`show`/`test` LLM profiles on the host |
| `steer <id> [msg]` | queue operator guidance for the next iteration, or `--list` what has been queued |
| `retry <id>` | wake a degraded run out of its infra backoff wait immediately |
| `budget <id> <+N\|N>` | change a running job's iteration budget in flight |
| `abort <id>` | terminate the job, recording state `aborted` |
| `stop <id>` | shut down and remove an idle finished container |
| `rm <id>` | delete a run's registry dir (`--force` stops a leftover container first) |
| `repair <id>` | diagnose (or, guarded, patch) an inconsistent run dir |
| `artifacts <id> …` | `ls`/`pull` the run's artifacts straight off disk |
| `doctor` | preflight the host, report strays and zombies, optionally recover |
| `config get\|set` | registry-wide defaults in `<registry>/config.yaml` |
| `ui` | serve the local web hub in the foreground (§11) |

`status`, `tasks`, `watch`, `interrupt`, `pause` and `unpause` are registered
from one loop and therefore take exactly one argument — the run id — and
nothing else. These are the six commands typed most often, and none of them
has an option worth remembering.

**Rotating a live job's model is not in this set.** `llm` reads the host's
profiles — `profiles`, `show`, `test` — and nothing more: a profile decides how
a *new* container is wired (§7), so changing one has no effect on a running job,
and the way to move a run onto a different route is `abort`/`resume` (or
`prompts set`/`steer` for the work itself). The three hot-swap verbs are
`skills`, `creds` and `prompts`, because those are the inputs an iteration
re-reads at its next boundary.

**Four of these commands read nothing but the run dir** — `iteration`, `fault`,
`cost` and `docs`, the detail surfaces of §10.5. They never call the API, not
even when the container is up, because everything they render (`meta.json`, the
transcript, `status.json`'s fault and usage blocks, the run's own prose) is
written by the engine atomically, so the run dir is authoritative for a live job
and a long-dead one alike. There is therefore no live/snapshot distinction to
report and no notice to print.

`logsf <id>` is an alias for `logs <id> --follow`, and `logs <id> -100`,
`-100f` and `-f` are tail-style shorthands. Argparse cannot parse a bare
`-100` token, so `_preprocess_logs_argv` rewrites all four forms into
`--tail`/`--follow` before the parser sees the command line; anything
unrecognized is passed through untouched so argparse's own
"unrecognized arguments" error still fires.

### 10.2 Starting and resuming a job

`ralphctl start` resolves the job's configuration on the host, writes it into
`<registry>/configs/<run-id>/`, then execs one `docker run`. By default it
detaches: it prints the run id, the short container id and the API URL, and
returns immediately.

| flag | default | meaning |
|------|---------|---------|
| `--prd FILE` | required unless `--template` supplies one | PRD markdown; `-` reads stdin |
| `--template NAME` | none | job defaults + optional `prd.md`/skills/creds from `<registry>/templates/<name>/` |
| `--workspace DIR[:NAME]` | none (internal workspace) | host dir mounted at `/workspace`; repeatable, and every one needs `:NAME` once there is more than one |
| `--run-id ID` | derived | explicit run id; refuses an id that already exists |
| `--iterations N` | `25` | iteration budget |
| `--max-approaches N` | `3` | distinct approaches before the job gives up |
| `--vigilant` | off | stricter review posture |
| `--reflect` | off | one extra post-terminal `reflect` iteration into `artifacts/reflection/` |
| `--model REF` | unset | `pi` model ref for the main phases |
| `--fast-model REF` | unset | model for cheap/auxiliary work |
| `--model-strategy S` | `quality-first` | one of `quality-first`, `cost-optimized`, `balanced` |
| `--thinking LEVEL` | unset | `pi` thinking level |
| `--price-strategy S` | `none` | derive a cost for routes the provider does not price (or prices with an implausible zero): `aws` uses the built-in Bedrock rate table (§8.6) |
| `--llm PROFILE` | `host` | `host`, `none`, or a name under `<registry>/llm-profiles/` |
| `--llm-env KEY=VAL` | none | extra env for the LLM wiring; repeatable |
| `--forward-env NAME\|PREFIX_*` | none | forward host env var(s) by name or prefix; repeatable |
| `--env KEY=VAL` | none | plain container env; repeatable |
| `--skills DIR` | none | skill dir (or a dir of them); repeatable |
| `--creds DIR` | none | copy `*.env` plus recognized extras into the job's creds |
| `--allow-docker` | off | mount the host docker socket — root-equivalent host access |
| `--image REF` | built `ralphd:<hash>`, or `$RALPHD_IMAGE` | pin a finished job image and run it as-is, hashing and building nothing (§3.2) |
| `--base-image REF` | none | build the job image *on top of* this image: it only has to carry your toolchain, ralphd layers the engine and `pi` onto it and runs the derived `ralphd-derived:<hash>`. Mutually exclusive with `--image` |
| `--dockerfile PATH` | none | build this Dockerfile (its own directory is the build context) into that base image instead of naming one that already exists; recorded in `job.yaml` and replayed by `resume` |
| `--on-complete idle\|exit` | `exit` | keep the container idling after completion, or tear it down |
| `--on-complete-cmd CMD` | none | in-container shell hook run once on reaching a terminal state |
| `--timeout MINUTES` | `480` | whole-job wall-clock budget |
| `--iteration-timeout MINUTES` | `45` | per-iteration wall-clock budget |
| `--infra-outage-budget SECONDS` | engine default `14400` (4h) | how long one LLM-endpoint outage may be ridden out |
| `--auto-resume` / `--no-auto-resume` | off | opt this run in or out of `doctor --fix` self-recovery |
| `--port N` | free ephemeral port | host port for the run's API |
| `--api-bind ADDR` | `127.0.0.1` | address the API is published on |
| `--network NET` | none (default bridge) | docker network; `host` shares the host netns |
| `--api-token VALUE\|auto` | none | bearer token for the run's API; `auto` generates one |
| `--no-detach` | detached | follow the event stream to completion in the foreground |

Timeouts are integers in **minutes**; there is no duration-string syntax.
`--infra-outage-budget` is the one exception, in seconds, because it is a
retry budget rather than a work budget and is tuned against how long a
gateway outage plausibly lasts.

**Scalar job settings resolve through one fixed precedence chain:** an
explicit CLI flag, then the `--template`'s `job.yaml`, then the registry-wide
default in `<registry>/config.yaml` (`ralphctl config set`), then the
hardcoded default in the table above. Only `image`, `base_image`, `dockerfile`,
`on_complete`, `default_llm_profile` (feeding `--llm`), `network`, `auto_resume`
and `price_strategy` have a registry-wide layer; the rest skip straight from
template to hardcoded default. Every flag defaults to `None` in the parser
rather than to its real value, so "not given" stays distinguishable from
"given the value the default happens to be" and the chain can be applied
honestly. The image flags are the one place where the *level* wins whole
rather than the key: `--dockerfile`, `--base-image` and `--image` are three
answers to one question, so the most specific level that answers it at all
settles all three (§3.2).

`--no-detach` turns `start` into a blocking call: it follows the event stream
and, on the terminal event, polls `/status` one last time, exiting `0` only
when `verdict == "verified"` and `1` otherwise. That final poll falls back to
`status.json` on disk, because `on-complete exit` tears the API down the
instant it emits the terminal event and a fresh connection can lose the race.

`ralphctl resume <id>` starts a fresh container over an existing run dir. The
engine detects the pre-existing `tasks.json` and completed iterations at
startup and continues the job rather than re-planning, so `resume`'s whole job
is to reproduce `start`'s docker wiring for the *same* run:

- the run dir and config dir mounts — the PRD, `job.yaml`, staged creds,
  skills and `pi` config are already there and are reused as-is;
- the workspace mount(s), read back from `host.json` (`workspace` for a
  single unnamed mount, `workspaces` as name→path for the multi-repo case),
  so `resume` never needs a `--workspace` flag of its own;
- the recorded `.api-token`, so a client-side token keeps working against the
  new container;
- the LLM wiring resolved at `start` time, replayed from
  `<config-dir>/llm-wiring.json` (mode `0600`);
- the `--forward-env`/`--llm-env`/`--env` pairs resolved at `start` time,
  replayed from `<config-dir>/env-wiring.json` (mode `0600`) in the exact
  order they were applied, `llm-wiring.json` first and a later duplicate name
  winning — matching `start`'s own precedence.

**Wiring is replayed, never re-derived.** A job whose credentials arrived via
`--forward-env 'AWS_*'` resumes with the values the shell held at `start`
time, regardless of what the resuming shell has — or lacks. That is the
difference between a recovery that works from a cron job at 04:00 and one that
only works from the terminal that happened to start the run. `resume`
therefore has no env-wiring flags of its own; `ralphctl repair --env KEY=VAL`
is the supported way to correct a recorded value.

`resume` recomputes only what must change for a new container: a fresh port
(`--port`, else a free ephemeral one), `--api-bind`, `--network` (defaulting
to the network recorded at start time), `--allow-docker`, and the
budget — `--iterations +10` tops it up in `job.yaml` before the container
starts, a bare `--iterations 30` sets it absolutely, omitting it continues
with whatever remains. It refuses a run whose container is currently running
(exit `5`), because a live engine already holds that run dir's lock; a
container that merely *exited* is `docker rm -f`'d first to free the
`ralphd-<run-id>` name.

**The image is reproduced, not re-resolved.** A resume must not swap the engine
out from under a half-finished run, so it ranks `--image <ref>` first (an
explicit pin, as everywhere else), then **the image this run started on**, read
from its own run state — by reference while that reference still names the
recorded image id, by the recorded id once a mutable tag has moved. Only when
that image is genuinely gone from the daemon does it replay the run's own
`base_image:`/`dockerfile:` recipe from `job.yaml`, rebuilding it if it now
means something else, and only a run dir that recorded neither falls back to the
default reference. Every step down from the recorded image is a warning on
stderr naming what could not be reproduced; none of them refuses the resume
(§3.2).

**A resumed run always appends an explicit `running` state event.** The engine
emits it unconditionally at startup (`src/ralphd/engine/loop.py`, carrying
`resumed: true|false`), so `events.jsonl` — which is append-only across
resumes — never ends on the previous episode's terminal marker. Without that
event, every follower replaying the log from id 0 would decide a resumed job
had already finished.

### 10.3 Watching a job

Two commands follow a live run, and they read different streams.

`ralphctl watch <id>` follows the **event stream** (`GET /events?since=0`) —
the run's lifecycle: state transitions, iteration boundaries, steering
consumed, budget changes, faults. Human output is one line per event,
`[<ts>] <type> <json-detail>`, with `id`/`ts`/`type` lifted out of the detail
object; `--json` makes it one compact JSON object per line, the shape a
supervising script wants. It reconnects while the container starts (up to 30
attempts, backing off) and dies with exit `4` and
`could not connect to event stream` if it never gets one.

`ralphctl logs <id>` follows the **agent transcript** — what the model actually
said and did. Pretty rendering is the default, produced by
`src/ralphd/cli/log_render.py`:

- boundary headers,
  `── iteration N · phase=… · model=… · approach=… · started <local time> ──`,
  closed once the iteration ends by
  `iteration N done, at …, took …, exit=…, tokens=…, cost=…`;
- assistant text as it streams;
- one compact line per tool call, `→ <tool> <salient argument> ✓ ok
  (<excerpt>)`. The salient argument is `bash`'s command with newlines
  collapsed, `read`/`write`/`edit`'s path, `grep`/`glob`/`find`'s pattern, or
  else the first scalar argument, truncated generously (~300 chars) so nine
  `bash` calls do not render as nine identical lines. Failures get a longer
  excerpt (~120 chars) than successes (~60): the detail matters more when
  something broke;
- one `[thinking…]` marker per thinking block, however many deltas it took;
- `! [malformed log line, N bytes]` for a line that will not parse, rather
  than a silent drop; unrecognized event types are skipped.

| aspect | `watch` | `logs` |
|--------|---------|--------|
| source | `GET /events` (SSE) | `GET /logs` / `GET /iterations/{n}/output` |
| content | lifecycle events | agent transcript |
| tail | not applicable (replays from id 0) | `--tail N`, `-N`, default 50 |
| follow | always | `--follow`, `-f`, `logsf` |
| machine mode | `--json` (one object per line) | `--raw` (NDJSON passthrough) |
| unreachable run | exit `4` after retries | on-disk snapshot, exit `0` |

**`-N` means N rendered lines in pretty mode and N raw events in `--raw` mode,**
and the trim is owned by a different layer in each. `--raw` is a wire contract:
the engine trims, one raw event is one line. In pretty mode a raw `tail=N`
would be the wrong input, because the renderer collapses whole bursts of delta
events into one visible line — so `ralphctl` fetches the *full* untailed
transcript, renders every line, then keeps the last N. `logs <id> -100` always
means "the last 100 lines you would actually see"; boundary lines count toward
N like any other. `-Nf` shows exactly N rendered backlog lines and then
continues from a fresh `follow=true` connection, skipping the raw lines the
backlog fetch already consumed so nothing is double-rendered or dropped. That
connection asks for an explicitly huge tail rather than omitting the parameter,
because `GET /logs` and `GET /iterations/{n}/output` have opposite defaults for
a bare follow — the latter seeks to EOF and replays nothing, which would break
the skip accounting.

**Absolute timestamps come from one shared formatter.** Every human-facing
absolute time ralphd prints — boundary lines, `ralphctl status`, `runs`'
`STARTED` column, the hub — is rendered by
`ralphd.engine.state.format_local_time`, in the host's timezone with the UTC
offset always included so a pasted timestamp stays unambiguous. A relative
duration alone cannot be lined up against anything outside the run — an
upstream outage window, a host reboot, another run's log — which is why the
absolute form is printed alongside (never instead of) `took`. Machine surfaces
keep the raw ISO value, and the hub is handed the formatted string rather than
reimplementing the format in JavaScript.

**A dead container does not cost you the transcript.** The transcript lives in
the run dir (`iterations/NNNN/output.jsonl`), so when a run's API does not
answer, all modes fall back to the same on-disk merge (`ralphd.log_merge`)
that the engine serves from the inside, exit `0`, and print one notice on
**stderr**:

```
ralphctl: on-disk snapshot: the run's API is not reachable, showing the transcript recorded in the run dir
```

stderr, not stdout, so `--raw` keeps its byte-for-byte wire contract and a pipe
into `jq` or `tee` behaves identically for a live and a dead run. A `--follow`
against an unreachable run prints that snapshot and returns cleanly with
`… (nothing to follow)` appended, instead of hanging on a container that will
never answer. `--iteration n` reads from disk the same way. A run id with no
run dir at all is still exit `3`.

A run whose `iterations/` dir is empty — it just started, or died before its
first iteration was recorded — prints the explicit line `(no transcript yet)`
and exits `0`, because zero bytes of output is indistinguishable from a broken
command. The wording is one constant (`ralphd.log_merge.NO_TRANSCRIPT`) so the
hub's log tail says the same thing. `--raw` is excluded on purpose: an empty
transcript honestly is zero events.

**A historical terminal marker never ends a live follow.** Because
`events.jsonl` is append-only across resumes and followers replay it from id 0,
the first terminal `state` event a follower sees may belong to an earlier
episode. `watch` closes on such a marker only when both conditions hold: no
later `state` event in the log supersedes it, and the run's live `/status` does
not report a non-terminal state. Only later *state* events count as
superseding, since the engine emits `on_complete_cmd` log events strictly after
the terminal state and those must not hold the stream open past a real
completion. An idling finished run — API up, state terminal — still ends the
stream, so the rule never turns a completed job into a hang.

While `logs --follow` runs on a TTY, `ralphctl` owns the terminal explicitly.
`_TerminalModeGuard` saves termios state and enters cbreak mode once, in the
main thread, around the entire follow, and restores it on every exit path —
normal return, `KeyboardInterrupt`, `SystemExit`, an arbitrary exception — with
an `atexit` hook as a last resort and an idempotent `restore()` so both firing
is harmless. SIGTERM becomes `SystemExit` for the guard's lifetime so a plain
`kill` unwinds through the same `with` block instead of terminating the process
with no Python-level cleanup. Terminal state belongs to the terminal, not to a
thread, so it is owned by the one code path guaranteed to run to completion.
A daemon thread watches stdin for a single `q` keypress and closes the response
to end the follow cleanly; it is never started when stdin is not a TTY, so a
piped follow never touches stdin.

### 10.4 Steering and control

| command | effect | when |
|---------|--------|------|
| `steer <id> <msg>` | queue guidance, consumed at the next iteration boundary | redirecting the work |
| `steer <id> --now` | queue it and SIGINT the current iteration | stopping active harm |
| `steer <id> --list` | show what has been queued and what the loop has applied | before steering again |
| `interrupt <id>` | SIGINT the current iteration, no guidance | the agent is stuck in one long call |
| `pause <id>` | hold the loop at the next iteration boundary | you need to look before it continues |
| `unpause <id>` | release the pause | after looking |
| `retry <id>` | wake a pending infra backoff wait now | you know the endpoint is back |
| `budget <id> +N\|N` | change the iteration budget in flight | it is nearly there and nearly out |
| `abort <id>` | terminate the job, state `aborted` | the job is wrong, not just stuck |
| `stop <id>` | shut down and remove an idle finished container | reclaiming resources |

**Three things that look similar are not.** `pause` stops the loop between
iterations and changes nothing else — the run stays non-terminal and resumes
exactly where it was. `interrupt` (and `steer --now`) cancels the work
*inside* the current iteration; the loop continues to the next one. `abort`
and `stop` end the job: the run reaches a terminal state and no further
iteration runs. Reach for `pause` when you want to think, `interrupt` when the
agent is doing something actively wrong, and `abort` when the job itself is
the mistake.

`steer` takes its message from a positional argument, `--file <f>`, or stdin.
The on-disk filename is always `NNN-<slug>.md` with an engine-assigned
monotonic `NNN` the caller never supplies; a `--name` that already carries its
own `NNN-` prefix has it stripped first, so a slug copy-pasted from an earlier
steering file does not yield `022-019-steering.md`. Steering is cheap and safe
because it applies at a boundary; `--now` is the sharp version, for stopping
harm rather than reprioritizing.

**Steering is not write-only.** `steer <id> --list` prints the same history the
hub's run-detail steering panel shows — sequence, `pending`/`applied` state,
arrival time, `--name`, and a one-line preview of each message, with `--json`
carrying every body in full. It asks the live engine first, because the engine is
the process that decides when an entry becomes `applied`, and falls back to
reading the run dir's `steering/` directory when the container is gone (one
notice on stderr, the same wording `logs` and `tasks` use). `--list` is a read:
it never touches stdin, and combining it with a message, `--file`, `--name` or
`--now` is a usage error rather than a send.

`retry` targets a **degraded** run — `health: degraded` with a populated
`infraWait`, sitting out an LLM-endpoint outage on an escalating backoff. It
posts `POST /retry`, cutting the current wait short and attempting the phase
again immediately, and resets the outage-budget episode clock so the wait
accumulated so far stops counting against `infra_outage_budget_s`. The attempt
number keeps escalating, so the *next* automatic backoff is unchanged. It never
unpauses a paused run and never touches steering. Exit `5` when the run is not
in an infra wait.

`budget` changes a running job's iteration budget through
`PATCH /config/budget` without restarting the container — the answer to "the
job is nearly there and nearly out of iterations". `+10` is relative, a bare
`40` absolute; a bare `-5` is an *absolute* -5 and is rejected rather than read
as a decrement, so lowering a budget means passing the value you want. The new
value takes effect at the next boundary and every accepted change emits a
`budget_changed` audit event. It is a live-engine change only, because
`/config/job.yaml` is a read-only mount the engine never rewrites — use
`resume <id> --iterations +N` when the new budget has to survive a fresh
container. Exit `5` when the engine refuses (the result would fall below
`iterationsUsed`, or the job is finished), `2` for a malformed spec.

`abort` terminates the job, honouring the run's on-complete mode. `stop` shuts
down an idle *finished* container — `/shutdown`, then `docker rm -f`, then
reaping the run's siblings — and refuses a still-running job with exit `5`
(abort first, or `--force` to abort and stop in one step); it never deletes the
run dir. **Neither can be undone by self-recovery:** both record the operator's
intent as `operator-termination.json`, which is what stops
`ralphctl doctor --fix` from resurrecting the run later. `stop --force` is the
case that marker exists for, since it removes the container while `status.json`
may still say `running`, which on disk is otherwise indistinguishable from a
crash.

`rm` deletes a run's registry dir — history, artifacts, and the workspace if it
was internal — plus its persisted config dir. It requires the container to be
gone (exit `5` otherwise) and asks for confirmation on a TTY unless `--yes` is
given; declining exits `1`.

`rm --force` makes disposing of a finished run **one** command instead of `stop`
then `rm`: it runs exactly `stop`'s teardown first (`/shutdown`, `docker rm -f`
the job container, reap the run's siblings, record the operator-termination
marker) and then deletes both directories. It is a shortcut past a *stale*
container, not a way to kill live work — it deletes only when the run's recorded
state is terminal, and anything else (`starting`/`running`, an unrecognized
state, a missing or unreadable `status.json`) exits `5` having touched nothing at
all: no container removed, no directory deleted. Killing a live job stays
explicit (`abort`, or `stop --force`). A run with no container record takes the
plain path, so a zombie run dir still recording `running` remains deletable as
before.

`repair <id>` sits one step outside the control set: it does not talk to a
running engine but validates a run dir left inconsistent by a crash, reporting
every issue it finds — malformed JSON, missing fields, unrecognized `state` or
task `status` values, a `schemaVersion` newer than this build knows, duplicate
task ids, and the dangling-container condition — without guessing at a fix. Two
guarded flags do write: `--set-state <state>` overwrites `status.json`'s
`state`, adding a `reason` naming the vanished container when the run really
was a zombie; `--env KEY=VAL` (repeatable) updates `env-wiring.json` in place,
preserving key order and mode `0600` and recording only the key name — never
the value, in stdout, stderr, or the `type: repair` audit line every invocation
appends to `events.jsonl`. Like `resume`, it refuses a live container (exit
`5`), because a live engine owns that run dir's state. Its dangling-container
check, and the remedy text it prints, are one implementation shared with
`doctor`'s `danglingRegistryEntries` report and `status`' own warning, so the
three surfaces can never disagree about whether a run is a zombie or recommend
different next commands.

### 10.5 Inspecting a finished job

`ralphctl status <id>` is the one-screen answer. It prefers the live API and
falls back to the run dir when the container is gone, reconstructing the fields
the engine would have computed (`health`, `infraWait`, `reflect`, and the
`tasks` counts via the shared `task_counts`) so both presentations carry the
same information. Human output is a block of aligned labels:

```
run:       v05-smoke
state:     running  (live api: True)
verdict:   unverified
duration:  1h 12m  (elapsed)
started:   2026-08-19 09:14:02 +0300
phase:     review  approach 2/3
iteration: 5/8
model:     amazon-bedrock/eu.anthropic.claude-opus-5  (gateway id: eu.anthropic.claude-opus-5)
image:     ralphd:9f2c1a4b7d80  (id 0f1e2d3c4b5a)
tasks:     5/7 completed (1 in-progress, 1 pending)
usage:     $0.56, 625k tokens (planning $0.10 / worker $0.40 / review $0.06)
```

Three of those lines answer "which run am I actually looking at", and each is
printed only when the run state carries it. `phase:` carries the **approach
counter against its limit** — `approach 2/3` when the run's `maxApproaches` is
recorded, a bare `approach 2` when it is not (a pre-v0.6 run dir, where the
denominator is unknown and is never guessed from this host's config), and no
approach segment at all for a run that has not entered the review ladder.
`model:` names the id `pi`
**resolved**, as observed in its own message stream, not the ref the operator
asked for — which is `null` exactly when nothing was pinned — with the
`(gateway id: …)` suffix only when the provider's own id differs. `image:` names
the job image the container actually got, reference plus the daemon's short id.
The `model:` and `image:` lines are omitted rather than printed as `None`, and
`--json` carries the raw
fields (`approach`/`maxApproaches`, `model`/`modelRaw`,
`image`/`imageId`/`imageSource`/`imageHash`/`imageBase`/`imageDockerfile`),
explicitly `null` when unknown.

Additional lines appear only when they have something to say, which keeps a
healthy run's output short:

- `reason:` — the terminal state's explanation, wrapped across lines.
- `degraded:` — the run is sitting out an infra outage: attempt number, phase,
  a ticking countdown to `nextAttemptAt`, the episode's wait against the outage
  budget, and the underlying `error`. A degraded run *between* two waits (the
  retry attempt itself is running, `infraWait` back to `null`) says so
  explicitly. A run that merely looks stuck at `state: running` is the failure
  mode this line exists to eliminate.
- `container:` — in bold red, the zombie warning: the container appears gone
  while `status.json` still records a non-terminal state, so this run stopped
  without recording a terminal state; diagnose with `ralphctl repair <id>`.
  Said once, explicitly, rather than leaving the operator to join
  `state: running` and an unreachable-API note printed lines apart.
- `reflection:` — `failed (<error>)` when a post-terminal reflect iteration
  failed. It needs its own line precisely because that failure deliberately
  leaves `state`, `verdict` and `reason` untouched: otherwise the only trace is
  a file in the artifacts tree, and the run looks like reflect was never on.
- `auto-resume:` — the crash-loop guard gave up after N attempts. A run
  self-recovery has stopped retrying is otherwise invisible: it sits recorded
  `running` with no container and nothing ever happens again.
- `!! UNCONSUMED STEERING:` — in bold, guidance queued but not yet picked up.

`ralphctl tasks <id>` prints one line per task, `[<status>] <id> <title>`,
asking the live `GET /tasks` first and reading the run dir's `tasks.json` when
the container is gone. Neither path ever answers "no tasks" for a plan it merely
failed to parse: the agent rewrites `tasks.json` by truncate-and-write, so a read
that lands mid-write is re-read briefly and then served from the last version
that *did* parse, marked stale on stderr (§5.3). With `--json` it is the raw
payload, carrying each task's `successCriteria`, `dependsOn`, `priority` and
`validationNotes`, plus the `tasksStale`/`tasksSource` flags that say which read
answered.

**Four commands answer the questions `status` deliberately does not.** Each reads
the run dir alone (§10.1) and takes `--json`:

| command | the question |
|---------|--------------|
| `iteration <id> <n>` | what happened to iteration `n`: phase, start/end, duration, why it ended, its own tokens and cost, and its full transcript (`--no-log` for the header alone) |
| `fault <id>` | why the run is in trouble: the fault's class, the signature its error text matched, the retry ladder so far, what is left of the outage budget |
| `cost <id>` | what it spent, per phase and per approach, with quoted, derived and unavailable money each labelled as such (§8.6) |
| `docs <id> [name]` | the prose the run left behind — `notes.md`, `review-findings.md`, `composite-prd.md` — and the effective `job.yaml` with secret values redacted |

A missing iteration exits `1` naming the ones that do exist, an unknown document
name exits `2` listing the keys, and an unknown run is exit `3` as everywhere
else. Each of the four renders the same text its hub dialog shows (§11.3), from
one shared shaping in `engine/state.py`, so the terminal and the browser cannot
grow two vocabularies for the same file.

`ralphctl artifacts <id> ls|pull [dest]` reads `<run-dir>/artifacts` **directly
off disk**, so it works for a dead container, a finished run, and a run whose
image no longer exists. `ls` prints `size  path` per file, or `(no artifacts)`;
`pull` copies the tree to `./artifacts` unless another destination is given.

Where the job's own output lands, all of it under `~/.ralphd/runs/<id>/`:

| output | location |
|--------|----------|
| artifacts the agent produced | `artifacts/` |
| review findings | `review-findings.md` |
| agent working notes | `notes.md` |
| per-approach archive of the three above | `approaches/<NN>/` |
| reflection report | `artifacts/reflection/report.md` |
| failed reflection | `artifacts/reflection/FAILED.md` |
| per-iteration metadata | `iterations/<n>/meta.json` |
| per-iteration transcript | `iterations/<n>/output.jsonl` |

**`verdict: "verified"` is the only success signal.** A `state: succeeded`
without it cannot occur, and a `failed` run still leaves useful state — read
`review-findings.md` and `notes.md` before resuming or starting a new job.

### 10.6 Machine-readable output

`--json` is a global flag and a stable interface, not a debugging convenience:
it is what an external orchestrator drives ralphd through, and it is
deliberately sufficient to run a job end to end without ever parsing human
output. It is honoured by `start`, `runs`, `status`, `tasks`, `watch`,
`resume`, `steer` (including `--list`), `interrupt`, `pause`, `unpause`,
`retry`, `budget`, `abort`, `stop`, `rm`, `repair`, `iteration`, `fault`,
`cost`, `docs`, `artifacts`, `doctor`, `config`, and the `ls`-style actions of
`skills`, `creds`, `prompts` and `llm`. `logs` uses `--raw`
instead, since its machine form is the engine's own NDJSON.

The shape follows two rules. Proxying commands print the engine's response
verbatim — `budget` emits
`{"iterations": 40, "previous": 25, "iterationsUsed": 17}`, `retry` emits
`{"retrying": true}` — so the CLI never becomes a second schema to keep in
sync. Commands that compute something add fields rather than replacing them:
`status --json` carries `live`, `containerGone`, `autoResume`,
`durationSeconds`, `sinceLastUpdateSeconds` and
`currentIteration.elapsedSeconds` beside the engine's own fields, and every
absolute-time field keeps its raw ISO value while human renderings live in
separate `*Local` keys.

For a supervising script, `watch --json` is the completion primitive:

```console
$ ralphctl --json watch <id> | jq -c 'select(.type=="state")'
```

It exits at the run's real terminus, including on a resumed run whose log
still carries an earlier episode's terminal event (§10.3).

`ralphctl runs` sorts with `--sort {approach,iterationsUsed,phase,runId,
startedAt,state,tasks,verdict}` and `--reverse`, defaulting to `startedAt`
descending — newest first, not the alphabetical order a directory listing
yields. Keys sort on raw payload values, not rendered cell text:
`iterationsUsed` numerically rather than on the `"17/250"` string, `approach`
on the bare counter rather than on the rendered `10/12`, `tasks` on the
**completion ratio** so `5/7` outranks `100/250`, `startedAt`
on the parsed instant so ISO values with different UTC offsets order correctly,
and `state`/`verdict` in lifecycle order (`starting → running → succeeded →
failed → aborted`, and no-verdict → `unverified` → `verified`). Missing values
sort last, and the run id breaks ties so the order is total and stable.
`startedAt`, `iterationsUsed` and `approach` start descending; `--reverse`
flips whichever direction the key starts with. A run with no plan at all has no
ratio rather than a zero one, so it sorts last ascending instead of pretending to
be 0% done — the same "unknown is not zero" rule that keeps its TASKS cell blank
instead of `0/0`.

**Those eight keys are exactly the hub's eight sortable columns, and both
surfaces use one implementation** (`sort_run_rows`/`RUN_SORT_KEYS` in
`main.py`, mirrored by `RUN_COLUMNS` in `app.js` against the same raw fields).
`--json` emits the human table's rows in the same sequence, so a script and a
reader never disagree about which run is first, and it carries the raw counts
(`approach`/`maxApproaches`, `tasksCompleted`/`tasksTotal` and the
validation-failed / in-progress flags) beside the rendered cells.

### 10.7 Exit codes

| code | meaning | examples |
|------|---------|----------|
| `0` | success | including a fallback that served on-disk data |
| `1` | generic error | `docker run` failed; malformed `status.json`; a `doctor`/`repair` run that found issues; `--no-detach` finishing unverified; a declined `rm` confirmation; an `iteration <n>` or `docs <name>` that was never written |
| `2` | usage error | argparse rejection; missing `--prd`; a run id that already exists; a `--workspace`/`--creds`/`--skills` path that is not a directory; an unnamed second `--workspace`; a malformed `--iterations`/`budget` spec; an invalid `--set-state` value or `--env KEY=VAL`; an invalid `prompts set` phase; a non-`.env` `creds add`; a `skills add` dir with no `SKILL.md`; an unknown `config` key or invalid value; an unknown `docs` name; `steer --list` combined with a message to send; two of `--image`/`--base-image`/`--dockerfile` set at one level; a `--dockerfile` path that is not a file with a `FROM` |
| `3` | run or profile not found | no such run dir; an unknown `--template`; an unknown LLM profile name |
| `4` | container or API unreachable | `API unreachable: …`; no `apiUrl` recorded; `could not connect to event stream` |
| `5` | operation invalid in the current job state | HTTP `409` from the engine; `resume`/`repair` against a running container; `stop` on a live job without `--force`; `rm` while the container exists; `rm --force` on a run whose recorded state is not terminal; `budget` below `iterationsUsed`; `steer` on a finished job; `retry` on a run that is not waiting |
| `130` | a follow was interrupted with Ctrl+C | `logs -f`, `logsf` |

Code `5` is the one worth designing against: the command was well-formed and
the run exists, but the run is in the wrong state for it. An orchestrator can
treat `5` as "re-read `status` and pick a different verb", distinct from `2`
(fix the command) and `4` (retry later).

`130` follows the shell convention that a process terminated by signal N exits
`128+N`, so SIGINT gives 130. `^C` out of a follow is a normal user-requested
stop, not a crash: it is caught explicitly rather than left to Python's default
handler, so it produces no traceback, and `_TerminalModeGuard` has already
restored the terminal by the time the process exits (§10.3). `doctor --fix`
never changes `doctor`'s exit code — it stays the AND of the preflight checks,
because a recovery sweep's outcome is reported in its own fields and must not
turn a healthy host into a failure.

## 11. Hub UI

### 11.1 Serving it

`ralphctl ui [--port N] [--bind ADDR]` starts the local web hub in the
foreground and prints where it is:

```console
$ ralphctl ui
ralphctl: serving hub at http://127.0.0.1:41235 (registry: /home/you/.ralphd)
```

`--port` defaults to a free ephemeral port. Ctrl-C stops it; the socket is
closed on the way out. The server is `src/ralphd/cli/ui_server.py`: a
`ThreadingHTTPServer` on `http.server` and `urllib`, with no `fastapi` or
`uvicorn` on this path even though those are dependencies of the engine side of
the same package. The hub is a host-side convenience and must not drag a web
stack into a `pipx` install.

It is also **not a second source of truth**: every JSON endpoint under `/api/`
either reads `~/.ralphd/runs/*` off disk or proxies the run's live container API
and falls back to disk when that does not answer. Nothing is cached, no state is
written, there is no database, and a dead run never produces an error — only
stale-but-valid data marked as such.

Everything that is not `/api/` is served from the static bundle packaged in the
wheel, `src/ralphd/cli/web/`: `index.html`, `app.js`, `style.css`. Plain
hand-written HTML, JavaScript and CSS — **no npm, no node, no bundler, no
build step**, and no separate frontend server process. `index.html` is 19 lines
and loads one script; the bundle talks only to the JSON endpoints, via
`fetch()`, and re-renders on a 4-second timer. A path that does not match a
real file under `web/` falls back to `index.html`, so a browser refresh on a
run's detail URL still loads the app shell (routing is client-side via
`location.hash`); a path that tries to escape the static root is refused with
`403`; and if the bundle is missing from an installed build, non-`/api` paths
return a plain-text "static hub bundle not installed in this build" rather
than crashing the server.

### 11.2 Run list

The default view (`#/`) is a table of every run under the registry, refreshed
every 4 seconds, with the run id linking to its detail view. Its eight columns
are exactly the eight `ralphctl runs --sort` keys:

`RUN` · `STATE` · `VERDICT` · `PHASE` · `APPROACH` · `TASKS` · `ITERATIONS` ·
`STARTED`

Every header is click-to-sort; clicking the active column reverses it; the
active column carries a `▲`/`▼` indicator and an `aria-sort` attribute. The
default is `STARTED` descending — newest first. Sort keys are the raw payload
values, not the rendered cell text, so `ITERATIONS` sorts numerically on
`iterationsUsed` rather than on the `"17/250"` string, `TASKS` on the
`tasksCompleted`/`tasksTotal` ratio rather than on `"5/7"` (a run with no plan
has no value and sorts last ascending), `STARTED` on the parsed instant, and
`STATE`/`VERDICT` in lifecycle order.

**The cells themselves are rendered by the server**, not composed in the
browser: `approachDisplay` (`2/3`, bare `2` when the run recorded no ceiling,
empty when it never reached an approach), `tasksDisplay` with its trouble flags
and stale pill, `costDisplay`. The raw numbers travel beside them for sorting.
The same strings are what `ralphctl runs` prints, from the same formatters in
`engine/state.py`, so a terminal and a browser tab cannot word the same run
differently.

**The chosen sort lives outside the DOM.** `load()` rebuilds the whole table
every 4 seconds, so sort state held in the markup would be destroyed on each
refresh; it is a module-level object in `app.js` instead, read on every rebuild.

A run flagged `containerGone` gets a highlighted row (`tr.row-warning`) and a
`⚠ container gone` marker next to its state pill, so a zombie does not look
like a healthy `running` run in the list either.

The list endpoint reads `status.json` and nothing else per run, plus exactly one
hardened `tasks.json` read per row (§5.3, `persist=False`: a viewer writes
nothing into somebody else's run dir), so listing stays cheap however many dead
runs the registry holds and **makes no live API call at all** while rendering.
Only rows that *could* be zombies — recorded state non-terminal — are probed,
concurrently, with a bare loopback TCP connect on a 0.3s timeout and a thread
pool capped at 8; no `docker` CLI is involved, deliberately, since the hub has to
work where the docker socket is not. A terminal run is unreachable by design and
always reports `containerGone: false`.

### 11.3 Run detail

`#/run/<id>` renders one run from a single payload —
`{runId, live, containerGone, deletable, deleteRefusal, status, tasks,
iterations}` — where
`status` and `tasks` are proxied from the container when its API answers and read
from disk otherwise, and `iterations` is always read from
`iterations/*/meta.json`. Every panel below it is one further endpoint, and only
four of them have a live answer to prefer at all: the run payload, the log tail,
the PRD and the steering history proxy the container and fall back to the run dir;
the iteration, document, artifact, fault and cost views are **on-disk only by
design**, because the engine, the agent and `start` write those files into the run
dir themselves and a live proxy would only add a way to disagree.

- **Summary card** — state, verdict, phase, approach as `2/3`, iterations,
  duration, and an explicit provenance row: `live: yes (proxied from container)`
  or `no (on-disk snapshot)`.
- **Usage panel** — total tokens and cost, plus the `byPhase` and `byApproach`
  breakdowns when present. The cost cell opens a **cost dialog**: the same
  per-phase and per-approach breakdown `ralphctl cost` prints, with quoted,
  derived and unavailable money each labelled as such (§8.6).
- **Task table** — one row per task, each clickable and keyboard-reachable
  (`role="button"`, `tabindex=0`, Enter and Space), opening that task's detail
  in a modal `<dialog>`: `status`, `priority`, `dependsOn` when set,
  `successCriteria` — the text the task is actually judged against — and any
  `validationNotes`. The records are already in the run-detail payload, so
  opening one costs no request. A read served from the last-good payload (§5.3)
  is labelled `stale` with the reader's own sentence, and the table keeps showing
  the plan instead of blinking empty while an agent rewrites `tasks.json`.
- **Iteration timeline** — per iteration: `#N`, absolute local start time,
  phase, model, an error pill when it failed, and duration once it ended. Each
  row opens an **iteration dialog** carrying that iteration's whole story —
  phase, timestamps, duration, exit reason, tokens, cost and its full rendered
  transcript — byte-identical to what `ralphctl iteration <id> <n>` prints.
- **State documents panel** — one row per run document: the worker's `notes.md`,
  `review-findings.md`, `composite-prd.md` and the run's `job.yaml` **with
  secrets redacted**, each opening its body in a dialog. A document that was
  never written is listed as such rather than omitted: absence is an answer.
- **Artifacts panel** — what the job left behind under `artifacts/`, size and
  path per entry, with a text artifact (the reflection report first among them)
  opening in a dialog and a binary one reported as such rather than mangled.
- **PRD dialog** — a *view PRD* button fed by the run's PRD endpoint, which
  proxies the live route and falls back to the run dir, so it works for a dead
  run too. Which file counts as "the PRD" — `composite-prd.md` when the engine
  composed one, else `prd.md` — is decided by the one shared helper
  `ralphd.engine.state.prd_path` that the engine's own route uses, so the live
  and on-disk answers can never disagree. A run dir with no PRD answers with
  the single line `(no PRD recorded)` rather than an empty string.
- **Steering form and history** — the form posts to the hub's steer endpoint,
  which forwards to the run's live `POST /steering`, and reports the created file
  name back; under it, every message the run has received, oldest first, with its
  arrival time and a `pending`/`applied` pill, each opening its full body in a
  dialog. A message queued from the form appears immediately rather than after
  the next poll, and flips to `applied` at the iteration boundary that consumed
  it.
- **Degraded card** — `health: degraded`/`infraWait` set produces a distinct
  `.card.degraded` carrying the attempt number, phase, error, the episode's
  wait against the outage budget, and a countdown to `nextAttemptAt` ticking
  every second, plus a **retry now** button doing exactly what `ralphctl retry`
  does. The button appears only while a backoff wait is pending *and* the run's
  API is reachable; on a dead run the card says `read-only on-disk snapshot`
  and offers none. The engine's status code is passed through, notably its
  `409 not waiting on an infra fault`, so the UI can say "nothing to wake"
  instead of reporting a generic failure. The badge itself opens a **fault
  dialog**: `ralphctl fault`'s explanation of what went wrong, which signals
  classified it (§8.3) and what the loop did about it.
- **Container-gone warning** — a run recorded `starting`/`running` whose API
  has stopped answering gets `.card.warning` (one CSS rule shared with
  `.card.degraded`) plus a `.container-gone` block pointing at
  `ralphctl repair <run-id>` for the authoritative docker-side diagnosis. The
  hub only knows the API stopped answering — it never consults docker — so it
  names the command that can. Without this block the only hint would be the
  `live: no (on-disk snapshot)` row, which reads identically for a finished run
  that is unreachable by design.
- **Failed reflection** — `reflection: failed (<error>)` on a `.reflect-failed`
  line, in the same wording `ralphctl status` uses and for the same reason: the
  failure leaves `state`, `verdict` and `reason` untouched.
- **Delete affordance** — a **finished** run can be deleted from here, which is
  the browser end of `ralphctl rm --force`: the button is enabled exactly when
  the server says the deletion would be accepted (`deletable`, with the refusal
  sentence when it would not), and pressing it opens a confirm dialog that
  requires the run id to be **typed back** before `DELETE /api/runs/<id>` is
  sent. A non-terminal run is refused by the server, not merely hidden by the
  UI, and the deletion runs through the CLI's own removal code rather than a
  second implementation.

Only one dialog exists at a time and closing it removes it, so the 4-second
refresh running behind it cannot accumulate copies. Every dialog body is built
from text nodes — the strings in them are agent- and provider-authored — and is
formatted by the *server*, using the same modules `ralphctl` prints with, so the
browser displays exactly what the terminal shows.

### 11.4 Live transcript

The detail view carries a transcript tail, requested with `?tail=200`. The
server fetches the run's **full** raw NDJSON backlog from the container, runs
it through the exact same `ralphd.cli.log_render.render_to_lines` that
`ralphctl logs` uses (with `tty=False`, so no ANSI and no carriage returns),
and only then trims to the last `tail` *rendered* lines — the same contract as
`logs -N`. The response, `{"live": bool, "lines": [...]}`, is never an error.

**Rendering is server-side, and there is exactly one renderer.** `app.js`
receives finished lines and displays one per element; it implements no
event-to-text rules of its own. That is what guarantees the hub and
`ralphctl logs` agree line for line, including collapsing a many-delta thinking
block to exactly one `[thinking…]` marker. A client-side reimplementation
appends one element per delta event and drifts from the CLI the moment either
side changes.

When the run's API is not reachable, `live` is `false` and the lines come from
the on-disk transcript merge — the very same merge the engine serves from
inside the container — so a dead run's log stays readable in the hub. Only
*following* needs a live container. `app.js` labels such a tail
`(on-disk snapshot — the run's API is not reachable, not following)`, in the
same wording style as the summary card's `live: no (on-disk snapshot)` row. A
run with no transcript at all yields the single line `(no transcript yet)`,
from the same constant `ralphctl logs` prints, never an empty array.

### 11.5 Rendering rules

Two disciplines hold everywhere in the bundle, and both are the difference
between a correct display and a confidently wrong one.

**Cost is rendered from `costDisplay` and never re-derived in the browser.**
The server adds a `costDisplay` string — produced by the shared
`ralphd.engine.state.format_cost` — to the usage total and to every `byPhase`
and `byApproach` bucket, leaving the raw `costUSD`/`costStatus` fields untouched
beside it. `app.js` prefers that string:

```js
const costText = usage.costDisplay ?? (cost != null ? "$" + Number(cost).toFixed(4) : null);
```

Some gateways bill tokens and quote no price, which ralphd records as *unknown*
rather than zero, and `Number(...).toFixed(4)` turns an unknown into a confident
`$0.0000` — which is how an unknown cost becomes a free one in the operator's
head. Only the server-side formatter knows the difference between unknown,
mixed, derived (`~$0.45 derived`) and genuinely zero, so only it may produce
the string. The same rule governs absolute times: the server sends
`startedAtLocal`, `endedAtLocal` and `updatedAtLocal` next to — never replacing
— the ISO fields, and the browser displays them rather than reimplementing the
format, keeping the ISO values for sorting and machine use.

**Payload text is inserted as text, never as markup.** The `h()` helper in
`app.js` appends every string child as a `document.createTextNode`; the PRD
dialog, the task dialog and each transcript line (one `.lg-line` element apiece)
are written with `textContent`. `innerHTML` is not used for payload text
anywhere. PRDs, task success criteria and agent transcripts are agent- and
operator-authored prose from outside the page's trust boundary; rendering them
as HTML would both invite injection and mangle the `<`-heavy, backtick-heavy,
fenced-snippet text the operator opened the dialog to read.

Redaction happens upstream of both surfaces: secret values are scrubbed at
write and serve time, so a `bash` command or file path shown in a rendered
line is exactly what a `--raw` reader already sees. Neither the pretty renderer
nor the hub is a new exposure surface.
## 12. Artifacts and notifications

A job produces two kinds of output: the code it changed (in the workspace, under
the operator's own version control) and everything it wants the operator to
*read*. The second kind lives in one directory, `artifacts/`, inside the run
dir. That separation is what makes "the job is over, what did it actually
produce" answerable without reading a transcript.

### 12.1 The artifacts directory

`<run-dir>/artifacts/` is created unconditionally when the run dir is
initialised, alongside `steering/`, `iterations/` and `approaches/` — so it exists
(empty) even for a job that never writes one file, and no surface has to
distinguish "missing" from "empty". Because the whole run dir is bind-mounted at
`/run/ralphd` in the container, everything written there is visible host-side at
`~/.ralphd/runs/<run-id>/artifacts/` the instant it is written, while the job is
still running.

**The agent owns it; the engine writes exactly one file there.** Every phase
prompt carries the path in its `Job context` block (`- Artifacts directory:
<path>`), and the worker prompt states the rule from both sides — "Do not touch
the run state directory except tasks.json, the notes file, and the artifacts
directory" and "Put anything the operator should see (reports, screenshots,
logs) in the artifacts directory". The only engine-written file under
`artifacts/` is `reflection/FAILED.md` (§12.2).

| path | writer | content |
| --- | --- | --- |
| `artifacts/reflection/report.md` | reflect iteration | the post-mortem; its existence is the reflect phase's success condition |
| `artifacts/reflection/suggestions.diff` | reflect iteration | a unified diff of proposed prompt/skill edits, never applied |
| `artifacts/reflection/FAILED.md` | engine | written only when the reflect phase produced no report |
| `artifacts/reports/*.md` | agent | evidence the PRD asked for, ideally machine-checked (§12.3) |
| `artifacts/screenshots/**` | agent | browser-driven verification of a UI change |
| anything else | agent | logs, samples, gate records — free-form by design |

Host-side access is a plain filesystem read, never an API call:

```
$ ralphctl artifacts <run-id> ls
     11147  reflection/report.md
      8514  reflection/suggestions.diff
     19904  reports/issue-traceability.md
$ ralphctl artifacts <run-id> pull ./out
artifacts copied to ./out
```

`ls` walks the tree recursively and prints size + path relative to
`artifacts/`, sorted, or `(no artifacts)`. `pull <dest>` (default `./artifacts`)
copies the tree with `copytree(..., dirs_exist_ok=True)`. **Both work on a dead
run**: nothing goes through the container's HTTP API, so artifacts survive the
container exactly as long as the run dir does. `ralphctl rm` deletes the run dir
and the config dir together, artifacts included — pull first.

### 12.2 Reflection

Reflection is the post-job post-mortem: opt-in (`reflect: true` in the job
config, `ralphctl start --reflect`), one extra iteration, run strictly *after*
the loop has reached a terminal state.

- **Exactly one iteration, outside the budget gate.** `run_job()` calls
  `_run_reflection()` once after `_run_job_core()` returns; it is not subject to
  `budget_left()`, so a job that exhausted its iteration budget still gets its
  post-mortem. The phase name is `reflect`, its prompt is the builtin
  `src/ralphd/prompts/reflect.md`, and its model is resolved by the job's model
  strategy like any other phase.
- **What it produces.** `artifacts/reflection/report.md` — what worked, what
  didn't, and concrete suggestions for the loop's own material — plus, when it
  has a specific textual change to propose, a `suggestions.diff` beside it: a
  unified diff against the prompt or skill files. **The diff is a proposal,
  never applied.** The prompt says so, and the value of the phase depends on it:
  a loop that edits its own prompts mid-flight makes every later iteration
  unreproducible.
- **What it may touch.** The prompt's hard constraint is `artifacts/reflection/`
  and nothing else: not the workspace, not `tasks.json`, `status.json`,
  `notes.md`, `review-findings.md`, steering files, nor anything under
  `iterations/`. That is instructed, not sandboxed — reflect has the same tool
  access as every other phase. The engine-level guarantee is narrower and
  absolute: the terminal `state`, `verdict` and `endedAt` are written *before*
  reflect starts and are never touched by it, and `phase` resets to `None` when
  it finishes, so a terminal run never looks mid-phase.
- **It retries on an infra-shaped ending.** `reflect` is in
  `INFRA_RETRY_PHASES`, so a dead endpoint during the post-mortem is retried like
  any other phase, with two adjustments that follow from the job being over. Its
  outage budget is capped at `REFLECT_OUTAGE_BUDGET_S` (300s, rather than the
  job's `infra_outage_budget_s`), because nothing is waiting on it; and when the
  job itself ended on an infra-shaped failure, reflect waits one backoff step
  (`reflect_infra_delay`) *before* its first attempt instead of firing into the
  same dead gateway in the same second. An operator abort gets no countdown and
  keeps its veto — the retry window parks the abort reason and the episode clock
  for its duration and restores them afterwards, so a reflect attempt can never
  rewrite the reason the job terminated with.

**A reflect failure is recorded, never swallowed.** From the outside, "reflect ran
and produced nothing" and "`reflect` was never enabled" look identical — an empty
`artifacts/reflection/` and a terminal run. So the engine leaves an explicit
verdict either way: `status.json` gains `reflect: {ok, error, endedAt}` (`null`
while reflect is disabled or has not ended); a `reflect_done` event carries `ok`
and, on failure, `error`; and on failure `artifacts/reflection/FAILED.md` names
the error, the timestamp, the run id and the job's own terminal state, and points
at the last `reflect` iteration's transcript and the `infra_wait`/`infra_retry`
events already spent on it.

The failure test is deliberately *not* `classify_fault()` — whose fault it was is
a question the retry wrapper already answered. The question here is only whether
the operator ended up with a post-mortem, so anything short of a readable report
counts: an iteration that never ran to completion, an interruption, a timeout or
tripped no-traffic watchdog, any non-empty error text (recorded verbatim), a
non-zero exit (`the reflect agent exited <code>`), and — the case that makes the
rule worth having — **a clean exit that wrote no `report.md`**, recorded as `the
reflect iteration wrote no artifacts/reflection/report.md`. The missing promise
line is the one thing that is *not* a failure: the report on disk is the
deliverable, not the sentinel.

A failed reflection never changes `state`, `verdict` or `reason`; the job is over.
That is exactly why it needs its own surface, and it has one: `ralphctl status`
prints a wrapped `reflection: failed (<error>)` line — and nothing at all for a
successful or absent reflection — and the hub's run-detail card renders the same
wording as a warning, not an error.

### 12.3 Reports

`artifacts/reports/` is a convention, not a mechanism: the PRD asks for a report,
the agent writes it, and — this is the part that makes it worth anything — **a
test in the repo re-reads it and fails when it stops being true.** An
agent-written report is a claim about the tree, and a claim about the tree is
checkable.

The traceability report is the worked example: `issue-traceability.md` maps each
backlog issue to a requirement, a commit and the tests that cover it, and
`tests/test_issue_traceability.py` asserts, against the real repo, that every
issue in `ISSUES_IN_SCOPE` has its own section; that it still quotes a floor of
shas, paths and node ids so a gutted report fails too; and that neither the tree
nor the commit history contains a `gh issue close`, which is the process claim
the report makes about itself.

The *generic* half of that — every 7-to-40-hex sha resolves via `git cat-file`,
every repo-relative path exists, every `::node_id` after such a path is a real
`def`/`async def` in that file — is not one report's privilege: it lives in
`tests/report_claims.py` and `tests/test_report_claims.py` applies it to every
`*.md` the reports directory holds, discovered by glob, so a report added later
is re-read from the day it lands. Two conventions keep that parser honest about
what it cannot check: a path that belongs to somebody else's tree (the
sibling-toolchain recipe's `ci/Dockerfile`) is left outside the repo's own
top-level directories, and a run-dir artifact is written `<run-dir>/artifacts/…`
rather than as a checkout-relative path.

So the failure modes that make a report actively harmful — a commit that never
landed, a test that was renamed away, a module renamed out from under a report, a
process claim nobody kept — are all build failures. A report that records an
investigation rather than a claim about the tree simply has fewer claims to
check, not an exemption. The distinction is worth carrying into a PRD — "write a
report" is weak, "write a report and a test that keeps it honest" is a
deliverable.

Screenshots follow the same rule from the other direction: the browser tier
writes its frames into the job's own artifacts dir (`RALPHD_ARTIFACTS_DIR`,
defaulting to `/run/ralphd/artifacts`), so the evidence for a UI change is
produced by the test that verifies it, not assembled afterwards by hand.

### 12.4 Completion hooks

`on_complete_cmd` is the notification hook, and it is deliberately the only one:
one shell command, run once, with the outcome in its environment. Everything
else — Telegram, Slack, a webhook, `mail`, a systemd unit trigger — is that
command's business, so ralphd ships no channel abstraction, no template language
and no retry queue. Set it with `ralphctl start --on-complete-cmd '<cmd>'` or as
a `job.yaml` field (so a template can supply it); unset is the default and runs
nothing.

- **When.** Once, in `ralphd-engine`'s `amain()`, immediately after
  `loop.run_job()` returns — strictly after the terminal state *and* after the
  reflect iteration when `reflect: true`, since both share that one point. It
  runs before the `on_complete: idle` wait, so an idling debug container has
  already fired its hook.
- **Where.** In the job container, via `asyncio.create_subprocess_shell`, as the
  non-root `agent` user. Not on the host.
- **What it receives.** The engine process's own environment plus
  `RALPHD_RUN_ID` (the configured run id), `RALPHD_STATE` (`succeeded`, `failed`
  or `aborted`) and `RALPHD_VERDICT` (`status.json`'s `verdict` —
  `verified`/`unverified` — or `""` when absent).
- **It cannot change the outcome.** A non-zero exit, or a failure to spawn at
  all, is recorded as an `events.jsonl` `log` event at `level: error` with a
  500-byte tail of stderr/stdout, and otherwise ignored — never the job's
  `state`, `verdict` or the engine's own exit code. Success is logged as
  `on_complete_cmd finished (rc=0)`.

**The security caveat is the environment, not the shell.** The hook inherits the
engine's environment, which is where forwarded LLM credentials live, and it runs
inside the container with the job's docker socket (given `--allow-docker`) and
its credential files at `~/.creds`. A hook that posts
`"$AWS_BEARER_TOKEN_BEDROCK"` to a webhook exfiltrates it, and the redaction of
§13.1 does not help: redaction scrubs what the *engine* persists and serves, not
what a hook sends over the network. Treat `on_complete_cmd` as code running with
the job's full privilege, and keep secrets out of its argument list for the same
reason the credential rules keep them out of tool-call arguments — the command
string is stored in the job config and echoed in the engine log.

---

## 13. Security

Four separate boundaries, each with a different failure mode: what happens to
secrets, what the docker socket grants, who can reach the API, and how much
latitude the agent has inside its own container.

### 13.1 Secrets

**The convention is env files, never flags.** `ralphctl start --creds <dir>`
points at a directory of `<name>.env` files; the CLI stages them into the job's
config dir (`~/.ralphd/configs/<run-id>/creds/`, the dir itself `0700`), which is
mounted read-only at `/config`; and the *engine* — not a shell script, not the
CLI — places them at container startup:

| placed at | mode | note |
| --- | --- | --- |
| `~/.creds/<name>.env` | `0600` | rebuilt in full on every placement, so a `DELETE` really removes the file |
| `~/.gitconfig` | default | copied verbatim |
| `~/.git-credentials` | `0600` | also runs `git config --global credential.helper store` |
| `~/.netrc` | `0600` | |
| `~/.ssh/` | `0700` dirs, `0600` files | recursive copy of `creds/ssh/` |
| — | — | an executable `creds/setup.sh` is run once with `HOME` set, for anything the above cannot express |

Placement happens in `engine/creds.py`, inside the one process that already
promises never to leak values, and it logs credential *names* only. The
inventory reaches the agent the same way: every phase prompt gets a
`## Credentials` section listing `~/.creds/<name>.env` file names with values
withheld, plus the sourcing rule (`set -a; . ~/.creds/<name>.env; set +a`).
Nothing is auto-exported into the agent's environment. Runtime CRUD
(`PUT`/`DELETE /config/creds/{name}`) writes into the writable overlay, not
`/config`, and the next iteration's prompt reflects it.

**Redaction is mechanical, because guidance is not enough.** The prompt rule
("never print, `cat`, `echo` or otherwise dump a credential file's contents, and
never paste a secret value into a command's arguments") is necessary and
insufficient — an agent reads a credential file to use it, and reads a
*container's environment* through `docker inspect` to debug it. So
`engine/redact.py` also enforces the rule the way Jenkins credential masking
does: the engine already knows every secret value it forwards or places, so it
scrubs those values out of everything it persists or serves.

The redaction set is a `value → label` map built from the process environment and
any `PUT /config/llm` env override — filtered to names matching
`TOKEN|KEY|SECRET|PASSWORD` (case-insensitive) or listed in
`KNOWN_LLM_ENV_NAMES` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`GOOGLE_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`, `AWS_BEARER_TOKEN_BEDROCK`) — plus a best-effort `KEY=value`
parse of `~/.creds/*.env` (quotes and a leading `export` stripped, unparseable
lines skipped), `~/.git-credentials` (the password inside the URL) and `~/.netrc`
(`password` fields). It is rebuilt at engine startup after credential placement,
and again after any `PUT`/`DELETE /config/creds/{name}` or `PUT /config/llm`.

Three independent scrub points then apply `scrub_text()`, longest value first so
one secret that is a substring of another is never left half-exposed, replacing
each hit with `[REDACTED:<label>]` (`env:AWS_BEARER_TOKEN_BEDROCK`,
`probe.env:TOKEN`, `git-credentials`, `netrc`):

1. `runner.py` — every line of a `pi` subprocess's stdout, before it is appended
   to that iteration's `output.jsonl`;
2. `state.py` — every event, as its serialised JSON text, before it is appended
   to `events.jsonl`;
3. `api.py` — `GET /logs`, in both tail and follow mode, at serve time, which
   retroactively catches a value only *recognised* as a secret after the line
   was written (a credential added mid-run).

**Honest limits.** Redaction is a value-substring filter over known values, and
that is all it is:

| limit | why it is accepted |
| --- | --- |
| only values `>= MIN_SECRET_LEN` (8) chars | a shorter floor mangles region codes and single words; corrupting ordinary transcript text is worse than the narrow gap |
| only values the engine knows | a token the agent mints itself mid-run (an OAuth exchange, a `curl` response) is not in the set and is not scrubbed |
| exact substring match | a value the agent transforms — base64, split across lines, URL-encoded — passes through |
| the map is never persisted | it is exactly as sensitive as the secrets in it, so writing it into the run dir to enable a second pass would put every secret on disk in plaintext next to the transcript it protects |
| host-side on-disk readers get write-time scrubbing only | `log_merge` takes its `scrub` callback as an injected argument; host-side callers pass nothing rather than pretending. A dead run's snapshot is exactly as scrubbed as the file, and misses only scrub point 3's retroactive catch |

**Never in an image, never in a commit.** No credential is baked into the
container image: the image ships the engine, `pi`, a docker client and a browser,
and receives everything job-specific through the read-only `/config` mount.
Nothing under `~/.ralphd/` belongs in version control, and the review phase
independently checks the workspace for "secrets in committed files".

**What stays your responsibility.** Three things the engine cannot do for you:

- **Secrets at rest on the host are plaintext `0600` files.** The config dir is
  `0700` and holds `llm-wiring.json` (the resolved `--llm` env, so `resume`
  reproduces the wiring from a different shell), `env-wiring.json` (the ordered
  `--forward-env`/`--llm-env`/`--env` pairs), `pi/models.json` (a resolved
  `apiKey`) and the staged `creds/` copies; the run dir's `.api-token` is `0600`.
  That is filesystem permissions, not encryption, and there is no keyring
  integration. `ralphctl repair --env KEY=VAL` edits the persisted wiring in
  place without echoing a value — its audit event records key names only.
- **Scope the credential, not the prompt.** The API bearer token is equivalent to
  the job's credentials, and the creds API is full CRUD including read-back. A
  job gets whatever the credential it was handed can do; hand it a scoped one.
- **Read a transcript before publishing it.** Transcripts are verbatim tool
  arguments and output, scrubbed for known values — a bound, not a guarantee.

### 13.2 Docker access

`ralphctl start --allow-docker` mounts the host docker socket (default
`/var/run/docker.sock`, overridable via `RALPHD_DOCKER_SOCK`) and adds its group
with `--group-add <gid>`, computed at launch because the gid differs per host.
Containers the job starts are **siblings** on the host daemon, not children.

**This is root on the host, stated plainly.** A job holding the socket can
`docker run --privileged -v /:/host …` and own the machine; the non-root `agent`
user, the read-only `/config` mount and every other bit of container hardening
are irrelevant the moment the socket is there. There is no partial-trust variant,
because a socket proxy that permits `run` at all permits arbitrary mounts — so
the CLI prints a loud warning at launch and the guidance is the honest one: use
it only with PRDs you trust as much as your own shell. Without the flag there is
no socket, which is the default.

Since the escalation is all-or-nothing, the mitigations are about the job not
destroying *itself*:

| mitigation | mechanism |
| --- | --- |
| the job container is identifiable | always labelled `ralphd.run=<run-id>` **and** `ralphd.role=job`, with or without `--allow-docker` |
| siblings are identifiable | prompts instruct labelling every sibling `ralphd.run=<run-id>` plus `ralphd.role=sibling`, and preferring `--rm` |
| the job knows its own id | `RALPHD_SELF_CONTAINER_ID` carries the `ralphd-<run-id>` name the CLI chose — known before `docker run` returns, and accepted anywhere docker accepts an id |
| cleanup cannot self-destruct | the documented idiom is two-filter, always: `docker ps -aq --filter label=ralphd.run=$RALPHD_RUN_ID --filter label=ralphd.role=sibling` |

**Never clean up by the run label alone.** The one-filter form matches the job
container too, so `docker rm -f` over it kills the run mid-iteration, loses that
iteration's work and transcript, and leaves the run dir non-terminal. That is why
the two-filter form appears in every copy of the idiom — phase prompt, example
skill, architecture doc, CLI doc — and why three tests keep it that way (§14.3).
In-container cleanup is optional anyway: end-of-run reaping is the CLI's job, and
`ralphctl stop`/`rm` reap by run label alone, host-side and deliberately, because
there the job container *should* go too. Labelled images and volumes survive both
and are your cleanup.

### 13.3 API exposure

The engine's HTTP API is the job's whole control surface, and its exposure is two
independent settings.

- **Default: loopback, no auth.** The container's port 7777 is published as
  `-p 127.0.0.1:<port>:7777`. Nothing off the host can reach it, so nothing more
  is required to make that true.
- **Token.** `--api-token <t>` (or `auto`, which generates
  `secrets.token_urlsafe(24)`) makes a middleware require `Authorization: Bearer
  <t>` on every route except `/healthz`, answering `401` with `{"title":
  "unauthorized", "status": 401}` otherwise. The CLI writes the token to the run
  dir's `.api-token` (`0600`) and sends it on every call; the hub proxy reads the
  same file per run.
- **Bind.** `--api-bind` (default `127.0.0.1`) is the host-side address of the
  publish rule. With `--network host` there is no publish rule at all — docker
  ignores `-p` in the host network namespace — so the CLI injects `RALPHD_PORT`
  and `RALPHD_BIND` instead and the engine binds that address itself. (Absent
  `RALPHD_BIND` the engine binds `0.0.0.0` *inside* the container, which is
  correct precisely because docker's publish rule is the boundary in that mode.)

**Off loopback, the token is the only boundary.** Two things change at once when
`--api-bind` is not `127.0.0.1`, or when `--network host` removes the
docker-level isolation layer: everything the job can do — read its credentials
back through the creds API, steer it, abort it, hot-swap its skills — becomes
available to whoever can reach the port, so `--api-token` stops being optional;
and no second layer is left to save a misconfigured bind. `ralphctl doctor`
therefore flags a `host`-network job explicitly, noting that the API binds
`--api-bind` directly with no docker port-publish isolation, so auditing a run's
exposure does not mean reconstructing that.

The hub is separate and simpler: `ralphctl ui --bind` defaults to `127.0.0.1` and
the hub has **no authentication of its own**. It reads the registry directly and
proxies live container APIs using each run's stored token, so binding it off
loopback publishes every run's control surface to whoever can reach that port. It
is a local tool; keep it local.

### 13.4 What the agent is allowed to do

Inside its container, essentially everything. That is the design, not an
oversight: a loop that has to ask permission cannot run for twenty hours
unattended, and the container plus a scoped credential is the boundary that makes
broad latitude affordable. The agent runs as non-root `agent`, owns `/workspace`
completely, has normal outbound network, and — with `--allow-docker` — the socket
of §13.2.

**Prohibitions belong in the PRD.** The review phase reads the PRD as the contract
and verifies it requirement by requirement, so a constraint written there is
independently checked by a phase told to trust nothing the worker claims — and
that phase also checks the workspace for damage, debris and secrets in committed
files. A constraint written only into a phase prompt gets no such check.

**Prompt rules are not a security boundary; engine-level self-protection is.**
The distinction is load-bearing rather than theoretical: an instruction not to
dump credentials is why redaction is also mechanical (§13.1), and an instruction
not to kill the run's own container is why the safe cleanup idiom is *expressible*
at all (§13.2). Where an invariant matters, the engine defends it in code:

- **`ralphd-engine --help` and `--version` are argument-parsed up front** and exit
  `0` before `amain()` runs: no config load, no directory creation, no server, no
  port bound, no lock taken. A bare `ralphd-engine --help` inside a live job's
  container is therefore harmless — which matters, because an agent exploring its
  own environment types exactly that.
- **The run dir takes an exclusive, non-blocking `flock`.** A second engine
  pointed at the same `RALPHD_RUN_DIR` prints a diagnostic naming the run dir and
  exits `3` (`EXIT_RUN_DIR_LOCKED`) without touching any other state file; the
  holder keeps serving, unaffected. Because `flock` is kernel-held and
  process-lifetime it is released on any exit including `SIGKILL`, so a killed
  engine never leaves a stale lock that blocks recovery.

What the engine deliberately does *not* protect against is an agent signalling
processes inside its own container: a `SIGKILL`/`SIGTERM` reaches the running `pi`
subprocess directly, because they share one PID namespace. Isolating iterations is
the intended fix and is not built (§15).

---

## 14. Testing

The suite is black-box: it drives the real `ralphctl` and `ralphd-engine`
executables, asserts through the HTTP API, the run dir's own files and `docker`
CLI introspection, and does not import engine internals to reach into a running
job. `tests/` holds **103 `test_*.py` modules with 721 test functions** (more
collected items than that once parametrisation expands), one `conftest.py`, and
three stub programs that are the whole reason the suite is affordable.

### 14.1 Tiers

Two markers are declared in `pyproject.toml` (`docker`, `browser`); the rest of
the tiering is a matter of which stub a module reaches for.

| tier | how it runs | modules | what it proves | cost |
| --- | --- | --- | --- | --- |
| **Pure unit** | no subprocess at all | `test_fault_classifier.py`, `test_pricing_map.py`, `test_pricing_aws.py`, `test_log_merge.py`, `test_durations.py`, `test_fault_class_meta.py` | pure functions: the infra signature table, pricing arithmetic and the shipped rate table, transcript merging, duration formatting | milliseconds |
| **Live engine + stub `pi`** | a real `ralphd-engine` process launched directly, no docker, with `tests/stub-pi/pi` first on `PATH` | ~50 modules | the whole loop, the API, retry/refund, reflection, steering, budgets, crash-and-resume — everything the engine does | seconds per module |
| **CLI + recording stub `docker`** | `RALPHD_DOCKER` points at `tests/stub-docker/docker`, which appends every argv to a log and fakes just enough daemon behaviour | ~20 modules | that `ralphctl` builds the right `docker run` — labels, mounts, `-e` wiring, ports, network mode — and that `resume` reproduces it | seconds |
| **Real docker siblings** (`-m docker`) | builds `container/Dockerfile` as a real image and drives the real daemon | `test_docker_sibling_e2e.py`, `test_sibling_cleanup_job_safe.py`, `test_cli_docker_integration.py`, `test_image_real_build.py` | the image actually works: creds land at `~/.creds`, skills are symlinked, the API is reachable, `stop` reaps, `resume` continues, `--no-detach` exits on the right verdict, the documented cleanup command spares the job container, and (`test_image_real_build.py`) the *generated* derived recipe really builds on a minimal base and really runs `ralphd-engine` | minutes; needs a socket |
| **Real browser** (`-m browser`) | shells out to `playwright-cli` (never imports it) driving real Chromium against a real `ralphctl ui` | `test_browser_hub.py`, 18 tests | the hub renders from real fixture and live run dirs, and its interactions have real effects — a submitted steering form creates a file under `steering/` | minutes; needs `playwright-cli` |
| **Doc and guidance consistency** | greps the tree, walks the real argparse tree (`cli.main.build_parser`) and renders real prompts | `test_docs_consistency.py`, `test_prompt_lint.py`, `test_report_claims.py`, `test_issue_traceability.py`, `test_sibling_cleanup_guidance.py`, `test_toolchain_sibling_guidance.py` | the docs, prompts, example skills and reports say what the code does — including that every documented flag, subcommand, route and response field exists | milliseconds |

The two heavyweight tiers **skip cleanly** rather than erroring when their
dependency is missing — no docker socket, no `playwright-cli` on `PATH` — so a
plain run on a bare machine is still green, and the skip is visible.

The `docker` tier carries two environment-specific accommodations that exist
because ralphd's own development happens *inside* a ralphd job container:
`tests/docker-hostpath-wrapper/docker` rewrites `-v` sources under `/workspace`
to their host equivalents before exec'ing the real binary (a no-op on a bare
host), and those tests pass `--api-bind <docker0-gateway>` so the published port
is reachable from a sibling container.

### 14.2 Determinism

**No test ever calls a real LLM.** `tests/stub-pi/pi` is a stub agent runtime: it
reads a phase prompt on stdin, emits `pi`-shaped NDJSON on stdout, and mutates
the run dir the way a real iteration would. Its behaviour is driven entirely by
`STUB_*` environment variables the test sets, which is what makes adversarial
scenarios reproducible instead of anecdotal — how many tasks the planner creates,
how many reviews reject before verifying, a worker that never changes
`tasks.json` (stagnation), a `message_end` carrying `stopReason: "error"` at
`exit_code: 0` (an in-band provider fault), a 1 MiB single line, one malformed
non-JSON line, a run of instant no-output exits (a broken credential), a sleep
between `tool_execution_start` and its matching end (a real observable window for
a liveness assertion), a deliberate echo of a planted secret into both assistant
text and a tool call's arguments.

**No real clocks, no real sleeps in the retry tests.** The backoff wait is an
interruptible `asyncio.Event` race in `_wait_out_backoff`, never a bare
`asyncio.sleep` — which is a product requirement (`POST /retry` must be able to
skip a countdown) and also the seam the tests replace. `_stub_attempts()` swaps
`_run_iteration_once` for a scripted result feeder and `_wait_out_backoff` for a
recorder returning `(seconds, False)` — "waited the whole backoff, woken by
nobody" — so a test asserts the exact wait sequence (`[0.1, 0.2, 0.4, 0.4, 0.4,
0.4]`) as data, and the thirty-minute endurance assertion rides out a *virtual*
outage against the shipped default schedule in milliseconds.

**No real containers where a recording will do.** The stub `docker` answers
`ps`/`inspect` from environment knobs and logs argv, so wiring assertions cost
nothing. Its `STUB_DOCKER_LIVE_ENGINE` knob goes one step further: `docker run`
launches a real engine via `live_engine_supervisor.py`, which `SIGKILL`s it the
instant `status.json` reaches a terminal state — deterministically reproducing
"the API dies exactly at job completion" instead of racing for it.

**No shared state.** Every fixture builds its own registry, config dir, run dir,
workspace and free TCP port under `tmp_path`, and stops every process it started
at teardown.

### 14.3 Invariants asserted by tests

Beyond behaviour coverage, a set of tests exist specifically to keep a
*structural* property true — the kind of property that decays silently and is
expensive to rediscover.

| invariant | guarded by |
| --- | --- |
| The auto-resume default lives in exactly one literal: `AUTO_RESUME_DEFAULT` in `src/ralphd/cli/main.py`. Every reader — the flag layering, the registry-config default, the pre-existing-run fallback, `doctor --fix` — goes through it, and the tests are parameterised over its value rather than spelling `False` out, so flipping it is a one-line change that does not rewrite the suite. | `tests/test_cli_auto_resume.py` (imports the constant, greps the source line, and asserts the roadmap note names it) |
| There is exactly one log-merge implementation. Only `src/ralphd/log_merge.py` may synthesise a `ralphd.iteration` boundary out of a `meta.json`, and only a named allowlist of modules may read `output.jsonl` at all (`log_merge.py`, `engine/api.py`'s single-iteration raw route, `engine/loop.py` which writes it, `engine/redact.py`). | `tests/test_log_merge.py::test_no_duplicate_merge_implementation`, plus `::test_api_and_on_disk_merge_are_identical` asserting the live and on-disk paths agree byte-for-byte |
| The documented sibling-cleanup command leaves the job container alive. The two-filter form is run for real against a real daemon and the job container must survive; the forbidden one-filter form is *also* run, and must really delete it, so the test fails if the danger it guards ever stops being real. | `tests/test_sibling_cleanup_job_safe.py` (`-m docker`): `::test_documented_cleanup_removes_siblings_and_spares_the_job`, `::test_the_forbidden_form_really_does_delete_the_job_container`, `::test_ralphctl_stop_and_rm_still_reap_everything` |
| No copy of the docs, prompts or example skills teaches the run-label-only form, and every rendered prompt carries the safe one. | `tests/test_docs_consistency.py::test_docs_and_examples_teach_the_sibling_only_cleanup_filter`, `::test_no_run_label_only_cleanup_command_in_docs_or_examples`, `::test_rendered_prompt_has_no_run_label_only_cleanup_command`, `::test_example_skill_run_sh_labels_siblings_with_the_role_label`; `tests/test_sibling_cleanup_guidance.py` for the per-prompt and self-id cases |
| The infra signature table holds family by family, with negative cases. Each family — DNS, TCP connect/teardown, half-closed stream, TLS, the SDK's opaque `Connection error.`, gateway 5xx, back-pressure, Bedrock/capacity — is asserted, and so is the converse: ordinary agent failure text is **not** infra. Named regression cases pin the exact strings observed in real incidents. | `tests/test_fault_classifier.py` (`::test_infra_signature_family_classifies_infra` and `::test_ordinary_agent_failure_text_is_not_infra`, both parameterised, plus the `::test_regression_*` cases) |
| An operator-initiated termination is never an infra fault, and never enters the retry loop, whatever the text says. | `tests/test_operator_abort_carve_out.py` (`::test_operator_abort_beats_every_infra_signal`, `::test_operator_abort_never_triggers_the_infra_retry_loop`, `::test_stale_interrupt_with_nothing_running_does_not_shield`) |
| A secret value never reaches `output.jsonl`, `events.jsonl`, `GET /logs` (tail or follow), the host-side on-disk merge, or the hub's dead-run fallback — while a non-secret sentinel passes through untouched, so redaction is not overzealous. | `tests/test_secret_redaction.py::test_secrets_redacted_from_output_events_and_logs` |
| Credential *values* never appear in a prompt, an event, stdout or an API response; only names do — including through full CRUD with read-back. | `tests/test_creds_prompt.py`, `tests/test_creds_placement.py`, `tests/test_creds_api.py`, `tests/test_creds_guidance.py::test_source_files_have_no_stray_secret_and_pattern_present` |
| A reflect phase that produces no report is recorded as a failure, not silence — including the clean-exit-wrote-nothing case. | `tests/test_reflection.py::test_reflect_that_writes_no_report_is_recorded_as_a_failure`, `::test_reflect_agent_error_is_recorded_as_a_failure`; `tests/test_cli_status_reflect.py` for the operator surface |
| Reflection never rewrites how the job ended, and gets its own capped budget rather than the job's. | `tests/test_reflect_infra_retry.py::test_reflect_gets_a_capped_outage_budget_every_other_phase_does_not`, `::test_operator_abort_keeps_its_veto_over_reflect`, `tests/test_reflection.py::test_reflect_runs_after_terminal_state_leaves_run_state_untouched` |
| The completion hook cannot change the outcome. | `tests/test_on_complete_cmd.py::test_hook_nonzero_exit_logged_but_state_verdict_and_exit_code_unaffected` |
| Every `ralphctl` verb the tutorial uses really exists in `--help`, and the tutorial's steps stay in the order an operator would follow them. | `tests/test_docs_consistency.py::test_every_ralphctl_command_in_tutorial_exists_in_help`, `::test_tutorial_exists_and_covers_required_steps_in_order` |
| Every flag, subcommand invocation, route and response field the reference docs document really exists in the code — a documented-but-nonexistent `--flag`, `ralphctl <verb> <action>`, route or JSON field fails the suite. | `tests/test_docs_consistency.py::test_every_flag_documented_in_cli_md_exists_in_the_parser`, `::test_every_option_table_row_names_a_flag_of_its_own_command`, `::test_every_documented_subcommand_invocation_exists`, `::test_every_route_documented_in_api_md_is_served`, `::test_every_field_documented_in_api_md_exists_in_the_code`, plus `::test_a_fake_cli_flag_in_the_docs_is_reported` / `::test_a_fake_api_route_or_field_in_the_docs_is_reported` keeping the demonstration |
| No prompt file narrates its own revision history. | `tests/test_prompt_lint.py::test_no_revision_history_phrases_in_prompts` |
| Every report under `artifacts/reports/` names only commits that landed, paths that exist and tests that exist. | `tests/test_report_claims.py` over `tests/report_claims.py`, plus `tests/test_issue_traceability.py` for that report's own claims (§12.3) |
| The hub ships with no build step: the static bundle contains no npm/node build artifacts. | `tests/test_cli_ui.py::test_static_bundle_js_has_no_npm_or_node_build_artifacts` |
| The shipped default retry schedule really rides out a 30-minute outage. | `tests/test_v05_definition_of_done.py::test_defaults_can_ride_out_a_thirty_minute_outage` |

### 14.4 Running the suite

```
pip install -e '.[dev]'
pytest                                    # everything available
pytest -m 'not docker and not browser'    # the fast tier
pytest -m docker                          # needs a reachable docker socket
pytest -m browser                         # needs playwright-cli on PATH
ruff check .                              # line-length 100, target py311
```

`pyproject.toml` sets `testpaths = ["tests"]` and `asyncio_mode = "auto"`, so a
bare `pytest` from the repo root is the whole invocation and `async def` tests
need no decorator. There are no `addopts` and no deselection by default:
**a plain `pytest` runs every tier it can**, which is the right default for a
suite whose heavyweight tiers skip themselves when unavailable.

Cost, measured: the committed full-suite log records `802 passed … 864.50s` —
a little over 800 items in about fourteen and a half minutes. The fast tier is a
few minutes; the pure-unit and consistency modules are effectively free and are
the right thing to run on every edit. There is no CI: the suite runs locally,
and inside the loop itself on a self-hosted job.

---

## 15. Deferred

Deliberately not built. Each of these is a decision with a reason, not a gap
nobody noticed.

- **`auto_resume` defaulting to ON.** Self-recovery ships opt-in, so the two
  rules that make it safe — the crash-loop guard, and "never resurrect a run the
  operator killed" — get validated against real runs before they are load-
  bearing for everybody. The intent is to flip it. The default is a single
  literal (`AUTO_RESUME_DEFAULT`) and every reader and test goes through it
  precisely so the flip is one line and the suite does not have to be rewritten
  to accept it (§14.3).
- **PID-namespace isolation of agent iterations.** A `SIGKILL`/`SIGTERM` from
  inside the container reaches the running `pi` subprocess directly, because the
  iteration shares the container's PID namespace. Giving each iteration its own
  namespace would let the engine distinguish "stop this one iteration" from "the
  whole container is being torn down" — and it belongs here rather than in the
  "an instruction is enough" pile because an instruction demonstrably does not
  stop an agent from signalling processes it should not.
- **Remote/daemon mode.** `--api-bind` plus `--api-token` already make running
  ralphd on a server and driving it over the network *possible*. Making it
  *nice* — TLS, discovery, a multi-run daemon — is deferred, because the missing
  pieces are the ones that must not be improvised.
- **Alternative agent runtimes behind an interface** (Claude Code, Codex,
  opencode). A second runtime while the `pi`-only loop is still settling would
  freeze an interface around assumptions that are still in motion.
- **Parallel-job orchestration and queueing.** Out of scope on purpose: every
  CLI verb has `--json`, `start` is asynchronous, and `watch` exits at the run's
  real terminus, which is deliberately sufficient for an external orchestrator —
  a human, a script, or another agent. An internal scheduler would duplicate
  that and own new failure modes.
- **First-class named env-wiring profiles.** Reusable `--forward-env`/creds
  bundles referenced by name at `start`, analogous to LLM profiles. The per-run
  persistence (`llm-wiring.json`, `env-wiring.json`) records only what was
  resolved for *that* run, so operators repeat the same multi-flag
  `--forward-env`/`--llm-env`/`--env` incantation on every launch. Worth
  building on the second profile-shaped use case, not the first.
- **Publishing the Docker image and the wheel.** The image builds locally and is
  content-hashed (§3.2) — including from a `pipx`-style install, whose wheel
  ships the image inputs as package data — and the `-m docker` tier proves the
  built image runs. What is deferred is the *publishing* pipeline: nothing pushes
  a tag to a registry or a wheel to PyPI, so `--image` names a locally built tag
  and there is no `ralphd` to install from an index yet.

---

## 16. Open questions

Genuinely unresolved, with the trade-off as it currently stands.

1. **Does the outage budget belong per-episode or per-job?** It is per-episode:
   `_reset_infra_episode()` clears the attempt counter, the accumulated wait and
   the instant-failure streak the moment any iteration reaches the model, so a
   job hitting a short glitch every hour is never starved by the earlier ones.
   The cost is that there is no ceiling on *total* time lost to a flapping
   endpoint — twelve separate near-budget episodes are permitted where one long
   one is not. A per-job cap would bound that, at the price of failing a job
   that was, by every local measure, recovering fine.
2. **Should a derived cost ever be shown without its marker?** It never is:
   host-side pricing lands in its own `costDerivedUSD` field, carries
   `costDerived`, and renders as `~$0.45 derived` everywhere. That is right for
   auditability and slightly hostile for budgeting — an operator comparing two
   runs reads `$0.56 + ~$0.45 derived, partial (rest unavailable)` when the
   question was "what did this cost". A single blended number answers that
   question and is also exactly the kind of quiet estimate the marker exists to
   prevent. An explicit opt-in ("show me blended totals, I know what I am
   reading") is the shape of a fix, not yet a decision.
3. **How much autonomy should the agent have over its own container?** A great
   deal: with `--allow-docker` it can inspect, and in principle delete, the
   container it is running in. The mitigations are identification
   (`ralphd.role`, `RALPHD_SELF_CONTAINER_ID`) plus prompt guidance, and
   identification only helps an agent that chooses to use it. Every alternative
   costs something real — a socket proxy cannot meaningfully restrict `run`;
   dropping the socket removes the toolchain-in-a-sibling pattern that makes a
   thin image viable; PID-namespace isolation (§15) helps with signals but not
   with `docker rm`.
4. **Should the retroactive scrub reach a dead run's snapshot?** Write-time
   scrubbing is the accepted host-side guarantee, because the only way to give
   an off-engine reader the retroactive catch is to persist the redaction map,
   which would put every secret value on disk in plaintext beside the transcript
   it protects — strictly worse than the gap. The gap is narrow and real: a value
   added as a credential *after* a transcript line already quoted it stays
   visible in that run's on-disk snapshot once its container is gone. Either the
   gap stays, or secrets-at-rest get a real mechanism (a keyring, an external
   secret manager) that the map could then safely use — a larger decision than
   this one.
5. **Should the reflection diff ever be applied automatically?**
   `suggestions.diff` is a proposal by construction: nothing applies it, and the
   prompt forbids the agent from doing so. That keeps every run reproducible
   against known prompt material, and it also makes the loop's self-improvement
   depend entirely on an operator reading a diff. A review-and-apply verb would
   close the gap without giving the loop write access to its own prompts
   mid-flight, and would have to answer what happens when two runs propose
   conflicting edits.

---

## 17. Answered questions

Questions that stood in §16 until a wave had the evidence to settle them. They
are recorded here with the answer and what it was decided *from*, rather than
edited in place in §16, so the reasoning that closed one is still readable next
to the reasoning that opened it. §16's remaining questions renumbered when an
entry moved out of it; each entry below names the number it used to carry.

1. **Can a provider-side stream abort mid-iteration be told from an operator's
   abort?** (§16 question 1 through v0.6; answered by task 014, #49 part 2.)
   **Yes, for the case that mattered, and from duration.** Not from the text —
   `pi` records both as the bare in-band error `aborted` — so the ladder decides
   it from the loop's own bookkeeping plus how long the iteration ran: with an
   abort recorded it is `"work"` and never retried (§8.2 step 2); with a signal
   after traffic it is `"signal"` (step 5); with traffic, a clean exit, no
   recorded abort and a duration inside `faults.ABORTED_STREAM_MAX_DURATION_S`
   it is `"infra"`, so it is retried and refunded (step 6); past that threshold
   it stays `"work"`.

   The threshold that "nobody can derive from first principles" was derived
   from a run instead. In `selfdev-v06-release`, iteration 145 recorded
   `error: aborted` with `faultClass: "work"` **39 seconds** into a 45-minute
   `iteration_timeout_s` — the leading edge of a DNS outage whose next five
   iterations matched an infra signature and were correctly retried and
   refunded. Same outage, opposite treatment, decided only by whether a token
   had been emitted before the stream died; and 145 was charged an approach
   *and* consumed a steering note. The constant is 120s: three times the
   observed 39s, and ~4% of the default cap, so an iteration that really did
   work cannot land under it. It is absolute rather than a fraction of
   `iteration_timeout_s` because the cap is not part of an iteration's
   `meta.json`, and `state.fault_explanation` has to re-derive the same verdict
   from that record alone.

   What is deliberately *not* answered: the same shape more than 120s in. See
   §8.2's "What is left, deliberately".
