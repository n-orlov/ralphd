"""Task 033 (PRD requirement I.6, filed in `tasks.json` under the label
"I.5"): the regression-coverage guard for the git-sink bound (#210),
landed in the `tests/*_guard.py` idiom that `scripts/final_sha_guards.sh`
discovers by grepping `tests/test_*.py` for a mention of `docs`,
`artifacts` or `README` as a word -- see that script's own header for the
discovery rule, and `docs/prds/` for this requirement's own text.

`tests/test_commit_hooks.py` and `tests/test_commit_scan_engine_boundary.py`
already exercise every layer of this bound in depth (tasks 029-031); this
module is the narrow, independently-discoverable slice PRD requirement I.6
asks for -- exactly the five cases named there, one test function each:

1. a commit whose message contains a redaction-map value is refused;
2. the same attempt made with `--no-verify` is caught at the task
   boundary instead;
3. a commit carrying only variable NAMES (no value) is accepted;
4. a fixture containing a synthetic token-SHAPED string (never itself a
   configured value) is accepted;
5. the refusal text never contains the matched value.

Every value used here is SYNTHETIC (PRD requirement I.9) -- constructed
for this module alone, never a real credential, and never one read off
this run's own environment.
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

# Synthetic only -- never a real credential (PRD requirement I.9). Chosen
# to look nothing like the real 49-character AWS_BEARER_TOKEN_BEDROCK
# value the incident this whole requirement answers actually leaked.
SYNTHETIC_VALUE = "sk-synthetic-guard-9988776655zzzzzzGUARD"
SYNTHETIC_NAME = "SYNTHETIC_GUARD_API_TOKEN"

# Shape-alike but never itself a configured value -- the shape-vs-value
# line PRD requirement I.4 draws.
TOKEN_SHAPED_BUT_UNKNOWN = "sk-lookalike-guard-1122334455qqqqqqSHAPE"


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


def _install(workspace: Path, hooks_dir: Path) -> Path:
    hook = commit_scan.install_push_gate(workspace, hooks_dir)
    assert hook is not None
    return hook


# ---------------------------------------------------------------------------
# Case 1: a commit whose message contains a redaction-map value is refused.
# ---------------------------------------------------------------------------

def test_a_commit_message_containing_a_redaction_map_value_is_refused(
        tmp_path, live_synthetic_map):
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    _install(workspace, tmp_path / "hooks")
    (workspace / "leak.txt").write_text("nothing interesting here\n")
    _git(workspace, "add", "leak.txt")

    ran = subprocess.run(
        ["git", "commit", "-m", f"chore: leak {SYNTHETIC_VALUE} in the message"],
        cwd=str(workspace), capture_output=True, text=True)

    assert ran.returncode != 0, "the leaking commit message was not refused"
    assert _head(workspace) == _head(workspace)  # only the seed commit landed
    assert subprocess.run(
        ["git", "-C", str(workspace), "log", "--format=%H"],
        capture_output=True, text=True, check=True,
    ).stdout.count("\n") == 1


# ---------------------------------------------------------------------------
# Case 2: the same attempt with `--no-verify` is caught at the task
# boundary instead (029/030's authoritative layer, which `--no-verify`
# cannot bypass).
# ---------------------------------------------------------------------------

def _sup(tmp_path: Path) -> tuple[LoopSupervisor, RunDir, Path]:
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "033t", "title": "the task this worker iteration was assigned",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    cfg = JobConfig(
        run_id="git-sink-guard", iterations=1, max_approaches=3,
        vigilant=False, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0)
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
    """A worker iteration that commits a leak with `git commit
    --no-verify` -- exactly what a real worker shell can do, and the one
    thing neither hook installed by task 031 can ever see."""

    def __init__(self, workspace: Path, content: str, message: str, final_text: str):
        self.workspace = workspace
        self.content = content
        self.message = message
        self.final_text = final_text
        self.calls = 0
        self.sha: str | None = None

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        if self.calls == 0:
            path = self.workspace / "diagnostics.log"
            path.write_text(self.content)
            _git(self.workspace, "add", "diagnostics.log")
            _git(self.workspace, "commit", "-q", "--no-verify", "-m", self.message)
            self.sha = _head(self.workspace)
        self.calls += 1
        return _ok(self.final_text)


def test_the_same_attempt_with_no_verify_is_caught_at_the_task_boundary(
        tmp_path, live_synthetic_map):
    sup, run, workspace = _sup(tmp_path)
    _init_repo(workspace)
    sup.runner = _NoVerifyCommittingWorker(  # type: ignore[assignment]
        workspace, f"connection ok, token={SYNTHETIC_VALUE}\n",
        f"diagnostics: leak {SYNTHETIC_VALUE} via --no-verify",
        json.dumps({"verdict": "no_verdict"}))

    asyncio.run(sup._step_worker())

    # The commit landed locally -- no hook was installed for this
    # supervisor, and even if one had been, --no-verify bypasses it.
    assert sup.runner.sha is not None

    hits = _events(run, LoopSupervisor.COMMIT_SECRET_SCAN_EVENT)
    assert len(hits) == 1, "the task boundary never scanned the --no-verify commit"
    assert hits[0]["variable"] == SYNTHETIC_NAME
    assert hits[0]["sha"] == sup.runner.sha
    assert hits[0]["pushed"] is False


# ---------------------------------------------------------------------------
# Case 3: a commit carrying only variable NAMES (no value) is accepted.
# ---------------------------------------------------------------------------

def test_a_commit_carrying_only_variable_names_is_accepted(
        tmp_path, live_synthetic_map):
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    _install(workspace, tmp_path / "hooks")
    (workspace / "notes.txt").write_text(
        f"failure shape: {SYNTHETIC_NAME} was unset when the worker ran\n")
    _git(workspace, "add", "notes.txt")

    ran = subprocess.run(
        ["git", "commit", "-m",
         f"docs: {SYNTHETIC_NAME} is the variable name, never its value"],
        cwd=str(workspace), capture_output=True, text=True)

    assert ran.returncode == 0, ran.stdout + ran.stderr


# ---------------------------------------------------------------------------
# Case 4: a fixture containing a synthetic token-SHAPED string (never
# itself a configured value) is accepted.
# ---------------------------------------------------------------------------

def test_a_fixture_containing_a_synthetic_token_shaped_string_is_accepted(
        tmp_path, live_synthetic_map):
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    _install(workspace, tmp_path / "hooks")
    (workspace / "fixtures.txt").write_text(
        f"a token looks like {TOKEN_SHAPED_BUT_UNKNOWN}\n")
    _git(workspace, "add", "fixtures.txt")

    ran = subprocess.run(
        ["git", "commit", "-m", "tests: a token-shaped fixture, never a real value"],
        cwd=str(workspace), capture_output=True, text=True)

    assert ran.returncode == 0, ran.stdout + ran.stderr


# ---------------------------------------------------------------------------
# Case 5: the refusal text never contains the matched value.
# ---------------------------------------------------------------------------

def test_the_refusal_text_never_contains_the_value(tmp_path, live_synthetic_map):
    workspace = tmp_path / "ws"
    _init_repo(workspace)
    _install(workspace, tmp_path / "hooks")
    (workspace / "leak.txt").write_text("nothing interesting here\n")
    _git(workspace, "add", "leak.txt")

    ran = subprocess.run(
        ["git", "commit", "-m", f"chore: leak {SYNTHETIC_VALUE} in the message"],
        cwd=str(workspace), capture_output=True, text=True)

    assert ran.returncode != 0
    refusal = ran.stdout + ran.stderr
    assert SYNTHETIC_NAME in refusal, "the refusal never named the variable"
    assert SYNTHETIC_VALUE not in refusal, "the refusal text leaked the matched value"
