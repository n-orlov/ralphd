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
import inspect
import json
import os
import site
import time
import tomllib
from pathlib import Path

import pytest

from ralphd import collapse_index
from ralphd.cli import log_render
from ralphd.engine import config as engine_config
from ralphd.engine import runner as runner_mod
from ralphd.engine.runner import CodexRunner, IterationResult
from ralphd.harness import codex as codex_mod
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
    obs_path = tmp_path / "observed.json"
    observed = json.loads(obs_path.read_text()) if obs_path.exists() else {}
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

@pytest.mark.parametrize("missing", ["AIGW_OPENAI_BASE_URL", "AWS_BEARER_TOKEN_BEDROCK"])
def test_gateway_variable_missing_ends_in_band_without_launching_codex(
        tmp_path, monkeypatch, missing):
    _setup(monkeypatch, tmp_path)
    monkeypatch.delenv(missing)
    itdir = tmp_path / "it"
    itdir.mkdir()
    (tmp_path / "ws").mkdir(exist_ok=True)
    result = asyncio.run(CodexRunner(tmp_path / "ws", home_fn=lambda: tmp_path / "home").run(
        "p", itdir / "output.jsonl", model=MODEL_REF, timeout_s=60))
    assert not (tmp_path / "observed.json").exists()
    assert (result.error_message or "").startswith("codex_gateway_not_configured")
    assert missing in result.error_message
    assert FAKE_TOKEN not in result.error_message
    ends = [json.loads(l) for l in (itdir / "output.jsonl").read_text().splitlines()
            if '"message_end"' in l]
    assert ends[-1]["message"]["stopReason"] == "error"
    assert ends[-1]["message"]["errorMessage"].startswith("codex_gateway_not_configured")


# -- B.2: the child environment and the secret --------------------------------

def test_child_environment_is_scrubbed_and_keeps_the_gateway(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_FOO",
                 "OPENAI_ORG_ID", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(name, "fake-" + name.lower())
    _, obs, _ = _run(tmp_path)
    env = obs["env"]
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_FOO",
                 "OPENAI_ORG_ID", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        assert name not in env, name
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == FAKE_TOKEN
    assert env["AIGW_OPENAI_BASE_URL"] == FAKE_BASE_URL
    assert env["CODEX_HOME"] == obs["codex_home"]
    assert not any(k.startswith(("CODEX_", "OPENAI_")) and k != "CODEX_HOME" for k in env)


def test_scrub_list_covers_the_claude_code_gateway_routing():
    """The claude-code wrapper's strip list, minus the gateway token codex
    needs, is a subset of codex's (data mirrored, not imported)."""
    from ralphd.harness import claude_code, codex
    cc = set(claude_code.STRIPPED_ENV) - {"AWS_BEARER_TOKEN_BEDROCK"}
    assert cc <= set(codex.STRIPPED_ENV)
    assert set(claude_code.STRIPPED_ENV_PREFIXES) <= set(codex.STRIPPED_ENV_PREFIXES)
    env = {n: "x" for n in codex.STRIPPED_ENV | {"CODEX_X", "OPENAI_X", "CLAUDE_CODE_USE_BEDROCK",
                                                  "PATH", "AIGW_OPENAI_BASE_URL",
                                                  "AWS_BEARER_TOKEN_BEDROCK"}}
    codex.scrub_environ(env)
    assert sorted(env) == ["AIGW_OPENAI_BASE_URL", "AWS_BEARER_TOKEN_BEDROCK", "PATH"]


def test_the_token_is_masked_in_every_artifact_the_runner_leaves(tmp_path, monkeypatch):
    from ralphd.engine import redact
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="echo_token")
    saved = redact.live_redaction_map()
    redact.refresh_redaction_map(tmp_path / "home")
    assert FAKE_TOKEN in redact.live_redaction_map()
    try:
        _, obs, itdir = _run(tmp_path)
    finally:
        with redact._lock:
            redact._map.clear()
            redact._map.update(saved)
    assert obs["env"]["AWS_BEARER_TOKEN_BEDROCK"] == FAKE_TOKEN
    files = [itdir / "output.jsonl", itdir / "output.codex.jsonl",
             itdir / "output.codex-scratch" / "stderr.log"]
    for f in files:
        assert f.is_file(), f
        text = f.read_text()
        assert FAKE_TOKEN not in text, f
        assert "[REDACTED:env:AWS_BEARER_TOKEN_BEDROCK]" in text, f
    for f in itdir.rglob("*"):
        if f.is_file():
            assert FAKE_TOKEN not in f.read_text(errors="replace"), f


