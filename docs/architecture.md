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
- **One task per worker iteration.** Checkpointing, steering application, and
  vigilant verification all key off iteration boundaries; a worker that batches
  several tasks into one iteration silently bypasses all three. The worker
  prompt makes this the headline rule (with the *why*, since capable models
  treat unexplained rules as optional efficiency advice), and the engine
  detects violations by diffing task statuses around each worker iteration,
  emitting a warning `log` event when more than one task completed. The engine
  cannot roll extra completions back — detection is for operator visibility
  and prompt regression testing.
- **Iteration failures are contained.** Any engine-side error while running an
  iteration (stream overflow, OS error, agent crash) fails that iteration —
  recorded in its `meta.json` and events — and the loop continues; only budget
  exhaustion, an explicit abort, or an unrecoverable engine bug ends the job.
  The runner must never leave an orphaned agent process behind (kill the
  process group on any exit path).
- **Task status transitions are visible live, not just at iteration end.**
  The worker prompt makes flipping the picked task to `in-progress` in
  `tasks.json` the mandatory first write of the iteration — before any other
  file edit or shell command. While the agent subprocess for a worker
  iteration is running, the engine runs a lightweight background poller
  (`LoopSupervisor._poll_task_changes`, ~4/s) that re-reads `tasks.json` and
  emits `task` events for any status change immediately, instead of only
  once via the post-iteration `_emit_task_changes()` call. An operator
  watching `GET /events` or `GET /tasks` (or `ralphctl`) therefore sees
  `pending -> in-progress` for the exact task being worked while that
  iteration is still in flight, not only after it ends.

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

A verify iteration that errors out mid-stream (agent/provider failure -- e.g. a
Bedrock 502 -- surfaced as an assistant `message_end` with `stopReason: "error"`)
before ever emitting the `<task-verified>{id}</task-verified>` sentinel is an
infrastructure fault, not a verdict, and must never be scored as a validation
failure: the engine retries verification (bounded, `MAX_VERIFY_ERROR_RETRIES = 3`)
without touching the task's `status` or `validationAttempts`. If every retry also
errors (or the iteration budget runs out first), the engine gives up on verifying
that task for now, logs an error event, and leaves its status exactly as the
worker left it -- it is not marked `validation-failed`, `failed`, or otherwise
penalized. The verify iteration's `meta.json` records `verifyOutcome: "error"`
for these attempts (distinct from `"pass"`/`"fail"`).

### Model strategy

Each phase resolves its model independently: per-phase override → strategy preset →
job default. Presets: `quality-first` (default; one strong model everywhere),
`cost-optimized` (strong model for planning only), `balanced` (strong for planning +
review). "Strong" and "fast" tiers are just two model IDs in job config — any model
pi can reach is valid in either slot. The engine sets the model per-iteration via
pi's model selection flag/env on the subprocess.

### Self-reflection phase (PRD req 24)

