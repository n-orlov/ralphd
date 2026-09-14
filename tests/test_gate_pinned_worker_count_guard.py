"""Task 012: the gate lane's fast tier must pin `-n <N>` to a fixed integer,
never `-n auto` -- `auto` re-derives the worker count from `nproc` at
collection time on every run, which is a second, independent source of
run-to-run flake on top of the suite's own test-order-dependent flake (steer
018's cancellation-boundary defect and task 011's timing-margin finding were
both found under exactly that kind of oversubscription). Pinning the worker
count is not a cure for those defects and is not claimed to be one here --
see the commit message for which commits (tasks 005-011) are.

This module parses `.github/workflows/gate.yml` with PyYAML, the same
discipline `tests/test_ci_dev_layout_matrix_guard.py` uses for
`nightly.yml`/`heavy.yml`: check the real file, then show the guard is
non-vacuous by mutating a deep copy back to `-n auto` in a scratch fixture
under `tmp_path` (never under `.github/`) and confirming it fires there.

The records-lint step (`Records lint`) is a separate `steps` entry with its
own `name` and is asserted unchanged -- this guard only inspects the step
whose `run` invokes `-m fast`. The step's name and run line were updated by
requirement A.1 (v0.20.2, the sweep-sha retirement): `tests/test_sweep_sha_
guard.py` no longer exists, so the records-lint step no longer invokes that
guard. Task 054 (K.3) then wrapped that same run line in
`scripts/run_bounded.sh` so an overrunning records lint reports a step
`failure` carrying the RALPHD_TIMEOUT_KILL marker instead of the job-level
bound's ambiguous `cancelled`; the pin below is still an exact-equality pin,
now on the wrapped text, and `tests/test_run_bounded_timeout_marker.py`
owns the bound's own coverage.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = REPO_ROOT / ".github" / "workflows" / "gate.yml"

RECORDS_STEP_NAME = "Records lint"
RECORDS_STEP_RUN = (
    'scripts/run_bounded.sh 300 "records lint" -- python -m pytest -q tests/records/'
)

# A numeric `-n` value: `-n 2`, `-n2`, `-n 12` all count; `-n auto` does not.
NUMERIC_N_RE = re.compile(r"-n\s*\d+\b")
AUTO_N_RE = re.compile(r"-n\s*auto\b")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _fast_tier_step(workflow: dict) -> dict | None:
    jobs = workflow.get("jobs") or {}
    fast = jobs.get("fast") or {}
    for step in fast.get("steps") or []:
        run = step.get("run") or ""
        if "-m fast" in run:
            return step
    return None


def _records_step(workflow: dict) -> dict | None:
    jobs = workflow.get("jobs") or {}
    fast = jobs.get("fast") or {}
    for step in fast.get("steps") or []:
        if step.get("name") == RECORDS_STEP_NAME:
            return step
    return None


def check_pinned_worker_count(workflow: dict) -> list[str]:
    """Return human-readable problems with the fast-tier step's pytest
    invocation; an empty list means it passes a fixed numeric `-n <N>`
    instead of `-n auto`, and the records-lint step is untouched."""
    problems: list[str] = []

    step = _fast_tier_step(workflow)
    if step is None:
        problems.append("no step in job 'fast' invokes '-m fast'")
        return problems

    run = step.get("run") or ""
    if AUTO_N_RE.search(run):
        problems.append(f"fast-tier step still passes -n auto: {run!r}")
    if not NUMERIC_N_RE.search(run):
        problems.append(f"fast-tier step lacks a fixed numeric -n <N>: {run!r}")

    records = _records_step(workflow)
    if records is None:
        problems.append(f"no step named {RECORDS_STEP_NAME!r} in job 'fast'")
    else:
        records_run = (records.get("run") or "").strip()
        if records_run != RECORDS_STEP_RUN:
            problems.append(
                f"records-lint step's run changed: {records_run!r} != "
                f"{RECORDS_STEP_RUN!r}"
            )

    return problems


def test_gate_fast_tier_pins_a_numeric_worker_count():
    problems = check_pinned_worker_count(_load(GATE_PATH))
    assert problems == [], problems


def test_guard_fires_when_mutated_back_to_n_auto(tmp_path):
    broken = copy.deepcopy(_load(GATE_PATH))
    step = _fast_tier_step(broken)
    assert step is not None
    step["run"] = re.sub(r"-n\s*\d+\b", "-n auto", step["run"])

    fixture = tmp_path / "broken-gate.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_pinned_worker_count(_load(fixture))
    assert any("-n auto" in p for p in problems), problems


def test_guard_fires_when_the_n_flag_is_dropped_entirely(tmp_path):
    broken = copy.deepcopy(_load(GATE_PATH))
    step = _fast_tier_step(broken)
    assert step is not None
    step["run"] = re.sub(r"-n\s*\d+\b", "", step["run"])

    fixture = tmp_path / "broken-gate-no-n.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_pinned_worker_count(_load(fixture))
    assert any("lacks a fixed numeric" in p for p in problems), problems


def test_guard_fires_when_the_records_lint_step_is_changed(tmp_path):
    broken = copy.deepcopy(_load(GATE_PATH))
    records = _records_step(broken)
    assert records is not None
    records["run"] = records["run"] + "\necho extra\n"

    fixture = tmp_path / "broken-gate-records.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_pinned_worker_count(_load(fixture))
    assert any("records-lint step's run changed" in p for p in problems), problems