def test_the_wrapper_masks_the_token_itself_when_the_engine_map_is_empty(tmp_path, monkeypatch):
    """The wrapper's own mask is the second layer: with no engine redaction
    map live, the artifacts still hold no token."""
    from ralphd.engine import redact
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="echo_token")
    monkeypatch.setattr(redact, "_map", {})
    _, _, itdir = _run(tmp_path)
    for f in itdir.rglob("*"):
        if f.is_file():
            assert FAKE_TOKEN not in f.read_text(errors="replace"), f


def test_raw_events_go_to_output_codex_jsonl_and_pi_events_to_output_jsonl(one_run):
    _, _, itdir, _ = one_run
    raw = [json.loads(l) for l in (itdir / "output.codex.jsonl").read_text().splitlines()]
    assert [e["type"] for e in raw][-1] == "turn.completed"
    pi = [json.loads(l) for l in (itdir / "output.jsonl").read_text().splitlines()]
    assert all(e["type"] != "ralphd.codex_raw" for e in pi)
    assert any(e["type"] == "message_end" for e in pi)


# -- (3) the version gate (B.3) -----------------------------------------------

def test_the_minimum_codex_version_is_pinned():
    assert engine_config.CODEX_MIN_VERSION == "0.156.1"


def test_a_codex_cli_below_the_minimum_ends_in_band_without_a_turn(tmp_path, monkeypatch):
    observe = _setup(monkeypatch, tmp_path, STUB_CODEX_VERSION="0.156.0")
    result, _, itdir = _run(tmp_path)
    assert result.error_message.startswith("codex_version_too_old")
    assert "0.156.0" in result.error_message
    assert "0.156.1" in result.error_message
    assert result.exit_code == 1
    assert not observe.exists()  # no turn was ever started
    ends = [json.loads(l) for l in (itdir / "output.jsonl").read_text().splitlines()
            if '"message_end"' in l]
    assert ends[-1]["message"]["stopReason"] == "error"


def test_an_old_minor_with_big_patch_is_still_too_old(tmp_path, monkeypatch):
    """Numeric, not textual: 0.99.9 < 0.156.1 although '9' > '1' as text."""
    _setup(monkeypatch, tmp_path, STUB_CODEX_VERSION="0.99.9")
    result, _, _ = _run(tmp_path)
    assert result.error_message.startswith("codex_version_too_old")


