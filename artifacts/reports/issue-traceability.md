# Issue traceability — ralphd v0.5 (`selfdev-v05-resilience`)

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
