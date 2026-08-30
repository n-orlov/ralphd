# Sweep records: a machine-readable sha, and how it is discharged

## The defect this closes (#103)

`docs/prds/README.md`'s own index says the first approach of every wave since
v0.11 was rejected for the same sentence: the suite was not green at the sha
the wave actually shipped. Each rejection traces back to the same shape of
report: a sweep record in `artifacts/reports/` states, in prose, "ran at
sha X" -- and nobody re-checks that sentence once the wave's own tail commits
(the roadmap block, the PRD index row, the closing report) move `HEAD` past
X. This document defines a **machine-readable** form for that sentence and
the guard that checks it (`tests/test_sweep_sha_guard.py`,
`tests/sweep_sha_guard.py`), so the discrepancy is a test failure, not a
thing a reader has to notice.

## The `sweep-sha` block

A sweep record (any file under `artifacts/reports/` that reports the result
of running a test sweep) that wants the guard's coverage declares, anywhere
in its body, a fenced code block tagged `sweep-sha` whose content is exactly
one line: the 40-character hex git commit sha the sweep ran at -- i.e.
`git rev-parse HEAD` *before* the record's own commit. (A commit cannot
truthfully name its own resulting sha: the hash is computed from the tree
the commit produces, which would have to already contain the hash it is
computing. Every sweep record therefore names its *parent* sha, the true
final code/docs sha of the work it verifies.)

    ```sweep-sha
    1a205cd637caba2874d7db293557fef8ac3558a9
    ```

A record with no `sweep-sha` block is not thereby out of scope. Every report
under `artifacts/reports/` that contains a genuine pytest result summary line
must declare something: a `sweep-sha` if its sweep is current enough to need
discharge-chain tracking to `HEAD`, or an `archived-sha` (below) if the
record's own sha predates this run's launch sha -- checked by the ancestor
rule (`git merge-base --is-ancestor <sha> <LAUNCH_SHA>`), never by exempting
a whole vintage of report by filename or by date.

An earlier revision of this rule said the opposite: an undeclared record
declared nothing and was simply out of scope, and every report predating the
`v0.14-` filename prefix (v0.5 through v0.13, as a class) was exempt by
construction, unedited. That blanket carve-out is the hole that let #105
hide -- the guard's own summary-line pattern was blind to every sweep whose
pytest run took longer than a minute (i.e. every sweep there is), and that
defect went unnoticed for waves precisely because most of the reports it
would have flagged were never run through the guard at all: they were
exempt, so nobody ever asked what the guard made of them. Requiring a
declaration from every report that shows a result line -- with no exemption
left to hide a blind spot behind -- is what makes the guard's coverage
provable instead of assumed.

## The discharge rule

A wave's tail always commits *after* its sweeps (the roadmap block, the PRD
index row, the closing report) -- by the time those commits land, every
sweep record's `sweep-sha` is provably one or more commits behind `HEAD`. A
guard that failed on that alone would be permanently red by construction, so
a declared sha that is not the current `HEAD` gets exactly one other way to
pass: an explicit **discharge**.

A `discharge-sha` block, in any report under `artifacts/reports/` (including
the same file, as a trailing addendum, or a different, later report),
retires a specific stale sha. Every line in the block is a **pair** of
40-hex shas: the stale sha being retired, then the sha the discharge check
was actually run at (`git rev-parse HEAD` at the moment of examination):

    ```discharge-sha
    1a205cd637caba2874d7db293557fef8ac3558a9 c4f24ad7ab060115cace6779a495dde77f76f8c7
    ```

The second sha is what makes a discharge checkable rather than an assertion:
the guard walks it. A line naming only one sha is **rejected as malformed**
(a hard failure naming the file and the line), because without a verified-at
sha there is no chain for the guard to terminate at `HEAD`.

Adding a `discharge-sha` block is a claim that the sha it names has been
*examined*, not merely aged -- the commits between that sha and `HEAD` are
known not to invalidate the sweep it belonged to. In practice that means one
of:

* the commits between the stale sha and `HEAD` are report/roadmap/index-only
  (no `src/`, `tests/`, or behaviour-affecting `docs/` change), and
  requirement B's discovered guard subset
  (`scripts/final_sha_guards.sh`, which greps `tests/` for its own membership
  at run time -- there is deliberately no fallback list of module names to
  copy, here or anywhere else) has been run
  green at the current `HEAD`; or
