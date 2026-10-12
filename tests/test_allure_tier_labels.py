"""v0.35 (#303): every Allure result carries its tier and its test module.

`tests/conftest.py` labels each result: the tier (`smoke`, `fast`, `docker` or
`browser`) is the Allure parent suite and the test module is the suite. The
nodes below run a child pytest (pytester) whose conftest reuses the REAL
collection hook from `tests/conftest.py`, with the allure-pytest plugin writing
results to a directory; the labels are read back out of those result files, one
node per tier. A child process, not an in-process run, so the child's listener
cannot write into the results of a parent run that itself uses `--alluredir`.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parent.parent

_BODY = "def test_node():\n    assert True\n"

# module file -> (source, tier expected)
_CHILD_MODULES = {
    # nothing in it spawns an engine: smoke (and fast)
    "test_in_process_flow.py": (_BODY, "smoke"),
    # names the real-engine harness, so it is fast but not smoke
    "test_stub_pi_flow.py": ("# drives a LiveRun\n" + _BODY, "fast"),
    "test_docker_flow.py": ("import pytest\n\n@pytest.mark.docker\n" + _BODY, "docker"),
    "test_browser_flow.py": ("import pytest\n\n@pytest.mark.browser\n" + _BODY, "browser"),
}


def _run_child(pytester) -> list[dict]:
    pytester.makeini(
        "[pytest]\nmarkers =\n    fast: tier\n    smoke: tier\n    docker: tier\n"
        "    browser: tier\n    flaky_infra: host-only flake\n")
    pytester.makeconftest(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from tests.conftest import pytest_collection_modifyitems  # noqa: F401\n")
    for name, (source, _tier) in _CHILD_MODULES.items():
        pytester.makepyfile(**{name[:-3]: source})
    out = pytester.path / "allure-results"
    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "no:xdist", "-q", "--alluredir", str(out))
    result.assert_outcomes(passed=len(_CHILD_MODULES))
    return [json.loads(p.read_text()) for p in sorted(out.glob("*-result.json"))]


def _label(result: dict, name: str) -> list[str]:
    return [lab["value"] for lab in result["labels"] if lab["name"] == name]


def test_each_tier_node_has_its_tier_as_parent_suite_and_its_module_as_suite(pytester):
    results = _run_child(pytester)
    by_module = {_label(r, "suite")[0]: r for r in results}
    assert sorted(by_module) == sorted(n[:-3] for n in _CHILD_MODULES)
    for name, (_source, tier) in _CHILD_MODULES.items():
        result = by_module[name[:-3]]
        assert _label(result, "parentSuite") == [tier], name
        assert _label(result, "suite") == [name[:-3]], name


def test_the_four_tiers_are_all_exercised():
    assert sorted(t for _s, t in _CHILD_MODULES.values()) == ["browser", "docker", "fast", "smoke"]


def test_the_dev_extra_lists_allure_pytest():
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    dev = cfg["project"]["optional-dependencies"]["dev"]
    assert any(d.startswith("allure-pytest") for d in dev)
