"""The `--notifier-url` flag (task 010, PRD requirement B.2, #197).

`ralphctl start` used to have no way to point the engine's notify hat tool
at an endpoint except `job.yaml`'s own `notifier_url:` key or the
`RALPHD_NOTIFIER_URL` env var -- so changing it on an already-running job
meant `start` -> `docker stop` -> hand-edit `job.yaml` -> `resume`. This adds
`--notifier-url URL`, written into the run's own `job.yaml` exactly like
`--price-strategy` (tests/test_price_strategy.py) is, so the flag and the
job.yaml key are the same underlying field -- never two competing knobs.

Precedence (also stated in the task 010 commit message):

1. `RALPHD_NOTIFIER_URL` -- read by `JobConfig.load()` inside the container
   at engine start -- wins unconditionally, over both of the below. It is
   a single-run override that never touches job.yaml.
2. `job.yaml`'s own `notifier_url` key -- written by `--notifier-url` at
   `start` time (the common path), by a `--template`'s own `notifier_url:`
   key when the flag is absent, or by hand -- applies when no env override
   is present.
3. Unset: `JobConfig.notifier_url` stays `None` (unconfigured); the notify
   hat tool answers "no notifier URL configured" rather than raising
   (tests/test_notify_hat_tool.py).

These tests exercise steps 1-2 directly against `JobConfig` (a fixture
config dir, never this run's own `/config/job.yaml` -- B.5) and against
`ralphctl start`'s in-process CLI surface (`tests/test_cli_docker.py`'s
`ctl` fixture, stub docker, no real container) for the flag ->
job.yaml -> `JobConfig.load()` path end to end.
"""

from __future__ import annotations

import json

import yaml

from test_cli_docker import Ctl, ctl, unix_sock  # noqa: F401  (fixtures)

from ralphd.engine.config import JobConfig
from tests.conftest import NOTIFIER_ENV_VAR

__all__ = ["ctl", "unix_sock"]

URL = "http://notifier.example/notify"


def _write_job(path, **fields):
    path.write_text("".join(f"{k}: {json.dumps(v)}\n" for k, v in fields.items()))
    return path


# --------------------------------------------------------------------------
# JobConfig: job.yaml key + env override, against a fixture config dir
# --------------------------------------------------------------------------

def test_the_shipped_default_is_unconfigured():
    assert JobConfig().notifier_url is None


def test_job_yaml_key_reaches_the_config(tmp_path):
    cfg = JobConfig.load(_write_job(tmp_path / "job.yaml", notifier_url=URL))
    assert cfg.notifier_url == URL


def test_a_job_yaml_without_the_key_stays_unconfigured(tmp_path):
    cfg = JobConfig.load(_write_job(tmp_path / "job.yaml", iterations=5))
    assert cfg.notifier_url is None


def test_env_var_reaches_the_config_without_a_job_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv(NOTIFIER_ENV_VAR, URL)
    assert JobConfig.load(tmp_path / "missing.yaml").notifier_url == URL


def test_env_var_beats_the_job_yaml_key_in_both_directions(tmp_path, monkeypatch):
    other = "http://other.example/notify"
    path = _write_job(tmp_path / "job.yaml", notifier_url=other)
    monkeypatch.setenv(NOTIFIER_ENV_VAR, URL)
    assert JobConfig.load(path).notifier_url == URL
    monkeypatch.delenv(NOTIFIER_ENV_VAR)
    assert JobConfig.load(_write_job(
        tmp_path / "job2.yaml", notifier_url=other)).notifier_url == other


# --------------------------------------------------------------------------
# `ralphctl start`: job.yaml persistence, named against the flag
# --------------------------------------------------------------------------

def _job_yaml(ctl: Ctl, run_id: str) -> dict:
    return yaml.safe_load((ctl.registry / "configs" / run_id / "job.yaml").read_text())


def test_start_notifier_url_flag_persists_into_job_yaml_and_reaches_config(
        ctl: Ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--notifier-url", URL, "--run-id", "tst-nu-flag")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-nu-flag")["notifier_url"] == URL
    cfg = JobConfig.load(ctl.registry / "configs" / "tst-nu-flag" / "job.yaml")
    assert cfg.notifier_url == URL


def test_start_without_the_flag_writes_no_notifier_url_key(ctl: Ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-nu-absent")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "tst-nu-absent")
    assert "notifier_url" not in job
    cfg = JobConfig.load(ctl.registry / "configs" / "tst-nu-absent" / "job.yaml")
    assert cfg.notifier_url is None


def _template(ctl: Ctl, name: str, **job_keys) -> None:
    tdir = ctl.registry / "templates" / name
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text(yaml.safe_dump(job_keys))
    (tdir / "prd.md").write_text("# T\n\nDo it.\n")


def test_a_templates_notifier_url_reaches_the_config(ctl: Ctl):
    """The claim `docs/cli.md` makes about the template level, checked:
    `notifier_url` is one of `_TEMPLATE_SCALAR_FIELDS`, so a template's own
    key lands in the started run's `job.yaml` and reaches `JobConfig` when
    no `--notifier-url` flag is given (v0.20.2 task 010 cure -- before it,
    the key was in no template field set and a template's value was read by
    nothing)."""
    _template(ctl, "notified", notifier_url=URL)
    res = ctl.run("start", "--template", "notified", "--llm", "none",
                  "--run-id", "tst-nu-template")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-nu-template")["notifier_url"] == URL
    cfg = JobConfig.load(ctl.registry / "configs" / "tst-nu-template" / "job.yaml")
    assert cfg.notifier_url == URL


def test_the_flag_beats_a_templates_notifier_url(ctl: Ctl):
    other = "http://template.example/notify"
    _template(ctl, "notified2", notifier_url=other)
    res = ctl.run("start", "--template", "notified2", "--llm", "none",
                  "--notifier-url", URL, "--run-id", "tst-nu-tmpl-flag")
    assert res.returncode == 0, res.stderr
    assert _job_yaml(ctl, "tst-nu-tmpl-flag")["notifier_url"] == URL
