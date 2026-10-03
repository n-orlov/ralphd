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

# The ratcheting ceilings for the live collected-tier check below: a code
# constant, so it cannot go stale silently. Each value sits on the count
# `.venv/bin/python -m pytest -q -m <tier> --collect-only` measured when it
# last moved. Move a value only with that measurement named in the commit
# message that moves it -- never as history in this comment. A value may be
# lowered to the measured count freely; raising it needs the tests that
# earned the room named in the same message.
TIER_CEILINGS: dict[str, dict[str, int]] = {
    # Task 008: +18 tests / +1 module (tests/test_hub_parked_panel.py),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6232/428, `-m smoke` -> 4553/308.
    # Task 010: +4 tests / +3 modules (tests/test_hub_steer_roundtrip.py,
    # tests/test_hub_rule_roundtrip.py, tests/test_hub_steer_textarea_and_
    # rule_rows.py), measured the same way -> fast 6236/431, smoke 4555/309.
    # Task 010 retry: +1 test (test_hub_steer_textarea_and_rule_rows.py::
    # test_steer_apply_now_is_labelled_with_its_cost) -> fast 6237, smoke 4556.
    # Task 009: +18 tests / +1 module (tests/test_hub_parked_actions.py),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6255/432, `-m smoke` -> 4574/310.
    # Task 009 retry: +7 tests in tests/test_hub_parked_actions.py (byte-
    # for-byte submit body, blank-line-preserving waive compose, Enter in
    # #rule-text), measured the same way -> fast 6262, smoke 4581.
    # Task 012: +5 tests / +1 module (tests/test_render_index.py) plus the
    # ingestion-lint suite's own automatic +1 parametrization for the new
    # src/ralphd/render_index.py source file, measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6268/433, `-m smoke` -> smoke 4587/311.
    # Task 013 retry: +2 tests in tests/test_hub_real_render_cap_whole_call.py
    # (lagging live index, streamed text/tool calls at every index lag) ->
    # fast 6270, smoke 4589.
    # Task 014: -1 test (tests/test_hub_real_render_cap_whole_call.py::
    # test_absent_index_residual_costs_measured_through_the_whole_client_
    # facing_call retired outright -- its own premise, a cheap-but-wrong
    # absent-index render, is cured; the rewritten tests/test_log_merge_
    # bounded_reads.py::test_silent_prelude_case_renders_the_full_
    # renderers_last_two_lines_on_both_real_paths covers the cure without
    # adding a node) -> fast 6269, smoke 4588.
    # Task 014 retry: +6 tests (tests/test_log_merge_bounded_reads.py::
    # test_cold_no_index_legacy_shapes_render_the_exact_full_tail_on_both_
    # hub_surfaces, 3 shapes x 2 hidden-setter variants), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast 6275,
    # `-m smoke` -> smoke 4594.
    # Task 015: +3 tests / +1 module (tests/test_hub_render_index_cache.py:
    # run-dir-untouched control, the absent-index cache's own C.4 bound at
    # both silent-prelude depths, and the cache-key-changes-on-append
    # control), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> fast 6278/434, `-m smoke` -> smoke 4597/312.
    # cure-01-01-2: +10 tests / +1 module (the new dev-layout supplemental-
    # run test module: the default no-argument invocation's primary +
    # supplemental pass, its explicit-tier/targeted/collect-only opt-outs,
    # either-run-nonzero, the SUPPLEMENTAL_TIER_MARKER/header pins, and the
    # fast+docker+browser+leftover collection partition), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6288/435, `-m smoke` -> smoke 4597/312 (unchanged: every new test
    # shells out to a subprocess, so none is `smoke`).
    # cure-01-02: +3 tests, no new module (tests/test_escalation_digest.py:
    # last_approach/incurable digests are evidence-free, repeated-signature
    # digest names the ruling command too), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> fast 6292/435, `-m smoke` ->
    # smoke 4600/312 (every new node runs in-process, so all three count
    # in both tiers).
    # cure-01-02 retry: +7 tests, no new module (tests/test_escalation_
    # digest.py: posted digests' A.7 preview numbers agree with their
    # event, and the incurable digest is bounded), measured `-m fast
    # --collect-only` -> fast 6299/435, `-m smoke` -> smoke 4607/312.
    # cure-01-02 second retry: +3 tests, no new module (tests/test_
    # escalation_digest.py: the last-approach/incurable full escalation
    # text names each finding's cured_when), measured `-m fast
    # --collect-only` -> fast 6302/435, `-m smoke` -> smoke 4610/312.
    # cure-01-03: +6 tests, no new module (tests/test_hub_real_render_cap_
    # whole_call.py: same-size leading-checkpoint rewrite, missing/wrong-
    # typed checkpoint state x4, one append-only-lagging regression guard),
    # measured `-m fast --collect-only` -> fast 6308/435, `-m smoke` ->
    # smoke 4616/312.
    # cure-01-03 retry: +8 tests, no new module (same module: malformed
    # NESTED state in a later checkpoint x7, a passed-in index whose state
    # cannot hydrate), measured `-m fast --collect-only` -> fast 6316/435,
    # `-m smoke` -> smoke 4624/312.
    # cure-01-03 second retry: +13 tests, no new module (same module: a
    # same-size rewrite of a later covered line x4, missing/wrong-typed/
    # inconsistent checkpoint fields x7, a checksum-free version-1 index,
    # the bounded verification window on a large prefix), measured `-m fast
    # --collect-only` -> fast 6329/435, `-m smoke` -> smoke 4637/312.
    # cure-01-04 second retry: +1 test, no new module (tests/test_hub_real_
    # render_cap_whole_call.py: SPEC.md §11.4 and docs/cli.md describe the
    # indexed bounded tail read), measured `-m fast --collect-only` -> fast
    # 6333/435, `-m smoke` -> smoke 4641/312.
    # cure-01-05: +8 tests, no new module (tests/test_hub_parked_actions.py:
    # the panel's own name-field-preservation/no-request-toggle tests replace
    # the retired opposite pin plus 4 more; tests/test_hub_steer_textarea_
    # and_rule_rows.py: the apply-now plan-review-revision disclosure),
    # measured `-m fast --collect-only` -> fast 6341/435, `-m smoke` ->
    # smoke 4649/312.
    # cure-01-05 retry: +1 test, no new module (tests/test_hub_parked_
    # actions.py: the named multi-line parked submission), measured
    # `-m fast --collect-only` -> fast 6342/435, `-m smoke` -> smoke 4650/312.
    # cure-01-01 (VG-01): +5 tests / +1 module (tests/test_confirmation_
    # scope_granted_pass.py: the confirmation section/listed ids/diff
    # range surviving a REFUTED-remedy insertion, the reopen-stays-
    # blocking control, the fresh-supervisor disk reload, two successive
    # remedy insertions, and B.4's no-progress refusal after a remedy),
    # measured `-m fast --collect-only` -> fast 6518/455, `-m smoke
    # --collect-only` -> smoke 4826/332 (every new node runs in-process
    # apart from its own `git` subprocess helpers, which still count in
    # both tiers).
    # Task cure-01-01-4 (petition remedy for cure-01-03 criterion (1)):
    # +6 tests in tests/test_hub_real_render_cap_whole_call.py (out-of-
    # window same-size rewrite/replacement, complete and lagging coverage,
    # preservation across a later append, safe append/index publication),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6348/435, `-m smoke` -> 4656/312.
    # Task cure-01-01-4 retry: +5 tests, no new module (tests/test_hub_real_
    # render_cap_whole_call.py: a non-leading out-of-window rewrite refused
    # across 20 later appends through both writer protocols, a refused
    # hub-cached index, and the two read-snapshot consistency cases),
    # measured -> fast 6353/435, smoke 4661/312.
    # cure-01-01-5: +5 tests, no new module (tests/test_hub_parked_actions.py:
    # the single-trailing-newline waive round trip across 3 prose shapes plus
    # an editable-prose-after-the-fix control; tests/test_hub_parked_panel.py:
    # findings live in the left column, the answer box and actions in the
    # right column), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> fast 6358/435, `-m smoke --collect-only` -> smoke
    # 4666/312.
    # cure-01-02-2: +1 test, no new module (tests/test_hub_real_render_cap_
    # whole_call.py: a 200-line tail spanning two absent-index silent-
    # prelude iterations at depths 1,000/1,700 stays bounded on the
    # SECOND call), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> fast 6359/435, `-m smoke --collect-only` -> smoke
    # 4667/312.
    # cure-01-03-2: +4 tests, no new module (tests/test_hub_real_render_cap_
    # whole_call.py: a same-size out-of-window rewrite that restores its
    # original mtime via os.utime, for full and lagging coverage, a hub-
    # cached index and a later legitimate append), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6363/435, `-m smoke --collect-only` -> smoke 4671/312.
    # cure-01-04-2: +1 test, no new module (tests/test_hub_real_render_cap_
    # whole_call.py: test_a_cold_positive_tail_fallback_can_render_the_
    # entire_backlog_exactly, a cold-run growth-loop fallback that reaches
    # `_TAIL_FULL_ASK_BOUND` and reads the entire backlog for a small
    # positive tail on both surfaces), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> fast 6364/435, `-m smoke
    # --collect-only` -> smoke 4672/312.
    # Cure task cure-01-05-2 (PRD requirement A.1/D.1, DoD 1): +8 tests /
    # +1 module (tests/test_other_escalations_digest.py), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> 6372/436,
    # `-m smoke` -> 4680/313.
    # cure-01-01-6 (B.3-B.4, D.1, DoD 2): +2 tests, no new module (tests/
    # test_hub_parked_actions.py: unchecking removes only the composed
    # line, never a matching substring in an unrelated operator sentence;
    # a second waiver checked after a hand edit between two toggles is
    # separate and removable), measured `.venv/bin/python -m pytest -q -m
    # fast --collect-only` -> 6374/436, `-m smoke` -> 4682/313.
    # cure-01-02-3 (C.4, D.1, DoD 3): +1 test, no new module (tests/
    # test_hub_real_render_cap_whole_call.py: test_unchanged_cached_
    # iteration_is_not_rescanned_after_another_changes, a two-cold-
    # iteration tail across the append/rewrite x disk/api matrix,
    # asserting the unchanged earlier iteration's own cache is reused
    # untouched), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> 6375/436, `-m smoke --collect-only` -> 4683/313.
    # cure-01-02-3 retry: +1 test, no new module (same module: test_spec_
    # and_cli_docs_key_cache_reuse_on_transcript_identity_not_size, the
    # docs name identity, not size, as the cache-reuse key), measured the
    # same way -> fast 6376/436, smoke 4684/313.
    # cure-01-01-7 (B.3-B.4, D.1, DoD 2): +3 tests, no new module (tests/
    # test_hub_parked_actions.py: unchecking the EARLIER of two composed
    # waivers keeps the later one's own separator intact, and removing
    # every composed waiver in either order restores the exact original
    # bytes), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> 6379/436, `-m smoke --collect-only` -> 4687/313.
    # cure-01-01-8 (A.1/D.1, DoD 1): +4 tests / +1 module (tests/
    # test_commit_scan_escalation_digest.py: the task-boundary commit-
    # secret-scan pause's digest carries the run id/pauseReason/approach/
    # spend/hub/answer fields, plus the pre-existing retry/marker
    # coverage copied over unchanged), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> 6383/437, `-m smoke
    # --collect-only` -> 4691/314.
    # cure-01-01-8 retry: +1 test (the same module: a real synthetic-
    # secret hit through _scan_task_boundary_commits never reaches the
    # POSTed digest, and the pause keeps its worker resume routing),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only`
    # -> 6384/437, `-m smoke --collect-only` -> 4692/314.
    # cure-01-01-8 second retry: +1 test (the same module: every answer
    # command the commit-scan digest advertises is run through the real
    # CLI handler and engine route and must release the pause), measured
    # `-m fast --collect-only` -> 6385/437, `-m smoke --collect-only`
    # -> 4693/314.
    # cure-01-01-9 (A.1/A.6, D.1, DoD 1/4): +12 tests / +1 module -- 11 in
    # the new tests/test_pause_ruling_release_sequence.py (every advertised
    # rule/unpause command driven through the real CLI parser + engine
    # route over the ACTUAL pause it answers, for all five ruling-facing
    # reasons) plus 1 in tests/test_commit_scan_escalation_digest.py (the
    # commit-scan hold inherits an earlier rejection signature and becomes
    # rule-releasable too) -- measured `.venv/bin/python -m pytest -q -m
    # fast --collect-only` -> 6397/438, `-m smoke --collect-only` ->
    # 4705/315.
    # cure-01-01-9 retry: +1 test in tests/test_commit_scan_escalation_
    # digest.py (test_user_docs_state_the_conditional_commit_scan_ruling_
    # release: no user doc calls the commit-scan hold universally
    # ruling-proof) -> fast 6398/438, smoke 4706/315.
    # cure-01-01-9 second retry: +13 tests in tests/test_pause_ruling_
    # release_sequence.py (the FULL operator_escalation.escalation text of
    # rejected_last_approach/rejected_incurable/stagnation, both variants,
    # names the unpause-alone alternative and the rule-then-unpause
    # sequence, each advertised route executed; repeated-signature control)
    # -> fast 6411/438, smoke 4719/315.
    # cure-01-01-10 (D.1, DoD 4): +1 test / +1 module -- tests/
    # test_render_index_live_write_cost.py (a real PiRunner probe pinning
    # LiveRenderIndex.flush's own total write cost across an iteration as
    # quadratic in the transcript's line count, not the non-quadratic
    # shape the old architecture doc claimed), measured `.venv/bin/python
    # -m pytest -q -m fast --collect-only` -> 6414/439, `-m smoke
    # --collect-only` -> 4722/316.
    # Render index pending-tool fix: +1 test, no new module (tests/test_
    # render_index.py::TestPendingToolCallAcrossFlushes: a live index stays
    # valid across flushes while a tool call is pending, and the hub's
    # 2-line tail stays bounded), measured `.venv/bin/python -m pytest -q
    # -m fast --collect-only` -> 6415/439, `-m smoke --collect-only` ->
    # 4723/316.
    # Task 001 (A.1, #274): +6 tests / +1 module (tests/
    # test_review_finding_rule_prompt.py: review.md files the broken rule
    # via cured_when, probes as evidence examples, one finding per rule;
    # findings.py's FINDING_SCHEMA field set unchanged), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> 6421/440,
    # `-m smoke --collect-only` -> 4729/317.
    # Task 002 (A.2, #274): +4 tests / +1 module (tests/
    # test_cure_worker_prompt.py: `_worker_assignment_extra`'s "Fixing a
    # review finding" section, present for a review-cure-origin task,
    # absent for a planner-origin task and for no task selected at all),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6425/441, `-m smoke --collect-only` -> 4733/318.
    # Task 003 (A.3, #274): +4 tests / +1 module (tests/
    # test_cure_verify_prompt.py: `_verify_task_extra`'s "Checking the
    # rule, not just the listed cases" section, present for a
    # review-cure-origin task, absent for a planner-origin task and for a
    # steering-origin task), measured `.venv/bin/python -m pytest -q -m
    # fast --collect-only` -> 6429/442, `-m smoke --collect-only` ->
    # 4737/319.
    # Task 004 (B.5 foundation, #274): +6 tests / +1 module (tests/
    # test_cure_lineage.py: the per-cure-pass findings/pre_cure_head
    # lineage record and a reopen's ancestor field), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> 6435/443,
    # `-m smoke --collect-only` -> 4743/320.
    # Task 005 (B.1/B.2/B.3 prompt side, #274): +5 tests / +1 module
    # (tests/test_confirmation_review_section.py: the `## Confirmation
    # review` section built from `cure-lineage.json`), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> 6440/444,
    # `-m smoke --collect-only` -> 4748/321.
    # Task 006 (B.3 enforcement, #274): +7 tests / +1 module (tests/
    # test_confirmation_downgrade.py: the confirmation-review downgrade
    # of an unlinked blocking finding to residual, the `review.
    # finding_downgraded` event, and the `reopens`/`regression`/exempt
    # findings staying blocking), measured `.venv/bin/python -m pytest
    # -q -m fast --collect-only` -> 6447/445, `-m smoke --collect-only`
    # -> 4755/322. Task 006 retry: +5 tests (the same module: the
    # contract-ladder path a `verified` claim takes over a downgraded
    # finding, its two controls, mid-run exclusion, once-per-pass event),
    # measured `-m fast --collect-only` -> 6452/445, `-m smoke
    # --collect-only` -> 4760/322.
    # Task 007 (B.4, #274): +3 tests / +1 module (tests/test_cure_
    # progress_identity.py: the identity-based no-progress comparison on
    # a confirmation review), measured `.venv/bin/python -m pytest -q
    # --collect-only -m fast` -> 6455/446, `-m smoke` -> 4763/323.
    # Task 008 (v0.27 requirement C.1, this run): +2 tests / +1 module
    # (tests/test_gate_pause_before_archive.py: the gate's own pause-
    # before-archive edge parks instead of archiving with the run-wide
    # cure ceiling spent, and still takes the cure edge with a pass
    # left), measured `.venv/bin/python -m pytest -q -m fast --collect-
    # only` -> fast 6457/447, `-m smoke` -> smoke 4765/324.
    # Task 009 (v0.27 requirement C.2, #274): +4 tests / +1 module
    # (tests/test_unpause_one_shot_proceed.py: review's and the gate's
    # own pause each unpaused with no ruling, then the same rejection
    # archives-and-replans once while a different rejection still parks),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # fast 6461/448, `-m smoke` -> smoke 4769/325.
    # Task 010 (v0.27 requirement C.3): +11 tests / +1 module (tests/test_
    # budget_cure_passes.py), measured `-m fast --collect-only` -> fast
    # 6472/449, `-m smoke` -> smoke 4780/326.
    # Task 011 (v0.27 requirement C.4, #275): +4 tests / +1 module (tests/
    # test_escalation_preview_real_edge.py: review's and the gate's own
    # A.7 preview zeroed and naming `--cure-passes` with the ceiling
    # spent, unchanged with a pass left), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> fast 6476/450, `-m smoke` ->
    # smoke 4784/327.
    # Task 012 (D.2, #275): -1 test / -1 module (retired tests/test_render_
    # index_live_write_cost.py, whose whole point -- pinning the old
    # quadratic write-cost shape -- is now the defect this task fixes) / +1
    # test / +1 module (tests/test_render_index_linear_writer.py, the
    # covering replacement, pinning the corrected linear shape) -- net
    # unchanged, measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> fast 6476/450, `-m smoke --collect-only` -> smoke
    # 4784/327.
    # Task 013 (D.1, #275): +2 tests / +1 module (tests/test_tail_whole_
    # call_read_bound.py: PRD Q's canonical fixture bounded on every
    # surface, and the two silent-prelude depths' disclosed residual
    # measured and pinned exactly), measured `.venv/bin/python -m pytest
    # -q -m fast --collect-only` -> fast 6478/451, `-m smoke
    # --collect-only` -> smoke 4786/328.
    # Task 014 (E.2): +3 tests / +1 module (tests/test_writing_a_prd_
    # skill.py: the never-converging open-domain skill entry is present
    # under its heading and the presence check discriminates against the
    # entry's absence), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> fast 6498/452, `-m smoke --collect-only` ->
    # smoke 4806/329.
    # cure-01-01-4 (D.1/D.2 together, DoD 4): +4 tests / +1 module (tests/
    # test_d1_real_pirunner_active_iteration_reads_within_cap.py: the real
    # `PiRunner.run` pump, mid-iteration before EOF, reads within the
    # whole-call cap on both the disk and live surfaces at both named
    # silent-prelude depths -- no process-boundary helper named, so this
    # new module's nodes count in `smoke` too), measured `.venv/bin/python
    # -m pytest -q -m fast --collect-only` -> fast 6502/453, `-m smoke
    # --collect-only` -> smoke 4810/330.
    # cure-01-02-2 (PRD B.3/B.5, DoD 2): +3 tests, no new module (tests/
    # test_confirmation_downgrade.py: a caller that omits listed_cure_
    # task_ids resolves nothing, an unrelated/unresolved reopens id must
    # not bypass the confirmation scope, and _confirmation_review_listed_
    # cure_task_ids reads the lineage), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> fast 6505/453, `-m smoke
    # --collect-only` -> smoke 4813/330.
    # cure-01-03 (PRD B.3, DoD 2): +2 tests, no new module (tests/
    # test_confirmation_downgrade.py: exception classification reads the
    # explicit `always_blocking_exception` field, not incidental "secret"
    # /"absent" text; a unit control on `findings.confirmation_review_
    # exempt` proves both directions), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> fast 6507/453, `-m smoke
    # --collect-only` -> smoke 4815/330.
    # cure-01-04 (PRD B.4-B.5, DoD 2): +2 tests / +1 module (tests/
    # test_gate_confirmation_progress_identity.py: a gate-origin cure
    # pass's sole target reopened, worded differently, on a confirmation
    # review is refused as no-progress; a fresh/unrelated finding after
    # the same gate pass is still granted), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> fast 6509/454, `-m smoke
    # --collect-only` -> smoke 4817/331.
    # cure-01-05 (PRD v0.27 requirement C.4, DoD 3): +4 tests, no new
    # module (tests/test_escalation_preview_real_edge.py: a confirmation
    # review's no-progress refusal zeroes the preview with the ceiling
    # unspent and spent; the gate's own last-approach pause names
    # `"finishing"`, never `"archive_and_replan"`; tests/test_hub_
    # parked_actions.py: the panel labels the new `"finishing"`
    # `nextStepIfUnanswered` value with its own outcome text), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6513/454, `-m smoke --collect-only` -> smoke 4821/331.
    # Task 002 (this wave): +6 tests, no new module (tests/test_gate_
    # pause_before_archive.py: the ceiling-spent park widened to the
    # mixed/all-incurable/all-curable fixtures, the last-approach mixed
    # case, and the pause-disabled 32352c15-parity case; tests/test_
    # unpause_one_shot_proceed.py: a parked mixed rejection's bare
    # unpause takes its own immediate terminal tail on the next repeat),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # fast 6524/455, `-m smoke --collect-only` -> smoke 4832/332.
    # Task 002 retry: +4 doc-guard tests, no new module (tests/test_gate_
    # pause_before_archive.py: SPEC.md's operator_escalation/pauseReason
    # rows and docs/api.md's pauseReason row state the gate pause covers
    # all-curable, mixed and all-incurable sets; no doc keeps the
    # all-curable-only scope), measured the same way -> fast 6528/455,
    # smoke 4836/332.
    # Task 003: +5 tests, no new module (tests/test_escalation_preview_
    # real_edge.py: `test_review_rejection_preview_matches_cure_
    # availability` parametrized over the four fixtures the task names,
    # `test_gate_escalation_mixed_with_ceiling_spent_previews_zero`),
    # measured the same way -> fast 6533/455, smoke 4841/332.
    # Task 004: +2 tests, no new module (tests/test_render_index_linear_
    # writer.py rewritten from one text_delta-only case to three sequence
    # classes -- text_delta_control, matched_tool_pairs, unended_tool_
    # starts), measured the same way -> fast 6535/455, smoke 4843/332.
    # Task 004 retry: +2 tests, no new module (tests/test_render_index_
    # linear_writer.py's TestTailKeepsItsOwnCadenceOnPublishDueCalls),
    # measured the same way -> fast 6537/455, smoke 4845/332.
    # Task 005: +60 tests / +1 module (tests/test_live_logs_prefix_read_
    # bound.py: PRD Q's canonical fixture and both silent-prelude depths,
    # each truncated one line short and to 35/60/83/97% of its own raw
    # line count, both index kinds, both surfaces -- an in-process module,
    # so every new node counts in both tiers), measured `.venv/bin/python
    # -m pytest -q -m fast --collect-only` -> fast 6597/456, `-m smoke
    # --collect-only` -> smoke 4905/333.
    # Task 005 retry: +82 tests, no new module (tests/test_live_logs_prefix_
    # read_bound.py: every case again with the finished iteration's stale
    # collapse_index.json left on disk, the malformed/future_version
    # siblings on the live surface, the render-index flag agreement and
    # /logs handler answer checks), measured the same way -> fast
    # 6679/456, smoke 4987/333.
    # Task 006: +1 test / +1 module (tests/test_docs_no_release_narrative.py:
    # the release/task-chronology regex guard over SPEC.md and docs/cli.md,
    # docs/api.md, docs/architecture.md), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> fast 6680/457, `-m smoke
    # --collect-only` -> smoke 4988/334.
    # VG-01: +5 tests / +1 module (tests/test_one_shot_proceed_cure_edge.py:
    # the one-shot unpause proceed is spent by the next matching rejection
    # on the cure edge too -- an in-process module, so every node counts in
    # both tiers) -> fast 6685/458, smoke 4993/335.
    # VG-02: +9 tests / +1 module (tests/test_escalation_cure_advice_
    # matches_edge.py: the gate's mixed/incurable notifier probe, event
    # text vs delivered digest agreement over three gate shapes, the
    # all-curable and last-approach gate advice, and the review-side
    # mixed and last-approach siblings -- in-process, so every node counts
    # in both tiers), measured the same way -> fast 6689/458, smoke
    # 4997/335.
    # VG-03: +27 tests / +1 module (tests/test_render_index_malformed_
    # state_patch.py: malformed MAIN and TAIL state patches on both
    # surfaces, malformed first full states, the writer's own round trip --
    # an in-process module, so every new node counts in both tiers),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # fast 6707/458, `-m smoke --collect-only` -> smoke 5015/335.
    # VG-01 + VG-02 + VG-03 merged: +41 tests / +3 modules, measured on the
    # merged tree the same way -> fast 6721/460, smoke 5029/337.
    # Task 001 (#277): +3 tests / +1 module (tests/test_hub_steer_now_
    # interrupt.py: now-absent/now-false never touch /interrupt, now:true
    # interrupts a real running iteration and its steer is consumed --
    # spawns a real engine, so it counts in fast only, not smoke),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # fast 6724/461, `-m smoke --collect-only` -> smoke unchanged 5029/337.
    # Task 001 retry: +2 tests (the same module's now:true engine-rejection
    # passthrough, 409 and 400), measured the same way -> fast 6726/461,
    # smoke unchanged 5029/337.
    # Task 002 (#278 B.1/B.4/B.7): +9 tests / +1 module (tests/
    # test_router_task_selection.py -- all in-process LoopSupervisor flows,
    # no real subprocess, so it counts in smoke too), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6735/462, `-m smoke --collect-only` -> smoke 5038/338.
    # Task 002 retry: +2 tests (the same module: plan_review's real edges
    # through _route_with_router, chosen and fallback), measured the same
    # way -> fast 6737/462, smoke 5040/338.
    # Task 003 (#278 B.5/B.6): +9 tests / +2 modules (tests/
    # test_router_model_rung_order.py: the four-rung model_for("router")
    # resolution order; tests/test_cli_route_task_selection.py: the
    # default-on/--no-route-tasks/resume CLI behaviour -- both in-process,
    # no real subprocess, so both count in smoke too), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6746/464, `-m smoke --collect-only` -> smoke 5049/340.
    # Task 004 (#278 B.2/B.3): +2 tests, no new module (tests/test_router.py:
    # the task-selection prompt carries every B.2 candidate field, the
    # deterministic pick, the last worker iteration's outcome and B.3's
    # ordering rule -- in-process, no real subprocess, counts in smoke
    # too), measured `.venv/bin/python -m pytest -q -m fast --collect-only`
    # -> fast 6748/464, `-m smoke --collect-only` -> smoke 5051/340.
    # Task 005 (#276, PRD v0.28 section C): +8 tests, no new module (the
    # REFUTED-remedy marker fold/ladder refusal: tests/test_petitions.py,
    # tests/test_prompt_sentinel_audit.py and tests/test_petitions_
    # review.py all gained nodes, in-process, no real subprocess, so
    # all count in smoke too), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6756/464, `-m smoke --collect-only` -> smoke 5059/340.
    # Task 005 retry: +4 tests, no new module (review.md's adjudication-
    # only block + its render helper and the mid-run prompt in tests/
    # test_petitions_review.py; the selfdev-v027 shape on the inserted
    # remedy task in tests/test_midrun_refuted_continues.py), measured the
    # same way -> fast 6760/464, smoke 5063/340.
    # Task 005 second retry: +1 test, no new module (the overrun post-
    # marker remedy refused on review's own contract ladder, tests/
    # test_petitions.py), measured the same way -> fast 6761/464, smoke
    # 5064/340.
    # Cure task cure-01-01 (PRD B.8/B.3/DoD 5): +5 tests, no new module
    # (tests/test_router.py's task-selection prompt now carries a real
    # ordering-phrase check plus 5 parametrized mutation cases, replacing
    # a tautological assertion), measured the same way -> fast 6766/464,
    # smoke 5069/340.
    # Cure task cure-01-01, second attempt: +37 tests, no new module
    # (the B.8 check is now a whole-line allowlist of vetted scaffolding,
    # proven by 35 sentence-x-position mutations, one more rule-constant
    # mutation and a last-worker-iteration variant of the positive case),
    # measured the same way -> fast 6803/464, smoke 5106/340.
    # Task 001 (#279, PRD v0.29 A.1-A.4): +5 tests / +1 module (tests/
    # test_repoll_backoff.py; the module spawns no real subprocess, an
    # in-process `LoopSupervisor._step_worker()` with a scripted runner
    # like `tests/test_progress_claim_corroboration.py`'s own, so it
    # counts in smoke too), measured the same way -> fast 6808/465,
    # smoke 5111/341.
    # Task 003 (#280, PRD v0.29 B.1-B.3): +8 tests / +1 module (tests/
    # test_cure_sweep_reopen.py; direct `state.insert_cure_tasks`/
    # `plan_ingestion.validate_proposal_shape` calls against an in-memory
    # `RunDir`, no real subprocess, so it counts in smoke too), measured
    # the same way -> fast 6816/466, smoke 5119/342.
    # Task 003 retry: +1 test (the literal-name event-type node in tests/
    # test_cure_sweep_reopen.py), measured the same way -> fast 6817/466,
    # smoke 5120/342.
    # Task 002 (#279, PRD v0.29 A.5): +4 tests / +1 module (tests/
    # test_wait_task_prompt.py; the module builds the worker prompt via
    # `LoopSupervisor.build_prompt("worker")`, no real subprocess, so it
    # counts in smoke too), measured the same way -> fast 6821/467,
    # smoke 5124/343.
    # Task 006: +4 tests / +1 module (tests/test_sweep_shape_rules.py),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6828/469, `-m smoke` -> 5131/345.
    # Task 005 (#281, PRD v0.29 C): +6 tests / +1 module (tests/
    # test_worker_verdict_recovery.py; direct `LoopSupervisor._step_worker()`
    # calls against a hand-built `RunDir`, no real subprocess, so it counts
    # in smoke too), measured the same way -> fast 6834/470, smoke 5137/346.
    # Task 007 (PRD v0.29 G): +11 tests, no new module (tests/
    # test_writing_a_prd_skill.py: one presence test per G.1 item plus one
    # for G.3 and one for G.4, plus a discriminator sanity test; pure text
    # assertions, no subprocess, so counted in smoke too), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> 6845/470,
    # `-m smoke` -> 5148/346.
    # Task 008 (PRD v0.29 F.2-F.3): +1 test, no new module (tests/
    # test_packaging_metadata.py::test_the_declared_version_is_at_least_0_29_0;
    # the module already spawns a real subprocess elsewhere, so it stays out
    # of smoke), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> 6846/470, `-m smoke` -> 5148/346.
    # Cure-01-01 (PRD v0.29 B.2/B.5, DoD 2, #280): +4 tests, no new module
    # (tests/test_cure_invalidates.py: a sweep stays pending through a
    # test-only cure with empty invalidates, through invalidates naming its
    # own path, through a cure that lands no commit at all, and the direct
    # `records_pending_reconciliation` exclusion unit -- pure in-process,
    # no subprocess, so counted in smoke too), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> 6850/470,
    # `-m smoke` -> 5152/346.
    # Cure-01-02 (PRD v0.29 C.1-C.3/F.1): +3 tests, no new module (tests/
    # test_worker_verdict_recovery.py: a schema-invalid verdict beside a
    # valid fenced payload skips shape 1's recovery and falls to the
    # generic schema ladder, ladder exhaustion never emits `worker.
    # verdict_absent`, and a valid-JSON-but-non-dict last line still takes
    # shape 3 -- pure in-process, no subprocess, so counted in smoke too),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6853/470, `-m smoke` -> 5155/346.
    # Cure-01-02 retry: +6 tests, no new module (the same file: a present
    # `verdict` key of ANY value -- null, int, empty string, list -- beside
    # a valid fence skips shape 1, plus a present null with no fence that
    # exhausts or is repaired by the generic schema ladder), measured the
    # same way -> fast 6859/470, smoke 5161/346.
    # G1 (this run): +32 tests / +3 modules (widespread in fast and smoke
    # both), measured `.venv/bin/python -m pytest -q -m fast --collect-only`
    # -> fast 6894/473, `-m smoke` -> smoke 5195/349.
    # G2 (this run): +62 tests / +3 modules (widespread in fast and smoke
    # both), measured `.venv/bin/python -m pytest -q -m fast --collect-only`
    # -> fast 6956/477, `-m smoke` -> smoke 5257/353.
    # G3 (this run): +80 tests / +5 modules (smoke), +80 tests / +5 modules
    # (fast), measured `.venv/bin/python -m pytest -q -m fast --collect-only`
    # -> fast 7036/482, `-m smoke` -> smoke 5336/358.
    # T09 (this run): +104 tests / +1 module in fast and +93 tests / +1
    # module in smoke (tests/test_refused_fault.py, plus refusal cases in
    # test_fault_classifier.py and test_cli_fault_explanation.py), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 7157/484, `-m smoke` -> smoke 5446/360.
    # v0.31 polish: +28 tests / +1 module in fast and +23 tests / +1 module
    # in smoke, measured `.venv/bin/python -m pytest -q -m fast --collect-only`
    # -> fast 7244/488, `-m smoke` -> smoke 5528/364.
    # v0.31 final-review fix (pi-model_not_found case, one more test per
    # tier), measured `.venv/bin/python -m pytest -q -m fast --collect-only`
    # -> fast 7247/488, `-m smoke` -> smoke 5530/364.
    # v0.31.1 (#288): +5 tests / +1 module (tests/test_hub_current_iteration.py),
    # measured by the gate's fast tier -> fast 7252/489, smoke 5535/365.
    "fast": {"tests": 7252, "modules": 489},
    "smoke": {"tests": 5535, "modules": 365},
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
    # The verify prompt is generic across projects, so it names no marker of
    # this repository's own: it selects the project's per-task tier from the
    # measured test commands the Standing rules block records.
    verify = " ".join(VERIFY_PROMPT.read_text().split())
    assert re.search(r"cheapest per-task tier -- the measured test commands "
                     r"the notes' `## Standing rules` block records", verify), (
        "the verify prompt does not select the project's per-task tier for "
        "its per-task run")


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
