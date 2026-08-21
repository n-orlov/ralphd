# Issue traceability — ralphd v0.5 (`selfdev-v05-resilience`)

> **Two waves live in this file.** The v0.5 wave (**#1–#11 and #13**) is below;
> the v0.6 wave (**#14–#22**) starts at *Issue traceability — ralphd v0.6*
> further down. Both are re-read by `tests/test_issue_traceability.py`.

Maps every GitHub issue in scope (**#1–#11 and #13**; #12 was already closed
before this wave) to its PRD requirement letter, the tasks that implemented it,
the commits, and the tests that cover it.

**No issue was closed by this run** — the PRD forbids it ("Not closing GitHub
issues from inside the run"). The operator closes them from this table.
Verified: `git log -p 5472453..HEAD | grep -i 'gh issue close'` and a
tree-wide grep for `gh issue close` both return nothing.

- Baseline commit (PRD): `5472453` "docs: PRD for v0.5".
- Machine check: this file is re-read by the suite -- every commit sha listed
  below must exist, and every test path (and every test node id) listed below
  must exist in the tree. Those three checks are
  `tests/test_report_claims.py` over `tests/report_claims.py`, applied to every
  report in this directory since task 041; `tests/test_issue_traceability.py`
  keeps what is specific to this report (its per-issue sections, how much
  evidence it must still carry, and the closing-command guard).
- Requirement letters are the PRD's (`/run/ralphd/prd.md`, sections A–N).

## Summary

| Issue | PRD req | Tasks | Commits |
|---|---|---|---|
| #1 PRD in the hub | N | 056 | `f9022bd` |
| #2 Task detail in the hub | N | 057 | `b389e2d` |
| #3 Top up the iteration budget in flight | J | 045, 046, 047 | `409d338`, `fc803b0`, `e4039c1` |
| #4 Absolute timestamps | K | 048 | `7972792` |
| #5 Aggressive retry, degraded state, retry-now, reflect | B, C, D | 006–020, 043 | `b97f6c5` … `3c5178f`, `7d65df7` |
| #6 Logs must survive the container | I | 038, 039, 040, 041, 042 | `20c1bd6`, `3ea1fa7`, `5f31559`, `4abd0e7`, `bad36e8` |
| #7 Agent must not delete its own container | H | 034, 035, 036, 037 | `be0a949`, `75adae3`, `589d11a`, `a34aea7` |
| #8 Dead run visible + opt-in self-recovery | E, F | 021–030 | `e478042` … `c922587` |
| #9 Sortable newest-first run list | M | 054, 055 | `affb6d7`, `73854b2` |
| #10 Unknown cost must not render as `$0.0000` | L | 049, 050, 051, 052, 053 | `12c8131`, `4f791ce`, `e56d8c0`, `86bbc53`, `61be67d` |
| #11 Classify in-band LLM errors as faults | A (+B) | 001, 002, 003, 004, 005, 008, 043 | `518b974`, `9d7ea75`, `4463c20`, `df0a4bd`, `7afabd2`, `173e3c7`, `7d65df7` |
| #13 `watch` must not close on a stale terminal marker | G | 031, 032, 033 | `aa33f0f`, `d52eb3b`, `1ed447d` |

Supporting (no single issue): `6338f6e` task 060 (flaky docker e2e assertion),
`2b4de79` task 044 (phase-1 gate report).

---

## #11 — in-band LLM errors are classified as faults (requirement A, plus B's taxonomy)

`src/ralphd/engine/faults.py` (`classify_fault`, `_INFRA_TEXT_PATTERNS`),
`engine/runner.py`/`engine/loop.py` (`faultClass` recording),
`docs/architecture.md` §10.1.

| Task | Commit | Tests |
|---|---|---|
| 001 error-bearing result is a failure regardless of exit code | `518b974` | `tests/test_fault_classifier.py::test_error_bearing_result_is_always_a_fault`, `::test_zero_token_no_traffic_termination_with_error_is_a_fault`, `::test_error_free_exit_zero_is_still_not_a_failure` |
| 002 signature table for the gateway/Bedrock families | `9d7ea75` | `tests/test_fault_classifier.py::test_infra_signature_family_classifies_infra`, `::test_regression_deck_phase1_getaddrinfo_eai_again`, `::test_regression_deck_phase1_econnreset_exit0_zero_tokens`, `::test_ordinary_agent_failure_text_is_not_infra` |
| 003 the `aborted` carve-out | `4463c20` | `tests/test_operator_abort_carve_out.py::test_bare_aborted_without_traffic_or_operator_abort_is_infra`, `::test_bare_aborted_with_operator_abort_is_not_infra`, `::test_operator_abort_beats_every_infra_signal` |
| 004 `faultClass` in meta.json + `iteration.end` | `df0a4bd` | `tests/test_fault_class_meta.py::test_fault_class_null_on_clean_and_infra_on_hung_iteration`, `::test_fault_class_null_for_a_fully_clean_run` |
| 005 stub-pi in-band-error knob + refund e2e | `7afabd2` | `tests/test_inband_error_retry.py::test_inband_exit0_error_is_retried_refunded_and_job_completes`, `::test_inband_errors_exhausting_retries_end_terminal_without_burning_budget` |
| 008 episode clock (shared with #5) | `173e3c7` | `tests/test_infra_outage_budget.py` |
| 043 docs §10 resilience | `7d65df7` | `tests/test_docs_consistency.py` |

## #5 — aggressive retry with an outage budget, degraded state, retry-now, reflect (requirements B, C, D)

`engine/config.py` (knobs), `engine/loop.py`
(`_run_iteration_with_infra_retry`, `INFRA_RETRY_PHASES`, `_run_reflection`),
`engine/api.py` (`POST /retry`, `health`/`infraWait`), `cli/main.py`,
`cli/ui_server.py` + `cli/web/app.js`, `docs/architecture.md` §§10.2–10.5.

| Task | Commit | Tests |
|---|---|---|
| 006 config knobs | `b97f6c5` | `tests/test_job_config_defaults.py::test_infra_retry_defaults_are_fast_backoff_with_outage_budget`, `::test_infra_retry_max_defaults_to_no_explicit_cap`, `::test_infra_knob_env_overrides`, `::test_infra_knobs_in_effective_budgets`, `tests/test_config_effective.py` |
| 007 `start --infra-outage-budget` | `945abad` | `tests/test_cli_start_infra_budget.py::test_flag_writes_infra_outage_budget_s_into_job_yaml`, `::test_omitting_the_flag_leaves_the_engine_default` |
| 008 wall-clock outage budget + episode clock | `173e3c7` | `tests/test_infra_outage_budget.py::test_five_consecutive_infra_faults_are_ridden_out_free`, `::test_outage_budget_exhaustion_ends_terminal_naming_the_duration`, `tests/test_infra_fault_retry.py` |
| 009 all five phases, no double-counting | `23b17fd` | `tests/test_infra_retry_all_phases.py::test_every_phase_goes_through_the_infra_retry_wrapper`, `::test_infra_shaped_review_is_retried_and_does_not_cost_an_approach` |
| 010 instant faults retried, broken env still fast-fails | `add39e5` | `tests/test_instant_infra_retry.py::test_instant_refused_connection_recovers_and_the_job_completes`, `::test_broken_credentials_still_fail_fast_not_after_the_outage_budget`, `tests/test_no_progress_instant_failures.py` |
| 011 deadline extension + `infraWaitTotalS` | `534cf6f` | `tests/test_infra_deadline_extension.py::test_status_json_records_infra_wait_total_and_extended_deadline`, `::test_total_survives_a_resume` |
| 012 `health` + `infraWait` status contract | `a61b7ca` | `tests/test_status_health_infra_wait.py::test_ralphctl_status_json_carries_health_and_infra_wait` |
| 013 `ralphctl status` degraded line | `d0d2f33` | `tests/test_cli_status_degraded.py::test_degraded_line_carries_countdown_attempt_and_error`, `::test_healthy_run_output_is_byte_identical_to_pre_task_013`, `::test_status_json_passes_health_and_infra_wait_through` |
| 014 hub degraded card | `466f98f` | `tests/test_browser_hub.py::test_run_detail_renders_degraded_infra_wait_distinctly` |
| 015 `POST /retry` | `9b0dc18` | `tests/test_retry_now.py::test_post_retry_cuts_a_long_backoff_short`, `::test_post_retry_resets_the_outage_budget_episode_clock`, `::test_post_retry_409s_when_the_run_is_not_waiting`, `::test_post_retry_does_not_unpause_or_touch_steering` |
| 016 `ralphctl retry` | `17756b7` | `tests/test_cli_retry.py::test_retry_posts_to_retry_and_exits_0`, `::test_retry_when_not_waiting_exits_5_with_the_engine_explanation`, `::test_retry_is_documented_next_to_pause` |
| 017 hub retry-now button + countdown | `06405ee` | `tests/test_browser_hub.py::test_degraded_card_offers_a_retry_now_button_with_a_ticking_countdown`, `tests/test_cli_ui.py::test_retry_proxy_posts_to_the_runs_retry_and_forwards_the_token`, `tests/test_cli_ui.py::test_retry_proxy_passes_the_engines_409_refusal_through` |
| 018 reflect inside the wrapper + pre-attempt wait | `056f15a` | `tests/test_reflect_infra_retry.py::test_reflect_survives_the_outage_that_killed_the_job`, `::test_reflect_gets_a_capped_outage_budget_every_other_phase_does_not` |
| 019 `_run_reflection` inspects its result | `f4d585a` | `tests/test_reflection.py::test_reflect_agent_error_is_recorded_as_a_failure`, `::test_reflect_that_writes_no_report_is_recorded_as_a_failure`, `::test_successful_reflection_records_ok_and_no_failure_marker` |
| 020 reflection failure on CLI + hub | `3c5178f` | `tests/test_cli_status_reflect.py::test_status_reports_a_failed_reflection`, `::test_status_output_unchanged_when_reflection_succeeded_or_was_disabled`, `tests/test_browser_hub.py::test_run_detail_shows_a_failed_reflection` |
| 043 docs resilience section | `7d65df7` | `tests/test_docs_consistency.py` |

## #8 — a dead run is visible everywhere, and opt-in self-recovery (requirements E, F)

`cli/main.py` (`repair`, `status`, `doctor --fix`, `AUTO_RESUME_DEFAULT`,
auto-resume guard), `cli/ui_server.py` + `app.js`, `state.py`
(`OPERATOR_TERMINATION_FILE`).

| Task | Commit | Tests |
|---|---|---|
| 021 `repair` learns the dangling-container condition | `e478042` | `tests/test_cli_repair.py::test_repair_reports_dangling_container_for_running_run`, `::test_repair_set_state_aborted_writes_vanished_container_reason`, `::test_repair_dangling_check_has_one_implementation`, `::test_repair_still_refuses_dangling_check_on_live_container` |
| 022 `status` says the container is gone | `9c6eac1` | `tests/test_cli_status_dead_run.py::test_status_warns_that_the_container_is_gone`, `::test_status_relabels_the_duration_as_time_since_last_update`, `::test_live_run_output_is_unchanged` |
| 023 CLI-side task counts in the on-disk fallback | `a0164c9` | `tests/test_cli_status_dead_run.py::test_status_of_a_dead_run_shows_task_counts_from_tasks_json`, `::test_task_counts_maps_statuses_to_the_status_contract_keys`, `::test_status_json_of_a_dead_run_carries_the_same_task_counts` |
| 024 hub warning treatment for a dead run | `dfd00ca` | `tests/test_browser_hub.py::test_dead_nonterminal_run_gets_the_warning_treatment`, `tests/test_cli_ui.py::test_dead_nonterminal_run_is_flagged_container_gone_in_list_and_detail` |
| 025 doctor and repair tell one story | `cbe9644` | `tests/test_cli_repair.py::test_doctor_and_repair_recommend_the_same_next_command`, `::test_dangling_remedy_text_has_one_implementation` |
| 026 `auto_resume` setting, default in one place | `cdb31f7` | `tests/test_cli_auto_resume.py::test_default_is_off_and_lives_in_one_place`, `::test_default_literal_appears_exactly_once_in_the_source`, `::test_setting_survives_resume` |
| 027 `doctor --fix` resumes opted-in dead runs | `32a4613` | `tests/test_cli_doctor_fix.py::test_fix_resumes_an_opted_in_dangling_run`, `::test_fix_leaves_an_opted_out_run_untouched_but_reported`, `::test_docs_document_cron_deployment` |
| 028 crash-loop guard | `cdfea5e` | `tests/test_cli_auto_resume_guard.py::test_backoff_prevents_an_immediate_second_attempt`, `::test_crash_loop_gives_up_and_the_reason_is_readable_from_status`, `::test_backoff_schedule_escalates` |
| 029 never resurrect terminal/operator-killed runs | `b647cc6` | `tests/test_cli_auto_resume_operator_terminated.py::test_fix_never_resumes_a_terminal_run`, `::test_fix_never_resumes_an_operator_terminated_run`, `::test_the_same_fixture_without_the_marker_is_resumed`, `::test_stop_force_records_the_marker_and_blocks_auto_resume` |
| 030 roadmap note for the planned default flip | `c922587` | `tests/test_cli_auto_resume.py::test_roadmap_records_the_planned_default_flip_and_names_the_single_place` |

## #13 — `watch`/`logs -f` must not close on a historical terminal marker (requirement G)

`engine/api.py` (`_terminal_event_ends_stream`, live reconciliation),
`engine/state.py` (resume state event), `docs/cli.md` completion-wait snippet.

| Task | Commit | Tests |
|---|---|---|
| 031 follower streams past a stale marker | `aa33f0f` | `tests/test_cli_watch_resumed_run.py::test_watch_streams_past_historical_terminal_marker_on_resumed_run`, `::test_watch_on_live_run_still_closes_on_its_own_terminal_event`, `::test_watch_on_finished_resumed_run_closes_at_the_final_marker` |
| 032 explicit running/resumed state event on resume | `d52eb3b` | `tests/test_resume_state_event.py::test_resume_appends_a_running_state_event_after_the_stale_terminal_marker`, `::test_fresh_run_opens_with_a_running_state_event` |
| 033 same audit for `logs -f` (shared code path) | `1ed447d` | `tests/test_cli_logs_resumed_run.py::test_logs_follow_streams_past_a_resumed_runs_stale_terminal_marker`, `::test_logs_follow_on_a_finished_resumed_run_still_exits_promptly` |

## #7 — the agent must not be able to delete its own container (requirement H)

`cli/main.py` (`ralphd.role=job`, `RALPHD_SELF_CONTAINER_ID`),
`engine/loop.py` `_docker_siblings_note`, `examples/skills/toolchain-sibling/SKILL.md`,
`docs/architecture.md`, `docs/cli.md`.

| Task | Commit | Tests |
|---|---|---|
| 034 role label + self-container env var | `be0a949` | `tests/test_cli_docker.py`, `tests/test_cli_resume.py` |
| 035 prompt teaches the sibling-only idiom and the why | `75adae3` | `tests/test_sibling_cleanup_guidance.py::test_every_prompt_forbids_the_run_label_only_cleanup`, `::test_sibling_run_example_carries_both_labels`, `::test_note_names_this_containers_own_id` |
| 036 same rule in every documented duplicate | `589d11a` | `tests/test_docs_consistency.py` |
| 037 the documented idiom cannot delete the job container | `a34aea7` | `tests/test_sibling_cleanup_job_safe.py::test_documented_cleanup_removes_siblings_and_spares_the_job`, `::test_the_forbidden_form_really_does_delete_the_job_container`, `::test_ralphctl_stop_and_rm_still_reap_everything` |

## #6 — logs must survive the container (requirement I)

`src/ralphd/log_merge.py` (single reader of `output.jsonl`), `engine/api.py`
`/logs`, `cli/main.py` `logs`, `cli/ui_server.py` `rendered_log_lines()`,
`docs/architecture.md` redaction section.

| Task | Commit | Tests |
|---|---|---|
| 038 shared merge module | `20c1bd6` | `tests/test_log_merge.py::test_api_and_on_disk_merge_are_identical`, `::test_no_duplicate_merge_implementation` |
| 039 hub falls back to the on-disk merge | `3ea1fa7` | `tests/test_cli_ui.py::test_log_endpoint_falls_back_to_on_disk_merge_for_a_dead_run`, `::test_log_endpoint_on_disk_lines_match_the_shared_merge`, `tests/test_browser_hub.py::test_dead_run_log_tail_shows_the_on_disk_snapshot_label` |
| 040 `ralphctl logs` on an unreachable run | `5f31559` | `tests/test_cli_logs_dead_run.py::test_logs_pretty_on_dead_run_prints_on_disk_merge_and_exits_0`, `::test_logs_follow_on_dead_run_prints_snapshot_and_exits_cleanly`, `::test_logs_raw_on_dead_run_is_the_shared_merge_verbatim`, `::test_logs_live_run_says_nothing_about_snapshots` |
| 041 explicit `(no transcript yet)` | `4abd0e7` | `tests/test_no_transcript_message.py::test_cli_logs_on_empty_iterations_dir_says_no_transcript`, `::test_hub_log_endpoint_on_empty_iterations_dir_says_no_transcript`, `::test_both_surfaces_use_the_one_shared_wording` |
| 042 redaction decision documented (write-time scrub accepted, map never persisted) | `bad36e8` | `tests/test_secret_redaction.py`, `tests/test_docs_consistency.py` |

## #3 — top up the iteration budget in flight (requirement J)

`engine/api.py` `PATCH /config/budget`, `cli/main.py`
(`ralphctl budget`, `_apply_iterations_topup`).

| Task | Commit | Tests |
|---|---|---|
| 045 `PATCH /config/budget` | `409d338` | `tests/test_budget_patch.py::test_relative_topup_applies_live_and_is_visible_everywhere`, `::test_rejects_a_budget_below_iterations_used`, `::test_rejects_bad_input_with_a_problem_detail`, `::test_refunded_infra_attempts_do_not_block_a_lower_budget` |
| 046 `ralphctl budget <run-id> +N\|N` | `fc803b0` | `tests/test_cli_budget.py::test_budget_round_trips_the_spec_verbatim`, `::test_budget_below_iterations_used_exits_5`, `::test_budget_is_documented_in_cli_docs` |
| 047 mid-flight top-up e2e | `e4039c1` | `tests/test_e2e_budget_topup.py::test_topup_midflight_lets_the_job_reach_a_terminal_state_it_could_not_have`, `::test_run_without_topup_dies_of_budget_exhaustion` |

## #4 — absolute timestamps (requirement K)

One shared formatter (`state.format_local_time`) used by the hub timeline,
`ralphctl logs` boundary lines and `ralphctl status`; raw ISO kept in payloads.

| Task | Commit | Tests |
|---|---|---|
| 048 absolute local-time timestamps everywhere | `7972792` | `tests/test_absolute_timestamps.py::test_format_local_time_renders_the_instant_in_local_time_with_offset`, `::test_status_prints_absolute_started_and_ended_alongside_the_duration`, `::test_logs_boundary_lines_carry_absolute_timestamps`, `::test_status_json_keeps_the_raw_iso_timestamps`, `tests/test_browser_hub.py::test_timeline_and_summary_show_absolute_local_timestamps` |

## #10 — unknown cost must not render as `$0.0000` (requirement L)

`engine/runner.py` (`costPriced`), `engine/state.py`
(`costStatus`, `format_cost`), `engine/pricing.py` (host-side map),
`cli/main.py`, `app.js`, `docs/api.md` usage contract.

| Task | Commit | Tests |
|---|---|---|
| 049 unknown instead of `$0` when unpriced | `12c8131` | `tests/test_cost_unknown.py::test_unpriced_iteration_with_tokens_has_no_cost_and_is_marked_unpriced`, `::test_priced_iteration_records_cost_and_marks_it_priced`, `::test_no_traffic_at_all_records_no_usage`, `::test_unpriced_run_records_unknown_cost_in_iteration_meta` |
| 050 mixed run totals marked partial | `4f791ce` | `tests/test_usage_accounting.py::test_fully_priced_run_usage_carries_no_cost_status_anywhere`, `::test_mixed_run_marks_the_total_partial`, `::test_fully_unpriced_run_marks_total_and_buckets_unknown`, `::test_cost_status_is_monotone_once_something_is_unknown` |
| 051 render unavailable on every surface | `e56d8c0` | `tests/test_cost_render_surfaces.py::test_status_usage_summary_for_a_fully_priced_run_is_byte_identical`, `::test_status_cli_prints_unavailable_for_an_unpriced_run`, `::test_logs_footer_of_an_unpriced_iteration_says_unavailable`, `tests/test_browser_hub.py::test_run_detail_renders_unknown_cost_as_unavailable` |
| 052 optional host-side pricing map, marked derived | `86bbc53` | `tests/test_pricing_map.py::test_alias_rewrites_a_gateway_model_id_to_its_canonical_name`, `::test_unpriced_iteration_with_a_rate_records_a_derived_cost`, `::test_a_provider_price_is_never_replaced_by_the_map`, `::test_no_map_at_all_leaves_task_049_behaviour_untouched` |
| 053 same-model priced/unpriced anomaly report | `61be67d` | `artifacts/reports/pricing-anomaly.md` (evidence from this host's run dirs; mitigation = tasks 049–052) |

## #9 — sortable, newest-first run list (requirement M)

`cli/web/app.js` (`RUN_COLUMNS`, `runSort` outside the DOM),
`cli/main.py` (`RUN_SORT_KEYS`, `runs --sort/--reverse`).

| Task | Commit | Tests |
|---|---|---|
| 054 hub run list sortable, default STARTED desc | `affb6d7` | `tests/test_browser_hub.py::test_run_list_is_sortable_and_defaults_to_newest_first` |
| 055 `ralphctl runs` parity | `73854b2` | `tests/test_cli_runs_sort.py::test_default_order_is_newest_first`, `::test_sort_iterations_is_numeric_not_the_rendered_cell`, `::test_sort_state_and_verdict_use_lifecycle_order`, `::test_json_order_matches_human_order_for_every_key`, `::test_sort_composes_with_state_filter`, `::test_sort_keys_mirror_the_hub_columns` |

## #1 — a run's PRD opens in a hub dialog (requirement N)

`GET /prd` with the on-disk fallback (`state.prd_path()`,
`ui_server.prd_text()`), `app.js` `openTextDialog` (textContent only).

| Task | Commit | Tests |
|---|---|---|
| 056 PRD dialog | `f9022bd` | `tests/test_hub_prd_dialog.py::test_prd_endpoint_falls_back_to_on_disk_for_a_dead_run`, `::test_prd_endpoint_prefers_the_composite_prd`, `::test_app_js_renders_dialog_text_with_text_nodes_only`, `tests/test_browser_hub.py::test_run_detail_opens_the_prd_in_a_dialog`, `tests/test_cli_ui.py` |

## #2 — a task's detail opens in a hub dialog (requirement N)

`app.js` `tr.task-row` → `openTaskDialog`/`taskDialogText` → `openTextDialog`.

| Task | Commit | Tests |
|---|---|---|
| 057 task dialog | `b389e2d` | `tests/test_hub_task_dialog.py::test_task_dialog_text_carries_the_plan_fields`, `::test_task_rows_are_clickable_and_keyboard_reachable`, `::test_task_dialog_text_uses_text_nodes_only`, `tests/test_browser_hub.py::test_run_detail_opens_a_task_in_a_dialog` |

## #12

Already closed upstream before this wave; out of scope, no work in this run.

---

# Issue traceability — ralphd v0.6 (`selfdev-v06-release`)

Second wave in this file. Maps every GitHub issue in scope (**#14–#22**) to its
PRD requirement letter (`docs/prds/v0.6-first-release.md`, sections A–J), the
tasks that implemented it, the commits, and the tests that cover it.

Unlike v0.5, this wave's requirement **I** is to close the issues *from inside
the run*: task 047 closed #14–#22 through the GitHub REST API (a small
`urllib` script, no `gh` CLI, no token value in any argument or log) and
recorded per issue the number, the HTTP status, the resulting state and the
comment url in `artifacts/reports/issue-closure.md` beside this file. Every
section below therefore carries a `**Closure:**` line, and
`tests/test_issue_traceability.py` holds those lines against that record: a
section may not claim a closure the record does not show.

- Baseline commit (PRD, this run's HEAD at start): `0963c9b`.
- Final verification sweep: `81a955c` — 1956 passed, 0 failed, 0 skipped across
  all three tiers (fast lane 1913, browser e2e 30, docker siblings 13); log in
  `<run-dir>/artifacts/v0.6-final-suite.log`.
- Machine check, as for the v0.5 wave: every 7-hex sha below must resolve to a
  commit, every path under `tests/` must exist and every test node id must be
  a real test — `tests/test_report_claims.py` over `tests/report_claims.py`,
  applied to every report in this directory; `tests/test_issue_traceability.py`
  keeps what is specific to this report (per-issue sections for both waves, the
  density floors, and the closure cross-check).

## Summary

| Issue | PRD req | Tasks | Commits |
|---|---|---|---|
| #15 mid-write `tasks.json` served as "no tasks" | A | 002, 003, 004, 005 | `ed2b189`, `f136b8c`, `9bbe300`, `badd26e` |
| #16 approach `n/m` on every surface | B | 006, 007, 008 | `ff58efb`, `d5b78e3`, `c772c1c` |
| #14 honest cost, derived rates, a visible model id | C | 049, 009, 010, 011, 012, 050 | `dec4545`, `8ef8148`, `f3de271`, `45bb405`, `54f8770`, `b03197c` |
| #21 task progress in the run list | D | 013, 014, 015 | `552f821`, `bedd3d8`, `0d494ef` |
| #17 steering readable, not just writable | E | 016, 017, 018 | `f6656cd`, `29be8d8`, `b19348c` |
| #18 click to view details across the run detail page | F | 019–028, 051 | `64edc91` … `0df8694`, `5807afc` |
| #19 deleting a dead run takes one command | G | 029, 030, 031 | `ff76a01`, `584a512`, `a7dd650` |
| #20 build the job image, and let a job bring its own | H | 032–039 | `48c8515` … `c42ffe5` |
| #22 release hygiene and the doc audit | J | 040–045, 043b–043e, 046, 048 | `0ed3be1` … `81a955c` |
| #14–#22 closing them from inside the run | I | 046 (this report), 047 | this report + `artifacts/reports/issue-closure.md` |

### Numbering gaps in the task→commit mapping (deliberate — do not "fix" them)

The mapping above has no hole, but three entries need saying out loud:

- **Task 001 produced no commit.** It confirmed the editable install and
  recorded the baseline full-suite run (802 passed) into
  `<run-dir>/artifacts/baseline-suite.log`; its success criteria explicitly
  required `git status --porcelain` to stay empty. No code changed, so no sha.
- **Tasks 002 and 003 landed without the `task NNN:` commit-title prefix**
  (`ed2b189` "Harden the tasks.json read path…", `f136b8c` "Serve the tasks
  stale flag…"). They were already pushed when the convention was noticed, and
  rewriting pushed history to tidy a title is not worth it, so they stay as
  they are; from task 004 (`9bbe300`) onwards every commit in this wave carries
  the prefix. The mapping in this report is the index that closes that gap.
- **Task 043d is recorded `failed`, and its scope shipped anyway** — see the
  #22 section below.

## This run's own cost (requirement C evidence, #14)

This run *is* the fixture #14 was written from, so the honest answer matters:

- **Recorded on disk: still an implausible zero.** The engine that has been
  running this job was built from the pre-`dec4545` source at `start`, so its
  own `<run-dir>/status.json` still records `costUSD: 0` with
  `costPriced: true` beside 300,995,660 total tokens (at iteration 139 of 300;
  the run was still going while this report was written).
- **No longer rendered as money.** Classification happens on *read*, so the
  shipped code reclassifies that payload without rewriting it:
  `state.is_zero_quote(usage)` is True, `state.cost_status(usage)` is
  `unknown`, and `state.format_cost(usage, decimals=4)` renders `unavailable`.
  `ralphctl status`, `ralphctl cost`, `ralphctl runs` and the hub therefore
  print `unavailable` for this run — never `$0.00`. That is the #14 defect
  fixed, verified against this very run dir.
- **It did not become *derivable* for this run.** `price_strategy` defaults to
  `none` and this job was started before the knob existed, so nothing derived
  money into its state; the route id (`amazon-bedrock/eu.anthropic.claude-opus-5`)
  also only ever reached `iterations/NNNN/meta.json`, not the rollup, because
  task 012's recording landed mid-run.
- **What it would be under `--price-strategy aws`.** Feeding the same recorded
  counters through the shipped table (`src/ralphd/engine/pricing_aws.py`, EU
  region rates, as-of date in its own `AS_OF` constant) derives ≈ **$342.80**
  total: planning $1.077671, worker $271.50, verify $70.22. Marked derived, not
  quoted — an estimate, and the first honest one this project has had.

So: **unknown, honestly labelled, with a derivable estimate available for the
next run**, which is exactly what requirement C asked for. Full anomaly write-up
(including the same numbers for the v0.5 runs) is in
`artifacts/reports/pricing-anomaly.md` §7.

---

## #15 — a mid-write `tasks.json` is never served as "no tasks" (requirement A)

`src/ralphd/engine/state.py` (`read_tasks_doc`, `TasksRead`, the sad-path-only
`.tasks-last-good.json`), `src/ralphd/engine/api.py`, `src/ralphd/cli/ui_server.py`,
`src/ralphd/cli/main.py`, `src/ralphd/cli/web/app.js`; `docs/architecture.md` §3.

| Task | Commit | Tests |
|---|---|---|
| 002 hardened reader: absent / file / last-good / unreadable, bounded re-read | `ed2b189` | `tests/test_tasks_read_hardening.py::test_absent_tasks_file_is_absent_not_stale`, `::test_unparseable_serves_last_good_flagged_stale`, `::test_unparseable_with_no_last_good_is_unreadable_not_empty`, `::test_bounded_reread_recovers_a_write_that_lands_mid_read`, `::test_last_good_survives_an_engine_restart`, `::test_a_poller_never_observes_an_empty_list_during_agent_rewrites` |
| 003 the stale contract on GET /tasks and the /status counts | `f136b8c` | `tests/test_tasks_stale_api.py::test_truncated_plan_serves_last_good_flagged_stale_on_both_endpoints`, `::test_a_plan_key_cannot_forge_the_freshness_flag`, `::test_polling_both_endpoints_during_agent_rewrites_never_sees_an_empty_plan` |
| 004 hub server and `ralphctl tasks` reuse the hardened reader | `9bbe300` | `tests/test_tasks_stale_cli.py::test_tasks_of_a_dead_run_serves_the_last_good_plan_flagged_stale`, `::test_hub_run_detail_never_renders_an_empty_table_under_a_rewrite_loop`, `::test_neither_host_side_read_json_will_touch_tasks_json`, `::test_tasks_of_a_live_run_is_unchanged` |
| 005 the stale label in the hub task table, proven not to blink | `badd26e` | `tests/test_browser_hub.py::test_run_detail_labels_a_stale_task_read_and_never_blinks_empty`, `tests/test_tasks_stale_cli.py::test_hub_run_detail_carries_the_stale_label_strings`, `::test_a_forged_label_in_tasks_json_cannot_fake_staleness`, `::test_a_live_pre_v06_engine_answer_gets_no_invented_label` |

**Closure:** closed — closing comment `POST .../issues/15/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/15#issuecomment-5368507712),
state change `PATCH .../issues/15` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:11:50Z;
recorded in `artifacts/reports/issue-closure.md`.

## #16 — approach `n/m` on every surface (requirement B)

`src/ralphd/engine/main.py` + `src/ralphd/engine/loop.py` (the `maxApproaches`
write), `src/ralphd/engine/state.py` (`format_approach`, the one renderer),
`src/ralphd/cli/main.py`, `src/ralphd/cli/ui_server.py`, `src/ralphd/cli/web/app.js`.

| Task | Commit | Tests |
|---|---|---|
| 006 `maxApproaches` in status.json and GET /status, from the first write | `ff58efb` | `tests/test_status_max_approaches.py::test_status_serves_the_denominator_written_on_disk`, `::test_pre_v06_status_json_yields_an_explicit_null_denominator`, `::test_the_first_status_write_already_carries_the_denominator`, `::test_engine_writes_max_approaches_from_starting_to_terminal` |
| 007 `2/3` in `ralphctl status` and `ralphctl runs`, bare `2` with no ceiling | `d5b78e3` | `tests/test_cli_approach_denominator.py::test_format_approach_never_invents_a_denominator`, `::test_status_omits_the_approach_segment_with_no_approach`, `::test_runs_renders_all_three_approach_cells`, `::test_runs_sorts_on_the_raw_number_not_the_rendered_string`, `::test_status_and_runs_agree_for_the_same_run` |
| 008 the same cell in the hub run list and run detail, sort stays numeric | `c772c1c` | `tests/test_browser_hub.py::test_run_list_and_detail_render_the_approach_denominator`, `tests/test_cli_approach_denominator.py::test_hub_and_cli_render_the_same_string_for_the_same_run`, `::test_hub_run_detail_recomputes_a_forged_display`, `::test_hub_does_not_guess_a_limit_for_a_pre_v06_live_engine` |

Its SPEC 11.2/11.3 prose got its own code-derived check later, in task 043e
(`cc8c8a2`) — see #22.

**Closure:** closed — closing comment `POST .../issues/16/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/16#issuecomment-5368507946),
state change `PATCH .../issues/16` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:11:52Z;
recorded in `artifacts/reports/issue-closure.md`.

## #14 — honest cost, derived rates, and a model id you can see (requirement C)

`src/ralphd/engine/state.py` (`is_zero_quote`, `billable_tokens`, `cost_status`,
`format_cost`, `model_ids`), `src/ralphd/engine/pricing.py` (`resolve_pricing`,
`PricingChain`), `src/ralphd/engine/pricing_aws.py`, `src/ralphd/engine/runner.py`,
`src/ralphd/engine/loop.py`, `src/ralphd/engine/config.py`,
`tools/refresh_bedrock_rates.py`.

| Task | Commit | Tests |
|---|---|---|
| 049 an implausible zero quote is unknown, never `$0.00` (steering 001) | `dec4545` | `tests/test_cost_zero_quote.py::test_the_live_iteration_payload_is_classified_unknown_not_priced`, `::test_the_live_run_level_rollup_is_classified_unknown_not_priced`, `::test_the_no_traffic_int_zero_still_renders_as_a_real_zero`, `::test_a_declared_free_route_keeps_its_zero_on_every_surface`, `::test_a_zero_quote_over_billed_tokens_is_recorded_as_unpriced` |
| 009 the built-in Bedrock rate table, alias map, as-of date, refresh tool | `8ef8148` | `tests/test_pricing_aws.py::test_every_documented_gateway_form_resolves_to_a_rate`, `::test_the_run_that_motivated_the_issue_now_derives_real_money`, `::test_region_prefixes_keep_their_own_rate`, `::test_the_table_carries_a_machine_readable_as_of_date`, `::test_a_missing_or_unparseable_as_of_date_is_an_error`, `::test_the_staleness_signal_flips_at_the_documented_threshold` |
| 010 the `price_strategy` knob across profile, job.yaml, env, API, `start` | `f3de271` | `tests/test_price_strategy.py::test_the_shipped_default_is_none`, `::test_an_unknown_strategy_degrades_to_none_with_a_warning`, `::test_env_override_beats_job_yaml_in_both_directions`, `::test_effective_reports_the_behaviour_not_the_typo`, `::test_get_config_serves_the_strategy_from_a_live_engine` |
| 011 derive from the table when the strategy is `aws`, operator map winning | `45bb405` | `tests/test_price_strategy_derive.py::test_aws_without_an_operator_map_resolves_to_the_builtin_table`, `::test_an_implausible_zero_quote_is_derived_from_the_builtin_table`, `::test_the_operator_map_wins_over_the_builtin_table`, `::test_an_unknown_model_under_aws_stays_unavailable`, `::test_a_provider_quoted_price_is_never_replaced_by_a_derived_one` |
| 012 record the model id the engine actually resolved (and the raw gateway id) | `54f8770` | `tests/test_model_id_recorded.py::test_the_scanner_records_the_model_pi_reports`, `::test_a_run_over_a_gateway_shaped_id_records_both_ids`, `::test_an_unpinned_run_records_the_model_pi_chose`, `::test_get_status_publishes_explicit_nulls_before_any_traffic`, `::test_status_text_names_the_model_and_the_gateway_id` |
| 050 price an unpinned run from the id pi reported | `b03197c` | `tests/test_price_strategy_observed_model.py::test_an_unpinned_zero_quoting_run_derives_from_the_observed_id`, `::test_a_pinned_ref_outranks_the_observed_id`, `::test_a_pinned_but_unknown_ref_stays_unavailable`, `::test_a_message_naming_no_model_is_byte_identical_to_pre_050`, `::test_an_unpinned_engine_run_reports_derived_money` |

Evidence from this run itself is in *This run's own cost* above, and in
`artifacts/reports/pricing-anomaly.md` §7.

**Closure:** closed — closing comment `POST .../issues/14/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/14#issuecomment-5368507414),
state change `PATCH .../issues/14` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:11:48Z;
recorded in `artifacts/reports/issue-closure.md`.

## #21 — task progress in the run list (requirement D, depends on A)

`src/ralphd/engine/state.py` (`format_task_counts`, `format_task_fraction`,
`format_task_trouble`, `format_task_column`, `TasksRead.row_fields`),
`src/ralphd/cli/ui_server.py`, `src/ralphd/cli/main.py`, `src/ralphd/cli/web/app.js`.

| Task | Commit | Tests |
|---|---|---|
| 013 per-row counts from one hardened read, no live call while rendering | `552f821` | `tests/test_hub_task_counts.py::test_run_list_reads_each_plan_exactly_once_through_the_hardened_reader`, `::test_run_list_makes_no_http_call_while_rendering`, `::test_a_plan_less_run_gets_a_blank_column_not_zero_over_zero`, `::test_a_mid_write_plan_keeps_its_fraction_flagged_stale`, `::test_rows_of_several_runs_do_not_borrow_each_others_counts` |
| 014 the hub TASKS column: ratio sort, trouble flags, blank sorts last | `bedd3d8` | `tests/test_browser_hub.py::test_run_list_tasks_column_renders_flags_and_sorts_on_progress`, `tests/test_hub_task_counts.py::test_the_tasks_column_sorts_on_the_ratio_not_the_rendered_text`, `::test_the_ratio_orders_five_sevenths_above_a_hundred_of_two_fifty` |
| 015 the same column in `ralphctl runs`, agreeing with `ralphctl status` | `0d494ef` | `tests/test_cli_runs_tasks_column.py::test_the_column_never_renders_zero_over_zero`, `::test_cli_and_hub_rows_carry_the_identical_task_fields`, `::test_cmd_runs_reads_each_plan_once_through_the_hardened_reader`, `::test_cmd_runs_does_not_read_the_plan_of_a_filtered_out_run`, `::test_runs_json_carries_the_raw_counts_and_the_flag_wording` |

**Closure:** closed — closing comment `POST .../issues/21/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/21#issuecomment-5368509058),
state change `PATCH .../issues/21` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:11:59Z;
recorded in `artifacts/reports/issue-closure.md`.

## #17 — steering must be readable, not just writable (requirement E)

`src/ralphd/engine/state.py` (`steering_entries`, the one reader of
`<run-dir>/steering/`), `src/ralphd/engine/api.py`, `src/ralphd/cli/ui_server.py`
(`steering_list`), `src/ralphd/cli/main.py`, `src/ralphd/cli/web/app.js`.

| Task | Commit | Tests |
|---|---|---|
| 016 the shared reader + GET /steering + the hub's live-first endpoint | `f6656cd` | `tests/test_hub_steering.py::test_entries_carry_name_timestamp_state_and_body`, `::test_hub_falls_back_to_disk_for_a_dead_run`, `::test_live_and_on_disk_answers_are_the_same_entries`, `::test_a_pre_v06_live_answer_is_completed_from_disk`, `::test_a_live_entry_with_no_file_on_disk_invents_nothing` |
| 017 the run-detail steering history, pending then applied, in one dialog | `29be8d8` | `tests/test_browser_hub.py::test_run_detail_lists_steering_history_from_the_on_disk_snapshot`, `::test_steering_entry_appears_pending_then_flips_to_applied`, `tests/test_hub_steering.py::test_an_entry_with_no_timestamp_claims_no_arrival_time`, `::test_a_forged_tslocal_from_a_live_answer_is_recomputed` |
| 018 `ralphctl steer --list` over the same code path, live and dead | `b19348c` | `tests/test_cli_steer_list.py::test_list_of_a_dead_run_prints_pending_and_applied`, `::test_list_does_not_consume_stdin_or_send_anything`, `::test_cli_and_hub_agree_for_a_live_run`, `::test_real_engine_live_then_container_gone` |

**Closure:** closed — closing comment `POST .../issues/17/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/17#issuecomment-5368508187),
state change `PATCH .../issues/17` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:11:53Z;
recorded in `artifacts/reports/issue-closure.md`.

## #18 — click to view details, across the run detail page (requirement F)

Five sub-surfaces, each shaped once in `src/ralphd/engine/state.py` and rendered
by both `ralphctl` and the hub through the single `openTextDialog`
(`src/ralphd/cli/web/app.js`), so the hub text is asserted byte-equal to the
CLI text rather than re-worded.

| Task | Commit | Tests |
|---|---|---|
| 019 #18.1 `ralphctl iteration <run> <n>` (`iteration_detail`, `format_exit_reason`) | `64edc91` | `tests/test_cli_iteration_detail.py::test_exit_reason_ranks_the_raw_signals`, `::test_iteration_detail_shapes_meta_plus_derived_fields`, `::test_iteration_detail_says_what_it_does_not_know`, `::test_iteration_detail_never_prices_an_implausible_zero`, `::test_iteration_detail_recomputes_forged_display_fields` |
| 020 #18.1 the hub timeline iteration dialog | `07a25e3` | `tests/test_browser_hub.py::test_timeline_row_opens_the_iteration_dialog`, `tests/test_hub_iteration_dialog.py::test_hub_text_is_exactly_what_ralphctl_iteration_prints`, `::test_no_transcript_says_so_instead_of_an_empty_log`, `::test_endpoint_log_query_can_skip_the_transcript` |
| 021 #18.2 `ralphctl docs` + `src/ralphd/engine/redact.py` (redacted job.yaml) | `6a9fa4a` | `tests/test_cli_run_documents.py::test_run_documents_lists_every_known_document_present_or_not`, `::test_run_documents_without_a_config_dir_says_out_of_reach_not_missing`, `::test_redact_job_yaml_masks_by_name_and_scrubs_by_value`, `::test_config_dir_secrets_reads_every_staged_source` |
| 022 #18.2 the hub state-document dialogs | `123d99d` | `tests/test_browser_hub.py::test_run_detail_opens_the_state_document_dialogs`, `tests/test_hub_run_documents.py::test_document_view_text_is_what_ralphctl_docs_prints`, `::test_document_view_redacts_job_yaml`, `::test_document_list_carries_no_bodies` |
| 023 #18.3 `ralphctl artifacts show` (reflection report, suggestions.diff) | `6f2c768` | `tests/test_cli_artifacts.py::test_well_known_names_resolve_to_their_paths_both_spellings`, `::test_arbitrary_paths_resolve_and_illegal_names_do_not`, `::test_artifact_body_wordings_cover_blank_and_binary`, `::test_ls_labels_the_well_known_names_and_needs_no_container` |
| 024 #18.3 the hub artifacts panel and reflect-report dialog | `a181660` | `tests/test_browser_hub.py::test_run_detail_browses_artifacts_and_opens_the_reflect_report`, `tests/test_hub_artifacts.py::test_artifact_view_text_is_what_ralphctl_artifacts_show_prints`, `::test_artifact_view_refuses_anything_that_is_not_an_artifact`, `::test_artifact_list_carries_no_bodies` |
| 025 #18.4 `ralphctl fault`: signature, ladder, budget (`explain_fault` is now the one ladder) | `7f5290a` | `tests/test_cli_fault_explanation.py::test_classify_fault_delegates_to_explain_fault`, `::test_every_signature_row_is_named_and_described`, `::test_format_fault_ladder_reads_the_runs_own_backoffs`, `::test_format_fault_budget_says_spent_left_and_the_run_total`, `::test_read_events_filters_and_survives_a_half_written_line` |
| 051 #18.4 the explanation stops claiming an operator abort it cannot establish (steering 004) | `5807afc` | `tests/test_cli_fault_explanation.py::test_a_recorded_abort_alone_is_never_blamed_on_the_operator`, `::test_an_established_operator_abort_is_allowed_to_say_operator`, `::test_a_signal_terminated_iteration_is_named_as_one`, `::test_the_loop_threads_who_recorded_the_abort_into_the_explanation` |
| 026 #18.4 the hub fault dialog behind the failure / infra-wait badge | `9b5b352` | `tests/test_browser_hub.py::test_run_detail_opens_the_fault_dialog_from_the_badge`, `tests/test_hub_fault_dialog.py::test_fault_view_text_is_what_ralphctl_fault_prints`, `::test_fault_view_text_carries_the_four_facts_18_4_asked_for`, `::test_hub_fault_endpoint_never_touches_the_live_api` |
| 027 #18.5 `ralphctl cost` by phase and approach, priced / derived / unavailable | `985f982` | `tests/test_cli_cost_breakdown.py::test_cost_source_words_each_kind_of_money`, `::test_cost_breakdown_lines_label_priced_derived_and_unavailable`, `::test_an_implausible_zero_is_unavailable_and_names_the_anomaly`, `::test_a_run_with_no_usage_says_so_instead_of_zero` |
| 028 #18.5 the hub cost dialog on the cost cell, headline unchanged | `0df8694` | `tests/test_browser_hub.py::test_run_detail_opens_the_cost_dialog_from_the_cost_cell`, `tests/test_hub_cost_dialog.py::test_cost_view_text_is_what_ralphctl_cost_prints`, `::test_cost_view_headline_is_the_string_the_card_already_shows`, `::test_cost_view_renders_the_implausible_zero_quote_as_unavailable` |

**Closure:** closed — closing comment `POST .../issues/18/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/18#issuecomment-5368508395),
state change `PATCH .../issues/18` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:11:55Z;
recorded in `artifacts/reports/issue-closure.md`.

## #19 — deleting a dead run takes one command, and works from the hub (requirement G)

`src/ralphd/cli/main.py` (`_teardown_container`, `remove_run_state` — one
sequence for both surfaces), `src/ralphd/cli/ui_server.py` (`delete_run`,
`deletion_refusal`, `deletion_fields`), `src/ralphd/cli/web/app.js`.

| Task | Commit | Tests |
|---|---|---|
| 029 `ralphctl rm --force` = stop then remove, still refusing a live job | `ff76a01` | `tests/test_cli_rm_force.py::test_rm_force_stops_the_container_then_deletes_everything`, `::test_rm_force_reuses_stops_container_teardown_sequence`, `::test_rm_force_refuses_a_running_job_and_touches_nothing`, `::test_rm_force_refuses_when_it_cannot_establish_the_job_is_over`, `::test_real_rm_force_leaves_no_container_run_dir_or_config_dir` |
| 030 DELETE /api/runs/<id>, terminal runs only, refusal carries its reason | `584a512` | `tests/test_hub_delete.py::test_a_terminal_run_may_be_deleted`, `::test_an_active_run_is_refused_with_its_state_named`, `::test_a_state_we_cannot_read_is_not_permission`, `::test_delete_uses_the_cli_removal_sequence`, `::test_delete_refuses_a_traversal_shaped_run_id` |
| 031 the hub affordance: confirm dialog naming the run id, disabled + reason | `a7dd650` | `tests/test_browser_hub.py::test_run_list_and_detail_delete_a_run_behind_a_confirm_dialog`, `tests/test_hub_delete_affordance.py::test_the_fields_are_exactly_the_gate_for_every_status_shape`, `::test_a_forged_deletable_in_status_json_cannot_offer_a_deletion`, `::test_deletable_predicts_what_the_endpoint_answers` |

**Closure:** closed — closing comment `POST .../issues/19/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/19#issuecomment-5368508618),
state change `PATCH .../issues/19` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:11:56Z;
recorded in `artifacts/reports/issue-closure.md`.

## #20 — build the job image, and let a job bring its own (requirement H)

`src/ralphd/cli/image.py` (docker-free by test), `src/ralphd/cli/main.py`
(`resolve_job_image`, `_resolve_image_supply`, `image_staleness`),
`src/ralphd/engine/state.py` (`image_record`, `format_image`),
`container/Dockerfile`, `pyproject.toml` (the wheel's packaged inputs).

| Task | Commit | Tests |
|---|---|---|
| 032 H1 stable, cheap content hashing of the image inputs | `48c8515` | `tests/test_image_hash.py::test_the_same_tree_hashes_the_same_twice`, `::test_the_hash_does_not_depend_on_mtimes`, `::test_editing_a_hashed_input_changes_the_hash`, `::test_editing_an_excluded_path_does_not_change_the_hash`, `::test_a_symlink_is_hashed_as_its_target_and_never_followed` |
| 033 H1 `start` builds `ralphd:<hash>` on a cache miss, loudly on failure | `4b59831` | `tests/test_image_build.py::test_start_builds_the_content_hashed_tag_when_it_is_missing`, `::test_a_second_start_is_a_tag_lookup_with_no_build`, `::test_an_explicit_image_neither_hashes_nor_builds`, `::test_a_failed_build_aborts_start_before_any_run_state_exists`, `::test_build_output_is_visible_as_it_arrives_but_bounded` |
| 034 H2 a supplied image is a base; the job image is derived from it | `641c728` | `tests/test_image_derived.py::test_the_derived_tag_depends_on_the_base`, `::test_the_derived_tag_depends_on_the_engine_source`, `::test_the_generated_dockerfile_layers_the_engine_and_pi_onto_the_base`, `::test_the_recipe_copies_the_version_pins_instead_of_restating_them` |
| 035 H3 `--dockerfile`, and one ranked answer to "which image runs" | `47a923e` | `tests/test_image_dockerfile.py::test_the_context_is_the_dockerfiles_own_directory`, `::test_two_recipes_in_one_context_are_two_base_images`, `::test_a_file_with_no_from_instruction_is_not_a_dockerfile`, `::test_a_dockerfile_builds_a_base_and_the_job_image_is_derived_from_it` |
| 036 H4 the resolved image recorded in run state, and resumed from it | `9312274` | `tests/test_image_recorded.py::test_start_records_the_resolved_reference_and_the_observed_id`, `::test_resume_reuses_the_recorded_image_and_neither_hashes_nor_builds`, `::test_absence_is_never_a_third_case`, `::test_the_two_readers_agree`, `tests/test_image_dockerfile.py::test_a_changed_recipe_is_replayed_only_once_the_recorded_image_is_gone` |
| 037 H4 `ralphctl doctor` reports staleness, not mere existence | `03ce620` | `tests/test_cli_doctor_image_staleness.py::test_the_current_tag_is_fresh`, `::test_a_hashed_tag_from_another_source_tree_is_stale`, `::test_a_pin_is_unknowable_not_fresh`, `::test_a_derived_or_base_tag_is_never_compared_to_a_source_hash`, `::test_the_record_answers_when_the_reference_cannot` |
| 038 H4 a wheel install ships its image inputs and builds the same tag | `fccf517` | `tests/test_image_packaged_inputs.py::test_the_wheel_mapping_ships_exactly_the_packaged_files`, `::test_a_checkout_wins_over_package_data`, `::test_a_wheel_install_finds_its_own_packaged_inputs`, `::test_staging_reproduces_a_checkout_and_therefore_its_tag` |
| 039 the real-build tier: it does run here, on the host daemon | `c42ffe5` | `tests/test_image_real_build.py::test_the_production_resolve_builds_a_derived_tag_from_the_minimal_base`, `::test_the_derived_image_runs_ralphd_engine`, `::test_the_derived_image_layers_onto_the_base_and_carries_the_pins`, `::test_a_second_resolve_of_the_same_base_is_a_pure_cache_hit`, `::test_the_build_does_not_ask_for_buildkit` |

Verdict, cost and evidence for the real-build tier: `artifacts/reports/real-build-tier.md`.

**Closure:** closed — closing comment `POST .../issues/20/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/20#issuecomment-5368508830),
state change `PATCH .../issues/20` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:11:58Z;
recorded in `artifacts/reports/issue-closure.md`.

## #22 — release hygiene and the doc audit (requirement J)

`pyproject.toml`, `src/ralphd/__init__.py`, `README.md`, `SPEC.md`,
`docs/cli.md`, `docs/api.md`, `docs/architecture.md`, `docs/roadmap.md`,
`docs/prds/v0.6-first-release.md`, and five doc-check test modules.

| Task | Commit | Tests |
|---|---|---|
| 040 drop the dead `cli` extra, ship a deliberate 0.6.0 | `0ed3be1` | `tests/test_packaging_metadata.py::test_version_is_a_deliberate_release_version`, `::test_pyproject_and_package_literal_agree_on_the_version`, `::test_both_console_entrypoints_report_the_declared_version`, `::test_no_declared_requirement_is_unused`, `::test_a_runtime_extra_must_be_imported_by_src` |
| 041 re-read every report under `artifacts/reports`, not just one | `e71f8b7` | `tests/test_report_claims.py::test_the_reports_directory_is_discovered_not_enumerated`, `::test_every_listed_commit_sha_exists`, `::test_every_listed_path_exists`, `::test_every_listed_test_node_exists`, `::test_the_claim_checks_have_exactly_one_implementation` |
| 042 a documented-but-nonexistent flag, subcommand, route or field fails the suite | `cb13e68` | `tests/test_docs_consistency.py::test_every_flag_documented_in_cli_md_exists_in_the_parser`, `::test_every_documented_subcommand_invocation_exists`, `::test_a_fake_cli_flag_in_the_docs_is_reported`, `::test_another_programs_flags_are_not_read_as_ralphctl_flags` |
| 043 the provably wrong doc claims corrected, each with its check | `c3579cb` | `tests/test_docs_semantics.py::test_documented_version_response_keys_are_exactly_the_served_ones`, `::test_the_documented_error_shape_is_the_shape_the_engine_sends`, `::test_the_error_shape_check_would_catch_the_rfc_7807_claim`, `::test_neither_the_engine_nor_the_docs_knows_a_paused_phase`, `::test_every_status_field_the_engine_writes_is_documented` |
| 043b the semantic pass over `docs/cli.md` and `docs/architecture.md`, plus two validation fixes | `b4552c3`, `25f450e`, `9ab8aa5` | `tests/test_docs_semantics.py::test_every_documented_value_list_is_the_parsers_own_choice_set`, `::test_the_documented_state_filter_names_every_state_a_run_can_record`, `::test_the_event_stream_format_switches_on_the_json_flag_only`, `::test_the_hub_reads_the_docs_call_live_are_the_ones_that_proxy`, `::test_the_workspace_is_a_host_path_and_no_doc_offers_a_volume`, `tests/test_docs_consistency.py::test_every_backticked_identifier_in_the_docs_exists_in_the_code` |
| 043c SPEC section 10 is the parser's own CLI surface | `98faa52` | `tests/test_spec_cli_surface.py::test_the_command_table_names_every_subcommand_and_invents_none`, `::test_the_action_check_would_catch_the_llm_set_claim`, `::test_the_start_flag_table_is_the_start_parsers_own_flag_set`, `::test_the_documented_sort_keys_are_the_cli_and_the_hubs_own_key_set` |
| 043d the rest of SPEC (5.x, 6.x, 7.1, 8.6, 9.x, 11.x, 3.2, 15) — **recorded `failed`**, scope shipped | `b923af2`, `168a041` | `tests/test_spec_state_surface.py::test_the_spec_run_dir_tree_lists_every_file_the_run_dir_holds`, `::test_every_status_field_the_engine_writes_is_in_the_spec_table`, `::test_the_tasks_read_section_is_the_readers_own_contract`, `::test_the_job_image_block_is_the_image_modules_own_vocabulary`, `::test_the_job_image_check_would_catch_the_wording_it_replaced` |
| 043e the residual gap: SPEC 11.2/11.3's approach denominator gets its check | `cc8c8a2` | `tests/test_spec_state_surface.py::test_the_hub_approach_cell_is_the_shared_formatters_own_renderings`, `::test_the_approach_cell_check_reads_the_code_not_only_the_prose`, `::test_the_approach_cell_check_would_catch_the_wording_it_replaced` |
| 044 the v0.6 roadmap block; nothing this wave delivered is still deferred | `4e90dec` | `tests/test_roadmap_v06.py::test_the_roadmap_has_a_block_for_the_declared_version`, `::test_every_prd_requirement_is_accounted_for_in_the_v06_block`, `::test_the_v06_block_names_the_verbs_the_wave_added`, `::test_the_deferred_list_defers_publishing_the_image_not_building_it` |
| 045 the final full sweep, all three tiers | `81a955c` | `tests/test_cli_logs_pty_termios.py::test_sigint_restores_termios_mode`, `tests/test_cli_logs_signal.py::test_sigint_during_follow_exits_clean_no_traceback` (the two load-flaky tests it made load-tolerant); sweep log `<run-dir>/artifacts/v0.6-final-suite.log` |
| 046 this report, and its retargeted checks | this file | `tests/test_issue_traceability.py` |
| 048 this run's row in `docs/prds/README.md` | after 047 | `tests/test_docs_consistency.py`, `tests/test_report_claims.py` |

### Task 043d: `failed` on the record, delivered in the code

Task 043d is the one task in this wave that ends **`failed`**, and it is left
that way deliberately — the record of three consumed validation attempts is
evidence, not noise:

- **What it delivered:** SPEC.md sections 3.2 (the job-image lifecycle), 3.5,
  5.1, 5.2, 5.3, 6.2, 6.7, 7.1, 8.6, 9.2, 9.3, 11.2, 11.3 and 15 rewritten to
  this wave's behaviour, each with a code-derived check in the new
  `tests/test_spec_state_surface.py` (47 tests at `168a041`, all paired with a
  would-catch-the-old-wording case). Commits `b923af2` (the pass) and
  `168a041` (the section-3.2 check that validation attempt 1 found missing).
- **Why it is `failed`:** attempt 2's independent verification found one
  enumerated item — SPEC 11.2/11.3's hub approach denominator — documented but
  checked by nothing (reverting the prose left all 160 SPEC-reading tests
  green). Three validation attempts had then been consumed, so the task was
  closed `failed` rather than looped again.
- **Who closed the gap:** task **043e** (`cc8c8a2`) added exactly that missing
  check — 9 tests deriving the hub's approach cell from real `ui_server.run_list`
  rows plus 7 mutation tests over the real SPEC text — and is `completed`.
- **Net effect on #22:** the requirement holds; no SPEC claim from this wave is
  left unchecked. The failure is a process record, not an outstanding gap.

**Closure:** closed — closing comment `POST .../issues/22/comments`
HTTP 201
(https://github.com/n-orlov/ralphd/issues/22#issuecomment-5368509311),
state change `PATCH .../issues/22` HTTP 200, state re-read
as `closed` (`completed`) at 2026-08-21T10:12:01Z;
recorded in `artifacts/reports/issue-closure.md`. The comment says plainly
that sub-task 043d ended `failed` while its scope landed in `b923af2`,
`168a041` and `cc8c8a2`.