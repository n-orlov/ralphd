"""Task 033 (operator-inserted scope, #114): the steer hat's tool surface,
WIRED LIVE -- callable tools whose execution happens in the ENGINE process.

## What was broken

`state.STEER_HAT_TOOLS` (task 010, requirement H, #98) declared the steer
hat's authority boundary -- task insert/update/delete plus the standing-rules
block, and deliberately nothing budget-shaped -- but declared it as *library
functions only*. Nothing ever handed those functions to the hat, so the hat
whose entire job is "reshape state, never do the work" could not reshape any
state: `tasks.json` is root-owned by design (`privsep.write_root_owned_json`,
#60/task 105) and the hat's own `pi` process runs as the agent uid, so its
only remaining move was to describe the edits it wanted in an artifact file
and ask the operator to apply them host-side. This run did exactly that
(`artifacts/steer-001-proposed-task-edits.json`), three times.

## The channel

The engine already spawns the hat as a child process it fully controls, and
the run dir is the one filesystem both uids share (the engine's *effective*
uid is the agent's, so everything it creates under the run dir is agent-owned
and agent-writable -- see `privsep`'s own module docstring). That is the
channel used here, and it is a request/response spool, not a socket:

    <run>/hat-tools/requests/<id>.json    written by the HAT (agent uid)
    <run>/hat-tools/responses/<id>.json   written by the ENGINE

`SteerToolBroker.serve()` runs as an ordinary asyncio task inside the engine
process for exactly as long as the steer iteration is in flight; it is the
only thing that ever executes a tool. The hat's process therefore never
writes `tasks.json`, the standing-rules block or any other engine-owned file
itself -- it writes a request and reads an answer, and every applier
guarantee (`apply_task_status_change` for a status move, `origin=steer` +
`status=pending` on an insert, a delete refused while another task's
`dependsOn` names the target) is kept because the engine calls the SAME
`state.STEER_HAT_TOOLS` callables it always had.

The hat sees them as real tools because the broker also generates a `pi`
extension (`extension_source`, one `pi.registerTool()` per wired tool) that
the steer iteration -- and only the steer iteration -- is launched with. The
extension's tool list is GENERATED from `STEER_WIRED_TOOLS` below, so the set
the hat can call cannot drift from the set the engine wired: there is one
declaration, and the TS file is a rendering of it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import state as state_module
from .run_record import NOTIFICATION_FAILED_EVENT, NOTIFICATION_SENT_EVENT
from .state import RunDir, atomic_write

# -- the spool -------------------------------------------------------------

HAT_TOOLS_DIRNAME = "hat-tools"
REQUESTS_DIRNAME = "requests"
RESPONSES_DIRNAME = "responses"
EXTENSION_FILENAME = "ralphd-steer-tools.ts"

# How often the engine-side broker looks for a new request while the hat's
# iteration is in flight, and how long the hat-side client waits for its
# answer before giving up and saying so (never hanging the iteration).
BROKER_POLL_INTERVAL_S = 0.05
CLIENT_TIMEOUT_S = 120.0
CLIENT_POLL_INTERVAL_S = 0.05

# The env var the generated extension passes to the client so one client
# implementation serves every run without the path being compiled in twice.
RUN_DIR_ENV = "RALPHD_HAT_TOOLS_RUN"

# Audit events. `steer.tool_call` is emitted for EVERY served call (applied
# or refused); `steer.context_write` is the second, narrower event a write
# emits, naming the file it wrote and the steering entry that caused it.
EVENT_TOOL_CALL = "steer.tool_call"
EVENT_CONTEXT_WRITE = "steer.context_write"
# Task 025 (PRD requirement H.1/H.4, #264): the third, narrower audit event
# emitted alongside `EVENT_TOOL_CALL` specifically for a served
# `record-verdict` call -- naming the approach and the checkpointed verdict
# so `events.jsonl` shows the moment review committed to one without a
# reader having to parse `EVENT_TOOL_CALL`'s generic `detail` payload.
EVENT_EARLY_VERDICT = "review.early_verdict_recorded"

# -- the wired tool set ----------------------------------------------------

CONTEXT_READ_TOOL = "context-read"
CONTEXT_WRITE_TOOL = "context-write"

# Task 022 (PRD requirement B.3, #140): a plain send -- available to BOTH
# the steer hat (through `STEER_WIRED_TOOLS` below) and the worker hat
# (through a `SteerToolBroker` restricted to just this one tool,
# `LoopSupervisor._run_worker_iteration`) -- never the task-table/standing-
# rules tools, which stay steer-only (PRD requirement H, the authority
# boundary the tool surface itself enforces).
NOTIFY_TOOL = "notify"

# Task 025 (PRD requirement H.1/H.4, #264): the review hat's own checkpoint
# tool -- wired ONLY for a review iteration (`LoopSupervisor._run_review_
# iteration`'s own broker, never the steer/worker brokers above), so
# calling it is what "emits a verdict as soon as it has one" actually means
# mechanically: the write happens in THIS process (the engine), atomically,
# the moment the hat calls the tool -- independent of whatever the hat's
# own subprocess does afterwards.
RECORD_VERDICT_TOOL = "record-verdict"

# One usage line per tool, rendered into both the generated extension's tool
# description and `prompts/steer.md` (the prompt names each tool; this table
# is what a caller reads to get the argument shape right). The usage payload
# is a JSON object whose TOP-LEVEL KEYS ARE the tool's parameter fields --
# see `_TOOL_PARAMS` and `parameters_source` below.
_TOOL_DOCS: dict[str, dict[str, str]] = {
    "task-insert": {
        "label": "Insert task",
        "description": (
            "Insert one new task into the run's task table. Always lands "
            "status=pending and origin=steer; a status you supply is ignored."
        ),
        "usage": '{"task": {"id": "037", "title": "...", '
                 '"successCriteria": "...", "dependsOn": [], "priority": 5}}',
    },
    "task-update": {
        "label": "Update task",
        "description": (
            "Change one existing task's fields (title, successCriteria, "
            "dependsOn, priority, notes, ...). A `status` key inside "
            "`changes` is routed through the engine's one status applier, "
            "which refuses an illegal transition and says so. Moving "
            "`status` from `failed` to `skipped` also requires a "
            "top-level `justification` string (defaulting to "
            "`changes.notes` when omitted) -- refused, with the "
            "applier's own diagnostic, when neither is given -- and that "
            "justification is written into the task's own `notes`, "
            "appended after (never overwritten by) a distinct "
            "`changes.notes` in the same call."
        ),
        "usage": '{"taskId": "012", "changes": {"successCriteria": "...", '
                 '"dependsOn": [], "priority": 9}, '
                 '"justification": "why (needed for failed -> skipped)"}',
    },
    "task-delete": {
        "label": "Delete task",
        "description": (
            "Remove one task by id. Refused while another task's dependsOn "
            "still names it -- re-point those first."
        ),
        "usage": '{"taskId": "021"}',
    },
    "standing-rules": {
        "label": "Standing rules",
        "description": (
            "Replace the run's pinned standing-rules block, which the "
            "harness re-injects into every following iteration's context."
        ),
        "usage": '{"text": "## Standing rules\\n\\n- ...\\n"}',
    },
    CONTEXT_READ_TOOL: {
        "label": "Read run context",
        "description": (
            "Read one run-context file by name. Names: "
            "notes.md, standing-rules.md, tasks.json, prd.md."
        ),
        "usage": '{"file": "notes.md"}',
    },
    CONTEXT_WRITE_TOOL: {
        "label": "Write run context",
        "description": (
            "Replace one writable run-context file's contents (notes.md, "
            "standing-rules.md). Every write emits an audit event naming the "
            "file and the steering entry that caused it. The task table is "
            "not writable here -- the three task-* tools are its only "
            "writers."
        ),
        "usage": '{"file": "notes.md", "text": "...", '
                 '"steeringEntry": "003-do-x.md"}',
    },
    NOTIFY_TOOL: {
        "label": "Notify the operator",
        "description": (
            "POST text to the configured notifier URL, from the engine "
            "side. Prefer this over the telegram-notify skill's own direct "
            "HTTP push when this tool is present -- it records a "
            "`notification.sent` event (ts, endpoint, textSha256, "
            "httpStatus) on success, or the distinct `notification.failed` "
            "event (same fields) on failure, and the same entry in the "
            "run record's notifications section. A failed send is "
            "answered with an error and never fails the calling task."
        ),
        "usage": '{"text": "..."}',
    },
    RECORD_VERDICT_TOOL: {
        "label": "Checkpoint your verdict",
        "description": (
            "Checkpoint your verdict the MOMENT you can name one -- before "
            "you finish refining, before review-findings.md/.json are even "
            "complete. Written atomically, in the engine process, so it "
            "survives this iteration being killed a moment later. It is a "
            "CHECKPOINT ONLY: your final reply's own {\"verdict\": ...} "
            "contract line is still what decides the approach when this "
            "iteration runs to completion. Call it again whenever your "
            "verdict changes while you keep working -- each call replaces "
            "the previous checkpoint."
        ),
        "usage": '{"verdict": "verified", "reason": "...", '
                 '"findingsPath": "review-findings.md"}',
    },
}

# The parameter SCHEMA of each tool, declared beside its usage line so the
# two cannot disagree. This replaced the defect task 033's second validation
# attempt found: the generated extension used to register EVERY tool with a
# single required string field named `args`, while the prompt and the table
# above documented the real payload (`{"task": {...}}`, `{"taskId": ...}`),
# so a hat that followed the documentation could not make a schema-valid
# call at all. Each entry maps a TOP-LEVEL payload key to `(typebox source,
# required)`, and `tests/test_steer_hat_tools_live.py` pins that a tool's
# declared field names are EXACTLY the top-level keys of its documented
# usage payload here and of its usage line in `prompts/steer.md`.
_TASK_OBJECT = """Type.Object({
        id: Type.String({ description: "The new task's id, unique in the table." }),
        title: Type.Optional(Type.String()),
        successCriteria: Type.Optional(Type.String()),
        dependsOn: Type.Optional(Type.Array(Type.String())),
        priority: Type.Optional(Type.Number()),
        tier: Type.Optional(Type.String()),
        notes: Type.Optional(Type.String()),
      }, { additionalProperties: true })"""
_CHANGES_OBJECT = """Type.Object({
        title: Type.Optional(Type.String()),
        successCriteria: Type.Optional(Type.String()),
        dependsOn: Type.Optional(Type.Array(Type.String())),
        priority: Type.Optional(Type.Number()),
        notes: Type.Optional(Type.String()),
        status: Type.Optional(Type.String({
          description: "Routed through the engine's one status applier, which refuses an illegal transition.",
        })),
      }, { additionalProperties: true })"""


def _string(description: str) -> str:
    return f'Type.String({{ description: {json.dumps(description)} }})'


_TOOL_PARAMS: dict[str, dict[str, tuple[str, bool]]] = {
    "task-insert": {
        "task": (_TASK_OBJECT, True),
    },
    "task-update": {
        "taskId": (_string("The id of the task to change."), True),
        "changes": (_CHANGES_OBJECT, True),
        "justification": (_string(
            "Required (non-empty) when changes.status moves a 'failed' "
            "task to 'skipped'; defaults to changes.notes when omitted. "
            "Written into the task's own notes on that one move, appended "
            "after a distinct changes.notes rather than replaced by it."),
            False),
    },
    "task-delete": {
        "taskId": (_string("The id of the task to remove."), True),
    },
    "standing-rules": {
        "text": (_string("The full replacement standing-rules block."), True),
    },
    CONTEXT_READ_TOOL: {
        "file": (_string("notes.md, standing-rules.md, tasks.json or "
                         "prd.md."), True),
    },
    CONTEXT_WRITE_TOOL: {
        "file": (_string("notes.md or standing-rules.md."), True),
        "text": (_string("The file's full replacement contents."), True),
        "steeringEntry": (_string("The steering entry file name that caused "
                                  "this write."), False),
    },
    NOTIFY_TOOL: {
        "text": (_string("The text to send to the operator."), True),
    },
    RECORD_VERDICT_TOOL: {
        "verdict": (_string('"verified" or "rejected" -- exactly the two '
                            "legal values the final review contract "
                            "payload accepts."), True),
        "reason": (_string("Why, so far -- refine it in your final reply; "
                           "this checkpoint never has the last word."),
                   False),
        "findingsPath": (_string("Path to the findings file, when you "
                                 "have started one."), False),
    },
}

# THE declaration. Derived from `state.STEER_HAT_TOOLS` (never re-listed) so
# a tool added there without a doc entry raises a loud KeyError at import
# instead of shipping an undocumented tool, plus the two context-file tools
# task 033 adds. `tests/test_steer_hat_tools_live.py` pins the whole set by
# equality, the same shape `test_steer_hat_tool_surface.py` already pins
# `STEER_HAT_TOOLS` itself.
STEER_WIRED_TOOLS: dict[str, dict] = {
    name: {**_TOOL_DOCS[name], "params": _TOOL_PARAMS[name]}
    for name in (*state_module.STEER_HAT_TOOLS, CONTEXT_READ_TOOL,
                 CONTEXT_WRITE_TOOL, NOTIFY_TOOL)
}

# Task 022: the worker hat's own, much narrower tool surface -- just the
# send, never a task-table/standing-rules/context tool. A `SteerToolBroker`
# constructed with `tools=frozenset(WORKER_WIRED_TOOLS)` generates an
# extension registering exactly this one tool.
WORKER_WIRED_TOOLS: dict[str, dict] = {NOTIFY_TOOL: STEER_WIRED_TOOLS[NOTIFY_TOOL]}

# Task 025 (PRD requirement H.1/H.4, #264): the review hat's own, equally
# narrow tool surface -- just the checkpoint, never a task-table/standing-
# rules/context/notify tool. Deliberately NOT folded into
# `STEER_WIRED_TOOLS` (unlike `NOTIFY_TOOL`, which lives in both tables):
# `record-verdict` only ever makes sense during a review iteration, and
# widening the steer hat's own surface with it would be exactly the
# authority-boundary drift `_hat_extensions`'s docstring warns against.
REVIEW_WIRED_TOOLS: dict[str, dict] = {
    RECORD_VERDICT_TOOL: {**_TOOL_DOCS[RECORD_VERDICT_TOOL],
                          "params": _TOOL_PARAMS[RECORD_VERDICT_TOOL]},
}

# The union every lookup below indexes into -- `STEER_WIRED_TOOLS` plus
# whatever a narrower, non-steer broker wires that is NOT already in it
# (today just `REVIEW_WIRED_TOOLS`; `WORKER_WIRED_TOOLS` is already a
# subset of `STEER_WIRED_TOOLS` and contributes nothing new here).
_ALL_WIRED_TOOLS: dict[str, dict] = {**STEER_WIRED_TOOLS, **REVIEW_WIRED_TOOLS}


def usage_payload(tool: str) -> dict:
    """The tool's documented usage line, parsed. Its top-level keys are
    exactly the tool's parameter fields (`parameters_source`)."""
    return json.loads(_ALL_WIRED_TOOLS[tool]["usage"])


