"""PRD v0.33 B.1: the codex wrapper (`python -m ralphd.harness.codex`) and
`runner.CodexRunner` -- the scratch `CODEX_HOME`, its `config.toml`, the `codex`
argv and the prompt on stdin.

Every test drives the real wrapper against the stub Codex CLI
(`tests/stub-codex/codex`, through `RALPHD_CODEX_CLI_PATH`). The stub dumps what
it observed (argv, env, stdin, the `CODEX_HOME` entries and `config.toml`) to
`STUB_CODEX_OBSERVE`. No network, no real token: the bearer value and the base
URL below are obvious fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
import site
import tomllib
from pathlib import Path

import pytest

from ralphd.engine.runner import CodexRunner, IterationResult
from ralphd.harness.codex import CODEX_EFFORT_TABLE

REPO = Path(__file__).resolve().parents[1]
STUB = REPO / "tests" / "stub-codex" / "codex"
FAKE_TOKEN = "fake-bearer-token-FOR-TESTS-ONLY-0123"
FAKE_BASE_URL = "https://aigw.invalid/api/openai/v1"
MODEL_REF = "aigw-openai/openai.gpt-6.1-sol"


def _setup(mp, tmp_path: Path, *, headers: dict | None = None, **stub) -> Path:
    """Point the wrapper at the stub and a fake gateway; returns the
    observation path. A fake home carries pi's `models.json`."""
    observe = tmp_path / "observed.json"
    mp.setenv("RALPHD_CODEX_CLI_PATH", str(STUB))
    mp.setenv("STUB_CODEX_OBSERVE", str(observe))
    mp.setenv("PYTHONUSERBASE", os.environ.get("PYTHONUSERBASE") or site.getuserbase())
    mp.setenv("AIGW_OPENAI_BASE_URL", FAKE_BASE_URL)
    mp.setenv("AWS_BEARER_TOKEN_BEDROCK", FAKE_TOKEN)
    for k in [k for k in os.environ if k.startswith("STUB_CODEX_") and k != "STUB_CODEX_OBSERVE"]:
        mp.delenv(k)
    for k, v in stub.items():
        mp.setenv(k, v)
    provider: dict = {"baseUrl": FAKE_BASE_URL, "api": "openai-responses"}
    if headers is not None:
        provider["headers"] = headers
    agent = tmp_path / "home" / ".pi" / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "models.json").write_text(json.dumps({"providers": {"aigw-openai": provider}}))
    return observe


def _run(tmp_path: Path, name: str = "it", **kw) -> tuple[IterationResult, dict, Path]:
    itdir = tmp_path / name
    itdir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    runner = CodexRunner(ws, home_fn=lambda: tmp_path / "home")
    kw.setdefault("model", MODEL_REF)
    kw.setdefault("timeout_s", 60)
    prompt = kw.pop("prompt", "the prompt")
    result = asyncio.run(runner.run(prompt, itdir / "output.jsonl", **kw))
    observed = json.loads((tmp_path / "observed.json").read_text())
    return result, observed, itdir


def _config(obs: dict) -> dict:
    return tomllib.loads(obs["config_toml"])


@pytest.fixture
def one_run(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, headers={"X-Jira-ID": "JIRA-123"})
    result, obs, itdir = _run(tmp_path, thinking="high")
    return result, obs, itdir, tmp_path


# -- (1) the scratch CODEX_HOME ----------------------------------------------

def test_wrapper_runs_the_stub_and_the_iteration_completes(one_run):
    result, obs, _, _ = one_run
    assert not result.error_message
    assert result.exit_code == 0
    assert result.harness == "codex"
    assert "<promise>COMPLETE</promise>" in result.final_text


def test_codex_home_is_a_scratch_directory_with_only_config_toml(one_run):
    _, obs, itdir, _ = one_run
    scratch = itdir / "output.codex-scratch"
    assert obs["codex_home_exists"] is True
    assert Path(obs["codex_home"]).parent == scratch
    assert obs["codex_home_entries"] == ["config.toml"]
    assert "AGENTS.md" not in obs["codex_home_entries"]


def test_codex_home_is_removed_after_the_call(one_run):
    _, obs, itdir, _ = one_run
    assert not Path(obs["codex_home"]).exists()
    assert (itdir / "output.codex-scratch" / "wrapper.json").is_file()


def test_codex_home_is_removed_when_the_call_fails(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="failed")
    _, obs, _ = _run(tmp_path)
    assert obs["codex_home_exists"] is True
    assert not Path(obs["codex_home"]).exists()


# -- (2) the top-level and isolation keys -------------------------------------

def test_model_has_its_provider_prefix_stripped(one_run):
    _, obs, _, _ = one_run
    assert _config(obs)["model"] == "openai.gpt-6.1-sol"


