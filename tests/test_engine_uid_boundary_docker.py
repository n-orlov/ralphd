"""Docker tier: task 007's proof that the three job-container-only tests in
`tests/test_engine_uid_boundary.py` cannot become a permanent skip (#57).

That module's `_missing_container_precondition()` makes
`test_the_agent_identity_resolves_by_name_first`,
`test_the_drop_keeps_root_real_and_saved_and_sets_groups_first` and
`test_an_unprivileged_engine_runs_and_records_that_it_has_no_boundary` skip,
with a named reason, on any host without a real `agent` account at uid/gid
1000 running the suite as that uid -- a bare developer laptop, most CI. A
skip condition with nothing ever exercising its "does not fire" branch is
indistinguishable from a permanent skip, so this file builds the *real*
`container/Dockerfile` (unmodified -- the only addition is the dev test
dependencies, see `Dockerfile.pytest-overlay`), runs the fast-lane module
inside it as uid 1000 exactly as the job container always does, and asserts
those three tests are collected and pass there -- not skipped.

Reuses `test_docker_sibling_e2e`'s docker-availability check (`pytestmark`)
and `REAL_DOCKER`/`RALPHD_RUN_ID` so there is one definition of "is a real
docker daemon reachable" and one run-id-derived tag namespace, per that
module's own cleanup-discipline note: every image tag here is unique to this
file and removed by its own exact tag at teardown, never touched by a label
query.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_docker_sibling_e2e import (  # noqa: F401  (fixtures/consts used by name)
    RALPHD_RUN_ID,
    REAL_DOCKER,
    pytestmark,
)

REPO = Path(__file__).parent.parent
OVERLAY = REPO / "tests" / "docker-hostpath-wrapper" / "Dockerfile.pytest-overlay"
BASE_TAG = f"ralphd:test-{RALPHD_RUN_ID}-uidboundary-base"
IMAGE_TAG = f"ralphd:test-{RALPHD_RUN_ID}-uidboundary-pytest"

GATED_TESTS = (
    "test_the_agent_identity_resolves_by_name_first",
    "test_the_drop_keeps_root_real_and_saved_and_sets_groups_first",
    "test_an_unprivileged_engine_runs_and_records_that_it_has_no_boundary",
)


@pytest.fixture(scope="module")
def pytest_image():
    """`container/Dockerfile`, unmodified, plus only the dev test deps."""
    res = subprocess.run(
        [REAL_DOCKER, "build", "-f", str(REPO / "container" / "Dockerfile"),
         "-t", BASE_TAG, "--label", f"ralphd.run={RALPHD_RUN_ID}", str(REPO)],
        capture_output=True, text=True, timeout=1800)
    assert res.returncode == 0, (res.stdout[-4000:] + res.stderr[-4000:])
    res = subprocess.run(
        [REAL_DOCKER, "build", "-f", str(OVERLAY),
         "--build-arg", f"BASE_IMAGE={BASE_TAG}",
         "-t", IMAGE_TAG, "--label", f"ralphd.run={RALPHD_RUN_ID}",
         str(REPO / "tests")],
        capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, (res.stdout[-4000:] + res.stderr[-4000:])
    yield IMAGE_TAG
    subprocess.run([REAL_DOCKER, "rmi", "-f", IMAGE_TAG], capture_output=True)
    subprocess.run([REAL_DOCKER, "rmi", "-f", BASE_TAG], capture_output=True)


def test_the_gated_uid_boundary_tests_are_collected_and_passing_in_a_job_container(pytest_image):
    """`container/entrypoint.sh` (unmodified, per the module docstring) ends
    in a bare `exec ralphd-engine`, which ignores any command handed to
    `docker run` after the image name -- without `--entrypoint python3`
    below, the command line here would silently start the *engine* instead
    of pytest (it would exit 2 for want of a PRD, never having run a
    single test), which is precisely how this test was found to have never
    actually executed anywhere: #57 reopening, diagnosed against this
    project's own image with `docker run --rm --user 1000:1000 <image>
    /bin/echo HELLO` (prints ralphd's "no PRD" error, exit 2) versus the
    same with `--entrypoint /bin/echo` (prints HELLO, exit 0)."""
    selector = " or ".join(GATED_TESTS)
    res = subprocess.run(
        [REAL_DOCKER, "run", "--rm", "--user", "1000:1000",
         "--entrypoint", "python3", pytest_image,
         "-m", "pytest", "-q", "-k", selector,
         "/opt/ralphd/tests/test_engine_uid_boundary.py"],
        capture_output=True, text=True, timeout=180)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert f"{len(GATED_TESTS)} passed" in out, out
    assert "skipped" not in out, out
    assert "deselected" in out, out  # proves the -k selector actually filtered