def parameters_source(tool: str) -> str:
    """Render `tool`'s typebox parameter object: one property per documented
    top-level payload key, `Type.Optional` where the declaration says so. The
    hat therefore calls the tool with EXACTLY the payload the prompt
    documents -- there is no wrapper field to stringify a payload into, which
    is the defect this replaced (a single required string field named `args`
    against a prompt documenting `{"task": {...}}`: no schema-valid call
    existed for the documented usage)."""
    lines = ["Type.Object({"]
    for field_name, (typebox, required) in _ALL_WIRED_TOOLS[tool][
            "params"].items():
        rendered = typebox if required else f"Type.Optional({typebox})"
        lines.append(f"      {field_name}: {rendered},")
    lines.append("    })")
    return "\n".join(lines)


def context_files(run: RunDir) -> dict[str, tuple[Path, bool]]:
    """The run-context files the context tools can reach: name -> (path,
    writable). Deliberately a small explicit allowlist keyed by bare name --
    a hat-supplied path is never joined onto the run dir, so no `../` escape
    exists to defend against, and `tasks.json` is readable but NOT writable
    here (its three task-* tools are its only writers, keeping every
    applier guarantee on the one path that has them)."""
    return {
        state_module.NOTES_FILE: (run.notes_file, True),
        state_module.STANDING_RULES_FILE: (run.standing_rules_file, True),
        "tasks.json": (run.tasks_file, False),
        state_module.PRD_FILE: (run.prd_file, False),
    }


