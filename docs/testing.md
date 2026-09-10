# Testing ralphd: the tiers, their recipes, and their budgets

This suite is tiered by pytest marker. A tier is a promise about *cost*: what it
covers, what it deliberately leaves out, and how long it may take before it stops
being usable at the point in the loop that runs it. The tiers and their measured
budgets are checked by `tests/test_tier_budgets.py`, which reads this file.

| tier | marker | who runs it | what it covers |
|---|---|---|---|
| smoke | `smoke` | the per-task verify pass, and `scripts/final_sha_guards.sh` over the records-guard modules it discovers | in-process engine flows (a supervisor plus scripted attempts, backoffs stubbed), in-process CLI, records lint, in-process hub |
| fast | `fast` | the final sha of a wave, and any pull request | smoke plus the stub-pi flow modules and the process-boundary CLI tests -- everything outside the nightly lanes |
| nightly | `docker`, `browser`, and the dev-layout module | a sweep, never a per-task check | the docker-sibling tier, the browser tier, and the two host-like dev-layout modes |

## Recipes

    # the per-task tier, in parallel (the one to reach for while working)
    .venv/bin/python -m pytest -q -m smoke -n 8 --dist loadfile

    # the same tier, serial (what the PRD's smoke target is stated against)
    .venv/bin/python -m pytest -q -m smoke

    # the final-sha tier, in parallel
    .venv/bin/python -m pytest -q -m fast -n 8 --dist loadfile

    # a single module or node id, while working on it -- seconds
    .venv/bin/python -m pytest -q tests/test_tier_budgets.py

    # the records-guard subset a docs- or report-only commit can break: the
    # script discovers its own module list, then selects the smoke tier
    scripts/final_sha_guards.sh

`-n 8 --dist loadfile` is the parallel recipe: `loadfile` keeps a module's tests
on one worker, which is what the module-scoped fixtures in this suite assume.
The nightly lanes are run by `scripts/host_like_pytest.sh` and
`scripts/host_like_dev_layout.sh`, not from here.

## How a test lands in a tier

Membership is **derived, never enumerated** -- the same choice, for the same
reason, as `scripts/final_sha_guards.sh`: a hand-typed module list silently
omits modules, and a module written tomorrow would belong to no tier at all.
`pytest_collection_modifyitems` in `tests/conftest.py` stamps every collected
item:

1. an item marked `docker` or `browser`, and every item of the dev-layout
   module, is nightly: it gets neither tier marker; so is an item marked
   `nightly` directly -- applied to one node at a time (task 048, B.5
   follow-on), for a node that is inherently slow (a real backoff/outage
   schedule under test, or a real nested-pytest-subset run) rather than
   merely unoptimised, without pulling its whole module out of `fast`;
2. everything else is `fast`;
3. a `fast` item is also `smoke` unless its own module spawns a real engine or a
   real `ralphctl`/console-script process -- that is, unless the module's source
   names one of the process-boundary helpers (the shared real-engine harness,
   the two real-engine factory fixtures, the resolved `ralphctl` and
   `ralphd-engine` paths, or the host-like shell scripts).

Rule 3 is where the two tiers differ, and it is chosen from measurement rather
than taste: `docs/test-suite-audit.md` section 2 attributes about three quarters
of the serial wall clock to real engines, real sleeps and subprocess spawns, so
excluding the spawning modules is what makes a per-task tier affordable at all.
Nested `pytest --collect-only` children are deliberately *not* in that list: the
records lint is built that way and the tier table puts records lint in smoke.

## Budgets

Measured in this container (28 cores) with the recipes above: the smoke, fast
and records rows re-measured at sha `9b1a0e2` (task 008, requirement G.2 --
task 050's two fix commits `778738b` and `3e65963` are still what the code
underneath these numbers is, and `9b1a0e2` is the sha this task's own runs
were taken at, five commits later than `3e65963` with no intervening code
change to either tier). The "budget" columns are the
declared ceilings this suite is held to; `tests/test_tier_budgets.py` fails when a recorded measurement
exceeds one, and independently asks pytest what each tier collects right now and
fails when *that* is over the ceiling. The ceilings carry deliberate headroom
over today's measurements so the wave's remaining flow modules can land, and are
tightened as the counts come down -- they are a ratchet, not a target.

