# Roadmap

What is planned for ralphd, and one line per shipped version. Versions are
milestones, not promises of dates. How the product behaves today is described in
[SPEC.md](../SPEC.md) and the user docs; the request each wave was built from is
in [docs/prds/](prds/README.md).

## Planned

### Open issues

- **Composite steps (#25).** Let a step upgrade to running one or more sub-jobs.
  Parked as a design discussion.

### Deferred directions

- **PID-namespace isolation of agent iterations.** The uid boundary already stops
  an iteration from signalling its own supervisor (SPEC §13.4). A private PID
  namespace would additionally let the engine tell "stop this one iteration" from
  "the whole container is being torn down" from the process tree itself, and hide
  the engine's pid from the agent.
- **Publishing the Docker image and the wheel.** The image builds locally,
  content-hash-tagged, including from a `pipx`-style install. Nothing pushes an
  image to a registry or a wheel to PyPI, so `--image` names a locally built tag
  and ralphd installs from a checkout.
- **Remote/daemon mode.** The token and bind options make running ralphd on a
  server with the CLI over the network possible; making it pleasant (TLS,
  discovery) is not yet done.
- **Alternative agent runtimes** (Codex, opencode) behind an interface.
- **Named env-wiring profiles.** Reusable `--forward-env`/`--llm-env`/`--env`/creds
  bundles referenced by name at `start`, analogous to LLM profiles. Today a run
  records the wiring it resolved for its own resume, but an operator cannot name
  and reuse a bundle across runs.
- **Parallel job orchestration and queueing** stays out of scope: the CLI's
  `--json` interface is the integration point for an external orchestrator.

## Shipped

- v0.31 — Claude Code polish — the hub and `ralphctl iteration` show each iteration's harness beside its model (#285), `llm test --hats` runs start's route check, takes `--network`, reports a runner's stderr tail and classifies `model_not_found` as refused, and a Claude Code hat no longer inherits gateway or cloud routing env (#286), and the wrapper passes the SDK's `skills=[]` so the CLI's bundled skills stay off (#287).
- v0.30 — a hat can run on Claude Code — another harness runs iterations through the Python Agent SDK on a subscription token, classifies non-transient refusals, waits for rate-limit resets and validates provider access (#284).
- v0.29 — a wait, a cure and a verdict are never lost — a CI-wait re-poll costs wall clock instead of iterations, a review cure after the final sweep re-opens it instead of leaving it validated at an unmeasured tree, and a fenced or verdict-less worker reply is repaired instead of dropped (#279, #280, #281, #282, #283).
- v0.28 — the hub's apply-now checkbox actually interrupts, task selection with two or more candidates is routed instead of claimed by priority-then-list-order, and a REFUTED remedy task's criteria are folded from its own Done when:/Cured when: marker (#276, #277, #278).
- v0.27 — a review converges in one fix round — a spent validation budget asks the operator instead of looping or replanning, and a tail's index read is bounded (#274, #275).
- v0.26 — the operator can see and answer — an oversized escalation is digested and
  retried instead of vanishing, the hub shows and answers a parked run's question
  in place, and the transcript tail stays exact under the byte cap by reading an
  incrementally maintained render index (#269, #270, #271, #272).
- v0.25.2 — the built-in rate table prices Opus 5.5 and the gpt-6 family.
- v0.25.1 — a run's only deliverables are working code and a true spec — engine prompts no longer ask for or enforce paperwork, and a task re-opened by a cure pass gets fresh validation attempts instead of stalling unverified.
- v0.25 — the harness already knows — lock waits, watchdog verdicts, early review verdicts, pushed-commit redaction and control-plane wakes now reach the part of the run that needs them, so an idle loop answers a steer or abort promptly.
- v0.24 — a budget is a boundary, not a verdict — the job deadline and an exhausted iteration budget hold a run in the resumable `timed_out` state instead of interrupting work or failing it.
- v0.23 — a decision must reach the work — `ralphctl rule` and the `operator_input_needed` pause let an operator answer a run's question before it spends an approach, and cures and petitions act on the decisions they reach.
- v0.22 — the harness owns its own state — the control credential, the authoritative `tasks.json` and the petition key store live where the agent cannot replace them, and unschedulable plans are refused.
- v0.21.1 — a green suite is not a met requirement — verdict-gate rejections, planning deaths and ingestion refusals behave as their requirements say, not as their earlier tests assumed.
- v0.21 — the budget belongs to the work — curable gate rejections get cure passes, planning deaths and petitions are costed and bounded, and the hub shows task, tier, cost, version and history per iteration.
- v0.20.2 — a requirement cannot be orphaned — an operator's rulings survive as requirements, and the sweep-sha bookkeeping they retired stays retired.
- v0.20.1 — inspection is not evidence — the progress baseline survives a resume, absorption-forced verification is gone, cures are scheduled before the freeze, and profile headers appear on run surfaces.
- v0.20 — a commit is progress — progress is corroborated by a workspace-tree fingerprint, exhaustion forces a split or a pause instead of a replan, and every hat knows its approach ordinal.
- v0.19.1 — a gate is not a wall — stagnation, cure and petition handling stop failing runs that are still delivering work.
- v0.19 — a note is not a rejection — the verifier can accept delivered work with a note, the stagnation guard sees commits and tool activity, and the suite splits into smoke and fast tiers.
- v0.18 — the run can finish — a bounded cure pass replaces a whole new approach, unsatisfiable criteria are caught before a worker sees them, and the last approach has an exit other than `failed`.
- v0.17 — a slip is not a verdict — a malformed final line from a judging hat is re-asked instead of burning an attempt, and both peer gates are `start` flags.
- v0.16 — a petition is not a postmortem — a worker's impossible-obligation petition is adjudicated when it is raised, and validation exhaustion becomes `petitioned` rather than `failed`.
- v0.15 — impossible is an answer — a run can say "done", "try again" or "this obligation cannot be met" as three distinct outcomes.
- v0.14.2 — green where it runs — the fast tier passes inside a job container that has no docker socket.
- v0.14.1 — a guard that cannot see, and a rule that cannot finish — two code defects left by v0.14 are fixed.
- v0.14 — every hand on the plan is named — the operator, the harness and the worker each get a typed, auditable way to change a plan, and a failed task no longer silently dooms its dependents.
- v0.13 — a broken contract is a conversation — a hat that breaks its output contract is re-asked about the payload instead of ending the run.
- v0.12 — green on a real host — code and tests stop reading the ambient `$HOME`, so the suite passes on a real operator host.
- v0.11 — a model per hat, then a model per task — each hat and each task tier can run on its own model and thinking level.
- v0.10 — the clock is not a verdict — a deadline that interrupts review no longer scores the run as rejected.
- v0.9 — the loop's rules become data — transitions are a declared table the engine executes, and only the harness writes task state.
- v0.8 — a run a human can interrupt, audit and believe — terminal states wait for the unwind, granted budgets survive a resume, and the hub shows approach history and image previews.
- v0.7 — trustworthy unattended operation — one bad iteration can no longer end a run, the agent cannot signal its supervisor, and auto-resume is on by default.
- v0.6 — first-release polish — truthful numbers on every surface, readable steering, one-command cleanup, and a job image that builds locally.
- v0.5 — session resilience & self-recovery — LLM endpoint faults are classified and survived instead of destroying approaches, and a vanished container can be resumed automatically.
- v0.4 — quality-of-life — self-reflection, job templates, completion notifications and multi-workspace jobs.
- v0.3 — hub UI — `ralphctl ui` serves a local web hub for run lists, progress, transcripts and steering.
- v0.2 — durability & recovery — `ralphctl resume`, crash consistency, run-dir schema migration and per-phase cost accounting.
- v0.1 — the working loop — planning, worker and review phases with approaches, a container API, and the `ralphctl` CLI.
