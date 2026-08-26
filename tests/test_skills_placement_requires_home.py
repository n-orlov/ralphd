"""Unit-level closure test for #83 (task 002): `place_skills()` must not be
able to rebuild a HOME or CONFIG_DIR it was not explicitly given.

Before this task, `place_skills(config_dir, home=None)` defaulted a missing
`home` to the real ambient `$HOME` (`_home()`) and never checked `config_dir`
at all -- see `tests/test_skills_placement_ambient_home_loss.py` for the
full root-cause writeup and a live-child-process reproduction of the damage
that silent fallback did. This module tests the *fix* directly, at the
function boundary, with no child process involved: `place_skills()` now
takes `home` as a required keyword-only argument (a caller that omits it
gets a `TypeError` before any filesystem write happens) and rejects any
`config_dir` that is not the engine's own `CONFIG_DIR` (a caller that passes
a foreign one gets a `ValueError`, also before any write). Both refusals are
proven not to touch a decoy symlink planted under a fake HOME that stands
in for "the caller's real $HOME" -- exactly the artifact #83 actually lost.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralphd.engine import skills
from ralphd.engine.config import CONFIG_DIR


def _plant_decoy(fake_home: Path) -> Path:
    """A pre-existing skill symlink under `fake_home/.pi/agent/skills/`,
    standing in for something a human or another process had placed under
    the caller's real $HOME before place_skills() ran."""
    skills_dir = fake_home / ".pi" / "agent" / "skills"
    skills_dir.mkdir(parents=True)
    source = fake_home / "decoy-source"
    source.mkdir()
    (source / "SKILL.md").write_text("# decoy\n")
    link = skills_dir / "decoy"
    link.symlink_to(source, target_is_directory=True)
    assert link.is_symlink() and link.exists()
    return link


def test_place_skills_without_home_raises_typeerror_not_ambient_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "fake-home"
    decoy = _plant_decoy(fake_home)
    # Stand-in for "the process's real ambient $HOME" that the old default
    # (`home or _home()`) would have silently rebuilt.
    monkeypatch.setenv("HOME", str(fake_home))

    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with pytest.raises(TypeError):
        skills.place_skills(config_dir)  # no home= at all

    assert decoy.is_symlink() and decoy.exists(), (
        "place_skills() must refuse (TypeError, missing required keyword "
        "argument 'home') before touching any HOME, including the ambient "
        "one it used to fall back to -- the decoy symlink must survive"
    )


def test_place_skills_with_foreign_config_dir_raises_valueerror(
    tmp_path: Path
) -> None:
    fake_home = tmp_path / "fake-home2"
    decoy = _plant_decoy(fake_home)

    foreign_config_dir = tmp_path / "not-the-engines-config-dir"
    foreign_config_dir.mkdir()
    assert foreign_config_dir != CONFIG_DIR

    with pytest.raises(ValueError, match="CONFIG_DIR"):
        skills.place_skills(foreign_config_dir, home=fake_home)

    assert decoy.is_symlink() and decoy.exists(), (
        "place_skills() must refuse a config_dir that isn't the engine's "
        "own CONFIG_DIR before touching the passed-in home at all -- the "
        "decoy symlink under fake_home must survive untouched"
    )


def test_place_skills_with_engines_own_config_dir_and_explicit_home_works(
    tmp_path: Path, monkeypatch
) -> None:
    """Sanity: the two guards don't break the legitimate call shape that
    engine/api.py's two call sites and engine/main.py now use -- explicit
    home=, and config_dir equal to the engine's own CONFIG_DIR."""
    real_config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", real_config_dir)
    real_config_dir.mkdir(parents=True, exist_ok=True)
    skill_dir = real_config_dir / "skills" / "greet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# greet\n")

    home = tmp_path / "home"
    home.mkdir()

    placed = skills.place_skills(real_config_dir, home=home)
    assert placed == ["greet"]
    assert (home / ".pi" / "agent" / "skills" / "greet").is_symlink()
