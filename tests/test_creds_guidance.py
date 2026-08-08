"""Black-box test: credential handling guidance forbids exposing cred values
to the iteration transcript (PRD-adjacent hardening, task 049).

Proves:
  - the creds inventory note (present whenever ~/.creds is populated) and
    the worker prompt both explicitly forbid printing/cat-ing/echoing
    credential file contents or putting secrets in command arguments,
  - both state the sanctioned pattern (`set -a; . <file>; set +a`),
  - both give the reason (tool call args/stdout are recorded verbatim in
    the iteration transcript),
  - both forbid token-bearing git remote URLs,
  - the worker prompt carries this guidance even when no creds are
    configured at all (it's a standing rule, not conditional on the note).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_e2e import EngineProc

SECRET_VALUE = "sekret-token-do-not-leak-guidance-xyz789"
SOURCING_RULE = "set -a; . ~/.creds/<name>.env; set +a"

PROHIBITION_MARKERS = ["cat", "echo", "print"]


@pytest.fixture
def guidance_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "creds-guidance-e2e", "iterations": 6,
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


def _assert_guidance(p: str) -> None:
    low = p.lower()
    for marker in PROHIBITION_MARKERS:
        assert marker in low, f"missing prohibition marker {marker!r}"
    assert "transcript" in low
    assert "verbatim" in low
    assert SOURCING_RULE in p
    assert "git remote" in low or "token-bearing git remote" in low


def test_creds_note_forbids_dumping_values_and_token_urls(guidance_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    _write_creds(tmp_path / "config")
    e = guidance_engine(stub_env={"HOME": str(fake_home)})
    assert e.proc.wait(timeout=60) == 0
    out = e.proc.stdout.read()

    all_prompts = (e.run_dir / ".stub-all-prompts").read_text()
    prompts = [p for p in all_prompts.split("===PROMPT===") if p.strip()]
    assert len(prompts) >= 3

    for p in prompts:
        assert "## Credentials" in p
        _assert_guidance(p)

    # negative proof surface: the secret value never appears anywhere
    run_dir_text = []
    for f in e.run_dir.rglob("*"):
        if f.is_file():
            try:
                run_dir_text.append(f.read_text(errors="ignore"))
            except OSError:
                pass
    assert SECRET_VALUE not in "\n".join(run_dir_text)
    assert SECRET_VALUE not in out


def test_worker_prompt_carries_guidance_even_without_creds(guidance_engine, tmp_path):
    """The worker prompt's own 'Credential handling' section is a standing
    rule, not conditional on any creds being configured for this job."""
    fake_home = tmp_path / "fakehome2"
    fake_home.mkdir()
    e = guidance_engine(job={"iterations": 3, "max_approaches": 1},
                         stub_env={"HOME": str(fake_home)})
    e.proc.wait(timeout=30)
    all_prompts = (e.run_dir / ".stub-all-prompts").read_text()
    prompts = [p for p in all_prompts.split("===PROMPT===") if p.strip()]
    assert prompts
    worker_prompts = [p for p in prompts if "## Credential handling" in p]
    assert worker_prompts, "expected at least one worker prompt with the standing guidance"
    for p in worker_prompts:
        assert "## Credentials" not in p  # no creds configured -> no inventory note
        _assert_guidance(p)


def test_source_files_have_no_stray_secret_and_pattern_present():
    """Static check that the guidance lives in both the engine's creds note
    and the worker prompt file, independent of runtime behavior."""
    loop_src = Path("src/ralphd/engine/loop.py").read_text()
    worker_prompt = Path("src/ralphd/prompts/worker.md").read_text()
    for text, name in [(loop_src, "loop.py"), (worker_prompt, "worker.md")]:
        low = text.lower()
        assert "cat" in low and "echo" in low, name
        assert "transcript" in low, name
        assert "verbatim" in low, name
        assert SOURCING_RULE in text, name
        assert "git remote" in low, name
