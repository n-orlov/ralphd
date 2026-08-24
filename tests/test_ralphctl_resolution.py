"""#65: how tests/conftest.py finds the `ralphctl` / `ralphd-engine` console
scripts it shells out to, and what it does when it cannot.

The defect this pins: `RALPHCTL = Path(sys.executable).parent / "ralphctl"`
is correct only when interpreter and script share a `bin/`. That holds in a
virtualenv and fails for `pip install --user -e .` against a system
interpreter -- interpreter /usr/bin/python3, script ~/.local/bin/ralphctl --
where it yields /usr/bin/ralphctl, which does not exist. All ~45 modules
that shell out then die on FileNotFoundError, and die *slowly*: the argv
never execs, so the `live` fixture's wait_api()/wait_terminal() poll until
their own timeouts expire, turning a one-line path bug into hours of wall
clock reported as a timeout.

Both halves are covered here without needing a second machine, because
conftest's resolver takes the interpreter directory and the environment as
parameters: planting scripts in tmp directories models either layout
exactly. The load-bearing case is
test_interpreter_adjacent_wins_over_an_earlier_path_entry -- the direction a
naive `shutil.which("ralphctl")` fix breaks, silently testing a stale
~/.local/bin build instead of the venv's own.

The last section is the one that keeps the fix: a resolver in one file is
only a fix while it stays the *only* copy, and this defect shipped as 19
independent re-derivations of the broken one-liner. So the suite scans
itself and fails if any module derives a console-script path or binds
RALPHCTL/RALPHD_ENGINE for itself again.
"""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

from tests.conftest import (
    RALPHCTL,
    RALPHD_ENGINE,
    REPO,
    SCRIPTS,
    _override_var,
    _resolve_engine,
    _resolve_ralphctl,
    _startup_error,
)


def _plant(directory: Path, name: str = "ralphctl") -> Path:
    """An executable file standing in for an installed console script."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\nexec echo {directory.name}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _env(path_dirs: list[Path], **extra: str) -> dict[str, str]:
    """A minimal environment: only what the resolver is allowed to read, so a
    stray ambient PATH entry (this box really does have ralphctl on PATH)
    cannot make one of these tests pass for the wrong reason."""
    return {"PATH": os.pathsep.join(str(d) for d in path_dirs), **extra}


# --------------------------------------------------------------------------
def test_override_env_var_wins_over_both_other_sources(tmp_path):
    """$RALPHD_TEST_RALPHCTL outranks everything -- it is the escape hatch for
    exactly the layout this issue is about, so nothing may outvote it."""
    interpreter_dir = tmp_path / "bin"
    _plant(interpreter_dir)
    on_path = tmp_path / "pathdir"
    _plant(on_path)
    override = _plant(tmp_path / "chosen")

    env = _env([on_path], RALPHD_TEST_RALPHCTL=str(override))
    assert _resolve_ralphctl(interpreter_dir, env) == override

    # Mutation pair: with the override removed and nothing else changed, the
    # answer moves -- so the assertion above is about the override and not
    # about `chosen` being the only resolvable candidate.
    assert _resolve_ralphctl(interpreter_dir, _env([on_path])) == interpreter_dir / "ralphctl"


def test_interpreter_adjacent_wins_over_an_earlier_path_entry(tmp_path):
    """The ordering a naive fix inverts. Inside a venv the venv's own script
    is the build under test; a stale ~/.local/bin copy can sit *earlier* on
    $PATH, so `shutil.which()` must never be consulted while something lives
    beside the interpreter."""
    interpreter_dir = tmp_path / "venv-bin"
    real = _plant(interpreter_dir)
    decoy_dir = tmp_path / "stale-local-bin"
    decoy = _plant(decoy_dir)

    # decoy first on PATH, i.e. what `which ralphctl` would answer
    env = _env([decoy_dir, interpreter_dir])
    resolved = _resolve_ralphctl(interpreter_dir, env)
    assert resolved == real
    assert resolved != decoy

    # Mutation pair: delete only the interpreter-adjacent script and the same
    # call returns the decoy. This proves the assertion above is a real
    # preference between two resolvable candidates -- not a resolver that
    # ignores $PATH altogether, which would satisfy it vacuously.
    real.unlink()
    assert _resolve_ralphctl(interpreter_dir, env) == decoy


def test_path_resolution_when_nothing_sits_beside_the_interpreter(tmp_path):
    """The `pip install --user -e .` layout: /usr/bin holds no ralphctl, and
    ~/.local/bin is reachable only through $PATH. This is the case the old
    one-liner got wrong."""
    interpreter_dir = tmp_path / "usr-bin"
    interpreter_dir.mkdir()
    user_bin = tmp_path / "local-bin"
    installed = _plant(user_bin)

    assert not (interpreter_dir / "ralphctl").exists()
    assert _resolve_ralphctl(interpreter_dir, _env([user_bin])) == installed


def test_unresolvable_returns_none_rather_than_a_wrong_path(tmp_path):
    """The resolver reports failure instead of handing back a path that does
    not exist -- the latter is precisely how #65 reached subprocess.run()."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _resolve_ralphctl(empty, _env([empty])) is None


