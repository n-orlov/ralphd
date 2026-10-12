"""v0.35 (#303): the `flaky_infra` marker gives a node exactly ONE rerun.

The policy is deliberately narrow: pytest-rerunfailures is a dev dependency,
`tests/conftest.py` attaches `flaky(reruns=1)` to a node that names
`flaky_infra`, and nothing applies a rerun globally. Each node below runs in a
child pytest (pytester) whose own conftest reuses the REAL collection hook from
`tests/conftest.py`, so what is exercised is the shipped policy, not a copy.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parent.parent

# A node that fails on its first attempt only, tracked by a file in the child's
# tmp dir (the rerun happens in the same process, so a module global would do,
# but a file survives either runner).
_FAIL_ONCE = '''
import pytest
from pathlib import Path

{mark}
def test_node():
    marker = Path("attempts.txt")
    n = int(marker.read_text()) if marker.exists() else 0
    marker.write_text(str(n + 1))
    assert n >= {needed}, f"attempt {{n + 1}} fails"
'''


def _child(pytester, *, mark: str, needed: int):
    pytester.makeini("[pytest]\nmarkers =\n    flaky_infra: host-only flake\n    fast: tier\n    smoke: tier\n")
    pytester.makeconftest(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from tests.conftest import pytest_collection_modifyitems  # noqa: F401\n")
    pytester.makepyfile(test_child=_FAIL_ONCE.format(mark=mark, needed=needed))
    return pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "no:xdist", "-rR", "-q")


def test_a_fail_once_node_under_the_marker_passes_on_its_second_attempt_and_is_reported_rerun(pytester):
    result = _child(pytester, mark="@pytest.mark.flaky_infra", needed=1)
    result.stdout.fnmatch_lines(["RERUN test_child.py::test_node", "*1 passed, 1 rerun*"])
    assert (pytester.path / "attempts.txt").read_text() == "2"


def test_the_same_fail_once_node_unmarked_fails(pytester):
    result = _child(pytester, mark="", needed=1)
    result.assert_outcomes(failed=1)
    assert "RERUN" not in result.stdout.str()
    assert (pytester.path / "attempts.txt").read_text() == "1"


def test_a_fail_twice_node_under_the_marker_fails_after_exactly_one_rerun(pytester):
    result = _child(pytester, mark="@pytest.mark.flaky_infra", needed=2)
    result.stdout.fnmatch_lines(["RERUN test_child.py::test_node", "*1 failed, 1 rerun*"])
    assert (pytester.path / "attempts.txt").read_text() == "2"


def test_the_marker_is_registered_and_the_dev_extra_lists_rerunfailures():
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("flaky_infra:") for m in markers)
    dev = cfg["project"]["optional-dependencies"]["dev"]
    assert any(d.startswith("pytest-rerunfailures") for d in dev)


def test_no_global_reruns_in_pyproject_or_any_workflow():
    pattern = re.compile(r"--reruns|reruns\s*=|--only-rerun|rerunfailures", re.I)
    cfg = (REPO / "pyproject.toml").read_text()
    ini = tomllib.loads(cfg)["tool"]["pytest"]["ini_options"]
    assert not pattern.search(str(ini.get("addopts", "")))
    assert "reruns" not in ini
    for wf in sorted((REPO / ".github" / "workflows").glob("*.y*ml")):
        assert not re.search(r"--reruns|--only-rerun", wf.read_text()), wf.name


def test_the_two_named_pty_and_signal_nodes_carry_the_marker():
    for module, name in (
        ("test_cli_logs_pty_termios.py", "test_sigint_restores_termios_mode"),
        ("test_cli_logs_signal.py",
         "test_sigint_during_follow_exits_clean_no_traceback"),
    ):
        text = (REPO / "tests" / module).read_text()
        m = re.search(r"((?:@[^\n]+\n)+)def " + name + r"\(", text)
        assert m and "@pytest.mark.flaky_infra" in m.group(1), (module, name)
