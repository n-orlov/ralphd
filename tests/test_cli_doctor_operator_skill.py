"""Black-box tests for `ralphctl doctor`'s operator-skill line (task 023,
requirement R): installed / missing / stale, driven by the install stamp's
recorded version (never guessed from mere presence). Invokes the real
`ralphctl` executable with `HOME` pointed at a tmp dir and `RALPHD_DOCKER` at
the recording stub so the check is isolated from anything else `doctor`
reports.

`_operator_skill_status` (main.py) is the one function both `doctor` and
this module read: it checks the same two agent layouts `skills install`
auto-detects (pi first), via `_skill_discoverable` -- the same discoverability
rule `skills install` verifies its own copy against -- so the three surfaces
can never disagree about what "installed" means.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.conftest import RALPHCTL

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"


class Ctl:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.home = tmp / "home"
        self.home.mkdir()
        self.log = tmp / "docker-argv.jsonl"

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {
            **os.environ,
            "HOME": str(self.home),
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            "STUB_DOCKER_INSPECT_OK": "1",
            **(env or {}),
        }
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=60)

    def doctor(self) -> subprocess.CompletedProcess:
        return self.run("--json", "doctor")

    def doctor_text(self) -> subprocess.CompletedProcess:
        return self.run("doctor")

    def install(self, *argv: str) -> subprocess.CompletedProcess:
        return self.run("skills", "install", *argv)


def _installed(home: Path) -> Path:
    return home / ".pi" / "agent" / "skills" / "ralphd"


def test_missing_when_never_installed(tmp_path):
    ctl = Ctl(tmp_path)
    res = ctl.doctor()
    body = json.loads(res.stdout)
    assert body["operatorSkill"] == {"status": "missing", "path": None,
                                     "installedVersion": None}
    text = ctl.doctor_text().stdout
    assert "operator skill: missing" in text


def test_installed_right_after_a_fresh_install(tmp_path):
    ctl = Ctl(tmp_path)
    inst = ctl.install()
    assert inst.returncode == 0, inst.stderr
    res = ctl.doctor()
    body = json.loads(res.stdout)
    assert body["operatorSkill"]["status"] == "installed"
    assert body["operatorSkill"]["path"] == str(_installed(ctl.home))
    text = ctl.doctor_text().stdout
    assert "operator skill: installed" in text
    line = next(ln for ln in text.splitlines() if "operator skill:" in ln)
    assert "stale" not in line


def test_stale_when_the_stamped_version_is_behind_ralphctls_own(tmp_path):
    ctl = Ctl(tmp_path)
    inst = ctl.install()
    assert inst.returncode == 0, inst.stderr
    stamp_path = _installed(ctl.home) / ".ralphd-skill-stamp.json"
    stamp = json.loads(stamp_path.read_text())
    old_version = "0.0.1"
    assert stamp["version"] != old_version
    stamp["version"] = old_version
    stamp_path.write_text(json.dumps(stamp))
    res = ctl.doctor()
    body = json.loads(res.stdout)
    assert body["operatorSkill"] == {
        "status": "stale", "path": str(_installed(ctl.home)),
        "installedVersion": old_version,
    }
    text = ctl.doctor_text().stdout
    assert f"operator skill: stale (built from {old_version})" in text


def test_a_discoverable_skill_with_no_stamp_is_installed_not_guessed_stale(tmp_path):
    """A skill placed some other way than `ralphctl skills install` (no
    stamp) is not a version claim `doctor` can compare -- it must not invent
    one and call it stale."""
    ctl = Ctl(tmp_path)
    dest = _installed(ctl.home)
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("---\nname: ralphd\n---\nbody\n")
    res = ctl.doctor()
    body = json.loads(res.stdout)
    assert body["operatorSkill"] == {"status": "installed", "path": str(dest),
                                     "installedVersion": None}


def test_claude_layout_is_also_detected(tmp_path):
    ctl = Ctl(tmp_path)
    inst = ctl.install()
    assert inst.returncode == 0, inst.stderr
    pi_dest = _installed(ctl.home)
    claude_dest = ctl.home / ".claude" / "skills" / "ralphd"
    claude_dest.parent.mkdir(parents=True)
    pi_dest.rename(claude_dest)
    res = ctl.doctor()
    body = json.loads(res.stdout)
    assert body["operatorSkill"]["status"] == "installed"
    assert body["operatorSkill"]["path"] == str(claude_dest)


def test_operator_skill_never_affects_the_overall_verdict(tmp_path):
    """Report-only, like image staleness: a missing/stale skill is not one of
    `checks` -- an operator who never installed it is not, on that account,
    a broken one."""
    ctl = Ctl(tmp_path)
    res = ctl.doctor()
    body = json.loads(res.stdout)
    assert "operator_skill" not in body["checks"]
    assert body["operatorSkill"]["status"] == "missing"
