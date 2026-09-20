"""Task 033 (operator-inserted scope, #114): the steer hat's tools are WIRED
LIVE -- callable tools whose execution happens in the ENGINE process, with
every applier guarantee kept, plus read/write tools over the run-context
files, plus a prompt that says the hat should use them itself.

## What was broken, observed in THIS run (steer 001, iteration 21)

`state.STEER_HAT_TOOLS` declared the steer hat's authority boundary (task 010,
requirement H, #98) as *library functions only*. Nothing handed them to the
hat, so a steer that required task-table edits could not be carried out from
inside the container at all: the hat wrote
`artifacts/steer-001-proposed-task-edits.json` and the operator applied it
host-side. `tests/test_steer_hat_tool_surface.py` pins the library half (the
set, and `task-update`'s routing through `apply_task_status_change`); this
module pins the LIVE half:

1. the steer phase registers exactly the wired tool set, and no other phase
   is launched with it at all -- and each registered tool's parameter schema
   is exactly the payload its usage line (in the doc table AND in
   `prompts/steer.md`) documents, so a hat that follows the documentation
   makes a schema-valid call;
2. a steer-hat `task-update` carrying a status change routes through the
   transition validator, and an illegal transition is refused with the
   applier's own documented diagnostic;
3. an insert lands `origin=steer` / `status=pending`;
4. a delete against a still-referenced task is refused;
5. a context-file write lands and emits its audit event, naming the file and
   the steering entry that caused it;
6. `prompts/steer.md` names the tools and states the act-directly default.

Everything below drives the REAL production path -- `hat_tools.SteerToolBroker`
as the engine constructs it, and `LoopSupervisor._step_generic_hat("steer",
...)` itself for the end-to-end case, where a scripted agent stands in for the
hat's `pi` subprocess and calls `hat_tools.call()` exactly as the generated
extension does (in a worker thread, because the real hat is a separate
process and the broker serves it from this one's event loop). No test-local
applier, no monkeypatched substitute for the behaviour under test.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from ralphd.engine import hat_tools, transitions
from ralphd.engine.state import (
    STEER_HAT_TOOLS,
    TASK_ORIGIN_STEER,
    RunDir,
    atomic_write_json,
)

from test_steer import _ok, _supervisor

PROMPT = (Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
          / "steer.md")

STEER_EDGES = transitions.successors("steer", vigilant=False)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == type_]


def _broker(tmp_path: Path, tasks: list[dict] | None = None,
            steering: tuple[str, ...] = ("001-opX.md",),
            vigilant: bool = False) -> hat_tools.SteerToolBroker:
    run = RunDir(root=tmp_path / "run")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks or []})
    return hat_tools.SteerToolBroker(run, vigilant=vigilant,
                                     steering=steering)


# ---------------------------------------------------------------------------
# 1. The steer phase registers exactly the wired tool set -- and only steer.
# ---------------------------------------------------------------------------


def test_the_wired_set_is_the_library_set_plus_the_two_context_tools():
    """By equality, the same shape `test_steer_hat_tool_surface.py` pins the
    library set with: a tool wired later that is not in this declaration
    fails here instead of silently widening the hat's authority."""
    assert set(hat_tools.STEER_WIRED_TOOLS) == set(STEER_HAT_TOOLS) | {
        "context-read", "context-write", "notify"}
    assert set(STEER_HAT_TOOLS) == {"task-insert", "task-update",
                                    "task-delete", "standing-rules"}


def test_no_budget_iteration_approach_or_deadline_tool_is_wired():
    for name in hat_tools.STEER_WIRED_TOOLS:
        for bad in ("budget", "approach", "deadline"):
            assert bad not in name, name


def _registered_blocks(source: str) -> dict[str, str]:
    """The generated extension's `pi.registerTool({...})` blocks, by tool
    name: everything up to that block's `async execute`, i.e. the declared
    name/label/description/parameters."""
    blocks: dict[str, str] = {}
    for chunk in source.split("pi.registerTool({")[1:]:
        name = re.search(r'name:\s*"([a-z-]+)"', chunk).group(1)
        blocks[name] = chunk[:chunk.index("async execute")]
    return blocks


def _declared_param_fields(tool: str) -> list[str]:
    """The top-level property names of `tool`'s rendered typebox parameter
    object -- what the live `pi` tool's schema actually accepts."""
    return re.findall(r"^      (\w+): ",
                      hat_tools.parameters_source(tool), re.M)


