# ralphd: Complete the Roadmap (v0.1 gaps → v0.4)

## Goal

Bring ralphd from its current proven core (loop engine, observation/steering API,
basic `ralphctl`) to the **full roadmap scope** defined in `docs/roadmap.md`:
finish v0.1, deliver v0.2 (durability & recovery), v0.3 (web hub UI), and v0.4
(quality-of-life). Every feature must be **proved by black-box e2e tests** —
nothing counts as done on the author's word.

## Workspace

The workspace is a git clone of the ralphd repository (Python 3.12 package,
`src/ralphd/`, docs in `docs/`, tests in `tests/`).

**Read these first — they are the contract:**

- `docs/architecture.md` — engine design, state model, creds/skills conventions,
  live-log design, invariants
- `docs/api.md` — every endpoint's exact contract (including `GET /logs` and the
  skills/creds CRUD)
- `docs/cli.md` — every `ralphctl` command's exact syntax and behavior (including
  the tail-style `logs -100 / -150f / logsf` syntax)
- `docs/llm-profiles.md` — LLM profile format and resolution rules
- `docs/roadmap.md` — scope checklist; items marked ⏳ are this PRD's scope

Where the docs specify behavior, **implement exactly that**. If implementation
reveals a doc to be wrong or impossible, fix the doc in the same task and record
the decision — docs and code must never diverge.

## Requirements

### A. Live job log (v0.1)

1. **Engine `GET /logs`** per `docs/api.md`: whole-job NDJSON stream merging all
   iterations' `output.jsonl` in order, with synthetic
   `{"type":"ralphd.iteration",...}` boundary lines (start and end, carrying
   phase/model/exit/error/usage). `?tail=N` bounds the backlog;
   `?follow=true` streams live **across iteration boundaries** until the job
   reaches a terminal state, then closes. Combines with `tail`.
2. **`ralphctl logs` pretty renderer** per `docs/cli.md`: pretty by default,
   Jenkins-console style — iteration/phase headers, streaming assistant text,
   tool calls as compact one-liners (name + key args + outcome), thinking elided
   to a marker, per-iteration usage/cost footer, errors highlighted. `--raw`
   passes NDJSON through untouched. `--iteration n` restricts to one iteration.
   Identical output minus ANSI color when stdout is not a TTY.
3. **tail-style syntax**: `logs <id> -100`, `logs <id> -150f`, `logs <id> -f`,
   and the `logsf <id>` alias. Default tail is 50 lines.
4. The renderer must **tolerate unknown event types** (skip silently) and
   malformed lines (render a one-line marker, never crash) — pi's event schema
   will evolve.

### B. Credentials — env-file convention (v0.1)

5. `ralphctl start --creds <dir>`: copies `<dir>/*.env` (plus recognized extras
   `gitconfig`, `git-credentials`, `netrc`, `ssh/`, `setup.sh`) into the job
   config; files land at `~/.creds/<name>.env` mode 0600 inside the container.
6. **Placement must be implemented in the engine (Python), not the bash
   entrypoint**, so the black-box suite can prove it without Docker (the engine
   reads `/config/creds/` at startup and on API update; honor `$HOME`). Keep the
   entrypoint thin; update `docs/architecture.md` where it currently says the
   entrypoint does this.
7. **Prompts advertise the inventory**: every phase prompt's job-context section
   lists the available `~/.creds/*.env` files by name with the usage rule
   (source the file you need: `set -a; . ~/.creds/github.env; set +a`). No
   values in prompts, ever.
8. Credential values must never appear in `/run` (run dir), events, engine
   stdout logs, or `job.json`.

### C. Skills forwarding polish (v0.1)

9. `ralphctl start --skills <dir>` validation per `docs/cli.md`: a dir with
   `SKILL.md` is one skill; a dir whose immediate children all have `SKILL.md`
   expands to the children; anything else is a usage error (exit 2, clear
   message).

### D. Runtime config CRUD API (v0.1)