def test_startup_error_names_every_location_searched_and_the_override(tmp_path):
    """When nothing resolves, the session must fail immediately with a
    message an operator can act on: both places looked in, and the variable
    that overrides the answer. pytest_configure() raises this text as a
    pytest.UsageError before any shelling-out module runs."""
    empty = tmp_path / "empty"
    empty.mkdir()
    problem = _startup_error(empty, _env([empty]))

    assert problem is not None
    # both scripts, and for each the two locations searched
    for name in ("ralphctl", "ralphd-engine"):
        assert f"`{name}`" in problem
        assert str(empty / name) in problem
        assert _override_var(name) in problem
    assert "beside the interpreter" in problem
    assert "on $PATH" in problem
    assert "#65" in problem

    # Mutation pair: plant both scripts in the same directory and the error
    # goes away entirely -- so the assertions above are driven by resolution
    # failing, not by _startup_error() always complaining.
    populated = tmp_path / "bin"
    _plant(populated, "ralphctl")
    _plant(populated, "ralphd-engine")
    assert _startup_error(populated, _env([populated])) is None


def test_override_pointing_at_nothing_fails_loudly_instead_of_falling_back(tmp_path):
    """A set-but-wrong override is a typo, not a hint: falling through to a
    $PATH copy would silently run the very build the operator was overriding
    away from. So it stays authoritative, and the error names only it."""
    on_path = tmp_path / "pathdir"
    _plant(on_path, "ralphctl")
    _plant(on_path, "ralphd-engine")
    missing = tmp_path / "typo" / "ralphctl"

    env = _env([on_path], RALPHD_TEST_RALPHCTL=str(missing))
    assert _resolve_ralphctl(on_path, env) is None

    problem = _startup_error(on_path, env)
    assert problem is not None
    assert str(missing) in problem
    assert "$RALPHD_TEST_RALPHCTL" in problem
    # the fallbacks are not offered as though they had been tried
    assert "beside the interpreter" not in problem
    # ralphd-engine has its own override, so it still resolves normally
    assert "`ralphd-engine`" not in problem


def test_engine_resolves_by_the_same_rules_with_its_own_override(tmp_path):
    """`ralphd-engine` is spawned by LiveRun the same way ralphctl is invoked,
    so it gets the same three-tier resolution and a derived override name
    (RALPHD_TEST_RALPHD_ENGINE) rather than depending on the ambient $PATH
    happening to carry ~/.local/bin."""
    assert _override_var("ralphd-engine") == "RALPHD_TEST_RALPHD_ENGINE"

    interpreter_dir = tmp_path / "bin"
    adjacent = _plant(interpreter_dir, "ralphd-engine")
    decoy_dir = tmp_path / "stale"
    _plant(decoy_dir, "ralphd-engine")
    assert _resolve_engine(interpreter_dir, _env([decoy_dir])) == adjacent

    override = _plant(tmp_path / "chosen", "ralphd-engine")
    env = _env([decoy_dir], RALPHD_TEST_RALPHD_ENGINE=str(override))
    assert _resolve_engine(interpreter_dir, env) == override


def test_this_sessions_scripts_actually_exist(tmp_path):
    """The regression guard proper, evaluated against the real interpreter
    running this test: whatever layout that is, conftest's module-level
    constants point at files that exist and can be executed. Under
    `pip install --user -e .` with a system interpreter this is the assertion
    the old one-liner failed."""
    for script in (RALPHCTL, RALPHD_ENGINE):
        assert isinstance(script, Path), "45 modules `str()` these straight into an argv"
        assert script.exists(), f"{script} does not exist"
        assert os.access(script, os.X_OK), f"{script} is not executable"


