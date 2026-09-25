# Review economics: the reusable contract fragment

Review has, historically, been strict and correct — and expensive: a run can
spend a large share of its budget re-litigating something no future approach
can change, or grinding an environmentally impossible requirement to
exhaustion before anyone gets to say so. This fragment is the fix, packaged so
a future PRD can *include it by reference* instead of retyping it: a
materiality rubric that keeps the judge exactly as strict while making the
verdict track what actually matters, a petition protocol for the run to say
"this obligation cannot be met" with evidence, and a late-cure rule so a
missed time-anchored duty is repaired rather than fatal.

**First instance:** this text originates as the inline contract-side
prototype in `docs/prds/v0.15-impossible-is-an-answer.md` ("The contract-side
prototype — this wave's own protection" and requirements A/B/C). This
document canonicalizes that prototype so later PRDs can cite it directly
instead of copying its prose. Read the PRD for that wave's full account,
including the engine-side mechanisms (structured findings with `severity` and
`curable`, the `rejected_incurable` pause/escalation edge, and run-scoped
petition adjudication) this fragment's *contract* text was prototyped
alongside.

**Second instance:** `docs/prds/v0.16-a-petition-is-not-a-postmortem.md`
cites this document by reference for its rubric, petition protocol and
late-cure rule (its own `## The contract` section says so explicitly), and
is the wave that adds the mid-run adjudication and criteria-amendment
mechanisms documented below — moving *when* the judge speaks (immediately
on a filed declaration, not only at approach-end) without loosening *how
strictly* it speaks.

## Materiality rubric

Review computes its verdict from **blocking** findings only; **residual**
findings are recorded and surfaced but never reject. A contract that supplies
no rubric of its own defaults to **this section's own boundary** — the
engine's `findings.DEFAULT_RUBRIC` (task 004, requirement A.2, #139) — never
to *every finding blocking*; the rubric never weakens a contract, it only
lets one narrow the boundary explicitly (never widen it past all-blocking).

**Blocking** — any of: a deliverable the contract requires is absent or
wrong; a test the contract names is missing, red or inverted (asserts the
opposite of the behaviour it names); a red test at the shipped sha;
`SPEC.md` or the user docs contradict the live code; an unmet requirement of
the contract; a secret exposed.

**Residual** — everything else, including: an inaccuracy in a record,
report, commit message, note or any other prose that is not the spec or the
user docs (a miscount, a stale sha, a wrong commit cited); imprecision or
infelicity in prose; a stale snapshot; a missing cross-reference;
formatting; a notification gap that has been cured under the late-cure rule
below. Residual findings are recorded and surfaced, and do not reject.

The boundary above is this fragment's own default text and the **engine's**
own fallback: a citing PRD may restate or narrow it, but the recomputed
verdict is always over blocking findings only, and a contract that states no
rubric at all — inline or by reference — still gets this boundary, never
"every finding blocking".

## Petition protocol

If a run believes a contract obligation is unachievable, it may file an
**impossibility petition**: one petition per obligation, with a type
(`environmental | temporal | contradictory-clauses | external`) and evidence.
See [`petition-format.md`](petition-format.md) for where the filing lands
(task 065, requirement N.6, #150: the run dir, not a workspace file). Review
adjudicates every open petition as part of its verdict:

- **CONFIRMED** converts that obligation to a disclosed residual for the
  remainder of the run — every approach, not just the current one.
- **REFUTED** must state concretely what would satisfy the obligation, as
  WORK whose completion is checkable: a `substance` remedy becomes one cure
  task, a `form` remedy (one that changes no deliverable) becomes a debt row.

**Once per obligation.** Re-filing a petition against an obligation that
already has one — whether or not the first was adjudicated, and including a
petition already adjudicated REFUTED — is itself a blocking finding, not a
second attempt at the same claim. The full file format (required fields,
where the file lives, the four types) is spelled out in
[`petition-format.md`](petition-format.md).

**Five types, and the table governs.** The two inline type lists in this
section are shorthand. The normative list is the five-type table in
[`petition-format.md`](petition-format.md):
`environmental | temporal | contradictory-clauses | external |
self-referential`, the fifth covering an obligation whose own text makes it
unmeetable as written (a criterion verifiable only from a state its own
satisfaction would have to create, or one that names its own not-yet-existing
sha). Where a shorthand count in this section and that table disagree, the
table governs: there are five types, and no wave narrows the list to four.

## Mid-run adjudication

An impossibility declaration does not have to wait for approach-end review.
The moment a worker names an obligation, or its own task's criterion clause,
as unachievable — with a type and evidence, filed through the same
once-per-obligation channel as any other petition — the engine schedules one
**bounded adjudication episode** with the **review-hat model** (the model
`config` resolves for the review hat, never the worker's own model), scoped
to that petition and to any other still-open ones, and runs it before the
affected task's next validation attempt. The verdict uses the same
vocabulary as an approach-end adjudication:

- **CONFIRMED** converts the obligation into a disclosed residual for the
  remainder of the run — persisted at run scope exactly as an approach-end
  CONFIRMED is, so no later approach re-litigates it.
- **REFUTED** states concretely what would satisfy the obligation, and the
  affected task's validation ladder keeps running rather than stopping. A
  `substance` remedy also becomes one cure task on the same path a curable
  rejection's cure task takes, spending no cure-pass budget of its own.

A task whose validation ladder exhausts while an obligation is still open
does not fall straight to a bare `failed`. Its last-attempt handoff becomes
the petition body, the task moves to a **`petitioned`** status, and that
status is itself adjudicated — by a still-scheduled mid-run episode if one
is pending, otherwise by the next approach-end review. `petitioned` is a
waypoint toward an adjudicated outcome, not a verdict in its own right: a
CONFIRMED `petitioned` task counts as accepted-with-residual like any other
CONFIRMED obligation, and a REFUTED one becomes `failed` with the
refutation recorded — and, for a `substance` remedy, with one cure task
built from that refutation inheriting the failed task's own dependents, so
the refutation leaves work rather than a doomed remainder. A contract that predates this mechanism, or declares no
petition protocol at all, keeps the plain `failed` routing it always had —
this section adds a path, it narrows none.

## Criteria amendment

A CONFIRMED adjudication — mid-run or approach-end — whose petition names a
task's own `successCriteria` (never the PRD, never a Definition-of-done
item, never another task, and never anything under `/config`) may carry a
replacement criteria text for that **one** task, authored by the
adjudicating judge and by no one else: not the worker who filed the
petition, not the planner who wrote the original text, not the run acting
on its own initiative. Applying it is recorded, never silent: a
`task.criteria_amended` event captures the old text, the new text and the
adjudication that authorized it (its sibling `task.criteria_repaired`, the
other engine-authored criteria rewrite, is the lexical-lint repair described
under *Ingestion lints are lexical by design* below); the task's
`criteriaFingerprint` is recomputed over the new text; and the amendment
plus its authorizing adjudication are disclosed in the closing report.
Everything else about the task — its status, its other fields — is untouched
by the amendment itself.
An edit to `successCriteria` that does not arrive from an adjudicating judge
through this path — a worker's own rewrite, a planner's revision outside
plan-ingestion, anything that targets a task other than the one the
petition named — is refused and is itself a blocking finding, the same
tamper-evidence treatment already applied to adjudication records.

## Late-cure rule

Any time-anchored duty in a contract (a send required "the moment it
happens", a record required "at the time") that is missed is **cured** by a
late entry explicitly labelled late, plus a residual line in the closing
report naming the miss. Once cured, it is discharged and is not grounds for
rejecting this or any later approach. Late and labelled beats never, and
never beats grinding an approach to rejection over it.

## Using this fragment from a PRD

A PRD that wants this contract fragment includes it by reference — see the
note in [`prds/README.md`](prds/README.md) — rather than retyping the rubric,
protocol and rule above. **Three adoption forms work**, resolved by
`findings.resolve_materiality_rubric` in this precedence order (task 002/003,
requirement A.1/A.3, #110):

1. an **inline** `## Materiality rubric` heading in the PRD's own contract
   text — always wins, even when either reference form below is also
   present;
2. a **prose reference line**, e.g. "the materiality rubric … of
   `docs/review-economics.md` applies" (case-insensitive, one line, a
   backticked path);
3. an explicit **HTML-comment marker**, `<!-- rubric: docs/review-economics.md -->`.

State plainly that the rubric/protocol/rule are inherited from this document
(via one of the two reference forms above, or by restating the section
inline), and note any narrowing the PRD makes to the default (never a
widening past all-blocking, and never a version of the petition or late-cure
text that drops the "once per obligation" or "cured, not waived"
guarantees). **All-blocking is no longer the fallback.** A contract that
adopts this fragment by any of the three forms above, or that names no
rubric at all, is handed the same product-blocking/residual boundary either
way — the engine's own default (`findings.DEFAULT_RUBRIC`, task 004,
requirement A.2, #139) — never the old collapse-everything-to-blocking
placeholder a pre-0.18 engine used.

## v0.18 addendum (#139–#147)

[`prds/v0.18-the-run-can-finish.md`](prds/v0.18-the-run-can-finish.md) bans a
set of success-criterion and record shapes that sank earlier runs on
paperwork rather than product defects (its "What this PRD deliberately does
not ask" section, and requirement E's lint that refuses them at plan-ingestion
time). Restated here so a later PRD adopting this fragment inherits the list
without retyping it:

- **No "last commit" rule.** Nothing may require being "the last commit".
  The final guard subset runs **at the final code sha** — the last commit
  that touches anything outside `artifacts/reports/`, `docs/roadmap.md` and
  `docs/prds/README.md` — never "the last commit"; a report-only commit may
  follow it freely.
- **No record about its own future.** No report, record or criterion states
  what a later event will be, or must record events after the sha it names
  ("none newly filed", "every petition and its adjudication", "the terminal
  send", a later commit's sha).
- **No per-task notification ledger.** Sends are best-effort and advisory;
  no criterion mentions a send, a ledger entry or an HTTP status.
- **No sha-scoped-count rule.** A count names the sha it was computed at
  when that is cheap; a missing sha is residual, never blocking.
- **No sweep-sha declaration rule beyond the existing guard.** A record
  declares `sweep-sha` exactly as `docs/sweep-records.md` already requires;
  its own rules (report-only gap allowed, discharge pairs) are the whole
  obligation.
- **No audit range, no "commit exactly once", no protected-path clause.**
- **No self-referential criterion** — a task whose criterion names the sha
  of a later commit, or a row that must cite the commit writing it (the
  fifth petition type above covers the case where a criterion is already
  written this way).

## Ingestion lints are lexical by design

The quantifier lint and the clause-count lint the v0.18 addendum above
names (`_unenumerated_quantifier_clause`, `_clause_count_overrun` in
`src/ralphd/engine/plan_ingestion.py`) are **lexical** — pattern and
clause-split matches over the criteria text, never a semantic read of what
the task actually means. That is deliberate, not a shortcut: a lint that
ran at plan-ingestion time has no source tree, no test run and no worker
report to check meaning against yet — the proposal is the only thing that
exists — so a semantic judgment is not available to it at all, only a
text-shaped one. Staying lexical also keeps the lint auditable and stable
across runs: the same criteria text always yields the same verdict, with no
model call and no drift between two runs of the identical proposal.

A lexical lint over-refuses at the margins on purpose — v0.20's real
casualties (`docs/prds/v0.21-the-budget-belongs-to-the-work.md` requirement
B.2, #135) are proof it does — and the fix for that is a **named escape
with a stated limit**, never a semantic read. Task 006 (requirement B.2)
adds exactly one such escape to the quantifier lint, the
**elsewhere-bounded-quantifier** rule: a clause whose universal quantifier
points BACKWARD with an explicit anaphor ("each of
THEM", "all of THOSE", "every one of THESE", "the above", "the
aforementioned", "such") is accepted when some OTHER clause of the SAME
task's criteria is itself bounded on its own merits (a named defining
command/file, or an explicit enumeration) —
`_is_anaphorically_bound_elsewhere` in `plan_ingestion.py`. The limit is
narrow and stays lexical: a
bare unenumerated noun phrase with no anaphor ("each shipped piece") is
unaffected by this escape and stays refused even when some unrelated
clause elsewhere in the same criteria happens to be bounded — the anaphor,
matched the same way every other cue in this module is matched, is what
proves the referent is the SAME already-bounded set rather than a
coincidental match somewhere else in the string. The second half of the
limit is the lookup's reach: it never leaves the one task's own criteria
string — the quantifier lint runs per task, so a bound sitting in some
OTHER task's criteria rescues nothing.

`tests/test_criteria_bound_lint.py` covers one clause the rule now accepts
beside one clause in the same criteria that is still refused:
`test_an_anaphoric_quantifier_bound_by_an_earlier_clause_is_accepted` and
`test_an_anaphoric_bound_elsewhere_does_not_rescue_an_unrelated_bare_quantifier`.

Escape #2, in the same register (#209 defect 1): a lexical lint that has
already computed a **lossless, lint-clean rewrite** of the text it objects to
now **applies** that rewrite and records it — `plan_ingestion.repair_criteria`,
a pre-pass over the proposal, and a `task.criteria_repaired` event carrying the
old text, the new text, the lint names applied and the writer (`source:
"plan-ingestion"` at plan ingestion, `"cure-insertion"` when the text is a
finding's `cured_when` becoming a cure task's criteria) — instead of refusing
the whole proposal and re-asking the hat for advice the harness could already
act on. The measured cost of not doing this: one run burnt planning's entire
three-attempt ladder on the clause-count lint, hit it again after replanning,
and spent ten planning phases to produce three approaches. The first stated
limit is the two fallback constants each rewrite ladder ends on: those are
generic **advice** for a human reader and one of them replaces the whole
criterion, so neither is ever applied — a rewrite that equals either one is
not a rewrite, and the plain refusal stands. So the refusal survives
everywhere no legal rewrite exists, which is exactly where a lexical lint is
not over-refusing at all. The second is the quantifier half's own bound
phrase, which names `pytest`: that is why the cure-insertion writer admits
only `clause-count-overrun` (the only lint with jurisdiction over a
`cured_when` anyway), while plan ingestion — where the text is a task's own
`successCriteria` and the alternative is refusing the whole plan — admits
both, so a planner working a non-Python checkout can be handed a `pytest`
bound it did not write and would have been re-asked for before. This escape
is deliberately not extended to a worker's `proposed_tasks`
batch, whose lint has no event sink in reach: repairing there would be a silent
harness rewrite, and silence is what the criteria-amendment rule above forbids.

## Why inspection is not evidence

[`prds/v0.20.1-inspection-is-not-evidence.md`](prds/v0.20.1-inspection-is-not-evidence.md) is the
wave that supplies this document's namesake case, and the reason the rubric above insists on a
red test or an unmet requirement rather than an inspector's belief that one holds. Across v0.20,
six reviews passed requirements A–L by source inspection alone — the review hat's own recorded
words were *"Source inspection found …"* and *"covered by the green fast rerun"* — while four
requirements were in fact unmet in the shipped code: the approach progress baseline did not
survive a resume (v0.20 B.3, gate finding VG-001), the absorption-forced verify was not actually
gone (v0.20 A.3, VG-002), no cure was scheduled before the freeze by the scheduler (v0.20 E.4,
VG-003), and profile-provided headers were not visible on the run surfaces (v0.20 J.3–J.4,
VG-004). The peer verdict gate, run against that same tip and that same shipped source in a
disposable clone (the protocol named in that PRD's requirement G.2), wrote four probe tests
directly from the requirement text and returned four blocking findings — VG-001, VG-002, VG-003
and VG-004 — in 0.41 seconds. Six rounds of inspection found none of the four gaps that four
tests found in under half a second; that is the economics this document exists to correct.

## Provenance

This document's rubric, petition protocol and late-cure rule are the
contract-side prototype first written for
[`prds/v0.15-impossible-is-an-answer.md`](prds/v0.15-impossible-is-an-answer.md)
(requirements A, B, C and D of that PRD); see that PRD for the engine-side
mechanisms A/B/C built alongside it, and
[`petition-format.md`](petition-format.md) for the petition file format this
protocol depends on.
