# PRDs

The PRD is the artifact `ralphctl start --prd <file>` consumes: the job's brief,
its standing rules, and its definition of done. This directory keeps the ones
ralphd was built with, so the reasoning behind a wave of work survives the run
that executed it.

Every file here for a wave that has **run** is the verbatim input, byte-identical
to `~/.ralphd/runs/<run-id>/prd.md` (and to `~/.ralphd/configs/<run-id>/prd.md`,
which is what a `resume` re-reads). The five `selfdev-*.md` files were recovered
from that storage after the fact — the earlier waves were never committed. A
brief may also be committed *ahead* of its launch, in which case its outcome
reads `not started`: there is no run dir to be byte-identical to yet, so the run
id names the launch it is written for, not a run that exists.

A row whose counts carry a `+` is a **snapshot**, written by an iteration of the
run it describes: the numbers are a floor, because a run cannot count the
iterations that go on to document and verify it. A finished run's row is the
other thing — the marker is gone and every cell is a settled figure read from
that run's final `status.json`, which is why the marker is what the checks in
`tests/test_docs_prd_index.py` key on.

A row whose Outcome cell reads **failed** / unverified but whose note reports
every planned task `completed` (and, where the plan reached review, verified)
is not reporting a judgement on the work — it is the shape `status.json`'s
`deadlineBeatVerdict` field names (task 006, #72): the run's own deadline took
the last review attempt before review ever rendered a verdict, after every
task was already done and the worker's own `COMPLETE` had already been
honoured. Such a row's Outcome cell states the phrase **`the terminal verdict
is a deadline artifact (#NN)`**, citing the issue the defect was filed under,
so the distinction reads from the table itself rather than depending on the
prose note beside it. `tests/test_docs_prd_index.py` enforces the exact
phrasing on every row whose run recorded the marker.

| Run | File | Outcome | Iterations | Tokens | Cost |
|---|---|---|---|---|---|
| `selfdev-vigilant-4` | [selfdev-vigilant-4.md](selfdev-vigilant-4.md) | succeeded / verified | 13 | 2.1M | $3.39 |
| `selfdev-roadmap-1` | [selfdev-roadmap-1.md](selfdev-roadmap-1.md) | **aborted** / unverified | 1 | 1.2M | $2.22 |
| `selfdev-roadmap-2` | [selfdev-roadmap-2.md](selfdev-roadmap-2.md) | succeeded / verified | 148 | 219.3M | $91.66 |
| `selfdev-roadmap-3` | [selfdev-roadmap-3.md](selfdev-roadmap-3.md) | succeeded / verified | 41 | 43.7M | $21.07 |
| `selfdev-roadmap-4` | [selfdev-roadmap-4.md](selfdev-roadmap-4.md) | succeeded / verified | 43 | 27.6M | $17.35 |
| `selfdev-v05-resilience` | [v0.5-resilience.md](v0.5-resilience.md) | succeeded / verified | 132 | 163.9M | unpriced route |
| `selfdev-v06-release` | [v0.6-first-release.md](v0.6-first-release.md) | succeeded / verified — 54 of 55 tasks completed, 1 skipped | 145 | 332.1M | unavailable |
| `selfdev-v07-unattended` | [v0.7-trustworthy-unattended.md](v0.7-trustworthy-unattended.md) | aborted / unverified — 46 of 47 tasks completed | 138 | 278.1M | ~$323.62 derived |
| `selfdev-v08-auditable` | [v0.8-auditable-interruptible.md](v0.8-auditable-interruptible.md) | succeeded / verified — approach 1: 42 of 43 tasks completed (archived `approaches/01`); approach 2: 8 of 8 tasks completed | 119 | 249.6M | ~$105.23 derived |
| `selfdev-v09-fsm-loop` | [v0.9-declarative-loop.md](v0.9-declarative-loop.md) | **failed** / unverified — 20 of 23 tasks completed and verified, 3 skipped-with-issue; the terminal verdict is a deadline artifact (#72) | 126 | 482.6M | ~$179.71 derived |
| `selfdev-v010-not-a-verdict` | [v0.10-clock-is-not-a-verdict.md](v0.10-clock-is-not-a-verdict.md) | succeeded / verified — approach 1: 17 of 17 tasks validated, then rejected by review for under-evidenced closure (archived `approaches/01`); approach 2: 4 of 4 tasks validated. The reflect iteration was interrupted at the deadline | 57 | 124.8M | ~$54.38 derived |
| `selfdev-v011-model-per-hat` | [v0.11-a-model-per-hat.md](v0.11-a-model-per-hat.md) | succeeded / verified — approach 1: 30 of 30 tasks validated, then rejected by review (a report-only commit left the suite red at the shipped sha, and requirement E's hat x tier matrix had no host-side surface; archived `approaches/01`); approach 2: 16 of 16 tasks validated | 104 | 262.5M | ~$116.07 derived |
| `selfdev-v012-green-on-a-real-host` | [v0.12-green-on-a-real-host.md](v0.12-green-on-a-real-host.md) | succeeded / verified — approach 1: 24 of 24 tasks validated, then rejected by review (#83 closed on a false "loss is now impossible" claim while a wider in-process survivor reproduced at HEAD, and the new dev-layout tier was not green at the shipped sha; archived `approaches/01`); approach 2: 12 of 12 tasks validated | 90 | 134.9M | ~$76.29 derived |
| `selfdev-v013-a-broken-contract` | [v0.13-a-broken-contract-is-a-conversation.md](v0.13-a-broken-contract-is-a-conversation.md) | succeeded / verified — approach 1: 33 of 33 tasks validated, then rejected by review (the suite was RED at the shipped sha from two of the wave's own regressions, three operator steering directives to fix or decide them were never actioned, and the shipped evidence misattributed both failures as pre-existing; archived `approaches/01`); approach 2: 18 of 18 tasks validated, then rejected by review (a docs-only commit correcting the roadmap N bullet reintroduced a stale test identifier the same wave had already renamed, making the fast tier RED at the shipped sha; archived `approaches/02`); approach 3: 11 of 12 tasks validated, 1 `failed` (task 012, "re-verify the docs- and report-reading guards at the true final sha, by discovery not by a hand list" — validation-exhausted, the very trap requirement B of this wave now fixes) | 145 | ~230M | ~$111.57 derived |
| `selfdev-v014-hands-on-the-plan` | [v0.14-every-hand-on-the-plan.md](v0.14-every-hand-on-the-plan.md) | failed / unverified — approach 1: 22 of 35 tasks validated, task 016 (requirement P) `failed` / validation-exhausted (archived `approaches/01`); approach 2: 17 of 18 tasks validated, task 018 `failed` (archived `approaches/02`); approach 3: 17 of 18 tasks validated, task 017 `failed` / validation-exhausted — the closing report itself, rejected for a self-contradictory account of task 018 and a missing notification inventory | 171 | 468.6M | ~$240.95 derived |
| `selfdev-v0141-a-guard-that-cannot-see` | [v0.14.1-a-guard-that-cannot-see.md](v0.14.1-a-guard-that-cannot-see.md) | **failed** / unverified — one approach, a 20-task flat plan; terminal tally 13 `validated`, 3 `skipped` (002, 015, 020), 4 `failed` (003, 009, 012, 019); review honoured all seven operator waivers by name and rejected on the single fast-tier-RED finding this wave's (v0.14.2's) requirement A fixes. That run's own `status.json` is unreachable from this container, so — same convention as v0.14.1's own task 011 used for the v0.14 row above — this row is sourced from this wave's PRD, `docs/prds/v0.14.2-green-where-it-runs.md`, requirement D, rather than a `status.json` read | 70 | 70.5M | ~$58.75 derived |
| `selfdev-v0142-green-where-it-runs` | [v0.14.2-green-where-it-runs.md](v0.14.2-green-where-it-runs.md) | in progress — approach 1, an 18-task flat plan; a fresh tally of `tasks.json` at this read (task 005) — 4 `validated`, 1 `in-progress` (this row-settling task, 005, itself), 13 `pending`, nothing `failed` or `skipped` yet. This row stays a live placeholder, updated as far as this run can observe itself, until the wave ends | 11+ | 9.4M+ | ~$6.11+ derived |

Notes worth keeping with the documents:

- **`selfdev-roadmap-1` and `-2` are the same brief, twice.** The first attempt
  died 50 seconds in: the job's own verifier ran `pkill -f ralphd-engine` and
  SIGTERM'd the engine driving it. `selfdev-roadmap-2` is that PRD plus the
  engine-self-protection task (`ralphd-engine` argument parsing with no side
  effects, an exclusive `flock` on the run dir) and the "you are running INSIDE
  a ralphd engine" prohibitions. Diffing the two is the cheapest way to see what
  one self-inflicted kill taught the project.
- **`v0.5-resilience.md` reports no cost** because the run went out over a
  gateway route whose model ids the provider never priced — the bug that run
  fixed (issue #10): unknown cost is no longer collapsed into `$0.0000`. The fix
  is not retroactive, so this run's own numbers stay zero.
- **`v0.6-first-release.md`'s row is that run's final record**, read from its
  `status.json` after it ended: `state: succeeded`, `verdict: verified` (the
  review passed at raw iteration 154), on approach 1 of a possible 10 — it
  never replanned. The iteration cell is the 145 the engine counted against a
  300 budget, not the 155 raw iteration slots the log holds: the 10-slot
  difference is attempts classified as infrastructure faults, which are retried
  and refunded rather than charged to the budget (`iterationsUsed`, task 001a).
  **That 145 is itself inflated**, by the defect issue #32 names: the refund
  counters lived only in the engine process, so each of that run's resumes
  restarted them at zero and re-charged what had already been credited. 27 of
  its iterations faulted on infrastructure and only 10 refunds survived, so the
  guarantee implies 128 charged, not 145. v0.7 fixes the mechanism (the counters
  are persisted as `status.json`'s `iterationsRefunded` and seeded back on
  resume) but not this row: the cell faithfully reports what the engine wrote,
  and rewriting it would hide the defect instead of recording it.
  The outcome, honestly: every issue in the brief (#14–#22) is closed on GitHub
  with a closing comment (`artifacts/reports/issue-closure.md`),
  54 of the 55 planned tasks are `completed`, and one — `043d`, a whole-SPEC
  rewrite — is recorded `skipped` with all three validation attempts consumed
  (`validationAttempts: 3`, left in place as the record). `skipped` is **not** a
  claim that it met its criteria: it did not. It was relabelled from `failed` by
  operator instruction so the run could leave its task loop and reach the review
  phase, and the scope it was meant to cover shipped in three narrower commits
  instead (`b923af2`, `168a041`, `cc8c8a2`). The cost is `unavailable` rather
  than unpriced-and-silent: the gateway quoted `costUSD: 0` beside 332M billed
  tokens, and the work in this very wave (issue #14) classifies that implausible
  zero as unknown on read instead of rendering `$0.00`. Feeding the same
  recorded counters through the built-in AWS Bedrock table this wave shipped
  (`ralphctl start --price-strategy aws`) derives ≈ $374 — an estimate at that
  table's rates, not money any provider quoted. Full evidence in
  `artifacts/reports/issue-traceability.md`.
- **`v0.7-trustworthy-unattended.md`'s row is now that run's settled
  record**, not the in-progress snapshot it wore before the job ended: the run
  ended `aborted` / `unverified` on an operator abort at `2026-08-23T08:50:53Z`,
  138 iterations charged against its budget (146 raw iteration slots the log
  holds — the 8-slot gap is attempts classified infrastructure faults, retried
  and refunded rather than charged, `iterationsUsed`, the same #32 mechanism
  v0.6's and v0.7's own earlier snapshot explained), 278.1M tokens, and a
  `derived` cost of ~$323.62: the gateway reported `costUSD: 0` beside those
  278.1M billed tokens, which the shipped classifier calls unknown (#14, v0.6),
  and the host-side AWS Bedrock price table derives ≈ $323.62 from the same
  recorded counters — an estimate at that table's rates, not money any
  provider quoted. All 24 intended issue closures happened (`#28, #30–#49,
  #52, #53, #55`, closing comments in `artifacts/reports/issue-closure.md`,
  traced per issue in `artifacts/reports/issue-traceability.md`), and 46 of
  the plan's 47 tasks are `completed`.
  **The 47th, task 041, reads `validation-failed`, and that reading is false.**
  The operator's abort landed mid-*verify* on task 041's own check
  (`ralphctl suggestions show|apply`), on an engine build that still predated
  the fix for #45: that build's verifier treated "no verdict reached" the
  same as "judged unmet" and demoted the task on the interrupt, instead of
  leaving its status untouched the way v0.7's own task 012 (the #45 fix) made
  every later engine build do. `selfdev-v08-auditable`'s own task 030
  independently re-verified `ralphctl suggestions show|apply` at that run's own
  HEAD, hand-driving the real binary against scratch registries as well as
  re-running the existing suite, and found the deliverable sound against both
  #28's and v0.7 requirement P's acceptance criteria, plus confirmed the
  demoting mechanism cannot recur on the engine as it stands today — see that
  run's own report for the evidence (a run-dir artifact, not a file this
  repository carries). **No automated check here catches this class of error
  in general**: a run can end — successfully, even — after the very iteration
  that would have caught its own last task's misfiled status, so this
  correction is a one-time, human-legible fact about one historical run, kept
  here because a person read the evidence, not a rule this table's own tests
  enforce by reading a different run's live registry, which they deliberately
  do not do.
- **`v0.8-auditable-interruptible.md`'s row is now that run's settled
  record**, not the in-progress snapshot it wore across the approach
  boundary: the run ended `succeeded` / `verified` at `2026-08-24T01:19:15Z`,
  119 iterations charged against its budget (121 raw iteration slots the log
  holds — the 2-slot gap is attempts classified infrastructure faults,
  retried and refunded rather than charged, `iterationsUsed`, the same #32
  mechanism v0.6's and v0.7's rows explained), 249.6M tokens, and a `derived`
  cost of ~$105.23, on approach 2 of a possible 8. Approach 1 landed the
  wave's entire behaviour (42 of its 43 planned tasks `completed`) and closed
  all nine wave issues (`#24 #26 #27 #50 #51 #54 #56 #57 #58`), but the
  engine ended it at `2026-08-23T22:58:46Z` on "3 iterations with no task
  progress": the 43rd, task 042, made its own success criteria depend on the
  run having left `state: running`, which cannot happen while an iteration is
  executing, so three worker iterations correctly refused to fabricate it and
  the approach starved. That task is **#63's impossible task** — the incident
  that got "no plan may contain a task gated on this run being terminal"
  filed as a mechanism its successor run (`selfdev-v09-fsm-loop`) now encodes
  at ingestion time. Approach 1's plan and notes are archived verbatim at
  `approaches/01/{tasks.json,notes.md}` rather than overwritten. Approach 2
  added no behaviour: it re-proved every PRD requirement at the post-wave
  HEAD, re-checked the closure record against a fresh GitHub GET, and
  completed its own 8 of 8 tasks — the replan cost 19 iterations and $18.33
  (`usage.byApproach`: approach 1 $86.90, run $105.23) to re-establish
  finished work. The outcome cell's task tally is approach 2's own plan, not
  approach 1's; the iteration/token/cost cells are the run's cumulative
  totals across both approaches, read from this run's own final `status.json`.
- **`v0.9-declarative-loop.md`'s row is now that run's settled record**, not
  the live snapshot it wore while it was writing it: the run ended `failed` /
  `unverified` at `2026-08-26T06:56:55Z`, 126 iterations charged against its
  budget (185 raw iteration slots the log holds — the 59-slot gap is attempts
  classified infrastructure faults, retried and refunded rather than charged,
  `iterationsUsed`, the same #32 mechanism v0.6's, v0.7's and v0.8's rows
  explained, and 1002s of wall clock went on waiting those outages out),
  482.6M tokens, and a `derived` cost of ~$179.71: the gateway reported
  `costUSD: 0` beside those billed tokens, which the shipped classifier calls
  unknown (#14, v0.6), and the host-side AWS Bedrock price table derives
  ≈ $179.71 from the same recorded counters — an estimate at that table's
  rates, not money any provider quoted. It ended on approach 4 of a possible 8
  (`usage.byApproach`: 1 $0.95, 2 $91.92, 3 $85.72, 4 $1.11 — approach 2
  carried the wave).
  **The terminal `failed` / `unverified` is an artifact of the deadline, not a
  judgement on the work (#72).** All 20 of the plan's 23 tasks that completed
  were also *verified*; the other three (108, 109, 110) are `skipped` under the
  run's own deadline rule and each was filed rather than dropped — #69 (G's
  conformance-ledger table left incomplete), #70 (A's and E's doc-only gaps),
  #71 (this wave's own roadmap block and version bump). The run reached
  COMPLETE and entered review; `deadlineAt` fired 3m34s into that review
  iteration, and the engine read the interrupted iteration's missing verdict as
  a substantive rejection — archiving approach 3, replanning into approach 4,
  which the same deadline ended at once. That is #72, filed after the fact; the
  row states the engine's own terminal words because rewriting them would hide
  the defect, exactly as with v0.6's inflated iteration cell. This is
  `selfdev-v08-auditable`'s heir: it inherits that run's guards and, per
  requirement M, wrote the settled row above from that run's own final record
  rather than leaving it as the snapshot it landed with — this row is that
  courtesy returned by hand, alongside requirement O's roadmap block and
  `0.9.0` bump (`8ec5eee`).
- **`selfdev-v012-green-on-a-real-host`'s row has now been resettled from that
  run's own terminal record**, as the snapshot note below asked a later hand to
  do: `state: succeeded`, `verdict: verified`, `endedAt 2026-08-27T20:40:29Z`,
  `iterationsUsed: 90`, `usage.totalTokens: 134912736` (134.9M),
  `usage.costDerivedUSD: 76.291202` (`costStatus: derived` → `~$76.29
  derived`), and all 12 of approach 2's tasks `validated`. The paragraph that
  follows is kept verbatim rather than edited away: it records what the row said
  while the run was live, and why, which is the more useful history.
- **`selfdev-v012-green-on-a-real-host`'s row was a live snapshot of a run still
  in progress** (`state: running`, no `verdict` yet), read fresh from
  `/run/ralphd/status.json` and `/run/ralphd/tasks.json` at
  `2026-08-27T19:09:19Z`: `iterationsUsed: 87`, `usage.totalTokens: 123557962`
  (123.6M), `usage.costDerivedUSD: 60.905272` (`costStatus: derived`, so
  `engine.state.format_cost` renders `~$60.91 derived`), and a fresh tally of
  that moment's `tasks.json` — 11 tasks `validated`, 1 `in-progress`, 12 total.
  Approach 1 ran the wave's entire 24-task plan to `validated` on every task,
  but review rejected it (`approaches/01/review-findings.md`, blocking): #83 was
  closed on a closing comment asserting "the loss is now impossible" while the
  review's own reproduction (`HOME=/tmp/... pytest tests/test_engine_budget_seed.py`)
  wiped a placed skill at the shipped sha, and the new dev-layout tier was not
  green there either. Approach 2 (this repair pass, archived nowhere because it
  is the run's current approach) reopened and re-fixed #83 (tasks 001-009,
  closed again on live evidence, comment `5443779978`), repaired
  `docs/roadmap.md`'s `## v0.12` block to match (task 010), and rewrote
  `artifacts/reports/v0.12-closing-report.md` to the reopened-refixed-reclosed
  truth (task 011); 11 of its 12 tasks are `validated`, the 12th (this row's own
  settle-the-index task) still `in-progress` at the moment of this read. This
  row carries no `+` snapshot marker per this task's own success criteria, even
  though the run has not reached a terminal `state` — a later task will need to
  resettle it once the run actually ends, the same way v0.10's and v0.11's rows
  were resettled after their own approach histories closed out.
- **`selfdev-v013-a-broken-contract`'s row has been resettled twice now,
  mid-run** — once at task 016 (approach 2, since superseded) and again at
  task 010 (approach 3, this resettle), read fresh from
  `/run/ralphd/status.json` and `/run/ralphd/tasks.json`: `state: running`,
  no `verdict` yet, `approach: 3`, `iteration: 137`, `iterationsUsed: 133`,
  `usage.totalTokens: 186676679` (186.7M), `usage.costDerivedUSD: 86.789897`
  (`costStatus: derived`, so `engine.state.format_cost` renders `~$86.79
  derived`). Approach 1 ran the wave's entire 33-task plan to `validated` on
  every task, but review rejected it (`/run/ralphd/approaches/01/review-
  findings.md`, blocking): the suite was RED at the shipped sha from two of
  the wave's own regressions (a third `runner.run(` dispatch site the guard
  did not allow for, and a stale ambient-`$HOME` allowlist pin), three
  operator steering directives asking for those fixes and a model decision
  were never actioned, and the shipped evidence artifacts misattributed both
  failures as "pre-existing". Approach 2 ran its own full 18-task plan to
  `validated` on every task too, but review rejected it again
  (`/run/ralphd/approaches/02/review-findings.md`, blocking): one of
  approach 2's own docs-only commits, correcting `docs/roadmap.md`'s v0.13 N
  bullet, named a test identifier the same wave's task 002 had already
  renamed away, and that stale name made the fast tier RED at every sha from
  that commit onward, including the shipped one — none of approach 2's own
  sweeps caught it because all five were taken before the stale name landed.
  Approach 3 (this repair pass) fixed the stale identifier first (task 001),
  landed the three remaining #94 budget knobs the operator had steered in
  during approach 2 plus a same-wave fix those knobs' own commit needed
  (tasks 002-004), re-took all five tier records at the resulting true final
  code sha (tasks 005-009), and resettled this row (task 010, itself); a
  fresh tally of `tasks.json` at this same read — 9 tasks `validated`, 1
  `in-progress` (this row-settling task, 010, itself), 2 `pending` (011 the
  closing operator report, 012 the guard re-verification), 12 total. This
  row still carries no `+` snapshot marker on any cell, per this task's own
  success criteria — the run has still not reached a terminal `state`, so a
  later task (or a fresh commit once this run ends) will need to resettle it
  again once `status.json` actually reaches a terminal state, the same way
  v0.10's, v0.11's and v0.12's rows were resettled after their own histories
  closed out.
- **`selfdev-v013-a-broken-contract`'s row is now settled from the run's own
  terminal record** (task 010, `selfdev-v014-hands-on-the-plan`'s own wave):
  the run ended `succeeded` / `verified`, `iterationsUsed: 145`,
  `usage.costDerivedUSD` rendering `~$111.57 derived`, and a plan tally of 11
  of approach 3's 12 tasks `validated` — the 12th, task 012 ("re-verify the
  docs- and report-reading guards at the true final sha, by discovery not by
  a hand list"), reads `failed` / `validation-exhausted`: its own attempt
  re-verified the guard subset with a hand-typed module list, the same shape
  requirement B of `selfdev-v014-hands-on-the-plan` replaced with grep
  discovery precisely because a hand list can silently omit a module. This
  run's own `status.json`/`tasks.json` are gone from this sandbox by the
  time this settle task ran; the figures above are this wave's own PRD
  record of that run's outcome (`docs/prds/v0.14-every-hand-on-the-plan.md`,
  half 9), not a fresh read repeated here. The token cell (`~230M`) is
  therefore an estimate, not a recorded counter: the last *read* figure on
  record for this run was 186.7M at `iterationsUsed: 133` (the bullet
  above); this settle scales that reading by the same ratio the settled
  cost grew over the settled iteration count (145/133 and $111.57/$86.79)
  to a round figure, and is marked `~` for that reason — a later hand with
  the real terminal `status.json` in hand should replace it with the
  recorded counter rather than trust this estimate.
- **`selfdev-v014-hands-on-the-plan`'s row is now settled from a terminal
  state** (task 011, this wave). `/run/ralphd/../selfdev-v014-hands-on-the-plan/status.json`
  is **not reachable from this container** — this sandbox only has `/run/ralphd`
  for the run driving it (this wave, `selfdev-v0141-a-guard-that-cannot-see`) —
  so the source for every figure in the row is the tracked v0.14 record plus
  this PRD's (`/run/ralphd/prd.md`, requirement D) own stated figures, named
  here rather than presented as a fresh read: `failed` / `unverified`, ended
  `2026-08-30T10:05:44Z`, `171` iterations, `~468.6M` tokens, `~$240.95 derived`.
  Approach 1 landed 22 of its own 35-task plan to `validated`, then task 016
  (requirement P) exhausted its validation ladder and shipped `failed`: the
  closing sequence had been built as a twelve-deep dependency chain
  (`024→025→…→035`), so one `failed` task made every remaining issue closure,
  doc update, sweep and the closing report itself permanently unreachable in
  one stroke — the exact #99 doom scenario this same wave's own requirements
  C/D ship the fix for, reproduced by the run driving them (its engine was
  built from the wheel before those fixes had landed in this tree, archived
  `approaches/01`). Approach 2 ran its own 18-task plan to 17 `validated`,
  then task 018 (the guard-subset-at-the-true-final-sha closing task)
  `failed` — the same no-fixed-point trap requirement B of this wave now
  fixes (archived `approaches/02`). Approach 3 (the repair pass, this run's
  final record) again ran 17 of its own 18 tasks to `validated`; the 18th,
  task 017, reads `failed` / `validation-exhausted` — its own
  closing report (`artifacts/reports/v0.14-a3-closing-report.md`), which
  review rejected for a self-contradictory account of task 018 (pending in
  four of its own sections, completed in a fifth amended later) and a
  notification inventory missing six of the wave's own sends. This row now
  carries **no** `+` snapshot marker on any cell, per this task's own success
  criteria — the run reached a terminal `state` and these are its settled
  figures, not a floor.
- **This PRD's own row (`selfdev-v0141-a-guard-that-cannot-see`) is a live
  snapshot of the run writing it** (task 011, itself), read fresh from this
  run's own `/run/ralphd/status.json` and `/run/ralphd/tasks.json`:
  `state: running`, `approach: 1`, a flat 20-task plan (no `dependsOn` on any
  task — the shape approach 3 of the predecessor wave proved out),
  `iterationsUsed: 31`, `usage.totalTokens: 23852436` (23.9M),
  `usage.costDerivedUSD: 17.970032` (`costStatus: derived`, so
  `engine.state.format_cost` renders `~$17.97 derived`). A fresh tally of
  `tasks.json` at this read: 7 `validated`, 1 `in-progress` (this
  row-settling task, 011, itself), 9 `pending`, 2 `failed` (003, 009) and 1
  `skipped` (002) — the plan is flat, so no other task's `dependsOn` names
  any of the three, and nothing is blocked. This row carries the `+`
  snapshot marker on every count cell, per this task's own success
  criteria — the run has not reached a terminal `state`, so a later task
  will need to resettle it once this run actually ends.
- Some run dirs also hold a `composite-prd.md`. That one is **engine-generated**
  (the PRD with approach history appended for later approaches), not an authored
  input, so it is not kept here.