Optional (`reflect: true` in job config, `ralphctl start --reflect`). After the
job's normal loop (`LoopSupervisor._run_job_core()`) reaches a terminal state
(`succeeded`/`failed`/`aborted`), the engine runs exactly one extra `reflect`
iteration (`LoopSupervisor._run_reflection()`), using its own builtin prompt
(`src/ralphd/prompts/reflect.md`, phase name `reflect`, model resolved per the
job's model strategy exactly like any other phase). This iteration is *not* part
of the normal budget accounting gate (`budget_left()`); it always runs once, even
if the iteration budget was already exhausted when the job reached its terminal
state.

The reflect prompt instructs the agent to analyze the run's PRD, final
`tasks.json`, `notes.md`, and iteration records, and write a report plus an
optional unified diff of proposed prompt/skill improvements to
`artifacts/reflection/` — and to touch nothing else (workspace files, `tasks.json`,
`status.json`, `notes.md`, `review-findings.md`, steering files, or any
`iterations/` content). This is instructed, not sandboxed: the reflect iteration
runs with the same tool access as any other phase, so it is trusted the same way
review/verify iterations are trusted to follow their own prompts. The one
engine-side guarantee is that `run_job()`'s returned final state and the job's
terminal `status.json` fields (`state`, `verdict`, `endedAt`) are set *before*
the reflect iteration runs and are never touched by it; the engine does reset
`status.json`'s `phase` field back to `None` immediately after the reflect
iteration finishes (it's transiently set to `"reflect"` while it runs, the same
as every other phase), so a terminal job never appears to still be mid-phase.

With `reflect` absent/`false` (the default), no extra iteration runs at all —
`run_job()` is a no-op wrapper around the same core loop as before this feature.

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
      "validationAttempts": 0,
      "dependsOn": ["000"],
      "priority": 0
    }
  ],
  "discovered": {}
}
```

`dependsOn` (list of task ids) and `priority` (number, higher = more
important) are OPTIONAL per task, added by the planner only when the plan
genuinely needs cross-task ordering or prioritisation (see
`prompts/planning.md`). The worker's pick rule (`prompts/worker.md`): among
`pending` tasks whose `dependsOn` are all `completed`, pick the highest
`priority` (missing = 0), ties broken by list order; a task blocked by a
`failed`/`skipped` dependency is never silently ground against — the worker
notes the blockage in the handoff notes file and moves on. With no
`dependsOn`/`priority` fields anywhere in the plan this is identical to
plain sequential list-order picking. This rule lives entirely in the phase
prompts (the engine does not parse or enforce it in code) — it is the agent
reading `tasks.json` and the prompt's instructions, same as task selection
always has been.

The **workspace** (`/workspace`) is separate from `/run`: it is the repo checkout the
agent edits. Two modes, chosen per job:

- `--workspace <host-dir>` — bind-mount an existing checkout (preferred; the
  operator's normal working copy or a dedicated clone)
- no workspace flag — the engine creates `/workspace` on the run dir
  (`runs/<id>/workspace/`) and the planning iteration clones the PRD-listed repos
  using whatever git credentials were injected

**Multi-workspace jobs** (PRD req 27): `--workspace` is repeatable. A single
bare `--workspace <dir>` keeps the single-mode behavior above (mounted at
`/workspace`). Two or more `--workspace` flags each require a `:name`
(`--workspace <dir>:<name>`) and are mounted at `/workspace/<name>` side by
side instead — one job container, several checked-out repos. The container
gets `RALPHD_WORKSPACES=<comma-separated names>`, which every phase prompt's
"Job context" section reads to list each mounted name/path explicitly, so
the agent never has to guess what's under `/workspace` from a directory
listing. `host.json` records the mapping as `workspaces` (name→host path)
instead of the single-mode `workspace` key, and `ralphctl resume` remounts
every named workspace the same way on the fresh container.

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

### Live job log (pretty console)

The per-iteration transcripts are raw pi NDJSON — correct as a record, unreadable
as a console. The engine therefore exposes a **whole-job log view** that spans
iteration boundaries (`GET /logs`, see [api.md](api.md)): a merged stream of every
iteration's transcript in order, with iteration/phase boundary markers, that can
be fetched as a bounded tail or followed live across iterations (no re-attach
dance when a new iteration starts). The engine serves **raw NDJSON**; making it
pretty is the CLI's job:

- `ralphctl logs` renders the stream human-first by default, Jenkins-console
  style: iteration/phase headers, assistant text as it streams, tool calls as
  one-liners (name + compact args + outcome), thinking elided to a marker,
  per-iteration usage/cost footers, agent errors highlighted.
- `--raw` emits the underlying NDJSON untouched (for machines and debugging).
- The syntax follows `tail`: `logs -100` (last 100 rendered lines), `-150f`
  (last 150, then follow), `logsf` (follow from now). See [cli.md](cli.md).

The renderer is pure presentation over the same events — nothing is stored
pretty; the run dir stays raw and replayable.

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

### Completion hook (PRD req 26)

Optional `on_complete_cmd: <shell command>` job option (`ralphctl start
--on-complete-cmd '<cmd>'`, or a job.yaml field a template can supply). The
engine runs the command exactly once, in-container, via `asyncio.create_
subprocess_shell`, strictly after the job has reached a terminal state
(`succeeded`/`failed`/`aborted`) — including after the reflect iteration
(PRD req 24) when `reflect: true`, since both share the single point in
`ralphd-engine`'s `amain()` right after `loop.run_job()` returns. The hook
receives the process's own environment plus three vars: `RALPHD_RUN_ID` (the
job's configured run id), `RALPHD_STATE` (the final state string), and
`RALPHD_VERDICT` (`status.json`'s `verdict`, `"verified"`/`"unverified"`, or
empty string if somehow absent). A nonzero exit, or the command failing to
spawn at all, is recorded as an `events.jsonl` `log` event at `level: error`
(with a tail of stderr/stdout) — it is purely observational: the hook can
never change the job's `state`/`verdict` or the engine process's own exit
code. With `on_complete_cmd` unset (the default), nothing extra runs.

A **job timeout** (wall clock, default 8h) and per-iteration timeout (default 45m)
bound runaway runs; hitting either aborts the current iteration and, for the job
timeout, fails the job.

### Steering

Steering files are markdown notes dropped into `steering/` (via API or by writing
the mounted dir directly). At each iteration start the engine checks for
*unconsumed* steering files, but only **actionable** phases -- `planning` and
`worker`, whose prompts explicitly instruct the agent to act on operator
guidance -- include the full steering text and mark it consumed (recorded in
`meta.json`'s `steeringConsumed` and in `steering/.consumed.json`). The
`review` and `verify` phases are pure verification roles ("Do NOT fix
anything yourself"; nothing in their prompts tells the agent to act on
steering), so if a steering file arrives while a worker iteration is in
flight and the very next iteration boundary happens to be a review or verify
iteration, that iteration only sees a passive notice (file names, no content,
not marked consumed) and leaves it pending -- it is picked up and consumed by
the next planning/worker iteration instead. This prevents steering from being
silently discarded (recorded as "consumed" yet never actually acted on) when
it happens to land just before a non-worker-bound phase.
`POST /interrupt` gives the "right now" variant: SIGINT the current iteration so the
next one starts immediately with the new guidance. Steering is guidance for the
agent; it does not mutate `tasks.json` directly.

### Self-protection

`ralphd-engine` is defensive about being invoked accidentally against a run dir
that is already live (a bare `ralphd-engine --help`, or a stray second process
pointed at the same `RALPHD_RUN_DIR`, must never double-write `events.jsonl` /
`status.json` into a running job):

- **`--help` / `--version` are argument-parsed up front** (`argparse`, in
  `build_arg_parser()`) and exit `0` via `SystemExit` *before* `amain()` runs —
  no config load, no directory creation, no server, no lock. This makes them
  safe to run bare, with no `RALPHD_*` env, from any cwd.
- **At startup the engine takes an exclusive, non-blocking `flock` on
  `<run-dir>/.lock`** (`RunDir.acquire_lock()` in `engine/state.py`). If another
  live engine already holds it, the new process prints a diagnostic naming the
  run dir to stderr/log and exits **`3`** (`EXIT_RUN_DIR_LOCKED` in
  `engine/main.py`) without touching any other state file; the holder is
  unaffected and keeps serving. The lock file records the holder's PID for
  diagnosis but the flock itself (not the PID content) is what's authoritative.
  Because `flock` is process-lifetime (kernel-held, not a lock file whose mere
  *existence* is checked), it is released automatically on any process exit —
  including `SIGKILL` — so a killed engine never leaves a stale false-positive
  lock behind; a fresh engine started immediately after can acquire it.

### Run-dir schema version (PRD req 18)

`status.json` carries a `schemaVersion` integer, stamped by the engine on every
startup (`RunDir.check_schema_version()` + the `update_status(...,
schemaVersion=CURRENT_SCHEMA_VERSION)` call in `engine/main.py`). It tracks the
run-dir *on-disk shape* (files/fields the engine relies on), not the job or
software version. Policy, checked immediately after the run-dir lock is
acquired and before anything else in the run dir is touched (no workspace
creation, no PRD copy, no `status.json`/`tasks.json` write):

- **Recorded version newer than this engine build's `CURRENT_SCHEMA_VERSION`**
  (an older engine pointed at a run dir a newer engine already advanced) →
  refuses to start: prints a diagnostic naming *both* versions to stderr/log
  and exits **`4`** (`EXIT_SCHEMA_TOO_NEW` in `engine/main.py`), touching
  nothing else.
- **Recorded version older than current, or absent** (a pre-schema run dir
  from before this feature existed reads as version `0`) → accepted; the
  engine proceeds normally and stamps `schemaVersion: CURRENT_SCHEMA_VERSION`
  into `status.json` on its first status update. There is currently only one
  schema version (`1`), so "upgrading" an older run dir is just this stamp;
  the check exists so a future on-disk shape change has a documented,
  enforced upgrade/refusal point to hang real migration logic off.
- **Recorded version equal to current** → no-op, same stamp is rewritten.

### Engine resume-from-existing-state (PRD req 16, engine side)

A container is disposable; the run dir is not. `ralphd-engine` can always be
started fresh over an *existing* run dir (same mounted `<run-dir>`, same or
adjusted `job.yaml` iteration budget) and picks up where the previous process
left off, instead of re-planning from scratch. This is what backs
`ralphctl resume <run-id>` (task 029, CLI side) and recovery after a killed
container (task 030, crash-consistency), but the detection and resumption
logic itself lives entirely engine-side and needs no CLI/API cooperation.

- **Iteration numbering.** `LoopSupervisor.__init__` seeds
  `self.iterations_used` from `RunDir.max_iteration_number()` -- the highest
  `iterations/<NNNN>/` directory whose `meta.json` already has an `endedAt`
  field (a genuinely *completed* iteration; a half-written directory left by
  a process killed mid-iteration is not counted and its number is reused).
  On a fresh run dir this is `0`, so numbering is unaffected; on a run dir
  with `N` completed iterations already on disk, the very next iteration is
  numbered `N+1`. This one seed is what makes numbering monotonic across
  restarts with no other change needed.
- **Skipping planning.** `LoopSupervisor._resume_point()` decides where
  `run_job()` starts: if `tasks.json` already has tasks *and* at least one
  completed iteration is on disk, it resumes the approach recorded in
  `status.json`'s `approach` field (that field persists across restarts --
  `update_status()` merges, it never resets fields it isn't given) and skips
  straight into the worker loop for that approach, emitting a `log` event
  ('resuming existing run-dir state: approach N, K iteration(s) already
  recorded; skipping planning') so the resume is operator-visible. A
  genuinely fresh run dir (no tasks yet, or `max_iteration_number() == 0`)
  is completely unaffected -- planning still runs first, exactly as before.
- **Budget accounting.** Because `iterations_used` starts from the prior
  count, `budget_left()`'s `iterations_used < cfg.iterations` check
  automatically reflects prior usage -- a bumped `iterations` value in the
  freshly-loaded `job.yaml` (the CLI-side top-up) is *remaining* budget on
  top of what was already spent, not a brand new allowance. The wall-clock
  `job_timeout_s` deadline, by contrast, is a fresh per-process guard
  (`self.deadline = time.monotonic() + cfg.job_timeout_s` in `__init__`) --
  each container invocation gets its own timeout window, it does not
  accumulate across restarts.
- This also covers resuming a *terminal* (e.g. `failed`/`aborted`) run dir
  after the operator increases the iteration budget and restarts: the same
  detection (`tasks.json` has tasks + completed iterations exist) applies
  regardless of what `state` the previous process left in `status.json`,
  since that field is unconditionally overwritten to `"running"` at the top
  of `run_job()` before the resume decision is made.
- Tests: `tests/test_resume.py` (no Docker, real `ralphd-engine` twice over
  the same run dir via `test_e2e.py`'s `engine_factory` fixture).

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
├── creds/              # operator-prepared credential env files + extras
│   ├── github.env      # KEY=value lines → placed at ~/.creds/github.env (0600)
│   ├── jenkins.env     # …one file per credential set, any names
│   └── setup.sh        # optional: run once before first iteration
└── pi/                 # pi settings fragments (models.json, provider config)
```

