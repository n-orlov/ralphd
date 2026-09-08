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

Measured in this container (28 cores) with the recipes above: all four smoke
and fast rows re-measured at sha `a1b201c` (task 039, requirement G.2 -- the
sha this task's own commit is built on top of). The "budget" columns are the
declared ceilings this suite is held to; `tests/test_tier_budgets.py` fails when a recorded measurement
exceeds one, and independently asks pytest what each tier collects right now and
fails when *that* is over the ceiling. The ceilings carry deliberate headroom
over today's measurements so the wave's remaining flow modules can land, and are
tightened as the counts come down -- they are a ratchet, not a target.

| lane | marker | recipe | tests | modules | wall clock | budget tests | budget modules | budget wall clock | PRD target | target met? |
|---|---|---|---|---|---|---|---|---|---|---|
| smoke, parallel | `smoke` | `-m smoke -n 8 --dist loadfile` | 4613 | 208 | 311.12 s | 5100 | 300 | 60 s | n/a | no target of this kind for this lane |
| smoke, serial | `smoke` | `-m smoke` | 4613 | 208 | 448.34 s | 5100 | 300 | 200 s | < 120 s | MISSED -- 448.34 s measured against the 120 s target |
| fast, parallel | `fast` | `-m fast -n 8 --dist loadfile` | 6166 | 321 | 325.40 s | 7000 | 430 | 150 s | < 120 s | MISSED -- 325.40 s measured against the 120 s target |
| fast, serial | `fast` | `-m fast` | 6166 | 321 | 802.14 s | 7000 | 430 | 1100 s | < 300 s | MISSED -- 802.14 s measured against the 300 s target |
| records, serial | n/a (selected by directory, not marker) | `.venv/bin/python -m pytest -q tests/records/` | 194 | 4 | 5.08 s | 300 | 15 | 15 s | < 15 s | met -- 5.08 s inside the 15 s target, 194 tests inside the 300-test target |

Task 039 (requirement G.2) re-measured the serial fast lane directly
(`.venv/bin/python -m pytest -q -m fast`, 802.14 s at sha `a1b201c`) rather than
carrying forward the audit's older 981 s figure at sha 836adc1 -- the two
selectors differ by the seven dev-layout tests, and letting the number drift
further from the audit's own sha risked understating how much the tier had
changed since. The parallel lane is still the one the loop uses day to day.

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

### The records-lint family (`tests/records/`, task 035, requirement E.4)

Measured serially (`.venv/bin/python -m pytest -q tests/records/`, repeated
three times, 5.35-5.37 s): **194 tests collected** across 4 test modules (plus
a `conftest.py`: `test_conformance_ledger.py`, `test_records_family_budget.py`,
`test_report_claims.py`, `test_roadmap.py`), against the PRD's **300-test** /
**15 s** targets -- both met, so neither is disclosed here as a miss.

**The subprocess rule: one `git ls-files` for a whole run, and nothing else.**
`tests/records/test_records_family_budget.py::test_the_records_family_spawns_only_one_git_ls_files`
pins it live -- it patches `subprocess.run`, runs the rest of the family
in-process from a cold cache, and fails on a second `git ls-files` or on any
other command at all. Three fixes in this task got the count from ~220 down to
one:

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

The one remaining call is `path_problems()`'s `git ls-files` snapshot, memoised
for the process: path membership genuinely is a question about git's index, and
nothing in the family asks it twice.

### Targets missed, with their numbers

Requirement G's rule is that a missed target is disclosed with its number, not
hidden. Re-measured at sha `a1b201c` (task 039, requirement G.2): all five of
G.8's wall-clock/count targets checked here are missed.

- **smoke serial 448.34 s** against a target of < 120 s. At `-n 8` the same
  tier is **311.12 s** -- also over its own (n/a-target) row, see below.
- **6166 tests collected** (fast tier) against a target of at most 3000. Down
  from the 6418 recorded at the previous measurement, but still well over
  target; the count reduction from folding the per-task pins (D.7) and
  consolidating the records lint (E) was not enough to clear it.
- **321 modules collected** (fast tier) against a target of at most 180, for
  the same reason -- down from 394.
- **fast, `-n 8`, 325.40 s** against a target of < 120 s. This target was met
  at the previous measurement (111.79 s); it is missed now.
- **fast serial 802.14 s** against a target of < 300 s -- down from the prior
  981 s figure, but still over target.

**A ceiling was breached, not just a PRD target.** Three of the four re-measured
rows now exceed their own declared `budget wall clock` (smoke parallel 311.12 s
> 60 s, smoke serial 448.34 s > 200 s, fast parallel 325.40 s > 150 s; fast
serial 802.14 s stays inside its 1100 s ceiling). Repeated measurement in this
container gave consistent numbers for the parallel recipes (two independent
`-m smoke -n 8` runs: 312.21 s and 311.12 s) while this same container showed
~22 GiB of swap in use out of 39 GiB against 62 GiB total RAM at the time of
measurement (`free -h`), a host-level condition this run does not control and
did not create. Per the ratchet rule below, the ceilings are not raised here;
the breach is filed as an impossibility petition (`artifacts/reports/
v0.19.1-petitions.md`) rather than absorbed into this table silently.

The two dev-layout modes' nightly target (under 15 min each, from ~40) is
measured by the sweep lanes, not here.

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
