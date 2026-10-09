"""v0.33 C.1-C.2: the pure Codex-event to pi-event normaliser
(`ralphd.harness.codex_normalise`).

One fixture file per scenario under `tests/fixtures/codex/`, in the event
shapes of the #301 spike comment section 4. No SDK, no network, no tokens.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import state
from ralphd.harness.codex_normalise import CodexNormaliser, is_traffic

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"
REF = "openai/gpt-6.1-sol"


def _load(name):
    return [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]


def _run(name, ref=REF):
    norm = CodexNormaliser(model_ref=ref)
    events = []
    for ev in _load(name):
        events.extend(norm.feed(ev))
    return events + norm.finish(None)


def _types(events):
    return [e["type"] for e in events]


def _updates(events):
    return [e["assistantMessageEvent"] for e in events if e["type"] == "message_update"]


def _ends(events):
    return [e["message"] for e in events if e["type"] == "message_end"]


def test_text_only_transcript():
    events = _run("text_only")
    assert _types(events) == ["message_start", "message_update", "message_update",
                              "message_update", "message_update", "message_end"]
    ups = _updates(events)
    assert [u["type"] for u in ups] == ["thinking_start", "thinking_delta", "text_delta", "text_end"]
    assert ups[1]["delta"] == "thinking about it"
    assert ups[2]["delta"] == "MOCK-DONE" and ups[3]["content"] == "MOCK-DONE"
    (end,) = _ends(events)
    assert end["role"] == "assistant"
    assert end["content"] == [{"type": "thinking", "thinking": "thinking about it"},
                              {"type": "text", "text": "MOCK-DONE"}]
    assert end["stopReason"] == "stop"


def test_message_end_arrives_on_turn_completed_not_before():
    norm = CodexNormaliser(model_ref=REF)
    evs = _load("text_only")
    out = []
    for ev in evs[:-1]:
        out.extend(norm.feed(ev))
    assert "message_end" not in _types(out)
    assert _types(norm.feed(evs[-1])) == ["message_end"]


def test_thread_and_turn_started_produce_no_event():
    norm = CodexNormaliser(model_ref=REF)
    assert norm.feed({"type": "thread.started", "thread_id": "t"}) == []
    assert norm.feed({"type": "turn.started"}) == []
    assert norm.finish(None) == []
    assert not is_traffic({"type": "thread.started", "thread_id": "t"})
    assert not is_traffic({"type": "turn.started"})


def test_tool_call_is_bash_with_args_command():
    events = _run("tool_call")
    (start,) = [e for e in events if e["type"] == "tool_execution_start"]
    (end,) = [e for e in events if e["type"] == "tool_execution_end"]
    assert start["toolName"] == "bash"
    assert start["args"] == {"command": "/bin/bash -lc 'echo hello'"}
    assert end["toolCallId"] == start["toolCallId"] == "item_2"
    assert end["toolName"] == "bash"
    assert end["isError"] is False
    assert end["result"] == "hello\n"


def test_failing_command_is_an_error_with_the_aggregated_output():
    events = _run("failing_command")
    (end,) = [e for e in events if e["type"] == "tool_execution_end"]
    assert end["isError"] is True
    assert end["result"] == "hello\nerr\n"


def test_file_change_is_one_pair_per_change_with_path_and_kind():
    events = _run("file_change")
    starts = [e for e in events if e["type"] == "tool_execution_start"]
    ends = [e for e in events if e["type"] == "tool_execution_end"]
    assert [(s["toolName"], s["args"]) for s in starts] == [
        ("write", {"path": "/abs/patched.txt", "kind": "add"}),
        ("edit", {"path": "/abs/existing.txt", "kind": "update"}),
    ]
    assert [e["toolCallId"] for e in ends] == [s["toolCallId"] for s in starts]
    assert len({s["toolCallId"] for s in starts}) == 2
    assert [e["isError"] for e in ends] == [False, False]


def test_mcp_call_on_server_ralphd_uses_the_bare_tool_name():
    events = _run("mcp_call")
    (start,) = [e for e in events if e["type"] == "tool_execution_start"]
    (end,) = [e for e in events if e["type"] == "tool_execution_end"]
    assert start["toolName"] == "task-insert"
    assert start["args"] == {"title": "fake task"}
    assert end["toolName"] == "task-insert"
    assert end["isError"] is False
    assert end["result"] == {"content": [{"type": "text", "text": "inserted"}]}


def test_mcp_call_on_another_server_keeps_the_server_in_the_name():
    norm = CodexNormaliser(model_ref=REF)
    events = norm.feed({"type": "item.started", "item": {
        "id": "i1", "type": "mcp_tool_call", "server": "other", "tool": "ping",
        "arguments": {}, "status": "in_progress"}})
    assert events[-1]["toolName"] == "mcp__other__ping"


def test_a_message_after_a_tool_item_starts_a_new_assistant_message():
    events = _run("tool_call")
    assert _types(events) == [
        "message_start", "message_update", "message_update",  # reasoning
        "tool_execution_start", "tool_execution_end",
        "message_end",                                         # closes the tool group
        "message_start", "message_update", "message_update",  # agent_message
        "message_end",
    ]
    first, second = _ends(events)
    assert [c["type"] for c in first["content"]] == ["thinking", "toolCall"]
    assert [c["type"] for c in second["content"]] == ["text"]


def test_reasoning_after_a_tool_item_also_starts_a_new_message():
    norm = CodexNormaliser(model_ref=REF)
    events = []
    for ev in _load("failing_command")[:4]:
        events.extend(norm.feed(ev))
    events.extend(norm.feed({"type": "item.completed",
                             "item": {"id": "r", "type": "reasoning", "text": "next"}}))
    assert _types(events).count("message_start") == 2
    assert _types(events).count("message_end") == 1


def test_consecutive_text_items_share_one_message():
    norm = CodexNormaliser(model_ref=REF)
    out = []
    for text in ("one", "two"):
        out.extend(norm.feed({"type": "item.completed",
                              "item": {"id": text, "type": "agent_message", "text": text}}))
    out.extend(norm.finish(None))
    assert _types(out).count("message_start") == 1
    (end,) = _ends(out)
    assert [c["text"] for c in end["content"]] == ["one", "two"]


def test_stop_reason_is_tool_use_with_a_tool_call_and_stop_without():
    assert [m["stopReason"] for m in _ends(_run("tool_call"))] == ["toolUse", "stop"]
    assert [m["stopReason"] for m in _ends(_run("text_only"))] == ["stop"]
    assert [m["stopReason"] for m in _ends(_run("file_change"))] == ["toolUse", "stop"]


def test_a_turn_that_ends_on_a_tool_group_closes_it_as_tool_use():
    norm = CodexNormaliser(model_ref=REF)
    evs = _load("tool_call")
    evs = evs[:-2] + evs[-1:]  # drop the closing agent_message
    out = []
    for ev in evs:
        out.extend(norm.feed(ev))
    (end,) = _ends(out)
    assert end["stopReason"] == "toolUse"
    assert end["usage"]["totalTokens"] == 2500


def test_provider_is_openai_and_model_is_the_ref():
    (msg,) = _ends(_run("text_only"))
    assert msg["provider"] == "openai"
    assert msg["model"] == REF


def test_model_ids_resolves_the_model_ref():
    (msg,) = _ends(_run("text_only"))
    assert state.model_ids(msg["provider"], msg["model"]) == (REF, None)
    bare = _ends(_run("text_only", ref="gpt-6.1-sol"))[0]
    assert state.model_ids(bare["provider"], bare["model"]) == ("openai/gpt-6.1-sol", "gpt-6.1-sol")
