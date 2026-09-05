"""Task 003 (finding 3, PRD requirement B.3): the live `telegram-notify`
skill pi actually reads is `$HOME/.pi/agent/skills/telegram-notify/
SKILL.md`, placed by `skills.place_skills()` from a host-supplied,
untracked mounted (or api) source directory -- `examples/skills/
telegram-notify/SKILL.md` in this repo is only ever a vendored copy that
is never itself live-mounted, so no hand-edit to a tracked file can ever
reach the copy pi actually loads.

The only repo-owned lever is engine code that runs AT placement time, so
`place_skills()` now injects the "prefer the engine's own `notify` tool"
instruction into a placed `telegram-notify/SKILL.md` that doesn't already
carry it -- and this module pins that mechanism directly, independently of
`tests/test_skills_placement_detection.py` (which exercises the
verify/lost-event machinery around it) and `tests/test_notify_hat_tool.py`
(which pins the vendored copy's own wording).

Three things get proven:
  (a) a `telegram-notify` source lacking the instruction gets it appended
      at placement time, with every byte of the source preserved verbatim
      ahead of the appended block;
  (b) a `telegram-notify` source that already carries the instruction is
      placed completely unchanged (no double-injection, no rewrite);
  (c) a differently-named skill -- and every OTHER file inside the
      telegram-notify skill directory besides SKILL.md itself -- is placed
      byte-identical to its source, exactly as before this task.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import skills


def _mount(config_dir: Path, name: str, skill_md: str) -> Path:
    d = config_dir / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(skill_md)
    return d


def _placed_md(home: Path, name: str) -> Path:
    return home / ".pi" / "agent" / "skills" / name / "SKILL.md"


NO_INSTRUCTION_SRC = (
    "---\nname: telegram-notify\ndescription: x\n---\n\n"
    "# Telegram notifications to the operator\n\n"
    "Some body text that must survive verbatim.\n"
)


def test_telegram_notify_missing_instruction_is_injected_at_placement(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    _mount(config_dir, "telegram-notify", NO_INSTRUCTION_SRC)
    home = tmp_path / "home"
    home.mkdir()

    placed = skills.place_skills(config_dir, home=home)
    assert placed == ["telegram-notify"]

    link = home / ".pi" / "agent" / "skills" / "telegram-notify"
    assert link.is_symlink() and link.is_dir()

    text = _placed_md(home, "telegram-notify").read_text()
    assert skills.NOTIFY_PREFERENCE_MARKER in text
    assert "notify" in text.lower()
    # everything from the source is preserved, unmodified, ahead of the
    # appended instruction -- never rewritten in place.
    assert text.startswith(NO_INSTRUCTION_SRC.rstrip("\n"))
    assert text != NO_INSTRUCTION_SRC


def test_telegram_notify_source_already_carrying_it_is_placed_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    already = (
        "---\nname: telegram-notify\n---\n\n# Telegram\n\n"
        f"## {skills.NOTIFY_PREFERENCE_MARKER}\n\n"
        "Already here, verbatim.\n"
    )
    _mount(config_dir, "telegram-notify", already)
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)

    assert _placed_md(home, "telegram-notify").read_text() == already


def test_a_differently_named_skill_is_placed_byte_identical_to_its_source(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    src_content = "# some-other-skill\n\nDoes a thing. Contains `notify` too.\n"
    src_dir = _mount(config_dir, "some-other-skill", src_content)
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)

    placed_md = _placed_md(home, "some-other-skill")
    assert placed_md.read_text() == src_content
    assert placed_md.read_text() == (src_dir / "SKILL.md").read_text()
    # still a plain symlink straight at the source -- no materialization
    # for any name other than telegram-notify.
    link = home / ".pi" / "agent" / "skills" / "some-other-skill"
    assert link.is_symlink()
    assert link.resolve() == src_dir.resolve()


def test_other_files_alongside_telegram_notifys_skill_md_are_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    src_dir = _mount(config_dir, "telegram-notify", NO_INSTRUCTION_SRC)
    (src_dir / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)

    helper = home / ".pi" / "agent" / "skills" / "telegram-notify" / "helper.sh"
    assert helper.read_text() == "#!/bin/sh\necho hi\n"


def test_re_placement_picks_up_a_changed_source_each_time(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    src_dir = _mount(config_dir, "telegram-notify", NO_INSTRUCTION_SRC)
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)
    first = _placed_md(home, "telegram-notify").read_text()
    assert skills.NOTIFY_PREFERENCE_MARKER in first

    updated_src = NO_INSTRUCTION_SRC + "\nAn extra paragraph added later.\n"
    (src_dir / "SKILL.md").write_text(updated_src)

    skills.place_skills(config_dir, home=home)
    second = _placed_md(home, "telegram-notify").read_text()
    assert skills.NOTIFY_PREFERENCE_MARKER in second
    assert "An extra paragraph added later." in second
    assert second.startswith(updated_src.rstrip("\n"))
