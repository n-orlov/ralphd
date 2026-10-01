# Writing a PRD for a ralphd run

A PRD is the contract review enforces **to the letter**. Everything a PRD asks
for becomes tasks, commits, verify calls and review checks — and anything
review can check, it can reject. So every clause has a price, and a clause
that does not move the product forward is pure cost with a rejection attached.

## The only two things a PRD asks for

1. **The product's main spec is reasonably complete and does not contradict
   the live code.** One spec document (e.g. `SPEC.md`), present tense, updated
   in the same commits as the behaviour it describes.
2. **The live code works, proven by comprehensive tests** that are collected
   by the suite and run in CI.

That is the whole deliverable. A requirement that serves neither is not a
requirement — cut it before launch. Write each requirement as **a behaviour
plus the test that proves it**: the observable (a value, an event, an exit
code, a file the product writes), and "a test exists that fails if this
behaviour is absent". Then the implementation task, verify and review all
check the same thing.

## Do not ask for paperwork

Paperwork is the dominant waste on long runs. A run that is asked to write
records writes many of them, each one full of shas and counts, and review then
cross-checks every one — one wrong word ("five" for "six" commits) blocks the
run and costs a cure pass plus a full CI re-sweep. Do not create the surface.

- **No report, record, close-out, audit, evidence or "re-take" documents**
  in the repository. No "record the number in …", no "cite the commits that
  …", no tables of shas or test counts in tracked prose.
- **Run evidence belongs to the run, not the repo.** Logs, sweep results,
  measurements and reports go to the run dir's artifacts (`/run/ralphd/
  artifacts` inside the job); they are never committed. CI evidence is the CI
  run itself, found by its head sha.
- **Closing an issue is a comment** naming the commit and the test node — not
  a tracked file describing the closure.
- **No release narrative** in docs: no per-wave "shipped sha" lines, no
  history-of-this-feature paragraphs. `git log` is the history. A roadmap, if
  the project keeps one, is what is planned plus one line per shipped version.
- **Do not ask for tests that assert prose.** A test that pins the wording of
  a roadmap, a report or a record turns every docs edit into a red suite. Tests
  prove the code. If such tests already exist, the PRD should allow retiring
  them explicitly — a blanket "no test is ever retired" clause keeps them alive
  forever.
- **Do not ask for bookkeeping tasks** (per-group audit tasks, closure-record
  tasks, a freeze tail of record-only commits). Put "a test that fails without
  the behaviour" in the implementation requirement itself.

## Materiality rubric — paste it inline, keep it to substance

Without a rubric review treats every finding as blocking. Put it in the PRD
as its own `## Materiality rubric` heading (by-reference adoption is not
honoured). Start from this and do not add paperwork grounds to it:

```markdown
## Materiality rubric

Reject only on substance:

- a requirement's behaviour is absent or wrong in the live code;
- a test the requirement names is missing, red, or asserts the opposite of
  the requirement;
- the spec contradicts the live code;
- a secret is exposed.

Everything else verifies, with the gap recorded as a residual note: wording,
form, provenance, process, and **any number or claim in prose outside the
spec** (commit messages, notes, issue comments, run artifacts). Prose outside
the spec is never a blocking ground.
```

Never write "a factual claim in a tracked record is false" as a blocking
ground: it makes every sentence the run writes a way to fail.

## Ending the run

The sweep is the plan's last task, and nothing of any kind is committed after
it: no CI-run report, record, close-out, notes file or doc touch-up.
`origin/main`'s head when the run ends is the swept sha. Issue closure is a
comment, not a tracked file. A stale sweep is never process: if `origin/main`
moved past the swept sha, the sweep is stale and must re-run before the plan
can be called done. Since 0.29 the plan marks the sweep task `sweep: true`,
so a cure pass re-opens it instead of leaving a stale sweep behind.

- **One sweep, at the end, and nothing committed after it.** If the PRD needs
  CI green, say "all lanes green at the final pushed sha" and make the sweep
  the last task. A clause that demands a record of the sweep *in the repo*
  creates a commit that makes the sweep stale — a loop with no exit.
- **DO NOT demand self-referential finals.** "The final record must cite the
  commits that made the final record" and "the guard must be green at HEAD as
  the last commit, recorded in a file" are fixed-point chases: every cure
  creates a new HEAD to cure.

## The standing rules block

Put these in the PRD's own `## Standing rules` block, verbatim, never only in
a task's own criteria:

- the test recipes, given verbatim, with their parallelism;
- a rule that only the final sweep runs a whole tier, and no other task runs
  one;
- commits are made per task and pushed;
- the sweep's execution shape — what it runs and in what order — belongs here,
  never inside a task's criteria;
- how to span a wait longer than ~85% of the iteration cap, using `deferred`;
- the CI dispatch recipe, so the run can close its own CI loop without an
  operator's help.

## The rubric's working clauses

Beyond the materiality rubric's four substance grounds, these working clauses
govern how review applies it:

- file the rule, not the example — a finding names the general rule a bullet
  stands for, not just the one instance it noticed;
- read the direction of the evidence — a log line or event that could support
  either side of a finding is not evidence for either side;
- a review after a cure pass confirms the cure landed, and does not re-audit
  the whole product from scratch;
- judge a hat or router by its contract, not its taste — a stylistic
  preference is not a materiality-rubric ground;
- a stale sweep is never process — a sweep that ran before the final commit
  landed does not satisfy a "green at HEAD" requirement.

## Non-negotiables

- **No weakened test.** A test pinning behaviour this wave changes is
  rewritten in the same commit, naming the covering node it is rewritten to;
  nothing else about the suite is retired.
