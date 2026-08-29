"""Black-box tests for `ralphctl skills install` (task 022, requirement R,
#88): the one `skills` action that takes no run id. Invokes the real
`ralphctl` executable with `HOME` pointed at a tmp dir -- no docker, no
engine, this verb never touches a run.

Covers: fresh install, an unchanged re-install, refusal on local
modification, `--force` overriding that refusal, both auto-detected
layouts (`~/.pi/agent/skills` and `~/.claude/skills`), `--target`, and that
the report reflects an explicit post-copy discoverability check rather than
just "the copy call returned".
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.conftest import RALPHCTL

REPO = Path(__file__).parent.parent
SOURCE_SKILL = REPO / "skills" / "ralphd"


def run(*argv: str, home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _installed(home: Path) -> Path:
    return home / ".pi" / "agent" / "skills" / "ralphd"


def test_source_skill_exists():
    # Sanity: the fixture this whole module exercises against is real.
    assert (SOURCE_SKILL / "SKILL.md").is_file()


def test_fresh_install_default_layout_when_neither_agent_detected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    res = run("--json", "skills", "install", home=home)
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    dest = _installed(home)
    assert body["target"] == str(dest)
    assert body["verified"] is True
    assert (dest / "SKILL.md").is_file()
    assert (dest / "SKILL.md").read_text() == (SOURCE_SKILL / "SKILL.md").read_text()
    assert (dest / ".ralphd-skill-stamp.json").is_file()


def test_fresh_install_reports_verification_in_human_output(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    res = run("skills", "install", home=home)
    assert res.returncode == 0, res.stderr
    assert "verified" in res.stdout
    assert "discoverable" in res.stdout
    assert "SKILL.md" in res.stdout


def test_auto_detect_pi_layout(tmp_path):
    home = tmp_path / "home"
    (home / ".pi" / "agent" / "skills").mkdir(parents=True)
    res = run("--json", "skills", "install", home=home)
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["target"] == str(home / ".pi" / "agent" / "skills" / "ralphd")


def test_auto_detect_claude_layout(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    res = run("--json", "skills", "install", home=home)
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["target"] == str(home / ".claude" / "skills" / "ralphd")


def test_pi_layout_wins_when_both_exist(tmp_path):
    home = tmp_path / "home"
    (home / ".pi" / "agent" / "skills").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    res = run("--json", "skills", "install", home=home)
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    assert body["target"] == str(home / ".pi" / "agent" / "skills" / "ralphd")


def test_target_flag_overrides_auto_detection(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "somewhere-else"
    target.mkdir()
    res = run("--json", "skills", "install", "--target", str(target), home=home)
    assert res.returncode == 0, res.stderr
    body = json.loads(res.stdout)
    dest = target / "ralphd"
    assert body["target"] == str(dest)
    assert (dest / "SKILL.md").is_file()
    # nothing under $HOME was touched
    assert not (home / ".pi").exists()
    assert not (home / ".claude").exists()


def test_unchanged_reinstall_succeeds(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first = run("--json", "skills", "install", home=home)
    assert first.returncode == 0, first.stderr
    stamp_path = _installed(home) / ".ralphd-skill-stamp.json"
    stamp_before = stamp_path.read_text()

    second = run("--json", "skills", "install", home=home)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["verified"] is True
    # re-running against an untouched, already-matching target is not a
    # refusal -- the stamp is rewritten but its content is identical.
    assert stamp_path.read_text() == stamp_before


def test_locally_modified_target_is_refused_without_force(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first = run("skills", "install", home=home)
    assert first.returncode == 0, first.stderr

    dest = _installed(home)
    original = (dest / "SKILL.md").read_text()
    (dest / "SKILL.md").write_text(original + "\n(hand-edited by the operator)\n")

    res = run("skills", "install", home=home)
    assert res.returncode == 2
    assert "local modification" in res.stderr
    assert "--force" in res.stderr
    # the refusal did not touch the tampered file
    assert (dest / "SKILL.md").read_text() == original + "\n(hand-edited by the operator)\n"


def test_force_overrides_local_modification_refusal(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first = run("skills", "install", home=home)
    assert first.returncode == 0, first.stderr

    dest = _installed(home)
    (dest / "SKILL.md").write_text("tampered\n")

    res = run("--json", "skills", "install", "--force", home=home)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["verified"] is True
    assert (dest / "SKILL.md").read_text() == (SOURCE_SKILL / "SKILL.md").read_text()


def test_target_with_no_skill_md_after_placement_is_never_reported_as_success(tmp_path):
    """White-box: `_skill_discoverable` -- the same rule `install` checks
    after every copy -- says no for a directory with no SKILL.md, so a
    caller cannot mistake 'the copy call returned' for 'the agent can find
    it'."""
    from ralphd.cli import main as climain

    empty = tmp_path / "not-a-skill"
    empty.mkdir()
    (empty / "notes.txt").write_text("no SKILL.md here\n")
    assert climain._skill_discoverable(empty) is False

    real = tmp_path / "a-skill"
    real.mkdir()
    (real / "SKILL.md").write_text("# hi\n")
    assert climain._skill_discoverable(real) is True


def test_skills_ls_still_requires_a_run_id(tmp_path):
    """`install` is the only run-less action; every other one still needs a
    run id, and a bare `ralphctl skills` (no action at all) is a normal
    argparse usage error, not a crash."""
    home = tmp_path / "home"
    home.mkdir()
    res = run("skills", "no-such-run", "ls", home=home)
    assert res.returncode == 3
    assert "no-such-run" in res.stderr

    res2 = run("skills", home=home)
    assert res2.returncode == 2
