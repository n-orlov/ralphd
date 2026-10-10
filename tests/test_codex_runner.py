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


def _header_values(obs: dict, itdir_unused=None) -> dict:
    """NAME -> value as `codex` sees them: `env_http_headers` names the
    variable, the observed environment holds the value."""
    table = _config(obs)["model_providers"]["aigw"]["env_http_headers"]
    return {name: obs["env"][var] for name, var in table.items()}


def test_profile_header_reaches_codex_through_env_http_headers(one_run):
    _, obs, _, _ = one_run
    provider = _config(obs)["model_providers"]["aigw"]
    assert "http_headers" not in provider
    assert list(provider["env_http_headers"]) == ["X-Jira-ID"]
    assert _header_values(obs) == {"X-Jira-ID": "JIRA-123"}
    assert "JIRA-123" not in obs["config_toml"]


def test_no_headers_means_no_header_tables(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run(tmp_path)
    provider = _config(obs)["model_providers"]["aigw"]
    assert "http_headers" not in provider
    assert "env_http_headers" not in provider


def test_header_values_with_quotes_survive_the_environment_round_trip(tmp_path, monkeypatch):
    tricky = 'a "quoted" \\ value, ünïcode'
    _setup(monkeypatch, tmp_path, headers={"X-Tricky": tricky, "X Odd.Name": "v"})
    _, obs, _ = _run(tmp_path)
    assert _header_values(obs) == {"X-Tricky": tricky, "X Odd.Name": "v"}


# -- a header that carries a credential (the gateway token or any other) -------

SECOND_SECRET = "fake-second-credential-FOR-TESTS-ONLY-9876"


def _credential_headers() -> dict:
    return {
        "Authorization": f"Bearer {FAKE_TOKEN}",          # the gateway token itself
        "X-Api-Key": SECOND_SECRET,                         # an unrelated credential
        "X-Signed": f"sig={SECOND_SECRET};tok={FAKE_TOKEN}",  # both in one value
        "X-Org-Id": "acme-corp",
    }


def _all_files(itdir: Path):
    return [f for f in itdir.rglob("*") if f.is_file()]


def test_credential_headers_never_reach_config_argv_or_scratch_files(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, headers=_credential_headers(),
           STUB_CODEX_SCENARIO="echo_stderr",
           STUB_CODEX_STDERR_LINES=f"sent {FAKE_TOKEN}|saw {SECOND_SECRET} twice {SECOND_SECRET}",
           STUB_CODEX_TEXT=f"header was {SECOND_SECRET}\n<promise>COMPLETE</promise>")
    result, obs, itdir = _run(tmp_path)
    assert not result.error_message
    for secret in (FAKE_TOKEN, SECOND_SECRET):
        assert secret not in obs["config_toml"]
        assert not any(secret in arg for arg in obs["argv"])
        files = _all_files(itdir)
        assert any(f.name == "wrapper.json" for f in files)
        assert any(f.name == "stderr.log" for f in files)
        for f in files:
            assert secret not in f.read_text(errors="replace"), f
    # codex still receives every value, by name, from its environment
    assert _header_values(obs) == _credential_headers()
    provider = _config(obs)["model_providers"]["aigw"]
    assert provider["env_key"] == "AWS_BEARER_TOKEN_BEDROCK"
    assert "http_headers" not in provider
    wrapper = json.loads((itdir / "output.codex-scratch" / "wrapper.json").read_text())
    assert set(wrapper["headers"]) == set(_credential_headers())
    assert all(v.startswith("RALPHD_CODEX_HEADER_") for v in wrapper["headers"].values())


def test_credential_header_values_are_masked_in_the_streams_the_runner_keeps(tmp_path, monkeypatch):
    from ralphd.engine import redact
    _setup(monkeypatch, tmp_path, headers=_credential_headers(),
           STUB_CODEX_SCENARIO="echo_stderr",
           STUB_CODEX_STDERR_LINES=f"saw {SECOND_SECRET}",
           STUB_CODEX_TEXT=f"header was {SECOND_SECRET}\n<promise>COMPLETE</promise>")
    monkeypatch.setattr(redact, "_map", {})  # the engine map knows neither value
    _, _, itdir = _run(tmp_path)
    log = (itdir / "output.codex-scratch" / "stderr.log").read_text()
    assert "[REDACTED:codex-header:X-Api-Key]" in log
    pi = (itdir / "output.jsonl").read_text()
    assert "[REDACTED:codex-header:X-Api-Key]" in pi
    raw = (itdir / "output.codex.jsonl").read_text()
    assert "[REDACTED:codex-header:X-Api-Key]" in raw
    for f in _all_files(itdir):
        assert SECOND_SECRET not in f.read_text(errors="replace"), f


def test_a_header_value_shorter_than_the_secret_floor_is_not_masked_but_still_not_written(
        tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, headers={"X-Tag": "t1"})
    _, obs, itdir = _run(tmp_path)
    assert "t1" not in obs["config_toml"]
    assert _header_values(obs) == {"X-Tag": "t1"}
    wrapper = json.loads((itdir / "output.codex-scratch" / "wrapper.json").read_text())
    assert "t1" not in json.dumps(wrapper)


def test_header_env_splits_names_from_values():
    names, values = codex_mod.header_env({"A": "one", "B": "two"})
    assert set(names) == {"A", "B"}
    assert {values[v] for v in names.values()} == {"one", "two"}
    assert all(not any(val in n for val in values.values()) for n in names.values())


def test_the_wrapper_scrub_keeps_the_header_variables():
    names, values = codex_mod.header_env({"A": "one"})
    env = dict(values, PATH="/bin", CODEX_FOO="x")
    codex_mod.scrub_environ(env)
    assert all(v in env for v in values)
    assert "CODEX_FOO" not in env


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


def _gate_calls(module_file: Path) -> tuple[set[str], set[str], set[str]]:
    """(names `version_gate` calls, names imported from `ralphd.cli.versions`,
    functions the module defines), read from source so the SDK-importing
    claude-code wrapper is never imported here."""
    import ast
    tree = ast.parse(module_file.read_text())
    imported = {a.asname or a.name for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("cli.versions")
                for a in n.names}
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    gate = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "version_gate")
    called = {n.func.id for n in ast.walk(gate)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return called, imported, defined


@pytest.mark.parametrize("module", ["codex.py", "claude_code.py"])
def test_both_harness_gates_compare_with_the_one_shared_helper(module):
    called, imported, defined = _gate_calls(REPO / "src" / "ralphd" / "harness" / module)
    assert "compare_versions" in imported
    assert "compare_versions" in called
    assert "parse_version" not in called
    assert not {"parse_version", "compare_versions"} & defined


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


# -- (6) abort, exit codes, cost and the cap kill (B.4) ---------------------------

import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from ralphd.engine import state as state_mod  # noqa: E402
from ralphd.engine.config import JobConfig  # noqa: E402
from ralphd.engine.pricing import PricingChain, PricingMap  # noqa: E402


def _pi_events(stdout: str) -> list[dict]:
    """The wrapper's stdout lines that are pi events (not raw envelopes)."""
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return [e for e in events if e.get("type") != codex_mod.CODEX_RAW_ENVELOPE_TYPE]


def _wrapper_config(tmp_path: Path, workspace: Path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    cfg = {"model": MODEL_REF, "thinking": None, "effort": None,
           "workspace": str(workspace), "codex_home": str(home), "headers": {},
           "hat_tools": [], "run_root": None, "skills": []}
    path = tmp_path / "wrapper.json"
    path.write_text(json.dumps(cfg))
    return path


def _spawn_wrapper(tmp_path: Path, grace_s: float | None = None) -> subprocess.Popen:
    """The real wrapper as a child of the test. The disposition of SIGINT is
    reset first (runners ignore it and children inherit that)."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    cfg = _wrapper_config(tmp_path, ws)
    if grace_s is None:
        cmd = [sys.executable, "-m", "ralphd.harness.codex", "--config", str(cfg)]
    else:
        code = ("import sys, ralphd.harness.codex as c; "
                f"c.INTERRUPT_GRACE_S = {grace_s!r}; sys.exit(c.main(sys.argv[1:]))")
        cmd = [sys.executable, "-c", code, "--config", str(cfg)]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, cwd=ws, env=dict(os.environ),
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    proc.stdin.write("the prompt")
    proc.stdin.close()
    proc.stdin = None  # closed: `communicate` must not flush it again
    return proc


def _gone(pid: int, timeout_s: float = 15.0) -> bool:
    """True once `pid` no longer exists (a zombie nobody has reaped counts as gone)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return True
        if state in ("Z", "X"):
            return True
        time.sleep(0.05)
    return False


def _first_event_seen(proc: subprocess.Popen) -> str:
    return proc.stdout.readline()


def test_sigint_during_a_stream_aborts_with_exit_130_and_leaves_no_orphan(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    observe = _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="hang",
                     STUB_CODEX_CHILD_PIDFILE=str(pidfile))
    proc = _spawn_wrapper(tmp_path)
    try:
        assert _first_event_seen(proc)
        child, stub = int(pidfile.read_text()), json.loads(observe.read_text())["pid"]
        assert not _gone(child, 0.2)
        proc.send_signal(signal.SIGINT)
        rest, _ = proc.communicate(timeout=30)
        assert proc.returncode == 130
    finally:
        if proc.poll() is None:
            proc.kill()
    last = [e for e in _pi_events(rest) if e.get("type") == "message_end"][-1]
    assert last["message"]["stopReason"] == "aborted"
    assert _gone(stub), "the codex stub outlived the abort"
    assert _gone(child), "the stub's child is an orphan"


def test_a_codex_that_ignores_sigint_is_killed_after_the_grace_and_the_wrapper_exits_130(
        tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    observe = _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="ignore_sigint",
                     STUB_CODEX_CHILD_PIDFILE=str(pidfile))
    proc = _spawn_wrapper(tmp_path, grace_s=1.0)
    try:
        assert _first_event_seen(proc)
        child, stub = int(pidfile.read_text()), json.loads(observe.read_text())["pid"]
        t0 = time.monotonic()
        proc.send_signal(signal.SIGINT)
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.4)  # still inside the grace: the stub survived SIGINT
        rest, _ = proc.communicate(timeout=30)
        assert proc.returncode == 130
        assert time.monotonic() - t0 >= 0.9
    finally:
        if proc.poll() is None:
            proc.kill()
    last = [e for e in _pi_events(rest) if e.get("type") == "message_end"][-1]
    assert last["message"]["stopReason"] == "aborted"
    assert _gone(stub) and _gone(child)


def test_runner_interrupt_ends_the_iteration_aborted(tmp_path, monkeypatch):
    observe = _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="hang")
    runner = CodexRunner(tmp_path / "ws", home_fn=lambda: tmp_path / "home")
    (tmp_path / "ws").mkdir()
    (tmp_path / "it").mkdir()
    transcript = tmp_path / "it" / "output.jsonl"

    async def go():
        task = asyncio.ensure_future(runner.run(
            "the prompt", transcript, model=MODEL_REF, timeout_s=60))
        deadline = time.monotonic() + 30
        while not observe.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        assert runner.interrupt()
        return await task

    result = asyncio.run(go())
    assert result.interrupted
    assert result.exit_code == 130
    ends = [json.loads(line) for line in transcript.read_text().splitlines()
            if '"message_end"' in line]
    assert ends[-1]["message"]["stopReason"] == "aborted"
    assert _gone(json.loads(observe.read_text())["pid"])


