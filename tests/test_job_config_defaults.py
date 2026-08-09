"""Task 010: the product default for `on_complete` is `exit`; `idle` is an
explicit debugging opt-in. Covers the dataclass default directly (the
`ralphctl start` precedence chain -- CLI flag > template > registry >
hardcoded fallback -- is covered black-box in tests/test_cli_config.py).
"""

from __future__ import annotations

from ralphd.engine.config import JobConfig


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
