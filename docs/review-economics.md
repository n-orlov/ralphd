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

## Materiality rubric

Review computes its verdict from **blocking** findings only; **residual**
findings are recorded and surfaced but never reject. A contract that supplies
no rubric of its own defaults to *every finding blocking* — the rubric never
weakens a contract, it only lets one loosen the boundary explicitly.

**Blocking** — any of: a red test at the shipped sha; an unmet requirement of
the contract; a false claim in a tracked artifact that changes what a reader
believes landed or its result (a wrong commit attributed for a change that
shipped differently than described, a test result misstated, an issue state
misreported, an invented notification); a missing required declaration (e.g.
a sweep's `sweep-sha`); an issue closed without evidence; a secret exposed; a
stated Definition-of-done predicate evaluating false.

**Residual** — everything else, including: imprecision or infelicity in
prose; a stale snapshot that is labelled as a snapshot; a missing
cross-reference; formatting; an inaccuracy in a record *about another
record* whose correction would not change what a reader believes shipped; a
notification gap that has been cured under the late-cure rule below.
Residual findings are recorded, must be listed in the closing report, and do
not reject.

The boundary above is this fragment's own default text; a citing PRD may
restate or narrow it (never widen it past all-blocking), but the recomputed
verdict is always over blocking findings only, and "every finding blocking"
is the fallback when a contract states no rubric at all.

## Petition protocol

If a run believes a contract obligation is unachievable, it may file an
**impossibility petition**: one petition per obligation, with a type
(`environmental | temporal | contradictory-clauses | external`) and evidence,
in that wave's own `artifacts/reports/<wave>-petitions.md`. Review adjudicates
every open petition as part of its verdict:

- **CONFIRMED** converts that obligation to a disclosed residual for the
  remainder of the run — every approach, not just the current one.
- **REFUTED** must state concretely what would satisfy the obligation.

**Once per obligation.** Re-filing a petition against an obligation that
already has one — whether or not the first was adjudicated, and including a
petition already adjudicated REFUTED — is itself a blocking finding, not a
second attempt at the same claim. The full file format (required fields,
where the file lives, the four types) is spelled out in
[`petition-format.md`](petition-format.md).

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
protocol and rule above. State plainly that the rubric/protocol/rule are
inherited from this document, and note any narrowing the PRD makes to the
default (never a widening past all-blocking, and never a version of the
petition or late-cure text that drops the "once per obligation" or "cured,
not waived" guarantees).

## Provenance

This document's rubric, petition protocol and late-cure rule are the
contract-side prototype first written for
[`prds/v0.15-impossible-is-an-answer.md`](prds/v0.15-impossible-is-an-answer.md)
(requirements A, B, C and D of that PRD); see that PRD for the engine-side
mechanisms A/B/C built alongside it, and
[`petition-format.md`](petition-format.md) for the petition file format this
protocol depends on.