def test_exit_0_without_turn_completed_is_an_error_naming_the_missing_event(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="no_terminal")
    result, _, itdir = _run(tmp_path)
    assert result.exit_code == 1
    assert "turn.completed" in result.error_message
    assert "without" in result.error_message
    ends = [json.loads(line) for line in (itdir / "output.jsonl").read_text().splitlines()
            if '"message_end"' in line]
    assert ends[-1]["message"]["stopReason"] == "error"


def test_exit_1_without_turn_failed_carries_the_last_stderr_line(tmp_path, monkeypatch):
    # a codex that dies with a crash message and no JSON terminal event
    crash = tmp_path / "crashing-codex"
    crash.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv: print('codex-cli 0.156.1'); sys.exit(0)\n"
        "sys.stdin.read()\n"
        "print('first complaint', file=sys.stderr)\n"
        "print('fatal: model gateway refused the handshake', file=sys.stderr)\n"
        "sys.exit(1)\n")
    crash.chmod(0o755)
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("RALPHD_CODEX_CLI_PATH", str(crash))
    result, _, _ = _run(tmp_path)
    assert result.exit_code == 1
    assert "exited 1" in result.error_message
    assert "turn.failed" in result.error_message
    assert result.error_message.endswith("fatal: model gateway refused the handshake")
    assert "first complaint" not in result.error_message


