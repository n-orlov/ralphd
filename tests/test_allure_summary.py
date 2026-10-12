"""v0.35 (#303) E.3/C.2: scripts/allure_summary.py over a fixture of results.

The fixture holds, across two tiers: passes, one failure, one skip, one node
that failed and then passed on a rerun (flaky), and one that failed twice
(rerun, still failed). The test asserts the exact per-tier counts and that the
rerun-then-pass node is listed as flaky while the double failure is not.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "allure_summary.py"


def _write(directory: Path, name: str, tier: str, status: str, stop: int,
           history: str | None = None) -> None:
    hid = history or name
    (directory / f"{hid}-{stop}-result.json").write_text(json.dumps({
        "uuid": f"{hid}-{stop}", "historyId": hid, "name": name,
        "fullName": f"tests.mod.{name}", "status": status, "stage": "finished",
        "start": stop - 10, "stop": stop,
        "labels": [{"name": "parentSuite", "value": tier},
                   {"name": "suite", "value": "mod"}],
    }))


def _fixture(root: Path) -> list[Path]:
    fast = root / "allure-results-fast-1-1"
    docker = root / "allure-results-docker-1-1"
    fast.mkdir()
    docker.mkdir()
    for n in ("a", "b", "c"):
        _write(fast, f"fast_pass_{n}", "fast", "passed", 100)
    _write(fast, "fast_fails", "fast", "failed", 100)
    _write(fast, "fast_skipped", "fast", "skipped", 100)
    # rerun-then-pass: two attempts of one node, the later one passes
    _write(fast, "fast_flaky", "fast", "failed", 100)
    _write(fast, "fast_flaky", "fast", "passed", 200)
    _write(docker, "docker_pass", "docker", "passed", 100)
    # fails on both attempts: a rerun that did not rescue it
    _write(docker, "docker_twice", "docker", "failed", 100)
    _write(docker, "docker_twice", "docker", "broken", 200)
    return [fast, docker]


def test_summary_counts_per_tier_and_lists_the_rerun_nodes(tmp_path):
    dirs = _fixture(tmp_path)
    out = tmp_path / "summary.md"
    proc = subprocess.run([sys.executable, str(SCRIPT), *map(str, dirs),
                           "--output", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    text = out.read_text()
    rows = {}
    for line in text.splitlines():
        if line.startswith("| ") and not line.startswith(("| Tier", "| ---")):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows[cells[0]] = [int(c) for c in cells[1:]]
    # passed, failed, skipped, flaky (flaky is a subset of passed)
    assert rows == {"fast": [4, 1, 1, 1], "docker": [1, 1, 0, 0]}
    rerun_section = text.split("### Rerun nodes", 1)[1]
    flaky_line = next(l for l in rerun_section.splitlines() if "fast_flaky" in l)
    assert "flaky" in flaky_line and "2 attempts" in flaky_line
    twice_line = next(l for l in rerun_section.splitlines() if "docker_twice" in l)
    assert "flaky" not in twice_line and "failed" in twice_line
    assert "fast_fails" not in rerun_section and "fast_pass_a" not in rerun_section


def test_summary_with_no_reruns_says_so_and_prints_to_stdout(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    _write(d, "only_pass", "fast", "passed", 100)
    proc = subprocess.run([sys.executable, str(SCRIPT), str(d)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "| fast | 1 | 0 | 0 | 0 |" in proc.stdout
    assert proc.stdout.rstrip().endswith("None.")