* a later sweep record in the same wave already re-ran the full check at (or
  past) `HEAD` and is itself either current or discharged the same way.

The guard resolves discharge transitively and *anchored*: a chain of stale
shas, each line pointing from the sha it retires at the sha the check was run
at, has to terminate somewhere the guard accepts.

**Literal equality with `HEAD` alone would make this rule unsatisfiable by
construction, so it is not the only way to terminate.** A commit can only
name shas that already exist -- it cannot truthfully name its own resulting
sha (see above) -- so the commit that *adds* a `discharge-sha` block always
leaves `HEAD` exactly one commit past the chain's own end: the discharge
commit itself. A guard that demanded the chain reach literal `HEAD` would
therefore reject every discharge the moment it was written, and the very
next report/roadmap/index tail commit would repeat the same off-by-one --
no reachable repository state could ever satisfy it. (This was a real,
measured defect in an earlier revision of this rule, not a hypothetical:
task 003, #103.)

The chain terminates successfully when it reaches a sha that either

* equals current `HEAD` exactly, or
* has a report-only gap to `HEAD` -- `git diff --name-only <sha>..HEAD`
  names nothing outside `artifacts/reports/**`.

The second form is what makes the rule satisfiable at all: it is precisely
wide enough to cover the discharge commit itself (and any further
report/roadmap/index-only tail commits after it), and no wider -- a single
intervening `src/` or `tests/` change still fails the chain. The allowance
only ever applies to a sha the chain actually walked to by following a real
`discharge-sha` hop; the bare `sweep-sha` a record declares never gets it on
its own, so an entirely undischarged stale sha is still a hard failure no
matter how report-only the time since has been -- discharge has to be
*stated*, not merely true by accident of what else did not change.

So when the wave's tail moves `HEAD` again, the previous discharge does not
silently keep covering the record forever: either the new tail is itself
report-only (already covered), or re-verify at the new `HEAD` and add
`<old-verified-at-sha> <new-HEAD>`, extending the chain, or re-take the
sweep.

**A stale, undischarged sha is always a hard failure, never a warning.**
`tests/test_sweep_sha_guard.py::test_stale_undischarged_sha_fails` proves
this with a fixture record built in a temporary directory (never inside the
real `artifacts/reports/`, so the fixture cannot itself trip the guard it is
testing) -- a `sweep-sha` naming a sha that is neither `HEAD` nor discharged
must be rejected.
`tests/test_sweep_sha_guard.py::test_discharged_stale_sha_passes` and
`::test_a_transitive_discharge_chain_ending_at_head_passes` prove the
discharge path actually clears the same failure, one hop and two hops;
`::test_a_discharge_chain_that_never_reaches_head_fails`,
`::test_a_discharge_cycle_never_clears_a_stale_sha` and
`::test_a_one_sha_discharge_block_is_rejected_as_malformed` prove the three
ways a discharge can fail to be one. `::test_a_discharge_ending_short_of_head_passes_when_the_tail_is_report_only`,
`::test_a_discharge_ending_short_of_head_fails_when_one_tail_commit_touches_src`
and `::test_an_undischarged_sha_still_fails_even_with_a_report_only_tail`
build real throwaway git repos (never inside the real `artifacts/reports/`)
to prove the report-only-tail allowance itself: it bridges a genuinely
report-only gap, it does not bridge a gap with even one `src/`-touching
commit in it, and it never applies to a sha that was never discharged in the
first place.

The failure message names the record's path, its declared sha, the current
`HEAD`, and what to do: re-take the sweep at `HEAD` and update the record, or
-- if the intervening commits are the report/roadmap/index tail requirement B
exists for -- run that subset at `HEAD` and add a `discharge-sha` block.

## Parent-of-HEAD acceptance: the terminating fixed point

A record's own add-commit is a special case of the discharge gap above, not
a different problem: the record names its true parent sha (a commit cannot
name its own resulting sha -- see above), so the instant that add-commit
lands, the declaration is exactly one commit behind `HEAD`, and there is no
`discharge-sha` any commit could yet add naming that not-yet-computed `HEAD`
as its verified-at sha. Requiring an explicit discharge for that gap would
make the guard permanently red the moment *any* record's own commit landed
-- not a corner case, the typical one.

`check_reports` therefore also accepts a declared sha with no
`discharge-sha` block at all when `HEAD` is *exactly* the commit that ADDED
the declaring record (`git diff --name-status` reports it as `A`, not `M`),
that commit's diff touches the record's own path and nothing else, and the
declared sha is precisely that commit's single parent
 (`tests/sweep_sha_guard.py::_is_self_anchoring_add_commit`). This is narrow
by construction: refused the moment the tip commit touches any path outside
the one record, and refused for a second commit stacked on top of the
first -- an edit to an already-existing record is git status `M`, so only
the literal add commit ever qualifies; report/docs commits are never
exempted as a class. The invariant preserved: the guard subset ran green
over every line of content that ships, and a record-only commit -- one that
does nothing but add a record naming its own true parent -- cannot change
what that subset would report if run again right now.

`tests/test_sweep_sha_guard.py::test_a_records_own_add_commit_naming_its_parent_passes_with_no_discharge`,
`::test_the_self_anchor_is_refused_when_the_tip_commit_touches_another_path`
and
`::test_the_self_anchor_is_refused_for_a_second_record_commit_stacked_on_top`
pin the mechanism and its two refusals, in real throwaway git repos under
`tmp_path`.

## The companion rule: every sweep record declares one

Task 002/#103 introduced this rule scoped to the wave that was current then
(any `artifacts/reports/*.md` file named with the `v0.14-` prefix). Task 008
(#103, requirement A) removed that filename gate: **every** report under
`artifacts/reports/`, whatever it is named, that contains a genuine pytest
result *summary line* is a sweep record and must declare a `sweep-sha` or
`archived-sha` block (the latter documented in the next section). A report
naming no pytest result line at all -- a plain notification log, for
instance -- is not a sweep record and is not required to declare anything,
regardless of its filename.

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

## The `archived-sha` block, and the ancestor rule

Before task 008 (#103, requirement A), every report predating the `v0.14-`
prefix was exempt from the companion rule above by construction -- its
filename simply did not match. Once the filename gate is gone, those
records (and every earlier wave's) need a way to satisfy the rule without a
discharge chain to current `HEAD`, because that chain was never going to
exist: the sweep they report ran, and the code and the record both stopped
changing, long before this run's own work started.

The `archived-sha` block is that second, narrower form. Its shape is
identical to `sweep-sha` -- exactly one 40-hex-char sha, in its own fenced
block:

    ```archived-sha
    035160131dfc52fec00c6b4415f543232fd428c3
    ```

but the check is different: `check_reports` accepts a declared
`archived-sha` only when it is `LAUNCH_SHA` (the sha this run itself
launched from, `docs/prds/README.md`'s "the sha the wave actually shipped"
for whichever run is current) or a genuine git **ancestor** of it
(`git merge-base --is-ancestor <sha> <LAUNCH_SHA>`) -- never a check against
current `HEAD`, and never a discharge chain. A sha that is *not* such an
ancestor -- in particular any sha produced during this run's own work, which
is by definition a descendant of `LAUNCH_SHA`, not an ancestor of it -- is a
hard failure naming the file, the declared sha and `LAUNCH_SHA`. This is the
rule's whole point: **a current-wave record cannot label itself archived**
to dodge the sweep-sha/discharge-sha chain the way an actually-current
record has to carry it. "Archived" means the record's own sha predates the
run that is checking it, not merely that nobody has re-verified it lately.

Backfilling an `archived-sha` for a pre-existing record: derive the real sha
the sweep ran at from git history -- `git log --follow --diff-filter=A --
<path>` finds the commit that added the file, and (per the "names its
parent sha" rule above) that add commit's **parent** is the sha the sweep
itself actually ran at, because the record commit necessarily comes after
the sweep it reports. Never hand-guess a sha for this.

`tests/test_sweep_sha_guard.py::test_a_post_launch_sha_declared_as_archived_is_refused`
and `::test_archived_sha_accepts_the_launch_sha_itself` pin the ancestor
check both ways;
`::test_a_pre_launch_filename_no_longer_exempts_a_sweep_record` pins that
the old filename exemption is gone.

## Where the checks live

* `tests/sweep_sha_guard.py` -- the parser (`sweep_shas`, `archived_shas`,
  `discharge_entries`, `malformed_discharge_lines`), the result-line rule
  (`is_pytest_result_line`, `contains_pytest_result_line`) and the two
  problem-list functions (`check_reports`, `sha_missing_problems`),
  shared the same way `tests/report_claims.py` shares its checks with every
  report.
* `tests/test_sweep_sha_guard.py` -- applies them to every report the
  directory currently holds, plus the tmp-dir fixtures that prove the guard
  actually fires and actually clears.
