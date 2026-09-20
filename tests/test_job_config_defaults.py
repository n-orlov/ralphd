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
import errno
import os
from pathlib import Path

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


@_privsep_root_skip
def test_job_config_load_when_the_engine_uid_is_not_the_config_dir_owner(
        tmp_path, monkeypatch):
    """Task 023 (#180, requirement E.1), the uid mismatch itself.

    The situation the requirement names cannot be built literally in the fast
    tier -- an unprivileged test process cannot `chown` a directory to another
    uid, and cannot change its own effective uid either (a real host-uid
    mount is covered docker-tier by task 024/E.2). So the *difference* is
    established here as the two numbers the kernel would compare, with the
    comparison itself modelled: the config directory is owned by whoever runs
    this test and chmod'ed 0700 exactly as `ralphctl start` leaves `/config`,
    the engine's effective uid is declared to be a different number, and
    `Path.exists`/`Path.read_text` under that directory answer the way the
    kernel's DAC check answers for that pair -- denied for any effective uid
    that is not the owner (a 0700 directory grants nothing to group or other),
    exempt for uid 0. Two loads run against that: with no elevation the
    mismatch alone hides `job.yaml` entirely (`Path.exists()` swallows EACCES,
    so the job silently gets defaults -- the bug E.1 is about), and inside
    `privsep.root_for_write()` the very same mismatch reads the file. `/config`
    itself is asserted unchanged by the read: same owning uid, still 0700.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    os.chmod(config_dir, 0o700)
    job_yaml = config_dir / "job.yaml"
    job_yaml.write_text("iterations: 7\n")
    os.chmod(job_yaml, 0o600)

    owner_uid = config_dir.stat().st_uid
    # The whole point of E.1: the host uid that ran `ralphctl start` is only
    # ever the agent's 1000 by accident. Any other number reproduces it.
    engine_euid = owner_uid + 4321
    assert engine_euid != owner_uid, (
        "the premise of this test is an engine effective uid that is NOT the "
        "uid owning the config directory")
    assert config_dir.stat().st_mode & 0o777 == 0o700

    effective_uid = [engine_euid]
    real_exists, real_read_text = Path.exists, Path.read_text

    def _denied_at(p: Path) -> bool:
        """The kernel's own owner/other check, for this uid pair."""
        if effective_uid[0] == 0:  # root is exempt from the mode check
            return False
        try:
            targets = [p, *p.parents]
        except Exception:  # pragma: no cover - defensive
            return False
        for target in targets:
            if config_dir != target and config_dir not in target.parents:
                continue
            st = os.stat(target)
            if st.st_uid == effective_uid[0]:
                if not st.st_mode & 0o400:
                    return True
            elif not st.st_mode & 0o004:
                return True
        return False

    def _fake_exists(self, *a, **kw):
        if _denied_at(self):
            return False  # exactly what pathlib does with an EACCES stat
        return real_exists(self, *a, **kw)

    def _fake_read_text(self, *a, **kw):
        if _denied_at(self):
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "exists", _fake_exists)
    monkeypatch.setattr(Path, "read_text", _fake_read_text)
    monkeypatch.setattr(privsep, "separated", lambda: True)

    # 1. No elevation: the uid mismatch alone loses the job's own config.
    monkeypatch.setattr(privsep, "root_for_write",
                        lambda: contextlib.nullcontext(False))
    unelevated = JobConfig.load(job_yaml)
    assert unelevated.iterations == 25, (
        "without the elevation the mismatched uid never sees job.yaml, so the "
        "run silently gets the dataclass default of 25 iterations")

    # 2. The production path: read inside the momentary raise to uid 0.
    entered: list[str] = []

    @contextlib.contextmanager
    def _stand_in_root_window():
        entered.append("raised")
        was, effective_uid[0] = effective_uid[0], 0
        try:
            yield True
        finally:
            effective_uid[0] = was

    monkeypatch.setattr(privsep, "root_for_write", _stand_in_root_window)
    cfg = JobConfig.load(job_yaml)

    assert cfg.iterations == 7
    assert entered, "the config read never entered the elevation window"
    assert effective_uid[0] == engine_euid, (
        "the engine's effective uid must be back to the agent's before "
        "JobConfig.load() returns")
    assert config_dir.stat().st_uid == owner_uid
    assert config_dir.stat().st_mode & 0o777 == 0o700, (
        "/config stays closed to the host at large -- the read widened no "
        "mode bit on the mount")
    assert job_yaml.stat().st_mode & 0o777 == 0o600


# -- task 014 (PRD requirement A.1, #224): the pause-before-archive gate ---

def test_operator_input_needed_pause_defaults_true():
    """ON by default -- unlike `peer_plan_review`/`peer_verdict_gate`, this
    gate is a disable, not an enable (`ralphctl start
    --no-operator-input-needed-pause`); the black-box `start`/template
    precedence chain is covered in `tests/test_cli_templates.py`, and the
    behavioural edge itself in `tests/test_operator_input_needed_pause.py`.
    """
    assert JobConfig().operator_input_needed_pause is True


def test_job_config_load_respects_explicit_operator_input_needed_pause_false(
        tmp_path):
    p = tmp_path / "job.yaml"
    p.write_text("operator_input_needed_pause: false\n")
    cfg = JobConfig.load(p)
    assert cfg.operator_input_needed_pause is False


def test_job_config_load_with_no_job_yaml_defaults_operator_input_needed_pause_true(
        tmp_path):
    cfg = JobConfig.load(tmp_path / "does-not-exist.yaml")
    assert cfg.operator_input_needed_pause is True


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
