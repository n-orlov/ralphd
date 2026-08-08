"""Black-box tests for `ralphctl creds <run-id> ls|get|add|rm` (PRD req 12).

Drives the real `ralphctl` binary against a real, directly-launched
`ralphd-engine` (the `live` fixture from conftest.py) — no engine internals
imported, only the CLI's stdout/exit code and files it writes back to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def creds_run(tmp_path, live):
    home = tmp_path / "home"
    home.mkdir()
    r = live(run_id="creds-cli", job={"iterations": 3, "vigilant": False},
             stub_env={"HOME": str(home)})
    r.wait_api()
    yield r


def _mounted_cred(config_dir: Path, name: str, value: str) -> None:
    d = config_dir / "creds"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.env").write_text(value)


def test_creds_ls_shows_names_no_values(creds_run):
    _mounted_cred(creds_run.config_dir, "github", "GITHUB_TOKEN=super-secret-value\n")
    res = creds_run.ralphctl("--json", "creds", creds_run.run_id, "ls")
    assert res.returncode == 0, res.stderr
    rows = json.loads(res.stdout)
    assert any(r["name"] == "github" for r in rows)
    assert "super-secret-value" not in res.stdout


def test_creds_add_uploads_and_ls_lists(creds_run, tmp_path):
    f = tmp_path / "gitlab.env"
    f.write_text("GITLAB_TOKEN=glpat-abc123\n")
    res = creds_run.ralphctl("creds", creds_run.run_id, "add", str(f))
    assert res.returncode == 0, res.stderr

    ls = json.loads(creds_run.ralphctl("--json", "creds", creds_run.run_id, "ls").stdout)
    assert any(r["name"] == "gitlab" for r in ls)


def test_creds_add_replaces(creds_run, tmp_path):
    f = tmp_path / "replaceme.env"
    f.write_text("A=1\n")
    res = creds_run.ralphctl("creds", creds_run.run_id, "add", str(f))
    assert res.returncode == 0, res.stderr

    f.write_text("A=2\nB=3\n")
    res = creds_run.ralphctl("creds", creds_run.run_id, "add", str(f))
    assert res.returncode == 0, res.stderr

    got = creds_run.ralphctl("creds", creds_run.run_id, "get", "replaceme")
    assert got.returncode == 0, got.stderr
    assert got.stdout == "A=2\nB=3\n"


def test_creds_get_prints_contents(creds_run, tmp_path):
    f = tmp_path / "printme.env"
    f.write_text("KEY=value123\n")
    res = creds_run.ralphctl("creds", creds_run.run_id, "add", str(f))
    assert res.returncode == 0, res.stderr

    got = creds_run.ralphctl("creds", creds_run.run_id, "get", "printme")
    assert got.returncode == 0, got.stderr
    assert got.stdout == "KEY=value123\n"


def test_creds_rm_deletes(creds_run, tmp_path):
    f = tmp_path / "oneoff.env"
    f.write_text("X=y\n")
    res = creds_run.ralphctl("creds", creds_run.run_id, "add", str(f))
    assert res.returncode == 0, res.stderr

    res = creds_run.ralphctl("creds", creds_run.run_id, "rm", "oneoff")
    assert res.returncode == 0, res.stderr

    ls = json.loads(creds_run.ralphctl("--json", "creds", creds_run.run_id, "ls").stdout)
    assert not any(r["name"] == "oneoff" for r in ls)

    # deleting again -> engine 404s, ralphctl surfaces nonzero, not silent success
    res = creds_run.ralphctl("creds", creds_run.run_id, "rm", "oneoff")
    assert res.returncode != 0


def test_creds_add_rejects_non_env_file(creds_run, tmp_path):
    f = tmp_path / "notacred.txt"
    f.write_text("whatever\n")
    res = creds_run.ralphctl("creds", creds_run.run_id, "add", str(f))
    assert res.returncode == 2
    assert ".env" in res.stderr


def test_creds_missing_run_exits_3(live):
    r = live(run_id="creds-cli-exists-only")  # ensures registry/RALPHCTL wiring exists
    res = r.ralphctl("creds", "no-such-run-at-all", "ls")
    assert res.returncode == 3


def test_creds_json_output_stable(creds_run):
    _mounted_cred(creds_run.config_dir, "aws", "AWS_KEY=abc\n")
    res = creds_run.ralphctl("--json", "creds", creds_run.run_id, "ls")
    assert res.returncode == 0
    rows = json.loads(res.stdout)
    assert isinstance(rows, list)
    for row in rows:
        assert set(row.keys()) >= {"name", "size"}
        assert "value" not in row
