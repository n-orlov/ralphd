"""Task 005 (#50), mechanism corrected in #64: `tests.conftest` (and the
sibling absolute imports several modules use, e.g. `from
tests.records.test_roadmap import closed_issues_v07`) must resolve to *this*
repository's copy no matter what else on `sys.path` also happens to
provide an importable `tests` package.

The whole mechanism is `tests_package_pin.py` (repo root). It pre-binds
`sys.modules["tests"]` to a namespace-style package whose `__path__` is
exactly `[<repo>/tests]`, so `tests.*` resolves out of `sys.modules` and
never reaches `PathFinder` at all. It is reached two ways and both are
covered below: `/conftest.py` calls it, which covers every invocation that
collects something under this repository; and `-p tests_package_pin` loads
it as a plugin for the invocations that do not, and so never load that
conftest at all (`scripts/host_like_pytest.sh` is one) --
`test_the_pin_holds_for_targets_outside_the_repo_via_the_plugin_path` is
that half, with its own mutation pair. See that module's docstring for why
the original `sys.path.insert(0, repo_root)` was not sufficient: namespace
*portions* accumulate in `sys.path` order, but a **regular** `tests`
package (a directory with `__init__.py`) found at *any* later `sys.path`
position yields a spec with a loader, which pre-empts the search and
discards every portion collected so far. Position 0 cannot beat that,
because this repo's `tests/` is a portion, not a package. Two installed
wheels (`az_assertpy_extensions`, `az_s3_url_helper`) ship exactly such a
`tests/` package and took this suite from 3000+ tests to 42 collection
errors and zero tests run.

That module's docstring also explains why `tests/__init__.py` (the more
obvious-looking fix) is deliberately *not* used: 102 test modules rely on
bare top-level imports (`from test_cli_docker import ...`), which need
`tests/` itself, not its parent, directly on `sys.path`, and pytest's
prepend mode only puts it there while `tests/` stays a namespace package.
`test_bare_top_level_import_style_still_resolves` and
`test_full_collection_under_the_strongest_decoy_has_no_errors` below pin
that constraint so a future "simplification" to a regular package fails
here instead of shipping.

Decoy shapes below are paired on purpose: a namespace portion (which the
old `sys.path` fix did beat) *and* a regular package (which it did not),
each delivered by `PYTHONPATH` and by an early-loading plugin, plus the
combined worst case. Both shapes are real; the pair is the check.

Mutation-confirmed against a scratch copy of the repo (results as of #64):

  * reverting the pin to a `sys.path.insert(0, ...)`-only `/conftest.py`
    (the pre-#64 body) -> 6 of the 8 pre-existing tests here fail: the three
    regular-package cases and the full-collection case resolve
    `tests.conftest` to the decoy, and the two namespace cases fail on
    `test_fixture_uses_this_repos_conftest`'s exact-`__path__` assertion (the
    merged namespace `__path__` picks up the decoy's directory as a second
    portion -- resolution still happens to land here, the *invariant* does
    not hold);
  * deleting `/conftest.py` outright -> the same 6 fail;
  * adding `tests/__init__.py` -> `test_bare_top_level_import_style_...`
    and `test_full_collection_...` fail: collection dies with
    `ModuleNotFoundError: No module named 'test_cli_docker'` on
    `test_absolute_timestamps.py` and exits 2 with no tests run.

The plugin half carries its own mutation pair inline (running the same
out-of-tree probe without `-p`), so it does not depend on this list.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _make_decoy(tmp_path: Path, *, regular_package: bool = False, name: str = "decoy") -> Path:
    """A top-level `tests` package, in its own directory, that would shadow
    this repo's `tests` package if it won the import.

    `regular_package=False` writes a PEP 420 namespace *portion* (no
    `__init__.py`): it competes with this repo's portion purely on
    `sys.path` order, so the pre-#64 `sys.path.insert(0, ...)` beat it.
    `regular_package=True` writes a *regular* package: `PathFinder` returns
    it with a loader and stops, discarding this repo's portion no matter how
    early it sits -- the #64 case, which only the `sys.modules` pre-bind
    beats.
    """
    decoy_root = tmp_path / name
    decoy_tests = decoy_root / "tests"
    decoy_tests.mkdir(parents=True)
    if regular_package:
        (decoy_tests / "__init__.py").write_text("DECOY_PACKAGE = True\n")
    (decoy_tests / "conftest.py").write_text(
        "DECOY_CONFTEST = True\nRALPHCTL = 'not-the-real-ralphctl'\n"
    )
    return decoy_root


def _prepend_pythonpath(env: dict[str, str], *entries: Path) -> dict[str, str]:
    parts = [str(e) for e in entries]
    existing = env.get("PYTHONPATH", "")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _write_plugin(plugin_dir: Path, body: str, module: str = "decoy_sys_path_plugin") -> Path:
    """An importable module suitable for `-p <module>`: pytest imports it,
    and runs its top-level code, *before* it loads any conftest.py."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / f"{module}.py").write_text(body)
    return plugin_dir