10. Full CRUD per `docs/api.md`:
    - `GET/PUT/DELETE /config/skills/{name}` (+ list) — tar bodies, must contain
      `SKILL.md`, visible next iteration
    - `GET/PUT/DELETE /config/creds/{name}` (+ list) — env-file convention,
      read-back allowed by design, placement re-run on PUT
    - `PUT /config/prompts/{name}` — phase prompt override, effective next
      iteration; `GET /config/prompts` lists effective sources
      (builtin/mounted/api)
    - `PUT /config/llm` — env + pi-config fragment, applied to subsequent
      iterations
    - `GET /config` — effective job config, redacted (no secret values)
11. Note the mount problem: `/config` is mounted read-only. The engine must keep
    a writable overlay (e.g. under the run dir is FORBIDDEN for creds — use a
    container-local path) that layers over `/config`; document the chosen
    mechanism in `docs/architecture.md`.
12. `ralphctl` counterparts per `docs/cli.md`: `skills <run-id>
    ls|get|add|rm`, `creds <run-id> ls|get|add|rm`, `prompts <run-id> ls|set`.

### E. LLM profiles (v0.1)

13. Named profiles per `docs/llm-profiles.md`: `~/.ralphd/llm-profiles/<name>.yaml`
    with `${env:}`/`${file:}`/`${cmd:}` host-side resolution; built-ins `host`
    and `none` keep working. `ralphctl llm profiles`, `llm show <name>`
    (redacted), and `--llm <name>` on start.
14. `ralphctl llm test <profile>`: validates that a profile resolves (all
    references resolvable, required fields present) and — when Docker is
    available — spins a throwaway container for a 1-token ping. The resolution
    part must be black-box testable without Docker.
15. Ship **example profiles** in the repo (`examples/llm-profiles/`) for AWS
    Bedrock and a generic OpenAI-compatible gateway — placeholders only, no real
    endpoints or org-specific values.

### F. Durability & recovery (v0.2)

16. **`ralphctl resume <run-id> [--iterations +N]`**: starts a fresh container
    over an existing run dir. Engine on startup detects pre-existing state:
    stale `running` status (crash) or terminal state with budget top-up →
    continue from `tasks.json` (skip planning when tasks exist), keep iteration
    numbering monotonic, recompute remaining budget.
17. **Crash-consistency e2e tests**: kill the engine (SIGKILL) mid-worker-
    iteration, resume, prove no state corruption and the job completes. Kill it
    between iterations too.
18. **Run-dir schema version**: a `schemaVersion` recorded in the run dir; the
    engine refuses (clear diagnostic) to run against a newer schema and
    upgrades/accepts older ones it knows.
19. **Per-phase and per-approach usage accounting**: `status.usage` gains
    `byPhase` and `byApproach` breakdowns (tokens + cost); surfaced in
    `ralphctl status`.
20. **Richer `ralphctl doctor`**: adds checks for default LLM profile
    resolution, registry schema, dangling containers (registry entry whose
    container is gone and vice versa).

### G. Web hub UI (v0.3)

21. `ralphctl ui [--port N]`: serves a local web hub reading
    `~/.ralphd/runs/*` and proxying live container APIs. Views: run list
    (state/verdict/phase/iterations), run detail (task table, iteration
    timeline, live log tail via the pretty-rendering rules, steering form,
    usage/cost).
22. **No build toolchain**: a static bundle (plain HTML/JS/CSS, no npm/node
    build step) packaged inside the wheel and served by the CLI process. The
    server side must not add heavy dependencies to `ralphctl` paths
    (stdlib `http.server`-family is fine; the package's existing deps are
    acceptable).
23. Black-box tests: start the hub against a fixture registry, assert the JSON
    endpoints it exposes to the page (run list, proxy) and that the static
    bundle is served.
23a. **End-to-end browser verification with playwright-cli** (preinstalled in
    this environment — see Environment): for the hub served against a fixture
    registry (and at least one *live* test engine with the stub agent), drive a
    real browser: load the run list, assert runs render; open a run detail,
    assert the task table and iteration timeline show fixture data; submit the
    steering form and assert the steering file appears in the run dir; capture
    screenshots of each view into the job's artifacts directory as review
    evidence. These are pytest tests shelling out to `playwright-cli`
    (open/goto/snapshot/eval/screenshot/close); mark them with
    `@pytest.mark.browser` and skip cleanly when `playwright-cli` is absent so
    the suite still runs in minimal environments.

### H. Quality of life (v0.4)

