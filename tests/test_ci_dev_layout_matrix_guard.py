"""Task 077 (steer 008 task A + steer 009): the nightly dev-layout lane is a
six-cell dockerized image x PYTHONUSERBASE matrix, and ci.yml's browser
job is pinned to the operator's `browser-runner` runner label.

This module parses `.github/workflows/nightly.yml` and
`.github/workflows/ci.yml` with PyYAML and checks the shape steer 008 and
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

Task 010 (requirement G) adds a third, independent concern to this same
module rather than a new file: `check_container_or_allowlisted` walks every
job in every `*.yml` under `.github/workflows/` (auto-discovered, not
enumerated -- a workflow added tomorrow is covered the first time this
module is collected) and fails a job that declares neither a `container:`
nor an entry in `CONTAINER_ALLOWLIST`, and separately fails any allowlist
entry whose reason is blank. Its own two new nodes prove both refusals
against a fixture workflow written under `tmp_path`, never a shipped file.
To keep the fast tier's collected count net-zero (`tests/test_tier_budgets.
py`'s `TIER_CEILINGS`, already at its 5800 ceiling with no headroom), the
three separate `_check_cell_hardening` mutation tests
(`test_dev_layout_guard_fires_when_git_install_is_dropped`,
`..._the_cell_runs_as_root`, `..._a_docker_socket_is_added`) are merged into
one `test_dev_layout_cell_hardening_guard_fires_on_each_mutation`, keeping
all three original assertions -- net -2 there, +2 here, net zero overall.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
NIGHTLY_PATH = REPO_ROOT / ".github" / "workflows" / "nightly.yml"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# ci.yml's two tiers that carry the docker/browser coverage the retired heavy
# workflow held, and so the reap-before-checkout rule (task 004).
HEAVY_JOBS = ("docker", "browser")

# The nightly job's runner labels: the pool plus capability labels only.
POOL_LABELS = ["self-hosted", "linux", "x64", "ci-pool"]
CAPABILITY_LABELS = {"docker-host", "browser-runner"}

EXPECTED_CELLS = {
    ("ubuntu:24.04", "exported"),
    ("ubuntu:24.04", "unset"),
    ("python:3.11-slim", "exported"),
    ("python:3.11-slim", "unset"),
    ("python:3.14-slim", "exported"),
    ("python:3.14-slim", "unset"),
}

# `-v "$RALPHD_HOST_WORKSPACE:<container path>:ro"` -- the mount must name
# the runner's own checkout (task 012, requirement I: resolved to its real
# host-side path rather than named directly -- `$GITHUB_WORKSPACE` is not
# that path once this job is itself containerized, see
# scripts/resolve_container_host_path.sh's own header), not just any
# read-only path (a `-v /tmp/x:/src-ro:ro` mount satisfies ":ro appears
# somewhere" while mounting nothing under test).
WORKSPACE_RO_MOUNT_RE = re.compile(
    r"-v\s+\"?\$(?:\{)?RALPHD_HOST_WORKSPACE(?:\})?:(?P<target>[^\s\":]+):ro\"?"
)
# Anything that writes into the tree or the image; each must follow the copy.
INSTALL_RE = re.compile(r"(apt-get\s+install|apt\s+install|pip3?\s+install)")

# Task 079 (steer 011 defects 1 and 2): every cell's docker payload must
# install a git binary, must drop privileges to a non-root uid before the
# suite runs, and must never gain a reachable docker daemon of its own.
GIT_INSTALL_RE = re.compile(r"apt-get\s+install[^\n]*\bgit\b")
UID_DROP_RE = re.compile(
    r"setpriv\b[^\n]*--reuid|\brunuser\b[^\n]*-u\b|\bgosu\b|\bsu\s+-\w+"
)
DOCKER_IN_CELL_RE = re.compile(
    r"docker\.sock|docker-ce|apt-get\s+install[^\n]*\bdocker(?:\.io)?\b|"
    r"pip3?\s+install[^\n]*\bdocker\b"
)


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
    # Task 012 (requirement I): the outer `dev-layout` job now DOES declare a
    # pinned `container:` of its own (I.1: "both jobs run in pinned
    # containers") -- its steps drive the per-cell nested `docker run`, which
    # needs a docker CLIENT and the host socket its own `options:` must
    # mount. This supersedes the invariant this block used to check (that
    # `container:` was never used here at all) -- that design assumption is
    # gone by this task's own explicit requirement, not by drift. Two things
    # stay real hazards even so, and both are checked below: an outer
    # container with no socket access (its steps could never launch a cell)
    # and an outer container built from one of the axis's own three images
    # (I.3, in so many words: "the outer job's container must not become a
    # second source of the cell's own packages").
    axis_images = {image for image, _ in EXPECTED_CELLS}
    for job_name, job in jobs.items():
        container = job.get("container")
        if container is None:
            continue
        options = str((container or {}).get("options") or "")
        if "docker.sock" not in options:
            problems.append(
                f"job {job_name!r} declares container: with no docker.sock "
                "option in options: -- its steps could never launch the "
                "per-cell docker run"
            )
        image = str((container or {}).get("image") or "").split("@")[0]
        if image in axis_images:
            problems.append(
                f"job {job_name!r}'s own container image {image!r} is one of "
                "the axis's own three images -- a second source of the "
                "cell's own packages"
            )

    dev_layout = jobs.get("dev-layout")
    if dev_layout is None:
        problems.append("no 'dev-layout' job in this workflow")
        return problems

    strategy = dev_layout.get("strategy") or {}
    if strategy.get("fail-fast") is not False:
        problems.append(
            f"strategy.fail-fast is {strategy.get('fail-fast')!r}, not False"
        )
    # The six cells run in parallel on the ci-pool: ANY max-parallel, however
    # high, is a serial ceiling the pool does not need.
    if "max-parallel" in strategy:
        problems.append(
            f"strategy.max-parallel is present ({strategy.get('max-parallel')!r}) "
            "-- the six cells run in parallel and the key must be absent"
        )

    runs_on = list(dev_layout.get("runs-on") or [])
    if runs_on[: len(POOL_LABELS)] != POOL_LABELS:
        problems.append(
            f"runs-on is {runs_on!r}, not led by the pool labels {POOL_LABELS!r}"
        )
    extra = [label for label in runs_on[len(POOL_LABELS) :] if label not in CAPABILITY_LABELS]
    if extra:
        problems.append(
            f"runs-on carries labels {extra!r} that are not capability labels "
            f"{sorted(CAPABILITY_LABELS)!r}"
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

    problems.extend(_check_image_pull(steps))
    problems.extend(_check_concurrency(workflow))
    problems.extend(_check_readonly_workspace_mount(run_scripts))
    problems.extend(_check_pip_resilience(run_scripts))
    problems.extend(_check_result_capture(workflow))
    problems.extend(_check_cell_hardening(run_scripts))

    for run in run_scripts:
        spans = _docker_payload_spans(run)
        for match in re.finditer(r"host_like_dev_layout\.sh", run):
            if not any(start <= match.start() < end for start, end in spans):
                problems.append(
                    "scripts/host_like_dev_layout.sh is invoked outside a "
                    "docker run command -- a host-level invocation"
                )

    return problems


PULL_ATTEMPTS_MIN, PULL_ATTEMPTS_MAX = 2, 5


def _check_image_pull(steps: list[dict]) -> list[str]:
    """The cell's image is pulled by its own step, bounded and retried, and
    that step precedes the step that runs the cell's container."""
    problems: list[str] = []
    pull_idx = [
        i for i, step in enumerate(steps) if "docker pull" in str(step.get("run") or "")
    ]
    run_idx = [
        i
        for i, step in enumerate(steps)
        if "docker run --rm" in str(step.get("run") or "")
        and "--cpus" in str(step.get("run") or "")
    ]
    if not pull_idx:
        return ["no step pulls the cell image with an explicit docker pull"]
    if not run_idx:
        return ["no step runs the cell's container"]
    if pull_idx[0] >= run_idx[0]:
        problems.append(
            "the image pull step does not precede the step that runs the cell's container"
        )
    pull = steps[pull_idx[0]]
    run = str(pull.get("run") or "")
    if not re.search(r"scripts/run_bounded\.sh\s+\d+\b", run):
        problems.append("the image pull is not wrapped in scripts/run_bounded.sh")
    env_image = str((pull.get("env") or {}).get("CELL_IMAGE") or "")
    if "matrix.image" not in env_image or 'docker pull "$CELL_IMAGE"' not in run:
        problems.append("the image pull does not pull the cell's matrix.image")
    loop = re.search(r"for\s+\w+\s+in\s+((?:\d+\s*)+);", run)
    attempts = len(loop.group(1).split()) if loop else None
    if attempts is None:
        problems.append("the image pull has no bounded retry loop")
    elif not PULL_ATTEMPTS_MIN <= attempts <= PULL_ATTEMPTS_MAX:
        problems.append(
            f"the image pull retries {attempts} times, outside the bound "
            f"{PULL_ATTEMPTS_MIN}..{PULL_ATTEMPTS_MAX}"
        )
    return problems


