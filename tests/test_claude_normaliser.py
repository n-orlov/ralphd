"""T04a: the pure SDK-to-pi normaliser (`ralphd.harness.normalise`).

Inputs are spike-shaped dicts (what `dataclasses.asdict` gives for SDK
messages); no SDK, no network, no tokens.
"""

from __future__ import annotations

import json

from ralphd.engine import state
from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.harness.normalise import Normaliser, pi_tool_args, pi_tool_name

MODEL = "claude-sonnet-5-5"
USAGE = {
    "input_tokens": 2,
    "output_tokens": 18,
    "cache_read_input_tokens": 30745,
    "cache_creation_input_tokens": 263,
}


def _asst(mid, blocks, usage=USAGE):
    return {"content": blocks, "model": MODEL, "message_id": mid, "usage": usage}


def _types(events):
    return [e["type"] for e in events]


def _run(norm, items):
    out = []
    for kind, msg in items:
        out.extend(norm.feed(kind, msg))
    return out


def _full_session(final):
    return [
        ("assistant", _asst("m1", [{"thinking": "", "signature": "sig"}])),
        ("assistant", _asst("m1", [{"id": "t1", "name": "Read", "input": {"file_path": "/w/a.txt"}}])),
        ("assistant", _asst("m1", [{"id": "t2", "name": "mcp__ralphd__task-insert", "input": {"x": 1}}])),
        ("user", {"content": [{"tool_use_id": "t1", "content": "1\talpha\n", "is_error": None}]}),
        (
            "user",
            {"content": [{"tool_use_id": "t2", "content": [{"type": "text", "text": "ok"}], "is_error": False}]},
        ),
        ("assistant", _asst("m2", [{"text": "done\n<promise>COMPLETE</promise>"}])),
        ("result", {"is_error": False, "result": final}),
    ]


def test_event_sequence_for_thinking_two_tools_and_text():
    norm = Normaliser(model_ref="anthropic/claude-sonnet-5-5")
    events = _run(norm, _full_session("done\n<promise>COMPLETE</promise>")) + norm.finish(None)
    assert _types(events) == [
        "message_start",
        "message_update",  # thinking_start
        "message_update",  # thinking_delta
        "tool_execution_start",
        "tool_execution_start",
        "message_end",
        "tool_execution_end",
        "tool_execution_end",
        "message_start",
        "message_update",  # text_delta
        "message_update",  # text_end
        "message_end",
    ]
    kinds = [e["assistantMessageEvent"]["type"] for e in events if e["type"] == "message_update"]
    assert kinds == ["thinking_start", "thinking_delta", "text_delta", "text_end"]
    first_end = events[5]["message"]
    assert [c["type"] for c in first_end["content"]] == ["thinking", "toolCall", "toolCall"]
    assert first_end["stopReason"] == "toolUse"
    assert first_end["provider"] == "anthropic" and first_end["model"] == MODEL