24. **Self-reflection phase**: `reflect: true` job option — after the job
    reaches a terminal state, one extra iteration (own prompt, `reflect` phase,
    model per strategy) analyzes the run's iteration logs and writes a proposed
    improvements report + unified diff for prompts/skills to
    `artifacts/reflection/`. It must never modify the workspace or run state.
25. **Job templates**: `ralphctl start --template <name>` loading
    `~/.ralphd/templates/<name>/` (job defaults + optional PRD skeleton, skills,
    creds refs); explicit flags override template values. `ralphctl config`
    get/set for registry defaults (image, on_complete, default llm profile).
26. **Completion hook**: `on_complete_cmd` job option — a shell command run by
    the engine (in-container) once on reaching a terminal state, receiving
    `RALPHD_RUN_ID`, `RALPHD_STATE`, `RALPHD_VERDICT` env vars. Failures are
    logged, never affect the job verdict.
27. **Multi-workspace**: repeatable `--workspace <dir>[:name]` mounting several
    repos at `/workspace/<name>`; single-workspace behavior unchanged; prompts
    list the mounted workspaces. Prove the CLI assembles correct mounts using a
    fake docker (`RALPHD_DOCKER` pointing at a recording stub).

### I. Regression fixes

28. **Fix `ralphctl start --no-detach` exit-code bug**: when the container
    exits (`--on-complete exit`), the final `GET /status` poll can hit a
    dying API and crash with `ConnectionResetError`, returning exit 1 even
    though the verdict is `verified`. The CLI must fall back to the run dir's
    `status.json` (it is host-mounted and authoritative) and honor the
    documented contract: exit 0 iff verdict is verified. Black-box test with
    the stub agent in exit mode.

### J. Documentation (continuous)

29. Every task that changes behavior updates the relevant doc **in the same
    task**. The review must check docs-vs-implementation consistency as a PRD
    requirement, not a nicety.
30. Add `docs/tutorial.md`: a start-to-finish walkthrough (install → doctor →
    profile → start a job with skills+creds → watch/logs → steer → collect
    artifacts → resume → ui) runnable by a newcomer.

## Testing rules (hard requirements)

- **Black-box e2e style only** for engine and CLI behavior: launch the real
  `ralphd-engine` (or real `ralphctl` against a stub/recording `docker` and a
  live test engine), observe only via HTTP API, run-dir files, process exit
  codes, and stdout. No importing engine internals in tests.
- **Container-level e2e via docker siblings**: since this job has docker
  access, add a top tier of tests that exercise ralphd at the real container
  boundary — build the image from the workspace Dockerfile (as a sibling
  `docker build`, tag `ralphd:test-<RALPHD_RUN_ID>`), then use the real
  `ralphctl` (real docker, not the stub) to start a job container running the
  stub pi and prove the full path: entrypoint config placement (creds at
  `~/.creds`, skills symlinked), API published and reachable from the test,
  run-dir files appearing on the host side, `stop` reaping. Remember the
  path-translation rule: everything mounted into that sibling must be under
  `$RALPHD_HOST_WORKSPACE` or `$RALPHD_HOST_RUN_DIR` host paths, and the
  registry for these tests must live inside the workspace so the container's
  mounts resolve. Mark these `@pytest.mark.docker`; skip cleanly when the
  docker socket is absent. Clean up images/containers by label afterwards.
  Key features that MUST have at least one container-level proof: creds
  placement, `resume` (stop container, resume over same run dir), and the
  `--no-detach` exit-code fix.
- Extend `tests/stub-pi/pi` as needed (e.g. richer NDJSON shapes for the
  renderer: tool-call events, thinking blocks, multi-chunk text; a `Role:
  Reflector` branch). The stub must keep emitting **realistic pi event shapes**.
- Every requirement above must map to at least one test that fails without the
  feature. The existing tests must keep passing untouched (unless a test
  itself encodes behavior this PRD changes — then change it deliberately and
  say so in notes).
- Quality gates for every task: `ruff check .` clean and the **full** pytest
  suite green (`python -m pytest tests/ -q`), run inside the task's venv.

### Evidence requirements (claims don't count — records do)

