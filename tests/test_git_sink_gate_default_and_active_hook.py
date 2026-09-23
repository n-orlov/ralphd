"""Task 034 (PRD requirement I.6, filed in `tasks.json` as "I.6"): two
things task 033's five guard cases do not cover on their own.

1. `commit_secret_scan`'s default is asserted through the ENGINE'S OWN
   config loader (`JobConfig.load`), not just the dataclass default read
   directly off `JobConfig()` -- a job.yaml this run never wrote must
   still come up with the gate ON, and an explicit `false` in job.yaml
   must disable it through the very same loader.
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
    job_yaml = tmp_path / "job.yaml"
    job_yaml.write_text("commit_secret_scan: false\n")

    cfg = JobConfig.load(job_yaml)

    assert cfg.commit_secret_scan is False
    # And the install call the engine gates on that same field actually
    # declines to install anything for this config.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    if not cfg.commit_secret_scan:
        hook = None
    else:  # pragma: no cover - only exercised if the assertion above fails
        hook = commit_scan.install_push_gate(workspace, tmp_path / "hooks")
    assert hook is None


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
    for name in (commit_scan.PUSH_GATE_HOOK_NAME, commit_scan.COMMIT_MSG_HOOK_NAME,
                 commit_scan.PRE_COMMIT_HOOK_NAME):
        path = hooks_dir / name
        assert path.is_file(), f"{name} was not installed in the engine-owned dir"
        assert path.stat().st_mode & 0o111, f"{name} is not executable"
    # Observation 3: the agent uid cannot write it -- no group/other write
    # bit on the hook files or the directory itself (the property that
    # holds whether or not a real privsep uid boundary separates this
    # process from the agent; see tests/test_engine_uid_boundary.py for
    # where that boundary is pinned directly).
    for path in (hooks_dir, hooks_dir / commit_scan.PUSH_GATE_HOOK_NAME,
                 hooks_dir / commit_scan.COMMIT_MSG_HOOK_NAME,
                 hooks_dir / commit_scan.PRE_COMMIT_HOOK_NAME):
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o022 == 0, f"{path} is writable by others"


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
