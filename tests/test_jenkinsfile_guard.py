"""Guard for the `Jenkinsfile` (ralphd/ci on the homelab Jenkins, docs/ci.md):
the tiers' commands and bounds, the nightly lane, the reports and badges, the
Telegram notification and the README badge block.

The Jenkinsfile is small and declarative, so it is read as text: stages are
found by `stage('<name>') {` and cut at their matching brace, comment lines
are dropped before matching. The per-tier commands live in
`ci/jenkins-tier.sh` (one `case` arm per tier) and the dev-layout cell in
`ci/jenkins-devlayout.sh`; both are pinned here too.

Every check returns a list of problems. Each one is run against the real
files (no problems) and against mutated fixtures: a copy edited back to a
broken shape, written under `tmp_path`, which the same check must reject.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JENKINSFILE = REPO / "Jenkinsfile"
TIER_SCRIPT = REPO / "ci" / "jenkins-tier.sh"
LANE_SCRIPT = REPO / "ci" / "jenkins-lane.sh"
DEVLAYOUT_SCRIPT = REPO / "ci" / "jenkins-devlayout.sh"
IMAGE_SCRIPT = REPO / "ci" / "jenkins-image.sh"
README = REPO / "README.md"

PY_IMAGE = (
    "python:3.12.3@sha256:"
    "3966b81808d864099f802080d897cef36c01550472ab3955fdd716d1c665acd6"
)

STAGES = ["Prepare", "Tiers", "Lint", "Fast", "Docker", "Browser", "Dev-layout cells"]

# stage -> (lane, stage timeout minutes, image, docker run options)
LANES = {
    "Lint": ("lint", 15, '"$PY_IMAGE"', "--cpus=4 --cpuset-cpus=4-11"),
    "Fast": ("fast", 45, '"$PY_IMAGE"',
             "--cpus=4 --cpuset-cpus=0-3 --cpu-shares=4096 --memory=12g --pids-limit=4096"),
    "Docker": ("docker", 120, '"$DOCKER_TIER_IMAGE"',
               "--cpus=4 --cpuset-cpus=4-11 --memory=12g --pids-limit=4096 "
               "-v /var/run/docker.sock:/var/run/docker.sock"),
    "Browser": ("browser", 60, '"$BROWSER_IMAGE"',
                "--cpus=4 --cpuset-cpus=4-11 --memory=16g --pids-limit=4096 "
                "--shm-size=1g"),
}

# tier -> the commands its case arm must run, verbatim
TIER_COMMANDS = {
    "lint": [
        'scripts/run_bounded.sh 300 "lint venv build" -- bash -euo pipefail -c',
        '.venv/bin/python -m pip install --quiet "ruff>=0.4"',
        'scripts/run_bounded.sh 300 "ruff check" -- .venv/bin/ruff check . --select E9,F63,F7,F821',
    ],
    "fast": [
        'scripts/run_bounded.sh 300 "venv build" -- bash -euo pipefail -c',
        "python3.12 -m venv .venv",
        'scripts/run_bounded.sh 1800 "fast tier" -- python -m pytest -q -m fast -n 4 '
        "--dist loadfile --durations=15 --alluredir=allure-results-fast --junitxml=junit-fast.xml",
    ],
    "docker": [
        'scripts/run_bounded.sh 600 "venv build" -- bash -euo pipefail -c',
        'scripts/run_bounded.sh 6000 "docker tier" -- .venv/bin/python -m pytest -q -m docker '
        "--durations=15 --alluredir=allure-results-docker --junitxml=junit-docker.xml",
        'docker image rm -f "ralphd:test-${RALPHD_RUN_ID}" "ralphd:test-${RALPHD_RUN_ID}-base"',
    ],
    "browser": [
        'scripts/run_bounded.sh 600 "venv build" -- bash -euo pipefail -c',
        """printf %s '{"browser":{"launchOptions":{"args":["--no-sandbox"]}}}' > .playwright/cli.config.json""",
        "playwright-cli --version",
        "google-chrome --version",
        'scripts/run_bounded.sh 2400 "browser tier" -- .venv/bin/python -m pytest -q -m browser '
        "--durations=15 --alluredir=allure-results-browser --junitxml=junit-browser.xml",
    ],
}
# The inner bounds each tier has always had; a tier may never raise them.
TIER_BOUNDS = {"lint": [300, 300], "fast": [300, 1800],
               "docker": [600, 6000], "browser": [600, 2400]}
# What a lane keeps back from its stage timeout for the venv-free steps.
LANE_RESERVE_S = 300

NIGHTLY_CRON = "cron(env.BRANCH_NAME == 'main' ? '23 2 * * *' : '')"
CELL_IMAGES = ["ubuntu:24.04", "python:3.11-slim", "python:3.14-slim"]
CELL_MODES = ["exported", "unset"]
CELL_COMMANDS = [
    'scripts/run_bounded.sh" 6300 "dev-layout cell" -- docker run --rm --cpus="2"',
    "-e HOST_LIKE_DEV_LAYOUT_ALLURE_DIR=/allure-results",
    "-e PIP_DEFAULT_TIMEOUT=60",
    "-e PIP_RETRIES=10",
    "-e HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR=/pip-cache",
    "-v ci-pip-cache:/pip-cache",
    '-e HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE="$CELL_MODE"',
    '-v "$WS:/src-ro:ro"',
    "cp -a /src-ro /src",
    'setpriv --reuid=1000 --regid="$CELL_GID" --init-groups env HOME="$CELL_HOME" '
    "scripts/host_like_dev_layout.sh",
]

BADGE_IDS = ["tests", "lint"]
BADGE_BASE = "https://int21h.mooo.com/jenkins-static/ralphd/ci/main/"
BUILD_LINK = "https://jenkins.wisent-owl.ts.net/job/ralphd/job/ci/job/main/lastCompletedBuild/"
README_BADGES = [
    f"[![build]({BADGE_BASE}build.svg)]({BUILD_LINK})",
    f"[![tests]({BADGE_BASE}tests.svg)]({BUILD_LINK}Allure/)",
    f"[![lint]({BADGE_BASE}lint.svg)]({BUILD_LINK}CI_20summary/)",
    "[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)",
]

STAGE_RE = re.compile(r"stage\('([^']+)'\)\s*\{")
FORBIDDEN_IN_PIPELINE = ("ghcr.io", "docker push", "docker login", "GITHUB_",
                         "sweep_sha_guard", "gh-pages")


# --- reading the Jenkinsfile ---------------------------------------------------

def _code(text: str) -> str:
    """`text` without its // comment lines."""
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))


