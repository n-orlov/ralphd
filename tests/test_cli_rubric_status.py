"""Black-box tests for `ralphctl start`/`ralphctl doctor --prd` printing
which materiality rubric applies (task 005, PRD requirement A.3): exactly
one `rubric: ...` line, one of `inline`, `by reference (<path>)` or
`engine default`, plus a warning on a referenced rubric path that does not
resolve. The line goes to **stderr** (task 041): `ralphctl start`'s stdout
is a machine-readable contract -- one JSON object under `--json` -- and
task 005 originally printed the rubric line to stdout, which broke every
caller that does `json.loads(res.stdout)`. Each test invokes the real
`ralphctl` executable with
RALPHD_DOCKER pointing at the recording stub (tests/stub-docker/docker) and
RALPHD_REGISTRY at a tmp dir. No CLI internals are imported -- this mirrors
tests/test_cli_docker.py's harness.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import RALPHCTL, with_ralphd_importable

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"

_RUBRIC_LINE_RE = re.compile(r"^rubric: .*$", re.MULTILINE)

INLINE_PRD = """# Some PRD

## Goal

Ship the thing.

## Materiality rubric

**Blocking** -- a red test at the shipped sha.

**Residual** -- a stale snapshot label.
"""

REFERENCE_DOCUMENT = """# Review economics

## Materiality rubric

**Blocking** -- a red test at the shipped sha.

**Residual** -- a stale snapshot label.
"""

BY_REFERENCE_PRD = """# Some PRD

<!-- rubric: docs/review-economics.md -->

## Goal

Ship the thing.
"""

NO_RUBRIC_PRD = """# Some PRD

## Goal

Ship the thing, no rubric anywhere in this contract.
"""

UNRESOLVABLE_REFERENCE_PRD = """# Some PRD

<!-- rubric: docs/does-not-exist.md -->

## Goal