# -- the engine-side broker ------------------------------------------------


@dataclass
class SteerToolBroker:
    """Serves the steer hat's tool calls, in the engine process.

    `steering` is the steering entry file names this steer pass is
    processing (`RunDir.pending_steering()` at the moment the pass began) --
    carried onto every audit event so a write can always be traced back to
    the operator message that caused it, even when the hat names no entry
    itself.
    """

    run: RunDir
    vigilant: bool = False
    steering: tuple[str, ...] = ()
    served: list[dict] = field(default_factory=list)
    # Task 022: `tools=None` (every existing caller, unchanged) keeps this
    # broker's full historical set (`STEER_WIRED_TOOLS`) -- the steer hat's
    # authority boundary. `LoopSupervisor._run_worker_iteration` passes
    # `tools=frozenset(WORKER_WIRED_TOOLS)` to get a broker that serves
    # `notify` and refuses everything else, both in the generated
    # extension (nothing else is even registered) and defensively in
    # `_execute` (a raw spooled request naming an unwired tool is refused
    # the same way an unknown tool name always was).
    tools: "frozenset[str] | None" = None
    # The URL the `notify` tool POSTs to; `None` means unconfigured (the
    # tool still answers and still records the event, it just never sends).
    notifier_url: str | None = None
    # Named on every `steer.tool_call` audit event this broker emits --
    # "steer" for the steer hat (unchanged default), "worker" for the
    # worker hat's own narrower broker.
    source: str = "steer"

    def wired_tools(self) -> dict[str, dict]:
        """This broker's own tool set -- `STEER_WIRED_TOOLS` unless `tools`
        narrows it (the worker hat's broker, restricted to `notify`; the
        review hat's broker, restricted to `record-verdict`, which is not
        even a member of `STEER_WIRED_TOOLS` -- so the narrowed lookup below
        reads from `_ALL_WIRED_TOOLS`, the union, rather than filtering the
        steer hat's own table down)."""
        if self.tools is None:
            return STEER_WIRED_TOOLS
        return {k: v for k, v in _ALL_WIRED_TOOLS.items() if k in self.tools}

    @property
    def base_dir(self) -> Path:
        return self.run.root / HAT_TOOLS_DIRNAME

    @property
    def requests_dir(self) -> Path:
        return self.base_dir / REQUESTS_DIRNAME

    @property
    def responses_dir(self) -> Path:
        return self.base_dir / RESPONSES_DIRNAME

    @property
    def extension_file(self) -> Path:
        return self.base_dir / EXTENSION_FILENAME

    def place(self) -> Path:
        """Create the spool and write the generated `pi` extension. Returns
        the extension path, which the steer iteration is launched with
        (`-e <path>`) and nothing else ever is -- the tools exist for the
        steer hat and for no other hat."""
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.extension_file,
                     extension_source(self.run.root, sys.executable,
                                      tools=self.wired_tools()))
        return self.extension_file

    # -- execution -------------------------------------------------------

    def execute(self, tool: str, args: dict) -> dict:
        """Run ONE tool call, in this (the engine's) process, and emit its
        audit event. Returns `{"ok": bool, ...}` -- never raises for a
        hat-supplied shape the tool cannot use; a refusal is an answer the
        hat can read and correct."""
        record = self._execute(tool, args)
        self.run.emit(EVENT_TOOL_CALL, tool=tool, ok=record.get("ok", False),
                      source=self.source, steering=list(self.steering),
                      detail=record.get("detail"),
                      error=record.get("error"))
        self.served.append({"tool": tool, "args": args, "response": record})
        return record

    def _execute(self, tool: str, args: dict) -> dict:
        if tool not in self.wired_tools():
            return {"ok": False, "error": f"unknown tool {tool!r}",
                    "detail": None}
        if not isinstance(args, dict):
            return {"ok": False, "error": "args must be a JSON object",
                    "detail": None}
        try:
            if tool == NOTIFY_TOOL:
                return self._notify(args)
            if tool == "task-insert":
                task = args.get("task")
                if not isinstance(task, dict) or not task.get("id"):
                    return {"ok": False, "detail": None,
                            "error": 'task-insert needs {"task": {"id": ...}}'}
                result = state_module.STEER_HAT_TOOLS["task-insert"](
                    self.run, task)
                return {"ok": bool(result.get("inserted")), "detail": result,
                        "error": result.get("reason")}
            if tool == "task-update":
                task_id = args.get("taskId") or args.get("task_id")
                changes = args.get("changes")
                if not task_id or not isinstance(changes, dict):
                    return {"ok": False, "detail": None,
                            "error": 'task-update needs {"taskId": ..., '
                                     '"changes": {...}}'}
                result = state_module.STEER_HAT_TOOLS["task-update"](
                    self.run, task_id, changes, self.vigilant,
                    justification=args.get("justification"))
                status_result = result.get("statusResult")
                ok = bool(result.get("updated"))
                error = result.get("reason")
                if status_result is not None and not status_result.get("applied"):
                    # A status move that the one applier refused is a
                    # refusal of THIS call, surfaced verbatim with the
                    # applier's own documented diagnostic -- never
                    # swallowed because the other fields happened to land.
                    if status_result.get("reason") != "already at that status":
                        ok = False
                        error = (f"status change refused: "
                                 f"{status_result.get('reason')}")
                # Task 012 (PRD requirement D.1/D.2, #184): a `dependsOn`
                # write refused because the picker has already claimed the
                # target task is a refusal of THIS call too -- surfaced the
                # same way the status refusal above already is, with the
                # applier's own diagnostic (naming the task, its status and
                # the remedy) never swallowed just because other fields in
                # the same call happened to land.
                depends_on_result = result.get("dependsOnResult")
                if depends_on_result is not None and not depends_on_result.get(
                        "applied"):
                    ok = False
                    error = depends_on_result.get("reason")
                return {"ok": ok, "detail": result, "error": error}
            if tool == "task-delete":
                task_id = args.get("taskId") or args.get("task_id")
                if not task_id:
                    return {"ok": False, "detail": None,
                            "error": 'task-delete needs {"taskId": ...}'}
                result = state_module.STEER_HAT_TOOLS["task-delete"](
                    self.run, task_id)
                return {"ok": bool(result.get("deleted")), "detail": result,
                        "error": result.get("reason")}
            if tool == "standing-rules":
                text = args.get("text")
                if not isinstance(text, str) or not text.strip():
                    return {"ok": False, "detail": None,
                            "error": 'standing-rules needs {"text": "..."}'}
                result = state_module.STEER_HAT_TOOLS["standing-rules"](
                    self.run, text)
                self._emit_context_write(
                    state_module.STANDING_RULES_FILE, len(text),
                    args.get("steeringEntry"))
                return {"ok": bool(result.get("written")),
                        "detail": {"file": state_module.STANDING_RULES_FILE,
                                   "bytes": len(text)},
                        "error": None}
            if tool == CONTEXT_READ_TOOL:
                return self._context_read(args)
            if tool == RECORD_VERDICT_TOOL:
                return self._record_verdict(args)
            return self._context_write(args)
        except OSError as exc:                       # pragma: no cover
            return {"ok": False, "detail": None, "error": f"{type(exc).__name__}: {exc}"}

    def _context_read(self, args: dict) -> dict:
        name = args.get("file")
        targets = context_files(self.run)
        if name not in targets:
            return {"ok": False, "detail": None,
                    "error": f"unknown context file {name!r}; "
                             f"names: {sorted(targets)}"}
        path, _writable = targets[name]
        try:
            text = path.read_text()
        except FileNotFoundError:
            return {"ok": False, "detail": {"file": name},
                    "error": f"{name} does not exist yet"}
        return {"ok": True, "detail": {"file": name, "bytes": len(text)},
                "text": text, "error": None}

    def _context_write(self, args: dict) -> dict:
        name = args.get("file")
        text = args.get("text")
        targets = context_files(self.run)
        if name not in targets:
            return {"ok": False, "detail": None,
                    "error": f"unknown context file {name!r}; "
                             f"names: {sorted(targets)}"}
        path, writable = targets[name]
        if not writable:
            return {"ok": False, "detail": {"file": name},
                    "error": f"{name} is not writable through this tool; "
                             f"the task-* tools are its only writers"}
        if not isinstance(text, str):
            return {"ok": False, "detail": {"file": name},
                    "error": 'context-write needs {"file": ..., "text": "..."}'}
        if name == state_module.STANDING_RULES_FILE:
            state_module.STEER_HAT_TOOLS["standing-rules"](self.run, text)
        else:
            atomic_write(path, text)
        self._emit_context_write(name, len(text), args.get("steeringEntry"))
        return {"ok": True, "detail": {"file": name, "bytes": len(text)},
                "error": None}

    def _emit_context_write(self, name: str, size: int,
                            steering_entry: str | None) -> None:
        """The second audit event a write emits: the file it wrote and the
        steering entry that caused it (the hat's own `steeringEntry` when it
        named one, otherwise every entry this steer pass is processing)."""
        entries = ([steering_entry] if steering_entry
                   else list(self.steering))
        self.run.emit(EVENT_CONTEXT_WRITE, file=name, bytes=size,
                      source=self.source, steering=entries)

    def _notify(self, args: dict) -> dict:
        """Task 022 (PRD requirement B.3, #140): the `notify` tool's own
        execution -- `send_notification` does the actual work (and the
        actual `notification.sent`/`notification.failed` event, split by
        outcome per task 009/requirement B.1), this just validates the
        shape the hat sent."""
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "detail": None,
                    "error": 'notify needs {"text": "..."}'}
        return send_notification(self.run, text, self.notifier_url)

    def _record_verdict(self, args: dict) -> dict:
        """Task 025 (PRD requirement H.1/H.4, #264): the review hat's
        `record-verdict` tool -- writing the early-verdict checkpoint,
        HERE, in the engine process, so the write (and its rename-into-
        place) happens and is durable regardless of whatever the hat's own
        subprocess does next, including being SIGKILLed the instant after
        this call returns.

        `verdict` must be exactly `"verified"` or `"rejected"` -- the same
        two legal values the ordinary `review` contract payload accepts
        (`transitions.HAT_SCHEMAS["review"]`); anything else is refused
        with an error the hat can read and correct, never silently
        coerced. `approach` is never taken from the hat's own args -- it is
        read off `status.json`, the engine's own record, so a checkpoint
        can never claim an approach number the hat merely asserts.
        """
        verdict = args.get("verdict")
        if verdict not in ("verified", "rejected"):
            return {"ok": False, "detail": None,
                    "error": 'record-verdict needs {"verdict": '
                             '"verified"|"rejected", ...}'}
        approach = self.run.read_status().get("approach") or 1
        reason = args.get("reason")
        findings_path = args.get("findingsPath")
        payload = state_module.write_review_early_verdict(
            self.run.root, approach=approach, verdict=verdict,
            reason=reason if isinstance(reason, str) else None,
            findings_path=findings_path if isinstance(findings_path, str)
            else None)
        self.run.emit(EVENT_EARLY_VERDICT, approach=approach,
                      verdict=verdict, source=self.source)
        return {"ok": True, "detail": payload, "error": None}

    # -- serving ---------------------------------------------------------

    def serve_once(self) -> int:
        """Execute every request currently spooled, oldest first, and answer
        each one. Returns how many were served."""
        try:
            pending = sorted(self.requests_dir.glob("*.json"))
        except OSError:                              # pragma: no cover
            return 0
        count = 0
        for path in pending:
            try:
                request = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                request = {"id": path.stem, "tool": None}
                response = {"ok": False, "detail": None,
                            "error": f"unreadable request: {exc}"}
            else:
                response = self.execute(request.get("tool"),
                                        request.get("args") or {})
            self._answer(request.get("id") or path.stem,
                         request.get("tool"), response)
            with contextlib.suppress(OSError):
                path.unlink()
            count += 1
        return count

    def _answer(self, request_id: str, tool: str | None,
                response: dict) -> None:
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.responses_dir / f"{request_id}.json",
                     json.dumps({"id": request_id, "tool": tool, **response}))

    async def serve(self, interval: float = BROKER_POLL_INTERVAL_S) -> None:
        """Serve until cancelled -- one asyncio task in the engine process,
        alive for exactly as long as the steer iteration is."""
        while True:
            self.serve_once()
            await asyncio.sleep(interval)


