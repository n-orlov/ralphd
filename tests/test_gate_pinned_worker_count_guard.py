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

Task 009 (requirement F, #205 step 1) added a second, independent concern to
this same module rather than a new file: job `fast` declares a
`container:` with a pinned image reference, and the interpreter's patch
level it names must be 3.12.3 -- the exact interpreter #182 needed and the
fleet lost when the lane moved off the VPS (see gate.yml's own header).
`check_pinned_worker_count` covers it, so the pass/fail node covers it too,
and `test_guard_fires_when_the_container_pin_is_removed` is its mutation
test.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = REPO_ROOT / ".github" / "workflows" / "gate.yml"

# A numeric `-n` value: `-n 2`, `-n2`, `-n 12` all count; `-n auto` does not.
NUMERIC_N_RE = re.compile(r"-n\s*\d+\b")
AUTO_N_RE = re.compile(r"-n\s*auto\b")

# F.2: the chosen patch level, and the shape a pinned reference must take --
# either a `name@sha256:<64 hex>` digest or a `name:tag` whose tag names a
# patch level outright (a bare `latest`/`slim`/major.minor tag would not).
CHOSEN_PATCH_LEVEL = "3.12.3"
PINNED_REFERENCE_RE = re.compile(
    r"^[^\s@:]+(?::[^\s@]+)?@sha256:[0-9a-f]{64}$|^[^\s@:]+:\d+\.\d+\.\d+[\w.-]*$"
)
PATCH_LEVEL_RE = re.compile(r"(\d+\.\d+\.\d+)")


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


def _fast_job_container_image(workflow: dict) -> str | None:
    jobs = workflow.get("jobs") or {}
    fast = jobs.get("fast") or {}
    container = fast.get("container")
    if isinstance(container, str):
        return container
    if isinstance(container, dict):
        return container.get("image")
    return None


def check_pinned_worker_count(workflow: dict) -> list[str]:
    """Return human-readable problems with the fast-tier step's pytest
    invocation; an empty list means it passes a fixed numeric `-n <N>`
    instead of `-n auto`, and job `fast` runs in the pinned container."""
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

    # F.1/F.2 (task 009, #205 step 1): job `fast` must declare a `container:`
    # naming a pinned image, and that pin's patch level must be the chosen
    # 3.12.3 (or the commit message states why not -- this guard only knows
    # the value actually in the file).
    image = _fast_job_container_image(workflow)
    if image is None:
        problems.append("job 'fast' declares no container: image")
    else:
        if not PINNED_REFERENCE_RE.match(image):
            problems.append(
                f"container image is not pinned by digest or exact patch tag: {image!r}"
            )
        match = PATCH_LEVEL_RE.search(image)
        if match is None:
            problems.append(f"container image names no interpreter patch level: {image!r}")
        elif match.group(1) != CHOSEN_PATCH_LEVEL:
            problems.append(
                f"container image pins {match.group(1)!r}, not the chosen "
                f"{CHOSEN_PATCH_LEVEL!r}: {image!r}"
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


def test_guard_fires_when_the_n_flag_is_dropped(tmp_path):
    broken_no_n = copy.deepcopy(_load(GATE_PATH))
    step = _fast_tier_step(broken_no_n)
    assert step is not None
    step["run"] = re.sub(r"-n\s*\d+\b", "", step["run"])

    fixture_no_n = tmp_path / "broken-gate-no-n.yml"
    fixture_no_n.write_text(yaml.safe_dump(broken_no_n, sort_keys=False), encoding="utf-8")

    problems = check_pinned_worker_count(_load(fixture_no_n))
    assert any("lacks a fixed numeric" in p for p in problems), problems


def test_guard_fires_when_the_container_pin_is_removed(tmp_path):
    broken = copy.deepcopy(_load(GATE_PATH))
    fast_job = broken["jobs"]["fast"]
    del fast_job["container"]

    fixture = tmp_path / "broken-gate-no-container.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_pinned_worker_count(_load(fixture))
    assert any("declares no container" in p for p in problems), problems

    mistagged = copy.deepcopy(_load(GATE_PATH))
    mistagged["jobs"]["fast"]["container"] = {"image": "python:3.14.5-slim"}

    fixture2 = tmp_path / "broken-gate-wrong-patch.yml"
    fixture2.write_text(yaml.safe_dump(mistagged, sort_keys=False), encoding="utf-8")

    problems = check_pinned_worker_count(_load(fixture2))
    assert any("pins " in p and "not the chosen" in p for p in problems), problems
