# ralphd

**A self-contained, locally runnable autonomous coding loop.**

ralphd runs an AI coding agent ([pi](https://pi.dev)) in a plan → work → review loop
inside a Docker container until a task is verifiably complete — the
[Ralph Wiggum technique](https://www.aihero.dev/tips-for-ai-coding-with-ralph-wiggum),
packaged as an engine you can run on a laptop with no cloud dependencies.

One container per job. The container exposes an HTTP API for observing progress and
steering the agent mid-flight. A CLI (`ralphctl`) does all the heavy lifting: starting
jobs, injecting credentials and skills, watching progress, steering, and collecting
outputs. The CLI is documented to be driven by a human **or** by another AI agent.

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
│  ~/.ralphd/llm-profiles/    │        │  /workspace (mounted or volume)      │
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
  directory (`~/.ralphd/runs/<run-id>/`), so history outlives the container. By
  default a finished container stays **idle** with its API up so the operator can
  inspect and collect outputs; it can be configured to exit on completion instead.
- **Verified, not just "done".** The worker claiming `COMPLETE` only triggers an
  independent review pass; the job succeeds only when the reviewer emits `VERIFIED`.
  Failed reviews start a new approach with the findings folded into a composite PRD.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/architecture.md](docs/architecture.md) | System design: engine, loop, state model, container layout, lifecycle, security |
| [docs/api.md](docs/api.md) | Container HTTP API specification |
| [docs/cli.md](docs/cli.md) | `ralphctl` reference — written for humans and AI agents |
| [docs/llm-profiles.md](docs/llm-profiles.md) | LLM auth: profile format, host forwarding, Bedrock and generic-gateway presets |
| [docs/roadmap.md](docs/roadmap.md) | Versioned delivery plan |

## Status

Design phase. No code yet — the documents above are the source of truth for v0.1
implementation.

## Provenance

ralphd is a from-scratch design and implementation of the Ralph loop concept
(planning/worker/review phases, task-state files, mid-flight steering). It shares
ideas — not code or prompts — with a prior internal implementation.

## License

Apache-2.0 (see [LICENSE](LICENSE)).
