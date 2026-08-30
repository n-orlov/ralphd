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

- ✅ **Q. Close the wave's issues from inside the run (#28, #30–#49).** The
  wave's own last requirement, done once all three verification sweeps were
  green at `bfe29d6`: `artifacts/reports/issue-traceability.md` gained a section
  per issue (requirement → task → commit → tests), each closing comment was
  generated from that section and posted before the state change, and every
  issue was re-read with a fresh `GET` afterwards — all of it recorded in
  `artifacts/reports/issue-closure.md`, which is the receipt those claims rest
  on. Closed: **#28, #30–#49**, this wave's 21, plus the three amendments that
  arrived mid-run by operator steering — **#52**, **#53** and **#55** (bullets
  S, T, U) — 24 in all, no hedged closures. Left open deliberately, and named in
  both reports so the silence is not mistaken for an oversight: **#50**, whose
  defect (a top-level `tests` package that shadows the installed one) is real
  and unfixed, so it got a correction comment and a body edit rather than a
  closure; **#51**, filed by the operator while this run was going; **#54**, not
  this wave's; and **#24–#27**, out of scope by the PRD's non-goals
- ✅ **R. Docs, SPEC and the roadmap.** Docs in the same commit as the
  behaviour, every claim this wave adds paired with a code-derived check and a
  mutation case, answered open questions moved out of SPEC §16 with their
  evidence rather than edited in place, and this block plus the `0.7.0` version
  bump landed together, late

