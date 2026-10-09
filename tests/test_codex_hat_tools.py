"""PRD v0.33 D: ralphd's hat tools on the `codex` harness.

`python -m ralphd.harness.codex_mcp` is a stdio MCP server (newline-delimited
JSON-RPC). It advertises `hat_tools.sdk_tool_specs` for the hat's tools and
relays a `tools/call` through `hat_tools.call` -- the same spool and broker the
claude-code SDK server and pi's extension use. The wrapper's `config.toml`
carries it as `[mcp_servers.ralphd]`. No network and no real token.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest

from ralphd.engine import hat_tools
from ralphd.engine.state import read_events
from ralphd.harness import codex as codex_mod
from ralphd.harness import codex_mcp
from ralphd.harness.codex_normalise import CodexNormaliser

from test_claude_code_hat_tools import HATS, _live_supervisor, _wire
from test_codex_runner import MODEL_REF, _config, _run, _setup

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "ralphd" / "harness" / "codex_mcp.py"


class _Relay:
    """The relay as a child process, spoken to over stdio."""

    def __init__(self, tools: list[str], run_root: Path | None = None):
        env = {k: v for k, v in os.environ.items() if k != hat_tools.RUN_DIR_ENV}
        if run_root is not None:
            env[hat_tools.RUN_DIR_ENV] = str(run_root)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ralphd.harness.codex_mcp", "--tools", ",".join(tools)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env)
        self._id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False) -> int | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return msg.get("id")

    def read(self) -> dict:
        return json.loads(self.proc.stdout.readline())

    def request(self, method: str, params: dict | None = None) -> dict:
        rid = self.send(method, params)
        reply = self.read()
        assert reply["id"] == rid and reply["jsonrpc"] == "2.0"
        return reply

    def close(self) -> None:
        self.proc.stdin.close()
        assert self.proc.wait(timeout=20) == 0


@pytest.fixture
def relay_factory():
    made: list[_Relay] = []

    def make(tools, run_root=None):
        r = _Relay(tools, run_root)
        made.append(r)
        return r

    yield make
    for r in made:
        if r.proc.poll() is None:
            r.proc.kill()
        r.proc.wait()


def _hat_names(tmp_path, hat: str):
    sup = _live_supervisor(tmp_path)
    broker = _wire(sup, hat)
    return sup, broker, sup._hat_tool_names(hat)


# -- (1) initialize / tools/list --------------------------------------------

def test_initialize_answers_with_the_tools_capability_and_the_ralphd_name(relay_factory):
    relay = relay_factory(["notify"])
    reply = relay.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "codex", "version": "0"}})
    result = reply["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "ralphd"
    relay.send("notifications/initialized", notify=True)  # no reply to a notification
    assert relay.request("ping")["result"] == {}
    relay.close()


@pytest.mark.parametrize("hat", HATS)
def test_tools_list_equals_sdk_tool_specs_for_the_hat(tmp_path, relay_factory, hat):
    sup, broker, names = _hat_names(tmp_path, hat)
    assert names
    relay = relay_factory(list(names))
    relay.request("initialize", {})
    tools = relay.request("tools/list")["result"]["tools"]
    specs = hat_tools.sdk_tool_specs(names)
    assert [t["name"] for t in tools] == [s["name"] for s in specs] == list(names)
    for tool, spec in zip(tools, specs):
        assert tool["inputSchema"] == spec["input_schema"]
        assert tool["description"] == spec["description"]
    relay.close()


def test_an_unknown_tool_name_fails_at_start_up():
    proc = subprocess.run(
        [sys.executable, "-m", "ralphd.harness.codex_mcp", "--tools", "no-such-tool"],
        input="", capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and "no-such-tool" in proc.stderr


def test_malformed_unknown_and_unlisted_requests_are_answered_as_errors(relay_factory):
    relay = relay_factory(["notify"])
    relay.proc.stdin.write("{not json\n")
    relay.proc.stdin.flush()
    assert relay.read()["error"]["code"] == codex_mcp.PARSE_ERROR
    assert relay.request("nope/nothing")["error"]["code"] == codex_mcp.METHOD_NOT_FOUND
    reply = relay.request("tools/call", {"name": "task-insert", "arguments": {}})
    assert reply["error"]["code"] == codex_mcp.INVALID_PARAMS
    relay.close()


# -- (2) tools/call lands in the spool, as one text block --------------------

def _call_while_serving(broker, relay, params):
    """Send one tools/call and serve the spool until the answer arrives."""
    box: dict = {}

    def read():
        box["reply"] = relay.read()

    relay.send("tools/call", params)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + 60
    while reader.is_alive() and time.monotonic() < deadline:
        broker.serve_once()
        time.sleep(0.02)
    reader.join(timeout=1)
    return box["reply"]


def test_a_tools_call_lands_in_the_spool_like_the_claude_code_call(tmp_path, relay_factory):
    sup, broker, names = _hat_names(tmp_path, "steer")
    relay = relay_factory(list(names), sup.run.root)
    relay.request("initialize", {})
    args = {"task": {"id": "t9", "title": "from the codex hat"}}
    reply = _call_while_serving(broker, relay, {"name": "task-insert", "arguments": args})
    # The call went through the broker: tasks.json changed, the audit event exists.
    assert [t["id"] for t in sup.run.read_tasks()["tasks"]] == ["t9"]
    served = read_events(sup.run.root, types=["steer.tool_call"])
    assert [e.get("tool") for e in served] == ["task-insert"]
    assert served[0].get("source") == "steer"
    # One text block carrying the broker's JSON response, never an error.
    result = reply["result"]
    assert [b["type"] for b in result["content"]] == ["text"]
    assert not result.get("isError")
    body = json.loads(result["content"][0]["text"])
    assert body["ok"] is True
    relay.close()


def test_a_refused_call_is_the_broker_json_not_a_flagged_error(tmp_path, relay_factory):
    sup, broker, names = _hat_names(tmp_path, "steer")
    relay = relay_factory(list(names), sup.run.root)
    relay.request("initialize", {})
    reply = _call_while_serving(broker, relay, {"name": "task-delete",
                                                "arguments": {"taskId": "absent"}})
    served = read_events(sup.run.root, types=["steer.tool_call"])
    assert len(served) == 1 and served[0].get("ok") is False
    result = reply["result"]
    assert len(result["content"]) == 1 and not result.get("isError")
    assert json.loads(result["content"][0]["text"])["ok"] is False
    relay.close()


def test_the_relay_is_rooted_at_ralphd_hat_tools_run(tmp_path, monkeypatch):
    seen = {}

    def fake_call(root, tool, args, **kw):
        seen.update(root=root, tool=tool, args=args)
        return {"ok": True}

    monkeypatch.setattr(hat_tools, "call", fake_call)
    reply = codex_mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "notify", "arguments": {"text": "hi"}}},
                             ["notify"], tmp_path)
    assert seen == {"root": tmp_path, "tool": "notify", "args": {"text": "hi"}}
    assert reply["result"]["content"] == [{"type": "text", "text": json.dumps({"ok": True})}]
    assert hat_tools.RUN_DIR_ENV == "RALPHD_HAT_TOOLS_RUN"
    assert codex_mcp.main.__module__ == "ralphd.harness.codex_mcp"


# -- (3) config.toml, runner env, normaliser --------------------------------

def test_config_toml_carries_the_ralphd_server_when_the_hat_has_tools(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run_with_run(tmp_path, ["notify"])
    server = _config(obs)["mcp_servers"]["ralphd"]
    assert server["command"] == sys.executable
    assert server["args"][:2] == ["-m", "ralphd.harness.codex_mcp"]
    assert server["args"][-2:] == ["--tools", "notify"]
    assert server["env"]["RALPHD_HAT_TOOLS_RUN"] == str(tmp_path / "run")
    assert server["tool_timeout_sec"] > hat_tools.CLIENT_TIMEOUT_S
    assert "secret" not in obs["config_toml"].lower()


def _run_with_run(tmp_path, names):
    import asyncio

    from ralphd.engine.runner import CodexRunner
    from ralphd.engine.state import RunDir

    run = RunDir(root=tmp_path / "run")
    run.root.mkdir(exist_ok=True)
    itdir = tmp_path / "it"
    itdir.mkdir(exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    runner = CodexRunner(ws, run=run, home_fn=lambda: tmp_path / "home")
    result = asyncio.run(runner.run("the prompt", itdir / "output.jsonl", model=MODEL_REF,
                                    timeout_s=60, hat_tools=list(names)))
    return result, json.loads((tmp_path / "observed.json").read_text()), itdir


def test_the_child_env_names_the_run_and_the_prompt_says_ralphd_namespace(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run_with_run(tmp_path, ["notify"])
    assert "`ralphd` namespace tools" in obs["stdin"]
    assert "notify" in obs["stdin"].split("the prompt")[0]
    assert obs["stdin"].rstrip().endswith("the prompt")
    assert "mcp__ralphd__" not in obs["stdin"]


def test_no_mcp_servers_table_when_the_hat_has_no_tools(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    _, obs, _ = _run(tmp_path)
    assert "mcp_servers" not in _config(obs)
    assert "mcp_servers" not in obs["config_toml"]
    assert "namespace tools" not in obs["stdin"]
    cfg = {"hat_tools": [], "run_root": "/x"}
    assert codex_mod.mcp_servers(cfg) == {}
    assert codex_mod.mcp_servers({"hat_tools": ["notify"]}) == {}
    assert "mcp_servers" not in tomllib.loads(codex_mod.build_config(cfg, "http://fake/v1"))


def test_a_ralphd_mcp_tool_call_item_normalises_to_a_bare_named_pair():
    item = {"id": "i1", "type": "mcp_tool_call", "server": "ralphd", "tool": "task-insert",
            "arguments": {"task": {"id": "t1"}}, "status": "in_progress", "result": None,
            "error": None}
    done = dict(item, status="completed",
                result={"content": [{"type": "text", "text": "{\"ok\": true}"}],
                        "structured_content": None})
    norm = CodexNormaliser(model_ref=MODEL_REF)
    events = norm.feed({"type": "item.started", "item": item})
    events += norm.feed({"type": "item.completed", "item": done})
    text = json.dumps(events)
    assert "task-insert" in text
    assert "mcp__" not in text
    starts = [e for e in events if "task-insert" in json.dumps(e) and e.get("type") ==
              "tool_execution_start"]
    ends = [e for e in events if e.get("type") == "tool_execution_end"]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0]["toolName"] == "task-insert" == ends[0]["toolName"]


# -- (4) the relay's import boundary ----------------------------------------

def test_codex_mcp_imports_no_sdk_and_only_hat_tools_from_the_engine():
    """Every import resolved to an absolute module (the file is in `ralphd.harness`)."""
    modules: list[str] = []
    for node in ast.walk(ast.parse(SRC.read_text())):
        if isinstance(node, ast.Import):
            modules += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:  # `from ..engine import x` -> ralphd.engine
                base = ".".join(["ralphd", *([node.module] if node.module else [])]
                                if node.level == 2 else ["ralphd", "harness",
                                                         *([node.module] if node.module else [])])
            modules += [base] + [f"{base}.{a.name}" for a in node.names]
    assert not [m for m in modules if "claude_agent_sdk" in m or "claude_code" in m]
    engine = {m for m in modules if m.startswith("ralphd.engine")}
    assert "ralphd.engine.hat_tools" in engine
    # `ralphd.engine` appears only as the base of `from ..engine import hat_tools`.
    assert engine <= {"ralphd.engine", "ralphd.engine.hat_tools"}, engine


def test_importing_the_relay_loads_no_sdk():
    code = ("import sys, ralphd.harness.codex_mcp; "
            "bad = [m for m in sys.modules if m.startswith('claude_agent_sdk') "
            "or m == 'ralphd.harness.claude_code']; "
            "sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], timeout=60).returncode == 0