def _prompt_usage_payload(tool: str) -> dict:
    """The JSON payload `prompts/steer.md`'s own usage line tells the hat to
    send for `tool`, parsed."""
    text = PROMPT.read_text()
    line = next(ln for ln in text.splitlines()
                if ln.strip().startswith(f"- `{tool}`"))
    block = text[text.index(line):]
    if "\n- " in block:
        block = block[:block.index("\n- ")]
    payloads = re.findall(r"`(\{.*?\})`", block, re.S)
    assert payloads, f"no JSON usage payload on {tool}'s prompt line"
    return json.loads(payloads[-1])


def test_the_generated_extension_registers_exactly_the_wired_set(tmp_path):
    broker = _broker(tmp_path)
    path = broker.place()
    source = path.read_text()
    assert set(_registered_blocks(source)) == set(hat_tools.STEER_WIRED_TOOLS)
    # the tools execute in the ENGINE, through the spool -- the extension
    # only hands the call over and reports the answer back
    assert "ralphd.engine.hat_tools" in source
    assert "pi.registerTool" in source
    assert str(broker.run.root) in source


def test_each_registered_tool_declares_the_fields_its_usage_payload_documents(
        tmp_path):
    """The defect this pins (found by task 033's second validation attempt):
    the extension used to register EVERY tool with one required string field
    named `args`, while the prompt and the doc table documented the real
    payload (`{"task": {...}}`, `{"taskId": ...}`), so a hat that followed
    the documentation could not make a schema-valid call at all. The declared
    schema fields must BE the documented payload's top-level keys."""
    source = _broker(tmp_path).place().read_text()
    blocks = _registered_blocks(source)
    for name in hat_tools.STEER_WIRED_TOOLS:
        documented = set(hat_tools.usage_payload(name))
        declared = set(_declared_param_fields(name))
        assert declared, f"{name} declares no parameter fields"
        assert declared == documented, (
            f"{name}: schema fields {sorted(declared)} != documented payload "
            f"keys {sorted(documented)}")
        # the rendered object is what the registration actually carries, and
        # the call is forwarded verbatim (no wrapper, no re-stringify)
        assert hat_tools.parameters_source(name) in blocks[name], name
        assert f'callEngine("{name}", params)' in source, name


def test_no_registered_tool_hides_its_payload_behind_a_stringified_wrapper(
        tmp_path):
    source = _broker(tmp_path).place().read_text()
    assert "params.args" not in source
    for name in hat_tools.STEER_WIRED_TOOLS:
        assert "args" not in _declared_param_fields(name), name


def test_the_prompt_usage_line_and_the_live_schema_are_the_same_shape():
    """Criterion 3's agreement, asserted against the prompt text itself: what
    `prompts/steer.md` tells the hat to send is exactly what the registered
    tool accepts, key for key."""
    for name in hat_tools.STEER_WIRED_TOOLS:
        prompt_keys = set(_prompt_usage_payload(name))
        assert prompt_keys == set(_declared_param_fields(name)), name
        assert prompt_keys == set(hat_tools.usage_payload(name)), name


def test_the_engine_applies_every_documented_usage_payload_as_written(
        tmp_path, monkeypatch):
    """End of the same chain: each payload the PROMPT documents, handed to the
    engine-side executor unchanged, is applied -- never bounced on shape."""
    broker = _broker(tmp_path, [
        {"id": "012", "title": "t", "status": "pending"},
        {"id": "021", "title": "t", "status": "pending"},
    ])
    broker.run.notes_file.write_text("notes\n")
    # `notify`'s documented payload (`{"text": "..."}`) says nothing about
    # a notifier URL -- unlike every other tool here it depends on runtime
    # config, so this one entry is stubbed to a configured URL and a
    # successful POST, the same way `test_notify_hat_tool.py` (task 022)
    # stubs the send for its own success/failure cases.
    broker.notifier_url = "http://example.invalid/notify"
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **k: (200, None))
    for name in hat_tools.STEER_WIRED_TOOLS:
        response = broker.execute(name, _prompt_usage_payload(name))
        assert response["ok"] is True, (name, response)
        assert "needs" not in (response.get("error") or ""), (name, response)


