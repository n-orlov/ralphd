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
than taste: a real engine, real sleep or subprocess spawn accounts for most of
the serial wall clock, so excluding the spawning modules is what makes a
per-task tier affordable at all.
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

Two different things are checked under this name, by `tests/test_tier_budgets.py`:

* **Wall clock (recorded).** Measuring a tier's wall clock means *running* it,
  which no per-task check can afford (the fast tier is minutes), so the table
  below is a recorded measurement, checked against the recorded budget beside
  it and against the PRD's own target, with a miss stated with its measured
  number rather than rounded quietly into compliance.
* **Collected count (live, in code).** How many tests and modules `smoke`/
  `fast` collect right now is asked of pytest itself at check time and
  compared against a ratcheting ceiling, `TIER_CEILINGS` in
  `tests/test_tier_budgets.py` (v0.21 task 062, requirement N.4) -- a code
  constant, not a cell in the table below, so it cannot go stale silently
  when this file is edited. A tier that grows past its ceiling is raised
  there, with the measurement that earned the raise named in the commit
  message that raises it, never recorded in this doc (v0.21 task 063,
  requirement N.4 -- a committed test-count or module-count disclosure is
  the class of record this PRD prohibits outright).

| lane | marker | recipe | wall clock | budget wall clock | PRD target | target met? |
|---|---|---|---|---|---|---|
| smoke, parallel | `smoke` | `-m smoke -n 8 --dist loadfile` | 24.41 s | 60 s | n/a | met -- 24.41 s inside the lane's own 60 s budget ceiling; no PRD target of this kind for this lane |
| smoke, serial | `smoke` | `-m smoke` | 147.68 s | 200 s | < 120 s | MISSED -- 147.68 s measured against the 120 s target |
| fast, parallel | `fast` | `-m fast -n 8 --dist loadfile` | 80.12 s | 150 s | < 120 s | met -- 80.12 s inside the 120 s target |
| fast, serial | `fast` | `-m fast` | 540.44 s (stale wall clock, not retaken since -- the standing rule caps one full fast-tier run per iteration and it is spent on the parallel recipe above) | 1100 s | < 300 s | MISSED -- 540.44 s measured against the 300 s target |
| records, serial | n/a (selected by directory, not marker) | `.venv/bin/python -m pytest -q tests/records/` | 11.35 s | 30 s | < 15 s | met -- 11.35 s inside the 15 s target |
| dev-layout, `PYTHONUSERBASE` unset (retired unfiltered measurement, superseded by task 076) | n/a (no marker; the whole suite, unfiltered -- this recipe no longer runs; see below) | `HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE=unset scripts/host_like_dev_layout.sh` | 2047.32 s | 3600 s | < 900 s (15 min) | MISSED -- 2047.32 s measured against the 900 s target |
| dev-layout, `PYTHONUSERBASE` exported (retired unfiltered measurement, superseded by task 076) | n/a (no marker; the whole suite, unfiltered -- this recipe no longer runs; see below) | `scripts/host_like_dev_layout.sh` (default mode, no `HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE` override) | 2146.54 s | 3600 s | < 900 s (15 min) | MISSED -- 2146.54 s measured against the 900 s target |