# --------------------------------------------------------------------------
# The duplication guard: one resolver, and only one (#65).
#
# The resolver above is worth nothing while a second copy of the rule exists,
# and that is exactly how #65 shipped: 19 modules had each written
# `RALPHCTL = Path(sys.executable).parent / "ralphctl"` at their own import
# time, so fixing tests/conftest.py reached 25 of the 45 shell-out modules
# and left 19 failing in precisely the layout the fix was for. Nothing a
# conftest does can reach a constant another module evaluates for itself, so
# the only durable defence is to make a reappearance fail the suite.
#
# Two independent rules, because the duplication appeared in more shapes than
# one and each shape has to be closed:
#
#   A. deriving a console-script path from the interpreter's location. Caught
#      as `<anything mentioning sys.executable> / "<script>"`, which covers
#      the module constant, the function-local variable
#      (tests/test_steering_consumed_on_success.py had one) and the inline
#      argument (tests/test_engine_uid_boundary.py had two) alike.
#   B. *binding* the names RALPHCTL / RALPHD_ENGINE at all. Rule A only knows
#      about today's wrong rule; rule B says these names may arrive by
#      `from tests.conftest import ...` and no other way, so tomorrow's
#      differently-wrong rule is caught too.
#
# tests/conftest.py is the single exemption -- it is the one place the rule is
# allowed to live. This module is not exempted and does not need to be: it
# names candidate directories that are function parameters (tmp dirs), never
# `sys.executable`, and it imports the two constants rather than assigning
# them.
#
# Deliberately structural (AST) rather than textual: a regex over the source
# would fire on the prose in these very comments, which quote the broken line
# on purpose. The known limit is that rule A only sees the `/` operator, so a
# string-formatted equivalent (`f"{Path(sys.executable).parent}/ralphctl"`)
# would pass it -- rule B still catches that shape whenever it is bound to one
# of the shared names, which is how all 19 instances were written.
_RESOLVER_HOME = REPO / "tests" / "conftest.py"
_SHARED_NAMES = ("RALPHCTL", "RALPHD_ENGINE")


def _mentions_interpreter_location(node: ast.AST) -> bool:
    """True if `sys.executable` appears anywhere in this subtree -- the input
    every re-derivation of the broken rule started from."""
    return any(isinstance(n, ast.Attribute) and n.attr == "executable"
               and isinstance(n.value, ast.Name) and n.value.id == "sys"
               for n in ast.walk(node))


def _offences(path: Path) -> list[str]:
    """Every place in `path` that re-derives a console-script location or
    binds one of the shared names. Empty list = this module gets its paths
    from tests/conftest.py, which is the entire requirement."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        # rule A: <...sys.executable...> / "ralphctl" (or "ralphd-engine")
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant)
                and node.right.value in SCRIPTS
                and _mentions_interpreter_location(node.left)):
            found.append(
                f"{path.name}:{node.lineno}: derives the `{node.right.value}`"
                f" path from sys.executable instead of importing it from"
                f" tests/conftest.py")
        # rule B: RALPHCTL = ... / RALPHD_ENGINE = ... at any scope
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in _SHARED_NAMES:
                found.append(
                    f"{path.name}:{node.lineno}: assigns {target.id}; the"
                    f" shared constant must be imported from"
                    f" tests/conftest.py, never recomputed")
    return found


def test_no_test_module_derives_its_own_console_script_path():
    """The check that stops #65 coming back. Scans every module in tests/ --
    including the ones added after this was written, which is the point of
    scanning rather than listing."""
    modules = sorted(p for p in (REPO / "tests").glob("*.py")
                     if p != _RESOLVER_HOME)
    assert len(modules) > 40, \
        f"only found {len(modules)} modules to scan -- the glob is wrong"

    offences = [o for p in modules for o in _offences(p)]
    assert offences == [], (
        "console-script resolution has been duplicated again (#65); import"
        " RALPHCTL / RALPHD_ENGINE from tests/conftest.py instead:\n  "
        + "\n  ".join(offences))


def test_the_duplication_guard_catches_both_shapes_it_is_aimed_at(tmp_path):
    """Mutation pair for the scan above. Re-introduce each of the two shapes
    the 19 modules used and the checker must object; a module that imports the
    constant instead must come back clean. Without this, a checker that had
    quietly stopped parsing anything would still report an empty list and pass
    forever."""
    reintroduced = tmp_path / "test_regressed.py"
    reintroduced.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        '\nRALPHCTL = Path(sys.executable).parent / "ralphctl"\n'
        "\n\ndef test_x():\n"
        '    engine = str(Path(sys.executable).parent / "ralphd-engine")\n'
        "    assert engine\n")
    offences = _offences(reintroduced)
    # the constant trips both rules (derived *and* bound); the local trips A
    assert len(offences) == 3, offences
    assert sum("derives the `ralphctl` path" in o for o in offences) == 1
    assert sum("derives the `ralphd-engine` path" in o for o in offences) == 1
    assert sum("assigns RALPHCTL" in o for o in offences) == 1

    # Rule B on its own: a *differently* wrong re-derivation, with no mention
    # of sys.executable, so only the name-binding rule can see it.
    rebound = tmp_path / "test_rebound.py"
    rebound.write_text('import shutil\n\nRALPHCTL = shutil.which("ralphctl")\n')
    assert len(_offences(rebound)) == 1
    assert "assigns RALPHCTL" in _offences(rebound)[0]

    # Clean: imports the shared constants, and divides by a *different* name.
    clean = tmp_path / "test_clean.py"
    clean.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "\nfrom tests.conftest import RALPHCTL, RALPHD_ENGINE\n"
        "\nSTUB = Path(sys.executable).parent / \"docker\"\n"
        "\n\ndef test_y():\n"
        "    assert RALPHCTL and RALPHD_ENGINE and STUB\n")
    assert _offences(clean) == []
