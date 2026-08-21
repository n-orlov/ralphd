# Traceability — selfdev-roadmap-4

Requirement-to-test mapping for the PRD requirements (A–F) plus the
operator-steered infra-fault-tolerance requirement (001a) closed in this
wave. All node IDs below exist in the tree at the commit this file was
last updated in and pass under `.venv/bin/python -m pytest`.

Built/maintained by task 013 per the pointer in `docs/roadmap.md`'s status
note and `docs/architecture.md`'s failure-handling section.

## A — resume reproduces full env wiring

Persist resolved `--forward-env`/`--llm-env`/`--env` values at start and
replay them on resume (`src/ralphd/cli/main.py: cmd_start`, `cmd_resume`).

| Test node ID | Covers |
|---|---|
| `tests/test_cli_resume_env_wiring.py::test_start_persists_extra_env_wiring_file` | start writes resolved name=value pairs to the config-dir wiring file, mode 0600 |
| `tests/test_cli_resume_env_wiring.py::test_resume_reproduces_forward_env_llm_env_and_env` | resume replays original values even after the shell env is wiped/changed |
| `tests/test_cli_resume_env_wiring.py::test_resume_of_run_without_env_wiring_file_is_unaffected` | migration: no wiring file resumes cleanly |
| `tests/test_cli_resume_env_wiring.py::test_resume_of_prewiring_run_with_neither_file_is_unaffected` | migration: pre-feature run (no llm-wiring.json either) resumes cleanly |
| `tests/test_cli_resume_env_wiring.py::test_no_extra_env_flags_leaves_no_wiring_file` | no spurious file written when no extra env flags given |
| `tests/test_cli_resume_env_wiring.py::test_env_wiring_secret_never_lands_in_run_dir` | secret values never written under the run dir |
| `tests/test_cli_resume_llm_wiring.py` (pre-existing suite) | llm-wiring.json precedence unaffected |

## 001a — infra-fault classification, fail-fast, retry/backoff (operator steering)

`src/ralphd/engine/faults.py` (pure classifier; it was named
fault_classifier.py in this wave and renamed in v0.5 -- the test module keeps
the old name) +
`src/ralphd/engine/loop.py` (startup watchdog, retry/backoff, budget
refund, status/event surfacing).

