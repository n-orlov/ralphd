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

Task 053 (v0.20, #113 step 4) removed `DESELECT_NODE` and its
required-deselect check (`check_nightly_matrix` no longer asserts that any
deselect line is present) along with its own mutation test,
`test_nightly_matrix_guard_fires_when_the_deselect_is_dropped`: the deselect
it enforced
(`tests/test_sweep_sha_guard.py::test_every_declared_sweep_sha_is_current_or_discharged`)
is itself retired in the same commit, so a guard requiring its presence
would fail against the now-correct workflow file. No surviving node covers
this one -- the requirement it pinned no longer exists to be covered.
"""

from __future__ import annotations

import copy
import re
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

# `-v "$GITHUB_WORKSPACE:<container path>:ro"` -- the mount must name the
# runner's own checkout, not just any read-only path (a `-v /tmp/x:/src-ro:ro`
# mount satisfies ":ro appears somewhere" while mounting nothing under test).
WORKSPACE_RO_MOUNT_RE = re.compile(
    r"-v\s+\"?\$(?:\{)?GITHUB_WORKSPACE(?:\})?:(?P<target>[^\s\":]+):ro\"?"
)
# Anything that writes into the tree or the image; each must follow the copy.
INSTALL_RE = re.compile(r"(apt-get\s+install|apt\s+install|pip3?\s+install)")


def _docker_payload_spans(run: str) -> list[tuple[int, int]]:
    """Return `(start, end)` character spans of `run` that lie inside a
    `docker run ... bash -c <quoted payload>` command.

    A `host_like_dev_layout.sh` occurrence OUTSIDE every such span runs on the
    runner itself -- including one appended to the very same `run:` script that
    also contains the containerized invocation, which a "first `docker run`
    precedes the first script mention" test would miss.
    """
    spans: list[tuple[int, int]] = []
    idx = 0
    while True:
        start = run.find("docker run", idx)
        if start == -1:
            return spans
        marker = run.find("bash -c", start)
        if marker == -1:
            spans.append((start, len(run)))
            return spans
        cursor = marker + len("bash -c")
        while cursor < len(run) and run[cursor] in " \t\n\\":
            cursor += 1
        if cursor < len(run) and run[cursor] in "\"'":
            quote = run[cursor]
            end = run.find(quote, cursor + 1)
            end = len(run) if end == -1 else end
            spans.append((start, end))
            idx = end + 1
        else:
            spans.append((start, len(run)))
            return spans


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
    listed = [(cell.get("image"), cell.get("mode")) for cell in include]
    cells = set(listed)
    if cells != EXPECTED_CELLS:
        problems.append(
            f"matrix cells are {sorted(cells)}, expected {sorted(EXPECTED_CELLS)}"
        )
    # Set equality alone would accept a seventh, duplicate cell (which really
    # does schedule a seventh check run): the list length is checked too.
    if len(listed) != len(EXPECTED_CELLS):
        problems.append(
            f"matrix declares {len(listed)} cells, expected exactly "
            f"{len(EXPECTED_CELLS)}: {listed}"
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

    problems.extend(_check_readonly_workspace_mount(run_scripts))

    for run in run_scripts:
        spans = _docker_payload_spans(run)
        for match in re.finditer(r"host_like_dev_layout\.sh", run):
            if not any(start <= match.start() < end for start, end in spans):
                problems.append(
                    "scripts/host_like_dev_layout.sh is invoked outside a "
                    "docker run command -- a host-level invocation"
                )

    return problems


def _check_readonly_workspace_mount(run_scripts: list[str]) -> list[str]:
    """$GITHUB_WORKSPACE must be mounted `:ro` into a `docker run`, and the
    mount target must be `cp -a`'d to a DIFFERENT container-local path before
    anything installs -- the whole point being that no root-in-container
    process writes into the runner's checkout."""
    problems: list[str] = []
    mounted = False
    for run in run_scripts:
        if "docker run" not in run:
            continue
        match = WORKSPACE_RO_MOUNT_RE.search(run)
        if match is None:
            continue
        mounted = True
        target = match.group("target").rstrip("/")
        payload = "".join(run[start:end] for start, end in _docker_payload_spans(run))
        copy = re.search(
            rf"cp\s+-a\s+{re.escape(target)}/?\s+(?P<dest>\S+)", payload
        )
        if copy is None:
            problems.append(
                f"the read-only mount target {target} is never `cp -a`'d to a "
                "container-local path inside the container"
            )
            continue
        if copy.group("dest").rstrip("/") == target:
            problems.append(
                f"`cp -a` copies {target} onto itself -- the build would run "
                "in the read-only mount"
            )
        install = INSTALL_RE.search(payload)
        if install is not None and install.start() < copy.start():
            problems.append(
                f"{install.group(0)!r} runs before the read-only checkout is "
                f"copied out of {target}"
            )
    if not mounted:
        problems.append(
            'no step mounts the runner checkout read-only into a docker run '
            '(-v "$GITHUB_WORKSPACE:<container path>:ro")'
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


def test_nightly_matrix_guard_fires_on_a_host_invocation_in_the_docker_step(tmp_path):
    """The host-level invocation need not get its own step: appending one to
    the SAME `run:` script that carries the containerized invocation must fire
    too."""
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    steps = broken["jobs"]["dev-layout"]["steps"]
    for step in steps:
        if "run" in step and "docker run" in step["run"]:
            step["run"] = step["run"] + "\nscripts/host_like_dev_layout.sh\n"
    fixture = _dump_fixture(tmp_path, "broken-host-invocation-same-step.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("host-level invocation" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_a_seventh_duplicate_cell_is_added(tmp_path):
    """A duplicate cell collapses under set equality but really does schedule
    a seventh check run, so the count is checked as well."""
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    include = broken["jobs"]["dev-layout"]["strategy"]["matrix"]["include"]
    include.append(copy.deepcopy(include[0]))
    fixture = _dump_fixture(tmp_path, "broken-seventh-cell.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("expected exactly" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_the_mount_is_not_the_workspace(tmp_path):
    """`:ro` appearing somewhere is not enough -- the mount must name
    $GITHUB_WORKSPACE itself."""
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    for step in broken["jobs"]["dev-layout"]["steps"]:
        if "run" in step and "docker run" in step["run"]:
            step["run"] = step["run"].replace(
                '"$GITHUB_WORKSPACE:/src-ro:ro"', '"/tmp/unrelated:/src-ro:ro"'
            )
    fixture = _dump_fixture(tmp_path, "broken-unrelated-mount.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("read-only into a docker run" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_the_copy_is_dropped(tmp_path):
    """Mounting the workspace read-only without copying it out means the
    build would run in the read-only mount."""
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    for step in broken["jobs"]["dev-layout"]["steps"]:
        if "run" in step and "cp -a" in step["run"]:
            step["run"] = "\n".join(
                line for line in step["run"].splitlines() if "cp -a" not in line
            ) + "\n"
    fixture = _dump_fixture(tmp_path, "broken-no-copy.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("never `cp -a`" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_an_install_precedes_the_copy(tmp_path):
    """An `apt-get install` ahead of the copy would build against the
    read-only mount instead of the container-local tree."""
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    for step in broken["jobs"]["dev-layout"]["steps"]:
        if "run" in step and "cp -a /src-ro /src" in step["run"]:
            step["run"] = step["run"].replace(
                "cp -a /src-ro /src",
                "apt-get install -y -qq python3\n              cp -a /src-ro /src",
            )
    fixture = _dump_fixture(tmp_path, "broken-install-before-copy.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("before the read-only checkout is copied" in p for p in problems), problems


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
