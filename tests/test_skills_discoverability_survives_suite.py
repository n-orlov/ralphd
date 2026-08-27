"""Task 004 (#83): regression-test skill discoverability the way a job
container actually experiences it -- a skill placed when the engine starts
must still be there, and still discoverable by the *agent's* own discovery
rule (not just `place_skills()`'s own bookkeeping), after a real slice of
this repo's own test suite has run in that same container/HOME.

`_discoverable_skill_names()` below is deliberately independent of
`ralphd.engine.skills` -- it re-implements, directly against the
filesystem, the exact rule `docs/skills.md` documents for
`~/.pi/agent/skills/`: "directories containing SKILL.md are discovered
recursively". That keeps this test honest about what the *agent* actually
sees, rather than re-asserting `ralphd.engine.skills.verify_placement`'s
own opinion of itself.

Scope, honestly stated (see `artifacts/reports/skills-placement-rootcause.md`
for the full record): this proves discoverability survives for the
representative subset of the e2e/live suite that task 001 identified as
*already* isolating its own `$HOME` (via `stub_env={"HOME": ...}` or
`monkeypatch.setenv`) -- i.e. the class of module #83's fix (task 002's
explicit-`home=` contract) actually protects. It deliberately does *not*
cover the still-open residual gap task 001 also identified and reproduced
(`tests/test_skills_placement_ambient_home_loss.py`): an e2e/live module
that spawns a child engine *without* overriding `HOME` in `stub_env`
inherits whatever `$HOME` the outer pytest process has, and that child's
ordinary, correct-for-itself startup call still wipes it. Fixing that
class (defaulting `EngineProc`/`LiveRun`'s env construction to an isolated
`HOME` unless a test opts into ambient inheritance) is a harness-level
change with dozens of call sites and would flip the very assertion task
001's own reproducer makes -- out of scope for this task; #83 stays open
because of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from test_e2e import EngineProc
from tests.conftest import with_ralphd_importable

REPO = Path(__file__).parent.parent

# The concrete subset task 001's rootcause report named as the modules that
# were part of the container's own live-recurrence trigger set *and* that
# isolate their own HOME (via `stub_env`/`monkeypatch.setenv`) rather than
# inheriting whatever HOME the outer process happens to have -- the class
# #83's task 002 fix (place_skills' explicit `home=` contract) protects.
SAFE_SUITE_SUBSET = [
    "tests/test_skills_placement_detection.py",
    "tests/test_skills_api.py",
    "tests/test_skills_placement_requires_home.py",
    "tests/test_cli_skills_crud.py",
]


def _discoverable_skill_names(home: Path) -> set[str]:
    """The agent's own discovery rule (docs/skills.md): under
    `~/.pi/agent/skills/`, a directory containing `SKILL.md` is a
    discoverable skill. Independent of ralphd.engine.skills on purpose --
    this is what the *agent* would see, not what the engine's own
    bookkeeping claims."""
    root = home / ".pi" / "agent" / "skills"
    if not root.is_dir():
        return set()
    return {p.name for p in root.iterdir()
            if p.is_dir() and (p / "SKILL.md").is_file()}


def _run_pytest_subset(home: Path, modules: list[str]) -> subprocess.CompletedProcess:
    # `with_ralphd_importable` for the same reason EngineProc uses it (#67):
    # overriding HOME moves *user* site-packages, so on a `pip install --user
    # -e .` layout the child pytest cannot import `ralphd` at all and every
    # module in the subset fails to collect -- a fixture failure that this
    # test's own assertion would then report as "a real regression". A
    # container with a system-wide install never sees it, which is why the
    # in-container sweeps this test shipped with were green.
    env = with_ralphd_importable({**os.environ, "HOME": str(home)})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *modules],
        cwd=str(REPO), capture_output=True, text=True, env=env, timeout=180,
    )


def test_discoverability_check_is_not_vacuous_without_a_placement(tmp_path):
    """Sanity clause the task's success criteria asks for directly: if
    nothing was ever placed, the check must not spuriously report a
    skill -- otherwise the "still discoverable after the suite ran"
    assertion below would pass even with the placement step removed."""
    empty_home = tmp_path / "no-skills-here"
    empty_home.mkdir()
    assert _discoverable_skill_names(empty_home) == set()


def test_skill_placed_at_engine_startup_is_still_discoverable_after_a_real_suite_subset_runs_in_the_same_home(
    tmp_path,
):
    # Stand-in for the job container's own real $HOME -- never the actual
    # ambient $HOME of the process running this test.
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()

    # -- 1. Engine startup places a real skill, using the exact production
    # call shape (explicit `home=`, matching #83 task 002's hardened
    # contract): a mounted skill under the engine's own config_dir, placed
    # by `main.py:186`'s ordinary, unconditional startup call.
    engine_root = tmp_path / "engine"
    mounted_skill = engine_root / "config" / "skills" / "manual-skill"
    mounted_skill.mkdir(parents=True)
    (mounted_skill / "SKILL.md").write_text("# manual-skill\n\nStands in for a "
                                            "human-placed/mounted skill.\n")

    e = EngineProc(engine_root,
                   {"run_id": "job-container-repro", "iterations": 1,
                    "max_approaches": 1, "on_complete": "idle"},
                   stub_env={"HOME": str(ambient_home)})
    try:
        e.wait_api(timeout=30)
        # -- 2. Discoverable right after startup, via the agent's own rule.
        assert _discoverable_skill_names(ambient_home) == {"manual-skill"}, (
            "the skill placed at this simulated engine's startup must be "
            "discoverable under the container's HOME before the suite runs"
        )
    finally:
        e.stop()

    # -- 3. Run a real slice of this repo's own suite in the SAME HOME
    # (task 001's identified safe subset -- see module docstring).
    res = _run_pytest_subset(ambient_home, SAFE_SUITE_SUBSET)
    assert res.returncode == 0, (
        f"suite subset failed (this is a real regression, not a fixture "
        f"issue):\n{res.stdout}\n{res.stderr}"
    )

    # -- 4. Still discoverable afterwards -- this is the actual regression
    # guard: if this subset regressed and started inheriting `ambient_home`
    # without overriding it, `place_skills()` would wipe it and this
    # assertion would fail.
    assert _discoverable_skill_names(ambient_home) == {"manual-skill"}, (
        "the skill placed at engine startup must still be discoverable "
        "after this repo's own test suite subset ran in the same HOME"
    )
