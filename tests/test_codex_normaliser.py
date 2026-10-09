"""v0.33 C.1-C.2: the pure Codex-event to pi-event normaliser
(`ralphd.harness.codex_normalise`).

One fixture file per scenario under `tests/fixtures/codex/`, in the event
shapes of the #301 spike comment section 4. No SDK, no network, no tokens.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import state
from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.harness.codex_normalise import CodexNormaliser, is_traffic
from tests.conftest import scripted_supervisor

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


# ---------------------------------------------------------------- C.3 usage
def _usage_event(**fields):
    return {"type": "turn.completed", "usage": fields}


def _one_message_usage(**fields):
    norm = CodexNormaliser(model_ref=REF)
    (end,) = [e for e in norm.feed(_usage_event(**fields)) if e["type"] == "message_end"]
    return end["message"]["usage"]


def test_usage_fields_map_to_pi_usage():
    usage = _one_message_usage(input_tokens=1000, cached_input_tokens=600,
                               cache_write_input_tokens=100, output_tokens=50,
                               reasoning_output_tokens=20)
    assert usage == {"input": 300, "cacheRead": 600, "cacheWrite": 100,
                     "output": 50, "totalTokens": 1050}


def test_missing_usage_fields_count_as_zero():
    assert _one_message_usage(input_tokens=10, output_tokens=5) == {
        "input": 10, "cacheRead": 0, "cacheWrite": 0, "output": 5, "totalTokens": 15}
    assert _one_message_usage() == {
        "input": 0, "cacheRead": 0, "cacheWrite": 0, "output": 0, "totalTokens": 0}
    norm = CodexNormaliser(model_ref=REF)
    (end,) = [e for e in norm.feed({"type": "turn.completed"}) if e["type"] == "message_end"]
    assert end["message"]["usage"]["totalTokens"] == 0


def test_second_turn_reports_the_delta_of_cumulative_usage():
    norm = CodexNormaliser(model_ref=REF)
    ends = []
    for text, usage in (("one", dict(input_tokens=1000, cached_input_tokens=400, output_tokens=50)),
                        ("two", dict(input_tokens=2600, cached_input_tokens=1500,
                                     cache_write_input_tokens=30, output_tokens=130))):
        out = norm.feed({"type": "item.completed",
                         "item": {"id": text, "type": "agent_message", "text": text}})
        out += norm.feed(_usage_event(**usage))
        ends.extend(e for e in out if e["type"] == "message_end")
    first, second = (e["message"]["usage"] for e in ends)
    assert first == {"input": 600, "cacheRead": 400, "cacheWrite": 0, "output": 50, "totalTokens": 1050}
    # delta: input_tokens 1600, cached 1100, written 30, output 80
    assert second == {"input": 470, "cacheRead": 1100, "cacheWrite": 30, "output": 80, "totalTokens": 1680}
    assert first["totalTokens"] + second["totalTokens"] == 2600 + 130


def test_message_end_carries_no_cost_block():
    events = _run("text_only")
    for end in _ends(events):
        assert "cost" not in end
        assert "cost" not in end["usage"]
    for ev in events:
        assert "cost" not in ev
        assert "cost" not in ev.get("message", {})


# ------------------------------------------------------------- C.4 failure
def _failed(message):
    norm = CodexNormaliser(model_ref=REF)
    out = norm.feed({"type": "item.completed",
                     "item": {"id": "a", "type": "agent_message", "text": "partial text"}})
    out += norm.feed({"type": "turn.failed", "error": {"message": message}})
    return out


def test_turn_failed_is_an_error_message_end_with_the_message():
    (end,) = [e for e in _failed("stream disconnected before completion") if e["type"] == "message_end"]
    assert end["message"]["stopReason"] == "error"
    assert end["message"]["errorMessage"] == "stream disconnected before completion"
    assert "ralphd" not in end


def test_turn_failed_http_status_is_present_when_parseable():
    (end,) = [e for e in _failed("exceeded retry limit, last status: 429 Too Many Requests")
              if e["type"] == "message_end"]
    assert end["message"]["stopReason"] == "error"
    assert end["ralphd"]["httpStatus"] == 429
    (end,) = [e for e in _failed("unexpected status 503 Service Unavailable: upstream")
              if e["type"] == "message_end"]
    assert end["ralphd"]["httpStatus"] == 503


def test_turn_failed_http_status_is_absent_when_not_parseable():
    for message in ("boom", "connection reset by peer after 2500 tokens", ""):
        (end,) = [e for e in _failed(message) if e["type"] == "message_end"]
        assert "ralphd" not in end, message
        assert end["message"]["stopReason"] == "error"


def test_reconnecting_error_events_are_non_terminal():
    norm = CodexNormaliser(model_ref=REF)
    for n in range(1, 6):
        ev = {"type": "error", "message": f"Reconnecting... {n}/5"}
        assert norm.feed(ev) == []
        assert not is_traffic(ev)
    # The stream goes on and ends normally afterwards.
    out = norm.feed({"type": "item.completed",
                     "item": {"id": "a", "type": "agent_message", "text": "ok"}})
    out += norm.feed(_usage_event(input_tokens=1, output_tokens=1))
    (end,) = _ends(out)
    assert end["stopReason"] == "stop" and "errorMessage" not in end


def test_max_output_tokens_is_length_and_keeps_the_partial_text():
    out = _failed("Response incomplete: max_output_tokens")
    (end,) = _ends(out)
    assert end["stopReason"] == "length"
    assert end["errorMessage"] == "Response incomplete: max_output_tokens"
    assert end["content"] == [{"type": "text", "text": "partial text"}]


def _scan_events(events):
    result = IterationResult(exit_code=0, harness="codex")
    for event in events:
        assert PiRunner._scan_line(json.dumps(event).encode(), result) is True
    return result


_supervisor = scripted_supervisor(run_id="unit", infra_retry_max=3,
                                  infra_retry_backoff_s=[0.01])


def test_text_free_max_output_tokens_classifies_output_truncated(tmp_path):
    norm = CodexNormaliser(model_ref=REF)
    out = norm.feed({"type": "item.completed",
                     "item": {"id": "r", "type": "reasoning", "text": "pondering"}})
    out += norm.feed({"type": "turn.failed", "error": {"message": "max_output_tokens"}})
    result = _scan_events(out)
    assert result.final_text == ""
    assert result.output_truncated is True
    assert _supervisor(tmp_path)._classify_result(result) == "output_truncated"


def test_max_output_tokens_with_text_is_not_output_truncated(tmp_path):
    result = _scan_events(_failed("max_output_tokens"))
    assert result.final_text == "partial text"
    assert result.output_truncated is False
    assert _supervisor(tmp_path)._classify_result(result) != "output_truncated"


# --------------------------------------------------- C.5 unknown tolerance
def test_unknown_items_fixture_produces_no_pi_event_and_no_traffic():
    norm = CodexNormaliser(model_ref=REF)
    events = []
    for ev in _load("unknown_items"):
        assert not is_traffic(ev), ev
        events.extend(norm.feed(ev))
    assert events == []
    assert norm.finish(None) == []


def test_unknown_event_and_item_types_are_silent_and_not_traffic():
    norm = CodexNormaliser(model_ref=REF)
    odd = [
        {"type": "item.completed", "item": {"id": "x", "type": "web_search", "query": "q"}},
        {"type": "item.started", "item": {"id": "y", "type": "todo_list", "items": []}},
        {"type": "item.completed", "item": {"id": "z", "type": "code_mode_exec", "code": "1"}},
        {"type": "item.completed", "item": "not-a-dict"},
        {"type": "item.completed"},
        {"type": "thread.compacted"},
        {"type": "totally.new.event", "payload": {"a": 1}},
        {"type": 7},
        {},
        "not-a-dict",
        None,
    ]
    for ev in odd:
        assert norm.feed(ev) == [], ev
        assert not is_traffic(ev), ev
    assert norm.finish(None) == []


def test_no_terminal_event_is_closed_by_finish_error():
    norm = CodexNormaliser(model_ref=REF)
    out = norm.feed({"type": "item.completed",
                     "item": {"id": "a", "type": "agent_message", "text": "half"}})
    assert "message_end" not in _types(out)
    out = norm.finish({"message": "codex exited 1 without a terminal event"})
    (end,) = _ends(out)
    assert end["stopReason"] == "error"
    assert end["errorMessage"] == "codex exited 1 without a terminal event"
    assert end["content"] == [{"type": "text", "text": "half"}]


def test_finish_error_with_no_open_group_still_emits_an_error_message_end():
    norm = CodexNormaliser(model_ref=REF)
    out = norm.finish({"message": "stream ended"})
    assert _types(out) == ["message_start", "message_end"]
    (end,) = _ends(out)
    assert end["stopReason"] == "error" and end["errorMessage"] == "stream ended"


def test_finish_with_a_stop_reason_ends_the_stream_with_that_reason():
    norm = CodexNormaliser(model_ref="aigw-openai/openai.gpt-6.1-sol")
    (end,) = [e for e in norm.finish({"message": "aborted", "stopReason": "aborted"})
              if e["type"] == "message_end"]
    assert end["message"]["stopReason"] == "aborted"
    assert end["message"]["errorMessage"] == "aborted"
