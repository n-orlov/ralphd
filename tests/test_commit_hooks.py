"""Task 031 (PRD requirement I.1, #210): the LOCAL half of the git-sink
bound -- a `commit-msg` and a `pre-commit` hook, reached through an
engine-owned `core.hooksPath` OUTSIDE the workspace, so the agent uid
cannot rewrite the bound it is subject to (the same privsep reasoning
v0.22's A/B/C states). `tests/test_commit_scan_boundary.py` and
`tests/test_commit_scan_engine_boundary.py` already pin the `pre-push`
half and the task-boundary scan (task 029/030); this module pins the two
hooks task 031 adds to the very same `install_push_gate` call, plus the
one property that ties all three layers together: `git commit
--no-verify` bypasses these two hooks, but the task-boundary scan from
029 still catches the exact same commit.

Everything here uses SYNTHETIC secret values only (PRD requirement I.9) --
no real credential is ever read or matched.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ralphd.engine import commit_scan, redact
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

SYNTHETIC_VALUE = "sk-synthetic-hooks-0123456789abcdefSYNTH"
SYNTHETIC_NAME = "SYNTHETIC_HOOKS_API_TOKEN"

# Shape-alike but never a configured value -- the shape-vs-value line PRD
# requirement I.4 draws, carried through the local hook layer too.
TOKEN_SHAPED_BUT_UNKNOWN = "sk-lookalike-hooks-7777777777wwwwwwSHAPE"


@pytest.fixture
def live_synthetic_map(monkeypatch, tmp_path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv(SYNTHETIC_NAME, SYNTHETIC_VALUE)
    redact.refresh_redaction_map(home=fake_home)
    assert redact.live_redaction_map().get(SYNTHETIC_VALUE) == f"env:{SYNTHETIC_NAME}"
    try:
        yield
    finally:
        monkeypatch.delenv(SYNTHETIC_NAME, raising=False)
        redact.refresh_redaction_map(home=fake_home)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(workspace: Path) -> str:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    (workspace / "seed.txt").write_text("seed\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")
    return _head(workspace)


def _head(workspace: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()


def _install(workspace: Path, hooks_dir: Path, base_sha: str | None = None) -> Path:
    hook = commit_scan.install_push_gate(workspace, hooks_dir, base_sha=base_sha)
    assert hook is not None
    return hook


# ---------------------------------------------------------------------------
# Ownership and mode: the engine-owned hooks dir, outside the workspace.
# ---------------------------------------------------------------------------

def test_the_commit_msg_and_pre_commit_hooks_are_engine_owned_and_outside_the_workspace(
        tmp_path):
    workspace = tmp_path / "ws"
    hooks_dir = tmp_path / "engine-hooks"
    _init_repo(workspace)

    _install(workspace, hooks_dir)

    commit_msg = hooks_dir / commit_scan.COMMIT_MSG_HOOK_NAME
    pre_commit = hooks_dir / commit_scan.PRE_COMMIT_HOOK_NAME
    assert commit_msg.is_file() and pre_commit.is_file()
    assert workspace not in hooks_dir.parents and hooks_dir != workspace
    for path in (commit_msg, pre_commit, hooks_dir):
        # "owned by the engine uid": this process IS the engine in every
        # test here (no privsep boundary in this sandbox -- see
        # tests/test_engine_uid_boundary.py), so the hooks it just wrote
        # are owned by exactly the uid running this test.
        assert path.stat().st_uid == commit_scan.os.getuid()
        # "not writable by the agent uid": no group or other write bit --
        # the one property that holds whether or not a real uid boundary
        # separates the engine from the agent.
        assert path.stat().st_mode & 0o022 == 0, f"{path} is writable by others"
    assert commit_msg.stat().st_mode & 0o111, "the commit-msg hook is not executable"
    assert pre_commit.stat().st_mode & 0o111, "the pre-commit hook is not executable"
    configured = subprocess.run(
        ["git", "-C", str(workspace), "config", "core.hooksPath"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert Path(configured) == hooks_dir
    # Neither hook file ever carries a secret VALUE -- both rebuild the map
    # in their own process instead.
    assert SYNTHETIC_VALUE not in commit_msg.read_text()
    assert SYNTHETIC_VALUE not in pre_commit.read_text()


# ---------------------------------------------------------------------------
# On a hit: refuse, naming the variable only, never the value.
# ---------------------------------------------------------------------------

def test_the_commit_msg_hook_refuses_a_leaking_message(tmp_path, live_synthetic_map):
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    hook = _install(workspace, tmp_path / "hooks")
    commit_msg_hook = hook.parent / commit_scan.COMMIT_MSG_HOOK_NAME
    msg_file = tmp_path / "MSG"
    msg_file.write_text(f"chore: leak {SYNTHETIC_VALUE} in the message\n")

    ran = subprocess.run([str(commit_msg_hook), str(msg_file)],
                         cwd=str(workspace), capture_output=True, text=True)

    assert ran.returncode == 1, "the commit-msg hook did not refuse"
    assert SYNTHETIC_NAME in ran.stderr
    assert SYNTHETIC_VALUE not in ran.stderr
    assert SYNTHETIC_VALUE not in ran.stdout


def test_the_pre_commit_hook_refuses_a_leaking_staged_diff(tmp_path, live_synthetic_map):
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    hook = _install(workspace, tmp_path / "hooks")
    pre_commit_hook = hook.parent / commit_scan.PRE_COMMIT_HOOK_NAME
    (workspace / "leak.txt").write_text(f"leaked={SYNTHETIC_VALUE}\n")
    _git(workspace, "add", "leak.txt")

    ran = subprocess.run([str(pre_commit_hook)], cwd=str(workspace),
                         capture_output=True, text=True)

    assert ran.returncode == 1, "the pre-commit hook did not refuse"
    assert SYNTHETIC_NAME in ran.stderr
    assert SYNTHETIC_VALUE not in ran.stderr
    assert SYNTHETIC_VALUE not in ran.stdout


def test_a_real_git_commit_is_refused_end_to_end(tmp_path, live_synthetic_map):
    """Non-vacuity through the real `git commit` path, not just the hook
    binary in isolation: `core.hooksPath` points at the engine's dir, and
    `git commit` itself refuses."""
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    _install(workspace, tmp_path / "hooks")
    (workspace / "leak.txt").write_text(f"leaked={SYNTHETIC_VALUE}\n")
    _git(workspace, "add", "leak.txt")

    ran = subprocess.run(
        ["git", "commit", "-m", "chore: a commit git itself must refuse"],
        cwd=str(workspace), capture_output=True, text=True)

    assert ran.returncode != 0
    assert SYNTHETIC_NAME in (ran.stdout + ran.stderr)
    assert SYNTHETIC_VALUE not in (ran.stdout + ran.stderr)
    assert _head(workspace) == _head(workspace)  # no new commit landed
    assert subprocess.run(
        ["git", "-C", str(workspace), "log", "--format=%H"],
        capture_output=True, text=True, check=True
    ).stdout.count("\n") == 1  # only the seed commit


def test_a_clean_commit_passes_both_hooks(tmp_path, live_synthetic_map):
    """Non-vacuity the other way: a commit with no map value anywhere,
    including a token-SHAPED string that is not itself a configured
    value, goes through both hooks untouched."""
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    _install(workspace, tmp_path / "hooks")
    (workspace / "notes.txt").write_text(
        f"token shaped like {TOKEN_SHAPED_BUT_UNKNOWN}\n")
    _git(workspace, "add", "notes.txt")

    ran = subprocess.run(
        ["git", "commit", "-m", "docs: a clean note about token shapes"],
        cwd=str(workspace), capture_output=True, text=True)

    assert ran.returncode == 0, ran.stdout + ran.stderr


# ---------------------------------------------------------------------------
# The gate is controlled by one config field, shipped ON.
# ---------------------------------------------------------------------------

def test_the_shipped_default_turns_the_gate_on():
    cfg = JobConfig(run_id="hooks-default")
    assert cfg.commit_secret_scan is True


def test_the_config_field_gates_hook_install_too(tmp_path):
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    _init_repo(workspace)
    cfg = JobConfig(run_id="hooks-off", commit_secret_scan=False)
    sup = LoopSupervisor(cfg, run, workspace)

    assert sup._install_commit_scan_push_gate(_head(workspace)) is None
    assert subprocess.run(
        ["git", "-C", str(workspace), "config", "core.hooksPath"],
        capture_output=True, text=True).returncode != 0


# ---------------------------------------------------------------------------
# `--no-verify` bypasses the hooks; the task-boundary scan (029) still
# catches the exact same commit.
# ---------------------------------------------------------------------------

def _sup(tmp_path: Path, name: str, **cfg_kw) -> tuple[LoopSupervisor, RunDir, Path]:
    root = tmp_path / name / "run"
    workspace = tmp_path / name / "workspace"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "031t", "title": "the task this worker iteration was assigned",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    kw = {"iterations": 1, "max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id=f"task031-{name}", **kw)
    sup = LoopSupervisor(cfg, run, workspace)
    sup._approach = 1
    return sup, run, workspace


def _events(run: RunDir, type_: str) -> list[dict]:
    path = run.root / "events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in events if e.get("type") == type_]


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


class _NoVerifyCommittingWorker:
    """A worker iteration that commits a leak with `git commit --no-verify`
    -- exactly what a real worker shell can do, and the one thing the two
    hooks installed by task 031 cannot see at all."""

    def __init__(self, workspace: Path, name: str, content: str, message: str,
                 final_text: str):
        self.workspace = workspace
        self.name = name
        self.content = content
        self.message = message
        self.final_text = final_text
        self.calls = 0
        self.sha: str | None = None

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        if self.calls == 0:
            path = self.workspace / self.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.content)
            _git(self.workspace, "add", self.name)
            _git(self.workspace, "commit", "-q", "--no-verify", "-m", self.message)
            self.sha = _head(self.workspace)
        self.calls += 1
        return _ok(self.final_text)


def test_no_verify_bypasses_the_hooks_but_the_task_boundary_still_catches_it(
        tmp_path, live_synthetic_map):
    sup, run, workspace = _sup(tmp_path, "no-verify")
    _init_repo(workspace)
    sup.runner = _NoVerifyCommittingWorker(  # type: ignore[assignment]
        workspace, "diagnostics.log",
        f"connection ok, token={SYNTHETIC_VALUE}\n",
        f"diagnostics: leak {SYNTHETIC_VALUE} via --no-verify",
        json.dumps({"verdict": "no_verdict"}))

    asyncio.run(sup._step_worker())

    # The commit landed locally -- the hooks never ran at all.
    assert sup.runner.sha is not None

    # ...and the task-boundary scan from 029 still caught it.
    hits = _events(run, LoopSupervisor.COMMIT_SECRET_SCAN_EVENT)
    assert len(hits) == 1, "the task boundary never scanned the --no-verify commit"
    assert hits[0]["variable"] == SYNTHETIC_NAME
    assert hits[0]["sha"] == sup.runner.sha
    assert hits[0]["pushed"] is False
    recorded = sup.run.read_status().get(LoopSupervisor.COMMIT_SECRET_SCAN_STATUS_KEY)
    assert recorded is not None
    assert SYNTHETIC_VALUE not in json.dumps(hits[0])
    assert SYNTHETIC_VALUE not in json.dumps(recorded)