| Test node ID | Covers |
|---|---|
| `tests/test_fault_classifier.py::test_success_is_not_a_failure` | no-failure baseline |
| `tests/test_fault_classifier.py::test_success_with_traffic_is_not_a_failure` | traffic present, success |
| `tests/test_fault_classifier.py::test_enotfound_error_text_classifies_infra` | DNS ENOTFOUND → infra |
| `tests/test_fault_classifier.py::test_enotfound_classifies_infra_even_with_some_traffic` | infra signature wins even with partial traffic |
| `tests/test_fault_classifier.py::test_econnrefused_error_text_classifies_infra` | ECONNREFUSED → infra |
| `tests/test_fault_classifier.py::test_tls_handshake_failure_classifies_infra` | TLS handshake failure → infra |
| `tests/test_fault_classifier.py::test_gateway_5xx_before_any_tokens_classifies_infra` | 5xx before first token → infra |
| `tests/test_fault_classifier.py::test_startup_window_no_traffic_timeout_is_always_infra` | no-traffic startup timeout → infra |
| `tests/test_fault_classifier.py::test_unclassifiable_no_traffic_failure_defaults_to_infra` | unclassifiable no-traffic case defaults to infra |
| `tests/test_fault_classifier.py::test_work_exit_with_traffic_classifies_work` | traffic + nonzero exit → work |
| `tests/test_fault_classifier.py::test_timed_out_with_traffic_and_no_infra_text_classifies_work` | full-timeout with traffic and no infra text → work |
| `tests/test_fault_classifier.py::test_interrupted_with_traffic_and_no_infra_text_classifies_signal` | interrupted with traffic, no infra text → `work` when this report was written; retargeted to the third verdict `signal` by task 013 (#49), same corner of the ladder |
| `tests/test_infra_fault_retry.py::test_startup_watchdog_kills_hang_within_startup_window_not_full_timeout` | e2e: hung stub killed within startup window, not full iteration timeout |
| `tests/test_infra_fault_retry.py::test_infra_hang_retry_does_not_consume_budget_then_recovers` | e2e: infra retry doesn't burn budget; later-healthy stub reaches VERIFIED |
| `tests/test_infra_fault_retry.py::test_infra_retries_exhausted_ends_terminal_with_infra_reason` | e2e: exhausted retries end terminal with a `reason` naming the infra fault |
| `tests/test_status_infra_retry_note.py::test_ralphctl_status_shows_infra_retry_note_during_backoff` | status.json/`ralphctl status` shows the retrying-after-infra-fault note |
| `tests/test_no_progress_instant_failures.py` (pre-existing suite, unmodified) | instant-exit no-API-key fail-fast path remains green |

## B — grace review at budget exhaustion

`src/ralphd/engine/loop.py` (`LoopSupervisor`): reserved final-slot grace
review when all tasks are completed at budget exhaustion.

| Test node ID | Covers |
|---|---|
| `tests/test_e2e.py::test_grace_review_when_budget_exhausts_with_all_tasks_completed` | last task completes in the final budgeted iteration; satisfied stub review still runs → VERIFIED |
| `tests/test_e2e.py::test_grace_review_not_granted_twice_and_can_fail` | at most one grace review per approach; unsatisfied grace review with zero budget left ends the approach/job |
| `tests/test_e2e.py::test_budget_exhaustion_fails_job` | negative case: tasks not all completed at exhaustion → no grace review, fails as before |

## C — surface `reason` and human-readable summaries

`cmd_status` in `src/ralphd/cli/main.py`; hub run-detail template.

| Test node ID | Covers |
|---|---|
| `tests/test_cli_status_summaries.py::test_reason_present_renders_a_line` | reason line rendered when present |
| `tests/test_cli_status_summaries.py::test_reason_absent_renders_nothing` | no reason line when absent |
| `tests/test_cli_status_summaries.py::test_reason_long_text_wraps_across_multiple_lines` | long reason text wraps readably |
| `tests/test_cli_status_summaries.py::test_tasks_summary_all_completed` | `tasks:` summary, all completed |
| `tests/test_cli_status_summaries.py::test_tasks_summary_mixed_statuses` | `tasks:` summary, mixed per-status counts |
| `tests/test_cli_status_summaries.py::test_tasks_summary_zero_counts_omitted` | zero-count statuses omitted from summary |
| `tests/test_cli_status_summaries.py::test_tasks_summary_validation_failed_label` | validation-failed status labeled correctly |
| `tests/test_cli_status_summaries.py::test_tasks_summary_empty` | empty tasks list handled |
| `tests/test_cli_status_summaries.py::test_usage_summary_with_phase_breakdown` | `usage:` summary with planning/worker/review breakdown |
| `tests/test_cli_status_summaries.py::test_usage_summary_no_phase_breakdown` | usage summary with no breakdown data |
| `tests/test_cli_status_summaries.py::test_usage_summary_partial_phase_breakdown_only_shows_present_phases` | partial phase breakdown |
| `tests/test_cli_status_summaries.py::test_usage_summary_empty` | empty usage handled |
| `tests/test_browser_hub.py::test_run_detail_shows_reason_for_terminal_failed_run` | hub run-detail page shows the reason prominently for a terminal failed run (browser tier, playwright-cli) |

## D — `--network` follow-through

`src/ralphd/cli/main.py` (`cmd_config`, `cmd_start`, `cmd_doctor`);
`docs/architecture.md` host-network section.

| Test node ID | Covers |
|---|---|
| `tests/test_cli_config.py::test_set_network_persists_to_registry_config_yaml` | `ralphctl config set network <value>` persists to config.yaml |
| `tests/test_cli_config.py::test_start_uses_registry_default_network` | start falls back to registry-configured network when `--network` absent |
| `tests/test_cli_config.py::test_explicit_network_flag_overrides_registry_default` | explicit `--network` flag wins over registry default |
| `tests/test_cli_doctor_enriched.py::test_doctor_notes_host_network_api_bind_exposure_when_configured` | doctor notes the API-bind exposure when network=host |
| `tests/test_cli_doctor_enriched.py::test_doctor_no_host_network_note_when_not_configured` | note absent when network != host |
| `tests/test_cli_network.py::test_default_network_publishes_port` | baseline: default network still publishes the port |
| `tests/test_cli_network.py::test_network_host_skips_publish_and_sets_bind_env` | `--network host` behavior unchanged by this wave |
| `tests/test_cli_network.py::test_network_host_honors_api_bind` | `--api-bind` honored under host networking |
| `tests/test_cli_network.py::test_named_network_keeps_port_publish` | named (non-host) network keeps port publish |
| `tests/test_cli_network.py::test_resume_reuses_recorded_network` | resume reuses the recorded network |

## E — `ralphctl repair`

`src/ralphd/cli/main.py` (`cmd_repair`); `docs/cli.md` repair section.

| Test node ID | Covers |
|---|---|
| `tests/test_cli_repair.py::test_repair_unknown_run_exits_3` | unknown run-id → nonzero exit |
| `tests/test_cli_repair.py::test_repair_refuses_while_container_running` | refuses to touch a run whose container is running |
| `tests/test_cli_repair.py::test_repair_clean_run_reports_no_issues` | diagnosis: clean run reports no issues |
| `tests/test_cli_repair.py::test_repair_reports_corrupted_status_json` | diagnosis: corrupted status.json reported |
| `tests/test_cli_repair.py::test_repair_reports_bad_task_status_and_missing_fields` | diagnosis: tasks.json schema violations reported |
| `tests/test_cli_repair.py::test_repair_missing_host_json_reported` | diagnosis: missing host.json reported |
| `tests/test_cli_repair.py::test_repair_appends_audit_event_never_leaking_values` | audit event appended to events.jsonl, no secret values |
| `tests/test_cli_repair.py::test_repair_stdout_never_contains_running_container_command_output` | no leakage of container inspection output |
| `tests/test_cli_repair.py::test_repair_set_state_success` | `--set-state` success path |
| `tests/test_cli_repair.py::test_repair_set_state_invalid_value_rejected` | `--set-state` rejects invalid state value |
| `tests/test_cli_repair.py::test_repair_set_state_refuses_while_container_running` | `--set-state` refuses while container running |
| `tests/test_cli_repair.py::test_repair_set_state_appends_audit_event_with_old_and_new` | audit event records old/new state |
| `tests/test_cli_repair.py::test_repair_env_adds_new_key_to_fresh_wiring_file` | `--env KEY=VAL` adds a new key, mode 0600 |
| `tests/test_cli_repair.py::test_repair_env_updates_existing_key_in_place` | `--env KEY=VAL` updates an existing key in place |
| `tests/test_cli_repair.py::test_repair_env_resume_carries_updated_value` | subsequent resume's argv carries the updated value |
| `tests/test_cli_repair.py::test_repair_env_value_never_echoed_or_in_events` | value never printed or written to events.jsonl |
| `tests/test_cli_repair.py::test_repair_env_invalid_kv_rejected` | malformed `KEY=VAL` rejected |
| `tests/test_cli_repair.py::test_repair_env_refuses_while_container_running` | `--env` refuses while container running |

## F — roadmap note only (named env-wiring profiles)

No code; `docs/roadmap.md`'s "Later / explicitly deferred" list carries
the bullet. Verified by:

| Test node ID | Covers |
|---|---|
| `tests/test_docs_consistency.py::test_tutorial_exists_and_covers_required_steps_in_order` | docs structural consistency sweep (roadmap/tutorial cross-checks) still green after the docs-only edit |

## G — "toolchain in a sibling" as a prompt-level capability

Outside the selfdev-roadmap-4 PRD (operator request, added after that wave):
any job whose work needs a toolchain the engine image lacks must learn from
the prompts alone to run that work in a sibling container with the HOST
workspace bind-mounted. `src/ralphd/engine/loop.py`
(`_docker_siblings_note()`); `docs/architecture.md` "Toolchain in a sibling";
`docs/cli.md` `--allow-docker` section; `examples/skills/toolchain-sibling/`.

| Test node ID | Covers |
|---|---|
| `tests/test_toolchain_sibling_guidance.py::test_every_prompt_carries_the_sibling_toolchain_recipe` | every phase prompt of an `--allow-docker` job carries the full recipe (repo-committed `ci/Dockerfile`+`ci/run.sh`, `--rm --user 1000:1000`, HOST workspace mount, named cache volume, bridge network) |
| `tests/test_toolchain_sibling_guidance.py::test_no_sibling_toolchain_recipe_without_docker_access` | none of the guidance is spent when the capability is off (no docker socket granted) |
| `tests/test_toolchain_sibling_guidance.py::test_prompt_warns_against_run_id_locked_cache_volume` | the run-id-locked cache-volume anti-pattern is warned against and the sanctioned alternative stated |
| `tests/test_toolchain_sibling_guidance.py::test_example_skill_is_a_valid_mountable_skill` | shipped skill has frontmatter + executable `run.sh` and states the rules |
| `tests/test_toolchain_sibling_guidance.py::test_example_run_sh_cache_volume_is_shared_not_run_scoped` | the example wrapper's cache volume is shared across runs, unlabeled, never run-scoped |
| `tests/test_toolchain_sibling_guidance.py::test_docs_name_the_pattern_and_its_failure_modes` | `docs/architecture.md` names the pattern with its failure modes; `docs/cli.md` cross-references it |
| `tests/test_e2e.py::test_docker_siblings_guidance_in_prompt` (pre-existing) | host-path guidance + run label still rendered |
| `tests/test_e2e.py::test_no_docker_siblings_guidance_without_env` (pre-existing) | section absent without the socket |

Suite evidence for this change: `.venv/bin/python -m pytest -q -m "not docker"`
→ **385 passed, 4 deselected** (docker tier) with 4 browser-tier failures that
are a host-environment artifact only — `tests/test_browser_hub.py` writes
screenshots to `RALPHD_ARTIFACTS_DIR`, defaulting to the container path
`/run/ralphd/artifacts`, which a bare host cannot create; re-running
`RALPHD_ARTIFACTS_DIR=/tmp/... pytest -m browser -q` → **8 passed**.
`.venv/bin/ruff check .` → clean. Docker tier not run (no attempt made to
build `container/Dockerfile` here); nothing in this change touches it.

## Suite/tier execution evidence (this run)

- `.venv/bin/python -m pytest -q` → **379 passed, 8 skipped** in ~6m10s.
- `.venv/bin/python -m pytest -m browser -q` → **8 passed** (playwright-cli
  present on PATH at `/usr/bin/playwright-cli`; browser tier actually
  executed, not skipped).
- `.venv/bin/python -m pytest -m docker -q` → **4 skipped**. Genuine
  environment limitation, not a code/config gap: `docker info` in this
  sandbox reports `dial unix /var/run/docker.sock: connect: no such file
  or directory` — no docker daemon/socket is available here, so the
  docker-sibling e2e tier (`tests/test_docker_sibling_e2e.py`, 4 tests)
  skips cleanly by design (`pytest.ini` marker: "skip cleanly if the
  docker socket is absent"). This tier has passed green in prior waves
  when a docker daemon was reachable; nothing in this wave's changes
  touches that skip condition.
- `.venv/bin/ruff check .` → all checks passed.
