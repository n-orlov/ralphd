"""PRD v0.30 B: the `claude-code` runner (`runner.ClaudeCodeRunner`) and its
wrapper process (`python -m ralphd.harness.claude_code`).

Every test drives the real wrapper, which drives the real Agent SDK, against
the stub Claude Code CLI (`tests/stub-claude/claude`, through
`RALPHD_CLAUDE_CLI_PATH`). The stub dumps what it observed (argv, env, uid,
pid, the config dir's skills) to `STUB_CLAUDE_OBSERVE`. No network, no real
token: the OAuth value below is an obvious fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from ralphd.engine import runner as runner_mod
from ralphd.engine import skills as skills_mod
from ralphd.engine.runner import CLAUDE_CODE_ROOT_REFUSAL, ClaudeCodeRunner, IterationResult
from ralphd.engine.skills import verify_placement
from ralphd.harness.claude_code import BUILTIN_TOOLS, EFFORT_TABLE

REPO = Path(__file__).resolve().parents[1]
STUB = REPO / "tests" / "stub-claude" / "claude"
FAKE_OAUTH = "sk-ant-oat01-FAKE-FOR-TESTS-ONLY"
MODEL_REF = "anthropic/claude-sonnet-5-5"


def _stub_env(mp, tmp_path: Path, **stub) -> Path:
    """Point the wrapper at the stub; returns the observation path."""
    observe = tmp_path / "observed.json"
    mp.setenv("RALPHD_CLAUDE_CLI_PATH", str(STUB))
    mp.setenv("STUB_CLAUDE_OBSERVE", str(observe))
    for k in [k for k in os.environ if k.startswith("STUB_CLAUDE_")
              and k != "STUB_CLAUDE_OBSERVE"]:
        mp.delenv(k)
    for k, v in stub.items():
        mp.setenv(k, v)
    return observe


def _run(runner: ClaudeCodeRunner, tmp_path: Path, name: str = "it", **kw) -> IterationResult:
    itdir = tmp_path / name
    itdir.mkdir(parents=True, exist_ok=True)
    kw.setdefault("model", MODEL_REF)
    kw.setdefault("timeout_s", 60)
    return asyncio.run(runner.run("the prompt", itdir / "output.jsonl", **kw))


def _runner(tmp_path: Path, **kw) -> ClaudeCodeRunner:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ClaudeCodeRunner(ws, **kw)


def _obs(path: Path) -> dict:
    return json.loads(path.read_text())


def _gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


def _wait_gone(pid: int, bound_s: float = 5.0) -> bool:
    deadline = time.monotonic() + bound_s
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return _gone(pid)


# -- one observed run: uid, env, tools, model, flags, final text -------------

@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    """One text-scenario iteration, observed. The spawn kwargs are captured
    and `agent_child_kwargs` returns a sentinel naming this process's own
    uid/gid, so the spawn really drops to the uid it is handed."""
    tmp_path = tmp_path_factory.mktemp("observed")
    sentinel = {"user": os.getuid(), "group": os.getgid()}
    spawned: list[dict] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*cmd, **kwargs):
        spawned.append({"cmd": list(cmd), "kwargs": kwargs})
        return await real_exec(*cmd, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        observe = _stub_env(mp, tmp_path)
        mp.setattr(runner_mod, "agent_child_kwargs", lambda: dict(sentinel))
        mp.setattr(asyncio, "create_subprocess_exec", spy)
        mp.setenv("RALPHD_API_TOKEN", "fake-control-plane-token")
        mp.setenv("ANTHROPIC_API_KEY", "fake-api-key-for-tests")
        mp.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        mp.setenv("CLAUDE_CODE_SESSION_ID", "fake-session")
        mp.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_OAUTH)
        runner = _runner(tmp_path)
        result = _run(runner, tmp_path, extra_env={
            "ANTHROPIC_AUTH_TOKEN": "fake-auth-token-for-tests",
            "RALPHD_CONTROL_TOKEN": "fake-control-token"})
    return {"result": result, "obs": _obs(observe), "spawned": spawned,
            "sentinel": sentinel, "itdir": tmp_path / "it"}


def test_the_wrapper_runs_as_the_agent_uid(observed):
    (spawn,) = observed["spawned"]
    assert spawn["cmd"][1:3] == ["-m", "ralphd.harness.claude_code"]
    assert spawn["kwargs"]["user"] == observed["sentinel"]["user"]
    assert spawn["kwargs"]["group"] == observed["sentinel"]["group"]
    assert spawn["kwargs"]["start_new_session"] is True
    assert observed["obs"]["uid"] == os.getuid()


def test_the_cli_env_is_stripped_and_isolated(observed):
    env = observed["obs"]["env"]
    for name in ("RALPHD_API_TOKEN", "RALPHD_CONTROL_TOKEN", "ANTHROPIC_API_KEY",
                 "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_SESSION_ID"):
        assert name not in env, name
    # The SDK sets its own entrypoint; the inherited `cli` never arrives.
    assert env["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH
    scratch = (observed["itdir"] / "output.claude-scratch").resolve()
    assert Path(env["HOME"]) == scratch / "home"
    assert Path(env["CLAUDE_CONFIG_DIR"]) == scratch / "config"


def test_exactly_the_six_builtin_tools(observed):
    assert observed["obs"]["tools"] == list(BUILTIN_TOOLS)
    assert sorted(BUILTIN_TOOLS) == ["Bash", "Edit", "Glob", "Grep", "Read", "Write"]
    assert observed["obs"]["sdk_tools"] == []


def test_the_model_arrives_without_its_provider_prefix(observed):
    assert observed["obs"]["model"] == "claude-sonnet-5-5"
    assert observed["result"].model == MODEL_REF


def test_isolation_flags_and_no_turn_bound(observed):
    obs = observed["obs"]
    assert obs["setting_sources"] == ""
    assert "--setting-sources=" in obs["argv"]
    assert obs["no_session_persistence"] is True
    assert obs["max_turns"] is None and "--max-turns" not in obs["argv"]
    assert obs["permission_mode"] == "bypassPermissions"
    assert obs["cwd"] == str((observed["itdir"].parent / "ws").resolve())
    assert obs["prompt"] == "the prompt"


def test_saw_complete_and_the_raw_transcript(observed):
    result, itdir = observed["result"], observed["itdir"]
    assert result.harness == "claude-code"
    assert result.exit_code == 0 and not result.error_message
    assert result.saw_complete
    raw = [json.loads(line) for line in
           (itdir / "output.claude.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in raw] == ["SystemMessage", "AssistantMessage",
                                        "ResultMessage"]
    pi = [json.loads(line) for line in (itdir / "output.jsonl").read_text().splitlines()]
    assert all(e["type"] != "ralphd.claude_raw" for e in pi)
    assert pi[-1]["type"] == "message_end"
    # The scratch config and home are gone; the wrapper config and its
    # stderr log stay.
    scratch = itdir / "output.claude-scratch"
    assert sorted(p.name for p in scratch.iterdir()) == ["stderr.log", "wrapper.json"]


def test_saw_complete_from_the_final_result_text(tmp_path, monkeypatch):
    """The marker only in the SDK `result` (not in an assistant message)
    still ends the transcript, so `saw_complete` reads it."""
    script = tmp_path / "script.json"
    sid = "s-1"
    script.write_text(json.dumps([
        {"type": "assistant", "message": {"model": "claude-sonnet-5-5", "id": "m1",
                                          "type": "message", "role": "assistant",
                                          "content": [{"type": "text", "text": "working"}],
                                          "stop_reason": None, "usage": {}},
         "parent_tool_use_id": None, "session_id": sid, "uuid": "u1"},
        {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1,
         "duration_api_ms": 1, "num_turns": 1, "session_id": sid,
         "result": "all done\n<promise>COMPLETE</promise>", "uuid": "u2"},
    ]))
    _stub_env(monkeypatch, tmp_path, STUB_CLAUDE_SCRIPT=str(script))
    result = _run(_runner(tmp_path), tmp_path)
    assert result.saw_complete, result.final_text


# -- refusals ----------------------------------------------------------------

def test_a_root_agent_uid_is_refused_before_spawn(tmp_path, monkeypatch):
    _stub_env(monkeypatch, tmp_path)
    monkeypatch.setattr(runner_mod, "agent_child_kwargs", dict)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    async def no_spawn(*a, **k):
        raise AssertionError("spawned the wrapper as root")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    result = _run(_runner(tmp_path), tmp_path)
    assert result.error_message.startswith(CLAUDE_CODE_ROOT_REFUSAL)
    assert result.exit_code is None and result.duration_s is None
    assert not (tmp_path / "it" / "output.claude-scratch").exists()


def test_a_cli_below_the_minimum_version_is_refused(tmp_path, monkeypatch):
    observe = _stub_env(monkeypatch, tmp_path, STUB_CLAUDE_VERSION="2.1.75")
    result = _run(_runner(tmp_path), tmp_path)
    assert "claude_code_version_too_old" in result.error_message
    assert "2.1.75" in result.error_message
    assert result.exit_code == 1
    assert not observe.exists()  # no query was ever sent


# -- effort --------------------------------------------------------------------

EFFORT_ROWS = [
    # (thinking, effort, observed --effort, observed --thinking)
    ("off", None, None, "disabled"),
    ("minimal", None, "low", None),
    ("low", None, "low", None),
    ("medium", None, "medium", None),
    ("high", None, "high", None),
    ("xhigh", None, "xhigh", None),
    ("high", "low", "high", None),   # thinking wins
]


def test_the_effort_table_covers_every_level():
    assert sorted(EFFORT_TABLE) == sorted(
        ["off", "minimal", "low", "medium", "high", "xhigh"])
    assert {r[0] for r in EFFORT_ROWS} == set(EFFORT_TABLE)


@pytest.mark.parametrize("thinking,effort,want_effort,want_thinking", EFFORT_ROWS,
                         ids=[f"{r[0]}-{r[1]}" for r in EFFORT_ROWS])
def test_effort_mapping(tmp_path, monkeypatch, thinking, effort, want_effort,
                        want_thinking):
    observe = _stub_env(monkeypatch, tmp_path)
    result = _run(_runner(tmp_path), tmp_path, thinking=thinking, effort=effort)
    assert result.exit_code == 0, result.error_message
    obs = _obs(observe)
    assert obs["effort"] == want_effort
    assert obs["thinking"] == want_thinking


# -- the ladder ------------------------------------------------------------------

def test_timeout_kills_the_group(tmp_path, monkeypatch):
    observe = _stub_env(monkeypatch, tmp_path, STUB_CLAUDE_SCENARIO="hang",
                        STUB_CLAUDE_IGNORE_SIGINT="1")
    monkeypatch.setattr(runner_mod, "SHUTDOWN_GRACE_S", 0.5)
    pgids: list[int] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*cmd, **kwargs):
        proc = await real_exec(*cmd, **kwargs)
        pgids.append(proc.pid)  # start_new_session: pid == pgid
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    t0 = time.monotonic()
    result = _run(_runner(tmp_path), tmp_path, timeout_s=3)
    assert result.timed_out
    assert time.monotonic() - t0 < 30
    stub_pid = _obs(observe)["pid"]
    assert _wait_gone(stub_pid), "the stub CLI outlived the iteration"
    (pgid,) = pgids
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_the_startup_watchdog_fires_on_a_silent_cli(tmp_path, monkeypatch):
    _stub_env(monkeypatch, tmp_path, STUB_CLAUDE_SCENARIO="slow_start",
              STUB_CLAUDE_SLEEP_BEFORE_INIT="60")
    monkeypatch.setattr(runner_mod, "SHUTDOWN_GRACE_S", 0.5)
    t0 = time.monotonic()
    result = _run(_runner(tmp_path), tmp_path, timeout_s=120, startup_timeout_s=1.0)
    assert result.no_traffic_timeout
    assert not result.timed_out
    assert time.monotonic() - t0 < 30


def test_interrupt_ends_the_iteration_as_aborted(tmp_path, monkeypatch):
    observe = _stub_env(monkeypatch, tmp_path, STUB_CLAUDE_SCENARIO="hang")
    runner = _runner(tmp_path)

    async def go():
        task = asyncio.ensure_future(runner.run(
            "the prompt", (tmp_path / "it").joinpath("output.jsonl"),
            model=MODEL_REF, timeout_s=60))
        deadline = time.monotonic() + 30
        while not observe.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert runner.interrupt()
        return await task

    (tmp_path / "it").mkdir()
    result = asyncio.run(go())
    assert result.interrupted
    assert result.exit_code == 130
    assert result.error_message == "aborted"


# -- structured evidence -----------------------------------------------------------

def test_rate_limit_is_kept_per_run_and_marked_fresh(tmp_path, monkeypatch):
    _stub_env(monkeypatch, tmp_path, STUB_CLAUDE_RL_STATUS="allowed",
              STUB_CLAUDE_RL_RESETS_AT="1790936400", STUB_CLAUDE_RL_UTIL="0.25")
    runner = _runner(tmp_path)
    first = _run(runner, tmp_path, name="one")
    assert first.rate_limit_fresh
    assert first.rate_limit["status"] == "allowed"
    assert first.rate_limit["unifiedWindows"]["five_hour"]["utilization"] == 0.25
    monkeypatch.delenv("STUB_CLAUDE_RL_STATUS")
    second = _run(runner, tmp_path, name="two")
    assert second.rate_limit == first.rate_limit
    assert not second.rate_limit_fresh


def test_an_error_result_is_in_band_with_status_and_type(tmp_path, monkeypatch):
    _stub_env(monkeypatch, tmp_path, STUB_CLAUDE_SCENARIO="rate_limited")
    result = _run(_runner(tmp_path), tmp_path)
    assert result.http_status == 429
    assert result.error_type == "rate_limit"
    assert result.rate_limit["status"] == "rejected" and result.rate_limit_fresh
    assert result.error_message.startswith("429 rate_limit: API Error: 429")
    assert result.exit_code == 1


# -- skills ------------------------------------------------------------------------

def _skill_sources(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_dir = tmp_path / "cfg"
    skill = config_dir / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: \"Demo skill for tests\"\n---\n# demo\n")
    monkeypatch.setattr(skills_mod, "OVERLAY_DIR", tmp_path / "overlay")
    home = tmp_path / "home"
    pi_dir = home / ".pi" / "agent" / "skills"
    pi_dir.mkdir(parents=True)
    (pi_dir / "demo").symlink_to(skill, target_is_directory=True)
    return config_dir, home


class _Events:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    root = None

    def emit(self, type_, **data):
        self.events.append((type_, data))


def test_skills_are_linked_into_the_config_dir_and_verified(tmp_path, monkeypatch):
    config_dir, home = _skill_sources(tmp_path, monkeypatch)
    observe = _stub_env(monkeypatch, tmp_path)
    events = _Events()
    runner = _runner(tmp_path, run=events, config_dir=lambda: config_dir,
                     home_fn=lambda: home)
    result = _run(runner, tmp_path)
    assert result.exit_code == 0, result.error_message
    obs = _obs(observe)
    assert obs["config_skills"] == ["demo@"]
    link = Path(obs["env"]["CLAUDE_CONFIG_DIR"]) / "skills" / "demo" / "SKILL.md"
    assert "demo: Demo skill for tests" in obs["system_prompt"]
    assert str(link) in obs["system_prompt"]
    assert ("skills.placement.verified", {"names": ["demo"]}) in events.events
    # Cleanup removed the links, never the sources.
    assert not link.parent.parent.exists()
    assert (config_dir / "skills" / "demo" / "SKILL.md").is_file()


def test_verify_placement_covers_the_extra_root(tmp_path, monkeypatch):
    config_dir, home = _skill_sources(tmp_path, monkeypatch)
    target = tmp_path / "claude-config" / "skills"
    assert verify_placement(config_dir, home) == (["demo"], [])
    assert verify_placement(config_dir, home, extra_targets=[target]) == ([], ["demo"])
    assert skills_mod.link_skills(config_dir, home=home, target=target) == ["demo"]
    assert (target / "demo").is_symlink()
    assert Path(os.readlink(target / "demo")).is_absolute()
    assert verify_placement(config_dir, home, extra_targets=[target]) == (["demo"], [])
    (target / "demo").unlink()
    assert verify_placement(config_dir, home, extra_targets=[target]) == ([], ["demo"])

