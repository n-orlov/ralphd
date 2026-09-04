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
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import state as state_module
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

# -- the wired tool set ----------------------------------------------------

CONTEXT_READ_TOOL = "context-read"
CONTEXT_WRITE_TOOL = "context-write"

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
            "justification is written into the task's own `notes`."
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
            "Written into the task's own notes on that one move."), False),
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
                 CONTEXT_WRITE_TOOL)
}


def usage_payload(tool: str) -> dict:
    """The tool's documented usage line, parsed. Its top-level keys are
    exactly the tool's parameter fields (`parameters_source`)."""
    return json.loads(STEER_WIRED_TOOLS[tool]["usage"])


def parameters_source(tool: str) -> str:
    """Render `tool`'s typebox parameter object: one property per documented
    top-level payload key, `Type.Optional` where the declaration says so. The
    hat therefore calls the tool with EXACTLY the payload the prompt
    documents -- there is no wrapper field to stringify a payload into, which
    is the defect this replaced (a single required string field named `args`
    against a prompt documenting `{"task": {...}}`: no schema-valid call
    existed for the documented usage)."""
    lines = ["Type.Object({"]
    for field_name, (typebox, required) in STEER_WIRED_TOOLS[tool][
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
                     extension_source(self.run.root, sys.executable))
        return self.extension_file

    # -- execution -------------------------------------------------------

    def execute(self, tool: str, args: dict) -> dict:
        """Run ONE tool call, in this (the engine's) process, and emit its
        audit event. Returns `{"ok": bool, ...}` -- never raises for a
        hat-supplied shape the tool cannot use; a refusal is an answer the
        hat can read and correct."""
        record = self._execute(tool, args)
        self.run.emit(EVENT_TOOL_CALL, tool=tool, ok=record.get("ok", False),
                      source="steer", steering=list(self.steering),
                      detail=record.get("detail"),
                      error=record.get("error"))
        self.served.append({"tool": tool, "args": args, "response": record})
        return record

    def _execute(self, tool: str, args: dict) -> dict:
        if tool not in STEER_WIRED_TOOLS:
            return {"ok": False, "error": f"unknown tool {tool!r}",
                    "detail": None}
        if not isinstance(args, dict):
            return {"ok": False, "error": "args must be a JSON object",
                    "detail": None}
        try:
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
                      source="steer", steering=entries)

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


def extension_source(run_root: Path, python_bin: str) -> str:
    """Render the steer hat's `pi` extension: one `pi.registerTool()` per
    entry in `STEER_WIRED_TOOLS`, each declaring the SAME parameter object
    its documented usage line shows (`parameters_source`) and executing by
    handing that very object to the engine through the spool above. Both the
    registered set and each tool's argument shape are generated from the one
    declaration, so neither can drift from what the engine wired or from what
    `prompts/steer.md` tells the hat to send."""
    blocks = []
    for name, doc in STEER_WIRED_TOOLS.items():
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