RESULTS_DIR = "allure-results-nightly"
RESULTS_MOUNT_TARGET = "/allure-results"
REPORT_GENERATION_RE = re.compile(
    r"allure_report\.sh|allure_summary\.py|\ballure\s+(?:generate|serve|open)\b|"
    r"allure-commandline|configure-pages|upload-pages-artifact|deploy-pages|gh-pages",
    re.IGNORECASE,
)


def _check_result_capture(workflow: dict) -> list[str]:
    """Each cell's pytest passes write Allure result files into a directory the
    cell container mounts from the job, an `if: always()` upload step after
    the cell publishes it under a name carrying image, mode and run id, and no
    report is generated anywhere in the workflow."""
    problems: list[str] = []
    job = (workflow.get("jobs") or {}).get("dev-layout") or {}
    steps = job.get("steps") or []

    cell_idx = [
        i
        for i, step in enumerate(steps)
        if "docker run --rm" in str(step.get("run") or "") and "--cpus" in str(step.get("run") or "")
    ]
    cell_run = str(steps[cell_idx[0]].get("run")) if cell_idx else ""
    if f"-e HOST_LIKE_DEV_LAYOUT_ALLURE_DIR={RESULTS_MOUNT_TARGET}" not in cell_run:
        problems.append(
            "the cell's docker run does not set HOST_LIKE_DEV_LAYOUT_ALLURE_DIR="
            f"{RESULTS_MOUNT_TARGET} -- the dev-layout script's pytest passes are given no results directory"
        )
    mount = re.search(
        r"-v\s+\"\$RALPHD_HOST_WORKSPACE/(?P<dir>[^:\"\s]+):" + re.escape(RESULTS_MOUNT_TARGET) + r"\"?(?=\s)",
        cell_run,
    )
    if not mount:
        problems.append(
            "the cell's docker run does not mount a job directory writable at "
            f"{RESULTS_MOUNT_TARGET} (result capture mount)"
        )
    elif mount.group("dir") != RESULTS_DIR:
        problems.append(f"the result capture mount names {mount.group('dir')!r}, not {RESULTS_DIR!r}")
    elif f"mkdir -p {RESULTS_DIR}" not in cell_run:
        problems.append(f"the cell step never creates the {RESULTS_DIR} directory it mounts")

    uploads = [
        (i, step)
        for i, step in enumerate(steps)
        if str(step.get("uses") or "").startswith("actions/upload-artifact@")
    ]
    if not uploads:
        problems.append("no actions/upload-artifact step uploads the cell's result files")
    else:
        idx, upload = uploads[0]
        if cell_idx and idx <= cell_idx[0]:
            problems.append("the result upload step does not come after the cell step")
        if str(upload.get("if") or "").strip() != "always()":
            problems.append(
                f"the result upload step's if is {upload.get('if')!r}, not 'always()' "
                "-- a red cell would upload nothing"
            )
        name = str((upload.get("with") or {}).get("name") or "")
        for needed, why in (
            ("matrix.slug", "the cell's image"),
            ("matrix.mode", "its PYTHONUSERBASE mode"),
            ("github.run_id", "the run id"),
        ):
            if needed not in name:
                problems.append(f"the result artifact name {name!r} does not carry {why} ({needed})")
        if str((upload.get("with") or {}).get("path") or "") != RESULTS_DIR:
            problems.append(f"the result upload step does not upload {RESULTS_DIR!r}")

    for cell in (job.get("strategy") or {}).get("matrix", {}).get("include") or []:
        if cell.get("slug") != str(cell.get("image")).replace(":", "-"):
            problems.append(
                f"matrix cell {cell!r}: slug is not the image with ':' replaced by '-'"
            )

    if not re.search(r"apt-get\s+install[^\n]*\bgit\b[^\n]*\bcurl\b", cell_run):
        problems.append(
            "the cell does not install curl beside git -- the fast tier's allure_report.sh "
            "checksum node fetches its archive through curl"
        )

    rendered = json.dumps(workflow)
    hit = REPORT_GENERATION_RE.search(rendered)
    if hit:
        problems.append(
            f"the workflow references report generation ({hit.group(0)!r}) -- "
            "the nightly lane only uploads raw result files"
        )
    return problems


