# Roadmap

Versions are milestones, not promises of dates. Each version is releasable on its
own; scope may shift between minors, not the ordering of the big rocks.

## v0.1 — the working loop (MVP)

The smallest thing that is genuinely useful and proves every load-bearing design
decision:

- Engine: planning → worker → review loop, approaches + composite PRD, sentinels,
  atomic `tasks.json`, iteration records, `events.jsonl`
- Vigilant mode and model strategies (they are cheap once per-phase model resolution
  exists, and were designed in from the start)
- Container API: status/tasks/iterations/events/steering/interrupt/abort/shutdown,
  prompts/skills/creds/llm runtime config; optional bearer token
- `ralphctl`: `start`, `runs`, `status`, `watch` (TUI + `--json` stream), `logs`,
  `tasks`, `steer`, `interrupt`, `pause/resume`, `abort`, `stop`, `rm`,
  `artifacts`, `skills`, `prompts`, `llm`, `config`, `doctor` — all with `--json`
- LLM profiles: format + `host`/`none` built-ins + bedrock and gateway example
  profiles **with acceptance tests proving both**
- Docker image (amd64/arm64), published; `pipx install ralphctl`
- Docs: the four design docs kept in sync with reality + a tutorial

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

- Self-reflection phase (post-job analysis proposing prompt/skill improvements as a
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