def _block(text: str, start: int) -> str:
    """The body of the brace block opening at or after `start`."""
    depth = 0
    for i in range(text.index("{", start), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[text.index("{", start) + 1:i]
    return text[start:]


def _stages(jf: str) -> list[str]:
    return STAGE_RE.findall(_code(jf))


def _stage(jf: str, name: str) -> str:
    code = _code(jf)
    for m in STAGE_RE.finditer(code):
        if m.group(1) == name:
            return _block(code, m.start())
    return ""


def _post(jf: str, section: str) -> str:
    code = _code(jf)
    m = re.search(r"\n  post \{", code)
    if not m:
        return ""
    post = _block(code, m.start())
    s = re.search(r"\n    " + section + r" \{", post)
    return _block(post, s.start()) if s else ""


def _tier_arm(script: str, tier: str) -> str:
    m = re.search(r"\n  " + re.escape(tier) + r"\)\n(.*?)\n    ;;", script, re.S)
    return m.group(1) if m else ""


# --- checks --------------------------------------------------------------------

def check_stages(jenkinsfile: Path) -> list[str]:
    jf = jenkinsfile.read_text()
    problems = []
    if _stages(jf) != STAGES:
        problems.append(f"stages are {_stages(jf)}, want {STAGES}")
    tiers = _stage(jf, "Tiers")
    if "parallel {" not in tiers:
        problems.append("the Tiers stage does not run its lanes in parallel")
    for lane in LANES:
        if f"stage('{lane}')" not in tiers:
            problems.append(f"lane {lane} is not inside the parallel Tiers stage")
    prepare = _stage(jf, "Prepare")
    for want in ("sh 'ci/jenkins-reap.sh'", "sh 'git clean -ffdxq'",
                 "ci/jenkins-image.sh browser", "ci/jenkins-image.sh docker-tier"):
        if want not in prepare:
            problems.append(f"the Prepare stage lacks {want!r}")
    if prepare.find("ci/jenkins-reap.sh") > prepare.find("git clean"):
        problems.append("the workspace is cleaned before it is handed back to the agent")
    for bad in FORBIDDEN_IN_PIPELINE:
        if bad in _code(jf):
            problems.append(f"the Jenkinsfile mentions {bad!r}")
    if f"PY_IMAGE = '{PY_IMAGE}'" not in jf:
        problems.append("PY_IMAGE is not the pinned python:3.12.3 digest")
    return problems


def check_lanes(jenkinsfile: Path) -> list[str]:
    jf = jenkinsfile.read_text()
    problems = []
    for name, (lane, minutes, image, opts) in LANES.items():
        body = _stage(jf, name)
        if not body:
            problems.append(f"no {name} stage")
            continue
        if f"timeout(time: {minutes}, unit: 'MINUTES')" not in body:
            problems.append(f"{name}: the stage timeout is not {minutes} minutes")
        if "catchError(buildResult: 'FAILURE', stageResult: 'FAILURE')" not in body:
            problems.append(f"{name}: a failed lane no longer fails the build")
        if body.find("timeout(") > body.find("catchError("):
            problems.append(f"{name}: the timeout must wrap catchError, so it fails the lane alone")
        want = f"sh 'ci/jenkins-lane.sh {lane} {minutes} {image} {opts} -- ci/jenkins-tier.sh {lane}'"
        if want not in body:
            problems.append(f"{name}: does not run {want}")
    return problems


def check_tier_commands(tier_script: Path) -> list[str]:
    script = tier_script.read_text()
    problems = []
    for tier, commands in TIER_COMMANDS.items():
        arm = _tier_arm(script, tier)
        if not arm:
            problems.append(f"ci/jenkins-tier.sh has no {tier} arm")
            continue
        for command in commands:
            if command not in arm:
                problems.append(f"{tier}: missing {command!r}")
        bounds = [int(b) for b in re.findall(r"scripts/run_bounded\.sh (\d+)", arm)]
        if bounds != TIER_BOUNDS[tier]:
            problems.append(f"{tier}: run_bounded bounds are {bounds}, want {TIER_BOUNDS[tier]}")
        if tier != "lint" and "trap " not in arm.split("run_bounded")[0]:
            problems.append(f"{tier}: allure_env.sh is not set to run on every exit")
    return problems


def check_bounds_fit_the_lanes(jenkinsfile: Path, tier_script: Path) -> list[str]:
    problems = []
    script = tier_script.read_text()
    jf = jenkinsfile.read_text()
    for name, (lane, minutes, _, _) in LANES.items():
        total = sum(int(b) for b in re.findall(r"scripts/run_bounded\.sh (\d+)",
                                               _tier_arm(script, lane)))
        m = re.search(r"timeout\(time: (\d+), unit: 'MINUTES'\)", _stage(jf, name))
        stage_s = int(m.group(1)) * 60 if m else 0
        if total + LANE_RESERVE_S > stage_s:
            problems.append(f"{name}: inner bounds {total}s + {LANE_RESERVE_S}s reserve "
                            f"exceed the {stage_s}s stage timeout")
    return problems


def check_nightly(jenkinsfile: Path, devlayout_script: Path) -> list[str]:
    jf = jenkinsfile.read_text()
    cell = devlayout_script.read_text()
    problems = []
    if NIGHTLY_CRON not in jf:
        problems.append("main is not built nightly at '23 2 * * *'")
    if "booleanParam(name: 'EXTENDED', defaultValue: false" not in jf:
        problems.append("the EXTENDED parameter is missing or not off by default")
    if ("params.EXTENDED ||" not in jf
            or "getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause')" not in jf):
        problems.append("the nightly lane is not chosen from EXTENDED and the timer cause")
    stage = _stage(jf, "Dev-layout cells")
    if "when { environment name: 'CI_NIGHTLY', value: 'true' }" not in stage:
        problems.append("the dev-layout cells do not run on nightly builds only")
    if f"for (mode in {CELL_MODES!r})".replace('"', "'") not in stage:
        problems.append(f"the cells are not one wave per mode {CELL_MODES}")
    images = "[" + ", ".join(f"'{i}'" for i in CELL_IMAGES) + "]"
    if f"for (image in {images})" not in stage:
        problems.append(f"the cell images are not {CELL_IMAGES}")
    for want in ("timeout(time: 120, unit: 'MINUTES')", "parallel cells",
                 "CELL_MINUTES=120 ci/jenkins-devlayout.sh",
                 "catchError(buildResult: 'FAILURE', stageResult: 'FAILURE')"):
        if want not in stage:
            problems.append(f"the dev-layout stage lacks {want!r}")
    for want in CELL_COMMANDS:
        if want not in cell:
            problems.append(f"ci/jenkins-devlayout.sh lacks {want!r}")
    return problems


def check_reports(jenkinsfile: Path) -> list[str]:
    always = _post(jenkinsfile.read_text(), "always")
    problems = []
    for want in (
        "sh 'ci/jenkins-report.sh previous/allure-history'",
        "filter: 'allure-history/**'",
        "junit testResults: 'junit/*.xml'",
        "reportName: 'Allure', reportDir: 'allure-report'",
        "reportName: 'CI summary', reportDir: 'ci-summary'",
        "readFile('ci-summary/counts.txt')",
        "archiveArtifacts",
    ):
        if want not in always:
            problems.append(f"post {{ always }} lacks {want!r}")
    if "copyArtifactPermission('/ralphd/ci/*')" not in jenkinsfile.read_text():
        problems.append("the next build may not copy this build's allure-history")
    return problems


def check_badges(jenkinsfile: Path) -> list[str]:
    jf = _code(jenkinsfile.read_text())
    declared = re.findall(r"addEmbeddableBadgeConfiguration\(id: '([^']+)'", jf)
    problems = []
    if declared != BADGE_IDS:
        problems.append(f"badges declared: {declared}, want {BADGE_IDS}")
    for badge in declared:
        if (f"addEmbeddableBadgeConfiguration(id: '{badge}', subject: '{badge}', "
                "status: 'not run', color: 'lightgrey')") not in jf:
            problems.append(f"badge {badge} does not start as 'not run' on every build")
    if "always {" not in jf or jf.find("addEmbeddableBadgeConfiguration") < jf.find("always {"):
        problems.append("badges are not declared in post { always }")
    return problems


def check_notify_last(jenkinsfile: Path) -> list[str]:
    jf = _code(jenkinsfile.read_text())
    problems = []
    if jf.count("notifyTelegram(") != 1:
        problems.append("notifyTelegram must be called exactly once")
    if not jf.startswith("@Library('homelab') _") and "\n@Library('homelab') _\n" not in jf:
        problems.append("the homelab shared library is not loaded")
    m = re.search(r"\n  post \{", jf)
    post = _block(jf, m.start()) if m else ""
    sections = re.findall(r"\n    (\w+) \{", post)
    if not sections or sections[-1] != "cleanup":
        problems.append(f"cleanup is not the last post section: {sections}")
    cleanup = _post(jenkinsfile.read_text(), "cleanup")
    calls = [l.strip() for l in cleanup.splitlines()
             if l.strip() and l.strip() not in ("script {", "}")]
    first = next((i for i, c in enumerate(calls) if c.startswith("notifyTelegram(")), -1)
    if first < 0:
        problems.append("post { cleanup } does not notify Telegram")
    elif any(not (c.startswith("def ") or c.startswith("extra:")) for c in calls[:first] + calls[first + 1:]):
        problems.append(f"notifyTelegram is not the last thing in post {{ cleanup }}: {calls}")
    return problems


def check_readme_badges(readme: Path, jenkinsfile: Path) -> list[str]:
    text = readme.read_text()
    head = text[: text.find("# ralphd")]
    lines = [l for l in head.splitlines() if l.strip()]
    problems = []
    if lines != README_BADGES:
        problems.append(f"README badge block is {lines}, want {README_BADGES}")
    declared = set(re.findall(r"addEmbeddableBadgeConfiguration\(id: '([^']+)'",
                              _code(jenkinsfile.read_text())))
    shown = set(re.findall(re.escape(BADGE_BASE) + r"([a-z0-9-]+)\.svg", head))
    if shown != declared | {"build"}:
        problems.append(f"README shows badges {sorted(shown)}, the Jenkinsfile declares "
                        f"{sorted(declared)} plus build")
    for link in re.findall(r"\]\((https://jenkins[^)]*)\)", head):
        if "/lastCompletedBuild/" not in link:
            problems.append(f"badge link {link} is not per build")
    return problems


def check_images(image_script: Path) -> list[str]:
    script = image_script.read_text()
    problems = []
    if 'git ls-files -s -- "$dir" | sha256sum' not in script:
        problems.append("the lane image tag is not a hash of ci/<name>/")
    if 'tag="ralphd-$name-ci:$hash"' not in script:
        problems.append("the lane image is not tagged ralphd-<name>-ci:<hash>")
    for bad in ("docker push", "docker login", "ghcr.io"):
        if bad in script:
            problems.append(f"ci/jenkins-image.sh mentions {bad!r}")
    return problems


CHECKOUT_CLONES = {
    "ci/jenkins-lane.sh": 'git clone --quiet --local --no-hardlinks --no-checkout "$ws" "$root/src"',
    "ci/jenkins-devlayout.sh": 'git clone --quiet --local --no-hardlinks --no-checkout "$ws" "$src"',
}


def check_checkouts(lane_script: Path, devlayout_script: Path) -> list[str]:
    """A lane's checkout never shares an inode with the workspace's .git: the
    lane chowns its tree to uid 1000, so a hardlinked pack would change owner
    in the workspace too and the next clone could not link it."""
    problems = []
    for script, (name, want) in zip((lane_script, devlayout_script), CHECKOUT_CLONES.items()):
        text = script.read_text()
        if want not in text:
            problems.append(f"{name} lacks {want!r}")
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            if "git clone" in line and "--no-hardlinks" not in line:
                problems.append(f"{name} clones with hardlinks: {line.strip()!r}")
    return problems


# --- the real files --------------------------------------------------------------

def test_the_stages_run_in_order_with_the_tiers_in_parallel():
    assert check_stages(JENKINSFILE) == []


def test_each_lane_runs_its_tier_with_its_bound_image_and_limits():
    assert check_lanes(JENKINSFILE) == []


def test_each_tier_runs_its_commands_with_its_inner_bounds():
    assert check_tier_commands(TIER_SCRIPT) == []


def test_the_inner_bounds_fit_inside_each_lane_timeout():
    assert check_bounds_fit_the_lanes(JENKINSFILE, TIER_SCRIPT) == []


def test_main_runs_the_nightly_lane_by_cron_or_extended():
    assert check_nightly(JENKINSFILE, DEVLAYOUT_SCRIPT) == []


def test_the_reports_are_built_and_published_after_every_build():
    assert check_reports(JENKINSFILE) == []


def test_the_badges_are_declared_on_every_build():
    assert check_badges(JENKINSFILE) == []


def test_telegram_is_notified_last_in_cleanup():
    assert check_notify_last(JENKINSFILE) == []


def test_the_readme_badge_block_matches_the_jenkinsfile():
    assert check_readme_badges(README, JENKINSFILE) == []


def test_lane_images_are_built_on_the_agent_by_content_hash():
    assert check_images(IMAGE_SCRIPT) == []


def test_lane_checkouts_are_copied_not_hardlinked():
    assert check_checkouts(LANE_SCRIPT, DEVLAYOUT_SCRIPT) == []


@pytest.mark.parametrize("which,old,new", [
    ("lane", "--local --no-hardlinks --no-checkout", "--local --no-checkout"),
    ("devlayout", "--local --no-hardlinks --no-checkout", "--local --no-checkout"),
    ("lane", 'git -C "$root/src" checkout', 'git clone --local "$ws" "$root/extra"\ngit -C "$root/src" checkout'),
])
def test_the_checkout_check_rejects_a_hardlinked_clone(tmp_path, which, old, new):
    lane, devlayout = LANE_SCRIPT, DEVLAYOUT_SCRIPT
    if which == "lane":
        lane = _mutate(tmp_path, LANE_SCRIPT, which, old, new)
    else:
        devlayout = _mutate(tmp_path, DEVLAYOUT_SCRIPT, which, old, new)
    assert check_checkouts(lane, devlayout), (which, old)


def test_no_github_actions_workflow_is_left():
    workflows = REPO / ".github" / "workflows"
    assert not workflows.exists() or not any(workflows.iterdir())


def test_a_lane_runs_as_uid_1000_at_its_own_host_path():
    lane = LANE_SCRIPT.read_text()
    assert '-v "$root:$root" -w "$root/src"' in lane
    assert '-e RALPHD_JOB_STARTED_AT="$(date +%s)" -e RALPHD_JOB_BOUND_MINUTES="$minutes"' in lane
    assert '-e RALPHD_TIMEOUT_EVIDENCE_DIR="$root/evidence"' in lane
    assert '--label "ralphd.ci=${BUILD_TAG:-local}"' in lane
    assert '-e CI_UID=1000 -e CI_GID=1000' in lane
    entry = (REPO / "ci" / "jenkins-lane-entry.sh").read_text()
    assert 'exec setpriv --reuid="$uid" --regid="$gid"' in entry


# --- mutated fixtures ------------------------------------------------------------

def _mutate(tmp_path: Path, source: Path, name: str, old: str, new: str) -> Path:
    text = source.read_text()
    assert old in text, f"{name}: fixture anchor {old!r} not found in {source.name}"
    out = tmp_path / f"{name}-{source.name}"
    out.write_text(text.replace(old, new, 1))
    return out


@pytest.mark.parametrize("name,old,new", [
    ("lanes-in-series", "      parallel {\n", "      stages {\n"),
    ("browser-renamed", "stage('Browser')", "stage('Chrome')"),
    ("no-reap", "        sh 'ci/jenkins-reap.sh'\n        sh 'git clean -ffdxq'",
     "        sh 'git clean -ffdxq'"),
    ("no-browser-image", "ci/jenkins-image.sh browser", "ci/build-browser.sh"),
    ("ghcr-pull", "env.BROWSER_IMAGE = sh(", "sh 'docker pull ghcr.io/n-orlov/x'\n          env.BROWSER_IMAGE = sh("),
    ("unpinned-python", PY_IMAGE, "python:3.12"),
])
def test_the_stage_check_rejects_a_broken_pipeline(tmp_path, name, old, new):
    assert check_stages(_mutate(tmp_path, JENKINSFILE, name, old, new)), name


@pytest.mark.parametrize("name,old,new", [
    ("fast-timeout-raised", "timeout(time: 45, unit: 'MINUTES')", "timeout(time: 90, unit: 'MINUTES')"),
    ("fast-uncapped", "fast 45 \"$PY_IMAGE\" --cpus=4 ", "fast 45 \"$PY_IMAGE\" "),
    ("fast-no-cpu-priority", " --cpu-shares=4096", ""),
    ("fast-shares-its-cpus", "--cpuset-cpus=0-3", "--cpuset-cpus=4-11"),
    ("browser-on-the-fast-cpus", "--cpus=4 --cpuset-cpus=4-11 --memory=16g", "--cpus=4 --memory=16g"),
    ("docker-no-socket", " -v /var/run/docker.sock:/var/run/docker.sock", ""),
    ("browser-wrong-image", 'browser 60 "$BROWSER_IMAGE"', 'browser 60 "$PY_IMAGE"'),
    ("lint-bound-drift", "ci/jenkins-lane.sh lint 15", "ci/jenkins-lane.sh lint 30"),
    ("docker-no-catch", "catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {\n"
     "                sh 'ci/jenkins-lane.sh docker", "{\n                sh 'ci/jenkins-lane.sh docker"),
])
def test_the_lane_check_rejects_a_broken_lane(tmp_path, name, old, new):
    assert check_lanes(_mutate(tmp_path, JENKINSFILE, name, old, new)), name


@pytest.mark.parametrize("name,old,new", [
    ("fast-serial", "-m fast -n 4 --dist loadfile", "-m fast"),
    ("fast-bound-raised", 'run_bounded.sh 1800 "fast tier"', 'run_bounded.sh 2400 "fast tier"'),
    ("docker-bound-raised", 'run_bounded.sh 6000 "docker tier"', 'run_bounded.sh 6600 "docker tier"'),
    ("browser-unwrapped", 'scripts/run_bounded.sh 2400 "browser tier" -- .venv', ".venv"),
    ("lint-wider-selection", "--select E9,F63,F7,F821", "--select E"),
    ("no-sandbox-dropped", '"args":["--no-sandbox"]', '"args":[]'),
    ("no-allure-on-exit", "    trap describe EXIT\n    scripts/run_bounded.sh 300", "    scripts/run_bounded.sh 300"),
    ("docker-images-kept", 'docker image rm -f "ralphd:test-${RALPHD_RUN_ID}"', "true"),
])
def test_the_tier_check_rejects_a_broken_tier(tmp_path, name, old, new):
    assert check_tier_commands(_mutate(tmp_path, TIER_SCRIPT, name, old, new)), name


def test_the_bounds_check_rejects_bounds_that_outgrow_the_lane(tmp_path):
    tier = _mutate(tmp_path, TIER_SCRIPT, "docker-7000",
                   'run_bounded.sh 6000 "docker tier"', 'run_bounded.sh 7000 "docker tier"')
    assert check_bounds_fit_the_lanes(JENKINSFILE, tier)
    jf = _mutate(tmp_path, JENKINSFILE, "browser-50",
                 "timeout(time: 60, unit: 'MINUTES')", "timeout(time: 50, unit: 'MINUTES')")
    assert check_bounds_fit_the_lanes(jf, TIER_SCRIPT)


@pytest.mark.parametrize("name,old,new", [
    ("cron-on-every-branch", NIGHTLY_CRON, "cron('23 2 * * *')"),
    ("cron-moved", "'23 2 * * *'", "'0 2 * * *'"),
    ("extended-on", "booleanParam(name: 'EXTENDED', defaultValue: false",
     "booleanParam(name: 'EXTENDED', defaultValue: true"),
    ("timer-ignored", "params.EXTENDED || !currentBuild", "params.EXTENDED && !currentBuild"),
    ("cells-every-build", "      when { environment name: 'CI_NIGHTLY', value: 'true' }\n", ""),
    ("cell-dropped", "'python:3.11-slim', ", ""),
    ("one-mode", "['exported', 'unset']", "['exported']"),
    ("cell-timeout-raised", "timeout(time: 120, unit: 'MINUTES') {\n                  catchError",
     "timeout(time: 240, unit: 'MINUTES') {\n                  catchError"),
])
def test_the_nightly_check_rejects_a_broken_jenkinsfile(tmp_path, name, old, new):
    assert check_nightly(_mutate(tmp_path, JENKINSFILE, name, old, new), DEVLAYOUT_SCRIPT), name


@pytest.mark.parametrize("name,old,new", [
    ("no-pip-timeout", "  -e PIP_DEFAULT_TIMEOUT=60 \\\n", ""),
    ("no-pip-retries", "-e PIP_RETRIES=10", "-e PIP_RETRIES=1"),
    ("no-pip-cache", "  -v ci-pip-cache:/pip-cache \\\n", ""),
    ("cell-uncapped", '--cpus="2"', '--cpus="4"'),
    ("cell-as-root", "setpriv --reuid=1000", "env --reuid=1000"),
    ("writable-source", '-v "$WS:/src-ro:ro"', '-v "$WS:/src-ro"'),
])
def test_the_nightly_check_rejects_a_broken_cell(tmp_path, name, old, new):
    assert check_nightly(JENKINSFILE, _mutate(tmp_path, DEVLAYOUT_SCRIPT, name, old, new)), name


@pytest.mark.parametrize("name,old,new", [
    ("no-report", "sh 'ci/jenkins-report.sh previous/allure-history'", "echo 'no report'"),
    ("no-history", "filter: 'allure-history/**'", "filter: 'nothing/**'"),
    ("no-allure", "reportName: 'Allure', reportDir: 'allure-report'", "reportName: 'X', reportDir: 'allure-report'"),
    ("no-summary", "reportName: 'CI summary'", "reportName: 'Summary'"),
    ("no-copy-permission", "copyArtifactPermission('/ralphd/ci/*')", "copyArtifactPermission('/other/*')"),
])
def test_the_report_check_rejects_a_missing_report(tmp_path, name, old, new):
    assert check_reports(_mutate(tmp_path, JENKINSFILE, name, old, new)), name


@pytest.mark.parametrize("name,old,new", [
    ("lint-badge-dropped",
     "        def lintBadge = addEmbeddableBadgeConfiguration(id: 'lint', subject: 'lint', status: 'not run', color: 'lightgrey')\n",
     "        def lintBadge = null\n"),
    ("tests-badge-stale", "id: 'tests', subject: 'tests', status: 'not run'", "id: 'tests', subject: 'tests', status: 'unknown'"),
    ("extra-badge", "        def lintBadge =",
     "        addEmbeddableBadgeConfiguration(id: 'coverage', subject: 'coverage', status: 'not run', color: 'lightgrey')\n        def lintBadge ="),
])
def test_the_badge_check_rejects_a_broken_badge(tmp_path, name, old, new):
    assert check_badges(_mutate(tmp_path, JENKINSFILE, name, old, new)), name


@pytest.mark.parametrize("name,old,new", [
    ("notify-not-last", "                       extra: env.CI_NIGHTLY == 'true' ? 'nightly (dev-layout cells)' : null)\n",
     "                       extra: env.CI_NIGHTLY == 'true' ? 'nightly (dev-layout cells)' : null)\n        sh 'ci/jenkins-reap.sh'\n"),
    ("notify-in-always", "    always {\n", "    always {\n      notifyTelegram(notifyOn: 'ALL')\n"),
    ("cleanup-not-last", "    cleanup {", "    failure {\n      echo 'red'\n    }\n    cleanup {"),
    ("no-library", "@Library('homelab') _", ""),
])
def test_the_notify_check_rejects_a_misplaced_notification(tmp_path, name, old, new):
    jf = _mutate(tmp_path, JENKINSFILE, name, old, new)
    if name == "cleanup-not-last":
        # failure {} before cleanup {} is fine; moving cleanup first is not.
        jf.write_text(jf.read_text().replace("    failure {\n      echo 'red'\n    }\n    cleanup {", "    cleanup {", 1))
        text = jf.read_text()
        cleanup = text[text.index("    cleanup {"):text.rindex("\n  }\n}")]
        jf.write_text(text.replace(cleanup, cleanup + "\n    failure {\n      echo 'red'\n    }", 1))
    assert check_notify_last(jf), name


@pytest.mark.parametrize("name,old,new", [
    ("no-lint-badge", README_BADGES[2] + "\n", ""),
    ("tests-to-last-build", f"{BUILD_LINK}Allure/", BUILD_LINK.replace("lastCompletedBuild/", "Allure/")),
    ("build-to-job", f"build.svg)]({BUILD_LINK})", "build.svg)](https://jenkins.wisent-owl.ts.net/job/ralphd/)"),
    ("no-license", README_BADGES[3] + "\n", ""),
    ("other-branch", "ralphd/ci/main/tests.svg", "ralphd/ci/jenkins-ci/tests.svg"),
])
def test_the_readme_check_rejects_a_broken_badge_block(tmp_path, name, old, new):
    assert check_readme_badges(_mutate(tmp_path, README, name, old, new), JENKINSFILE), name


@pytest.mark.parametrize("name,old,new", [
    ("fixed-tag", 'tag="ralphd-$name-ci:$hash"', 'tag="ralphd-$name-ci:latest"'),
    ("pushed", 'echo "$tag"\n', 'docker push "$tag"\necho "$tag"\n'),
    ("unhashed", 'git ls-files -s -- "$dir" | sha256sum', "date | sha256sum"),
])
def test_the_image_check_rejects_a_registry_or_unhashed_image(tmp_path, name, old, new):
    assert check_images(_mutate(tmp_path, IMAGE_SCRIPT, name, old, new)), name