def _check_concurrency(workflow: dict) -> list[str]:
    group = workflow.get("concurrency") or {}
    if not isinstance(group, dict) or group.get("cancel-in-progress") is not False:
        return [
            "concurrency.cancel-in-progress is "
            f"{group.get('cancel-in-progress') if isinstance(group, dict) else group!r}"
            ", not False -- an in-flight nightly must not be cancelled by a newer one"
        ]
    return []


def _check_pip_resilience(run_scripts: list[str]) -> list[str]:
    """Each cell's `docker run` receives PIP_DEFAULT_TIMEOUT=60 and
    PIP_RETRIES=10 and mounts the `ci-pip-cache` volume at the directory the
    script uses as its pip cache (HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR)."""
    problems: list[str] = []
    commands = []
    for run in run_scripts:
        for start, _ in _docker_payload_spans(run):
            marker = run.find("bash -c", start)
            commands.append(run[start : marker if marker != -1 else len(run)])
    header = "\n".join(commands)
    if not header:
        return ["no `docker run` command launches a cell"]
    for name, value in (("PIP_DEFAULT_TIMEOUT", "60"), ("PIP_RETRIES", "10")):
        if not re.search(rf"-e\s+{name}={value}(?!\d)", header):
            problems.append(f"the cell's docker run does not pass {name}={value}")
    mount = re.search(r"-v\s+ci-pip-cache:(?P<dir>/[^\s:\"]+)(?!\S)", header)
    if mount is None:
        problems.append("the cell's docker run does not mount the ci-pip-cache volume")
    elif not re.search(
        rf"-e\s+HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR={re.escape(mount.group('dir'))}(?!\S)",
        header,
    ):
        problems.append(
            f"the ci-pip-cache mount {mount.group('dir')} is not the pip cache "
            "directory (HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR) of the user running "
            "scripts/host_like_dev_layout.sh"
        )
    return problems


def _check_cell_hardening(run_scripts: list[str]) -> list[str]:
    """Task 079 (steer 011 defects 1 and 2): every cell's docker payload must
    install `git`, must drop privileges to a non-root uid before the suite
    runs, and must never gain a reachable docker daemon of its own (`-m
    docker` coverage stays in ci.yml's `docker` job)."""
    problems: list[str] = []
    payloads = [
        "".join(run[start:end] for start, end in _docker_payload_spans(run))
        for run in run_scripts
    ]
    combined = "\n".join(p for p in payloads if p)
    if not combined:
        return problems
    if not GIT_INSTALL_RE.search(combined):
        problems.append("no docker payload installs a git binary")
    if not UID_DROP_RE.search(combined):
        problems.append(
            "no docker payload drops privileges to a non-root uid before "
            "running the suite -- the cell would run as root"
        )
    if DOCKER_IN_CELL_RE.search(combined):
        problems.append(
            "a docker payload installs a docker client or references a "
            "docker socket -- a cell must never gain a reachable daemon"
        )
    return problems


def _check_readonly_workspace_mount(run_scripts: list[str]) -> list[str]:
    """$RALPHD_HOST_WORKSPACE must be mounted `:ro` into a `docker run`, and
    the mount target must be `cp -a`'d to a DIFFERENT container-local path
    before anything installs -- the whole point being that no root-in-
    container process writes into the runner's checkout."""
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
            '(-v "$RALPHD_HOST_WORKSPACE:<container path>:ro")'
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


def check_ci_browser_runner(workflow: dict) -> list[str]:
    problems: list[str] = []
    jobs = workflow.get("jobs") or {}
    for job_name, job in jobs.items():
        labels = job.get("runs-on") or []
        if job_name != "browser" and "browser-runner" in labels:
            problems.append(f"{job_name}'s runs-on carries browser-runner")
    if "browser-runner" not in ((jobs.get("browser") or {}).get("runs-on") or []):
        problems.append("browser's runs-on is missing browser-runner")
    return problems


