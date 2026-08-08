"""Black-box tests for `ralphctl skills <run-id> ls|get|add|rm` (PRD req 12).

Drives the real `ralphctl` binary against a real, directly-launched
`ralphd-engine` (the `live` fixture from conftest.py) — no engine internals
imported, only the CLI's stdout/exit code and files it writes back to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def skills_run(tmp_path, live):
    home = tmp_path / "home"
    home.mkdir()
    r = live(run_id="skills-cli", job={"iterations": 3, "vigilant": False},
             stub_env={"HOME": str(home)})
    r.wait_api()
    yield r


def _mounted_skill(config_dir: Path, name: str) -> None:
    d = config_dir / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {name}\n\nMounted test skill.\n")
    (d / "notes.txt").write_text("supporting content\n")


def test_skills_ls_shows_mounted_and_origin(skills_run):
    _mounted_skill(skills_run.config_dir, "git")
    res = skills_run.ralphctl("--json", "skills", skills_run.run_id, "ls")
    assert res.returncode == 0, res.stderr
    rows = json.loads(res.stdout)
    assert any(r["name"] == "git" and r["origin"] == "mounted" for r in rows)


def test_skills_add_validates_and_uploads(tmp_path, skills_run):
    bad = tmp_path / "not-a-skill"
    bad.mkdir()
    (bad / "readme.txt").write_text("no SKILL.md here\n")
    res = skills_run.ralphctl("skills", skills_run.run_id, "add", str(bad))
    assert res.returncode == 2
    assert "SKILL.md" in res.stderr

    good = tmp_path / "playwright"
    good.mkdir()
    (good / "SKILL.md").write_text("# playwright\n\nDrive a browser.\n")
    (good / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    res = skills_run.ralphctl("skills", skills_run.run_id, "add", str(good))
    assert res.returncode == 0, res.stderr

    ls = json.loads(skills_run.ralphctl("--json", "skills", skills_run.run_id, "ls").stdout)
    row = next(r for r in ls if r["name"] == "playwright")
    assert row["origin"] == "api"


def test_skills_get_roundtrips(tmp_path, skills_run):
    good = tmp_path / "sonar"
    good.mkdir()
    (good / "SKILL.md").write_text("# sonar\n\nStatic analysis.\n")
    (good / "sub").mkdir()
    (good / "sub" / "config.yaml").write_text("key: value\n")
    res = skills_run.ralphctl("skills", skills_run.run_id, "add", str(good))
    assert res.returncode == 0, res.stderr

    dest = tmp_path / "downloaded"
    res = skills_run.ralphctl("skills", skills_run.run_id, "get", "sonar", str(dest))
    assert res.returncode == 0, res.stderr
    assert (dest / "SKILL.md").read_text() == "# sonar\n\nStatic analysis.\n"
    assert (dest / "sub" / "config.yaml").read_text() == "key: value\n"


def test_skills_rm_deletes(tmp_path, skills_run):
    good = tmp_path / "one-off"
    good.mkdir()
    (good / "SKILL.md").write_text("# one-off\n")
    res = skills_run.ralphctl("skills", skills_run.run_id, "add", str(good))
    assert res.returncode == 0, res.stderr

    res = skills_run.ralphctl("skills", skills_run.run_id, "rm", "one-off")
    assert res.returncode == 0, res.stderr

    ls = json.loads(skills_run.ralphctl("--json", "skills", skills_run.run_id, "ls").stdout)
    assert not any(r["name"] == "one-off" for r in ls)

    # deleting again -> the engine's DELETE returns 404, ralphctl surfaces
    # a nonzero exit (not 0/success) rather than silently succeeding again.
    res = skills_run.ralphctl("skills", skills_run.run_id, "rm", "one-off")
    assert res.returncode != 0


def test_skills_missing_run_exits_3(tmp_path, live):
    r = live(run_id="skills-cli-exists-only")  # ensures registry/RALPHCTL wiring exists
    res = r.ralphctl("skills", "no-such-run-at-all", "ls")
    assert res.returncode == 3


def test_skills_json_output_stable(skills_run):
    _mounted_skill(skills_run.config_dir, "git")
    res = skills_run.ralphctl("--json", "skills", skills_run.run_id, "ls")
    assert res.returncode == 0
    rows = json.loads(res.stdout)
    assert isinstance(rows, list)
    for row in rows:
        assert set(row.keys()) >= {"name", "origin"}


def test_skills_tar_shape_matches_get_add_dir(tmp_path, skills_run):
    """A sanity check that `add` produces the same tar-with-no-wrapper-folder
    shape the engine's own tar_dir() produces, so `get` back into a fresh
    dir is byte-identical file-by-file (not just readable)."""
    good = tmp_path / "shape-check"
    good.mkdir()
    (good / "SKILL.md").write_text("shape\n")
    skills_run.ralphctl("skills", skills_run.run_id, "add", str(good))

    dest = tmp_path / "shape-check-out"
    skills_run.ralphctl("skills", skills_run.run_id, "get", "shape-check", str(dest))
    assert {p.name for p in dest.iterdir()} == {"SKILL.md"}
