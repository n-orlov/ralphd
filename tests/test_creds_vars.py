"""`<name>.vars` credential files and the no-silent-drop rule (#314, PRD F.1
copy/placement, F.2, F.3): a `.vars` file carries NON-secret values (a server
URL, a user name) a `.env` can source; it is copied, placed and rebuilt like a
`.env`, never value-scrubbed, and every file `--creds` does not copy is named.
"""

from __future__ import annotations

import json
import stat

import pytest
from test_cli_docker import Ctl

from ralphd.engine import creds, redact

URL = "https://ci.example.test/jenkins"
SECRET = "s3kr3t-token-value-0123456789"


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    d = tmp_path / "overlay"
    monkeypatch.setattr(creds, "OVERLAY_DIR", d)
    return d


def _src(tmp_path):
    src = tmp_path / "src-creds"
    src.mkdir()
    (src / "ci.env").write_text(f"CI_TOKEN={SECRET}\n")
    (src / "ci.vars").write_text(f"CI_URL={URL}\nCI_USER=builder\n")
    return src


def _start(ctl, src, run_id="vars-run"):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", run_id, "--creds", str(src))
    assert res.returncode == 0, res.stderr
    return res, ctl.registry / "configs" / run_id / "creds"


# --- copy ---------------------------------------------------------------

def test_start_creds_copies_vars_files(ctl, tmp_path):
    _res, staged = _start(ctl, _src(tmp_path))
    assert (staged / "ci.vars").read_text() == f"CI_URL={URL}\nCI_USER=builder\n"
    assert (staged / "ci.env").is_file()


def test_template_creds_directory_copies_vars_files(ctl, tmp_path):
    tdir = ctl.registry / "templates" / "t1"
    (tdir / "creds").mkdir(parents=True)
    (tdir / "creds" / "ci.vars").write_text(f"CI_URL={URL}\n")
    (tdir / "job.yaml").write_text("creds: creds\n")
    res = ctl.run("start", "--template", "t1", "--prd", str(ctl.prd),
                  "--llm", "none", "--run-id", "tpl-run")
    assert res.returncode == 0, res.stderr
    staged = ctl.registry / "configs" / "tpl-run" / "creds"
    assert (staged / "ci.vars").read_text() == f"CI_URL={URL}\n"


@pytest.mark.parametrize("job_yaml", [None, "iterations: 7\n"],
                         ids=["no-job-yaml", "job-yaml-without-creds-key"])
def test_template_default_creds_directory_copies_vars_files(ctl, tmp_path, job_yaml):
    """SPEC 6.x: a template with no job.yaml (or one naming no `creds:`
    directory) still contributes its `creds/` directory."""
    tdir = ctl.registry / "templates" / "implicit"
    (tdir / "creds").mkdir(parents=True)
    (tdir / "creds" / "ci.vars").write_text(f"CI_URL={URL}\n")
    if job_yaml is not None:
        (tdir / "job.yaml").write_text(job_yaml)
    res = ctl.run("start", "--template", "implicit", "--prd", str(ctl.prd),
                  "--llm", "none", "--run-id", "implicit-run")
    assert res.returncode == 0, res.stderr
    staged = ctl.registry / "configs" / "implicit-run" / "creds"
    assert (staged / "ci.vars").read_text() == f"CI_URL={URL}\n"


def test_template_without_creds_directory_stages_no_creds(ctl):
    tdir = ctl.registry / "templates" / "bare"
    tdir.mkdir(parents=True)
    res = ctl.run("start", "--template", "bare", "--prd", str(ctl.prd),
                  "--llm", "none", "--run-id", "bare-run")
    assert res.returncode == 0, res.stderr
    staged = ctl.registry / "configs" / "bare-run" / "creds"
    assert not staged.exists() or list(staged.iterdir()) == []


# --- placement, rebuild, delete -------------------------------------------

def _config_with(tmp_path, **files):
    cfg = tmp_path / "config"
    (cfg / "creds").mkdir(parents=True)
    for name, text in files.items():
        (cfg / "creds" / name.replace("__", ".")).write_text(text)
    return cfg


def test_engine_places_vars_at_home_creds_mode_0600(tmp_path, overlay):
    cfg = _config_with(tmp_path, ci__vars=f"CI_URL={URL}\n", ci__env="A=b\n")
    home = tmp_path / "home"
    home.mkdir()
    placed = creds.place_creds(cfg, home=home)
    dest = home / ".creds" / "ci.vars"
    assert dest.read_text() == f"CI_URL={URL}\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert "ci.vars" in placed


