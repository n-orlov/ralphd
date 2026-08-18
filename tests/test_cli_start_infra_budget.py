"""Black-box tests for `ralphctl start --infra-outage-budget` (task 007, #5).

Reuses tests/test_cli_docker.py's `Ctl` runner (real ralphctl subprocess,
recording stub docker, temp registry) so nothing CLI-internal is imported.
The flag must land in the run's job.yaml as `infra_outage_budget_s` and be
picked up by the engine's JobConfig -> `GET /config` budgets; omitting it
must leave the key out entirely so the engine default (defined once, in
engine/config.py) applies.
"""

from __future__ import annotations

import pytest
import yaml
from test_cli_docker import Ctl

from ralphd.engine.config import DEFAULT_INFRA_OUTAGE_BUDGET_S, JobConfig


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _job_yaml_path(ctl: Ctl, run_id: str):
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, f"expected one job.yaml for {run_id}, found {matches}"
    return matches[0]


def _start(ctl: Ctl, run_id: str, *extra: str):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", run_id, *extra)
    assert res.returncode == 0, res.stderr
    return _job_yaml_path(ctl, run_id)


def test_flag_writes_infra_outage_budget_s_into_job_yaml(ctl):
    path = _start(ctl, "tst-budget", "--infra-outage-budget", "900")
    assert yaml.safe_load(path.read_text())["infra_outage_budget_s"] == 900
    # ... and the engine reads it back into the GET /config budgets block.
    cfg = JobConfig.load(path)
    assert cfg.infra_outage_budget_s == 900
    assert cfg.effective()["budgets"]["infraOutageBudgetS"] == 900


def test_omitting_the_flag_leaves_the_engine_default(ctl):
    path = _start(ctl, "tst-budget-default")
    assert "infra_outage_budget_s" not in yaml.safe_load(path.read_text())
    cfg = JobConfig.load(path)
    assert cfg.infra_outage_budget_s == DEFAULT_INFRA_OUTAGE_BUDGET_S
    assert (cfg.effective()["budgets"]["infraOutageBudgetS"]
            == DEFAULT_INFRA_OUTAGE_BUDGET_S)


def test_flag_appears_in_start_help(ctl):
    res = ctl.run("start", "--help")
    assert res.returncode == 0, res.stderr
    assert "--infra-outage-budget" in res.stdout
