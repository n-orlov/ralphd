"""Black-box test: every phase prompt's job-context section advertises the
credential inventory (file names + sourcing rule), never values (PRD req 7).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_e2e import EngineProc

SECRET_VALUE = "sekret-token-do-not-leak-prompt-abc123"
SOURCING_RULE = "set -a; . ~/.creds/<name>.env; set +a"


@pytest.fixture
def creds_prompt_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "creds-prompt-e2e", "iterations": 12,
                    "max_approaches": 3, "on_complete": "exit"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _write_creds(config_dir: Path) -> None:
    creds = config_dir / "creds"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / "github.env").write_text(f"GITHUB_TOKEN={SECRET_VALUE}\n")
    (creds / "jenkins.env").write_text("JENKINS_URL=https://example.com\n")
    # a non-.env extra: must not show up in the *.env inventory list
    (creds / "netrc").write_text(f"machine example.com login bot password {SECRET_VALUE}\n")


def test_every_phase_prompt_lists_creds_inventory_no_values(creds_prompt_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    # EngineProc starts the subprocess synchronously in __init__, so the
    # creds dir must exist under tmp_path/"config" before construction.
    _write_creds(tmp_path / "config")
    e = creds_prompt_engine(stub_env={"HOME": str(fake_home)})
    assert e.proc.wait(timeout=60) == 0
    out = e.proc.stdout.read()

    all_prompts = (e.run_dir / ".stub-all-prompts").read_text()
    prompts = [p for p in all_prompts.split("===PROMPT===") if p.strip()]
    # planning, worker (>=1), review at minimum for a job that completes
    assert len(prompts) >= 3

    for p in prompts:
        assert "## Credentials" in p
        assert "`~/.creds/github.env`" in p
        assert "`~/.creds/jenkins.env`" in p
        assert SOURCING_RULE in p
        # never the extras list (only *.env are sourced this way) and never
        # any secret value
        assert "netrc" not in p
        assert SECRET_VALUE not in p

    # negative proof surface: nowhere in the run dir, events, or stdout
    run_dir_text = []
    for f in e.run_dir.rglob("*"):
        if f.is_file():
            try:
                run_dir_text.append(f.read_text(errors="ignore"))
            except OSError:
                pass
    assert SECRET_VALUE not in "\n".join(run_dir_text)
    assert SECRET_VALUE not in out


def test_no_creds_no_credentials_section(creds_prompt_engine, tmp_path):
    """Absence of ~/.creds must not fabricate a Credentials section."""
    fake_home = tmp_path / "fakehome2"
    fake_home.mkdir()
    e = creds_prompt_engine(job={"iterations": 3, "max_approaches": 1},
                             stub_env={"HOME": str(fake_home)})
    e.proc.wait(timeout=30)
    all_prompts = (e.run_dir / ".stub-all-prompts").read_text()
    prompts = [p for p in all_prompts.split("===PROMPT===") if p.strip()]
    assert prompts
    for p in prompts:
        assert "## Credentials" not in p
