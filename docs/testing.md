# Testing ralphd: the tiers, their recipes, and their budgets

This suite is tiered by pytest marker. A tier is a promise about *cost*: what it
covers, what it deliberately leaves out, and how long it may take before it stops
being usable at the point in the loop that runs it. The tiers and their measured
budgets are checked by `tests/test_tier_budgets.py`, which reads this file.

| tier | marker | who runs it | what it covers |
|---|---|---|---|
| smoke | `smoke` | the per-task verify pass, and `scripts/final_sha_guards.sh` over the records-guard modules it discovers | in-process engine flows (a supervisor plus scripted attempts, backoffs stubbed), in-process CLI, records lint, in-process hub |
| fast | `fast` | the final sha of a wave, and any pull request | smoke plus the stub-pi flow modules and the process-boundary CLI tests -- everything outside the nightly lanes |
| nightly | `docker`, `browser`, and the dev-layout module | a sweep, never a per-task check | the docker-sibling tier, the browser tier, and `nightly.yml`'s six-cell dev-layout container matrix (`ubuntu:24.04`, `python:3.11-slim`, `python:3.14-slim` x `PYTHONUSERBASE` exported/unset, `max-parallel: 2` across the VPS's two registered runner slots) -- each cell's `pytest` invocation itself now runs the `fast` tier by default (task 076, `scripts/host_like_dev_layout.sh`'s `HOST_LIKE_DEV_LAYOUT_TIER`), not the whole suite unfiltered; see the dev-layout rows below |

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

## Loop-driving tests must be bounded

A test that drives the engine loop through `run_job`, `_run_job_core` or
`_step_review` must never do so with a bare `asyncio.run(...)`: two states in
the loop can wait forever -- the operator-gated `paused` ending (`_step_
paused`'s `await self._gate()`, cleared only by `POST /pause`'s
`loop.pause()`) and a `_ScriptedRunner` script whose repeating last step never
reaches a terminal phase -- and a bare `asyncio.run(...)` over either one hangs
the test process, and with it whichever lane collected that test. Bound the
await instead: `asyncio.run(asyncio.wait_for(coro, timeout))`, or a per-module
`_bounded(coro, timeout=30.0)` helper when several call sites in one module
need the same wrapping (`tests/test_peer_verdict_gate.py` and `tests/test_
peer_plan_review_gate.py` both added one for task 072). A bound written in the
module that only *imports* a loop-driving helper from elsewhere does not reach
an `asyncio.run(...)` inside that helper's own module -- the helper's own
module needs its own bound, which is exactly what task 072's second commit
had to add after its first missed it.

`tests/test_loop_driving_await_guard.py` (task 073, steer 001 §2) enforces
this for the three named calls: it fails on a NEW bare `asyncio.run(...)` over
any of them, in any module under `tests/`, against a per-file allowlist of the
pre-existing occurrences it grandfathers (`LOOP_DRIVING_AWAIT_ALLOWLIST`,
each entry naming the file, its permitted count and a reason). A call the
guard does not already permit needs either the bound above or a new,
reasoned allowlist entry -- never a widened timeout on an existing bound, and
never a deleted assertion, to make the guard pass.

## Budgets

Measured in this container (28 cores) with the recipes above: an earlier wave's
task 008 (requirement G.2) re-measured the smoke, fast and records rows at sha
`9b1a0e2` (task 050's two fix commits `778738b` and `3e65963` are still what
the code underneath those numbers was). This wave's own task 008 (v0.20.2,
requirement A.6) re-measures the same three rows again, at this task's own
tip, to fold in the counts that requirements A.1-A.4 removed since `9b1a0e2` --
the fast tier's live collected count had drifted from 5732 at this wave's own
launch baseline (task 001, sha `e1d5226`) down to **5629** by task 004
(A.2)'s own tip, and stayed there through task 005 (A.3), task 006 (A.4) and
task 007 (A.5), none of which changed a collected count -- while the table
above still carried the pre-launch 5329 figure. The "budget" columns are the
declared ceilings this suite is held to; `tests/test_tier_budgets.py` fails when a recorded measurement
exceeds one, and independently asks pytest what each tier collects right now and
fails when *that* is over the ceiling. The ceilings still carry headroom over
today's measurements so the wave's remaining tasks (009-016, still ahead of the
F.5/A.7 freeze line) can land a modest number of new nodes, but every ceiling in
the table below moved down from its own prior value in this same commit,
requirement A.6's own "downward only" rule -- they are a ratchet, not a target,
and not a licence to leave stale headroom in place once a removal has made it
larger than the tree it is meant to bound.