def test_model_without_a_prefix_is_verbatim(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run(tmp_path, model="openai.gpt-6.1-sol")
    assert _config(obs)["model"] == "openai.gpt-6.1-sol"


def test_wire_and_isolation_keys(one_run):
    _, obs, _, _ = one_run
    cfg = _config(obs)
    assert cfg["model_provider"] == "aigw"
    assert cfg["service_tier"] == "auto"
    assert cfg["web_search"] == "disabled"
    assert cfg["project_doc_max_bytes"] == 0
    assert cfg["skills"]["bundled"]["enabled"] is False
    assert cfg["skills"]["include_instructions"] is False
    for feature in ("multi_agent", "goals", "view_image"):
        assert cfg["features"][feature] is False


@pytest.mark.parametrize("level,expected", sorted(CODEX_EFFORT_TABLE.items()))
def test_reasoning_effort_for_each_table_row(tmp_path, monkeypatch, level, expected):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run(tmp_path, thinking=level)
    assert _config(obs)["model_reasoning_effort"] == expected


def test_effort_argument_is_used_when_thinking_is_unset(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run(tmp_path, effort="xhigh")
    assert _config(obs)["model_reasoning_effort"] == "high"


def test_unset_level_falls_back_to_medium(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run(tmp_path)
    assert _config(obs)["model_reasoning_effort"] == "medium"


# -- (3) the aigw provider ----------------------------------------------------

def test_aigw_provider_table(one_run):
    _, obs, _, _ = one_run
    provider = _config(obs)["model_providers"]["aigw"]
    assert provider["name"]
    assert provider["base_url"] == FAKE_BASE_URL
    assert provider["env_key"] == "AWS_BEARER_TOKEN_BEDROCK"
    assert provider["wire_api"] == "responses"
    assert 0 <= provider["request_max_retries"] <= 2
    assert 0 <= provider["stream_max_retries"] <= 2


def test_profile_header_is_carried_in_http_headers(one_run):
    _, obs, _, _ = one_run
    assert _config(obs)["model_providers"]["aigw"]["http_headers"] == {
        "X-Jira-ID": "JIRA-123"}


def test_no_headers_means_no_http_headers_table(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run(tmp_path)
    assert "http_headers" not in _config(obs)["model_providers"]["aigw"]


def test_header_values_with_quotes_survive_the_toml_round_trip(tmp_path, monkeypatch):
    tricky = 'a "quoted" \\ value, ünïcode'
    _setup(monkeypatch, tmp_path, headers={"X-Tricky": tricky, "X Odd.Name": "v"})
    _, obs, _ = _run(tmp_path)
    assert _config(obs)["model_providers"]["aigw"]["http_headers"] == {
        "X-Tricky": tricky, "X Odd.Name": "v"}


# -- (4) argv, stdin, no secret on disk or in argv ------------------------------

def test_argv_is_the_codex_exec_driver_with_no_prompt_argument(one_run):
    _, obs, _, tmp_path = one_run
    assert obs["argv"] == [
        "exec", "--json", "--skip-git-repo-check", "--ephemeral",
        "-C", str(tmp_path / "ws"),
        "--dangerously-bypass-approvals-and-sandbox", "-"]
    assert "the prompt" not in obs["argv"]
    assert obs["cwd"] == str(tmp_path / "ws")


def test_prompt_arrives_on_stdin(one_run):
    _, obs, _, _ = one_run
    assert obs["stdin"] == "the prompt"


def test_skills_index_is_prepended_to_the_prompt(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "cfg" / "skills" / "demo"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "SKILL.md").write_text("---\nname: demo\ndescription: a demo skill\n---\nbody\n")
    itdir = tmp_path / "it"
    itdir.mkdir()
    (tmp_path / "ws").mkdir(exist_ok=True)
    runner = CodexRunner(tmp_path / "ws", config_dir=tmp_path / "cfg",
                         home_fn=lambda: tmp_path / "home")
    asyncio.run(runner.run("the prompt", itdir / "output.jsonl", model=MODEL_REF, timeout_s=60))
    stdin = json.loads((tmp_path / "observed.json").read_text())["stdin"]
    assert stdin.startswith("Skills are available for this task.")
    assert "- demo: a demo skill (" in stdin
    assert stdin.endswith("\n\nthe prompt")


def test_no_skills_means_the_prompt_alone(one_run):
    _, obs, _, _ = one_run
    assert not obs["stdin"].startswith("Skills are available")


def test_token_reaches_codex_only_through_its_environment_variable(one_run):
    _, obs, itdir, _ = one_run
    assert obs["env"]["AWS_BEARER_TOKEN_BEDROCK"] == FAKE_TOKEN
    assert FAKE_TOKEN not in obs["config_toml"]
    assert not any(FAKE_TOKEN in arg for arg in obs["argv"])
    for f in (itdir / "output.codex-scratch").rglob("*"):
        if f.is_file():
            assert FAKE_TOKEN not in f.read_text(), f
    assert obs["env"]["CODEX_HOME"] == obs["codex_home"]


# -- the wrapper's own seams ---------------------------------------------------

def test_gateway_variable_missing_ends_in_band_without_launching_codex(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    monkeypatch.delenv("AIGW_OPENAI_BASE_URL")
    itdir = tmp_path / "it"
    itdir.mkdir()
    (tmp_path / "ws").mkdir(exist_ok=True)
    result = asyncio.run(CodexRunner(tmp_path / "ws", home_fn=lambda: tmp_path / "home").run(
        "p", itdir / "output.jsonl", model=MODEL_REF, timeout_s=60))
    assert not (tmp_path / "observed.json").exists()
    assert (result.error_message or "").startswith("codex_gateway_not_configured")
    assert "AIGW_OPENAI_BASE_URL" in result.error_message
    assert FAKE_TOKEN not in result.error_message


def test_raw_events_go_to_output_codex_jsonl_and_pi_events_to_output_jsonl(one_run):
    _, _, itdir, _ = one_run
    raw = [json.loads(l) for l in (itdir / "output.codex.jsonl").read_text().splitlines()]
    assert [e["type"] for e in raw][-1] == "turn.completed"
    pi = [json.loads(l) for l in (itdir / "output.jsonl").read_text().splitlines()]
    assert all(e["type"] != "ralphd.codex_raw" for e in pi)
    assert any(e["type"] == "message_end" for e in pi)
