"""The stub Claude Code CLI (`tests/stub-claude/claude`) driven by the real
`claude_agent_sdk.query()` in-process, one test per scenario. No network."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from claude_agent_sdk._errors import ResultError

STUB = Path(__file__).resolve().parent / "stub-claude" / "claude"
BUILTIN_TOOLS = ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]


def _drive(tmp_path, env=None, tools_cb=None, prompt="hello"):
    """Run the SDK against the stub. Returns (messages, error, observed)."""
    observe = tmp_path / "observed.json"
    calls: list = []

    @tool("task-insert", "insert a task", {"title": str})
    async def task_insert(args):
        calls.append(args)
        return {"content": [{"type": "text", "text": f"inserted {args['title']}"}]}

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    opts = ClaudeAgentOptions(
        cli_path=str(STUB), tools=BUILTIN_TOOLS,
        mcp_servers={"ralphd": create_sdk_mcp_server("ralphd", tools=[task_insert])},
        setting_sources=[], permission_mode="bypassPermissions",
        model="claude-sonnet-5-5", effort="low", cwd=str(ws),
        env={"STUB_CLAUDE_OBSERVE": str(observe),
             "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1", **(env or {})},
        extra_args={"no-session-persistence": None},
    )
    msgs: list = []
    err: list = []

    async def main():
        try:
            async for m in query(prompt=prompt, options=opts):
                msgs.append(m)
        except ResultError as e:
            err.append(e)

    anyio.run(main)
    obs = json.loads(observe.read_text()) if observe.exists() else None
    return msgs, (err[0] if err else None), obs, calls


def _result(msgs):
    return [m for m in msgs if isinstance(m, ResultMessage)][-1]


def test_version_probe():
    out = subprocess.run([str(STUB), "-v"], capture_output=True, text=True, timeout=20)
    assert out.stdout.strip() == "2.1.286 (Claude Code)"
    out = subprocess.run([str(STUB), "--version"], capture_output=True, text=True, timeout=20,
                         env={**os.environ, "STUB_CLAUDE_VERSION": "2.1.75"})
    assert out.stdout.strip() == "2.1.75 (Claude Code)"


def test_text_scenario_and_observation(tmp_path):
    msgs, err, obs, _ = _drive(tmp_path, prompt="the prompt")
    assert err is None
    res = _result(msgs)
    assert res.result == "done\n<promise>COMPLETE</promise>"
    assert res.total_cost_usd == pytest.approx(0.0444998)
    assert obs["prompt"] == "the prompt"
    assert obs["tools"] == BUILTIN_TOOLS
    assert [t["name"] for t in obs["sdk_tools"]] == ["task-insert"]
    assert obs["sdk_tools"][0]["inputSchema"]
    assert obs["model"] == "claude-sonnet-5-5"
    assert obs["effort"] == "low"
    assert obs["permission_mode"] == "bypassPermissions"
    assert obs["no_session_persistence"] is True
    assert obs["max_turns"] is None
    assert obs["uid"] == os.getuid()
    assert obs["cwd"].endswith("ws")
    assert obs["setting_sources"] == ""


def test_final_text_cost_and_usage_overrides(tmp_path):
    msgs, err, _, _ = _drive(tmp_path, env={
        "STUB_CLAUDE_FINAL_TEXT": "custom", "STUB_CLAUDE_COST": "1.5",
        "STUB_CLAUDE_USAGE": json.dumps({"input_tokens": 7, "output_tokens": 9})})
    assert err is None
    res = _result(msgs)
    assert res.result == "custom"
    assert res.total_cost_usd == 1.5
    assert res.usage["input_tokens"] == 7


def test_full_scenario(tmp_path):
    msgs, err, _, _ = _drive(tmp_path, env={
        "STUB_CLAUDE_SCENARIO": "full", "STUB_CLAUDE_TOOL_OUTPUT": "secret-output-xyz"})
    assert err is None
    kinds = [type(b).__name__ for m in msgs if isinstance(m, AssistantMessage) for b in m.content]
    assert kinds[0] == "ThinkingBlock"
    names = [b.name for m in msgs if isinstance(m, AssistantMessage)
             for b in m.content if type(b).__name__ == "ToolUseBlock"]
    assert names == ["Bash", "Read"]
    assert "secret-output-xyz" in repr(msgs)
    assert _result(msgs).is_error is False


def test_sdk_tool_reaches_in_process_handler(tmp_path):
    calls_env = json.dumps([{"tool": "task-insert", "args": {"title": "from-stub"}}])
    msgs, err, obs, calls = _drive(tmp_path, env={
        "STUB_CLAUDE_SCENARIO": "sdk_tool", "STUB_CLAUDE_SDK_CALLS": calls_env})
    assert err is None
    assert calls == [{"title": "from-stub"}]
    assert "inserted from-stub" in repr(msgs)


def test_error_scenario(tmp_path):
    msgs, err, _, _ = _drive(tmp_path, env={"STUB_CLAUDE_SCENARIO": "error"})
    res = _result(msgs)
    assert res.is_error and res.api_error_status == 500
    assert err is not None


def test_error_knobs(tmp_path):
    msgs, err, _, _ = _drive(tmp_path, env={
        "STUB_CLAUDE_SCENARIO": "error", "STUB_CLAUDE_ERROR_STATUS": "401",
        "STUB_CLAUDE_ERROR_TYPE": "authentication_failed",
        "STUB_CLAUDE_ERROR_MESSAGE": "API Error: 401 nope"})
    res = _result(msgs)
    assert res.api_error_status == 401 and res.result == "API Error: 401 nope"
    asst = [m for m in msgs if isinstance(m, AssistantMessage)][-1]
    assert asst.error == "authentication_failed"
    assert err is not None


def test_refusal_scenario(tmp_path):
    msgs, err, _, _ = _drive(tmp_path, env={"STUB_CLAUDE_SCENARIO": "refusal"})
    res = _result(msgs)
    assert res.is_error and res.api_error_status == 400
    assert "Third-party apps now draw from your extra usage" in res.result
    assert err is not None


def test_rate_limited_raises_after_result(tmp_path):
    msgs, err, _, _ = _drive(tmp_path, env={"STUB_CLAUDE_SCENARIO": "rate_limited"})
    assert err is not None
    res = _result(msgs)
    assert res.is_error and res.api_error_status == 429
    assert res.terminal_reason == "api_error"
    asst = [m for m in msgs if isinstance(m, AssistantMessage)][-1]
    assert asst.error == "rate_limit"
    ev = [m for m in msgs if type(m).__name__ == "RateLimitEvent"][0]
    assert ev.rate_limit_info.status == "rejected"


def test_rate_limit_event_iso_resets_at_passes_through(tmp_path):
    msgs, err, _, _ = _drive(tmp_path, env={
        "STUB_CLAUDE_RL_STATUS": "allowed_warning",
        "STUB_CLAUDE_RL_RESETS_AT": "2026-10-02T12:00:00Z",
        "STUB_CLAUDE_RL_UTIL": "0.91", "STUB_CLAUDE_RL_TOPLEVEL_UTIL": "1"})
    assert err is None
    info = [m for m in msgs if type(m).__name__ == "RateLimitEvent"][0].rate_limit_info
    assert info.status == "allowed_warning"
    assert info.resets_at == "2026-10-02T12:00:00Z"
    assert info.rate_limit_type == "five_hour"


def test_rate_limit_event_epoch_and_no_event_when_unset(tmp_path):
    msgs, _, _, _ = _drive(tmp_path)
    assert not [m for m in msgs if type(m).__name__ == "RateLimitEvent"]
    msgs, _, _, _ = _drive(tmp_path, env={
        "STUB_CLAUDE_RL_STATUS": "allowed", "STUB_CLAUDE_RL_RESETS_AT": "1790936400"})
    info = [m for m in msgs if type(m).__name__ == "RateLimitEvent"][0].rate_limit_info
    assert info.resets_at == 1790936400


def test_script_scenario(tmp_path):
    script = tmp_path / "script.json"
    script.write_text(json.dumps([
        {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1,
         "duration_api_ms": 1, "num_turns": 1, "session_id": "s", "result": "scripted",
         "uuid": "u-1"}]))
    msgs, err, _, _ = _drive(tmp_path, env={"STUB_CLAUDE_SCRIPT": str(script)})
    assert err is None and _result(msgs).result == "scripted"


def test_slow_start_delays_before_any_frame(tmp_path):
    import time
    t0 = time.monotonic()
    msgs, err, _, _ = _drive(tmp_path, env={
        "STUB_CLAUDE_SCENARIO": "slow_start", "STUB_CLAUDE_SLEEP_BEFORE_INIT": "0.5"})
    assert time.monotonic() - t0 >= 0.5
    assert err is None and _result(msgs).is_error is False


def test_hang_exits_130_on_sigint(tmp_path):
    import signal
    import time
    proc = subprocess.Popen(
        [sys.executable, str(STUB)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        env={**os.environ, "STUB_CLAUDE_SCENARIO": "hang", "STUB_CLAUDE_OBSERVE": str(tmp_path / "o.json")},
        text=True)
    try:
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert json.loads(line)["subtype"] == "init"
        time.sleep(0.2)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=20) == 130
    finally:
        if proc.poll() is None:
            proc.kill()


def test_every_stub_env_knob_is_documented():
    source = STUB.read_text()
    doc = source.split('"""', 2)[1]
    used = set(re.findall(r"STUB_CLAUDE_[A-Z_]+", source.split('"""', 2)[2]))
    missing = sorted(k for k in used if k not in doc)
    assert not missing, f"undocumented knobs: {missing}"
    assert os.access(STUB, os.X_OK)
