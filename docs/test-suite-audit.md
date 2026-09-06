# Test-suite audit (v0.19 baseline, requirement G)

Measured by the operator on 2026-09-06 before the v0.19 launch, read-only, on the host that runs the lanes (28 cores). This file is the baseline every G target in `docs/prds/v0.19-a-note-is-not-a-rejection.md` is measured against, and the map of what to consolidate; the run appends the § "Subsumed" table (deleted node → surviving node) as it lands G.3–G.6. The four failures in the timing run below were environmental: the operator's then-uncommitted v0.19 PRD, index row and petitions file tripped the "directory is discovered, not enumerated" guards.


Measured 2026-09-06 on this host (28 cores, load ~1), repo at 68c6692 with the v0.19 PRD,
index row and petitions file staged by the operator. Read-only audit; scratch in /tmp only.

## 1. Inventory (fast tier, `-m "not docker and not browser"`)

| fact | value |
|---|---|
| test modules under tests/ | 383 (`tests/test_*.py`), 372 collected in the fast tier |
| tests collected | 6,230 (6,295 total; 23 docker, 42 browser) |
| test functions defined | 4,063 (the rest is parametrisation) |
| modules named per task (`*_taskNNN.py`) | 65 modules, 451 tests |
| modules whose docstring opens with "Task NNN" | 248 of 383 |
| modules discovered by `scripts/final_sha_guards.sh` (mention docs/artifacts/README) | 140 modules, 3,789 tests |
| ...of which import no `ralphd` at all (pure prose/record guards) | 40 modules, 1,791 tests |
| modules that construct `LoopSupervisor` in-process | 149 modules, 1,824 tests |
| modules that define their own `def _supervisor(` harness | 87 (plus 24 `_run(`, 9 `_run_resumed(`) |
| modules spawning a real engine (`live` fixture / `engine_factory`) | 43 + `test_e2e.py` = 44 modules, ~175 tests |
| modules shelling out to `ralphctl` | 64 modules, 159 call sites (52 via `RALPHCTL`) |
| modules spawning a nested `pytest` | 7 (`test_conformance_ledger.py` does it 68x, once per node id) |
| tests named as mutation/discriminator pairs | 137 (`mutat|is_caught|is_rejected|tamper|...`) |
| tests asserting a substring of a `.md` read off disk | ~416 |

Biggest 30 modules by test count: `tests/test_report_claims.py` 1095, `tests/test_conformance_ledger.py` 153,
`tests/test_host_like_pytest_script.py` 127, `tests/test_tiers.py` 97, `tests/test_cli_fault_explanation.py` 82,
`tests/test_model_tiers_config.py` 67, `tests/test_spec_state_surface.py` 62, `tests/test_fault_classifier.py` 62,
`tests/test_cli_suggestions_guards.py` 61, `tests/test_promise_marker_last_line.py` 56, `tests/test_peer_verdict_gate.py` 53,
`tests/test_docs_semantics.py` 51, `tests/test_failed_task_vocabulary.py` 50, `tests/test_task_failure_kind_surfaces.py` 49,
`tests/test_plan_ingestion.py` 49, `tests/test_seventh_lint_task013.py` 46, `tests/test_stranding_lints.py` 45,
`tests/test_aborted_stream_fault_class.py` 45, `tests/test_image_dockerfile.py` 43, `tests/test_suggestions_patch.py` 42,
`tests/test_pricing_aws.py` 42, `tests/test_engine_uid_boundary.py` 42, `tests/test_cost_zero_quote.py` 41,
`tests/test_price_strategy_derive.py` 39, `tests/test_peer_plan_review_gate.py` 39,
`tests/test_no_dead_return_after_return_task014.py` 39, `tests/test_image_hash.py` 38, `tests/test_cli_suggestions_subset.py` 38,
`tests/test_image_derived.py` 36, `tests/test_hub_delete_affordance.py` 36.

Families (modules / tests): cli 66/966, hub 14/253, image 6/203, docs 5/167, prompt 14/126,
steer+steering 14/174, roadmap 9/106, peer 3/97, spec 2/85, price+pricing+cost 8/220,
e2e 7/46, per-task pins 65/451, report_claims 1/1095, conformance 1/153, host_like 2/134.

`test_report_claims.py` is 17 functions x 217 reports (fixture-parametrised over every
`artifacts/reports/*.md`). It is 1,095 of the 6,230 tests and takes 2.0 s.