def test_the_steer_phase_is_the_only_phase_launched_with_the_extension(tmp_path):
    """`_hat_extensions` is what puts `-e <extension>` on the hat's argv, and
    it is empty for every phase but `steer` -- the tool surface IS the
    authority boundary (requirement H), so a worker/verify/review iteration
    must never find these tools registered."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    # no broker yet: even `steer` gets nothing
    for phase in ("worker", "verify", "review", "reflect", "planning", "steer"):
        assert sup._hat_extensions(phase) == [], phase

    broker = hat_tools.SteerToolBroker(sup.run)
    broker.place()
    sup._steer_tool_broker = broker
    assert sup._hat_extensions("steer") == [str(broker.extension_file)]
    for phase in ("worker", "verify", "review", "reflect", "planning"):
        assert sup._hat_extensions(phase) == [], phase


def test_the_runner_turns_the_extension_list_into_pi_e_flags():
    """The one place the argv is built (`PiRunner.run`) -- asserted at the
    source level so the flag cannot quietly stop being passed."""
    import inspect

    from ralphd.engine.runner import PiRunner

    src = inspect.getsource(PiRunner.run)
    assert 'for path in extensions or ()' in src
    assert '"-e", str(path)' in src


# ---------------------------------------------------------------------------
# 2. A status change routes through the transition validator.
# ---------------------------------------------------------------------------


def test_a_task_update_status_change_is_applied_through_the_one_applier(tmp_path):
    broker = _broker(tmp_path, [{"id": "010", "title": "t",
                                 "status": "pending",
                                 "successCriteria": "do X"}])
    response = broker.execute("task-update",
                              {"taskId": "010",
                               "changes": {"status": "in-progress",
                                           "priority": 7}})
    assert response["ok"] is True
    task = broker.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress"
    assert task["priority"] == 7
    # the applier's own audit event, with the steer source on it
    applied = _events(broker.run.root, "amendment_status_applied")
    assert len(applied) == 1
    assert applied[0]["source"] == "steer"
    assert applied[0]["taskId"] == "010"


def test_an_illegal_status_transition_is_refused_with_the_documented_reason(tmp_path):
    # Cure-01-05 (PRD requirement B.3/B.4, F05, #221) moves this node's
    # fixture from `pending -> validated` to `pending -> awaiting-
    # validation`, exactly as task 007 already moved
    # `tests/test_amendment_status_applier.py`'s equivalent node when it
    # first declared the un-reopen edge: `pending -> validated` is a
    # DECLARED edge in both variants now (the engine's own restore of a
    # status it persisted before a cure pass re-opened a record), so it is
    # no longer an example of the table refusing a move, while `pending ->
    # awaiting-validation` is nowhere in either variant's table -- that
    # status is only ever reached by CLAIM, out of `in-progress`. The
    # documented `"illegal transition"` reason stays pinned, against a
    # combination that genuinely earns it; the second half of this node
    # keeps the steer hat's refusal on the un-reopen shape covered too.
    broker = _broker(tmp_path, [{"id": "011", "title": "t",
                                 "status": "pending"}])
    response = broker.execute("task-update",
                             {"taskId": "011",
                              "changes": {"status": "awaiting-validation"}})
    assert response["ok"] is False
    assert "illegal transition" in response["error"]
    assert response["detail"]["statusResult"]["reason"] == "illegal transition"
    assert broker.run.read_tasks()["tasks"][0]["status"] == "pending"
    assert _events(broker.run.root, "amendment_status_applied") == []
    # ... and the refusal is auditable as a served call
    calls = _events(broker.run.root, hat_tools.EVENT_TOOL_CALL)
    assert [c["tool"] for c in calls] == ["task-update"]
    assert calls[0]["ok"] is False

    # the un-reopen restore itself: declared, but admitted for the engine's
    # own `source="cure-reconciliation"` call alone, so the steer hat is
    # refused here with the semantic layer's own reason instead.
    restore = broker.execute("task-update",
                             {"taskId": "011",
                              "changes": {"status": "validated"}})
    assert restore["ok"] is False
    assert "cure-reconciliation" in restore["error"]
    assert broker.run.read_tasks()["tasks"][0]["status"] == "pending"
    assert _events(broker.run.root, "amendment_status_applied") == []


def test_a_non_status_update_is_a_plain_field_write(tmp_path):
    broker = _broker(tmp_path, [{"id": "012", "title": "old",
                                 "status": "pending",
                                 "dependsOn": ["004"]}])
    response = broker.execute("task-update",
                              {"taskId": "012",
                               "changes": {"title": "new",
                                           "successCriteria": "bounded",
                                           "dependsOn": []}})
    assert response["ok"] is True
    task = broker.run.read_tasks()["tasks"][0]
    assert task["title"] == "new"
    assert task["dependsOn"] == []
    assert task["successCriteria"] == "bounded"
    assert task["status"] == "pending"


# ---------------------------------------------------------------------------
# 3. An insert lands origin=steer / status=pending.
# ---------------------------------------------------------------------------


def test_an_insert_lands_origin_steer_and_status_pending(tmp_path):
    broker = _broker(tmp_path, [])
    response = broker.execute("task-insert",
                              {"task": {"id": "037", "title": "new work",
                                        "successCriteria": "do it",
                                        # a status the hat supplied is ignored
                                        "status": "completed",
                                        "origin": "planner"}})
    assert response["ok"] is True
    task = broker.run.read_tasks()["tasks"][0]
    assert task["id"] == "037"
    assert task["status"] == "pending"
    assert task["origin"] == TASK_ORIGIN_STEER


def test_an_insert_without_an_id_is_refused_not_written(tmp_path):
    broker = _broker(tmp_path, [])
    response = broker.execute("task-insert", {"task": {"title": "no id"}})
    assert response["ok"] is False
    assert broker.run.read_tasks()["tasks"] == []


# ---------------------------------------------------------------------------
# 4. A delete against a still-referenced task is refused.
# ---------------------------------------------------------------------------


def test_a_delete_is_refused_while_another_tasks_dependson_names_it(tmp_path):
    broker = _broker(tmp_path, [
        {"id": "020", "title": "t", "status": "pending"},
        {"id": "021", "title": "u", "status": "pending", "dependsOn": ["020"]},
    ])
    response = broker.execute("task-delete", {"taskId": "020"})
    assert response["ok"] is False
    assert "still depended on" in response["error"]
    assert len(broker.run.read_tasks()["tasks"]) == 2


def test_a_delete_of_an_unreferenced_task_lands(tmp_path):
    broker = _broker(tmp_path, [{"id": "022", "title": "t",
                                 "status": "pending"}])
    assert broker.execute("task-delete", {"taskId": "022"})["ok"] is True
    assert broker.run.read_tasks()["tasks"] == []


# ---------------------------------------------------------------------------
# 5. A context-file write lands and emits its audit event.
# ---------------------------------------------------------------------------


def test_a_context_write_lands_and_emits_an_audit_event_naming_file_and_steer(
        tmp_path):
    broker = _broker(tmp_path, steering=("003-do-x.md",))
    broker.run.notes_file.write_text("old notes\n")
    response = broker.execute("context-write",
                              {"file": "notes.md", "text": "new notes\n"})
    assert response["ok"] is True
    assert broker.run.notes_file.read_text() == "new notes\n"
    writes = _events(broker.run.root, hat_tools.EVENT_CONTEXT_WRITE)
    assert len(writes) == 1
    assert writes[0]["file"] == "notes.md"
    assert writes[0]["steering"] == ["003-do-x.md"]


def test_a_context_write_can_name_the_steering_entry_itself(tmp_path):
    broker = _broker(tmp_path, steering=("001-a.md", "002-b.md"))
    broker.execute("context-write", {"file": "notes.md", "text": "x\n",
                                     "steeringEntry": "002-b.md"})
    writes = _events(broker.run.root, hat_tools.EVENT_CONTEXT_WRITE)
    assert writes[0]["steering"] == ["002-b.md"]


def test_the_standing_rules_tool_writes_the_pinned_block_and_audits_it(tmp_path):
    broker = _broker(tmp_path)
    response = broker.execute("standing-rules",
                              {"text": "## Standing rules\n\n- new rule\n"})
    assert response["ok"] is True
    assert broker.run.read_standing_rules() == "## Standing rules\n\n- new rule\n"
    writes = _events(broker.run.root, hat_tools.EVENT_CONTEXT_WRITE)
    assert [w["file"] for w in writes] == ["standing-rules.md"]


def test_context_read_serves_a_run_context_file(tmp_path):
    broker = _broker(tmp_path, [{"id": "030", "title": "t",
                                 "status": "pending"}])
    broker.run.notes_file.write_text("the notes\n")
    assert broker.execute("context-read", {"file": "notes.md"})["text"] == \
        "the notes\n"
    tasks_text = broker.execute("context-read", {"file": "tasks.json"})["text"]
    assert json.loads(tasks_text)["tasks"][0]["id"] == "030"


def test_the_task_table_is_not_writable_through_the_context_tool(tmp_path):
    """`tasks.json` is readable but never writable here: the three `task-*`
    tools are its only writers, so every applier guarantee stays on the one
    path that has them."""
    broker = _broker(tmp_path, [{"id": "031", "title": "t",
                                 "status": "pending"}])
    response = broker.execute("context-write",
                              {"file": "tasks.json", "text": "{}"})
    assert response["ok"] is False
    assert "only writers" in response["error"]
    assert broker.run.read_tasks()["tasks"][0]["id"] == "031"
    assert _events(broker.run.root, hat_tools.EVENT_CONTEXT_WRITE) == []


def test_an_unknown_context_file_and_an_unknown_tool_are_refused(tmp_path):
    broker = _broker(tmp_path)
    assert broker.execute("context-write",
                          {"file": "../../etc/passwd", "text": "x"})["ok"] is False
    unknown = broker.execute("no-such-tool", {})
    assert unknown["ok"] is False and "unknown tool" in unknown["error"]


# ---------------------------------------------------------------------------
# The end-to-end path: the hat's own process calls, the ENGINE applies.
# ---------------------------------------------------------------------------


def _hat_that_calls_tools(calls: list[tuple[str, dict]], answers: list[dict]):
    """A scripted steer hat that does what the generated `pi` extension does:
    hand each tool call to the engine over the run dir's spool
    (`hat_tools.call`) and read the answer back."""
    def handler(sup, prompt=""):
        for tool, args in calls:
            answers.append(hat_tools.call(sup.run.root, tool, args,
                                          timeout_s=20.0))
        return _ok(json.dumps({"settled": True}))
    return handler


class _ThreadedAgent:
    """`test_steering_priority_routing_task012._ScriptedAgent`'s shape, with
    the handler run in a worker thread: the real hat is a SEPARATE PROCESS
    that blocks on the spool while the engine's own event loop serves it, and
    a handler that blocked on the loop's own thread could never be served at
    all. The thread is this suite's stand-in for that process boundary --
    nothing about the engine side is stubbed."""

    def __init__(self, handler):
        self.handler = handler
        self.running = False
        self.calls: dict[str, int] = {}

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw):
        phase = self.sup.run.read_status().get("phase")
        self.calls[phase] = self.calls.get(phase, 0) + 1
        self.kw = kw
        self.running = True
        try:
            return await asyncio.to_thread(self.handler, self.sup, prompt)
        finally:
            self.running = False


@pytest.mark.asyncio
async def test_a_real_steer_iteration_applies_the_hats_tool_calls(tmp_path):
    """The whole mechanism, through `_step_generic_hat("steer", ...)` itself:
    the hat's process never writes the task table -- it calls, the engine
    applies, and the changes are on disk when the phase ends."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    answers: list[dict] = []
    handler = _hat_that_calls_tools([
        ("task-insert", {"task": {"id": "099", "title": "steered work",
                                  "successCriteria": "do the thing"}}),
        ("task-update", {"taskId": "001",
                         "changes": {"priority": 12, "dependsOn": []}}),
        ("context-write", {"file": "notes.md",
                           "text": "### Steering: do X\n"}),
    ], answers)

    sup = _supervisor(root, {"steer": [handler]})
    agent = _ThreadedAgent(handler)
    agent.sup = sup                            # type: ignore[attr-defined]
    sup.runner = agent                         # type: ignore[assignment]
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "existing", "status": "pending",
         "dependsOn": ["000"], "priority": 1},
    ]})
    sup.run.add_steering("insert a task and re-point 001", "opTools")
    sup.iterations_used = 1

    outcome = await sup._step_generic_hat("steer", STEER_EDGES)
    assert outcome == ("advance", "worker", "steer_settled")

    # the hat's own iteration was launched with the generated extension --
    # the tools were live, not merely placed
    assert agent.kw.get("extensions"), agent.kw
    assert agent.kw["extensions"][0].endswith(hat_tools.EXTENSION_FILENAME)

    assert [a["ok"] for a in answers] == [True, True, True], answers
    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert tasks["099"]["status"] == "pending"
    assert tasks["099"]["origin"] == TASK_ORIGIN_STEER
    assert tasks["001"]["priority"] == 12
    assert tasks["001"]["dependsOn"] == []
    assert sup.run.notes_file.read_text() == "### Steering: do X\n"

    # every call is audited, and the write names the steering entry that
    # caused it (the entry pending when the pass began)
    served = [e["tool"] for e in _events(root, hat_tools.EVENT_TOOL_CALL)]
    assert served == ["task-insert", "task-update", "context-write"]
    writes = _events(root, hat_tools.EVENT_CONTEXT_WRITE)
    assert writes[0]["file"] == "notes.md"
    assert writes[0]["steering"] == ["001-opTools.md"]

    # the broker is torn down with the phase: no tool surface outlives it
    assert sup._steer_tool_broker is None
    assert sup._hat_extensions("steer") == []


