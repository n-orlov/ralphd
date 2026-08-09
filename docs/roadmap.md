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

## Later / explicitly deferred

- PID-namespace isolation of agent iterations from in-container kill signals
  (a supervisor-level SIGKILL/SIGTERM currently reaches the running `pi`
  subprocess directly since it shares the container's PID namespace; giving
  each iteration its own PID namespace would let the engine distinguish
  "stop this one iteration" from "the whole container is being torn down"
  more cleanly) — noted during task 060's operator escalation, not attempted
  there; rationale: prompt rules alone did not prevent the iteration-103
  pkill incident, so engine-level self-protection is needed, not just
  prompt-level guidance.
- A `ralphctl repair` command for hand-fixing corrupted run-dir state (e.g. a
  `tasks.json`/`status.json` left in an inconsistent shape by a crash outside
  the paths task 030's crash-consistency tests already cover) — noted during
  task 060's operator escalation, not attempted there; rationale: the
  previous incident's recovery required hand-editing `status.json` directly,
  which should be sanctioned tooling instead of an ad hoc operator edit.
- Remote/daemon mode (running ralphd on a server, CLI over the network) — the
  token+bind options already make this *possible*; making it *nice* (TLS, discovery)
  is deferred
- Alternative agent runtimes behind an interface (claude code, codex, opencode) —
  revisit once the pi-only loop is stable
- Parallel jobs orchestration/queueing — out of scope; the CLI's `--json` interface
  is deliberately sufficient for an external orchestrator (human, script, or agent)
