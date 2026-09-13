"""`ralphctl doctor` surfaces the notifier-URL state (task 011, PRD
requirement B.3, #197).

The `notify` hat tool is wired for every run (`WORKER_WIRED_TOOLS`/
`STEER_WIRED_TOOLS` in `hat_tools.py` both carry it unconditionally), so a
run with no `notifier_url` configured is one whose prompts can still reach
the tool while every call is a guaranteed no-op. `LoopSupervisor` warns
about that once, at engine startup, into the run's own `events.jsonl`
(`tests/test_notifier_startup_warning.py`); this covers the OTHER half of
B.3 -- the same fact, mirrored into `ralphctl doctor`'s registry-wide
report, per run recorded non-terminal, exactly like the existing
`runImageStaleness`/`IMAGE_STALE_RUNS_HEADING` report beside it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from ralphd.cli import main as cli
from tests.conftest import call_ralphctl_in_process

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"


class Ctl:
    def __init__(self, tmp: Path, capsys):
        self.tmp = tmp
        self.capsys = capsys
        self.registry = tmp / "registry"
        (self.registry / "runs").mkdir(parents=True)
        self.log = tmp / "docker-argv.jsonl"

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {k: v for k, v in os.environ.items()
                    if k not in ("RALPHD_IMAGE", "RALPHD_NOTIFIER_URL")}
        full_env.update({
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            **(env or {}),
        })
        return call_ralphctl_in_process(list(argv), env=full_env, capsys=self.capsys)

    def doctor(self, *argv: str, env: dict | None = None) -> dict:
        res = self.run("--json", "doctor", *argv, env=env)
        assert res.stdout, res.stderr
        return json.loads(res.stdout)

    def seed_run(self, run_id: str, state: str = "running",
                notifier_url: str | None = None,
                extra_env: list[str] | None = None) -> None:
        """A run dir as `start` leaves it (`status.json`), plus, when
        `notifier_url` is given, the same `job.yaml` key `--notifier-url`
        writes under the run's own CONFIG dir (task 010) -- never the run
        dir, mirroring `config_root()`. `extra_env` seeds the OTHER
        supported path: the resolved `name=value` pairs `start` persists in
        that same config dir's `env-wiring.json` for `--env`/`--forward-env`
        (`_write_extra_env_wiring`), which are what actually reach a job
        container's engine process as its own environment."""
        d = self.registry / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text(json.dumps({"state": state,
                                                    "schemaVersion": 1}))
        cfg_dir = self.registry / "configs" / run_id
        cfg_dir.mkdir(parents=True, exist_ok=True)
        job_yaml = {"run_id": run_id}
        if notifier_url is not None:
            job_yaml["notifier_url"] = notifier_url
        # `yaml.safe_dump`, not a hand-joined `f"{k}: {v}"` string: the
        # latter renders an empty string the same as an absent value
        # (`notifier_url: ` parses back as `None`, not `""`) which would
        # silently collapse the cure-01-02 `notifier_url: ""` fixture case
        # into the already-covered omitted/None one.
        (cfg_dir / "job.yaml").write_text(yaml.safe_dump(job_yaml))
        if extra_env is not None:
            cli._write_extra_env_wiring(cfg_dir, list(extra_env))


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def test_an_unconfigured_non_terminal_run_is_reported_by_doctor(ctl):
    ctl.seed_run("bare-run")
    doc = ctl.doctor()
    assert doc["unconfiguredNotifierRuns"] == ["bare-run"]
    res = ctl.run("doctor")
    assert cli.NOTIFIER_UNCONFIGURED_RUNS_HEADING in res.stdout
    assert "bare-run" in res.stdout
    # report-only, exactly like the stray/dangling/stale-image reports
    assert doc["ok"] == all(doc["checks"].values())


def test_a_configured_non_terminal_run_is_quiet(ctl):
    ctl.seed_run("wired-run", notifier_url="http://notifier.example/notify")
    doc = ctl.doctor()
    assert doc["unconfiguredNotifierRuns"] == []
    res = ctl.run("doctor")
    assert cli.NOTIFIER_UNCONFIGURED_RUNS_HEADING not in res.stdout


def test_a_job_yaml_empty_string_notifier_url_is_reported_unconfigured(ctl):
    """Cure of B.3 DoD 9: `notifier_url: ""` (a real, supported YAML
    representation of "no endpoint", distinct from the key being absent)
    must be just as visibly unconfigured in `doctor` as an omitted or
    `None` value -- never silently read as a configured, non-empty URL.
    """
    ctl.seed_run("blank-url-run", notifier_url="")
    doc = ctl.doctor()
    assert doc["unconfiguredNotifierRuns"] == ["blank-url-run"]
    res = ctl.run("doctor")
    assert cli.NOTIFIER_UNCONFIGURED_RUNS_HEADING in res.stdout
    assert "blank-url-run" in res.stdout


def test_the_doctor_shells_own_notifier_url_does_not_configure_a_run(ctl):
    """The state reported is each RUN's, not this process's (task 011).

    A job container's engine only ever sees `RALPHD_NOTIFIER_URL` if
    `start` recorded it in that run's own wiring, so an operator whose
    shell exports the variable while typing `ralphctl doctor` has
    configured nothing for the bare run in front of them -- reporting it
    as configured would hide exactly the no-op `notify` calls B.3 exists
    to surface.
    """
    ctl.seed_run("bare-run")
    doc = ctl.doctor(env={"RALPHD_NOTIFIER_URL": "http://ambient.example/notify"})
    assert doc["unconfiguredNotifierRuns"] == ["bare-run"]


def test_a_run_whose_persisted_env_wiring_carries_the_url_is_quiet(ctl):
    """The env-variable configuration path, read per run: `--env
    RALPHD_NOTIFIER_URL=...` (or `--forward-env`) lands in that run's own
    `env-wiring.json`, and that IS the engine's environment -- with or
    without the variable in the shell running `doctor`."""
    ctl.seed_run("env-wired",
                 extra_env=["RALPHD_NOTIFIER_URL=http://wired.example/notify"])
    assert ctl.doctor()["unconfiguredNotifierRuns"] == []
    assert ctl.doctor(env={"RALPHD_NOTIFIER_URL": "http://ambient.example/notify"}
                      )["unconfiguredNotifierRuns"] == []
    # an empty value configures nothing (`JobConfig.load`'s own truthiness)
    ctl.seed_run("env-blank", extra_env=["RALPHD_NOTIFIER_URL="])
    assert ctl.doctor()["unconfiguredNotifierRuns"] == ["env-blank"]


def test_terminal_runs_are_left_out_of_the_report_either_way(ctl):
    for state in ("succeeded", "failed", "aborted"):
        ctl.seed_run(f"done-{state}", state=state)
    doc = ctl.doctor()
    assert doc["unconfiguredNotifierRuns"] == []