**The two dev-layout rows above are retired measurements (task 076, steer
008).** `scripts/host_like_dev_layout.sh` no longer runs the whole suite
unfiltered by default: its own `pytest` invocation now adds `-m fast` unless
a caller names a different tier through `HOST_LIKE_DEV_LAYOUT_TIER` (see the
script's own header), including the literal value `unfiltered`, which
reproduces exactly the recipe the two rows above measured. A value that is
not even shaped like a marker expression (blank, unbalanced parentheses, a
character no marker expression uses) is refused by the script with exit 2
before its install step; whether a shape-valid expression *means* anything is
pytest's own judgement and surfaces as pytest's usage error. Both rows are
kept verbatim -- same recipe text, same wall clock, same budget columns --
because they are still the only measurement this repo has of that unfiltered
invocation; they are labelled as retired rather than deleted or rewritten to
a recipe they were never run against. The lane itself (task 077's container
matrix) now runs each cell under the `fast` selection: the docker and
browser tiers that selection drops are already covered at the gate-blocking
level by `.github/workflows/heavy.yml` and do not vary with the interpreter
a dev-layout cell varies, so the six cells lose no coverage that varies with
the axis under test. No fresh dev-layout wall-clock measurement exists yet
for the `-m fast` selection; task 077's own container matrix cells are the
mechanism that will produce one, as a check-run verdict, never as a locally
recorded lane sweep (standing rule 1). The workflow's own per-cell budget
(`nightly.yml`'s `timeout-minutes: 120` -- raised from 60 by task 035 (steer
026 §4) because 60 was already marginal (58 min observed at `bd284e79` with
no interference) and task 035's own cure adds real per-attempt wall time to
any cell whose startup watchdog has to re-arm under contention; `docs/ci.md`'s
matching table entry carries the same figure) is sized to that `-m fast`
selection's ~80 s parallel wall clock plus a cell's own image pull and, for
the `ubuntu:24.04` cell only, its `apt-get install python3 python3-pip`
step -- not to the 3600 s ceiling the two retired rows above were measured
against, which was sized to the unfiltered whole suite this lane no longer
runs.

## When a tier goes over budget

The failure names the lane, the measurement and the ceiling. Three honest
answers, in order of preference:

1. make the new tests cheaper (stub the backoff, drop the real sleep, keep the
   module in-process) so the tier stays inside its ceiling;
2. move the expensive coverage to the tier that can afford it -- a module that
   spawns a real engine belongs in `fast`, a docker-dependent one in nightly;
3. raise the ceiling -- `TIER_CEILINGS` in `tests/test_tier_budgets.py` for a
   collected-count ceiling, the `budget wall clock` column above for a
   wall-clock one -- and name the measurement that earned the raise in the
   raising commit's OWN message, never as a fresh row or cell edited into
   this table: a committed measurement whose subject is this wave's own
   work is exactly the class of record the PRD prohibits (requirement N.4),
   whichever budget is being raised. The commit message is the visible,
   reviewable act; the table is not where a future raise's evidence lives.

Deleting or loosening the assertion is not on that list.

### The stub-`pi` flow lane (`tests/flows/`)

The flow modules requirement G.5 asks for live in their own directory, are
`fast`-only by rule 3 above (each one spawns a real engine), and each has to
stay under 20 s serial. Measured serially in this container, one module per
invocation (`.venv/bin/python -m pytest -q tests/flows/<module>.py`):

| module | wall clock |
|---|---|
| `tests/flows/test_flow_happy_path.py` | 1.41 s |
| `tests/flows/test_flow_review_replan_pause.py` | 2.40 s |
| `tests/flows/test_flow_petition_adjudication.py` | 1.40 s |
| `tests/flows/test_flow_cure_pass.py` | 1.59 s |
| `tests/flows/test_flow_validation_ladder.py` | 2.32 s |
| `tests/flows/test_flow_peer_gates.py` | 2.13 s |
| the whole directory (`tests/flows`) | 10.81 s |

That table is prose, not the budget table `tests/test_tier_budgets.py` parses
(the one under "## Budgets" above): this one carries no `wall clock`/`budget
wall clock` header pair, so the guard leaves it alone. The 10.81 s the
directory adds is inside the fast lane's own recorded measurement above.

### The records-lint family (`tests/records/`)

Measured serially (`.venv/bin/python -m pytest -q tests/records/`): well
inside the family's own **15 s** PRD target's neighbourhood at last
measurement, against a collected-count ceiling that lives as a code constant
(`COLLECTED_BUDGET` in `tests/records/test_records_family_budget.py`) rather
than as a row here, for the same reason `TIER_CEILINGS` moved out of this doc
above.

**The subprocess rule.** `tests/records/test_the_records_family_spawns_no_subprocess`
pins a real run of the whole family (minus its own one deliberate exemption)
at spawning no real subprocess at all -- see that module's own docstring for
the mechanism and the one exemption, `test_records_family_fresh_clone.py`,
whose single node deliberately clones this repository for real and runs the
family again inside the clone, to prove it passes against a genuine fresh
clone and not merely this checkout's own working tree. That node is marked
`@pytest.mark.nightly` directly, so it stays out of `smoke`/`fast`, but still
runs whenever this directory is selected by path.
