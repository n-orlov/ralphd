"""Black-box tests for `ralphctl start --model-for` (task 008, #60 pillar 4,
requirement A: "the table must be reachable from ralphctl start, and today's
is not"). Reuses tests/test_cli_docker.py's `Ctl` runner (real ralphctl
subprocess, stub recording docker, temp registry) so it inspects job.yaml
without importing any CLI internals, then hands that exact job.yaml to
`JobConfig.load` to prove the engine resolves it -- the same discipline
tests/test_cli_start_infra_budget.py and tests/test_cli_config.py already
use for the other `start` knobs.

Both tests build job.yaml purely through supported paths (`--model-for`,
a `--template`'s job.yaml `models:` key, `<registry>/config.yaml`'s own
`models:` key) -- never a hand-edited job.yaml.
"""

from __future__ import annotations

import yaml
from test_cli_docker import Ctl

import pytest

from ralphd.engine.config import JobConfig


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _job_yaml_path(ctl: Ctl, run_id: str):
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, f"expected one job.yaml for {run_id}, found {matches}"
    return matches[0]


def _job_yaml(ctl: Ctl, run_id: str) -> dict:
    return yaml.safe_load(_job_yaml_path(ctl, run_id).read_text())


# The PRD's worked configuration (requirement A's acceptance test): an Opus
# planner, a Sonnet worker, and a gpt-5.6-terra cross-family inspector.
PLANNER = "amazon-bedrock/eu.anthropic.claude-opus-5"
WORKER = "amazon-bedrock/eu.anthropic.claude-sonnet-5"
VERIFIER = "aigw-openai/openai.gpt-5.6-terra"


def test_prd_worked_configuration_via_model_for_flags_resolves_all_three_hats(ctl):
    """The PRD's own acceptance example, built with nothing but repeatable
    `--model-for` flags -- no hand-edited job.yaml anywhere."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "hat-worked",
                  "--model-for", f"planning={PLANNER}",
                  "--model-for", f"worker={WORKER}",
                  "--model-for", f"verify={VERIFIER}")
    assert res.returncode == 0, res.stderr
    path = _job_yaml_path(ctl, "hat-worked")
    job = yaml.safe_load(path.read_text())
    assert job["models"] == {"planning": PLANNER, "worker": WORKER, "verify": VERIFIER}

    # ... and the engine resolves all three hats to those exact refs.
    cfg = JobConfig.load(path)
    assert cfg.model_for("planning") == PLANNER
    assert cfg.model_for("worker") == WORKER
    assert cfg.model_for("verify") == VERIFIER


def test_precedence_explicit_flag_beats_template_beats_registry_config(ctl):
    """Pins the order the flag's own help text and docs/cli.md document:
    explicit `--model-for` > `--template`'s `models:` key > `<registry>/
    config.yaml`'s own `models:` key -- merged key by key, not
    replace-whole, so a hat only the registry names still survives an
    explicit flag that names a different hat."""
    (ctl.registry / "config.yaml").write_text(
        "models:\n"
        "  planning: prov/registry-planner\n"
        "  review: prov/registry-reviewer\n")
    tdir = ctl.registry / "templates" / "hats"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text(
        "models:\n"
        "  planning: prov/template-planner\n"
        "  worker: prov/template-worker\n")

    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "hat-precedence", "--template", "hats",
                  "--model-for", "planning=prov/flag-planner")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "hat-precedence")
    assert job["models"] == {
        "planning": "prov/flag-planner",     # explicit flag wins over both
        "worker": "prov/template-worker",    # template survives (flag never named it)
        "review": "prov/registry-reviewer",  # registry survives (template/flag never named it)
    }

    cfg = JobConfig.load(_job_yaml_path(ctl, "hat-precedence"))
    assert cfg.model_for("planning") == "prov/flag-planner"
    assert cfg.model_for("worker") == "prov/template-worker"
    assert cfg.model_for("review") == "prov/registry-reviewer"


def test_malformed_model_for_value_exits_2(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "hat-malformed", "--model-for", "not-a-kv-pair")
    assert res.returncode == 2, res.stderr
    assert "--model-for" in res.stderr
    assert "HAT=REF" in res.stderr


def test_omitting_the_flag_leaves_the_models_key_out_of_job_yaml(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "hat-none")
    assert res.returncode == 0, res.stderr
    assert "models" not in _job_yaml(ctl, "hat-none")


def test_flag_appears_in_start_help(ctl):
    res = ctl.run("start", "--help")
    assert res.returncode == 0, res.stderr
    assert "--model-for HAT=REF" in res.stdout
