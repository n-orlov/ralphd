"""Task 029 (PRD requirement I.2's content, filed as "I.1" in `tasks.json`):
the ENGINE's own task boundary scans the commits this run created.

`tests/test_commit_scan_boundary.py` pins the detector and the push gate as a
module (`ralphd.engine.commit_scan`). This module pins the thing the
requirement actually asks for: that `LoopSupervisor` -- the engine running a
real worker iteration -- reaches that scan at the task boundary, over exactly
the commits THAT iteration created (`head_before..head_after`, the bracketing
`_step_worker` already takes for its progress witness), and that on a hit no
push subprocess is ever started.

Everything here uses SYNTHETIC secret values only (PRD requirement I.9), and
reaches the live redaction map the way production does -- an env var with a
secret-shaped NAME plus `redact.refresh_redaction_map(home=<a fake home>)`,
so no real credential file is ever read, let alone matched. Nothing here ever
writes a matched value into an assertion message.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from ralphd.engine import commit_scan, redact
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

SYNTHETIC_VALUE = "sk-synthetic-engine-0123456789abcdefSYNTH"
SYNTHETIC_NAME = "SYNTHETIC_ENGINE_API_TOKEN"

# A string that LOOKS like a secret (long, mixed-case, `sk-`-shaped) but is
# never itself a configured value -- the shape-vs-value distinction PRD
# requirement I.4 draws, carried through the engine boundary here.
TOKEN_SHAPED_BUT_UNKNOWN = "sk-lookalike-8888888888yyyyyySHAPE"

# One of the three corpora this task's criteria name: this contract's own PRD
# text (requirement I.4's literal wording), which must pass unflagged.
PRD_I4_TEXT = (
    "**I.4 \u2014 the matcher keys on values, never on shapes.** It matches "
    "actual redaction-map values. It must\n"
    "**not** match a \"looks like a token\" pattern. A diff or a doc that "
    "merely discusses a token's shape, a\n"
    "test fixture containing a synthetic value, and this very PRD must all "
    "pass. Name these as tests, not as\n"
    "hopes.\n"
)


@pytest.fixture
def live_synthetic_map(monkeypatch, tmp_path):
    """Make one SYNTHETIC value live in the engine's real in-memory redaction
    map, the same way production does (`refresh_redaction_map`, called at
    engine startup after creds placement) -- via an env var whose NAME is
    secret-shaped. `home` points at an empty scratch directory so the refresh
    reads NO real `~/.creds/*.env`, `~/.git-credentials` or `~/.netrc` (PRD
    requirement I.9: synthetic values only, never a real credential read to
    test the matcher). Restored on the way out, the idiom
    `tests/test_self_inflicted_abort.py` already uses.
    """
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


def _commit(workspace: Path, name: str, content: str, message: str) -> str:
    path = workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(workspace, "add", name)
    _git(workspace, "commit", "-q", "-m", message)
    return _head(workspace)


def _sup(tmp_path: Path, name: str, **cfg_kw) -> tuple[LoopSupervisor, RunDir, Path]:
    """A supervisor over a REAL git workspace, with the run dir and the
    workspace as separate directories (as in production, SPEC.md 3.3)."""
    root = tmp_path / name / "run"
    workspace = tmp_path / name / "workspace"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "029t", "title": "the task this worker iteration was assigned",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    kw = {"iterations": 1, "max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id=f"task029-{name}", **kw)
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


class _CommittingWorker:
    """A worker iteration that makes a REAL commit in the workspace before it
    returns -- the production shape of "the commits this run created", landing
    strictly between `_step_worker`'s `head_before` and `head_after` reads."""

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
            self.sha = _commit(self.workspace, self.name, self.content, self.message)
        self.calls += 1
        return _ok(self.final_text)


# ---------------------------------------------------------------------------
# The engine boundary, driven through the REAL production call site
# (`LoopSupervisor._step_worker`): a worker iteration that commits a leak.
# ---------------------------------------------------------------------------

def test_a_real_worker_iterations_leaking_commit_is_caught_at_the_task_boundary(
        tmp_path, live_synthetic_map):
    sup, run, workspace = _sup(tmp_path, "step-worker")
    _init_repo(workspace)
    sup.runner = _CommittingWorker(  # type: ignore[assignment]
        workspace, "diagnostics.log",
        f"connection ok, token={SYNTHETIC_VALUE}\n",
        "diagnostics: capture what the retry saw",
        json.dumps({"verdict": "no_verdict"}))

    asyncio.run(sup._step_worker())

    hits = _events(run, LoopSupervisor.COMMIT_SECRET_SCAN_EVENT)
    assert len(hits) == 1, "the engine's task boundary never scanned the commit"
    assert hits[0]["variable"] == SYNTHETIC_NAME
    assert hits[0]["part"] == "diff"
    assert hits[0]["sha"] == sup.runner.sha
    assert hits[0]["pushed"] is False
    recorded = sup.run.read_status().get(
        LoopSupervisor.COMMIT_SECRET_SCAN_STATUS_KEY)
    assert recorded is not None, "no commitSecretScanHit recorded in status.json"
    assert recorded["variable"] == SYNTHETIC_NAME
    # The whole event/status record, serialised, never carries the value.
    assert SYNTHETIC_VALUE not in json.dumps(hits[0])
    assert SYNTHETIC_VALUE not in json.dumps(recorded)