def test_a_turn_failed_keeps_its_own_message_and_exit_1(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="failed",
           STUB_CODEX_FAILED_MESSAGE="quota exceeded for stub")
    result, _, _ = _run(tmp_path)
    assert result.exit_code == 1
    assert "quota exceeded for stub" in result.error_message


# -- cost: three models, one metered basis ----------------------------------------

_STUB_USAGE_TOKENS = {"input": 400, "cacheRead": 2000, "output": 100}


def _pricing() -> PricingChain:
    operator = PricingMap.from_config({"models": {
        MODEL_REF: {"input": 2.0, "output": 8.0, "cacheRead": 0.5}}})
    builtin = PricingMap(models={
        "aigw-openai/openai.gpt-6.1-mini":
            PricingMap.from_config({"m": {"input": 1.0, "output": 4.0}}).models["m"]},
        name="builtin-test-table")
    return PricingChain((operator, builtin))


def _priced_run(tmp_path, monkeypatch, model: str, pricing, name: str):
    _setup(monkeypatch, tmp_path)
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / name).mkdir(exist_ok=True)
    runner = CodexRunner(tmp_path / "ws", home_fn=lambda: tmp_path / "home", pricing=pricing)
    result = asyncio.run(runner.run("p", tmp_path / name / "output.jsonl",
                                    model=model, timeout_s=60))
    assert not result.error_message
    cfg = JobConfig(billing={"payer": "gw-team", "basis": "metered", "profile": "cx",
                             "harness": "codex",
                             "subscription_payer": "claude-code subscription"})
    state_mod.stamp_billing(result.usage, *cfg.billing_for("codex"))
    return result.usage


