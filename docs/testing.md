# Testing ralphd: the tiers, their recipes, and their budgets

This suite is tiered by pytest marker. A tier is a promise about *cost*: what it
covers, what it deliberately leaves out, and how long it may take before it stops
being usable at the point in the loop that runs it. The tiers and their measured
budgets are checked by `tests/test_tier_budgets.py`, which reads this file.

| tier | marker | who runs it | what it covers |
|---|---|---|---|
| smoke | `smoke` | the per-task verify pass of a run on this repository (the run's Standing rules name this recipe; the verify prompt itself names no project tier), and `scripts/final_sha_guards.sh` over the docs-reading modules it discovers | in-process engine flows (a supervisor plus scripted attempts, backoffs stubbed), in-process CLI, in-process hub, and the in-process checks of the docs and SPEC.md against the code |
| fast | `fast` | the final sha of a wave, and any pull request | smoke plus the stub-pi flow modules and the process-boundary CLI tests -- everything outside the nightly lanes |
| nightly | `docker`, `browser`, and the dev-layout module | a sweep, never a per-task check | the docker-sibling tier, the browser tier, and `nightly.yml`'s six-cell dev-layout container matrix (`ubuntu:24.04`, `python:3.11-slim`, `python:3.14-slim` x `PYTHONUSERBASE` exported/unset, `max-parallel: 2` across the VPS's two registered runner slots) -- each cell's `pytest` invocation itself now runs the `fast` tier by default (task 076, `scripts/host_like_dev_layout.sh`'s `HOST_LIKE_DEV_LAYOUT_TIER`), not the whole suite unfiltered, PLUS (task cure-01-01-2) a supplemental pass over every node that default leaves outside `fast`/`smoke`/`docker`/`browser` -- the `nightly`-marked nodes and the dev-layout module's own nodes -- so nothing collected goes uncovered by every lane; see the dev-layout rows below |

## Recipes

    # the per-task tier, in parallel (the one to reach for while working)
    .venv/bin/python -m pytest -q -m smoke -n 8 --dist loadfile

    # the same tier, serial (what the PRD's smoke target is stated against)
    .venv/bin/python -m pytest -q -m smoke

    # the final-sha tier, in parallel
    .venv/bin/python -m pytest -q -m fast -n 8 --dist loadfile

    # a single module or node id, while working on it -- seconds
    .venv/bin/python -m pytest -q tests/test_tier_budgets.py

    # the subset a docs-only commit can break: the script discovers every
    # test module that reads docs/, README or SPEC.md, then selects the
    # smoke tier
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
Nested `pytest --collect-only` children are deliberately *not* in that list, so
a module that only collects (never runs) a nested suite stays in smoke.

## The `flaky_infra` marker: one rerun, for named nodes only

`flaky_infra` marks a node whose only known failure is the host it runs on (a
pty, a signal race), not the product. `pytest-rerunfailures` is in the `dev`
extra, and `pytest_collection_modifyitems` in `tests/conftest.py` gives a marked
node exactly one rerun through the plugin's own marker arguments
(`flaky(reruns=1)`). A node that already carries its own `flaky` marker keeps it.
A node that fails twice fails; an unmarked node is never rerun. There is no
global `--reruns` in `pyproject.toml` or in any workflow, and a guard in
`tests/test_flaky_infra_marker.py` pins that. Two nodes carry it:
`test_cli_logs_pty_termios.py::test_sigint_restores_termios_mode` and
`test_cli_logs_signal.py::test_sigint_during_follow_exits_clean_no_traceback`.
Do not apply it to a node that can fail for a product reason: a rerun would hide
the regression. `tests/test_flaky_infra_marker.py` shows, through pytester, that
a fail-once marked node passes on its second attempt and is reported as rerun,
the same node unmarked fails, and a fail-twice marked node fails.

## Running the suite inside a ralphd job container

This repository is developed by ralphd runs of its own, so the suite is often
collected *inside* a ralphd job container -- and such a container already has
`/var/lib/ralphd/record` (`privsep.DEFAULT_TASK_RECORD_DIR`) materialised
root-owned 0700, because the engine running that job put the authoritative
record for *its* run there. `privsep.task_record_dir()` answers truthfully
that the directory exists (`is_dir()` needs only `x` on the root-owned 0755
parent), so without isolation every in-process `RunDir.write_tasks()` in the
suite aims at that directory and dies at the agent uid with `PermissionError:
[Errno 13] ... '/var/lib/ralphd/record/.tasks-last-good.json'`, and every
spawned live engine dies the same way before it binds a port -- surfacing as
the misleading `TimeoutError: engine API never came up`.

`tests/conftest.py::_isolate_ambient_task_record_dir` (autouse) closes that
ambient leak, in the same spirit as the in-process `$HOME` and
`RALPHD_NOTIFIER_URL` fixtures beside it: when -- and only when -- the default
record directory both exists and is unusable at this uid, and the invocation
named no `$RALPHD_TASK_RECORD_DIR` of its own, it points that variable at a
path that does not exist. That is precisely the state a developer host or a CI
runner is in, where nothing ever created the directory: `task_record_dir()`
returns `None` and the record falls back to the run-dir location
(`privsep.legacy_protected_tasks_dir`), the degrade privsep.py documents for
"the test suite's own unprivileged engine". The fixture is therefore inert
wherever the suite is already green, and `monkeypatch.setenv` reaches spawned
children through `_child_base_env()`, so the live-engine harness needs no
second mechanism. No hand-exported override is needed to run the suite from
inside a job container -- which matters for `scripts/host_like_pytest.sh`,
whose whole job is to scrub every ambient `RALPHD_*` name and which therefore
cannot carry one.