- **Traceability matrix**: maintain `artifacts/reports/traceability.md` — a
  table mapping every numbered requirement above to the exact test node IDs
  (`tests/test_x.py::test_y`) that prove it, updated as part of each task.
  Requirements without a test entry are unfinished, full stop.
- **Recorded test runs**: after each task's quality gate, append the full
  pytest output (verbatim, with counts and node IDs) to
  `artifacts/reports/test-runs/<task-id>.txt`. The final full-suite run goes
  to `artifacts/reports/final-test-run.txt` including `-v` output and the
  `@pytest.mark.docker` / `@pytest.mark.browser` tiers actually executed (not
  skipped) — a skipped tier is not evidence.
- **Negative proof for at least the 5 highest-risk features** (resume,
  crash-consistency, creds placement, `GET /logs` follow, vigilant regression):
  demonstrate the test actually guards the feature — e.g. run it against a
  deliberately broken revision or record why the test cannot pass vacuously.
  Note the method per feature in the traceability matrix.
- **Browser evidence**: the playwright-cli hub tests save their screenshots to
  `artifacts/screenshots/hub/` — these are review artifacts, not just test
  by-products.
- **The reviewer must not trust any of the above**: the review phase re-runs
  the full suite itself (all tiers), cross-checks the traceability matrix
  against the requirement list for gaps, and spot-checks at least 5 matrix
  entries by reading the named tests and confirming they test what they claim.
  A missing or stale matrix, or a suite that only passes with tiers skipped,
  is grounds to reject the approach.

## Environment & constraints

- **This job's container has extra capabilities, installed upfront:**
  - **playwright-cli** with headless Chrome, proven working (use the
    playwright-cli skill; no sandbox/shm flags needed). Use it for requirement
    23a and anywhere visual verification helps; screenshots go to artifacts.
  - **Docker sibling containers** (the job was launched with `--allow-docker`):
    the `docker` CLI talks to the host daemon. CRITICAL: sibling `-v` paths are
    HOST paths — use `$RALPHD_HOST_WORKSPACE` / `$RALPHD_HOST_RUN_DIR`, never
    `/workspace` or `/run/ralphd`. Label siblings
    `--label ralphd.run=$RALPHD_RUN_ID` and prefer `--rm`. Use this for any
    test needing an isolated runtime; do NOT use it to escape the sandbox —
    treat the host as read-only except your own labeled siblings.
  - These features (docker opt-in, playwright layer) are **already implemented
    and committed** — they are not PRD scope; don't reimplement them.
- Create a venv in the workspace and `pip install -e ".[dev]"`; the e2e tests
  find `ralphd-engine`/`ralphctl` via the venv's bin on PATH.
- **Do not** run `git commit`, `git push`, or touch `.git/` in any way. The
  operator reviews and commits the diff after the run.
- Do not touch `~/.ralphd` of the host user beyond test-scoped
  `RALPHD_REGISTRY` temp dirs.
- No hardcoded environment-specific values (endpoints, org names, model IDs
  beyond neutral examples) anywhere in code, prompts, or example profiles.
- One task per iteration (the worker prompt's headline rule applies).
- Keep tasks atomic: one endpoint, one command, one feature slice per task,
  each with its tests and doc update.

## Non-goals

- Publishing artifacts (ghcr image push, PyPI release, multi-arch builds) — no
  publishing credentials in this environment; make things *buildable* only.
- Remote/daemon mode, TLS, discovery.
- Alternative agent runtimes (claude code, codex, …).
- Parallel job orchestration/queueing.
- Browser-automation tests for the web hub (data endpoints tested; visual
  polish is operator-verified).

## Definition of done

- All roadmap items v0.1–v0.4 marked ⏳ in `docs/roadmap.md` implemented, or
  explicitly listed in notes as descoped-with-reason.
- `ruff check .` clean; full pytest suite green **with the docker and browser
  tiers executed** (final run recorded in `artifacts/reports/final-test-run.txt`).
- `artifacts/reports/traceability.md` complete: every requirement mapped to
  passing test node IDs, no gaps.
- Docs (`architecture.md`, `api.md`, `cli.md`, `llm-profiles.md`,
  `roadmap.md` status, `tutorial.md`) consistent with the implementation.
- `docs/roadmap.md` checkboxes updated to reflect reality.