def test_a_model_in_the_pricing_map_gets_costusd(tmp_path, monkeypatch):
    usage = _priced_run(tmp_path, monkeypatch, MODEL_REF, _pricing(), "a")
    assert usage["costUSD"] == pytest.approx(0.0026)  # 400*2 + 100*8 + 2000*0.5 per Mtok
    assert usage["costPriced"] is True
    assert "costDerivedUSD" not in usage
    assert usage["costBasis"] == "metered"
    assert (usage["payer"], usage["basis"]) == ("gw-team", "metered")
    assert usage["costUSD"] > 0 and not usage.get("costFree")
    for key, tokens in _STUB_USAGE_TOKENS.items():
        assert usage[key] == tokens


def test_a_model_only_a_built_in_table_knows_is_derived_like_a_pi_call(tmp_path, monkeypatch):
    pricing = _pricing()
    model = "aigw-openai/openai.gpt-6.1-mini"
    expected = pricing.derive({"input": 400, "output": 100, "cacheRead": 2000}, model)
    assert expected is not None and pricing.table_for(model) == "builtin-test-table"
    usage = _priced_run(tmp_path, monkeypatch, model, pricing, "b")
    assert usage["costDerivedUSD"] == pytest.approx(expected)
    assert usage["costDerived"] is True
    assert usage["costPriced"] is False
    assert "costUSD" not in usage
    assert usage["costBasis"] == "metered"
    assert (usage["payer"], usage["basis"]) == ("gw-team", "metered")


def test_a_model_with_no_rate_is_unpriced_not_free(tmp_path, monkeypatch):
    usage = _priced_run(tmp_path, monkeypatch, "aigw-openai/openai.no-such-model",
                        _pricing(), "c")
    assert usage["costPriced"] is False
    assert usage["costDerived"] is False
    assert "costUSD" not in usage and "costDerivedUSD" not in usage
    assert not usage.get("costFree")
    assert state_mod.cost_status(usage) == "unknown"
    assert usage["costBasis"] == "metered"
    assert (usage["payer"], usage["basis"]) == ("gw-team", "metered")