- **Skills**: directories of instruction files (`SKILL.md` convention), forwarded
  **explicitly and scoped per job** — `ralphctl start --skills <dir>` (repeatable),
  one directory per skill. There is deliberately no "forward all host skills"
  mode: the operator picks exactly what a job needs. Each named dir is *copied*
  into the job's config dir at start (so later host edits don't leak into a
  running job), mounted at `/config/skills/<name>`. **The engine itself**
  (`engine/skills.py:place_skills()`, run at startup from `engine/main.py`,
  before the job loop starts — not the container entrypoint script, mirroring
  the creds discipline below) symlinks the effective skill set into the
  location pi discovers skills from (`~/.pi/agent/skills/`).
  Gotcha: `--skills` treats its argument as *one* skill — passing a parent
  directory of many skills forwards it as a single mis-named skill. `ralphctl`
  rejects a `--skills` dir that has no `SKILL.md` unless every immediate child
  has one, in which case it expands to the children.
  Skills are full CRUD at runtime via the API (`GET/PUT/DELETE
  /config/skills/{name}`, tar bodies) — `PUT`/`DELETE` land in the writable
  overlay (`<overlay>/skills/<name>/`, an "api"-origin skill always wins over
  a same-named mounted one; `DELETE` writes a tombstone in
  `<overlay>/skills-deleted/<name>` so a mounted skill of that name isn't
  resurrected) and call `place_skills()` again immediately, so the mutation
  is live for the very next iteration without a container restart.