def test_the_client_answers_instead_of_hanging_when_no_engine_serves(tmp_path):
    """A tool call with nothing serving it returns an answer the hat can act
    on -- never a blocked iteration."""
    root = tmp_path / "run"
    root.mkdir(parents=True)
    response = hat_tools.call(root, "task-insert", {"task": {"id": "1"}},
                              timeout_s=0.2)
    assert response["ok"] is False
    assert "did not answer" in response["error"]


def test_a_spooled_request_is_served_by_the_engine_side_only(tmp_path):
    """The spool protocol itself: the hat's side writes a request and the
    ENGINE's `serve_once()` is what executes and answers it."""
    broker = _broker(tmp_path, [{"id": "050", "title": "t",
                                 "status": "pending"}])
    broker.place()
    base = broker.run.root / hat_tools.HAT_TOOLS_DIRNAME
    (base / hat_tools.REQUESTS_DIRNAME / "abc.json").write_text(json.dumps(
        {"id": "abc", "tool": "task-update",
         "args": {"taskId": "050", "changes": {"priority": 3}}}))

    assert broker.serve_once() == 1
    answer = json.loads(
        (base / hat_tools.RESPONSES_DIRNAME / "abc.json").read_text())
    assert answer["ok"] is True and answer["tool"] == "task-update"
    assert broker.run.read_tasks()["tasks"][0]["priority"] == 3
    # the request is consumed, so a second pass does nothing
    assert broker.serve_once() == 0