- **No raised timeout, budget, worker count or per-test bound.**
- **No new required contract field.** On a self-development run, the
  installed engine's own judge rejects a verdict or report carrying one
  anyway.

## Issues in scope

Name the issues the PRD covers. The run reads each named issue in full, and
the PRD wins wherever the PRD and an issue's own text differ. Check symbols
(functions, event names, fields) against a named sha, and treat drift from
that sha as expected, not as a finding.

## Budget and shape

Pre-split each large requirement into named tasks at plan time, rather than
leaving a single oversized task for a worker to split later. Update the spec
inside each task that changes behaviour — never in a separate docs-only task.
State the iteration budget, the approach budget, the wall-clock window and
the per-iteration cap, and the peer gate settings, so the run knows its own
budget without guessing.

## Clauses that make acceptance unreachable — DO NOT

Each of these has burned whole approach budgets on real runs:

- **DO NOT anchor an audit clause on a range the run cannot make clean.** A
  clause like "the audit range `<pre-prd-sha>..HEAD` must show **zero**
  commits touching protected paths" is unsatisfiable the moment the PRD-cut
  commit itself sits inside that range — a forward-only run can never empty
  history, review marks the finding `curable: false`, and every fresh approach
  dies on the same rock. Anchor guards at the PRD commit itself
  (`<prd-sha>..HEAD`) or exempt named shas inside the clause's own command.
- **DO NOT rely on nearby prose to soften a literal command.** A carve-out
  sentence next to the clause ("operator commits are not this run's problem")
  does not override the mandated command's literal output — review enforces
  the command. If an exemption matters, put it in the check itself.
- **DO NOT time-anchor an obligation without a cure path.** "Send it the
  moment it happens" plus "no reconciliation" turns one missed send into a
  permanent rejection of every later approach. Write the late-cure into the
  PRD and assume the operator was asleep when the moment passed.
- **DO NOT pin exact line numbers as criteria.** Name the function or the
  behaviour; say "if a reference has drifted, find the current site and carry
  on" so drift is not a finding.
- **Put authority in the PRD, not in a later ruling.** An amendment keyed to a
  task id is orphaned when a replan renumbers the tasks; a decision that must
  survive the whole run belongs in the PRD text.
- **DO NOT write a requirement that says "any"/"every" over an open domain and
  leans on a heuristic to enforce it.** "Every session settles to its expected
  state" or "any queue converges" sounds finite but is not: a heuristic check
  (a clause comparison, a text match, a re-park rule) always has one more
  counterexample it did not anticipate, so review keeps building a fresh one
  every cure pass — each one fixed, none of them the last one. A requirement
  shaped like "R150 Success: every session's queue settles
  to the expected state" (no product named — the shape is the lesson, not the
  session or the queue) will come back in cure passes 1, 2 and 3 with a new
  counterexample each time, because the domain it quantifies over was never
  closed. Enumerate the finite set of cases the rule must actually cover —
  the concrete states, transitions or shapes — and require each one by name,
  instead of asking a heuristic to hold for "any"/"every" member of a domain
  nothing closes.

## When the product is ralphd itself

A self-development run is driven by the **installed** engine, not the code it
writes. Say so in the PRD, and:

- do not ask the run to prove its own fixes "live" in its own behaviour —
  prove them with tests;
- keep lessons about *the harness* out of the product PRD. A harness defect
  the run hits is a filed issue, not a new requirement or a record for the run
  to maintain;
- the two-things rule applies unchanged: the spec matches the code, and the
  tests prove the code. Engine incidents are not a reason to add paperwork.
- **a hat prompt is product behaviour**, proven by a test on the prompt built
  for the case — the same discipline as any other behaviour, not an
  exception for prompts;
- the standing rules tell the run how to avoid, by hand, the known defects of
  the engine it runs on, since the run cannot patch the engine that is
  running it.

## Before launch

- [ ] Every requirement is a behaviour plus the test that proves it.
- [ ] One requirement keeps the spec in step with the behaviour changed.
- [ ] No clause asks for a tracked report, record, count or sha narrative.
- [ ] `## Materiality rubric` is inline and has no paperwork ground.
- [ ] Definition of Done = behaviours shipped, tests green in CI at the final
      sha, spec consistent, no secret exposed. Nothing else.
- [ ] Out of scope is stated, and there is an escape hatch (how the run
      petitions a requirement it proves contradictory or impossible).
- [ ] Ending the run: the sweep is the last task, nothing is committed after
      it, `origin/main`'s head is the swept sha, issue closure is a comment,
      a stale sweep is never process, and the sweep task is marked
      `sweep: true`.
- [ ] The Standing rules block gives the test recipes verbatim, the
      only-the-sweep-runs-a-tier rule, the sweep's execution shape, the
      deferred-wait rule and the CI dispatch recipe.
- [ ] The rubric's working clauses are stated: file the rule not the
      example, read the direction of the evidence, a cure-pass review
      confirms rather than re-audits, judge a hat or router by its contract,
      and a stale sweep is never process.
- [ ] Non-negotiables are stated: no weakened test, no raised timeout or
      bound, no new required contract field.
- [ ] Issues in scope are named, read in full, and checked against a named
      sha with drift from it treated as expected.
- [ ] Large requirements are pre-split into named tasks at plan time, with
      the iteration/approach/window/cap and peer gate settings stated.
- [ ] Self-development: hat prompts are proven by tests on the prompt built
      for the case, and the standing rules name the engine defects to avoid
      by hand.
