"""Task 013: tasks.json has exactly one authorized writer,
`RunState.write_tasks` (engine/state.py), and everything else that used to
call `atomic_write_json(self.run.tasks_file, ...)` directly -- three sites
in loop.py at HEAD (:2478, :2508, :2800) -- now routes through it.

The audit below is a structural (AST) scan, not a list of known-good call
sites: it walks every module under `src/ralphd`, finds every call to a
write-shaped function (`atomic_write_json`, or a raw `.write_text`/
`.write_bytes`/`json.dump` on the tasks file) whose target expression
mentions `tasks_file`, and fails if that call sits anywhere other than
inside the one designated method. A planted second writer (mirroring
tests/test_response_contracts.py's and tests/test_ralphctl_resolution.py's
shape) proves the audit actually catches one rather than passing by
omission.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "ralphd"
STATE_PATH = SRC_ROOT / "engine" / "state.py"

DESIGNATED_MODULE = STATE_PATH
DESIGNATED_FUNC = "write_tasks"

# Any call to one of these, with a target expression that mentions the
# tasks_file marker below, is a write to tasks.json.
WRITE_CALL_NAMES = frozenset({"atomic_write_json", "write_text", "write_bytes", "dump"})
TASKS_FILE_MARKER = "tasks_file"


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _mentions_tasks_file(node: ast.AST) -> bool:
    return TASKS_FILE_MARKER in ast.dump(node)


def _target_expr(call: ast.Call) -> ast.AST | None:
    """The expression a write call is writing *to*: the positional arg for
    a free function (`atomic_write_json(path, obj)`), or the receiver for
    a method call (`path.write_text(...)`, `json.dump(obj, fh)` -- fh is
    what we want for dump, but `.write_text`/`.write_bytes` want the
    receiver). Good enough for an audit that only needs to recognise
    "this expression is (or derives from) `tasks_file`", not to fully
    resolve dataflow.
    """
    if isinstance(call.func, ast.Attribute):
        return call.func.value  # the `x` in `x.write_text(...)`
    if call.args:
        return call.args[0]
    return None


def _enclosing_function(tree: ast.AST, node: ast.AST):
    """Innermost FunctionDef/AsyncFunctionDef whose line range contains
    `node`, or None if it's module-level code."""
    best = None
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(fn, "end_lineno", fn.lineno)
            if fn.lineno <= node.lineno <= end:
                if best is None or fn.lineno > best.lineno:
                    best = fn
    return best


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return path.name  # planted fixture outside the repo (mutation case)


def _offences(path: Path, *, designated_path: Path | None = None) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offences: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) not in WRITE_CALL_NAMES:
            continue
        target = _target_expr(node)
        if target is None or not _mentions_tasks_file(target):
            continue
        fn = _enclosing_function(tree, node)
        is_designated = (
            designated_path is not None
            and path == designated_path
            and fn is not None
            and fn.name == DESIGNATED_FUNC
        )
        if is_designated:
            continue
        offences.append(
            f"{_display_path(path)}:{node.lineno}: writes tasks_file outside "
            f"the one designated {DESIGNATED_FUNC}() path (task 013) -- route "
            "through RunState.write_tasks() instead")
    return offences


def _scan_repo() -> list[str]:
    modules = sorted(SRC_ROOT.rglob("*.py"))
    offences: list[str] = []
    for path in modules:
        offences.extend(_offences(path, designated_path=DESIGNATED_MODULE))
    return offences


def test_the_scan_finds_modules():
    modules = sorted(SRC_ROOT.rglob("*.py"))
    assert len(modules) > 10, "the glob under src/ralphd found suspiciously little"


def test_no_module_writes_tasks_file_outside_the_designated_path():
    offences = _scan_repo()
    assert offences == [], (
        "tasks.json has grown a second writer (task 013's whole point was to "
        "prevent this):\n  " + "\n  ".join(offences))


def test_the_designated_module_defines_exactly_one_write_tasks():
    tree = ast.parse(STATE_PATH.read_text(encoding="utf-8"))
    defs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == DESIGNATED_FUNC
    ]
    assert len(defs) == 1, (
        f"expected exactly one {DESIGNATED_FUNC}() in {STATE_PATH}, "
        f"found {len(defs)}")