- **Credentials — env-file convention.** This mirrors the original Ralph's AWS
  Secrets Manager approach, made file-based: every credential set the job needs
  is prepared by the operator as one `<name>.env` file (`KEY=value` lines, `#`
  comments), e.g. `github.env`, `jenkins.env`, `sonarqube.env`. `ralphctl start
  --creds <dir>` copies `<dir>/*.env` into the job config; **the engine itself**
  (`engine/creds.py:place_creds()`, run at startup from `engine/main.py`, before
  the job loop starts) places them at **`~/.creds/*.env`** (agent-owned, mode
  `0600`) inside the container -- not the container entrypoint script, so that
  secret handling stays inside the same process that already guarantees
  values never reach `/run`, `events.jsonl`, stdout, or `job.json` (only file
  *names* are ever logged).
  **The agent knows where to look**: every phase prompt lists the available cred
  file names and the usage rule — *source the file you need, in the shell where
  you need it* (`set -a; . ~/.creds/github.env; set +a`). Values are **not**
  auto-exported into the engine or agent process environment; a credential is
  visible only to commands that explicitly source its file. Recognized
  non-env extras keep their conventional placement (`gitconfig`,
  `git-credentials`, `netrc`, `ssh/`), and an executable `setup.sh` still runs
  once at container start as the escape hatch. Nothing credential-shaped is
  ever written to `/run` (which is host-visible history) or logged.
  **The prompt-level rule that makes this hold in practice**: every tool
  call's arguments and stdout land verbatim in the run's iteration
  transcript, so the creds note (`loop.py:_creds_note()`) and the worker
  prompt's dedicated "Credential handling" section both explicitly forbid
  printing/`cat`-ing/`echo`-ing a credential file's contents or pasting a
  secret value into a command's arguments (query strings, `--token` flags,
  inline `Authorization:` headers) — either would permanently persist the
  secret in that transcript. The sanctioned pattern is exactly `set -a; .
  ~/.creds/<name>.env; set +a` followed by letting the tool read `$VARNAME`
  from its own environment. The same guidance forbids token-bearing git
  remote URLs (`https://<token>@host/...`, which leaks via `git remote -v`
  and `.git/config`) in favor of a credential helper / `~/.git-credentials`.
  Creds are full CRUD at runtime via the API (`GET/PUT/DELETE
  /config/creds/{name}`) — including read-back of values, an explicit design
  choice: **holding the API bearer token is defined as equivalent to holding the
  job's credentials** (see §6). Prompts see updated inventories at the next
  iteration.
