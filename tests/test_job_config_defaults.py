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

import contextlib
import os

import pytest

from ralphd.engine import privsep
from ralphd.engine.config import (
    DEFAULT_INFRA_OUTAGE_BUDGET_S,
    DEFAULT_INFRA_RETRY_BACKOFF_MAX_S,
    DEFAULT_INFRA_RETRY_BACKOFF_S,
    JobConfig,
)

# A root-only file cannot be modelled at all when the tests themselves run
# as root: mode 0o000 would still be readable and the "unreadable at my own
# uid" premise the boundary test below rests on would be vacuous -- the same
# guard `tests/test_run_secrets.py` uses for the same reason.
_privsep_root_skip = pytest.mark.skipif(
    os.geteuid() == 0, reason="models a file root can read and this uid cannot")


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


@_privsep_root_skip
def test_job_config_load_reads_job_yaml_the_agent_uid_cannot_open(
        tmp_path, monkeypatch):
    """Task 023 (#180, requirement E.1): `/config` is a host directory
    `ralphctl start` chmod's 0700 and leaves owned by whichever host uid ran
    it -- under the uid boundary (`privsep.separate_engine_identity()`) the
    engine's *effective* uid is the agent's, so a host uid other than the
    agent's leaves the mount unreadable at that effective uid alone.
    Modelled the same way `test_run_secrets.py` models a root-only MAC-key
    file: `job.yaml` is chmod 0o000 (genuinely unreadable at this uid,
    skipped when the suite itself runs as root), and the boundary is put in
    force with the same stand-in for `privsep.root_for_write()` -- it
    records that the elevation window was entered and, for its duration,
    makes the file readable again, modelling root's exemption from the mode
    check rather than reimplementing anything the production path does.
    `JobConfig.load()` must still return the file's contents, and the file
    must be exactly as unreadable again once the read returns.
    """
    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text("iterations: 7\n")
    os.chmod(job_yaml, 0o000)
    with pytest.raises(OSError):
        job_yaml.read_text()

    entered: list[str] = []

    @contextlib.contextmanager
    def _stand_in_root_window():
        entered.append("raised")
        os.chmod(job_yaml, 0o600)
        try:
            yield True
        finally:
            os.chmod(job_yaml, 0o000)

    monkeypatch.setattr(privsep, "separated", lambda: True)
    monkeypatch.setattr(privsep, "root_for_write", _stand_in_root_window)

    cfg = JobConfig.load(job_yaml)

    assert cfg.iterations == 7
    assert entered, "the config read never entered the elevation window"
    assert job_yaml.stat().st_mode & 0o777 == 0o000, (
        "the elevation window must close again -- job.yaml is left exactly "
        "as unreadable at the agent uid as it was before the read")


def test_job_config_load_with_no_boundary_reads_normally(tmp_path, monkeypatch):
    """No boundary to raise into (the test suite's own unprivileged engine,
    or `docker run --user 1000`) -- `root_for_write()` is a documented no-op
    there, so an agent-readable `job.yaml` is read exactly as before, with no
    elevation attempted at all."""
    monkeypatch.setattr(privsep, "separated", lambda: False)
    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text("iterations: 9\n")
    cfg = JobConfig.load(job_yaml)
    assert cfg.iterations == 9