def test_exactly_one_tool_execution_start_per_call(tmp_path):
    norm = Normaliser(model_ref=MODEL)
    events = _run(norm, _full_session("done")) + norm.finish(None)
    starts = [e for e in events if e["type"] == "tool_execution_start"]
    assert [s["toolCallId"] for s in starts] == ["t1", "t2"]
    out = state.iteration_output_path(tmp_path, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    assert state.count_tool_calls(tmp_path, 1) == 2


def test_tool_name_and_arg_mapping():
    assert [pi_tool_name(n) for n in ("Read", "Write", "Edit", "Bash", "Grep", "Glob")] == [
        "read", "write", "edit", "bash", "grep", "glob",
    ]
    assert pi_tool_name("mcp__ralphd__task-insert") == "task-insert"
    assert pi_tool_name("Other") == "Other"
    assert pi_tool_args("read", {"file_path": "/a"}) == {"path": "/a"}
    assert pi_tool_args("bash", {"command": "ls"}) == {"command": "ls"}
    assert pi_tool_args("grep", {"pattern": "x", "path": "/y"}) == {"pattern": "x", "path": "/y"}
    norm = Normaliser(model_ref=MODEL)
    events = _run(norm, _full_session("done"))
    starts = [e for e in events if e["type"] == "tool_execution_start"]
    assert starts[0]["toolName"] == "read" and starts[0]["args"] == {"path": "/w/a.txt"}
    assert starts[1]["toolName"] == "task-insert"
    ends = [e for e in events if e["type"] == "tool_execution_end"]
    assert ends[0] == {
        "type": "tool_execution_end", "toolCallId": "t1", "toolName": "read",
        "isError": False, "result": "1\talpha\n",
    }
    assert ends[1]["toolName"] == "task-insert"
    assert ends[1]["result"] == {"content": [{"type": "text", "text": "ok"}]}


def test_usage_is_summed_once_per_message_id():
    norm = Normaliser(model_ref=MODEL)
    events = _run(norm, _full_session("done")) + norm.finish(None)
    ends = [e["message"] for e in events if e["type"] == "message_end"]
    first = ends[0]["usage"]
    assert first == {"input": 2, "output": 18, "cacheRead": 30745, "cacheWrite": 263, "totalTokens": 2 + 18 + 30745 + 263}
    # three SDK lines shared m1, so the group carries one usage, not three.
    result = IterationResult(exit_code=0, interrupted=False, timed_out=False, final_text="", error_message="", usage={})
    for e in events:
        PiRunner._scan_line(json.dumps(e).encode(), result)
    assert result.usage["totalTokens"] == 2 * (2 + 18 + 30745 + 263)
    assert result.model == f"anthropic/{MODEL}"


def test_final_result_text_ends_with_promise_line():
    norm = Normaliser(model_ref=MODEL)
    items = _full_session("done\n<promise>COMPLETE</promise>")
    events = _run(norm, items) + norm.finish(None)
    result = IterationResult(exit_code=0, interrupted=False, timed_out=False, final_text="", error_message="", usage={})
    for e in events:
        PiRunner._scan_line(json.dumps(e).encode(), result)
    assert result.saw_complete
    assert result.error_message == ""


def test_result_text_differing_from_last_assistant_text_gets_trailing_message():
    norm = Normaliser(model_ref="anthropic/" + MODEL)
    items = [
        ("assistant", _asst("m1", [{"text": "working"}])),
        ("result", {"is_error": False, "result": "all good\n<promise>COMPLETE</promise>"}),
    ]
    events = _run(norm, items) + norm.finish(None)
    assert _types(events)[-4:] == ["message_start", "message_update", "message_update", "message_end"]
    tail = events[-1]["message"]
    assert tail["content"] == [{"type": "text", "text": "all good\n<promise>COMPLETE</promise>"}]
    assert tail["usage"]["totalTokens"] == 0
    result = IterationResult(exit_code=0, interrupted=False, timed_out=False, final_text="", error_message="", usage={})
    for e in events:
        PiRunner._scan_line(json.dumps(e).encode(), result)
    assert result.saw_complete


def test_no_trailing_message_when_result_equals_last_text():
    norm = Normaliser(model_ref=MODEL)
    items = [
        ("assistant", _asst("m1", [{"text": "same"}])),
        ("result", {"is_error": False, "result": "same"}),
    ]
    events = _run(norm, items) + norm.finish(None)
    assert _types(events) == ["message_start", "message_update", "message_update", "message_end"]


def test_error_result_becomes_error_message_end():
    norm = Normaliser(model_ref=MODEL)
    items = [
        ("assistant", {"content": [{"text": "API Error: 429 rate_limit_error"}], "model": "<synthetic>",
                       "message_id": "s1", "usage": None, "error": "rate_limit"}),
        (
            "result",
            {"is_error": True, "result": "API Error: 429 rate_limit_error", "api_error_status": 429,
             "terminal_reason": "api_error"},
        ),
    ]
    events = _run(norm, items) + norm.finish(None)
    last = events[-1]
    assert last["type"] == "message_end"
    msg = last["message"]
    assert msg["stopReason"] == "error"
    assert msg["errorMessage"] == "429 api_error: API Error: 429 rate_limit_error"
    assert msg["usage"] == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0}
    assert last["ralphd"] == {"httpStatus": 429, "errorType": "api_error"}
    result = IterationResult(exit_code=1, interrupted=False, timed_out=False, final_text="", error_message="", usage={})
    for e in events:
        PiRunner._scan_line(json.dumps(e).encode(), result)
    assert result.error_message.startswith("429 api_error")


def test_explicit_finish_error_without_result():
    norm = Normaliser(model_ref="anthropic/" + MODEL)
    events = norm.finish({"status": None, "type": "ProcessError", "message": "exit 1"})
    assert len(events) == 1
    msg = events[0]["message"]
    assert msg["errorMessage"] == "ProcessError: exit 1"
    assert msg["model"] == MODEL
    assert "ralphd" in events[0] and events[0]["ralphd"] == {"errorType": "ProcessError"}


def test_unknown_kinds_and_non_dicts_are_ignored():
    norm = Normaliser(model_ref=MODEL)
    assert norm.feed("rate_limit", {"rate_limit_info": {}}) == []
    assert norm.feed("system", {"subtype": "init"}) == []
    assert norm.feed("assistant", "junk") == []  # type: ignore[arg-type]
    assert norm.finish(None) == []


def test_wire_shaped_blocks_with_type_tags_are_accepted():
    norm = Normaliser(model_ref=MODEL)
    events = _run(
        norm,
        [
            ("assistant", _asst("m1", [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}}])),
            ("user", {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "x", "is_error": True}]}),
        ],
    )
    assert events[-1]["isError"] is True and events[-1]["toolName"] == "bash"
