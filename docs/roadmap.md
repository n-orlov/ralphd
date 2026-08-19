# Roadmap

Versions are milestones, not promises of dates. Each version is releasable on its
own; scope may shift between minors, not the ordering of the big rocks.

> **Status (2026-08-09, refreshed by task 059):** v0.1 through v0.4's engine/
> CLI/hub-UI feature scope is implemented and covered by black-box tests (252
> passing, including a real docker-sibling e2e tier and a real browser e2e
> tier for the hub) — see `artifacts/reports/traceability.md` for the full
> requirement-to-test mapping. What remains outside that scope: publishing a
> built Docker image + `pipx` packaging (never attempted, no CI to do it
> from). Every discovered-task follow-up in the 045–060 series is done — no
> open process/hardening follow-ups remain.

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
- ⏳ Docker image published (amd64/arm64); `pipx install ralphctl` — the image
  builds and runs locally (proven by the docker-sibling e2e tier) but has never
  been pushed to a registry, and the package has never been published to PyPI;
  not attempted (no CI/publishing pipeline exists in this environment)
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

- ⏳ In-band LLM errors classified as faults at all — `pi` reports endpoint
  failures with `exit_code: 0`, which `classify_fault()` scores as success, so
  the whole retry/refund apparatus is unreachable in production (#11)
- ⏳ Aggressive retry: fast escalating backoff, a wall-clock **outage budget**
  (default 4h) in place of a 3-attempt cap, every phase covered, plus an
  explicit `health: degraded` / `infraWait` status contract — while keeping the
  fail-fast path for a broken credential (#5, #11)
- ⏳ `POST /retry` + `ralphctl retry <run-id>`: skip the remaining backoff when
  the operator knows the endpoint is back
- ⏳ Reflect phase gets retry *and* reports its own failure instead of silently
  producing no post-mortem (#5)
- ⏳ A run recorded `running` with no container is visible to `repair`,
  `status`, and the hub — not only to `doctor` (#8)
- ✅ Opt-in self-recovery, shipped **off by default**: `ralphctl start
  --auto-resume` (or the registry default `ralphctl config set auto_resume
  true`) marks a run as opted in, and `ralphctl doctor --fix` — run from cron/
  systemd, no new daemon — resumes exactly those runs recorded non-terminal
  whose container has vanished. Opted-out runs are reported and left alone;
  terminal runs and runs whose termination was operator-initiated (`abort`/
  `stop`) are never resurrected; a crash-loop guard (`autoResume: {attempts,
  lastAt, maxAttempts}` in the run dir) spaces attempts with escalating
  backoff and gives up with a readable reason. The default is a single literal,
  `AUTO_RESUME_DEFAULT` in `src/ralphd/cli/main.py` — see the deferred note
  below for the planned flip
- ⏳ `ralphctl watch` stops closing at a *historical* terminal-state marker —
  today the documented agent completion-wait reports a resumed run as finished,
  silently, exit 0 (#13)
- ⏳ The documented sibling-cleanup idiom can no longer delete the job
  container (`ralphd.role` labels + `RALPHD_SELF_CONTAINER_ID`) (#7)
- ⏳ Logs readable from disk when the container is gone, on both hub and CLI —
  the transcripts are already there (#6)

Phase 2 — operator surfaces:

- ⏳ In-flight iteration-budget top-up via API + `ralphctl budget` (#3)
- ⏳ Absolute timestamps in the iteration timeline, `logs`, and `status` (#4)
- ⏳ Unknown cost rendered as unknown instead of `$0.0000` — four runs on the
  dev host, up to 102M tokens, currently read as free (#10)
- ⏳ Sortable, newest-first run list in the hub, with `ralphctl runs` parity (#9)
- ⏳ PRD dialog and clickable task detail in the hub (#1, #2)

## Later / explicitly deferred

- `auto_resume` defaulting to **ON** in a later version. v0.5 ships opt-in
  self-recovery with the default OFF so the crash-loop guard and the "never
  resurrect an operator-killed run" rule can be validated on real runs first;
  the intent is to flip the default once they have been. So that the flip is a
  one-line change, the default lives in exactly one place — the
  `AUTO_RESUME_DEFAULT` literal in `src/ralphd/cli/main.py` (every other
  surface, including the registry-config default and the tests, reads it from
  there; `tests/test_cli_auto_resume.py` is parameterised over its value).
- PID-namespace isolation of agent iterations from in-container kill signals
  (a supervisor-level SIGKILL/SIGTERM currently reaches the running `pi`
  subprocess directly since it shares the container's PID namespace; giving
  each iteration its own PID namespace would let the engine distinguish
  "stop this one iteration" from "the whole container is being torn down"
  more cleanly) — noted during task 060's operator escalation, not attempted
  there; rationale: prompt rules alone did not prevent the iteration-103
  pkill incident, so engine-level self-protection is needed, not just
  prompt-level guidance.
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
