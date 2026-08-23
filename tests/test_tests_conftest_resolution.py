"""Task 005 (#50): `tests.conftest` (and the sibling absolute imports several
modules use, e.g. `from tests.test_roadmap_v07 import closed_issues`) must
resolve to *this* repository's copy no matter what else on `sys.path` also
happens to provide an importable `tests` package.

The whole mechanism is `/conftest.py` (repo root): it inserts this
repository's root at `sys.path[0]` the moment pytest loads it -- before
pytest imports anything under `tests/`, including `tests/conftest.py`
itself, and after any early-loading plugin has already run its own
top-level code. See that file's own docstring for exactly why this is
enough and why `tests/__init__.py` (the more obvious-looking fix) is
deliberately *not* used: it would break ~40 test modules that rely on
bare top-level imports (`from test_cli_docker import ...`), which need
`tests/` itself, not its parent, directly on `sys.path`.

Both scenarios below are exercised; one of them (the harder ordering) is
confirmed by mutation to actually require `/conftest.py`: deleting that
file makes `test_early_plugin_sys_path_injection_...` fail (see that
test's own docstring for why the other one does not need it).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _make_decoy(tmp_path: Path) -> Path:
    """A top-level `tests` package, in its own directory, that would shadow
    this repo's `tests` package if its portion were merged in first."""
    decoy_root = tmp_path / "decoy"
    decoy_tests = decoy_root / "tests"
    decoy_tests.mkdir(parents=True)
    (decoy_tests / "conftest.py").write_text(
        "DECOY_CONFTEST = True\nRALPHCTL = 'not-the-real-ralphctl'\n"
    )
    return decoy_root


def _run_fixture_test(env: dict[str, str], extra_args: list[str]) -> subprocess.CompletedProcess:
    target = "tests/test_tests_conftest_resolution.py::test_fixture_uses_this_repos_conftest"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *extra_args, target],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_pytest_collection_with_decoy_ahead_of_repo_on_pythonpath_still_resolves_here(
    tmp_path,
):
    """A decoy `tests` package reachable via `PYTHONPATH` (the scenario a
    stray dev-tooling entry, or another checked-out project, could
    produce) must not shadow this repo's `tests.conftest` when pytest
    collects a real test module. With `python -m pytest`, the working
    directory ('') already sits ahead of `PYTHONPATH` entries on
    `sys.path`, so this scenario alone does not require `/conftest.py`'s
    fix -- it is still worth pinning as a regression guard, and the
    harder ordering (a decoy pushed genuinely ahead of the working
    directory) is `test_early_plugin_sys_path_injection_...` below, which
    *does* fail by mutation without `/conftest.py`.
    """
    decoy_root = _make_decoy(tmp_path)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{decoy_root}{os.pathsep}{existing}" if existing else str(decoy_root)

    result = _run_fixture_test(env, [])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_early_plugin_sys_path_injection_ahead_of_cwd_still_resolves_here(tmp_path):
    """Model a pytest plugin that runs *before* any conftest.py is loaded
    and pushes its own directory onto `sys.path[0]` -- ahead of the
    working directory itself, not merely ahead of it on `PYTHONPATH`. This
    is the harder ordering: `/conftest.py` must run *after* the plugin and
    still win, by reordering `sys.path` rather than merely prepending to
    it (a plain prepend would leave the plugin's earlier entry still
    ahead if it also inserted at index 0).

    Confirmed by mutation: deleting `/conftest.py` makes this fail (the
    only one of the two tests in this module that actually depends on it
    -- see `test_pytest_collection_with_decoy_ahead_of_repo_on_pythonpath_...`
    above for why the other one does not).
    """
    decoy_root = _make_decoy(tmp_path)
    plugin_dir = tmp_path / "early_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "decoy_sys_path_plugin.py").write_text(
        f"import sys\nsys.path.insert(0, {str(decoy_root)!r})\n"
    )

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{plugin_dir}{os.pathsep}{existing}" if existing else str(plugin_dir)
    )

    result = _run_fixture_test(env, ["-p", "decoy_sys_path_plugin"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_fixture_uses_this_repos_conftest():
    """Collected directly (fast tier) and as the target of the two
    subprocess pytest invocations above (each with a decoy ahead of this
    repo on `sys.path` by a different route): asserts that whatever
    `tests.conftest` this process ended up importing is this repo's real
    one, not a decoy."""
    import tests.conftest as c

    assert not getattr(c, "DECOY_CONFTEST", False)
    assert c.__file__ == str(REPO / "tests" / "conftest.py")
    assert str(c.RALPHCTL).endswith("ralphctl")