def test_an_unreadable_request_is_answered_not_crashed(tmp_path):
    broker = _broker(tmp_path)
    broker.place()
    base = broker.run.root / hat_tools.HAT_TOOLS_DIRNAME
    (base / hat_tools.REQUESTS_DIRNAME / "bad.json").write_text("{not json")
    assert broker.serve_once() == 1
    answer = json.loads(
        (base / hat_tools.RESPONSES_DIRNAME / "bad.json").read_text())
    assert answer["ok"] is False and "unreadable" in answer["error"]


# ---------------------------------------------------------------------------
# 6. The prompt contract: the tools are named, and acting directly is the
#    default (deferring to the operator is the exception).
# ---------------------------------------------------------------------------


def test_steer_prompt_names_every_wired_tool():
    text = PROMPT.read_text()
    for name in hat_tools.STEER_WIRED_TOOLS:
        assert f"`{name}`" in text, f"prompts/steer.md never names {name}"


def test_steer_prompt_documents_each_tool_with_a_usage_line():
    """One usage line per tool: the argument shape, not just the name."""
    text = PROMPT.read_text()
    for name, expected in (("task-insert", '"task"'),
                           ("task-update", '"changes"'),
                           ("task-delete", '"taskId"'),
                           ("standing-rules", '"text"'),
                           ("context-read", '"file"'),
                           ("context-write", '"file"')):
        line = next((ln for ln in text.splitlines()
                     if ln.strip().startswith(f"- `{name}`")), None)
        assert line, f"no usage line for {name}"
        # the usage JSON may wrap onto the same bullet's continuation
        block = text[text.index(line):]
        block = block[:block.index("\n- ") if "\n- " in block else len(block)]
        assert expected in block, f"{name}'s usage line lacks {expected}"


def test_steer_prompt_states_the_act_directly_default_and_the_exception():
    text = " ".join(PROMPT.read_text().split()).lower()
    # CAN and SHOULD, itself, directly
    assert "you can, and in almost every case should, apply the task-table" in text
    assert "directly" in text
    # deferring is the exception, reserved for what no tool can express
    assert "deferring to the operator is the exception" in text
    assert "proposed edits" in text
    assert "host-side" in text


def test_steer_prompt_keeps_the_authority_boundary_and_the_status_rule():
    text = " ".join(PROMPT.read_text().split())
    assert "There is no budget, iteration, approach or deadline tool" in text
    assert "one status applier" in text or "one applier" in text
