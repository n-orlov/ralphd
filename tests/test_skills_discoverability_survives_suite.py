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
for the full record): this proves discoverability survives for two
representative subsets of the e2e/live suite --

1. `SAFE_SUITE_SUBSET`: modules task 001 identified as *already* isolating
   their own `$HOME` (via `stub_env={"HOME": ...}` or
   `monkeypatch.setenv`) -- the class module #83's task 002 fix
   (`place_skills()`'s explicit-`home=` contract) protects directly.
2. `PREVIOUSLY_AMBIENT_SUBSET`: two modules (`test_iteration_env_strip.py`,
   `test_stagnation_signature.py`) drawn from the exact 46-module
   ambient-HOME-inheriting set task 001's own rootcause report named --
   modules that construct `LiveRun`/`LiveRun` and name no `"HOME"` of
   their own, so before task 001 they inherited whatever `$HOME` the outer
   pytest process had and their child engine's ordinary startup wiped it.
   Task 001 (`tests/conftest.py`'s `_resolved_home_env`) now defaults every
   such child's `HOME` to an isolated per-instance directory instead of the
   ambient one, unless a test opts in with `stub_env={"HOME": AMBIENT_HOME}`
   -- neither module here does, so both now run their child engine against
   an isolated `HOME` that is not `ambient_home` at all, and `place_skills`'
   clear-then-fill has nothing of `ambient_home`'s to touch.

Together these two subsets hold the *whole* class task 001's rootcause
report identified: every e2e/live module that spawns a child engine either
already isolated its own `HOME` (subset 1) or now gets an isolated `HOME`
by default (subset 2, task 001's fix). The residual gap this docstring
used to describe here -- an e2e/live module inheriting ambient `HOME` and
its child's startup wiping a real skill placed there -- no longer exists
for either subset; `tests/test_skills_placement_ambient_home_loss.py` (task
002) is the unit-level regression test for exactly this mechanism, and this
module is the suite-level demonstration that it holds for real pytest
subsets, not just a single isolated call.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# xdist_group (task 003, G.3): the nested-pytest module -- it shells out to a
# real pytest subprocess over module subsets also collected by the outer
# xdist run, and shares the AMBIENT_HOME family's group so it never runs
# concurrently with another AMBIENT_HOME module on a different worker under
# a loaded parallel (`-n 8`) run.
pytestmark = pytest.mark.xdist_group(name="ambient_home")

from tests.conftest import LiveRun, with_ralphd_importable

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

# Two modules drawn from the exact 46-module ambient-HOME-inheriting set
# named in `artifacts/reports/task-001-home-isolation.md` (the command
# there -- `grep -rln "LiveRun\|LiveRun\|engine_factory\|live_run"
# tests/*.py` minus any file already naming `"HOME"` -- lists exactly 46
# files, both of these among them): they construct `LiveRun`/`LiveRun`
# and name no `"HOME"` of their own, so before task 001 they inherited
# whatever `$HOME` the outer pytest process had. Task 001's
# `_resolved_home_env` now defaults their child engine's `HOME` to an
# isolated per-instance directory instead, so running them with the outer
# process's own `HOME` pointed at `ambient_home` (below) no longer lets
# their child engine's startup touch `ambient_home` at all.
PREVIOUSLY_AMBIENT_SUBSET = [
    "tests/test_iteration_env_strip.py",
    "tests/test_stagnation_signature.py",
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
    # `with_ralphd_importable` for the same reason LiveRun uses it (#67):
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


def test_skill_placed_in_operator_home_survives_a_real_subset_from_task_002s_attributed_list(tmp_path):
    """Task 004 (#83): task 002's wipewatch attribution named ten fast-tier
    node ids that call `amain()` *in-process* (no child spawn) against
    whatever `HOME` the pytest PROCESS itself has. Task 003 closed that
    door with an autouse fixture,
    `tests/conftest.py::_isolate_ambient_home_in_process`, that isolates
    every test's own `HOME` to a throwaway directory by default. This test
    proves that fix holds for a real pytest subprocess run drawn straight
    from task 002's own attributed list, not just a single isolated call:
    place a skill under an operator-like `$HOME`, run a subset --
    `tests/test_engine_budget_seed.py` and
    `tests/test_status_max_approaches.py`, both named in task 002's list and
    both calling `amain()` directly with no `HOME` override of their own --
    inside a pytest subprocess whose own environment names that same
    `$HOME` (via `_run_pytest_subset`), then re-checks the skill is still
    discoverable by the agent's own rule afterward.

    Reverting task 003's isolation with this exact one-line mutation to
    `tests/conftest.py::_isolate_ambient_home_in_process` --

        -    monkeypatch.setenv("HOME", str(isolated))
        +    return

    -- makes this node fail: with the fixture reduced to a no-op, the
    subprocess's own tests inherit the outer subprocess's `HOME` (the
    operator-like home this test set up), `amain()` reads it via
    `ambient_home()`, and `place_skills()`'s clear-then-fill wipes the
    planted skill directory. See
    `artifacts/reports/task-009-before-after-mutation-evidence.md`'s
    section D for the recorded failing node id and both pass/fail counts.
    """
    operator_home = tmp_path / "operator-home"
    skill_dir = operator_home / ".pi" / "agent" / "skills" / "operator-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# operator-skill\n\nPlaced by the operator, not by place_skills().\n")

    assert _discoverable_skill_names(operator_home) == {"operator-skill"}, (
        "sanity: the skill just planted must be discoverable before the "
        "suite subset runs at all"
    )

    subset = [
        "tests/test_engine_budget_seed.py",
        "tests/test_status_max_approaches.py",
    ]
    res = _run_pytest_subset(operator_home, subset)
    assert res.returncode == 0, (
        f"suite subset failed (this is a real regression, not a fixture "
        f"issue):\n{res.stdout}\n{res.stderr}"
    )

    assert _discoverable_skill_names(operator_home) == {"operator-skill"}, (
        "the skill placed in the operator-like $HOME before this subset "
        "ran must still be discoverable afterward -- if this fails, task "
        "003's in-process HOME isolation has regressed"
    )


def test_discoverability_check_is_not_vacuous_without_a_placement(tmp_path):
    """Sanity clause the task's success criteria asks for directly: if
    nothing was ever placed, the check must not spuriously report a
    skill -- otherwise the "still discoverable after the suite ran"
    assertion below would pass even with the placement step removed."""
    empty_home = tmp_path / "no-skills-here"
    empty_home.mkdir()
    assert _discoverable_skill_names(empty_home) == set()


@pytest.mark.nightly  # task 048 (B.5 follow-on): spawns a real nested
# pytest subprocess over SAFE_SUITE_SUBSET (task 001) -- inherently several
# real seconds by design; moved rather than thinning the subset or its
# assertions, which would weaken the very proof this node exists to give.
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

    e = LiveRun(engine_root,
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


def test_skill_placed_at_engine_startup_is_still_discoverable_after_previously_ambient_modules_run_in_the_same_home(
    tmp_path,
):
    """Same shape as the test above, but the suite subset (step 3) is drawn
    from `PREVIOUSLY_AMBIENT_SUBSET` -- modules that, before task 001, spawn
    a child engine with no `stub_env` HOME override at all and so inherited
    whatever `HOME` the outer pytest process had. Here the *outer* pytest
    subprocess itself is given `HOME=ambient_home` (via `_run_pytest_subset`)
    to reproduce exactly that inheritance path; task 001's default isolation
    (`tests/conftest.py::_resolved_home_env`) means each module's own child
    engine no longer picks up `ambient_home` regardless, so `place_skills`'
    clear-then-fill has nothing of `ambient_home`'s left to touch."""
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()

    engine_root = tmp_path / "engine"
    mounted_skill = engine_root / "config" / "skills" / "manual-skill"
    mounted_skill.mkdir(parents=True)
    (mounted_skill / "SKILL.md").write_text("# manual-skill\n\nStands in for a "
                                            "human-placed/mounted skill.\n")

    e = LiveRun(engine_root,
                   {"run_id": "job-container-repro-2", "iterations": 1,
                    "max_approaches": 1, "on_complete": "idle"},
                   stub_env={"HOME": str(ambient_home)})
    try:
        e.wait_api(timeout=30)
        assert _discoverable_skill_names(ambient_home) == {"manual-skill"}, (
            "the skill placed at this simulated engine's startup must be "
            "discoverable under the container's HOME before the suite runs"
        )
    finally:
        e.stop()

    res = _run_pytest_subset(ambient_home, PREVIOUSLY_AMBIENT_SUBSET)
    assert res.returncode == 0, (
        f"suite subset failed (this is a real regression, not a fixture "
        f"issue):\n{res.stdout}\n{res.stderr}"
    )

    assert _discoverable_skill_names(ambient_home) == {"manual-skill"}, (
        "the skill placed at engine startup must still be discoverable "
        "after this previously-ambient-inheriting suite subset ran with "
        "the outer process's own HOME pointed at the same directory"
    )
