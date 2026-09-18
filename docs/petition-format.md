# Impossibility petition format

An **impossibility petition** is how a run tells review that one of its own
contract obligations cannot be satisfied — instead of grinding attempts
against something arithmetically certain to stay red. This is requirement C
(#112) of `docs/prds/v0.15-impossible-is-an-answer.md`; the reusable rubric
this format is part of lands as `docs/review-economics.md` (requirement D of
the same PRD).

## Where the file lives

**Task 065 (requirement N.6, #150): the run dir, not the workspace tree.**
The engine's own writers (`engine.petitions.file_worker_petition`/
`file_exhaustion_petition`) and its own reader/write-back path
(`open_petitions_in_workspace`/`write_back_petition_adjudication`) take an
optional `run` handle; every production call site in `LoopSupervisor`
passes its own run, so a filing and its later adjudication both land in
`run.petitions_file` (`<run dir>/petitions.md`) — never a tracked
`artifacts/reports/<wave>-petitions.md` file in the workspace. #150's own
glob-discovery bug read whichever `*-petitions.md` file it found last
under the workspace tree; N.6 removes that tree-side file for a run built
on this code rather than fixing the glob again, and #150 stays CLOSED.

The historical shape — one file per wave, `artifacts/reports/<wave>-
petitions.md`, tracked alongside the wave's other evidence reports — is
why `artifacts/reports/v0.21-petitions.md` still exists in this repo: N.1
deletes every other file under `artifacts/reports/` but leaves that one
behind, since the engine *running this wave's own work* is an older build
and still appends to it. A future wave, built on this code, never creates
a workspace file for its own petitions at all.

**Task 008 (requirement H): no workspace-tree fallback at all.** Calling
any of the four functions above with a run handle writes only inside that
run's own directory, exactly as N.6 already described. Calling one
**without** a run handle used to fall back to reading or writing whichever
`<wave>-petitions.md` file happened to exist under the workspace tree —
the shape a call with no run handle at all (every pre-existing test
fixture that exercised these functions directly, never a production call
site) took before this task. That fallback is retired outright: without a
run handle, `file_worker_petition`/`file_exhaustion_petition` refuse (an
error string, nothing written) and `write_back_petition_adjudication`
performs a documented no-op (`None`, nothing written) — `artifacts/
reports/` is left byte-identical either way. `open_petitions_in_workspace`
(a pure reader) is unaffected: it still reads the historical workspace
file when one exists, which is how `v0.21-petitions.md` above keeps
surfacing to review at all.

**This cure (requirement H.2, #200): the same posture for the one helper
the four functions above never named.** `engine.petitions.ensure_
petitions_file` predates task 008 and had already lost every production
caller when task 008 landed — but it remained directly callable, and its
own tree-creation path still created a brand-new `<wave>-petitions.md`
under the workspace on demand, precisely the write task 008's own
requirement forbids on ANY path, not merely the two writers it named.
Calling `ensure_petitions_file` now never creates or modifies anything:
it returns the CURRENT wave's file if one already exists on disk, else
`None`, always — a pure read, the exact shape `open_petitions_in_
workspace` already took, and `artifacts/reports/` stays byte-identical
no matter what workspace or contract text is passed in.

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
2. **Type** — exactly one of the five values below.
3. **Evidence** — concrete, checkable support for the claim (a command's
   output, a missing capability in this sandbox, a contradiction between two
   named clauses, an external fact outside the run's control). A petition
   with no evidence is not adjudicated as confirmed; review may reject it on
   that basis alone.

A petition missing the obligation, the type, or evidence is not a valid
filing — review treats an invalid filing as a finding against whichever
requirement demanded the petition in the first place, not as a free pass.

## The five types

| Type | Meaning |
|---|---|
| `environmental` | The sandbox/container this run executes in lacks a capability the obligation needs (no network route, no device, no credential, a service that is not reachable from here). |
| `temporal` | The obligation is anchored to a moment that has already passed, or depends on a clock the run cannot rewind (a "the moment it happens" duty for an event that already happened without it). |
| `contradictory-clauses` | Two clauses in the same contract cannot both be satisfied at once; satisfying one obligation necessarily violates another. |
| `external` | The fact blocking the obligation lives outside the run's control entirely (a third-party service's behaviour, a state on a system the run cannot write to). |
| `self-referential` | The obligation's own text refers to itself in a way that makes it unmeetable as written — a criterion that can only be verified using a state the criterion's own satisfaction would have to create, or that names its own not-yet-existing commit/sha. |

## Adjudication

Review adjudicates every open petition as part of its verdict:

- **CONFIRMED** converts that obligation into a disclosed residual for the
  remainder of the run — every later approach inherits the adjudication and
  must not re-litigate it (requirement C, item 3 of the same PRD). A
  run-scope CONFIRMED obligation (`engine.petitions.confirmed_obligations`,
  persisted by `record_confirmed_adjudications`) is not re-surfaced to
  review as an OPEN petition on any later review pass, even while its own
  `<wave>-petitions.md` filing's `Adjudication` field still reads blank —
  `petitions_review_context` consults the run-scope record before
  rendering, and drops exactly that obligation while leaving every other
  open petition in the same file rendered; a tampered record is treated as
  an empty confirmed set, so tampering surfaces the obligation for
  adjudication again rather than manufacturing an exemption.
- **REFUTED** must state concretely what would satisfy the obligation, so
  the run knows exactly what to do differently. Re-filing a REFUTED
  petition against the same obligation is the duplicate case above: a
  blocking finding, not a second hearing. A run-scope REFUTED obligation is
  likewise not re-surfaced to review as an OPEN petition on any later
  review pass, even while its own `<wave>-petitions.md` filing's
  `Adjudication` field still reads blank — for a workspace filing that
  field can never close at all, because the engine annotates only its own
  run-dir petitions file and never a tracked workspace file, so the
  run-scope record is the only channel a settlement has.
  `petitions_review_context`/`open_unconfirmed_petitions` consult that
  record through `engine.petitions.settled_obligations` (CONFIRMED **and**
  REFUTED) before rendering, and a tampered record is treated as an empty
  settled set, so tampering surfaces the obligation for a fresh hearing
  rather than silently hiding it. This changes nothing about exemptions:
  REFUTED still exempts nothing and is still not something a later approach
  inherits as an exemption — only the rehearing is retired, never the
  entry, which stays in the run-scope record, in the `petition_refuted`
  event and in the run record with its remedy.

The run never grants itself a severity, a curability, or a petition outcome
— only review adjudicates.

## Where duplicate detection lives, and what pins it

Both duplicate cases above are detected **engine-side**, never in the run's
own prose: `engine.petitions.duplicate_petition_obligations` groups every
filing in every `<wave>-petitions.md` by normalised obligation and reports
any obligation with more than one filing, and
`engine.petitions.duplicate_petition_findings` turns each into a
`severity: "blocking"`, `curable: true` finding. `LoopSupervisor` appends
those findings unconditionally in `_review_findings_channel` (whatever
review's own `findings.json` says) and names the same fact in review's own
prompt via `_duplicate_petitions_review_context`, joined at
`_review_extra_context`.

That pair is the POST-HOC pass, over whatever already landed in a
`<wave>-petitions.md` file however it got there. A worker's own structured
impossibility declaration (task 004, requirement A.1, #114 --
`HAT_SCHEMAS["worker"]["properties"]["petition"]` in
`engine/transitions.py`) never reaches that state in the first place: the
ENGINE — never the worker — is the one thing that writes a filing to disk
at all (`engine.petitions.file_worker_petition`), and it checks the same
normalised-obligation match against every already-filed petition *before*
writing, refusing a second declaration against an obligation already
filed instead of filing it. A malformed declaration (a blank obligation or
evidence field the schema's own `type: "string"` check cannot catch) is
refused the same way, through the ordinary contract re-ask ladder, and
never reaches the file either.

`tests/test_duplicate_petition_findings.py` (12 tests) pins each case by
name:

- a second petition for the same obligation —
  `test_duplicate_petition_for_the_same_obligation_is_detected` and
  `test_duplicate_petition_becomes_a_blocking_finding`;
- a petition already adjudicated REFUTED and re-filed —
  `test_refiled_refuted_petition_is_detected` and
  `test_refiled_refuted_petition_becomes_a_blocking_finding`;
- the engine/prompt surfaces themselves —
  `test_review_findings_channel_appends_duplicate_finding_engine_side`,
  `test_review_findings_channel_no_extra_finding_without_a_duplicate`,
  `test_duplicate_petitions_review_context_names_the_obligation` and
  `test_duplicate_petitions_context_joins_review_extra_context`, with
  `test_no_duplicate_when_every_obligation_has_one_filing` and
  `test_no_duplicates_across_disjoint_files_with_distinct_obligations` as
  the negative controls.

See also: `docs/review-economics.md` for the materiality rubric and the
late-cure rule this petition protocol is packaged alongside, and
`docs/prds/v0.15-impossible-is-an-answer.md` (`### Petition protocol`,
`## C — impossibility petitions (#112)`) for the original contract text this
document canonicalizes.
