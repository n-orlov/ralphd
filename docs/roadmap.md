# Roadmap

Versions are milestones, not promises of dates. Each version is releasable on its
own; scope may shift between minors, not the ordering of the big rocks.

> **Status (2026-08-08):** the v0.1 core loop is built and proven — engine
> (planning → worker → review, approaches, vigilant mode, model strategies),
> observation/steering API, `ralphctl` core commands, 17 black-box e2e tests, and
> two real-LLM runs including ralphd building its own vigilant mode. The
> remaining scope below (v0.1 gaps through v0.4) is consolidated into a single
> self-development PRD executed by ralphd itself with vigilant mode on.

## v0.1 — the working loop (MVP)

The smallest thing that is genuinely useful and proves every load-bearing design
decision:

- ✅ Engine: planning → worker → review loop, approaches + composite PRD, sentinels,
  atomic `tasks.json`, iteration records, `events.jsonl`
- ✅ Vigilant mode and model strategies (they are cheap once per-phase model resolution
  exists, and were designed in from the start)
- Container API: ✅ status/tasks/iterations/events/steering/interrupt/abort/shutdown
  + bearer token; ⏳ `GET /logs` whole-job stream, prompts/skills/creds/llm runtime
  CRUD
- `ralphctl`: ✅ `start`, `runs`, `status`, `watch`, `tasks`, `steer`, `interrupt`,
  `pause/unpause`, `abort`, `stop`, `rm`, `artifacts`, `doctor`; ✅ `resume`
  (fresh container over an existing run dir); ⏳ pretty `logs`
  (tail-style, whole-job console), `skills`, `creds`, `prompts`, `llm`, `config`
- ⏳ Credentials: env-file convention (`--creds <dir>` of `<name>.env` files →
  `~/.creds/` in-container; prompts advertise the inventory; agent sources on
  demand) — the file-based analogue of original Ralph's AWS Secrets approach
- ⏳ LLM profiles: format + `host`/`none` built-ins + bedrock and gateway example
  profiles **with acceptance tests proving both** (today: `host`/`none` +
  `--forward-env` proven against Bedrock and a corporate gateway)
- ⏳ Docker image published (amd64/arm64); `pipx install ralphctl`
- Docs: the design docs kept in sync with reality (ongoing) + a tutorial

Non-goals for v0.1: resume, web UI, self-reflection, multi-job orchestration.

## v0.2 — durability & recovery

- `ralphctl resume <run-id>`: fresh container over an existing run dir (crash
  recovery, budget top-up)
- Engine-crash consistency tests (kill -9 at nasty moments)
- Run-dir schema version + migration story
- `ralphctl llm test` hardening; richer `doctor`
- Cost/usage accounting surfaced per phase and per approach

## v0.3 — hub UI

- `ralphctl ui`: local web hub reading `~/.ralphd/runs/*` and proxying live
  container APIs — run list, task progress, iteration timeline, live transcript
  tail, steering form, history browsing
- Static bundle served by the CLI (no separate server process to manage)

## v0.4 — quality-of-life

- ✅ Self-reflection phase (post-job analysis proposing prompt/skill improvements as a
  diff in artifacts)
- Job templates (`ralphctl start --template <name>`) bundling PRD skeleton +
  skills + profile
- Pluggable notifications on completion (shell hook: `on_complete_cmd`)
- Multi-workspace jobs (multiple repos mounted side by side)

## Later / explicitly deferred

- Remote/daemon mode (running ralphd on a server, CLI over the network) — the
  token+bind options already make this *possible*; making it *nice* (TLS, discovery)
  is deferred
- Alternative agent runtimes behind an interface (claude code, codex, opencode) —
  revisit once the pi-only loop is stable
- Parallel jobs orchestration/queueing — out of scope; the CLI's `--json` interface
  is deliberately sufficient for an external orchestrator (human, script, or agent)
