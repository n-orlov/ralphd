"""Task 005 (v0.12 PRD, #83 class): task 003's autouse fixture
(`tests/conftest.py::_isolate_ambient_home_in_process`) isolates every
test's own in-process `$HOME` by default, with exactly one greppable
opt-in (the `AMBIENT_HOME` pytest marker, registered in
`pytest_configure`). Nothing at the level of source alone guards THAT
DOOR against itself: nothing stops a future edit from quietly removing
the unconditional `monkeypatch.setenv("HOME", ...)` call (so the fixture
still runs on every test, matches every test node, but no longer isolates
anything), or from weakening the "exactly one opt-in" invariant by adding
a SECOND marker spelling that also short-circuits isolation (e.g. a new
`REAL_HOME` marker check next to the existing `AMBIENT_HOME` one) --
either change reopens the #83 shape task 003 closed, silently, because
every existing `@pytest.mark.AMBIENT_HOME` test keeps passing regardless
and the fixture is still present and still autouse.

This module is that static guard. It reads the OUTERMOST function
literally named `_isolate_ambient_home_in_process` (task 003's own
greppable fixture name) out of a module's source via `ast`, and requires:

  1. an unconditional `monkeypatch.setenv("HOME", ...)` call reachable
     anywhere in that function's body -- if it has been removed, the
     fixture still runs on every test but isolates nothing;
  2. exactly one DISTINCT marker name checked via
     `<node>.get_closest_marker("...")` inside the function -- a second,
     different name checked there is a second spelling of the same
     opt-in, exactly what task 003's own docstring rules out ("there is
     still only one greppable spelling of 'inherit the real ambient
     $HOME' and not a second one under a different name").

Four cases below, entirely against synthetic module sources written to a
`tmp_path` scratch tree -- never the real `tests/conftest.py`:

  * mutation A: a planted fixture with the marker check intact but the
    `monkeypatch.setenv("HOME", ...)` call removed -- caught, by exact
    file:line;
  * mutation B: a planted fixture with a second marker name
    (`REAL_HOME`) checked alongside `AMBIENT_HOME` -- caught, by exact
    file:line naming the second spelling;
  * mutation C: a planted fixture shaped exactly like the real one
    (single marker check, unconditional `monkeypatch.setenv("HOME", ...)`
    afterwards) -- NOT flagged, proving the scanner does not simply fire
    on the fixture's mere presence;
  * the real check: the SAME scanner run against the actual `tests/`
    directory, requiring the fixture to actually be found there (so a
    silent rename of `_isolate_ambient_home_in_process` cannot blind this
    guard) and requiring zero violations against it today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# xdist_group (task 003, G.3): an AMBIENT_HOME-family module -- keeps this
# module's tests on one worker under a loaded parallel (`-n 8`) run rather
# than racing another AMBIENT_HOME/nested-pytest module over the real
# ambient $HOME on a different worker.
pytestmark = pytest.mark.xdist_group(name="ambient_home")

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"

FIXTURE_NAME = "_isolate_ambient_home_in_process"


def _is_setenv_home_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setenv"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "HOME"
    )


def _marker_name_checked(node: ast.AST) -> str | None:
    """The literal marker name a `<obj>.get_closest_marker("...")` call
    names, or None if `node` is not that call shape."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_closest_marker"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _fixture_defs(root: Path) -> list[tuple[str, ast.FunctionDef]]:
    """Every `(relpath, FunctionDef)` under `root` whose function is
    literally named `_isolate_ambient_home_in_process`."""
    out: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == FIXTURE_NAME:
                out.append((rel, node))
    return out


def find_in_process_home_isolation_violations(root: Path) -> list[tuple[str, int, str]]:
    """Every violation of the two invariants above, found in every
    function named `_isolate_ambient_home_in_process` under `root`.
    Returns `(relpath, 1-based lineno, message)` tuples.
    """
    violations: list[tuple[str, int, str]] = []
    for rel, func in _fixture_defs(root):
        violations.extend(_check_fixture(func, rel))
    return violations


