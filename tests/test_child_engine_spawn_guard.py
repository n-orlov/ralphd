"""Task 010 (v0.12 PRD, #83 class, D's "cheap static half"): the runtime fix
(task 001) made the shared launchers isolate a spawned child engine's `HOME`
by default, with exactly one greppable opt-in (`AMBIENT_HOME`,
`tests/conftest.py::_resolved_home_env`). That fix only protects call sites
that actually go *through* those two classes. Nothing stopped a THIRD module
from reaching past them and spawning `ralphd-engine` with a raw
`subprocess.Popen`/`run` call whose `env=` silently inherits this pytest
process's own ambient `$HOME` -- exactly the #83 shape, reintroduced by a
different door. This module is the static guard PRD requirement D and
Definition-of-Done item 6 ask for: it scans `tests/` for that door and fails,
naming file and line, when it finds one standing open.

Scope, stated precisely because a scan like this is only honest if its scope
is: this guard's target is a raw `subprocess.{Popen,run,check_call,
check_output}` call whose argv's first element resolves -- directly, as
`str(RALPHD_ENGINE)`/`RALPHD_ENGINE`, or through one lexical local alias
(`engine = str(RALPHD_ENGINE)` earlier in the same enclosing function) -- to
`RALPHD_ENGINE`, i.e. a spawn of the child *engine* binary itself, the literal
subject of Definition-of-Done item 6 ("no e2e/live module can spawn a CHILD
ENGINE..."). A raw `ralphctl` spawn against a hand-written registry with no
engine anywhere in the picture (the `_ctl()`-helper shape used by ~30
`test_cli_*`/`test_hub_*` modules, each explicitly documented as running
"over hand-written registries (container gone)") never constructs a child
engine at all, so Definition-of-Done item 6 does not reach it -- and
`ambient_home_allowlist.py`'s own entry for `cli/main.py:155` already reasons
that a bare `ralphctl` invocation's ambient-`$HOME` fallback is correct for
exactly that case (task 007). The one sanctioned pattern that pairs a live
engine with a `ralphctl` spawn against it, `tests/conftest.py::LiveRun.
ralphctl`, is covered here too, for free, by the same class-membership rule
that already exempts `LiveRun.__init__`'s own engine spawn (below) -- so a
reimplementation of that *pairing* outside `LiveRun` would still
need its own engine spawn caught by this scan, which it would be.

A guarded spawn is compliant if ANY of the following holds, each read off
the textual source of the OUTERMOST function that lexically contains the
call (so a spawn hidden in a closure still sees a `HOME`/`AMBIENT_HOME`
literal written in the enclosing test function, one level up):

  1. the call is a method of a class literally named `LiveRun` -- "goes
     through the shared launcher" verbatim. Until task 021 (G.1) this set
     also held `EngineProc`, tests/test_e2e.py's second, near-identical
     launcher; that class is gone, folded into `LiveRun`, so the exempt set
     names one class again;
  2. the enclosing function's source calls `_resolved_home_env(` --
     task 001's own isolation-by-default mechanism;
  3. the enclosing function's source names `AMBIENT_HOME` -- task 001's one
     opt-in spelling;
  4. the enclosing function's source sets an explicit `"HOME"` key (a dict
     literal `"HOME": ...` or a `[...]["HOME"] = ...` subscript assignment).

Known, stated limitations (a coarse lexical scan, same tradeoff
`test_ambient_home_allowlist_guard.py` already makes for `src/ralphd/`):

  * only catches `subprocess.<attr>(...)` calls qualified through a module
    name literally spelled `subprocess` (no bare `from subprocess import
    run` forms -- none exist in `tests/` today);
  * alias resolution is ONE lexical hop (`engine = str(RALPHD_ENGINE)`), not
    full dataflow -- a spawn target passed through two or more local
    variables would not be recognized as a guarded spawn at all (a false
    negative, not a false positive);
  * compliance markers are read off the OUTERMOST enclosing function's
    *entire* source text, not scoped to the exact lines that build that
    specific call's `env=`, so an unrelated `"HOME"` literal elsewhere in an
    unusually large enclosing function could hide a real violation. Every
    enclosing function actually scanned in this tree today is a small,
    single-purpose test function or fixture, so this has not mattered yet.

Two mutation cases below (synthetic source strings written to a `tmp_path`
scratch tree, never to the real `tests/` tree) prove the scanner both fires
on a planted violation and stays quiet on a planted compliant call.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# xdist_group (task 003, G.3): an AMBIENT_HOME-family module -- keeps this
# module's tests on one worker under a loaded parallel (`-n 8`) run rather
# than racing another AMBIENT_HOME/nested-pytest module over the real
# ambient $HOME on a different worker.
pytestmark = pytest.mark.xdist_group(name="ambient_home")

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"

SPAWN_METHODS = {"Popen", "run", "check_call", "check_output"}
GUARDED_NAMES = {"RALPHD_ENGINE"}
EXEMPT_CLASSES = {"LiveRun"}

HOME_KEY_RE = re.compile(
    r"""(['"])HOME\1\s*:"""              # {"HOME": ...} dict-literal key
    r"""|\[\s*(['"])HOME\2\s*\]\s*="""   # env["HOME"] = ... subscript assign
)
RESOLVED_HOME_ENV_RE = re.compile(r"_resolved_home_env\s*\(")
AMBIENT_HOME_RE = re.compile(r"\bAMBIENT_HOME\b")


def _resolves_to_guarded_name(expr: ast.expr, outer_src: str) -> str | None:
    """`RALPHD_ENGINE` if `expr` is `RALPHD_ENGINE`, `str(RALPHD_ENGINE)`, or
    a bare local name that `outer_src` assigns from one of those two shapes
    -- else `None` (not a guarded spawn's target, or not resolvable)."""
    if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
            and expr.func.id == "str" and len(expr.args) == 1
            and isinstance(expr.args[0], ast.Name)):
        name = expr.args[0].id
    elif isinstance(expr, ast.Name):
        name = expr.id
    else:
        return None
    if name in GUARDED_NAMES:
        return name
    for guarded in GUARDED_NAMES:
        if re.search(rf"\b{re.escape(name)}\s*=\s*(str\(\s*{guarded}\s*\)|{guarded}\b)",
                      outer_src):
            return guarded
    return None