def test_vars_are_rebuilt_on_every_placement_so_a_delete_takes_effect(tmp_path, overlay):
    cfg = _config_with(tmp_path, ci__vars=f"CI_URL={URL}\n", other__vars="X=y\n")
    home = tmp_path / "home"
    home.mkdir()
    creds.place_creds(cfg, home=home)
    assert (home / ".creds" / "other.vars").is_file()
    (cfg / "creds" / "other.vars").unlink()
    (home / ".creds" / "stale.vars").write_text("S=1\n")
    creds.place_creds(cfg, home=home)
    assert not (home / ".creds" / "other.vars").exists()
    assert not (home / ".creds" / "stale.vars").exists()
    assert (home / ".creds" / "ci.vars").is_file()


def test_vars_only_creds_still_places_and_rebuilds(tmp_path, overlay):
    """No `.env` at all: the `.vars` file alone is placed, and removing it
    empties the placed directory of `.vars`."""
    cfg = _config_with(tmp_path, ci__vars=f"CI_URL={URL}\n")
    home = tmp_path / "home"
    home.mkdir()
    creds.place_creds(cfg, home=home)
    assert (home / ".creds" / "ci.vars").is_file()
    (cfg / "creds" / "ci.vars").unlink()
    creds.place_creds(cfg, home=home)
    assert list((home / ".creds").glob("*.vars")) == []


# --- scrubbing --------------------------------------------------------------

def test_vars_value_is_absent_from_config_dir_secrets(tmp_path):
    cfg = _config_with(tmp_path, ci__vars=f"CI_URL={URL}\nCI_USER=builder-user\n",
                       ci__env=f"CI_TOKEN={SECRET}\n")
    secrets = redact.config_dir_secrets(cfg)
    assert SECRET in secrets  # the `.env` value is still a secret
    assert URL not in secrets
    assert "builder-user" not in secrets
    assert redact.scrub_text(f"see {URL} as builder-user", extra=secrets) \
        == f"see {URL} as builder-user"


def test_token_named_variable_in_a_vars_file_is_still_masked_by_name():
    """Control: name-based masking looks at the variable NAMES a `.vars` file
    defines, whatever file they came from -- passes with and without the
    value-scrub exclusion."""
    parsed = {"CI_URL": URL, "CI_API_TOKEN": SECRET}
    masked = redact.mask_secret_names(parsed)
    assert masked["CI_API_TOKEN"] == redact.MASK
    assert masked["CI_URL"] == URL
    found: dict[str, str] = {}
    redact._from_env(parsed, "vars:", found)
    assert SECRET in found and URL not in found


# --- no silent drop -----------------------------------------------------------

def test_copy_creds_names_every_skipped_file_and_directory_on_stderr(ctl, tmp_path):
    src = _src(tmp_path)
    (src / "foo.txt").write_text("x")
    (src / ".hidden").write_text("x")
    (src / "extras").mkdir()
    (src / "ssh").mkdir()
    (src / "gitconfig").write_text("[user]\n")
    res, staged = _start(ctl, src)
    lines = [ln for ln in res.stderr.splitlines() if ln.startswith("--creds: ignored")]
    assert any("`foo.txt`" in ln for ln in lines), res.stderr
    assert any("`.hidden`" in ln for ln in lines), res.stderr
    assert any("directory `extras/`" in ln for ln in lines), res.stderr
    assert not any("ci.vars" in ln or "ci.env" in ln or "gitconfig" in ln
                   or "ssh" in ln for ln in lines), res.stderr
    assert not (staged / "foo.txt").exists()
    assert (staged / "ci.vars").is_file()


def test_doctor_reports_skipped_files_in_registry_and_template_creds(ctl, tmp_path):
    reg_creds = ctl.registry / "creds"
    reg_creds.mkdir()
    (reg_creds / "ok.vars").write_text("A=b\n")
    (reg_creds / "stray.txt").write_text("x")
    tcreds = ctl.registry / "templates" / "t1" / "creds"
    tcreds.mkdir(parents=True)
    (tcreds / "notes.md").write_text("x")
    (tcreds / "junkdir").mkdir()
    res = ctl.run("--json", "doctor")
    doc = json.loads(res.stdout)
    got = {(c["source"], c["name"], c["directory"]) for c in doc["credsSkipped"]}
    assert got == {("registry creds", "stray.txt", False),
                   ("template t1", "notes.md", False),
                   ("template t1", "junkdir", True)}
    text = ctl.run("doctor").stdout
    assert "stray.txt" in text and "notes.md" in text and "junkdir/" in text
    assert "ok.vars" not in text
