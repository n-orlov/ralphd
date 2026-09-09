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
collects **5363 tests across 325 modules** at `ed9112b`, over the PRD's
`< 3000` tests / `<= 180` modules targets, and the smoke tier **3799 tests**,
all re-measured directly with
`.venv/bin/python -m pytest -m <fast|smoke> --collect-only -q`. Recorded here
because `test_tier_budgets.py::test_the_tier_wide_count_targets_are_met_or_disclosed`
requires the live number to be *stated* once it exceeds a target -- disclosure
is a visible, reviewable act rather than a silent drift. The number moved
because this wave's own tasks add test modules: it was 5329 at `9b1a0e2`, 5332
at the wave's first commit (`1ba8c46`, task 001's new
`tests/test_unconsumed_steering_live.py`), 5358 by task 011, and 5363 by task
012's `fed9096c` (`tests/test_progress_claim_corroboration.py`, 5 new nodes,
plus the fingerprint-corroboration coverage it added to
`tests/test_worker_progress_claim.py`).

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