def _check_fixture(func: ast.FunctionDef, rel: str) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []

    has_setenv_home = any(_is_setenv_home_call(n) for n in ast.walk(func))
    if not has_setenv_home:
        out.append((
            rel, func.lineno,
            f"in-process HOME isolation removed at {rel}:{func.lineno} -- "
            f"{FIXTURE_NAME}() no longer calls "
            f"monkeypatch.setenv(\"HOME\", ...) anywhere in its body",
        ))

    distinct: list[tuple[str, ast.AST]] = []
    seen_names: set[str] = set()
    for n in ast.walk(func):
        name = _marker_name_checked(n)
        if name is not None and name not in seen_names:
            seen_names.add(name)
            distinct.append((name, n))

    if len(distinct) > 1:
        first_name = distinct[0][0]
        second_name, second_node = distinct[1]
        out.append((
            rel, second_node.lineno,
            f"second ambient-HOME opt-in spelling {second_name!r} at "
            f"{rel}:{second_node.lineno} -- {FIXTURE_NAME}() must check "
            f"exactly one marker name, not a second one alongside the "
            f"existing {first_name!r}",
        ))

    return out


def test_no_in_process_home_isolation_regressions_in_tests_tree():
    """The real check: the fixture must still be findable under `tests/`
    (a silent rename would blind this guard, so require at least one
    match) and the scanner must report zero violations against it."""
    found = _fixture_defs(TESTS_ROOT)
    assert found, (
        f"no function named {FIXTURE_NAME}() found under tests/ -- either "
        f"task 003's fixture was removed/renamed, or this guard's target "
        f"name has drifted from it"
    )
    violations = find_in_process_home_isolation_violations(TESTS_ROOT)
    assert violations == [], "\n".join(v[2] for v in violations)


def test_mutation_a_isolation_removed_is_caught(tmp_path):
    """A planted fixture whose marker check is intact but whose
    `monkeypatch.setenv("HOME", ...)` call has been removed -- the
    fixture still runs on every test, still matches the shape, but no
    longer isolates anything. Must be reported by exact file:line."""
    planted = tmp_path / "conftest_planted_no_isolation.py"
    planted.write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture(autouse=True)\n"
        "def _isolate_ambient_home_in_process(request, monkeypatch, tmp_path_factory):\n"
        "    if request.node.get_closest_marker(\"AMBIENT_HOME\"):\n"
        "        return\n"
        "    # isolation removed: no monkeypatch.setenv(\"HOME\", ...) call left\n"
        "    return\n"
    )
    violations = find_in_process_home_isolation_violations(tmp_path)
    assert len(violations) == 1
    rel, lineno, msg = violations[0]
    assert rel == "conftest_planted_no_isolation.py"
    assert lineno == 5
    assert "conftest_planted_no_isolation.py:5" in msg
    assert "isolation removed" in msg


def test_mutation_b_second_spelling_is_caught(tmp_path):
    """A planted fixture that checks a SECOND marker name (`REAL_HOME`)
    alongside the sanctioned `AMBIENT_HOME` one -- a second spelling of
    the same opt-in. Must be reported by exact file:line naming the
    second spelling."""
    planted = tmp_path / "conftest_planted_two_spellings.py"
    planted.write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture(autouse=True)\n"
        "def _isolate_ambient_home_in_process(request, monkeypatch, tmp_path_factory):\n"
        "    if request.node.get_closest_marker(\"AMBIENT_HOME\"):\n"
        "        return\n"
        "    if request.node.get_closest_marker(\"REAL_HOME\"):\n"
        "        return\n"
        "    isolated = tmp_path_factory.mktemp(\"ambient-home-in-process\")\n"
        "    monkeypatch.setenv(\"HOME\", str(isolated))\n"
    )
    violations = find_in_process_home_isolation_violations(tmp_path)
    assert len(violations) == 1
    rel, lineno, msg = violations[0]
    assert rel == "conftest_planted_two_spellings.py"
    assert lineno == 8
    assert "conftest_planted_two_spellings.py:8" in msg
    assert "REAL_HOME" in msg


def test_mutation_c_compliant_fixture_is_not_flagged(tmp_path):
    """The mirror image, proving the scanner does not simply fire on the
    fixture's mere presence: a planted fixture shaped exactly like the
    real one in tests/conftest.py (one marker check, unconditional
    `monkeypatch.setenv("HOME", ...)` afterwards) must NOT be reported."""
    planted = tmp_path / "conftest_planted_compliant.py"
    planted.write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.fixture(autouse=True)\n"
        "def _isolate_ambient_home_in_process(request, monkeypatch, tmp_path_factory):\n"
        "    if request.node.get_closest_marker(\"AMBIENT_HOME\"):\n"
        "        return\n"
        "    isolated = tmp_path_factory.mktemp(\"ambient-home-in-process\")\n"
        "    monkeypatch.setenv(\"HOME\", str(isolated))\n"
    )
    violations = find_in_process_home_isolation_violations(tmp_path)
    assert violations == []
