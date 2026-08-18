"""Task 010: the product default for `on_complete` is `exit`; `idle` is an
explicit debugging opt-in. Covers the dataclass default directly (the
`ralphctl start` precedence chain -- CLI flag > template > registry >
hardcoded fallback -- is covered black-box in tests/test_cli_config.py).

Task 006 (#5) extends this module with the infra-fault resilience knobs:
the fast escalating backoff schedule, its per-wait cap, the wall-clock
outage budget, and the fact that `infra_retry_max` is now an opt-in
attempt cap (`None` = honoured only when set explicitly).
"""

from __future__ import annotations

from ralphd.engine.config import (
    DEFAULT_INFRA_OUTAGE_BUDGET_S,
    DEFAULT_INFRA_RETRY_BACKOFF_MAX_S,
    DEFAULT_INFRA_RETRY_BACKOFF_S,
    JobConfig,
)


def test_job_config_default_on_complete_is_exit():
    cfg = JobConfig()
    assert cfg.on_complete == "exit"


def test_job_config_load_with_no_job_yaml_defaults_to_exit(tmp_path):
    # No job.yaml at all at this path -> JobConfig.load() falls through to
    # the dataclass default.
    cfg = JobConfig.load(tmp_path / "does-not-exist.yaml")
    assert cfg.on_complete == "exit"


def test_job_config_load_respects_explicit_idle(tmp_path):
    p = tmp_path / "job.yaml"
    p.write_text("on_complete: idle\n")
    cfg = JobConfig.load(p)
    assert cfg.on_complete == "idle"


# -- task 006 (#5): infra-fault backoff / outage-budget knobs ---------------

def test_infra_retry_defaults_are_fast_backoff_with_outage_budget():
    cfg = JobConfig()
    assert cfg.infra_retry_backoff_s == [2.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0]
    assert cfg.infra_retry_backoff_s == DEFAULT_INFRA_RETRY_BACKOFF_S
    assert cfg.infra_retry_backoff_max_s == 300.0 == DEFAULT_INFRA_RETRY_BACKOFF_MAX_S
    # 4 hours of outage tolerance by default.
    assert cfg.infra_outage_budget_s == 4 * 3600.0 == DEFAULT_INFRA_OUTAGE_BUDGET_S


def test_infra_retry_max_defaults_to_no_explicit_cap():
    """`infra_retry_max` is honoured only when set explicitly; the default
    sentinel means 'no attempt cap, retry within the outage budget'."""
    assert JobConfig().infra_retry_max is None


def test_infra_retry_backoff_schedule_default_is_not_shared_between_configs():
    a, b = JobConfig(), JobConfig()
    a.infra_retry_backoff_s.append(999.0)
    assert b.infra_retry_backoff_s == DEFAULT_INFRA_RETRY_BACKOFF_S
    assert DEFAULT_INFRA_RETRY_BACKOFF_S[-1] == 300.0


def test_infra_knobs_from_job_yaml(tmp_path):
    p = tmp_path / "job.yaml"
    p.write_text("infra_retry_max: 2\n"
                 "infra_retry_backoff_max_s: 42\n"
                 "infra_outage_budget_s: 900\n")
    cfg = JobConfig.load(p)
    assert cfg.infra_retry_max == 2
    assert cfg.infra_retry_backoff_max_s == 42
    assert cfg.infra_outage_budget_s == 900


def test_infra_knob_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("RALPHD_INFRA_RETRY_BACKOFF_S", "0.1,0.2")
    monkeypatch.setenv("RALPHD_INFRA_RETRY_BACKOFF_MAX_S", "7.5")
    monkeypatch.setenv("RALPHD_INFRA_OUTAGE_BUDGET_S", "120")
    monkeypatch.setenv("RALPHD_INFRA_RETRY_MAX", "5")
    cfg = JobConfig.load(tmp_path / "does-not-exist.yaml")
    assert cfg.infra_retry_backoff_s == [0.1, 0.2]
    assert cfg.infra_retry_backoff_max_s == 7.5
    assert cfg.infra_outage_budget_s == 120.0
    assert cfg.infra_retry_max == 5


def test_infra_knobs_in_effective_budgets():
    budgets = JobConfig().effective()["budgets"]
    assert budgets["infraRetryBackoffS"] == DEFAULT_INFRA_RETRY_BACKOFF_S
    assert budgets["infraRetryBackoffMaxS"] == DEFAULT_INFRA_RETRY_BACKOFF_MAX_S
    assert budgets["infraOutageBudgetS"] == DEFAULT_INFRA_OUTAGE_BUDGET_S
    assert budgets["infraRetryMax"] is None
    assert JobConfig(infra_retry_max=2).effective()["budgets"]["infraRetryMax"] == 2
