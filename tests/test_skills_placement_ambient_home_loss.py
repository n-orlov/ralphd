"""Regression reproducer for #83 (same family as #67): a real child
`ralphd-engine` process wipes the *ambient* `$HOME`'s `~/.pi/agent/skills/`
the moment it starts, whenever the caller does not explicitly override HOME.

Root cause, traced from the operator's established facts:

- `place_skills()` (engine/skills.py:150-175) unconditionally rebuilds its
  target directory from scratch: every existing symlink/file/dir directly
  under the target is removed first (:161-166), then repopulated from
  `_sources(config_dir)` (mounted `<config_dir>/skills/*` + api overlay
  `<OVERLAY_DIR>/skills/*`). That is correct and by design *for the engine's
  own config_dir/overlay* -- it is meant to be safe to call on every
  startup and after every skill CRUD call.
- The target it rebuilds is `_pi_skills_dir(home)` = `<home>/.pi/agent/skills`
  (:99-100), and when `home` is not passed, `_home()` (:78-79) reads the
  real `$HOME` from the process environment -- not a per-run sandboxed HOME.
- `engine/main.py:186` (`amain()`, the same PID-1 entrypoint the container
  runs) calls `place_skills(CONFIG_DIR)` with no `home` argument, on every
  single engine startup, unconditionally, before the PRD is even read.
  `engine/api.py:497` and `:511` (PUT/DELETE `/config/skills/{name}`) do the
  same.
- The engine's own long-lived process (this container's PID 1 for a real
  ralphd job) has `CONFIG_DIR` == the run's real config dir and `$HOME` ==
  the container's real home, so its own startup call is self-consistent:
  mounted skills really do live under `<CONFIG_DIR>/skills`, so nothing is
  lost.
- But this repo's own black-box tests (`tests/test_e2e.py`'s `EngineProc`,
  `tests/conftest.py`'s `LiveRun` -- ~19+ modules) spawn a **second, child**
  `ralphd-engine` process to test the engine itself, with a throwaway tmp
  `config_dir` that (almost always) has no `skills/` subdirectory at all.
  Both helpers build that child's env as `{**os.environ, **(stub_env or {})}`
  (test_e2e.py's `EngineProc.__init__`, conftest.py's `LiveRun.__init__`):
  `stub_env` is where a HOME override *can* land (and does, in the ~19
  modules that test creds/skills/prompts/llm precisely because they need a
  virgin HOME -- see conftest.py's #67 commentary), but it is opt-in per
  test, not a default. Any e2e/live test module that does not pass a HOME
  override in `stub_env` spawns a child engine that inherits the **real**
  ambient `$HOME` of whatever process is running pytest.
- That child's very first startup action, per `main.py:186`, is
  `place_skills(CONFIG_DIR)` against its own tmp `config_dir` -- which has no
  `skills/` subdir -- and no `home=` override, so `_home()` resolves the
  *real* ambient `$HOME` again. The rebuild empties
  `<real $HOME>/.pi/agent/skills/` and repopulates it with nothing, because
  the child's sources (mounted + api, both keyed off its own tmp
  `config_dir`/overlay) are empty. Any skill a human or another process had
  placed under that real HOME -- exactly the operator's observed
  `~/.pi/agent/skills/` going empty after "this run's suite ran" -- is gone.

This is the same family as #67 (HOME-override footguns in the e2e/live test
harness), not a new defect class: #67 was about import path under an
*overridden* HOME; this is about side effects on the real HOME when tests
do *not* override it, from a callee (`place_skills`) that clears-then-fills
its target unconditionally by design.

The reproducer below does not touch this container's real `$HOME` -- it
monkeypatches `HOME` to a throwaway `tmp_path` directory that stands in for
"the ambient ~/.pi/agent/skills/ a human had placed something under", then
spawns exactly the class of child engine the rest of the e2e suite spawns
(no HOME in `stub_env`), and shows the pre-placed skill symlink is gone
afterwards.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_e2e import EngineProc


def test_child_engine_without_home_override_wipes_ambient_home_skills(
    tmp_path: Path, monkeypatch
) -> None:
    # Stand-in for "the real $HOME" the operator observed losing its skill.
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

    # Everything downstream of this reads $HOME from the environment, same
    # as the real container ambient env would.
    monkeypatch.setenv("HOME", str(ambient_home))

    # Same construction as tests/test_e2e.py's `engine_factory` fixture,
    # deliberately with NO stub_env HOME override -- this is the common
    # case across the e2e/live suite, not a contrived one.
    e = EngineProc(
        tmp_path / "engine",
        {"run_id": "skills-loss-repro", "iterations": 1,
         "max_approaches": 1, "on_complete": "idle"},
    )
    try:
        e.wait_api(timeout=30)
    finally:
        e.stop()

    # The command that reproduced the loss, for the report:
    #   HOME=<ambient_home> <ralphd-engine> with CONFIG_DIR=<tmp config dir
    #   with no skills/ subdir>
    # Observed: the manually-placed symlink under the ambient HOME is gone.
    assert not manual_link.exists(), (
        "expected place_skills(CONFIG_DIR) at engine/main.py:186, called "
        "with no home= override inside the child engine, to have wiped "
        "the ambient $HOME's ~/.pi/agent/skills/manual-skill symlink"
    )
    assert list(ambient_skills.iterdir()) == [], (
        "expected the ambient ~/.pi/agent/skills/ directory to be emptied, "
        "not merely have this one entry replaced"
    )