def test_a_plain_pricing_map_prices_through_the_same_path(tmp_path, monkeypatch):
    operator = PricingMap.from_config({"models": {MODEL_REF: {"input": 1.0, "output": 1.0}}})
    usage = _priced_run(tmp_path, monkeypatch, MODEL_REF, operator, "d")
    assert usage["costUSD"] == pytest.approx(operator.derive(_STUB_USAGE_TOKENS, MODEL_REF))
    assert usage["costBasis"] == "metered"


def test_no_pricing_source_leaves_the_cost_unknown_not_zero(tmp_path, monkeypatch):
    usage = _priced_run(tmp_path, monkeypatch, MODEL_REF, None, "e")
    assert usage["costPriced"] is False
    assert "costUSD" not in usage
    assert usage["costBasis"] == "metered"


# -- the iteration_timeout_s cap kill ---------------------------------------------

_CLAIM_TEXT = 'done\n<promise>COMPLETE</promise>\n{"verdict": "complete", "task_id": "001"}'


def test_a_cap_kill_keeps_the_workspace_edit_and_discards_the_completion_claim(
        tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    # the stub streams a full completion claim (a verdict line AND the
    # whole-plan marker), then hangs until the cap kills it
    observe = _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="hang",
                     STUB_CODEX_HANG_CLAIM="1", STUB_CODEX_TEXT=_CLAIM_TEXT,
                     STUB_CODEX_WRITE_FILE="edit.txt",
                     STUB_CODEX_CHILD_PIDFILE=str(pidfile))
    monkeypatch.setattr(runner_mod, "SHUTDOWN_GRACE_S", 0.5)
    t0 = time.monotonic()
    result, _, itdir = _run(tmp_path, timeout_s=4)
    assert result.timed_out
    assert time.monotonic() - t0 < 40
    # the claim really reached the engine before the cap: both streams carry it
    raw = (itdir / "output.codex.jsonl").read_text()
    assert '"agent_message"' in raw and "<promise>COMPLETE</promise>" in raw
    assert "turn.completed" not in raw
    transcript = (itdir / "output.jsonl").read_text()
    assert '\\"verdict\\": \\"complete\\"' in transcript
    # the edit the agent made before the cap stays in the workspace
    assert (tmp_path / "ws" / "edit.txt").read_text().startswith("written by stub-codex")
    # ...and the claim is discarded: the engine reads the worker verdict from
    # the last line of `final_text` and the plan marker from `final_text`
    assert result.final_text == ""
    assert "<promise>COMPLETE</promise>" not in result.final_text
    assert runner_mod.last_nonblank_line(result.final_text) == ""
    assert result.exit_code != 0
    assert _gone(json.loads(observe.read_text())["pid"])
    assert _gone(int(pidfile.read_text()))


def test_a_claim_on_a_finished_turn_is_kept(tmp_path, monkeypatch):
    # the discard is the cap's alone: the same text on a completed turn stands
    _setup(monkeypatch, tmp_path, STUB_CODEX_TEXT=_CLAIM_TEXT)
    result, _, _ = _run(tmp_path)
    assert not result.timed_out and result.exit_code == 0
    assert json.loads(runner_mod.last_nonblank_line(result.final_text))["verdict"] == "complete"


# -- the live loop builds CodexRunner with the resolved pricing source ------------

def _supervisor(tmp_path, **cfg):
    from ralphd.engine.loop import LoopSupervisor
    from ralphd.engine.state import RunDir
    return LoopSupervisor(JobConfig(run_id="unit", infra_retry_backoff_s=[0.0],
                                    infra_retry_backoff_max_s=0.0, **cfg),
                          RunDir(root=tmp_path / "run"), tmp_path / "ws")


def test_the_supervisor_hands_codex_the_same_pricing_source_as_pi(tmp_path):
    sup = _supervisor(tmp_path, pricing={"models": {MODEL_REF: {"input": 2.0}}})
    codex = sup._runner_for("codex")
    assert isinstance(codex, CodexRunner)
    assert codex.pricing is not None
    assert codex.pricing is sup.runner.pricing
    aws = _supervisor(tmp_path, price_strategy="aws")
    assert isinstance(aws._runner_for("codex").pricing, PricingChain)
    assert aws._runner_for("codex").pricing is aws.runner.pricing
    assert _supervisor(tmp_path)._runner_for("codex").pricing is None


