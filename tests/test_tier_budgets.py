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
TIER_CEILINGS: dict[str, dict[str, int]] = {
    "fast": {"tests": 6000, "modules": 399},
    "smoke": {"tests": 4364, "modules": 282},
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
