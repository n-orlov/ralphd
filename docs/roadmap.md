# Roadmap

Versions are milestones, not promises of dates. Each version is releasable on its
own; scope may shift between minors, not the ordering of the big rocks.

> **Status (v0.7 close-out):** v0.1 through v0.7's engine/CLI/hub-UI feature
> scope is implemented and covered by black-box tests, including a real
> docker-sibling e2e tier and a real browser e2e tier for the hub — see
> `artifacts/reports/traceability.md` (v0.1–v0.4 requirements),
> `artifacts/reports/issue-traceability.md` (backlog issues → requirement
> letter → task → commit → tests: v0.5's #1–#11/#13, plus v0.6's #14–#22 as
> requirement I lands them) and
> `artifacts/reports/v0.5-definition-of-done.md` (evidence per DoD bullet).
> The job image now **builds** locally, content-hash-tagged, from a checkout or
> a `pipx`-style install (v0.6, requirement H). What remains outside the
> scope of every version so far: *publishing* that image to a registry and the
> wheel to an index — see the deferred list.

## v0.1 — the working loop (MVP)

The smallest thing that is genuinely useful and proves every load-bearing design
decision:

- ✅ Engine: planning → worker → review loop, approaches + composite PRD, sentinels,
  atomic `tasks.json`, iteration records, `events.jsonl`
- ✅ Vigilant mode and model strategies (they are cheap once per-phase model resolution
  exists, and were designed in from the start)
- Container API: ✅ status/tasks/iterations/events/steering/interrupt/abort/shutdown
  + bearer token; ✅ `GET /logs` whole-job stream (incl. `follow`), prompts/skills/
  creds/llm runtime CRUD
- `ralphctl`: ✅ `start`, `runs`, `status`, `watch`, `tasks`, `steer`, `interrupt`,
  `pause/unpause`, `abort`, `stop`, `rm`, `artifacts`, `doctor`, `resume`
  (fresh container over an existing run dir); ✅ pretty `logs`
  (tail-style, whole-job console), `skills`, `creds`, `prompts`, `llm`, `config`
- ✅ Credentials: env-file convention (`--creds <dir>` of `<name>.env` files →
  `~/.creds/` in-container; prompts advertise the inventory; agent sources on
  demand) — the file-based analogue of original Ralph's AWS Secrets approach
- ✅ LLM profiles: format + `host`/`none` built-ins + bedrock and gateway example
  profiles **with acceptance tests proving both**
- ⏳ Docker image published (amd64/arm64); `pipx install ralphd` from an index —
  the image is built and run locally by `start`/`resume` themselves (v0.6
  requirement H, proven by the docker-sibling e2e tier and the real-build
  tier) but has never been pushed to a registry, and the package has never
  been published to PyPI; publishing is not attempted (no CI/publishing
  pipeline exists in this environment) and is deferred explicitly below
- Docs: the design docs kept in sync with reality (ongoing) + a tutorial ✅
  (`docs/tutorial.md`)

Non-goals for v0.1: resume, web UI, self-reflection, multi-job orchestration.

## v0.2 — durability & recovery

- ✅ `ralphctl resume <run-id>`: fresh container over an existing run dir (crash
  recovery, budget top-up)
- ✅ Engine-crash consistency tests (kill -9 at nasty moments)
- ✅ Run-dir schema version + migration story
- ✅ `ralphctl llm test` hardening; richer `doctor`
- ✅ Cost/usage accounting surfaced per phase and per approach

## v0.3 — hub UI

- ✅ `ralphctl ui`: local web hub reading `~/.ralphd/runs/*` and proxying live
  container APIs — run list, task progress, iteration timeline, live transcript
  tail, steering form, history browsing
- ✅ Static bundle served by the CLI (no separate server process to manage,
  no npm/node build step)

## v0.4 — quality-of-life

- ✅ Self-reflection phase (post-job analysis proposing prompt/skill improvements as a
  diff in artifacts)
- ✅ Job templates (`ralphctl start --template <name>`) bundling PRD skeleton +
  skills + profile
- ✅ Pluggable notifications on completion (shell hook: `on_complete_cmd`)
- ✅ Multi-workspace jobs (multiple repos mounted side by side)

## v0.5 — session resilience & self-recovery

