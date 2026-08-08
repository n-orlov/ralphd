"""Black-box tests for `ralphctl start --template <name>` (PRD req 25,
template part). Reuses tests/test_cli_docker.py's `Ctl` runner (real
ralphctl subprocess, stub recording docker, temp registry) so it can
inspect job.yaml plus the recorded `docker run` argv without importing
any CLI internals.
"""

from __future__ import annotations

import pytest
import yaml
from test_cli_docker import Ctl


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _template_dir(ctl: Ctl, name: str):
    d = ctl.registry / "templates" / name
    d.mkdir(parents=True)
    return d


def _job_yaml(ctl: Ctl, run_id: str) -> dict:
    # job.yaml lives under <registry>/configs/<run_id>/ (main.py's
    # config_root()) -- glob rather than importing CLI internals.
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, f"expected one job.yaml for {run_id}, found {matches}"
    text = matches[0].read_text()
    # each line is `key: json-value` (see cmd_start) -- safe_load parses it
    # as ordinary YAML.
    return yaml.safe_load(text)


def test_unknown_template_exits_3(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--template", "nope-does-not-exist")
    assert res.returncode == 3, res.stderr
    assert "nope-does-not-exist" in res.stderr


def test_template_supplies_job_defaults(ctl):
    tdir = _template_dir(ctl, "quick")
    (tdir / "job.yaml").write_text(
        "iterations: 7\nmax_approaches: 2\nvigilant: true\non_complete: exit\n"
        "model_strategy: cost-optimized\n"
    )
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tpl-defaults", "--template", "quick")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "tpl-defaults")
    assert job["iterations"] == 7
    assert job["max_approaches"] == 2
    assert job["vigilant"] is True
    assert job["on_complete"] == "exit"
    assert job["model_strategy"] == "cost-optimized"


def test_explicit_flag_overrides_template_value(ctl):
    tdir = _template_dir(ctl, "override-me")
    (tdir / "job.yaml").write_text("iterations: 7\nvigilant: true\n")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tpl-override", "--template", "override-me",
                  "--iterations", "42")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "tpl-override")
    assert job["iterations"] == 42          # explicit flag wins
    assert job["vigilant"] is True          # template value still applied


def test_template_prd_skeleton_used_when_no_prd_flag(ctl):
    tdir = _template_dir(ctl, "with-prd")
    (tdir / "prd.md").write_text("# Skeleton PRD\n\nBuilt from a template.\n")
    res = ctl.run("start", "--llm", "none", "--run-id", "tpl-prd",
                  "--template", "with-prd")
    assert res.returncode == 0, res.stderr
    matches = list(ctl.tmp.glob("**/configs/tpl-prd/prd.md"))
    assert len(matches) == 1
    assert "Skeleton PRD" in matches[0].read_text()


def test_missing_prd_and_no_template_prd_exits_2(ctl):
    res = ctl.run("start", "--llm", "none", "--run-id", "tpl-noprd")
    assert res.returncode == 2, res.stderr
    assert "--prd" in res.stderr


def test_template_skills_and_creds_applied(ctl):
    tdir = _template_dir(ctl, "with-extras")
    skill_dir = tdir / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# a skill\n")
    creds_dir = tdir / "creds"
    creds_dir.mkdir()
    (creds_dir / "secrets.env").write_text("FOO=bar\n")
    (tdir / "job.yaml").write_text("skills: [my-skill]\ncreds: creds\n")

    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tpl-extras", "--template", "with-extras")
    assert res.returncode == 0, res.stderr
    skill_matches = list(ctl.tmp.glob("**/configs/tpl-extras/skills/my-skill/SKILL.md"))
    assert len(skill_matches) == 1
    creds_matches = list(ctl.tmp.glob("**/configs/tpl-extras/creds/secrets.env"))
    assert len(creds_matches) == 1
    assert creds_matches[0].read_text() == "FOO=bar\n"


def test_explicit_skills_flag_overrides_template_skills(ctl, tmp_path):
    tdir = _template_dir(ctl, "skills-override")
    tpl_skill = tdir / "tpl-skill"
    tpl_skill.mkdir()
    (tpl_skill / "SKILL.md").write_text("# template skill\n")
    (tdir / "job.yaml").write_text("skills: [tpl-skill]\n")

    explicit_skill = tmp_path / "explicit-skill"
    explicit_skill.mkdir()
    (explicit_skill / "SKILL.md").write_text("# explicit skill\n")

    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tpl-skills-override", "--template", "skills-override",
                  "--skills", str(explicit_skill))
    assert res.returncode == 0, res.stderr
    assert list(ctl.tmp.glob("**/configs/tpl-skills-override/skills/explicit-skill/SKILL.md"))
    assert not list(ctl.tmp.glob("**/configs/tpl-skills-override/skills/tpl-skill"))


def test_template_without_job_yaml_uses_hardcoded_defaults(ctl):
    _template_dir(ctl, "bare")
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tpl-bare", "--template", "bare")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "tpl-bare")
    assert job["iterations"] == 25
    assert job["max_approaches"] == 3


def test_template_model_and_llm_defaults(ctl):
    tdir = _template_dir(ctl, "llm-defaults")
    (tdir / "job.yaml").write_text("model: anthropic/claude-x\nllm: none\n")
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", "tpl-llm",
                  "--template", "llm-defaults")
    assert res.returncode == 0, res.stderr
    job = _job_yaml(ctl, "tpl-llm")
    assert job["model"] == "anthropic/claude-x"
