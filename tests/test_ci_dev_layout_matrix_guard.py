"""Task 077 (steer 008 task A + steer 009): the nightly dev-layout lane is a
six-cell dockerized image x PYTHONUSERBASE matrix, and heavy.yml's browser
tier is pinned to the operator's `browser-runner` runner label.

This module parses `.github/workflows/nightly.yml` and
`.github/workflows/heavy.yml` with PyYAML and checks the shape steer 008 and
009 committed to, rather than trusting that a future edit keeps it -- the
same "read what the assertion actually reads" discipline notes.md rule 10
names for a rendered prompt applies to a workflow file: this module reads
every job/step steer 008/009 named, not just the lines that happened to
change in this task's own commit.

Each check function below is exercised twice: once against the real
`.github/workflows/*.yml` file (must return no problems) and once against a
broken fixture built from a deep copy of that same real file and dumped to
its OWN file under `tmp_path` (must return at least one problem) -- so every
guard is shown non-vacuous. The fixture is never written under `.github/`.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
NIGHTLY_PATH = REPO_ROOT / ".github" / "workflows" / "nightly.yml"
HEAVY_PATH = REPO_ROOT / ".github" / "workflows" / "heavy.yml"

EXPECTED_CELLS = {
    ("ubuntu:24.04", "exported"),
    ("ubuntu:24.04", "unset"),
    ("python:3.11-slim", "exported"),
    ("python:3.11-slim", "unset"),
    ("python:3.14-slim", "exported"),
    ("python:3.14-slim", "unset"),
}

DESELECT_NODE = (
    "tests/test_sweep_sha_guard.py::"
    "test_every_declared_sweep_sha_is_current_or_discharged"
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump_fixture(tmp_path: Path, name: str, workflow: dict) -> Path:
    fixture = tmp_path / name
    fixture.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
    return fixture


def _on_block(workflow: dict) -> dict:
    """PyYAML's SafeLoader resolves the unquoted scalar key `on:` to the
    YAML 1.1 boolean `True` -- every GitHub Actions workflow trips this."""
    if "on" in workflow:
        return workflow.get("on") or {}
    return workflow.get(True) or {}


def check_nightly_matrix(workflow: dict) -> list[str]:
    """Return human-readable problems with `workflow`'s dev-layout job;
    an empty list means the six-cell matrix shape holds."""
    problems: list[str] = []

    if workflow.get("name") != "nightly":
        problems.append(f"workflow name is {workflow.get('name')!r}, not 'nightly'")

    on = _on_block(workflow)
    if "workflow_dispatch" not in on:
        problems.append("workflow_dispatch trigger is missing")
    crons = [entry.get("cron") for entry in (on.get("schedule") or [])]
    if "0 2 * * *" not in crons:
        problems.append(f"the '0 2 * * *' cron entry is missing, found {crons!r}")

    jobs = workflow.get("jobs") or {}
    for job_name, job in jobs.items():
        if "container" in job:
            problems.append(f"job {job_name!r} uses GitHub's own container: key")

    dev_layout = jobs.get("dev-layout")
    if dev_layout is None:
        problems.append("no 'dev-layout' job in this workflow")
        return problems

    strategy = dev_layout.get("strategy") or {}
    if strategy.get("fail-fast") is not False:
        problems.append(
            f"strategy.fail-fast is {strategy.get('fail-fast')!r}, not False"
        )
    if strategy.get("max-parallel") != 2:
        problems.append(
            f"strategy.max-parallel is {strategy.get('max-parallel')!r}, not 2"
        )

    matrix = strategy.get("matrix") or {}
    include = matrix.get("include") or []
    cells = {(cell.get("image"), cell.get("mode")) for cell in include}
    if cells != EXPECTED_CELLS:
        problems.append(
            f"matrix cells are {sorted(cells)}, expected {sorted(EXPECTED_CELLS)}"
        )

    job_name_field = str(dev_layout.get("name") or "")
    if "matrix.image" not in job_name_field or "matrix.mode" not in job_name_field:
        problems.append(
            f"the job's own name {job_name_field!r} does not render both matrix axes"
        )

    steps = dev_layout.get("steps") or []
    checkout_ok = any(
        "checkout" in str(step.get("uses", ""))
        and (step.get("with") or {}).get("fetch-depth") == 0
        for step in steps
    )
    if not checkout_ok:
        problems.append("no checkout step declares fetch-depth: 0")

    run_scripts = [step.get("run", "") for step in steps if step.get("run")]
    joined = "\n".join(run_scripts)
    if ":ro" not in joined or "docker run" not in joined:
        problems.append(
            "no step runs a read-only ($GITHUB_WORKSPACE ... :ro) docker run"
        )
    if DESELECT_NODE not in joined:
        problems.append(f"the sweep-sha carve-out deselect for {DESELECT_NODE} is missing")

    for run in run_scripts:
        if "host_like_dev_layout.sh" not in run:
            continue
        docker_idx = run.find("docker run")
        script_idx = run.find("host_like_dev_layout.sh")
        if docker_idx == -1 or docker_idx > script_idx:
            problems.append(
                "scripts/host_like_dev_layout.sh is invoked outside a "
                "docker run command -- a host-level invocation"
            )

    return problems


def check_no_browser_runner_label(workflow: dict, *, job_names: list[str]) -> list[str]:
    """None of `job_names` in `workflow` may carry the browser-runner label."""
    problems: list[str] = []
    jobs = workflow.get("jobs") or {}
    for job_name in job_names:
        job = jobs.get(job_name)
        if job is None:
            continue
        runs_on = job.get("runs-on") or []
        if "browser-runner" in runs_on:
            problems.append(f"job {job_name!r} carries the browser-runner label")
    return problems


def check_heavy_browser_runner(workflow: dict) -> list[str]:
    problems: list[str] = []
    jobs = workflow.get("jobs") or {}
    docker_tier = jobs.get("docker-tier") or {}
    browser_tier = jobs.get("browser-tier") or {}
    if "browser-runner" in (docker_tier.get("runs-on") or []):
        problems.append("docker-tier's runs-on carries browser-runner")
    if "browser-runner" not in (browser_tier.get("runs-on") or []):
        problems.append("browser-tier's runs-on is missing browser-runner")
    return problems


# --- the real files -----------------------------------------------------


def test_nightly_workflow_matches_the_six_cell_matrix():
    problems = check_nightly_matrix(_load(NIGHTLY_PATH))
    assert problems == [], problems


def test_nightly_workflow_carries_no_browser_runner_label_anywhere():
    workflow = _load(NIGHTLY_PATH)
    job_names = list((workflow.get("jobs") or {}).keys())
    problems = check_no_browser_runner_label(workflow, job_names=job_names)
    assert problems == [], problems


def test_heavy_workflow_pins_the_browser_tier_to_browser_runner():
    problems = check_heavy_browser_runner(_load(HEAVY_PATH))
    assert problems == [], problems


# --- non-vacuous: each guard must actually fire on a broken fixture -----


def test_nightly_matrix_guard_fires_when_a_cell_is_dropped(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["strategy"]["matrix"]["include"].pop()
    fixture = _dump_fixture(tmp_path, "broken-missing-cell.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("matrix cells" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_max_parallel_is_raised(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["strategy"]["max-parallel"] = 3
    fixture = _dump_fixture(tmp_path, "broken-max-parallel.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("max-parallel" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_fail_fast_is_enabled(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["strategy"]["fail-fast"] = True
    fixture = _dump_fixture(tmp_path, "broken-fail-fast.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("fail-fast" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_the_container_key_is_used(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["container"] = "python:3.12"
    fixture = _dump_fixture(tmp_path, "broken-container-key.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("container:" in p for p in problems), problems


def test_nightly_matrix_guard_fires_on_a_host_level_script_invocation(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["steps"].append(
        {"name": "Bare host invocation", "run": "scripts/host_like_dev_layout.sh"}
    )
    fixture = _dump_fixture(tmp_path, "broken-host-invocation.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("host-level invocation" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_the_deselect_is_dropped(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    steps = broken["jobs"]["dev-layout"]["steps"]
    for step in steps:
        if "run" in step and DESELECT_NODE in step["run"]:
            lines = step["run"].splitlines()
            lines = [line for line in lines if "--deselect" not in line]
            step["run"] = "\n".join(lines) + "\n"
    fixture = _dump_fixture(tmp_path, "broken-no-deselect.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("carve-out" in p for p in problems), problems


def test_nightly_browser_runner_guard_fires_when_a_cell_carries_the_label(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["runs-on"] = list(
        broken["jobs"]["dev-layout"]["runs-on"]
    ) + ["browser-runner"]
    fixture = _dump_fixture(tmp_path, "broken-nightly-browser-label.yml", broken)
    problems = check_no_browser_runner_label(_load(fixture), job_names=["dev-layout"])
    assert problems, problems


def test_heavy_browser_runner_guard_fires_when_the_label_is_on_docker_tier(tmp_path):
    broken = copy.deepcopy(_load(HEAVY_PATH))
    broken["jobs"]["docker-tier"]["runs-on"] = list(
        broken["jobs"]["docker-tier"]["runs-on"]
    ) + ["browser-runner"]
    fixture = _dump_fixture(tmp_path, "broken-heavy-docker-label.yml", broken)
    problems = check_heavy_browser_runner(_load(fixture))
    assert any("docker-tier" in p for p in problems), problems


def test_heavy_browser_runner_guard_fires_when_missing_from_browser_tier(tmp_path):
    broken = copy.deepcopy(_load(HEAVY_PATH))
    runs_on = broken["jobs"]["browser-tier"]["runs-on"]
    broken["jobs"]["browser-tier"]["runs-on"] = [
        label for label in runs_on if label != "browser-runner"
    ]
    fixture = _dump_fixture(tmp_path, "broken-heavy-missing-label.yml", broken)
    problems = check_heavy_browser_runner(_load(fixture))
    assert any("browser-tier" in p for p in problems), problems