Ship the thing.
"""


class Ctl:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.log = tmp / "docker-argv.jsonl"

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = with_ralphd_importable({
            **os.environ,
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            **(env or {}),
        })
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=60)


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _rubric_line(stderr: str) -> str:
    m = _RUBRIC_LINE_RE.search(stderr)
    assert m is not None, f"no rubric line in stderr:\n{stderr}"
    return m.group(0)


# --------------------------------------------------------- ralphctl start --

def test_start_prints_inline_rubric(ctl, tmp_path):
    prd = tmp_path / "prd.md"
    prd.write_text(INLINE_PRD)
    res = ctl.run("start", "--prd", str(prd), "--llm", "none",
                  "--run-id", "tst-inline")
    assert res.returncode == 0, res.stderr
    assert _rubric_line(res.stderr) == "rubric: inline"


def test_start_stdout_is_one_json_object_when_a_rubric_line_is_printed(
        ctl, tmp_path):
    """Task 041: `ralphctl --json start`'s stdout is exactly the one JSON
    object it was before task 005 -- the rubric line is on stderr, so
    `json.loads(res.stdout)` (what every docker e2e does) still parses."""
    prd = tmp_path / "prd.md"
    prd.write_text(INLINE_PRD)
    res = ctl.run("--json", "start", "--prd", str(prd), "--llm", "none",
                  "--run-id", "tst-stdout-json")
    assert res.returncode == 0, res.stderr
    # the rubric line WAS printed -- on stderr, where it belongs
    assert _rubric_line(res.stderr) == "rubric: inline"
    doc = json.loads(res.stdout)
    assert isinstance(doc, dict), doc
    assert doc["runId"] == "tst-stdout-json"
    assert "rubric" not in res.stdout


def test_start_prints_by_reference_rubric(ctl, tmp_path):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs" / "review-economics.md").write_text(REFERENCE_DOCUMENT)
    prd = tmp_path / "prd.md"
    prd.write_text(BY_REFERENCE_PRD)
    res = ctl.run("start", "--prd", str(prd), "--llm", "none",
                  "--workspace", str(ws), "--run-id", "tst-byref")
    assert res.returncode == 0, res.stderr
    assert _rubric_line(res.stderr) == \
        "rubric: by reference (docs/review-economics.md)"
    assert res.stderr == "" or "did not resolve" not in res.stderr


def test_start_prints_engine_default_rubric(ctl, tmp_path):
    prd = tmp_path / "prd.md"
    prd.write_text(NO_RUBRIC_PRD)
    res = ctl.run("start", "--prd", str(prd), "--llm", "none",
                  "--run-id", "tst-default")
    assert res.returncode == 0, res.stderr
    assert _rubric_line(res.stderr) == "rubric: engine default"


def test_start_warns_on_unresolvable_reference(ctl, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    prd = tmp_path / "prd.md"
    prd.write_text(UNRESOLVABLE_REFERENCE_PRD)
    res = ctl.run("start", "--prd", str(prd), "--llm", "none",
                  "--workspace", str(ws), "--run-id", "tst-warn")
    assert res.returncode == 0, res.stderr
    assert _rubric_line(res.stderr) == "rubric: engine default"
    assert "did not resolve" in res.stderr
    assert "docs/does-not-exist.md" in res.stderr


# -------------------------------------------------------- ralphctl doctor --

def test_doctor_prints_inline_rubric_for_readable_prd(ctl, tmp_path):
    prd = tmp_path / "prd.md"
    prd.write_text(INLINE_PRD)
    res = ctl.run("doctor", "--prd", str(prd),
                  env={"STUB_DOCKER_INSPECT_OK": "1"})
    assert _rubric_line(res.stderr) == "rubric: inline"


def test_doctor_prints_by_reference_rubric_relative_to_cwd(ctl, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "review-economics.md").write_text(REFERENCE_DOCUMENT)
    prd = tmp_path / "prd.md"
    prd.write_text(BY_REFERENCE_PRD)
    res = subprocess.run(
        [str(RALPHCTL), "doctor", "--prd", str(prd)],
        cwd=str(tmp_path),
        env=with_ralphd_importable({
            **os.environ,
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(ctl.registry),
            "STUB_DOCKER_LOG": str(ctl.log),
            "STUB_DOCKER_INSPECT_OK": "1",
        }),
        capture_output=True, text=True, timeout=60)
    assert _rubric_line(res.stderr) == \
        "rubric: by reference (docs/review-economics.md)"


def test_doctor_prints_engine_default_rubric(ctl, tmp_path):
    prd = tmp_path / "prd.md"
    prd.write_text(NO_RUBRIC_PRD)
    res = ctl.run("doctor", "--prd", str(prd),
                  env={"STUB_DOCKER_INSPECT_OK": "1"})
    assert _rubric_line(res.stderr) == "rubric: engine default"


def test_doctor_warns_on_unresolvable_reference(ctl, tmp_path):
    prd = tmp_path / "prd.md"
    prd.write_text(UNRESOLVABLE_REFERENCE_PRD)
    res = ctl.run("doctor", "--prd", str(prd),
                  env={"STUB_DOCKER_INSPECT_OK": "1"})
    assert _rubric_line(res.stderr) == "rubric: engine default"
    assert "did not resolve" in res.stderr
    assert "docs/does-not-exist.md" in res.stderr


def test_doctor_without_prd_prints_no_rubric_line(ctl):
    res = ctl.run("doctor", env={"STUB_DOCKER_INSPECT_OK": "1"})
    assert _RUBRIC_LINE_RE.search(res.stderr) is None


def test_doctor_with_unreadable_prd_path_prints_no_rubric_line(ctl, tmp_path):
    missing = tmp_path / "nope.md"
    res = ctl.run("doctor", "--prd", str(missing),
                  env={"STUB_DOCKER_INSPECT_OK": "1"})
    assert _RUBRIC_LINE_RE.search(res.stderr) is None