def test_the_supervisor_codex_runner_prices_a_live_call_from_the_operator_map(
        tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "it").mkdir()
    sup = _supervisor(tmp_path, pricing={"models": {
        MODEL_REF: {"input": 2.0, "output": 8.0, "cacheRead": 0.5}}})
    codex = sup._runner_for("codex")
    result = asyncio.run(codex.run("p", tmp_path / "it" / "output.jsonl",
                                   model=MODEL_REF, timeout_s=60))
    assert not result.error_message and result.exit_code == 0
    assert result.usage["costUSD"] == pytest.approx(0.0026)
    assert result.usage["costPriced"] is True
    assert result.usage["costBasis"] == "metered"


# -- B.2.3: stderr.log passes through the engine's scrub_text ---------------------

OTHER_SECRET = "fake-other-secret-value-0123456789"
THIRD_SECRET = "fake-third-secret-value-9876543210"


def _with_map(extra: dict):
    """Context manager: the live redaction map holds `extra` (and is restored)."""
    import contextlib
    from ralphd.engine import redact

    @contextlib.contextmanager
    def cm():
        with redact._lock:
            saved = dict(redact._map)
            redact._map.update(extra)
        try:
            yield
        finally:
            with redact._lock:
                redact._map.clear()
                redact._map.update(saved)
    return cm()


def test_stderr_log_masks_a_non_token_redaction_map_secret(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="echo_stderr",
           STUB_CODEX_STDERR_LINES=f"warn: key={OTHER_SECRET} rejected|plain line")
    with _with_map({OTHER_SECRET: "env:OTHER_KEY"}):
        _, _, itdir = _run(tmp_path)
    log = (itdir / "output.codex-scratch" / "stderr.log").read_text()
    assert OTHER_SECRET not in log
    assert "warn: key=[REDACTED:env:OTHER_KEY] rejected" in log
    assert "plain line" in log
    for f in itdir.rglob("*"):
        if f.is_file():
            assert OTHER_SECRET not in f.read_text(errors="replace"), f


def test_stderr_log_masks_several_secrets_in_one_line_and_keeps_line_order(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="echo_stderr",
           STUB_CODEX_STDERR_LINES=f"first|{OTHER_SECRET} and {THIRD_SECRET} and {OTHER_SECRET}|last")
    with _with_map({OTHER_SECRET: "env:A", THIRD_SECRET: "env:B"}):
        _, _, itdir = _run(tmp_path)
    lines = (itdir / "output.codex-scratch" / "stderr.log").read_text().splitlines()
    assert lines == ["first", "[REDACTED:env:A] and [REDACTED:env:B] and [REDACTED:env:A]", "last"]


def test_stderr_lines_never_reach_the_transcript_or_the_raw_log(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, STUB_CODEX_SCENARIO="echo_stderr",
           STUB_CODEX_STDERR_LINES="ralphd.codex_raw looks like an envelope|ralphd.codex_stderr too")
    _, _, itdir = _run(tmp_path)
    log = (itdir / "output.codex-scratch" / "stderr.log").read_text()
    assert "ralphd.codex_raw looks like an envelope" in log
    assert "ralphd.codex_stderr too" in log
    assert "looks like an envelope" not in (itdir / "output.jsonl").read_text()
    assert "looks like an envelope" not in (itdir / "output.codex.jsonl").read_text()


class _ObservingRunner(CodexRunner):
    """Records `stderr.log` as it stands on disk the moment the child is
    reaped, before anything in `run`'s `finally` could touch it."""

    on_disk_at_exit: str | None = None

    def _after_child(self, pgid, result):
        log = self._stderr_log
        self.on_disk_at_exit = log.read_text() if log and log.is_file() else None
        super()._after_child(pgid, result)


def _observed_run(tmp_path: Path, name: str, **kw) -> tuple[IterationResult, _ObservingRunner, Path]:
    itdir = tmp_path / name
    itdir.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    runner = _ObservingRunner(ws, home_fn=lambda: tmp_path / "home", **kw)
    result = asyncio.run(runner.run("the prompt", itdir / "output.jsonl",
                                    model=MODEL_REF, timeout_s=60))
    return result, runner, itdir