def check_ci_litter_reap(workflow: dict) -> list[str]:
    """Task 004 (requirement B, #202): every ci.yml docker/browser job must reap root-owned
    `.docker-e2e-tmp-*` litter from the runner's reused workspace BEFORE its
    `actions/checkout` step, with root. Without it a hard-killed docker-tier
    container leaves the root-owned authoritative task record inside the
    checkout, `git clean` cannot remove it and the next job on that
    workspace dies in checkout (CI runs 35108739999/35108738499). An empty
    list means the order holds.

    Root comes from one of two places: a nested `docker run --user 0:0 ...`
    when the job itself still runs unprivileged on the bare runner
    (the docker job), or -- task 011 (requirement H) -- the
    job's own pinned `container:`, whose default user already IS root, so a
    plain `rm -rf` needs no nested privilege escalation (the browser job). Only the first shape is held to the nested-docker pattern;
    a containerized job reaping with a bare `rm -rf` is correct, not a
    defect this function should flag.
    """
    problems: list[str] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        if job_name not in HEAVY_JOBS:
            continue
        steps = job.get("steps") or []
        containerized = "container" in (job or {})
        reap_at = None
        checkout_at = None
        for index, step in enumerate(steps):
            run = str(step.get("run") or "")
            if reap_at is None and ".docker-e2e-tmp-" in run and "rm -rf" in run:
                if not containerized and (
                    "docker run" not in run or "--user 0:0" not in run
                ):
                    problems.append(
                        f"job {job_name!r} reaps litter without a root "
                        "container -- an unprivileged rm cannot remove it"
                    )
                reap_at = index
            if checkout_at is None and "checkout" in str(step.get("uses") or ""):
                checkout_at = index
        if checkout_at is None:
            continue
        if reap_at is None:
            problems.append(f"job {job_name!r} has no litter-reaping step")
        elif reap_at > checkout_at:
            problems.append(
                f"job {job_name!r} reaps litter at step {reap_at}, AFTER its "
                f"checkout at step {checkout_at} -- checkout dies first"
            )
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


def test_ci_workflow_pins_the_browser_job_and_reaps_litter_before_checkout(
    tmp_path,
):
    """Two properties of the same file, checked in one node on purpose: the
    fast tier is at its declared collection ceiling, so task 004's reap guard
    lands as assertions inside this existing node (with its own broken
    fixture, so it is non-vacuous) rather than as new nodes."""
    heavy = _load(CI_PATH)
    assert check_ci_browser_runner(heavy) == []
    assert check_ci_litter_reap(heavy) == []

    dropped = copy.deepcopy(heavy)
    for job in dropped["jobs"].values():
        job["steps"] = [
            step
            for step in job["steps"]
            if ".docker-e2e-tmp-" not in str(step.get("run") or "")
        ]
    fixture = _dump_fixture(tmp_path, "broken-ci-no-reap.yml", dropped)
    problems = check_ci_litter_reap(_load(fixture))
    assert [p for p in problems if "no litter-reaping step" in p], problems

    late = copy.deepcopy(heavy)
    for job in late["jobs"].values():
        steps = job["steps"]
        reap = [s for s in steps if ".docker-e2e-tmp-" in str(s.get("run") or "")]
        job["steps"] = [s for s in steps if s not in reap] + reap
    fixture = _dump_fixture(tmp_path, "broken-ci-late-reap.yml", late)
    problems = check_ci_litter_reap(_load(fixture))
    assert [p for p in problems if "AFTER its" in p], problems

    unprivileged = copy.deepcopy(heavy)
    for job in unprivileged["jobs"].values():
        for step in job["steps"]:
            run = str(step.get("run") or "")
            if ".docker-e2e-tmp-" in run:
                step["run"] = run.replace("--user 0:0 ", "")
    fixture = _dump_fixture(tmp_path, "broken-ci-unprivileged-reap.yml", unprivileged)
    problems = check_ci_litter_reap(_load(fixture))
    assert [p for p in problems if "without a root" in p], problems


# --- non-vacuous: each guard must actually fire on a broken fixture -----


