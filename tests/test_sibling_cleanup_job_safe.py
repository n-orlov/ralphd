"""Task 037 (#7): the *documented* cleanup idiom cannot delete the job container.

tests/test_sibling_cleanup_guidance.py proves the sibling-only cleanup rule is
*present* in the prompt and the docs (text assertions). This module proves the
rule actually *works* against a real docker daemon: it builds a labeled fleet
(a job container carrying `ralphd.run=<rid>` + `ralphd.role=job`, two siblings
carrying `ralphd.run=<rid>` + `ralphd.role=sibling`, plus a sibling of an
unrelated run), then executes the exact cleanup command lifted verbatim from
the two places an agent copies it from -- the rendered prompt
(`LoopSupervisor._docker_siblings_note()`) and
`examples/skills/toolchain-sibling/SKILL.md` -- and asserts the siblings are
removed while the job container is still *running*. The command text is never
retyped here: it is extracted from the source of truth, so a future edit that
drops the `role=sibling` filter fails this test instead of a real run.

The counterpart is asserted too: host-side `ralphctl stop`/`rm` still reap
everything with the run label alone (job container included) -- that asymmetry
is deliberate (issue #7) and must not be "fixed" by narrowing the reap query.

Marked ``@pytest.mark.docker``; the whole module skips cleanly when the docker
socket is unreachable (or no small local image is available to run, so the
tier never depends on a registry pull).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from ralphd.engine.loop import LoopSupervisor

REPO = Path(__file__).parent.parent
SKILL_MD = REPO / "examples" / "skills" / "toolchain-sibling" / "SKILL.md"
RALPHCTL = Path(sys.executable).parent / "ralphctl"
DOCKER_SOCK = "/var/run/docker.sock"
REAL_DOCKER = shutil.which("docker")

# Only images already present locally are used: this tier must not need a pull.
CANDIDATE_IMAGES = ["busybox:stable", "busybox:latest", "busybox:1.36",
                    "alpine:latest", "alpine:3", "python:3.12-slim-bookworm"]


def _docker_available() -> bool:
    if not REAL_DOCKER or not Path(DOCKER_SOCK).is_socket():
        return False
    return subprocess.run([REAL_DOCKER, "version"],
                          capture_output=True, timeout=10).returncode == 0


def _local_image() -> str | None:
    for image in CANDIDATE_IMAGES:
        if subprocess.run([REAL_DOCKER, "image", "inspect", image],
                          capture_output=True, timeout=30).returncode == 0:
            return image
    return None


pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(not _docker_available(),
                                 reason="docker socket not available")]


def d(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([REAL_DOCKER, *argv], capture_output=True,
                          text=True, timeout=120)


def _exists(name: str) -> bool:
    return d("inspect", name).returncode == 0


def _running(name: str) -> bool:
    res = d("inspect", "-f", "{{.State.Running}}", name)
    return res.returncode == 0 and res.stdout.strip() == "true"


@pytest.fixture(scope="module")
def image():
    img = _local_image()
    if not img:
        pytest.skip("no small image available locally (refusing to pull)")
    return img


class Fleet:
    """A labeled container fleet for one synthetic run, plus a bystander.

    The run id is freshly generated so it can never collide with this very
    job's own `ralphd.run=$RALPHD_RUN_ID` label, and teardown removes only
    the exact names created here -- never a label query (which, run from
    inside a job container, is the whole hazard under test).
    """

    def __init__(self, image: str):
        self.run_id = f"t037-{uuid.uuid4().hex[:8]}"
        assert self.run_id != os.environ.get("RALPHD_RUN_ID")
        self.other_run_id = f"t037-other-{uuid.uuid4().hex[:8]}"
        self.image = image
        self.job = f"ralphd-{self.run_id}"          # main.job_container_name()
        self.siblings = [f"{self.run_id}-sib1", f"{self.run_id}-sib2"]
        self.bystander = f"{self.other_run_id}-sib"
        self.names = [self.job, *self.siblings, self.bystander]

    def start(self) -> None:
        self._up(self.job, self.run_id, "job")
        for name in self.siblings:
            self._up(name, self.run_id, "sibling")
        self._up(self.bystander, self.other_run_id, "sibling")

    def _up(self, name: str, run_id: str, role: str) -> None:
        res = d("run", "-d", "--name", name,
                "--label", f"ralphd.run={run_id}",
                "--label", f"ralphd.role={role}",
                self.image, "sleep", "600")
        assert res.returncode == 0, res.stderr
        assert _running(name), f"{name} did not start"

    def add_sibling(self, suffix: str) -> str:
        name = f"{self.run_id}-{suffix}"
        self._up(name, self.run_id, "sibling")
        self.names.append(name)
        return name

    def teardown(self) -> None:
        for name in self.names:
            d("rm", "-f", name)


@pytest.fixture
def fleet(image):
    f = Fleet(image)
    try:
        f.start()
        yield f
    finally:
        f.teardown()


# ---------------------------------------------------------------------------
# The cleanup command, lifted verbatim from the two places agents copy it from
# ---------------------------------------------------------------------------

def _rendered_note(run_id: str) -> str:
    env = {"RALPHD_HOST_WORKSPACE": "/host/ws", "RALPHD_HOST_RUN_DIR": "/host/run",
           "RALPHD_RUN_ID": run_id, "RALPHD_SELF_CONTAINER_ID": f"ralphd-{run_id}"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return LoopSupervisor._docker_siblings_note()
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _prompt_cleanup_command(run_id: str) -> str:
    """The sanctioned `remove:` one-liner out of the `## Docker siblings` note.

    The note also quotes the *forbidden* form (as the thing not to run), so the
    sanctioned one is addressed by its `remove:` bullet rather than by being
    the only match.
    """
    note = _rendered_note(run_id)
    cmds = re.findall(r"remove: `(docker rm -f \$\(docker ps [^`]*\))`", note)
    assert len(cmds) == 1, f"expected exactly one removal one-liner, got {cmds}"
    return cmds[0]


def _prompt_forbidden_command(run_id: str) -> str:
    """The run-label-only form the note explicitly forbids."""
    note = _rendered_note(run_id)
    cmds = [c for c in re.findall(r"`(docker rm -f \$\(docker ps [^`]*\))`", note)
            if "ralphd.role" not in c]
    assert len(cmds) == 1, f"expected one forbidden form, got {cmds}"
    return cmds[0]


def _skill_cleanup_command() -> str:
    """The fenced bash block from SKILL.md rule 5, executed as written."""
    blocks = re.findall(r"```bash\n(.*?)```", SKILL_MD.read_text(), re.DOTALL)
    matching = [b for b in blocks if "remove siblings only" in b]
    assert len(matching) == 1, f"expected one cleanup block, got {len(matching)}"
    return matching[0]


@pytest.fixture(params=["prompt", "skill"])
def documented_cleanup(request):
    """Callable(run_id) -> the exact documented cleanup shell snippet."""
    if request.param == "prompt":
        return _prompt_cleanup_command
    return lambda run_id: _skill_cleanup_command()


def test_documented_cleanup_removes_siblings_and_spares_the_job(
        fleet, documented_cleanup):
    snippet = documented_cleanup(fleet.run_id)
    # Both sources filter on the run label *and* role=sibling -- that pairing
    # is what makes the command safe to run from inside the job container.
    assert "label=ralphd.run=$RALPHD_RUN_ID" in snippet
    assert "label=ralphd.role=sibling" in snippet

    res = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True,
                         timeout=180,
                         env={**os.environ, "RALPHD_RUN_ID": fleet.run_id,
                              "PATH": os.environ["PATH"]})
    assert res.returncode == 0, res.stdout + res.stderr

    for name in fleet.siblings:
        assert not _exists(name), f"sibling {name} survived the cleanup"
    # The whole point: the container the agent runs in is untouched and still
    # running -- not merely present, so a `stop` would fail this too.
    assert _running(fleet.job), "the documented cleanup killed the job container"
    assert _running(fleet.bystander), "cleanup reached another run's sibling"


def test_the_forbidden_form_really_does_delete_the_job_container(fleet):
    """The prohibition is not a strawman: the run-label-only one-liner the
    prompt forbids destroys the job container on the same fleet the sanctioned
    form leaves running. Safe here only because the fixture's run id is freshly
    generated and can never be this job's own label (asserted again below)."""
    assert fleet.run_id != os.environ.get("RALPHD_RUN_ID")
    snippet = _prompt_forbidden_command(fleet.run_id)
    assert "ralphd.role" not in snippet

    res = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True,
                         timeout=180,
                         env={**os.environ, "RALPHD_RUN_ID": fleet.run_id})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not _exists(fleet.job), "the forbidden form spared the job container"
    assert _running(fleet.bystander), "cleanup reached another run's sibling"


def test_ralphctl_stop_and_rm_still_reap_everything(fleet, tmp_path):
    """Host-side reaping is deliberately run-label-only: `stop`/`rm` take the
    job container down with the siblings. Unchanged behaviour (task 034)."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / fleet.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"state": "succeeded"}))
    env = {k: v for k, v in os.environ.items() if k != "RALPHD_DOCKER"}
    env["RALPHD_REGISTRY"] = str(registry)

    res = subprocess.run([str(RALPHCTL), "stop", fleet.run_id], env=env,
                         capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, res.stdout + res.stderr
    assert not _exists(fleet.job), "stop left the job container behind"
    for name in fleet.siblings:
        assert not _exists(name), f"stop left sibling {name} behind"
    assert _running(fleet.bystander), "stop reached another run's sibling"

    # `rm` reaps siblings created after the container went away, and the dir.
    late = fleet.add_sibling("late")
    res = subprocess.run([str(RALPHCTL), "rm", fleet.run_id, "--yes"], env=env,
                         capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, res.stdout + res.stderr
    assert not _exists(late), "rm left a labeled sibling behind"
    assert not run_dir.exists()
    assert _running(fleet.bystander), "rm reached another run's sibling"
