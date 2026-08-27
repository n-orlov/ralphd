"""Black-box tests for `ralphctl start --model-for-tier` (task 004, #68:
"make the tier matrix reachable from ralphctl start, a template and the
registry"). Reuses tests/test_cli_docker.py's `Ctl` runner (real ralphctl
subprocess, stub recording docker, temp registry) so it inspects job.yaml
without importing any CLI internals, then hands that exact job.yaml to
`JobConfig.load`/`tiers.default_host_map`/`tiers.resolve_bundle` to prove
the engine resolves it -- the same discipline
tests/test_cli_model_per_hat.py already uses for the flat per-hat table
task 008 gave `--model-for`.

Both tests build the hat x tier matrix purely through supported paths
(`--model-for-tier`, a `--template`'s job.yaml `model_tiers:` key,
`<registry>/config.yaml`'s own `model_tiers:` key) -- never a hand-edited
job.yaml.
"""

from __future__ import annotations

import yaml
from test_cli_docker import Ctl

import pytest

from ralphd.engine import tiers
from ralphd.engine.config import JobConfig


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _job_yaml_path(ctl: Ctl, run_id: str):
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, f"expected one job.yaml for {run_id}, found {matches}"
    return matches[0]


def _job_yaml(ctl: Ctl, run_id: str) -> dict:
    return yaml.safe_load(_job_yaml_path(ctl, run_id).read_text())


WORKER_HARD = "amazon-bedrock/eu.anthropic.claude-opus-5"
WORKER_NORMAL = "amazon-bedrock/eu.anthropic.claude-sonnet-5"
VERIFY_HARD = "aigw-openai/openai.gpt-5.6-terra"


def test_matrix_via_model_for_tier_flags_resolves_hat_x_tier(ctl):
    """A real hat x tier matrix, built with nothing but repeatable
    `--model-for-tier` flags -- no hand-edited job.yaml anywhere."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tier-matrix",
                  "--model-for-tier", f"worker:hard={WORKER_HARD}",
                  "--model-for-tier", f"worker:normal={WORKER_NORMAL}",
                  "--model-for-tier", f"verify:hard={VERIFY_HARD}")
    assert res.returncode == 0, res.stderr
    path = _job_yaml_path(ctl, "tier-matrix")
    job = yaml.safe_load(path.read_text())
    assert job["model_tiers"] == {
        "worker": {"hard": WORKER_HARD, "normal": WORKER_NORMAL},
        "verify": {"hard": VERIFY_HARD},
    }

    # ... and the engine's pure resolver honours every configured rung.
    cfg = JobConfig.load(path)
    host_map = tiers.default_host_map(cfg)
    assert tiers.resolve_bundle("worker", "hard", host_map).model == WORKER_HARD
    assert tiers.resolve_bundle("worker", "normal", host_map).model == WORKER_NORMAL
    assert tiers.resolve_bundle("verify", "hard", host_map).model == VERIFY_HARD
    # worker's trivial rung was never configured -- falls back to the
    # plain per-hat default (task 002's collapse-for-a-one-model-operator).
    assert tiers.resolve_bundle("worker", "trivial", host_map).model == cfg.bundle_for("worker").model


def test_precedence_explicit_flag_beats_template_beats_registry_config(ctl):
    """Pins the order documented for the flat table
    (`_resolve_model_table`) at the tier-table's own two merge levels
    (hat AND tier): explicit `--model-for-tier` > `--template`'s
    `model_tiers:` key > `<registry>/config.yaml`'s own `model_tiers:`
    key -- merged rung by rung, not replace-whole, so a (hat, tier) rung
    only the registry names survives an explicit flag that names a
    different rung, even within the SAME hat."""
    (ctl.registry / "config.yaml").write_text(
        "model_tiers:\n"
        "  worker:\n"
        "    trivial: prov/registry-worker-trivial\n"
        "    normal: prov/registry-worker-normal\n"
        "  review:\n"
        "    hard: prov/registry-review-hard\n")
    tdir = ctl.registry / "templates" / "tiers"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text(
        "model_tiers:\n"
        "  worker:\n"
        "    normal: prov/template-worker-normal\n"
        "    hard: prov/template-worker-hard\n")

    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tier-precedence", "--template", "tiers",
                  "--model-for-tier", "worker:hard=prov/flag-worker-hard")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "tier-precedence")
    assert job["model_tiers"] == {
        "worker": {
            "trivial": "prov/registry-worker-trivial",  # registry survives (nothing above named it)
            "normal": "prov/template-worker-normal",    # template wins over registry
            "hard": "prov/flag-worker-hard",             # explicit flag wins over both
        },
        "review": {"hard": "prov/registry-review-hard"},  # registry survives (a different hat)
    }

    cfg = JobConfig.load(_job_yaml_path(ctl, "tier-precedence"))
    host_map = tiers.default_host_map(cfg)
    assert tiers.resolve_bundle("worker", "trivial", host_map).model == "prov/registry-worker-trivial"
    assert tiers.resolve_bundle("worker", "normal", host_map).model == "prov/template-worker-normal"
    assert tiers.resolve_bundle("worker", "hard", host_map).model == "prov/flag-worker-hard"
    assert tiers.resolve_bundle("review", "hard", host_map).model == "prov/registry-review-hard"


def test_malformed_model_for_tier_value_exits_2(ctl):
    for bad in ("not-a-kv-pair", "worker=prov/model-id", "worker:hard-prov/model-id"):
        res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                      "--run-id", f"tier-malformed-{abs(hash(bad))}",
                      "--model-for-tier", bad)
        assert res.returncode == 2, res.stderr
        assert "--model-for-tier" in res.stderr
        assert "HAT:TIER=REF" in res.stderr


def test_omitting_the_flag_leaves_the_model_tiers_key_out_of_job_yaml(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tier-none")
    assert res.returncode == 0, res.stderr
    assert "model_tiers" not in _job_yaml(ctl, "tier-none")


def test_flag_appears_in_start_help(ctl):
    res = ctl.run("start", "--help")
    assert res.returncode == 0, res.stderr
    assert "--model-for-tier HAT:TIER=REF" in res.stdout