The key store (`petitions.key_store_dir()`, `/var/lib/ralphd/run-secrets`,
root-owned 0700 in the same container) has no counterpart fixture and needs
none: it resolves to that default only under a real uid boundary
(`privsep.separated()`), which this suite's agent-uid processes never have, so
an unusable ambient store is never consulted. Forcing an override there would
instead *create* a store where a developer host has none, changing the
memory-only-key degrade under test.

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
  `tests/test_tier_budgets.py` -- a code constant, not a cell in the table
  below, so it cannot go stale silently when this file is edited. A ceiling
  moves only with the measured count named in the commit message that moves
  it; neither the count nor its history is written into this doc or into a
  code comment.

| lane | marker | recipe | wall clock | budget wall clock | PRD target | target met? |
|---|---|---|---|---|---|---|
| smoke, parallel | `smoke` | `-m smoke -n 8 --dist loadfile` | 24.41 s | 60 s | n/a | met -- 24.41 s inside the lane's own 60 s budget ceiling; no PRD target of this kind for this lane |
| smoke, serial | `smoke` | `-m smoke` | 147.68 s | 200 s | < 120 s | MISSED -- 147.68 s measured against the 120 s target |
| fast, parallel | `fast` | `-m fast -n 8 --dist loadfile` | 80.12 s | 150 s | < 120 s | met -- 80.12 s inside the 120 s target |
| fast, serial | `fast` | `-m fast` | 540.44 s (stale wall clock, not retaken since -- the standing rule caps one full fast-tier run per iteration and it is spent on the parallel recipe above) | 1100 s | < 300 s | MISSED -- 540.44 s measured against the 300 s target |
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

**Task cure-01-01-2 (task 018 criterion 3) adds one more pass on top of that
`-m fast` default, for the exact no-override, no-extra-argument shape
`nightly.yml`'s cell calls the script with.** `-m fast` alone left the
`nightly`-marked nodes and `tests/test_host_like_dev_layout_script.py`'s own
nodes -- neither `fast` nor `smoke` nor `docker` nor `browser` -- uncovered
by any CI lane: not this cell (it never selected them), not
`.github/workflows/heavy.yml` (its `docker`/`browser` jobs do not select
them either). The script now runs a second pytest invocation afterwards, in
the same already-built `$HOME`/user base (no re-install), selecting `-m
"not fast and not smoke and not docker and not browser"` -- exactly that
leftover set, since `tests/conftest.py`'s derivation stamps every collected
node with exactly one of `fast`/`docker`/`browser`/that leftover. Passing an
explicit `HOST_LIKE_DEV_LAYOUT_TIER` (even the literal value `fast`) or any
pytest argument of the caller's own (a node id, a path, `--collect-only`)
disables the addition, so a targeted or `--collect-only` invocation still
runs exactly what it asked for. The script's own exit code is nonzero if
either pass is, so a regression in the previously-uncovered set now fails
the nightly cell instead of passing silently. This adds real wall time to
the cell (the leftover set includes real editable-install-driven tests in
its own module) but no new bound: the cell's existing 120-minute
`timeout-minutes` is unchanged, and neither this table nor `docs/ci.md`'s
matching row is edited for it, per the standing prohibition on raising a
bound to make a requirement pass.

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

### The stub Claude Code CLI (`tests/stub-claude/claude`)

Beside stub-`pi`, `tests/stub-claude/claude` is a fake Claude Code CLI that the
real `claude_agent_sdk.query()` drives over the stream-json control protocol
(`cli_path` or `RALPHD_CLAUDE_CLI_PATH`). Its scenarios and every `STUB_CLAUDE_*`
knob are documented in the script's module docstring, and
`tests/test_stub_claude.py` (smoke) covers each scenario.

### The stub Codex CLI (`tests/stub-codex/codex`)

`tests/stub-codex/codex` is a fake `codex` CLI for the codex harness
(`RALPHD_CODEX_CLI_PATH`). It answers `--version` with `codex-cli
$STUB_CODEX_VERSION` (default `0.156.1`), reads the prompt from stdin, writes
its observations (argv, env, cwd, uid, pid, stdin, the `$CODEX_HOME/config.toml`
text) as JSON to `$STUB_CODEX_OBSERVE`, and replays the `codex exec --json`
event stream of the scenario `$STUB_CODEX_SCENARIO` selects: `text` (default),
`tools` (command execution, file change and MCP call items), `failed` (a
`turn.failed` carrying `$STUB_CODEX_FAILED_MESSAGE`), `incomplete` (a partial
`agent_message`, then a `turn.failed` citing `max_output_tokens`),
`no_terminal` (exit 0 without `turn.completed`), `hang`, `ignore_sigint`,
`unknown_items` (item and event types no normaliser knows) and `echo_token`
(prints `$AWS_BEARER_TOKEN_BEDROCK` on stdout and stderr). The other knobs
(`STUB_CODEX_TEXT`, `STUB_CODEX_USAGE`, `STUB_CODEX_SILENT`,
`STUB_CODEX_SLEEP_BEFORE`, `STUB_CODEX_CHILD_PIDFILE`, `STUB_CODEX_WRITE_FILE`,
`STUB_CODEX_HANG_CLAIM`, `STUB_RUN_DIR`) are documented in the script's module docstring, and
`tests/test_stub_codex.py` covers each scenario and the observation file. The
stub touches no network and needs no credential.