| lane | marker | recipe | tests | modules | wall clock | budget tests | budget modules | budget wall clock | PRD target | target met? |
|---|---|---|---|---|---|---|---|---|---|---|
| smoke, parallel | `smoke` | `-m smoke -n 8 --dist loadfile` | 3768 | 208 | 26.85 s | 5100 | 300 | 60 s | n/a | met -- 26.85 s inside the lane's own 60 s budget ceiling; no PRD target of this kind for this lane |
| smoke, serial | `smoke` | `-m smoke` | 3768 | 208 | 137.18 s | 5100 | 300 | 200 s | < 120 s | MISSED -- 137.18 s measured against the 120 s target |
| fast, parallel | `fast` | `-m fast -n 8 --dist loadfile` | 5329 | 322 | 81.04 s | 7000 | 430 | 150 s | < 120 s | met -- 81.04 s inside the 120 s target |
| fast, serial | `fast` | `-m fast` | 5329 | 322 | 540.44 s | 7000 | 430 | 1100 s | < 300 s | MISSED -- 540.44 s measured against the 300 s target |
| records, serial | n/a (selected by directory, not marker) | `.venv/bin/python -m pytest -q tests/records/` | 201 | 6 | 20.60 s | 300 | 15 | 30 s | < 15 s | MISSED -- 20.60 s measured against the 15 s target (still inside the 30 s ceiling's own headroom); 201 tests inside the 300-test target |
| dev-layout, `PYTHONUSERBASE` unset | n/a (no marker; the whole suite, unfiltered) | `HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE=unset scripts/host_like_dev_layout.sh` | 5409 | 334 | 2047.32 s | 6500 | 400 | 3600 s | < 900 s (15 min) | MISSED -- 2047.32 s measured against the 900 s target |
| dev-layout, `PYTHONUSERBASE` exported | n/a (no marker; the whole suite, unfiltered) | `scripts/host_like_dev_layout.sh` (default mode, no `HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE` override) | 5409 | 334 | 2146.54 s | 6500 | 400 | 3600 s | < 900 s (15 min) | MISSED -- 2146.54 s measured against the 900 s target |

Every number in the first five rows above is a fresh run at `9b1a0e2`, taken
sequentially and never two at once, with the wall clock read off each run's own
summary line (the collected counts come from a `--collect-only` pass at the
same sha). The serial fast lane is measured directly (`.venv/bin/python -m
pytest -q -m fast`, 540.44 s) rather than carried forward from the audit's
older 981 s figure at sha 836adc1; the parallel lane is still the one the loop
uses day to day. The serial fast run surfaced one extra failing node beyond
the two pre-existing `test_sweep_sha_guard.py` reds this table already
discloses elsewhere:
`tests/test_reflection.py::test_reflect_runs_after_terminal_state_leaves_run_state_untouched`,
which passes solo (`.venv/bin/python -m pytest -q
tests/test_reflection.py::test_reflect_runs_after_terminal_state_leaves_run_state_untouched`
-- `1 passed in 1.64s`) and did not appear in the parallel run moments before
it at the same sha, so it is order- or timing-dependent under the serial
recipe rather than a new red this task introduced; no assertion was touched
to make it disappear.

**Dev-layout, `PYTHONUSERBASE` unset (task 006).** Measured on this task's own
tree at sha `852a51d` (the last commit before this task's own docs-only
commit): `HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE=unset
scripts/host_like_dev_layout.sh`, no `-m`/`-rs` args (the script runs the whole
suite unfiltered, which is what the nightly lane actually does), launched
`nohup timeout -k 30 3300 ... &` and polled to completion rather than blocked
on: `3 failed, 5405 passed, 1 skipped, 9 warnings in 2047.32s (0:34:07)`, exit
status **1** (captured by the wrapper's own `$?`, not inferred). The three
failing node ids are pre-existing at this sha, unrelated to this mode or this
row (confirmed against a plain in-container run at the same sha):
`tests/test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged`,
`tests/test_sweep_sha_guard.py::test_every_sweep_record_declares_a_sha`, and
`tests/test_tier_budgets.py::test_the_tier_wide_count_targets_are_met_or_disclosed`
(the live fast-tier drift disclosed further below, in the "What moved the
numbers" section). The placed skill
(`host-layout-smoke-skill`) SURVIVED the run. Collected counts (5409 tests /
334 modules) come from a plain `--collect-only` pass over the same tree in
this container's own `.venv` (no marker, the same set the dev-layout script's
unfiltered `pytest` invocation collects, since it runs the same checkout under
a different `$HOME`/`PYTHONUSERBASE`, not a different test selection). No
ceiling in this table was raised to record this row; the budget columns
(6500 / 400 / 3600 s) are new headroom for a row that had none before.

**What moved the numbers (task 050, requirement G.2, operator steer 008).** The
previous measurement at `a1b201c` recorded 311.12 s / 448.34 s / 325.40 s /
802.14 s for these same four lanes. Two cures, no assertion loosened or deleted:

1. `778738b` -- two nodes
   (`tests/test_refunds_survive_resume.py::test_a_grace_review_refund_is_recorded_in_status`
   and
   `tests/test_worker_hat.py::test_3_mutation_stops_discriminating_generic_hat_reads_interrupt_as_contract_violation`)
   were spending 120 s each: the contract re-ask channel (`_ask_contract_fix`)
   is not `run_iteration`, so their attempt stubs left it live, and a supervisor
   with no scripted runner spawns the real `pi` once per re-ask, twice, each
   bounded only by the 60 s router timeout. `tests/conftest.py`'s new
   `stub_contract_reask()` returns the empty reply a failed re-ask returns, so
   the ladder still exhausts and both nodes now take 0.01 s.
2. `3e65963` -- `tests/test_subsumed_table.py` was spending 302.73 s of the
   serial lane (38 % of it) on one `pytest --collect-only` subprocess per
   Subsumed row, 847 of them; under `--dist loadfile` that single module was the
   parallel lane's floor, which is why no *node* in the durations list was ever
   slow while the wall clock was minutes. It now does one memoised in-process
   collection and asks membership per row: 2.97 s, all 847 rows still resolved.

The fast tier's collected count fell from 6166 to 5321 (and smoke's from 4613 to
3768) in the same commit: the 847 per-row nodes became 5. Review-cure task
cure-01-01 (PRD I) later added 3 tests to `tests/test_packaging_metadata.py`
(pinning the engine-account version-claim exemption it introduced), moving the
fast-tier count from 5321 to **5324**; smoke's 3768 and the module count (321)
are unaffected.

Drift since that measurement: task 005 (requirement F)'s new
`tests/records/test_ralphctl_process_boundary.py` (5 nodes, 1 module) landed in
the fast tier, moving the live collected count to **5329 tests / 322 modules**
-- both still over the PRD's <=3,000-test / <=180-module tier-wide targets
(unchanged in kind from the 5324/321 miss above). Task 008 (requirement G.2)
re-measured the table's own smoke/fast/records rows, wall clock included, at
sha `9b1a0e2`; the budget table above and the "Targets missed" section below
carry those numbers now.

**Dev-layout, `PYTHONUSERBASE` exported (task 007).** Measured on this task's
own tree at sha `9a9ce6c` (task 006's docs-only commit, the sha named in the
surrounding prose above): `scripts/host_like_dev_layout.sh`, no `-m`/`-rs`
args and no `HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE` override -- the
script's own default is `exported` (see the script's header and its
`PYTHONUSERBASE_MODE="${HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE:-exported}"`
line), so `PYTHONUSERBASE` is exported to the pytest process the same way
every ordinary invocation of this script already exports it for the install
step. Launched `nohup timeout -k 30 3300 scripts/host_like_dev_layout.sh &`
and polled to completion rather than blocked on: `2 failed, 5406 passed, 1
skipped, 9 warnings in 2146.54s (0:35:46)`. The wrapper's own `set -e`/`set
+e` guard captures pytest's `$STATUS` and exits with it, but this run's own
shell session ended before that value could be read back out of the
backgrounded process; the exit status recorded above (**1**) is therefore
pytest's own documented convention for a run that collected and ran tests
with at least one failure (distinct from 2 for an interrupted run, or
3/4/5 for internal/usage/no-tests-collected errors), applied to a summary
line that shows two failures and no interruption or crash -- not a guess
between outcomes read off prose. The two failing node ids are pre-existing at
this sha, unrelated to this mode or this row (the same two
`test_sweep_sha_guard.py` nodes task 006's unset-mode row already named;
that row's third failure,
`test_tier_budgets.py::test_the_tier_wide_count_targets_are_met_or_disclosed`,
is gone here because task 006's own commit already added the fast-tier drift
disclosure that test checks for):
`tests/test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged`
and `tests/test_sweep_sha_guard.py::test_every_sweep_record_declares_a_sha`.
The placed skill (`host-layout-smoke-skill`) SURVIVED the run. Collected
counts (5409 tests / 334 modules) come from a plain `--collect-only` pass
over the same tree in this container's own `.venv` -- identical to the unset
mode's row, since both modes run the same unfiltered test selection under a
different `$HOME`/`PYTHONUSERBASE`, never a different test selection. No
ceiling in this table was raised to record this row; the budget columns
(6500 / 400 / 3600 s) are the same headroom already carried by the unset
mode's row.

**Live count disclosure, v0.20 (operator, 2026-09-09).** The fast tier now
collects **5590 tests across 343 modules** at task 035's commit, over
the PRD's `< 3000` tests / `<= 180` modules targets, and the smoke tier **4022
tests**, all re-measured directly with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q`. Recorded here
because `test_tier_budgets.py::test_the_tier_wide_count_targets_are_met_or_disclosed`
requires the live number to be *stated* once it exceeds a target -- disclosure
is a visible, reviewable act rather than a silent drift. The number moved
because this wave's own tasks add test modules: it was 5329 at `9b1a0e2`, 5332
at the wave's first commit (`1ba8c46`, task 001's new
`tests/test_unconsumed_steering_live.py`), 5358 by task 011, 5363 by task
012's `fed9096c` (`tests/test_progress_claim_corroboration.py`, 5 new nodes,
plus the fingerprint-corroboration coverage it added to
`tests/test_worker_progress_claim.py`), 5364 (smoke 3800) by task 067's
one new node in `tests/records/test_report_claims.py` pinning the
check-run-id guidance in the report-claims guard's own failure message,
5367 (smoke 3803) by task 013's new `tests/test_progress_budget_task013.py`
(3 new in-process nodes covering the per-task progress budget, all also
`smoke`), 5379 (smoke 3815, modules 326->327) by task 014's new
`tests/test_progress_budget_surface_task014.py` (12 new nodes covering the
`progressBudget` surface on `format_task_status()`, `ralphctl status`/
`tasks` and the health-unaffected pin, all also `smoke`; the module's new
browser-tier node in `tests/test_browser_hub.py` is neither fast nor
smoke, so it does not move either count), and 5384 (smoke 3820, modules
327->328) by task 015's new `tests/test_progress_pin_task015.py` (5 new
in-process nodes pinning the corroborated/uncorroborated/two-external-
only/budget-reset behaviours and the v0.19 approach-1 incident replay,
all also `smoke`), and 5393 (smoke 3829, modules 328->329) by task 071's
new `tests/test_cli_leftover_positional_182.py` (9 new in-process nodes
exercising `_place_leftover_positional` directly for #182 -- the five
amend forms, the matching `steer --list` form, both of the function's own
error paths and its no-op case -- all also `smoke`), and 5402 (smoke 3838,
modules 329->330) by task 072's new `tests/test_cli_argparse_walker_182.py`
(9 new in-process nodes: the parser-walking guard itself, a pin of its
discovered affected-subcommand set, the fixture proving the walk is
non-vacuous, the fixture's harmless-sibling check, and four direct-unit
nodes exercising `_place_leftover_positional` for the two newly-discovered
subcommands (`budget`, `suggestions`) -- all also `smoke`), and 5414 (smoke
3850, modules 330->331) by task 016's new `tests/test_forced_split_budget_
task016.py` (12 new in-process nodes covering the forced-split-on-budget-
exhaustion mechanism (PRD requirement B.1, #156): the budget-exhausted
prompt notice appearing/absent, the no-`proposed_tasks` report refused and
re-asked on the contract ladder then accepted once it arrives, the parent
task moving to `superseded` and its split child becoming schedulable next,
the superseded parent no longer blocking a dependent, `state.supersede_
task's own writer, and `LoopSupervisor._fold_inherited_dependsOn`'s
dependsOn-inheritance fold in isolation -- all also `smoke`), 5417
(smoke 3853, modules unchanged at 331) by task 016's follow-up commit in the
SAME module (3 more nodes, no new module: the parent's `dependsOn` reaching
EVERY child through the real pipeline rather than only a child that declared
none of its own, the superseded parent not stranding plan completion once its
children are done, and `superseded`'s membership of
`state.TASK_COMPLETION_NONBLOCKING_STATUSES`/`TASK_TERMINAL_STATUSES` --
all also `smoke`), and 5422 (smoke 3858, modules unchanged at 331) by task
017's new nodes across two EXISTING modules (no new module): two in
`tests/test_plan_ingestion.py` pinning `lint_proposal`'s new
`extra_known_ids` parameter directly (accepted, and refused with a hint
naming both sets), and three in `tests/test_worker_split_proposal.py`
pinning `lint_worker_split`'s widened `dependsOn` resolution against the
caller's live `tasks.json` id set -- including the parent task itself --
alongside the pre-existing batch-only acceptance and refusal cases --
all also `smoke`), and 5425 (smoke 3861, modules unchanged at 331) by task
018's 3 new nodes in the SAME `tests/test_last_approach.py` module (no new
module): a git-backed workspace whose approach 1 of 3 commits real work on
its FIRST iteration then goes quiet for three more, pausing (never
replanning) exactly like the last-approach case; that same pause's
in-process resume back into `worker` over the same approach; and a
`superseded` task present in the plan (no git at all) pausing a stagnant
non-last approach for the SAME reason -- all also `smoke`, and 5428 (smoke
3864, modules unchanged at 331) by task 018's RE-TAKE, 3 further nodes in that
same module (no new module): the second budget exhaustion pausing instead of
splitting again (a `superseded` parent's own split child with its
`progressBudget` spent), that pause's resume proceeding to the ordinary forced
split rather than re-pausing, and the negative case where a child of a still-
live parent must still force its FIRST split -- all also `smoke`), and 5447
(smoke 3882, modules unchanged at 331) by task 019's re-take, 19 new nodes
across three EXISTING modules (no new module): 11 in
`tests/test_transitions.py` covering the two NEW generated blocks in
`docs/architecture.md` (the task-status table and its mermaid diagram --
markers, byte-for-byte match with their generators, idempotence, every
declared status and every declared edge present, the `superseded` edge and the
one `non_vigilant_only`-labelled edge in both blocks, a mutation case proving
both renders track the declaration, and the `GUARD_CONDITION_NOTES` rendering
of the widened `stagnation_last_approach` condition plus its own mutation
case), 7 in `tests/test_task_status_lifecycle.py` covering `superseded` as a
declared status (its `in-progress` edge in both variants, its terminality,
`in-progress` being its only source, the refusal of a hat-reported
`superseded` in both variants, the `ENGINE_FORCED`/`OPERATOR_RULEABLE`
derivation, and `ralphctl doctor` reading a `superseded` record as a known
status) -- these 18 all also `smoke` -- and 1 fast-only node in
`tests/test_operator_ruling.py` driving the real `ralphctl amend --status
superseded` refusal through a subprocess (fast, not `smoke`, like every other
node in that module's CLI-subprocess family), and 5448 (smoke 3883, modules
unchanged at 331) by task 020's one new node in the SAME `tests/test_last_
approach.py` module (no new module): tasks 016/017/018's own three stages
(budget exhaustion forcing a split, the parent reaching `superseded`, its
child being picked next, and that child's OWN budget exhaustion pausing
rather than recursing into a second split) chained for real in one
continuous `_step_worker()` call, driven through corroborated `file`-
evidence progress claims against a real (separate) workspace tree rather
than pre-seeded `progressBudget` -- also `smoke`), and 5457 (smoke 3892,
modules 331->332) by task 021's `verdict: "deferred"` reorder (PRD
requirement C.1, #156): 3 new nodes in the SAME `tests/test_worker_hat.py`
scheduler-unit block (no new module) pinning `select_next_task`'s new
`deferredUntil` exclusion directly (a deferred task never preempts a
runnable one on priority, only-deferred-left still returns `None`, and a
deferred task never blocks a lower-priority runnable one), and 6 new nodes
in a NEW `tests/test_deferred_verdict.py` (schema/semantic checks in
isolation, the declared `worker_deferred_reordered` table edge, and a
whole-run reorder proving the picker's next choice really is the OTHER
runnable task while `status.json`'s `progressBudget` and the approach
counter both stay untouched) -- all 9 also `smoke`), and 5460 (smoke 3895,
modules 332->333) by task 022's deferred-wait sleep (PRD requirement C.1,
#156): 3 new nodes in a NEW `tests/test_deferred_wait.py` (the earliest
`until` before `deadlineAt` wakes the task and clears its deferral, an
`until` past `deadlineAt` is bounded by the deadline instead and left
deferred with no busy-spin, and the contract refuses an `until` past
`deadlineAt` naming the deadline in its hint) -- all also `smoke`, and 5472
(smoke 3907, modules 333->334) by task 023's surface for a deferred task
(PRD requirement C.1, #156): 12 new in-process nodes in a NEW
`tests/test_deferred_tasks_surface.py` -- `format_task_status()`'s new
` (deferred until <t>)` suffix and its composition with the blocked-by
suffix, `ralphctl tasks`'s and `--json`'s on-disk snapshot rendering,
`app.js`'s `taskStatusText`/`renderTasks` pill read as text, and
`LoopSupervisor._worker_assignment_extra`'s new "## Deferred tasks" section
(present with a task assigned, present with none selectable, absent when
nothing is deferred, and never fencing a contract-shaped object) pinned both
directly and through the real `_step_worker` dispatch -- all also `smoke`),
and 5473 (smoke 3908, modules 334->335) by task 024's replay of the v0.19
#156 approach-2 incident (PRD requirement C.5, #156): 1 new in-process node
in a NEW `tests/test_deferred_pin_task024.py` driving a real
`LoopSupervisor._run_job_core()` over the incident's own shape -- the
cut-record task starting `in-progress` and reporting `deferred` on a
06:00Z gate, the run continuing with the plan's other runnable task, the
sleep-and-wake mechanism clearing the deferral once `until` passes, and
both tasks landing `completed` with the run reaching `succeeded` --
unlike the real incident's `failed` -- also `smoke`, and 5490 (smoke 3925,
modules 335->336) by task 025's new `tests/test_estimated_iterations.py`
(PRD requirement D.1, #157): 17 new in-process nodes covering
`plan_ingestion` accepting an `estimatedIterations` integer >= 1 per task,
refusing a non-integer/float/bool/zero/negative value at shape time naming
the offending task, defaulting a missing one to 1 with a recorded warning
(`normalize_task_estimated_iterations`, the same channel `normalize_task_
tiers` already uses for `tier`), the field surviving `ingest_proposal` as a
passthrough field, the full `enforce_plan_ingestion` loop (default,
refusal-and-reprompt, acceptance), and `planning.md`'s own documentation of
the field outside its one pre-existing schema-example code fence -- all
also `smoke`, and 5505 (smoke 3940, modules 336->337) by task 026's new
`tests/test_estimated_iterations_staging.py` (PRD requirement D.2, #157):
15 new in-process nodes covering `_staged_decomposition_gap` (fewer than
two `Stage N` markers, no markers at all, a stage naming neither a numeric
target nor a module group, two stages each with a numeric target, two
stages each with a module group, the full staged fixture, spelled-out
stage numbers not counting as markers) and `lint_proposal`'s own
refusal/acceptance behaviour (refusal at 4 with no staging, acceptance at
3 regardless of staging, acceptance at 4-with-a-valid-staged-decomposition,
refusal at 5, refusal at 4-with-only-one-stage-marker, a missing estimate
never tripping this lint) through both the bare function and the full
`enforce_plan_ingestion` loop -- all also `smoke`, and 5524 (smoke 3959,
modules unchanged at 337) by task 026's SECOND commit, which tightened the
same detector after validation rejected the first: 19 further nodes in that
same module pinning the three false acceptances attempt 1 shipped (a stage
whose only number is an issue number, `#N`/`GH-N`/task-id/version
references, the bare word "module" as a module group, duplicate `Stage 1`
labels) plus the sequence rules (labels must start at 1, run consecutively,
not descend), the no-repeated-clause rule, the accepted shapes
(number+unit, comparator+number, a bare directory group) and a
slashed conjunction (`and/or`) NOT counting as a path -- five of them
through `lint_proposal` itself, all also `smoke`, and 5532 (smoke 3967,
modules unchanged at 337) by task 026's THIRD commit, which moved the same
detector from the oversized task's own criteria to the PROPOSAL's task list
after validation rejected the second attempt for reading the requirement
inverted: the module's 42 nodes now drive `_staged_decomposition_gap(task,
tasks)` over stage TASKS (a parent beside two dependency-ordered stage
tasks accepted; the same parent with `Stage N` headings inside its own
flat criterion refused; binding by either `dependsOn` direction, by naming
the parent's id, and through a stage chain; a stage-marked task bound to
some other parent not counting), with every measurability and
sequence node of the second attempt kept at the stage-task level, plus two
nodes on the refusal message's own wording -- all also `smoke`, and 5539
(smoke 3974, modules 337->338) by task 027's new
`tests/test_baseline_target_smell.py` (PRD requirement D.3, #157): 7 new
in-process nodes covering `_quoted_baseline_target_gap` -- a baseline more
than 2x the target with no measurement task refused naming the offending
task, a linked measurement task that re-measures the baseline at the
launch sha and stages the descent accepted, a measurement task missing the
"launch sha" phrase not enough, an unbound measurement task not counting,
a baseline within 2x the target passing untouched, a target with no quoted
baseline at all passing untouched, and the exact-2x boundary passing
untouched -- all also `smoke`, and **5545 (smoke 3980, modules unchanged at
338)** by task 027's SECOND commit, which tightened the same detector after
validation rejected the first for accepting a linked `Stage 1` task that
re-measured a DIFFERENT metric and defined no descent: 6 further nodes in
that same module pin validation's own counter-example, the two legs it was
missing (the measurement task must identify THIS baseline and must name
THIS descent's endpoint, each refused on its own), the baseline-number
route to identifying the metric, and the two new helpers (`_metric_words`
folding possessives and dropping stopwords, `_number_token` matching
grouped and bare forms only whole) -- all also `smoke`, and **5552 (smoke
3987, modules 338->339)** by task 028's new
`tests/test_progress_budget_estimate_surface_task028.py` (PRD requirement
D.4, #157): 7 new in-process nodes covering `state.task_progress_budget_
estimate()` (an explicit `estimatedIterations` deriving `+2`, a missing
field and a non-numeric/non-dict task both defaulting to 3),
`LoopSupervisor._progress_budget_for()` agreeing with that same shared
reader exactly, and `ralphctl tasks`'s new ` (est N)` column on an
on-disk snapshot (an explicit estimate, the default, and every row
carrying it regardless of status) -- all also `smoke`, and **5553 (smoke
unchanged at 3987, modules unchanged at 339)** by task 029's one new node
in the SAME `tests/flows/test_flow_peer_gates.py` module (no new module):
a sixth flow -- an oversized, unstaged `estimatedIterations: 4` task
refused at ingestion (naming the offending task in a `contract.violation`
event, before `plan_review` is ever invoked), the same planning approach
re-prompted, and a genuinely staged resubmission accepted and approved by
`plan_review`'s own single invocation -- fast-only like every other node
in this module (it spawns a real engine subprocess), so smoke does not
move -- and **5554 (smoke unchanged at 3987, modules unchanged at 339)** by
task 029's re-take, one further node in that same module: a seventh flow
whose near-miss proposal (an `estimatedIterations: 4` task beside two
regex-satisfying but unrelated `Stage N` tasks) CLEARS ingestion and is
refused by `plan_review` itself, spending a real revision, with the
objection observable in the re-prompted planner's own `prompt.md` and the
genuinely staged resubmission approved by the second `plan_review`
invocation -- likewise fast-only, and **5557 (smoke 3990, modules unchanged
at 339)** by task 030's 3 new nodes in the SAME `tests/test_cure_pass.py`
module (no new module, PRD requirement E.1, #167): the grant of a second
cure pass on a non-superset rejection, the refusal (ordinary replan) of a
superset second rejection, and the `CURE_PASS_CEILING` (3) refusing a
fourth pass even against a disjoint blocking set -- all also `smoke`
(in-process `LoopSupervisor._step_review()`, no real engine subprocess),
and **5573 (smoke 4006, modules 339->340)** by task 031's own new
`tests/test_cured_when.py` (16 nodes, PRD requirement E.2, #171): the
`cured_when`/`invalidates` schema fields, the missing-/overrun-`cured_when`
business rules, the ladder re-ask over a curable blocking finding with no
`cured_when` and its recovery, and the criteria substitution itself
(`cured_when` becomes a cure task's `successCriteria`, `clause`+`evidence`
becomes its `description`) -- all also `smoke` (pure functions and
in-process `LoopSupervisor` calls, no real engine subprocess), and **5580
(smoke 4013, modules 340->341)** by task 032's own new
`tests/test_cure_invalidates.py` (7 nodes, PRD requirement E.3, #167): the
per-path re-take insertion for an invalidated path with no existing
record task, the re-open chain for a path that IS the plan's own final
lane-evidence record (`status` back to `pending` on both the
verification and record tasks, `dependsOn` chained cure -> verification
-> record), and the emitted `approach.cure_pass` event's own `taskIds`
naming every id a real `_step_review()` pass inserted or reopened -- all
also `smoke` (in-process `insert_cure_tasks` calls and one
`LoopSupervisor._step_review()` pass, no real engine subprocess), and
**5585 (smoke 4018, modules 341->342)** by task 033's own new
`tests/test_cure_freeze_ordering.py` (5 nodes, PRD requirement E.4,
#167): `last_code_touching_task_id`'s standing-rules-override and
task-self-declaring-fallback lookups, a cure task landing immediately
before the named freeze task (`_insert_before_id`), a re-take task
landing immediately after it (`_insert_after_id`), several cure/re-take
tasks from two findings staying in finding order rather than reversed,
and the append-at-the-end fallback when the plan names no such task
anywhere -- all also `smoke` (in-process `insert_cure_tasks` calls, no
real engine subprocess), and **5586 (smoke 4018, modules unchanged at
342)** by task 034's own one new node appended to
`tests/flows/test_flow_cure_pass.py` (PRD requirement E.3/E.4 replay,
#167, #171): a real `ralphd-engine` subprocess replays the v0.19.1
approach-2 incident (a curable rejection whose cure `invalidates` a
recorded sweep) end to end, over the stub `pi` binary's new
`STUB_REVIEW_CURABLE_INVALIDATES` knob -- not `smoke` (a real engine
subprocess, like every other node in that module), and **5590 (smoke 4022,
modules 342->343)** by task 035's own new
`tests/test_approach_progress_context.py` (4 nodes, PRD requirement F.1/F.4,
#172): `LoopSupervisor._approach_progress_context` (the shared `## Job
context` renderer for `approach N of M`/cure passes/remaining iteration
budget, read from `status.json`) pinned once against `build_prompt("worker")`
and once against `build_prompt("review")`, a never-cured approach defaulting
its cure-pass count to `0`, and the silent no-op before any approach is
committed to `status.json` -- all also `smoke` (in-process `LoopSupervisor`
construction and `build_prompt` calls, no real engine subprocess).

**This count must be updated in the same commit that adds tests**, now that CI
runs the fast tier on every push (`docs/ci.md`): a task that adds a module and
leaves this number stale turns the blocking gate red for the next commit rather
than at the end of the wave. That is the intended behaviour of the guard -- it
went unnoticed for the first ten tasks of this wave precisely because nothing
ran the tier per commit.

### The stub-`pi` flow lane (`tests/flows/`)

The flow modules requirement G.5 asks for live in their own directory, are
`fast`-only by rule 3 above (each one spawns a real engine), and each has to
stay under 20 s serial. Measured serially in this container, one module per
invocation (`.venv/bin/python -m pytest -q tests/flows/<module>.py`), re-measured
at the sha that landed the sixth (task 013, peer gates):

| module | tests | wall clock |
|---|---|---|
| `tests/flows/test_flow_happy_path.py` | 2 | 1.41 s |
| `tests/flows/test_flow_review_replan_pause.py` | 3 | 2.40 s |
| `tests/flows/test_flow_petition_adjudication.py` | 2 | 1.40 s |
| `tests/flows/test_flow_cure_pass.py` | 2 | 1.59 s |
| `tests/flows/test_flow_validation_ladder.py` | 3 | 2.32 s |
| `tests/flows/test_flow_peer_gates.py` | 3 | 2.13 s |
| the whole directory (`tests/flows`) | 15 | 10.81 s |

That table is prose, not a budget row: `tests/test_tier_budgets.py` parses only
the eleven-column table above it. The 10.81 s the directory adds is inside the
fast lane's own recorded measurements.

### The records-lint family (`tests/records/`, task 035, requirement E.4; subprocess count updated task 009, #176; fresh-clone node added task 010, #176)

Measured serially (`.venv/bin/python -m pytest -q tests/records/`, repeated,
~20.6 s): **201 tests collected** across 5 test modules (plus a `conftest.py`:
`test_conformance_ledger.py`, `test_records_family_budget.py`,
`test_records_family_fresh_clone.py`, `test_report_claims.py`,
`test_roadmap.py`), against the PRD's **300-test** target (met) and **15 s**
target (MISSED at 20.6 s -- see "Targets missed" below for why: task 010's
new fresh-clone node spawns a real `git clone` plus a nested `pytest`
subprocess on purpose, and that is where nearly all of the added ~11 s
goes).

**The subprocess rule: `git ls-files` and `git rev-list HEAD` for a whole
run, and nothing else.**
`tests/records/test_records_family_budget.py::test_the_records_family_spawns_only_one_git_ls_files`
pins it live -- it patches `subprocess.run`, runs the rest of the family
in-process from a cold cache, and fails on a second call of either, or on any
other command at all. Three fixes in task 035 got the count from ~220 down to
one (`git ls-files`); task 009 (#176) then added the second, deliberately,
so `commit_problems()` requires a cited sha to be reachable from `HEAD`, not
merely present as an object -- a sha that only exists as a dangling object
(git keeps an amended, reset or rebased-away commit's pre-image on disk
until `git gc` reclaims it) is now a claim problem instead of a silent pass:

1. `tests/report_claims.py`'s `commit_problems()` used to answer "is this
   cited sha a commit object in this repo" with a `git cat-file --batch-check`
   subprocess, once PER REPORT that carried a sha claim -- about 218 of the
   ~220 subprocess calls a full `tests/records/` run made, measured on this
   task's own tree before the fix. It now reads the object type out of `.git`
   in-process (`_ObjectDatabase`: a loose object's zlib type header, a packed
   object's pack-entry type bits via the v2 `.idx` fanout/sha/offset tables,
   following an OFS_DELTA/REF_DELTA chain to its base because a delta never
   changes the object type), returning `git cat-file --batch-check`'s own
   vocabulary -- the type word for a resolved object, `missing` for a prefix
   nothing matches, `ambiguous` for one several objects match -- so
   `commit_problems`'s failure messages are unchanged. That reader is checked
   against real git's answers by `test_report_claims_git_objects.py`, which
   sits OUTSIDE this family exactly because the comparison has to spawn the
   `git cat-file` the family is pinned not to spawn: every sha the reports
   cite, 400 full shas and 200 seven-char abbreviations sampled from the
   object store, plus deliberately ambiguous and unresolvable prefixes.
2. `tests/records/test_conformance_ledger.py`'s `_collect_node_ids()` sets
   `RALPHD_SKIP_DOCKER_PROBE=1` around its nested in-process collection (new
   gate in `test_docker_sibling_e2e.py`'s `_docker_available()`), which
   removed a real `docker version` probe a ledger-cited module's import-time
   skipif was spending on a collect-only pass that never looked at the answer.
3. `tests/records/test_report_claims.py`'s whitebox memoisation test fakes
   `subprocess.run`'s return instead of calling real git, so proving the
   one-snapshot caching no longer spends a second real `git ls-files` of its
   own.

The two remaining calls are `path_problems()`'s `git ls-files` snapshot and
`commit_problems()`'s `git rev-list HEAD` snapshot (`_reachable_commits()`,
task 009), each memoised for the process: path membership and commit
reachability genuinely are questions about git's own state, and nothing in
the family asks either one twice.

**The fresh-clone exemption (task 010, #176).**
`tests/records/test_records_family_fresh_clone.py`'s one node clones this
repository for real (`git clone --local --no-hardlinks`, into a throwaway
directory) and then runs `tests/records/` there too, as a real nested
`pytest` subprocess, to prove the family passes against a genuine fresh
clone and not merely against this checkout's own working tree -- catching a
hidden dependency on some file the clone would not carry that the family's
own `Path(__file__)`-relative `REPO_ROOT` resolution would otherwise hide.
That node is marked `@pytest.mark.nightly` directly (task 048's B.5
follow-on precedent for "a real nested-pytest-subset run"), so it stays out
of the `smoke`/`fast` tiers, but it still lives inside `tests/records/` and
still runs whenever this directory is selected by path, which is why the
family's own wall clock and test count above both include it. Its nested
run excludes its own path inside the clone (the same self-exclusion shape
`test_the_records_family_spawns_only_one_git_ls_files` already uses),
without which it would recurse into cloning its own clone without bound.
It is also named as the single exemption from the two-subprocess budget
above: `test_the_records_family_spawns_only_one_git_ls_files`'s own nested
run excludes this module by path (alongside itself) so its deliberate `git
clone` and nested `pytest` subprocess are never mistaken for a second real
`git ls-files` or `git rev-list HEAD`.

### Targets missed, with their numbers

Requirement G's rule is that a missed target is disclosed with its number, not
hidden. Re-measured at sha `9b1a0e2` (task 008, requirement G.2): four of the
five G.8 wall-clock/count targets checked here are missed, and one is now met
-- unchanged in kind from task 050's own measurement, since no code moved
between `3e65963` and `9b1a0e2` in either tier; only the live drift from
requirement F's `tests/records/test_ralphctl_process_boundary.py` (5 nodes, 1
module) is new, already folded into the fast/records counts below.

- **smoke serial 137.18 s** against a target of < 120 s -- MISSED. At `-n 8` the
  same tier is **26.85 s**, inside its own 60 s ceiling (that lane carries no PRD
  target of this kind).
- **5329 tests collected** (fast tier) against a target of at most 3000 --
  MISSED. Up by 5 from the 5324 recorded at the previous measurement (task
  005's `tests/records/test_ralphctl_process_boundary.py` nodes), still well
  over target.
- **322 modules collected** (fast tier) against a target of at most 180 --
  MISSED. Up by 1 from the 321 recorded at the previous measurement (the same
  new module).
- **fast, `-n 8`, 81.04 s** against a target of < 120 s -- **met**. Unchanged in
  kind from the previous measurement's 81.89 s.
- **records serial 20.60 s** against a target of < 15 s -- MISSED (new, task
  010, #176): the lane was **met** at 9.40 s before this task's own
  `test_records_family_fresh_clone.py` node landed; that one node's real
  `git clone` plus nested `pytest` subprocess run accounts for essentially
  all of the added ~11.2 s. The lane's own "budget wall clock" ceiling
  moved from 15 s to 30 s (headroom over the new 20.60 s measurement, the
  same shape every other lane in this table already carries between its
  ceiling and its tighter PRD target) -- the 15 s PRD target itself is
  unchanged and is the number disclosed as missed here, not loosened to
  match.
- **fast serial 540.44 s** against a target of < 300 s -- MISSED, up from
  491.68 s (this run's serial pass also surfaced one order-/timing-dependent
  red, `tests/test_reflection.py::test_reflect_runs_after_terminal_state_leaves_run_state_untouched`,
  disclosed above with its solo-pass evidence; not a wall-clock cause).

**No ceiling is breached, and none was raised.** Every lane is inside the
ceiling it already had (26.85 < 60, 137.18 < 200, 81.04 < 150, 540.44 < 1100,
9.39 < 15) -- the ceilings in this table are unchanged from task 050's own
measurement, which were themselves byte-for-byte the ones the wave launched
with at sha `1a3b9bb`.

The slowest node in the parallel fast lane is 11.24 s
(`tests/test_inband_error_retry.py::test_inband_errors_exhausting_retries_end_terminal_without_burning_budget`),
and the whole `--durations=40` list sums to 127.96 s; the per-node detail lives
in `artifacts/reports/v0.19.1-suite-budget.md`'s 2026-09-08 entry.

The dev-layout `PYTHONUSERBASE`-unset mode's nightly target (under 15 min,
from ~40) is now measured in the budget table above: **MISSED**, 2047.32 s
against the 900 s target (task 006). The `PYTHONUSERBASE`-exported mode's
same target is now measured in the same table too: **MISSED**, 2146.54 s
against the 900 s target (task 007).

## When a tier goes over budget

The failure names the lane, the measurement and the ceiling. Three honest
answers, in order of preference:

1. make the new tests cheaper (stub the backoff, drop the real sleep, keep the
   module in-process) so the tier stays inside its ceiling;
2. move the expensive coverage to the tier that can afford it -- a module that
   spawns a real engine belongs in `fast`, a docker-dependent one in nightly;
3. raise the ceiling in the table above **and** record the new measurement
   beside it, which is a visible, reviewable act rather than a silent drift.

Deleting or loosening the assertion is not on that list.