# -- notifications (PRD requirement B.3, #140, task 022) ------------------

def _http_post_text(url: str, text: str,
                    timeout_s: float) -> tuple[int | None, str | None]:
    """The real POST -- `{"text": text}`, matching the telegram-notify
    skill's own `/notify` shape exactly, so the same notifier serves either
    caller. Returns `(status, error)`: `error` is set only when no HTTP
    status came back at all (connection refused, DNS, timeout) -- a 4xx/5xx
    is returned as its own status, same as a 2xx, with `error` left `None`
    (the caller derives "failed" from the status range, not from this)."""
    body = json.dumps({"text": text}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return None, str(exc)


def send_notification(run: RunDir, text: str, url: str | None, *,
                      poster=None, timeout_s: float = 15.0,
                      max_attempts: int = 2) -> dict:
    """POST `text` to `url` (the engine's own configured notifier -- PRD
    requirement B.3) and record an event carrying EXACTLY the field set
    the requirement names: `ts` (the event's own, from `RunDir.emit`),
    `endpoint`, `textSha256`, `httpStatus` -- never an `ok`/`error` field
    on the persisted event itself.

    Task 009 (PRD requirement B.1, #197) splits the event NAME on outcome:
    a successful send (a poster status in [200, 300)) emits
    `NOTIFICATION_SENT_EVENT` (`notification.sent`); an unconfigured URL,
    a connection error (`status is None`), and a non-2xx status are all a
    FAILURE and instead emit the distinct `NOTIFICATION_FAILED_EVENT`
    (`notification.failed`) -- never both, and never `notification.sent`
    gaining an `ok` field, because the point of the split is that a reader
    grepping the `sent` name for "was the operator told" gets a true
    answer. The field SET is identical either way; only the event's own
    `type` differs.

    Never raises: every one of the above outcomes is answered the same
    way -- exactly one event is written, and `{"ok": False, ...}` carries
    an `error` string back to whatever called this (the `notify` tool's
    response, read by the hat; never a reason to fail the calling
    task/iteration).

    `poster` is `(url, text, timeout_s) -> (status, error)`, defaulting to
    the real `_http_post_text` -- the one seam a test stubs to cover a
    success and a failure without a real network call.

    Task 017 (PRD requirement A.7, #224): a first attempt that fails (a
    connection error, or a non-2xx status -- the SAME `ok` test the event
    split above already uses) is retried up to `max_attempts` (default
    2 -- exactly one retry, immediately, no backoff), before either event
    is written -- an escalation notifier is the one send this run may
    never get a second chance to raise through any other channel, so a
    single transient failure (a notifier warming up, one dropped
    connection) must not silently lose it. Still exactly ONE event per
    call, whatever `max_attempts` is: only the FINAL attempt's outcome is
    recorded, so a reader of `notification.sent`/`notification.failed`
    never sees two rows for one logical send. A first attempt that
    already succeeds never retries at all -- the common case costs
    nothing extra.

    Task cure-01-01-3 (PRD requirement A.4): `max_attempts=1` (passed by
    `LoopSupervisor._notify_escalation_delivery`'s own OUTER retry loop,
    `engine/loop.py`) disables this retry-once entirely, so a caller that
    already retries with backoff at a higher level can bound the TOTAL
    number of real POSTs across both layers instead of paying for both:
    the outer loop's own attempt count is exactly the number of real
    POSTs made, with none hidden inside this call.
    """
    poster = poster or _http_post_text
    text_sha256 = hashlib.sha256(text.encode()).hexdigest()
    if not url:
        record = {"endpoint": None, "textSha256": text_sha256,
                  "httpStatus": None}
        run.emit(NOTIFICATION_FAILED_EVENT, **record)
        return {"ok": False, "detail": record,
                "error": "no notifier URL configured"}
    status, error = poster(url, text, timeout_s)
    ok = status is not None and 200 <= status < 300
    attempt = 1
    while not ok and attempt < max_attempts:
        status, error = poster(url, text, timeout_s)
        ok = status is not None and 200 <= status < 300
        attempt += 1
    record = {"endpoint": url, "textSha256": text_sha256,
              "httpStatus": status}
    run.emit(NOTIFICATION_SENT_EVENT if ok else NOTIFICATION_FAILED_EVENT,
            **record)
    if ok:
        return {"ok": True, "detail": record, "error": None}
    return {"ok": False, "detail": record,
            "error": error or f"notifier responded {status}"}


# -- the hat-side client ---------------------------------------------------


def call(run_root: Path, tool: str, args: dict,
         timeout_s: float = CLIENT_TIMEOUT_S) -> dict:
    """Ask the engine to run one tool, from the HAT's side of the boundary
    (a different process, and under `privsep` a different uid). Writes the
    request atomically -- rename, so the broker never reads a half-written
    file -- then waits for the engine's answer.

    A timeout is an answer too (`ok: False`), never a hang: the hat is told
    the engine did not respond and can report `settled: false` instead of
    burning its whole iteration in a blocking read.
    """
    base = Path(run_root) / HAT_TOOLS_DIRNAME
    requests_dir = base / REQUESTS_DIRNAME
    responses_dir = base / RESPONSES_DIRNAME
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    payload = json.dumps({"id": request_id, "tool": tool, "args": args,
                          "ts": time.time()})
    tmp = requests_dir / f".{request_id}.tmp"
    tmp.write_text(payload)
    tmp.rename(requests_dir / f"{request_id}.json")
    answer = responses_dir / f"{request_id}.json"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if answer.exists():
            try:
                return json.loads(answer.read_text())
            except json.JSONDecodeError:             # pragma: no cover
                pass
        time.sleep(CLIENT_POLL_INTERVAL_S)
    return {"id": request_id, "tool": tool, "ok": False,
            "error": f"the engine did not answer within {timeout_s}s"}


def main(argv: list[str] | None = None) -> int:
    """`python -m ralphd.engine.hat_tools <tool>` with the argument object on
    stdin: the one client implementation, called by the generated extension
    so the protocol is written down exactly once."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(json.dumps({"ok": False, "error": "usage: <tool> (args JSON on stdin)"}))
        return 2
    tool = argv[0]
    raw = sys.stdin.read().strip() or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"args are not JSON: {exc}"}))
        return 2
    run_root = os.environ.get(RUN_DIR_ENV)
    if not run_root:
        print(json.dumps({"ok": False,
                          "error": f"{RUN_DIR_ENV} is not set"}))
        return 2
    response = call(Path(run_root), tool, args)
    print(json.dumps(response))
    return 0 if response.get("ok") else 1


# -- the generated `pi` extension -----------------------------------------


def extension_source(run_root: Path, python_bin: str,
                     tools: dict[str, dict] | None = None) -> str:
    """Render a `pi` extension: one `pi.registerTool()` per entry in
    `tools` (default `STEER_WIRED_TOOLS`, the steer hat's full set -- the
    worker hat's broker passes its own narrower `wired_tools()` so the
    generated extension registers `notify` and nothing else), each
    declaring the SAME parameter object its documented usage line shows
    (`parameters_source`) and executing by handing that very object to the
    engine through the spool above. Both the registered set and each
    tool's argument shape are generated from the one declaration, so
    neither can drift from what the engine wired or from what
    `prompts/steer.md` tells the hat to send."""
    tools = STEER_WIRED_TOOLS if tools is None else tools
    blocks = []
    for name, doc in tools.items():
        description = (f"{doc['description']}\n\nArguments (JSON object): "
                       f"{doc['usage']}")
        blocks.append(f'''  pi.registerTool({{
    name: {json.dumps(name)},
    label: {json.dumps(doc["label"])},
    description: {json.dumps(description)},
    parameters: {parameters_source(name)},
    async execute(_id: string, params: Record<string, unknown>) {{
      return callEngine({json.dumps(name)}, params);
    }},
  }});''')
    registrations = "\n\n".join(blocks)
    return f'''// Generated by ralphd (engine/hat_tools.py) for the steer hat.
// Every tool here EXECUTES IN THE ENGINE PROCESS: this extension only hands
// the call over the run dir's request/response spool and reports the answer.
// Each tool's parameters are the exact JSON payload documented in
// prompts/steer.md -- the call the hat is told to make is the call the
// schema accepts, and it is forwarded verbatim.
import type {{ ExtensionAPI }} from "@earendil-works/pi-coding-agent";
import {{ Type }} from "typebox";
import {{ spawnSync }} from "node:child_process";

const RUN_DIR = {json.dumps(str(run_root))};
const PYTHON = {json.dumps(str(python_bin))};

function callEngine(tool: string, params: Record<string, unknown>) {{
  const proc = spawnSync(PYTHON, ["-m", "ralphd.engine.hat_tools", tool], {{
    input: JSON.stringify(params ?? {{}}),
    encoding: "utf8",
    env: {{ ...process.env, {json.dumps(RUN_DIR_ENV)}: RUN_DIR }},
  }});
  const text = (proc.stdout || "") + (proc.stderr || "");
  return {{ content: [{{ type: "text" as const, text: text.trim() }}], details: {{}} }};
}}

export default function (pi: ExtensionAPI) {{
{registrations}
}}
'''


if __name__ == "__main__":                           # pragma: no cover
    raise SystemExit(main())