def test_a_missing_codex_binary_is_codex_cli_not_found(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("RALPHD_CODEX_CLI_PATH", str(tmp_path / "no-such-dir" / "codex"))
    result, obs, _ = _run(tmp_path)
    assert result.error_message.startswith("codex_cli_not_found")
    assert str(tmp_path / "no-such-dir" / "codex") in result.error_message
    assert result.exit_code == 1
    assert obs == {}


@pytest.mark.parametrize("version", ["0.156.1", "0.156.2", "0.157.0", "1.0.0"])
def test_exactly_the_minimum_and_newer_run(tmp_path, monkeypatch, version):
    _setup(monkeypatch, tmp_path, STUB_CODEX_VERSION=version)
    result, obs, _ = _run(tmp_path)
    assert not result.error_message
    assert result.exit_code == 0
    assert obs["argv"][0] == "exec"


def test_the_cli_path_defaults_to_codex_on_path(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").symlink_to(STUB)
    monkeypatch.delenv("RALPHD_CODEX_CLI_PATH")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert codex_mod.resolve_cli_path() == "codex"
    result, obs, _ = _run(tmp_path)
    assert not result.error_message
    assert obs["argv"][0] == "exec"


def test_no_codex_anywhere_is_codex_cli_not_found(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.delenv("RALPHD_CODEX_CLI_PATH")
    monkeypatch.setenv("PATH", str(empty))
    result, _, _ = _run(tmp_path)
    assert result.error_message.startswith("codex_cli_not_found")


def test_the_version_prefix_and_suffix_are_tolerated():
    gate = codex_mod.version_gate
    assert codex_mod.cli_version("/nonexistent/codex") is None
    assert gate("codex", "0.156.1") is None
    assert gate("codex", "0.200.3") is None
    assert gate("codex", "0.156.0")["type"] == "codex_version_too_old"


def test_an_unreadable_version_is_refused_not_assumed_fine(tmp_path):
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\necho banner without a number\n")
    cli.chmod(0o755)
    assert codex_mod.cli_version(str(cli)) is None
    assert codex_mod.version_gate(str(cli), None)["type"] == "codex_version_too_old"


def test_the_gate_uses_the_shared_comparison_helper():
    from ralphd.cli import versions
    assert codex_mod.compare_versions is versions.compare_versions
    src = inspect.getsource(codex_mod)
    assert "def parse_version" not in src
    assert "def compare_versions" not in src


# -- (4) the child path: agent uid and the startup watchdog (B.3) --------------

def test_the_wrapper_runs_as_the_agent_uid_privsep_selects(tmp_path, monkeypatch):
    sentinel = {"user": os.getuid(), "group": os.getgid()}
    spawned: list[dict] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*cmd, **kwargs):
        spawned.append({"cmd": list(cmd), "kwargs": kwargs})
        return await real_exec(*cmd, **kwargs)

    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(runner_mod, "agent_child_kwargs", lambda: dict(sentinel))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    result, obs, _ = _run(tmp_path)
    assert not result.error_message
    (spawn,) = spawned
    assert spawn["cmd"][1:3] == ["-m", "ralphd.harness.codex"]
    assert spawn["kwargs"]["user"] == sentinel["user"]
    assert spawn["kwargs"]["group"] == sentinel["group"]
    assert spawn["kwargs"]["start_new_session"] is True
    assert obs["uid"] == sentinel["user"]


def test_the_startup_watchdog_fires_on_a_silent_codex(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="hang", STUB_CODEX_SILENT="1")
    monkeypatch.setattr(runner_mod, "SHUTDOWN_GRACE_S", 0.5)
    t0 = time.monotonic()
    result, _, _ = _run(tmp_path, timeout_s=120, startup_timeout_s=1.0)
    assert result.no_traffic_timeout
    assert not result.timed_out
    assert time.monotonic() - t0 < 30


# -- (5) the two output files (B.3) ---------------------------------------------

@pytest.fixture
def tools_run(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="tools")
    return _run(tmp_path)


def test_a_run_leaves_unwrapped_raw_events_and_pi_events(tools_run):
    result, _, itdir = tools_run
    assert not result.error_message
    raw = [json.loads(l) for l in (itdir / "output.codex.jsonl").read_text().splitlines()]
    pi = [json.loads(l) for l in (itdir / "output.jsonl").read_text().splitlines()]
    assert raw and pi
    # Codex's own event vocabulary, never the wrapper envelope.
    assert all("raw" not in e and e["type"] != "ralphd.codex_raw" for e in raw)
    assert raw[0]["type"] == "thread.started"
    assert {"item.started", "item.completed", "turn.completed"} <= {e["type"] for e in raw}
    # pi's vocabulary, never a codex event.
    assert not {e["type"] for e in pi} & {"thread.started", "turn.completed", "item.completed"}
    assert {"message_end", "tool_execution_start"} <= {e["type"] for e in pi}


def test_output_jsonl_parses_through_log_render_and_collapse_index(tools_run):
    _, _, itdir = tools_run
    out = itdir / "output.jsonl"
    text = log_render.render_to_text(out.read_text(), False, log_render.new_render_state())
    assert text.strip()
    index = collapse_index.compute_collapse_index(out)
    assert index["transcriptBytes"] == out.stat().st_size
    assert index["segments"]
