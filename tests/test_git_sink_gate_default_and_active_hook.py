"""Task 034 (PRD requirement I.6, filed in `tasks.json` as "I.6"): two
things task 033's five guard cases do not cover on their own.

1. `commit_secret_scan`'s default is asserted through the ENGINE'S OWN
   config loader (`JobConfig.load`), not just the dataclass default read
   directly off `JobConfig()` -- a job.yaml this run never wrote must
   still come up with the gate ON, and an explicit `false` in job.yaml
   must disable it through the very same loader. "Disable" is measured by
   driving the ENGINE'S own gate (`LoopSupervisor
   ._install_commit_scan_push_gate`) with the loaded config, never by a
   branch the test writes itself.
2. The hook task 031 installs is shown ACTIVE for a working copy of THIS
   repository -- a real clone of the tree these tests themselves live
   in, not a bare `git init` scratch repo -- and a commit carrying a
   synthetic redaction-map value against that clone's own installed hook
   is refused, naming the variable and never the value.

Every value used here is SYNTHETIC (PRD requirement I.9) -- constructed
for this module alone, never a real credential.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ralphd.engine import commit_scan, redact
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, atomic_write_json

REPO_ROOT = Path(__file__).resolve().parents[1]

SYNTHETIC_VALUE = "sk-synthetic-i6-3141592653ABCDEFghijSYNTH"
SYNTHETIC_NAME = "SYNTHETIC_I6_GATE_TOKEN"


@pytest.fixture
def live_synthetic_map(monkeypatch, tmp_path):
    fake_home = tmp_path / "fake-home-i6"
    fake_home.mkdir()
    monkeypatch.setenv(SYNTHETIC_NAME, SYNTHETIC_VALUE)
    redact.refresh_redaction_map(home=fake_home)
    assert redact.live_redaction_map().get(SYNTHETIC_VALUE) == f"env:{SYNTHETIC_NAME}"
    try:
        yield
    finally:
        monkeypatch.delenv(SYNTHETIC_NAME, raising=False)
        redact.refresh_redaction_map(home=fake_home)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _supervisor(root: Path, workspace: Path, cfg: JobConfig) -> LoopSupervisor:
    """A real `LoopSupervisor` over a real git workspace, so the gate under
    test is the engine's own `_install_commit_scan_push_gate` and not a
    branch this module writes."""
    root.mkdir(parents=True)
    workspace.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    _git(workspace, "init", "-q")
    (workspace / "seed.txt").write_text("seed\n")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    _git(workspace, "add", "seed.txt")
    _git(workspace, "commit", "-q", "-m", "chore: seed")
    return LoopSupervisor(cfg, run, workspace)


def _configured_hooks_path(workspace: Path) -> subprocess.CompletedProcess:
    return _git(workspace, "config", "core.hooksPath")


def _clone_this_repo(dest: Path) -> None:
    """A real, throwaway clone of the repository these tests live in --
    `--no-hardlinks` so nothing in the scratch clone can alias back into
    this workspace's own objects."""
    ran = subprocess.run(
        ["git", "clone", "--no-hardlinks", "-q", str(REPO_ROOT), str(dest)],
        capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    _git(dest, "config", "user.email", "t@example.com")
    _git(dest, "config", "user.name", "t")


# ---------------------------------------------------------------------------
# 1. The default, and an explicit off, through the engine's own loader.
# ---------------------------------------------------------------------------

def test_the_default_through_the_engine_config_loader_is_on(tmp_path):
    """No job.yaml at all (the loader's `path.exists()` branch is
    skipped) still comes up with the gate ON -- through `JobConfig.load`
    itself, not the dataclass default read directly."""
    missing = tmp_path / "no-such-job.yaml"
    assert not missing.exists()

    cfg = JobConfig.load(missing)

    assert cfg.commit_secret_scan is True


def test_an_explicit_off_value_through_the_loader_disables_the_gate(tmp_path):
    """An explicit `false` in job.yaml, read through `JobConfig.load`, makes
    the ENGINE's own gate install nothing -- and the same engine call, with
    a job.yaml that merely omits the field (the loader's default), installs
    it. Both halves run the engine's gate; neither branches here."""
    off_yaml = tmp_path / "off" / "job.yaml"
    off_yaml.parent.mkdir()
    off_yaml.write_text("commit_secret_scan: false\nrun_id: gate-off\n")
    off_cfg = JobConfig.load(off_yaml)
    assert off_cfg.commit_secret_scan is False
    off_ws = tmp_path / "off-ws"
    off_sup = _supervisor(tmp_path / "off-run", off_ws, off_cfg)

    assert off_sup._install_commit_scan_push_gate(None) is None
    # Nothing was pointed anywhere: git has no `core.hooksPath` at all.
    assert _configured_hooks_path(off_ws).returncode != 0

    # Non-vacuity: the identical engine call, config loaded from a job.yaml
    # that never mentions the field, DOES install the gate.
    on_yaml = tmp_path / "on" / "job.yaml"
    on_yaml.parent.mkdir()
    on_yaml.write_text("run_id: gate-default\n")
    on_cfg = JobConfig.load(on_yaml)
    assert on_cfg.commit_secret_scan is True
    on_ws = tmp_path / "on-ws"
    on_sup = _supervisor(tmp_path / "on-run", on_ws, on_cfg)

    hook = on_sup._install_commit_scan_push_gate(None)

    assert hook is not None and hook.is_file()
    configured = _configured_hooks_path(on_ws)
    assert configured.returncode == 0
    assert Path(configured.stdout.strip()) == hook.parent


# ---------------------------------------------------------------------------
# 2. The installed hook is active for a real clone of this repository.
# ---------------------------------------------------------------------------

def test_the_installed_hook_is_active_for_a_clone_of_this_repository(tmp_path):
    clone = tmp_path / "repo-clone"
    hooks_dir = tmp_path / "engine-owned-hooks"
    _clone_this_repo(clone)

    hook = commit_scan.install_push_gate(clone, hooks_dir)

    assert hook is not None
    # Observation 1: the configured hooks path resolves to the
    # engine-owned directory -- outside the cloned working copy.
    configured = _git(clone, "config", "core.hooksPath").stdout.strip()
    assert Path(configured) == hooks_dir
    assert clone not in hooks_dir.parents and hooks_dir != clone
    # Observation 2: the hook file is present there, for all three modes.
    hook_paths = [hooks_dir / name for name in
                  (commit_scan.PUSH_GATE_HOOK_NAME,
                   commit_scan.COMMIT_MSG_HOOK_NAME,
                   commit_scan.PRE_COMMIT_HOOK_NAME)]
    for path in hook_paths:
        assert path.is_file(), f"{path.name} was not installed in the dir"
        assert path.stat().st_mode & 0o111, f"{path.name} is not executable"
    # Observation 3: the agent uid -- the uid THIS process runs as, which is
    # the uid a worker iteration's own shell runs as -- cannot write the
    # installed hook. Not "no group/other write bit": no write bit at all,
    # so the kernel refuses an actual write attempt by this very uid. The
    # attempt below is made, not assumed.
    assert os.getuid() != 0, "a root uid can write anything; this proves nothing"
    for path in hook_paths:
        assert path.stat().st_mode & 0o222 == 0, f"{path} carries a write bit"
        assert not os.access(path, os.W_OK), f"{path} is writable by this uid"
        with pytest.raises(PermissionError):
            with open(path, "a") as fh:  # noqa: SIM115 - the refusal IS the test
                fh.write("# the agent tampering with its own bound\n")
        assert "tampering" not in path.read_text()
    # The `scan-base` note the pre-push hook reads its boundary base from is
    # no more writable than the hooks themselves.
    base = hooks_dir / commit_scan.SCAN_BASE_FILENAME
    assert base.stat().st_mode & 0o222 == 0 and not os.access(base, os.W_OK)
    # The ENGINE, which refreshes the gate before every worker iteration,
    # still gets through that: it replaces each file rather than writing
    # through it, and the replacement is non-writable again.
    again = commit_scan.install_push_gate(clone, hooks_dir, base_sha="a" * 40)
    assert again == hook
    assert base.read_text().strip() == "a" * 40
    for path in hook_paths:
        assert path.stat().st_mode & 0o222 == 0, f"{path} came back writable"


def test_a_poisoned_commit_message_against_that_active_hook_is_refused(
        tmp_path, live_synthetic_map):
    clone = tmp_path / "repo-clone-poison"
    hooks_dir = tmp_path / "engine-owned-hooks-poison"
    _clone_this_repo(clone)
    hook = commit_scan.install_push_gate(clone, hooks_dir)
    assert hook is not None
    (clone / "scratch-note.txt").write_text("nothing interesting here\n")
    _git(clone, "add", "scratch-note.txt")

    ran = subprocess.run(
        ["git", "commit", "-m",
         f"chore: a synthetic leak {SYNTHETIC_VALUE} in the commit message"],
        cwd=str(clone), capture_output=True, text=True)

    assert ran.returncode != 0, "the active hook did not refuse the poisoned commit"
    refusal = ran.stdout + ran.stderr
    assert SYNTHETIC_NAME in refusal, "the refusal never named the variable"
    assert SYNTHETIC_VALUE not in refusal, "the refusal text leaked the matched value"
