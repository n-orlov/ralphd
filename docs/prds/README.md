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

| Run | File | Outcome | Iterations | Tokens | Cost |
|---|---|---|---|---|---|
| `selfdev-vigilant-4` | [selfdev-vigilant-4.md](selfdev-vigilant-4.md) | succeeded / verified | 13 | 2.1M | $3.39 |
| `selfdev-roadmap-1` | [selfdev-roadmap-1.md](selfdev-roadmap-1.md) | **aborted** / unverified | 1 | 1.2M | $2.22 |
| `selfdev-roadmap-2` | [selfdev-roadmap-2.md](selfdev-roadmap-2.md) | succeeded / verified | 148 | 219.3M | $91.66 |
| `selfdev-roadmap-3` | [selfdev-roadmap-3.md](selfdev-roadmap-3.md) | succeeded / verified | 41 | 43.7M | $21.07 |
| `selfdev-roadmap-4` | [selfdev-roadmap-4.md](selfdev-roadmap-4.md) | succeeded / verified | 43 | 27.6M | $17.35 |
| `selfdev-v05-resilience` | [v0.5-resilience.md](v0.5-resilience.md) | succeeded / verified | 132 | 163.9M | unpriced route |
| `selfdev-v06-release` | [v0.6-first-release.md](v0.6-first-release.md) | succeeded / verified — 54 of 55 tasks completed, 1 skipped | 145 | 332.1M | unavailable |
| `selfdev-v07-unattended` | [v0.7-trustworthy-unattended.md](v0.7-trustworthy-unattended.md) | still running / 44+ of 47 tasks completed | 100+ | 239.1M+ | ~$279.16+ derived |
| `selfdev-v08-auditable` | [v0.8-auditable-interruptible.md](v0.8-auditable-interruptible.md) | still running / 0+ of 36 tasks completed | 2+ | 1.2M+ | ~$2.27+ derived |

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
- **`v0.7-trustworthy-unattended.md`'s row is that run's own snapshot of
  itself**, read from its `status.json` by the iteration that wrote the row —
  hence the `+` on every count. At that moment: `state: running`, no verdict
  yet, on approach 1 of a possible 10 (it never replanned), 44 of its 47 planned
  tasks `completed`. The three still open are the tail a run cannot do for
  itself before it has done it: this very row, the roadmap bullet that waits on
  the issue closures, and the final green sweep of the test suite. All 24 issues
  in the brief (#28, #30–#49, #52, #53, #55) are already closed on GitHub with a
  closing comment (`artifacts/reports/issue-closure.md`, traced per issue in
  `artifacts/reports/issue-traceability.md`).
  The iteration cell is the 100 the engine charged against a 500 budget, not the
  107 raw iteration slots the log holds: 12 attempts were classified
  infrastructure faults, which are retried and refunded rather than charged
  (`iterationsUsed`). **That 100 is itself inflated**, by the same issue #32 this
  wave fixes — the run is driven by an engine image built before the fix, so its
  one resume restarted the refund counters at zero: 12 attempts faulted on
  infrastructure and only the 7 after the resume survived as credit, so the
  guarantee implies 95 charged, not 100. The cell reports what the engine wrote,
  for the same reason v0.6's does. That resume is itself this wave's subject
  matter: the engine died mid-iteration 38 on issue #28 (an iteration that
  overran its 45-minute cap took the supervisor down with it), and the crashed
  attempt's transcript is gone because the resumed attempt reused the slot —
  issue #44, also fixed here, but not in the image that was running.
  The cost cell is `derived`, not quoted: the gateway reported `costUSD: 0`
  beside 239M billed tokens, which the shipped classifier calls unknown (#14,
  v0.6), and the host-side AWS Bedrock price table derives ≈ $279 from the same
  recorded counters. It is an estimate at that table's rates, not money any
  provider quoted, and — like the counts beside it — a floor.
- Some run dirs also hold a `composite-prd.md`. That one is **engine-generated**
  (the PRD with approach history appended for later approaches), not an authored
  input, so it is not kept here.