def test_write_tasks_itself_is_not_flagged_as_an_offence():
    # Sanity check on the scan: state.py obviously *does* call
    # atomic_write_json against self.tasks_file inside write_tasks() --
    # if scanning it produced an offence, the "designated path" exemption
    # would be vacuous and every future run of the real test would be a
    # false positive against the harness's own only writer.
    offences = _offences(STATE_PATH, designated_path=DESIGNATED_MODULE)
    assert offences == []


def test_the_audit_catches_a_planted_second_writer_via_atomic_write_json(tmp_path):
    """Mutation case: a brand-new module, elsewhere in the tree, that calls
    atomic_write_json directly against something named tasks_file must be
    caught -- whatever it's named, however it gets the path."""
    planted = tmp_path / "rogue_writer.py"
    planted.write_text(
        "def _sneaky_persist(run):\n"
        "    atomic_write_json(run.tasks_file, {'tasks': []})\n")
    offences = _offences(planted, designated_path=DESIGNATED_MODULE)
    assert len(offences) == 1
    assert "_sneaky_persist" not in offences[0]  # the message names the file:line, not fn
    assert "rogue_writer.py:2" in offences[0]


def test_the_audit_catches_a_planted_second_writer_via_write_text(tmp_path):
    """Mutation case: bypassing atomic_write_json entirely with a raw
    `.write_text(json.dumps(...))` against the tasks file must also be
    caught -- the property name is the tell, not the helper used."""
    planted = tmp_path / "rogue_write_text.py"
    planted.write_text(
        "import json\n"
        "\n"
        "def _also_sneaky(run, data):\n"
        "    run.tasks_file.write_text(json.dumps(data))\n")
    offences = _offences(planted, designated_path=DESIGNATED_MODULE)
    assert len(offences) == 1
    assert "rogue_write_text.py:4" in offences[0]


def test_a_write_inside_write_tasks_itself_is_exempt(tmp_path):
    """Positive control: if the planted module happens to define its own
    write_tasks(), the exemption only fires when designated_path also
    points at *that* module -- i.e. the exemption is per-module, not
    per-function-name globally."""
    planted = tmp_path / "impersonator.py"
    planted.write_text(
        "def write_tasks(run, data):\n"
        "    atomic_write_json(run.tasks_file, data)\n")
    # Without pointing designated_path at this file, the impersonator is
    # still an offence -- only the real state.py is exempted.
    offences = _offences(planted, designated_path=DESIGNATED_MODULE)
    assert len(offences) == 1
    # But scanned as if IT were the designated module, the same call is
    # exempt -- confirms the exemption logic itself (module + function
    # name) is what's being tested above, not some accidental match.
    offences_if_designated = _offences(planted, designated_path=planted)
    assert offences_if_designated == []


def test_a_read_of_tasks_file_is_not_flagged(tmp_path):
    """Negative control: reading tasks_file (no write-shaped call at all)
    must never be flagged -- the audit is about writers, not references."""
    planted = tmp_path / "reader_only.py"
    planted.write_text(
        "def read_it(run):\n"
        "    return run.read_tasks_result().doc\n"
        "\n"
        "def also_fine(run):\n"
        "    return run.tasks_file.read_text()\n")
    assert _offences(planted, designated_path=DESIGNATED_MODULE) == []


def test_loop_pys_three_known_sites_now_route_through_write_tasks():
    """The three call sites the task names (loop.py:2478, :2508, :2800 at
    the commit that introduced this test) must have moved off
    atomic_write_json(self.run.tasks_file, ...) and onto
    self.run.write_tasks(...); this is a source-text check (not line-number
    pinned, since the task only promises those sites route through the one
    path, not that they stay on those exact lines forever) that there are
    zero remaining direct writes in loop.py and at least three calls to the
    new single path."""
    loop_path = SRC_ROOT / "engine" / "loop.py"
    assert _offences(loop_path, designated_path=DESIGNATED_MODULE) == []
    tree = ast.parse(loop_path.read_text(encoding="utf-8"))
    write_tasks_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == DESIGNATED_FUNC
    ]
    assert len(write_tasks_calls) >= 3, (
        f"expected loop.py's (at least) three known write sites to call "
        f"{DESIGNATED_FUNC}(); found {len(write_tasks_calls)}")