def test_nightly_matrix_guard_fires_when_a_cell_is_dropped(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["strategy"]["matrix"]["include"].pop()
    fixture = _dump_fixture(tmp_path, "broken-missing-cell.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("matrix cells" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_max_parallel_is_raised(tmp_path):
    """Rewritten by the parallel-cells task (it used to pin `max-parallel: 2`):
    the cells run in parallel on the ci-pool, so the guard fires when ANY
    `max-parallel` is present -- lower, equal to the old 2, or raised."""
    assert "max-parallel" not in _load(NIGHTLY_PATH)["jobs"]["dev-layout"]["strategy"]
    for value in (1, 2, 3, 6):
        broken = copy.deepcopy(_load(NIGHTLY_PATH))
        broken["jobs"]["dev-layout"]["strategy"]["max-parallel"] = value
        fixture = _dump_fixture(tmp_path, f"broken-max-parallel-{value}.yml", broken)
        problems = check_nightly_matrix(_load(fixture))
        assert any("max-parallel" in p for p in problems), (value, problems)


def test_nightly_matrix_guard_fires_when_the_labels_leave_the_pool(tmp_path):
    for labels in (
        ["self-hosted", "linux", "x64", "ralphd-vps", "docker-host"],
        ["self-hosted", "linux", "x64", "ci-pool", "int21h-ws-1"],
        ["self-hosted", "linux", "x64", "docker-host"],
    ):
        broken = copy.deepcopy(_load(NIGHTLY_PATH))
        broken["jobs"]["dev-layout"]["runs-on"] = labels
        fixture = _dump_fixture(tmp_path, "broken-labels.yml", broken)
        problems = check_nightly_matrix(_load(fixture))
        assert any("runs-on" in p for p in problems), (labels, problems)


def _mutate_cell_run(workflow: dict, old: str, new: str) -> dict:
    broken = copy.deepcopy(workflow)
    hit = False
    for step in broken["jobs"]["dev-layout"]["steps"]:
        if "docker run --rm" in str(step.get("run") or "") and "--cpus" in step["run"]:
            assert old in step["run"], old
            step["run"] = step["run"].replace(old, new)
            hit = True
    assert hit
    return broken


def test_nightly_matrix_guard_fires_when_a_pip_setting_or_the_cache_mount_is_wrong(
    tmp_path,
):
    workflow = _load(NIGHTLY_PATH)
    assert check_nightly_matrix(workflow) == []
    cases = [
        ("-e PIP_DEFAULT_TIMEOUT=60", "-e PIP_DEFAULT_TIMEOUT=15", "PIP_DEFAULT_TIMEOUT=60"),
        ("-e PIP_DEFAULT_TIMEOUT=60", "", "PIP_DEFAULT_TIMEOUT=60"),
        ("-e PIP_RETRIES=10", "-e PIP_RETRIES=5", "PIP_RETRIES=10"),
        ("-e PIP_RETRIES=10", "", "PIP_RETRIES=10"),
        ("-v ci-pip-cache:/pip-cache", "-v other-cache:/pip-cache", "ci-pip-cache volume"),
        ("-v ci-pip-cache:/pip-cache", "", "ci-pip-cache volume"),
        (
            "-e HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR=/pip-cache",
            "-e HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR=/tmp/elsewhere",
            "pip cache",
        ),
    ]
    for old, new, expect in cases:
        broken = _mutate_cell_run(workflow, old, new)
        fixture = _dump_fixture(tmp_path, "broken-pip.yml", broken)
        problems = check_nightly_matrix(_load(fixture))
        assert any(expect in p for p in problems), (old, new, problems)


def _pull_step_index(workflow: dict) -> int:
    steps = workflow["jobs"]["dev-layout"]["steps"]
    return next(i for i, st in enumerate(steps) if "docker pull" in str(st.get("run") or ""))


def test_nightly_matrix_guard_fires_when_the_pull_does_not_precede_the_container_run(tmp_path):
    workflow = _load(NIGHTLY_PATH)
    assert check_nightly_matrix(workflow) == []
    broken = copy.deepcopy(workflow)
    steps = broken["jobs"]["dev-layout"]["steps"]
    pull = steps.pop(_pull_step_index(broken))
    steps.append(pull)
    problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "pull-last.yml", broken)))
    assert any("precede" in p for p in problems), problems
    gone = copy.deepcopy(workflow)
    gone["jobs"]["dev-layout"]["steps"].pop(_pull_step_index(gone))
    problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "no-pull.yml", gone)))
    assert any("pulls the cell image" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_the_pull_retry_bound_is_wrong(tmp_path):
    workflow = _load(NIGHTLY_PATH)
    for old, new, expect in [
        ("for attempt in 1 2 3;", "for attempt in 1;", "retries 1 times"),
        ("for attempt in 1 2 3;", "for attempt in 1 2 3 4 5 6 7 8;", "retries 8 times"),
        ("for attempt in 1 2 3;", "while true;", "no bounded retry loop"),
        ("scripts/run_bounded.sh 300 ", "", "run_bounded"),
        ('docker pull "$CELL_IMAGE"', "docker pull ubuntu:24.04", "matrix.image"),
    ]:
        broken = copy.deepcopy(workflow)
        step = broken["jobs"]["dev-layout"]["steps"][_pull_step_index(broken)]
        assert old in step["run"], old
        step["run"] = step["run"].replace(old, new)
        problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "pull-bound.yml", broken)))
        assert any(expect in p for p in problems), (old, new, problems)


def test_nightly_workflow_keeps_its_cron_dispatch_and_non_cancelling_group(tmp_path):
    workflow = _load(NIGHTLY_PATH)
    assert check_nightly_matrix(workflow) == []
    assert workflow["concurrency"]["cancel-in-progress"] is False
    cases = []
    broken = copy.deepcopy(workflow)
    broken["concurrency"]["cancel-in-progress"] = True
    cases.append((broken, "cancel-in-progress"))
    broken = copy.deepcopy(workflow)
    del broken["concurrency"]
    cases.append((broken, "cancel-in-progress"))
    broken = copy.deepcopy(workflow)
    _on_block(broken)["schedule"] = [{"cron": "0 3 * * *"}]
    cases.append((broken, "cron"))
    broken = copy.deepcopy(workflow)
    del _on_block(broken)["workflow_dispatch"]
    cases.append((broken, "workflow_dispatch"))
    for broken, expect in cases:
        problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "trigger.yml", broken)))
        assert any(expect in p for p in problems), (expect, problems)