def test_a_real_worker_iterations_clean_commit_leaves_the_boundary_silent(
        tmp_path, live_synthetic_map):
    sup, run, workspace = _sup(tmp_path, "step-worker-clean")
    _init_repo(workspace)
    sup.runner = _CommittingWorker(  # type: ignore[assignment]
        workspace, "notes.md", "nothing secret here\n",
        "docs: write a note", json.dumps({"verdict": "no_verdict"}))

    asyncio.run(sup._step_worker())

    assert _events(run, LoopSupervisor.COMMIT_SECRET_SCAN_EVENT) == []
    assert sup.run.read_status().get(
        LoopSupervisor.COMMIT_SECRET_SCAN_STATUS_KEY) is None


# ---------------------------------------------------------------------------
# The two hits the criteria name, at the engine boundary: a commit MESSAGE and
# a staged (then committed) DIFF.
# ---------------------------------------------------------------------------

def test_the_boundary_catches_a_map_value_in_a_commit_message(
        tmp_path, live_synthetic_map):
    sup, _run, workspace = _sup(tmp_path, "message-hit")
    base = _init_repo(workspace)
    head = _commit(workspace, "notes.txt", "nothing secret in the file\n",
                   f"debug: dumped env, {SYNTHETIC_VALUE} looked fine to me")

    hit = sup._scan_task_boundary_commits(base, head)

    assert hit is not None
    assert hit["part"] == "message"
    assert hit["variable"] == SYNTHETIC_NAME
    assert hit["sha"] == head


def test_the_boundary_catches_a_map_value_in_a_staged_diff(
        tmp_path, live_synthetic_map):
    sup, _run, workspace = _sup(tmp_path, "diff-hit")
    base = _init_repo(workspace)
    # The value lands only in the file content that was staged and committed;
    # the commit message names nothing.
    head = _commit(workspace, "diagnostics.log",
                   f"connection ok, token={SYNTHETIC_VALUE}\n",
                   "diagnostics: capture what the retry saw")

    hit = sup._scan_task_boundary_commits(base, head)

    assert hit is not None
    assert hit["part"] == "diff"
    assert hit["variable"] == SYNTHETIC_NAME
    assert hit["sha"] == head


# ---------------------------------------------------------------------------
# The three corpora that must pass UNFLAGGED, through the engine boundary.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,filename,content,message", [
    ("shape-talk", "shape-notes.md",
     "Tokens from this provider look like `sk-` followed by 32 hex "
     "characters; they are never printed here.\n",
     "docs: describe the provider's token shape"),
    ("fixture", "tests/fixture_data.py",
     f'SAMPLE_TOKEN = "{TOKEN_SHAPED_BUT_UNKNOWN}"\n',
     "tests: add a synthetic token-shaped fixture value"),
    ("prd", "docs/requirement-i4.md", PRD_I4_TEXT,
     "docs: land requirement I.4's own text"),
])
def test_the_boundary_passes_the_three_named_corpora_unflagged(
        tmp_path, live_synthetic_map, name, filename, content, message):
    sup, run, workspace = _sup(tmp_path, f"unflagged-{name}")
    base = _init_repo(workspace)
    head = _commit(workspace, filename, content, message)

    assert sup._scan_task_boundary_commits(base, head) is None
    assert _events(run, LoopSupervisor.COMMIT_SECRET_SCAN_EVENT) == []
    assert sup.run.read_status().get(
        LoopSupervisor.COMMIT_SECRET_SCAN_STATUS_KEY) is None


# ---------------------------------------------------------------------------
# On a hit the run does not push -- observed as the absence of a push
# subprocess, with the clean case proving the observation is not vacuous.
# ---------------------------------------------------------------------------

