# Sweep records: a machine-readable sha

## The defect this closes (#103)

`docs/prds/README.md`'s own index says the first approach of every wave since
v0.11 was rejected for the same sentence: the suite was not green at the sha
the wave actually shipped. Each rejection traces back to the same shape of
report: a sweep record in `artifacts/reports/` states, in prose, "ran at
sha X" -- and nobody re-checks that sentence. This document defines a
**machine-readable** form for that sentence and the guard that checks it
(`tests/test_sweep_sha_guard.py`, `tests/sweep_sha_guard.py`), so a record
with no declared sha at all is a test failure, not a thing a reader has to
notice.

**What this guard checks now, since task 053 (v0.20, #113 step 4): only
that a declaration exists.** A GitHub check run is bound to its own commit
sha by GitHub itself and cannot go stale as `HEAD` moves, so
`GET /repos/.../commits/<sha>/check-runs` is the attestation review reads
for whether the suite was actually green at a given sha. Re-deriving that
same claim inside this repo's own test suite -- a HEAD-relative currency
check, a hand-maintained discharge chain, a self-anchoring acceptance walk,
a per-run `LAUNCH_SHA` constant to keep bumping at every wave boundary --
was exactly the self-authored ledger #113 exists to stop trusting, and it
cost real iterations every time a wave's own tail commits (the roadmap
block, the PRD index row, the closing report) moved `HEAD` past a record's
declared sha. That machinery is deleted outright, not replaced by anything
in this repo: **the sha a record names is provenance, checked once by the
person or run that read it, never re-verified by this guard against
`HEAD`.**

## The `sweep-sha` block

A sweep record (any file under `artifacts/reports/` that reports the result
of running a test sweep) declares, anywhere in its body, a fenced code block
tagged `sweep-sha` whose content is exactly one line: the 40-character hex
git commit sha the sweep ran at -- i.e. `git rev-parse HEAD` *before* the
record's own commit. (A commit cannot truthfully name its own resulting sha:
the hash is computed from the tree the commit produces, which would have to
already contain the hash it is computing. Every sweep record therefore names
its *parent* sha, the true final code/docs sha of the work it verifies.)

    ```sweep-sha
    1a205cd637caba2874d7db293557fef8ac3558a9
    ```

A record with no `sweep-sha` block is not thereby out of scope. Every report
under `artifacts/reports/` that contains a genuine pytest result summary line
must declare something: a `sweep-sha`, or an `archived-sha` (below) for a
record that predates this guard, or that predates the sha-declaration
requirement reaching it. There is no filename or date exemption: an earlier
revision of this rule exempted every report predating the `v0.14-` filename
prefix by construction, unedited, and that blanket carve-out is the hole
that let #105 hide -- the guard's own summary-line pattern was blind to
every sweep whose pytest run took longer than a minute (i.e. every sweep
there is), and that defect went unnoticed for waves precisely because most
of the reports it would have flagged were never run through the guard at
all: they were exempt, so nobody ever asked what the guard made of them.
Requiring a declaration from every report that shows a result line -- with
no exemption left to hide a blind spot behind -- is what makes the guard's
coverage provable instead of assumed.

## The `archived-sha` block

Identical shape to `sweep-sha` -- exactly one 40-hex-char sha, in its own
fenced block:

    ```archived-sha
    035160131dfc52fec00c6b4415f543232fd428c3
    ```

