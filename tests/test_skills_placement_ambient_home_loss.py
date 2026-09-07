"""Regression test for #83 (same family as #67): a real child
`ralphd-engine` process must NOT wipe the *ambient* `$HOME`'s
`~/.pi/agent/skills/` just because the caller (the test harness spawning
that child) didn't explicitly override HOME.

History, traced from the operator's originally-observed facts (this module
started life as the reproducer for the bug, before task 001/002 fixed it):

- `place_skills()` (engine/skills.py) rebuilds its target directory from
  scratch on every call: every existing symlink/file/dir directly under the
  target is removed first, then repopulated from `_sources(config_dir)`
  (mounted `<config_dir>/skills/*` + api overlay `<OVERLAY_DIR>/skills/*|`).
  That clear-then-fill mechanism is correct and by design *for the engine's
  own config_dir/overlay* -- it is meant to be safe to call on every startup
  and after every skill CRUD call, so a deleted/replaced skill's stale
  symlink never lingers.
- The target it rebuilds is `_pi_skills_dir(home)` = `<home>/.pi/agent/skills`.
  `place_skills()` used to default a missing `home` to `_home()`, which reads
  the real `$HOME` from the process environment -- not a per-run sandboxed
  HOME -- whenever no override reached it.
- `engine/main.py`'s `amain()` (the same PID-1 entrypoint the container
  runs) calls `place_skills(CONFIG_DIR, ...)` on every single engine startup,
  unconditionally, before the PRD is even read; `engine/api.py`'s PUT/DELETE
  `/config/skills/{name}` handlers do the same on every skill CRUD call.
- The engine's own long-lived process (this container's PID 1 for a real
  ralphd job) has `CONFIG_DIR` == the run's real config dir and its ambient
  `$HOME` == the container's real home, so its own startup call was always
  self-consistent there.
- But this repo's own black-box tests (`tests/conftest.py`'s `LiveRun`, the
  shared launcher behind `engine_factory` and `live`) spawn a **second, child** `ralphd-engine`
  process to test the engine itself, with a throwaway tmp `config_dir` that
  (almost always) has no `skills/` subdirectory at all. Before task 001, a
  test module that forgot to pass a HOME override in `stub_env` spawned a
  child that inherited the *real* ambient `$HOME` of whatever process was
  running pytest -- and that child's first startup action rebuilt
  `<real $HOME>/.pi/agent/skills/` from its own (empty) sources, wiping any
  skill a human or another process had placed there. That was the operator's
  observed `~/.pi/agent/skills/` going empty after "this run's suite ran".

Task 001 closed the root cause two ways, both exercised by this test:
`_resolved_home_env()` (conftest.py) now defaults every spawned child's HOME
to a per-instance tmp directory unless the caller opts in via the single
`AMBIENT_HOME` sentinel, AND `place_skills()` itself now requires `home=`
as a keyword-only argument with no ambient fallback, plus a `config_dir`
check against the engine's own `CONFIG_DIR` -- so even a caller that *did*
reach the real ambient HOME by some other path can no longer clobber it
with a placement built from the wrong source tree.

This module used to be the reproducer that demonstrated the loss (asserting
the pre-placed skill symlink was gone and the directory emptied); it is now
inverted into a regression test proving the opposite holds by default: the
old assertion claimed a child engine spawned without an explicit HOME
override in `stub_env` wipes the ambient HOME's placed skill. The new
assertion is strictly stronger, because it demands survival under exactly
the same construction (`LiveRun` with no HOME in `stub_env` -- the common,
non-opt-in case across the e2e/live suite) instead of merely tolerating loss:
the placed skill under the stand-in ambient HOME must still exist, unchanged,
and `~/.pi/agent/skills/` must still list exactly that one entry, after a
default (non-opt-in) child engine spawn and shutdown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# xdist_group (task 003, G.3): an AMBIENT_HOME-family module -- keeps this
# module's tests on one worker under a loaded parallel (`-n 8`) run rather
# than racing another AMBIENT_HOME/nested-pytest module over the real
# ambient $HOME on a different worker.
pytestmark = pytest.mark.xdist_group(name="ambient_home")

from tests.conftest import LiveRun


def test_child_engine_without_home_override_preserves_ambient_home_skills(
    tmp_path: Path, monkeypatch
) -> None:
    # Stand-in for "the real $HOME" a human had placed a skill under.
    ambient_home = tmp_path / "ambient-home"
    ambient_skills = ambient_home / ".pi" / "agent" / "skills"
    ambient_skills.mkdir(parents=True)

    skill_source = tmp_path / "skill-source" / "manual-skill"
    skill_source.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# stub skill\n")

    manual_link = ambient_skills / "manual-skill"
    manual_link.symlink_to(skill_source, target_is_directory=True)
    assert manual_link.is_symlink() and manual_link.exists(), (
        "sanity: the skill must actually be visible under the ambient HOME "
        "before the child engine runs"
    )

    # This process's own ambient $HOME, so a regression that reintroduces
    # the ambient fallback would be reading the stand-in above, same as the
    # real container ambient env would present it.
    monkeypatch.setenv("HOME", str(ambient_home))

    # Same construction as tests/conftest.py's `engine_factory` fixture,
    # deliberately with NO stub_env HOME override -- this is the common
    # case across the e2e/live suite, not a contrived one. Since task 001,
    # `_resolved_home_env()` gives this child its own isolated tmp HOME
    # (`tmp_path / "engine" / "home"`) instead of inheriting the ambient one
    # set above.
    e = LiveRun(
        tmp_path / "engine",
        {"run_id": "skills-loss-repro", "iterations": 1,
         "max_approaches": 1, "on_complete": "idle"},
    )
    try:
        e.wait_api(timeout=30)
    finally:
        e.stop()

    # The stand-in ambient HOME's manually-placed skill must survive a
    # default (non-opt-in) child engine spawn untouched.
    assert manual_link.is_symlink() and manual_link.exists(), (
        "expected the ambient $HOME's ~/.pi/agent/skills/manual-skill "
        "symlink to survive a child engine spawned without a HOME override "
        "in stub_env (#83 fixed by task 001/002: place_skills() no longer "
        "defaults to the ambient HOME, and the child's own HOME is isolated "
        "by default)"
    )
    assert [p.name for p in ambient_skills.iterdir()] == ["manual-skill"], (
        "expected ~/.pi/agent/skills/ under the stand-in ambient HOME to "
        "still list exactly the one manually-placed entry -- not emptied, "
        "not replaced, not added to"
    )
    assert manual_link.resolve() == skill_source.resolve(), (
        "expected the surviving symlink to still point at its original "
        "source directory"
    )
