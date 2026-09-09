"""Task 012 (PRD requirement A.2, #156): "the corroboration" -- the second
of the five #156 stages. Task 011 accepted the worker contract's `progress`
claim shape and logged it; this task makes the claim MEAN something.

The engine fingerprints the workspace tree (content hashes over tracked and
untracked-but-not-ignored files, git-agnostic) immediately before and after
each worker iteration -- `LoopSupervisor._workspace_fingerprint`, the same
before/after bracketing `_workspace_head_sha` already uses for the git-HEAD
stagnation witness. `commit`/`file` evidence is corroborated iff the
fingerprint changed between the two snapshots; `external` evidence is
honoured at face value once consecutively -- a SECOND consecutive
external-only claim with no tree delta is uncorroborated
(`LoopSupervisor._progress_external_streak`, reset the moment the task
changes, a real delta lands, or the claim isn't external-only). Every
accepted claim records a `progress_claim` event carrying `taskId`,
`corroborated` and `deltaSize` (`_apply_worker_progress_report`).

Deliberately out of scope, same split task 011's own docstring already
drew: the per-task progress budget and the stagnation-counter exemption for
a corroborated claim are task 013's job, not this one's -- this task only
computes and records `corroborated`/`deltaSize`, it does not yet consume
them for anything beyond the record itself.

Three behaviours, each driven through the REAL production call site
(`LoopSupervisor._step_worker`, a stubbed `sup.runner` only, exactly the
style `tests/test_worker_progress_claim.py` already uses for the claim's
acceptance):

- the git-less tree: `_workspace_fingerprint` works in a workspace with no
  `.git` at all, and a full worker iteration through that workspace still
  records a `progress_claim`;
- the corroborated case: a worker iteration that actually writes a file to
  the workspace (standing in for "made a commit"/"touched a file") between
  the fingerprint's before/after snapshots is corroborated, with a nonzero
  `deltaSize`;
- the two-external case: two consecutive iterations on the same task, each
  claiming only `external` evidence with no tree delta -- the first is
  corroborated at face value, the second is not.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


class _ScriptedWorker:
    """Same shape `test_worker_progress_claim.py`'s own `_ScriptedWorker`
    uses: every call advances through `script`, the last entry repeating
    forever. Never touches the filesystem -- for the git-less and
    two-external cases, where no real tree delta is meant to occur."""

    def __init__(self, script: list[IterationResult]):
        self.script = script
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step


class _FileWritingWorker(_ScriptedWorker):
    """Same script-advancing shape, but the FIRST call also writes a file
    into the workspace before returning -- standing in for the real commit/
    file-touch a worker would make, so the before/after fingerprint this
    task adds actually sees a tree delta to corroborate against."""

    def __init__(self, script: list[IterationResult], workspace: Path,
                 filename: str = "progress_evidence.txt"):
        super().__init__(script)
        self.workspace = workspace
        self.filename = filename

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        if self.calls == 0:
            (self.workspace / self.filename).write_text("touched\n")
        return await super().run(prompt, transcript, **kw)


def _worker_sup(tmp_path: Path, name: str, **cfg_kw) -> tuple[LoopSupervisor, RunDir]:
    # Task 012 (PRD requirement A.2, #156): the run dir and the workspace
    # are DIFFERENT directories in production (SPEC.md §3.3 -- `runs/<id>/`
    # vs. the job's own code checkout), and this task's fingerprint reads
    # `self.workspace` specifically -- sharing one directory for both
    # (`test_worker_progress_claim.py`'s own `_worker_sup`, fine for a task
    # that never reads the tree) would have the fingerprint see the run
    # dir's OWN bookkeeping writes (`tasks.json`, `events.jsonl`,
    # `iterations/`) as tree "progress" that no worker made.
    root = tmp_path / name / "run"
    workspace = tmp_path / name / "workspace"
    root.mkdir(parents=True)
    workspace.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "012t", "title": "the task this worker iteration was assigned",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    kw = {"iterations": 1, "max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id=f"task012-{name}", **kw)
    sup = LoopSupervisor(cfg, run, workspace)
    sup._approach = 1
    return sup, run


def _events_of_type(run: RunDir, type_: str) -> list[dict]:
    events = [json.loads(x) for x in
              (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    return [e for e in events if e.get("type") == type_]


COMMIT_PROGRESS = {
    "summary": "wrote a file, corroborated by a real tree delta",
    "evidence": [{"kind": "commit", "ref": "abc1234"}],
}
EXTERNAL_PROGRESS = {
    "summary": "checked the deploy dashboard, nothing to commit yet",
    "evidence": [{"kind": "external", "ref": "watched the pipeline run"}],
}


# ---------------------------------------------------------------------------
# The git-less tree
# ---------------------------------------------------------------------------


def test_workspace_fingerprint_works_with_no_git_directory(tmp_path):
    sup, _run = _worker_sup(tmp_path, "gitless")
    assert not (sup.workspace / ".git").exists()
    (sup.workspace / "a.txt").write_text("hello\n")
    sub = sup.workspace / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world\n")

    fp = sup._workspace_fingerprint()

    assert "a.txt" in fp
    assert str(Path("sub") / "b.txt") in fp
    # A stable content hash, not merely a presence marker: re-fingerprinting
    # an untouched tree is byte-for-byte the same.
    assert fp == sup._workspace_fingerprint()


def test_a_worker_iteration_in_a_git_less_workspace_still_records_a_progress_claim(tmp_path):
    sup, run = _worker_sup(tmp_path, "gitless-iteration")
    assert not (sup.workspace / ".git").exists()
    payload_text = json.dumps({"verdict": "no_verdict", "progress": EXTERNAL_PROGRESS})
    sup.runner = _ScriptedWorker([_ok(payload_text)])  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 1
    assert claims[0]["taskId"] == "012t"
    assert claims[0]["deltaSize"] == 0
    assert claims[0]["corroborated"] is True  # first external-only claim


# ---------------------------------------------------------------------------
# The corroborated case: a real tree delta backs commit/file evidence
# ---------------------------------------------------------------------------


def test_commit_evidence_with_a_real_tree_delta_is_corroborated(tmp_path):
    sup, run = _worker_sup(tmp_path, "corroborated")
    payload_text = json.dumps({"verdict": "no_verdict", "progress": COMMIT_PROGRESS})
    sup.runner = _FileWritingWorker(  # type: ignore[assignment]
        [_ok(payload_text)], sup.workspace)

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 1
    assert claims[0]["corroborated"] is True
    assert claims[0]["deltaSize"] >= 1


def test_commit_evidence_with_no_tree_delta_is_not_corroborated(tmp_path):
    sup, run = _worker_sup(tmp_path, "uncorroborated-commit")
    payload_text = json.dumps({"verdict": "no_verdict", "progress": COMMIT_PROGRESS})
    sup.runner = _ScriptedWorker([_ok(payload_text)])  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 1
    assert claims[0]["corroborated"] is False
    assert claims[0]["deltaSize"] == 0


# ---------------------------------------------------------------------------
# The two-external case: honoured once consecutively, not twice
# ---------------------------------------------------------------------------


def test_two_consecutive_external_only_claims_only_the_first_is_corroborated(tmp_path):
    sup, run = _worker_sup(tmp_path, "two-external", iterations=2)
    payload_text = json.dumps({"verdict": "no_verdict", "progress": EXTERNAL_PROGRESS})
    sup.runner = _ScriptedWorker([_ok(payload_text)])  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 2
    assert [c["taskId"] for c in claims] == ["012t", "012t"]
    assert [c["deltaSize"] for c in claims] == [0, 0]
    assert claims[0]["corroborated"] is True
    assert claims[1]["corroborated"] is False