Before task 053 (v0.20, #113 step 4), this form existed to bypass an
ancestor check against a per-run `LAUNCH_SHA` constant, which is why the tag
carries the name "archived" at all: a record's sha that predated the current
run's launch had no discharge chain to `HEAD` it could ever build, so it
took this second form instead. **That ancestor check, and `LAUNCH_SHA`
itself, are gone.** `sweep-sha` and `archived-sha` are now equally
sufficient to satisfy the one lint this guard runs (`sha_missing_problems`
in `tests/sweep_sha_guard.py`): declaring *some* 40-hex sha, in either
fenced form, is the whole requirement. The two names are kept distinct only
because hundreds of existing records already use one or the other and their
fenced blocks are provenance, exactly as written, never rewritten -- not
because the guard treats them differently going forward. A new record can
use either tag; `sweep-sha` (naming the record's own true parent sha, per
the rule above) is the more informative choice for a record produced during
an active run.

Backfilling a declaration for a pre-existing record with none: derive the
real sha the sweep ran at from git history -- `git log --follow
--diff-filter=A -- <path>` finds the commit that added the file, and (per
the "names its parent sha" rule above) that add commit's **parent** is the
sha the sweep itself actually ran at, because the record commit necessarily
comes after the sweep it reports. Never hand-guess a sha for this.

## The companion rule: every sweep record declares one

Task 002/#103 introduced this rule scoped to the wave that was current then
(any `artifacts/reports/*.md` file named with the `v0.14-` prefix). Task 008
(#103, requirement A) removed that filename gate: **every** report under
`artifacts/reports/`, whatever it is named, that contains a genuine pytest
result *summary line* is a sweep record and must declare a `sweep-sha` or
`archived-sha` block. A report naming no pytest result line at all -- a
plain notification log, for instance -- is not a sweep record and is not
required to declare anything, regardless of its filename.

A "genuine pytest result summary line" is defined by shape, and the shape is
anchored to a **whole line**: the line's entire content (ignoring
indentation and pytest's optional `=` padding) is one or more
comma-separated "N \<outcome\>" clauses (`passed`, `failed`, `skipped`,
`error`/`errors`, `deselected`, `warning`/`warnings`, `xfailed`, `xpassed`)
followed by pytest's own "in \<n\>s" duration -- e.g. `588 passed in 21.92s`,
`2227 passed, 1 skipped, 42 deselected in 164.09s`, or
`==== 2 failed, 10 passed in 1.23s ====`. So the practical rule when writing
a record is: **quote the wrapper's own summary line verbatim, on a line of
its own** (a fenced block is the natural place).

Two kinds of prose are therefore *not* result lines, and neither makes the
file holding it a sweep record:

* a sentence with a count next to an outcome word but no duration --
  "Attempt 1 failed validation because ...", "cannot relabel 016
  failed->skipped itself";
* a sentence that quotes a real summary *inline* -- "the fast tier came back
  4302 passed, 1 skipped in 856.12s, same as last wave" -- because the
  counts-plus-duration shape is not the whole line there. It is prose
  *about* a sweep, in a notification log or a plan note; the sweep's own
  record is the file that quotes the line itself.

`tests/sweep_sha_guard.py` implements this as `is_pytest_result_line` /
`contains_pytest_result_line` over `PYTEST_SUMMARY_RE`, anchored with
`^`/`$` and matched a line at a time -- an unanchored search would classify
both kinds of prose above as sweep records.
`tests/test_sweep_sha_guard.py::test_every_sweep_record_declares_a_sha`
enforces the companion rule;
`::test_an_english_sentence_with_a_count_is_not_a_pytest_result_line`,
`::test_a_real_pytest_summary_line_is_a_pytest_result_line`,
`::test_a_report_that_only_quotes_counts_inline_is_not_a_sweep_record` and
`::test_the_real_notification_log_is_not_misclassified_as_a_sweep_record`
pin both directions of the line rule. A file that names no test result at
all (a plain notification log, for instance) is not a sweep record and is
not required to declare anything.

## What this guard no longer does

Retired outright in task 053 (v0.20, #113 step 4), together with every test
node that existed only to exercise it:

* the HEAD-relative currency check (a declared `sweep-sha` had to equal
  current `HEAD` or be discharged to it);
* the hand-maintained `discharge-sha` chain and its report-only-tail
  allowance;
* the self-anchoring/fixed-point acceptance walk (a bare, undischarged
  `sweep-sha` accepted anyway when its whole range to `HEAD` was
  report/roadmap/index-only);
* the per-run `LAUNCH_SHA` constant, and the ancestor check that used it to
  decide whether a record could call itself `archived-sha`.

None of that is replaced by anything else in this repo. The GitHub check
run bound to each commit's own sha is the attestation for "was the suite
green here"; this guard's only remaining job is "did the record say which
sha it means at all." A `discharge-sha` block in an existing record is
untouched, historical provenance -- nothing reads it any more, and nothing
requires a new one.

Every existing record's own `sweep-sha`/`archived-sha`/`discharge-sha` lines
are unchanged by this retirement -- only the drift-chasing *check* is gone,
never the provenance a record already states. Some historical reports cited
one of the now-deleted functions or test nodes
(`tests/sweep_sha_guard.py::check_reports`, `::_reaches_head`,
`::_report_only_tail`, `::_is_self_anchoring_add_commit`,
`::_self_anchoring_reaches_head`, or
`tests/test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged`
and the other now-deleted currency/discharge/self-anchoring test nodes) by
exact node id; those citations were marked `!retired` in the same commit
(`tests/report_claims.py`'s strong direction for a citation that must no
longer resolve), which is a note about the *citation*, not about the sha the
record declares.

## Where the checks live

* `tests/sweep_sha_guard.py` -- the parser (`sweep_shas`, `archived_shas`),
  the result-line rule (`is_pytest_result_line`,
  `contains_pytest_result_line`) and the one problem-list function that
  still runs (`sha_missing_problems`), shared the same way
  `tests/report_claims.py` shares its checks with every report.
* `tests/test_sweep_sha_guard.py` -- applies it to every report the
  directory currently holds, plus two tmp-dir fixtures that prove the one
  surviving lint fires and clears.

## What a claim is: citation conventions (task 067, steer 004; task 053, #113 step 4)

`tests/report_claims.py` reads every backticked token in a report under two
rules that trip readers who have not seen them written down:

1. **Never backtick a check-run, job or run id.** `SHA_RE` reads *any*
   backticked 7-40 character lowercase-hex token as a claimed commit sha --
   and a GitHub check-run, job or run id is a decimal number, which is
   entirely hex characters, so it matches `SHA_RE` too. Backticking one
   (`` `102507184201` ``) gets it read as a sha claim, and since no commit in
   this repo is ever going to hash to that value, `commit_problems()` fails
   it as `is not a commit in this repo (missing)`. This is not a false
   positive to work around: it is the guard doing exactly what it is for,
   applied to a citation it was never meant to receive. Write such an id as
   bare prose ("check run 102507184201") followed by its html_url field,
   which is unambiguous and independently resolvable; reserve backticked hex
   for actual commit shas.
2. **Cite only a sha already reachable from `origin/main` -- never one you
   are about to create, and never one you have amended away.**
   `commit_problems()` requires every cited sha to resolve to a real commit
   object AND be reachable from `HEAD` (`git rev-list HEAD`); a sha that
   exists only as a dangling object (git keeps an amended, reset or
   rebased-away commit's pre-image on disk until `git gc` reclaims it) is
   not evidence a report can point at, because a fresh clone of the repo can
   never see it. A report that names the sha its own commit is about to
   produce is naming something that, by definition, does not exist yet at
   the moment the report is written -- write the report as a follow-up
   commit after the sha it cites has actually landed, never as part of the
   same commit.

The failure message itself now names the first convention, so a report
author who trips it does not have to already know where to look.
