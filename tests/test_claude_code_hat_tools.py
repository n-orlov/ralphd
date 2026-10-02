"""PRD v0.30 D: ralphd's hat tools on the `claude-code` harness.

One declaration (`hat_tools._TOOL_PARAMS` / `_TOOL_DOCS`), two renderings: the
typebox source pi's extension registers, and the JSON Schema the wrapper's
in-process SDK MCP server advertises. A call relays through the same spool and
broker. These tests drive the real wrapper and Agent SDK against the stub
Claude Code CLI; no network and no real token.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from ralphd.engine import hat_tools
from ralphd.engine.runner import ClaudeCodeRunner
from ralphd.engine.state import RunDir, atomic_write_json, read_events

from test_steer import _supervisor

REPO = Path(__file__).resolve().parents[1]
STUB = REPO / "tests" / "stub-claude" / "claude"
MODEL_REF = "anthropic/claude-sonnet-5-5"
HATS = ("steer", "worker", "review")


def _typebox_fields(tool: str) -> tuple[list[str], list[str]]:
    """(all, required) top-level fields of `parameters_source`."""
    src = hat_tools.parameters_source(tool)
    rows = re.findall(r"^      (\w+): (Type\.Optional)?", src, re.M)
    return [n for n, _ in rows], [n for n, opt in rows if not opt]


@pytest.mark.parametrize("tool", sorted(hat_tools._ALL_WIRED_TOOLS))
def test_json_schema_has_the_same_fields_as_the_typebox_and_the_usage(tool):
    schema = hat_tools.json_schema(tool)
    fields, required = _typebox_fields(tool)
    assert schema["type"] == "object"
    assert list(schema["properties"]) == fields
    assert schema.get("required", []) == required
    assert set(schema["properties"]) == set(hat_tools.usage_payload(tool))
    json.dumps(schema)


def test_json_schema_renders_nested_objects_arrays_and_options():
    task = hat_tools.json_schema("task-insert")["properties"]["task"]
    assert task["type"] == "object" and task["additionalProperties"] is True
    assert task["required"] == ["id"]
    assert task["properties"]["id"]["description"].startswith("The new task")
    assert task["properties"]["dependsOn"] == {"type": "array", "items": {"type": "string"}}
    assert task["properties"]["priority"] == {"type": "number"}
    changes = hat_tools.json_schema("task-update")["properties"]["changes"]
    assert "required" not in changes
    assert "status" in changes["properties"]
    assert "engine's one status applier" in changes["properties"]["status"]["description"]


def test_sdk_tool_specs_use_the_extension_description():
    (spec,) = hat_tools.sdk_tool_specs(["notify"])
    doc = hat_tools.STEER_WIRED_TOOLS["notify"]
    assert spec["name"] == "notify"
    assert spec["description"] == (
        doc["description"] + "\n\nArguments (JSON object): " + doc["usage"])
    assert json.dumps(spec["description"]) in hat_tools.extension_source(
        Path("/x"), "python", {"notify": doc})
    assert spec["input_schema"] == hat_tools.json_schema("notify")
    with pytest.raises(KeyError):
        hat_tools.sdk_tool_specs(["no-such-tool"])


def _live_supervisor(tmp_path):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    return sup


def _wire(sup, hat: str):
    tools = {"steer": None, "worker": frozenset(hat_tools.WORKER_WIRED_TOOLS),
             "review": frozenset(hat_tools.REVIEW_WIRED_TOOLS)}[hat]
    broker = hat_tools.SteerToolBroker(sup.run, tools=tools, source=hat)
    broker.place()
    setattr(sup, f"_{hat}_tool_broker", broker)
    return broker


def _registered_names(broker) -> list[str]:
    src = hat_tools.extension_source(
        broker.run.root, "python", {n: hat_tools._ALL_WIRED_TOOLS[n]
                                    for n in broker.wired_tools()})
    return re.findall(r'pi\.registerTool\(\{\s+name: "([a-z-]+)"', src)


def test_tool_names_per_hat_equal_the_pi_extension_and_are_scoped(tmp_path):
    sup = _live_supervisor(tmp_path)
    for phase in (*HATS, "verify", "reflect", "planning"):
        assert sup._hat_tool_names(phase) == ()
    expect = {"steer": set(hat_tools.STEER_WIRED_TOOLS),
              "worker": {"notify"}, "review": {"record-verdict"}}
    for hat in HATS:
        broker = _wire(sup, hat)
        names = sup._hat_tool_names(hat)
        assert list(names) == sorted(names)
        assert set(names) == expect[hat]
        assert set(names) == set(_registered_names(broker))
        # Scoped to its own phase: no other phase sees the broker.
        for other in set(HATS) - {hat}:
            if getattr(sup, f"_{other}_tool_broker", None) is None:
                assert sup._hat_tool_names(other) == ()
    assert "record-verdict" not in sup._hat_tool_names("steer")
    assert "task-insert" not in sup._hat_tool_names("worker")


def _stub_env(mp, tmp_path: Path, **stub) -> Path:
    observe = tmp_path / "observed.json"
    mp.setenv("RALPHD_CLAUDE_CLI_PATH", str(STUB))
    mp.setenv("STUB_CLAUDE_OBSERVE", str(observe))
    for k in [k for k in os.environ if k.startswith("STUB_CLAUDE_")
              and k != "STUB_CLAUDE_OBSERVE"]:
        mp.delenv(k)
    for k, v in stub.items():
        mp.setenv(k, v)
    return observe


async def _run_with_broker(runner, broker, tmp_path, names):
    itdir = tmp_path / "it"
    itdir.mkdir(exist_ok=True)
    serving = asyncio.ensure_future(broker.serve())
    try:
        return await runner.run("the prompt", itdir / "output.jsonl",
                                model=MODEL_REF, timeout_s=60, hat_tools=list(names))
    finally:
        serving.cancel()
        try:
            await serving
        except asyncio.CancelledError:
            pass
        broker.serve_once()


def _runner(tmp_path, run) -> ClaudeCodeRunner:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ClaudeCodeRunner(ws, run=run)


def _drive_hat(monkeypatch, tmp_path, hat: str, calls: list | None = None):
    sup = _live_supervisor(tmp_path)
    broker = _wire(sup, hat)
    stub = {}
    if calls is not None:
        stub = {"STUB_CLAUDE_SCENARIO": "sdk_tool",
                "STUB_CLAUDE_SDK_CALLS": json.dumps(calls)}
    observe = _stub_env(monkeypatch, tmp_path, **stub)
    names = sup._hat_tool_names(hat)
    result = asyncio.run(_run_with_broker(
        _runner(tmp_path, sup.run), broker, tmp_path, names))
    return sup, broker, result, json.loads(observe.read_text()), names


def test_a_steer_call_goes_through_the_broker_and_changes_tasks_json(
        monkeypatch, tmp_path):
    calls = [{"tool": "task-insert",
              "args": {"task": {"id": "t9", "title": "from the claude-code hat"}}}]
    sup, broker, result, obs, names = _drive_hat(monkeypatch, tmp_path, "steer", calls)
    assert result.error_message in (None, "")
    ids = [t["id"] for t in sup.run.read_tasks()["tasks"]]
    assert ids == ["t9"]
    served = read_events(sup.run.root, types=["steer.tool_call"])
    assert [e.get("tool") for e in served] == ["task-insert"]
    assert served[0].get("source") == "steer"
    # The pi transcript names the tool without the prefix.
    transcript = (tmp_path / "it" / "output.jsonl").read_text()
    assert "task-insert" in transcript
    assert "mcp__ralphd__" not in transcript


def test_a_tool_response_is_the_broker_json_never_flagged_as_an_error(
        monkeypatch, tmp_path):
    calls = [{"tool": "task-delete", "args": {"taskId": "absent"}}]
    sup, broker, result, obs, names = _drive_hat(monkeypatch, tmp_path, "steer", calls)
    served = read_events(sup.run.root, types=["steer.tool_call"])
    assert len(served) == 1 and served[0].get("ok") is False
    raw = (tmp_path / "it" / "output.claude.jsonl").read_text()
    results = []
    for line in raw.splitlines():
        for block in (json.loads(line).get("content") or []):
            if isinstance(block, dict) and "tool_use_id" in block:
                results.append(block)
    assert results, raw
    assert all(not b.get("is_error") for b in results)
    assert '\\"ok\\": false' in json.dumps(results)


@pytest.mark.parametrize("hat", HATS)
def test_the_observed_sdk_tools_are_exactly_the_hats_wired_tools(
        monkeypatch, tmp_path, hat):
    sup, broker, result, obs, names = _drive_hat(monkeypatch, tmp_path, hat)
    seen = sorted(t["name"] for t in obs["sdk_tools"])
    assert seen == list(names)
    assert set(seen) == set(_registered_names(broker))
    assert obs["tools"] == ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]
    by_name = {t["name"]: t["inputSchema"] for t in obs["sdk_tools"]}
    for n in names:
        assert by_name[n]["required"] == hat_tools.json_schema(n).get("required", [])
    if hat == "worker":
        assert seen == ["notify"]
    if hat == "review":
        assert seen == ["record-verdict"]
    assert "mcp__ralphd__" in obs["system_prompt"]


def test_no_hat_tools_means_no_sdk_server(monkeypatch, tmp_path):
    sup = _live_supervisor(tmp_path)
    observe = _stub_env(monkeypatch, tmp_path)
    itdir = tmp_path / "it"
    itdir.mkdir()
    asyncio.run(_runner(tmp_path, sup.run).run(
        "p", itdir / "output.jsonl", model=MODEL_REF, timeout_s=60))
    assert json.loads(observe.read_text())["sdk_tools"] == []