## 2. Timing (`--durations=80`, full fast tier)

Result: **981 s (16:21)**, 6,221 passed, 4 failed, 5 skipped. The 4 failures are environmental
(staged-but-uncommitted `v0.19` PRD/report from the live run trip the "directory is discovered not
enumerated" guards in `tests/test_docs_prd_index.py`, `tests/test_report_claims.py`, `tests/test_sweep_sha_guard.py`); not a
suite defect, but a reminder that record guards depend on working-tree state.

Where the time goes:

| bucket | evidence | est. share |
|---|---|---|
| slowest 80 tests | sum 599 s of 981 s; all are real-engine + stub-pi or real sleeps | 61 % |
| single worst test | `test_interrupt_not_substantive_outcome_task004::test_3_mutation_...` **80.3 s** -- the mutated path falls into the contract ladder and hits an unstubbed `_wait_out_backoff` (loop.py:1389/3257) | 8 % |
| next | `test_reflect_infra_retry::test_reflect_survives_the_outage...` 35 s; `tests/test_refunds_survive_resume.py` 22 s; `tests/test_durations.py` 25 s/2 (STUB_SLEEP=3); `tests/test_cli_logs_follow_liveness.py` 37 s/4; `test_e2e.py` 46 s/6 in the top 80 | |
| `ralphctl` subprocess CLI tests | group of 7 cli modules: 320 tests in 17.3 s (~55 ms each, interpreter start + imports) -> 64 modules ~150-200 s | ~18 % |
| nested `pytest --collect-only` per node id | `tests/test_conformance_ledger.py`: 153 tests, 16.4 s | 2 % |
| bash `scripts/host_like_*.sh` runs | `test_host_like_*`: 134 tests, 10.7 s | 1 % |
| docs/spec derivation checks | `test_docs_consistency/semantics/prd_index/spec_*/sweep_sha`: 224 tests, 11.1 s | 1 % |
| in-process engine + prose pins | e.g. 7 lint/gate modules: 368 tests in 0.9 s; 9 roadmap modules: 106 tests in 0.45 s; report_claims 1,100 in 2.0 s | ~10 % for all ~4,000 |

Per-module slow time in the top 80: `tests/test_interrupt_not_substantive_outcome_task004.py` 80 s,
`tests/test_e2e.py` 46 s, `tests/test_cli_logs_follow_liveness.py` 37 s, `tests/test_reflect_infra_retry.py` 35 s, `tests/test_cli_ui.py`
26 s, `tests/test_durations.py` 25 s, `tests/test_refunds_survive_resume.py` 24 s, `tests/test_resume_by_state.py` 22 s,
`tests/test_iteration_timeout_contained.py` 21 s, `tests/test_inband_error_retry.py` 19 s, `tests/test_config_overlay.py` 17 s,
`tests/test_steer_tool_spool_two_uid_task030.py` 16 s, `tests/test_crash_consistency.py` 16 s,
`tests/test_status_health_infra_wait.py` 14 s, `tests/test_infra_outage_budget.py` 14 s.

**Key finding:** the pinned-prose/per-task tests are a *count and maintenance* problem, not a
wall-clock problem (4,000 in-process tests run in roughly 100 s). Wall clock is ~75 % real engines,
real sleeps and subprocess spawns, and is almost perfectly parallelisable.

## 3. Static analysis

- **Real-engine tests are spread thin.** 44 modules / ~175 tests each spawn `ralphd-engine` +
  `tests/stub-pi` (1,301-line stub with ~45 `STUB_*` knobs). `STUB_SLEEP` is set to 1-5 s in 42
  call sites (15 use 0). `test_e2e.py` carries its own `EngineProc` that duplicates
  `conftest.LiveRun`. Loop paths covered by a real engine today: happy path, review rejection ->
  new approach, stagnation, budget/grace review, steering (4 variants), abort, huge line, vigilant
  verify fail/transient/three strikes, criteria tamper, lock, logs follow, peer gates (task029/031/
  032), ladder exhaustion (task033), cure pass (task023), budget top-up. **Not covered by any
  real-engine test:** petition + adjudication (24 in-process modules, 0 e2e), last-approach pause
  + resume (3 in-process modules), deadline/pause credit, contract re-ask ladder end-to-end.
- **Harness duplication.** 87 modules define `_supervisor()`; 98 modules build `IterationResult`
  by hand; 14 assign `sup._run_iteration_once = fake`, 7 `sup.run_iteration = fake`, 6 stub
  `_wait_out_backoff`. The rest inherit real sleeps (hence the 80 s and 35 s tests).
- **Per-task pins overlap by feature.** Last-approach pause: `tests/test_last_approach_pause_task011.py` (9), `tests/test_last_approach_pause_persisted_task036.py`
  (9), `resume_..._task024` (2). Cure pass: `tests/test_cure_pass_task009.py` (6), `tests/test_cure_pass_not_on_tamper_task039.py` (6),
  `tests/test_bounded_cure_pass_task010.py` (5), `tests/test_cure_pass_id_collision_task006.py` (4), `tests/test_cure_pass_e2e_task023.py` (1). Contract ladder: 7 modules.
  Steer: 14 modules. Eight modules re-pin the same transition-table edges
  (`test_the_edge_is_declared_in_both_variants`, 29 `transitions.is_legal/successors` call sites).
- **Prose pins inside engine modules.** Of the 100 guard modules that *do* import ralphd, 320 of
  1,591 test functions read a `.md` in the body (e.g. `tests/test_docs_semantics.py` 23, `tests/test_suggestions_patch.py`
  16, `tests/test_seventh_lint_task013.py` 14, `tests/test_cli_suggestions_verb.py` 12, `tests/test_hub_artifacts.py` 12).
  `docs/cli.md` is read by 34 modules, `SPEC.md` by 29, `architecture.md` by 24, `roadmap.md` by 11.
- **Parametrised explosions:** `tests/test_report_claims.py` (217 reports x 5), `tests/test_conformance_ledger.py`
  (79 citations + 68 node ids, one `pytest` subprocess each), `tests/test_host_like_pytest_script.py`
  (quantifier x template paraphrase grid, ~100 regex cases). No `parametrize` list >8 literal items
  elsewhere.
- **pytest-xdist:** not installed (`pytest 9.1.1`, `pytest-asyncio 1.4.0`); `pip download
  pytest-xdist` succeeds (3.8.0), so it is installable. Shared-state audit for `-n auto`:
  ports are always `free_port()` (bind-0, TOCTOU race only); HOME is isolated per test by the
  autouse fixture; all writes go to `tmp_path`; no module writes into the repo tree; 3 direct
  `os.environ[...] =` mutations (`test_iteration_env_strip:118`, `test_e2e:1066`,
  `test_sibling_cleanup_job_safe:154` [docker tier]); 1 `monkeypatch.chdir` (`test_image_dockerfile:123`);
  3 cwd-relative `Path("docs/...")` uses; module-scoped fixtures only in docker-tier modules;
  2 `AMBIENT_HOME`-marked modules read the real HOME; `tests/test_skills_discoverability_survives_suite.py`
  runs a nested pytest subset (8 s). Nothing here blocks xdist; the two `os.environ` writes in the
  fast tier should become `monkeypatch.setenv`.

## 4. Proposal

### (a) Consolidate into black-box flow tests
Replace the 44 real-engine modules (~175 tests, ~600 s) with **one module per loop path, ~12 flows,
each asserting many facts along one run** (status.json, `--json status`, events.jsonl, logs
merge/tail/follow, `runs`, `artifacts`, hub JSON). Target ~15-20 s each serial with `STUB_SLEEP=0`
and backoffs set to 0 via job.yaml:

1. happy path + exit modes + durations + cost breakdown + artifacts (absorbs `tests/test_durations.py`,
   `tests/test_cli_cost_breakdown.py`, `tests/test_cli_iteration_detail.py`, `tests/test_cli_artifacts.py`, `test_cli_runs_*`, `test_cli_logs*` x11)
2. validation rejection -> retry -> three strikes -> review; criteria tamper refusal (absorbs vigilant `tests/test_e2e.py` cases, `tests/test_criteria_tamper_refusal_task024.py`)
3. review rejection -> replan -> ladder exhaustion -> last-approach pause -> resume with/without ruling (absorbs `tests/test_ladder_exhaustion_e2e_task033.py`, 3 last-approach modules, `tests/test_resume_by_state.py`)
4. cure pass: curable-only -> cure tasks -> re-review; bounded; not on tamper; id collision (absorbs 5 cure-pass modules)
5. petition -> adjudication -> refuted/confirmed -> history persisted (new e2e; today 0 real-engine coverage)
6. peer gates: plan-review objections, verdict-gate reject/malformed/peek (absorbs task029/031/032, `test_peer_*`)
7. steer: mid-review, mid-verify, redelivery, uncharged, steer tool spool, priority routing (absorbs 14 steer modules' engine halves)
8. infra: instant fail, hang, in-band error, outage budget, reflect retry -- **with `_wait_out_backoff` stubbed / backoff config 0** (absorbs `test_*infra*`, `tests/test_inband_error_retry.py`, `tests/test_reflect_infra_retry.py`: ~90 s today)
9. crash/resume: SIGKILL at boundary, mid-review, mid-reflect; refunds survive resume; lock semantics (absorbs `tests/test_crash_consistency.py`, `tests/test_refunds_survive_resume.py`, `tests/test_vigilant_crash_resume.py`)
10. budget/deadline: exhaustion, grace review once, top-up mid-flight, deadline beats verdict, pause-deadline credit
11. control plane: abort, API token, health/infra-wait JSON, secret redaction, huge/bad NDJSON line
12. hub over a live engine: run detail, steering POST, task counts, delete (absorbs `tests/test_cli_ui.py`, `tests/test_hub_steering.py`)

Delete/merge: the 65 per-task modules fold into their feature module (keep the *assertion*, drop
the module); the 87 `_supervisor` copies become two conftest fixtures (`supervisor(tmp_path, **cfg)`,
`scripted_attempts(sup, [...])` that also stubs every backoff); the 8 transition-edge re-pins
become one `test_transitions.py`; `test_e2e.EngineProc` is deleted in favour of `LiveRun`.

CLI: 64 modules / 159 `ralphctl` spawns -> call `cli.main.main()` in-process with argv + capsys
except where the process boundary is the subject (`tests/test_cli_logs_signal.py`, `_pty_termios`,
`_follow_liveness`, `tests/test_ralphctl_resolution.py`, `tests/test_engine_child_import_path.py`). Saves ~150 s.

### (b) Records lint: one fast module family, kept
Move to `tests/records/` (so `final_sha_guards.sh` selects a directory, not a grep) and keep:
`tests/test_report_claims.py` (collapse to 5 tests that iterate all 217 reports and list every problem),
`tests/test_sweep_sha_guard.py`, `tests/test_conformance_ledger.py` (replace 68 `pytest` spawns with one in-process
`pytest --collect-only` over all node ids: 16 s -> <1 s), `tests/test_docs_consistency.py`,
`tests/test_docs_semantics.py`, `tests/test_spec_cli_surface.py`, `tests/test_spec_state_surface.py`, `tests/test_docs_prd_index.py`,
`tests/test_roadmap_terminal_claims.py`, `tests/test_issue_traceability.py`, `tests/test_commit_closing_keyword_scope.py`,
`tests/test_final_sha_guards_discovery.py`, the transition table/diagram docs checks. Fold the 8
`test_roadmap_v0N.py` into one parametrised `test_roadmap_blocks.py` (106 -> ~15 tests). Keep exactly
one "the check is substantive" mutation per *checker* (47 exist today across these modules), never
per fact. Prose pins that live inside engine modules (320 test bodies) move here or are deleted when
they only restate a sentence. Budget: <=300 tests, <15 s, zero subprocesses except one `git ls-files`.

### (c) Parallelise
Yes. Add `pytest-xdist` to the dev extras and run the fast tier with `-n 8 --dist loadfile`.
Nothing structural blocks it (section 3). Fixes needed first: the two `os.environ[...] =` writes ->
`monkeypatch.setenv`; `free_port()` retry-on-EADDRINUSE in `LiveRun`; `AMBIENT_HOME` tests and
`tests/test_skills_discoverability_survives_suite.py` marked `@pytest.mark.xdist_group` or run serial. Expected on this host:
the 600 s of engine/sleep time is I/O-bound and independent -> ~80-100 s at `-n 8`; the in-process
remainder ~50 s. Fast tier ~3 min *before* any consolidation; ~1.5-2 min after.

### (d) Tiers
| lane | contents | serial est. | `-n 8` est. |
|---|---|---|---|
| **smoke** (per-task verify hat) | in-process engine flows (LoopSupervisor + scripted attempts, backoffs stubbed), CLI in-process, records lint, hub in-process | ~90-120 s | ~30 s |
| **fast** (final sha / PR) | smoke + the 12 stub-pi flows + process-boundary CLI tests | ~5-6 min | ~2 min |
| **nightly** | docker tier (~20 min), browser (~8 min), the two dev-layout modes (today 40 min each; scale with the fast tier -> ~15 min each) | ~1.5 h | ~45 min |

### (e) Risks: what the merged flows must still catch
- Ordering/precedence bugs the pins were written for: incurable-over-curable, tamper never cures,
  cure pass once per approach, last-approach pauses instead of failing, petition adjudication
  persisted before notify, refunds not double-applied on resume. Each flow must assert *sequence*
  (events.jsonl order), not only terminal state.
- Mutation discipline: keep one paired mutation per *checker/discriminator* (`_iteration_cut_short_by_engine`,
  lint regexes, record parsers) with backoffs stubbed; drop per-fact mutations.
- Flaky timing: flows must poll status/events with deadlines, never `STUB_SLEEP>0` for
  "measurable duration" -- assert `durationSeconds >= 0` and monotonicity instead (`tests/test_durations.py` 25 s -> <2 s).
- Loss of "diff-shaped" failure messages: a merged flow that fails must print which step; use
  `pytest.fail(f"step {name}: ...")` helpers or `subtests`.
- Record guards keyed to working-tree state (today's 4 failures): run them on committed state or
  make discovery use `git ls-files`.

### (f) Ordered plan (PRD-ready requirements with measurable criteria)
1. **Shared harness.** conftest fixtures `supervisor`/`scripted_attempts(sup, [...])` (backoffs and sleeps
   stubbed by default); delete all 87 module-local `_supervisor` and `test_e2e.EngineProc`.
   *Criterion:* `grep -c 'def _supervisor(' tests/` == 0; no fast-tier test >10 s in `--durations`.
2. **Kill real sleeps.** Stub `_wait_out_backoff` in every mutation test; `STUB_SLEEP=0` everywhere;
   backoff config 0 in flow job.yaml. *Criterion:* sum of slowest 80 < 120 s (today 599 s).
3. **xdist.** Add `pytest-xdist`, fix the 2 env writes and the port race, document `-n 8
   --dist loadfile` in `scripts/host_like_pytest.sh` and the verify hat. *Criterion:* fast tier
   green at `-n 8` three consecutive runs; wall clock < 4 min.
4. **Records lint module family** (section b) in `tests/records/`; `final_sha_guards.sh` selects the
   directory. *Criterion:* records lane <= 300 tests, < 15 s, `tests/test_report_claims.py` <= 10 tests,
   `tests/test_conformance_ledger.py` spawns 0 subprocesses per node id, roadmap modules == 1.
5. **Twelve stub-pi flows** (section a) replacing the 44 real-engine modules; petition/adjudication
   and last-approach pause gain real-engine coverage. *Criterion:* modules using `live(`/`engine_factory`
   <= 14; every flow asserts an events.jsonl sequence; each flow < 20 s serial.
6. **Fold per-task pins.** Every `tests/test_*_taskNNN.py` either merges into its feature module or
   is deleted; no surviving module opens with "Task NNN". *Criterion:* `ls tests/test_*_task*.py`
   empty; feature modules for last-approach, cure pass, contract ladder, steer, petitions each == 1.
7. **CLI in-process.** Convert `ralphctl` subprocess call sites to `main()` + capsys except the 5
   process-boundary modules. *Criterion:* `grep -c 'RALPHCTL' tests/` <= 8 modules; cli family < 30 s.
8. **Tiering + budget guard.** Markers `smoke`/`fast`; a test in `tests/records/` fails if the fast
   tier exceeds N tests or the recorded `--durations` budget is blown. *Criterion:* smoke < 2 min
   serial; fast tier < 5 min serial / < 2 min `-n 8`; total collected <= 3,000 tests (today 6,230);
   modules <= 180 (today 383); the two dev-layout modes < 15 min each.

Net effect: 6,230 -> ~3,000 tests, 383 -> ~180 modules, 16.3 min -> ~5 min serial / ~2 min
parallel, with petition/adjudication and last-approach pause gaining the end-to-end coverage they
lack today.

## Subsumed

Filled in by the v0.19 run as requirement G lands: one row per deleted test node, naming the
surviving flow/records test that covers the behaviour it pinned. A deleted node with no row here
is a blocking finding under the PRD's rubric.

| deleted node | surviving cover | landed in |
|---|---|---|