- **LLM config**: env vars + pi config fragments produced by the CLI's LLM-profile
  resolution — see [llm-profiles.md](llm-profiles.md). The engine merges
  `/config/pi/*` into the container-local pi settings at startup and again on API
  update.

### Writable config overlay

In real containers `/config` is mounted **read-only** (`ro`) from the host, by
design: the operator-provided config is immutable job input, and the run dir
(`/run`) must never carry credential-shaped content. But the runtime
config-CRUD API (`/config/*` PUT/DELETE routes — skills, creds, prompts, llm)
needs *somewhere* writable to land mutations that must survive for the rest
of the job and be visible to the next iteration.

That somewhere is a **container-local writable overlay**,
`$HOME/.ralphd/config-overlay/` by default (override via
`RALPHD_CONFIG_OVERLAY_DIR`) — deliberately neither `/config` (read-only, and
host-visible input shouldn't be mutated by the running job) nor the run dir
(host-visible history; also where creds must never appear). It lives entirely
in the container's own writable filesystem layer and disappears with the
container.

Every config-relative read goes through a single resolution order, implemented
once in `engine/config.py`:

1. `$HOME/.ralphd/config-overlay/<rel>` — a runtime mutation via the API, if any.
2. `/config/<rel>` — the operator-mounted (possibly read-only) config.
3. A builtin default, if the caller has one (e.g. `src/ralphd/prompts/*.md`).

`engine/config.py:overlay_or_config(rel)` implements steps 1–2;
`overlay_write_path(rel)` is what API handlers call to get a writable
destination for step 1, creating parent directories as needed. The one
running example today is prompt overrides: `PUT /config/prompts/{name}`
writes to the overlay, and `LoopSupervisor.prompt_text()` resolves through
the order above on every iteration, so an override is effective starting with
the next iteration that builds that phase's prompt — without ever touching
the read-only `/config` mount. Skills CRUD follows the same pattern (see
above); creds/llm CRUD are a later milestone.

### Security: mechanical secret redaction

Prompt-level guidance (the creds note's "never `cat`/`echo`/paste a secret"
rule, task 049) is necessary but not sufficient: it has already failed twice
in this project's own self-hosted run — a worker `cat`-ed
`~/.git-credentials`, and a separate iteration ran `docker inspect` on the
*production* engine container and got back its real `AWS_BEARER_TOKEN_BEDROCK`
in the output. Both happened *after* the prompt guidance already existed, so
the engine also enforces this mechanically (`engine/redact.py`), the same
principle as Jenkins credential masking: scrub every known secret *value*
from everything the engine persists or serves, regardless of how an agent's
command happened to produce it.

**Redaction-set sources** (rebuilt fresh at engine startup, and again after
any `PUT`/`DELETE /config/creds/{name}` or `PUT /config/llm` — never
persisted, never returned by any API route, in-memory only for this process's
lifetime):
- process/LLM-forwarded env vars whose *name* matches `TOKEN`/`KEY`/`SECRET`/
  `PASSWORD` (case-insensitive) or is a known LLM provider var (e.g.
  `ANTHROPIC_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`) — covers both the
  process's own environment and any `PUT /config/llm` env override;
- values parsed best-effort from placed creds files: `~/.creds/*.env` (`KEY=
  value` lines), `~/.git-credentials` (the URL password), `~/.netrc`
  (`password` fields). Unparseable files are simply skipped.

**Scrub points** (defense-in-depth, several independent layers): every line
of a `pi` subprocess's stdout is scrubbed before being appended to that
iteration's `output.jsonl` (`runner.py`); every event is scrubbed as its
serialized JSON text before being appended to `events.jsonl`
(`state.py:RunDir.emit()`); `GET /logs` (both tail and follow modes) scrubs
again as it serves content, so a value only *recognized* as a secret after a
transcript line was originally written (e.g. a cred added mid-run) is still
caught retroactively when served.

**Length floor**: only values at least 8 characters long are ever redacted,
so short/common substrings (region codes, single words) are never mangled —
deliberately trading a narrow gap in coverage for never corrupting ordinary
transcript text.

**Memory-only guarantee**: the redaction set itself is exactly as sensitive
as the secrets it holds, so it is never written to disk (not even under the
writable overlay) and no API route (`GET /config`, `GET /config/creds`,
etc.) ever exposes it — those routes already only ever return credential
*names*, never values, and that discipline is unchanged by this feature.

**Non-goals** (recorded as roadmap notes, not implemented here — see
[roadmap.md](roadmap.md)): PID-namespace isolation of agent iterations from
in-container kill signals, and a `ralphctl repair` command for hand-fixing
corrupted run state.

## 6. API security

- Default: container port published to `127.0.0.1` only, **no auth**.
- Optional: `--api-token <t>` (or auto-generate with `--api-token auto`) makes the
  engine require `Authorization: Bearer <t>` on every route; combine with
  `--api-bind 0.0.0.0` for LAN/remote access. The CLI stores the token in the run
  registry (`~/.ralphd/runs/<id>/.api-token`, mode 0600) and sends it automatically.
- `job.json` is stored redacted (no secret values). The creds API, however, is
  full CRUD **including read-back** — by design, the API bearer token *is* the
  job's credential boundary. Protect it accordingly: keep the default
  loopback-only bind unless the token is set, and treat `--api-bind 0.0.0.0`
  + token as granting whoever holds the token everything the job can do.

### Docker socket opt-in (`--allow-docker`)

By default the job container has **no docker socket** (§8) — that stays the
baseline. `ralphctl start --allow-docker` is an explicit, per-job trust
escalation for PRDs that need to build/run containers (integration tests,
image builds):

- **Trust model: root-equivalent.** The mounted socket lets the job
  `docker run --privileged -v /:/host …` and own the machine; the non-root
  `agent` user and any container hardening are irrelevant once it holds the
  socket. There is no partial-trust variant (socket proxies that allow `run`
  at all still allow arbitrary mounts). ralphctl prints a loud warning at
  launch; use only with PRDs you trust as much as your own shell.
- **Mechanics.** ralphctl mounts the socket (default `/var/run/docker.sock`,
  overridable via `RALPHD_DOCKER_SOCK`) and adds the socket's group via
  `--group-add <gid>` (computed at launch — the gid differs per host). The
  containers the job starts are **siblings** on the host daemon, not children.
- **Path translation gotcha.** A sibling's `-v` is resolved by the *host*
  daemon: container-local paths (`/workspace`, `/run/ralphd`) mount as empty
  dirs and the daemon may auto-create them root-owned on the host. ralphctl
  therefore injects the host-side equivalents as env vars —
  `RALPHD_HOST_WORKSPACE` (when a single unnamed `--workspace` was given) or
  `RALPHD_HOST_WORKSPACES` (a name→host-path JSON object, for the
  multi-workspace case above), plus `RALPHD_HOST_RUN_DIR` and `RALPHD_RUN_ID`
  — and the engine appends a "Docker siblings" section to every phase prompt
  telling the agent to use them. (`docker build` contexts are exempt: the
  CLI streams the context itself.)
- **Label + reap lifecycle.** The job container always carries
  `--label ralphd.run=<run-id>` (with or without `--allow-docker`); prompts
  instruct the agent to put the same label on every sibling and prefer `--rm`.
  `ralphctl stop` and `ralphctl rm` best-effort `docker rm -f` everything
  matching the label (idempotent, never fails the command); `ralphctl doctor`
  reports stray labeled containers whose run id no longer has a registry dir
  (report-only). Anything unlabeled and detached outlives the job — the daemon
  has no parentage notion between a job and its siblings.

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
task table, current phase/iteration, tail of agent output, budget gauge.
`ralphctl ui [--port N]` (PRD reqs 21-22) serves the same registry over a
stdlib-only HTTP server (`http.server`; no `fastapi`/`uvicorn` on this path):
`GET /api/runs` (run list) and `GET /api/runs/<id>` (`/logs`, `/steer`) proxy
to each run's live container API when reachable and fall back to the on-disk
`status.json`/`tasks.json` snapshot otherwise, so a dead run degrades
gracefully instead of erroring (see [cli.md](cli.md)). The static bundle
served at non-`/api` paths is still pending (v0.3, task 034).

## 8. Docker image

`ghcr.io/…/ralphd` (multi-arch), containing: Python 3.12 (engine), Node 22 +
a **pinned** pi CLI version (npm silently resolves an old pi when the node
engine requirement isn't met — pin both, upgrade deliberately),
git, ripgrep, curl/jq, build essentials, and a non-root `agent` user. Deliberately
**thin on toolchains** — language runtimes beyond Python/Node are the operator's
business via `--image` (derived images `FROM ralphd`) or a job-level
`setup.sh`. Engine and API run as the non-root user; no docker socket, no host
network. The image does ship the static docker **client** binary (pinned
`DOCKER_VERSION`), but it is inert without the socket — which is only mounted
by the explicit `--allow-docker` opt-in (§6). It also bundles **playwright-cli** (pinned
`PLAYWRIGHT_CLI_VERSION`) with headless Google Chrome so jobs can drive real
web UIs — e2e verification of frontend changes, screenshots as artifacts.
Chrome, not chromium, because playwright-cli's default channel is `chrome`;
only that channel ships (chromium would add ~500 MB for no default-path gain).

## 9. Failure containment

- Agent process crash / nonzero exit → iteration recorded as failed, loop continues
  (budget permitting). Repeated immediate failures (3 consecutive iterations with no
  task-state change) → job fails fast with a diagnostic event.
- Engine crash → container exits; run dir remains consistent (atomic writes);
  `ralphctl resume <run-id>` starts a fresh container against the same run dir and
  the loop continues from `tasks.json` (v0.2).
- Host reboot ⇒ same as engine crash; nothing lives only in container memory.