def test_nightly_matrix_guard_fires_when_fail_fast_is_enabled(tmp_path):
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["strategy"]["fail-fast"] = True
    fixture = _dump_fixture(tmp_path, "broken-fail-fast.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("fail-fast" in p for p in problems), problems


def test_nightly_matrix_guard_fires_when_the_container_lacks_docker_socket_access(tmp_path):
    """Task 012 (requirement I): dev-layout's own outer job now DOES declare a
    pinned `container:` -- superseding the invariant this node used to check
    (that `container:` was never used here at all), retired because the
    design it pinned is gone: I.1 explicitly asks both heavy's docker-tier
    and this job to run in a pinned container. What survives as a real
    hazard, and what this rewritten node proves instead: an outer container
    with no docker-socket access, and an outer container built from one of
    the axis's own three images (I.3's own words: "a second source of the
    cell's own packages")."""
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    broken["jobs"]["dev-layout"]["container"] = {
        "image": "python:3.12.3@sha256:3966b81808d864099f802080d897cef36c01550472ab3955fdd716d1c665acd6"
    }
    fixture = _dump_fixture(tmp_path, "broken-container-no-socket.yml", broken)
    problems = check_nightly_matrix(_load(fixture))
    assert any("docker.sock" in p for p in problems), problems

    broken_axis = copy.deepcopy(_load(NIGHTLY_PATH))
    broken_axis["jobs"]["dev-layout"]["container"] = {
        "image": "ubuntu:24.04",
        "options": "-v /var/run/docker.sock:/var/run/docker.sock",
    }
    fixture2 = _dump_fixture(tmp_path, "broken-container-axis-image.yml", broken_axis)
    problems2 = check_nightly_matrix(_load(fixture2))
    assert any("second source" in p for p in problems2), problems2


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
    $RALPHD_HOST_WORKSPACE itself."""
    broken = copy.deepcopy(_load(NIGHTLY_PATH))
    for step in broken["jobs"]["dev-layout"]["steps"]:
        if "run" in step and "docker run" in step["run"]:
            step["run"] = step["run"].replace(
                '"$RALPHD_HOST_WORKSPACE:/src-ro:ro"', '"/tmp/unrelated:/src-ro:ro"'
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


def test_ci_browser_runner_guard_fires_when_the_label_is_on_docker_job(tmp_path):
    broken = copy.deepcopy(_load(CI_PATH))
    broken["jobs"]["docker"]["runs-on"] = list(
        broken["jobs"]["docker"]["runs-on"]
    ) + ["browser-runner"]
    fixture = _dump_fixture(tmp_path, "broken-ci-docker-label.yml", broken)
    problems = check_ci_browser_runner(_load(fixture))
    assert any("docker" in p for p in problems), problems


def test_ci_browser_runner_guard_fires_when_missing_from_browser_job(tmp_path):
    broken = copy.deepcopy(_load(CI_PATH))
    runs_on = broken["jobs"]["browser"]["runs-on"]
    broken["jobs"]["browser"]["runs-on"] = [
        label for label in runs_on if label != "browser-runner"
    ]
    fixture = _dump_fixture(tmp_path, "broken-ci-missing-label.yml", broken)
    problems = check_ci_browser_runner(_load(fixture))
    assert any("browser" in p for p in problems), problems


def _replace_docker_step_run(broken: dict, transform) -> None:
    for step in broken["jobs"]["dev-layout"]["steps"]:
        if "run" in step and "docker run" in step["run"]:
            step["run"] = transform(step["run"])


def test_dev_layout_cell_prelude_matches_the_real_workflow():
    problems = _check_cell_hardening(
        [
            step.get("run", "")
            for step in _load(NIGHTLY_PATH)["jobs"]["dev-layout"]["steps"]
            if step.get("run")
        ]
    )
    assert problems == [], problems


def test_dev_layout_cell_hardening_guard_fires_on_each_mutation(tmp_path):
    """Task 010 (requirement G) merges what were three functions -- the
    git-install, uid-drop and docker-socket mutations of
    `_check_cell_hardening` -- into one node so the fast tier's collected
    count stays net-zero while this task adds its own two new
    allowlist-guard nodes below; all three original assertions survive
    unchanged, each against its own scratch fixture."""
    broken_no_git = copy.deepcopy(_load(NIGHTLY_PATH))
    _replace_docker_step_run(
        broken_no_git,
        lambda run: re.sub(r"apt-get install -y -qq git[^\n]*\n", "", run),
    )
    fixture = _dump_fixture(tmp_path, "broken-no-git.yml", broken_no_git)
    problems = check_nightly_matrix(_load(fixture))
    assert any("installs a git binary" in p for p in problems), problems

    broken_root = copy.deepcopy(_load(NIGHTLY_PATH))
    _replace_docker_step_run(
        broken_root,
        lambda run: re.sub(
            r"setpriv[^\n]*\\\n[^\n]*scripts/host_like_dev_layout\.sh",
            "scripts/host_like_dev_layout.sh",
            run,
        ),
    )
    fixture = _dump_fixture(tmp_path, "broken-runs-as-root.yml", broken_root)
    problems = check_nightly_matrix(_load(fixture))
    assert any("run as root" in p for p in problems), problems

    broken_docker = copy.deepcopy(_load(NIGHTLY_PATH))
    _replace_docker_step_run(
        broken_docker,
        lambda run: run.replace(
            "cp -a /src-ro /src",
            "cp -a /src-ro /src\n              apt-get install -y -qq docker.io >/dev/null",
        ),
    )
    fixture = _dump_fixture(tmp_path, "broken-docker-in-cell.yml", broken_docker)
    problems = check_nightly_matrix(_load(fixture))
    assert any("reachable daemon" in p for p in problems), problems


# --- task 010 (requirement G): every workflow job is containerized or ------
# --- allowlisted with a stated reason --------------------------------------

# Filenames must match exactly what `_load_all_workflows` (and the CI system
# itself) sees: the basename of each file under `.github/workflows/`.
# ci.yml's `fast` and `browser` jobs and nightly.yml's `dev-layout` need no
# entry: each declares a pinned `container:` (task 009, task 011 and task 012,
# requirement I). ci.yml's `docker` job is the one containerless tier whose
# reason is recorded below.
CONTAINER_ALLOWLIST: dict[tuple[str, str], str] = {
    ("browser-image.yml", "browser-image"): (
        "task 011 (requirement H): this job BUILDS the pinned image the "
        "browser tier runs in, over the runner's own docker socket. It cannot "
        "run in that image -- it is what produces it -- and a container: of "
        "its own would have to carry both a docker CLI and the glibc the "
        "runner's JS actions need, which no image this repository already "
        "pins supplies. browser-image.yml is triggered by workflow_dispatch "
        "only, so it runs on no push and no pull request."
    ),
    ("ci.yml", "trusted"): (
        "v0.35: the trust gate checks out nothing and runs one `echo` into "
        "$GITHUB_OUTPUT on the elastic runner itself; there is no code of the "
        "head under test for a container to isolate, and its job-level `if:` "
        "keeps a fork pull request from ever scheduling it."
    ),
    ("ci.yml", "docker"): (
        "v0.35: the docker tier keeps the host-run shape the retired heavy "
        "workflow gave it (task 012, requirement I, branch b -- the exit "
        "ramp): containerizing it was tried and reverted after live runs "
        "showed the tier's fixed-window tests (30s/60s hold windows racing an "
        "iteration's child spawn) losing wall-clock margin once the docker "
        "client lived one container layer deeper than its daemon. It builds "
        "real images and drives real sibling containers over the runner's "
        "own docker socket; the container changes nothing the tests assert, "
        "only the margin they depend on. nightly.yml's dev-layout "
        "containerization (the other half of requirement I) is unaffected."
    ),
    ("ci.yml", "required"): (
        "v0.35: the verdict job checks out nothing, reads no secret and uses no "
        "action: its one script compares the needed jobs' results. It runs on "
        "a GitHub-hosted runner so that a fork pull request schedules nothing "
        "on a self-hosted one, and there is no code of the head under test for "
        "a container to isolate."
    ),
    ("ci.yml", "publish"): (
        "v0.35: the Pages deployment runs three pinned actions over the report "
        "artifact: it checks out nothing and runs no script of the head under "
        "test, so there is no code for a container to isolate. It runs on the "
        "elastic ci-pool runner for the trusted `main` only; a fork pull "
        "request never schedules it."
    ),
    ("ci.yml", "lint"): (
        "v0.35: one ruff syntax/undefined-name check in a throwaway venv built "
        "from the runner image's own python3 on the elastic ci-pool runner. It "
        "needs no pinned interpreter patch level, no docker socket and no "
        "browser; asking for `docker-host` only to wrap a ruff run in a "
        "container would hold one of the pool's scarce socket-bearing slots."
    ),
}

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def check_container_or_allowlisted(
    workflows: dict[str, dict], allowlist: dict[tuple[str, str], str]
) -> list[str]:
    """Return human-readable problems: every job in every workflow in
    `workflows` (keyed by filename) must either declare a USABLE `container:`
    or carry a non-empty reason in `allowlist`, keyed by `(filename,
    job_name)`. An empty list means every job in `workflows` passes both
    checks.

    Review finding G (task cure-01-05): a `container:` key merely being
    PRESENT is not enough -- `container: null` (a job that declares the key
    but supplies nothing) and `container: {}` / `container: ""` (an empty
    declaration with no image to run in) are exactly as host-reachable as no
    key at all, so all three are treated as "no usable container" here.
    """
    problems: list[str] = []
    for filename, workflow in workflows.items():
        jobs = (workflow or {}).get("jobs") or {}
        for job_name, job in jobs.items():
            key = (filename, job_name)
            if key in allowlist:
                reason = allowlist[key]
                if not (reason or "").strip():
                    problems.append(
                        f"{filename}:{job_name} is allowlisted with no "
                        "stated reason"
                    )
                continue
            container = (job or {}).get("container")
            if not container:
                problems.append(
                    f"{filename}:{job_name} declares no usable container: "
                    "(missing, null or empty) and no allowlist entry"
                )
    return problems


def _load_all_workflows(workflows_dir: Path | None = None) -> dict[str, dict]:
    """Discover every workflow under `workflows_dir` (default
    `WORKFLOWS_DIR`), by BOTH extensions GitHub Actions itself accepts --
    `*.yml` and `*.yaml` (review finding G: the shipped helper globbed only
    `*.yml`, so a `.yaml` workflow -- a real host job -- was invisible to
    this guard entirely, discovery producing an empty dict rather than a
    rejection)."""
    directory = workflows_dir if workflows_dir is not None else WORKFLOWS_DIR
    paths = sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))
    return {path.name: _load(path) for path in sorted(paths, key=lambda p: p.name)}


def test_every_workflow_job_is_containerized_or_allowlisted_with_a_reason():
    problems = check_container_or_allowlisted(_load_all_workflows(), CONTAINER_ALLOWLIST)
    assert problems == [], problems


def test_container_allowlist_guard_fires_on_a_fixture_workflow(tmp_path):
    """A fixture workflow -- never an edit to a shipped one -- proves both
    refusals: a job with no usable container: and no allowlist entry, and an
    allowlist entry whose reason is absent from the data. Task cure-01-05
    (review finding G) folds two further refusals into this SAME node
    instead of adding new ones (the fast tier is at its collection ceiling
    with no headroom): a newly discovered `.yaml` workflow (the shipped
    helper globbed only `*.yml`) and `container: null` / `container: {}`
    (present but unusable), each proven through the PRODUCTION discovery
    helper (`_load_all_workflows`), never a hand-built dict."""
    fixture_workflow = {
        "name": "fixture",
        "on": {"workflow_dispatch": {}},
        "jobs": {
            "unguarded": {
                "runs-on": "ubuntu-latest",
                "steps": [{"run": "echo hi"}],
            },
        },
    }
    fixture = _dump_fixture(tmp_path, "fixture-container-allowlist.yml", fixture_workflow)
    loaded = _load(fixture)

    problems = check_container_or_allowlisted({fixture.name: loaded}, {})
    assert any("no usable container" in p for p in problems), problems

    problems = check_container_or_allowlisted(
        {fixture.name: loaded}, {(fixture.name, "unguarded"): "   "}
    )
    assert any("no stated reason" in p for p in problems), problems

    # Discovery: a `.yaml` workflow, dropped into its OWN directory (never
    # `.github/workflows/`), must be found and rejected by the real
    # production helper -- not a hand-built dict standing in for discovery.
    yaml_dir = tmp_path / "yaml-discovery"
    yaml_dir.mkdir()
    _dump_fixture(yaml_dir, "new.yaml", fixture_workflow)
    discovered = _load_all_workflows(yaml_dir)
    assert "new.yaml" in discovered, discovered
    problems = check_container_or_allowlisted(discovered, {})
    assert any("no usable container" in p for p in problems), problems

    # `container: null` and `container: {}` are present but unusable, and
    # must be refused exactly like a missing key -- not silently accepted
    # because the key exists.
    for unusable_container in (None, {}, ""):
        broken = copy.deepcopy(fixture_workflow)
        broken["jobs"]["unguarded"]["container"] = unusable_container
        broken_fixture = _dump_fixture(
            tmp_path, "fixture-unusable-container.yml", broken
        )
        problems = check_container_or_allowlisted(
            {broken_fixture.name: _load(broken_fixture)}, {}
        )
        assert any("no usable container" in p for p in problems), (
            unusable_container,
            problems,
        )


def _mutate_steps(workflow: dict, fn) -> dict:
    broken = copy.deepcopy(workflow)
    fn(broken["jobs"]["dev-layout"]["steps"])
    return broken


def _upload_step(steps: list[dict]) -> dict:
    return next(s for s in steps if str(s.get("uses") or "").startswith("actions/upload-artifact@"))


def test_nightly_workflow_captures_and_uploads_each_cells_results():
    workflow = _load(NIGHTLY_PATH)
    assert _check_result_capture(workflow) == []
    name = _upload_step(workflow["jobs"]["dev-layout"]["steps"])["with"]["name"]
    assert "matrix.slug" in name and "matrix.mode" in name and "github.run_id" in name


def test_result_capture_guard_fires_when_the_results_directory_is_not_handed_to_the_cell(tmp_path):
    workflow = _load(NIGHTLY_PATH)
    for old, expect in (
        ("-e HOST_LIKE_DEV_LAYOUT_ALLURE_DIR=/allure-results", "HOST_LIKE_DEV_LAYOUT_ALLURE_DIR"),
        ('-v "$RALPHD_HOST_WORKSPACE/allure-results-nightly:/allure-results"', "result capture mount"),
    ):
        broken = _mutate_cell_run(workflow, old, "")
        fixture = _dump_fixture(tmp_path, "broken-capture.yml", broken)
        problems = check_nightly_matrix(_load(fixture))
        assert any(expect in p for p in problems), (old, problems)
    broken = _mutate_cell_run(workflow, "mkdir -p allure-results-nightly", "true")
    problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "broken-mkdir.yml", broken)))
    assert any("never creates" in p for p in problems), problems


def test_result_capture_guard_fires_when_the_upload_step_is_missing_or_not_always(tmp_path):
    workflow = _load(NIGHTLY_PATH)

    broken = _mutate_steps(workflow, lambda steps: steps.remove(_upload_step(steps)))
    problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "no-upload.yml", broken)))
    assert any("no actions/upload-artifact" in p for p in problems), problems

    broken = _mutate_steps(workflow, lambda steps: _upload_step(steps).pop("if"))
    problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "no-always.yml", broken)))
    assert any("always()" in p for p in problems), problems

    broken = _mutate_steps(workflow, lambda steps: _upload_step(steps).update({"if": "success()"}))
    problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "success-only.yml", broken)))
    assert any("always()" in p for p in problems), problems


def test_result_capture_guard_fires_when_the_artifact_name_drops_a_part(tmp_path):
    workflow = _load(NIGHTLY_PATH)
    for part in ("matrix.slug", "matrix.mode", "github.run_id"):
        def drop(steps, part=part):
            with_ = _upload_step(steps)["with"]
            with_["name"] = with_["name"].replace("${{ " + part + " }}", "x")
        broken = _mutate_steps(workflow, drop)
        problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "name.yml", broken)))
        assert any(part in p for p in problems), (part, problems)


def test_result_capture_guard_fires_when_the_workflow_generates_a_report(tmp_path):
    workflow = _load(NIGHTLY_PATH)
    for extra in (
        {"name": "Report", "run": "scripts/allure_report.sh allure-results-nightly report"},
        {"name": "Report", "run": "allure generate allure-results-nightly"},
        {"name": "Pages", "uses": "actions/deploy-pages@" + "0" * 40},
    ):
        broken = _mutate_steps(workflow, lambda steps, extra=extra: steps.insert(-1, extra))
        problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "report.yml", broken)))
        assert any("report generation" in p for p in problems), (extra, problems)


def test_cell_curl_guard_fires_when_the_cell_stops_installing_curl(tmp_path):
    workflow = _load(NIGHTLY_PATH)
    broken = _mutate_cell_run(workflow, "install -y -qq git curl", "install -y -qq git")
    problems = check_nightly_matrix(_load(_dump_fixture(tmp_path, "no-curl.yml", broken)))
    assert any("install curl" in p for p in problems), problems