def _run_pytest(env: dict[str, str], args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def _run_fixture_test(env: dict[str, str], extra_args: list[str]) -> subprocess.CompletedProcess:
    target = "tests/test_tests_conftest_resolution.py::test_fixture_uses_this_repos_conftest"
    return _run_pytest(env, [*extra_args, target])


def test_pytest_collection_with_decoy_ahead_of_repo_on_pythonpath_still_resolves_here(
    tmp_path,
):
    """A decoy `tests` *namespace portion* reachable via `PYTHONPATH` (the
    scenario a stray dev-tooling entry, or another checked-out project,
    could produce) must not shadow this repo's `tests.conftest` when pytest
    collects a real test module. With `python -m pytest`, the working
    directory ('') already sits ahead of `PYTHONPATH` entries on
    `sys.path`, so *resolution* in this scenario survives even with
    `/conftest.py` deleted -- what does not survive is the exact-`__path__`
    invariant asserted in `test_fixture_uses_this_repos_conftest` (without
    the pre-bind, the decoy's directory joins the merged namespace
    `__path__` as a second portion, so a `tests.*` submodule this repo does
    *not* ship could still be served from the decoy). The harder orderings
    and the harder decoy shape are the tests below.
    """
    decoy_root = _make_decoy(tmp_path)
    env = _prepend_pythonpath(dict(os.environ), decoy_root)

    result = _run_fixture_test(env, [])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_early_plugin_sys_path_injection_ahead_of_cwd_still_resolves_here(tmp_path):
    """Model a pytest plugin that runs *before* any conftest.py is loaded
    and pushes its own directory onto `sys.path[0]` -- ahead of the
    working directory itself, not merely ahead of it on `PYTHONPATH`. This
    is the harder *ordering*: `/conftest.py` must run after the plugin and
    still win (a plain prepend would leave the plugin's earlier entry
    still ahead if it also inserted at index 0). Since #64 the pre-bind
    settles it outright, and the ordering no longer matters -- kept as the
    regression guard for the ordering bug #50 was filed about.

    Confirmed by mutation: deleting `/conftest.py` makes this fail on
    resolution (the decoy's `tests/conftest.py` wins), which is the
    original #50 symptom.
    """
    decoy_root = _make_decoy(tmp_path)
    plugin_dir = _write_plugin(
        tmp_path / "early_plugin",
        f"import sys\nsys.path.insert(0, {str(decoy_root)!r})\n",
    )
    env = _prepend_pythonpath(dict(os.environ), plugin_dir)

    result = _run_fixture_test(env, ["-p", "decoy_sys_path_plugin"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_regular_package_decoy_on_pythonpath_still_resolves_here(tmp_path):
    """The #64 case, and the one measured in the wild (installed wheels
    shipping a top-level `tests/` package): the decoy carries
    `__init__.py`, so it is a *regular* package. `PathFinder` returns it
    with a loader and stops walking, throwing away this repo's namespace
    portion -- even though this repo's root is at `sys.path[0]` and the
    decoy is behind it on `PYTHONPATH`. Ordering is irrelevant here;
    precedence decides, so only the `sys.modules["tests"]` pre-bind in
    `/conftest.py` saves it.

    Confirmed by mutation: with `/conftest.py` reduced to its pre-#64
    `sys.path.insert(0, repo_root)` body, this fails with
    `tests.conftest` resolved to the decoy's file.
    """
    decoy_root = _make_decoy(tmp_path, regular_package=True)
    env = _prepend_pythonpath(dict(os.environ), decoy_root)

    result = _run_fixture_test(env, [])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_early_plugin_that_already_imported_a_regular_package_decoy_is_displaced(tmp_path):
    """Worse than injecting a `sys.path` entry: the early plugin *imports*
    the decoy, so by the time `/conftest.py` runs, `sys.modules` already
    holds a foreign `tests` **and** a foreign `tests.conftest`. No amount
    of `sys.path` or `__path__` work helps then -- `sys.modules` is
    consulted first -- so `/conftest.py` has to detect that the bound
    `tests` is not ours, replace it, and purge the stale `tests.*`
    children it seeded.
    """
    decoy_root = _make_decoy(tmp_path, regular_package=True)
    plugin_dir = _write_plugin(
        tmp_path / "early_plugin",
        "import sys\n"
        f"sys.path.insert(0, {str(decoy_root)!r})\n"
        "import tests\n"
        "import tests.conftest\n"
        # Prove the decoy really did win at plugin time; if it had not, this
        # test would be vacuous and pass for the wrong reason.
        "assert tests.conftest.DECOY_CONFTEST is True\n",
    )
    env = _prepend_pythonpath(dict(os.environ), plugin_dir)

    result = _run_fixture_test(env, ["-p", "decoy_sys_path_plugin"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_regular_package_decoys_from_both_pythonpath_and_early_plugin(tmp_path):
    """The strongest combined form: one regular-package decoy sitting on
    `sys.path` via `PYTHONPATH`, a *second* regular-package decoy pushed to
    `sys.path[0]` by an early-loading plugin which also imports it (so
    `sys.modules["tests"]` is already poisoned), and this repo's root
    ending up at position 0 courtesy of `/conftest.py`. Every failure mode
    in #50 and #64 at once.
    """
    sys_path_decoy = _make_decoy(tmp_path, regular_package=True, name="decoy_pythonpath")
    plugin_decoy = _make_decoy(tmp_path, regular_package=True, name="decoy_plugin")
    plugin_dir = _write_plugin(
        tmp_path / "early_plugin",
        "import sys\n"
        f"sys.path.insert(0, {str(plugin_decoy)!r})\n"
        "import tests, tests.conftest\n"
        "assert tests.conftest.DECOY_CONFTEST is True\n",
    )
    env = _prepend_pythonpath(dict(os.environ), plugin_dir, sys_path_decoy)

    result = _run_fixture_test(env, ["-p", "decoy_sys_path_plugin"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_bare_top_level_import_style_still_resolves():
    """`tests/` must stay a namespace package (no `__init__.py`), because
    pytest's prepend import mode then puts `tests/` *itself* on `sys.path`
    for every module it collects there -- which is what makes the 102
    modules using `from test_cli_docker import ...` work. Adding
    `tests/__init__.py` moves that insertion up to the repo root, so the
    fix for #50/#64 must never take that shortcut.

    Resolved with `find_spec` rather than a real import: it proves the bare
    name maps to this repo's file without executing a foreign module if it
    does not.

    Confirmed by mutation: creating `tests/__init__.py` makes `find_spec`
    return None for all three names and this test fail.
    """
    assert not (REPO / "tests" / "__init__.py").exists(), (
        "tests/ must remain a PEP 420 namespace package -- see /conftest.py's docstring (#64)"
    )
    assert str(REPO / "tests") in sys.path, (
        "pytest's prepend import mode should have put tests/ itself on sys.path"
    )

    for bare_name in ("test_cli_docker", "test_e2e", "test_llm_api"):
        spec = importlib.util.find_spec(bare_name)
        assert spec is not None, f"bare top-level import of {bare_name} no longer resolves"
        assert spec.origin == str(REPO / "tests" / f"{bare_name}.py"), spec.origin


def test_full_collection_under_the_strongest_decoy_has_no_errors(tmp_path):
    """End-to-end counterpart to the two constraints above: collect the
    whole suite (both import styles, all 200+ modules) with a
    regular-package decoy ahead of this repo, and require zero collection
    errors. This is the exact failure #64 reports -- installed wheels
    shipping a top-level `tests` package turned `python -m pytest -q` into
    "42 errors during collection" with no tests run at all -- expressed as
    a check. `--collect-only` keeps it to about a second.
    """
    decoy_root = _make_decoy(tmp_path, regular_package=True)
    env = _prepend_pythonpath(dict(os.environ), decoy_root)

    result = _run_pytest(env, ["--collect-only", "tests"])
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    # Matched narrowly: `-q --collect-only` prints every node id, and plenty of
    # this suite's test *names* contain the word "error".
    assert "errors during collection" not in result.stdout, result.stdout[-4000:]
    assert not [line for line in result.stdout.splitlines() if line.startswith("ERROR")], (
        result.stdout[-4000:]
    )
    # A guard against a silently empty collection passing this test: the
    # suite is 3000+ tests, so any plausible future trim is still far above.
    collected = [line for line in result.stdout.splitlines() if "tests collected" in line]
    assert collected, result.stdout[-2000:]
    assert int(collected[0].split()[0]) > 2000, collected[0]


def test_the_pin_holds_for_targets_outside_the_repo_via_the_plugin_path(tmp_path):
    """The second load path, and the reason `tests_package_pin.py` is a module
    rather than code inside `/conftest.py`.

    pytest loads only the conftests on the path to the arguments it is given, so
    `pytest /tmp/probe/test_x.py` -- even with this repository as the working
    directory -- never loads `/conftest.py`, and the pin never runs. That is not
    hypothetical: `scripts/host_like_pytest.sh` runs exactly such a probe (it is
    the only way a test can observe the environment the script assembles), and
    an installed `tests` package in site-packages is on `sys.path` for every
    invocation regardless of what is being collected.

    `-p tests_package_pin` closes it: pytest imports plugin modules before any
    conftest. The mutation pair below is the whole point -- without the flag the
    same command resolves `tests.conftest` to the decoy.
    """
    decoy_root = _make_decoy(tmp_path, regular_package=True)
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "test_outside_probe.py").write_text(
        "import tests.conftest as c\n"
        "\n"
        "\n"
        "def test_resolves_here():\n"
        "    assert c.RALPHCTL != 'not-the-real-ralphctl', c.__file__\n"
    )
    # cwd is this repo, so `-m pytest` puts the repo root on sys.path -- exactly
    # what the script arranges, and what makes the plugin importable by name.
    env = _prepend_pythonpath(dict(os.environ), decoy_root)

    with_plugin = _run_pytest(env, ["-p", "tests_package_pin", str(probe)])
    assert with_plugin.returncode == 0, with_plugin.stdout + with_plugin.stderr
    assert "1 passed" in with_plugin.stdout, with_plugin.stdout

    # Mutation pair: drop the plugin and nothing else. The conftest is not on
    # the path to the target, so the decoy wins -- which is what the flag is
    # for, and what this repo shipped before #64's second half.
    without = _run_pytest(env, [str(probe)])
    assert without.returncode != 0, (
        "expected the decoy to win without the plugin; if this passes, the pin"
        " is arriving some other way and this test is no longer proving anything"
    )
    assert "not-the-real-ralphctl" in without.stdout, without.stdout[-3000:]


def test_fixture_uses_this_repos_conftest():
    """Collected directly (fast tier) and as the target of the subprocess
    pytest invocations above (each with a decoy ahead of this repo on
    `sys.path` by a different route and in a different shape): asserts that
    whatever `tests.conftest` this process ended up importing is this
    repo's real one, not a decoy."""
    import tests.conftest as c

    assert not getattr(c, "DECOY_CONFTEST", False)
    assert not getattr(sys.modules["tests"], "DECOY_PACKAGE", False)
    assert c.__file__ == str(REPO / "tests" / "conftest.py")
    assert str(c.RALPHCTL).endswith("ralphctl")
    # The pre-bind's whole point: `tests` resolves submodules from exactly
    # one directory, so no other `tests/` anywhere can contribute one.
    assert list(sys.modules["tests"].__path__) == [str(REPO / "tests")]
