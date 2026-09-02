"""Task 017 (D.3, #117 side finding): `ralphctl start` gains
`--peer-plan-review`/`--peer-verdict-gate`, and both keys surface in
`JobConfig.effective()['flags']` so `GET /config` shows them.

Reuses tests/test_cli_docker.py's `Ctl` runner (real `ralphctl` subprocess,
stub recording docker, temp registry) the same way tests/test_cli_config.py
does, so this drives the CLI flag through `start` -> job.yaml -> a real
loaded `JobConfig` object without importing any CLI internals for the
launch half.
"""

from __future__ import annotations

import yaml
from test_cli_docker import Ctl

import pytest

from ralphd.engine.config import JobConfig


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _job_yaml_path(ctl: Ctl, run_id: str):
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, f"expected one job.yaml for {run_id}, found {matches}"
    return matches[0]


def test_start_accepts_both_peer_gate_flags(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "peer-gates-on",
                  "--peer-plan-review", "--peer-verdict-gate")
    assert res.returncode == 0, res.stderr
    job = yaml.safe_load(_job_yaml_path(ctl, "peer-gates-on").read_text())
    assert job["peer_plan_review"] is True
    assert job["peer_verdict_gate"] is True


def test_the_cli_flag_drives_through_to_the_config_object(ctl):
    """Not just job.yaml -- the flag reaches a real `JobConfig.load()`
    read of the file `start` wrote, the same object the engine itself
    loads at boot."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "peer-gates-load",
                  "--peer-plan-review", "--peer-verdict-gate")
    assert res.returncode == 0, res.stderr
    path = _job_yaml_path(ctl, "peer-gates-load")
    cfg = JobConfig.load(path)
    assert cfg.peer_plan_review is True
    assert cfg.peer_verdict_gate is True


def test_omitting_the_flags_keeps_both_gates_off(ctl):
    """Unset -- like `--vigilant`/`--reflect` -- stays out of job.yaml
    entirely, so the engine's own `JobConfig` default (False) applies."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "peer-gates-off")
    assert res.returncode == 0, res.stderr
    job = yaml.safe_load(_job_yaml_path(ctl, "peer-gates-off").read_text())
    assert "peer_plan_review" not in job
    assert "peer_verdict_gate" not in job
    cfg = JobConfig.load(_job_yaml_path(ctl, "peer-gates-off"))
    assert cfg.peer_plan_review is False
    assert cfg.peer_verdict_gate is False


def test_both_peer_gate_keys_appear_in_effective_flags():
    flags = JobConfig(run_id="t").effective()["flags"]
    assert "peerPlanReview" in flags and flags["peerPlanReview"] is False
    assert "peerVerdictGate" in flags and flags["peerVerdictGate"] is False


def test_both_peer_gate_keys_reflect_true_when_the_gates_are_on():
    flags = JobConfig(run_id="t", peer_plan_review=True,
                       peer_verdict_gate=True).effective()["flags"]
    assert flags["peerPlanReview"] is True
    assert flags["peerVerdictGate"] is True