def _is_subprocess_spawn_call(call: ast.Call) -> str | None:
    func = call.func
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess" and func.attr in SPAWN_METHODS):
        return func.attr
    return None


def _argv_expr(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "args":
            return kw.value
    return None


def find_unguarded_child_engine_spawns(root: Path) -> list[tuple[str, int, str]]:
    """Every raw `subprocess.*` spawn under `root` whose argv resolves to
    `RALPHD_ENGINE` and is neither inside a `LiveRun` method,
    nor covered by `_resolved_home_env(`/`AMBIENT_HOME`, nor sets an
    explicit `"HOME"` key -- in the outermost function lexically containing
    the call. Returns `(relpath, 1-based lineno, message)` tuples.
    """
    violations: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text()
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(root).as_posix()
        violations.extend(_scan_module(tree, text, rel))
    return violations


def _scan_module(tree: ast.Module, text: str, rel: str) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []

    def visit(node: ast.AST, class_stack: list[str], func_stack: list[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, class_stack + [child.name], func_stack)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, class_stack, func_stack + [child])
            else:
                if isinstance(child, ast.Call):
                    _check(child, class_stack, func_stack)
                visit(child, class_stack, func_stack)

    def _check(call: ast.Call, class_stack: list[str], func_stack: list[ast.AST]) -> None:
        attr = _is_subprocess_spawn_call(call)
        if attr is None:
            return
        argv = _argv_expr(call)
        if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
            return
        outer_func = func_stack[0] if func_stack else None
        outer_src = (ast.get_source_segment(text, outer_func) if outer_func else text) or text
        target = _resolves_to_guarded_name(argv.elts[0], outer_src)
        if target is None:
            return
        if any(name in EXEMPT_CLASSES for name in class_stack):
            return
        if RESOLVED_HOME_ENV_RE.search(outer_src):
            return
        if AMBIENT_HOME_RE.search(outer_src):
            return
        if HOME_KEY_RE.search(outer_src):
            return
        out.append((
            rel, call.lineno,
            f"unguarded child-engine spawn at {rel}:{call.lineno} "
            f"(subprocess.{attr} of {target}) -- goes through neither "
            f"LiveRun nor AMBIENT_HOME, and sets no explicit "
            f"HOME override",
        ))

    visit(tree, [], [])
    return out


def test_no_unguarded_child_engine_spawns_in_tests_tree():
    """The real check: every `RALPHD_ENGINE` spawn under `tests/` is either
    inside `LiveRun`, opts into `AMBIENT_HOME`, or sets an
    explicit `HOME`."""
    violations = find_unguarded_child_engine_spawns(TESTS_ROOT)
    assert not violations, "\n".join(v[2] for v in violations)


def test_mutation_a_planted_unguarded_spawn_is_caught(tmp_path):
    """A synthetic module (never under the real `tests/` tree) that spawns
    `RALPHD_ENGINE` with a plain `{**os.environ}` env -- no class, no
    `_resolved_home_env`, no `AMBIENT_HOME`, no explicit `HOME` -- must be
    reported by exact file:line."""
    planted = tmp_path / "test_planted_bad_spawn.py"
    planted.write_text(
        "import os\n"
        "import subprocess\n"
        "from tests.conftest import RALPHD_ENGINE\n"
        "\n"
        "\n"
        "def test_bad_spawn(tmp_path):\n"
        "    env = {**os.environ}\n"
        "    return subprocess.run([str(RALPHD_ENGINE), \"start\"], env=env)\n"
    )
    violations = find_unguarded_child_engine_spawns(tmp_path)
    assert len(violations) == 1
    rel, lineno, msg = violations[0]
    assert rel == "test_planted_bad_spawn.py"
    assert lineno == 8
    assert "test_planted_bad_spawn.py:8" in msg


def test_mutation_b_a_compliant_spawn_is_not_flagged(tmp_path):
    """The mirror image, proving the scanner does not simply fire on every
    `RALPHD_ENGINE` spawn: the same shape as mutation A, but with an
    explicit `HOME` key in the env dict, must NOT be reported."""
    planted = tmp_path / "test_planted_good_spawn.py"
    planted.write_text(
        "import os\n"
        "import subprocess\n"
        "from tests.conftest import RALPHD_ENGINE\n"
        "\n"
        "\n"
        "def test_good_spawn(tmp_path):\n"
        "    env = {**os.environ, \"HOME\": str(tmp_path / 'home')}\n"
        "    return subprocess.run([str(RALPHD_ENGINE), \"start\"], env=env)\n"
    )
    violations = find_unguarded_child_engine_spawns(tmp_path)
    assert violations == []
