"""v0.35 (#303) C.2 / E.3: a real flaky node is flaky for BOTH consumers.

Every node drives a child pytest (pytester) whose conftest reuses the REAL
collection hook from `tests/conftest.py`, with pytest-rerunfailures and
allure-pytest both loaded. What it writes is read two ways:

* the step summary (`scripts/allure_summary.py`) over the results directory;
* the results themselves: the passing rerun attempt carries
  `statusDetails.flaky`, the field the pinned report generator reads.

The fast nodes check those two consumers and that ordinary passes, final
failures and first-attempt passes are never called flaky. The `docker`-marked
node adds the third consumer, the real pinned report generator
(`scripts/allure_report.sh`), and asserts its test case is `flaky` with a rerun.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parent.parent
SUMMARY = REPO / "scripts" / "allure_summary.py"
REPORT = REPO / "scripts" / "allure_report.sh"

_MODULE = '''
import pytest
from pathlib import Path


def _attempt(name):
    marker = Path(name + ".attempts")
    n = int(marker.read_text()) if marker.exists() else 0
    marker.write_text(str(n + 1))
    return n


@pytest.mark.flaky_infra
def test_infra_fails_once():
    assert _attempt("infra_once") >= 1


@pytest.mark.flaky_infra
def test_infra_fails_twice():
    assert _attempt("infra_twice") >= 2


@pytest.mark.flaky(reruns=2)
def test_plain_flaky_passes_on_third_attempt():
    assert _attempt("plain_flaky") >= 2


def test_ordinary_pass():
    assert True


def test_ordinary_failure():
    assert False


@pytest.mark.flaky_infra
def test_infra_passes_first_time():
    assert True
'''


def _run_child(pytester, module: str = _MODULE) -> Path:
    pytester.makeini("[pytest]\nmarkers =\n    flaky_infra: infra\n    fast: tier\n    smoke: tier\n")
    pytester.makeconftest(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from tests.conftest import pytest_collection_modifyitems  # noqa: F401\n")
    pytester.makepyfile(test_child=module)
    results = pytester.path / "results"
    pytester.runpytest_subprocess(f"--alluredir={results}", "-p", "no:cacheprovider",
                                  "-p", "no:xdist", "-q", "-rR")
    return results


def _attempts(results: Path) -> dict[str, list[dict]]:
    by_name: dict[str, list[dict]] = {}
    for path in results.glob("*-result.json"):
        data = json.loads(path.read_text())
        by_name.setdefault(data["name"], []).append(data)
    for group in by_name.values():
        group.sort(key=lambda r: (r["stop"], r["start"]))
    return by_name


def _flag(result: dict) -> bool:
    return bool((result.get("statusDetails") or {}).get("flaky"))


def test_the_passing_rerun_attempt_is_flagged_flaky_in_the_allure_results(pytester):
    attempts = _attempts(_run_child(pytester))
    once = attempts["test_infra_fails_once"]
    assert [a["status"] for a in once] == ["failed", "passed"]
    assert len({a["historyId"] for a in once}) == 1
    assert [_flag(a) for a in once] == [False, True]
    # a plain flaky(reruns=2) node that needed a third attempt is flaky too
    third = attempts["test_plain_flaky_passes_on_third_attempt"]
    assert [a["status"] for a in third] == ["failed", "failed", "passed"]
    assert [_flag(a) for a in third] == [False, False, True]


def test_ordinary_passes_and_final_failures_are_never_flagged_flaky(pytester):
    attempts = _attempts(_run_child(pytester))
    for name in ("test_ordinary_pass", "test_ordinary_failure", "test_infra_passes_first_time"):
        assert len(attempts[name]) == 1, name
        assert not _flag(attempts[name][0]), name
    twice = attempts["test_infra_fails_twice"]
    assert [a["status"] for a in twice] == ["failed", "failed"]
    assert not any(_flag(a) for a in twice)


def test_the_step_summary_and_the_results_agree_on_which_nodes_are_flaky(pytester):
    results = _run_child(pytester)
    summary = subprocess.run([sys.executable, str(SUMMARY), str(results)],
                             capture_output=True, text=True, check=True).stdout
    # smoke tier: 6 tests; passed = infra_once, plain_flaky, ordinary, first_time;
    # failed = infra_twice, ordinary_failure; flaky = 2
    assert "| smoke | 4 | 2 | 0 | 2 |" in summary, summary
    assert "`test_child#test_infra_fails_once` (smoke): 2 attempts, flaky (passed on a rerun)" in summary
    assert "`test_child#test_plain_flaky_passes_on_third_attempt` (smoke): 3 attempts, flaky" in summary
    assert "`test_child#test_infra_fails_twice` (smoke): 2 attempts, failed" in summary
    flaky_in_results = sorted(name for name, group in _attempts(results).items()
                              if any(_flag(a) for a in group))
    assert flaky_in_results == ["test_infra_fails_once", "test_plain_flaky_passes_on_third_attempt"]


@pytest.mark.docker
@pytest.mark.timeout(600)
def test_the_pinned_report_generator_calls_the_rerun_node_flaky(pytester, tmp_path):
    results = _run_child(pytester)
    report = tmp_path / "report"
    env = {**os.environ, "ALLURE_TOOL_CACHE": str(tmp_path / "tool-cache")}
    proc = subprocess.run([str(REPORT), str(results), str(report)], env=env,
                          capture_output=True, text=True, timeout=580)
    assert proc.returncode == 0, proc.stderr
    cases = {}
    for path in (report / "data" / "test-cases").glob("*.json"):
        case = json.loads(path.read_text())
        if case.get("retry"):  # an earlier attempt, kept as history of the final case
            continue
        assert case["name"] not in cases, f"two final cases for {case['name']}"
        cases[case["name"]] = case
    for name in ("test_infra_fails_once", "test_plain_flaky_passes_on_third_attempt"):
        assert cases[name]["status"] == "passed", name
        assert cases[name]["flaky"] is True, name
        assert cases[name]["retriesCount"] >= 1, name
    assert cases["test_infra_fails_once"]["retriesCount"] == 1
    assert cases["test_plain_flaky_passes_on_third_attempt"]["retriesCount"] == 2
    for name in ("test_ordinary_pass", "test_infra_passes_first_time", "test_ordinary_failure"):
        assert cases[name]["flaky"] is False, name
    assert cases["test_infra_fails_twice"]["status"] == "failed"
    assert cases["test_infra_fails_twice"]["flaky"] is False
