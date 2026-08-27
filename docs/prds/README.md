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
| `selfdev-v012-green-on-a-real-host` | [v0.12-green-on-a-real-host.md](v0.12-green-on-a-real-host.md) | not started | — | — | — |

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
- Some run dirs also hold a `composite-prd.md`. That one is **engine-generated**
  (the PRD with approach history appended for later approaches), not an authored
  input, so it is not kept here.