Arriving mid-wave, by operator steering rather than from the PRD — the first
requirements this project has taken while the run producing them was still
going, which is why they have no letter in
`docs/prds/v0.7-trustworthy-unattended.md`
(that file stays byte-identical to the run's own `prd.md`) and are recorded in the
run's steering notes instead:

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
- ✅ **T. `balanced` is the shipped default model strategy (#53).** Every run
  that never chose a strategy was getting `quality-first` — the strong model in
  all five phases — so the default was the most expensive preset, chosen by
  nobody. `balanced` now ships: strong for planning, review and reflect, fast
  for worker and verify. The default lives in exactly one place
  (`engine/config.DEFAULT_MODEL_STRATEGY`, imported host-side by
  `ralphctl start`'s template table), and it is deliberately **not** the same
  constant as `model_for`'s unknown-name fallback, which stays `quality-first`:
  what ships may be the cheap compromise, but a misspelled strategy must
  degrade to the most capable preset rather than quietly downgrade a phase.
  Nothing changes for a run with no `fast_model` — the fast tier falls back to
  `model` — and a host that wants the old behaviour states it once with
  `ralphctl config set model_strategy quality-first` (bullet S)
- ✅ **U. A prose mention of a promise marker no longer ends the approach
  (#55).** The engine tested `COMPLETE in final_text`, so a worker that obeyed
  the contract and ended its reply *"36 tasks are still pending, so I will not
  emit `<promise>COMPLETE</promise>`"* had its explanation read as the signal:
  the worker loop ended with 36 tasks pending and only the reviewer rejecting
  the approach stopped that run going terminal `succeeded`/`verified`.
  `saw_verified`, which makes a run terminal with nothing behind it, had the
  same defect. Three layers now: a marker counts only as the **last non-blank
  line** of the final message (not an "own line anywhere" match, which a fenced
  block or an echoed `notes.md` section defeats), and one seen elsewhere is
  absent *and logged*; a `COMPLETE` is honoured only after the freshly re-read
  `tasks.json` is checked for tasks that are not `completed`/`skipped`, which
  stops the class rather than the sentence; and the stagnation counter consults
  the honoured completion, so ignoring an unearned marker cannot trade a false
  conclusion for an endless loop. `worker.md` and `review.md` supply the prose
  names — "the completion marker", "the verification marker" — because an agent
  that must name the signal to explain withholding it will otherwise reproduce
  the literal line

## v0.8 — a run a human can interrupt, audit and believe

Full PRD:
[`docs/prds/v0.8-auditable-interruptible.md`](prds/v0.8-auditable-interruptible.md).
v0.7 made the loop survive its own bad iterations; this wave decided whether the
loop can be trusted at the moment a human touches it. Three of its requirements
trace to one abort of a real run, and one to the closure that followed it.
Requirement letters below are the PRD's; it does not use I–L.

Phase 1 — the moment a human touches a run:

- ✅ **A. A terminal state is not recorded until the unwind is over (#56).** A
  third non-terminal state, `finishing`, written the instant the verdict is
  known — with that verdict and the terminal state it will become already on
  disk — and promoted only when the engine actually exits, so the crash-durable
  write order v0.5 established is unchanged. `deletion_refusal` and
  `deletion_fields` share the one refusal sentence during that window,
  `ralphctl rm` refuses without `--force` and `--force`'s widened escape hatch
  is documented, `ralphctl status`/the hub never show a terminal record beside
  a live iteration, an `on_complete: idle` run stays deletable with its
  container up (the regression the obvious fix invites), and killing the
  engine mid-reflect still leaves the settled verdict readable on resume or
  `repair`
- ✅ **B. The fast lane is green on a developer host (#57, #50).** `tests.conftest`
  now resolves to this repo's own conftest regardless of what else is on
  `sys.path` (proven with a decoy top-level `tests` package ahead of the repo —
  **but only against the weaker of the two decoy shapes** — see "After v0.8"
  below, which corrects the scope of this claim);
  the browser tier's screenshot directory defaults to a user-writable tempdir
  with the container path still honoured via env override; the three
  uid-boundary tests skip outside a job container with a named reason and are
  asserted to run and pass inside one; the timeline dialog's keyboard-focus
  race was diagnosed and fixed; both environments' measured suite size and
  wall-clock are recorded in `artifacts/reports/`. Two more host-only failures
  surfaced afterward, via operator steering rather than the PRD, and were
  fixed forward rather than left standing: the uid-boundary meta-tests needed
  to exercise the non-container branch directly (faking `pwd.getpwnam`/
  `os.getuid` rather than only running inside the container), and
  `test_engine_uid_boundary.py`'s module-level read of `docs/architecture.md`
  failed collection inside a job container outright (`docs/` is the last line
  of `.dockerignore`, so the image never ships it) — fixed by making that read
  lazy. Both fixes are verified only in this container; the docker-tier proof
  itself (`--entrypoint python3` so the image's bare `exec` doesn't swallow
  the test command) is host-verified-only, since this run's own container has
  no docker socket
- ✅ **C. The budget an operator granted survives a resume (#51).** The
  effective iteration budget resumes as the larger of `status.json` and
  `job.yaml`, logged once naming both numbers so the operator sees which won;
  `resume --iterations +N` adds to that effective number, matching `--help`,
  `status` and the hub; `PATCH /config/budget`'s "live-engine change" wording
  is corrected to say it now persists across a resume

Phase 2 — the surfaces a human reads:

- ✅ **D. A closing keyword cannot close an issue nobody closed (#58).** A check
  fails a commit message carrying a closing keyword plus an issue number the
  closure record does not list as closed, fixtured on the real `9eaee65` body
  that caused the regression; a second check re-GETs every issue a closure
  record names as deliberately left open and fails if GitHub disagrees; both
  the prohibition and its substitute wording ("a later wave that *resolves*
  this", bare `#N`, `GH-N`) are stated in `worker.md`'s git policy, ahead of
  the commit that needed them
- ✅ **E. The hub stops flickering between two task lists (#27).** Both
  client-side defects fixed: a stale poll timer surviving a navigation, and a
  response from an older render landing after a newer one started (now
  dropped by render-generation tag); browser-tier coverage fails with either
  fix reverted; the reflex — the hub renders decisions Python made and never
  applies a response it did not ask for — is stated once in
  `docs/architecture.md` with a code-derived check
- ✅ **F. A run's approach history is visible (#24).** `ralphctl tasks <run>
  --approach N|--all` (and `--json`) read the archived approach directories
  directly, counts always derived from the archived file rather than
  recomputed; the hub's run detail renders the current approach expanded (the
  live Tasks section and State documents panel already are that) and every
  previous one collapsed, lazily fetching and rendering that archive's own
  task table, `notes.md` and `review-findings.md` on first expand, cached
  against the periodic poll rebuilding and silently re-collapsing an operator's
  open row
- ✅ **G. Image artifacts preview in the hub (#54).** One server-decided kind —
  text / previewable image / opaque binary — sniffed from magic bytes and never
  the extension, shared by `ralphctl artifacts ls` and the hub; a `.tar.gz`
  keeps today's exact `ARTIFACT_BINARY` wording; a raw byte route resolves the
  name through `state.artifact_relpath` and nothing else, serves only the
  four-signature allow-list (`png`/`jpeg`/`gif`/`webp`) with
  `X-Content-Type-Options: nosniff` and a Content-Type drawn from that
  allow-list rather than `mimetypes.guess_type`, and falls back to today's
  `pull` wording above a 5 MiB inline cap; SVG is deliberately excluded, with a
  comment and a test naming the reason (stored XSS); a
  Content-Security-Policy now covers every hub response; `app.js` branches the
  artifact dialog on the server's kind, no client-side extension list anywhere

Phase 3 — the deadlock we know about:

- ✅ **H. An operator ruling can reach review (#26).** A written design
  (`artifacts/reports/`) weighed four candidate mechanisms and chose a new
  `ralphctl amend` verb writing directly into the run's `:ro`-mounted config
  directory — never the run dir, never the API — so review's own
  `_operator_amendment_review_context()` reads a file structurally outside the
  agent's write access; a test proves anything the worker can write into the
  run dir does not satisfy that reader; a fast-tier test drives a genuinely
  deadlocked run to `succeeded`/`verified` with the amendment present and to
  `failed`/`unverified` without it, both directions in one suite

Phase 4 — close the loop:

- ✅ **M. Finish the verification your predecessor could not (#45 context).** An
  independent verification of `ralphctl suggestions show|apply`, run fresh at
  this run's own HEAD rather than trusting the predecessor's notes, against
  #28's and v0.7 requirement P's acceptance criteria; recorded in
  `artifacts/reports/`, stating why the false `validation-failed` demotion
  (an operator abort landing mid-verify on a pre-#45-fix engine) cannot recur
  now that `_verify_no_verdict` covers `interrupted` uniformly
- ✅ **N. Close this wave's issues, from inside the run.** All nine issues this
  wave owns — #24, #26, #27, #50, #51, #54, #56, #57 and #58 — closed over the
  GitHub REST API, 9 in all, no hedged closures: a comment generated from
  that issue's own `artifacts/reports/issue-traceability.md` section posted
  before the state change, closed, then re-read with a fresh `GET`; every
  posted comment, HTTP status and resulting state recorded in
  `artifacts/reports/issue-closure.md`'s v0.8 section. Untouched by this
  wave's closure action, deliberately: #25 (a non-goal) and #59 (arrived from
  outside this run, operator's call)
- ✅ **O. Docs, SPEC, the roadmap and the PRD index.** This block and the
  `0.8.0` version bump landing together, late; the predecessor's PRD index row
  replaced with its settled record rather than left reading a live snapshot
  after that run ended (fact 13); this wave's own PRD committed
  byte-identical to the job's `prd.md`, with its own index row, early

## After v0.8 — the harness stops assuming its own environment (#64, #65, #67)

Not a milestone and not a release: three defects found by the operator between
v0.8 and v0.9, all in the *test harness* rather than in the code under test, all
the same mistake. Recorded here because they change the **scope** of a claim this
document makes above, not its numbers: v0.8's suite figures were measured in a
virtualenv and are correct there. What was wrong was reading them as
environment-independent.

- ✅ **A `tests` package that ships wins over one that does not (#64).** The
  guard for #50 planted a decoy `tests/` with no `__init__.py` — a PEP 420
  namespace *portion*, which loses to plain `sys.path` ordering. The shape that
  actually ships (two installed wheels with a top-level `tests/` package)
  carries `__init__.py`, and a **regular** package found anywhere on `sys.path`
  pre-empts a namespace portion at position 0: `python -m pytest -q` died with
  42 collection errors and ran zero tests. Fixed by winning on precedence
  instead of ordering — `tests_package_pin.py` pre-binds `sys.modules["tests"]`
  to a package whose `__path__` is exactly `[<repo>/tests]`, so `tests.*` never
  reaches `PathFinder`. It is a module, not conftest code, because a conftest is
  only loaded for invocations that collect something under the repo, and
  `scripts/host_like_pytest.sh` does not — that script loads it with
  `-p tests_package_pin`. `tests/` deliberately stays a namespace package: 102
  modules use bare top-level imports that need `tests/` itself on `sys.path`
- ✅ **The suite finds its console scripts instead of assuming a layout
  (#65).** `RALPHCTL = Path(sys.executable).parent / "ralphctl"` holds in a
  virtualenv and fails for `pip install --user -e .`, where the interpreter is
  `/usr/bin/python3` and the script is `~/.local/bin/ralphctl`. Now a three-tier
  resolver with an explicit override, one `pytest.UsageError` at session start
  naming every location searched, and a venv's own script still beating a stale
  `PATH` copy. The durable half is an AST self-scan: the rule had been
  re-derived in 22 modules, so fixing the shared constant reached only 25 of the
  ~45 that shell out
- ✅ **A fresh `$HOME` no longer un-installs Python for the engine child
  (#67).** 19 modules launch the engine with `HOME` pointed at a tmp dir
  deliberately; on a `--user` install that also hides user site-packages, so the
  child died on `import ralphd` — invisibly, because its stdout is an unread
  `PIPE`, surfacing 15 seconds later as `TimeoutError: engine API never came
  up`. Fixed once, in `tests/conftest.py:with_ralphd_importable()`, pinning
  `PYTHONUSERBASE` (the load-bearing half — `uvicorn` lives there too) and
  prepending this checkout's `src/`. Importability and `$HOME` isolation are
  asserted as one pair, so a "fix" that leaked the real `$HOME` back cannot pass
- The lesson, and the reason all three are in one block: each was a check that
  only held in the environment it was written in — the class v0.8's requirement
  B existed to remove, surviving one level out into the thing doing the
  checking. `scripts/host_like_pytest.sh`'s hard-coded `.venv` path (fixed in
  v0.8's own task 043) was the first instance; these are the second, third and
  fourth. A suite figure is a claim about an environment, so it now names one

## v0.9 — the loop's rules become data, and only the harness writes state

Full PRD:
[`docs/prds/v0.9-declarative-loop.md`](prds/v0.9-declarative-loop.md).
v0.8 decided whether a run can be trusted at the moment a human touches it; this
wave made the machine itself readable — the transition space becomes a declared
table, and the authority for task state moves from the agent to the harness.
Requirement letters below are the PRD's; it does not use J, K or N. The run that
landed it (`selfdev-v09-fsm-loop`) reached its deadline mid-review with 20 of its
23 tasks completed **and verified** and three deliberately skipped, so this block
names what did not land as plainly as what did.

Phase 1 — the boundary every later claim rests on:

- ✅ **A. An iteration cannot reach its own supervisor's control plane (#62).**
  v0.7's #48 closed the signal half (real and saved uid `0`, so no `pkill`
  reaches the engine); this closes the HTTP half. The surface is split so an
  iteration's environment reaches only the observation routes, and every mutating
  route — `/abort`, `/interrupt`, `/pause`, `/resume`, `/retry`, `/shutdown`,
  `PATCH /config/budget` and every `PUT`/`DELETE` under `/config` — is asserted
  refused **per route**, so a route later added to the wrong listener fails the
  check instead of passing by omission, with a docker-tier twin proving it in a
  real sibling container; the shape was chosen and argued in
  `artifacts/reports/requirement-a-boundary-design.md`, and `--api-token`'s
  documented purpose is corrected: it is a network control and was never a
  container boundary. **Not finished:** `SPEC.md`'s prompt-resolution prose and
  its `PUT /config/prompts/{name}` row still describe that surface as freely
  API-writable rather than host-only — a doc-only gap, filed and left open as
  **#70**

Phase 2 — the machine becomes data:

- ✅ **B. The transition space is a declared table the engine executes (#60
  pillar 2).** `src/ralphd/engine/transitions.py` holds `TABLE` as data over
  `ALL_STATES`, with vigilant and non-vigilant as two variants of one table
  (`VIGILANT_DELTA`) rather than two implicit paths through one function;
  `loop.py`'s imperative core is replaced by `_step_*` dispatch through an
  executor that refuses a transition the table does not declare; and
  `docs/architecture.md` carries the table *rendered from the code* by
  `scripts/render_transition_table.py`, with a drift test rather than a
  hand-maintained copy
- ✅ **C. A hat reports; the harness writes (#60 pillar 1).**
  `src/ralphd/engine/contracts.py` declares `HAT_SCHEMAS` for all five hats and
  validates every payload against it; `contract_enforcement.py` and
  `plan_ingestion.py` turn a planning answer into the task record that the
  harness itself writes. `planning.md`, `worker.md`, `task-verify.md`,
  `review.md` and `reflect.md` all moved from "write the state file" to "report
  through your contract", pinned by a cross-prompt check that fails any prompt
  still instructing an agent to edit `tasks.json`, and the standing-rules block
  is harness-pinned rather than trusted to survive a rewrite
- ✅ **D. Task state is unwritable by the agent where the kernel can say so (#60
  pillar 1).** `engine/privsep.py` gains `root_for_write()` /
  `write_root_owned_json()`, `RunDir.write_tasks()` is the sole writer and goes
  through it, and a docker-tier test proves the agent uid really takes
  `EACCES`/`EPERM` on the real file inside a real sibling container. The
  regression the obvious fix invites is covered too: `ralphctl
  rm`/`repair`/`tasks`, the hub reader and `DELETE /api/runs/<id>` are all proven
  still operable — and still able to delete the run dir — against a root-owned
  record
- ✅ **E. Transitions are deterministic first, and the router answers one question
  (#60 pillar 2).** `engine/router.py` is consulted only where the table declares
  a routed edge, answers against `ROUTER_SCHEMA`, has its own `router_model`,
  retry and timeout config keys, falls back deterministically
  (`ROUTER_FALLBACK`), and its cost is accounted as its own phase. **Not
  finished:** `SPEC.md`, `docs/architecture.md` and `docs/api.md` still never
  name `router_model` — the other half of **#70**
- ✅ **F. Task status is a mini-FSM the harness advances at transitions (#60
  pillar 3).** `awaiting-validation` and `validated` join the status vocabulary,
  every value has a declared edge behind it (`TASK_STATUS_TABLE`), the
  `vigilant-verified.json` sidecar is folded into the task record itself, a dead
  dependency is derived rather than guessed, and the 0.25 s `tasks.json` poll is
  retired — with `artifacts/reports/poll-retirement.md` recording what carries
  liveness in its place
- ⏳ **G. Nothing the last eight waves learned is lost in the rewrite.** The half
  that landed: `artifacts/reports/conformance-ledger.md` exists with a row per
  incident-derived guard, and its `LoopSupervisor`-importing module count is
  drift-tested against the code rather than retyped. The half that did not: 25 of
  its rows still read `TODO` in the "new expression" column, and it carries no
  before/after collected-test-count section. Task 108 was **skipped** under the
  run's deadline rule and the remaining design filed as **#69** — the one
  requirement this wave leaves substantively incomplete rather than doc-gapped

Phase 3 — the two incidents that bought this wave:

- ✅ **H. A correct refusal is not stagnation, and an impossible task is caught at
  plan time (#63).** A worker can report `unsatisfiable` with a reason through
  its contract; the harness records it, which makes it a *state change* and
  therefore progress rather than silence, and the table routes to a targeted
  replan of that one task while keeping the approach. A genuinely silent hat — no
  report, no state change — still fails the approach after three, pinned in the
  same suite so this cannot become an escape hatch from the stagnation guard.
  The plan-time lint rejects a task gated on the run's own terminal state,
  fixtured on v0.8 task 042's real text: the regression it exists for
- ✅ **I. A structural wall stops costing every remaining approach (#61).** An
  approach's blocker signature is persisted to `status.json`
  (`blockerSignatures`), so it survives the approach boundary — nothing did
  before — and a successor that stagnates on a *matching* signature ends the run
  terminally with the blocker text in `reason` instead of consuming a third
  approach, while a *different* wall keeps every remaining approach. Both
  directions are driven against the real engine binary, and the pre-existing
  infra-outage degraded path is untouched. Operator steering then found the
  adjacent defect and it was fixed forward rather than filed: a bare aborted
  stream is now classified by *shape* rather than by a duration bound, and an
  interrupted iteration no longer counts toward the stagnation window, because an
  interrupted iteration did no work

Phase 4 — close the loop:

- ✅ **L. Close this wave's issues, from inside the run.** All four issues this
  wave owns — #61, #62, #63 and #66 — closed over the GitHub REST API, 4 in all,
  no hedged closures: a comment generated from that issue's own
  `artifacts/reports/issue-traceability.md` section posted before the state
  change, closed, then re-read with a fresh `GET`; every comment, status and
  resulting state recorded in `artifacts/reports/issue-closure.md`'s v0.9
  section. Left open deliberately: **#60**, which is five pillars where this wave
  lands three plus the boundary — it carries a comment naming, pillar by pillar,
  what landed and what did not, and waits for the next wave
- ✅ **M. Your predecessor's row, and this wave's (fact 12).**
  `docs/prds/README.md` carries `selfdev-v08-auditable`'s settled row —
  `succeeded` / `verified`, 119 iterations, 249.6M tokens, ~$105.23 derived, the
  `+` snapshot markers gone — with prose recording that its approach 1 completed
  42 of 43 tasks and that the 43rd was #63's impossible task; and this wave's own
  PRD is committed byte-identical to the job's `prd.md`, with its own index row,
  early. That row is now settled by the same rule, the run having ended
- ✅ **O. Docs, SPEC, the roadmap and the version bump.** `SPEC.md` gained the
  transition table and its two variants, the response-contract vocabulary, the
  task-status lifecycle, who owns task state and what that means for
  hand-editing, the router and its cost accounting, and requirement A's boundary;
  `docs/architecture.md` gained the rendered table and the liveness story after
  the poll retired; `docs/api.md` and `docs/cli.md` followed the boundary. This
  block and the `0.9.0` bump land **last, and by operator hand**: task 110 was
  `skipped` under the run's deadline rule — deferred by the clock, not by
  decision — and filed as **#71**, which the commit carrying this block closes.
  `docs/architecture.md`'s own "as shipped in" status line deliberately stays a
  wave behind until #70's corrections land, rather than claiming a currency the
  run itself denies

## v0.10 — the clock is not a verdict

Full PRD:
[`docs/prds/v0.10-clock-is-not-a-verdict.md`](prds/v0.10-clock-is-not-a-verdict.md).
`selfdev-v09-fsm-loop` finished its work — 20 of 23 tasks completed and
verified, `COMPLETE` honoured, every commit pushed — and the job deadline fired
mid-review, 3m34s later. The harness read the interrupted review's silence as a
rejection and landed the run `failed`/`unverified` anyway: an absence of
judgement was scored as a judgement against. This wave fixes that, plus the
rest of v0.9's own ticketed debt (the conformance ledger, two doc gaps, a test
script that disarms its own isolation) and one more defect the repair uncovered:
the legacy vigilant-sidecar migration losing a whole approach's verifications.
Requirement letters below are the PRD's; it uses `A`-`J` and `M`, not `K`, `L`,
`N` or `O`.

- ✅ **A. An interrupted review is not a rejection (#72).** `_step_review` now
  discriminates the engine's own cutoff (`interrupted`/`timed_out`/
  `no_traffic_timeout`/a non-`0`/non-`None` exit code -- `LoopSupervisor.
  _review_interrupted`) from an ordinary iteration that ran to completion and
  merely replied with nothing parseable. With budget and wall clock left it
  self-loops in place (`_run_review_iteration`, the same shape as `planning`'s
  `instant_failure_streak` and `worker`'s `no_verdict_yet`) and re-asks at no
  cost to the approach; with none left it advances straight to `finishing` on
  the new `review_unjudged_at_budget_end` edge (`_review_unjudged_at_budget_end`)
  without archiving the approach, without a `review rejected approach N` log
  line, and without writing a composite PRD. `_review_verdict`'s existing
  semantics for a completed iteration with an absent/malformed payload are
  unchanged. `transitions.TABLE`'s two new edges (`review_interrupted`,
  `review_unjudged_at_budget_end`) were declared in task 002; this task wires
  the behaviour. A named regression test reproduces `selfdev-v09-fsm-loop`
  iteration 183 exactly, and a paired mutation test proves the discrimination
  actually matters.
- ✅ **B. A run that has earned `COMPLETE` does not lose it to the clock (#72).**
  Requirement A stops the approach being burned; this is the terminal-record
  half. `LoopSupervisor._deadline_beat_verdict_patch()` adds a
  `deadlineBeatVerdict` status.json field (`{deadline, completeHonouredAt}`)
  to the terminal write IFF every task in the record is done AND a
  `signal: "COMPLETE"` event was actually recorded -- computed from exactly
  one call site, `_review_unjudged_at_budget_end` (never unconditionally at
  the shared tail exit: a genuinely REJECTED last approach leaves the
  identical trace on disk and must not be misreported). `state`/`verdict`
  stay `failed`/`unverified` -- the operator's ruling that `verdict: verified`
  stays the review hat's alone to grant is unchanged -- the marker exists so
  a human or a script can tell "the clock beat the verdict" from "the work
  failed" without parsing free-text `reason`, the same precedent
  `_unconsumed_steering_patch` already set. Two rejected designs (a distinct
  `verdict` value; a distinct terminal `state`) are recorded in
  `artifacts/reports/task-006-deadline-beat-verdict-design.md`.
  `ralphctl status` (human and `--json`) surfaces the marker as a
  `deadline:` line (task 007) so an operator reading a terminal `failed` /
  `unverified` run can tell this shape apart from an ordinary failure
  without reading `--json` by hand; `docs/prds/README.md`'s row-convention
  notes state the matching Outcome-cell phrasing for such a run
  (`the terminal verdict is a deadline artifact (#NN)`), enforced by
  `tests/test_docs_prd_index.py`.
- ✅ **C. The finishing tail gets time it cannot be robbed of (#72).**
  `reflect` (PRD req 24) runs strictly after the job's fate is already
  settled, so by the time it gets its one attempt `self.deadline` is
  ordinarily already behind `time.monotonic()` -- and the per-iteration
  timeout formula every phase shared (`min(iteration_timeout_s, max(60,
  deadline - now))`) floored reflect at 60s regardless of how far past the
  deadline the run already was, nowhere near enough for a real post-mortem
  write (`selfdev-v09-fsm-loop` iterations 183/184/185, a 2m01s window,
  `artifacts/reflection/FAILED.md` and nothing else). Of the PRD's three
  options -- a reserved tail budget carved out of `job_timeout_s`; extending
  `self.deadline` itself the way `_account_infra_wait` extends it for infra
  waits; an independent bound for reflect -- the third was chosen:
  `LoopSupervisor.REFLECT_TAIL_TIMEOUT_S` (600s, still capped by
  `cfg.iteration_timeout_s`) replaces the plain 60s floor for `phase ==
  "reflect"` only, in `_run_iteration_once`'s own timeout computation; every
  other phase's arithmetic, `self.deadline`, `deadlineAt` and
  `job_timeout_s`'s documented meaning are untouched, and
  `_run_reflection()`'s one-attempt-per-process contract bounds the total
  extra wall time this can ever cost a run to `min(iteration_timeout_s,
  600)` seconds. The two rejected designs are written down in
  `artifacts/reports/task-008-finishing-tail-budget.md`.
- ✅ **J. A resumed run dir does not lose an approach's verifications (#75).**
  `RunDir.migrate_legacy_vigilant_verified()` used to match the legacy
  `vigilant-verified.json` sidecar's approach-namespaced entries
  (`"<approach>:<taskId>"`) against `current_approach()` alone -- safe only
  while the live tasks.json is a fresh replan for that approach, which it is
  not on a run dir where the approach counter advanced without the
  archive-then-replan that normally goes with it. Observed on
  `selfdev-v09-fsm-loop`: `status.json.approach == 4` while tasks.json still
  held 20 tasks completed and verified under approach 3, unrenumbered --
  matching only approach 4's empty namespace demoted all 20 and then deleted
  the sidecar anyway. Fixed: `RunDir._resolve_legacy_vigilant_ids` now falls
  back to another approach's namespaced entries for a task id only when that
  approach's own archived snapshot (`archived_approach_dir`) shows the
  identical task dict already `completed` -- keeping #29's collision guard
  intact for an approach that was never archived or whose archived copy
  differs -- and `RunDir._legacy_vigilant_fold_is_uncertain` withholds the
  sidecar delete whenever the fold needed that fallback, or matched nothing
  while there were completed tasks it could have matched, emitting a
  `vigilant_verified_migration` event either way so "legitimately nothing to
  fold" and "matched nothing while there was something to match" are
  distinguishable in the record. `tests/test_vigilant_verified_migration_task005.py`
  reproduces the incident and pins the ordinary same-approach fold
  unchanged; a mutation test proves the reproduction fails against the old
  current-approach-only logic.
- ✅ **D. Complete the conformance ledger (#69).** `f40d053` filled every
  remaining `TODO` New-expression cell in both guard tables (24 rows: guards
  1-3, 6-19, 21-26) with the guard's post-cutover location -- a
  `transitions.TABLE` guard name where the guard became a declared table edge,
  or the `loop.py` file:line where an internal bookkeeping mechanism is
  unchanged in shape but relocated by the `_step_planning`/`_step_worker`/
  `_step_review`/`_step_replan` refactor -- added the Test-count-drop section
  walking the LoopSupervisor-importing-test-module delta table's two negative
  deltas (the PRD's stale 51->45 naive-grep correction, and task 020's real
  53->52 retirement), and added the retired-guard line naming guard 5's
  `_warn_if_verify_gate_empty` half as the only guard the FSM cutover retires
  outright rather than re-expresses.
  `tests/test_conformance_ledger.py::test_no_new_expression_todo_cells` parses
  both guard tables' last cell and fails on any literal `TODO`, paired with a
  mutation test that reverts one real filled cell back to `TODO` in memory and
  proves the same parser catches it.
- ✅ **E. Close SPEC.md's and docs/architecture.md's remaining gaps (#70).**
  `grep -c router_model SPEC.md docs/architecture.md docs/api.md` was `0`/`0`/`0`
  at planning, now `2`/`2`/`2`: a new router paragraph in SPEC.md §4.1, a new
  "The router" section in `docs/architecture.md` after the generated
  transition table, and a `model.routerModel` paragraph plus a
  `routerTimeoutS`-row update in `docs/api.md` name `router_model`, its
  timeout and how its cost is accounted (`usage.byPhase["router"]`). SPEC.md's
  prompt-resolution paragraph (§6.4) and every `PUT /config/prompts/{name}`
  row across SPEC.md/docs/api.md/docs/architecture.md now say that surface is
  host-only under requirement A's `control_token` split (#62). A wider
  re-check of SPEC.md against v0.9's shipped behaviour (not only #70's named
  gaps) found and fixed three further stale claims predating this task: the
  `scheduler.py` row still said `select_next_task` was "not yet wired" (wired
  since task 116); §4.1's `worker` phases-table cell and §4.3 still described
  the retired whole-plan-`COMPLETE`-only mechanism instead of task 116's
  per-task JSON contract; the task-status mini-FSM paragraph called that same
  cutover future work when task 116 had already landed it.
  `docs/architecture.md`'s "as shipped in" line moves `v0.8.0` -> `v0.9.0`.
- ✅ **F. `host_like_pytest.sh` stops disarming the isolation it depends on
  (#73, #74).** F1 (#73): `RALPHD_RUN_ID` is preserved through the `RALPHD_*`
  scrub (or minted fresh when absent), so the docker tier's per-run tag is
  never the shared `ralphd:test-local-dev` fallback. F2 (#74): the script
  detects a usable docker socket and filters the docker tier out with
  `-m "not docker"` instead of letting it run nested and time out; the
  header no longer claims the tier self-skips for want of a socket. Making
  the nested docker tier pass is explicitly out of scope.
- ✅ **G. Nothing the last nine waves learned is lost.** `review`'s four
  pre-existing successors in `transitions.TABLE` are unchanged by task 002's
  two new edges, and the table's own invariants (terminal states carry empty
  successor tuples; exactly the three `RUN_TERMINAL_STATES` do) still hold.
  `tests/test_stagnation_excludes_nonwork_faults.py`, task 117's fault-shape
  modules (`tests/test_aborted_stream_fault_class.py`,
  `tests/test_fault_classifier.py`, `tests/test_cli_fault_explanation.py`) and
  the claim-rollback tests from v0.9's task 123
  (`tests/test_no_progress_instant_failures.py`'s
  `test_claim_rollback_is_two_directional_by_fault_class`) all have an empty
  `git diff` against this wave's own pre-wave sha `c4252df` -- nothing in them
  was touched or weakened. The 11-module drift lane stayed green at every
  commit this wave landed, most recently 444 passed in 18.64s.
- ✅ **H. Close this wave's issues, from inside the run.** Task 015 (`75b1dd8`)
  posted a requirement -> task -> commit -> tests comment on each of #69, #70,
  #72, #73, #74 and #75, `PATCH`ed each to `state: closed`, then re-`GET` each
  fresh (not the write's own echo) to confirm the resulting state --
  `artifacts/reports/issue-closure.md`'s v0.10 section records the comment id
  and the fresh-read closed timestamp for all six; #60 and #68 were read fresh
  too and confirmed untouched, per this requirement's own exclusion.
- ✅ **I. Docs, SPEC, the roadmap and the version bump.** Land this one early
  rather than last — the lesson v0.9's own requirement O paid for, deferred to
  the tail and lost to the clock. `pyproject.toml` and
  `src/ralphd/__init__.py` both declare `0.10.0`; `tests/test_roadmap_v09.py`
  is pinned to `SERIES = (0, 9)` the same way it once pinned
  `tests/test_roadmap_v08.py`, and `tests/test_roadmap_v010.py` carries the
  current-release checks forward. This bullet shipped first, per the PRD's own
  instruction — unlike v0.9's requirement O, deferred to the tail and caught by
  the clock — and does not claim more than its own requirement; the other
  requirements' bullets in this block are updated in place as each lands.
- ✅ **M. Your predecessor's row, and this wave's.** `selfdev-v09-fsm-loop`'s
  row in `docs/prds/README.md` is left exactly as the operator settled it, per
  this requirement's own instruction. This wave's own row was refreshed from a
  fresh `status.json` read in `7b4a9b5` (task 016), replacing the `not
  started` placeholder with a `+`-marked in-flight snapshot -- task count,
  task-list total, transcript size and derived cost -- per the marker
  convention `docs/prds/README.md`'s own notes describe for a non-terminal
  run.

## v0.12 — green on a real host

Full PRD:
[`docs/prds/v0.12-green-on-a-real-host.md`](prds/v0.12-green-on-a-real-host.md).
`pyproject.toml` and `src/ralphd/__init__.py` still said `0.10.0` at launch:
v0.11 (`docs/prds/v0.11-a-model-per-hat.md`, the per-hat/per-tier model work)
shipped its features on `main` but never bumped the version or gained a
roadmap block of its own, so this wave carries **`0.10.0` -> `0.12.0` in one
step** rather than implying a v0.11 release that never happened. v0.12 itself
closes one defect shape that appeared in three places across the suite --
code that reads the ambient `$HOME`, tests that inherit it, and guards whose
answer depends on the filesystem they happen to run against -- proven at
`129e677` (v0.11's last commit) by seven failures and two red guards on an
ordinary developer host that never showed up in a job container or in
`scripts/host_like_pytest.sh`'s own empty-`$HOME` run. Requirement letters
below are the PRD's own, `A`-`H`.

- ✅ **A. The test harness isolates `HOME` by default (#83, #85, #84).**
  `EngineProc` (`tests/test_e2e.py`) and `LiveRun` (`tests/conftest.py`) now
  spawn every child engine into an isolated `HOME` unless a call site opts in
  explicitly, through one greppable spelling
  (`77d8980`). `tests/test_skills_placement_ambient_home_loss.py` -- the #83
  reproducer -- was inverted into a regression test asserting the loss
  **cannot** happen, its docstring still documenting the mechanism it used to
  reproduce (`b9fc1c6`; this is the wave's one licensed test-weakening
  inversion, and the commit message says so). The discoverability-survives-
  suite proof was widened to every module that used to inherit the ambient
  home (`cbe7975`), and `e9bb54d` records the exact 46-module ambient-`$HOME`
  baseline this requirement replaces.
- ✅ **B. `check_hat_routes`/`provider_keys` refuse a home they were not given
  (#85).** Both now take an explicit home rather than reading `$HOME`
  themselves, with the documented empty-provider skip unchanged (`a40b027`).
  Closed with evidence in `artifacts/reports/task-005-issue-85-closure.md`
  (`d064259`).
- ✅ **C. No ambient-`$HOME` read is left, and a new one cannot land quietly.**
  Every remaining ambient-`$HOME` read under `src/ralphd/` is either threaded
  through explicitly (`place_creds`/`apply_llm`) or landed on a reasoned
  allowlist (`5a96910`), and a guard fails the suite the moment a new read is
  added without being listed or a listed one goes stale (`b3e2298`).
- ✅ **D. A third layout, one that can actually fail (#84).** A host-user-base
  editable install plus a populated, hostile `$HOME` joins the job-container
  and `host_like_pytest.sh` layouts (`0aa20fd`), before/after mutation
  evidence proves the new layout fails against a deliberate mutation of A/B/C
  and passes with them (`219fb21`), and no e2e/live module can spawn a child
  engine without an isolated or explicitly ambient home (`e70cfb2`). Closed
  with evidence in `artifacts/reports/task-011-issue-84-closure.md`
  (`f941df5`).
- ✅ **E. #83's disposition, stated in full (#83).** A first close (`6dbb5a0`,
  before this block existed) argued A-D together made the loss impossible;
  reopening it (`b67f195`, task 001) withdrew exactly that claim, because a
  planning-time measurement at pre-fix `f0ed156` found a **wider** in-process
  `amain()` survivor -- a fast-tier run that explicitly excluded both
  originally-attributed modules still lost the placed skill. Task 002
  (`f0e077c`, corrected `c934a7b`) enumerated the losing nodes; the fix
  landed as an autouse in-process `HOME`-isolation fixture in
  `tests/conftest.py` (`4c5e0fc`, task 003), a regression test that fails
  when that fixture is reverted (`9bef045`, task 004), and a static guard
  against a second ambient-read spelling (`807aba4`, task 005) -- then
  proven at full-suite scale under a hostile, populated `$HOME`: the whole
  fast tier (`78e32bd`, task 006, 4062 nodes) and the developer-host layout
  (`74262b1`, task 008, 4113 nodes) both show the placed skill intact
  afterward. #83's final disposition, per task 009
  (`artifacts/reports/task-009-issue-83-disposition.md`, `293c80f`): closed a
  second time via a fresh GitHub comment (id 5443779978) and
  `PATCH .../issues/83`, live-confirmed `state: closed`. This closes the
  in-process `HOME`-leak mechanism the issue tracked -- it is not a claim
  that no test can ever touch the ambient `$HOME` (a test may still opt out
  via the single greppable `AMBIENT_HOME` marker) or that no unrelated
  mechanism could ever reintroduce a similar loss.
- ✅ **F. A report may only cite paths the repository actually contains (#86).**
  `tests/report_claims.py`'s path check now asks `git ls-files` whether a
  cited path is tracked, not merely whether it `.exists()` on the machine
  that happens to be checking -- with two distinguishable messages for
  "generated, not committed" versus "deleted or renamed" (`6f1055b`). Closed
  with evidence in `artifacts/reports/task-013-issue-86-closure.md`
  (`33c9b36`, corrected to quote GitHub's comment byte-for-byte in `bdd7456`).
- ✅ **G. A reflect that cannot parse itself is a degraded reflection, not a
  failed job (#87).** `_reflect_failure()` now returns a `(error, faultClass)`
  pair split into `REFLECT_FAULT_DEGRADED`/`REFLECT_FAULT_FATAL` spellings,
  neither of which touches the run's already-settled `state`/`verdict`/
  `reason` (`e2868fd`). Closed with evidence in
  `artifacts/reports/task-015-issue-87-closure.md` (`1f19927`).
- **H. The version, the roadmap and the closure.** `SPEC.md` and the
  conformance ledger record A/D/F/G's new contracts (`ac3e14c`). Landed:
  `pyproject.toml` and `src/ralphd/__init__.py` both declare `0.12.0`
  (`82f219e`), this `## v0.12` block exists in the established shape
  (`82f219e`, repaired for E's actual history by this entry), and
  `tests/test_roadmap_v010.py` was retargeted to the past-milestone shape
  `tests/test_roadmap_v09.py` already uses (`f4e58c4`, `82f219e`) --
  #85/#84/#86/#87/#83 are all closed (A-E above), #88
  (`ralphctl skill install`) stays untouched, per the PRD's own exclusion.
  Not yet landed as of this entry: settling this run's row in
  `docs/prds/README.md` from a fresh `status.json` read, and the closing
  operator report walking the repaired A-H -- both tracked as this run's
  own remaining tasks and not claimed here.

## v0.13 — a broken contract is a conversation

Full PRD:
[`docs/prds/v0.13-a-broken-contract-is-a-conversation.md`](prds/v0.13-a-broken-contract-is-a-conversation.md).
`deck-phase3g` ended `aborted`/`unverified` on `'worker' contract violation:
$.task_id: expected string, got int` after 14h 36m and ~$174.52 — the harness
threw away 765 remaining iterations and 10 of 12 approaches over an unquoted
JSON scalar, though the reported work was real and done. This wave finishes a
cutover the code's own comments had been promising since v0.9 (#92): a hat
that breaks its output contract is re-asked, precisely, about the payload
alone, not executed against. It also gives a faulted iteration its own
derived cause (#89), moves eight hand-repeated plan-building practices into
the planner prompt (#90), and fixes a `--user`-layout regression that would
otherwise make this wave's own final sweep a false green (#93). Requirement
letters below are the PRD's own, `A`-`N`; the PRD splits `F` into two
un-lettered sub-headings (`F1`, `F2`) rather than giving each its own letter,
so this block folds them into one `F` bullet, the same way the coverage test
does.

- ✅ **A. All five `loop.py` contract sites go through `enforce_contract`
  (#92).** The hat schema pass (`2be8b86`), the reported `task_status` move
  (`edbcb79`), and all three worker verdict sites — `unsatisfiable`
  (`1311548`), `complete` (`8599b7b`), any other verdict's schema errors
  (`aa0a7b1`) — now share the one retry ladder, with the semantic checks
  supplied as `extra_validate` closures and `contracts.validate_payload`
  still the only `HAT_SCHEMAS` reader. Task 001 (`4e377ff`) recorded the
  five-site cutover map and the re-ask design decision before any site
  moved.
- ✅ **B. The re-ask is a cheap re-ask, never a repeated work iteration
  (#92).** The uncharged contract-fix channel (`d21c9b9`, wired to nothing
  yet) is proven end to end against the real worker path (`b79b258`): a
  malformed first payload followed by a corrected re-ask moves the task as
  the correction said, `iterations_used` advances by exactly one, the re-ask
  prompt carries the validation error and not the worker's task prompt, and
  a transcript is left on disk.
- ✅ **C. An exhausted ladder is an agent fault, and the run continues per
  the table (#92).** `466151f` pins the invariant directly against
  `deck-phase3g`'s own shape (approach 2/12, 765 iterations left): four
  straight exhausted attempts leave `_abort_reason` unset and the run
  advances on a `transitions.TABLE`-declared edge, never a special case in
  the executor.
- ✅ **D. An engine-side cutoff is still not a contract violation.**
  `_iteration_cut_short_by_engine` is checked before the contract pass at
  every cutover site; `tests/test_interrupt_not_substantive_outcome_task004.py`'s
  three pinned assertions were not touched by this wave and still pass.
- ✅ **E. The tests that pin the abort are strengthened, never deleted
  (#92).** The five assertions in `test_worker_harness_owned_status.py`,
  `test_unsatisfiable_criteria.py`, `test_hat_extensibility.py`,
  `test_task_status_lifecycle.py` and `test_tier_escalation.py` were
  strengthened in the same commits that cut over their own site
  (`2be8b86`, `edbcb79`, `1311548`, `8599b7b`), each commit message stating
  the old claim and why the new one is stronger; `test_task_status_lifecycle.py`'s
  own docstring describing the retry loop is now true of shipped behaviour.
- ✅ **F. The `deck-phase3g` regression survives, and the fix is not a
  coercion (#92).** `ff869a4` replays `deck-phase3g`'s exact killing payload
  (`{"verdict": "complete", "task_id": 104}`) end to end and shows the run
  refused-then-repaired rather than aborted (F1). The same commit records
  F2's decision: no scalar coercion was added — normalising one field
  repairs one line, not the next violation — so the coercion-proof-violation
  obligation F2 would otherwise attach does not apply here.
- ✅ **G. The violation is recorded as an event, from the live path (#92).**
  `contract.violation`/`contract.agent_fault` are emitted with hat, attempt,
  maxAttempts and errors from the live path (`b384340`), then surfaced
  through `ralphctl status`'s reason line and `ralphctl iteration N`
  (`fcccaec`).
- ✅ **H. The fault cause is derived per iteration, not only for the last
  one (#89).** `iteration_detail` derives `reason`/`signature` for its own
  iteration through the same `faults.explain_fault` call, both listed in
  `ITERATION_DERIVED_KEYS`, with `faultClass` staying the recorded verdict
  and any divergence reported rather than silently preferred (`2c2c31d`).
- ✅ **I. Every iteration surface renders it (#89).** `ralphctl iteration N`
  (`cd9e3f2`) and the hub's timeline row plus iteration dialog (`f8e5ebd`)
  all render the cause from that one shared derivation.
- ✅ **J. The plan-building practices move into the planner prompt (#90).**
  All eight practices recent waves had to spell out by hand landed in
  `src/ralphd/prompts/planning.md`, each naming the incident that taught it
  (`ca9fa49`).
- ✅ **K. What belongs in a lint instead, decided — and nothing added (#90).**
  No lint was added for practice 5 (cited paths, unlintable at ingestion by
  construction) or practice 7 (closure `dependsOn`, would need a schema
  field that does not exist); the decision is recorded against #76's own
  cautionary shape (`7b96ab5`). The lint machinery itself is the part cut —
  both practices stay prose-only.
- ✅ **L. The prompt change is live and safe (#90).** `planning.md`'s
  existing docs/flag consistency guards still pass with the eight practices
  in place, verified in the same pass as J (`ca9fa49`).
- ✅ **M. The `--user` layout is green again, and the tier can see it
  (#93).** The autouse in-process `HOME` fixture now also pins
  `PYTHONUSERBASE` (`5506ff8`), and `scripts/host_like_dev_layout.sh` gained
  a `PYTHONUSERBASE`-unset mode, shown by mutation evidence to fail against
  the reverted fix and pass with it (`ba78cc2`).
- ⏳ **N. The version, the roadmap and the closure.** `pyproject.toml` and
  `src/ralphd/__init__.py` both declare `0.13.0` (`f07b965`); this `## v0.13`
  block lands in the established shape in this same entry; #92, #89, #90 and
  #93 are all closed with evidence comments (tasks 020-023), #88 and #91
  left untouched per the PRD's own exclusion. Approach 1 (33/33 tasks
  `validated`) landed the fast/docker/browser tier sweeps (`afe3f0d`,
  `5caf387`, `80a70e3`, tasks 026-028), both dev-layout-mode sweeps
  (`ddfc598` exported, `8eadada` unset, tasks 029-030), the
  repository-guard re-verification (`9820c66`, task 031), the
  `docs/prds/README.md` row (`b6e82b4`, task 032) and the closing operator
  report walking A-N (`5b5efee`, task 033) — but that sha was **rejected
  by review**: the guard re-verification and every sweep it cites ran
  RED, not green, because approach 1's own tasks 002 and 015 had each
  drifted a guard the sweeps never repaired
  (`test_loop_subprocess_dispatch_sites_are_owned_by_exactly_these_functions`
  — a third `runner.run(` site task 002 added at `d21c9b9` without updating
  the count it asserted — and `test_no_unlisted_or_stale_ambient_home_reads`
  — the allowlisted ambient-`$HOME` line numbers drifting under the
  wave's own `loop.py`/`ui_server.py` edits), and three operator steering
  directives went unactioned. Approach 2 repaired both wave-introduced
  guard regressions
  by re-pinning the allowlist to the reads' current lines (`b0beb59`) and
  strengthening the dispatch guard to assert the *set* of owning
  functions rather than a bare count (`e2c3e01`), then re-ran every
  sweep at the resulting green sha: fast tier (`a9236cf`, 4198
  passed/1 skipped/0 failed/852.84s), docker tier (`dc91ac1`, 20
  passed/0 failed/723.74s), browser tier (`d94fb2f`, 42 passed/0
  failed/477.84s), dev-layout exported mode (`6fc51b8`, 4255 passed/1
  skipped/20 deselected/0 failed/1324.70s) and dev-layout unset mode
  (`5ac512b`, 4260 passed/1 skipped/20 deselected/0 failed/1301.49s) —
  tasks 009-013 — but that sha was **rejected by review again**: task
  015's own edit to this bullet (`2da0a67`) named a stale test identifier
  in prose — the old name approach 2's own task 002 had already renamed to
  `test_loop_subprocess_dispatch_sites_are_owned_by_exactly_these_functions`
  at `e2c3e01` — and every sha from `2da0a67` onward, including the
  shipped `5e26f3f`, carried it and made the fast tier RED
  (`test_docs_consistency.py`'s backticked-identifier check, 1
  failed/4237 passed/840.97s at `5e26f3f`), though none of approach 2's
  own five sweeps (all taken before `2da0a67`) ever collected it. Approach
  3 (this repair pass) fixed the stale identifier first (task 001,
  `508f95b`), landed the three remaining #94 budget knobs the operator
  had steered in during approach 2 and a same-wave auto-resume/allowlist
  fix those knobs' own commit needed (tasks 002-004, `6497422`/`c2b54fe`/
  `bdf2bb4`/`eb5dbcc`), then re-took all five tier records at the
  resulting true final code sha (`eb5dbcc`): docker (`b8e15b9`, 20
  passed/0 failed/889.08s), browser (`f9e442c`, 42 passed/0
  failed/473.94s), dev-layout exported mode (`154d07e`, 4318 passed/1
  skipped/20 deselected/0 failed/1337.40s), dev-layout unset mode
  (`9610c78`, 4323 passed/1 skipped/20 deselected/0 failed/1355.48s) and
  fast tier, run last (`c11c1f5`, 4286 passed/1 skipped/62 deselected/0
  failed/856.29s) — tasks 005-009. The `docs/prds/README.md` row
  resettle (task 010) has now landed too. Not yet landed as of this
  entry: the repository-guard re-verification at approach 3's own true
  final sha, and the closing operator report walking the repaired A-N —
  both tracked as this run's own remaining tasks and not claimed here.

## v0.14 — every hand on the plan is named

Full PRD:
[`docs/prds/v0.14-every-hand-on-the-plan.md`](prds/v0.14-every-hand-on-the-plan.md).
`deck-phase3g` lost six dependent tasks to one `failed` task with neither `worker.md`
resolution able to clear the doom; `selfdev-v013-a-broken-contract` steer 003 was marked
`consumed` with three quarters of it undone; the first approach of every wave since v0.11 was
rejected for a suite that was not green at its own shipped sha. Nine issues (#103, #99, #98,
#101, #102, #100, #95, #96, #88 — and #91, closing as a consequence of #98), and this run
(`selfdev-v014-hands-on-the-plan`) supplied a further data point for #99 on its own:
approach 1 landed 22 of its own 35 tasks (requirements A through R, including P's own work),
then task 016 — requirement P, itself only a test-fidelity shortfall on landed code —
went `failed`, and the twelve-deep dependency chain the closing sequence had been built as
(`024→025→…→035`) made every remaining issue closure, doc update, sweep and the closing
report itself permanently unreachable in one stroke. The engine driving that approach had
been built from the wheel at launch, hours before this same wave's own requirement E and D
fixes landed in its own tree — so the run reproduced the #99 doom scenario its own fix was
shipping, and could not reach the escape hatch it had just built for the next run. Approach 2
(this record) repairs two defects requirement A's own guard mechanism carried into that same
approach, gives P's test-fidelity gap one honest attempt, and closes the wave's issues and
record. Requirement letters below are the PRD's own, `A`-`S`.

- ✅ **A. A sweep record's sha becomes a checkable, machine-readable fact — and the checker
  itself was wrong twice (#103).** Approach 1 (`c4f24ad`, `007c33c`, task 002) shipped the
  `sweep-sha` block and the discharge-chain check. Approach 2 found and fixed two defects in
  that same mechanism before trusting it for this wave's own closing sequence: the pytest
  summary-line detector matched prose containing "N failed" (misclassifying
  `artifacts/reports/v0.14-operator-notifications.md` as an undeclared sweep record),
  fixed by anchoring the regex to a whole line with a duration suffix (task 001, `e7524cc`,
  `5f9d7cf`); and the discharge rule could never be satisfied at a wave's own true final sha,
  because a `discharge-sha` line can only name a sha that already exists, so the commit that
  adds it always moves `HEAD` one past the chain it discharges — fixed by accepting a
  report-only tail past the chain's end (task 003, `d46199c`). Both fixes are pinned by tests
  that fail against the pre-change code (`tests/test_sweep_sha_guard.py`,
  `tests/test_final_sha_guards_discovery.py`). A third defect surfaced only once approach 3
  reviewed the mechanism's own reach: the mandatory `sweep-sha` declaration applied only to
  filenames beginning `v0.14-`, so dozens of real sweep records outside that prefix carried a
  pytest summary line with no declared sha at all. Fixed (task 008, `11f8b41`+`e7fbe91`): the
  filename gate is gone, the rule now applies to every report under `artifacts/reports/` that
  contains a pytest result line, a new `archived-sha` block form is accepted only for a sha that
  is `LAUNCH_SHA` or a genuine ancestor of it, and all 24 previously-undeclared records were
  backfilled with one.
- ✅ **B. The guard subset a docs- or report-only commit can break, discovered rather than
  listed (#103).** `scripts/final_sha_guards.sh` (`0ee0ec0`, widened at `34be5fa`) finds its
  membership by grepping `tests/` for modules that read `docs/`, `artifacts/` or `README`
  content, rather than a hand-maintained list — the shape v0.13's own approach 3 task 012
  attempted as a list and shipped `failed`.
- ✅ **C. `worker.md` stops confidently recommending a no-op (#99).** The `## Completion
  signal` section now states plainly that neither of its two resolutions for a
  validation-exhausted task clears dependency doom for that task's dependents, and instructs
  the worker to report the doomed set and stop rather than pick a resolution that cannot work
  (`945fcf2`).
- ✅ **D. A doomed plan raises a named fault instead of starving quietly (#99).** When the
  doomed set covers the entire actionable remainder, the loop now raises a fault naming the
  doomed tasks and their dead ancestors rather than producing three silent no-progress
  iterations and a stagnation misattribution (`838753e`, `tests/test_doomed_plan_fault.py`).
  This run's own approach 1 reproduced exactly the failure D exists to prevent — but under the
  wheel-baked engine that predates this fix, so D's own mechanism was never live in the process
  that needed it; the fix is proven by test, not by the run demonstrating it against itself.
- ✅ **E. `ralphctl amend --status`: the operator's typed, unforgeable status ruling
  (#99).** The engine-side applier validates a ruling against `TASK_STATUS_TABLE` at load
  time, preserves `validationAttempts`/`tierLadderExhausted`, and emits an audited event
  (`12f7c9e`, task 005); `ralphctl amend <run> <task> --status` writes it through the same
  read-only amendments mount (`df28b1e`, task 006). `deck-phase3g`'s operator amendment did
  nothing for the scheduler; this is the lever that would have.
- ✅ **F. The vocabulary question, decided (#99, #51).** No third status term was added.
  The decision — E is the sanctioned resolution for "the work landed, one stated property did
  not" — is recorded in `SPEC.md` and in this wave's closing record rather than left implicit
  (`4ec3629`, task 008).
- ✅ **G. The steer hat: a state, a schema, a prompt, a model role (#98).** `steer` is a real
  `TABLE` entry, `HAT_SCHEMAS` entry and `MODEL_HATS` entry with its own prompt, reached only
  through declared edges (`0650acd`, task 009, `tests/test_steer_hat_table_entry.py`).
- ✅ **H. The authority boundary is the tool surface, not the prompt (#98).** The steer hat's
  tool set carries no budget tool, asserted by a test that fails the moment one is added to
  the wrong list; its status-changing tool calls requirement E's own applier rather than a
  second implementation (`a6f63ec`, task 010, `tests/test_steer_hat_tool_surface.py`).
- ✅ **I. Priority, `--now`, and what "settled" means (#98).** Steering-origin tasks are
  marked on `tasks.json` and every surface (`dd550fd`, task 011); unsettled steering runs
  next, above validation-failed retries and any pending task, deterministically
  (`45860c2`, task 012); `settled` is split from `done`, and terminal success is gated on
  steering-origin tasks rather than on a steering file (`a3d31d4`, `3215df4`, task 013) — the
  exact conflation steer 003 fell through (`tests/test_steering_origin_marker.py`,
  `tests/test_steering_priority_routing_task012.py`, `tests/test_status_unconsumed_steering.py`).
- ✅ **J. Uncharged, laddered, pre-plan left exactly alone (#98).** The steer hat's own
  iteration is uncharged via a third refund kind, malformed output gets the existing contract
  ladder, and pre-plan steering keeps its prompt-injected shape unchanged (`db4a9a2`,
  `c6385c5`, task 014, `tests/test_steer_uncharged_task014.py`). **With G-J landed, #91
  closes as a consequence**: adding scope to a running approach is now possible through the
  steer hat's task-insertion path. The "every hat" pin itself went stale once `plan_review` and
  `verdict_gate` joined the hat table without a matching update to the two whole-hat-table lists
  this requirement's own test walks — re-pinned by approach 3 before trusting it for this
  closing sequence (task 002, `40cc84d`).
- ✅ **K. The peer plan review gate (#101).** An opt-in peer model approves a plan or routes
  objections back into a bounded planning revision (`1a4c1bf`, task 018,
  `tests/test_peer_plan_review_gate.py`).
- ✅ **L. The peer verdict gate, genuinely blind (#101).** An opt-in peer model re-verifies a
  `verified` review without the reviewer's own reasoning or verdict before the run may exit
  (`75e6c43`, `07aa515`, task 020, `tests/test_peer_verdict_gate.py`).
- ✅ **M. The router's first live edge, and the fail-fast that must not be skipped
  (#101).** The peer gate declares the first real `ROUTER_FALLBACK` entry and exercises
  `router.decide()` from production rather than a fixture hat; an opt-in gate whose configured
  model has no route fails at start time rather than silently degrading to "no gate"
  (`f5fbd29`, `8832903`, task 019).
- ✅ **N. A worker's split, proposed through the contract; budgeted, marked, no escape hatch
  (#102).** `proposed_tasks` is an optional `HAT_SCHEMAS["worker"]` field ingested through
  `plan_ingestion`'s existing lint, scoped to splits only, budgeted per approach, marked with
  a worker-origin marker, checked for `dependsOn` coherence, and pinned that proposing a task
  alone still fails the approach under the stagnation guard (`191aa3f`, task 021,
  `tests/test_worker_split_proposal.py`); the superseded `worker.md` prose was deleted in the
  same commit.
- ✅ **O. The whole-plan completion marker becomes a contract value (#100).** The worker
  contract's `verdict` enum gains a fourth value valid only when no task was assigned,
  cross-checked against `_outstanding_for_completion()` the same way the old marker was, and
  `<promise>COMPLETE</promise>` is retired from `worker.md` including the paragraph that
  existed only to explain the textual channel (`9a6f202`, task 015,
  `tests/test_worker_plan_complete_task015.py`).
- ✅ **P. A paused run does not burn its own deadline (#95) — landed, with a disclosed
  residual now also landed.** `_gate()` credits only the span actually parked, extends both
  deadline fields, republishes `deadlineAt`, and records `pausedTotalS` beside
  `infraWaitTotalS` with a pause-specific `deadline_extended` reason distinct from an infra
  wait (`b4fa78a`, task 016, requirement P). That same task's validator found the module's own
  test fidelity wanting — no test drove a pause requested while an iteration was genuinely in
  flight through a stubbed `sup.runner` — and the task was marked `failed` on that gap alone,
  becoming the doom this wave's own D and E were shipped to answer (see D above). This
  approach gave the residual its one prescribed honest attempt and it landed:
  `test_pause_mid_flight_iteration_credits_only_the_real_park` drives a real in-flight
  iteration and is hand-verified to fail when `_account_paused_wait`'s call in `_gate()` is
  removed (`8ed5083`, task 004, `tests/test_pause_deadline_credit_task016.py`). **#104**, filed
  against exactly this gap, remains **open** as of this entry — the fix it asked for is in the
  tree and green, but closing the tracking issue itself was not this wave's own task.
- ✅ **Q. Reflect gets the ladder every other hat has (#96).** `_reflect_verdict` now goes
  through `contract_enforcement.enforce_contract` like the other five sites, keeping v0.12
  requirement G's guarantee that an unrecovered reflect stays degraded rather than fatal, and
  bounding the uncharged re-ask inside `REFLECT_TAIL_TIMEOUT_S` (`82b05a6`, task 017,
  `tests/test_reflect_tail_budget.py`).
- ✅ **R. The shipped operator skill can be installed (#88).** `ralphctl skills install`
  copies (not symlinks), stamps, refuses on local modification, verifies discoverability, and
  auto-detects both `~/.claude/skills/` and `~/.pi/agent/skills/` (`80e4678`, task 022); the
  wheel ships `skills/` via the same force-include treatment as `container/`, and
  `ralphctl doctor` reports the installed/missing/stale line (`0839fbb`, task 023,
  `tests/test_cli_skills_install.py`).
- ⏳ **S. The predecessor's row, this wave's row, the version, the roadmap, the closures.**
  Landed as of this entry: `0.14.0` in `pyproject.toml` and `src/ralphd/__init__.py` (task 008);
  this `## v0.14` block; #103, #99, #100 closed with evidence (task 005); #95, #96 closed with
  evidence disclosing the P residual (task 006); #98, #91, #101, #102, #88 closed with evidence
  (task 007) — all ten issues this wave owns are closed, and a fresh `GET` at this entry confirms
  every one of the ten still reads `closed`. Two claims this block's own first draft made under
  "not yet landed" were wrong the moment the tree already held the work:
  `selfdev-v013-a-broken-contract`'s own PRD index row is settled, not stale (task 010,
  `2c698d5`), and `SPEC.md`/`docs/cli.md`/`docs/api.md`/`docs/architecture.md` already carry the three new hats, the
  amendment ruling, the `plan_complete` contract value, pause semantics and `skills install`
  (task 011, `48ace84`) — both corrected here rather than left standing.

  **Approach 3 is a recovery approach**, opened because the independent review at `551066c`
  found the shipped suite genuinely RED at that sha: 25 fast-tier failures, all this wave's own
  regressions and none pre-existing (evidence:
  `/run/ralphd/artifacts/plan-a3-attribution-evidence.md`,
  `/run/ralphd/artifacts/plan-a3-redmods.log` — the same 14 modules at the run's
  launch sha `8fc9ee3` give 778 passed / 0 failed), and found that seven v0.14 records had
  already called those 25 failures "pre-existing". All 25 are now fixed: the legacy loop-driving
  fixtures that still signalled completion with the retired `<promise>COMPLETE</promise>` marker
  were migrated onto the `plan_complete` contract value (task 001, `5820b45`); the two
  whole-hat-table lists requirement J's every-hat pin walks were re-pinned to include
  `plan_review` and `verdict_gate` (task 002, `40cc84d`, see J above); the line-cited
  ambient-`$HOME` allowlist and conformance-ledger guards were re-pinned to the code they cite
  (task 003, `3427628`); the pinned verbatim documentation text the wave's own edits broke was
  restored (task 004, `1ff384e`); and the roadmap-vocabulary and issue-traceability claim guards
  were made green (task 005, `0f658c4`) — re-running the exact 25-node set plus the two modules
  that did not exist at launch now gives 880 passed, 0 failed. The false "pre-existing"
  attribution was then corrected in all seven records that carried it, dated and without
  deleting any original count (task 006, `e5d6247`+`29348d0`+`9a03037`); the approach-2 closing
  report's non-absolute predecessor path and its 18/18-vs-actual-17/1 task tally were fixed, and
  every place it called a red (`exit=1`) run green now names it red (task 007, `9a16689`); and
  requirement A's own `sweep-sha` mandate was widened from files named `v0.14-*` to every report
  under `artifacts/reports/` carrying a pytest result line, with the 24 previously-undeclared
  records backfilled and a new `archived-sha` ancestor form added (task 008, `11f8b41`+
  `e7fbe91` — see A above).

  **Corrected by v0.14.1, from a terminal, self-contradicting record — this is what actually
  happened, not the live "not yet landed" list this entry originally carried.** Approach 3
  ran all five fresh sweeps, re-verified the discovered guard subset at its own final sha, posted
  fresh evidence to the ten closed issues, cleared the debris the prior review found, and wrote
  the closing operator report (task 017) walking A through S — every item the first draft of
  this paragraph called outstanding did land. **17 of 18 approach-3 tasks validated** (001–016
  and 018); **task 017 itself is `failed` / validation-exhausted** — not because the work it
  reported was undone, but because its own report contradicted itself (see the errata below) and
  its notification inventory was incomplete. No requirement was cut; the standing cut order
  (task 013 → 012 → 016) was never exercised.

  An independent review at the shipped sha `f73e94f` then rejected the approach outright on
  **four record-and-evidence findings**, two of them real defects in requirement A's own guard
  mechanism rather than mere paperwork: (1) **#105** — the sweep-record guard's summary-line
  regex rejects pytest's parenthesized elapsed suffix, so it is blind to every sweep longer than
  60 seconds, which is every sweep there is; (2) **#106** — "the last commit is followed by the
  guard subset, green, recorded" has no fixed point, because recording the result is itself a
  commit; (3) the record itself was stale and self-contradictory — this block's own "not yet
  landed" paragraph (the one this text replaces), `docs/prds/README.md`'s index row still
  reading `running`, and the closing report's B/cuts/residuals sections still calling task 018
  pending after its S section called it complete; (4) #103's and #104's own evidence was not
  re-verified with a fresh `GET` before this entry claimed it. Notably, review's own independent
  rerun of the discovered guard subset at that same shipped sha `f73e94f` gave **110 modules,
  2747 passed, exit 0** — **the suite was green at the shipped sha while the record about it was
  not**, which is exactly the shape #106 exists to close. v0.14.1 (below) is the wave that fixes
  #105 and #106 and settles the rest of this paragraph's own bookkeeping.

## v0.14.1 — a guard that cannot see, and a rule that cannot finish

Full PRD:
[`docs/prds/v0.14.1-a-guard-that-cannot-see.md`](prds/v0.14.1-a-guard-that-cannot-see.md).
v0.14 shipped nineteen requirements' worth of working code and then failed on its own record;
independent review found the code sound (379 focused tests across C–R, six green suite lanes,
110 modules / 2747 passed at the shipped sha `f73e94f`) but rejected the approach on the four
findings named in the corrected `## v0.14` block above. This wave is deliberately small: six
requirements, no new subsystems, fixing the two real code defects and settling the bookkeeping
review found around them.

- **A. The guard learns to read the summary line pytest actually prints (#105).**
  `PYTEST_SUMMARY_RE` was widened to accept pytest's optional parenthesized elapsed suffix and
  pinned against both literal PRD summary lines (task 002, `2c97af0`) — landed as substance,
  though task 002 itself is `skipped` on one structurally un-meetable clause (a `-k` selector
  that names its own expected-red node). The sha-declaration backfill this fix exposed (20 newly
  discovered undeclared records) landed in task 003's commits (`8a5882c`, `b71258e`), though task
  003 itself is `failed` / validation-exhausted on a residual (no manifest exhaustively
  enumerating every path those two commits touched) that a later task, or a `skipped`
  relabelling, still owes.
- **B. The final-sha rule gets a fixed point (#106).** Parent-of-HEAD acceptance — a
  sweep-sha declaration needs no `discharge-sha` block when HEAD is exactly the add-commit for
  that record, touching only that record's own path, whose sha is precisely that commit's single
  parent — is implemented and pinned with its two must-still-fail fixtures (tasks 005/006,
  `b64f59e`+`76a1b94`) and documented in `docs/sweep-records.md`, which also now names #106 and
  the five-wave cost the missing fixed point had already run up (task 007, `98cfa7d`).
- **C. The roadmap says what landed.** The `## v0.14` block's stale "not yet landed" summary
  paragraph is rewritten above to state what actually landed, the four review findings, and that
  the suite was green at the shipped sha while the record about it was not (task 008); a
  fast-tier test pinning that no terminal wave's block may carry a live "not yet landed" claim
  follows (task 009).
- **D. The version and the two rows.** `0.14.1` in `pyproject.toml` and
  `src/ralphd/__init__.py`, v0.14's `docs/prds/README.md` row settled from its terminal state
  (`failed`/`unverified`, approach 3 17 of 18 validated, task 017 `failed`), and this PRD's own
  row added (task 010, task 011).
- **E. v0.14's closing report stops contradicting itself.** An errata section reconciles task
  018's actual disposition and the true 17/1 task tally across the report's B, cuts, residuals
  and current-accounting sections (task 012).
- **F. The evidence, the closures, and the record.** #105 and #106 closed with the evidence
  above (tasks 013/014); #103 re-verified with a fresh evidence comment (task 015); #104
  verified and closed, or left open with findings (task 016); the discovered guard subset
  discharged and re-run at the true final sha (tasks 017/020); a fast-tier sweep recorded at the
  shipped sha (task 018); the closing report walking A–F (task 019).

This entry is written from task 008, mid-wave: A and B's code landed and is pinned; C is in
progress (this entry); D, E and F are this wave's remaining tasks, not yet claimed landed here.

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