def test_a_wrapper_traceback_is_masked_before_it_reaches_stderr_log(tmp_path, monkeypatch):
    """The wrapper's own traceback (not codex's stderr): `config.toml` is a
    directory, so writing it raises IsADirectoryError naming a path that
    carries a non-token redaction-map secret. The line is already masked when
    the child exits -- nothing scrubs the file after the fact."""
    _setup(monkeypatch, tmp_path)
    name = f"it-{OTHER_SECRET}"
    (tmp_path / name / "output.codex-scratch" / "codex-home" / "config.toml").mkdir(parents=True)
    with _with_map({OTHER_SECRET: "env:OTHER_KEY"}):
        result, runner, itdir = _observed_run(tmp_path, name)
    at_exit = runner.on_disk_at_exit
    assert at_exit is not None
    assert "Traceback (most recent call last)" in at_exit
    assert "IsADirectoryError" in at_exit
    assert OTHER_SECRET not in at_exit
    assert "it-[REDACTED:env:OTHER_KEY]" in at_exit
    assert (itdir / "output.codex-scratch" / "stderr.log").read_text() == at_exit
    assert result.exit_code != 0
    assert "IsADirectoryError" not in (itdir / "output.jsonl").read_text()


def test_raw_fd2_writes_of_the_wrapper_process_are_masked_before_they_reach_stderr_log(tmp_path, monkeypatch):
    """What the wrapper's process writes straight to fd 2, below Python's
    `sys.stderr` (an interpreter fatal error, a crash before `main`), reaches
    the runner on the merged pipe as a line that is not a JSON object: a
    plain line, a JSON scalar, a JSON array and a trailing partial line
    without a newline all land in `stderr.log` masked, and none of them in
    the transcript."""
    _setup(monkeypatch, tmp_path)
    fake = tmp_path / "fake-python"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf 'Fatal Python error: key {OTHER_SECRET}\\n' >&2\n"
        "printf '42\\n' >&2\n"
        f"printf '[\"{THIRD_SECRET}\"]\\n' >&2\n"
        f"printf 'tail {OTHER_SECRET}' >&2\n"
        "exit 1\n")
    fake.chmod(0o755)
    with _with_map({OTHER_SECRET: "env:A", THIRD_SECRET: "env:B"}):
        result, runner, itdir = _observed_run(tmp_path, "it", python_bin=str(fake))
    at_exit = runner.on_disk_at_exit
    assert at_exit is not None
    assert at_exit.splitlines() == ["Fatal Python error: key [REDACTED:env:A]", "42",
                                    '["[REDACTED:env:B]"]', "tail [REDACTED:env:A]"]
    transcript = (itdir / "output.jsonl").read_text()
    assert "Fatal Python error" not in transcript and "tail" not in transcript
    for f in itdir.rglob("*"):
        if f.is_file():
            text = f.read_text(errors="replace")
            assert OTHER_SECRET not in text and THIRD_SECRET not in text, f


def test_the_wrapper_never_writes_its_own_stderr_outside_the_envelopes(tmp_path, monkeypatch):
    """Run the wrapper directly: its Python-level traceback leaves only as
    `ralphd.codex_stderr` envelopes on stdout (whole lines, each a JSON
    object of its own), and its fd 2 stays empty."""
    import subprocess
    import sys
    _setup(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _wrapper_config(tmp_path, ws)
    (tmp_path / "codex-home" / "config.toml").mkdir()
    proc = subprocess.run([sys.executable, "-m", "ralphd.harness.codex", "--config", str(cfg)],
                          input="the prompt", capture_output=True, text=True, cwd=ws,
                          env=dict(os.environ), timeout=60)
    assert proc.returncode != 0
    assert proc.stderr == ""
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    lines = [e["line"] for e in events if e.get("type") == codex_mod.CODEX_STDERR_ENVELOPE_TYPE]
    text = "".join(lines)
    assert text.startswith("Traceback (most recent call last)")
    assert "IsADirectoryError" in text
    assert all(line.endswith("\n") for line in lines[:-1])
