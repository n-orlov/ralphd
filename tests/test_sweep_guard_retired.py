"""Requirement A.1 (v0.20.2, #113 step 4): the sweep-sha practice goes
entire. `tests/test_sweep_sha_guard.py`, `tests/sweep_sha_guard.py` and
`docs/sweep-records.md` are deleted outright, and the sweep enforcement is
removed from `.github/workflows/gate.yml` and `.github/workflows/nightly.yml`.

This module is the named test the requirement itself asks for. It parses
both workflow files with PyYAML and asserts that no job step's `run` field
invokes the retired guard, by path or by pytest node id -- the same
"inspect the shape, not the surrounding prose" discipline
`tests/test_gate_pinned_worker_count_guard.py` and
`tests/test_ci_dev_layout_matrix_guard.py` already use for these same two
files. A *comment* explaining the retirement (this task's own commit adds
one to `gate.yml`) is not an invocation and must not trip this check --
only an executed `run:` line counts.

This deliberately does not touch historical reports under
`artifacts/reports/`: their existing `sweep-sha`/`archived-sha` fenced
blocks become inert text, not deleted (requirement A.1's own text), so a
report that still names the module in prose is expected and out of scope
here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = REPO_ROOT / ".github" / "workflows" / "gate.yml"
NIGHTLY_PATH = REPO_ROOT / ".github" / "workflows" / "nightly.yml"

# Either of these appearing in an executed `run:` line means that workflow
# still invokes the retired guard, whatever step name wraps it.
_RETIRED_MARKERS = ("sweep_sha_guard", "test_sweep_sha_guard")


def _run_lines(workflow_path: Path) -> list[str]:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    lines: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if run:
                lines.append(run)
    return lines


def test_gate_and_nightly_invoke_no_sweep_guard() -> None:
    for path in (GATE_PATH, NIGHTLY_PATH):
        assert path.exists(), f"{path} is missing"
        for run in _run_lines(path):
            for marker in _RETIRED_MARKERS:
                assert marker not in run, (
                    f"{path.relative_to(REPO_ROOT)} still runs the "
                    f"retired sweep guard ({marker!r}): {run!r} -- "
                    f"requirement A.1 retires that module entire"
                )
