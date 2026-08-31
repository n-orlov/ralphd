# Impossibility petition format

An **impossibility petition** is how a run tells review that one of its own
contract obligations cannot be satisfied — instead of grinding attempts
against something arithmetically certain to stay red. This is requirement C
(#112) of `docs/prds/v0.15-impossible-is-an-answer.md`; the reusable rubric
this format is part of lands as `docs/review-economics.md` (requirement D of
the same PRD).

## Where the file lives

One file per wave: `artifacts/reports/<wave>-petitions.md` (for example
`artifacts/reports/v0.15-petitions.md`). It is tracked alongside the wave's
other evidence reports, never in `docs/` and never per-task. If a wave needs
no petition, the file still exists and says so explicitly — silence is not
evidence that nothing was unachievable, and a missing file cannot be
distinguished from a forgotten one.

## One petition per obligation

Each petition is its own section in that file, headed by the obligation it
names — a PRD requirement letter or a Definition-of-done item, quoted or
referenced precisely enough that a reader can find the exact clause without
guessing. A petition may be filed **once per obligation**. Filing a second
petition against an obligation that already has one — regardless of whether
the first was adjudicated yet — is a duplicate, and a duplicate is a
**blocking finding** in review's verdict, not a residual one. The same rule
covers re-filing an obligation whose petition was already **REFUTED**: the
run does not get a second attempt at the same claim without new obligation
text to attach it to.

## Required fields per petition

Every petition section states, at minimum:

1. **Obligation** — the requirement letter or DoD item it claims is
   unachievable, quoted or cited precisely.
2. **Type** — exactly one of the four values below.
3. **Evidence** — concrete, checkable support for the claim (a command's
   output, a missing capability in this sandbox, a contradiction between two
   named clauses, an external fact outside the run's control). A petition
   with no evidence is not adjudicated as confirmed; review may reject it on
   that basis alone.

A petition missing the obligation, the type, or evidence is not a valid
filing — review treats an invalid filing as a finding against whichever
requirement demanded the petition in the first place, not as a free pass.

## The four types

| Type | Meaning |
|---|---|
| `environmental` | The sandbox/container this run executes in lacks a capability the obligation needs (no network route, no device, no credential, a service that is not reachable from here). |
| `temporal` | The obligation is anchored to a moment that has already passed, or depends on a clock the run cannot rewind (a "the moment it happens" duty for an event that already happened without it). |
| `contradictory-clauses` | Two clauses in the same contract cannot both be satisfied at once; satisfying one obligation necessarily violates another. |
| `external` | The fact blocking the obligation lives outside the run's control entirely (a third-party service's behaviour, a state on a system the run cannot write to). |

## Adjudication

Review adjudicates every open petition as part of its verdict:

- **CONFIRMED** converts that obligation into a disclosed residual for the
  remainder of the run — every later approach inherits the adjudication and
  must not re-litigate it (requirement C, item 3 of the same PRD).
- **REFUTED** must state concretely what would satisfy the obligation, so
  the run knows exactly what to do differently. Re-filing a REFUTED
  petition against the same obligation is the duplicate case above: a
  blocking finding, not a second hearing.

The run never grants itself a severity, a curability, or a petition outcome
— only review adjudicates.

See also: `docs/review-economics.md` for the materiality rubric and the
late-cure rule this petition protocol is packaged alongside, and
`docs/prds/v0.15-impossible-is-an-answer.md` (`### Petition protocol`,
`## C — impossibility petitions (#112)`) for the original contract text this
document canonicalizes.