def _push_spy():
    """Record every git argv the boundary starts, and never let a real `git
    push` reach a network/remote."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _spy(args, **kwargs):
        calls.append([str(a) for a in args])
        if "push" in [str(a) for a in args]:
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return real_run(args, **kwargs)

    return calls, _spy


def test_on_a_hit_the_boundary_starts_no_push_subprocess(
        tmp_path, live_synthetic_map):
    # The engine is configured to own the push here (`workspace_push_remote`),
    # so a push subprocess is exactly what WOULD happen on a clean scan --
    # which is what makes its absence on a hit a real observation.
    sup, run, workspace = _sup(tmp_path, "hit-no-push",
                               workspace_push_remote="origin",
                               workspace_push_branch="main")
    base = _init_repo(workspace)
    head = _commit(workspace, "leak.txt", f"oops: {SYNTHETIC_VALUE}\n",
                   "chore: paste a diagnostic value")
    calls, spy = _push_spy()

    with mock.patch("ralphd.engine.commit_scan.subprocess.run", side_effect=spy):
        hit = sup._scan_task_boundary_commits(base, head)

    assert hit is not None
    assert not any("push" in call for call in calls), (
        "a push subprocess was started despite the hit")
    assert _events(run, LoopSupervisor.COMMIT_SECRET_SCAN_EVENT)[0]["pushed"] is False


def test_on_a_clean_scan_the_boundary_does_push_exactly_once(
        tmp_path, live_synthetic_map):
    sup, _run, workspace = _sup(tmp_path, "clean-push",
                                workspace_push_remote="origin",
                                workspace_push_branch="main")
    base = _init_repo(workspace)
    head = _commit(workspace, "clean.txt", "nothing secret here\n",
                   "chore: clean commit")
    calls, spy = _push_spy()

    with mock.patch("ralphd.engine.commit_scan.subprocess.run", side_effect=spy):
        assert sup._scan_task_boundary_commits(base, head) is None

    pushes = [c for c in calls if "push" in c]
    assert len(pushes) == 1
    assert pushes[0][-2:] == ["origin", "main"]


def test_with_no_push_remote_configured_the_boundary_pushes_nothing_either_way(
        tmp_path, live_synthetic_map):
    """The shipped default (`workspace_push_remote is None`): the WORKER
    pushes its own commit from its own shell, so the engine's boundary only
    ever scans and records -- it starts no push subprocess even on a clean
    scan."""
    sup, _run, workspace = _sup(tmp_path, "no-remote")
    base = _init_repo(workspace)
    head = _commit(workspace, "clean.txt", "nothing secret here\n",
                   "chore: clean commit")
    calls, spy = _push_spy()

    with mock.patch("ralphd.engine.commit_scan.subprocess.run", side_effect=spy):
        assert sup._scan_task_boundary_commits(base, head) is None

    assert JobConfig().workspace_push_remote is None
    assert not any("push" in call for call in calls)


# ---------------------------------------------------------------------------
# The refusal text: the variable NAME only, never the matched value.
# ---------------------------------------------------------------------------

def test_the_boundarys_refusal_names_the_variable_only_and_never_the_value(
        tmp_path, live_synthetic_map):
    sup, run, workspace = _sup(tmp_path, "refusal-text")
    base = _init_repo(workspace)
    head = _commit(workspace, "leak2.txt", f"leaked={SYNTHETIC_VALUE}\n",
                   "chore: another leak")

    assert sup._scan_task_boundary_commits(base, head) is not None

    refusal = sup.run.read_status()[
        LoopSupervisor.COMMIT_SECRET_SCAN_STATUS_KEY]["refusal"]
    assert SYNTHETIC_VALUE not in refusal
    assert SYNTHETIC_NAME in refusal
    # The label's source prefix is stripped too -- the variable NAME only.
    assert "env:" not in refusal
    assert _events(run, LoopSupervisor.COMMIT_SECRET_SCAN_EVENT)[0]["refusal"] == refusal


def test_the_scan_is_controllable_by_one_named_config_field_defaulting_on(
        tmp_path, live_synthetic_map):
    assert JobConfig().commit_secret_scan is True
    sup, run, workspace = _sup(tmp_path, "scan-off", commit_secret_scan=False)
    base = _init_repo(workspace)
    head = _commit(workspace, "leak3.txt", f"leaked={SYNTHETIC_VALUE}\n",
                   "chore: a leak nobody scans for")

    assert sup._scan_task_boundary_commits(base, head) is None
    assert _events(run, LoopSupervisor.COMMIT_SECRET_SCAN_EVENT) == []


def test_the_boundary_uses_the_live_redaction_map_not_a_rebuilt_one(
        tmp_path, live_synthetic_map, monkeypatch):
    """The engine keys on `redact.live_redaction_map()` -- what
    `refresh_redaction_map` made live in THIS process -- not on a fresh
    `build_redaction_map()` of its own: drop the env var WITHOUT refreshing
    and the still-live value is still caught."""
    sup, _run, workspace = _sup(tmp_path, "live-map")
    base = _init_repo(workspace)
    head = _commit(workspace, "leak4.txt", f"leaked={SYNTHETIC_VALUE}\n",
                   "chore: a leak whose env var is already gone")
    monkeypatch.delenv(SYNTHETIC_NAME, raising=False)
    assert SYNTHETIC_VALUE not in commit_scan.redact.build_redaction_map(
        home=tmp_path / "empty-home")

    hit = sup._scan_task_boundary_commits(base, head)

    assert hit is not None
    assert hit["variable"] == SYNTHETIC_NAME
