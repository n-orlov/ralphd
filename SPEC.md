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
| **R5** | **"Done" is a claim; only verification is a verdict.** The worker's whole-plan `"plan_complete"` verdict (retiring the `<promise>COMPLETE</promise>` marker, v0.14 requirement O) gates entry to review and nothing else; the job succeeds only on the reviewer's own `{"verdict": "verified"}` contract report, and only when no steering is left unconsumed. A budget exhausted before review ever renders a judgement now **holds** — the run enters the non-terminal `timed_out` state with `verdict: null` (v0.24 requirements A–D, #169) rather than being granted an extra off-budget review or ending terminal `failed`/`unverified` with the work done and nobody having looked at it — and a plan whose only unresolved tasks are `failed` ones is routed to review by the engine rather than left to stagnate (§5.3). | Otherwise a confident agent ends the job, and the two shapes that actually cost operators time recur: a "successful" run nobody re-checked, and a `failed/unverified` run whose work was in fact complete but whose last budgeted iteration went to finishing it instead of reviewing it. |
| **R6** | **Secrets never reach disk or a transcript.** Credentials arrive as `<name>.env` files, are placed at `~/.creds/*.env` (mode `0600`) by the engine itself, and are never auto-exported into the agent's environment; only file *names* ever reach the run dir, the persisted job config, or a log line. On top of the prompt rule, redaction is **mechanical**: known secret values are scrubbed from `output.jsonl` at write time, from `events.jsonl` at emit time, and again as `GET /logs` serves. The redaction set is memory-only and no route returns it. | Otherwise one `cat` of a cred file, or one `docker inspect` of the engine container, writes a live token into host-visible history permanently — which prompt guidance alone demonstrably does not prevent. Persisting the redaction map to enable a second pass would put every secret on disk next to the transcript it protects, so that is forbidden too. |
| **R7** | **Unknown cost reads as unknown, never as `$0.0000`.** An iteration with tokens but no provider-quoted price is marked `costPriced: false`; a run total mixing priced and unpriced iterations reads *partial*; an optional host-side pricing map may supply a *derived* cost, which lives in its own `costDerivedUSD` field with its own marker and is never merged into provider-reported `costUSD`. | Otherwise a 102M-token run reads as free, a cheap per-hat models table optimises against zeros, and no surface can distinguish "free" from "unmeasured". |
| **R8** | **The engine cannot be killed by the agent it supervises.** The job container carries `ralphd.role=job` alongside `ralphd.run=<run-id>`, siblings carry `ralphd.role=sibling`, and the container is told its own name via `RALPHD_SELF_CONTAINER_ID`. Every documented cleanup command — prompt, skill example, docs — is the two-filter, sibling-only form. | Otherwise the idiom ralphd itself teaches deletes the job container mid-iteration: that iteration's work and transcript are lost and the run dir is left non-terminal, which is exactly how a run becomes a zombie that `status` reports as healthy and still-running. |
| **R9** | **A job is resumable from its own run directory.** A fresh `ralphd-engine` over an existing run dir seeds `iterations_used` from the highest *completed* iteration, resumes the recorded approach, skips planning, and treats a topped-up `iterations` as remaining budget. `resume` reproduces the original `docker run` wiring from files (`llm-wiring.json`, `env-wiring.json`, `host.json`), not from the resuming shell. Concurrency is bounded by an exclusive `flock` on `<run-dir>/.lock`; an on-disk `schemaVersion` newer than the build refuses to run. | Otherwise recovery re-plans from scratch, or — worse — resumes into a credential-less container and fails instantly on every iteration because the `-e` flags only ever existed in the shell that typed `start`. Two engines over one run dir would interleave writes into `status.json` and `events.jsonl`. |
| **R10** | **The CLI's `--json` output is a stable machine interface.** Every command supports it, the schema is stable, human text goes to stderr on that path, and exit codes are documented (`0` success · `1` error · `2` usage · `3` unknown run · `4` API unreachable · `5` invalid in current state · `130` follow interrupted). Followers reconcile against live state: a terminal-state event ends a stream only when it is the log's last event *and* the engine is not live. | Otherwise the documented agent completion-wait (`ralphctl watch <id> --json \| jq …`) returns a *historical* `aborted` from before a resume and exits `0` while the job is still running — failing silently, successfully, and precisely after a crash recovery, which is when supervision matters most. |
| **R11** | **Steering is never silently discarded.** A steering file is only marked consumed by a phase whose prompt tells the agent to act on it (`planning`, `worker`, `steer`); a `VERIFIED` verdict is refused while anything the exit gate names is outstanding — an **unsettled** steering file (deferred once through `steering_pending_at_verified`, so the ordinary dispatch loop routes it to the `steer` hat at absolute top priority) or a **non-terminal steering-origin task** (`settled` is not `done`, task 013/requirement I, #98); and every terminal `status.json` carries `unconsumedSteering`. | Otherwise a note accepted with `202` while the worker was in flight is recorded as consumed by a review iteration that was never going to act on it, or is stranded forever by a run that went terminal — and a terminal run never reads pending steering again. Gating on the *file* alone was not enough either: a message can be `consumed` with three quarters of the work it asked for undone. |
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
working) · PID-namespace isolation of an *iteration* (v0.7 made the engine
unsignalable from inside the container by uid instead, §13.4; what a namespace
would add beyond that is telling "stop this iteration" from "the container is
being torn down") · inbound remote control beyond the documented API · web-scale
anything.

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
  `jq`, `ripgrep`, `procps`; a non-root `agent` user at uid 1000 that *iterations*
  run as (the image deliberately sets no `USER`: the engine starts as root and
  drops its effective uid to `agent` so an iteration cannot signal it, §13.4); the static docker
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
dying mid-write. Task 009 (PRD v0.11 requirement D) adds one more: `5`
`EXIT_UNROUTABLE_HAT_MODEL` — a configured hat's model ref names a provider
the profile's own `~/.pi/agent/models.json` does not declare, checked before
iteration 1 (`engine/llm.py:check_hat_routes`), never via a live gateway call.

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
├── config.yaml                  # registry-wide defaults (`ralphctl config`, §6.2):
│                                #   image, base_image, dockerfile, on_complete,
│                                #   default_llm_profile, network, auto_resume,
│                                #   price_strategy, model,
│                                #   thinking
│                                # plus the nested `pricing:` map, which
│                                #   `config set` cannot write (§8.6)
├── llm-profiles/<name>.yaml     # named LLM profiles (env/mounts/pi fragments)
├── templates/<name>/            # job templates: job.yaml + optional prd/skills/creds
├── runs/<run-id>/               # bind-mounted at /run/ralphd — the source of truth
│   ├── status.json              # engine-maintained: state, phase, approach, verdict,
│   │                            #   health, infraWait, schemaVersion, usage, reflect
│   ├── tasks.json               # agent-readable VIEW of the plan, refreshed on every
│   │                            #   authoritative write (task 004, requirement B, #202);
│   │                            #   never itself the engine's own source of truth
│   │                            #   (the AUTHORITATIVE plan is NOT in this directory at
│   │                            #   all -- see configs/<run-id>/record/ below)
│   ├── prd.md · composite-prd.md · notes.md · review-findings.md · debt.json · debt.md
│   ├── events.jsonl             # append-only event log, also fed to GET /events
│   ├── iterations/NNNN/{meta.json,output.jsonl}
│   ├── approaches/ · artifacts/ · steering/{NNN-*.md,.consumed.json}
│   ├── vigilant-verified.json   # engine-owned record of tasks that passed verify
│   ├── operator-termination.json# durable record of an operator abort/stop
│   ├── auto-resume.json         # crash-loop guard: attempts, lastAt, maxAttempts
│   ├── host.json                # container id, port, apiUrl, the image record (§3.2), network, workspace
│   └── .lock
├── control/<run-id>/           # HOST-ONLY -- never bind-mounted into any container (task 003, #201)
│   ├── .control-token (0600)  # unconditional; the mutating-only gate's secret
│   └── .api-token (0600)      # only when --api-token was passed
└── configs/<run-id>/            # bind-mounted at /config, read-only
    ├── job.yaml                 # the job config as launched
    ├── prd.md                   # as handed to `start`
    ├── prompts/ · skills/<name>/ · creds/<name>.env · pi/{models.json,…}
    ├── llm-wiring.json (0600)   # resolved --llm env + mounts, for `resume`
    ├── env-wiring.json (0600)   # resolved --forward-env/--llm-env/--env pairs, ordered
    ├── run-secrets/ (0700)      # bind-mounted RW at /var/lib/ralphd/run-secrets: the
    │                            #   per-run petition MAC key store (§5.1.1's sibling)
    ├── record/ (0700)           # bind-mounted RW at /var/lib/ralphd/record: the
    │                            #   AUTHORITATIVE tasks.json, root-owned 0600 inside a
    │                            #   root-owned 0700 directory with only root-owned
    │                            #   ancestors above it in the container (task 004,
    │                            #   requirement B, #202) -- §5.1.1
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
| `src/ralphd/ambient_home_allowlist.py` | Task 006 (#83 class, requirement C): the reasoned allowlist of every ambient-`$HOME` read left under `src/ralphd/` after every read that could take its home from its caller was moved to do so — one entry per surviving `Path.home()`/`os.environ["HOME"]`/`os.path.expanduser("~")` occurrence, path plus a one-line reason the ambient home is genuinely correct there. |
| `src/ralphd/collapse_index.py` | Task 020 (requirement G): the engine-owned side index recording prefix-collapse state (`text_seen`/`thinking_seen`/`toolcall_seen`) for one iteration's `output.jsonl`, written once per iteration by the engine's own end-of-iteration bookkeeping and never by any render path. One segment per reset line (`message_start`, `ralphd.iteration`), each recording where every flag first fires inside it, so a bounded reader resolves the flags at any byte offset without re-reading the transcript. Absence, truncation, an unknown version or a size mismatch all fall back to a plain scan — never an error, never a wrong render. |
| `src/ralphd/render_index.py` | Task 012 (v0.26 requirement C.1, #271): a successor side index beside `collapse_index.json` — `render_index.json`, one per iteration — that also records what a *bounded tail* needs: one checkpoint per raw transcript line (its byte offset, the cumulative count of pretty-rendered lines up to it, and `log_render`'s own render-state dict at that point), plus the same collapse segments `collapse_index.py` records (reusing its `_transitions`/`apply_transition` mirror of the renderer's own dispatch, so the two can never disagree). Unlike `collapse_index.json`, it is extended *while the iteration is still running*: `engine/runner.py`'s `pump()` — the one place `output.jsonl` is written live — feeds it every line through `LiveRenderIndex.feed` and periodically flushes it to disk (`LIVE_FLUSH_EVERY_LINES`, the base batch size). Task 012 (v0.27 requirement D.2, #275) schedules that periodic publish geometrically rather than at a fixed interval (`_next_flush_threshold`: the next publish is due once the checkpoint count has at least doubled since the last one, or grown by `LIVE_FLUSH_EVERY_LINES`, whichever needs more lines first), so the total records and bytes a whole iteration's live writer serialises grow linearly in the transcript's own line count instead of quadratically (each publish still rewrites the whole accumulated snapshot, but publishing at `every, 2x, 4x, ...` sums to a series bounded by `2x` the final count); `engine/loop.py`'s end-of-iteration bookkeeping then calls `write_index` once more as a belt-and-braces full rebuild. Its own `load_index` allows `transcriptBytes` to sit *behind* the transcript's current on-disk size (an ordinary, expected lag for a live iteration) — the caller treats the gap as an unindexed suffix to scan directly, never as reason to discard the index — but refuses one that claims to cover *more* than the transcript's actual size, or that is otherwise malformed, wrong-version or internally inconsistent. Beyond byte sizes (v0.26 requirements C.1/C.3, D.1): `load_index` now requires every checkpoint's `state` to carry EXACTLY `log_render.new_render_state()`'s own key set with correctly-typed values (never a bare `isinstance(state, dict)`), refusing a checkpoint whose `state` is missing a key, carries an unknown extra one, or holds a wrong-typed value — checked inside the containers too, on every checkpoint: boolean collapse flags, an object `tool_args`, a `tool_pending` object whose every value is a two-element `[name, args]` array with a string `name`, a string-or-`null` `open_tool_line`, and a `collapse_unresolved` array of strings — the shape that used to reach `log_render.render_to_text` (or `_state_from_json`'s hydration) and raise instead of falling back, and `bounded_tail` itself answers `None` rather than raising if a passed-in index's stored state still cannot be hydrated or rendered; and every checkpoint also carries `lineCrc32` (`zlib.crc32` of its raw line's bytes; an index without it — `version` 1 — is refused), against which `load_index` verifies a BOUNDED set of covered segments: `checkpoints[0]` (the transcript's first raw line, rendered from a fresh `new_render_state()`) and every segment lying wholly inside the covered prefix's last `VERIFY_WINDOW_BYTES` (128 KiB), rendered in order from the stored state just before that window. A segment matches only when its CRC-32, its rendered fragment count and the render state it leaves behind all equal its checkpoint's, so a stored `state`/`renderedLines` is re-derived from its predecessor's, never trusted blind; `bounded_tail`'s backward walk applies the same check to every segment it reads, and any mismatch refuses the index (the caller falls back to the exact full render). The resulting validity contract: a same-size rewrite or same-size replacement of the transcript is refused whenever it touches the first line, a line wholly inside that window, or a line the tail reads — every covered line when the covered prefix fits in the window — even when it would render identically; a valid append-only index is accepted and only its uncovered suffix is scanned, verification adding at most the first line plus `VERIFY_WINDOW_BYTES` to the tail's reads; and beyond the window too (task cure-01-01-4, the petition remedy for cure-01-03 criterion (1)) a same-size rewrite or replacement of any covered line the tail does not read is still refused, at no extra byte cost, by an engine-published identity/provenance witness rather than by reading the rewritten bytes, which the C.2 byte bound still forbids on a silent prefix: every index carries `transcriptInode`/`transcriptWitnessBytes`/`transcriptMtimeNs`/`transcriptCtimeNs` (the transcript's inode, size, mtime and ctime as of the moment the writer vouched for it) and a sticky `poisoned` flag; the engine's writer (`LiveRenderIndex.append`, the call `engine/runner.py`'s `pump()` makes for every line, and `write_index`) sets the transcript's mtime after each write to a provenance stamp derived from its inode and size; and `load_index` refuses, off one `stat()`, a poisoned index, a changed inode, a shorter transcript, a transcript at the witnessed size whose mtime OR ctime moved (task cure-01-03-2: an ordinary write that restores the original mtime via `os.utime` after a same-size in-place rewrite still moves ctime, which a normal write can never restore the way it can mtime, so checking ctime alongside mtime catches that rewrite too), and a longer one whose mtime is not the engine's stamp for its current inode and size. Before each write the writer checks the transcript is exactly as its own last write left it; the first time it is not, it poisons and republishes the index and never stamps again, so no later legitimate growth blesses a previously modified prefix (a writer that grows the transcript without `append` has broken the same chain of custody and poisons itself at its next publish). `bounded_tail` re-checks the witness after its own reads, so a transcript modified mid-read is refused rather than half-seen while the engine's own stamped appends in the meantime are not, and holds a passed-in (hub-cached) index to the same witness. Never written by a render path (`cli/log_render.py`, `log_merge.py`, `cli/ui_server.py`). Task 013 adds the read-side consumer, `bounded_tail(run_root, n)`. A checkpoint's `renderedLines` counts per-raw-line render fragments (a streamed text line spans many raw lines), so it rises on exactly the raw lines that print anything; checkpoint `state`s are deep copies, never shared with the live render state. `bounded_tail` builds the latest iteration's output from its end backward — the unindexed suffix a lagging live index does not cover yet (read in full, seeded from the last checkpoint's `state`), the synthesized "end" boundary once the iteration has finished, then the covered prefix's printing raw lines newest first, each located by binary search over `renderedLines` and re-rendered from its predecessor checkpoint's `state` — until the captured text fixes the last *N* lines. Bytes read are those printing lines plus the unindexed suffix, never the silent (recognized-but-nothing-rendered) transcript between them and the transcript's end, and the result equals the full render's last *N* lines. Returns `None` (never a wrong-but-plausible answer) when this one iteration cannot answer by itself: no valid index, a transcript that re-renders differently from its index, or fewer than *N* lines in the iteration. Task 015 (v0.26 requirement C.4, #271/#272) adds `bounded_tail`'s own `index=` keyword (an already-built index a caller passes in INSTEAD of `load_index`'s on-disk read, so a hub-owned cache built off a genuinely absent on-disk index can still be served through the exact same walk) and `latest_iteration_cache_key(run_root)` (the `(iteration number, transcript byte size)` pair `cli/ui_server`'s own cache keys its entries by, alongside the run id). Task cure-01-04 (v0.26 requirement C.2, DoD 3) extends `bounded_tail` past its own "fewer than *N* lines in the iteration" case: reaching the latest iteration's own byte 0 with still fewer than *N* lines no longer answers `None` (which used to send the whole caller straight into an unbounded rescan for an answer already fully known) -- it prepends that iteration's own genuine `ralphd.iteration` "start" boundary line (`_one_iteration_tail_text`, factored out of the single-iteration body this task replaces) and tries the PRECEDING iteration the identical way, purely off ITS own `render_index.json`, and keeps walking backward through however many iterations this run has. Reaching the run's very FIRST iteration's own byte 0 still short of *N* lines means there is genuinely no more history anywhere, indexed or not, so `bounded_tail` returns every line the run ever rendered instead of falling back; any iteration in the walk lacking a valid index (or disagreeing with one) still answers `None`, exactly as a single absent index always has. Every iteration but the latest is always read from its own on-disk index -- the hub's `index=` cache only ever describes the latest one. Task cure-01-02-2 (v0.26 requirement C.4, D.1; DoD 3) adds `bounded_tail`'s own `on_missing=` keyword: a callback invoked with an iteration number whenever this backward walk needs that iteration and finds no usable index for it (neither passed in via `index=` nor on disk), called BEFORE the walk gives up and answers `None` -- returning a fresh dict from it lets the walk retry that one iteration exactly as if `load_index` had found it on disk, so a hub-owned cache can now cover EVERY iteration a tail's walk needs, not merely the latest one `index=` alone ever could. Returning `None` from `on_missing` (declining) is exactly the old behaviour: the walk answers `None` and the caller's own unbounded fallback runs. |
| `src/ralphd/log_merge.py` | The one implementation of "a run's transcript": `iterations/NNNN/output.jsonl` in order, wrapped in synthesized `ralphd.iteration` boundary lines. Imported by the engine's `GET /logs` snapshot path *and* by host-side readers straight off the run dir, so both emit byte-identical lines. Scrubbing is an injected callback, never assumed. |
| `src/ralphd/prompts/planning.md` | Planning phase: read the PRD, draft a file-borne `plan-proposal.json`, report a pointer plus the notes' `## Standing rules` block content through the `planning` contract (§4.2, §5.1), decide scope and optional `dependsOn`/`priority`. Its plan-building practices plan no paperwork: no task's deliverable is a tracked report, record, close-out, audit, evidence or re-take document; every implementation task names a test that fails if its behaviour is absent; closures are proven by an issue comment naming the commit and the test node; the tail is work → closures → sweeps, with nothing committed after the final sweep; run evidence goes to the run's artifacts directory, never into a commit; and the spec is updated in the same task as the behaviour it describes. |
| `src/ralphd/prompts/worker.md` | Worker phase: the one-task-per-iteration rule with its reasons, the task pick rule, credential handling, and the sibling-cleanup prohibition. For a `review-cure`-origin task (`state.is_review_cure_origin`, task 002/A.2), `LoopSupervisor._worker_assignment_extra` folds a "## Fixing a review finding" section into the dispatched prompt (via `build_prompt`'s own `extra` parameter): fix the CAUSE the task's own `successCriteria` names (the finding's `cured_when` verbatim, when the finding carried one), write and pass at least two cases of the same rule the finding did not list beside the finding's own examples, and petition rather than ship a fix narrower than `cured_when`. Absent for a planner/steering-origin task and when no task is selected at all. |
| `src/ralphd/prompts/review.md` | Review phase: independently re-check every PRD requirement, re-verify criteria flagged as edited after a validation failure, report `{"verdict": "verified"}` or `{"verdict": "rejected", ...}` and write findings on rejection. Checks the product spec (`SPEC.md`, the user docs) against live behaviour, where the PRD's changes are described there, and treats a contradiction as blocking; numbers and claims in any other prose (commit messages, notes, issue comments, run artifacts, tracked reports) are neither evidence nor grounds for rejection. Never fix anything. When the approach under review has taken at least one GRANTED cure pass, `LoopSupervisor._confirmation_review_context` (v0.27 requirement B.1/B.2/B.3, #274, task 005) folds a "## Confirmation review" section into the dispatched prompt, built entirely from `cure-lineage.json` (task 004): every blocking finding of the MOST RECENT granted pass for this approach, named by the id of the cure task `insert_cure_tasks` built for it (e.g. `cure-01-02`), with its `clause` and `cured_when`; the diff range to check, `<preCureHead>..<current HEAD>` (that pass's own `preCureHead` to the workspace's live HEAD); the three checks, in this order — each listed finding closed at the level of its rule, the diff range breaks no requirement it touches, the tier suite named by the PRD is green at HEAD (run once) — and the rule not to re-audit a requirement the diff range did not touch. The same section states review's own confirmation-review-only linking obligation: every blocking finding it files must carry `reopens` (one of the listed cure task ids, naming a still-open finding) or `regression: true` (a failure caused by a change inside the diff range); a blocking finding carrying neither field is downgraded to residual (`review.finding_downgraded`, v0.27 requirement B.3, #274, task 006: `LoopSupervisor._review_findings_channel` applies `findings.apply_confirmation_review_downgrade` to the list it returns, so `_review_contract_ladder` -- which runs before `_review_verdict` -- and every later reader judge the post-downgrade severities, and a `verified` claim beside an unlinked blocking finding is no contradiction to re-ask over); an exposed secret and a requirement whose behaviour is absent altogether stay blocking regardless (`findings.confirmation_review_exempt`). Absent entirely for a first review of an approach that has taken no cure pass yet, and rebuilt identically from disk by a resumed process (nothing here is held only in memory). |
| `src/ralphd/prompts/task-verify.md` | Vigilant-mode per-task verification against `successCriteria`, reporting through the `verify` response contract (task 118) instead of a `<task-verified>id</task-verified>` sentinel. Rejects only on substance: an absent or wrong deliverable, a red named test, a spec that contradicts the live code, a named command never run, or an exposed secret; prose outside the spec is never a rejection ground. For a `review-cure`-origin task (`state.is_review_cure_origin`, task 003/A.3), `LoopSupervisor._verify_task_extra` folds a "## Checking the rule, not just the listed cases" section into the dispatched prompt (via `build_prompt`'s own `extra` parameter): the task's own `successCriteria` above is the finding's own `cured_when` verbatim, and before validating, the verifier must try at least one case of the same rule that is NOT taken from the finding's own evidence and NOT taken from any test the worker added, rejecting if that case still fails. Absent for a planner/steering-origin task. |
| `src/ralphd/prompts/reflect.md` | Post-terminal self-reflection: analyse the run and write a report plus an optional prompt/skill diff to `artifacts/reflection/`, touching nothing else, reporting through the `reflect` response contract (task 121) instead of a bare `<promise>REFLECTED</promise>` sentinel. Builtin-only — not in `PROMPT_NAMES`, so not overridable via the prompts API. |
| `src/ralphd/prompts/steer.md` | v0.14 requirement G (#98, task 009): the `steer` hat's prompt. Reshapes state (inserts/updates/deletes tasks, amends the standing rules) in response to unsettled operator steering; writes no code, holds absolute top priority over any pending/validation-failed task, and reports `{"settled": ..., "notes": ...}` -- never a claim about whether the tasks it touched will pass. Builtin-only, like `reflect.md`. |
| `src/ralphd/prompts/plan_review.md` | v0.14 requirement K (#101, task 018): the opt-in (`peer_plan_review: false` by default) peer plan-review gate's prompt -- runs once after planning, before the worker touches a task, and either approves the fresh plan or objects with findings that route back into a bounded planning revision (`peer_plan_review_max_revisions`, default `2`). Its objection grounds include a paperwork task (a tracked report or record as the deliverable) and an implementation task with no behaviour-failing test. Builtin-only. |
| `src/ralphd/prompts/verdict_gate.md` | v0.14 requirement L (#101, task 020): the opt-in (`peer_verdict_gate: false` by default), genuinely BLIND peer verdict gate's prompt -- runs once per approach after review's own `verified` verdict, handed only the finished work and the PRD, never the reviewer's reasoning or verdict, and either approves the run's exit or rejects it back into a new approach. Builtin-only. The blindness is an HONOR-SYSTEM limit, not a technical wall: nothing stops the agent subprocess from reading `iterations/`, `events.jsonl` or `output.jsonl` anyway, and nothing in the harness prevents that read before it happens -- so v0.17 requirement D.5 (#125 A) adds a post-hoc check instead (`state.detect_verdict_gate_peek`, called from `LoopSupervisor._record_verdict_gate_peek` at both call sites) that flags a tool call whose arguments mention any of those three paths as a `verdict_gate_peek` event plus a `verdictGatePeeks` entry on the run's status document: a peek is DETECTED after the fact, never PREVENTED, and the flag is a RECORDED OBSERVATION, never a verdict override; see `test_verdict_gate_peek_task020.py`. |
| `src/ralphd/engine/main.py` | Entrypoint and startup order: argparse-only `--help`/`--version`, run-dir `flock`, `schemaVersion` check, creds/skills placement, redaction-set build, PRD copy, then `LoopSupervisor.run_job()` + `uvicorn` in one event loop. Owns the `on_complete_cmd` hook and the process exit codes. |
| `src/ralphd/engine/config.py` | `JobConfig` (every budget, flag, timeout and retry knob, with its default and `RALPHD_*` override) and its `effective()` view for `GET /config`; the container path constants; the overlay → `/config` → builtin resolution order (`overlay_or_config`, `overlay_write_path`); per-hat model resolution (`model_for`, `MODEL_HATS`) and the retired-knob migration (`_migrate_legacy_model_config`, task 006). |
| `src/ralphd/engine/state.py` | The run directory as an object: paths, atomic write-then-rename JSON, `status.json` merge-patch updates, `events.jsonl` emit (scrubbed), the `.lock` flock, iteration directories and `max_iteration_number()`, steering inbox and consumed marker, the vigilant-verified record, operator-termination record, schema-version policy, the hardened `tasks.json` read (`read_tasks_doc()` → `TasksRead`, §5.3), and the shared duration/time/cost/approach/task-count/image formatters both sides render with. v0.27 requirement B.4 (#274, task 007): `root_cure_task_id` follows `tasks[].reopensCureTaskId` from any cure task id back to the root cure task its whole reopen chain descends from -- the identity `LoopSupervisor._review_cure_pass_available`'s confirmation-review comparison and `findings.reopened_root_finding_ids` both resolve through, never through a finding id's spelling or its `clause` text. |
| `src/ralphd/engine/loop.py` | `LoopSupervisor`: the whole loop. Approaches and composite PRDs, per-phase prompt assembly, budget accounting and refunds, vigilant verification, criteria fingerprinting, the stagnation and instant-failure guards, the infra-retry wrapper and outage-budget episode clock, the deadline-hold (`timed_out`) freeze path, steering application, pause/interrupt/abort/retry gates, the live `tasks.json` poller, and reflection. |
| `src/ralphd/engine/transitions.py` | The declared FSM table (§4, task 008/014): states, legal transitions, guard-predicate names and per-hat entries as one data structure, with vigilant and non-vigilant as two variants of it (`VIGILANT_DELTA`), plus `TransitionExecutor` — the one code path that walks the table and refuses an illegal move — and the router's declared per-state fallback (`ROUTER_FALLBACK`). |
| `src/ralphd/engine/tiers.py` | Task 012 (#68 half 2, requirements E/J): the three-tier vocabulary (`trivial < normal < hard`) with `escalate_tier`/`step_down_tier` as the only ordering moves, and `resolve_bundle` — the pure `(hat, effective_tier, host_map) -> HatBundle` resolver, no I/O, no clock, never touching `transitions.TABLE`. `default_host_map` is the one place a `JobConfig` is consulted, delegating to `JobConfig.bundle_for` rather than opening a second model-resolution pathway. Applied at the single dispatch call site in `loop.py` (task 016), never inside the transition table itself. |
| `src/ralphd/engine/scheduler.py` | Task 115 (#60, requirement C/F): the pure task-selection algorithm (`select_next_task()`) -- validation-failed first, then in-progress, then the `dependsOn`/`priority`-aware pending scheduler, else `None` -- mirroring `worker.md`'s documented pick rule. Wired into `loop.py`'s `_step_worker` (task 116): the harness runs it and writes the pre-spawn `in-progress` claim itself, before the worker subprocess ever starts (§4.1). |
| `src/ralphd/engine/router.py` | The harness's single constrained question for a state whose next legal edge is not decidable from disk alone (task 018): deterministic-first (never asks for a state with ≤1 legal edge), one JSON question validated against the table and a schema, bounded by a timeout, falling back to the table's declared choice rather than ever accepting an illegal transition. |
| `src/ralphd/engine/contracts.py` | The one validator every hat response contract goes through (task 009, fact 16/#65): per-hat JSON schema declared beside its table entry, `validate_payload()`/`validate_against()` as the sole check, so there is exactly one re-derivation of "does this payload match its schema" in the whole codebase. |
| `src/ralphd/engine/hat_tools.py` | Task 033 (operator-inserted scope, #114): the steer hat's tool surface WIRED LIVE. `STEER_WIRED_TOOLS` is the one declaration of what the hat can call -- `state.STEER_HAT_TOOLS`'s four entries (never re-listed here) plus `context-read`/`context-write` over the run-context files -- and `extension_source()` renders it into the generated `pi` extension (`pi.registerTool()` per entry, each declaring via `parameters_source()` exactly the top-level keys of its own documented usage payload -- the schema the live tool accepts is the payload `prompts/steer.md` tells the hat to send, and the call is forwarded verbatim) the steer iteration alone is launched with (`LoopSupervisor._hat_extensions` -> `PiRunner.run`'s `-e`), so the registered set cannot drift from the wired set. `SteerToolBroker` is the only thing that EXECUTES a call, in the engine process (the uid that owns `tasks.json` under the #60 privsep boundary), over a request/response spool under `<run>/hat-tools/` that the hat writes into and reads answers from: every applier guarantee therefore holds unchanged (`state.apply_task_status_change` for a `status` key with its documented refusal diagnostics, `origin=steer`/`status=pending` on an insert, a delete refused while another task's `dependsOn` names the target), `tasks.json` is readable but never writable through `context-write`, and every served call emits `steer.tool_call` while every write also emits `steer.context_write` naming the file and the steering entry that caused it. `call()`/`main()` are the hat-side client the extension shells out to (`python -m ralphd.engine.hat_tools <tool>`), so the protocol is written down exactly once; a call nothing serves times out into an answer, never a hang. |
| `src/ralphd/engine/findings.py` | The structured findings channel beside `review-findings.md` (task 001, requirement A, #110): `FINDING_SCHEMA` (`severity` enum `blocking|residual`, `curable` bool, `clause`, `evidence`), validated per-element through `contracts.validate_against` — no second validator — and round-tripped through the `review-findings.json` sidecar (`write_findings_json`/`read_findings_json`). The markdown channel is kept unchanged. `compute_verdict` (task 002, requirement A.2, #110) is the ONE place that reduces a findings list to `"rejected"`/`"verified"` from its severities alone — `LoopSupervisor._review_verdict`/`_review_contract_ladder` (`loop.py`) are its only callers; the rubric default and the prompt update stay later tasks. `all_blocking_findings_incurable` (task 005, requirement B, #111) is the ONE place that answers whether a rejection's blocking findings are ALL incurable — it takes the findings list alone (no run dir, no workspace path), so curability stays asserted by review only; wiring it into a `rejected_incurable` edge is a later task's scope. v0.27 requirement B.3 (#274, task 006): `FINDING_SCHEMA` also declares two more optional fields, `reopens` (string) and `regression` (boolean) — a confirmation review's own linking obligation (see the `review.md` row below) — and `apply_confirmation_review_downgrade`/`confirmation_review_downgrade_candidates`/`confirmation_review_exempt` implement the downgrade rule itself: every `severity: "blocking"` entry carrying neither field, and not exempt (an exposed secret or an absent-behaviour finding, matched case-insensitively against the finding's own `clause`/`evidence` text), is replaced by a copy carrying `severity: "residual"` instead. Called from `LoopSupervisor._review_findings_channel` (every reader's one source of the list -- the contract ladder, `_review_verdict`, the post-verdict readers), guarded on `LoopSupervisor._is_confirmation_review()` and on the read not being a mid-run adjudication episode or mid-plan review (`mid_run=True`), which emits `review.finding_downgraded` once per downgraded entry per review pass. v0.27 requirement B.4 (#274, task 007): `reopened_root_finding_ids` is the identity side of `LoopSupervisor._review_cure_pass_available`'s confirmation-review no-progress comparison -- every blocking finding's `reopens` cure task id, followed back through `state.root_cure_task_id` to its root finding, never through a `clause` string. |
| `src/ralphd/engine/petitions.py` | Task 065 (requirement N.6, #150): every reader/writer below takes an optional `run` handle; every `LoopSupervisor` production call site passes its own run, so a filing and its adjudication both land in `RunDir.petitions_file` (`<run dir>/petitions.md`), never a tracked workspace file -- `run=None` (every pre-existing test fixture) keeps the historical behaviour below byte-for-byte. Task 010 (requirement C, #112): parses every `<wave>-petitions.md` file under the WORKSPACE's `artifacts/reports/` (`docs/petition-format.md`'s own convention, task 009) into petition dicts (`obligation`, `type`, `evidence`, `adjudication`, `remedy`), finds the OPEN ones (no `Adjudication` field yet), and renders them as a named review-prompt context block (`petitions_review_context`) -- the same treatment `LoopSupervisor._operator_amendment_review_context` gives an operator ruling, joined at the same `_review_extra_context` seam via `LoopSupervisor._petitions_review_context`. Does not adjudicate anything itself -- only review does, through the `petition_adjudications` field `HAT_SCHEMAS["review"]` now accepts. Task 011 (requirement C.3/C.4, #112) adds the run-scope persistence layer: `record_confirmed_adjudications`/`read_confirmed_adjudications` (an authenticated JSON at `RunDir.petition_adjudications_file`, written ONLY by `LoopSupervisor` -- never a worker or review hat process -- through `privsep.write_root_owned_json`, carrying a keyed HMAC-SHA256 over a per-run secret that NO file under the (agent-owned) run dir holds -- it lives in the engine PROCESS's memory for the episode (`_authority`), plus, only where `privsep.separated()` or `$RALPHD_RUN_SECRETS_DIR` names one, a root-owned 0600 file in a root-owned 0700 store OUTSIDE the run dir (`key_store_dir`) so a later episode can still authenticate -- plus an append-only `events.jsonl` witness line per write (`petition_adjudications_recorded`) and the engine's own in-memory memory of the last MAC it wrote, which is the tamper-evidence property: an unkeyed digest stored beside the data it covers is recomputable by anyone who can write the file, and so is a keyed MAC whose key sits in the same agent-owned tree), `confirmed_obligations` (the normalised set, or an empty set + `tampered=True` on any of: a sticky prior `petition_adjudications_tampered` detection, a missing/malformed record the engine did announce writing, a keyed-MAC mismatch, an unkeyed-digest mismatch, a MAC other than the last one this engine process wrote, or a witness mismatch; a record this episode holds no key for at all is read as EMPTY with a `petition_adjudications_unauthenticated` event and is NOT tampering, since destroying trust in the record only ever removes exemptions), `settled_obligations` (#209 §5: the same normalised set over CONFIRMED **and** REFUTED entries, with the identical tampered-means-empty posture, read by `open_unconfirmed_petitions`/`petitions_review_context` alone so an obligation review already ruled on either way is not re-presented as OPEN to a later review pass -- suppression of a rehearing, never an exemption, and never a deletion: the entry, its remedy and its `petition_refuted` event all stay), `downgrade_confirmed_findings` (recategorises a matching `blocking` finding to `residual`, applied inside `LoopSupervisor._review_findings_channel` for every caller), and `tamper_review_context` (surfaces a detected tamper to review's own prompt as a mandatory blocking finding). Task 012 (requirement C.1, #112) adds `duplicate_petition_obligations`/`duplicate_petition_findings`: every normalised obligation with MORE THAN ONE filed petition section across every `<wave>-petitions.md` file (a duplicate filing, or a re-filed petition whose earlier filing was already `REFUTED` -- `docs/petition-format.md`'s single rule covers both) is purely structural, so the engine detects and reports it directly as a `severity: "blocking"` finding, APPENDED unconditionally inside `LoopSupervisor._review_findings_channel` regardless of what review's own findings.json said; `duplicate_petitions_review_context` names the same fact in review's own prompt via `LoopSupervisor._duplicate_petitions_review_context`, joined at `_review_extra_context`. #209 defect 4 adds `refuted_remedy_cure_findings` (the pure DECISION half of turning a REFUTED `substance` remedy into WORK: one blocking-finding-shaped record per entry that clears all six gates -- REFUTED, a non-blank obligation and remedy, `remedy_type` resolving to `substance` rather than the advice-only `form`, an obligation matching a petition still OPEN on disk, and no task already stamped `remedyFor` for it -- read by `LoopSupervisor._insert_refuted_remedy_tasks`, which turns each one into ONE cure task through `state.insert_cure_tasks` and emits `PETITION_REMEDY_TASK_EVENT`; until then a `substance` remedy reached the plan nowhere, because the only thing it drove was moving the petitioned task to `failed`, whose sole legal successor is `skipped`). |
| `src/ralphd/engine/run_record.py` | Task 006 (PRD requirement B.1, #140): builds and renders the run's own terminal record -- `build_run_record()` reads status.json (state/verdict/reason/approach/iterationsUsed/usage), the engine-owned per-approach judge verdicts in `approach-metadata.json` for both the live and every archived approach, falling back to the `approaches/NN/` findings sidecars only for an approach with no metadata entry (`build_approach_history`, task 035), `petition-adjudications.json` (`build_petitions_section`, via `petitions.read_adjudications` -- the RENDER read, which returns EVERY adjudicated entry, REFUTED with its remedy text included, and then merges the `petitions.PETITION_REFUTED_EVENT` witness log for a REFUTED adjudication that reached only that channel; `petitions.read_confirmed_adjudications` is the narrower EXEMPTION read behind `confirmed_obligations`, and the record renders more than it), every consumed steer (`build_steers_section`), every operator ruling under `<config_root>/amendments/` (`build_operator_rulings_section`), and every `notification.sent` event (`build_notifications_section`; a FAILED send emits the distinct `notification.failed` event instead -- task 009, requirement B.1 -- and never appears in this section). `render_run_record_markdown()` renders the identical record for a human -- every top-level section declared in `RECORD_SECTIONS`, including `usage`'s per-direction token fields and its byPhase/byApproach/byTier breakdowns (task 035); `write_run_record()` writes both `artifacts/run-record.json` and `artifacts/run-record.md` and is called by `LoopSupervisor._write_run_record()` from `_finish()` (every entry to `finishing`) and from `_promote_finishing_state()`/`_promote_recorded_finishing_state()` (the later terminal transition that rewrites both). |
| `src/ralphd/engine/contract_enforcement.py` | The re-prompt loop around `contracts.validate_payload` (task 010): malformed JSON, an absent payload, and valid JSON of the wrong shape are all ordinary cases here, re-prompted with the specific validation error up to `contract_retry_max` times, logged loudly to `events.jsonl`, and scored as one agent-fault iteration if the hat never recovers. |
| `src/ralphd/engine/plan_ingestion.py` | Ingests the planner's file-borne `plan-proposal.json` as untrusted input (task 011): schema validation, then semantic lint (unresolvable `dependsOn`, cycles, duplicate ids, boilerplate `successCriteria`, a task gated on this run's own terminal state), then apply — initialising every task to `pending` regardless of what the planner wrote for `status`. |
| `src/ralphd/engine/runner.py` | `PiRunner.run()` + `IterationResult`: spawn one `pi` process, pump and scrub its NDJSON into `output.jsonl`, parse events and usage, scan for the exact sentinels, enforce the iteration timeout and the startup-window watchdog, and kill the process group on every exit path. |
| `src/ralphd/engine/privsep.py` | The uid boundary (§13.4): resolve the account iterations run as, drop the engine's *effective* identity to it while keeping root real and saved, and hand every spawn site the kwargs that drop a child all the way down. `separated()` reads the live process credentials, so nothing claims a boundary the kernel is not enforcing; every function is a no-op with one warning when the engine did not start as root. `root_for_write()`/`write_root_owned_json()` (task 105, #60 requirement D) reuse the same boundary the other direction: momentarily raise the engine's own effective uid back to root to write a file root-owned, restoring it before returning. `write_protected_tasks_json()`/`release_protected_tasks_dir()` (task 004, requirement B, #202) close the gap `write_root_owned_json()` left open for `tasks.json` specifically -- a root-owned FILE in an agent-writable DIRECTORY still lets a dropped child `os.replace()` a substituted document onto it, a directory-entry operation gated by the parent, never the target -- by landing the authoritative copy inside a root-owned 0700 directory (`PROTECTED_TASKS_DIRNAME`, self-healed against a precreated file or a planted symlink on every write, not only at creation) and refreshing an ordinary agent-readable view copy alongside it; the protected directory is handed back to the agent uid at the engine's own shutdown funnel so a host-side `rm`/`DELETE` can still remove it. |
| `src/ralphd/engine/api.py` | The FastAPI app: every route in §3.1, bearer-token enforcement, the SSE event stream, `GET /logs` tail and live-follow on top of `log_merge`, `PATCH /config/budget`, and the config CRUD that writes into the overlay and re-places creds/skills immediately. |
| `src/ralphd/engine/faults.py` | `classify_fault()` — a pure function mapping a finished iteration to `"infra"`, `"work"`, or `None`, with `_INFRA_TEXT_PATTERNS` as the single reviewable signature table (one commented line per family) and the `aborted`/operator carve-out. No engine state. |
| `src/ralphd/engine/pricing.py` | `PricingMap`: the optional host-side per-million-token rate table with gateway alias globs and declared-free patterns, used *only* when the provider quoted no usable price, producing a cost marked derived and never merged into `costUSD`. `PricingChain` layers the operator map over a built-in table so exactly one of them prices any message, and `resolve_pricing()` builds whichever of the two `price_strategy` asks for (§8.6). |
| `src/ralphd/engine/pricing_aws.py` | The shipped AWS Bedrock rate table: canonical per-model USD/Mtok rates, a generated alias map for the gateway spellings, and a machine-readable `AS_OF` date with a `staleness()` verdict. Exposed as a `PricingMap` through `pricing_map()`, so there is one matcher rather than a second resolver, and refreshed by `tools/refresh_bedrock_rates.py`. |
| `src/ralphd/engine/redact.py` | The mechanical secret scrubber: build the value set from env vars and placed cred files, rebuild it after any creds/llm mutation, keep it in memory only, and expose `scrub_text` to the three write/serve points. Enforces the ≥8-character floor. |
| `src/ralphd/engine/commit_scan.py` | Task 029 (PRD v0.25 requirement I.2, #210): the git sink's bound. `redact.py` scrubs known secret VALUES out of everything the engine persists or serves; a commit the worker writes and pushes is neither, and that sink is irrevocable. `scan_commits_for_secrets` is the detector — VALUE matching only against the redaction map's `value -> label` pairs, never a "looks like a token" shape, over every commit in a boundary range's MESSAGE and then DIFF. `push_if_clean` is the engine-side gate (scan first; a `git push` subprocess starts only on a clean scan), `install_push_gate`/`gate_push_refs`/`hook_main` are the same scan reached from git's OWN `pre-push` hook through an engine-owned `core.hooksPath` outside the workspace — the layer that stops the WORKER's own mid-iteration push, which the task boundary is too late to prevent — and `refusal_text` is the one refusal spelling, naming the variable NAME only and never the matched value. `LoopSupervisor._scan_task_boundary_commits`/`_install_commit_scan_push_gate` are the production call sites; fails open on an unreadable git read, like its `loop.py` siblings. |
| `src/ralphd/engine/creds.py` | `place_creds()`: `/config/creds` (plus overlay and tombstones) → `~/.creds/*.env` at `0600`, conventional extras (`gitconfig`, `git-credentials`, `netrc`, `ssh/`), and the one-shot `setup.sh` escape hatch. Logs names, never contents. |
| `src/ralphd/engine/skills.py` | `place_skills()`: rebuild `~/.pi/agent/skills/` from scratch (symlinks) out of mounted skills, API-origin overlay skills that win over same-named mounted ones, and tombstones — cheap enough to re-run after every mutation, so skill CRUD needs no restart. |
| `src/ralphd/engine/llm.py` | The mid-run LLM path: `PUT /config/llm` writes the env-override set to the overlay (read fresh each iteration) and deep-merges a `models.json` fragment into the file `pi` itself reads, so a rotated key applies to the next invocation. `provider_keys()`/`check_hat_routes()` (§3.1) take `home` as a required keyword-only argument — no ambient `$HOME` fallback — so a caller that omits it gets a `TypeError` before either ever reads a filesystem path. |
| `src/ralphd/cli/main.py` | `ralphctl`: every subcommand, the `docker run` assembly for `start`/`resume`, the registry and its defaults, templates, run-id generation, the API client, `--json` output and exit codes, `logs`/`watch` streaming and `tail`-style argv preprocessing, `doctor`/`doctor --fix`/`repair`, the auto-resume opt-in and its crash-loop guard, and `AUTO_RESUME_DEFAULT` as the single default literal. |
| `src/ralphd/cli/log_render.py` | The shared NDJSON→lines pretty renderer: iteration/phase headers, streamed assistant text, tool calls as one-liners, thinking collapsed to a single marker per block, per-iteration usage/cost footers, errors highlighted. `tty=False` yields plain text with no ANSI and no `\r`. Lives here, not in `main.py`, because the hub must import it without a cycle. |
| `src/ralphd/cli/image.py` | The job image, declaratively and docker-free: which files are inputs (`IMAGE_INPUTS`), how they hash to a tag, the packaged-inputs fallback for an install with no checkout (`PACKAGED_FILES`), and the text of the generated derived recipe (§3.2). Runs no builds — `cli/main.py` does that. |
| `src/ralphd/cli/patch.py` | Reading a reflect phase's `suggestions.diff` (§16): parse a unified diff, select a subset of its files up front (`select_patches`, §17 entry 2), plan it against a target tree (which paths it would touch, the new content, one structured rejection per hunk that does not fit — naming the file and the `@@` header), and write only under a separate `apply_plan()` call, all-or-nothing. No `git`/`patch` subprocess, no fuzzy matching, no renames: a diff this cannot apply is refused, never guessed at. Prints nothing and reads no run directory — `cli/main.py` owns the verb. |
| `src/ralphd/cli/ui_server.py` | The hub server: `/api/runs` and `/api/runs/<id>[/logs,/prd,/steering,/documents,/artifacts,/fault,/cost,/iterations/<n>,/steer,/retry,/unpause]` over the registry (`DELETE /api/runs/<id>` for a terminal run), proxying a run's live API with a short timeout and falling back to the run dir (`"live": false`) rather than 500-ing on the four views that have a live answer at all; renders log tails, cost breakdowns, fault explanations and iteration detail through the same modules `ralphctl` prints, server-side, so the browser only displays strings; serves the static bundle with `index.html` fallback. Task 013: `rendered_log_lines`'s tail request tries `render_index.bounded_tail` first, on both its live and dead-run surfaces alike (reading the run directory directly serves either identically) — `bounded_tail` walks backward across however many earlier iterations still carry a usable render index when the requested line count exceeds the latest iteration's own rendered output, so only when no usable index covers the request (no iteration yet, or an iteration the walk still needs has no usable index) does the older, growth-based outer fetch (`_fetch_live_tail`/`_fetch_dead_tail`) run at all. Task 014: when that render index is unusable (genuinely absent, truncated, or version/size-mismatched) so this outer fetch is the one answering, it no longer accepts a fetch merely because it already rendered enough lines — if the raw text's own `collapseSeed` still discloses an `unresolved` flag (`log_merge`'s own guess, never a measurement), the loop keeps doubling its bound and re-fetching, paying whatever scan the transcript demands, until either the flag resolves or there is provably no more of this iteration left to read (`content_lines < bound`). A guessed flag is never rendered as if it were certain, and an already-streamed suffix is never replayed in its place. The byte bound is a property of the indexed read only: a cold read with no usable index is a correctness scan with no byte bound, and can read the whole latest-iteration transcript. Task 015 (v0.26 requirement C.4, #271/#272): the absent-index cost above is paid at most once per iteration -- originally scoped to the LATEST iteration alone. `_ABSENT_INDEX_CACHE` (module-level, `{run_id: {iteration number: ((inode, size, mtime_ns, ctime_ns), index)}}`, the same per-run-id shape/LRU discipline `_bound_run_cache` already applies to `_LOG_TAIL_CACHE`/`_ITERATION_ROW_CACHE`/etc., capped and dropped by `_drop_run_detail_caches` on `delete_run`) is a HUB-owned cache, never written into any run dir the hub only reads: it holds, per run id, one entry PER ITERATION NUMBER, so a tail spanning several unindexed/unusably-indexed cold iterations caches each of their scans independently rather than sharing one slot the newest iteration's own scan would otherwise evict. Task cure-01-02-2 (v0.26 requirement C.4, D.1; DoD 3) re-keys each entry's own identity witness from a size-only pair to the full `(inode, size, mtime_ns)` triple `render_index` itself already uses -- a size-only key let two DIFFERENT runs' same-numbered iterations collide whenever their transcripts happened to share a byte count, a correctness gap size alone could not close; the finer witness also means a live iteration's in-place, same-size rewrite (whose mtime moves even though neither its inode nor its size do) is caught exactly as `render_index`'s own witness already catches it. Task cure-01-03-2 (v0.26 requirement C.1/C.3, D.1; DoD 3) extends that triple to a quadruple by adding `ctime_ns`: an ordinary, timestamp-preserving rewrite that restores the transcript's original mtime via `os.utime` afterwards leaves the triple unchanged, but always moves ctime (a normal write cannot restore it the way it can mtime), so both the cache's own lookup and `render_index.bounded_tail(..., index=)`'s witness check on an already-cached index now catch it too. `_absent_index_cache_get`/`_absent_index_cache_put` are the read/write sides (`compute_render_index`, never `write_index`, so the run dir is never touched); `_absent_index_cache_lookup_or_rebuild_refused(run_root, run_id)` builds `bounded_tail`'s new `on_missing=` callback (`render_index.py`, above) for `rendered_log_lines`'s FIRST, always-attempted pass: a hit in the per-iteration cache is reused outright, an iteration with an on-disk `render_index.json` that exists but was refused is rebuilt from the transcript's current bytes right here and cached (folding in task cure-01-01-4's own single-iteration `_refused_index_rebuild`, now retired), and an iteration with neither -- but this SAME run already has at least one OTHER iteration number cached and still valid for its current on-disk identity -- is rebuilt eagerly too (task cure-01-02-3, v0.26 requirement C.4, D.1; DoD 3, review-135): declining here would make `bounded_tail` abandon the WHOLE walk and fall through to the growth-based fetch below, which reads every iteration the tail needs straight off disk with no cache of its own, rescanning that OTHER, genuinely unchanged iteration's full bytes purely because THIS one changed -- exactly the falsifying case a review caught (a two-cold-iteration tail, only the newer one appended to or rewritten, still re-read the older, unchanged one's ~11.8M bytes). An iteration with neither an on-disk index nor ANY other iteration of this run cached and valid declines (`None`) exactly as an absent index always has, falling through to the growth-based fetch below -- so a lone or still-growing absent-index iteration with nothing else of its run cached yet never pays a from-scratch rebuild on every single poll merely because its own size changed again; only a run that already has another settled iteration's cache worth protecting pays the eager rebuild instead. Deliberately never keyed off the mere PRESENCE of a same-numbered cache entry regardless of whether it still matches, since this cache is shared, by run id and iteration number alone, across every run this hub process ever serves, and a bare stale entry left behind by an unrelated earlier run reusing the same id and number would otherwise force a needless eager rebuild -- the OTHER-iteration check above always re-validates through `_absent_index_cache_get`'s own identity check against THIS run's iterations, never a bare presence test. Once that first attempt fails and the older growth-based fetch (`_fetch_live_tail`/`_fetch_dead_tail`) has resolved the tail some other way, `_absent_index_cache_fill(run_root, run_id, n, force=grew)` walks `bounded_tail`'s backward walk a second time with an `on_missing` that builds and caches (`_absent_index_cache_put`) every iteration still found missing or unusable -- not merely the latest one (task 015's own original scope) -- so a LATER repeated tail spanning the SAME cold iterations is served entirely by the first, lookup-only pass, never rescanning any of them again. `force` (task cure-01-04, v0.26 requirement C.4, DoD 3; unchanged by this task) is the caller's own report of whether ITS scan actually needed the growth-based fetch's own FULL-ask bound to resolve (both fetch functions' own third return value); an iteration is skipped (a silent no-op, no cache write) only when `force` is false AND that iteration's own legacy `collapse_index.json` is present and valid -- PRD P's own cheap, collapse-index-bounded fixtures resolve within a couple of growth rounds without ever reading their iteration's own transcript in full, so building a whole extra render index for one here would be a NEW cost this task must not add. Any transcript growth, in-place rewrite or replacement at all changes an iteration's own identity and the next request against it scans (and recaches) again, so staleness only ever costs a repeat of that ONE iteration's own scan, never a wrong render and never every other iteration's own already-cached coverage. |
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
| `planning` | `planning.md` | PRD, workspace | a file-borne `plan-proposal.json` plus `{"proposal_path": ..., "summary": ..., "standing_rules": ...}` (task 120/107) -- the harness ingests the proposal into `tasks.json`, initialising every task to `pending`, and pins `standing_rules` (when supplied) as this run's own `standing-rules.md`, re-injected into `notes.md`'s `## Standing rules` block after every later iteration (§5.1) |
| `worker` | `worker.md` | `tasks.json`, `notes.md`, workspace | `{"verdict": "complete", "task_id": ...}` for the task the harness assigned this iteration (task 116) -- `{"verdict": "complete"}` alone is refused; a `no_verdict`/`unsatisfiable` verdict, or nothing parseable, leaves the task's status untouched -- the harness alone applies the resulting move (`awaiting-validation`/`completed`), never the agent's own `tasks.json` write; `notes.md` refreshed; the narrower whole-plan `{"verdict": "plan_complete"}` (v0.14 requirement O, #100, task 015 -- retiring the `<promise>COMPLETE</promise>` marker) only when no task was assigned this iteration and every task the plan holds was ALREADY terminal before it started (§4.3) |
| `verify` | `task-verify.md` | one task's `id`/`title`/`successCriteria` | `{"task_id": {id}, "verdict": "verified"}`, or `{"task_id": {id}, "verdict": "rejected", "reason": ...}` -- the harness applies `validated`/`validation-failed`/`failed` and copies `reason` onto that task's `validationNotes` (task 118) |
| `review` | `review.md` | PRD (composite when present), `tasks.json`, workspace | `{"verdict": "verified"}`, or `{"verdict": "rejected", "reason": ..., "findings_path": "review-findings.md"}` with `review-findings.md` listing every unmet requirement -- the harness applies `succeeded`/`verified` or archives the approach from this payload (task 119) |
| `reflect` | `reflect.md` | the finished run's own records | `artifacts/reflection/report.md`, optionally `suggestions.diff`, plus `{"report_written": ..., "suggestions_written": ...}` (harness applies the `ok`/`error` verdict from this payload and the file check together, task 121) |

`planning` runs once per approach, then the `worker` loop (with `verify`
iterations interleaved in vigilant mode, §4.4), then `review` once the worker
signals completion; `reflect` runs after the run is already terminal and cannot
change how it ended. What runs next is decided from disk state, not from agent
narration: `_resume_point()` skips `planning` when the run dir already holds a
`tasks.json` with tasks and at least one completed iteration, continuing the
existing approach's worker loop; `planning` that produces no tasks abandons the
approach; the `worker` loop runs while budget remains, ending on an honoured
`"plan_complete"` verdict (§4.3), on a plan with nothing actionable left and at
least one `failed` task (§5.3), on three consecutive iterations that changed
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

`JobConfig.model_for(phase)` resolves each hat's model: an explicit
per-hat override (`model_overrides`/`models`), else the plain `model` field.
`None` means "whatever `pi` defaults to". An unrecognised hat name degrades
to `planning`'s own configured entry (`UNRECOGNISED_HAT_FALLBACK`), never to
some cheaper choice — a typo must never quietly downgrade a hat.

Task 006 retired the earlier `model_strategy`/`fast_model` phase→tier preset
table entirely (`quality-first`/`cost-optimized`/`balanced`, keyed by
`fast_model` falling back to `model`) — there is no implicit "fast" tier any
more without an explicit `models`/`model_overrides` entry naming it. A
job.yaml still carrying either retired key is translated deterministically,
at load time, into an equivalent `models:` table naming every hat
(`engine.config._migrate_legacy_model_config`), so an old run keeps
resolving to the models it always did; the two CLI surfaces
(`ralphctl start --model-strategy`/`--fast-model`, `ralphctl config set
model_strategy`/`fast_model`) are refused outright, naming the `models:`
replacement. `reflect` has its own table entry like any other hat. The
resolved reference is recorded per iteration (`meta.json`'s `model`, the
`iteration.start` event), so a run's model mix is auditable.

**The router is not a sixth phase (task 018, requirement E, #60 pillar 2).**
`_step_generic_hat` only ever calls `engine.router.decide()` when
`transitions.successors(state, vigilant)` returns more than one edge for the
current state — deterministic-first: a state with ≤1 legal edge (most states,
most of the time) never routes at all. When it does, `model_for("router")`
resolves the model: `model_overrides["router"]` first if set, else the
dedicated `router_model` config key (`JobConfig.router_model`, env
`RALPHD_ROUTER_MODEL`, `job.yaml` key `router_model`), else `model` directly
— `router` carries no strategy-tier row (the mechanism it would have had a
row in is gone), deliberately (a one-question classification never needs a
per-strategy tier).
The call is a single JSON question validated against `router.ROUTER_SCHEMA`
and the table's own legal edges, re-prompted up to `contract_retry_max` times,
bounded by `router_timeout_s` (`RALPHD_ROUTER_TIMEOUT_S`, default `60.0`,
surfaced as `GET /config`'s `budgets.routerTimeoutS`); a hang or an
unrecoverable answer falls back to the state's own declared choice
(`transitions.fallback_guard`/`ROUTER_FALLBACK`) rather than ever taking an
edge the table does not list. `LoopSupervisor._ask_router()` is deliberately
uncounted — no iteration slot, no `iterations_used` increment, no
`meta.json` — but its cost is not hidden: each call gets its own transcript
under `artifacts/router/NNNN.jsonl` (a plain counter, not an iteration
directory) and its usage folds into `usage.byPhase["router"]` (§4.2) exactly
like the five hat phases, so a run's model mix and cost are auditable even for
the calls that never cost an iteration.

**A contract re-ask is not an iteration either (task 001 requirement B, task
004).** `LoopSupervisor._ask_contract_fix()` is the uncharged channel every
`contract_enforcement.enforce_contract` retry ladder calls as its `respond`
once a hat's response fails `contracts.validate_payload` or its semantic
sibling check: it states the validation errors verbatim and asks ONLY for the
corrected final payload, never the hat's own task prompt again, so a re-ask
can fix a payload's shape but can never hand the hat a second bite at doing
more workspace work. Built directly on `_ask_router`'s own precedent, it is
uncounted the same way, by the same construction: no iteration slot, no
`iterations_used` increment, no `meta.json` (`loop.py` never calls
`_run_iteration_once` or touches `self.iterations_used` from inside it).
Each call gets its own transcript under `artifacts/contract/NNNN.jsonl` (a
plain counter, `loop.py:2963-2966`, not an iteration directory), and its
usage folds into `usage.byPhase["contract"]` (§4.2, `loop.py:2982`) — kept
out of `"router"`'s bucket because the two uncharged channels answer
structurally different questions (which edge to take, versus what the
corrected payload is) — so a run's cost is auditable even for the calls that
never cost an iteration or a retry against `contract_retry_max`. The re-ask
runs on the offending hat's own model (`self.cfg.model_for(hat)`, task 004),
not a fixed `"contract"` lookup, so the repair stays inside the tier that
made the mistake.

### 4.2 Iterations, approaches and the composite PRD

An iteration is one agent subprocess and one directory, `iterations/NNNN/`
(4-digit, zero-padded, monotonically increasing). A restarted engine seeds its
counter from `RunDir.max_iteration_number()`, which counts only iteration
directories whose `meta.json` has an `endedAt`, so a slot left half-written by
a killed process is reused and a finished one is never renumbered. What is
reused is the *number*: `RunDir.begin_iteration_dir()` first moves the crashed
attempt's `prompt.md`, transcript and partial `meta.json` into
`iterations/NNNN/attempts/NN/` (§5.5), so the resumed attempt cannot overwrite
the record of the iteration that died — the one an operator most wants to read.

An *approach* is one full attempt at the PRD: plan, work, review. Approaches
run sequentially from 1 to `max_approaches` (`--max-approaches`, default `3`)
and **share one iteration budget** — approaches bound how many times the job
may start over, not how much work it may do.

| an approach ends because | archived | composite PRD rewritten |
|---|---|---|
| `planning` produced no tasks | no | no |
| three consecutive worker iterations changed nothing (stagnation guard) | yes | no |
| `review` rejected it | yes | yes |
| `review` itself was cut short by the engine, with no budget/wall clock left to re-ask (`review_unjudged_at_budget_end`, v0.10 requirement A, #72) | no | no |
| `review` ran to completion but never produced a parseable verdict across its whole contract ladder (`review_contract_unrepaired`, GH #209 defect 2) | no | no |
| `review` ran to completion with a legible verdict claim, but its own REVIEW CONTRACT CHANNEL never became usable across the whole contract ladder -- a present-but-invalid findings sidecar, a `verified` claim contradicting a blocking finding, a curable finding with no `cured_when`, or a stray/wrong-typed field elsewhere in the payload's own closed schema (`review_findings_unusable`, v0.23 requirement C.2/C.3, #219) | no | no |

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

**An interrupted review is not a rejection (v0.10 requirement A, #72).**
`LoopSupervisor._review_interrupted()` reads the `IterationResult` itself --
`interrupted`, `timed_out`, `no_traffic_timeout`, or a non-`0`/non-`None`
`exit_code` -- to tell "the engine cut this attempt short" apart from "the
iteration ran to completion and replied with nothing `_review_verdict` could
parse"; it is the only thing that routes a review iteration onto the two
edges named below (GH #209 defect 2 added a third, separately declared exit
for a completed iteration whose verdict claim was never readable — the next
paragraph).
With budget and wall clock left, `_run_review_iteration()` self-loops in
place on the `review_interrupted` edge and re-asks at no cost to the
approach — the same shape as `planning`'s `instant_failure_streak` and
`worker`'s `no_verdict_yet`. With none left, `_step_review` advances straight
to `finishing` on `review_unjudged_at_budget_end` (see the table above): no
archive, no `review rejected approach N` log line, no composite PRD, and the
approach counter does not move. This closes the incident recorded at
`selfdev-v09-fsm-loop` iteration 183, where the clock cutting a review
attempt short was scored as review's own judgement.

**Nor is a review that never produced a parseable verdict (GH #209 defect
2).** A review iteration that DID run to completion but whose own verdict
claim was unreadable — no parseable object at all, or a `verdict` absent or
outside `HAT_SCHEMAS["review"]`'s `verified`/`rejected` enum — in the reply
the run was CHARGED for, and whose ladder then exhausted without ever
returning a repaired payload (the uncharged re-ask answers are discarded on
exhaustion, so the charged reply is also the text `_review_verdict` re-reads
and the only verdict claim the engine still has in hand), is recorded on
`LoopSupervisor._review_contract_unrepaired` by `_review_contract_ladder`,
and `_step_review` then takes the declared `review_contract_unrepaired` edge
to `paused`: the approach is left unarchived, no composite PRD is written, no
`review rejected approach N` log line is emitted, the approach counter does
not move, `approach.verdict_recorded` says `verdict: "unreviewed"`, and a
`resume` re-enters `review` over the SAME approach. Before this a
still-broken payload fell through to `_review_verdict`'s "not verified"
default and was scored as a substantive rejection, so `$.verdict: 'fail' is
not one of ['verified', 'rejected']` — pure schema pedantry — archived an
approach and wrote a composite PRD for a successor that need not exist.

**Nor is a review whose verdict claim was legible but whose findings channel
never became usable (v0.23 requirement C.2, #219).** The discriminating
sibling of the paragraph above: a review reply whose OWN verdict claim IS
legible (a schema-valid `verified`/`rejected` word) but whose ladder still
exhausted because the FINDINGS CHANNEL it re-asked over never re-validated
-- a present-but-invalid sidecar, a `verified` claim contradicting a
blocking finding, or a curable finding with no `cured_when`
(`LoopSupervisor._review_contract_ladder`'s own `findings_channel_unusable_
reply`, requirement C.1/task 010) -- is recorded on `LoopSupervisor._review_
findings_unusable`, and `_step_review` takes the declared `review_findings_
unusable` edge to `paused`, checked immediately after the edge above for
the identical reason: there is no rejection to classify off a channel the
engine itself cannot trust. The approach is left unarchived, no composite
PRD is written, no cure pass is opened, and a `resume` re-enters `review`
over the SAME approach. Before this task the three shapes above fell
through to the ordinary rejection ladder, archiving the approach and
writing a composite PRD off a findings channel the engine itself could not
read.
`contract.agent_fault` (already emitted by
`contract_enforcement.enforce_contract`) is the witness; no new event is
added. Three existing bounds stop a permanently-broken reviewer spinning:
`contract_retry_max` inside one pass, the pause itself (no automatic retry —
each further attempt needs an operator `resume`), and the iteration/wall-clock
budget behind every resumed attempt. Those bound the RESUMED cycle, not the
park: like every other engine pause, the parked time is credited straight back
onto both deadline fields (`_account_paused_wait`, task 016 #P), so
`job_timeout_s` does not expire a parked run — the hold is released by
`resume` or by tearing the run down. Two things are deliberately narrower
than "the ladder exhausted": an exhaustion over the review's FINDINGS SIDECAR,
or over a stray or wrong-typed field elsewhere in a payload whose own
`verdict` is legible, keeps the ordinary rejection path (a judge that said
"rejected" for a stated reason HAS rendered a verdict, whatever else its
payload got wrong); and `_verify_contract_ladder`/`_verdict_gate_verdict`
still turn their own exhaustion into a substantive negative. One tolerance is
added alongside, `transitions.normalize_negative_verdict`: a `verdict` of
`fail` or `failed` is rewritten in place to `rejected` before the ladder is
entered at all, witnessed by `review.verdict_synonym_normalised`,
one-directionally — no synonym maps to `verified`, so a wrong guess can only
land on the negative word, and from there the payload is treated exactly like
a literal `rejected` — `_review_verdict`'s own correction of an all-residual
refusal (`review.verdict_recomputed`) included, so a hat that writes `fail`
while filing only residual findings can still reach `succeeded`, on its own
findings, exactly as it would have with the compliant word.

**The same discrimination covers every other hat-driving path (v0.10 task
004, #72).** `_iteration_cut_short_by_engine()` is the predicate
`_review_interrupted()` now delegates to, factored out so the identical
engine-side-cutoff test applies wherever a hat attempt or a planning
attempt can be cut short: `_step_generic_hat()`'s per-state retry (any
hat wired through the generic table executor, not only `review`) self-loops
in place while budget/wall clock remain and, once they do not, ends via
the ordinary tail with no `_abort_reason` set — an engine-side cutoff
never reads as a contract violation over a payload nobody produced.
`_one_planning_attempt()` gets the same carve-out `instant_failure_streak`
already had: an interrupted planning attempt is retried in place rather
than handed to `plan_ingestion.enforce_plan_ingestion` as an ordinary
attempt, which would otherwise burn one of that method's finite
contract retries on something the model never got a fair shot at. Two
paths already had the right shape and needed no fix: `_step_worker`'s
stagnation counter already excludes `result.interrupted` explicitly, and
the reflect path (`_reflect_failure`/`_record_reflect_outcome`) cannot
rewrite the run's own `state`/`verdict` regardless of whether the
reflection attempt itself was cut short.

**A run that has earned `COMPLETE` does not lose it to the clock (v0.10
requirement B, #72).** `review_unjudged_at_budget_end` (just above) stops
the approach being burned; this is about the terminal record that follows.
`LoopSupervisor._deadline_beat_verdict_patch()` adds a `deadlineBeatVerdict`
status.json field -- `{"deadline": ..., "completeHonouredAt": ...}` -- to
exactly that write, IFF every task in the record is done
(`_outstanding_for_completion` empty, the same check `_step_worker`'s
`honoured_complete` uses) AND a `signal: "COMPLETE"` event was actually
recorded in `events.jsonl`. `state`/`verdict` stay `failed`/`unverified` --
the operator's ruling is that `verdict: verified` stays something only the
review hat can grant -- the marker exists so a human or a script can tell
"the clock beat the verdict" from "the work failed" without parsing the
free-text `reason`, the same precedent `_unconsumed_steering_patch` already
set for stranded steering. Computed from exactly one call site
(`_review_unjudged_at_budget_end`, stashed on
`self._deadline_beat_verdict_extra`), never unconditionally at the shared
tail exit (`_finish_tail_unverified()`) the way `_unconsumed_steering_patch`
is: a genuinely REJECTED last approach leaves the identical trace on disk
(completed tasks, a past COMPLETE signal, since `_archive_approach` copies
rather than resets) and must not be reported as an un-rendered verdict.

### 4.3 Sentinels and completion

Every worker/verify/review claim is a structured JSON contract
(task 116, task 118, task 119). The whole-plan completion claim was the one
exception -- a bare sentinel scanned from the final assistant text of the
iteration rather than from tool output, `<promise>COMPLETE</promise>` --
until v0.14 requirement O (#100, task 015) retired it: the literal marker,
and the paragraph in `worker.md` that named the signal in prose ("the
completion marker", so an agent explaining why it withheld it never had to
reproduce the literal), are both gone from the prompt. What replaced it is
below, folded into the SAME last-line JSON contract the per-task claim
already used.

**The worker's own per-task contract (task 116, #60 pillar 3, fact 9).**
`_step_worker` runs `engine.scheduler.select_next_task()` itself and writes
the pre-spawn `in-progress` claim before the subprocess even starts (§4.1) --
the agent no longer picks a task or edits `tasks.json` at all. Its last line
is parsed opportunistically as JSON and, if it carries a `verdict` key,
validated against `transitions.HAT_SCHEMAS["worker"]`: `{"verdict":
"complete", "task_id": ...}` for the task just assigned (a mismatched
`task_id` is a contract violation, never silently redirected -- a stray or
fabricated id must not claim a move the harness never assigned) moves it to
`awaiting-validation` under vigilant mode or `completed` otherwise, through
the SAME `_apply_reported_task_status` every other hat's reported move goes
through; `{"verdict": "unsatisfiable", "task_id": ..., "reason": ...}`
(requirement H, #63) marks it `skipped` with that reason recorded, routing to
a targeted replan of that one task while the approach continues; `{"verdict":
"no_verdict"}` (still working, or nothing assigned) applies nothing and the
worker loop simply continues. A last line that is not valid JSON, or valid
JSON with no `verdict` key, leaves this whole mechanism untouched.

**The worker's own whole-plan contract (v0.14 requirement O, #100, task 015).**
The SAME last line, when its `verdict` is `"plan_complete"`, is the
whole-plan sibling of the per-task claim above -- a name distinct from the
per-task `"complete"` so it can never read as a per-task claim. It is valid
only when `assigned_task_id` is `None`: the harness had nothing to assign
this iteration (`scheduler.select_next_task()` found no pickable task) --
a worker handed real work this iteration has no business claiming the
whole plan is done in the same breath; that claim belongs to `"complete"`
alone. `_apply_worker_plan_complete_report()` then cross-checks it against
`_outstanding_for_completion(tasks_doc)`, the IDENTICAL helper the retired
marker's own `honoured_complete` cross-check used (below): any task not
`completed`/`skipped`/`validated` refuses the claim, naming the outstanding
tasks. The structural refusal (a task WAS assigned this iteration) still
reaches `enforce_contract`'s re-ask ladder like every other malformed claim,
so a badly-formed attempt is recoverable rather than reading as silent
no-progress; the retired marker's equivalent miss only ever logged a warning
and left the loop working.

**Task 003 (#166): one semantic refusal shape skips the ladder entirely.**
When `assigned_task_id` IS `None` (the structural check already passed) and
the reason `_outstanding_for_completion` is non-empty is `LoopSupervisor.
_unresolved_failures(tasks_doc)` itself non-empty -- nothing else in the plan
is actionable and at least one task is `failed` -- a `plan_complete` claim
refused for exactly that reason is checked and routed to `review` with
`blocked_with_failures` (§5.3's own guard 9 mechanism, reused rather than
duplicated) BEFORE `enforce_contract` is ever invoked: worker.md's two
honest resolutions (carve the residual into a new task, or relabel
`skipped`) are both already proven unavailable by the claim itself, so
re-asking the SAME worker to fix it is not a legitimate retry, only a
wasted one. Any other semantic refusal shape (an outstanding task that is
still `pending`/`in-progress`/`validation-failed` -- `_unresolved_failures`
returns `[]` the instant anything is actionable, and such a task is always
this iteration's `assigned_task_id` under `scheduler.select_next_task()`'s
own priority order, making the claim structurally invalid instead) reaches
the ordinary ladder exactly as before this task.

An accepted claim still emits `signal: "COMPLETE"` (surfaces and
`IterationResult.saw_complete` below both still read it) and ends the
worker loop into `review`, exactly as the retired marker did.

`IterationResult.saw_complete` (`engine/runner.py`, still matched against the
literal `<promise>COMPLETE</promise>` text) and `saw_verified` are both now
purely operator-facing diagnostics -- "mentioned but did not end with it"
(task 046, #55) for whichever of the two a reply's text happens to name,
regardless of phase. Neither drives a decision any more: the whole-plan
verdict above reads the JSON contract exclusively, exactly as review's own
decision already did before this task.

`verify`'s own claim is `{"task_id": ..., "verdict": "verified"|"rejected",
"reason": ...}` (`transitions.HAT_SCHEMAS["verify"]`), validated against the task
under verification by `_verify_task()` itself -- the harness applies the
resulting status, never the agent's own `tasks.json` write. A `verified`
verdict may carry a fourth, optional field, `notes` (v0.19 requirement A.2,
#154, task 005) -- an array the verifier uses to "verify with a note" a
clause met only in form rather than reject over it (`prompts/task-verify.md`);
`_verify_task` persists the list verbatim as the task's own `verifyNotes`
(§5.3) in the same write that sets `validated`, and appends it to the run's
debt ledger (below) in the same pass. Absent on an ordinary clean verdict. `review`'s own
claim is `{"verdict": "verified"|"rejected", "reason": ..., "findings_path":
...}` (`transitions.HAT_SCHEMAS["review"]`), read by `_review_verdict()` --
the harness decides `succeeded`/`verified` or archives the approach from this
payload alone, never from the retired `<promise>VERIFIED</promise>` sentinel.
There is a third case (GH #209 defect 2): a payload whose `verdict` claim the
contract ladder could never make readable archives nothing at all and holds
for an operator instead, and one tolerance sits on top of the enum --
`transitions.normalize_negative_verdict` rewrites a `verdict` of `fail`/
`failed` to `rejected` in place, one-directionally, before the ladder is
entered.

**A third, mid-run legal reply that never ends a run at all (task 014,
requirement F.1, #144).** Before the engine hands any task back to `verify`,
it runs at most one bounded, off-budget adjudication iteration on the
review hat's own model whenever a workspace impossibility petition is open
and unconfirmed (`_maybe_run_midrun_adjudication_episode`) -- its ONLY job is
to settle every open petition's `petition_adjudications` entry
(CONFIRMED/REFUTED, the same vocabulary an ordinary review answers with),
and its `verdict` field carries no terminal effect: no `_advance`, no
`_finish`, no approach scored. Because the ordinary `review` schema requires
`verdict` to be `"verified"`/`"rejected"` only, this call validates against
a separate, wider schema instead (`transitions.MIDRUN_REVIEW_SCHEMA`) that
adds the third legal value `"adjudicated"` -- naming exactly what a reply
with nothing terminal to claim is answering -- and also accepts `verdict`
being omitted entirely. `"adjudicated"` is illegal everywhere else: an
ordinary run-ending review that answers it is a contract
violation like any other malformed `verdict`.

`COMPLETE` is matched in `engine/runner.py` (`IterationResult.saw_complete`) as
before, but see the diagnostic-only demotion just above: the retired `VERIFIED`
sentinel is still matched too (`saw_verified`), for the same "mentioned but
did not end with it" operator diagnostic (task 046, #55) that runs for every
iteration regardless of phase -- both hats' own decisions read their JSON
contract exclusively. Each honoured claim emits a `signal` event. A `VERIFIED`
signal is refused while
operator steering is still unconsumed (§4.6). The reflect prompt no longer ends on a bare sentinel; its own `reflect`
contract claim (`{"report_written": ..., "suggestions_written": ...,
"notes": ...}`, `transitions.HAT_SCHEMAS["reflect"]`) is read by
`_reflect_verdict()` and checked ON TOP OF, never instead of,
`artifacts/reflection/report.md`'s own file-presence check -- both a
missing file and an absent/malformed/`report_written: false` contract are
recorded as a failed reflection (`_reflect_failure()`, task 121).

**A promise counts only as the last line, and only when the plan agrees.**
The rule the retired `<promise>COMPLETE</promise>` marker followed, and
which `"plan_complete"` above preserves in JSON form. Two rules, one
string-level and one semantic, and the second is the load-bearing one --
load-bearing enough that it survived the marker's own retirement unchanged,
just re-expressed as a contract cross-check instead of a warning-only miss.

1. **Last non-blank line, stripped, equal to the marker** --
   `runner.last_nonblank_line()`. A marker anywhere else in the message is
   **absent**, and the engine emits a `log` warning saying it was mentioned but
   not ended with, so failing closed never looks like an unexplained
   non-conclusion. This is deliberately not an "on its own line anywhere"
   (`MULTILINE ^…$`) match: an agent quoting the protocol in a fenced block, or
   echoing a `notes.md` section documenting it, legitimately puts the marker on a
   line of its own. `review.md`
   has no sentinel of its own to defend this way either: a malformed JSON
   payload simply is not the last line's valid JSON, so the same defence
   falls out of requiring the LAST line to parse as the contract, with no
   separate naming convention needed -- exactly the shape `"plan_complete"`
   now uses too, since it is JSON rather than a bare string.
2. **A COMPLETE is cross-checked against `tasks.json` before it is honoured.**
   `_outstanding_for_completion()` lists every task that is not `completed` or
   `skipped` (`state.TASK_COMPLETION_STATUSES`, `task_blocks_completion()`) in
   the plan the worker loop has just re-read; if any exist the claim is refused,
   a `log` warning names the outstanding count and the first ids, and (for the
   retired marker) the loop kept working, or (for `"plan_complete"` today) the
   contract's re-ask ladder engages. This is strictly stronger than any string
   assertion because it does not depend on how the marker was formatted, and it
   is checked against the plan on disk rather than the agent's narration (§4.1's
   rule). An empty or unreadable plan is not refused — review is the gate there
   (§5.3).

Both rules exist because of a live near-miss, back when the claim was still the
bare marker: a worker that obeyed the contract
perfectly ended its reply with "36 tasks are still pending, so I will not emit
`<promise>COMPLETE</promise>`", the substring match read that explanation as the
signal, and the run left the worker loop with 36 tasks pending. Only the reviewer
rejecting the approach contained it; `saw_verified`, which makes a run terminal,
had the same defect with nothing behind it. The stagnation counter (§5.3)
therefore consults the **honoured** completion too: resetting it on the raw
marker would let an agent that emits COMPLETE every iteration while changing
nothing hold `stagnant` at zero forever, trading a false conclusion for an
endless loop.

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

**The gate warns when it goes quiet (task 026, requirement N carried from the
closed #29).** A verified-task record that wrongly claims a completed task is
verified makes `pending_verify` compute empty, and an engine that just keeps
running worker iterations looks — from `ralphctl status` and the event stream —
exactly like a run with nothing left to verify. #29's colliding keys were silent
for 17 consecutive iterations of a real run. So the first time a worker
iteration finds the gate empty while some currently-`completed` task was never
verified *by this process*, the supervisor emits one `log` event at level
`warning` (`_warn_if_verify_gate_empty()`) naming those task ids and the
approach. Exactly once per engine process, not per iteration: the condition
holds for every remaining worker iteration, so repeating it would bury the run's
real events. A legitimate resume has the same shape (an earlier process did the
verifying), so the message states both explanations and which one this process's
own history supports — it is a tripwire, not a verdict.

Task-failure bookkeeping, all in `_verify_task()`:

| condition | effect |
|---|---|
| `verdict: "verified"` reported (via the `verify` contract, task 118) | task status set to `validated`; `signal` event `taskVerified` |
| verdict miss (a verifier ran to completion with no valid `verify` contract payload for this task, or an explicit `rejected` verdict, and no error) | `validationAttempts` incremented; `status` forced to `validation-failed`; `validationNotes` taken from the payload's own `reason` when one was given, or a generic default naming the contract problem when the reply carried no valid payload at all |
| `validationAttempts` reaches `VALIDATION_ATTEMPT_LIMIT` (3) | `status` forced to `failed` **and** `failureKind` written as `validation-exhausted` (§5.3) — unless the contract declares a petition protocol (`petitions.contract_declares_petition_protocol`) and `petitions.file_exhaustion_petition` accepts the last-attempt handoff as a petition, in which case `status` becomes `petitioned` and the mid-run adjudication episode runs |
| task reaches verify already at `validationAttempts >= VALIDATION_ATTEMPT_LIMIT` | no verifier is dispatched; `status` forced to `failed` **and** `failureKind` written as `validation-exhausted`, and a `task.verify_skipped_exhausted` event is emitted; this path never files an exhaustion petition, even when the contract declares a petition protocol — the task never rests in `awaiting-validation`, so `_unresolved_failures` routes it to review rather than to the stagnation guard |
| the verify iteration reached **no verdict at all** — an in-band agent/provider error, the full `iteration_timeout_s`, the startup-window watchdog, or a signal (`error_message` / `timed_out` / `no_traffic_timeout` / `interrupted`) | retried up to `MAX_VERIFY_ERROR_RETRIES` (3) **without** consuming a validation attempt; if no attempt ever reaches a verdict, `status`, `validationAttempts` and `validationNotes` are left byte-for-byte as they were |
| no verify iteration ran at all (budget gone) | same: nothing observed, nothing recorded against the task |

The last two rows are the load-bearing ones (task 012, #45): absence of a
verdict is not a negative verdict, so neither an infrastructure fault nor one of
the engine's own timeouts/interrupts may be recorded as a failed validation, and
the note written on those paths says the verifier never reached a verdict rather
than claiming it reported nothing usable. The verify iteration's `meta.json` keeps the
distinction on disk: `verifyOutcome: "error"` means no verdict was reached,
`"fail"` means a verifier judged the criteria unmet (`verifiedTask`,
`verifyOutcome`).

Independently of vigilant mode, every worker pass fingerprints success criteria:
`_ensure_criteria_baseline()` stores a `criteriaFingerprint` (sha256 of
`successCriteria`) per task and `_check_criteria_edits()` compares it
afterwards. A rewrite is flagged unconditionally -- including at
`validationAttempts: 0` -- which sets the sticky
`criteriaEditedAfterValidationFailure` marker (never cleared) and restores
`successCriteria` from the task's recorded `criteriaOfRecord`; the restore
is witnessed with its own `task.criteria_edit_refused` event, and the
engine-authored blocking finding (`findings.flagged_criteria_findings`)
recurs on every later `review` pass until the task is resolved, alongside
the standing section naming flagged tasks and demanding an explicit
per-task pass/fail conclusion against the *current* criteria text. There
are exactly two legal ways to move a task's criteria forward, each of
which advances `criteriaFingerprint`/`criteriaOfRecord` atomically with the
replacement text so it is never mistaken for one of these unauthorized
rewrites: a judge-authored amendment on a CONFIRMED petition
(`_apply_criteria_amendments`), and an operator-issued steer `task-update`
naming `successCriteria` (`steer_task_update`, #132). Otherwise a worker
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
| reflect's own timeout floor | `REFLECT_TAIL_TIMEOUT_S` | `600s` | requirement C (#72): a floor under reflect's ONE attempt, independent of how far past `job_timeout_s` the run already is -- see below |
| instant-failure fail-fast | `INSTANT_FAILURE_MAX_DURATION_S`, `MAX_CONSECUTIVE_INSTANT_FAILURES` | `5.0s`, `3` | a run of identical sub-5s zero-work failures aborts the job |
| stagnation guard | — | `3` | consecutive no-change worker iterations that abandon an approach |

`budget_left()` is the single gate, evaluated at every turn of the loop:

```python
charged = (self.iterations_used - self._infra_refunded
          - self._steer_refunded - self._adjudication_refunded)
return (charged < self.cfg.iterations
        and time.monotonic() < self.deadline
        and self._abort_reason is None)
```

Raw attempts always increase — every attempt gets its own iteration number and
directory — but three kinds are refunded and never charged: an attempt retried
after a fault `classify_fault()` scored as `infra`, an off-budget midrun
adjudication episode (`_maybe_run_midrun_adjudication_episode()`, task 014,
requirement F.1, #144 — settling an open workspace-impossibility petition; v0.24
task 015, requirement E.1, #169 renamed this bucket's `"grace"` predecessor,
the retired off-budget grace review, to `"adjudication"` and kept its one
distinguishing behaviour, see below), and the `steer` hat's own iteration (task
014, requirement J, #98 — "an operator message must not cost the run a work
iteration"). `status.json`'s `iterationsUsed` publishes raw attempts minus
infra AND steer refunds (an adjudication refund is deliberately not subtracted
from it — it stays visible as a used iteration, see below), so a refund an
operator was shown is never quietly re-charged. All three counters are
persisted as `iterationsRefunded` and seeded back on the next engine process
(§8.4), so none is lost across a resume, either.

The per-iteration timeout is clamped by what is left of the deadline:

```python
timeout = min(self.cfg.iteration_timeout_s,
              max(self.REFLECT_TAIL_TIMEOUT_S if phase == "reflect" else 60,
                  int(self.deadline - time.monotonic())))
startup_timeout_s = (min(self.cfg.infra_startup_timeout_s, timeout)
                     if phase in self.INFRA_RETRY_PHASES else None)
```

**`reflect`'s own attempt does not inherit the plain 60s floor once the job's
deadline has already passed** (requirement C, #72). `reflect` (PRD req 24)
runs strictly after `_run_job_core()` has already settled the job's fate, so
by the time it gets its one attempt `self.deadline` is ordinarily already
behind `time.monotonic()` -- and the plain formula floors every phase at
60s regardless of how far past the deadline the run is. Sixty seconds is not
enough for a real post-mortem write: `selfdev-v09-fsm-loop` lost its
reflection entirely this way (iterations 183/184/185, a 2m01s window,
`artifacts/reflection/FAILED.md` and nothing else). `REFLECT_TAIL_TIMEOUT_S`
(600s) is the floor for reflect specifically instead of 60 -- still capped by
`cfg.iteration_timeout_s`, and `_run_reflection()` runs this exactly once per
process, so the worst-case extra wall time this adds to a run, ever, is
`min(cfg.iteration_timeout_s, 600)` seconds. `job_timeout_s`'s own meaning
("wall-clock limit for the whole job") and `self.deadline`/`deadlineAt` are
unchanged by this -- only the one floor reflect's attempt is measured
against moved.

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

**When the budget runs out, the run holds instead of ending unreviewed (v0.24
requirements A–D, #169).** Task 015 (requirement E.1) retired the off-budget
**grace review** this used to read as "exactly one extra review, granted at
most once per approach, when every task was already `completed`" — requirements
A and D instead move the ordinary review path itself so it finishes before the
deadline/iteration budget cuts it short, so there is no off-budget shot left to
attempt. The plain out-of-budget shape (`_finish_tail_unverified`'s
`self._budget_exhausted_tail`, set by every `if not self.budget_left(): return
("tail",)` guard that is not already a review-specific hold) now calls
`_finish_timed_out()` (task 010, requirement B.3) instead of ending terminal
`failed`/`unverified`: the run enters the non-terminal `timed_out` state
(§5.2) with `verdict: null` and a `reason` naming the hold, and is
resume-eligible like any other hold — an operator resume gets it back into the
ordinary dispatch loop with a fresh window, never a second implementation of
the old grace pass. `max_approaches` exhaustion and the genuinely structural
tails (a repeated blocker signature, a verdict-gate rejection on the merits,
the contract-violation-once-budget-is-also-gone branch) never set this flag —
they still fail, unchanged (requirement C.2).

**A worker's optional `progress` claim is corroborated against the workspace tree,
not merely logged (PRD requirement A.2, #156, task 012).** Alongside
`verdict: "no_verdict"` a worker may report `progress: {"summary": ..., "evidence":
[{"kind": "commit"|"file"|"external", "ref": ...}, ...]}` (task 011, requirement
A.1); the engine takes a content-hash fingerprint of the workspace tree
(`LoopSupervisor._workspace_fingerprint()`) immediately before the worker's
subprocess starts and again immediately after it returns -- the same before/after
bracketing `_workspace_head_sha()` already uses for the git-HEAD stagnation
witness (§5.3), but over file CONTENT rather than a commit sha, so it works
unchanged in a workspace with no `.git` at all. The fingerprint lists tracked and
untracked-but-not-ignored files (`git ls-files -z --cached --others
--exclude-standard`, falling back to a plain recursive walk when there is no git
repository to ask) and hashes each file's bytes (`sha256`); `_progress_claim_delta_size()`
counts how many paths differ between the two snapshots -- added, removed or
content-changed each count once.

`commit`/`file` evidence is corroborated **iff** that count is nonzero -- the tree
actually moved. `external` evidence (a claim about something outside the tree: a
dashboard watched, a manual reproduction) is honoured at face value **once
consecutively**: `LoopSupervisor._progress_external_streak` counts consecutive
iterations, on the same task, whose claim was external-only with zero tree delta,
and only the first such claim in a run of them is corroborated -- the second
consecutive one is not. The streak resets the moment the assigned task changes,
a real tree delta lands, or the claim carries `commit`/`file` evidence instead.
Every accepted claim records a `progress_claim` event (§5.4) carrying `taskId`,
`corroborated` and `deltaSize` -- `_apply_worker_progress_report()`'s own record,
consumed by nothing downstream yet: the per-task progress budget that reads
`corroborated` for something beyond the record, and the stagnation-counter
exemption it grants, are a later task's own scope.

### 4.6 Steering, pause, interrupt, abort

Steering is a file, not a message queue. Each message lands in
`steering/NNN-<name>.md` with an engine-assigned sequence prefix;
`steering/.consumed.json` lists the names already actioned, and everything else
is pending.

- Pending steering is injected into the next `planning` or `worker` prompt under
  `## Operator steering (MUST take priority)` and marked consumed once **that
  iteration has finished cleanly** (`steering.consumed` names the file and the
  iteration). Delivery is therefore at-least-once and application at-most-once:
  an iteration that fails, is interrupted, times out or dies with the engine
  leaves its notes pending, and the next actionable iteration is handed them
  again (issue #34 — marking them at iteration *start* recorded a note as
  applied that nothing ever acted on). The ordering is the guarantee: build the
  prompt → run the agent → record the outcome → append to `.consumed.json`; the
  marker write is idempotent, so no name can be recorded or evented twice.
  `iterations/NNNN/meta.json` records both halves — `steeringDelivered` (what
  was in the prompt) and `steeringConsumed` (what the outcome earned).
- `review` and `verify` are pure verification roles whose prompts carry no
  steering instructions, so they must not consume it
  (`STEERING_ACTIONABLE_PHASES`): they get a read-only note that steering is
  waiting for the next actionable phase. Marking it consumed there would record
  it as actioned and silently discard it.
- The **steering ledger** (task 034) is the run-scope, engine-owned join of
  those two halves: `state.steering_ledger` pairs every `steering/NNN-*.md`
  file with the engine's own `steering/.ledger.json` bookkeeping (consumption
  timestamp and iteration, the steer hat's `settled` claim, its settlement
  `notes` and the resulting application status — `applied`,
  `partially-applied` or `pending`, the `STEER_APPLICATION_STATUSES`
  vocabulary). `LoopSupervisor._steering_ledger_context` renders it into
  EVERY hat's prompt from `build_prompt`, the same engine-side surfacing
  `_operator_amendment_review_context` and `_petitions_review_context` use, so
  a verifier or reviewer can see what was steered, what was applied and what
  is still pending. A steering file nobody has settled yet appears as
  `pending` in the very next iteration's prompt, with no record written. The
  semantics are stated in the rendered text and in `prompts/task-verify.md`
  and `prompts/review.md`: the ledger INFORMS (never fail work for conforming
  to an applied steer; weigh a pending steer when judging staleness), but a
  steer is NOT a waiver — only operator amendments in `/config/amendments`
  waive an obligation (§13.4).
- A `VERIFIED` verdict is refused while the exit gate
  (`LoopSupervisor._terminal_success_blockers()`, the one predicate
  `_step_review` asks -- v0.24 task 015/E.1 retired the off-budget grace
  review's own second call site) names anything: an **unsettled**
  steering file, a **non-terminal steering-origin task** (task 013,
  requirement I, #98 — `settled` is not `done`), or a **non-terminal
  `review-cure`-origin task** (v0.23 requirement D.1, #220 — engine-inserted
  cure/remedy work, including the REFUTED petition remedy a run-ending review
  inserts under requirement D.2). The verdict is discarded and
  the run defers ONCE back to `worker` through the declared edge that names
  the blocker being acted on — `steering_pending_at_verified` while anything
  on the steering side is outstanding, `review_cure_pending_at_verified` when
  the non-terminal `review-cure`-origin task is all that is left
  (`LoopSupervisor._pending_at_verified_guard`, the one choice site);
  the dispatch loop then routes to the `steer` hat (unsettled message)
  or lets the worker's own scheduler finish the outstanding task, and review
  is asked again the ordinary way. Nothing reads pending steering after a
  terminal state, so going terminal there would strand it forever.
- Every terminal write records `unconsumedSteering`, so stranded steering is
  visible from `status.json` alone.

**The operator RULING channel is a distinct one (task 013, v0.23
requirement A.2, #224), never an extension of steering or of `amend`
(§13.4).** `ralphctl rule <run> --file ruling.md --name <slug>`, `POST
/rule` and the hub run page's textarea all land through the ONE path
(`RunDir.add_ruling` → `state.write_ruling`) into `rulings/NNN-<name>.md`
under the RUN root -- deliberately not `config_root`/`amendments/`
(§13.4): `POST /rule` is handled in-container, so the write has to be
reachable from there, unlike an amendment's host-only, `:ro`-mounted
write. `ralphctl rule` refuses a bare positional message and names
`--file` rather than silently dropping the text (#224's own named
incident -- a positional message slot has previously swallowed a ruling
this way while `--file` was ignored). Each ruling is keyed to the
signature `LoopSupervisor._review_rejection_signature` computed for the
rejection it answers (persisted as `status.json`'s `rejectionSignatures`,
§5.2) -- resolved from the run's OWN current `approach` at write time,
never from caller input -- rather than to a task id, so a replan that
restarts every task id at 001 (as an amendment's task-id key does) leaves
the ruling record fully intact: `state.ruling_entries` reads every
recorded ruling for the run unconditionally, with no
approach/plan-membership scoping to compute. `LoopSupervisor.
_operator_ruling_review_context` renders all of them as an authoritative
clause of the contract, and reaches BOTH a subsequently-built `review`
prompt (via `_review_extra_context`) and a subsequently-built `verify`
prompt -- verify needs it explicitly, since it reads `tasks.json` and
honours no amendment today, so a ruling that only reached review would
come back as a verify failure. No hub-level auth and no new secret: `POST
/rule` is a POST, and the control-token gate is method-based middleware
(§13.4), so it is authenticated the moment it exists, and the hub's proxy
already forwards `X-Ralphd-Control-Token` for every mutating method.

**Every live-run pause and escalation text names the two live-run levers
(task 003).** Each `_*_escalation_text` builder in `engine/loop.py`, the
notification digest (`_operator_escalation_digest_text`), the petition-hold
notice (`_petition_held_escalation_text`) and the `pause()` log message name
`ralphctl unpause <run>` to proceed -- the CLI command that POSTs `/resume`
to the LIVE engine and clears `pauseReason` -- and, wherever the pause waits
on an operator ruling (every rejection, stagnation and incurable pause,
`operator_input_needed`, the petition hold and the `pause()` message),
`ralphctl rule <run> --file <ruling.md> --name <slug>` to answer. None of
them names `ralphctl resume`, which starts a fresh container over an
existing run dir and is the wrong tool for a run that is still alive. The
dependency-cycle, unresolvable-edge and broken-review-channel pauses name
their own remedy (a `tasks.json` edit, a working reviewer) plus `ralphctl
unpause <run>`. `tests/test_escalation_commands.py` covers every one.
The full `operator_escalation.escalation` text follows the same rule (cure
task cure-01-01-9): for `rejected_last_approach`, `rejected_incurable` and
`stagnation` (both variants) it says only `ralphctl unpause <run>` releases
the pause, that filing a ruling records it but leaves the run paused, and
gives the `ralphctl rule ... then ralphctl unpause ...` sequence beside the
unpause-alone alternative, and for `rejected_repeated_signature` it says a
ruling releases the pause on its own, or `ralphctl unpause <run>` resumes
with none recorded to accept the default.

**Every notification this engine posts is bounded by construction (task 001,
PRD requirement A, #269; widened to two more reasons by cure task
cure-01-02, PRD requirement A.1/A.2/A.6, #269).** `MAX_NOTIFY_CHARS`
(`engine/config.py`, an int below the notifier's own 16,000-character cap)
is the one ceiling every `self.notifier(...)` call site in `engine/loop.py`
passes through -- `LoopSupervisor._notify()`, which truncates with a
trailing marker on the rare call whose text still overruns despite that.
`operator_input_needed` and `rejected_repeated_signature` are the two
escalation reasons whose body scales with an unbounded finding count ×
evidence length -- the #269 incident itself: a real escalation body
reached 45,876 characters and was refused `422` by the notifier's own cap,
silently, and the operator never saw it. `rejected_last_approach` (D.1)
and `rejected_incurable` (B.2) carry the exact same shape of risk -- a
curable finding with its cure pass spent, or an incurable one with a real
environmental/temporal fact behind it, either way a full `evidence` clause
per blocking finding -- so `LoopSupervisor._operator_escalation_digest_
text` builds the SAME DIGEST for all four reasons instead of posting the
full text: run id, `pauseReason`, `approach n of m`, blocking and residual
counts, the A.7 preview numbers (tasks reopened, tasks inserted, spend
this approach -- for `operator_input_needed`/`rejected_repeated_signature`
read off the same effective preview the `operator_escalation` event's own
A.7 fields carry, so the two always agree; all-zero/`unavailable` for
`rejected_last_approach`/`rejected_incurable`, since neither previews a
cure pass that will run), one
line per blocking finding naming only its digest-local id and `clause`
(never its evidence), the hub URL (only when `hub_base_url` is
configured), and the exact commands that answer the pause -- ALWAYS both
`ralphctl rule <run> --file <ruling.md> --name <slug>` and `ralphctl
unpause <run>`, since every reason this digest renders for waits on an
operator ruling. Cure task cure-01-01-9 (PRD A.1/A.6, D.1; DoD 1/4): the
two pairs differ in what filing that ruling actually DOES before an
unpause, and the digest's wording follows -- `resume_if_ruling_answers_
pause` releases `operator_input_needed`/`rejected_repeated_signature` the
instant the ruling is filed, so their own line reads `ralphctl rule ...,
or ralphctl unpause ... to accept the default`, two genuinely
interchangeable single-step alternatives; that check never covers
`rejected_last_approach`/`rejected_incurable` (its own reason allow-list
excludes both), so `rule` alone there leaves the run paused and their own
line instead reads `ralphctl rule ... then ralphctl unpause ... to record
a ruling before resuming, or ralphctl unpause ... alone to resume with
none recorded` -- an explicit two-step sequence beside the working
unpause-alone alternative, never an `or` between two things that each
resolve it on their own. When even the per-finding lines would overflow
the cap, the digest drops to a bare count -- "*k* more findings in the
hub" -- but never drops the counts themselves. The full, un-digested text
-- every blocking finding's full `clause`, `evidence` and, whenever the
finding carries one, its own `cured_when` verbatim (a `cured_when:` line,
for all four reasons; an incurable finding without one simply has no such
line) -- is unaffected and still lands on the `operator_escalation`
event's own `escalation` field (§5.4); only the POSTED notification is
capped and digested, and it carries neither a finding's `evidence` nor
its `cured_when`.

**Cure task cure-01-05-2 (PRD requirement A.1/D.1, DoD 1; widens cure-01-02's
own digest contract): every OTHER real operator-escalation pause posts a
bounded digest too, never the raw diagnostic text.** `LoopSupervisor.
_pause_escalation_digest_text` gives `review_contract_unrepaired`, `review_
findings_unusable`, `stagnation`, `unschedulable_dependency_cycle` and
`unresolvable_dependency_edge` the same header shape
`_operator_escalation_digest_text` gives the four finding-bearing reasons --
run id, `pauseReason`, `approach n of m`, spend this approach, the hub URL
(only when `hub_base_url` is configured) and the exact command(s) that
answer the pause -- but honestly, since none of these five ever carries a
review findings channel or a cure-pass preview at all: `blocking`/
`residual`/`tasks reopened`/`tasks inserted` read the literal string
`unavailable`, never an invented `0` a reader could mistake for "checked
and empty." Each reason's own short, already-actionable fault summary
follows the header -- outstanding task ids for `stagnation`, cycle/edge
ids and dependents for the two dependency faults, a bounded per-error
preview (never the raw validation-error text unbounded) for the two
review-contract faults -- bounded the same way the four-reason digest
bounds its own per-finding lines: once the remaining fault lines would
overflow `MAX_NOTIFY_CHARS`, the digest drops to a trailing count instead
("*k* more in the hub/events.jsonl"). The dependency-cycle/unresolvable-
edge and broken-review-channel digests name their own remedy (a
`tasks.json` edit, a working reviewer) plus `ralphctl unpause <run>`, never
a rule command. `stagnation` resumes into `worker`, never `review`
(`_step_paused`), and `resume_if_ruling_answers_pause` never releases it
either (cure task cure-01-01-9, PRD A.1/A.6, D.1; DoD 1/4) -- its own line
reads the same explicit two-step sequence `rejected_last_approach`/
`rejected_incurable` use above (`rule ... then unpause ...`, or `unpause`
alone), never the `or ... to accept the default` phrasing that would
falsely claim `rule` alone resolves it. The full diagnostic text --
every `contractErrors` entry, every outstanding task, every cycle/edge id --
is unaffected and still lands on the `operator_escalation` event itself
(§5.4); only the POSTED notification is capped and digested.

**The task-boundary commit-secret-scan hold uses the same digest (PRD
requirement A.1/D.1, DoD 1).** `LoopSupervisor._pause_for_commit_secret_hit`
(task 030, PRD requirement I.3, #210) posts the SAME
`_pause_escalation_digest_text` shape the five non-finding reasons above
get: run id, `pauseReason: operator_input_needed`, `approach n of m`, spend
this approach, the hub URL (only when `hub_base_url` is configured), and
`blocking`/`residual`/`tasks reopened`/`tasks inserted` reading the literal
`unavailable` (this pause carries no review findings channel or cure-pass
preview). The fault summary is the hit's own `sha`/`part`/`variable`, read
back off the `commit_secret_scan` event just emitted -- never the matched
value. Cure task cure-01-01-9 (PRD A.1/A.6, D.1; DoD 1/4): whether `POST
/rule` releases THIS hold depends on whether the current approach already
carries a recorded `rejectionSignatures` entry (`resume_if_ruling_
answers_pause`, which reads only the pause's `pauseReason` and that
approach's own signature, never whether the CURRENT pause is the one
that recorded it) -- this hold shares the `operator_input_needed` reason
STRING with review's own pause, and an EARLIER `operator_input_needed`/
`rejected_repeated_signature` pause on the SAME approach, already
answered by a ruling, leaves that signature behind for a commit-scan
hold raised LATER in that approach to inherit. With no such signature
(the common case -- a fresh approach, or one with no earlier reasoned
hold), a ruling filed against it is stored unscoped and the run stays
paused, so its answer command names only `ralphctl unpause <run>` (`POST
/resume`, which releases it) and states that a ruling does not release
it; with one, `ralphctl rule <run> --file <ruling.md> --name <slug>` and
`ralphctl unpause <run>` are interchangeable single-step alternatives,
the same pair `operator_input_needed`'s own review-side digest
advertises, and the answer command names both plus the reason why. It
emits no `operator_escalation` event (its record is the
`commit_secret_scan` event), so the resume-into-`review` routing
(`_paused_awaiting_operator_input_needed_ruling`) never applies to it and a
resume re-enters `worker`.

**A failed escalation delivery is retried with bounded backoff, and an
exhausted retry is recorded distinctly (task 004, PRD requirement A.4,
#269).** Every `self.pause()` + `self.notifier` escalation site in
`engine/loop.py` -- the nine sites that pause with a reason (`rejected_last_approach`,
`rejected_repeated_signature`, `operator_input_needed` (both the review
ladder's own and the task-boundary commit-secret-scan pause), `stagnation`,
`review_contract_unrepaired`, `review_findings_unusable`,
`unschedulable_dependency_cycle` and `unresolvable_dependency_edge`) and
`rejected_incurable`, whose `_pause_and_escalate_incurable_rejection` takes
the reason-less `self.pause()` -- calls
`LoopSupervisor._notify_escalation_delivery(text, reason)` instead of
`_notify()` directly. It reads back each attempt's own result through the
SAME `self._notify`/`self.notifier` seam (never a second HTTP client, and
never a second `notification.sent`/`notification.failed` split -- task
009/requirement B.1 is unchanged, and every attempt still gets exactly one
of those two names) and retries a failed attempt, after a bounded backoff
(`_ESCALATION_DELIVERY_BACKOFF_S`: 60s, then 120s -- 180s total, inside the
PRD's "a few minutes" and its own 300s ceiling), up to
`_ESCALATION_DELIVERY_MAX_ATTEMPTS` (3) attempts in total -- and that
number (3) is also the TOTAL count of real notifier HTTP POSTs an
escalation ever makes (task cure-01-01-3, PRD requirement A.4's own DoD 1),
never a second, independent three-attempt budget stacked underneath it:
each of the 3 outer attempts calls `_notify(text, max_attempts=1)`, which
disables `hat_tools.send_notification`'s own separate task 017/requirement
A.7 retry-once-immediately-no-backoff for this path only -- every other,
non-escalation `self._notify(...)` call site keeps that retry unchanged.
If the LAST attempt still failed, the engine emits one additional, distinct
event,
`notification.escalation_undelivered` (`reason` names the pause reason that
went undelivered) -- the signal that this escalation never reached the
operator at all, not merely that one attempt did not. Neither a failing nor
an always-failing notifier ever delays the pause itself: every site calls
`self.pause()` -- and every site but the commit-secret-scan pause, whose own
record is its `commit_secret_scan` event, emits its `operator_escalation`
event -- before delivery is ever attempted, so the in-memory hold is in
force first. For the nine
reasoned sites that also means `status.json` already reads `state: "paused"` with its `pauseReason` before the first delivery attempt returns.
`rejected_incurable` is the exception: its reason-less pause is the same
in-memory hold `POST /pause` takes and is never persisted (it is not one of
`pauseReason`'s values, §5.2), so status.json keeps its prior `state` and a
null `pauseReason` throughout; its delivery is still retried and still
recorded undelivered the same way. `tests/test_escalation_delivery.py`
covers the retry, the bounded attempts and backoff, the undelivered event,
that the pause takes effect regardless, and the `rejected_incurable`
exception.

**An undelivered escalation is visible on three surfaces without reading
`events.jsonl` (task 005, PRD requirement A.5, #269).**
`engine.state.escalation_pause_markers(run_root, status)` derives, from one
read of `events.jsonl` plus `status`, the three facts every surface carries:
`pauseMarker`, `escalationUndelivered` and `escalationUndeliveredMarker`.
The run counts as still paused on its latest escalation in two shapes: a
persisted engine pause (recorded `state` and `pauseReason`), or the `rejected_incurable`
in-memory hold above -- recorded `state` still `running`/`starting` (or not
yet recorded), the MOST RECENT `operator_escalation` carries `reason:
"rejected_incurable"`, and neither an `iteration.start` nor the `resumed`
`log` event `resume()` emits (`state.RESUMED_LOG_MESSAGE`) has landed since.
`escalationUndelivered` is `True` for exactly as long as the run is paused
in one of those shapes AND that most recent `operator_escalation` was
followed, once `_notify_escalation_delivery`'s bounded retries above were
exhausted, by `notification.escalation_undelivered`, with no LATER
`operator_escalation` since (a fresh escalation cycle resets the flag until
its own retries are exhausted too). `pauseMarker` is task 017's own `⏸
parked for input` for `pauseReason: "operator_input_needed"`, `⏸ held on
rejected_incurable escalation` (`state.ESCALATION_HOLD_MARKER`) for the
in-memory hold -- which records no `pauseReason` for task 017's marker to
read -- and `""` otherwise. Answering the pause removes both markers at
once: `resume()` (`POST /resume`, `ralphctl unpause`, the hub's `POST
/api/runs/<id>/unpause` proxy (task 006) -- the SAME `/resume` route, no
second unpause mechanism, a ruling
`resume_if_ruling_answers_pause` recognises, the abort wind-down) clears a
persisted hold on status.json synchronously and emits its `resumed` event,
which ends the in-memory hold's derivation too; a restarted episode that
re-enters review over the held approach ends it with its `iteration.start`.
Three surfaces render `❗ escalation undelivered`
(`state.ESCALATION_UNDELIVERED_MARKER`) right after the pause marker:
`ralphctl status`, on its `state:` line, with `--json` carrying the same
three fields the hub payloads carry, live or on-disk fallback alike; the
hub's runs-list row (`ui_server.run_list`); and the hub's run-detail page
(`ui_server.run_detail`'s own status doc, via `_with_pause_markers`).
`ralphctl runs` carries the same `pauseMarker`. `app.js`'s shared
`stateMarkerSpans` renders whichever of the two marker fields a payload
carries, on both the runs-list row (`runRowStateChildren`) and the
run-detail page's state cell (`stateCell`). A runs-list row whose
container is gone (`containerGone`) still renders both markers after its
`⚠ container gone` marker: the container being gone does not answer the
pause. `tests/test_escalation_undelivered_marker.py` covers presence on
all three surfaces while paused-and-undelivered -- for a persisted
`rejected_last_approach` pause and the in-memory `rejected_incurable` hold
alike -- and absence on all three once the pause is answered, executing
`app.js`'s own renderers in node for both the live and the gone runs-list
row.

Pause takes effect at iteration boundaries only: `pause()` clears an event that
`_gate()` awaits before each iteration, so the running agent finishes undisturbed,
and `resume()` releases it. An operator pause changes no status field; an
engine-initiated one (`pause(reason=...)`, taken by the
`rejected_last_approach` escalation edge, v0.18 requirement D.1/D.2, and by the
`stagnation` escalation edge on a stagnant last approach, v0.19 requirement
B.3, #153) also
persists `state: "paused"` and `pauseReason` on status.json immediately, and
`resume()` clears that pair back to `running`/`null` while leaving `approach`
and `phase` alone — nobody typed that pause, so the host has to be able to read
it (§5.2). Interrupt is `SIGINT` to the agent's process group —
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

### 4.7 Tiered provisioning

Task 005/007 gave each hat its own `(model, thinking)` pair -- `model_overrides`/
`models` plus `thinking_overrides` in `job.yaml`, resolved by `JobConfig.
bundle_for(hat)`. Task 012 (#68 half 2) widens that single per-hat bundle into a
per-hat **ladder** of three, so a task can provision above or below its plan's
default without a second model-resolution pathway existing anywhere.

**The vocabulary is exactly three ordinal tiers** (`engine/tiers.py`):
`trivial < normal < hard`. Not configurable in count or name -- "tune the tier
count" is an explicit non-goal. Every hat (`planning`, `worker`, `review`,
`verify`, `reflect`, `router`) has its OWN three-rung ladder; nothing assumes a
relationship between one hat's `hard` rung and another's.

**The hat x tier matrix** is built, not hand-configured: `tiers.
default_host_map(cfg)` widens `JobConfig.bundle_for`'s existing per-hat table
into `{hat: {tier: HatBundle}}` by giving every one of a hat's three rungs that
hat's single already-resolved bundle. A job.yaml naming only `model`/
`model_overrides` therefore collapses cleanly: every rung of every hat's ladder
names the same bundle, and tiering is a no-op rather than an error, with no
special-case branch. An operator who wants a REAL per-(hat, tier) override
names it in job.yaml's `model_tiers` table (§6.2) -- one leaf per rung, keyed
`{hat: {tier: leaf}}` -- or reaches that same table from `start` with the
repeatable `--model-for-tier HAT:TIER=REF` flag (§10.2), which merges rung by
rung over the `--template`'s and the registry's own `model_tiers:` key.
`default_host_map` is still the ONE place that resolves it into the map
`resolve_bundle` actually looks up (task 016's single dispatch call site).
Two display/validation-only readers of the same table exist beside it and
both are pinned OFF that call site by the same guard
(`test_resolver_has_exactly_one_call_site_in_src`): `JobConfig.
provisioning_table` (§6.2) reuses `tiers._apply_tier_table`/`_bundle_from_
leaf` directly (private helpers the guard does not name), and `engine/
llm.py`'s startup route check (§3.1) mirrors the same per-leaf model-ref
logic in its own dedicated helper rather than calling either function --
neither is a second RESOLUTION algorithm, both defer to `bundle_for` for
any rung `model_tiers` leaves unset.

**The resolver is a pure function, not a transition-table entry.**
`tiers.resolve_bundle(hat, effective_tier, host_map) -> HatBundle` does no I/O,
touches no clock, and never reads `JobConfig` itself (only `default_host_map`,
the map's builder, does that, once, by delegating to `bundle_for` rather than
opening a second pathway). Fallback mirrors `model_for`'s own anti-downgrade
rule: an exact `(hat, tier)` rung wins; a hat missing that rung borrows
`planning`'s ladder at the SAME tier (never the cheap tier); a map missing even
that answers with pi's own default. `transitions.TABLE` is deliberately
untouched by any of this (`test_transition_table_carries_no_provisioning_
policy`) -- the table stays about *what happens next*; provisioning is *how*,
applied at the single dispatch call site in `loop.py`'s `run_iteration()`
(task 016), never inside the FSM itself.

**A task carries a tier, plan ingestion is the only place it is set.** Task 013
adds an optional `tier` key beside `priority` in the planner's proposal;
`plan_ingestion.normalize_task_tiers` defaults a missing or unrecognised value
to `tiers.DEFAULT_TIER` (`normal`) and warns rather than rejecting the plan --
an old, tier-naive proposal ingests exactly as it always did. A plan where
every task is annotated `hard` also warns: a uniformly maximal tier defeats the
point of tiering. `tasks.json`'s persisted `effectiveTier` (§5.3) is a SEPARATE,
harness-owned field from the planner's own `tier`: `state.
claim_task_effective_tier` sets it, equal to the baseline, on a task's first
`pending` -> `in-progress` claim; no hat's response contract has a `tier`/
`effectiveTier` property (`additionalProperties: false` refuses one that tries),
so only the harness ever writes it.

**Escalation moves one rung per validation-failed retry, never down.** Task 014:
every `validation-failed` -> `in-progress` re-claim calls `state.
escalate_task_effective_tier`, `tiers.escalate_tier`'s `+1`-capped-at-`hard`
move -- a task already at `hard` stays at `hard`. There is no de-escalation
path at all (requirement G), which is true by construction rather than by
caller discipline: the only functions that write `effectiveTier` either claim
the baseline once or move it up. The moment a validation verdict comes back
negative for a task already at `hard`, `state.
mark_tier_ladder_exhausted_if_at_top` records `tierLadderExhausted: true` on
the record -- a distinct, queryable fact ('this task has spent its whole tier
ladder and still failed') feeding the stagnation guard, and once true it never
clears. An infra-fault re-entry that had already escalated the tier hands the
pre-escalation tier back (the same refund reasoning as an infra-retry's
iteration refund) rather than charging the task for an attempt that never ran.

**Verify provisions one rung below the work it checks; the run-ending verify
never economises.** Task 015 (decision 7/8): a per-task verify/review dispatch
runs at `state.task_verify_tier(task)` -- `tiers.step_down_tier` applied to the
task's CURRENT effective tier (post-escalation), floored at `trivial`. The
run-ending verification that decides whether the run itself goes terminal
(`_review_verdict`, the ordinary run-ending call -- v0.24 task 015/E.1
retired its off-budget `_maybe_grace_review` twin) always provisions at
`tiers.TOP_TIER`
(`state.run_level_verify_tier()`) regardless of any task's tier -- there is no
task in hand to read one from, and the exit gate is never a bottom-rung rubber
stamp. Every hat with no task in hand at all -- `planning`, `reflect`, and the
router's own call -- keeps its static half-1 role: `tiers.DEFAULT_TIER`,
unaffected by any task's tier in the plan.

**Spend is broken down by tier too.** Task 017 folds the tier a dispatch
provisioned at into `usage.byTier` (§8.6, §9.3) alongside the existing
`byPhase`/`byApproach` buckets, each entry also carrying a plain `iterations`
count -- so a run's cost can be read "how much did escalating this task cost"
as directly as "how much did the review phase cost". `taskEffectiveTier` (task
019, the highest `effectiveTier` any task in the plan currently carries) is
published on the hub/`ralphctl runs` row precisely because the fact that
escalation happened at all is otherwise invisible outside a task's own detail
view.

Model-resolution history: task 006 retired the earlier `model_strategy`/
`fast_model` phase->tier PRESET table (`quality-first`/`cost-optimized`/
`balanced`) that predates this mechanism entirely -- see §6.2 and §7.2 for that
migration. The two are unrelated other than by name: the retired presets chose
a model per PHASE from a fixed three-name vocabulary; this tier system chooses
a `(model, thinking)` BUNDLE per `(hat, tier)` cell from whatever the job's own
`job.yaml` configures, with the effective tier itself moving under a task's own
validation history.

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
├── tasks.json               # agent-readable VIEW of the plan and every task's status
│                           #   (§5.3); the AUTHORITATIVE copy moved OUT of the run
│                           #   dir entirely, to <config>/<run-id>/record/tasks.json
│                           #   (task 004, requirement B, #202) -- this file is
│                           #   refreshed from it on every write but is never itself
│                           #   read by the engine
├── events.jsonl             # append-only lifecycle log, monotonic ids (§5.4)
├── prd.md                   # the PRD exactly as handed to `ralphctl start`
├── composite-prd.md         # PRD + previous-approach history (§4.2), when written
├── notes.md                 # the agent's cross-iteration handoff notes
├── standing-rules.md        # harness-pinned copy of notes.md's "## Standing rules" block (§4.3)
├── review-findings.md       # the reviewer's unmet-requirement list, when rejected
├── review-findings.json     # the same findings, structured: severity/curable/clause/evidence, plus cured_when/invalidates (task 001, #110; task 031, #171)
├── review-verdict-early.json # review's own early-verdict checkpoint (task 025, requirement H.1/H.4, #264): {approach, verdict, reason (capped), findingsPath, recordedAt}, written atomically by the `record-verdict` tool THE MOMENT review calls it, replaced (never appended) on every refinement, so it survives a SIGKILL of the review iteration that wrote it
├── debt.json                # the debt ledger: verifier notes on a form-met-substance-missed clause, appended by `RunDir.record_debt` (task 006, requirement A.3, #155)
├── debt.md                  # the same ledger, rendered for a human, open then resolved (task 006, requirement A.3, #155)
├── verdict-gate-findings.md # verdict_gate's own free-markdown findings, when it rejects (task 019, requirement D.4, #125)
├── verdict-gate-findings.json # the same findings, structured with per-finding authorship (task 019, requirement D.4, #125)
├── petition-adjudications.json # run-scope CONFIRMED impossibility-petition adjudications, survives approach archiving (task 011, requirement C.3, #112)
├── petitions.md             # RUN-scope impossibility-petition filings/adjudications, never a workspace file (task 065, requirement N.6, #150)
├── approach-metadata.json   # engine-owned per-approach judge verdicts: review/verdictGate, each with explicit blocking+residual arrays, written as the judge runs, survives approach archiving (task 035, requirement B.1)
├── cure-lineage.json        # run-scope, disk-backed ledger of every granted cure pass's own findings-to-cure-task-id map and pre_cure_head, survives approach archiving and a resume (v0.27 requirement B.5 foundation, #274)
├── vigilant-verified.json   # engine-owned ids of tasks that passed verification
├── operator-termination.json # the operator's abort/stop intent (§4.6), when recorded -- archived on resume (below), never carried forward
├── auto-resume.json         # host-side crash-loop guard bookkeeping
├── host.json                # host-side container/registry metadata
├── .tasks-last-good.json    # last `tasks.json` that parsed; written only on a failed read (§5.3)
│                           #   (the AUTHORITATIVE task record is NOT in the run dir:
│                           #   task 004 moved it to <config>/<run-id>/record/tasks.json,
│                           #   bind-mounted at /var/lib/ralphd/record — §5.1.1)
├── .api-token               # the run's API bearer token, mode 0600
├── .lock                    # exclusive flock held by the live engine; holds its pid
├── steering/
│   ├── NNN-<name>.md        # one operator steering message
│   ├── .consumed.json       # names already actioned by an iteration
│   └── .ledger.json         # per-message ledger: consumption + the steer hat's settlement claim (§4.6)
├── rulings/
│   └── NNN-<name>.md        # one operator RULING (task 013, v0.23 requirement A.2, #224) --
│                           #   `ralphctl rule --file`/`POST /rule`/the hub textarea, keyed to
│                           #   `_review_rejection_signature`, never a task id (§4.6 below);
│                           #   deliberately under the run root, NOT `config_root`/`amendments/`
│                           #   (§13.4) -- a `POST /rule` handled in-container needs write access
├── iterations/
│   └── NNNN/                # one iteration (§5.5)
│       ├── meta.json        # what ran, how it ended, what it cost
│       ├── prompt.md        # the exact prompt the agent was given
│       ├── output.jsonl     # the agent's raw NDJSON transcript
│       └── attempts/NN/     # earlier attempt(s) at this same number, archived on resume (§5.5)
├── approaches/
│   └── NN/                  # archived tasks.json/notes.md/review-findings.md
├── previous-endings/        # superseded episodes' operator-termination markers, archived on resume (task 005, #173, §5.2)
│   └── NN-operator-termination.json # NN is the matching `previousEndings` entry's 1-based position
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

#### 5.1.1 The record mount — where the authoritative tasks.json lives

**Not in the run directory** (task 004, requirement B, #202). `ralphctl
start`/`resume` bind-mount a mode-0700 host directory,
`cli.main.task_record_dir(run_id)` (`<config>/<run-id>/record/`), at the fixed
container path `privsep.DEFAULT_TASK_RECORD_DIR` (`/var/lib/ralphd/record`,
overridable with `$RALPHD_TASK_RECORD_DIR`), and `RunDir.write_tasks()` lands
`tasks.json` there — root-owned 0600 inside a root-owned 0700 directory under
the uid boundary, re-asserted on every write
(`privsep.write_protected_tasks_json`). `RunDir.read_tasks()`/
`read_tasks_result()` read only that copy, through the same momentary root
re-raise the write uses; the run dir's own `tasks.json` is the agent-readable
VIEW every other reader uses (§5.3).

Why not a protected subdirectory of the run dir, which is where this record
first moved: a directory entry is gated by its PARENT, and the run dir is
agent-owned by construction (the agent writes `notes.md`, `artifacts/` and
every iteration record into it), so a dropped agent child could rename a
root-owned `.tasks-protected/` aside and plant its own in its place —
requirement B.1 asks for **every ancestor entry on the path**, not only the
file. `/var/lib/ralphd` is root-owned 0755 in the running container — created
by the docker daemon as it materialises this very mount point, not by an image
layer (task 019 measured it absent from both `python:3.12-slim-bookworm` and
the job image) — so nothing on the
record's container path is modifiable at the agent uid; the host directory
behind the mount lives under `config_root()`, bind-mounted READ-ONLY at
`/config`, so no agent-writable path aliases it either. It still survives a
`resume`'s fresh container (it is a host directory), `remove_run_state`
already deletes `config_root(run_id)` so `ralphctl rm --force` and the hub's
`DELETE /api/runs/<id>` take it with the rest of the run's state, and
`privsep.release_protected_tasks_dir` hands it back to the agent uid at the
engine's own shutdown funnel so those unprivileged deletes can. A run dir
created before this move is lifted into the mount once, at engine startup,
by `privsep.migrate_task_record()` — before the API listens and before any
iteration exists — and the superseded copy is removed rather than left as a
second, agent-writable source of truth. With no mount at all (a `ralphctl`
predating this task, or the test suite's own unprivileged engine) the record
degrades back to `privsep.legacy_protected_tasks_dir()` inside the run dir.

### 5.2 status.json

The single answer to "what is this run doing". One document, patched field by field
(`update_status()` reads, merges, rewrites, and always refreshes `updatedAt`).

| field | type | meaning |
|---|---|---|
| `runId` | string | the run id |
| `engineVersion` | string | v0.21 requirement F.4 (#159/#194, task 030) -- the running engine's own `ralphd.__version__`, written unconditionally by `LoopSupervisor._record_engine_version` at every process start (the same "once per process start" placement as the drift check and the notifier-configured check below it), alongside the matching `run_record.ENGINE_STARTED_EVENT` startup event. Distinct from the drift check just below: this one names the ENGINE's own version and fires on every run, drift or not; `_check_workspace_version_drift`'s `workspace_version_mismatch` event names the WORKSPACE's own version and fires only on a mismatch |
| `workspaceVersion` | string | v0.21 requirement G.3 (task 035) -- the workspace's own `__version__` literal, persisted by `LoopSupervisor._record_workspace_version` once per process start whenever that literal is READABLE at all, whether or not it matches the running engine's, so a hub run page can show both this run's engine version and the version of the workspace it is executing without scanning the raw event log itself. Absent only when the workspace names no version (a pre-literal checkout, one mid-edit), i.e. its absence means "not knowable here", never "equal to `engineVersion`". Deliberately a WIDER condition than E.3's `workspace_version_mismatch` event (task 017), which still fires on a mismatch alone |
| `state` | string | `starting` \| `running` \| `paused` \| `finishing` \| `timed_out` \| `succeeded` \| `failed` \| `aborted` (task 012, #56 adds `finishing`: the outcome is settled, the post-terminal `reflect` iteration has not run yet -- see §12.2; v0.18 task 036, #142, adds `paused`: an ENGINE-initiated hold on the `rejected_last_approach` escalation edge, never an operator `POST /pause` -- see `pauseReason` below; v0.24 task 008, #169, adds `timed_out`: the deadline hold, resume-eligible, never a member of `TERMINAL_STATES`) |
| `schemaVersion` | int | run-dir schema this state was written under (§5.7) |
| `health` | string | `ok` \| `degraded` — degraded while sitting out an infra outage |
| `infraWait` | object \| null | the backoff wait in progress: `since`, `attempt`, `error`, `phase`, `nextAttemptAt`, `waitedS`, `budgetS`, `remainingS`; `null` whenever nothing is being waited on |
| `infraWaitTotalS` | float | cumulative seconds this run spent in infra backoff waits |
| `pausedTotalS` | float | cumulative seconds credited back for operator pauses (v0.14 requirement P, #95, task 016) -- the pause twin of `infraWaitTotalS` above, published beside it; absent (read as `0`) for a run that has never been paused, and seeded from `status.json` at startup so it survives `resume` |
| `planReviewRevisions` | object | `{"<approach>": <count>}` -- per-approach peer plan-review revision counter (v0.14 requirement K, #101, task 018); absent for a run with `peer_plan_review: false` (the default) or one whose plan was never objected to |
| `planReviewApproved` | object | `{"<approach>": true}` -- per-approach marker that the peer plan review let this approach's plan proceed (an `approved` verdict or the revision bound being reached), written by `_step_plan_review` (#161); a resume reads it (or any task past `pending`) to skip re-entering `plan_review` over a plan that is already being worked, since a revision re-ingests a proposal with every status reset to `pending`. Absent for a run with `peer_plan_review: false` or one whose plan review has not spoken yet |
| `workerSplitProposals` | object | `{"<approach>": <count>}` -- per-approach running total of tasks an accepted worker `proposed_tasks` split proposal has inserted (v0.14 requirement N, #102, task 021); compared against `worker_split_budget_per_approach` (default `3`) before a further proposal is accepted; absent for a run that has never had one accepted |
| `midPlanReviewEpisodes` | object | `{"<approach>": true}` -- per-approach mid-plan review episode marker (v0.24 requirement K.2, #169, task 022), written strictly BEFORE the episode's own `run_iteration` call is awaited (at dispatch), so an episode killed mid-flight by a crash, an interrupt or the job deadline still counts as spent and a resumed process never re-runs the ONE episode an approach is entitled to; the episode runs once the declared fraction of the plan has already reached terminal-success, scoped to those requirements, opening a cure task per blocking finding through the existing cure machinery. Absent for a run with `mid_plan_review: false` (the default) or one that has not yet reached the declared fraction |
| `workerRepointUpdates` | object | `{"<approach>": <count>}` -- per-approach running total of EXISTING tasks an accepted worker `proposed_task_updates` re-point has moved (#209 defect 7); compared against `worker_repoint_budget_per_approach` (default `3`) before a further batch is accepted, and refused whole rather than partially once that would be exceeded; a separate counter from `workerSplitProposals` above because a re-point inserts nothing. Absent for a run that has never had one accepted |
| `progressBudget` | object \| absent | `{taskId, used, budget}` -- the in-progress task's own progress budget (v0.20 requirement A.3/A.4, #156, task 013): a corroborated `no_verdict` on task `taskId` spends `used` against `budget` (`estimatedIterations + 2`, default 1), resetting to `1` the moment a DIFFERENT task's claim is corroborated. Absent until the first corroborated progress claim; `health` is never touched by writing it (progress is not infra). `ralphctl status`, `ralphctl tasks` (a ` (progress budget N/M)` suffix on the NAMED task's own row, only while that task is still `in-progress`) and the hub's task row (a `budget N/M` pill, same gating) all render it |
| `phase` | string \| null | the phase running now; `null` between phases and once terminal |
| `iteration` | int | the current (raw) iteration number |
| `iterationsUsed` | int | charged iterations: raw attempts minus infra and steer refunds (task 014, #98) |
| `iterationsRefunded` | object | the run's earned refunds, `{infra, steer, adjudication}` — the counters `budget_left()` subtracts, persisted here so they survive a resume (§8.4); absent (read as zeroes) until the run earns its first refund. v0.24 task 015 (requirement E.1, #169) renamed this object's third key from `grace` (the retired off-budget grace review's own bucket) to `adjudication` (the midrun petition-adjudication episode, task 014/F.1, that inherited its refund shape); a pre-v0.24 run dir's persisted `grace` key is read as history, never migrated |
| `iterationsBudget` | int | the current iteration budget, including in-flight top-ups |
| `currentIteration` | object \| null | `{number, phase, model, startedAt}` while an iteration runs, or `{phase, note}` while retrying after an infra fault; `null` otherwise |
| `approach` | int | the approach running now |
| `maxApproaches` | int | configured approach ceiling, so every surface can render `approach 2/3` rather than a bare number |
| `model` | string \| null | the model reference the run's iterations actually reported, `provider/model`; `null` until an iteration observes one |
| `modelRaw` | string \| null | the provider's own id when it differs from the resolved reference (a gateway-local spelling); `null` when it does not |
| `verdict` | string \| null | `verified` \| `verified-with-residuals` \| `unverified` \| `unreviewed`; `null` until terminal, and also the value while `state` is the non-terminal `timed_out` hold (v0.24 task 010, requirement B.3, #169 -- a budget/deadline exhaustion no longer ends the approach terminal, see §4.5) -- `verified-with-residuals` (task 012, requirement D.3) is review's own VERIFIED edge written instead of plain `verified` when the settled findings channel carries at least one `severity: "residual"` finding (`LoopSupervisor._review_success_verdict_label`), so a clean approach still reads `verified`; `unverified` is the ordinary negative (review ran and rejected with no approach/budget left, or the run was aborted). A deadline that lands mid-review (self-loop exhaustion) used to write `unreviewed` here too; v0.24 task 009 (requirement B.2, #169) carved that case out to carry `null` instead (`reason` still reads `"deadline during review"`), because the run is resumable and a resumed review may still render a real verdict -- `unreviewed` is now written by exactly ONE site: review ran to completion and its own contract ladder never got a readable verdict claim out of the hat (`LoopSupervisor._review_contract_unrepaired`, GH #209 defect 2), so `reason` reads `"review never produced a parseable contract payload; no verdict was ever scored"` -- reached only when the iteration/wall-clock budget is ALSO exhausted at that point (the ordinary tail exit into `_finish_tail_unverified`), since a run with budget left instead HOLDS on `pauseReason: "review_contract_unrepaired"`. Either way `reason` is not a real rejection reason, and neither writer overrides an actual operator abort (which keeps `unverified`) |
| `pendingTerminalState` | string | present only while `state` is `finishing`: the terminal state this run is going to become once the reflect window closes (§12.2) |
| `reason` | string | why a non-succeeded run ended, including the `timed_out` hold's own note (§4.5); **episode-scoped** like `endedAt` |
| `termination` | object \| null | present only for the CURRENT episode, and only once it was told to stop: `{class, action, at, signal, reason, evidence}`, where `class` is `operator` (someone asked through `POST /abort`) or `self-inflicted` (a signal reached the engine and nobody claimed it — §4.6). `evidence` is the last tool call before the signal (`{iteration, tool, args, transcript}`) or `null`. Task 005 (#173): a resume that opens a fresh episode folds the SUPERSEDED episode's `termination` into the matching `previousEndings` entry (below) and **removes the top-level key outright** (not `null`: a presence test is how consumers ask "was this run told to stop?"), so a run stopped by an operator and then resumed into a clean ending carries no `termination` key at all, and a `--json` consumer/the hub/any future classifier reading a terminal run's `termination` is reading the CURRENT episode's own record, never a stale one two episodes old |
| `reflect` | object | `{ok, error, endedAt}` — the post-terminal reflect phase's own verdict; `error` is `null` when it produced a report, and a failure also leaves `artifacts/reflection/FAILED.md`. `ok: null` with `{attempted, skipped}` instead means a signal was already taking the engine down, so the phase produced no verdict and no tombstone was written (§8.4) |
| `unconsumedSteering` | array | steering files still pending at the terminal write |
| `steerSettled` | object \| null | the most recent `steer` hat iteration's own `settled` claim: `{settled, at, notes}`; `null` until the first `steer` iteration runs. `settled` is a harness-observable fact SEPARATE from `done` (task 013, requirement I, #98) -- it means only "the hat processed this iteration's pending steering message(s) and applied its changes to the plan", never a claim about whether the tasks it inserted or changed will pass. That verdict is read off those tasks' own `status` plus their `origin: "steer"` marker (`tasks[].origin`, §5.3) -- the exit gate (R11 below) waits on THAT, never on this field |
| `deadlineBeatVerdict` | object \| absent | present only on a `failed`/`unverified` terminal write where every task was done, `COMPLETE` was already honoured, and review never rendered a verdict because the deadline took the last attempt (v0.10 requirement B, #72): `{deadline, completeHonouredAt}`, naming the wall-clock deadline and the `ts` of the honoured `COMPLETE` event -- see §4.2 |
| `onComplete` | string | `exit` \| `idle` |
| `usage` | object | token and cost totals, plus `byPhase`, `byApproach` and `byTier` buckets |
| `createdAt` | string | UTC ISO-8601, first status write |
| `startedAt` | string | when the loop entered `running` |
| `endedAt` | string | this **episode**'s terminal write, reset when the next episode enters `running` (see "Ending fields are episode-scoped" below), so it can never predate `startedAt` and a terminal run's value is exactly what its final episode wrote |
| `previousEndings` | array | earlier episodes' endings, oldest first: `{endedAt, reason, verdict}` per superseded ending, plus `termination` (task 005, #173) when that episode was told to stop, capped at the 50 most recent, `[]` in a first episode |
| `updatedAt` | string | last write of any field |
| `deadlineAt` | string | wall-clock deadline, extended by every infra wait |
| `approachStartHeads` | object | `{"<approach>": "<sha>"|null}` — the workspace HEAD `approach` started at (v0.20.1 requirement A.1, task 001), written by `_approach_start_head` the FIRST time `_step_worker` reads it for that approach and read back unchanged on every later entry for the SAME approach — another iteration of the same process, an in-process resume, or a genuinely fresh process/`LoopSupervisor` built over the same run-state tree — so the stagnation-exhaustion "did this approach gain commits" comparison's own origin can never be silently reset to whatever HEAD happens to be at the moment of a restart. `null` for a non-git workspace, still recorded (distinguishing "already read" from "never read yet"). Absent for an approach whose `_step_worker` has never run |
| `preCureHeads` | object | `{"<taskId>": "<sha>"|null}` — one entry per record task `state.insert_cure_tasks`'s `invalidates` walk has re-opened (v0.23 requirement B.2, task 006, #221), keyed by the RECORD task's own id (never a verification/retake task's), carrying the workspace HEAD sha the cure pass that re-opened it started at — the same `pre_cure_head` value the caller (`LoopSupervisor._take_cure_pass`/`_take_gate_cure_pass`/`_insert_refuted_remedy_tasks`) reads once via `_workspace_head_sha()` before the call, in the exact `{"<key>": "<sha>"|null}` shape `approachStartHeads` above already uses. Written the moment the record is re-opened and emitted on that same reopen's own `task.pre_cure_head_recorded` event, so a reader can confirm the two agree without a second disk read. A LATER re-open of the SAME record id overwrites its entry with that pass's own sha — one live pre-cure baseline per record at a time, the one the CURRENT re-open owes. Read back by `state.records_pending_reconciliation` (v0.23 requirement B.4, task 008, #221), which the engine calls from BOTH completion-path writers (`LoopSupervisor._apply_reported_task_status`, the per-task verify write) every time ANY task reaches `completed`/`validated`, naming that task's own id: only a cure task some reopen actually named as its own `cureTaskId` (`task.pre_cure_head_recorded`'s own field) has anything to reconcile — every other caller is a cheap no-op. One cure PASS can insert several cure tasks whose `invalidates` name the SAME record, and all of them share the one `pre_cure_head` that pass read, so a record's live baseline can be owned by several cure tasks at once; the record is reconciled only once EVERY owner of its live baseline has itself reached `completed`/`validated`, over a single diff spanning all their commits — an owner still open (or one that ended `failed`/`skipped`) leaves the record `pending`, so no owner's clean diff can restore a record another owner then changes a cited path in. `null` when the caller's own workspace is non-git or a caller passed none, which the reconciliation hook treats as "cannot diff" and leaves the record exactly where it is. Absent for a run that has never re-opened a record |
| `preCureStatuses` | object | `{"<taskId>": "<status>"}` — one entry per record task `state.insert_cure_tasks`'s `invalidates` walk has re-opened (cure-01-05, PRD requirement B.3/B.4, F05, #221), keyed by the RECORD task's own id, same key set and same overwrite-not-accumulate rule as `preCureHeads` just above, carrying the record's OWN ACTUAL status immediately before THIS reopen — `completed`, `validated`, `failed`, `skipped` or `superseded`, whichever it genuinely held, never a value derived from `cfg.vigilant`. Emitted on that same reopen's own `task.pre_cure_head_recorded` event as `priorStatus`. A record already `pending` at reopen time (a still-unreconciled earlier reopen this same approach) does NOT overwrite this entry — the true terminal status an earlier reopen already captured stays live until reconciliation consumes it. Read back by `state.records_pending_reconciliation`'s own `priorStatus` field, which `LoopSupervisor._reconcile_pre_cure_heads` restores the record to (through `state.apply_task_status_change`, `allow_un_reopen_restore=True` only for its own `source="cure-reconciliation"` call) instead of guessing `validated`/`completed` from the run's variant — the declared un-reopen edge covers all five statuses in BOTH variants for exactly that reason (`transitions.UN_REOPEN_TASK_STATUS_TARGETS`, base table only), and a candidate carrying no entry here at all is left `pending` rather than restored to a guess. Absent for a run that has never re-opened a record |
| `blockerSignatures` | object | `{"<approach>": "<signature>"}` — per-approach worker-stagnation blocker signature (task 101, #61), written the moment an approach fails on stagnation; `_step_replan`'s `repeated_blocker_signature` guard compares the entry for the approach that just failed against its immediate predecessor's, and a genuine match ends the run rather than spending another approach on the identical structural wall. Absent for a run that has never stagnated |
| `blockerSignatureOrigins` | object | `{"<approach>": "stagnation"}` — per-approach origin of the matching `blockerSignatures` entry (task 012, requirement C.2, #124), written alongside it by `_record_stagnation_signature`. Task 003 (requirement C) retired plan-ingestion's own use of this shared, approach-keyed map — a zero-deliverable planning death deliberately never advances the approach counter, so "stagnation" is the only value ever written to this field since; the field itself was kept, not folded away, for the run history it already accumulated before task 003. Absent for a run that has never stagnated |
| `planningDeathSignature` | string or null | plan-ingestion's own two-strike signature (task 003, requirement C), NOT keyed by approach — a zero-deliverable planning death deliberately never spends a product approach, so `_step_planning` compares a failing ingestion's normalised signature against this field directly, unkeyed, to tell a genuinely repeated failure from a distinct one across consecutive planning attempts on the SAME approach. Reset to `null` the moment any ingestion succeeds. Absent for a run that has never had a plan proposal refused at ingestion |
| `planningDeathDetail` | string or null | the RAW ingestion-error text (task 007, requirement B.3, #135 wording preserved) paired with `planningDeathSignature` — quoted verbatim into the terminal reason when the two-strike guard fires on it. Absent for a run that has never had a plan proposal refused at ingestion |
| `steerRedeliveries` | object | `{"<name>": <count>}` — per-message redelivery counter (task 006, requirement B.1, #122) `_reverse_stale_steer_settlement` maintains, disk-backed and surviving a resume, the same shape `planReviewRevisions` already uses: a clean steer iteration that honestly reports `settled: false` gets the message unconsumed and redelivered while its count stays below `steer_redelivery_max`, and is instead marked settled-by-exhaustion once it reaches the cap. Absent for a run that has never had a `settled: false` steer payload |
| `verdictGatePeeks` | array | v0.17 requirement D.5 (#125 A): one `{approach, iteration, calls}` entry per `verdict_gate` iteration (either call site) whose own transcript held a tool call reading past the gate's honor-system blindness — `iterations/`, `events.jsonl`, or the per-iteration transcript file (`state.detect_verdict_gate_peek`). A recorded observation only: never consulted by `_verdict_gate_verdict`, so it never changes `verdict`. Absent for a run with `peer_verdict_gate: false` or one whose gate iterations never peeked |
| `pauseReason` | string \| null | v0.18 requirement D.1/D.2 (#142, task 036) -- why the engine paused ITSELF, written together with `state: "paused"` at the escalation site the moment the decision is made, and cleared back to `null` (with `state` back to `running`) by `POST /resume`. Nine values: `"rejected_last_approach"` -- the last approach was rejected on blocking findings that are all `curable: true` with its single cure pass already spent, so the run holds for an operator ruling instead of ending `failed` on paperwork, and a resume re-enters `review` over the SAME approach; `"stagnation"` (v0.19 requirement B.3, #153, task 012) -- the last approach's worker stagnation guard (three no-progress iterations) would otherwise fail the run silently, so the run holds instead, and a resume re-enters `worker` over the SAME approach (there is no verdict pending, only unfinished tasks); `"rejected_repeated_signature"` (v0.20.1 requirement E.5, #190, task 016) -- an ORDINARY rejection's own blocking-clause signature (`rejectionSignatures`, below) repeated the immediately preceding approach's own recorded rejection signature, so the run holds instead of spending a fresh approach on a wall it has already lost one approach to, and a resume re-enters `review` over the SAME approach, exactly like `"rejected_last_approach"`; and `"unschedulable_dependency_cycle"` (v0.22 requirement E, #206, task 002) -- a `dependsOn` CYCLE (a self-loop is the length-one case) touches the plan's own actionable remainder, detected by graph SHAPE alone (never by any task's status, which is why a merely in-flight, not-yet-resolved dependency never trips it) on the very first selection pass nothing is schedulable, so the run holds for an operator's `tasks.json` edit instead of grinding through three no-progress worker iterations first, and a resume re-enters `worker` over the SAME approach, exactly like `"stagnation"`; `"unresolvable_dependency_edge"` (v0.22 requirement E, #206) -- a `dependsOn` edge naming a task id the plan does not carry at all, which no worker iteration can ever satisfy, so the run holds for the same `tasks.json` edit and a resume re-enters `worker`, exactly like `"unschedulable_dependency_cycle"`; `"review_contract_unrepaired"` (GH #209 defect 2) -- `review` ran to completion but its own verdict claim was never readable across `contract_retry_max + 1` attempts, so no verdict was ever rendered and the run holds instead of attributing a rejection to a judge that never spoke, and a resume re-enters `review` over the SAME approach, exactly like `"rejected_last_approach"`; `"review_findings_unusable"` (v0.23 requirement C.2, #219, task 011) -- the discriminating sibling of `"review_contract_unrepaired"`: review's own verdict claim WAS legible, but the REVIEW CONTRACT CHANNEL the ladder re-asked over never became usable (a present-but-invalid findings sidecar, a `verified` claim contradicting a blocking finding, a curable finding with no `cured_when`, or -- v0.23 requirement C.3, task 012 -- a stray/wrong-typed field elsewhere in the payload's own closed schema, the `additionalProperties: False` slip a stubborn hat repeats on every re-ask), so there is no rejection to classify off a channel the engine itself cannot trust and the run holds instead, and a resume re-enters `review` over the SAME approach, exactly like the edge above; and `"operator_input_needed"` (v0.23 requirement A.1, #224, task 014) -- an ORDINARY rejection that would otherwise archive the approach (whether the next step would have been a fresh `replan` or the terminal `rejected_no_approach_left` outcome) holds instead, ON by default (`JobConfig.operator_input_needed_pause`, disabled with `ralphctl start --no-operator-input-needed-pause`), so an operator can answer -- with a ruling (`ralphctl rule <run> --file <ruling.md> --name <slug>`, task 013) or a plain `ralphctl unpause <run>` -- before that spend happens, and a resume re-enters `review` over the SAME approach, exactly like every edge above; and `"verdict_gate_operator_input_needed"` (v0.27 requirement C.1, task 008) -- the peer verdict gate's own mirror, narrower than `"operator_input_needed"`: a `verdict_gate` rejection that would have earned the gate's own cure-pass edge (`gateCurePasses`, `verdict_gate_rejected_curable_pass_available`) if the run-wide `cure_pass_ceiling` were not already spent (`gateRejectionEnding: "rejected_cure_unavailable"`) holds instead of the ordinary archive/replan-or-finish edges, ON by default through the SAME `JobConfig.operator_input_needed_pause` flag; a resume re-enters `verdict_gate` (never `review`, which has already spoken `verified` for this approach) over the SAME approach. Unlike `"operator_input_needed"`/`"rejected_repeated_signature"`, filing a ruling against this pause alone never releases it (`resume_if_ruling_answers_pause`'s own reason allow-list names neither this nor `"rejected_last_approach"`/`"rejected_incurable"`) -- releasing it always needs a separate `ralphctl unpause <run>`. The task-boundary commit-secret-scan hold (task 030, requirement I.3, #210, §4.6) records this SAME value from `worker` with no review verdict behind it, emits no `operator_escalation` event (its record is the `commit_secret_scan` event), and resumes into `worker`, exactly like `"stagnation"`; its own answer depends on whether its approach already carries a recorded `rejectionSignatures` entry (cure task cure-01-01-9, PRD A.1/A.6, D.1) -- with none (the common case), `ralphctl unpause <run>` alone (a ruling does not release it); with one (an earlier `"operator_input_needed"`/`"rejected_repeated_signature"` pause on the SAME approach, already answered by a ruling before this hold ever landed), `ralphctl rule <run> --file <ruling.md> --name <slug>` releases it too, since `resume_if_ruling_answers_pause` reads only this field and that signature, never which pause raised it. Either way `approach` and `phase` are left untouched and the approach is not archived. Absent/`null` for an operator `POST /pause`, which records no status field at all |
| `rejectionSignatures` | object | `{"<approach>": "<signature>"}` -- v0.20.1 requirement E.5 (#190, task 016) per-approach findings-channel REJECTION signature: the sorted, normalised blocking-clause set (`LoopSupervisor._review_rejection_signature`) of an ORDINARY rejection (one that took none of `rejected_incurable`/`rejected_curable_pass_available`/`rejected_last_approach`), written the moment that rejection is about to archive the approach and replan. `_step_review` compares the entry for the approach that just rejected against its predecessor's; a genuine match takes the `rejected_repeated_signature` pause edge instead of archiving/replanning. Disk-backed and survives a resume. Absent for a run that has never taken this ordinary-rejection branch |
| `rulingReparked` | array | `[<seq>, ...]` -- v0.23 requirement A.5 (#224, task 016) the operator ruling `seq` numbers (`state.ruling_entries`) that have already spent their ONE re-park: a re-run `review` re-rejecting on the SAME finding signature a recorded ruling addressed (`LoopSupervisor._ruling_honoured_for_approach`) takes the `operator_input_needed` pause once for that ruling and appends its `seq` here (`_record_ruling_reparked`); a later rejection matching that same ruling's own signature (`_ruling_already_reparked`) proceeds to the ordinary archive/replan tail instead of re-parking a second time -- one re-park per ruling, never per signature (a fresh ruling filed later against the identical signature is a different ruling and earns its own re-park). Disk-backed for the life of the RUN, additive, never reset per approach. Absent for a run that has never re-parked on a ruled signature |
| `noRulingProceed` | object | `{"<reason>": "<signature>"}` -- v0.27 requirement C.2 (#274, task 009) a one-shot 'proceed', keyed by pause reason (`"operator_input_needed"` \| `"verdict_gate_operator_input_needed"`), granted the moment a bare `ralphctl unpause <run>`/`POST /resume` releases that exact pause with NO ruling filed to answer it (`LoopSupervisor.resume`'s own `_record_unpaused_no_ruling_proceed`, called before `pauseReason` is cleared) -- keyed on the SAME rejection signature the pause itself was raised over (`rejectionSignatures[str(approach)]` for review's own pause; `LoopSupervisor._gate_rejection_signature` off the gate's own findings channel for its C.1 sibling, which keeps no signature map of its own). The very next rejection whose own signature matches the grant (`_no_ruling_proceed_signature`) consumes it (`_consume_no_ruling_proceed`, deleting the entry) and takes the ordinary archive-and-replan (or finishing) edge instead of parking again -- mirroring the one-re-park bound `rulingReparked` already gives a ruling. A rejection on a DIFFERENT signature never matches and still parks. Review's own grant is additionally refused when a ruling was just filed that already answers the signature (`_ruling_honoured_for_signature`) -- that case is `rulingReparked`'s own bound, not a second, independent one-shot on top of it. Disk-backed and survives a resume; absent for a run that has never unpaused either of these two pauses with no ruling filed |
| `discretionaryGateSpent` | object | `{"<approach>": true}` -- v0.23 requirement A.3 (#224, task 017) the ONE shared discretionary gate trigger conditions 2 and 3 draw from: trigger condition 2 (`state.cure_pass_preview` shows a cure pass would re-open task(s) already `validated`) and trigger condition 3 (`LoopSupervisor._repeated_rejection_signature`) each check `_discretionary_gate_available` before pausing and `_spend_discretionary_gate` the moment they do -- whichever fires FIRST in an approach spends that approach's own entry; the other, firing later in the SAME approach, falls through to its ordinary edge instead. Condition 1's own `operator_input_needed` gate (task 014) is a SEPARATE, unbudgeted pause and never reads this map, with one exception that keeps this budget from being a no-op under the default-enabled flag: the very rejection whose condition-3 match this spent budget just refused does not then take condition 1's gate either -- it goes to the archive/replan tail -- while any rejection that never matched condition 3 still raises condition 1's gate, spent budget or not, and a reviewer-raised gate (`raise_operator_gate`, requirement A.6) overrides both. Disk-backed and survives a resume; a fresh approach always reads its own key back unset. Absent for a run that has never spent this gate |
| `curePasses` | object | `{"<approach>": <count>}` — v0.18 requirement C.1/C.2 (#141) per-approach cure-pass counter: a `rejected` review whose blocking findings are all `curable: true` and whose entry for that approach reads `0` takes the `review -> worker` cure-pass edge (cure tasks appended, `origin: "review-cure"`) instead of replanning, as long as the RUN's own cure budget is not spent. v0.20 requirement E.1 (#167, task 030) widens the bound past the original one-per-approach cap: a second or later `rejected` in the same approach takes ANOTHER cure pass iff that budget is still open AND this rejection is genuinely narrower or different from the immediately preceding granted pass's own target. On an ORDINARY (non-confirmation) review that comparison is by `clause` text — this rejection's own blocking clause set (`findings.blocking_finding_clauses`) is not a superset of the preceding pass's own set (`cureBlocking`, below). On a CONFIRMATION review (v0.27 requirement B.4, #274, task 007, `LoopSupervisor._is_confirmation_review`) it is instead by FINDING IDENTITY: the set of `reopens` cure task ids this rejection's own blocking findings carry, each followed back through `tasks[].reopensCureTaskId` (`state.root_cure_task_id`) to the root finding it descends from, against the identical resolution of the preceding pass's own targeted set (`_cure_pass_targeted_roots`, read from `cure-lineage.json`) — a reworded `clause` on a re-filed finding changes nothing here, only the `reopens` chain does. Either way, a repeated or grown target set falls through to the ordinary replan/pause edges regardless. The budget is ONE run-wide pot read from configuration (v0.21 requirement A.2, #192, task 002): `LoopSupervisor._cure_passes_for_run()` — this counter's values plus `gateCurePasses`' own, summed over every approach — must be below `JobConfig.cure_pass_ceiling` (job.yaml key `cure_pass_ceiling`, default 3), where a per-approach count was compared against the module-level `CURE_PASS_CEILING` literal before that task. Once the run's ceiling is spent, no further pass is granted on either channel or in any approach, a freshly replanned approach's own first rejection included. Disk-backed and survives a resume. Absent for a run that has never taken a cure pass |
| `cureBlocking` | object | `{"<approach>": [<clause>, ...]}` — v0.20 requirement E.1 (#167, task 030) companion to `curePasses`: the blocking clause set (`findings.blocking_finding_clauses`, sorted) the MOST RECENT granted cure pass for that approach recorded, written by `LoopSupervisor._take_cure_pass` in the same status update that increments `curePasses[approach]`. `_review_cure_pass_available` reads it back (`_cure_pass_blocking_clauses`) to decide whether a later rejection in the same approach is a superset (refuse) or not (grant, up to the ceiling) — but only on an ORDINARY (non-confirmation) review; on a confirmation review the comparison reads `cure-lineage.json` instead (v0.27 requirement B.4, #274, task 007, `_cure_pass_targeted_roots`), never this field. Still written unconditionally by every granted pass, confirmation review or not, so a later approach that never takes another confirmation review still has it available. Disk-backed and survives a resume; absent for a run that has never taken a cure pass |
| `cure-lineage.json` (own file, not a `status.json` key) | object | v0.27 requirement B.5 foundation (#274): `{"passes": [{"approach", "preCureHead", "findings", "recordedAt"}, ...]}` -- one entry PER GRANTED cure pass, appended (never overwritten or replaced) by `state.insert_cure_tasks` itself, so both `LoopSupervisor._take_cure_pass` (review's own edge) and `_take_gate_cure_pass` (the peer verdict gate's) persist through this one write path with no second caller to keep in sync. `findings` is `{"<cureTaskId>": {"clause", "curedWhen"}}`, one entry per blocking finding THIS pass inserted a cure task for, keyed by that cure task's own (collision-resolved) id -- the finding's RAW `clause`/`cured_when`, read before `_repair_cure_task_criteria` may fold an overrun `cured_when` into something shorter. `preCureHead` is the same `pre_cure_head` value the caller handed to `insert_cure_tasks` (`_workspace_head_sha()`, `null` for a non-git workspace or a caller with none to give). `state.read_cure_lineage`/`cure_lineage_path` are the one reader/path -- re-read from disk on every call, never cached, so a fresh `LoopSupervisor` over the same run dir (a resume, or a second process/test) sees byte-for-byte the same record a live one wrote. At the run root, like `approach-metadata.json` beside it -- one file answers for every approach and every pass the run ever takes, never archived per-approach by `_archive_approach`. The review prompt's own "## Confirmation review" section (B.1, task 005, `LoopSupervisor._confirmation_review_context`) reads this record to list the MOST RECENT granted pass's own findings and diff range for the approach under review; the identity-based no-progress rule (B.4, task 007, `LoopSupervisor._cure_pass_targeted_roots`) reads the same MOST RECENT pass's own `findings` keys and follows each one's `tasks[].reopensCureTaskId` back through it to a finding's root cure task, to build the previous pass's own targeted-root set a confirmation review's rejection is compared against. Absent for a run that has never taken a cure pass whose blocking findings inserted at least one cure task. |
| `gateCurePasses` | object | `{"<approach>": <count>}` — v0.21 requirement A.1 (#192, task 001) per-approach cure-pass counter for the PEER VERDICT GATE's own rejection, kept under a separate key from `curePasses` so a cure pass the gate takes is never conflated with one review already took on the same approach: a `rejected` `verdict_gate` verdict whose blocking findings (`RunDir.verdict_gate_findings_json_file`) are all `curable: true`, carry no criteria-tamper finding, and which arrives while the run-wide cure-pass total (this counter's values plus `curePasses`' own, summed over every approach — `LoopSupervisor._cure_passes_for_run`) is below the configured `JobConfig.cure_pass_ceiling` (job.yaml key `cure_pass_ceiling`, default 3 — v0.21 requirement A.2, #192, task 002; a per-approach comparison against the module-level `CURE_PASS_CEILING` literal before that task, so both cure channels now spend from the same shared run-wide pot) takes the `verdict_gate -> cure` edge (cure tasks appended via the same `state.insert_cure_tasks` review's own cure pass uses, `origin: "review-cure"`) instead of archiving the approach and replanning (or ending the run on the last approach). Disk-backed and survives a resume. Absent for a run that has never taken a gate cure pass |
| `curePassCeiling` | integer | v0.27 requirement C.3 (task 010) — the LIVE, run-wide cure-pass ceiling `curePasses`/`gateCurePasses` above are both bounded by once `PATCH /config/budget`'s `curePassCeiling` knob (`ralphctl budget <run-id> --cure-passes +N\|N`) has raised or set it at least once; written by `LoopSupervisor.set_cure_pass_ceiling` every time the knob is applied. PERSISTS across a resume, unlike `maxApproaches`/`iterationTimeoutS`/the job deadline (#94 (c)+(d)): a fresh container's own startup seeds `JobConfig.cure_pass_ceiling` from `max(status.json's curePassCeiling, job.yaml's cure_pass_ceiling)` (`main._seed_cure_pass_ceiling`, mirroring `iterationsBudget`'s own `main._seed_iteration_budget`). Absent for a run whose ceiling has never been changed via the API -- job.yaml's own `cure_pass_ceiling` (default 3) is the effective figure then |
| `gateRejectionEnding` | string | v0.21 requirement A.4 (#192, task 004) — which of THREE endings the peer verdict gate's MOST RECENT rejection was: `"rejected_on_the_merits"` (no curable path existed — the gate's own mirror of A.3's mixed/incurable case), `"rejected_cure_taken"` (this rejection took the `gateCurePasses` cure-pass edge above), or `"rejected_cure_unavailable"` (every blocking finding WAS curable but the run-wide cure-pass ceiling was already spent — A.2's budget-exhausted case). This last ending takes v0.27 requirement C.1's (task 008) `verdict_gate_operator_input_needed` pause instead of the ordinary archive/replan-or-finish edges whenever `JobConfig.operator_input_needed_pause` is on (the default), unarchived and unreplanned exactly like `"rejected_cure_taken"` above, never `"rejected_on_the_merits"`'s own terminal `failed`. Written by `LoopSupervisor._step_verdict_gate` (`_gate_rejection_ending`) on every gate rejection, before the cure/replan/tail edge that rejection takes, so it survives into whichever terminal record eventually results; `ralphctl status` renders each value with its own distinct wording (`cli.main._format_gate_rejection_ending_lines`) instead of leaving all three to read as the same `state: failed` / `verdict: unverified`. Disk-backed and survives a resume, overwritten by each later gate rejection. Absent for a run that has never had a gate rejection |
| `taskAbsorptionStreak` | object | RETIRED at task 004 (v0.20.1 PRD requirement B.1, gate finding VG-002) -- nothing writes this key any more. Formerly (v0.19 requirement B.2, #153, task 011) the vigilant hand-off counter, `{"taskId": <id>, "count": <n>}`: consecutive worker iterations spent on the SAME task, each landing a real commit (git-HEAD witness) but no completion signal, since the last one that did not. At `count == task_absorption_max_iterations` (default `3`) the engine used to write `awaiting-validation` on the worker's own behalf, appending a `notes` entry on the task naming the hand-off, and route to `verify` exactly like an ordinary completion, REGARDLESS of whether any progress claim on the task was ever corroborated -- `LoopSupervisor._record_task_absorption_streak` and its call site are deleted outright, not merely widened, because the pre-existing progress-budget/forced-split path (task 016, `progressBudget` below) already governs exhaustion independently. Always absent now |

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

`llmHeaders` (v0.20 requirement J, #177, task 047) is host-merged the same
way: a plain `name -> value` map read from `<config-dir>/llm-wiring.json`
(never from `status.json`), attached by `ralphctl status`/the hub/`ui_server.
_with_llm_headers` through the ONE shared reader (`llm_profiles.
read_llm_wiring_headers`) so a header's redaction cannot read differently on
one surface than another. Every value that came from a
`${env:}`/`${file:}`/`${cmd:}` reference is already replaced with
`llm_profiles.MASK`; a literal header value (a plain ticket id) stays
visible. A wiring file written before task 047 carries no `headerRefNames`
key at all (its `${env:}` headers were already resolved into `headers` with
nothing recording that they had been references), so every value of such a
file is masked wholesale rather than guessed literal — the names still
show. Empty for a run with no `--llm-header` wiring at all.

`runSecretsMount` (v0.19 requirement E.4, #150/#152, task 020) is host-merged
the same way: `ralphctl start`/`resume` bind a mode-0700 host directory,
`run_secrets_dir(run_id)` (`<config>/<run>/run-secrets/`), at the fixed
container path `petitions.DEFAULT_RUN_SECRETS_DIR`
(`/var/lib/ralphd/run-secrets`), so a per-run petition-adjudication MAC key an
earlier episode minted (§4.3's mid-run adjudication) survives a `resume`'s
fresh container. `ralphctl status`/`doctor` add `{path, target, bound,
keyAvailable, keyFiles, fallbackCheck}`: `bound` is whether the mount was
created at all, `keyAvailable`/`keyFiles` (`run_secrets_key_files`) is the
strictly weaker fact that a non-empty key file actually survived into it, and
`fallbackCheck` — the `events.jsonl` `petition_adjudications_recorded` MAC
witness — is named whenever no key is available (a bound-but-empty mount, the
ordinary single-uid shape, reads identically to one never bound at all).

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

**Ending fields are episode-scoped.** An *episode* is one engine process over one
run dir — a fresh start or a resume. `endedAt`, `reason` and `verdict` say how an
episode *ended*, so the write that enters `running` re-bases all three (as it
already does `health` and `infraWait`) instead of leaving the previous engine's
values to be read as this one's. Without it (#30) `selfdev-v06-release` finished
`succeeded / verified` while `ralphctl status` still printed `reason: signal 15`
from a `pkill` two episodes earlier, and a *running* resumed run showed an
`ended` timestamp **before** its `started` one, because `startedAt` is rewritten
every episode and `endedAt` was not. Because the reset happens once per episode,
a terminal run's `endedAt`/`reason` remain exactly what its final episode wrote.

The superseded ending is kept, not dropped: `previousEndings` appends
`{endedAt, reason, verdict}` (oldest first, capped at the 50 most recent — the
document is rewritten in full on every status update). The terminal `state` event
in `events.jsonl` records *which* state an earlier episode reached but neither
its reason nor its verdict, so discarding the field would lose the only record of
why a run was killed two episodes ago. A crashed episode appends nothing (it left
no ending), so no ending can be recorded twice. Task 005 (#173): `termination`
was ONCE deliberately left unreset here on the theory that clearing it would
erase the only status-side trace of an operator's intent — but that made a
terminal run's `status.json` lie instead: a run stopped by `docker stop` two
episodes ago, resumed and then edited into a clean success, still read
`termination: {class: "operator", ...}` at the top level, which any consumer
of `termination` reasonably reads as "this run was aborted". The trace is not
lost, only relocated: the SAME write that appends the superseded ending also
folds that episode's `termination` (if any) into it, and archives the on-disk
`operator-termination.json` marker into `previous-endings/NN-operator-
termination.json` (§5.1) — the top-level key is then REMOVED for the fresh
episode (`state.REMOVE_STATUS_KEY`, the `update_status` sentinel that deletes a
key rather than nulling it), exactly like `endedAt`/`reason` are reset, so it
describes the CURRENT episode only. Removal rather than `termination: null`
matters because a presence test is how a `--json` consumer, the hub or a
classifier asks "was this run told to stop?": a run stopped by an operator two
episodes ago and then resumed into `succeeded / verified` carries no
`termination` key at all.

### 5.3 tasks.json

Written by `planning`, mutated by `worker` and `verify`, read by everything: the
agent-facing source of truth for what is left to do. The engine only ever adds
its own bookkeeping fields to it.

**Task 004 (requirement B, #202): two copies, one schema.** Every field below
describes the document's shape, which both copies share byte-for-byte at the
moment of any write. `RunDir.write_tasks()` lands the AUTHORITATIVE copy in
the record mount (`<config>/<run-id>/record/tasks.json`, bind-mounted at
`/var/lib/ralphd/record`; root-owned 0600 inside a root-owned 0700 directory
under the uid boundary, self-healed on every write — §5.1.1) and refreshes
this file, `tasks.json` at the run-dir root, as the agent-readable
VIEW (root-owned 0644, replaceable only by a directory-entry operation in
the agent-owned run root -- §5.1.1) -- what `ralphctl tasks`, the hub's task
table, the worker/steer prompts
and the steer hat's `context-read` tool all read, and a courtesy mirror
only: `RunDir.read_tasks()`/`read_tasks_result()` (the engine's own trust
decisions -- `GET /tasks`, `/status`'s task counts, criteria-edit detection,
the worker's own task selection) read only the protected copy, through the
same momentary root re-raise the write uses, so nothing an agent's own raw
file write could ever land in the view reaches a decision the engine makes.
Undoes back to the pre-task-004 single-file shape (agent-owned, no split)
the moment there is no uid boundary to raise into (`privsep.separated()` is
`False` -- the documented degrade, same as `write_root_owned_json`'s).

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
| `tasks[].status` | string | `pending` \| `in-progress` \| `awaiting-validation` \| `validated` \| `completed` \| `validation-failed` \| `failed` \| `skipped` \| `petitioned` \| `superseded` — every one of them declared in `engine.transitions.TASK_STATUSES` and in the task-status mini-FSM `TASK_STATUS_TABLE`/`TASK_STATUS_VIGILANT_DELTA` (rendered as a generated table and mermaid diagram in `docs/architecture.md`); `awaiting-validation`/`validated` (task 019, requirement F) are the vigilant mini-FSM's own claim/terminal pair, applied by the harness off a hat's declared transition, never written by an agent itself (see the note below the actionable-statuses paragraph); `superseded` (v0.20 requirement B.1, #156, tasks 016/019) is the one status the ENGINE forces on its own — `in-progress -> superseded`, declared in both variants and written by `state.supersede_task()` at a forced split, terminal for scheduling from the moment it lands, and reportable by no hat and rulable by no operator (see the `superseded` paragraph below the doomed-plan one) |
| `tasks[].successCriteria` | string | independently checkable, in natural language |
| `tasks[].description` | string | absent on a planner-authored task — engine-written the moment a cure task is built (`state.cure_task_from_finding`/`insert_cure_tasks`, v0.20 requirement E.2, #171, task 031): the curing finding's own `clause` + `evidence`, joined, human-readable context beside `successCriteria` rather than the criterion itself (that role now belongs to the finding's own `cured_when` when present) |
| `tasks[].priority` | int | optional scheduler hint, missing means `0` |
| `tasks[].dependsOn` | array | optional task ids that must be `completed` first |
| `tasks[].orderingOnly` | array | optional, planner-declared (GH #209 defect 3): the subset of this task's own `dependsOn` ids that are pure ORDERING edges -- sequencing alone, producing none of the evidence this task's `successCriteria` reports on. Read only by `state._lane_evidence_record_chain` through `plan_ingestion.ordering_only_dependencies()`, which is what stops a cure pass dragging such a dependency from `validated` back to `pending` merely for sitting in a re-staled evidence record's `dependsOn` (measured in `selfdev-v022`: task `012`, landed and nightly-green, re-worked because record `013`'s criteria named an invalidated module). ABSENT MEANS "every edge produces evidence" -- the conservative reading, and the reason a plan written without the field behaves identically to one written before it existed. An entry naming an id this task does not depend on is refused at ingestion (`lint_proposal`); a task that names its WHOLE `dependsOn` set this way (v0.23 requirement F.2, #222, task 023) is still ACCEPTED but earns a `lint_proposal` WARNING naming the task, surfaced through the same `PlanIngestionResult.warnings` channel requirement F.1 (task 022) made a runtime consumer of -- never a refusal, since the semantic claim itself is uncheckable by any lint, which is why the field is DECLARED by the planner rather than guessed from criteria text, and why the bound on a wrong declaration is that requirement E.3's own re-open of the RECORD is untouched -- every pass, from any terminal status, with the cure edge landing on the record itself when no verification step was re-opened to carry it. The one consequence a planner takes on by declaring an edge: a declared ordering edge already at `failed`/`skipped` is not resurrected to `pending` by that re-open either, so the re-opened record is blocked on it until a re-point clears it (a worker's `proposed_task_updates`, an operator `steer task-update`) -- and once that record is the plan's whole actionable remainder, the doomed-plan net routes the approach to `review` for a verdict (`LoopSupervisor._doomed_plan_fault`, task 007/#99) rather than letting it starve into `stagnation` |
| `tasks[].estimatedIterations` | int | planner-annotated (v0.20 requirement D.1, #157), integer >= 1: how many worker iterations the planner expects this task to take. `plan_ingestion.normalize_task_estimated_iterations()` defaults a missing one to `DEFAULT_ESTIMATED_ITERATIONS` (1) at ingestion time, once, recording a warning that it defaulted -- the same shape `tier` normalisation above uses. It feeds the task's own progress budget (`state.task_progress_budget_estimate()`, requirement A.3/D.4): `estimatedIterations + 2`, so an un-annotated task's budget is `3`. `ralphctl tasks` prints this derived number as an ` (est N)` suffix on every task's row (not only the in-progress one -- contrast `progressBudget` below, which is a LIVE usage count against the same figure, scoped to whichever task is currently in-progress) |
| `tasks[].validationAttempts` | int | failed verification verdicts so far; `VALIDATION_ATTEMPT_LIMIT` (3) forces `failed`. Reset to 0 when the engine reopens the task to `pending` for new work (a cure pass's `invalidates` walk: a lane-evidence record, its verification steps, or a reused re-take row) |
| `tasks[].priorValidationAttempts` | int \| absent | engine-written when such a reopen resets a non-zero `validationAttempts`: the count the task had spent before the reopen, kept for the audit trail |
| `tasks[].validationNotes` | string | what the verifier observed |
| `tasks[].verifyNotes` | array \| absent | v0.19 requirement A.2 (#154, task 005): a `verified` verdict's optional structured notes (each expected to be `{clause, observed, suggested}`, though only the outer array type is schema-checked), copied verbatim onto the task record by `_verify_task` in the SAME write that sets `status: "validated"` -- a verifier that judges a clause met only in form, not in substance, records the gap here rather than rejecting over it. Task 006 (requirement A.3, #155) appends the identical list, unchanged, to the run's own debt ledger (`RunDir.record_debt` -> `debt.json`/`debt.md`, §5.1) in the same pass; a `rejected` verdict or a non-list `notes` payload leaves `verifyNotes` unset and writes nothing to the ledger |
| `tasks[].failureKind` | string | engine-written on a `failed` task: `validation-exhausted` \| `requirement-unmet` (below) |
| `tasks[].criteriaFingerprint` | string | engine-written sha256 of `successCriteria` |
| `tasks[].criteriaEditedAfterValidationFailure` | bool | engine-written, sticky (§4.4) |
| `tasks[].tier` | string | optional, planner-annotated (task 013): one of `trivial`\|`normal`\|`hard` (§4.7); a missing or unrecognised value is normalised to `normal` at ingestion time, once, with a warning -- never left for a reader to default |
| `tasks[].effectiveTier` | string | engine-written (§4.7): this task's CURRENT tier, claimed equal to `tier` on first claim, escalated one rung per `validation-failed` re-claim, never de-escalated; absent on a task never yet claimed |
| `tasks[].tierLadderExhausted` | bool | engine-written, sticky (§4.7): set the moment a validation verdict comes back negative while `effectiveTier` is already `hard` -- there is nowhere left to escalate to |
| `tasks[].origin` | string | absent (planner-authored, the ordinary case) \| `"steer"` \| `"worker"` \| `"review-cure"` -- engine-written the instant a task is inserted OUTSIDE ordinary planning, never by the agent itself: `"steer"` (`TASK_ORIGIN_STEER`) the moment the `steer` hat's own edit inserts one (v0.14 requirement I, #98, task 013), `"worker"` (`TASK_ORIGIN_WORKER`) the moment an accepted `proposed_tasks` split proposal inserts one (v0.14 requirement N, #102, task 021). `state.is_steering_origin()`/`is_worker_origin()` are the one-place readers every surface that marks such a task (`format_task_status`, the CLI table, the hub row/dialog) calls, so an injected task is never indistinguishable from one the planner reasoned about. A worker-origin task also carries `tasks[].splitFrom`, the id of the task it was split out of. `"review-cure"` (`TASK_ORIGIN_REVIEW_CURE`, `is_review_cure_origin`) is the third such marker -- every task `state.insert_cure_tasks` writes, whether a cure task for a blocking finding, a re-take task for an `invalidates` path, or (#209 defect 4) a remedy task built from a REFUTED petition's `substance` remedy; the last of those also carries `tasks[].remedyFor`, the obligation text it was built from, stamped by `state.cure_task_from_finding` and by nothing else exactly as `splitFrom` is, and read back off disk as that insertion's own idempotence key. |
| `tasks[].reopensCureTaskId` | string | v0.27 requirement B.5 foundation (#274): present only on a cure task built from a finding that itself carried `reopens` -- a confirmation review's own claim (B.1) that a listed prior finding is still open. Stamped by `state.cure_task_from_finding`, read back by `_review_cure_pass_available`'s identity-based comparison on a confirmation review (B.4, task 007, `state.root_cure_task_id`), which follows this field back to the cure task's own root finding rather than ever comparing a finding id's spelling or its `clause` text. Absent on every ordinary rejection-derived cure task, on every re-take/reopened-record task `insert_cure_tasks` also writes, and on a remedy task (#209 defect 4) -- exactly the same absence discipline `tasks[].remedyFor` above already holds to for the fields it does not apply to. |

The worker picks the first `validation-failed` task, else the first
`in-progress` one, else the first `pending` task whose `dependsOn` are all
`completed`, breaking ties by highest `priority` then list order. A plain
linear plan omits both optional fields entirely. Those three statuses —
`TASK_ACTIONABLE_STATUSES` — are what "a worker iteration could still act on
this" means, in one place; `completed`, `skipped` and `failed` are resolved as
far as the worker loop is concerned, whether or not the requirement behind them
was met.

**The task-status mini-FSM (task 019, requirement F, #60 pillar 3).** A task's
`status` is a small declared transition table of its own
(`engine.transitions.TASK_STATUS_TABLE` / `TASK_STATUS_VIGILANT_DELTA`,
mirroring the run's own step-state table, §4.1) — a status change is something
the *harness* applies at a hat's step transition, off that hat's own reported
outcome, never a report card an agent writes into the record directly. The
vigilant variant inserts `awaiting-validation` as the worker's claim state (in
place of claiming `completed` outright) and `validated` as the terminal verify
grants — both join `TASK_COMPLETION_STATUSES` (`validated` only) and stay out
of `TASK_ACTIONABLE_STATUSES` (`awaiting-validation` is waiting on verify,
`validated` is done); the non-vigilant variant never produces either. A status
reported that the declared table does not permit for the task's current status
is refused the same way any other malformed hat payload is (§4.3's contract
enforcement) — re-prompted, then scored an agent fault once retries run out,
not silently applied "just in case". As of task 019, the mechanism was proven
end to end only through the generic hat fallback (§4.1's synthetic/future
states); `worker`'s own per-task claim and `verify`'s own `verify`-contract
path (§4.3) were deliberately left untouched there. Task 116 is that
cutover: `worker`'s reported `{"verdict": "complete", "task_id": ...}` now
goes through this SAME `_apply_reported_task_status` mechanism, moving the
assigned task to `awaiting-validation`/`completed`; `verify`'s applied
`validated`/`validation-failed`/`failed` already went through it from task
019 itself (verify never had a separate, legacy write path to retire). The
only claim left outside this table is the whole-plan
`"plan_complete"` verdict (§4.3, retiring the `<promise>COMPLETE</promise>`
sentinel, v0.14 requirement O), which ends the worker loop
rather than moving any one task's status and so has nothing in
`TASK_STATUS_TABLE` to route through.

**The two meanings of `failed` (task 024, #33).** `failed` used to mean both
"a verifier judged this requirement unmet" and "the engine consumed the last of
this task's validation rounds", so a task record could not be read without a
human explaining it. The distinction is *not* a sixth status — `skipped` already
exists and every surface that counts statuses would have to learn one — but a
single engine-written label beside it, `failureKind`, with two values:

| `failureKind` | written when | means |
|---|---|---|
| `validation-exhausted` | `_verify_task()` sees `validationAttempts` reach `VALIDATION_ATTEMPT_LIMIT` (§4.4) | the validation rounds are used up; whether the requirement is met is *unknown* |
| `requirement-unmet` | an agent marks a task `failed` itself | a judgement that the requirement was not met |

`state.task_failure_kind()` is the one reader, and it is a function rather than a
field read because of migration: a `tasks.json` written before this label existed
has no such key, so the kind is **derived** from the evidence already on the
record — at or past `VALIDATION_ATTEMPT_LIMIT` recorded rounds reads as
`validation-exhausted` (the only way the engine itself ever wrote `failed`),
anything else as `requirement-unmet`. An unrecognised value is read the same way,
so a garbled label can neither crash a reader nor invent a third meaning.

**Why the vocabulary is load-bearing: a `failed` task used to be able to strand a
whole run.** `worker.md`'s completion signal recognises only `completed` and
`skipped`, so with a `failed` task in the plan the whole-plan claim (§4.3) is
never legitimate — and from task 046 (#55) the engine refuses one rather than
taking the worker's word for it (§4.3) — the run never reaches `review`, and the
stagnation guard
eventually replans the wave against an already-finished repo — the `043d`
incident, four iterations and two steering notes, resolved only by an operator
hand-editing the record. The worker's two honest resolutions (carve the residual
gap into a new task, or relabel `skipped` with a justification that does not
claim the criteria were met) are in `worker.md`, and both change `tasks.json`.
The `failed -> skipped` relabel is itself a DECLARED CONTRACT-CHANNEL move, not
a direct edit: the worker reports `{"task_status": "skipped", "task_id": ...,
"justification": ...}` alongside its ordinary verdict in the same last-line JSON
reply, and the harness -- never the worker -- applies the move and writes the
justification into that task's own `notes` field in the same write, exactly as
the per-task and whole-plan claims above go through `_apply_reported_task_status`
rather than a raw `tasks.json` write. Relabelling to `skipped` never manufactures
an unmet dependency's satisfaction: `skipped` is not in
`TASK_DEPENDENCY_SATISFIED_STATUSES` (`completed`, `validated`) any more than
`failed` was, so every task whose `dependsOn` names the relabelled id stays
blocked exactly as before the relabel -- clearing that takes a `dependsOn`
re-point, or a planning/replan pass. The engine writes a re-point itself on two
paths that need no agent at all: `state._repoint_dependents_onto_cure_task`
(from `insert_cure_tasks`, whenever a blocking finding names the dead task) and
the CONFIRMED-petition path. An operator's `steer task-update` writes one too,
and that is what both of #209's own observed incidents were cleared by. The
residual none of those reaches -- a doomed dependent with no cure task to be
re-pointed ONTO, whose replacement is a sibling that already exists -- is the
worker's own optional `proposed_task_updates` contract field (#209 defect 7):
`dependsOn` only,
never a status, a title or a `successCriteria`; standing granted solely by
`state.task_blocked_by`'s own derivation over a `pending` task, so the harness
decides what is unschedulable and the worker only repairs what it has already
called so; drops only `failed`/`skipped` edges, and at least one of them, since
an entry that drops none leaves the task exactly as blocked as it found it; the
replacement may name no `failed`/`skipped` task at all; mandatory `reason`,
recorded on the task's own
`notes` and on `task.dependency_repointed`; no cycle and no unresolved id (the
shared `plan_ingestion.validate_dependency_edges`, whose one rejected edge
refuses this caller's WHOLE array rather than being dropped from it); bounded
per approach by `worker_repoint_budget_per_approach`; and applied by the
harness through `state.apply_worker_dependency_repoints`, never by the agent's
own `tasks.json` write. An UNGUARDED guess at the graph from a worker iteration
is still exactly what that guard set exists to prevent -- the lint can check
the graph's shape but never the semantic claim that the new edge carries the
dead one's work, which is why the `reason` and the audit event are mandatory,
and why a refused or over-budget re-point leaves the doomed set where it stood,
reported in the notes file and nothing more.

So the engine gives the worker exactly one iteration to apply one of its two
honest resolutions above and then acts itself:
`LoopSupervisor._unresolved_failures()` reports the `failed` tasks of
a plan with nothing actionable left in it, and the worker loop routes to `review`
— entry to review only, the reviewer still decides the verdict — instead of
stagnating. Both kinds of `failed` route, because both block the worker's
signal identically and the reviewer is the right judge of either. Task 003
(#166) reuses this SAME `_unresolved_failures()` check one layer up: a
`plan_complete` claim (§4.3) refused for no reason other than this exact
shape -- nothing else actionable, at least one `failed` -- is routed to
`review` with `blocked_with_failures` directly out of the contract-dispatch
site itself, never through `enforce_contract`'s re-ask ladder, so the same
plan shape reaches the same verdict whether the worker noticed it (an
honest `plan_complete` attempt) or simply ran out of iterations first (the
end-of-loop routing above).

**A doomed plan gets the same routing, one hop earlier (v0.14, requirement D, #99).**
`_unresolved_failures()` just above is itself dependency-blind: `state.task_is_actionable()`
reads `status in ("pending", "in-progress", "validation-failed")` with no regard for
`dependsOn`, so a `pending` task whose own dependency chain is dead (unsatisfiable per
`state.task_blocked_by()`'s doomed-set fixpoint) still reads as actionable — the routing
above never fires, the plan starves through three no-progress worker iterations, and it
lands on `stagnation_exhausted` (§5.3) with the wrong cause recorded: the plan did not
stagnate, it became unreachable. `LoopSupervisor._doomed_plan_fault()`, checked from the
same worker-dispatch site the routing above lives at and BEFORE any worker iteration for
the pass is dispatched, asks a narrower question than `blocked_by` alone: does the doomed
set cover the ENTIRE actionable remainder, not merely a subset? A doomed subset with real
work left is untouched by this — the ordinary per-task blocked-notes/stagnation machinery
still governs exactly as before this requirement. When it does cover the whole remainder,
the loop emits a `log` event (`level: error`) carrying `doomedTaskIds` (the full doomed
set, not only its actionable subset), `deadAncestorIds` (every `failed`/`skipped` task
reachable by walking `dependsOn` back from a doomed task — the actual dead root cause,
as opposed to `blocked_by`'s own per-task lists, which stop at the first doomed hop) and
`planUnverifiable: true`, then takes the declared `doomed_plan_unverifiable` edge straight
to `review` for a verdict — reusing the mechanism just above rather than a
dependency-graph replan or a terminal degraded state, because a doomed plan is exactly
the "work with a hole" shape `blocked_with_failures` already hands a reviewer.

**A `petitioned` dependency is held, not doomed (v0.19 requirement C.1, #149,
task 013).** `state.task_blocked_by(..., include_petitioned=True)` widens the
harness-derived `taskBlockedBy` map (`{task id: [dead/doomed ancestor ids]}`,
above) with a second, unrelated cause: a task whose `dependsOn` names a
`petitioned` task DIRECTLY (one hop only, never propagated through the doomed
fixpoint) is included too, with that ancestor as its cause. `petitioned` is
deliberately never added to `dead`/`doomed` itself -- an open petition can
still resolve (REFUTED leaves the dependency exactly where it is; CONFIRMED
moves it to `skipped` and re-points dependents) -- so treating it as
permanently dead here would make `_doomed_plan_fault`'s "nothing can ever move
this plan again" verdict fire on a plan that is merely holding for an
adjudication. `include_petitioned=True` is opt-in, `False` by default, so doom
detection itself is untouched; the four display surfaces that DO pass it are
`ralphctl tasks`, `GET /tasks`'s `TasksRead.blocked_by`, the hub's
`ui_server._with_task_blocked_by`, and `worker.md`'s own prose naming the same
map for the fourth (a worker reading its own blocked dependents).

**`superseded` unblocks its own dependents and never strands plan completion
(v0.20 requirement B.1, #156, task 016).** A forced split -- triggered when a
task's own progress budget (§5.2's `progressBudget` field, above) is spent
with no `proposed_tasks` split arriving to relieve it -- moves the exhausted
parent to `superseded` and inserts its child(ren) with the parent's own
`dependsOn` inherited onto every one of them (task 016's fold,
widened by task 017 to resolve a WORKER-proposed split's `dependsOn` against
the live `tasks.json`, not only the batch just proposed). `superseded` joins
`TASK_DEPENDENCY_SATISFIED_STATUSES` alongside `completed`/`validated` --
deliberately, not `TASK_STATUS_TABLE`'s wider membership, this one tuple is
about what "downstream is unblocked" means -- so a task whose `dependsOn`
names a now-`superseded` parent reads schedulable, never doomed the way a
`failed`/`skipped` ancestor would leave it: the parent's obligation moved to
the child, it did not die. It joins `TASK_COMPLETION_NONBLOCKING_STATUSES`
for the identical reason `awaiting-validation` is in that tuple: a
steering-origin task that gets force-split must not hold the whole-plan exit
gate open forever waiting for a status (`superseded`) that has no further
transition to reach. And it joins `TASK_TERMINAL_STATUSES` alongside
`petitioned` -- terminal here means "nothing further moves it on its own",
not "the plan is done" -- so `state.task_is_terminal()` and every counts
surface treat a `superseded` parent as settled without ever reading it as a
failure. `superseded` is a declared TARGET in the task-status mini-FSM --
`in-progress -> superseded`, present identically in both variants (a spent
budget is not a verification concern), which is what makes the engine's own
write legal against the same table every other status move is checked
against and legible to every reader of it (`ralphctl doctor` diagnoses a
`superseded` record as a known status; `docs/architecture.md`'s generated
task-status table and mermaid diagram both show the edge). Declared is not
claimable: `transitions.ENGINE_FORCED_TASK_STATUSES` names `superseded`, and
two refusals derive from that one tuple -- `task_status_report_errors`
refuses any REPORTED `task_status: "superseded"` (so neither a hat's contract
channel nor `apply_task_status_change`'s operator/steer callers can take it),
and `ralphctl amend --status` validates against
`OPERATOR_RULEABLE_TASK_STATUSES` (`TASK_STATUSES` minus exactly that tuple)
so a status ruling naming it is rejected on the host before a file is
written. The engine forces this move itself, at the one moment a progress
budget is spent with nothing to relieve it, and an operator never rules a
task superseded by hand.

**The every-approach pause widening reuses the SAME declared FSM edge
(v0.20 requirement B.3, #156, task 018).** `docs/architecture.md`'s
generated transition table already carries `worker -> paused` on guard
`stagnation_last_approach` (task 012, #153) for a stagnant LAST approach; task
018 widens the guard's own *condition*, never the table row or the mermaid
arrow it renders as, to any approach that PROGRESSED (a workspace commit, or
a `superseded` task left behind) but still stalled three iterations running,
and to a SECOND budget exhaustion -- a `superseded` parent's own split child
spending its own `progressBudget` too pauses instead of forcing a further
split, naming that child on the `operator_escalation` event's
`exhaustedSplitChild` field (§5.4, below) rather than splitting the plan a
second time for the same stall. `_step_paused` still routes
`stagnation_last_approach` straight back to `worker` over the same approach
in both cases -- the resume path is exactly what task 012 already declared,
unchanged by which condition triggered the pause. Because the guard's NAME
now under-states what it fires on, the widened condition is itself declared
as data (`transitions.GUARD_CONDITION_NOTES`, task 019) and rendered into
`docs/architecture.md`'s generated step-state table block under "Guards whose
declared condition is wider than their name" -- so the generated table states
the every-approach condition instead of leaving a reader to infer it from a
row whose guard is still spelled `stagnation_last_approach`.

**No status for "landed short of the letter" (v0.14, requirement F, #99 suggestion 3, #51).**
Issue #99's third suggestion was a status that satisfies dependents while recording a
shortfall -- the case where the substantive work landed but one stated property
did not, which `skipped` overstates as loss and `completed` overstates as full
delivery. The decision is to leave `TASK_STATUSES`, `TASK_STATUS_TABLE`,
`TASK_DEPENDENCY_SATISFIED_STATUSES` and `state.task_counts()`'s mapping
unwidened for THAT case (the vocabulary was eight terms when this was decided;
`petitioned` (#114) and `superseded` (#156) have since joined it for two
different reasons, neither of them this one), and to name `ralphctl amend --status` (requirement E,
`state.apply_amendment_status_rulings()`) as the sanctioned resolution instead: an
operator who judges a task's shortfall honest, not disqualifying, rules it
`completed` (or any other legal transition) through that channel, in the
operator's own words in `validationNotes`, without the engine ever having to
guess the distinction from an agent's report. Adding a term for it would not stop
at the enum -- `TASK_STATUS_TABLE`'s transitions, the counts mapping, `worker.md`'s
and `verify`'s completion contracts, and every hub/CLI label that renders a
status would all have to move with it, on the strength of one nine-issue wave's
need. That blast radius is exactly how a vocabulary ends up with two words for
the same thing, which is what #51 was filed to prevent by asking that this
choice be made explicitly rather than left implicit. So the widening is
deferred, not merely postponed by omission: E already gives an operator a way
to say "this landed short of the letter, and I am ruling on it" without a new
word, and a future wave that finds E insufficient in practice -- not in theory --
is the one that should reopen this paragraph.

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

**Counting both meanings of `failed` (task 025, #33).** A failure kind is not a
status, so it gets no status key of its own: both kinds keep counting under
`failed`, and the breakdown travels as the **sub-counts**
`failedValidationExhausted`/`failedRequirementUnmet` beside them
(`state.TASK_COUNT_SUBKEYS`, present only for the kinds actually in the plan).
Two invariants hold for every plan, including one written before the label
existed (the kind is derived, above):

* the status keys sum to `total` — what "the tallies add up" means, and the
  reason no surface had to learn a sixth status;
* the sub-counts sum to `failed`.

The *wording* is one vocabulary too (`state.TASK_STATUS_LABELS` /
`format_task_status`): `1 failed (validation-exhausted)` in a tally, `failed
(validation-exhausted)` on the task itself. A summary renders the kinds
*instead of* a plain `2 failed` whenever they are known, so one failed task is
never counted twice in the same sentence; plain `failed` remains the fallback
for a counts dict carrying no sub-counts (a pre-v0.7 engine's `GET /status`),
and it is a *trouble* flag under both meanings — `ralphctl runs`' `⚠` and the
hub's TASKS cell mark a plan that gave up, not just one mid-flight. The
per-task half of the same answer is `state.task_failure_kinds()`, published as
`taskFailureKinds` by every surface that serves the plan itself (`GET /tasks`,
`ralphctl tasks --json`, the hub's run-detail payload) so no renderer
re-implements the migration rule — `app.js` included.

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
| `planning.warning` | `approach`, `warning` | v0.23 requirement F.1 (#222, task 022): the accepted-proposal path (`_step_planning`) surfaces one entry of `plan_ingestion.PlanIngestionResult.warnings` -- until this task accumulated but never read by anything at runtime, so `normalize_task_tiers`' degenerate-all-`hard`-tier warning and `normalize_task_estimated_iterations`' defaulted-estimate warning were invisible outside a unit test. One `log` event (`level: "warning"`) plus one of this event per warning string; a proposal that produces no warning emits neither |
| `iteration.start` | `number`, `phase`, `model` | an iteration's agent process is about to start |
| `iteration.attempt_archived` | `number`, `attempt`, `path`, `files` | a reused slot's earlier (crashed) attempt was moved to `iterations/NNNN/attempts/NN/` before the new one wrote (§5.5) |
| `iteration.end` | `number`, `phase`, `exitCode`, `interrupted`, `sawComplete`, `sawVerified`, `error`, `faultClass` | that iteration finished |
| `task` | `taskId`, `oldStatus`, `newStatus` | a task's status changed in `tasks.json` (polled while the agent runs, so it lands mid-iteration) |
| `signal` | `signal` (`COMPLETE` \| `VERIFIED` \| `taskVerified`), `taskId` | a sentinel was accepted |
| `progress_claim` | `taskId`, `corroborated`, `deltaSize` | a worker's `progress` block (§4.5) was accepted alongside `no_verdict` -- `corroborated` iff `commit`/`file` evidence lined up with a nonzero workspace-fingerprint delta, or `external` evidence was the first of a consecutive run of tree-delta-free claims on that task; `deltaSize` is `_progress_claim_delta_size()`'s own count of changed paths (task 012, PRD requirement A.2, #156) |
| `deferred_wait` | `taskId`, `until`, `deadlineAt`, `boundedBy` (`"until"` \| `"deadlineAt"`), `sleptUntil`, `waitedS` | nothing is runnable and at least one `pending` task carries `deferredUntil` (§4.5's `"deferred"` verdict, task 021) -- the engine sleeps until the earliest `deferredUntil`, bounded by the job's own `deadlineAt`; `boundedBy` names which of the two the sleep actually stopped at (v0.20 requirement C.1, #156, task 022) |
| `steering.received` | `file` | a steering message was stored |
| `steering.consumed` | `file`, `iteration` | an actionable phase finished cleanly with it (§4.6) |
| `infra_wait` | the whole `infraWait` payload plus `backoffS` | a backoff wait starts |
| `infra_retry` | `phase`, `attempt`, `maxAttempts`, `error`, `noTrafficTimeout`, `instantFailure`, `backoffS`, `waitedS`, `budgetS` | an attempt classified as an infra fault |
| `infra_retry_now` | `phase`, `attempt`, `error`, `source`, `message` | the operator woke a backoff wait |
| `infra_recovered` | `health`, `infraWaitTotalS` | an iteration reached the model again |
| `deadline_extended` | `phase`, `attempt`, `waitedS`, `infraWaitTotalS`, `deadlineAt`, `reason` | a finished wait pushed the deadline out |
| `reflect_infra_delay` | `phase`, `delayS`, `error`, `budgetS` | reflect waits before its first attempt because the job just died on an infra fault |
| `reflect_done` | `ok`, `error` | the reflect phase's verdict |
| `reflect_tombstone_cleared` | `path` | a successful reflect attempt removed a stale `reflection/FAILED.md` left by an earlier one (§12.2) |
| `reflect_skipped` | `attempted`, `signal`, `reason` | a signal was taking the engine down, so the reflect phase rendered no verdict (§8.4) |
| `budget_changed` | `field`, `previous`, `iterations`\|`maxApproaches`\|`iterationTimeoutS`\|`jobDeadlineEpochS`\|`curePassCeiling`, `delta`, `iterationsUsed` (`iterations` field only), `source` | a live-engine budget knob was changed in flight via `PATCH /config/budget` -- task 003/#94 (a) widens `field` past `iterations` alone, task 010 (v0.27 requirement C.3) adds `curePassCeiling` |
| `log` | `level`, `message` | anything the loop wants an operator to see: stagnation, batching violations, instant-failure streaks, abort diagnostics |
| `operator_escalation` | `approach`, `reason` (the escalating pause's own name -- `"rejected_incurable"` \| `"rejected_last_approach"` \| `"rejected_repeated_signature"` \| `"review_contract_unrepaired"` \| `"review_findings_unusable"` \| `"stagnation"` \| `"unschedulable_dependency_cycle"` \| `"unresolvable_dependency_edge"` \| `"operator_input_needed"` \| `"verdict_gate_operator_input_needed"`; the middle two of the dependency pair carry no `approach`, and `"operator_input_needed"`/`"verdict_gate_operator_input_needed"` additionally carry `replanAvailable`), `outstandingTasks`, `escalation`, plus on a `"stagnation"` pause `approachProgressed`, `awaitingValidation` (every task resting in `awaiting-validation`, each `{id, validationAttempts}`, also listed in the `escalation` text) and — only for a second budget exhaustion — `exhaustedSplitChild` (`id`, `splitFrom`, `used`, `budget`), on a `"review_contract_unrepaired"` or `"review_findings_unusable"` pause `contractErrors` (the verbatim validation errors review never repaired -- the whole diagnosis, camelCase to match `channelErrors` on the repeated-signature escalation), and -- v0.23 requirement A.7 (#224, task 017; cure task cure-01-04, F04, widens the scope below) -- six more fields on EVERY `"operator_input_needed"` pause (condition 1's plain archive-before-pause gate, a reviewer-requested gate whether or not a cure pass is even available, and the driven condition-2 cure-reopen gate) and on EVERY `"rejected_repeated_signature"` pause (task 007, v0.26, #269: not only one taken as trigger condition 3 -- a pause handed no `cure_preview` builds these off `LoopSupervisor._effective_cure_preview`'s own fresh `state.cure_pass_preview` walk over the current tasks and its blocking findings) -- never on any OTHER pause reason in this list: `taskIdsValidatedToPending` (`state.cure_pass_preview`'s own `reopenedTaskIds`, `[]` when nothing would be reopened), `insertCount` (that same preview's `insertCount`, still counted even when nothing would be reopened), `costSpentThisApproach`/`tokensSpentThisApproach` (`usage.byApproach[str(approach)]`'s own `state.format_cost` text and `totalTokens`, both possibly `null` for an approach with no usage yet), `approachOfMax` (`"approach <n> of <N>"`), and `hubLink` -- `JobConfig.hub_base_url` (optional, `RALPHD_HUB_BASE_URL` or a job.yaml `hub_base_url:` key, unconfigured by default, same shape as `notifier_url`) joined with `"/#/run/<runId>"` when configured, else the hub's own default loopback host with no port (`"http://127.0.0.1/#/run/<runId>"` -- the engine has no channel to learn the port `ralphctl ui --port` chose), never the bare relative fragment F04 named as not actionable from a phone notification. Task 007 (v0.26, #269) adds a SEVENTH field to that same widened set, `nextStepIfUnanswered` -- `"cure_pass"` \| `"archive_and_replan"`, `LoopSupervisor._review_cure_pass_available(approach)`'s own fresh, read-only verdict at the moment of pausing (never inferred from `taskIdsValidatedToPending`/`insertCount` above -- a reviewer-requested gate or a ruled re-park can be curable while reopening nothing) -- so the hub's own `parkedQuestion.consequence` (below) can name which of the two an unanswered pause is about to take without re-deriving it. Task 011 (v0.27 requirement C.4, #275): the six-item preview itself now names the edge `nextStepIfUnanswered` names, not the raw findings' own reopen prediction, whenever those two disagree -- a rejection that WOULD earn a cure pass (`LoopSupervisor._review_rejection_curable_ignoring_ceiling`) but finds the run-wide `cure_pass_ceiling` already spent (`LoopSupervisor._cure_pass_ceiling_spent`) reads `taskIdsValidatedToPending: []`/`insertCount: 0` here, never the real reopen/insert numbers a cure pass over the identical findings would have produced, and the `escalation` text names the live lever that would change that outcome (`ralphctl budget <run> --cure-passes +N|N`, v0.27 requirement C.3, task 010). A rejection that was never curable at all (mixed/incurable findings, a criteria-tamper finding, a superset repeat) is untouched: its own preview already read the honest partial reopen/insert count `state.cure_pass_preview` computes off the findings themselves, and raising the ceiling would not change its outcome, so no budget note is appended for it. `"verdict_gate_operator_input_needed"` now carries this SAME six-item payload too (before task 011 it carried none): the gate's own C.1 pause fires ONLY on the ceiling-spent edge, so its preview is always the zeroed `taskIdsValidatedToPending: []`/`insertCount: 0` and its text always names `--cure-passes`, unconditionally. The rendered `escalation` text for every one of these pauses also names each blocking finding's own `cured_when` verbatim, beside its `clause`/`evidence` (cure task cure-01-04, F04) | the engine paused itself (§4.6): the last approach was rejected on all-curable findings with its cure pass spent, or (v0.19 requirement B.3, #153, task 012) the last approach's worker-stagnation guard would otherwise fail the run silently — same event shape, `reason` is the only thing that tells the two apart. v0.20 requirement B.3 (#156, task 018) widens the `"stagnation"` pause to EVERY approach that progressed (`approachProgressed: true` — a workspace commit somewhere in the approach, or a `superseded` task in the plan) and to the second budget exhaustion: a `superseded` parent's own split child spending its budget pauses instead of forcing a further split, and names that child on `exhaustedSplitChild`. GH #209 defect 2 adds `"review_contract_unrepaired"`: review's own contract ladder never got a readable verdict claim out of the hat, so the run holds for an operator with the approach unarchived instead of scoring a rejection against a judge that never spoke (§4.2). v0.23 requirement C.2 (#219, task 011) adds `"review_findings_unusable"`: the discriminating sibling -- review's own verdict claim WAS legible, but its REVIEW CONTRACT CHANNEL never became usable across the ladder -- the findings sidecar, or (requirement C.3, task 012) a stray/wrong-typed field elsewhere in the payload's own closed schema -- so the run holds the same way instead of scoring a rejection off a channel the engine itself cannot trust. v0.23 requirement A.1 (#224, task 014) adds `"operator_input_needed"`: ON by default (`JobConfig.operator_input_needed_pause`), any ORDINARY rejection that would otherwise reach `_archive_approach` -- whether the next step would have been a fresh `replan` or the terminal `rejected_no_approach_left` outcome -- holds instead, with `replanAvailable` recording which of the two it preempted, so an operator can answer before that spend happens. v0.27 requirement C.1 (task 008) adds `"verdict_gate_operator_input_needed"`: the peer verdict gate's own mirror of `"operator_input_needed"`, narrower on purpose -- it fires only on a `verdict_gate` rejection that would have earned the gate's own cure-pass edge if the run-wide `cure_pass_ceiling` were not already spent, ON by default through the SAME `JobConfig.operator_input_needed_pause` flag, with `replanAvailable` naming which of `replan`/`rejected_no_approach_left` it preempted. v0.23 requirement A.3 (#224, task 017) adds two MORE ways an ordinary rejection holds instead of spending, sharing ONE discretionary gate per approach with each other (never with condition 1's own unbudgeted gate above): trigger condition 2 -- a cure pass that would re-open task(s) already `validated` (`state.cure_pass_preview`, never a second copy of `state.invalidates_reopen_walk`) -- takes the SAME `"operator_input_needed"` pause condition 1 does, before `LoopSupervisor._take_cure_pass` ever runs; trigger condition 3 -- a rejection whose blocking-clause signature repeats the immediately preceding approach's own recorded one -- takes the pre-existing `"rejected_repeated_signature"` pause (task 016), but ONLY once this shared gate is available; whichever of the two fires first in an approach spends it, and the other, firing later in the SAME approach, falls through to its ordinary edge (the cure pass taken silently, or the repeated signature recorded and the approach archived/replanned) instead of a second pause. Both driven firings carry the six-item payload named on the left. Task 001 (PRD requirement A, #269): for `"operator_input_needed"`/`"rejected_repeated_signature"`, this event's own `escalation` field always keeps the FULL text above, unedited; the notification actually POSTED for either reason is a separate, bounded DIGEST (§4.6) that never carries a finding's evidence. Task 002 (#269 follow-on): each finding's own `resolve with:` `ralphctl amend` invocation -- present on the FULL text of every pause reason in this list, never only the two the digest above covers -- now names the finding by id (`F<n>`, matching the `Finding <n> (...)` header immediately above it) plus its own `clause`, instead of repeating that finding's `evidence` a second time inside the `--waive` argument; the evidence still appears exactly once, on the finding's own `evidence:` line, never inside the resolve command too |
| `operator_ruling_honoured` | `approach`, `signature`, `rulingSeq`, `rulingFile`, `verdict` (review's own raw `verdict` string for this pass) | v0.23 requirement A.4 (#224, task 015): once `operator_input_needed` (above) has parked a rejection and review is re-asked over the SAME approach, a LEGIBLE fresh verdict (never on a contract-unrepaired/findings-unusable pass, which never rendered one) that lands over a signature a recorded operator ruling (task 013's `ralphctl rule`/`POST /rule`/hub textarea, `LoopSupervisor._ruling_honoured_for_approach`) answers is named here — the auditable trace of which ruling review actually honoured, whatever the fresh verdict turned out to be. Plain equality on the same content-derived signature `_repeated_rejection_signature` already compares; the ruling's own TEXT is never read, parsed or classified to decide this |
| `repair` | `action`, plus the names/ids involved | appended host-side by `ralphctl repair`, which records only names, never values |
| `workspace_version_mismatch` | `workspaceVersion`, `engineVersion` | v0.20.1 requirement E.3 (#190/#191, task 017): emitted once per process start, from `LoopSupervisor.__init__`, when the workspace's own recorded `__version__` (`src/ralphd/__init__.py` under the workspace, read as text, never imported) differs from the running engine's -- alerted through the same `self.notifier` path an `operator_escalation` pause already uses (D.1/E.5), never a second channel. Silent (no event, no alert) when the workspace's version literal is unreadable/absent or matches exactly |
| `notification.escalation_undelivered` | `reason` (the escalating pause's own `reason` string, same vocabulary as `operator_escalation` above) | v0.26 requirement A.4 (task 004, #269): an operator-escalation notification never reached the notifier after `LoopSupervisor._ESCALATION_DELIVERY_MAX_ATTEMPTS` (3) delivery attempts with bounded backoff between them (§4.6). Each attempt still emits its own honest `notification.sent`/`notification.failed` event (task 009, requirement B.1, #197, unchanged); this is the ADDITIONAL, distinct signal that the escalation itself was never delivered at all, so a reader watching only `notification.failed` cannot mistake a transient blip for a pause nobody was ever told about |
| `approach.cure_pass` | `approach`, `curePasses`, `taskIds`, `findings`, `blockingBefore`, `blockingAfter`, `passNumber` | a `rejected` review whose blocking findings are all curable took the cure-pass edge (v0.18 requirement C.1, #141) rather than an ordinary replan -- `taskIds` is `state.insert_cure_tasks`'s own return value verbatim: the appended cure tasks, plus (v0.20 requirement E.3, #167, task 032) one re-take task id per `invalidates` path with no existing record task and no re-take task already appended for that path in this approach (#209 defect 3: a path that already has one names that SAME id again, re-opened at the new cure's tree, never a duplicate row), plus the ids of any lane-verification/lane-record task pair `insert_cure_tasks` re-opened instead (a path that IS the plan's own final lane-evidence record -- and, since the same defect, a task the engine itself authored, `origin: "review-cure"`, is never recognised as that record nor as its verification step, so a later pass no longer re-opens an earlier pass's landed cure task; #209 defect 3's RESIDUAL makes it not always a pair -- a lane-record id can be named with NO verification id beside it, when every one of that record's own evidence steps is declared `tasks[].orderingOnly`, and then the cure task joins the RECORD's `dependsOn` rather than a verification step's while the declared step keeps its terminal status and is not named at all) -- `findings` the blocking findings they were appended for, `curePasses`/`passNumber` are the same new per-approach count (`passNumber` added by v0.20 requirement E.1, #167, task 030, naming which of the run's up-to-`JobConfig.cure_pass_ceiling` passes this is — job.yaml key `cure_pass_ceiling`, default 3, a run-wide budget across both cure channels since v0.21 requirement A.2, #192, task 002, the module-level `CURE_PASS_CEILING` (3) literal before it), and `blockingBefore`/`blockingAfter` (also task 030) are the blocking clause sets (sorted, by `clause`) the immediately preceding granted pass recorded and this pass itself recorded -- `blockingBefore` is `[]` on an approach's first pass. **Since v0.23 requirement B.4 (task 008, #221), this snapshot is no longer everything this pass put into motion, permanently**: a record/verification pair named here because the walk re-opened it on the finding's `invalidates` prediction can be un-reopened AFTER this event was written, once every cure task that shared its `preCureHeads` baseline reaches `completed`/`validated` and their combined diff never touches the record's cited path (`task.un_reopened`, `source: "cure-reconciliation"`) -- so a reader reconstructing "what is still pending because of this pass" from `taskIds` alone, without also reading the later event stream for a matching `task.un_reopened`, can overcount |
| `task.criteria_repaired` | `taskId`, `oldCriteria`, `newCriteria`, `lints`, `source` (`"plan-ingestion"` \| `"cure-insertion"`), plus `hat`/`attempt` on the plan-ingestion writer | #209 defect 1: one of the two LEXICAL ingestion lints (`unenumerated-quantifier`, `clause-count-overrun`) had already computed a lossless, lint-clean rewrite of the text it objected to, and the harness APPLIED it (`plan_ingestion.repair_criteria`) instead of refusing the whole proposal and re-asking the hat. Two writers, one event name and a `source` field naming which acted (the same shape `task.dependency_edge_rejected` uses): `"plan-ingestion"` for a planner's own `successCriteria`, pre-pass, emitted per attempt whether or not some other lint then refused the same proposal; `"cure-insertion"` for a curable finding's `cured_when` at the point `state.insert_cure_tasks` turns it into a cure task's criteria. Never emitted when the computed rewrite is one of the two ladder fallback constants -- those are advice and one of them replaces the whole criterion, so the plain refusal stands instead |
| `review.verdict_recomputed` | `approach`, `claimed_verdict` (always `"rejected"`), `computed_verdict` (always `"verified"`), `residual_count`, `residual_findings` (the residual entries verbatim) | Task 002 (requirement A.2, #110), DoD predicate 3: review's own payload claimed `verdict: "rejected"` but its structured findings channel computed `verified` under `findings.compute_verdict` -- i.e. not one finding is `severity: "blocking"` -- so `LoopSupervisor._review_verdict` recorded the correction and the approach was treated as verified on the hat's OWN findings rather than on its over-cautious word. One-directional, like its sibling below: the engine only ever corrects a refusal INTO a success on the findings, never a success into a refusal. Snake_case payload keys, the spelling that sibling then copied |
| `review.verdict_synonym_normalised` | `approach`, `claimed_verdict`, `normalised_verdict` | #209 defect 2: the review payload's `verdict` was one of `transitions.NEGATIVE_VERDICT_SYNONYMS` (`fail`, `failed`) -- a word outside `HAT_SCHEMAS["review"]`'s two-word enum that plainly meant this contract's own NEGATIVE verdict -- so `_review_contract_ladder` rewrote it to `rejected` in place before its own early-exit check, and the reply became a real rejection with no re-ask and no `contract.violation` instead of exhausting the ladder. The audit trail keeps what the hat LITERALLY said: `claimed_verdict` is the word replaced. Snake_case payload keys deliberately, matching its sibling `review.verdict_recomputed` in the same namespace. One-directional by construction -- no synonym maps to `verified`, so a wrong guess can only ever land on the negative word. What it lands on is then treated exactly like a literal `rejected`, which still includes `_review_verdict`'s recorded correction of an over-cautious refusal (`review.verdict_recomputed`: a `rejected` claim whose findings are ALL `severity: "residual"` is recomputed to `verified`) -- so a hat that writes `fail` while filing only residual findings can still reach `succeeded`, on its own findings, exactly as it would have with the compliant word. Making the illegal word BINDING where the legal one is correctable would be a second, undeclared verdict vocabulary |
| `review.finding_downgraded` | `approach`, `clause`, `evidence` | v0.27 requirement B.3 (#274, task 006): on a CONFIRMATION review (`LoopSupervisor._is_confirmation_review` -- the approach under review has taken at least one GRANTED cure pass), a `severity: "blocking"` finding carrying neither optional field `reopens` (the cure task id of a listed finding of the previous granted pass that is still open) nor `regression: true` (the failure is caused by a change inside the diff range the confirmation review was told to check) is downgraded to `severity: "residual"` BEFORE the verdict is computed -- `findings.apply_confirmation_review_downgrade`, applied inside `LoopSupervisor._review_findings_channel`, so every reader of that list (`_review_contract_ladder`'s entry check and re-ask validation, which run BEFORE `_review_verdict`; `_review_verdict`'s schema/contract checks and its `review.verdict_recomputed` residual-recomputation branch; `_review_success_verdict_label`, `_review_rejection_signature`) sees the post-downgrade severities -- a `{"verdict": "verified"}` reply beside an unlinked blocking finding is therefore accepted without a re-ask. Emitted once per downgraded finding per review pass, however many times the channel is re-read. `reopens`/`regression` are both optional, undeclared-required additions to `FINDING_SCHEMA` -- no new REQUIRED field. Two things always stay blocking with neither field, never downgraded: an exposed secret, and a requirement whose behaviour is absent altogether (`findings.confirmation_review_exempt`, a case-insensitive `clause`/`evidence` text match on "secret"/"absent"). Never emitted on a first review of an approach (no granted cure pass yet) or from the bounded mid-run adjudication episode or mid-plan review (`mid_run=True`; neither folds in the confirmation-review section) |
| `task.dependency_repointed` | `taskId`, `removed` (the ids dropped, sorted), `added` (the ids gained, sorted), `dependsOn` (the new full list), `reason`, `source` (`"worker-repoint"` or `"cure-insertion"`) | #209 defect 7, widened by task 003 (requirement E.1) and task 004 (requirement E.2): an existing task's whole `dependsOn` list was replaced -- from a worker's own optional `proposed_task_updates` report (`state.apply_worker_dependency_repoints`, `source: "worker-repoint"`), or from one of two engine-driven re-points that both write `source: "cure-insertion"`: `state._repoint_dependents_onto_cure_task`'s dependent-repointing of a cure insertion, and `petitions.apply_confirmed_petition_adjudications`'s dependent-repointing off a `petitioned` task a CONFIRMED adjudication just moved to `skipped` -- one event per dependent whose write actually changes, either way -- the ACCEPTED-write sibling of `task.dependency_edge_rejected`, whose `taskId`/`reason`/`source` field names it copies. Standing for the worker-repoint write is `state.task_blocked_by`'s own derived doom over a `pending` task; only a `failed`/`skipped` edge may be dropped and the replacement may name none at all; the entry's mandatory `reason` also lands on the task's own `notes`, because the lint can check the graph's shape but never the semantic claim that the new edge carries the dead one's work, so this event plus that note are what let review and the operator judge it after the fact. Standing for a `"cure-insertion"` write is either the finding's own naming of a `failed` task in its `clause`/`evidence`, or a CONFIRMED petition adjudication naming the `petitioned` task's own obligation; its `reason` is a fixed sentence naming what covers the removed task's scope, and lands on `events.jsonl` only, never on the dependent's own `notes` |
| `petition.remedy_task_inserted` | `approach`, `obligation`, `taskId`, `finding` | #209 defect 4: a REFUTED petition adjudication whose remedy is WORK (`remedy_type` resolving to `substance`) became ONE cure task, through the same `state.insert_cure_tasks` call review's own cure pass makes (`petitions.refuted_remedy_cure_findings` decides, `LoopSupervisor._insert_refuted_remedy_tasks` writes) -- one line per inserted task, `finding` the synthesized blocking-finding record verbatim, mirroring `approach.cure_pass`'s own `findings` key. Emitted from the mid-run adjudication episode (§4.6) and from an ordinary run-ending review pass alike (v0.23 requirement D.2, task 019, #220 -- before that task the call was restricted to the mid-run episode, the one dispatch site whose inserted task the worker loop was guaranteed to act on; v0.23 requirement D.1's own new `review_cure_pending_at_verified` terminal-success blocker for a non-terminal `review-cure`-origin task made an approach-end insertion safe too), and it increments NEITHER `curePasses` nor `gateCurePasses`, so a remedy can never spend `cure_pass_ceiling` out from under review's or the peer verdict gate's own cure pass |
| `task.verify_skipped_exhausted` | `taskId`, `validationAttempts`, `limit` | a task reached the verify dispatch in `awaiting-validation` with its verify budget already spent (`validationAttempts >= VALIDATION_ATTEMPT_LIMIT`); no verifier ran, and the task moved to `failed` with `failureKind: validation-exhausted` in the same step |
| `task.un_reopened` | `taskId`, `previous` (always `"pending"`), `status` (the restored terminal status -- whichever of `"completed"`/`"validated"`/`"failed"`/`"skipped"`/`"superseded"`/`"petitioned"` (task 001, F04) the record genuinely held, read from `preCureStatuses`, never guessed from `cfg.vigilant`; since cure-01-05 (PRD requirement B.3/B.4, F05, #221) every one of the six is declared out of `pending` in BOTH variants, so the restore no longer depends on the verification mode in force), `source` (always `"cure-reconciliation"`), `reason` | v0.23 requirement B.3 (task 007, #221): `state.apply_task_status_change` took the declared un-reopen edge (`transitions.is_un_reopen_transition`, `TASK_STATUS_TABLE["pending"]`) -- `pending` back to a record's own pre-reopen terminal status -- instead of the generic `amendment_status_applied` audit event every other transition through that function still emits; `source` is always `"cure-reconciliation"` (v0.23 requirement B.4, task 008, #221), naming `LoopSupervisor._reconcile_pre_cure_heads` -- the completion-path hook that diffs a re-opened record's persisted `preCureHeads` sha against HEAD, on the cure task's OWN completion, and takes this edge only for a record none of whose cited paths that diff touched, and only once every cure task that re-opened that record at the same baseline has landed -- because this restore is the engine's OWN reconciliation of a status it itself persisted, never a caller's claim: an amendment ruling's `"operator"` source and the steer hat's `"steer"` `task-update` source reach the SAME `apply_task_status_change`, but `transitions.task_status_report_errors` refuses the un-reopen edge unconditionally for any caller other than this one reconciliation branch (`allow_un_reopen_restore=True` only when `source == "cure-reconciliation"`, cure-01-05-2, #221, generalized to every non-engine caller at `88fe9d69`) -- so `task.un_reopened`'s own `source` field never reads anything but `"cure-reconciliation"`; `reason` names those cure task ids, the diffed sha range and that no cited path was touched. Never emitted for the ordinary `pending -> in-progress` claim edge, which keeps `amendment_status_applied`/no event exactly as before |

**A run's terminal `state` event is appended before the status.json write
that makes `state` itself read as terminal (task 006, #169).**
`LoopSupervisor._finish()`'s non-reflect branch, `_promote_finishing_state()`,
and `_promote_recorded_finishing_state()` -- the three places `state` is ever
set to `succeeded`/`failed`/`aborted` -- each call `self.run.emit("state",
state=...)` immediately before the matching `update_status(state=...)`, never
after. A reader that polls `status.json` until it sees a terminal `state` and
only then reads `events.jsonl` for the matching event is therefore guaranteed
to find it already there; the reverse order (write first, event later, as
`engine/main.py` used to do once `run_job()` had already returned) could let
such a reader observe the terminal status with no event to explain it yet.

```json
{"id": 1, "ts": "2026-08-18T18:01:26Z", "type": "phase", "phase": "planning", "approach": 1}
{"id": 2, "ts": "2026-08-18T18:01:26Z", "type": "iteration.start", "number": 1, "phase": "planning", "model": "amazon-bedrock/eu.anthropic.claude-opus-5"}
{"id": 3, "ts": "2026-08-18T18:05:40Z", "type": "task", "taskId": "001", "oldStatus": null, "newStatus": "pending"}
```

### 5.5 Iteration records

`iterations/NNNN/meta.json` is written twice: once before the agent starts (so a
killed iteration still says what it was doing) and once when it ends.

A slot can hold more than one attempt. An engine killed mid-iteration leaves its
directory without an `endedAt`, so the resumed engine reuses that number
(§4.2) — and `RunDir.begin_iteration_dir()` moves whatever the dead attempt
wrote into `iterations/NNNN/attempts/NN/` (2-digit, oldest first, the same shape
as `approaches/NN/`) before the new attempt writes a byte. The archive is a
record *of* an iteration, never an extra one: no counter, budget or numbering
seed reads it, and `ralphctl iteration`/`GET /iterations/{n}`-backed detail
report it as `archivedAttempts` (`0` for a slot attempted once). Archiving is
best effort — a run dir that refuses the move logs a warning and the iteration
still runs.

| field | type | meaning |
|---|---|---|
| `number` | int | iteration number, matching the directory name |
| `phase` | string | `planning` \| `worker` \| `verify` \| `review` \| `reflect` |
| `model` | string \| null | the model reference the engine *requested*; `null` means the agent's own default |
| `modelResolved` | string \| null | the `provider/model` reference the iteration's own messages reported — the answer when `model` is `null` |
| `modelRaw` | string \| null | the provider's own id, when it differs from the resolved reference |
| `approach` | int \| null | the approach this iteration belongs to |
| `startedAt` / `endedAt` | string | UTC ISO-8601; a missing `endedAt` means the iteration never finished |
| `steeringDelivered` | array | steering file names this iteration's prompt carried |
| `steeringConsumed` | array | of those, the names its clean outcome marked applied (§4.6); empty for a failed/interrupted attempt, whose notes stay pending |
| `exitCode` | int \| null | agent process exit code; negative for a signal, `null` if it never spawned |
| `interrupted` | bool | ended by `SIGINT` (operator, timeout or watchdog) |
| `timedOut` | bool | the full per-iteration timeout fired |
| `noTrafficTimeout` | bool | the startup watchdog fired: zero observed agent traffic |
| `sawComplete` / `sawVerified` | bool | the marker was the final non-blank line of the final assistant text (§4.3) |
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
  "steeringDelivered": [], "steeringConsumed": [], "exitCode": 0, "interrupted": false,
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
than re-derived: `llm-wiring.json` holds the LLM `mode`, the resolved `env` map,
the `mounts` list and the resolved `--llm-header` `headers` map (task 045,
#177, empty/absent when none was ever resolved), and `env-wiring.json` holds
the `extra_env` list of
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
model: "amazon-bedrock/eu.anthropic.claude-opus-5"          # plain default
models:                         # per-hat overrides (task 005); "model_overrides" is
  worker: "amazon-bedrock/eu.anthropic.claude-sonnet-5"     # an accepted alias
  verify: "amazon-bedrock/eu.anthropic.claude-sonnet-5"
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
| `model` | string \| null | `null` | plain default pi model ref (`provider/model-id`) every hat falls back to when it has no per-hat entry; `null` means pi's own default |
| `model_overrides` / `models` | map hat→ref | `{}` | per-hat model, beating `model`; keys are `planning`, `worker`, `review`, `verify`, `reflect`, `router` (`MODEL_HATS`). `models` is an accepted alias, merged into `model_overrides` once at load (the latter wins on a shared key). Settable in `job.yaml` directly, or from `start` with the repeatable `--model-for HAT=REF` flag (§10.2), which merges into this table over the `--template`'s and the registry's own `models:`/`model_overrides:` key. Task 006 retired the `model_strategy`/`fast_model` keys that used to feed a phase→tier preset table (`quality-first`/`cost-optimized`/`balanced`); a job.yaml still carrying either is translated deterministically into this table at load time (`engine.config._migrate_legacy_model_config`), never silently dropped |
| `thinking` | string \| null | `null` | pi `--thinking` level, e.g. `medium`, `high` |
| `thinking_overrides` | map hat→setting | `{}` | per-hat thinking/reasoning table (task 007, §4.7), same key vocabulary as `model_overrides` but never merged into it -- a model ref and its thinking setting are two independent axes. Each entry is either a plain `pi --thinking` string (the same vocabulary `thinking` speaks) or a provider-shaped object (`{thinking: ...}`/`{effort: ...}`); read together with `model_overrides` by `JobConfig.bundle_for`/`tiers.resolve_bundle` into a hat's `(model, thinking)` bundle. `job.yaml`-only |
| `model_tiers` | map hat→tier→leaf | `{}` | the hat x tier host map's own job.yaml surface (task 002, §4.7): one leaf per `(hat, tier)` rung a job.yaml wants to name explicitly, `hat`/`tier` drawn from `MODEL_HATS`/`tiers.TIERS` (`trivial\|normal\|hard`) and `leaf` shaped like a `model_overrides` entry widened with an optional `model` key (`{model: ..., thinking: ...}`/`{model: ..., effort: ...}`, or a plain ref string). A rung this table leaves unset falls through to `bundle_for`'s already-resolved bundle for that hat, so a job.yaml naming only `model`/`models` still collapses every rung to the same bundle -- see §4.7. Settable in `job.yaml` directly, or from `start` with the repeatable `--model-for-tier HAT:TIER=REF` flag (task 004, §10.2), which merges rung by rung into this table over the `--template`'s and the registry's own `model_tiers:` key |
| `api_token` | string \| null | `null` | bearer token the API requires; `RALPHD_API_TOKEN` (how `start --api-token` delivers it) overrides |
| `infra_startup_timeout_s` | float | `150.0` | how long a phase may run with **zero** parseable pi events before the engine kills it as an infra fault instead of waiting out `iteration_timeout_s` |
| `infra_retry_backoff_s` | list[float] | `[2, 5, 15, 30, 60, 120, 300]` | sleep before each retry of the same phase after an infra-classified failure; the last value repeats. Fast at the start on purpose — a 30-second gateway blip should cost seconds, not minutes |
| `infra_retry_backoff_max_s` | float | `300.0` | cap on one wait, so the repeating tail polls at a 5-minute cadence through a long outage |
| `infra_retry_max` | int \| null | `null` | back-compat attempt cap, honoured **only** when set explicitly; `null` means "no cap — retry as long as the outage budget allows" |
| `infra_outage_budget_s` | float | `14400.0` (4h) | the real stopping rule: retries continue while one fault episode's accumulated wait stays under this. The episode clock resets on any successful iteration |
| `pricing` | map | `{}` | host-side rate table, consulted only when the provider quoted no price (§7.3) |
| *(anything else)* | — | — | unknown keys are preserved on `JobConfig.extra`, never rejected, and never surfaced by `GET /config` (they may contain operator-supplied secret-shaped values) |

Task 006 retired the earlier per-strategy tier table (`JobConfig.STRATEGY_TIERS`)
entirely — see §4.1's "model resolution" paragraphs above for the current
per-hat mechanism and the migration that keeps an old job.yaml working.

**`model_tiers` sits beside `model_overrides`/`models`, one axis over.** Where
`model_overrides` names one `(model, thinking)` bundle per hat, `model_tiers`
names one bundle per `(hat, tier)` rung — the job.yaml surface for the pure
`tiers.resolve_bundle(hat, effective_tier, host_map) -> HatBundle` resolver
described in §4.7 to actually read something a job author wrote, rather than
only ever seeing every rung collapsed to `bundle_for`'s single per-hat answer.
The two tables are independent the same way `model_overrides` and
`thinking_overrides` are: a `model_tiers` leaf can rename a hat's `model`, its
`thinking`/`effort`, or both, but a hat with no `model_tiers` entry at all is
unaffected — it still resolves through `model_overrides`/`model` exactly as it
did before task 008.

Seven fields also take an env override at engine load, so a run (or a test) can be
retuned without editing `job.yaml`: `RALPHD_API_TOKEN`,
`RALPHD_INFRA_STARTUP_TIMEOUT`, `RALPHD_INFRA_RETRY_BACKOFF_S` (comma-separated
seconds), `RALPHD_INFRA_RETRY_BACKOFF_MAX_S`, `RALPHD_INFRA_RETRY_MAX`,
`RALPHD_INFRA_OUTAGE_BUDGET_S` and `RALPHD_PRICING` (JSON of the same shape as
`pricing:`; invalid JSON logs a warning and is ignored).

**Precedence, stated once.** For every scalar `start` can set:

1. an explicit flag on `ralphctl start`;
2. the `--template`'s `job.yaml` value (§6.7);
3. the registry default in `<registry>/config.yaml` — only for the ten keys
   that have one (`ralphctl config set`): the three image supply keys `image`,
   `base_image` and `dockerfile`, plus `on_complete`, `network`, `auto_resume`,
   `default_llm_profile` (the fallback for `--llm`), `price_strategy`, `model`
   and `thinking` (#52). A named LLM profile may itself carry `model`/
   `price_strategy`, and a registry-wide value beats it: a profile answers
   *where do I route*;
4. the hardcoded default in `ralphctl` (`_TEMPLATE_SCALAR_FIELDS`).

The winner is written into `job.yaml`, and **only non-null keys are written** — an
absent key means the engine's own `JobConfig` default applies, so every default
lives in `engine/config.py` alone and is never duplicated host-side. Inside the
container, `job.yaml` loses to the `RALPHD_*` overrides above and to nothing else.
Two edits happen after the fact: `ralphctl resume --iterations +N` rewrites
`job.yaml` on the host before the new container starts, while `ralphctl budget
<run-id> +N` changes only the **live** engine's counter and status.json —
`/config/job.yaml` is a read-only mount, so the engine cannot rewrite it there.
That live change still persists across a resume, because a fresh container's
own startup seeds its budget from `max(status.json's iterationsBudget,
job.yaml's iterations)` (§9.2, task 010, #51); `resume --iterations +N` is only
needed to add *more* on top of that effective number, not merely to keep it.

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
6. a `## Recent operator messages (read-only context)` echo — the most recent
   operator messages of the run (up to `STEERING_ECHO_MAX_MESSAGES`, each body
   capped at `STEERING_ECHO_MAX_BODY_CHARS` with a pointer to the file),
   **whatever the phase and whatever each message's state**, including messages
   already settled. It carries no consumption semantics: building a prompt never
   consumes or settles anything, `applied (settled)` is only the harness fact
   that some hat processed the message and never a claim that the tasks it
   produced are `done` (`tasks.json` is the single source of truth for that),
   and presence in the list is not permission to act — whether the reading hat
   may act on a still-pending message is what item 5 says;
7. the phase's own `extra` (the task under verification, the review findings, and
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

**The `api` overlay is a host-only surface under requirement A's split
(#62, §13.3).** `PUT /config/prompts/{name}` (and every other `/config`
mutation) is one of the routes `control_token` gates unconditionally, and
`PiRunner` strips `RALPHD_CONTROL_TOKEN` from every spawned iteration's own
environment (task 003) — so no phase's agent process can ever construct the
header that would let it call this route from inside the job container itself.
In practice the `api` overlay is set exactly the way `ralphctl prompts <run-id>
set` implies: from the host, by an operator who holds the token `ralphctl
start`/`resume` provisioned. This is the same shape as `PUT /config/skills`,
`PUT /config/creds` and `PUT /config/llm` — every writer of the config overlay
is reachable only from outside the container the phase it configures runs in.

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
| `llm-wiring.json` | `{mode, env, mounts, headers}` from the `--llm` resolution — `host`'s forwarded vars and `~/.aws` mount, or a named profile's fully-resolved `env:`/`mounts:` including `${env:}`/`${file:}`/`${cmd:}` results; `headers` (task 045, #177) is the resolved `--llm-header` name -> value map applied to every provider's `headers:` (absent/empty when no `--llm-header` was ever resolved for this run) | `0600` |
| `env-wiring.json` | `{extra_env: [...]}` — the resolved `name=value` pairs from the three flags above, in the exact order applied | `0600` |
| `auto-resume.json` | `{auto_resume: bool}` — a host-side setting, never passed into the container | plain |

`resume` replays `llm-wiring.json`'s env and mounts first, then
`env-wiring.json`'s pairs, matching `start`'s own precedence. `resume
--llm-header` (task 045) is the one flag among these three files that DOES
take a live override on resume: it merges its resolved specs over the
persisted `headers` map (a same-named header wins, others are kept),
re-merges the result into `<config-dir>/pi/models.json` and re-writes
`llm-wiring.json`'s `headers` field, so a LATER resume with no flag replays
the overridden value, not the original `start`'s. A run whose config dir
has no such file resumes with nothing extra and no error. Neither secret file
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
`operator_input_needed_pause` (§10.2, PRD v0.23 requirement A.1, #224, task
014 -- ON by default, unlike `peer_plan_review`/`peer_verdict_gate` below,
which have no template-level default of their own),
`on_complete`, `on_complete_cmd`, `timeout` and `iteration_timeout` (both in
**minutes**, defaults `480` and `45`), `llm`, `model`,
`thinking`, `price_strategy`, `notifier_url` (§10.2, PRD v0.20.2 requirement
B.2 — no `ralphctl config` registry level for this one: an endpoint is one
operator's channel for one job), `review_clone_protocol` (§10.2, #208 — the
judging hats' disposable-clone identity protocol; no registry level either,
since the path is a property of one product's repository), `network` and
`auto_resume`, plus the three image supply keys `image`, `base_image` and
`dockerfile` (settled as one unit, §3.2), plus three
path-valued keys resolved relative to the template directory: `skills` (a list of
directory names), `creds` (one directory name) and `prd` (a filename, default
`prd.md`). Task 006 retired `model_strategy`/`fast_model` from this list — a
template's own `job.yaml` may still literally carry either (they translate at
engine load time like any other job.yaml, §4.1), but `start` no longer copies
them down field-by-field.

`job.yaml` may also set `llm_header` (task 045, #177): a list of
`--llm-header`-shaped strings (`Name: value`/`Name=value`) supplying default
headers for the run's resolved pi config. Unlike every scalar key above, an
explicit `--llm-header` flag does not wholesale-replace the template's list —
it is appended AFTER the template's own specs, so a later spec for the same
name wins (`resolve_header_specs`'s existing rule): flag > template > the
profile's own `pi.providers.<p>.headers:`. There is no registry-wide
`llm_header` level.

Every one of those has a corresponding `start` flag, and the flag always wins
(§6.2's chain, with `llm_header`'s own precedence rule above). `--skills` and
`--creds` override **wholesale**, not by merge — a
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
model: provider/model-id             # default model for jobs using this profile
fast_model: provider/cheap-model-id  # profile's own second model ref (task 006:
                                      # no longer forwarded into job.yaml by
                                      # `start` -- see the key table below)
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
| `model` | string | default model ref for a job started with this profile; an explicit `--model` beats it, and so does a template/registry value (#52) |
| `fast_model` | string | the profile's own second model ref; carried through by `ralphctl llm show`/`resolve_profile()` but, since task 006 retired job.yaml's `fast_model` key, no longer forwarded into a started job's job.yaml |
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

`host` sets no `model`, so pi's own default applies unless `--model`
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

**A failed iteration costs one iteration, never the run** (tasks 010 and 011,
#28). Every one of those five signals — the timeout included — is a *recorded*
outcome, and so is anything the agent subprocess or its plumbing throws on the
way: the single `await self.runner.run(...)` in `_run_iteration_once` is wrapped
in the loop's iteration containment boundary, which catches `BaseException` and
turns whatever it caught into an ordinary failed `IterationResult` whose
`error_message` reads `engine iteration failure: <repr>`. Everything below that
boundary — `meta.json`'s `endedAt` and `faultClass`, the `iteration.end` event,
usage accounting, the infra refund of §8.4 — therefore still runs, which is why
the boundary sits at that one `await` and not around the run: a guard further
out can only end the job with an "engine error", losing the record of the
attempt that caused it. `BaseException` rather than `Exception` is deliberate.
The defect this closed was an `asyncio.CancelledError` escaping the runner's own
timeout plumbing — `wait_for` cancels the future it timed out on, so re-awaiting
that task re-raises, and `CancelledError` is a `BaseException` that no
`except Exception` in between could see — so a worker iteration blowing its
`iteration_timeout_s` killed the engine instead of failing one iteration. The
runner's timeout path now uses `asyncio.wait({pump_task}, timeout=...)`, which
leaves the task alone so `pi` can be `SIGINT`ed and its final output drained
within `runner.SHUTDOWN_GRACE_S`; the boundary is the structural half of the
same fix, so the next stray cancellation is contained too.

Two things are deliberately *not* contained, because both mean "stop, do not
start another iteration": `KeyboardInterrupt`/`SystemExit`, and a cancellation
genuinely requested on the iteration's own task from outside
(`asyncio.Task.cancelling() > 0`, which is `0` for a `CancelledError` that
merely leaked out of inner plumbing). `_run_job_core`'s outer guard stays
`except Exception` for the same reason.

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
   error is a bare `aborted` and the exit status is clean (`exit_code == 0`),
   in which case return `"infra"` (task 014, #49 part 2, re-answered by task
   117 / steering 005 — the answer to what was §16's open question 1, see
   §17). A one-word `aborted` after traffic, with a clean exit and no abort
   recorded for the run, is the provider hanging up mid-stream **at any
   duration**: no iteration that did real work exits 0 with nothing but that
   one word as its error text. Task 117 (steering 005) retired the duration
   bound (`faults.ABORTED_STREAM_MAX_DURATION_S`, 120s) this step used to
   carry — see §17 entry 1 for why an absolute clock could not be raised out
   of the failure it caused.
7. Otherwise return `"infra"`.

`produced_traffic` is derived in `LoopSupervisor._classify_result()` as
`bool(result.final_text) or bool(result.usage)` — the agent said something, or
tokens were accounted for.

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
`interrupted` (step 5), `exit_code` (step 6) and `produced_traffic` (steps 6
and 7) decide it instead — a bare `aborted` with a recorded operator abort is
`"work"`; with no traffic and no recorded abort it is `"infra"`; with traffic
and no recorded abort it is `"signal"` when a signal killed the process, and
`"infra"` whenever it arrived with a clean exit — regardless of duration.

**What is left, deliberately.** A genuine agent crash whose *entire* final
error text happens to be the single word `aborted`, with a clean exit code,
still classifies as `"infra"` (step 6) rather than `"work"` — deliberately:
that text is identical to the real provider-side shape, and a crash reporting
nothing but that one word with a clean exit is vanishingly unlikely (a crash
reports what crashed). Task 014's original residual — the same shape past an
arbitrary duration bound — is gone along with the bound that created it; see
§17 entry 1 for the run that showed the bound could not be raised without
recreating the same failure further out.

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

**Refunds survive a resume** (task 023, #32; task 014 added the third
counter; v0.24 task 015, requirement E.1, #169 renamed that third counter
from `grace` to `adjudication` when it retired the off-budget grace review
and handed the same off-budget-refund shape to the midrun
petition-adjudication episode). `_refund_iteration()` is the one place that
bumps any counter, and it writes all three to `status.json` as
`iterationsRefunded: {infra, steer, adjudication}` in the same breath — an outage
retry is precisely when a run is most likely to be killed and resumed. A
new engine process seeds the counters back from that field
(`_seed_refunds()`), each clamped into `[0, iterations_used]` with their
sum never exceeding it either. The alternative the PRD allowed — seeding
`iterations_used` from a charged count instead — is deliberately **not** taken:
that counter is also the number of the next iteration directory, so a resumed
engine would reuse a number a *finished* iteration already holds and
`begin_iteration_dir()` (§4.2) would archive a completed record as a crashed
attempt. What is discounted is the budget comparison, never the numbering.
Refunds also cannot be re-derived from the iteration dirs afterwards: an
adjudication episode is refunded and carries no `infra` faultClass at all.

*Residual limitation, deliberately unchanged:* the midrun adjudication
episode's own per-process guard (`self._petition_adjudication_seen`, an
obligation set rather than an approach-keyed count) is still per-process, so
a resumed engine may run one more bonus episode over an obligation an earlier
process already adjudicated in this same approach — the refund is a budget
fact that has to survive, the guard is a policy whose resume behaviour is not
part of #32.

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

**An operator pause must not eat the job's working time either (v0.14, requirement P,
#95).** `self.deadline`/`deadlineAt` used to be fixed at construction, so a run paused near
its deadline burned `job_timeout_s` doing no work — and once the deadline fell behind the
run, every remaining iteration got only the 60-second floor
(`min(cfg.iteration_timeout_s, max(60, int(deadline - now)))`). `_account_paused_wait()` is
the pause twin of `_account_infra_wait()` just above, sharing its deadline arithmetic
(`_extend_deadline_by`) but its own running total, `pausedTotalS` (published beside
`infraWaitTotalS`), and its own `deadline_extended` `reason` (`"operator pause"`, never
`"infra wait"`), so an operator reading the event log can tell the two credits apart
without cross-referencing anything else. The amount booked is measured at the wait site
itself — `_gate()`, via `_park_until_resumed()` — never in `pause()`/`resume()`: a pause
requested while an iteration is still running credits only the time the loop was actually
parked. `pausedTotalS` is seeded from `status.json` at startup, the same way
`infraWaitTotalS` is, and therefore survives `resume`.

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
failure is indistinguishable from `reflect` never having been enabled. On
**success** it removes that tombstone again (`_clear_reflect_tombstone()`,
emitting `reflect_tombstone_cleared`): the file is written per attempt but the
run dir outlives the attempt, so a failed episode's tombstone otherwise sits
beside the next episode's report claiming the run left no post-mortem (§12.2).
"Ran but
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
broke. It equally does not *remove* a tombstone an earlier attempt left: a
signal that stopped this engine does not make that earlier failure untrue, and
only a report on disk contradicts it. `attempted: false` is the signal-before-the-phase case, `attempted: true`
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
`costUSD: 0` with `costPriced: true` over half a million billable tokens.
`state.is_zero_quote()` recognises the
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
`("starting", "running", "paused", "finishing", "timed_out")` -- task 012, #56, adds
`finishing`: an engine that dies mid-reflect leaves a dangling run exactly like
one that dies mid-worker; v0.18 task 036, #142, adds `paused` for the same
reason, since an engine parked on the `rejected_last_approach` escalation still
owns the run through an in-process `asyncio.Event`; v0.24 task 008, #169, adds
`timed_out` for the deadline hold, resume-eligible and never terminal) while **no container is
running for it** is a dangling run. It is the shape a host reboot, an OOM kill or a `docker rm`
leaves behind, and the run dir alone cannot distinguish it from a run that is
working normally.

Liveness therefore has **three** states, not two (issue #31): no container by
that name exists (`absent`), one exists and is running (`running`), one exists
and has exited (`exited`). Only `running` means a live engine owns the run dir;
the other two are both dangling. The distinction lives in the one shared
helper the four surfaces (`status`, `doctor`, `repair`, auto-resume) call, not
in the surfaces, because the original defect was not a missing check anywhere
— it was one caller reading "a container record exists" as "something is
alive", which silently blinded all four. The two dangling shapes share a
remedy but not a diagnosis: an exited container is still there to be inspected
(`docker logs`), so each surface words them apart from one shared table.

`status` detects it by pairing the recorded state with a container lookup by
name and surfaces it explicitly:

- a warning line stating what happened to the container (gone, or present but
  exited), backed by a `dangling` field (either shape) and a
  `containerLiveness` field naming which one, so the condition is
  machine-readable and not only a rendering flourish. The older
  `containerGone` field keeps its narrower pre-#31 meaning — `true` only for
  the vanished case — rather than widening under a name that would then be
  wrong for half the runs it covers. A host with **no `docker` client at all**
  adds a fourth *reading* (not a fourth docker state): `containerLiveness`
  is `"unknown"`, `containerGone` is `false`, the warning says the liveness
  could not be checked here, and `status` still exits 0 off the run dir;
- task counts read from the run dir rather than from the engine, since there
  is no engine left to ask;
- **staleness instead of a growing live elapsed time.** A dangling run's
  duration is frozen and the display switches to a "since last update"
  reading derived from `sinceLastUpdateSeconds`. A ticking elapsed counter on
  a dead run is an actively misleading display.

`doctor` sweeps the whole registry rather than one run and reports two
findings without changing anything: `danglingRegistryEntries` (recorded
non-terminal, no *running* container, each entry naming its `liveness`) and
`strayContainers` (a container with no
registry entry). Report-only is the default because both findings have benign
explanations, and each carries a suggested remedy — resume first for a
dangling run, or record the truth if resuming is not wanted. `doctor --fix`
auto-resumes exactly what that sweep matches, so both dangling shapes are
auto-resume-eligible; `resume` itself already removes an exited container
occupying the name before starting a fresh one. `doctor` also names the
run-secrets mount (v0.19 requirement E.4, #150/#152, task 020)
registry-wide, unconditionally, since it is not tied to any one run: the
mode-0700 host directory `start`/`resume` bind at
`petitions.DEFAULT_RUN_SECRETS_DIR` for the petition-adjudication MAC key, and
the `events.jsonl` `petition_adjudications_recorded` MAC witness that is the
documented fallback authentication check whenever no key survived into it
(§5.2's `runSecretsMount` is the per-run answer to whether one actually did).

`repair` is the guarded editor for the run dir. It refuses to touch a run
whose container is actually running, exiting `5`, because editing state under
a live engine produces a run dir that contradicts itself. `--set-state`
validates the target state, writes a `reason` recording that this was an
operator repair and what had happened to the container (vanished, or exited
without the engine recording a terminal state), and appends a `repair`
audit event, so a hand-edited terminal state is never indistinguishable from
one the engine wrote. `--env KEY=VAL` updates the persisted env wiring
(`env-wiring.json`) in place for the next resume; only the key *names* are
recorded in the audit trail, never the values.

### 8.8 Self-recovery, on by default

Automatic resurrection of a job is a policy decision. A host that reboots
nightly wants dangling runs picked back up; a host where a container died
because the job was making things worse does not.

**Self-recovery is on by default since v0.7** (requirement O), and the default
literal is written in exactly one place: `AUTO_RESUME_DEFAULT = True` in
`src/ralphd/cli/main.py`. A default that appears in two places is a default
that will eventually disagree with itself — one copy in a flag definition, one
in a config loader, and no way to tell which one a given run obeyed. Every
consumer reads the single constant, which is why the flip itself was one line.

It shipped OFF in v0.5 on purpose: the two rules that make it safe had to be
validated against real runs before they became load-bearing for everybody —
the crash-loop guard (below) and "never resurrect a run the operator killed"
(also below, narrowed in v0.7 so an *accidental* self-inflicted signal, §4.6,
is still resumable). Both now are. The default flipped because the failure it
prevents is the common one for an unattended run: a host reboot or an
OOM-killed container leaves the job dangling until a human notices, spending
the rest of its wall-clock budget doing nothing, while the failure an ON
default risks is bounded by the guard to a handful of containers.

Opting out is per run or per registry, and is what an operator who disagrees
reaches for: `start --no-auto-resume`, or `ralphctl config set auto_resume
false` for every run in the registry. Opting *in* explicitly is still
`start --auto-resume`. Either way `start` writes an `auto-resume.json` marker
in the run's config dir recording the value it resolved, so a run's opt-in
never depends on what the default happened to be on the day it was resumed;
a run started before the marker existed reads as the current default. The
marker in the *config* dir is the immutable opt-in; a separate
`auto-resume.json` in the *run* dir holds mutable guard state, so the
operator's intent and the machine's bookkeeping cannot overwrite each other.

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
| `POST` | `/rule` | record an operator RULING keyed to a rejection signature (task 013, requirement A.2, #224); `202` with the written filename -- see §4.6 |
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
value, the delta and the usage at the time. It is a live-engine change to
`/config/job.yaml` (read-only, so the engine cannot rewrite it there) that
persists across a resume anyway: a fresh container's own startup seeds
`cfg.iterations` from `max(status.json's iterationsBudget, job.yaml's
iterations)` (task 010, #51), so a plain `resume` with no flags already
carries a live top-up forward; `resume --iterations +N` (task 011) adds to
that same effective number rather than to job.yaml's alone, only needed to
add *more*. Two rejections are specified: `422` when `iterations` is missing or
unparseable, and `409` when the requested value is below
`iterations_used_charged`, because a budget may be set to current usage or
above but never retroactively below what has already been spent.

`PATCH /config/budget` also takes a `curePassCeiling` key (task 010, v0.27
requirement C.3 -- `ralphctl budget <run-id> --cure-passes +N|N`), the SAME
`"+N"`/absolute grammar raising (or lowering) the run-wide
`cure_pass_ceiling` both cure channels (`curePasses`, `gateCurePasses`)
spend from one shared pot. `409` when the resolved value is below the run's
own cure passes already taken (`curePasses`' and `gateCurePasses`' combined
total). Like `iterations`, it PERSISTS across a resume: a fresh container's
own startup seeds `cfg.cure_pass_ceiling` from `max(status.json's
curePassCeiling, job.yaml's cure_pass_ceiling)`, mirroring the `iterations`
seeding immediately above -- unlike `maxApproaches`/`iterationTimeoutS`/
the job deadline (#94 (c)+(d)), which are process-only and revert on a plain
resume.

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
| `POST /rule` | the job is finished; a ruling has no effect |
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
`image.py` for the job-image hash, `patch.py` for reading a reflect phase's
proposed diff, and `llm_profiles.py` for the profiles — on
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
| `skills <id> …` | `ls`/`get`/`add`/`rm` skills on a live job; `skills install` (no run id) places the shipped operator skill on this machine |
| `creds <id> …` | `ls`/`get`/`add`/`rm` runtime credentials on a live job |
| `prompts <id> …` | `ls`/`set` phase prompt overrides on a live job |
| `llm …` | `profiles`/`show`/`test` LLM profiles on the host |
| `steer <id> [msg]` | queue operator guidance for the next iteration, or `--list` what has been queued |
| `rule <id> --file <path>` | record an operator ruling in answer to a rejection (task 013, requirement A.2, #224), reaching both review and verify, keyed to the rejection signature not a task id |
| `amend <id> <task> <text>` | record an operator ruling review must honour (waiver/replacement of a task's criteria), or `--list` what's recorded |
| `retry <id>` | wake a degraded run out of its infra backoff wait immediately |
| `budget <id> <+N\|N>` | change a running job's iteration budget in flight |
| `abort <id>` | terminate the job, recording state `aborted` |
| `stop <id>` | shut down and remove an idle finished container |
| `rm <id>` | delete a run's registry dir (`--force` stops a leftover container first) |
| `repair <id>` | diagnose (or, guarded, patch) an inconsistent run dir |
| `artifacts <id> …` | `ls`/`pull` the run's artifacts straight off disk |
| `router <id> [ls\|show] [N]` | this run's router calls: which legal edge the router picked for an ambiguous state, and its own cost/tokens -- never a work iteration |
| `suggestions <id> …` | `show` a finished run's reflection diff and exactly what it would touch, and `apply` it to a checkout on an explicit `--yes`; a subset by file with `--only PATH`; refused while a live run is executing that tree, and while another run's diff also fits the same file (`--ignore-conflicts` picks this one) |
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
| `--peer-plan-review` | off | opt-in peer plan-review gate (PRD requirement K, #101): a peer model approves or objects to the plan before work starts; `JobConfig.peer_plan_review`, unset stays out of `job.yaml` entirely so the engine's own default applies |
| `--peer-verdict-gate` | off | opt-in, genuinely blind peer verdict gate (PRD requirement L, #101): a second peer model re-verifies completed work before terminal success; `JobConfig.peer_verdict_gate`, same unset-stays-out-of-`job.yaml` shape as `--peer-plan-review` above |
| `--mid-plan-review` | off | opt-in mid-plan review episode (PRD requirement K.2, #169): once per approach, once a declared fraction of the plan has already validated, run one review episode scoped to those requirements, opening cure tasks from its findings through the existing cure machinery; `JobConfig.mid_plan_review`, same unset-stays-out-of-`job.yaml` shape as `--peer-plan-review` above |
| `--no-operator-input-needed-pause` | on (disable flag) | task 014's pause-before-archive gate (PRD requirement A.1, #224): ON by default, unlike the two peer gates above -- a rejection that would otherwise archive the approach holds instead with `pauseReason: "operator_input_needed"`; `JobConfig.operator_input_needed_pause`, resolved by the `_TEMPLATE_SCALAR_FIELDS` loop (§6.7) so job.yaml always carries an explicit `true`/`false`, unlike the two peer gates. The SAME flag also gates the peer verdict gate's own mirror edge (v0.27 requirement C.1, task 008): a `verdict_gate` rejection that would have earned the cure-pass edge if the run-wide `cure_pass_ceiling` were not already spent holds instead with `pauseReason: "verdict_gate_operator_input_needed"`, disabled by the same flag |
| `--reflect` | off | one extra post-terminal `reflect` iteration into `artifacts/reflection/` |
| `--model REF` | unset | `pi` model ref used by any hat with no per-hat override |
| `--model-for HAT=REF` | none | per-hat model override (repeatable), e.g. `--model-for worker=amazon-bedrock/eu.anthropic.claude-sonnet-5`; merges into the job's `models:` table over the `--template`'s and the registry's own `models:`/`model_overrides:` key -- see docs/cli.md |
| `--model-for-tier HAT:TIER=REF` | none | per-hat-per-tier model override (task 004, #68), repeatable, e.g. `--model-for-tier worker:hard=amazon-bedrock/eu.anthropic.claude-opus-5`; merges rung by rung (hat AND tier) into the job's `model_tiers:` table (§4.1) over the `--template`'s and the registry's own `model_tiers:` key -- see docs/cli.md |
| `--fast-model` (retired) | n/a | accepted-but-hidden (`argparse.SUPPRESS`); `cmd_start` refuses it with exit `2` naming the replacement (`models:`/`model_overrides:`, §4.1) rather than silently doing nothing |
| `--model-strategy` (retired) | n/a | same shape as `--fast-model` above -- accepted-but-hidden, refused with exit `2` naming the replacement |
| `--thinking LEVEL` | unset | `pi` thinking level |
| `--price-strategy S` | `none` | derive a cost for routes the provider does not price (or prices with an implausible zero): `aws` uses the built-in Bedrock rate table (§8.6) |
| `--notifier-url URL` | unset | endpoint the notify hat tool POSTs operator updates to (PRD requirement B.2, #197); written to `job.yaml` as `notifier_url` (§10.2 precedence: `RALPHD_NOTIFIER_URL` beats it; without the flag a `--template`'s own `notifier_url:` key (§6.7) or a hand-set key in the run's own `job.yaml` still reaches it; unset means unconfigured) |
| `--review-clone-protocol PATH` | unset | workspace-relative executable the `review` and `verdict_gate` hats run inside their disposable clone to prove the clone is measuring itself, in place of the prompts' Python `pip install -e .` / `__file__` recipe (#208); written to `job.yaml` as `review_clone_protocol` (so `resume` replays it), and reachable without the flag via a `--template`'s own key (§6.7) or a hand-set key in the run's own `job.yaml`. Rendered to every hat as the `## Disposable-clone protocol (operator-authorized)` block; the engine neither runs nor validates it. Unset leaves the prompts' Python recipe in force, which a product with no `pyproject.toml` cannot satisfy |
| `--llm PROFILE` | `host` | `host`, `none`, or a name under `<registry>/llm-profiles/` |
| `--llm-env KEY=VAL` | none | extra env for the LLM wiring; repeatable |
| `--llm-header 'Name: value'` | none | set this HTTP header on every provider of the resolved pi config (`Name=value` also accepted; repeatable; a same-named header the profile already carries is overridden, others are kept; `${env:}`/`${file:}`/`${cmd:}` resolve on the host, task 044, #177) |
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
| `--auto-resume` / `--no-auto-resume` | on (§8.8) | opt this run in to or out of `doctor --fix` self-recovery |
| `--port N` | free ephemeral port | host port for the run's API |
| `--api-bind ADDR` | `127.0.0.1` | address the API is published on |
| `--network NET` | none (default bridge) | docker network; `host` shares the host netns |
| `--api-token VALUE\|auto` | none | bearer token for the run's API; `auto` generates one |
| `--no-detach` | detached | follow the event stream to completion in the foreground |

`--fast-model` and `--model-strategy` are retired (task 006): both are still
accepted by the parser (hidden from `--help`) so `cmd_start` can refuse them
with exit `2` and a message naming the replacement (a job.yaml `models:`/
`model_overrides:` table, §4.1) rather than silently doing nothing with the
value.

Timeouts are integers in **minutes**; there is no duration-string syntax.
`--infra-outage-budget` is the one exception, in seconds, because it is a
retry budget rather than a work budget and is tuned against how long a
gateway outage plausibly lasts.

**Scalar job settings resolve through one fixed precedence chain:** an
explicit CLI flag, then the `--template`'s `job.yaml`, then the registry-wide
default in `<registry>/config.yaml` (`ralphctl config set`), then the
hardcoded default in the table above. Only `image`, `base_image`, `dockerfile`,
`on_complete`, `default_llm_profile` (feeding `--llm`), `network`, `auto_resume`,
`price_strategy`, `model` and `thinking` — ten
keys — have a registry-wide layer; the rest skip straight from
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
| `amend <id> <task> <text>` | record an operator ruling review must honour (waiver/replacement of a task's criteria) | a PRD requirement is impossible, or a scope call only you can make |
| `amend <id> --list` | show what rulings have been recorded | before ruling again |
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

**`amend` is a different kind of write than everything else in this table.**
Every other control verb calls the engine's HTTP API; `amend` never does — it
writes, from the host process, directly into `config_root(run_id)` (the
directory `start`/`resume` bind-mount `:ro` as `/config`), the exact same path
`job.yaml`/`prd.md` already occupy. That is deliberate: review re-derives its
verdict from the PRD every pass and is outside `STEERING_ACTIONABLE_PHASES`
(§4.6), so a steering message never reaches it — if a criterion turns out to
be impossible mid-run, or the operator wants to make a scope call, `amend` is
the one channel that can still change the verdict, and it has to be one the
run itself cannot forge (§13.4). Storage is `config_root(run_id)/amendments/
NNN-<task-id>.md`, `NNN` assigned by `ralphctl amend` itself; `--list` has no
live half at all (there is nothing an engine could say that disagrees with
the file on disk) and always reads that directory directly.

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
`budget_changed` audit event. It is a live-engine change to `/config/job.yaml`
(read-only, the engine never rewrites it) that still persists across a
resume — a fresh container's own startup seeds its budget from the larger of
status.json's and job.yaml's figures (task 010, #51) — so `resume <id>
--iterations +N` (task 011) is only needed to add *more* on top, adding to
that same effective number rather than to job.yaml's alone. Exit `5` when the
engine refuses (the result would fall below `iterationsUsed`, or the job is
finished), `2` for a malformed spec. `--cure-passes` (task 010, v0.27
requirement C.3) raises the same knob a fifth way: the run-wide
`curePassCeiling` both cure channels (review's own and the peer verdict
gate's, `_cure_passes_for_run`) spend from one shared pot, engine-applied
via `LoopSupervisor.set_cure_pass_ceiling`, refused with `5` when the
result would fall below the cure passes this run has already taken. Like
`iterations`, and unlike `--approaches`/`--timeout`/`--iteration-timeout`
(#94 (c)+(d)), `--cure-passes` PERSISTS across a plain resume: a fresh
container's own startup seeds `cure_pass_ceiling` from the larger of
status.json's `curePassCeiling` and job.yaml's own `cure_pass_ceiling`
(`main._seed_cure_pass_ceiling`, the same winner rule task 010/#51 applies
to `iterations`) — so a run parked on a spent ceiling
(`verdict_gate_operator_input_needed`, v0.27 requirement C.1) can be
raised and unpaused into taking the cure pass it was parked over, with no
container restart required.

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
state is terminal (or `finishing`/`timed_out`, neither of which has an iteration
in flight), and anything else (`starting`/`running`, an unrecognized
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
`state`, adding a `reason` naming the container and its fate (vanished, or
present but exited) when the run really
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
- `deadline:` (task 007, #72) — shown only when `status.json` carries the
  `deadlineBeatVerdict` marker (task 006, above): every task was completed,
  the worker's own `COMPLETE` was already honoured, and the deadline took
  the last review attempt before it could ever render a verdict. `state`/
  `verdict` above still read the ordinary `failed`/`unverified` — that ruling
  stays the review hat's alone to grant — so without this line the run reads
  on screen exactly like an ordinary failure; it names both timestamps the
  marker carries and says outright that the ending is not a judgement on
  the work. `--json` carries `deadlineBeatVerdict` itself through untouched.
- `degraded:` — the run is sitting out an infra outage: attempt number, phase,
  a ticking countdown to `nextAttemptAt`, the episode's wait against the outage
  budget, and the underlying `error`. A degraded run *between* two waits (the
  retry attempt itself is running, `infraWait` back to `null`) says so
  explicitly. A run that merely looks stuck at `state: running` is the failure
  mode this line exists to eliminate.
- `container:` — in bold red, the zombie warning: no container is running for
  this run — it either appears gone or is present but exited (issue #31) —
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
| `docs <id> [name]` | the prose the run left behind — `notes.md`, `review-findings.md`, `composite-prd.md`, `debt.md` — and the effective `job.yaml` with secret values redacted |

Each document's operator-facing key (`state.run_document_keys()`, in listing
order) is `notes`, `findings`, `composite-prd`, `debt`, `job`, `run-record` or
`run-record-json`; either the key or the file name works for
`docs <id> <name>`. `debt` is the debt ledger's own key (task 006, requirement
A.3, #155) — `debt.json` rendered for a human at `debt.md` — and it appears in
the no-name LISTING (`state.run_documents()`) exactly like every other
document: with its size when the run wrote one, or `state.RUN_DOCUMENT_ABSENT`
when it never did.

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
`status --json` carries `live`, `containerGone`, `dangling`,
`containerLiveness`, `autoResume`,
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
and `state`/`verdict` in lifecycle order (`starting → running → paused →
finishing → succeeded → failed → aborted`, and
no-verdict → `unverified` → `verified`). Missing values
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
and falls back to disk when that does not answer. No state is written, there is
no database, and a dead run never produces an error — only stale-but-valid data
marked as such.

**`GET /api/runs/<id>` carries an `ETag` (task 003, requirement A.1).** The
server derives it from the run's own on-disk timestamps — the max `mtime`
across `status.json`, `tasks.json`, `events.jsonl` and the newest iteration's
`meta.json` — so it changes exactly when the payload it would compute *would*
change, never more, never less. A request that sends that same tag back as
`If-None-Match` gets a bare `304` with the `ETag` repeated and no body; there
is nothing to recompute or resend, since nothing in the run dir has moved
since the `200` that produced it. Two small in-memory derivation caches (task
004, requirement A.2) save the recomputation on a miss too: an
iteration-row cache keyed per run id and iteration number on `(meta.json
mtime, transcript size)`, and a `tasks.json`-shaping cache and a
`status.json`-shaping cache each keyed on their own file's `mtime`. All three
(`_ITERATION_ROW_CACHE`, `_TASKS_BLOCK_CACHE`, `_STATUS_BLOCK_CACHE` in
`ui_server.py`) live only in the hub process's memory, are dropped for a run
the moment that run is deleted, and never answer for any run but the one they
were keyed on — so this is still not a second source of truth, only a memo of
a computation the same on-disk files would reproduce.

**The client backs off, and pauses while its tab is hidden (task 005,
requirement A.3).** The run-detail poll (§11.3) sends the last `ETag` it saw
back as `If-None-Match` on every tick; two consecutive `304`s, or the run
leaving `state: "running"`, widen that view's own polling interval from the
normal 4 seconds (`REFRESH_MS`) to a fixed 20 seconds (`BACKOFF_POLL_MS` —
inside the PRD's 15-30s band), snapping back to 4 seconds the instant a
poll's payload actually differs again. Every view arms its refresh timer
through one shared `scheduleRefresh()`, which refuses to arm one at all while
`document.hidden`; a single page-level `visibilitychange` listener stops
whichever timer is currently running the moment the tab is hidden and
restarts it, at whatever interval was already in force, the moment the tab is
shown again — so a hub tab left open in the background neither polls nor
advances its own back-off state while nothing is looking at it.

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

**The bundle is mount-agnostic.** A reverse proxy may publish the hub under a
path prefix (`tailscale serve --set-path=/ralph`); such a proxy strips the
prefix before proxying and forwards no header naming it, so the server cannot
know it is mounted and does not try. `index.html` references its two assets
relatively, and `app.js` derives `HUB_MOUNT` from
`document.currentScript.src` and prepends it to every request — `""` at the
root, so an unproxied hub issues exactly the requests it always did. The
document URL must end in `/`, since `/ralph` resolves the bundle's own assets
above the mount; `tests/test_hub_path_prefix.py` fixes both halves and the
trailing-slash rule.

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
iterations, parkedQuestion}` — where
`status` and `tasks` are proxied from the container when its API answers and read
from disk otherwise, and `iterations` is always read from
`iterations/*/meta.json`. Every panel below it is one further endpoint, and only
four of them have a live answer to prefer at all: the run payload, the log tail,
the PRD and the steering history proxy the container and fall back to the run dir;
the iteration, document, artifact, fault and cost views are **on-disk only by
design**, because the engine, the agent and `start` write those files into the run
dir themselves and a live proxy would only add a way to disagree.

- **Parked-question card** (task 007, v0.26, #269) — shown only when `status.
  pauseReason` is `operator_input_needed` or `rejected_repeated_signature`, the
  two pause reasons a plain operator RULING can actually answer
  (`ralphctl rule`/`ralphctl unpause`); `null`/absent otherwise, including a
  running run and every other pause reason (`stagnation`,
  `rejected_last_approach`, the dependency-edge pair, the two review-channel
  pauses). Read entirely on disk (`state.parked_question`, the same on-disk-
  first discipline `escalation_pause_markers` already uses) off the MOST
  RECENT matching `operator_escalation` event NOT YET ANSWERED by a later
  resume with no fresher matching escalation since, plus the on-disk
  structured findings sidecar — never a second live-only path. That
  answered-since check (cure task cure-01-02-4, PRD B.1, D.1; DoD 2/4) is
  what keeps a later task-boundary commit-secret-scan hold
  (`LoopSupervisor._pause_for_commit_secret_hit`, §4.6 `pauseReason` row),
  which persists the SAME `operator_input_needed` reason string with no
  fresh `operator_escalation` event of its own, from resurrecting an
  already-answered review gate's own question and its now-obsolete cure
  preview: once a resume follows that gate's escalation with no fresher
  matching one after it, `parked_question` returns `null` for the
  commit-scan hold instead. Carries: `reason`,
  `approach`/`maxApproaches`/`approachOfMax` (`"approach n of m"`),
  `parkedAt` (that event's own timestamp), `replanAvailable` (present only
  for `operator_input_needed`, `null` for `rejected_repeated_signature`,
  which never carries it), `blockingCount`/`residualCount` (the on-disk
  findings sidecar's own severity split, not merely the event's blocking
  list; each equals the number of `findings` entries of that severity), `findings` (one entry per finding, blocking AND residual, each
  `{id, severity, clause, evidence, cured_when, invalidates}`: first every
  blocking finding in the escalation's own order under the same 1-based
  `F<n>` labelling the escalation text and `ralphctl amend` already share,
  then every residual finding off the on-disk sidecar, in the sidecar's
  order, under its own 1-based `R<n>` labelling so a residual never shifts
  an `F<n>` id; a residual's `cured_when` is `null` when it has none and
  `invalidates` is `[]` when absent),
  and `consequence`: `taskIdsValidatedToPending`, `insertCount`,
  `costSpentThisApproach`, `tokensSpentThisApproach` (the same A.7 preview
  numbers the escalation event already carries) plus `nextStepIfUnanswered`
  (`"cure_pass"` \| `"archive_and_replan"`, §4 above) naming which of the
  two an unanswered pause is about to take. Every finding's `evidence` is
  carried whole, never truncated. An event lacking `approachOfMax` or the
  spend pair still yields them: `approachOfMax` from the event's `approach`
  and `status.maxApproaches`, the spend from `status.usage.byApproach[
  str(approach)]` (a parked run spends nothing while parked). The run-detail `ETag` (§ below)
  already moves whenever `events.jsonl` gains a new entry, so a fresh
  escalation is never served stale under an unchanged tag -- but `app.js`'s
  poll loop (cure-01-01-5, B.3-B.5) never rebuilds the panel over an
  UNRELATED move of that tag: it keeps the last rendered `parkedQuestion`
  as JSON (`lastParkedQuestionJSON`) and only calls `renderParkedPanel`
  again when the freshly-fetched `parkedQuestion` differs from it, so an
  unrelated event (a `notification.failed`, a fresh iteration's `meta.json`
  write, anything that moves the tag without changing which question is
  parked) leaves the operator's own in-progress draft name/ruling text
  exactly as typed. A genuinely different question -- a fresh re-park over
  new findings, or the pause getting answered/resumed (`parkedQuestion`
  turns `null`) -- always differs there and rebuilds: the panel disappears
  for an answered pause, and a re-park gets its own fresh, EMPTY answer box
  rather than the previous, unrelated question's stale draft. Task 008 (v0.26,
  #269) renders this payload: `app.js`'s `renderParkedPanel` builds a
  full-width **parked panel** at the very TOP of the run page (ahead of the
  summary card, and ahead of the Steer/Rule sections further down, which
  stay their own separate sections) whenever `parkedQuestion` is present --
  reason, approach n of m, how long the run has been parked, `replan
  available`, the blocking/residual counts, the consequence of not
  answering (spelled out in prose from `consequence`), and every finding by
  `id`/`severity`/`clause`, each a native `<details>` disclosure expandable
  to its own `evidence`/`cured_when`/`invalidates` -- the evidence block
  scrolls (CSS `max-height`/`overflow`) rather than truncating, matching the
  payload's own "never truncated" promise. `null`/absent `parkedQuestion`
  renders no panel at all. cure-01-01-5 (B.3-B.5, adopted #270 D.14) places
  the findings list in a LEFT column and the answer box plus its two
  actions in a RIGHT column at desktop widths (`.parked-panel-columns`,
  `min-width: 900px` in `style.css`) -- a live browser probe on an earlier
  checkout found both spanning the panel's full width and STACKED, the
  answer strictly below the findings rather than beside them. Narrower than
  that breakpoint the two columns stack vertically in the same order
  (findings, then the answer box, then the two actions) the panel already
  used before this fix. The runs list's own `\u23F8 parked for input`
  marker (task 017, A.7) becomes a link straight to `#/run/<id>` -- landing
  on the parked panel at the top of that page -- on both the runs-list row
  and the run-detail page's own state cell; the escalation-hold and
  undelivered markers stay plain text, since neither answers to a panel of
  its own. No new frontend dependency: the panel is built with the same
  `h()` helper every other view already uses. Task 009 (v0.26, #269) adds
  the panel's own answer controls, directly below the findings list, ahead
  of the Steer/Rule sections further down (which stay exactly as they
  were): a **waive checkbox** on every finding (`parkedFindingItem`'s own
  `#waive-<id>` input) whose ONLY effect is composing one editable line
  (`parkedWaiveLineText`: `waive <id> (<clause>)`) into the ruling
  textarea -- checking it sends no request, so no finding is ever waived
  by that click alone. Checking appends that line on a line of its own
  (after a newline if the text does not already end in one); unchecking
  removes only that finding's own line (`applyWaiveLine`, a pure text
  transform). Every other byte in the textarea -- hand-typed prose, blank
  lines between paragraphs, indentation, trailing whitespace, another
  finding's waived line -- is left exactly as it was, so a check-then-uncheck
  returns the text byte-identical, an UNTERMINATED last line included
  (cure-01-05: `applyWaiveLine` takes an optional 4th argument, every
  finding's own composed line text, letting it tell an operator's own
  unterminated last line apart from another finding's already-composed
  one -- the only case a plain suffix-strip is ambiguous about). cure-01-01-5
  (B.3-B.5) widens that same fix to the companion case `applyWaiveLine`'s
  4th argument alone cannot resolve: operator prose that ALREADY ends in
  exactly one real newline is byte-IDENTICAL, after `check()` composes a
  line onto it, to prose that had no trailing newline at all before
  `check()` inserted its own separator -- no amount of inspecting the
  resulting string can tell those two histories apart, so a fix that
  always strips one lone trailing newline on `uncheck()` silently ate the
  operator's own byte whenever the text already ended cleanly. The fix
  carries the one bit of real history through a 5th, caller-owned argument
  (`sepState`, an object `renderParkedPanel` allocates ONCE per rendering of
  a question and shares across every waive toggle) that `applyWaiveLine`
  mutates in place -- set only at the instant the composed region grows
  from zero active waive lines to one (recording whether IT inserted a
  separator), and consulted only at the mirror instant it shrinks back to
  zero, so an intermediate toggle (another finding already active, or
  still active afterwards) never touches it; a check-then-uncheck round
  trip, repeated any number of times, returns the text byte-identical
  whether it started empty, unterminated, ending in exactly one newline, or
  ending in a blank line. cure-01-01-6 (B.3-B.4, D.1, DoD 2) replaces
  that activeBefore-counted scheme with a simpler, exact one: sepState is
  now keyed by each composed line's OWN text, recording -- at the instant
  THAT line is inserted, never inferred later from shape -- whether ITS
  OWN insertion had to add a leading separator (the text did not already
  end in a newline right then), so a hand edit typed directly into the
  textarea between two checks (leaving it unterminated again with one
  waive line already active) is tracked correctly instead of being missed
  by a rule that only fired on the very first 0-to-1 transition. Removal
  now always locates a composed line with a boundary-anchored search (a
  real newline, or the start of the text, immediately before it, and a
  real newline immediately after) instead of a bare substring search --
  so a waive line's own text embedded inside an unrelated operator
  sentence ('Do not waive F1 (clause one (#111))' followed by its own
  line's newline and unrelated prose) is never mistaken for that composed
  line and spliced out of the operator's own prose. cure-01-01-7
  (B.3-B.4, D.1, DoD 2) fixes the one case that per-line scheme still
  got wrong: checking two findings, then unchecking the EARLIER one
  first, always stripped that line's own recorded separator even when
  the LATER finding's still-active line now sits immediately after it
  and still needs that exact byte to stay off whatever precedes it --
  merging, say, an unterminated `orig` straight into `waive F2 (...)`
  with no separating byte at all. Removal now strips a line's own
  recorded separator only when nothing composed follows it; when
  something does, the still-active composed line immediately after it
  inherits that debt (`sepState[thatLine] = true`) instead of losing it,
  so THAT line's own later removal -- once nothing follows at all --
  strips the byte this removal correctly left alone. Checking any number
  of findings and unchecking them in any order now always restores the
  exact original bytes once every composed waiver is gone, and every
  remaining composed waiver stays on its own removable line throughout.
  cure-01-05
  (adopted #270 D.14) gives the panel its OWN answer box -- a name field
  (`#parked-rule-name`) and a resizable 12-row ruling textarea
  (`#parked-rule-text`, `Enter` inserts a newline and never submits) --
  INSIDE the panel, right alongside the findings above and the two
  actions below, rather than reaching into the separate Rule section's
  own `#rule-text`/`#rule-name` further down the page; the waive checkbox
  now composes into this panel-own textarea. Two buttons follow, each
  naming its own effect in visible text right beside it (never only a
  hover title): **`Submit ruling &
  resume`** posts the panel's own textarea body unchanged -- byte-for-byte,
  never trimmed or reflowed -- together with the panel's own name field
  when it is non-blank, to
  `POST /api/runs/<id>/rule` (a body of nothing but whitespace is refused
  client-side with no request sent) -- the SAME route the separate Rule
  form below
  posts to, which resumes the run itself when the ruling answers the
  current pause (`LoopSupervisor.resume_if_ruling_answers_pause`); it
  never calls `/unpause` separately. **`Resume without a ruling`** instead
  posts straight to `POST /api/runs/<id>/unpause` (task 006) with no
  ruling recorded at all, and is labelled with the exact predicted outcome
  `consequence.nextStepIfUnanswered` already names (`parkedNextStepOutcomeText`,
  the same wording the consequence list above renders) -- "a cure pass over
  this approach's blocking findings" or "this approach is archived and the
  run replans" -- so an operator taking this action sees, before clicking,
  exactly what letting the pause go unanswered is about to do. Both
  actions report their own result (or the engine's own failure reason,
  `innermostReason`) in a shared status line beneath them, and the
  panel's own name/text fields are cleared on a successful ruling submit.
- **Summary card** — state, verdict, phase, approach as `2/3`, iterations,
  duration, and an explicit provenance row: `live: yes (proxied from container)`
  or `no (on-disk snapshot)`.
- **Usage panel** — total tokens and cost, plus the `byPhase`, `byApproach`
  and `byTier` breakdowns when present (the tier bucket also carries
  `iterations`, a plain per-tier iteration count). The cost cell opens a
  **cost dialog**: the same per-phase/per-approach/per-tier breakdown
  `ralphctl cost` prints, with quoted, derived and unavailable money each
  labelled as such (§8.6).
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
  `review-findings.md`, `composite-prd.md`, the debt ledger `debt.md` (task 006,
  requirement A.3) and the run's `job.yaml` **with
  secrets redacted**, each opening its body in a dialog. A document that was
  never written is listed as such rather than omitted: absence is an answer.
- **Artifacts panel** — what the job left behind under `artifacts/`, size and
  path per entry, with each row's dialog branching on the server-decided
  `kind` (§12.1) and never a filename's extension: a text artifact (the
  reflection report first among them) opens as text, a previewable image
  opens holding an `<img>` whose `src` is the raw byte route, and everything
  else (opaque binary, e.g. a tarball) is reported as such rather than
  mangled. `app.js` carries no client-side extension list of its own.
- **Router calls panel** — behind a native `<details>`/`<summary>` disclosure
  (task 022, P, #80), this run's own router decisions: `GET /api/runs/<id>/
  router` → `{runId, routerCalls: [...]}`, the very entries `ralphctl router
  <run> ls` prints, each labelled `chargesIteration: false` so a router call
  is never mistaken for a work iteration -- the router decides which of a
  state's legal edges to take when more than one is legal (§4, §9.4); it opens
  no `iterations/NNNN/` directory of its own and spends no approach. A run
  that never reached a router-ambiguous state (every shipped run, today --
  `transitions.ROUTER_FALLBACK` is `{}` by design) shows the panel's own
  empty-state sentence instead of nothing.
- **Approach history panel** — every previous approach `_archive_approach`
  copied into `approaches/NN/` before the loop started the next one, most
  recent last, each row its own archived task counts (never the live plan's)
  taken straight from that approach's OWN `tasks.json` — the same reader
  `ralphctl tasks <run> --approach N|--all` uses, so the two can never list a
  different set of approaches or disagree about one's counts. Expanding a row
  opens that approach's full task table plus its frozen `notes.md`/
  `review-findings.md`, shaped exactly like the state-documents panel's
  entries (absence is `(not written)`, never an error). A run still on its
  first approach shows no history at all rather than an empty panel.
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
  it. `#steer-message` (v0.26 task 010, #270) is a resizable `textarea` of
  about 8 rows, not a single-line `<input>` -- operator prose that can run
  to several paragraphs -- so `Enter` inserts a newline instead of
  submitting; only the form's own button submits. `#steer-name` (the
  message's optional `--name`) is unchanged, and the `apply now` checkbox
  (`ralphctl steer --now`) is labelled with its cost (#161): the label
  reads `apply now (cost: interrupts the running iteration -- its
  uncommitted work is lost; an idle loop is only woken; a running plan
  review is re-asked in place for free the first time, but a second
  interrupt in a row over a fresh, unworked plan spends a revision and
  re-ingests every task pending)`. cure-01-05 (adopted #270 C.12): that
  last clause discloses #161's existing plan-review cost with its real
  conditions -- a single interrupt of a running `plan_review` iteration is
  re-asked in place with no revision spent
  (`test_steer_now_against_a_running_plan_review_iteration_keeps_todays_
  161_cost`), exactly as a single `planning` interrupt retries in place
  (`test_steer_now_against_a_running_planning_iteration_keeps_todays_
  retry_in_place`); only a SECOND interrupt in a row over a fresh,
  not-yet-worked plan spends a revision and re-runs `planning`, which
  re-ingests every task `pending` off the fresh proposal
  (`test_steer_now_twice_against_a_running_plan_review_still_spends_a_
  revision`, all in `tests/test_transitions.py`). The label names that
  cost rather than changes it; interrupt/rejection semantics themselves
  are untouched.
- **Ruling form** — `#rule-text` (v0.26 task 010, #270) is a resizable
  `textarea` of about 12 rows (grown from 4), for the same reason and with
  the same `Enter`-inserts-a-newline/button-submits behaviour as the steer
  textarea above; `#rule-name` is unchanged. The form posts to the hub's
  `POST /api/runs/<id>/rule`, which forwards to the run's live `POST
  /rule` verbatim -- both routes are pure JSON proxies, so a multi-line
  message submitted through either textarea reaches the engine
  byte-identical to the same text sent with `ralphctl steer --file`/
  `ralphctl rule --file`. This section is separate from, and unaffected
  by, the parked panel's OWN `#parked-rule-name`/`#parked-rule-text`
  answer box above (§11.3's parked-panel paragraph) -- posting through
  either one reaches the SAME `POST /api/runs/<id>/rule` route.
- **Unpause proxy** — `POST /api/runs/<id>/unpause` (task 006) forwards
  (empty body) to the run's live `POST /resume`, the SAME engine route
  `ralphctl unpause <run-id>` calls, so there is no second unpause
  mechanism: an `operator_input_needed`/`rejected_last_approach`/etc.
  pause the run page shows is released through this one route, never a
  hub-owned copy of the pause/resume logic. `404` for an unknown run,
  `503` for an unreachable one, the engine's own status code passed
  through otherwise.
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

Only one dialog exists at a time and closing it removes it, so the refresh
loop running behind it — 4 seconds normally, backed off to 20 once the poll
settles (above) — cannot accumulate copies. Every dialog body is built
from text nodes — the strings in them are agent- and provider-authored — and is
formatted by the *server*, using the same modules `ralphctl` prints with, so the
browser displays exactly what the terminal shows.

### 11.4 Live transcript

The detail view carries a transcript tail, requested with `?tail=200`: the
last `tail` *rendered* lines — the same contract as `logs -N` — rendered by
the exact same `ralphd.cli.log_render` renderer `ralphctl logs` uses (with
`tty=False`, so no ANSI and no carriage returns). This is a **bounded indexed
read** whenever a usable index covers it: the server first reads the run's own
per-iteration render indexes (`render_index.json`, via
`ralphd.render_index.bounded_tail`) straight off disk, walking backward across
as many earlier indexed iterations as `tail` needs, so the cost is
proportional to `tail`, not to the run's history or to silent transcript; a
`GET /logs?tail=1` probe of the container's API then only decides the `live`
flag. When no usable index covers the request — a cold run with no index yet,
or an index the witness refuses — the fast path answers `None` and an
uncapped **correctness scan** takes over instead: it asks the container for a
bounded `GET /logs?tail=<bound>` (the on-disk merge with the same bound when
the API is unreachable), re-asking with a growing bound until enough rendered
lines exist, then trims to the last `tail` rendered lines. This growing-bound
fallback has no byte cap, and — unlike the indexed path — it can end up
asking for and rendering the *entire* available transcript even for a small
positive `tail`, exactly as `tail=0` does, when enough of the transcript is
silent or unindexable that no smaller bound yields the requested rendered-line
count; the render itself is still exact either way. When that scan was
expensive it caches an index built off the on-disk transcript in the hub's own
memory (never in the run dir), so a second request against the same iteration
with an unchanged transcript identity -- the same inode, size, `mtime` and
`ctime` -- takes the bounded indexed read instead of repeating the scan. A
same-size rewrite or replacement keeps the size but changes that identity, so
it invalidates that one iteration's entry and is rescanned. The cache is kept
per iteration: when a tail spans several cold iterations and only one of them
grows, is rewritten or is replaced, only that iteration's own entry is rebuilt,
and every unchanged iteration the same tail needs is served from cache, never
rescanned merely because a different iteration changed. `tail=0` is the one request that is EXPLICITLY for the whole transcript
(`GET /logs`, unconditionally) — it is not the only path capable of reading
all of it, since the cold-scan fallback above can reach the same result for a
positive `tail` too. The response, `{"live": bool, "lines": [...]}`, is never
an error.

**The read side's whole-call byte cost, index reads included, is bounded too
(task 013, v0.27 requirement D.1, #275).** The frozen 1,000,000-byte cap
(requirement C.2/G.2) covers every byte a call actually reads to answer, not
just the transcript: `render_index.bounded_tail`'s own `load_index` reads
`render_index.json` in FULL (there is no partial or bounded read of that
single monolithic JSON document — it must be parsed whole to validate its
ascending checkpoint order, per-checkpoint CRC/state shape and its identity
witness), so a call whose iteration carries a large index pays for that whole
file every time, whether or not the resolved tail ever touches most of the
transcript behind it. PRD Q's own canonical fixture (`gap_count=1,000`, no
prelude) stays comfortably under the cap this way. Both of its silent-prelude
depths (`gap_count=10,000`, prelude 1,000/1,700) do not: their own index is
dominated by 10,500+ one-checkpoint-per-raw-line records, and reading it whole
costs roughly three times the frozen cap, identically on a first call and a
repeated one, live or dead-run alike — a disclosed residual
(`tests/test_tail_whole_call_read_bound.py`, which pins the exact measured
figures), not a bound this wave closes: doing so would need `render_index.
json`'s own on-disk shape to become seekable/partial-readable without
weakening the write side's own one-checkpoint-per-raw-line density that
`tests/test_render_index_linear_writer.py` and `tests/test_render_index.py`
already pin as deliberate, shipped behaviour.

**The write side of that index is bounded too (task 012, v0.27 requirement
D.2, #275).** The engine's own live writer (`render_index.LiveRenderIndex`,
fed one raw line at a time by `engine/runner.py`'s `pump()` while an
iteration is still running) periodically rewrites `render_index.json` from
its whole accumulated snapshot. Publishing that rewrite at a FIXED
interval made the total cost of every publish across a whole iteration
quadratic in the transcript's own line count (each rewrite costs O(total
checkpoints so far), and there are O(N) of them). The writer instead
schedules its next publish geometrically -- not due again until the
checkpoint count has at least doubled since the last one, or grown by the
base batch size, whichever needs more lines first -- so the total
checkpoint records and bytes written across a whole iteration grow at most
linearly (a small ~2x constant) in the transcript's own final line count:
doubling the line count at most doubles either total.

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

**The agent owns it; the engine writes two things there.** Every phase
prompt carries the path in its `Job context` block (`- Artifacts directory:
<path>`), and the worker prompt states the rule from both sides — "Do not touch
the run state directory except tasks.json, the notes file, and the artifacts
directory" and "Put anything the operator should see (reports, screenshots,
logs) in the artifacts directory". The engine-written files under
`artifacts/` are `reflection/FAILED.md` (§12.2) and the run-record pair below
(v0.18 requirement B.1, #140).

| path | writer | content |
| --- | --- | --- |
| `artifacts/reflection/report.md` | reflect iteration | the post-mortem; its existence is the reflect phase's success condition |
| `artifacts/reflection/suggestions.diff` | reflect iteration | a unified diff of proposed prompt/skill edits, never applied by the loop — only ever by an operator's explicit `ralphctl suggestions <id> apply --yes` (§10.1) |
| `artifacts/reflection/FAILED.md` | engine | written only when the reflect phase produced no report, and removed again as soon as an attempt produces one (§12.2) |
| `artifacts/run-record.json` | engine | the run's own terminal record, machine-readable (below) |
| `artifacts/run-record.md` | engine | the SAME record, rendered for a human (below) |
| `artifacts/reports/*.md` | agent | a report the PRD asked for; only a `*-petitions.md` file is read back (§12.3) |
| `artifacts/screenshots/**` | agent | browser-driven verification of a UI change |
| anything else | agent | logs, samples, gate records — free-form by design |

**The run record (`run_record.py`, v0.18 requirement B.1, #140).** Neither
file is agent-produced, and neither is faulted for omitting or predating
anything a closing report claims (requirement B.2) -- it is the engine's own
account of the run, built only from files the engine itself owns
(`status.json`, `approach-metadata.json`, `petition-adjudications.json`,
`debt.json` (task 006, requirement A.3), the
steering ledger, `CONFIG_DIR/amendments/`, the archived `approaches/NN/`
findings sidecars) and never from a hat's live reasoning. `write_run_record()`
writes both files together, atomically, at every entry to `finishing`
(`LoopSupervisor._finish()`) and again on the later terminal transition that
promotes a recorded `finishing` write (`_promote_finishing_state()`/
`_promote_recorded_finishing_state()`), so a run that dies between `finishing`
and its true terminal state still leaves a record matching whichever write
actually landed. `build_run_record()` returns the JSON shape (identical to
`run-record.json`); `render_run_record_markdown()` renders the SAME record
for a human, one markdown section per entry in `RECORD_SECTIONS` so a field
added to the JSON without a renderer for it fails a test rather than quietly
going unrendered. Its top-level sections:

| section | content |
| --- | --- |
| header (`runId`/`generatedAt`/`state`/`verdict`/`reason`/`approach`/`maxApproaches`/`startedAt`/`endedAt`/`engineVersion`) | the run's own identity and terminal facts, lifted straight off `status.json` -- `engineVersion` (v0.21 requirement F.5, task 031) carries F.4's `status.json` field into both `run-record.json` and `run-record.md`, so a removed container does not take the answer with it |
| `usage` | `iterationsUsed` plus the full usage block: per-direction token fields and the `byPhase`/`byApproach`/`byTier` breakdowns (task 035, requirement B.1), never recomputed here -- `LoopSupervisor._accumulate_usage` is the one place that adds anything up |
| `approaches` | one entry per approach the run ever planned, oldest first: task tally, and each judge channel's (`review`/`verdictGate`) verdict with explicit `blocking`/`residual` arrays. Every entry reads the engine-owned per-approach metadata in `approach-metadata.json` FIRST (task 035) -- written the moment that judge's verdict was decided, so it survives approach archiving -- and falls back to the judge's optional findings sidecar (`review-findings.json`/`verdict-gate-findings.json`) ONLY for an approach judged before that metadata existed; each entry's `source` field (`"engine-metadata"` vs `"findings-sidecar"`) says which |
| `petitions` | every CONFIRMED impossibility-petition adjudication this run recorded (`petition-adjudications.json`), plus a `tampered` flag |
| `steersConsumed` | every steering message this run actually consumed (§4.6) |
| `operatorRulings` | every ruling found under `<config_root>/amendments/` |
| `notifications` | every `notification.sent` event the run emitted -- a failed send emits the distinct `notification.failed` event instead (task 009, requirement B.1, #197) and never appears here |

Tests: `tests/test_run_record.py` (the module's own unit tests, one per
section-builder) and `tests/test_run_record.py`
(a succeeded run and a run with an archived rejected approach, asserting the
record's own key set equals `RECORD_SECTIONS` and that every section renders).

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

#### Artifact kinds (task 022/023, issue #54)

An artifact is an arbitrary file the agent wrote, and the two surfaces that show
one (`ralphctl artifacts`, the hub's Artifacts panel) need to answer the same
question before rendering it: can this be printed, previewed, or neither. That
answer — `state.ARTIFACT_KIND_TEXT`, `state.ARTIFACT_KIND_IMAGE` or
`state.ARTIFACT_KIND_BINARY` — is decided **once**, server-side, by
`ralphd.engine.state._artifact_kind`, from the two sniffs `artifact_entry`
already runs over the file's leading bytes, and travels to both surfaces as
the entry's own `kind` field. Neither surface, nor the code that builds either
one, ever branches on a filename's extension: a file named `screenshot.png`
whose bytes are actually a ZIP (`PK\x03\x04`) is `ARTIFACT_KIND_BINARY`, and a
file named `payload.bin` whose bytes start with the PNG signature is
`ARTIFACT_KIND_IMAGE`.

- **The image allow-list is four raster signatures**, checked in
  `_sniff_image_mime` against the file's leading bytes only:
  `image/png` (`\x89PNG\r\n\x1a\n`), `image/jpeg` (`\xff\xd8\xff`), `image/gif`
  (`GIF87a`/`GIF89a`) and `image/webp` (a `RIFF`/`WEBP` container) —
  `state.ARTIFACT_IMAGE_MIME_TYPES`, the same tuple the raw byte route below
  draws its `Content-Type` from, so a fifth format cannot be added in one
  place and forgotten in the other.
- **SVG is deliberately excluded**, even though it is an image in the everyday
  sense: it has no raster magic-byte signature (it is XML text), and rendering
  agent-authored SVG inline — an `<img>` or `<object>` whose `src` is a run's
  own artifact — can execute an embedded `<script>` or event handler against
  the hub's own origin: stored XSS wearing a picture's extension, not a
  preview. A file that sniffs as SVG answers `_TEXT` (it decodes as text) or
  `_BINARY`, never `_IMAGE`, and stays behind the same `pull`/print path every
  other non-image file takes.
- **A previewable image is still capped at `state.ARTIFACT_INLINE_MAX_BYTES`
  (5 MiB)** before the raw byte route (`GET
  /api/runs/<id>/artifacts-raw/<path>`, task 023) will stream its bytes — an
  operator's browser has no business rendering an arbitrarily large file
  inline, and a job could otherwise make the hub's own response huge just by
  writing a big enough PNG. Over the cap, the route answers exactly like a
  non-image kind: `200` with today's `pull` wording (`state.ARTIFACT_BINARY`,
  "binary file -- copy it out with `ralphctl artifacts <run> pull`") as plain
  text, not bytes.
- **The raw byte route resolves a name through `state.artifact_relpath` only**
  — the one resolver the JSON artifact routes already use, so traversal and an
  absolute path are refused there and nowhere else needs its own check — and
  answers a `Content-Type` drawn **only** from `state.ARTIFACT_IMAGE_MIME_TYPES`,
  never `mimetypes.guess_type` (which would fall back to guessing from the
  URL's extension, exactly the thing this whole mechanism refuses to do).
  Every response, including a refused one, carries `X-Content-Type-Options:
  nosniff`.

`ralphctl artifacts ls`/`show` and the hub read the identical `kind` off the
same `artifact_entries()`/`artifact_entry()` dicts — one entry-for-entry
agreement, not two independent renderings that could drift.

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
  never applied by the loop.** The prompt says so, and the value of the phase
  depends on it: a loop that edits its own prompts mid-flight makes every later
  iteration unreproducible. Applying it is an operator's act on a host checkout,
  through `ralphctl suggestions <id> apply --yes` (§10.1), and it is the *next*
  `start` that runs the result — the content hash moved, so a new image is
  built.
- **What it may touch.** The prompt's hard constraint is `artifacts/reflection/`
  and nothing else: not the workspace, not `tasks.json`, `status.json`,
  `notes.md`, `review-findings.md`, steering files, nor anything under
  `iterations/`. That is instructed, not sandboxed — reflect has the same tool
  access as every other phase. The engine-level guarantee is narrower and
  absolute: the job's outcome — `verdict`, `reason`, and the terminal state
  it is going to reach — is **settled** before reflect starts and is never
  touched by it, whatever `state` says on disk in the meantime (below), and
  `phase` resets to `None` when it finishes, so a terminal run never looks
  mid-phase.

**The `finishing` state (task 012, #56) covers the reflect window itself.**
Until this task, `_run_job_core()` wrote the real terminal `state` (and
`endedAt`) the instant the outcome was decided — *before* the reflect
iteration ran — so every surface gated on `state`/`TERMINAL_STATES` (`rm`,
the hub's delete button, `ralphctl status`'s `ended:` line, a follower
reading `events.jsonl`) treated a run that was still doing agent work as
finished and safe to act on. With `reflect: true`, `LoopSupervisor._finish()`
now writes the non-terminal `finishing` state instead — a member of
`NONTERMINAL_STATES` (§5.2), so every zombie/liveness check still treats an
engine that dies in this window as owning the run, exactly like `running` —
plus `verdict` (the settled value; it does not wait for `state` to go
terminal) and `pendingTerminalState` (the value `state` is going to become).
`run_job()` calls `_promote_finishing_state()` once `_run_reflection()`
returns (whether reflect ran, failed, or was skipped outright because a
signal was already taking the engine down), which writes the real terminal
`state` from `self._pending_terminal` — the exact values `_finish()`
decided, never re-derived. With `reflect: false`, `_finish()` writes the
terminal state directly, exactly as before this task, and `finishing` never
appears at all.

A process that **resumes** a run dir already recorded `finishing` (the
engine died mid-reflect — a crash, a `docker stop`, a host reboot) has
nothing left to decide: `run_job()` checks `state == "finishing"` before
doing anything else and, if so, promotes straight from the recorded
`pendingTerminalState`/`verdict` without re-entering the job loop, without
re-running planning/worker/review, and without re-attempting reflect —
redoing any of them would let a dead process's stale opinion overrule one
that was already settled and written down. `ralphctl repair --set-state
<state>` reaches the identical outcome by hand, with no engine started at
all, since `pendingTerminalState` is sitting right there in status.json for
an operator or script to read.
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
verdict either way: `status.json` gains `reflect: {ok, error, endedAt}`, plus
`errorClass` on a failure (`null` while reflect is disabled or has not ended); a
`reflect_done` event carries `ok` and, on failure, `error`/`errorClass`; and on
failure `artifacts/reflection/FAILED.md` names the error, the fault class, the
timestamp, the run id and the job's own terminal state, and points at the last
`reflect` iteration's transcript and the `infra_wait`/`infra_retry` events
already spent on it.

**Two failure spellings, never one (task 014, #87).** #87's own incident — the
deadline fired mid-run, the reflect iteration returned no parseable contract
payload, and that window's `state`/`verdict` still came out `failed`/`unverified`
for a run whose work had actually held up — is a **degraded** reflection: the
iteration ran to a clean exit and left an untrustworthy or missing deliverable
(no `report.md`, an invalid/unparseable contract, or a contract that itself
disclaims `report_written`). `_reflect_failure()` (`loop.py`) returns
`REFLECT_FAULT_DEGRADED` for exactly that shape. Everything else — the iteration
never ran to completion, was interrupted, timed out, carried an in-band error, or
exited nonzero — is **fatal**, `REFLECT_FAULT_FATAL`: the iteration itself never
produced a trustworthy attempt at all. The two are never spelled the same way,
and neither one is wired to change the job's outcome: this task's decision is
that NO reflect failure, of either class, can fail a run — `_finish()` already
settled `state`/`verdict`/`reason` into `self._pending_terminal` strictly before
`_run_reflection()` is ever called, and `_promote_finishing_state()` promotes
only that recorded value, never anything reflect wrote. `REFLECT_FAULT_FATAL`
exists so a future change that decides a reflect failure SHOULD be able to fail
a run has a name to key off of that could never be confused with
`REFLECT_FAULT_DEGRADED`, which every existing `status.json`/CLI/hub consumer
already reads as "the job is unaffected".

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

**The tombstone is an assertion, so it only exists while it is true.** A run dir
outlives the attempt that wrote `FAILED.md`: a run whose reflection failed, was
resumed and reflected successfully the second time would otherwise end
`succeeded / verified` with a file beside its report claiming the post-mortem
never happened — and `terminal state: aborted (verdict unverified)` frozen from
the earlier episode. So a *successful* attempt deletes it
(`_clear_reflect_tombstone()`, one `reflect_tombstone_cleared` event, best
effort — a read-only artifacts dir must not turn a good post-mortem into a
crash). Deleting the file is also what retires the `reflect-failed` alias:
`ralphctl artifacts <run> ls` derives its rows from the files on disk
(`ARTIFACT_ALIASES`, §12.1), so there is no second piece of alias state to keep
in sync, and `artifacts show reflect-failed` goes back to exiting non-zero.
Only success falsifies a tombstone: the no-verdict path (§8.4, a signal taking
the engine down) neither writes nor removes one.

### 12.3 Reports

`artifacts/reports/` is a convention, not a mechanism: a PRD may ask for a
report and the agent writes it under the workspace's `artifacts/reports/`.
Apart from a `*-petitions.md` file there, which review reads from disk as
petition context ([docs/petition-format.md](docs/petition-format.md)), the
engine never reads a report, and no test in this repository checks one. What a run must deliver is working code proven by tests the CI
collects and a spec that does not contradict it; a report is at most a
pointer to that evidence. This repository ignores `/artifacts/` at its root,
so no run record, log or report is tracked here.

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
promises never to leak values, and it logs credential *names* only.
**`place_creds()`, like `place_skills()` and `check_hat_routes()`/`provider_keys()`
(#83 class), takes `home` as a required keyword-only argument with no ambient
`$HOME` fallback** — a caller that omits it gets a loud `TypeError` before any
filesystem write, never a silent rebuild of the real ambient `$HOME`'s cred
files; every production call site (`engine/main.py`'s startup, `engine/api.py`'s
creds/llm `PUT`/`DELETE` handlers) already has `skills.ambient_home()` on hand
and passes it explicitly. The mid-run LLM path's `apply_llm()` takes the same
required `home`. The handful of ambient-`$HOME` reads that genuinely are
correct as ambient — the CLI's own host-operator trust boundary, `skills.
ambient_home()`'s own sanctioned entry point, and a few more — are enumerated
with a one-line reason each in `src/ralphd/ambient_home_allowlist.py`, and
`tests/test_ambient_home_allowlist_guard.py` fails the suite the moment a new,
unlisted ambient read appears or a listed one goes stale. The inventory
reaches the agent the same way: every phase prompt gets a
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

**`--api-token` is a network control (who on the network may call the API at
all) and was never a container boundary (task 005/#62, requirement A).** It
predates and is orthogonal to the *container* boundary below — a second,
independent gate that closes the hole `--api-token` never covered: an
iteration running inside the same container and network namespace as the
engine's own control plane, over the same loopback port an operator's
`ralphctl` also reaches. `ralphctl start`/`resume` provision a
`JobConfig.control_token` **unconditionally**, whether or not the operator
ever passes `--api-token`, and it gates only the mutating half of the API
(`POST /steering`, `/rule`, `/interrupt`, `/pause`, `/resume`, `/retry`, `/abort`,
`/shutdown`, `PATCH /config/budget`, every `PUT`/`DELETE` under `/config`),
checked against `X-Ralphd-Control-Token`, a header distinct from `--api-token`'s
`Authorization: Bearer`. `PiRunner` strips `RALPHD_CONTROL_TOKEN` (and
`RALPHD_PORT`/`RALPHD_API_TOKEN`/`RALPHD_BIND`) from a spawned iteration's own
environment (task 003), so an iteration's own HTTP client can never present
it — asserted per mutating route in `tests/test_control_plane_auth.py`. See
`docs/api.md`'s "container boundary" section for the full route inventory and
the fact-5 measurement this closes. This is not a sandbox claim (fact 17): a
container with its own docker socket mounted can still reach the control plane
some other way; what this closes is the one demonstrated, cheap route.

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

#### Content-Security-Policy (task 024, issue #54)

Every hub response — the static bundle, every JSON route under `/api/`, and the
raw artifact byte route (§12.1) — carries the same `Content-Security-Policy`
header, `ui_server.HUB_CSP`, sent from the two shared response helpers
(`_send_json`/`_send_bytes`) so no route can forget it:

```
default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self';
connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

No `'unsafe-inline'`/`'unsafe-eval'` anywhere — the static bundle has no inline
`<script>`, no `onclick="..."` attribute and no inline `style="..."` attribute
or `<style>` block, so the tight policy costs the hub nothing. The point is the
same one `X-Content-Type-Options: nosniff` serves on the raw artifact route: a
file an agent inside the job container wrote — an artifact, a note, a PRD, a
reflection report — is served back to a browser at the operator's own origin,
and this is the barrier that stops one of those files turning into script that
acts against that origin. It applies to the hub only; the engine's own HTTP API
(§9) is a JSON control surface with no HTML rendering and carries no such
header.

### 13.4 What the agent is allowed to do

Inside its container, essentially everything. That is the design, not an
oversight: a loop that has to ask permission cannot run for twenty hours
unattended, and the container plus a scoped credential is the boundary that makes
broad latitude affordable. The agent runs as non-root `agent`, owns `/workspace`
completely, has normal outbound network, and — with `--allow-docker` — the socket
of §13.2. **The one thing it may not do is end its own run** (v0.7, §13.4's uid
boundary below).

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
other processes inside its own container: a `SIGKILL`/`SIGTERM` reaches the
running `pi` subprocess (its own iteration) directly, because they share one PID
namespace. What it *does* protect — since v0.7 — is itself.

**The uid boundary: an iteration cannot signal its supervisor (#48).** The engine
is PID 1 with the argv `ralphd-engine`, and the image used to end in `USER agent`,
the same uid iterations run as, so `pkill -f ralphd-engine` from inside a task was
both a match and permitted — it ended a 9h36m run. `container/Dockerfile` now sets
**no `USER`**: the entrypoint and the engine start as root, and
`engine/privsep.py:separate_engine_identity()` runs `setresgid(0, agent_gid, 0)`
and `setresuid(0, agent_uid, 0)` before anything else in the process — before the
first file is written, before the API listens.

- **Real and saved uid stay `0`**, and `kill(2)` permission is decided by exactly
  those two, so no process running as `agent` can signal the engine: not by pid,
  not by process group, not by any `pkill`/`killall` pattern (`ptrace` follows the
  same rule). The attempt is `EPERM`; the iteration carries on as an ordinary
  iteration and the run keeps going.
- **The effective uid stays the agent's**, and file ownership is decided by that,
  so every file the engine writes into the bind-mounted run dir, the workspace and
  `$HOME` is owned by uid 1000 exactly as before. This is why the cheap route was
  the right one: the `$HOME` config overlay (§5), the `~/.creds` files pi reads
  (§13.1), the git identity commits are made under, and the host user's ability to
  read and delete its own run dir are all unchanged. An engine holding a *third*
  uid would have had to give one of those up.
- **Each iteration is dropped all the way down**, `user=`/`group=` on the spawn
  (`privsep.agent_child_kwargs()`), which moves the child's saved uid too — no
  half-dropped iteration that could climb back. `on_complete_cmd`, being
  operator-supplied shell, runs under the same drop. Signals still flow *downward*:
  the engine's `interrupt()` can `killpg` its own iteration.
- **No boundary is claimed where there is none.** The engine also runs unprivileged
  — the whole test suite does, `docker run --user 1000` does, and a *derived* image
  (`--base`, §8) still ends in `USER 1000` — and then every privsep function is a
  no-op that logs one warning, leaving the pre-v0.7 arrangement rather than
  refusing to start. `privsep.separated()` reads the live process credentials, not a
  startup flag, so nothing can report a boundary the kernel is not enforcing.

**The task record is root-owned under the same boundary (task 105, #60
requirement D).** `RunDir.write_tasks()` — the sole authorized writer of
`tasks.json` (§3.5, task 013) — routes its atomic write through
`privsep.write_root_owned_json()`, which uses `privsep.root_for_write()` to
raise the engine's own *effective* uid back to `0` for exactly the duration of
that one write (`setresuid(0, 0, 0)` … write … `setresuid(0, agent_uid, 0)`,
restored in a `finally` so a write that raises never leaves the effective uid
at root) and then `chmod`s the file `0644`. Under the boundary `tasks.json`
ends up owned by root, readable by the agent uid but not writable by it —
**hand-editing a live run's task record from inside a job container is no
longer available**; `ralphctl steer` is the channel an operator uses instead.
Every other file the engine writes stays owned by the agent uid exactly as
above; this is the one deliberate exception. Without the boundary
(`separated()` false) the write degrades to an ordinary agent-owned write with
one warning per process, the same discipline as every other privsep function —
never a second code path. Proved in a real container by
`tests/test_engine_uid_docker.py` (ownership, mode `0644`, and a `docker exec
--user 1000:1000` write attempt refused with `Permission denied`); the
degrade and the elevate/restore bracketing are covered at the fast-lane tier
by `tests/test_engine_uid_boundary.py`.

**An operator ruling review must honour, unforgeable from inside (#26).**
Review trusts nothing the run itself produced — `tasks.json`, commit
messages, its own notes are all things a phase running inside the container
can write — so "let the operator write text review will read" cannot mean
another file under `/run/ralphd`; a prompt instruction not to touch it would
be policy, not a property, exactly the distinction this section already
draws. `ralphctl amend` (§10.4) instead writes, from the HOST process, into
`config_root(run_id)`, the SAME directory already bind-mounted `:ro` as
`/config` — the one thing `job.yaml`/`prd.md` already lean on for "the
operator configured this, the run didn't." The job container carries no
`--privileged`/`--cap-add` that could remount it writable, so a file placed
there after the container starts is something every phase (planning, worker,
review, verify, reflect) can read but provably cannot write. `engine.loop`'s
`_operator_amendment_review_context()` reads `CONFIG_DIR/amendments/`
directly at every review-phase prompt build — deliberately never through the
writable config overlay (§6.4), which lives inside the container's own
filesystem and is reachable by the worker's shell with no API needed, which
would reopen exactly the forgeability this channel exists to close — and
`review.md`'s "Operator rulings" section states the rule the reviewer follows:
treat every ruling found there as authoritative, because of where it lives,
never because of how it reads. A third ruling kind (v0.14, requirement E),
`mode: status`, travels the same channel but never reaches that review
context: it is applied to `tasks.json` by the engine itself at load time and
at every later worker/review step boundary
(`LoopSupervisor._apply_operator_status_rulings()` calling the single applier
`engine.state.apply_amendment_status_rulings()`, which validates the move
against `transitions.TASK_STATUS_TABLE`/`TASK_STATUS_VIGILANT_DELTA`, refuses
an illegal one, leaves `validationAttempts`/`tierLadderExhausted`/
`successCriteria` byte-identical, and emits `amendment_status_applied` with
`source: operator`).

**The operator answer channel (task 013, v0.23 requirement A.2, #224) is
deliberately NOT built on this unforgeability property.** `ralphctl rule
--file`/`POST /rule`/the hub textarea all need a write path reachable from
INSIDE the container (`POST /rule` is handled by the very process this
section is about, not by a host-side `ralphctl` process the way `amend`
is), so `rulings/` lives under the RUN root, bind-mounted read-write, not
under `config_root`. Its trust rests on a different property: there is
exactly ONE writer path to it (`RunDir.add_ruling`, reached identically by
the CLI, the API and the hub proxy), so a ruling the operator did not
submit can only appear there if something already holds control-plane
write access to this run — the same trust boundary `ralphctl steer`
already relies on, not a NEW one. `review.md`/`task-verify.md`'s "Operator
answer-channel ruling(s)" section is deliberately a different heading from
"Operator rulings" in the same prompt, precisely so neither hat conflates
the two channels' trust justifications.

What is left: the derived-image path above, and PID-namespace isolation per
iteration (§15), which would additionally let the engine tell "stop this one
iteration" from "the container is being torn down".

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
| **Doc and guidance consistency** | greps the tree, walks the real argparse tree (`cli.main.build_parser`) and renders real prompts | `test_docs_consistency.py`, `test_prompt_lint.py`, `test_sibling_cleanup_guidance.py`, `test_toolchain_sibling_guidance.py` | the docs, prompts and example skills say what the code does — including that every documented flag, subcommand, route and response field exists | milliseconds |

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
| The auto-resume default lives in exactly one literal: `AUTO_RESUME_DEFAULT` in `src/ralphd/cli/main.py`. Every reader — the flag layering, the registry-config default, the pre-existing-run fallback, `doctor --fix` — goes through it, and the tests are parameterised over its value rather than spelling the boolean out, so flipping it is a one-line change that does not rewrite the suite. v0.7 flipped it (§8.8) and that is exactly what it cost: the one literal, the two tests that are *about* the default, and the fixtures that meant "an opted-out run" and now say `--no-auto-resume`. | `tests/test_cli_auto_resume.py` (imports the constant and greps the source line) |
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

- **PID-namespace isolation of agent iterations.** v0.7 closed the hazard that
  made this urgent — an iteration signalling the *engine* — with the uid boundary
  of §13.4, which is kernel-enforced, costs the agent nothing it legitimately
  needs, and is a Dockerfile plus a `setresuid` pair. What a namespace would still
  add is a different capability, not more protection: an iteration in its own PID
  namespace lets the engine distinguish "stop this one iteration" from "the whole
  container is being torn down", and it also contains the
  `docker`-commands-filtered-on-a-label variant of the same hazard, which no uid
  can. Still deferred, and no longer load-bearing for containment. A `SIGKILL` from
  inside the container does still reach the running `pi` subprocess — that is the
  agent's own iteration, and requirement A1's prompt rule is the answer to it,
  because an iteration ending itself costs one iteration and not the run.
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

---

## 17. Answered questions

Questions that stood in §16 until a wave had the evidence to settle them. They
are recorded here with the answer and what it was decided *from*, rather than
edited in place in §16, so the reasoning that closed one is still readable next
to the reasoning that opened it. §16's remaining questions renumbered when an
entry moved out of it; each entry below names the number it used to carry.

1. **Can a provider-side stream abort mid-iteration be told from an operator's
   abort?** (§16 question 1 through v0.6; answered by task 014, #49 part 2;
   refined by task 117, steering 005, `selfdev-v09-fsm-loop`.)
   **Yes, for the case that mattered, and from the error's shape, not a
   clock.** Not from the text alone — `pi` records both as the bare in-band
   error `aborted` — so the ladder decides it from the loop's own bookkeeping
   plus the rest of the iteration's own signal: with an abort recorded it is
   `"work"` and never retried (§8.2 step 2); with a signal after traffic it
   is `"signal"` (step 5); with traffic, a clean exit and no recorded abort
   it is `"infra"` (step 6, retried and refunded), **at any duration**.

   Task 014's first answer used a duration bound instead of the exit-code test
   above: `faults.ABORTED_STREAM_MAX_DURATION_S` (120s), derived from a SAMPLE
   OF ONE. In `selfdev-v06-release`, iteration 145 recorded `error: aborted`
   with `faultClass: "work"` **39 seconds** into a 45-minute
   `iteration_timeout_s` — the leading edge of a DNS outage whose next five
   iterations matched an infra signature and were correctly retried and
   refunded. The constant was set at 120s: three times that observed 39s.
   Task 014 itself flagged the residual it left: "the same shape more than
   120s in" still classified `"work"`.

   That residual is exactly what cost `selfdev-v09-fsm-loop` an approach.
   Every one of that run's 15 bare-`aborted`-after-traffic incidents was a
   genuine provider-side abort; 14 of the 15 landed *above* 120s (115s, 156s,
   173s, 174s, 218s, 272s, 279s, 307s, 339s, 364s, 530s, 800s, 996s, 1439s,
   2542s), and three of them in a row (94/100/101) tripped the stagnation
   guard and failed the approach for a plan that was not actually stuck. A
   duration bound calibrated on one 39s sample could not have been raised to
   fix this without failing on the next outage that ran longer still — a
   provider can hang up on a stream at any point, so the population of
   genuine abort durations has no upper bound to calibrate a clock against.
   Task 117 retired `ABORTED_STREAM_MAX_DURATION_S` and decided step 6 on
   shape alone instead: a bare `aborted`, a clean `exit_code == 0`, traffic
   present, no signature, not interrupted, no abort recorded. An agent that
   fails at its own work reports what failed; it does not exit 0 with the
   single word "aborted" as its whole error text, whatever the duration.

   What is deliberately *not* answered: the residual shape this still
   misclassifies. See §8.2's "What is left, deliberately".

2. **Should the reflection diff ever be applied automatically?** (§16 question 6
   through v0.6, question 5 after task 014's move; answered by tasks 028, 041
   and 029, requirement P.) **No — and it no longer has to be, because an
   operator can now apply it in one command.** `suggestions.diff` stays a
   proposal by construction: nothing in the loop applies it and the reflect
   prompt still forbids the agent from doing so, so every run remains
   reproducible against prompt material that was known before it started. What
   that cost, until v0.7, was the whole self-improvement loop: v0.6's diff was
   read by hand and retyped as eight GitHub issues, which is exactly Phase 0 of
   this wave's PRD. `ralphctl suggestions <id> [show|apply --yes]` (§10.1)
   closes the gap from the *host* side — it plans a diff against a checkout,
   prints the diff and the files it would touch before the single all-or-nothing
   writer runs, refuses what does not fit naming the hunk, and leaves the **next**
   `start` to build the changed tree, because the content hash of `src/ralphd/`
   moved. The loop never gains write access to its own prompts; an operator's
   explicit `--yes` does.

   **Never mid-flight**, and that is a refusal rather than a convention: an
   `apply` is refused, exit 1, while any run recorded non-terminal *whose
   container is really running* is tied to the target tree — either because
   `host.json`'s `imageHash` says it is executing the image built from exactly
   this tree, or because the tree is (contains, or sits inside) its mounted
   workspace. The first tie is the subtle one: the write cannot reach the live
   container at all, since the prompts are baked into its image, and that is the
   damage — the checkout would silently stop describing the run being watched.
   No override exists because none is needed: `--target` a copy, or wait, and
   the diff still reaches a job only through the next `start`.

   **Two runs proposing edits to the same prompt** are refused with both diffs
   named, never merged: while another run's `suggestions.diff` also still applies
   to a file in this plan, `apply` exits 1 naming that run, its diff and the
   shared paths. Applying both would not corrupt anything — the matcher is
   context-exact — it would produce a third version of the prompt that neither
   run proposed and no one reviewed as a whole, and nothing would say so.
   `--ignore-conflicts` is the operator's explicit "this run's suggestions win";
   the diff that lost is then re-planned against what was actually written and
   refused by the matcher (`the change is already present`, `no such context`)
   rather than quietly landing on top. A sibling diff that no longer applies is
   not a conflict at all: it can never be written, so it can take nothing.

   **Taking six of eight suggestions** is `--only PATH` (repeatable, task 030,
   requirement P part 3): a selection by whole file, made on the parsed diff
   *before* a plan exists (`patch.select_patches`), after which everything
   downstream — the plan that is printed, both refusals above, the
   all-or-nothing writer — sees only what was selected. That is the opposite of
   writing the hunks that happened to apply: the tree that lands is one the
   operator named and was shown, and the files left out are printed beside the
   plan (and in `--json`'s `skipped`) so a partial apply announces itself.
   Hunk-level selection is refused, with the reason: a `@@` header is not a
   stable name to select by, since the same file's hunks renumber the moment one
   of them applies, while a path is exactly what the plan prints. A `--only`
   path the diff does not touch is a usage error listing the paths it does
   touch, never a quiet apply of the rest. The hub surfaces none of this: it can
   read `suggestions.diff` as an artifact and nothing else, so applying the
   loop's own material stays an act typed at a terminal.