**5631 (smoke 4024, modules 360 unchanged) by v0.20.2 task 009 (requirement
B.1, #197): the notification success/failure event split.** Two named tests
land in the EXISTING `tests/test_notify_hat_tool.py` module --
`test_a_failed_send_emits_the_distinct_failure_event_and_never_sent` and
`test_a_successful_send_still_emits_notification_sent` -- covering the split
over the fixture run directory `_run(tmp_path)` builds (`tmp_path / "run"`),
never this run's own events (requirement B.5). Neither spawns a real engine
or CLI process, so both are `smoke` as well as `fast`; net +2 fast, +2
smoke, module count unchanged (no new module). From the pre-task
5629/4022/360: **5631/4024/360**. Re-measured directly at this task's own
tip with `.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q`
(fast: `5631/5736 tests collected (105 deselected)`, smoke: `4024/5736
tests collected (1712 deselected)`). `tests/records/` is unaffected (116
unchanged): neither touched module lives under `tests/records/`. Both
ceilings (5800 tests/375 modules fast, 4300/260 smoke) still carry
headroom for the remaining pre-freeze tasks.

**5640 (smoke 4033, modules 360->361) by v0.20.2 task 010 (requirement
B.2, #197): `ralphctl start --notifier-url`.** Nine named tests land in
the new `tests/test_notifier_url_flag.py` module: the shipped default is
unconfigured, a `job.yaml` `notifier_url` key alone reaches `JobConfig`,
`RALPHD_NOTIFIER_URL` reaches it with no `job.yaml` at all, the env var
beats an explicit `job.yaml` key in both directions, `ralphctl start
--notifier-url` both persists the value into the run's own `job.yaml`
and leaves the key out entirely when the flag is absent, and (task 010's
cure pass) a `--template`'s own `notifier_url:` key reaches the config
while an explicit flag beats it -- the CLI-driving ones go through
`tests/test_cli_docker.py`'s in-process `ctl` fixture (stub docker, no
real container). None of the nine spawns a real engine or CLI process, so
all nine are `smoke` as well as `fast`; net +9 fast, +9 smoke, +1 module
in each (the new module). From the pre-task 5631/4024/360:
**5640/4033/361**. Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5640/5745 tests collected (105 deselected)`, smoke: `4033/5745 tests
collected (1712 deselected)`). `tests/records/` is unaffected (116
unchanged): the new module lives directly under `tests/`. Both ceilings
(5800 tests/375 modules fast, 4300/260 smoke) still carry headroom for
the remaining pre-freeze tasks.

**5645 (smoke 4038, modules 361->363) by v0.20.2 task 011 (requirement
B.3, #197): the startup warning and its `doctor` mirror.** Five named
tests: two in the new `tests/test_notifier_startup_warning.py` (an
unconfigured run's `LoopSupervisor` construction emits
`NOTIFIER_UNCONFIGURED_EVENT` exactly once; a configured run emits none)
and three in the new `tests/test_cli_doctor_notifier_config.py` (an
unconfigured non-terminal run is named in `doctor`'s report and
`unconfiguredNotifierRuns`; a configured one is quiet; terminal runs are
left out either way) -- the latter drives `ralphctl doctor` in-process
(`tests/conftest.py`'s `call_ralphctl_in_process`) against a stub-docker
registry, never a real engine or CLI subprocess, so all five are `smoke`
as well as `fast`; net +5 fast, +5 smoke, +2 modules in each (the two new
modules). From the pre-task 5640/4033/361: **5645/4038/363**.
Re-measured directly at this task's own tip with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q` (fast: `5645/5750 tests
collected (105 deselected)`, smoke: `4038/5750 tests collected (1712
deselected)`). `tests/records/` is unaffected (116 unchanged): neither
new module lives under `tests/records/`. Both ceilings (5800 tests/375
modules fast, 4300/260 smoke) still carry headroom for the remaining
pre-freeze tasks.

**5647 (smoke 4040, modules unchanged at 363) by v0.20.2 task 011's cure
pass (same requirement B.3): the `doctor` mirror reads each RUN's own
persisted notifier state.** Two more named tests join the existing
`tests/test_cli_doctor_notifier_config.py` (no new module): the shell
`doctor` itself runs in cannot configure a bare run merely by exporting
`RALPHD_NOTIFIER_URL`, and a run whose own recorded `--env`/`--forward-env`
wiring carries that variable is quiet (with an empty value still counting
as unconfigured). Both drive `ralphctl doctor` in-process against the same
stub-docker registry, so both are `smoke` as well as `fast`; net +2 fast,
+2 smoke, +0 modules. From 5645/4038/363: **5647/4040/363**. Re-measured
directly at the cure's own tip with `.venv/bin/python -m pytest -m
<fast|smoke> --collect-only -q` (fast: `5647/5752 tests collected (105
deselected)`, smoke: `4040/5752 tests collected (1712 deselected)`).
`tests/records/` is unaffected (116 unchanged). Both ceilings (5800
tests/375 modules fast, 4300/260 smoke) still carry headroom for the
remaining pre-freeze tasks.

**5650 (smoke 4043, modules unchanged at 363) by v0.20.2 task 015
(requirement F.1-F.5): the unreadable-findings-channel rejection
fingerprint.** Three more named tests join the existing
`tests/test_repeated_rejection_signature.py` (no new module): a second
identical unreadable-channel rejection pauses through `_pause_and_
escalate_repeated_rejection_signature`, a differently-worded channel error
replans normally instead, and the residual-only/no-wall case stays
distinguishable from the unreadable-channel case. All three are `smoke` as
well as `fast`, mirroring the module's existing tests; net +3 fast, +3
smoke, +0 modules. From 5647/4040/363: **5650/4043/363**. Re-measured
directly at this task's own tip with `.venv/bin/python -m pytest -m
<fast|smoke> --collect-only -q` (fast: `5650/5755 tests collected (105
deselected)`, smoke: `4043/5755 tests collected (1712 deselected)`).
`tests/records/` is unaffected (116 unchanged). Both ceilings (5800
tests/375 modules fast, 4300/260 smoke) still carry headroom for the
remaining pre-freeze tasks.

**5662 (modules unchanged at 363) by v0.21 task 001 (requirement A.1,
#192): the peer verdict gate's own cure pass.** Two new nodes join
`tests/test_peer_verdict_gate.py` (no new module) --
`test_gate_rejection_all_curable_takes_a_cure_pass_not_a_replan` and
`test_the_gate_cure_pass_edge_is_declared_only_under_the_peer_delta` --
plus `test_verdict_gate_has_exactly_the_three_declared_edges` widened in
place (no net node change from that one). Net +2 fast, +0 modules. From
5660 (the v0.20.2 baseline this task's launch measured): **5662/363**.
Re-measured directly at this task's own tip with `.venv/bin/python -m
pytest -m fast --collect-only -q` (`5662 tests collected`). `tests/
records/` is unaffected. The fast ceiling (5800 tests/375 modules) still
carries headroom.

| lane | marker | recipe | tests | modules | wall clock | budget tests | budget modules | budget wall clock | PRD target | target met? |
|---|---|---|---|---|---|---|---|---|---|---|
| smoke, parallel | `smoke` | `-m smoke -n 8 --dist loadfile` | 4038 | 248 | 24.41 s | 4300 | 260 | 60 s | n/a | met -- 24.41 s inside the lane's own 60 s budget ceiling; no PRD target of this kind for this lane |
| smoke, serial | `smoke` | `-m smoke` | 4038 | 248 | 147.68 s | 4300 | 260 | 200 s | < 120 s | MISSED -- 147.68 s measured against the 120 s target |
| fast, parallel | `fast` | `-m fast -n 8 --dist loadfile` | 5645 | 363 | 80.12 s | 5800 | 375 | 150 s | < 120 s | met -- 80.12 s inside the 120 s target |
| fast, serial | `fast` | `-m fast` | 5645 | 363 | 540.44 s (stale wall clock, not retaken this task -- see note below) | 5800 | 375 | 1100 s | < 300 s | MISSED -- 540.44 s measured against the 300 s target |
| records, serial | n/a (selected by directory, not marker) | `.venv/bin/python -m pytest -q tests/records/` | 116 | 5 | 11.35 s | 150 | 10 | 30 s | < 15 s | met -- 11.35 s inside the 15 s target |
| dev-layout, `PYTHONUSERBASE` unset (retired unfiltered measurement, superseded by task 076) | n/a (no marker; the whole suite, unfiltered -- this recipe no longer runs; see below) | `HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE=unset scripts/host_like_dev_layout.sh` | 5409 | 334 | 2047.32 s | 6500 | 400 | 3600 s | < 900 s (15 min) | MISSED -- 2047.32 s measured against the 900 s target |
| dev-layout, `PYTHONUSERBASE` exported (retired unfiltered measurement, superseded by task 076) | n/a (no marker; the whole suite, unfiltered -- this recipe no longer runs; see below) | `scripts/host_like_dev_layout.sh` (default mode, no `HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE` override) | 5409 | 334 | 2146.54 s | 6500 | 400 | 3600 s | < 900 s (15 min) | MISSED -- 2146.54 s measured against the 900 s target |

**The two dev-layout rows above are retired measurements (task 076, steer
008).** `scripts/host_like_dev_layout.sh` no longer runs the whole suite
unfiltered by default: its own `pytest` invocation now adds `-m fast` unless
a caller names a different tier through `HOST_LIKE_DEV_LAYOUT_TIER` (see the
script's own header), including the literal value `unfiltered`, which
reproduces exactly the recipe the two rows above measured. A value that is
not even shaped like a marker expression (blank, unbalanced parentheses, a
character no marker expression uses) is refused by the script with exit 2
before its install step; whether a shape-valid expression *means* anything is
pytest's own judgement and surfaces as pytest's usage error. Both rows are kept
verbatim -- same recipe text, same counts, same wall clock, same budget
columns -- because they are still the only measurement this repo has of that
unfiltered invocation; they are labelled as retired rather than deleted or
rewritten to a recipe they were never run against. The lane itself (task 077's
container matrix) now runs each cell under the `fast` selection: the docker
and browser tiers that selection drops are already covered at the
gate-blocking level by `.github/workflows/heavy.yml` and do not vary with the
interpreter a dev-layout cell varies, so the six cells lose no coverage that
varies with the axis under test. No fresh dev-layout wall-clock measurement
exists yet for the `-m fast` selection; task 077's own container matrix cells
are the mechanism that will produce one, as a check-run verdict, never as a
locally recorded lane sweep (standing rule 1). The workflow's own per-cell
budget (`nightly.yml`'s `timeout-minutes: 120` -- raised from 60 by task 035
(steer 026 §4) because 60 was already marginal (58 min observed at
`bd284e79` with no interference) and task 035's own cure adds real
per-attempt wall time to any cell whose startup watchdog has to re-arm under
contention; `docs/ci.md`'s matching table entry carries the same figure) is
sized to that `-m fast` selection's ~80 s parallel wall clock plus a cell's own
image pull and, for the `ubuntu:24.04` cell only, its `apt-get install
python3 python3-pip` step -- not to the
3600 s ceiling the two retired rows above were measured against, which was
sized to the unfiltered whole suite this lane no longer runs.

**Task 008 (v0.20.2, requirement A.6) re-measurement.** The smoke-parallel,
smoke-serial, fast-parallel and records-serial cells above are fresh runs at
this task's own tip, taken sequentially and never two at once, with the wall
clock read off each run's own summary line: smoke parallel `4021 passed, 1
skipped ... in 24.41s`, smoke serial `4021 passed, 1 skipped ... in 147.68s`,
fast parallel `5628 passed, 1 skipped ... in 80.12s (0:01:20)`, records
`116 passed ... in 11.35s`. The fast-parallel run is this task's one full
fast-tier run (standing rule: at most one per iteration); its own
`--collect-only` output agrees exactly with a separate collect-only pass
(`5629/5734 tests collected (105 deselected)`), and the same collect-only
mechanism gives the module counts (360 fast, 245 smoke -- unique module paths
in each tier's own `--collect-only` output) and, by directory, `tests/records/`
(5 modules). The fast-serial cell's wall clock is **not** retaken this task:
the standing-rule cap of one full fast-tier run per iteration already spent
itself on the parallel recipe above, so that cell keeps the last real
measurement of the serial recipe (540.44 s at sha `9b1a0e2`, itself run at a
higher fast-tier count than today's) rather than fabricate a number; its own
`tests`/`modules` cells still carry today's real collected count, since a
count is recipe-independent (the same `-m fast` selection collects the same
nodes whether `-n 8` is passed or not) and needs only a `--collect-only`
pass, not a second full run, to state truthfully. The pre-existing serial-fast
finding this paragraph used to carry forward, about an order-dependent extra
failure in `tests/test_reflection.py`, is not re-confirmed by this task (the
serial recipe was not re-run) and is left below as prior-measurement history
rather than restated as current:
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
`test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged`,
`test_sweep_sha_guard.py::test_every_sweep_record_declares_a_sha`, and
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
`test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged`
and `test_sweep_sha_guard.py::test_every_sweep_record_declares_a_sha`.
The placed skill (`host-layout-smoke-skill`) SURVIVED the run. Collected
counts (5409 tests / 334 modules) come from a plain `--collect-only` pass
over the same tree in this container's own `.venv` -- identical to the unset
mode's row, since both modes run the same unfiltered test selection under a
different `$HOME`/`PYTHONUSERBASE`, never a different test selection. No
ceiling in this table was raised to record this row; the budget columns
(6500 / 400 / 3600 s) are the same headroom already carried by the unset
mode's row.

**Which surface a per-commit count change lands on.** The `tests`/`modules`
columns of the lane table above are part of a *measurement* -- the counts as
they stood at the sha each row's wall clock was taken at (`9b1a0e2` for the
first five rows, the recorded dev-layout runs for the last two) -- and are
not rewritten by a commit that adds or reworks nodes: re-recording them
would mean launching a lane locally to record its result, which standing
rule 1 of this wave forbids, and the guard those columns feed is the
ceiling comparison, not an equality with today's collection. The per-commit
surface for a count change is the live disclosure paragraph immediately
below, which `test_tier_budgets.py::test_the_tier_wide_count_targets_are_met_or_disclosed`
reads; a lane row moves only when that lane is measured again.

**Live count disclosure, v0.20 (operator, 2026-09-09).** The fast tier now
collects **5660 tests across 350 modules** at task 074's own commit
(task 048's re-take left it unchanged; task 074's new
`tests/test_prompt_sentinel_audit.py` added 2), over
the PRD's `< 3000` tests / `<= 180` modules targets, and the smoke tier **4079
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
construction and `build_prompt` calls, no real engine subprocess), and **5600
(smoke 4022, modules unchanged at 343)** by task 036's 10 new nodes in the
SAME `tests/test_operator_ruling.py` (no new module, #168): `write_amendment`
stamping/omitting the header's new `approach:` line and `amendment_entries`
parsing it back, and 7 `ralphctl amend` subprocess nodes covering the new
target-title/status echo, the not-in-plan refusal and its `--force` override,
the no-`tasks.json`-at-all non-refusal, and `--approach N` both explicit and
defaulted from `status.json`, and **5603 (smoke unchanged at 4022, modules
unchanged at 343)** by task 037's 3 new nodes in the SAME module (#168):
`_operator_amendment_review_context` scoping a ruling to the current
approach, an earlier-approach ruling rendered under the labelled
"Historical rulings" heading, a no-`approach:`-header entry treated as
current iff its id is in the plan, and an entry naming an id absent from
the plan never rendered as binding while `amendment_target_not_in_plan`
fires exactly once per render call -- none of these are `smoke` (the whole
module is a `ralphctl` subprocess family, fast-only, like every other node
in it), and **5610 (smoke 4029, modules 343->344)** by task 039's new
`tests/test_prompt_fence_conformance.py` (7 nodes, #170): the
prompts-directory scan is a real directory listing (not a fixed filename
tuple), no contract object appears inside a code fence, planning.md's one
surviving fence stays the plan-proposal.json file schema and carries none
of the contract keys, each of steer.md/reflect.md/planning.md/worker.md
states the fence-closing-line sentence, and steer.md/reflect.md/worker.md's
own examples are now shown bare -- all also `smoke` (pure text-file
assertions, no engine subprocess), and **5616 (smoke 4035, modules
344->345)** by task 040's new `tests/test_planning_reflection_additions.py`
(6 nodes, #170, #175): the practice-count sentence agrees with the numbered
list (nine items, 1..9 with no gaps), practice 9 names the freeze and the
last code-touching task, step 2c names the approach ordinal and both of
PRD fact 5's prohibitions, step 3a self-lints against the three refusal
shapes with markers (i)/(ii)/(iii), the four new/edited steps land in
2b/2c/3/3a/4 order, and planning.md still carries exactly one code fence
(the plan-proposal.json file schema) -- all also `smoke` (pure text-file
assertions, no engine subprocess), and **5621 (smoke 4040, modules
345->346)** by task 041's new `tests/test_worker_reflection_additions.py`
(5 nodes, #170, #175): worker.md now states evidence you invalidate is
yours to re-take, that a lane is re-measured in the same environment
(same checkout/venv/layout, never a fresh `git worktree` or clone), the
two refused split shapes (a forced split is a contract-ladder re-ask, a
voluntary split is a silent refusal-and-log), and that a refused
`plan_complete` is work -- plus a same-file fence-balance sanity check --
all also `smoke` (pure text-file assertions, no engine subprocess), and
**5625 (smoke 4044, modules 346->347)** by task 042's new
`tests/test_review_verdict_steer_additions.py` (4 nodes, #170, #175):
review.md now states a curable finding is a specification naming
`cured_when` and `invalidates`, verdict_gate.md now states the engine
owns the terminal record so the gate never reads `events.jsonl`, and
steer.md now states every premise a steering message rests on is
checked against the live plan before it is applied -- plus a same-file
fence-balance sanity check across all three files -- all also `smoke`
(pure text-file assertions, no engine subprocess), and **5630 (smoke 4049,
modules unchanged at 347)** by task 044's new nodes in the EXISTING
`tests/test_cli_llm_profiles.py` (no new module, #177): 5 new in-process
nodes pinning `--llm-header`'s provider-`headers:` merge on both the named-
profile `pi.providers` path and the `--llm host` copied `models.json` path
(the flag winning a same-named header, other headers kept), a `${env:...}`
value resolving on the host, and a malformed spec / an unresolvable
reference each refused before `docker run` -- all also `smoke` (no real
engine/`ralphctl` process spawned), and **5636 (smoke 4055, modules
unchanged at 347)** by task 045's new nodes across two EXISTING modules
(no new module, #177): three in `tests/test_cli_llm_profiles.py` pinning
the resolved header pairs landing in `llm-wiring.json`, a template's own
`llm_header:` list applying with no flag, and the flag winning over the
template which wins over the profile's own `pi.providers.<p>.headers:`;
three in `tests/test_cli_resume_llm_wiring.py` pinning a plain `resume`
reproducing the persisted headers unchanged, `resume --llm-header`
overriding one header while keeping another from `start`, and a
malformed `resume --llm-header` value refused -- all also `smoke` (no
real engine spawned; this entry also corrects the smoke count stated at
task 045's own commit, which had been left at the task-044 figure of 4049
rather than the re-measured 4055), and **5638 (smoke 4057, modules
unchanged at 347)** by task 046's new nodes in the EXISTING
`tests/test_cli_llm_profiles.py` (no new module, #177): a two-provider
profile's top-level `headers:` shorthand reaching both providers, and a
provider's own `headers:` map winning a same-named header while the
other top-level header still reaches that provider -- both `resolve_
profile`-direct, no real engine/`ralphctl` process spawned, also `smoke`,
and **5651 (smoke 4070, modules 347->348)** by task 047's new module
`tests/test_llm_header_visibility.py` (#177): 13 new in-process nodes
pinning `--llm-header` visibility+redaction across all four surfaces --
`resolve_header_specs`'s ref-name tracking and its last-one-wins clearing
rule, `llm_profiles.read_llm_wiring_headers` masking a ref-derived value
while leaving a literal one visible (plus its empty-dict/pre-047-file
edge cases), `ralphctl llm show <profile>` (text and `--json`),
`ralphctl status` (text and `--json`, the header block landing under the
`model:` line and absent for a header-less run), `ralphctl docs <run>
job.yaml` (text and `--json`, the real ref-resolved value never even
reaching disk), `ui_server.run_detail` (present and empty cases), and
`resume --llm-header`'s ref-name merge -- none spawn a real engine/
`ralphctl` process, so all 13 are also `smoke`, and **5656 (smoke 4075,
modules unchanged at 348)** by task 047's RE-TAKE in that SAME module (#177):
5 further in-process nodes and one browser-tier node, for the
provenance-unknown fix -- a pre-047 `llm-wiring.json` (a `headers` map with
no `headerRefNames` key) masking EVERY value instead of publishing a
task-045-era run's resolved `${env:}` secret, the all-literal post-047 file
still showing its values, `start` recording the empty provenance list that
distinguishes the two, and the same wholesale masking asserted end to end on
`ralphctl status` and `ui_server.run_detail`, plus `resume --llm-header` over
a provenance-less file keeping the old name masked -- all 5 also `smoke`;
the sixth is a `@pytest.mark.browser` node driving real Chromium over the
hub's rendered summary card (the `header: <name>` rows under the `model:`
row, the ref-derived one as `***`), which is neither fast nor smoke and so
moves neither count, and **5658 (smoke 4077, modules 348->349)** by task
048's new module `tests/test_llm_header_e2e.py` (#177, requirement J.5's
last clause -- "at the e2e tier the stub-`pi` ... observes the header on
a request"): 2 new nodes proving a `--llm-header` merge reaches the
process that would actually build the outgoing request, not merely the
config file `start` staged -- a real `ralphd-engine`, spawned through the
same `STUB_DOCKER_LIVE_ENGINE` knob `tests/test_cli_start_nodetach.py`/
`tests/test_cli_resume_llm_wiring.py` already use, with
`live_engine_supervisor.py` now additionally mirroring `container/
entrypoint.sh`'s own `/config/pi/*` -> `$HOME/.pi/agent/` copy step before
launch, and `tests/stub-pi/pi` dumping the provider `headers` maps it
observes in that same ambient `~/.pi/agent/models.json` to a new
`<run>/.stub-llm-headers.json` marker on every invocation. Task 048's
RE-TAKE reworks those same 2 nodes -- **no count moves** (fast 5658, smoke
4077, modules 349, re-measured with `--collect-only -q` after the change):
a dumped config file shows only that configuration reached the `pi`
process, never that a REQUEST carried the header, so each node now starts a
loopback CAPTURE ENDPOINT in the test process, names it as the profile's
own `baseUrl`, and asserts on the request that endpoint received
(`x-jira-id`, resolved literal, at `/v1/chat/completions`); the stub gained
the `STUB_LLM_REQUEST` knob under which it issues that POST per provider
with the provider's configured `headers` applied, recording each attempt's
url/status/header NAMES (never values) to `<run>/.stub-llm-request.json`.
Both nodes land in `smoke` too by the SAME derivation gap `test_cli_start_nodetach.py`/
`test_cli_resume_llm_wiring.py` already carry -- this module's own source
names none of `_PROCESS_BOUNDARY_RE`'s literal tokens even though it
spawns a real engine through that stub chain, so `_module_is_in_process`
reads it as in-process; disclosed here rather than silently read as proof
the module is smoke-cheap, and not this task's derivation to fix, and
**5660 (smoke 4079, modules 349->350)** by task 074's new module
`tests/test_prompt_sentinel_audit.py` (the prompt-sentinel audit, steer
007): 2 new in-process nodes -- the guard itself, scanning every shipped
`src/ralphd/prompts/*.md` file for a single declared list of literals other
tests assert the absence of elsewhere, and the fixture proving the scan is
non-vacuous (a `tmp_path`-only decoy template that trips it) -- neither
spawns a real engine/`ralphctl` process, so both are also `smoke`, and
**5677 (smoke unchanged at 4079, modules 350->351)** by task 077's new module
`tests/test_ci_dev_layout_matrix_guard.py` (the dockerized dev-layout matrix
plus the `browser-runner` pin, steer 008 task A + steer 009): 17 new
in-process nodes parsing `.github/workflows/nightly.yml` and `heavy.yml`
with PyYAML -- three pinning the real files' shape (the six-cell matrix,
no `browser-runner` label anywhere in `nightly.yml`, `heavy.yml`'s
browser tier carrying it while its docker tier does not) and fourteen proving
each check is non-vacuous against a broken `tmp_path`-only fixture built
from a deep copy of the real workflow. None spawns a real engine or
`ralphctl` process and none is marked `smoke`, so the smoke count is
unchanged.

**5659 (smoke 4062, modules unchanged at 351)** by task 053 (v0.20, #113
step 4): net -17 nodes in `test_sweep_sha_guard.py` (27 -> 10) --
the entire currency/discharge-chain/self-anchoring machinery and every test
node that existed only to exercise it, deleted with no in-process
replacement, since the retirement has no successor test to fold into --
and net -1 in `tests/test_ci_dev_layout_matrix_guard.py` (17 -> 16), the
mutation test for the now-retired required-deselect check. All 18 were
in-process (no `ralphctl`/engine process spawned), and 17 of them (the
sweep-sha guard's) were also counted in `smoke`; the matrix-guard deletion
was not, hence the one-node gap between the fast and smoke deltas.

**5663 (smoke unchanged at 4062, modules unchanged at 351)** by task 079
(steer 011 defects 1 and 2, git binary + real uid 1000 in every nightly
dev-layout cell): net +4 nodes in `tests/test_ci_dev_layout_matrix_guard.py`
(16 -> 20) -- one non-vacuous prelude check
(`test_dev_layout_cell_prelude_matches_the_real_workflow`, asserting the real
file's docker payload installs `git`, drops privileges to a non-root uid and
never gains a docker client/socket) plus three mutation tests proving that
check fires when git is dropped, the uid drop is removed, or a docker client
is added. None spawns a real engine/`ralphctl` process, so none is marked
`smoke` and the smoke count is unchanged; no new module, so the module count
is unchanged too.

**5666 (smoke 4065, modules 351->352)** by task 080 (steer 011 defect 3,
`ralphctl status` degrading instead of raising when no `docker` binary is on
`PATH`): net +3 nodes in the new `tests/test_status_docker_absent_task080.py`
-- a unit-level `_container_liveness` check plus the two named steer-011
nodes' own fixtures re-driven with `RALPHD_DOCKER` pointed at a name no
`PATH` entry provides. None spawns a real engine/`ralphctl` process (the CLI
is driven in-process through `call_ralphctl_in_process`), so all three are
also `smoke`; one new module.

**5667 (smoke 4066, modules unchanged at 352)** by task 085 (steer 013
section 2, #183, broadening the docker-absent degradation from
`_container_liveness()` to `sh()` itself): net +1 node in the same
`tests/test_status_docker_absent_task080.py`
(`test_doctor_reports_docker_unreachable_instead_of_raising`), covering
`cmd_doctor`'s own direct `sh([DOCKER, "version", ...])` call, which never
went through `_container_liveness` and so was untouched by task 080's
narrower fix. Driven in-process the same way as its three siblings, so it is
also `smoke`; no new module.

**5669 (smoke 4068, modules unchanged at 352)** by task 085's second commit
(the two named regression nodes its criteria require and its first commit at
`571fa54` omitted): net +2 nodes in the same
`tests/test_status_docker_absent_task080.py` --
`test_sh_reports_127_and_names_the_command_instead_of_raising` (drives `sh()`
itself with a name no `PATH` entry provides and pins `returncode == 127`, the
unchanged `args`, empty stdout and a stderr naming the missing binary) and
`test_container_record_exists_is_false_when_docker_is_unreachable` (the
`sh([DOCKER, "inspect", ...])` call site at
`src/ralphd/cli/main.py:5254`, which `ralphctl rm` and the hub's delete
endpoint share and which never went through `_container_liveness`). Both are
in-process, so both are also `smoke`; no new module.

**5670 (smoke 4069, modules unchanged at 352)** by task 091 (steer 018
section 1, #91, guarding `LoopSupervisor._classify_result` against a `None`
result instead of leaving a signature no caller could enforce): net +1 node
in the same `tests/test_iteration_cancellation_contained.py`
(`test_classify_result_of_none_never_raises`), directly pinning that
`_classify_result(None)` returns a fault classification rather than raising
`AttributeError`. In-process, no real engine/`ralphctl` process spawned, so
also `smoke`; no new module.

**5672 (smoke 4071, modules unchanged at 352)** by task 091's second commit,
which closes the half its first commit missed: guarding `_classify_result`
alone still left `self._last_fault_error = result.error_message or ""` (and a
dozen other `result.` reads on the recording path) dereferencing a `None`
result, so the boundary now NORMALISES a missing result into a populated
`IterationResult` before the first of those reads. Net +2 nodes in the same
`tests/test_iteration_cancellation_contained.py` --
`test_a_runner_that_returns_no_result_is_a_recorded_failure_not_an_attributeerror`
(drives `_run_iteration_once` with a runner returning `None`: fails with the
operator's exact `AttributeError` without the normalisation, passes with it)
and `test_a_cancellation_mid_iteration_is_not_turned_into_a_fault` (a cancel
delivered while the runner is in flight leaves the boundary as
`CancelledError` and records no `iteration.end`). Both in-process, so both
also `smoke`; no new module.

**5675 (smoke 4074, modules 352->353)** by task 006 of the v0.20 lane-green
approach (the `python:3.14-slim` nightly cells' `infra_retry:planning:1`
inflation of `tests/flows/test_flow_crash_resume_refunds.py`): the
startup-window watchdog now re-arms its own unchanged window while the agent
process is provably still consuming CPU, so a healthy invocation starved by a
contended host is no longer killed and retried as a no-traffic infra fault.
Net +3 nodes in one new module,
`tests/test_startup_watchdog_cpu_progress.py` -- a child that burns its own
`process_time()` past the window is not classified an infra hang, a child that
produces nothing and consumes no further CPU still is (on the first such
window, with the iteration timeout proven not to be what ended it), and
`runner.process_cpu_seconds()` reads a live pid and declines an unreadable
one. All three drive `PiRunner` in-process with a fake `pi` script, so all
three are also `smoke`.

**5679 (smoke 4074->4078, modules 353->354)** by task 012 of the same v0.20
lane-green approach, at `37f3b61` (gate's fast tier pinned to `-n 2` instead of
`-n auto`): net +4 nodes in one new module,
`tests/test_gate_pinned_worker_count_guard.py` -- the guard itself, reading
`.github/workflows/gate.yml`'s own fast-tier step for a numeric `-n` value, plus
three mutation nodes proving that guard fires when the pin is reverted to
`-n auto`, when the `-n` flag is dropped entirely, and when the records-lint step
is changed. All four parse the workflow file in-process with PyYAML (no real
engine/`ralphctl` process spawned), so all four are also `smoke`.

**No count moves at `35e3225`** (task 010 of that same approach: fast 5675,
smoke 4074, modules 353, both at its parent and at it). It reworked the five
existing nodes of `tests/test_cli_logs_follow_liveness.py` in place -- the
mid-tool-call node gained a `tmp_path` fixture for the stub's new hold file --
and added no node and no module; `src/ralphd/cli/log_render.py`'s
`render_pending_tool_invocations` fix it carries is covered by those same five.

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

**5682 (smoke 4081, modules 354->355)** by v0.20.1 task 001 (requirement
A.1): +3 fast / +3 smoke nodes in one new module,
`tests/test_approach_start_head_persistence.py`, all in-process -- the new
`_approach_start_head` persistence helper's first-read write, the literal
requirement A.1 probe (a second `LoopSupervisor` over the same run-state
tree reading the baseline back rather than re-deriving it after the
workspace gained a real commit in between), and the non-git-workspace
null-persistence case. Re-measured directly with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q`.

**5683 (smoke 4082, modules unchanged at 355)** by v0.20.1 task 002
(requirement A.2): +1 fast / +1 smoke node in the same module,
`tests/test_approach_start_head_persistence.py` --
`test_a_fresh_process_resume_after_a_commit_and_stagnation_pause_stays_on_approach_one_and_pauses_again`,
the literal end-to-end proof of task 001's fix: a real `_step_worker()`
commit-then-pause, a genuinely fresh-process `LoopSupervisor` resuming into
`worker`, and a renewed stagnation pause, still on approach 1 with no
`approaches/01` archive. Re-measured directly with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q`.

**5684 (smoke 4083, modules unchanged at 355)** by v0.20.1 task 003
(requirement A.3): +1 fast / +1 smoke node in the same module,
`tests/test_approach_start_head_persistence.py` --
`test_worker_reentry_after_an_operator_steer_stays_on_approach_one_and_pauses_again`,
the literal end-to-end proof of task 001's fix over the OTHER re-entry path:
a real `_step_worker()` commit-then-pause, an operator steer plus a real
`resume()`, worker re-entry into `steer` (settled in one `_step_generic_hat`
pass) and back into `worker` over the same approach, then a renewed
stagnation pause -- still on approach 1 with no `approaches/01` archive.
Re-measured directly with `.venv/bin/python -m pytest -m <fast|smoke>
--collect-only -q`.

**5682 (smoke 4081, modules unchanged at 355)** by v0.20.1 task 004
(requirement B.1/B.2/B.5, gate finding VG-002): net -2 fast / -2 smoke
nodes in `tests/test_worker_hat.py` -- three legacy nodes pinning the
retired per-task absorption-streak hand-off
(`test_three_consecutive_commit_only_iterations_hand_the_task_to_verify`,
`test_a_lower_configured_threshold_fires_sooner`,
`test_a_broken_streak_resets_the_counter_and_needs_a_fresh_run`) are
retired (`docs/test-suite-audit.md` "Subsumed"), replaced by ONE new node,
`test_three_consecutive_commit_only_iterations_never_force_verify_dispatch`,
pinning the opposite verdict now that `LoopSupervisor._record_task_
absorption_streak` and its call site are deleted outright: the same
three-consecutive-commit-only shape now leaves the task `in-progress`,
never dispatches `verify`. Re-measured directly with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q`.

**5683 (smoke 4082, modules 355->356)** by v0.20.1 task 005 (requirement
B.3): +1 fast / +1 smoke node in one new module,
`tests/test_progress_budget_governs_exhaustion_task005.py` --
`test_exhaustion_is_produced_by_the_progress_budget_forced_split_path_not_absorption`,
in-process (reuses `tests/test_worker_hat.py`'s `_spw010_run`/`_steps`
scripted-agent shape, no real subprocess engine) -- drives a real
`_run_job_core()` run through planning and four worker iterations that
spend a task's own progress budget for real (three corroborated commits,
a fourth dispatched once `used >= budget`), with `task_absorption_max_
iterations=1` configured far below that streak, and asserts the
forced-split path (never the deleted absorption streak) produced the
exhaustion outcome. Re-measured directly with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q`.

**5684 (smoke 4083, modules 356->357)** by v0.20.1 task 006 (requirement
B.4): +1 fast / +1 smoke node in one new module,
`tests/test_uncorroborated_claim_three_strike_task006.py` --
`test_three_uncorroborated_no_verdict_claims_still_trip_the_three_strike_guard`,
in-process (reuses `tests/test_worker_hat.py`'s `_spw010_supervisor`/
`_drive_until_paused`/`_fv_*` helpers, no real subprocess engine) -- drives
a real `_run_job_core()` run over three consecutive worker iterations
each claiming `commit` progress evidence with no real workspace delta
behind it, uncorroborated every time, and asserts the ordinary
three-strike stagnation witness still fires exactly as before task 004
deleted the legacy absorption-streak dispatch: three `progress_claim`
events all read `corroborated: False`, the `"3 iterations with no task
progress"` log line lands, and the run parks at `state: paused`/`pauseReason:
stagnation` on the (default, `max_approaches=1`) last approach, the same
outcome `tests/test_worker_hat.py::test_a_plan_with_work_left_is_never_
routed_to_review` already pins for the pre-existing guard. Re-measured
directly with `.venv/bin/python -m pytest -m
<fast|smoke> --collect-only -q`.

**5687 (smoke 4086, modules 357->358)** by v0.20.1 task 007 (requirements
C.1, C.2, gate finding VG-003): +3 fast / +3 smoke nodes in one new
module, `tests/test_cure_freeze_scheduling_task007.py` --
`test_E4_cure_precedes_high_priority_freeze_in_schedule` (the gate's own
named test, reproduced: `scheduler.select_next_task` picks the inserted
cure ahead of a pending named freeze carrying `priority: 10`, asserted on
the scheduler's own choice rather than list order), plus two more nodes
pinning the same mechanism -- the freeze becomes selectable once its cure
reaches a satisfied status, and a second cure pass appends a second
dependency rather than a duplicate. `insert_cure_tasks` now adds every
cure task it inserts to the named freeze task's own `dependsOn`, an
actual scheduling constraint the freeze's `priority` can no longer
outrun, alongside the pre-existing list-position splice (task 033).
Re-measured directly with `.venv/bin/python -m pytest -m <fast|smoke>
--collect-only -q`.

**5688 (smoke 4087, modules 358->359)** by v0.20.1 task 008 (requirement
C.3, gate `Cured when:` clause): +1 fast / +1 smoke node in one new
module, `tests/test_cure_freeze_dependent_reselection_task008.py` --
`test_C3_dependent_retake_waits_on_both_cure_and_freeze` walks the chain
task 007 proved one link of: a dependent re-take task naming the freeze
in its own `dependsOn` (with a `priority` that would win the pending
bucket outright) is not picked by `scheduler.select_next_task` while the
cure is pending, not picked while the cure is done but the freeze itself
is still pending, and only picked once both prerequisites reach a
satisfied status. Re-measured directly with `.venv/bin/python -m pytest
-m <fast|smoke> --collect-only -q`.

**5711 (smoke 4110, modules 359->361)** by v0.20.1 task 016 (requirement
E.5, #190). Re-measured directly with `.venv/bin/python -m pytest -m
<fast|smoke> --collect-only -q` immediately before this task's own
change landed and it already read **5705 (smoke 4104, modules 360)** --
17 fast / 17 smoke nodes and 1 module ahead of the 5688/4087/359 this
section last recorded, none of it this task's own doing and none of it
chased down here; disclosed rather than silently absorbed into this
task's own delta the way the standing rule for this section already asks
(see the `tests/test_debt_ledger.py`-style "pre-existing gap ... not
widened" notes above). This task's own new module,
`tests/test_repeated_rejection_signature.py`, adds the remaining +6 fast
/ +6 smoke nodes (in-process, `supervisor_factory`-built, no real
engine/`ralphctl` process spawned) and the remaining +1 module, landing
at 5711/4110/361.

**5718 (smoke 4117, modules 361->362)** by v0.20.1 task 017 (requirement
E.3, #190/#191). This task's own new module,
`tests/test_workspace_version_drift.py`, adds +7 fast / +7 smoke nodes
(in-process, `make_supervisor`-built, no real engine/`ralphctl` process
spawned) and +1 module -- no pre-existing drift found at task start.
Re-measured directly with `.venv/bin/python -m pytest -m <fast|smoke>
--collect-only -q`.

**5726 (smoke 4119, modules 362)** by v0.20.1 task 023 (requirement F.4,
#191). This task adds +1 fast / +0 smoke node and no module: one node (the
requirement F.4 observables cross-check, in that module's own F.4 section --
named without backticks here on purpose, since
`tests/test_docs_consistency.py`'s identifier corpus deliberately excludes
`tests/test_docs_semantics.py`) in
the existing, fast-only `tests/test_docs_semantics.py`. The remaining +8
fast / +2 smoke over the 5718/4117 this section last recorded is
pre-existing drift, disclosed rather than absorbed into this task's own
delta, and measured per commit (`-m <fast|smoke> --collect-only -q` in a
throwaway worktree at each sha): `a2f1b4f` (task 019) already read
5721/4117/362, `0a6c34c` (020) 5721/4117, `57614a4` (021) 5722/4118,
`314fcc6` (022) 5723/4119, `6d9b73a` (this task's own first, rejected
attempt) 5725/4119. Only the last of those is this task's, and its two
nodes are still collected here. Re-measured directly with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q`.

**5730 (smoke 4123, modules 362)** by v0.20.1 task 042, disclosing the +4
fast / +4 smoke nodes requirement G.4 (task 027) added to the existing
`tests/test_review_findings_prompt_shape.py` -- the three new review-prompt
rules plus the disposable-clone protocol check across both judging prompts.
No module was added (362 unchanged), and the fast tier's ceiling is not
raised anywhere: this entry only states the measured number the guard asks
for. Task 027's own commit added the nodes without restating the count here,
which is what turned `tests/test_tier_budgets.py::test_the_tier_wide_count_targets_are_met_or_disclosed`
red on the gate run at `d74b6a3`; that red is cured by this entry. Measured
in this same iteration at the workspace tip with `.venv/bin/python -m pytest
-q -m <fast|smoke> --collect-only`, and the module figure from the same fast
collection's distinct module paths.

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

**5633 (smoke 4026, modules 360 unchanged)** by v0.20.1 task cure-01-01
(PRD A.2 / Definition of done 1, H.3 evidence integrity): +1 fast / +1 smoke
node over the 5632/4025 task 045 last measured, from
`tests/test_approach_start_head_persistence.py::test_a_genuinely_fresh_os_process_resume_after_a_commit_and_stagnation_pause_stays_on_approach_one_and_pauses_again`,
which drives requirement A.2's `Cured when:` sequence across a real OS
process boundary (two independently spawned `python -m` child processes,
no shared python state, over the same run-state tree/workspace) instead of
two `LoopSupervisor` objects inside one shared interpreter -- the existing
probe's own shape, which the fresh-process claim it made did not actually
exercise. No module was added (the node lands in the same existing test
module), so the module count is unchanged at 360. Measured directly at
this task's own tip with `.venv/bin/python -m pytest -m <fast|smoke>
--collect-only -q`.

**5634 (smoke 4027, modules 360 unchanged)** by v0.20.1 task 001, disclosing
the drift left over the 5633/4026 task cure-01-01 last measured: task
cure-01-01-3 (requirement F, commit `830acc4`) added +1 fast / +1 smoke node,
`tests/test_pricing_aws.py::test_the_check_guard_passes_with_no_positional_source_against_the_shipped_table`,
to the existing `tests/test_pricing_aws.py` module without restating the
count here, which is what turned
`tests/test_tier_budgets.py::test_the_tier_wide_count_targets_are_met_or_disclosed`
red again. No module was added (360 unchanged). Re-measured directly at
this task's own iteration, at the workspace tip `830acc4`, with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q`.

**5732 (smoke 4125, modules 362)** by v0.20.1 task cure-02-02, restoring
the sweep-sha and conformance-ledger guard coverage that v0.20.1 tasks
044/045/046 had removed under operator ruling `001-039.md` -- a
review finding for approach 2 held that ruling not binding for that
review, so its removals are reverted (`test_sweep_sha_guard.py`,
`tests/sweep_sha_guard.py`, `docs/sweep-records.md` restored: +10 fast /
+10 smoke nodes, +1 module; `tests/records/test_conformance_ledger.py`
restored: +88 fast / +88 smoke nodes, +1 module; the historical
node/result checking removed from `tests/report_claims.py` and
`tests/records/test_report_claims.py` by task 046 is restored too, adding
no new collected nodes). Net from the pre-restoration 5634/4027/360:
+98 fast, +98 smoke, +2 modules -> 5732/4125/362. Re-measured directly at
this task's own tip with `.venv/bin/python -m pytest -m <fast|smoke>
--collect-only -q`.

**5732 (smoke 4125, modules 362, unchanged) -- 2026-09-13, re-take of
v0.20.1 task 001, at `2694209`.** cure-02-01's own push (restoring
`artifacts/reports/v0.20.1-lanes.md` and dispatching a fresh sweep) added
no test or module: this entry re-measures the fast/smoke tiers at task
001's re-take, freezing this commit as the wave's final code sha, rather
than copying the 5732/4125/362 the previous (cure-02-02) entry already
states. Measured directly at the workspace tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5732/5837 tests collected (105 deselected)`, smoke:
`4125/5837 tests collected (1712 deselected)`), with the module count taken
from the fast tier's distinct collected module paths.

**5723 (smoke 4116, modules 362, unchanged) by v0.20.2 task 003
(requirement A.1, #113 step 4): the sweep-sha practice retired entire.**
`test_sweep_sha_guard.py` (10 fast/10 smoke nodes) and
`tests/sweep_sha_guard.py` (its implementation, no test nodes of its own)
are deleted outright, along with `docs/sweep-records.md`; the sweep
enforcement is removed from `.github/workflows/gate.yml`'s "Records lint"
step and there was never an invocation in `.github/workflows/nightly.yml`
to remove. A new `tests/test_sweep_guard_retired.py` (1 fast/1 smoke node)
is the named test the requirement itself asks for, parsing both workflow
files and asserting neither one's `run:` step invokes the retired guard.
Net: -10 +1 = -9 fast, -9 smoke, module count unchanged at 362 (one module
removed, one added). From the pre-task 5732/4125/362:
5723/4116/362. Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5723/5828 tests collected (105 deselected)`, smoke:
`4116/5828 tests collected (1712 deselected)`). `tests/records/` is
unaffected (204 unchanged): none of the three deleted paths lived under
`tests/records/`.

**5629 (smoke 4022, modules 360) by v0.20.2 task 004 (requirement A.2,
#113 step 4): the conformance ledger frozen as history.**
`tests/records/test_conformance_ledger.py` (88 fast/88 smoke nodes, one
module) and `test_ledger_loopsupervisor_modules.py` (6 fast/6 smoke
nodes, one module) are both deleted outright -- A.2's own text authorises
retiring "all mechanical checking of `artifacts/reports/conformance-ledger.md`",
which covers both modules, not only the one A.2 names by path. Neither
test spawns a real engine or CLI process, so both were `smoke` as well as
`fast`; net -94 fast, -94 smoke, -2 modules. From the pre-task 5723/4116/362:
5629/4022/360. Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5629/5734 tests collected (105 deselected)`, smoke:
`4022/5734 tests collected (1712 deselected)`). `tests/records/` drops from
204 to **116** (-88, the deleted module's own 88 nodes; the top budget
table's `records, serial` row and its 300-test ceiling are reconciled by
requirement A.6's own task, not this one): re-measured directly with
`.venv/bin/python -m pytest -q tests/records/` (116 passed, 18.25 s, still
well inside the existing 30 s ceiling).

A.2's second stage, in the same task's follow-up commit, moves no count at
all and is recorded here so the reader does not go looking for one: the
generic claim checker stopped reading the ledger too (`report_files()` skips
`FROZEN_REPORTS` in `tests/report_claims.py`), which deletes no test node --
the claim families are a fixed number of nodes iterating a discovered list,
so dropping one report from that list changes nothing they collect. Verified
by re-measuring at that commit's own tip rather than assuming: fast
`5629/5734 tests collected (105 deselected)`, smoke
`4022/5734 tests collected (1712 deselected)`, 360 fast-tier modules (unique
module paths in the same fast `--collect-only` output), `tests/records/`
still 116.

**Still 5629/4022/360, `tests/records/` still 116, by v0.20.2 task 005
(requirement A.3, #113 step 4): the historical-node-id claim check retired,
the anti-fabrication check kept.** `node_problems()` in `tests/report_claims.py`
-- the check that a `path::node` claim in any report under
`artifacts/reports/` must still resolve to a real test -- is deleted
outright, along with its two call sites (inside
`tests/records/test_report_claims.py::test_every_report_passes_its_claim_checks`
and `tests/test_issue_traceability.py`, the latter renamed
`test_every_path_this_report_names_exists` since it no longer checks a node).
Both call sites were plain function calls inside an existing test's own
body, never a parametrized node of their own, so this retirement deletes no
pytest node: re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5629/5734 tests collected (105 deselected)`, smoke:
`4022/5734 tests collected (1712 deselected)`) and
`.venv/bin/python -m pytest -q tests/records/` (116 passed), all identical
to task 004's own count. The result-line half of A.3 is a no-op here: that
check lived in `tests/sweep_sha_guard.py`, already deleted whole by A.1;
`tests/report_claims.py` never had one of its own. Kept, unweakened:
`commit_problems()` (the sha-reachability anti-fabrication guard A.3 names
by its own test, `test_a_dangling_commit_sha_fails_and_a_reachable_one_passes`),
`path_problems()`, and `retired_problems()` (both its path and node forms --
the opposite, "must NOT resolve", obligation, which A.3 does not touch). The
`missing`-kind failure text drops its now-dangling `docs/sweep-records.md`
pointer (deleted by A.1) while keeping the required "do not backtick it"
guidance.

**Still 5629/4022/360, `tests/records/` still 116, by v0.20.2 task 006
(requirement A.4) and task 007 (requirement A.5).** Task 006 deletes
`artifacts/reports/v0.20.1-lanes.md`, a report with no pytest node of its own,
and touches `tests/report_claims.py` only to add the narrow
`FROZEN_PATH_CITATIONS` allowlist (a data tuple, not a test); task 007 adds
prose to `docs/test-suite-audit.md`, no test code. Neither moves a collected
count in any tier or in `tests/records/`; re-measured directly at each task's
own tip with the same `--collect-only` recipe, confirming no drift.

**Table synced to 5629/4022/360 (fast/smoke/modules), `tests/records/` to
116, by v0.20.2 task 008 (requirement A.6): the counts A.1-A.4 removed since
the table's last full measurement, and the ceilings that bounded the old
count, both reconciled.** The live counts above had already drifted to
5629/4022/360 by task 004 and stayed there through tasks 005-007 (this
section's own preceding entries), while the top budget table still carried
sha `9b1a0e2`'s pre-launch 5329/3768/322 and the `tests/records/` row still
carried its own pre-`A.2` 204. This task re-measures the smoke, fast and
records rows fresh (see the "Task 008 (v0.20.2, requirement A.6)
re-measurement" paragraph above the table for the exact recipe and summary
lines) and writes the live counts into the table itself, closing the gap
between what the disclosure paragraphs above already said and what the
table's own cells claimed. The fast tier's own count is stated beside the
wave's launch baseline once more here, for a reader who lands on this
paragraph without the commit message: **5629** measured at this task's own
tip against **5732** at launch (task 001, sha `e1d5226`), a drop of 103
nodes -- 10 from A.1's sweep-sha retirement, 94 from A.2's conformance-ledger
retirement (both already accounted for above), -1 net from a module rename
wash within A.1's own removal; A.3, A.4 and A.5 changed no count. The
ceilings move down with them, in the same commit, per A.6's own "downward
only" rule: smoke 5100->4300 tests, 300->260 modules; fast 7000->5800 tests,
430->375 modules; records 300->150 tests, 15->10 modules (the matching
`COLLECTED_BUDGET` constant in
`tests/records/test_records_family_budget.py` moves from 300 to 150 in the
same commit). Every new ceiling still sits above today's own measurement --
headroom for tasks 009-016, still ahead of the F.5/A.7 freeze line, to add a
modest number of new nodes without this task's own reconciliation forcing an
immediate second pass -- but every one of them is strictly lower than the
value it replaces, never raised.

**5655 (smoke 4048, modules unchanged at 363) by v0.20.2 review-cure pass 1
(cure-01-01, requirement A.3, and cure-01-02, requirement B.3 DoD 9).**
cure-01-01 (`22fc372`) narrowed
`test_the_retired_marker_direction_is_enforced` to the path form of
`!retired` and added
`test_a_retired_node_citation_is_never_rechecked_against_live_source`, a wash of
-1/+1 net +1 fast/smoke node, taking the tier from the 5650/4043/363 task
015 last measured to 5651/4044/363 -- left undisclosed here at the time
(the guard's own substring check happened to still pass, since "5651"
already occurs elsewhere in this doc for an unrelated v0.19 count, at a
different task). cure-01-02 (this cure) then joins four more named tests
to `tests/test_notifier_startup_warning.py` (three: a `notifier_url: ""`
job.yaml fixture warns unconfigured, an omitted key warns the same way, a
configured nonempty URL warns nothing -- no new module) and
`tests/test_cli_doctor_notifier_config.py` (one: the same empty-string
fixture is reported unconfigured by `doctor` too -- no new module); all
four are `smoke` as well as `fast`, mirroring their modules' existing
tests. Net across both cures from 5650/4043/363: **5655/4048/363**.
Re-measured directly at this task's own tip with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q` (fast: `5655/5760 tests
collected (105 deselected)`, smoke: `4048/5760 tests collected (1712
deselected)`), and fast-tier module count via the same recipe used
throughout this section (unique module paths in the fast `--collect-only`
output): still 363. `tests/records/` is unaffected (116 unchanged). Both
ceilings (5800 tests/375 modules fast, 4300/260 smoke) still carry
headroom.

**5664 (smoke 4057, modules unchanged at 363) by v0.21 tasks 001-003
(requirements A.1-A.3), left undisclosed here at the time.** Task 001
(`a74db530`) widens `tests/test_peer_verdict_gate.py` (a new gate-cure-pass
test plus supporting scripted-fixture helpers) and `tests/test_verdict_gate.py`;
task 002 (`2ae026e`, `79a36d7`) adds `tests/test_cure_pass.py` as a new module
first, then folds its cases into the existing cure-pass coverage once the
run-wide ceiling landed (`0607a37` is docs-only, no test change); task 003
(`aa4f50b`) adds one more named test,
`test_a_mix_of_curable_and_incurable_gate_findings_still_ends_the_run`, to
`tests/test_peer_verdict_gate.py`. Net across all three from the
5655/4048/363 this section last measured: **5664/4057/363** (+9 fast, +9
smoke, +0 modules -- no task added a new module, so the guard's own
substring check on the raw number happened to keep passing until the count
it measures moved to a value this doc had never stated, exactly the gap
this paragraph now closes). Re-measured directly at task 004's own tip
(next paragraph) rather than re-checked out at each of these three shas in
turn, since none of the three touched `tests/records/` or moved the module
count, and the fast/smoke totals compose additively across them.

**5668 (smoke 4061, modules 363->364) by v0.21 task 004 (requirement
A.4).** Adds one new module, `tests/test_cli_status_gate_rejection_
ending.py`, with four named tests (one per `gateRejectionEnding` value
`ralphctl status` renders distinct wording for, plus the fourth proving a
terminal run dir with no such field still renders) -- all four are
`smoke` as well as `fast` (no live engine/container, a seeded status.json
read back through the CLI's on-disk fallback path, the same harness
`tests/test_cli_status_finishing.py`'s own `deadlineBeatVerdict` tests
use). From the 5664/4057/363 the paragraph above reconciles: **5668/4061/
364**. Re-measured directly at this task's own tip with `.venv/bin/python
-m pytest -m <fast|smoke> --collect-only -q` (fast: `5668/5773 tests
collected (105 deselected)`, smoke: `4061/5773 tests collected (1712
deselected)`), and the module count via the same unique-module-path recipe
used throughout this section: `364`. `tests/records/` is unaffected (117
measured at this task's own tip -- a further +1 drift from the 116 this
section's own last `tests/records/` mention states, already present before
this task and untouched by it; left for a task that actually touches that
directory to reconcile). Both ceilings (5800 tests/375 modules fast,
4300/260 smoke) still carry headroom.

**5671 (smoke 4064, modules 364->365) by v0.21 task 005 (requirement B.1).**
Adds one new module, `tests/test_legal_rewrite_reprompt.py`, with three named
tests (both fast and smoke -- no live engine/container: two drive
`plan_ingestion.lint_proposal` directly, the third drives `LoopSupervisor`
through a scripted `_ScriptedRunner`, the same shape
`tests/test_plan_ingestion_rejection.py` already uses). From the
5668/4061/364 the paragraph above measures: **5671/4064/365**. Re-measured
directly at this task's own tip with `.venv/bin/python -m pytest -m
<fast|smoke> --collect-only -q` (fast: `5671/5776 tests collected (105
deselected)`, smoke: `4064/5776 tests collected (1712 deselected)`), and the
module count via the same unique-module-path recipe used throughout this
section: `365`. `tests/records/` is unaffected (still 117, untouched by this
task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke) still
carry headroom.

**5682 (smoke 4075, modules 365) by v0.21 task 005's second attempt
(requirement B.1).** The task's first attempt was rejected because the
suggested rewrite it emitted was not itself legal (feeding it back into
`lint_proposal` drew a fresh refusal), so the rewrite builder now checks
every candidate against every clause-scoped lint before emitting it and
`tests/test_legal_rewrite_reprompt.py` grew the cover that proves it: a
parametrised `test_the_emitted_suggestion_is_itself_accepted` (10 cases, one
per banned shape plus the two clauses the rejection quoted) and
`test_every_shapes_own_default_rewrite_is_legal`, +11 tests in the same
module, no new module. From the 5671/4064/365 the paragraph above measures:
**5682/4075/365**. Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5682/5787 tests collected (105 deselected)`, smoke: `4075/5787 tests
collected (1712 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `365`.
`tests/records/` is unaffected (still 117, untouched by this task). Both
ceilings (5800 tests/375 modules fast, 4300/260 smoke) still carry headroom.

**5684 (smoke 4077, modules unchanged at 365) by v0.21 task 006 (requirement
B.2).** Two new nodes in the EXISTING `tests/test_criteria_bound_lint.py`
module (no new module):
`test_an_anaphoric_quantifier_bound_by_an_earlier_clause_is_accepted` and
`test_an_anaphoric_bound_elsewhere_does_not_rescue_an_unrelated_bare_
quantifier`, both also `smoke` (no live engine/container: both drive
`plan_ingestion.lint_proposal` directly, the same shape every other test in
that module already uses). From the 5682/4075/365 the paragraph above
measures: **5684/4077/365**. Re-measured directly at this task's own tip
with `.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5684/5789 tests collected (105 deselected)`, smoke: `4077/5789 tests
collected (1712 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `365`.
`tests/records/` is unaffected (still 117, untouched by this task). Both
ceilings (5800 tests/375 modules fast, 4300/260 smoke) still carry headroom.

**5685 (smoke 4078, modules 365->366) by v0.21 task 007 (requirement B.3).**
One new module, `tests/test_planning_death_terminal_reason.py`, one new
node: `test_a_repeated_planning_death_names_the_lexical_lint_and_quotes_the_
clause` (also `smoke` -- an in-process `LoopSupervisor`/`_ScriptedRunner`
flow, no live engine/container, the same shape
`tests/test_plan_ingestion_rejection.py` already uses). No existing node's
count changed (`tests/test_contract_events.py`'s repeated-ingestion-
signature test only had one assertion string updated, not a new node).
From the 5684/4077/365 the paragraph above measures: **5685/4078/366**.
Re-measured directly at this task's own tip with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q` (fast: `5685/5790 tests collected
(105 deselected)`, smoke: `4078/5790 tests collected (1712 deselected)`),
and the module count via the same unique-module-path recipe used
throughout this section: `366` (fast), `251` (smoke). `tests/records/` is
unaffected (still 117, untouched by this task). Both ceilings (5800
tests/375 modules fast, 4300/260 smoke) still carry headroom.

**5692 (smoke 4085, modules 367 (fast), 252 (smoke)) by v0.21 task 008
(requirement B.4).** One new module,
`tests/test_worker_split_refusal_reprompt.py`, seven new nodes (also
`smoke` -- no live engine/container: three drive `_worker_assignment_extra`
directly, three drive `_apply_worker_proposed_tasks` against a real
`RunDir`, and one drives a real `LoopSupervisor._run_job_core()` through a
scripted `ScriptedRunner`, the same shape
`tests/test_deferred_tasks_surface.py` already uses for its own
production-call-site pin). From the 5685/4078/366 the paragraph above
measures: **5692/4085/367**. Re-measured directly at this task's own tip
with `.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5692/5797 tests collected (105 deselected)`, smoke: `4085/5797 tests
collected (1712 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `367` (fast),
`252` (smoke). `tests/records/` is unaffected (still 117, untouched by
this task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke)
still carry headroom.

**5694 (smoke 4087, modules 368 (fast), 253 (smoke)) by v0.21 task 009
(requirement C.1/C.2, #187).** One new module,
`tests/test_petition_deadlock_adjudication.py`, two new nodes (also
`smoke` -- no live engine/container: one drives `LoopSupervisor._verify_task`
directly to exhaustion, one drives a real `_step_worker()` pass over a
scripted `ScriptedRunner`, both against a real `RunDir`, no live
engine/container involved). From the 5692/4085/367 the paragraph above
measures: **5694/4087/368**. Re-measured directly at this task's own tip
with `.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5694/5799 tests collected (105 deselected)`, smoke: `4087/5799 tests
collected (1712 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `368` (fast),
`253` (smoke). `tests/records/` is unaffected (still 117, untouched by
this task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke)
still carry headroom.

**5700 (smoke 4093, modules 369 (fast), 254 (smoke)) by v0.21 task 073
(steer 001 §2).** One new module, `tests/test_loop_driving_await_guard.py`,
six new nodes (also `smoke` -- no live engine/container at all: the guard
reads test-module source text with a regex and diffs it against its own
in-code allowlist, entirely in-process; its four mutation nodes read/write
only `tmp_path` scratch files, never the real `tests/` tree). From the
5694/4087/368 the paragraph above measures: **5700/4093/369**.
Re-measured directly at this task's own tip with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q` (fast: `5700/5805 tests
collected (105 deselected)`, smoke: `4093/5805 tests collected (1712
deselected)`), and the module count via the same unique-module-path
recipe used throughout this section: `369` (fast), `254` (smoke).
`tests/records/` is unaffected (still 117, untouched by this task). Both
ceilings (5800 tests/375 modules fast, 4300/260 smoke) still carry
headroom.

**5702 (smoke 4095, modules 370 (fast), 255 (smoke)) by v0.21 task 010
(requirement C.3, #149).** One new module,
`tests/test_petition_hold_report.py`, two new nodes (also `smoke` -- no
live engine/container: both drive `LoopSupervisor._step_worker()` over a
scripted `ScriptedRunner` against a real `RunDir`, one across two held
passes of the same call, one across two separate `LoopSupervisor`
instances built over the same run dir). From the 5700/4093/369 the
paragraph above measures: **5702/4095/370**. Re-measured directly at
this task's own tip with `.venv/bin/python -m pytest -m <fast|smoke>
--collect-only -q` (fast: `5702/5807 tests collected (105 deselected)`,
smoke: `4095/5807 tests collected (1712 deselected)`), and the module
count via the same unique-module-path recipe used throughout this
section: `370` (fast), `255` (smoke). `tests/records/` is unaffected
(still 117, untouched by this task). Both ceilings (5800 tests/375
modules fast, 4300/260 smoke) still carry headroom.

**5704 (smoke 4097, modules 371 (fast), 256 (smoke)) by v0.21 task 011
(requirement C.4/C.5, #187).** One new module,
`tests/test_petition_hold_bound.py`, two new nodes (also `smoke` -- no
live engine/container: one drives `LoopSupervisor._step_worker()` over a
scripted `ScriptedRunner` against a real `RunDir` to prove the new
`PETITION_HELD_MAX_NO_TASK_ITERATIONS` bound actually cuts a held run
off; one is a pure-data check of `transitions.TASK_STATUS_TABLE`/
`_midrun_adjudication_extra`'s own prompt text, no engine call at all).
From the 5702/4095/370 the paragraph above measures: **5704/4097/371**.
Re-measured directly at this task's own tip with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q` (fast: `5704/5809 tests
collected (105 deselected)`, smoke: `4097/5809 tests collected (1712
deselected)`), and the module count via the same unique-module-path
recipe used throughout this section: `371` (fast), `256` (smoke).
`tests/records/` is unaffected (still 117, untouched by this task). Both
ceilings (5800 tests/375 modules fast, 4300/260 smoke) still carry
headroom -- fast is now within 96 of its stated ceiling.

**5709 (smoke 4101, modules 371 (fast), 256 (smoke)) by v0.21 task 012
(requirement D.1/D.2, #184).** No new module -- five new nodes in two
existing ones. `tests/test_steer.py` gains four (also `smoke` -- no live
engine/container: all drive `hat_tools.SteerToolBroker.execute()` against
a real `RunDir`, in-process): `test_dependson_write_is_refused_on_a_task_the_picker_has_claimed[in-progress]`
and `[validation-failed]`, `test_dependson_refusal_leaves_other_requested_fields_applied`,
`test_dependson_write_to_a_pending_task_stays_legal`. `tests/test_cli_steer_list.py`
gains one, `fast`-only (the module also drives a real `ralphctl`
subprocess elsewhere, so `_module_is_in_process` classifies the whole
file out of `smoke`): `test_a_dependson_refusal_is_readable_in_ralphctl_steer_list`.
From the 5704/4097/371 the paragraph above measures: **5709/4101/371**.
Re-measured directly at this task's own tip with `.venv/bin/python -m
pytest -m <fast|smoke> --collect-only -q` (fast: `5709/5814 tests
collected (105 deselected)`, smoke: `4101/5814 tests collected (1713
deselected)`), and the module count via the same unique-module-path
recipe used throughout this section: `371` (fast), `256` (smoke).
`tests/records/` is unaffected (still 117, untouched by this task). Both
ceilings (5800 tests/375 modules fast, 4300/260 smoke) still carry
headroom.

**5711 (smoke 4103, modules 371 (fast), 256 (smoke)) by v0.21 task 013
(requirement D.3, #184).** No new module -- two new nodes in an existing
one. `tests/test_steer.py` gains two (also `smoke` -- no live
engine/container: both drive `hat_tools.SteerToolBroker.execute()`
against a real `RunDir`, in-process): `test_successcriteria_write_stays_legal_on_a_task_the_picker_has_claimed[in-progress]`
and `[validation-failed]`. From the 5709/4101/371 the paragraph above
measures: **5711/4103/371**. Re-measured directly at this task's own
tip with `.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q`
(fast: `5711/5816 tests collected (105 deselected)`, smoke: `4103/5816
tests collected (1713 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `371` (fast),
`256` (smoke). `tests/records/` is unaffected (still 117, untouched by
this task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke)
still carry headroom -- fast is now within 89 of its stated ceiling.

**5712 (smoke 4104, modules 371 (fast), 256 (smoke)) by v0.21 task 014
(requirement D.4, #184).** No new module -- one new node in an existing
one, also `smoke` (no live engine/container: drives
`LoopSupervisor._step_generic_hat`/`run_iteration` against a real `RunDir`,
in-process): `tests/test_steer.py::test_an_unsettled_message_is_withheld_until_a_non_steer_iteration_completes`.
From the 5711/4103/371 the paragraph above measures: **5712/4104/371**.
Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5712/5817 tests collected (105 deselected)`, smoke: `4104/5817 tests
collected (1713 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `371` (fast),
`256` (smoke). `tests/records/` is unaffected (still 117, untouched by
this task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke)
still carry headroom -- fast is now within 88 of its stated ceiling.

**5713 (smoke 4105, modules 371 (fast), 256 (smoke)) by v0.21 task 015
(requirement D.5, #184).** No new module -- one new node in an existing
one, also `smoke` (no live engine/container: drives
`LoopSupervisor._step_generic_hat`/`_flush_deferred_steering_redelivery`
against a real `RunDir`, in-process):
`tests/test_steer.py::test_a_crash_between_the_counter_persist_and_the_unconsume_never_exceeds_the_cap`.
From the 5712/4104/371 the paragraph above measures: **5713/4105/371**.
Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5713/5818 tests collected (105 deselected)`, smoke: `4105/5818 tests
collected (1713 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `371` (fast),
`256` (smoke). `tests/records/` is unaffected (still 117, untouched by
this task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke)
still carry headroom -- fast is now within 87 of its stated ceiling.

**5714 (smoke 4106, modules 371 (fast), 256 (smoke)) by v0.21 task 016
(requirement D.6, #184).** No new module -- one new node in an existing
one, also `smoke` (no live engine/container: drives
`LoopSupervisor._step_generic_hat`/`_flush_deferred_steering_redelivery`/
`_terminal_success_blockers` against a real `RunDir`, in-process):
`tests/test_steer.py::test_a_deferred_but_not_yet_flushed_redelivery_still_blocks_the_terminal_gate`.
From the 5713/4105/371 the paragraph above measures: **5714/4106/371**.
Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5714/5819 tests collected (105 deselected)`, smoke: `4106/5819 tests
collected (1713 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `371` (fast),
`256` (smoke). `tests/records/` is unaffected (still 117, untouched by
this task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke)
still carry headroom -- fast is now within 86 of its stated ceiling.

**5716 (smoke 4106, modules 371 (fast), 256 (smoke)) by v0.21 task 017
(requirement D.7, #196).** No new module -- two new nodes in an existing
one, `fast` only (`tests/test_operator_ruling.py` spawns a real
`ralphctl` subprocess, so the whole module is process-boundary, not
in-process, per `tests/conftest.py`'s own derivation):
`test_amend_wave_records_a_wave_scoped_ruling_with_no_task_id` and
`test_amend_list_shows_the_scope_column_for_a_wave_scoped_ruling`. From
the 5714/4106/371 the paragraph above measures: **5716/4106/371** (smoke
unchanged -- neither new node is in-process). Re-measured directly at
this task's own tip with `.venv/bin/python -m pytest -m <fast|smoke>
--collect-only -q` (fast: `5716/5821 tests collected (105 deselected)`,
smoke: `4106/5821 tests collected (1715 deselected)`), and the module
count via the same unique-module-path recipe used throughout this
section: `371` (fast), `256` (smoke). `tests/records/` is unaffected
(still 117, untouched by this task). Both ceilings (5800 tests/375
modules fast, 4300/260 smoke) still carry headroom -- fast is now within
84 of its stated ceiling.

**5718 (smoke 4106, modules 371 (fast), 256 (smoke)) by v0.21 task 018
(requirement D.8, #196).** No new module -- two new nodes in an existing
one, `fast` only (`tests/test_operator_ruling.py` spawns a real
`ralphctl` subprocess elsewhere in the module, so the whole module is
process-boundary, not in-process, per `tests/conftest.py`'s own
derivation):
`test_wave_scoped_ruling_survives_a_replan_and_stays_binding` (review's
half) and
`test_wave_scoped_ruling_reaches_the_peer_verdict_gate_after_a_replan`
(the peer verdict gate's half, added by this task's rework). From the
5716/4106/371 the paragraph above measures: **5718/4106/371** (smoke
unchanged -- neither new node is in-process). Re-measured directly at
this task's own tip with `.venv/bin/python -m pytest -m <fast|smoke>
--collect-only -q` (fast: `5718/5823 tests collected (105 deselected)`,
smoke: `4106/5823 tests collected (1717 deselected)`), and the module
count via the same unique-module-path recipe used throughout this
section: `371` (fast), `256` (smoke). `tests/records/` is unaffected
(still 117, untouched by this task). Both ceilings (5800 tests/375
modules fast, 4300/260 smoke) still carry headroom -- fast is now within
82 of its stated ceiling.

**5721 (smoke 4106, modules 371 (fast), 256 (smoke)) by v0.21 task 019
(requirement D.9/D.10, #196).** No new module -- three new nodes in an
existing one, `fast` only (`tests/test_operator_ruling.py` spawns a real
`ralphctl` subprocess elsewhere in the module, so the whole module is
process-boundary, not in-process, per `tests/conftest.py`'s own
derivation):
`test_binding_disposition_carries_no_historical_or_orphaned_heading`,
`test_superseded_disposition_keeps_todays_historical_heading_and_wording`
and
`test_orphaned_disposition_renders_binding_with_a_caveat_naming_the_task`.
A fourth node in the same module was renamed, not added -- task 037's
absent-id node (old name, deliberately unbackticked here because it no
longer exists in the code: test_entry_naming_an_id_absent_from_the_plan_
never_binds_and_emits_once) is now
`test_entry_naming_an_id_absent_from_the_plan_is_orphaned_not_historical`,
same node count. From the 5718/4106/371 the paragraph above measures:
**5721/4106/371** (smoke unchanged -- none of the three new nodes is
in-process). Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5721/5826 tests collected (105 deselected)`, smoke: `4106/5826 tests
collected (1720 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `371` (fast),
`256` (smoke). `tests/records/` is unaffected (still 117, untouched by
this task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke)
still carry headroom -- fast is now within 79 of its stated ceiling.

**5723 (smoke 4106, modules 371 (fast), 256 (smoke)) by v0.21 task 020
(requirement D.11, #196).** No new module -- two new nodes in an
existing one, `fast` only (`tests/test_operator_ruling.py` spawns a real
`ralphctl` subprocess elsewhere in the module, so the whole module is
process-boundary, not in-process, per `tests/conftest.py`'s own
derivation):
`test_orphaning_notifies_the_operator_naming_ruling_target_and_approach`
(the notification payload, driven through the same `hat_tools.
_http_post_text` stub `test_workspace_version_drift.py` uses) and
`test_amend_list_shows_the_orphaned_state_column_without_events_jsonl`
(the `ralphctl amend --list` `STATE` column and its `--json` `orphaned`
key, a subprocess node). From the 5721/4106/371 the paragraph above
measures: **5723/4106/371** (smoke unchanged -- neither new node is
in-process). Re-measured directly at this task's own tip with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q` (fast:
`5723/5828 tests collected (105 deselected)`, smoke: `4106/5828 tests
collected (1722 deselected)`), and the module count via the same
unique-module-path recipe used throughout this section: `371` (fast),
`256` (smoke). `tests/records/` is unaffected (still 117, untouched by
this task). Both ceilings (5800 tests/375 modules fast, 4300/260 smoke)
still carry headroom -- fast is now within 77 of its stated ceiling.

