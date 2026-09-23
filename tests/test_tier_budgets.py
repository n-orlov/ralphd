"""Task 004 (requirement G.8): the `smoke` and `fast` tiers have declared
budgets, and a tier that outgrows its budget fails here.

Two halves, because the two facts have very different costs to check:

* **Counts (live).** The tier membership is derived at collection time
  (tests/conftest.py stamps `smoke`/`fast` onto every collected item), so this
  module can ask pytest itself, in a `--collect-only` child, how many tests and
  how many modules each tier currently holds -- about a second and a half per
  tier -- and compare that against the ratcheting ceiling `TIER_CEILINGS`
  declares, in this module, as a code constant (v0.21 task 062, requirement
  N.4: "the ratcheting ceilings ... are code, they cannot go stale silently").
  Add enough tests to push a tier past its ceiling and this goes red, which is
  the ratchet requirement G.8 asks for.
* **Wall clock (recorded).** Measuring a tier's wall clock means *running* the
  tier, which no per-task records test can afford (the fast tier is minutes).
  So the wall clock is a recorded measurement in `docs/testing.md`'s budget
  table, and what is checked here is that every recorded number sits inside the
  budget declared beside it, and that a number which misses the PRD's target is
  disclosed as missed *with* that measured number rather than quietly rounded
  into compliance.

The wall-clock half still reads the doc, since that half checks a *recorded*
measurement against a *recorded* budget -- both numbers already live nowhere
else. The live count/module ratchet no longer does: its ceiling is a code
constant below, not a doc cell, so it cannot drift when `docs/testing.md`'s
count rows are edited or deleted (v0.21 task 063).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTING_DOC = REPO_ROOT / "docs" / "testing.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "final_sha_guards.sh"
VERIFY_PROMPT = REPO_ROOT / "src" / "ralphd" / "prompts" / "task-verify.md"

# The PRD's own G.8 targets (docs/prds/v0.19-a-note-is-not-a-rejection.md,
# requirement G.8): the contract this suite is measured against. Wall-clock
# targets are per lane and live in the table's own "PRD target" column; the
# tier-wide test/module counts used to be checked here too, by
# `test_the_tier_wide_count_targets_are_met_or_disclosed`, which v0.21 task
# 043 retired -- see that retirement's note below.

TIER_MARKERS = ("smoke", "fast")

# The ratcheting ceilings for the live collected-tier check below (v0.21 task
# 062, requirement N.4: "the ratcheting ceilings ... stay -- they are code,
# they cannot go stale silently"). Moved out of docs/testing.md's budget
# table's "budget tests"/"budget modules" columns, which this module no
# longer reads for this purpose. Raise a value only with the measurement
# that earned it named in the commit message that raises it; task 006
# (PRD F) raised `fast`'s test ceiling 5800 -> 5801: three new regression
# tests in `tests/test_log_render_bounded_tail.py` (a bounded-tail/
# live-tail/dead-run cut mid-iteration must carry `text_seen`/
# `thinking_seen`/`toolcall_seen` collapse state across the restart) push
# the fast tier's live collected count to 5801 (`pytest -m fast
# --collect-only`). `modules`/`smoke` are unchanged (390 fast modules /
# 270 smoke modules, 4300 smoke tests).
#
# Task 007 (PRD G) raised `fast`'s test ceiling 5801 -> 5803: two new
# regression tests in `tests/test_hub_log_tail_cache.py` (the
# append-and-revalidation scenario, and the freshly-issued-validator
# revalidates-without-rerendering scenario) push the fast tier's live
# collected count to 5803 (`pytest -m fast --collect-only`).
# `modules`/`smoke` are unchanged.
#
# Task 008 (requirement H) raised `fast`'s test ceiling 5803 -> 5804: one
# net new test in `tests/test_petitions.py`
# (`test_confirmed_is_appended_to_the_run_dir_matching_section_only`, the
# run-dir survivor of the CONFIRMED tree-write test retired by this same
# task) pushes the fast tier's live collected count to 5804 (`pytest -m
# fast --collect-only`). `modules`/`smoke` are unchanged.
#
# Task 009 (requirement I) raised `fast`'s test ceiling 5804 -> 5810 (net
# +6 mutation-node tests in `tests/test_record_tax_growth_back.py`), and
# cure-01-03 (H.2, #200) pushed the live collected count one further to
# 5811 without any matching ceiling bump at all -- review found both: the
# 5804 -> 5810 raise itself breaches non-negotiable 2 ("never raise a
# budget ceiling") and the standing rule against loosening a guard, and
# 5811 > 5810 was already a live violation of the ceiling as it stood.
#
# cure-01-04 (#204) is the cure: it does NOT raise anything. It lowers
# `fast`'s test ceiling back to its 5,800 launch value and reduces the
# live collected count to match, by consolidating -- never deleting the
# coverage of -- fifteen of the tests the four raises above (F/G/H/I plus
# cure-01-03's own +1) had added, into six test functions that keep every
# original assertion, just merged into fewer bodies (each merged
# function's own docstring names which retired functions it carries and
# says so is unchanged): three bounded-tail/dead-run/live-tail scenarios
# in `tests/test_log_render_bounded_tail.py`; three no-run-handle
# exhaustion scenarios in `tests/test_petitions.py`; six
# nested-file/budget-table-row scenarios in
# `tests/test_record_tax_growth_back.py`; and four ETag/cache scenarios in
# `tests/test_hub_log_tail_cache.py`. Measured directly with
# `.venv/bin/python -m pytest -m fast --collect-only -q` at this commit's
# own tip: 5800/5920 tests collected (120 deselected) -- exactly the
# lowered ceiling, with headroom for nothing further until a future task
# earns it the same way every prior raise above did. `modules`/`smoke`
# are unchanged (no module gained or lost; no smoke test touched).
#
# GH #209 ("a refusal is not a repair") is the future task that earned it,
# and raises `fast`'s test ceiling 5800 -> 5844. It is the ONE raise for
# that whole change: each of its seven implementation units was forbidden
# to touch this literal, so `test_the_collected_tier_is_inside_its_declared_
# budget[fast]` was deliberately red for thirteen commits and one measured
# raise, made last, closes it -- the same discipline cure-01-04 above
# established, applied to a change that genuinely does add coverage rather
# than to one padding a budget. The +44 is regression coverage for seven
# closing-half defects, each of which REJECTED work that was already done,
# attributed per unit by collecting `-m fast` at each unit's own tip:
# defect 5's ledger retirement +3, all in `tests/test_confirmed_petition_
# not_resurfaced.py`; defect 1's clause-bound criteria repair +0 -- every
# new proof folded into an existing body, five nodes renamed in
# `test_criteria_bound_lint.py`/`test_cured_when.py`/`test_legal_rewrite_
# reprompt.py` and no function added; defects 3+4's cure-insertion
# correctness and REFUTED-remedy cure task +10 (`test_cure_invalidates.py`
# 4, `test_petitions.py` 4, `test_midrun_refuted_continues.py` 2); defect
# 2's exhausted review contract ladder +8 (`test_contract_ladder.py` 7,
# `test_transitions.py` 1); defect 6's live operator status rulings +4
# (`test_amendment_status_load_time.py`, 5 added and 1 retired); defect 7's
# worker `proposed_task_updates` +17, all in `test_worker_split_proposal.
# py`; and defect 3's residual, the `orderingOnly` edge kind, +2
# (`test_cure_invalidates.py` 1, `test_plan_ingestion.py` 1). No unit
# created a new test MODULE (24 existing modules were edited) and no test
# was deleted, merged or weakened to buy room.
#
# Two of those per-unit numbers are three nodes above what the units
# themselves declared -- defect 2 declared +6 and defect 7 declared +16 --
# and the difference is named rather than papered over: each unit measured
# its delta at its implementation commit, and its own review-fix commit
# then added a further regression node on top. `cf2d1242` added
# `test_contract_ladder.py::test_a_legible_rejected_verdict_with_a_schema_
# slip_elsewhere_still_rejects` and `::test_a_normalised_fail_inherits_the_
# all_residual_recomputation` (+2); `bd6df464` added `test_worker_split_
# proposal.py::test_a_repoint_that_repairs_nothing_is_refused_and_never_
# repeatable` and `::test_repointing_alone_is_bounded_as_a_stagnation_
# escape` while retiring `::test_a_doom_clearing_repoint_cannot_be_
# repeated_on_the_same_task` into the first of them (+1 net). Nothing else
# moved: the branch's own base, `e4aee1f1`, measures exactly 5800.
#
# Measured directly with `.venv/bin/python -m pytest -m fast --collect-only
# -q` at this commit's own tip: 5844/5976 tests collected (132 deselected)
# -- exactly this ceiling, with headroom for NOTHING further, which is the
# chosen number and not an oversight. Every raise recorded above set the
# ceiling to the measured count and no higher, cure-01-04 LOWERED it back
# to the measured count rather than keep room it had not earned, and a
# ceiling padded above the live count stops guarding in exactly the amount
# it is padded by. The next task earns its own raise the same way this one
# did. `modules`/`smoke` are unchanged, and were measured rather than
# assumed: `-m fast` collects 386 modules (this 390 ceiling has stood 4
# above the live count since before this branch -- `e4aee1f1` measures 386
# too), and `-m smoke` collects 4220 tests over 270 modules, the module
# count sitting exactly on its own zero-headroom ceiling, so no unit here
# could have added a smoke-tier module either.
#
# GH #208 ("review/verdict_gate mandate a Python `pip install -e .` clone
# bootstrap") raises `fast`'s test ceiling 5844 -> 5857 and -- for the first
# time in this record -- `smoke`'s MODULE ceiling 270 -> 271. It is one
# change with one new test module, and both raises are that module:
# `tests/test_review_clone_protocol.py`, 13 tests, carrying no tier marker
# and so collected by both tiers. Attribution is the whole module, and the
# 13 split three ways: 2 pin `JobConfig.review_clone_protocol` (the shipped
# default, and the job.yaml key), 4 pin the rendered `## Disposable-clone
# protocol` block (every hat sees it, it grants authority and bounds a
# non-zero exit, absent renders nothing, whitespace-only reads as absent),
# 4 pin the two judging prompts (the invariant rather than a Python mandate,
# the operator-authorized protocol, the `curable: false` prohibition, the
# standing ban on manufacturing identity evidence), and 3 pin `ralphctl
# start --review-clone-protocol` end to end (flag, absent, template key).
#
# A new module rather than a new scenario inside an existing one, stated as
# a decision: the subject is a config field plus two prompt sections plus a
# CLI flag, which no single existing module owns -- `test_notifier_url_flag.
# py` owns a different field's three levels, and folding prompt-text
# assertions into it would make that module pin two unrelated things, which
# this block's own prose calls worse than a raised ceiling.
#
# Measured directly with `.venv/bin/python -m pytest -m fast --collect-only
# -q` and `-m smoke --collect-only -q` at this commit's own tip: `-m fast`
# 5857/5989 tests collected (132 deselected) over 387 modules, `-m smoke`
# 4233/5989 (1756 deselected) over 271 modules. Both raised literals sit
# exactly on the measured count with headroom for NOTHING further, the same
# choice every entry above made. The two UNraised literals were measured
# rather than assumed: `fast`'s 390 module ceiling still stands 3 above the
# live 387 (it was 4 above 386 before this module), and `smoke`'s 4300 test
# ceiling stands 67 above the live 4233. Every one of these six numbers is
# `_modules(collected(marker))` in this module's own helpers, not a count of
# `tests/*.py` paths in the raw collect-only text -- the latter over-counts,
# which is how the 388 first written here was wrong by one.
#
# v0.23 (this wave, tasks 002-024) raises `fast`'s test ceiling 5857 -> 5997
# and its module ceiling 390 -> 398 (the module ceiling was already 3 above
# the live 387 going in, so 387 -> 398 is the live jump this raise actually
# earns), and `smoke`'s test ceiling 4300 -> 4361 and its module ceiling
# 271 -> 281. Twenty-two implementation tasks (002-023) were forbidden to
# touch this literal, exactly like GH #209's seven units above, so
# `test_the_collected_tier_is_inside_its_declared_budget` stayed red across
# the whole wave until this one measured raise -- disclosed in every PR body
# and commit message, never fixed early. Its two parameters went red at
# different points, and the difference is recorded rather than flattened,
# because `fast`'s test ceiling had zero headroom going in while `smoke`'s
# test ceiling had 67 (see the GH #208 entry above). Both points were
# measured at the tip that reached them, not derived:
#   * `...[fast]` was red from the wave's very first added test. Task 002's
#     own tip `2b75a8a3` (no longer an ancestor of `origin/main` after the
#     task 029 rebase-merge; reachable rebased commit `1fc9291f`) collects
#     5860 fast tests against the 5857 ceiling standing then, so every
#     unit-group PR, H onward, carried this one.
#   * `...[smoke]` was GREEN on the first four unit-groups -- H (002),
#     E (003-004), B (005-009), C (010-012). Task 002's tip collects 4233
#     smoke tests over 271 modules, and C's tip `4bc00afe` (no longer an
#     ancestor after the rebase-merge; reachable rebased commit
#     `66bcb926`) collects 4261 over 271, both inside the 4300 tests / 271
#     modules standing then. It first went red at task 013 (`bf5c109b`, no
#     longer an ancestor after the rebase-merge; reachable rebased commit
#     `36df53d4`), and through the MODULE half rather than the test half:
#     275 smoke modules against a 271 ceiling, with that tip's smoke test
#     count, 4295, still under 4300. The smoke test count first exceeded
#     4300 one task later, at 014 (`5b4d9bbc`, no longer an ancestor after
#     the rebase-merge; reachable rebased commit `2405940e`; 4315 over 276
#     modules). So the `[smoke]` red rode only the A
#     (013-017), D (018-020), G (021) and F (022-023) PRs; the four earlier
#     unit-group PRs carried `[fast]` alone.
# Per-task attribution, measured by checking out each task's own final
# commit into a throwaway worktree (never this run directory) and running
# `-m fast --collect-only -q` / `-m smoke --collect-only -q` there, so each
# delta below is the tier's live count at that exact tip minus the tip
# before it, not an estimate. Every sha named below is the ORIGINAL
# pre-rebase-merge measurement revision -- the one actually checked out and
# measured, task by task, as the wave landed. Task 029's rebase-merge of
# each unit-group PR onto `origin/main` gave every one of these an
# equal-diff (identical `git diff <sha>^..<sha>` stat) rebased commit under
# a new sha, so the original measurement revision named here is NO LONGER
# an ancestor of `origin/main` after that merge; the reachable rebased
# commit carrying the same diff is named alongside it in each entry
# (residual R01):
#   - 002 (H, hub mutating-route identity + startup opt-in) `2b75a8a3`
#     (no longer an ancestor after the rebase-merge; the reachable rebased
#     commit is `1fc9291f`): fast +3, smoke +0, no module change.
#   - 003 (E.1, cure-insertion re-point event) `b5b0f445` (no longer an
#     ancestor after the rebase-merge; reachable rebased commit `6c09d625`):
#     +0/+0 (plumbing only, no new test).
#   - 004 (E.2, confirmed-petition re-point event) `2e66175f` (no longer an
#     ancestor after the rebase-merge; reachable rebased commit `b63f5aa6`):
#     fast +2, smoke +2, no module change.
#   - 005 (B.1, invalidates re-open walk extracted as a pure predicate)
#     `c791be84` (no longer an ancestor after the rebase-merge; reachable
#     rebased commit `abcd991d`): fast +2, smoke +2, no module change.
#   - 006 (B.2, pre-cure workspace HEAD sha per re-opened record)
#     `7a45acc2` (no longer an ancestor after the rebase-merge; reachable
#     rebased commit `4090a785`): fast +4, smoke +4, no module change.
#   - 007 (B.3, un-reopen task-status edge) `c1fb6ebd` (no longer an
#     ancestor after the rebase-merge; reachable rebased commit
#     `d951a82e`): fast +5, smoke +5, no module change.
#   - 008 (B.4, cure-task diff reconciliation + re-open ownership, plus its
#     own red-lane cure retiring a grandfathered await-binding entry and a
#     final commit pinning the vigilant half of the variant-conditioned
#     un-reopen target) `83e33dde` (no longer an ancestor after the
#     rebase-merge; reachable rebased commit `b7bc32ea`): fast +13,
#     smoke +13, no module change.
#     The task's delta is measured at its LAST commit, `83e33dde`, not at
#     the red-lane cure `57e13284` one commit earlier (no longer an
#     ancestor after the rebase-merge; reachable rebased commit
#     `0ab97756`): that earlier tip collects 5885 fast / 4258 smoke, and
#     `83e33dde`'s single added node in `tests/test_cure_invalidates.py`
#     (the vigilant supervisor over a real git workspace) takes it to
#     5886 / 4259, thirteen above task 007's tip `c1fb6ebd` (5873 / 4246).
#   - 009 (B.5, reconciled invalidates cascade documented) `eca85160`
#     (no longer an ancestor after the rebase-merge; reachable rebased
#     commit `221cf492`): +0/+0 (record-and-prose only -- `SPEC.md`,
#     `docs/architecture.md`, `state.insert_cure_tasks`'s disclosure
#     paragraph, and a docstring added to an existing
#     `tests/test_cure_invalidates.py` test; no test function added, so
#     the tip collects the same 5886 / 4259 as 008's).
#   - 010 (C.1, legible-verdict-with-unusable-findings discrimination)
#     `c07b90e1` (no longer an ancestor after the rebase-merge; reachable
#     rebased commit `e2953198`): fast +1, smoke +1, no module change.
#   - 011 (C.2, review_findings_unusable pause edge) `f17cc7bd` (no longer
#     an ancestor after the rebase-merge; reachable rebased commit
#     `2a0ee098`): fast +1, smoke +1, no module change.
#   - 012 (C.3, schema-slip pin inversion) `4bc00afe` (no longer an
#     ancestor after the rebase-merge; reachable rebased commit
#     `66bcb926`): +0/+0 (inverted an existing pin, added none).
#   - 013 (A.2, operator answer channel -- `ralphctl rule`, `POST /rule`,
#     hub textarea, ruling record -- plus the root-owned task-record mount
#     isolation fix) `bf5c109b` (no longer an ancestor after the
#     rebase-merge; reachable rebased commit `36df53d4`): fast +38
#     (5 new modules), smoke +34 (4 new modules) -- the largest single
#     jump, matching the size of the new surface (a new CLI verb, a new
#     route, a new hub control, a new record kind).
#   - 014 (A.1, `operator_input_needed` pause before an approach is
#     archived) `5b4d9bbc` (no longer an ancestor after the rebase-merge;
#     reachable rebased commit `2405940e`): fast +20 (1 new module),
#     smoke +20 (1 new module).
#   - 015 (A.4, review re-asked once with the ruling as an authoritative
#     clause) `3c11ef42` (no longer an ancestor after the rebase-merge;
#     reachable rebased commit `1c09adea`): fast +6 (1 new module),
#     smoke +6 (1 new module).
#   - 016 (A.5/A.6, operator-ruling re-park bound to one per ruling,
#     optional reviewer-raised gate field) `335583e5` (no longer an
#     ancestor after the rebase-merge; reachable rebased commit
#     `a4b2b289`): fast +8 (1 new module), smoke +8 (1 new module).
#   - 017 (A.3/A.7, shared discretionary trigger-condition gate, escalation
#     payload; reworked once after a validation rejection named in this
#     module's own task-history above) `5443d9b0` (no longer an ancestor
#     after the rebase-merge; reachable rebased commit `492962cd`):
#     fast +21 (2 new modules), smoke +21 (2 new modules).
#   - 018 (D.1, third `_terminal_success_blockers` blocker for
#     non-terminal review-cure tasks) `e069ecdf` (no longer an ancestor
#     after the rebase-merge; reachable rebased commit `d4be764b`):
#     fast +2, smoke +2, no module change.
#   - 019 (D.2, `review_cure_pending_at_verified` edge actually taken for
#     a run-ending REFUTED remedy) `fec9f84e` (no longer an ancestor after
#     the rebase-merge; reachable rebased commit `f70178d3`): fast +3,
#     smoke +3, no module change.
#   - 020 (D.3, prompt-only un-scoping of the REFUTED remedy cure-task
#     paragraph) `b1a9ea23` (no longer an ancestor after the rebase-merge;
#     reachable rebased commit `944ccc60`): +0/+0 (prose, no new test).
#   - 021 (G, `produced_traffic` derived from `billable_tokens` at all
#     three sites) `5358d3df` (no longer an ancestor after the
#     rebase-merge; reachable rebased commit `59de116d`): fast +5,
#     smoke +0 (the new nodes are fast-only engine-retry/fault-explanation
#     probes, none marked smoke), no module change.
#   - 022 (F.1, `PlanIngestionResult.warnings` surfaced at the accepted-
#     proposal site) `d4c54c7a` (no longer an ancestor after the
#     rebase-merge; reachable rebased commit `97f5e170`): fast +2
#     (1 new module), smoke +2 (1 new module).
#   - 023 (F.2, `orderingOnly` whole-dependsOn-set warning) `f6dfec97`
#     (no longer an ancestor after the rebase-merge; reachable rebased
#     commit `a962e2e0`): fast +4, smoke +4, no module change.
#
# Every rebased sha named above (`1fc9291f`, `6c09d625`, `b63f5aa6`,
# `abcd991d`, `4090a785`, `d951a82e`, `0ab97756`, `b7bc32ea`, `221cf492`,
# `e2953198`, `2a0ee098`, `66bcb926`, `36df53d4`, `2405940e`, `1c09adea`,
# `a4b2b289`, `492962cd`, `d4be764b`, `f70178d3`, `944ccc60`, `59de116d`,
# `97f5e170`, `a962e2e0`) is confirmed an ancestor of `origin/main` with
# `git merge-base --is-ancestor`, and each one's `git diff <sha>^..<sha>
# --stat` matches its original counterpart's exactly (residual R01,
# task 030).
# The twenty-two deltas above sum to exactly the totals this raise applies:
# fast +140 tests / +11 modules, smoke +128 tests / +10 modules -- 5857+140
# = 5997, 387+11 = 398, 4233+128 = 4361 (smoke's own prior-wave base, from
# the GH #208 entry above, not the 4300 ceiling it was measured 67 under),
# and 271+10 = 281.
#
# Measured directly with `.venv/bin/python -m pytest -m fast --collect-only
# -q` and `-m smoke --collect-only -q` at this commit's own tip (task 024):
# `-m fast` 5997/6130 tests collected (133 deselected) over 398 modules,
# `-m smoke` 4361/6130 (1769 deselected) over 281 modules. All four numbers
# sit exactly on the measured count, headroom for NOTHING further -- the
# same choice every entry above made. Confirmed against
# `_modules(collected(marker))`, this module's own helpers, not a raw path
# count.
#
# Ratcheted again at task 001 (v0.24, requirement I.1): +1 module and +3 tests
# for `tests/test_task_totals_approach_scope.py` (fast 5997/398 -> 6000/399,
# smoke 4361/281 -> 4364/282), re-measured with this module's own
# `collected()`/`_modules()` helpers at this commit's tip. This is the ratchet
# moving for legitimately added tests, not a raised safety ceiling.
#
# Ratcheted again at task 004 (v0.24, requirement J.1): +0 modules, +2 tests
# in the EXISTING `tests/test_ruling_re_park_bound.py` (the two A.5 bound
# questions' own tracked tests, `test_A5_second_ruled_rejection_cannot_use_
# unspent_reopen_gate`/`test_A5_same_ruling_bound_survives_actual_replan`,
# both stamped `fast`+`smoke` by `pytest_collection_modifyitems` since the
# module never crosses a process boundary), fast 6000/399 -> 6002/399, smoke
# 4364/282 -> 4366/282, re-measured with this module's own `collected()`/
# `_modules()` helpers at this commit's tip.
#
# Ratcheted again at task 005 (v0.24, requirement J.2): +0 modules, +2 tests
# in the EXISTING `tests/test_discretionary_gate.py` (the two A.3 trigger
# questions' own tracked tests, `test_A3_repeated_insert_only_is_still_
# explicit_nontrigger`/`test_A3_discretionary_gate_renews_after_actual_
# replan`, both stamped `fast`+`smoke` by `pytest_collection_modifyitems`
# since the module never crosses a process boundary), fast 6002/399 ->
# 6004/399, smoke 4366/282 -> 4368/282, re-measured with this module's own
# `collected()`/`_modules()` helpers at this commit's tip.
#
# Task 007 (requirement A.2, #169): one new `fast`+`smoke`-stamped node in
# the EXISTING `tests/test_deadline.py` (`test_deadline_expiring_mid_
# flight_leaves_the_iterations_own_outcome_untouched`) -- fast 6004/399 ->
# 6005/399, smoke unchanged at 4368/282 (the new node is not itself smoke-
# marked; module count on both tiers is unchanged), re-measured with this
# module's own `collected()`/`_modules()` helpers at this commit's tip.
# Task 008 (v0.24 requirement B.1, #169): one new module,
# `tests/test_timed_out_state.py`, `fast`+`smoke`-stamped throughout (it
# never crosses a process boundary -- `call_ralphctl_in_process` only, no
# literal `RALPHCTL`/`RALPHD_ENGINE`/real-engine spawn) -- fast 6005/399 ->
# 6020/400, smoke 4368/282 -> 4381/283, re-measured with this module's own
# `collected()`/`_modules()` helpers at this commit's tip.
# Task 008's second attempt (same requirement B.1, #169) replaced three
# `tests/test_timed_out_state.py` nodes that pinned `stop`/`rm` REFUSING a
# `timed_out` run with six that pin the acceptance the requirement actually
# asks for (+3 nodes, module count unchanged) -- fast 6020 -> 6023, measured
# with the same helpers; smoke (4373) stays inside its existing ceiling.
# Task 010 (v0.24 requirement B.3, #169) added two nodes to
# `tests/test_deadline.py` (already-counted module, +2 nodes) pinning
# `_finish_timed_out()`'s freeze-on-the-spot entry path -- fast 6023 ->
# 6025, measured with this module's own `collected()` helper at this
# commit's tip. `test_deadline.py`'s whole module is fast-only (its
# Section D/E `live(` fixture usages trip `_module_is_in_process`'s
# source-level scan for every node in the file), so smoke (4373) is
# unaffected and stays inside its existing 4381 ceiling.
# Task 011 (v0.24 requirement C.1, #169) added ONE more node to the same
# already-counted `tests/test_deadline.py` (Section G) driving a real
# `_step_worker` budget exhaustion through `run_job()` onto the same
# `_finish_timed_out()` path -- fast 6025 -> 6026, measured the same way.
# Still fast-only (same module-wide `live(` scan trip), smoke unaffected.
# Task 012 (v0.24 requirement C.2, #169) added TWO more nodes to the same
# already-counted `tests/test_deadline.py` (Section H) pinning that
# `_step_replan`'s own `max_approaches` check and `_step_review`'s
# post-rejection twin both still reach `failed`, unchanged by task 011 --
# fast 6026 -> 6028, measured the same way. Still fast-only (same
# module-wide `live(` scan trip), smoke unaffected.
# Task 013 (v0.24 requirement D.1, #169) added ONE more node to the same
# already-counted `tests/test_deadline.py` (new Section I) pinning that a
# passing verdict rendered after the deadline still concludes the run
# `succeeded`, never `timed_out` -- fast 6028 -> 6029, measured the same
# way. Still fast-only (same module-wide `live(` scan trip), smoke
# unaffected.
# Task 018 (v0.24 requirement F.2, #46) added ONE node to the already-
# counted `tests/test_cli_auto_resume_guard.py` (in-process CLI, so it
# counts in both tiers) pinning that an `auto_resume`-enabled `timed_out`
# run is never resumed by `doctor --fix` -- fast 6030 -> 6031, smoke 4368 ->
# 4369 (well inside its existing 4381 ceiling, no smoke bump needed),
# measured with `.venv/bin/python -m pytest -q -m <tier> --collect-only`.
# Task 019 (v0.24 requirement G, #169) added three nodes pinning the new
# `Deadline`/`Remaining wall clock` lines in every hat's `## Job context`
# (`tests/test_approach_progress_context.py`,
# `tests/test_review_clone_protocol.py`,
# `tests/test_planning_reflection_additions.py`) -- fast 6031 -> 6034 (still
# 400 modules, no new module), smoke 4369 -> 4372 (well inside its existing
# 4381 ceiling, no smoke bump needed), measured with
# `.venv/bin/python -m pytest -q -m <tier> --collect-only`.
# Task 021 (v0.24 requirement K.1, #169) added TWO new in-process modules
# (no `live(`/process-boundary usage in either, so both count in smoke too):
# `tests/test_plan_ingestion_probe_audit_lint.py` (the probe-audit lint's
# flagged/clean proposals) and `tests/test_probe_audit_prompt_shape.py`
# (the two prompt additions) -- fast 6034 -> 6050 tests over 400 -> 402
# modules, smoke 4372 -> 4388 tests over 282 -> 284 modules, measured with
# `.venv/bin/python -m pytest -q -m <tier> --collect-only` at this task's
# own tip.
# Task 022 (v0.24 requirement K.2, #169) added ONE new in-process module
# (`tests/test_mid_plan_review_episode.py`, two nodes, no `live(`/process-
# boundary usage, so it counts in smoke too) -- fast 6050 -> 6052 tests over
# 402 -> 403 modules, smoke 4388 -> 4390 tests over 284 -> 285 modules,
# measured the same way at this task's own tip. Its cure (same task, same
# requirement) added a THIRD node to that same module, pinning that the
# one-per-approach marker is written at dispatch and so survives an episode
# cancelled mid-flight -- fast 6052 -> 6053, smoke 4390 -> 4391, still 403/
# 285 modules, measured the same way.
# Task cure-01-01 (v0.24 requirement K.2, #169; review finding R7) added THREE
# nodes to that same already-counted `tests/test_mid_plan_review_episode.py`
# (`test_admitted_mid_plan_cure_updates_the_persisted_cure_counters`,
# `test_exhausted_run_wide_cure_budget_admits_no_mid_plan_cure_task`,
# `test_incurable_blocking_finding_admits_no_mid_plan_cure_task` -- the
# exhausted/incurable/admitted probes the finding's `cured_when` asks for),
# all `fast`+`smoke`-stamped since the module never crosses a process
# boundary -- fast 6053 -> 6056 tests, smoke 4391 -> 4394 tests, both module
# ceilings unchanged at 403/285 (no module added). Measured with this
# module's own `collected()`/`_modules()` helpers at this commit's own tip:
# `-m fast` 6056 tests over 403 modules, `-m smoke` 4394 over 285. Both
# raised literals sit exactly on the measured count, headroom for NOTHING
# further, the same choice every entry above made. This is the ratchet
# moving for legitimately added tests under operator ruling 001 ("a tier
# ratchet is not a budget ceiling", 2026-09-22), not a raised safety
# ceiling and not a weakened guard: no test was retired, skipped,
# de-marked or weakened to buy room.
# Task cure-01-03 (v0.24 requirement A.1/B.3, DoD 1-3, K.2, #169) added FOUR
# nodes, both parametrized pairs, to two already-counted modules: the
# already-expired-boundary no-dispatch probe for the ordinary run-ending
# review (`tests/test_deadline.py`'s new Section Jb,
# `test_no_new_review_iteration_starts_at_an_already_expired_boundary
# [deadline|iterations]`, fast-only like its whole module) and the same
# boundary at the mid-plan review episode's own call site
# (`tests/test_mid_plan_review_episode.py::test_no_mid_plan_review_episode_
# starts_at_an_already_expired_boundary[deadline|iterations]`,
# `fast`+`smoke` like its whole module) -- fast 6057 -> 6061 tests, smoke
# 4394 -> 4396 tests, both module ceilings unchanged at 403/285 (no module
# added). Measured with `.venv/bin/python -m pytest -q -m <tier>
# --collect-only` at this task's own tip. Same ratchet under operator
# ruling 001 as every entry above: no test retired, skipped, de-marked or
# weakened to buy the room.
# Task cure-01-06 (v0.24 requirement I.1, DoD 10, #169) added TWO nodes to
# an already-counted module, `tests/test_task_totals_approach_scope.py`
# (`test_pending_task_shows_zero_before_any_dispatch` and
# `test_new_run_pending_task_shows_zero_before_any_dispatch_via_run_detail`
# -- the measured-zero-before-any-dispatch probe the review finding named,
# plus the same case through the real `run_detail` surface), neither
# crossing a process boundary so both sort `fast`+`smoke` like the rest of
# that module -- fast 6070 -> 6072 tests, smoke 4401 -> 4403 tests, both
# module ceilings unchanged at 404/286 (no module added). Measured with
# `.venv/bin/python -m pytest -q -m <tier> --collect-only` at this task's
# own tip. Same ratchet under operator ruling 001 as every entry above: no
# test retired, skipped, de-marked or weakened to buy the room.
TIER_CEILINGS: dict[str, dict[str, int]] = {
    # cure-01-03 (v0.24 requirement A.1/B.3/K.2): fast 6057 -> 6061, smoke
    # 4394 -> 4396, +4 tests (0 new modules) for the two already-expired-
    # boundary no-dispatch probes described directly above.
    # cure-01-04 (v0.24 requirement D.1/D.2, operator rulings 3-5, DoD 5):
    # fast 6061 -> 6064, smoke unchanged (+3 tests, 0 new modules) for the
    # two enabled-gate D.1/D.2 probes plus their in-budget control, all in
    # tests/test_deadline.py -- a module that already carries a real
    # `LiveRun`/`engine_factory` process-boundary test further down, so
    # every node in it (old and new alike) sorts `fast`-only, never smoke.
    # cure-01-04, second attempt (same requirements): fast 6064 -> 6065,
    # smoke unchanged (+1 test, 0 new modules) for the D.1 hold/resume probe
    # that measures where a held-at-the-peer-gate run resumes and proves the
    # peer gate itself still renders a verdict there -- same module.
    # cure-01-05 (v0.24 requirement B.1, DoD 3): fast 6065 -> 6070, smoke
    # 4396 -> 4401 (+5 tests, +1 module each) for
    # tests/test_hub_infra_wait_timed_out.py -- the runtime probe that
    # evals app.js's real `renderInfraWait` under Node and pins a held
    # `timed_out` run's rendering, plus its unchanged running-outage and
    # terminal-degraded control cases. No `docker`/`browser`/process-
    # boundary fixture, so it sorts into both tiers, not fast-only.
    # cure-01-01-2 (v0.24 requirement A.1/B.3, DoD 1-3): fast 6072 -> 6074,
    # smoke 4403 -> 4405 (+2 tests, 0 new modules) for the already-
    # expired-boundary no-dispatch probe (parametrized deadline/
    # iterations) on `_maybe_run_midrun_adjudication_episode`'s OTHER
    # dispatch site -- tests/test_midrun_adjudication_episode.py, a
    # module that already sorts into both tiers (no process-boundary
    # fixture), so both ceilings move together.
    # cure-01-02-2 (v0.24 requirement B.1): fast 6074 -> 6077, smoke 4405
    # -> 4408 (+3 tests, 0 new modules) for the CLI-status analogue of
    # cure-01-05's app.js fix -- `_format_degraded_lines`'s own `timed_out`
    # branch, tests/test_cli_status_degraded.py, a module that already
    # sorts into both tiers (no process-boundary fixture), so both
    # ceilings move together.
    # task 001 (v0.25 requirement A.1/A.2/A.3/A.4, #216/#217): fast 6077 ->
    # 6082, smoke 4408 -> 4413 (+5 tests, +1 module each) for
    # tests/test_lock_acquire_retry.py -- the bounded-retry `acquire_lock`
    # probes (named-constant justification, no-sleep happy path, the two
    # A.4 directions, and an in-process `amain()` end-to-end check). No
    # `docker`/`browser`/process-boundary fixture (every `amain()` call is
    # in-process, like tests/test_engine_budget_seed.py's), so it sorts
    # into both tiers. Measured with `.venv/bin/python -m pytest -q -m
    # <tier> --collect-only` at this commit's own tip.
    # task 004 (v0.25 requirement B.1, #215): fast 6082 -> 6083, smoke
    # 4413 -> 4414 (+1 test, +1 module each) for
    # tests/test_startup_watchdog_verdict_escalation.py -- the deliberately
    # SIGINT-ignoring-child probe proving the no-traffic verdict path now
    # escalates to SIGKILL through SHUTDOWN_GRACE_S instead of falling
    # through to the outer timeout_s wait. No `docker`/`browser`/process-
    # boundary fixture (it drives `PiRunner.run` directly, like
    # tests/test_startup_watchdog_cpu_progress.py's own nodes), so it sorts
    # into both tiers. Measured with `.venv/bin/python -m pytest -q -m
    # <tier> --collect-only` at this commit's own tip.
    # task 006 (v0.25 requirement C.1/C.2, #211): fast 6083 -> 6085, smoke
    # 4414 -> 4416 (+2 tests, +1 module each) for
    # tests/test_reflect_engine_cutoff.py -- the two `_iteration_cut_
    # short_by_engine` reflect-gate probes (a tail-timeout kill that skips
    # the ladder/agent_fault path entirely, and the unchanged ordinary
    # success path). No `docker`/`browser`/process-boundary fixture (pure
    # `asyncio.run(sup._run_reflection())` against a stubbed runner, like
    # tests/test_reflect_tail_budget.py's own nodes), so it sorts into
    # both tiers. Measured with `.venv/bin/python -m pytest -q -m <tier>
    # --collect-only` at this commit's own tip.
    # task 007 (v0.25 requirement C.2/C.3/C.4, #211): fast 6085 -> 6094,
    # smoke 4416 -> 4425 (+9 tests, +1 module each) for the C.2 drop of
    # `report_written`/`suggestions_written` from the `reflect` contract:
    # 2 new nodes in tests/test_response_contracts.py (a historical
    # both-booleans payload still validates clean; an empty `{}` reflect
    # payload validates clean now that nothing is required) plus the new
    # tests/test_reflect_derived_facts.py (7 nodes: the four present/
    # absent file combinations `_reflect_derived_facts()` tracks, a
    # missing-report failure and a report-present success with an empty
    # contract, and the historical-payload end-to-end case). No
    # `docker`/`browser`/process-boundary fixture anywhere in the new set
    # (pure `make_supervisor`/`_reflect_verdict`/`_reflect_failure` calls,
    # like tests/test_reflect_tombstone_cleared.py's in-process tier),
    # so all 9 sort into both tiers. Measured with `.venv/bin/python -m
    # pytest -q -m <tier> --collect-only` at this commit's own tip.
    # task 008 (v0.25 PRD C.3): fast 6094 -> 6095, smoke unchanged (+1 test,
    # 0 new modules) for the new degraded-with-files-on-disk tombstone probe
    # in tests/test_reflect_tombstone_cleared.py -- that whole module already
    # has a real process-boundary fixture (`subprocess.run` via `_ctl`), so
    # `_module_is_in_process()` sorts every node in it fast-only, never
    # smoke, exactly like its 8 pre-existing nodes. Measured with
    # `.venv/bin/python -m pytest -q -m <tier> --collect-only` at this
    # commit's own tip.
    # task 010 (v0.25 requirement D.1, #265): fast 6095 -> 6098, smoke 4425
    # -> 4428 (+3 tests, 0 new modules) for the three D.1/D.4 probes in
    # tests/test_review_clone_protocol.py -- a value with arguments accepted
    # by the flag/key, that same value surviving start -> job.yaml ->
    # resume -> rendered prompt unchanged, and an existing bare-path value
    # still rendering exactly as before. No `docker`/`browser`/process-
    # boundary fixture in the new nodes (the `ctl` fixture drives
    # `ralphctl` in-process against a stub docker script, like the module's
    # own pre-existing `start`/template nodes), so all three sort into both
    # tiers. Measured with `.venv/bin/python -m pytest -q -m <tier>
    # --collect-only` at this commit's own tip.
    # task 012 (v0.25 requirement E.1/E.2, #212): fast 6098 -> 6106, smoke
    # 4428 -> 4436 (+8 tests, 0 new modules) for the bare-id anchor's #212
    # three-row parametrized table (test_212_three_row_table_all_resolve_
    # against_task_ids, 3 cases) plus five history-event/no-op nodes, all
    # in tests/test_petitions.py. No `docker`/`browser`/process-boundary
    # fixture in the new nodes (in-process `RunDir`/`petitions.open_
    # petitions_in_workspace` calls, like the module's own pre-existing
    # history-filter nodes), so all eight sort into both tiers. Measured
    # with `.venv/bin/python -m pytest -q -m <tier> --collect-only` at
    # this commit's own tip.
    # task 013 (v0.25 requirement E.3/E.4/E.5, #212): fast 6106 -> 6115,
    # smoke 4436 -> 4445 (+9 tests, 0 new modules) for the out-of-
    # vocabulary Type refusal (logger + event, plus the in-vocabulary
    # no-op control), the zero-entries-but-looks-like-a-petition warning
    # (plus its genuinely-empty no-op and its suppression when a Type
    # refusal already fired for the same section) and the recorded-paths
    # event (never-opened, opened-and-empty, and the review-context
    # adjudication-point path), all in tests/test_petitions.py. Every new
    # node is an in-process RunDir/petitions.parse_petitions/open_
    # petitions_in_workspace/petitions_review_context call with no
    # docker/browser/process-boundary fixture, so all nine sort into
    # both tiers, exactly like the module's own pre-existing nodes.
    # Measured with `.venv/bin/python -m pytest -q -m <tier>
    # --collect-only` at this commit's own tip.
    # task 014 (v0.25 requirement E.7, #212): fast 6115 -> 6121, smoke 4445
    # -> 4451 (+6 tests, +1 module each) for tests/test_verify_petition_
    # awareness_decision.py -- the decision-record pins (the decline
    # heading's own decision line, its #212 measured-cost citation, its
    # ordering-guarantee citation, and the same three echoed off `_verify_
    # task`'s own docstring). No `docker`/`browser`/process-boundary
    # fixture (a plain `docs/petition-format.md` grep plus `inspect.
    # getdoc(LoopSupervisor._verify_task)`), so the new module sorts into
    # both tiers. Measured with `.venv/bin/python -m pytest -q -m <tier>
    # --collect-only` at this commit's own tip.
    # task 016 (v0.25 requirement F.1/F.3, #216): fast 6121 -> 6124, smoke
    # 4451 -> 4454 (+3 tests, +1 module each) for tests/test_flowlib_engine_
    # exit_wait.py -- the new `wait_engine_exit` helper's own two synthetic-
    # hang probes (a hung child process and a parked-run status, each
    # asserting the correct shape name plus all four dump sections) plus
    # the happy-path exit-code control. No `LiveRun`/`engine_factory`/
    # `RALPHD_ENGINE`/process-boundary fixture (a bare `subprocess.Popen`
    # sleeper against a hand-written run dir, like tests/flows/test_
    # flowlib_status_barrier.py's own fake-run nodes), so all three sort
    # into both tiers. Measured with `.venv/bin/python -m pytest -q -m
    # <tier> --collect-only` at this commit's own tip.
    # task 017 (v0.25 requirement F.2, #216): fast 6124 -> 6128, smoke
    # unchanged at 4454 (+4 tests, +1 module fast-only) for the new
    # tests/test_engine_exit_wait_wiring.py guard (the real check over
    # tests/, the F.4 cure-pass-shape pin, and two allow/deny mutation
    # nodes). This module's OWN source literally spells `engine_factory`
    # inside its two synthetic mutation fixtures' planted text (task 016's
    # own gotcha, restated), so `_module_is_in_process` reads it as
    # crossing a process boundary and sorts every node fast-only, never
    # smoke -- confirmed directly (`-m smoke --collect-only` on this file
    # alone: 0 collected, 4 deselected). Measured with `.venv/bin/python -m
    # pytest -q -m <tier> --collect-only` at this commit's own tip.
    "fast": {"tests": 6128, "modules": 411},
    "smoke": {"tests": 4454, "modules": 292},
}

# The budget table: one row per *lane* (a tier plus the recipe that runs it).
# No `tests`/`modules`/`budget tests`/`budget modules` columns -- those moved
# out of this doc entirely (v0.21 task 062/063, requirement N.4); this table
# now carries only the wall-clock half, which is a recorded measurement
# against a recorded budget, not a live collected count.
BUDGET_COLUMNS = ("lane", "marker", "recipe", "wall clock",
                  "budget wall clock", "PRD target", "target met?")

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# A cell that deliberately states "no target of this kind for this lane".
_NO_TARGET = {"-", "--", "\u2014", "n/a", "none"}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(set(c) <= set("-: ") and c for c in cells)


def parse_budget_table(text: str) -> list[dict[str, str]]:
    """Every row of the budget table, as `column name -> raw cell text`.

    The table is found by its header, not by position, so prose may move
    around it freely.
    """
    rows: list[dict[str, str]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        header = _cells(line)
        if [c.lower() for c in header] != [c.lower() for c in BUDGET_COLUMNS]:
            continue
        for following in lines[index + 1:]:
            if not following.lstrip().startswith("|"):
                break
            cells = _cells(following)
            if _is_separator(cells):
                continue
            if len(cells) != len(BUDGET_COLUMNS):
                pytest.fail(f"budget table row has {len(cells)} cells, "
                            f"expected {len(BUDGET_COLUMNS)}: {following!r}")
            rows.append(dict(zip(BUDGET_COLUMNS, cells)))
        break
    return rows


def _number(cell: str) -> float | None:
    match = _NUMBER_RE.search(cell.replace(",", ""))
    return float(match.group(0)) if match else None


def budget_violations(text: str) -> list[str]:
    """Every lane whose recorded measurement exceeds its declared budget.

    The whole discriminator of this module, kept as a plain function over the
    doc's text so a mutated table can be checked without touching the file.
    """
    problems: list[str] = []
    for row in parse_budget_table(text):
        lane = row["lane"]
        for measured_col, budget_col in (("wall clock", "budget wall clock"),):
            measured, budget = _number(row[measured_col]), _number(row[budget_col])
            if measured is None or budget is None:
                problems.append(f"{lane}: {measured_col}/{budget_col} carries no "
                                f"number ({row[measured_col]!r} / {row[budget_col]!r})")
                continue
            if measured > budget:
                problems.append(
                    f"{lane}: measured {measured_col} {row[measured_col]!r} exceeds "
                    f"the declared budget {row[budget_col]!r}")
    return problems


def _collect(marker: str) -> list[str]:
    """The node ids `-m <marker>` selects right now, from pytest itself."""
    env = {k: v for k, v in os.environ.items() if k != "RALPHD_NOTIFIER_URL"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-m", marker],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=600)
    assert proc.returncode == 0, (
        f"collect-only for -m {marker} failed ({proc.returncode}):\n"
        f"{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}")
    return [line.strip() for line in proc.stdout.splitlines() if "::" in line]


_COLLECTED: dict[str, list[str]] = {}


def collected(marker: str) -> list[str]:
    if marker not in _COLLECTED:
        _COLLECTED[marker] = _collect(marker)
    return _COLLECTED[marker]


def _modules(node_ids: list[str]) -> set[str]:
    return {node.split("::", 1)[0] for node in node_ids}


# --------------------------------------------------------------------------
# the tiers exist and are wired where the requirement says
# --------------------------------------------------------------------------

def test_both_tier_markers_are_registered_in_pyproject() -> None:
    text = PYPROJECT.read_text()
    for marker in TIER_MARKERS:
        assert re.search(rf'^\s*"{marker}: ', text, re.MULTILINE), (
            f"pyproject.toml does not register the `{marker}` marker; an "
            "unregistered marker is a warning pytest can be configured to "
            "ignore, not a declared tier")


def test_the_recorded_tier_doc_exists_and_names_both_recipes() -> None:
    text = TESTING_DOC.read_text()
    for marker in TIER_MARKERS:
        assert f"-m {marker}" in text, (
            f"docs/testing.md never shows a recipe selecting `-m {marker}`")


def test_the_per_task_run_selects_the_smoke_tier() -> None:
    assert re.search(r"-m\s+'?smoke'?", GUARD_SCRIPT.read_text()), (
        "scripts/final_sha_guards.sh does not select the smoke tier")
    assert re.search(r"-m\s+smoke", VERIFY_PROMPT.read_text()), (
        "the verify prompt does not select the smoke tier for its per-task run")


# --------------------------------------------------------------------------
# the budget guard
# --------------------------------------------------------------------------

def test_the_budget_table_covers_both_tiers() -> None:
    rows = parse_budget_table(TESTING_DOC.read_text())
    assert rows, ("docs/testing.md has no budget table with the columns "
                  f"{BUDGET_COLUMNS}")
    markers = {r["marker"].strip("`") for r in rows}
    assert set(TIER_MARKERS) <= markers, (
        f"the budget table covers {sorted(markers)}, not both tiers")


def test_no_lane_exceeds_its_declared_budget() -> None:
    problems = budget_violations(TESTING_DOC.read_text())
    assert not problems, (
        "docs/testing.md records a tier over its own declared budget:\n"
        + "\n".join(problems))


def test_the_budget_guard_discriminates() -> None:
    """The mutation this guard exists to catch: a measurement pushed past the
    budget beside it. Applied to a copy of the real table, so the check is
    proven on the real column layout rather than on a hand-built fixture."""
    text = TESTING_DOC.read_text()
    assert not budget_violations(text)
    rows = parse_budget_table(text)
    row = rows[0]
    blown = text.replace(
        f"| {row['recipe']} | {row['wall clock']} |",
        f"| {row['recipe']} | 99999 s |", 1)
    assert blown != text, "could not mutate the first budget row"
    problems = budget_violations(blown)
    assert any("wall clock" in p for p in problems), (
        f"a 99999 s wall clock inside a smaller budget went unreported: {problems}")


def test_a_missed_prd_target_is_disclosed_with_its_measured_number() -> None:
    """A target the suite does not meet is stated as missed, with the number.

    The PRD's rule for requirement G: "a missed target is disclosed with its
    number, not hidden".
    """
    text = TESTING_DOC.read_text()
    for row in parse_budget_table(text):
        target = _number(row["PRD target"])
        if target is None:
            assert row["PRD target"].lower() in _NO_TARGET, (
                f"{row['lane']}: PRD target cell {row['PRD target']!r} carries "
                "neither a number nor an explicit 'no target' marker")
            continue
        measured = _number(row["wall clock"])
        verdict = row["target met?"]
        if measured is not None and measured > target:
            assert "MISSED" in verdict.upper(), (
                f"{row['lane']}: measured {row['wall clock']} against a "
                f"{row['PRD target']} target, but the verdict cell says "
                f"{verdict!r}")
            assert _NUMBER_RE.search(verdict), (
                f"{row['lane']}: the miss is declared without its measured "
                f"number: {verdict!r}")
        else:
            assert "MISSED" not in verdict.upper(), (
                f"{row['lane']}: measured {row['wall clock']} is inside its "
                f"{row['PRD target']} target, but the verdict says {verdict!r}")


# `test_the_tier_wide_count_targets_are_met_or_disclosed` stood here until
# v0.21 task 043 retired it as the cure for the fast lane's own red at
# `b0361e1` (gate run 34885356133: "the fast tier collects 5756 tests against
# the PRD's target of 3000, and docs/testing.md never states that measured
# number"). It was satisfiable ONLY by writing this wave's own measured test
# count into `docs/testing.md` as prose -- which v0.21's PRD prohibits ("No
# committed record whose subject is this wave's own work ... not a count
# disclosure") and requirement N.4 deletes outright, with N.9 failing a test
# if such a row comes back. So every test-adding task in this wave would
# otherwise have to break one rule to satisfy the other; the same node forced
# a docs-only cure commit in v0.19.1 and a petition in v0.20.
#
# Surviving cover for what it actually bounded (the collected counts):
#   - `test_the_collected_tier_is_inside_its_declared_budget[fast]` and
#     `[smoke]` below still refuse a tier that grows past the ratcheting
#     ceilings, now `TIER_CEILINGS` above (5844/4300 tests, 390/270 modules;
#     moved out of the doc's "budget tests"/"budget modules" columns by
#     v0.21 task 062, requirement N.4).
#   - `test_a_missed_prd_target_is_disclosed_with_its_measured_number` above
#     still requires a missed PRD target to be stated with its number for
#     every row of the budget table.


# --------------------------------------------------------------------------
# the live half: what the tiers actually collect
# --------------------------------------------------------------------------

@pytest.mark.parametrize("marker", TIER_MARKERS)
def test_the_collected_tier_is_inside_its_declared_budget(marker: str) -> None:
    node_ids = collected(marker)
    assert node_ids, f"-m {marker} selects nothing at all"
    ceiling = TIER_CEILINGS[marker]
    test_budget = ceiling["tests"]
    module_budget = ceiling["modules"]
    assert len(node_ids) <= test_budget, (
        f"the {marker} tier collects {len(node_ids)} tests, over the "
        f"{test_budget} ceiling in TIER_CEILINGS")
    modules = _modules(node_ids)
    assert len(modules) <= module_budget, (
        f"the {marker} tier collects {len(modules)} modules, over the "
        f"{module_budget} ceiling in TIER_CEILINGS")


def test_the_smoke_tier_is_a_subset_of_the_fast_tier() -> None:
    """The tier table's own definition: fast is "smoke + ...", so a smoke test
    the fast tier does not run would make the final-sha lane weaker than the
    per-task one."""
    extra = sorted(set(collected("smoke")) - set(collected("fast")))
    assert not extra, (
        f"{len(extra)} smoke tests are outside the fast tier, e.g. {extra[:5]}")


def test_the_smoke_tier_leaves_out_the_process_boundary_modules() -> None:
    """What makes smoke cheaper than fast: no module that spawns a real
    engine or `ralphctl` process is in it (a real engine, real sleep or
    subprocess spawn attributes ~75 % of the serial wall clock)."""
    smoke_modules = _modules(collected("smoke"))
    assert smoke_modules, "the smoke tier collects no modules"
    offenders = []
    for module in sorted(smoke_modules):
        source = (REPO_ROOT / module).read_text(errors="replace")
        if re.search(r"\bLiveRun\b|\bengine_factory\b|\blive\(|\bRALPHCTL\b", source):
            offenders.append(module)
    assert not offenders, (
        f"smoke-tier modules spawn real engine/CLI processes: {offenders}")
