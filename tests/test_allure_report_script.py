"""v0.35 (#303): scripts/allure_report.sh turns a results directory into a report.

The script downloads a pinned Allure CLI, checks the archive against a pinned
SHA-256 before it unpacks or runs anything, and keeps the verified copy in the
runner tool cache. Two nodes:

* the report node runs the real script (real download, real CLI, a JRE fetched
  and verified the same way when the host has no `java`) over a tiny fixture
  directory and finds the fixture's test names in the generated report. It is
  marked `docker`: that tier's `ci.yml` job runs on the host runner with
  network, a tool cache and a shell, and the node passes there instead of
  skipping;
* the wrong-checksum node serves an archive whose hash is not the pinned one
  through a `file://` URL: exit status is non-zero, nothing is unpacked and no
  report directory appears. It needs no network and runs in the fast tier.
"""
from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "allure_report.sh"

_NAMES = ("fixture_alpha_passes", "fixture_beta_fails")


def _fixture_results(root: Path) -> Path:
    results = root / "allure-results"
    results.mkdir()
    for i, (name, status) in enumerate(zip(_NAMES, ("passed", "failed"))):
        (results / f"{i}-result.json").write_text(json.dumps({
            "uuid": f"0000000{i}-0000-0000-0000-000000000000",
            "historyId": f"h{i}",
            "name": name,
            "fullName": f"fixture.{name}",
            "status": status,
            "stage": "finished",
            "start": 1000, "stop": 2000,
            "labels": [{"name": "parentSuite", "value": "fast"},
                       {"name": "suite", "value": "fixture_module"}],
        }))
    return results


def _run(args: list[str], env_extra: dict[str, str], timeout: int):
    env = {**os.environ, **env_extra}
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True,
                          text=True, timeout=timeout)


@pytest.mark.docker
@pytest.mark.timeout(600)
def test_report_contains_the_fixture_tests(tmp_path):
    results = _fixture_results(tmp_path)
    report = tmp_path / "report"
    cache = {"ALLURE_TOOL_CACHE": str(tmp_path / "tool-cache")}
    proc = _run([str(results), str(report)], cache, timeout=580)
    assert proc.returncode == 0, proc.stderr
    assert (report / "index.html").is_file()
    published = "\n".join(p.read_text(errors="replace")
                          for p in report.rglob("*.json"))
    for name in _NAMES:
        assert name in published, f"{name} missing from the generated report"

    # the verified copy is reused: with both download URLs unreachable a second
    # run still succeeds out of the tool cache
    again = tmp_path / "report-again"
    offline = {**cache, "ALLURE_CLI_URL": "file:///nonexistent/allure.tgz",
               "ALLURE_JRE_URL": "file:///nonexistent/jre.tgz"}
    second = _run([str(results), str(again)], offline, timeout=120)
    assert second.returncode == 0, second.stderr
    assert "reusing verified" in second.stderr
    assert (again / "index.html").is_file()


def test_wrong_checksum_exits_non_zero_and_writes_no_report(tmp_path):
    results = _fixture_results(tmp_path)
    # a perfectly good tarball whose hash is simply not the pinned one
    payload = tmp_path / "payload" / "bin"
    payload.mkdir(parents=True)
    marker = tmp_path / "ran"
    (payload / "allure").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (payload / "allure").chmod(0o755)
    archive = tmp_path / "allure.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "payload", arcname="allure-x")
    cache = tmp_path / "cache"
    report = tmp_path / "report"
    proc = _run([str(results), str(report)],
                {"ALLURE_CLI_URL": archive.as_uri(),
                 "ALLURE_TOOL_CACHE": str(cache)}, timeout=60)
    assert proc.returncode != 0
    assert "checksum mismatch" in proc.stderr
    assert not report.exists()
    assert not marker.exists(), "the unverified archive's contents ran"
    assert not (cache / "allure").exists(), "the unverified archive was unpacked"