Full PRD: [`docs/prds/v0.5-resilience.md`](prds/v0.5-resilience.md). Driven by
three live incidents (a DNS wobble destroying 4 of 8 approaches, a lost
post-mortem, an agent deleting its own container) and the GitHub backlog
(#1–#11, #13). Phase 1 is the resilience half and lands first; Phase 2 is
operator surfaces.

Phase 1 — the environment must not be able to destroy a job:

- ✅ In-band LLM errors classified as faults at all — `pi` reports endpoint
  failures with `exit_code: 0`, which `classify_fault()` used to score as
  success, making the whole retry/refund apparatus unreachable in production.
  A non-empty `error_message` is now a fault regardless of exit code, the
  signature table covers the observed gateway/Bedrock families, the bare
  `aborted` error is `infra` only when there was no traffic *and* no operator
  abort, and the verdict is recorded as `faultClass` in each iteration's
  `meta.json` and the `iteration.end` event (#11)
- ✅ Aggressive retry: fast escalating backoff (`2,5,15,30,60,120,300`s, last
  value repeating up to `infra_retry_backoff_max_s`), a wall-clock **outage
  budget** (`infra_outage_budget_s`, default 4h, `ralphctl start
  --infra-outage-budget`) in place of the 3-attempt cap (`infra_retry_max` is
  now honoured only when set explicitly), all five phases covered without
  double-counting phase-local error budgets, waits accounted as
  `infraWaitTotalS` with the job deadline extended by them, plus an explicit
  `health: ok|degraded` / `infraWait` status contract surfaced in
  `ralphctl status` and the hub — while keeping the fail-fast path for a
  broken credential (stable instant no-traffic failures still get the
  broken-environment diagnosis in seconds) (#5, #11)
- ✅ `POST /retry` + `ralphctl retry <run-id>` (+ a hub retry-now button with a
  countdown): the backoff wait is an interruptible event, so an operator who
  knows the endpoint is back skips the remaining wait and resets the
  outage-budget episode clock
- ✅ Reflect phase gets retry (with a pre-attempt wait after an infra-shaped
  ending) *and* reports its own failure — `status.json`'s `reflect` outcome +
  `artifacts/reflection/FAILED.md`, surfaced in `ralphctl status` and the hub —
  instead of silently producing no post-mortem (#5)
- ✅ A run recorded `running` with no container is visible to `repair` (guarded
  `--set-state` writing a vanished-container reason + audit event), `status`
  (explicit warning line, on-disk task counts, staleness instead of a growing
  live elapsed) and the hub's warning treatment — not only to `doctor`, whose
  remedy text now tells the same story (#8)
- ✅ Opt-in self-recovery, shipped **off by default**: `ralphctl start
  --auto-resume` (or the registry default `ralphctl config set auto_resume
  true`) marks a run as opted in, and `ralphctl doctor --fix` — run from cron/
  systemd, no new daemon — resumes exactly those runs recorded non-terminal
  whose container has vanished. Opted-out runs are reported and left alone;
  terminal runs and runs whose termination was operator-initiated (`abort`/
  `stop`) are never resurrected; a crash-loop guard (`autoResume: {attempts,
  lastAt, maxAttempts}` in the run dir) spaces attempts with escalating
  backoff and gives up with a readable reason. The default is a single literal,
  `AUTO_RESUME_DEFAULT` in `src/ralphd/cli/main.py`; **v0.7 (requirement O)
  flipped that literal to ON** now that the guard and the never-resurrect rule
  have been validated on real runs — a dangling unattended run is worth more
  resumed than left sitting until a human notices — so opting a run out is
  `start --no-auto-resume` (or `ralphctl config set auto_resume false`
  registry-wide). What v0.5 itself shipped is the opt-in version above
- ✅ `ralphctl watch` (and `ralphctl logs -f`) stop closing at a *historical*
  terminal-state marker: the stream ends on a terminal event only when it is
  the log's last event *and* the engine is not live, and resume now appends an
  explicit `running` state event so a stale marker can never be the log's
  final word (#13)
- ✅ The documented sibling-cleanup idiom can no longer delete the job
  container: `ralphd.role=job|sibling` labels + `RALPHD_SELF_CONTAINER_ID`,
  the sibling-only cleanup filter propagated to every documented copy
  (prompt, skill example, architecture, CLI docs) with a test that runs the
  documented command and proves the job container survives (#7)
- ✅ Logs readable from disk when the container is gone, on both hub and CLI —
  one shared `log_merge` module behind the live API, the hub's snapshot label
  and `ralphctl logs` (pretty/`--raw`/`--follow`, exit 0 with a stderr
  notice), plus an explicit `(no transcript yet)` for an empty run (#6)

Phase 2 — operator surfaces:

- ✅ In-flight iteration-budget top-up via `PATCH /config/budget` + `ralphctl
  budget <run-id> +N|N`, proven e2e on a job that was about to exhaust its
  budget (#3)
- ✅ Absolute timestamps (one shared formatter, ISO kept in the payload) in the
  iteration timeline, `logs`, and `status` (#4)
- ✅ Unknown cost rendered as unknown instead of `$0.0000` — unpriced
  iterations are marked (`costPriced: false`), run totals mixing priced and
  unpriced read as *partial*, every surface renders unknown/partial as
  unavailable, and an optional host-side pricing map (with gateway aliases)
  can supply a *derived* cost that is never conflated with a
  provider-reported one (#10); `artifacts/reports/pricing-anomaly.md` records
  the investigation of the same-model priced/unpriced anomaly
- ✅ Sortable run list in the hub (all seven columns, sort state outside the
  DOM, newest-first by default), with `ralphctl runs --sort/--reverse` parity (#9)
- ✅ PRD dialog and clickable task detail in the hub (#1, #2)

## v0.6 — first-release polish

Full PRD: [`docs/prds/v0.6-first-release.md`](prds/v0.6-first-release.md).
Driven by the release-hygiene backlog (#14–#22) and by three defects this
project's own runs kept hitting: numbers on screen that were wrong rather than
unknown, a run that could not explain itself without an operator reading the
run dir by hand, and a job image nothing ever built. Requirement letters below
are the PRD's.

Phase 1 — stop printing wrong numbers:

- ✅ **A. A mid-write `tasks.json` is never served as "no tasks" (#15).** One
  hardened read path (`read_tasks_doc`) behind every surface: a bounded re-read
  on a parse failure, then the last successfully parsed payload flagged stale,
  with **absent**, **unparseable** and **parsed-empty** kept apart instead of
  collapsing into one default. The last-good cache is written only on the sad
  path, so it is a fallback for a file being rewritten and never a mirror; the
  stale flag is rendered by `GET /tasks`, `GET /status`, `ralphctl tasks` and
  the hub table, which a browser test drives through an agent-style rewrite to
  prove it does not blink
- ✅ **B. Approach `n/m` on every surface (#16).** `maxApproaches` in
  `status.json` (written at the first status write, so a job that dies in
  startup still has its denominator), one shared `format_approach` renderer in
  `ralphctl status`, `ralphctl runs`, the hub run list and run detail; a
  pre-v0.6 `status.json` renders `2` bare rather than inventing a ceiling, a
  run with no approach yet renders empty, and the column still sorts numerically
- ✅ **C. Derived cost from a built-in AWS Bedrock table, and a model id you can
  see (#14).** A shipped Bedrock rate table with an alias map for the gateway
  forms, a machine-readable as-of date and a staleness signal, reusing
  `PricingMap`'s matching rules; selected by `price_strategy` (LLM profile,
  `ralphctl start --price-strategy`, replayed by `resume`), with an operator
  `pricing:` map still winning and `price_strategy: none` byte-identical to
  v0.5. Derived money stays derived (`costDerivedUSD`), `GET /config` names
  which table answered, and the resolved/raw model ids are recorded per
  iteration and in `status.json` — including the id `pi` chose when the run
  pinned none, which is what makes the derivation fire on an unpinned route.
  An **implausible zero quote** (a quoted `costUSD` of 0 beside hundreds of
  thousands of billed tokens, which is what this project's own gateway sends)
  is now classified as unknown instead of `$0.00`; only a route that declares
  itself free reads as free
- ✅ **D. Task progress in the run list (#21).** A `TASKS` column (`5/7`,
  blank rather than `0/0` for a plan-less run) in the hub and in
  `ralphctl runs`, sorting on the completion fraction with plan-less runs last,
  flagging validation-failed and in-progress in `_summarize_tasks`' own
  wording, from one hardened local read per row and no live proxy call

Phase 2 — make the run explain itself:

- ✅ **E. Steering is readable, not just writable (#17).** `GET /steering` on
  the engine, a hub steering-history panel (pending/applied, body in the single
  dialog) and `ralphctl steer --list` (`--json`), all through one live-first,
  on-disk-fallback reader, so a post-mortem still sees what was sent
- ✅ **F. Click to view details, across the run detail page (#18).** Five new
  dialogs, each with its CLI counterpart: iteration detail
  (`ralphctl iteration`), the run state documents including a redacted
  `job.yaml` (`ralphctl docs`), artifacts and the reflection report
  (`ralphctl artifacts`), the fault explanation (`ralphctl fault`) and the
  per-phase/per-approach cost breakdown (`ralphctl cost`). Text nodes only,
  exactly one dialog alive across a poll, every view answerable from the
  on-disk snapshot with the container gone
- ✅ **G. Deleting a dead run takes one command (#19).** `ralphctl rm --force`
  stops a leftover container and then removes state, while plain `rm` keeps
  refusing and `--force` still refuses a *running* job outright; the hub grows
  a delete affordance for terminal runs only, behind a confirm dialog naming
  the run id and disabled with a reason while a run is active

Phase 3 — the image lifecycle:

- ✅ **H. The job image builds itself, and a job can bring its own (#20).**
  `start`/`resume` hash the image inputs and build `ralphd:<hash>` only on a
  cache miss, so running a stale engine is structurally impossible; a
  user-supplied image is a **base** (`ralphd-base:<hash>`) that ralphd layers
  the engine and `pi` onto (`ralphd-derived:<hash>`); `--dockerfile`,
  `job.yaml` and registry `config.yaml` supply it with the most specific
  winning; the resolved reference is recorded in run state so `resume`
  reproduces the image the run started with rather than the current hash; a
  failed build fails `start` before any run state exists; `doctor` reports
  staleness; and a `pipx`-style install hashes the same inputs shipped as
  package data

Phase 4 — close the loop:

- ✅ **I. Close #14–#22 from inside the run (#14–#22).** The wave's own last
  requirement, done after the final verification sweep passed: it extends
  `artifacts/reports/issue-traceability.md` with an issue → requirement → task
  → commit → tests section per issue, closes each issue over the GitHub REST
  API with a comment naming that evidence, and writes
  `artifacts/reports/issue-closure.md` as the auditable record (issue number,
  status code, resulting state, comment url). Read those two reports for what
  actually closed; an issue that did not fully land is left open and named in
  both
- ✅ **J. Release hygiene and the doc audit (#22).** A deliberate first-release
  version asserted against this roadmap, the dead `cli` extra dropped and every
  remaining requirement tied to the place it is used, every evidence report
  under `artifacts/reports/` re-read by the suite, a documented-but-nonexistent
  CLI flag or API field now failing the suite, and the semantic pass over
  `README.md`, `docs/` and `SPEC.md` — each correction landed with the check
  that would have caught it

## v0.7 — trustworthy unattended operation

Full PRD: [`docs/prds/v0.7-trustworthy-unattended.md`](prds/v0.7-trustworthy-unattended.md).
v0.6 made a *running* run's numbers truthful; this wave decides whether ralphd
can be left running overnight. Its organising principle: **no single iteration
may be able to end the run, and no surface may report a previous episode's facts
as this one's.** Every requirement came from a real incident in this project's
own runs — a verifier that killed its own supervisor and ended a 9h36m job, a
slow iteration that took the engine down with it, a resume that deleted the
transcript explaining the crash. Requirement letters below are the PRD's.

Phase 0 — the loop improves its own material:

- ✅ **A. v0.6's reflection diff, reviewed, tightened and tested (#35, #36, #37,
  #38, #39, #40, #41, #42).** The eight prompt changes the previous wave's
  post-mortem proposed, landed as committed prose rather than a diff nobody
  applied, each tighter than the suggestion and each pinned by a prompt test
  with a mutation matrix: sandbox safety — never signal by pattern, never touch
  a container/image/volume you did not create (#35); how to resolve a
  terminally `failed` task instead of grinding on it (#36); steering is
  delivered once, so its durable part goes into the notes first (#37); the test
  budget against the iteration clock (#38); a `## Standing rules` notes block
  written by planning and preserved by the worker (#39); no tally that was not
  derived from `tasks.json` in that same iteration, and a review that
  cross-checks evidence (#40); planning spot-checks the PRD's claims about the
  code before breaking it down (#41); a last-attempt validation verdict hands
  off the residual gap (#42)

Phase 1 — containment: one bad iteration must not cost the run:

- ✅ **B. The iteration timeout fails the iteration, not the engine (#28).** The
  runner no longer re-awaits a task `asyncio.wait_for` already cancelled, and
  the per-iteration guard catches `BaseException` — a deliberate containment
  boundary, commented where it is and why — so a timed-out or cancelled
  iteration is recorded failed (`meta.json`, `faultClass`, refund bookkeeping)
  and the next iteration starts
- ✅ **C. No verdict means no verdict (#45).** One predicate decides "the
  verifier never reached a verdict" (error, timeout, no traffic, interrupt);
  on that path a task's `status`, `validationAttempts` and `validationNotes`
  are left byte-for-byte unchanged, and the wording keeps "reached a negative
  verdict" apart from "never reached one"
- ✅ **D. Fault classification: signals, and a bare `aborted` after traffic
  (#49).** A third verdict `signal` for an iteration killed by one (never
  retried into whatever sent it), and a bare in-band `aborted` that ended well
  inside its own timeout with no operator abort is `infra` — retried and
  refunded — with the threshold a named constant justified from the run that
  produced the evidence
- ✅ **E. A self-inflicted abort is not an operator abort (#46).** Termination
  carries a class (`operator`/`self`) plus evidence — the last tool call before
  the signal, read from the transcript — so `ralphctl status` never prints a
  bare `reason: signal 15`; a genuine operator abort is still never
  resurrected, a self-inflicted one is eligible for auto-resume
- ✅ **F. No manufactured reflection failures, and no stale tombstones (#47,
  #43).** An engine unwinding on a signal records reflect as *not attempted*
  instead of writing a `FAILED.md` nobody caused, and a reflect attempt that
  succeeds deletes any earlier tombstone, so `artifacts ls` stops advertising
  a reflect-failed alias beside the report that exists
- ✅ **G. Steering is consumed on success, not on delivery (#34).**
  At-least-once delivery with at-most-once application: the note is marked
  consumed after the iteration that carried it succeeded, so an iteration that
  dies leaves its instruction pending for the next one; `meta.json` keeps
  `steeringDelivered` apart from `steeringConsumed`
- ✅ **H. A crashed iteration's transcript survives the resume (#44).** The
  resume reuses the iteration *number*, not the record: whatever an earlier
  attempt left in the slot moves to `iterations/NNNN/attempts/NN/`, readable
  through `ralphctl iteration`, and an archive is never counted as an iteration
- ✅ **I. The agent cannot signal its own supervisor (#48).** The uid boundary:
  the engine keeps real and saved uid `0` while each iteration's `pi` runs as
  the agent's uid, so `pkill -f ralphd-engine` from inside a task iteration is
  a no-op — proven by a docker-tier test — while run-dir ownership, the config
  overlay, the editable install and the commit identity are unchanged

Phase 2 — honest status across a crash and a resume:

- ✅ **J. Liveness comes from docker, not from a field the dead engine wrote
  (#31).** Three states instead of two: an exited-but-present container is
  `dangling`, surfaced as such by `status`, `runs`, `doctor` and the hub
- ✅ **K. `endedAt` and `reason` are episode-scoped (#30).** A resumed run's
  permanent record no longer carries a previous episode's ending; the earlier
  endings are kept as `previousEndings` instead of being reported as this one's
- ✅ **L. Refund counters survive a resume (#32).** `iterationsRefunded`
  ({infra, grace}) is persisted and re-seeded, so budget earned back during an
  outage is not silently forfeited by the next episode
- ✅ **M. `failed` says which kind of failed (#33).** A failed task may carry
  `failureKind` (`validation-exhausted`/`requirement-unmet`), derived for
  pre-v0.7 records, with sub-counts on every surface that counts statuses — and
  a plan whose only obstacle is a failed task routes to review instead of
  waiting for an operator
- ✅ **N. A silently skipped verification gate warns (carried from #29).** A
  vigilant run whose worker iteration finds the gate empty while unverified
  work sits behind it says so once, per approach

Phase 3 — the two deferred decisions, taken:

- ✅ **O. `auto_resume` defaults to ON.** The single literal
  (`AUTO_RESUME_DEFAULT` in `src/ralphd/cli/main.py`) flipped now that the
  crash-loop guard and the never-resurrect rule have been validated on real
  runs; opting out is `start --no-auto-resume` or `ralphctl config set
  auto_resume false`
- ✅ **P. A review-and-apply verb for the reflection diff.**
  `ralphctl suggestions <id> [show|apply]` plans a reflection diff host-side and
  says what it would touch before writing anything, all-or-nothing, `--only
  PATH` for a subset, refusing to touch a live run's prompts and naming another
  run's conflicting diff — an operator-driven verb, never a loop that edits its
  own prompts

Phase 4 — close the loop:

- ⏳ **Q. Close the wave's issues from inside the run (#28, #30–#49).** The
  wave's own last requirement, done after the verification sweeps pass: it
  extends `artifacts/reports/issue-traceability.md` with a section per issue
  (requirement → task → commit → tests) and closes each fully-landed issue over
  the GitHub REST API, recording every closure in
  `artifacts/reports/issue-closure.md`. Pending as this block is written; read
  those two reports for what actually closed, and treat an issue named there as
  not landed as still open
- ✅ **R. Docs, SPEC and the roadmap.** Docs in the same commit as the
  behaviour, every claim this wave adds paired with a code-derived check and a
  mutation case, answered open questions moved out of SPEC §16 with their
  evidence rather than edited in place, and this block plus the `0.7.0` version
  bump landed together, late

Arriving mid-wave, by operator steering rather than from the PRD — the first
requirement this project has taken while the run producing it was still going,
which is why it has no letter in `docs/prds/v0.7-trustworthy-unattended.md`
(that file stays byte-identical to the run's own `prd.md`) and is recorded in the
run's steering note instead:

- ✅ **S. Registry-wide defaults for the four keys that choose a job's models
  (#52).** `model`, `fast_model`, `model_strategy` and `thinking` joined the
  eight keys `ralphctl config set` already covered, so an operator states them
  once per host instead of on every launch: same precedence chain as before
  (flag > `--template` > `<registry>/config.yaml` > hardcoded), one table
  wider, no second mechanism. `model_strategy` is validated against
  `JobConfig.STRATEGY_TIERS`' presets so a typo exits `2` rather than silently
  resolving to the shipped strategy; the other three stay free-form, because
  ralphd cannot enumerate a host's routes. The hardcoded fallbacks are
  deliberately untouched — `_TEMPLATE_SCALAR_FIELDS["model"] = None` means
  "pi's own default", and baking one host's Bedrock route into the source would
  break every host without it

## Later / explicitly deferred

- PID-namespace isolation of agent iterations. **No longer load-bearing:** v0.7
  requirement I closed the hazard it was filed for with a **uid boundary** —
  the engine keeps real and saved uid `0` while each iteration's `pi` runs as
  the agent's uid, so an iteration cannot signal its own supervisor (`pkill -f
  ralphd-engine` from inside a task iteration is a no-op, proven by the docker
  tier). What a private PID namespace would still add is narrower: letting the
  engine tell "stop this one iteration" from "the whole container is being torn
  down" from the process tree itself, and hiding the engine's pid from the agent
  entirely. Not attempted; SPEC §13.4 records the boundary and what is left.
- Publishing the Docker image and the wheel. The image builds locally and is
  content-hashed (v0.6 requirement H) — including from a `pipx`-style install,
  whose wheel ships the image inputs as package data — and the docker tier
  proves the built image runs. What is deferred is the *publishing* pipeline:
  nothing pushes a tag to a registry or a wheel to PyPI, so `--image` names a
  locally built tag and there is no `ralphd` to install from an index yet.
- Remote/daemon mode (running ralphd on a server, CLI over the network) — the
  token+bind options already make this *possible*; making it *nice* (TLS, discovery)
  is deferred
- Alternative agent runtimes behind an interface (claude code, codex, opencode) —
  revisit once the pi-only loop is stable
- Parallel jobs orchestration/queueing — out of scope; the CLI's `--json` interface
  is deliberately sufficient for an external orchestrator (human, script, or agent)
- First-class named env-wiring profiles (reusable `--forward-env`/creds bundles
  referenced by name at `start`, analogous to LLM profiles) — operators
  currently repeat the same multi-flag `--forward-env`/`--llm-env`/`--env`
  incantation on every run; the per-run env-wiring persistence added for
  resume (roadmap-4 requirement A) only records what was resolved for *that*
  run, it doesn't let an operator name and reuse a bundle across runs. Worth
  revisiting once there's a second real profile-shaped use case.
