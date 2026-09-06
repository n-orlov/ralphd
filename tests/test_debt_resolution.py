"""Task 007 (PRD requirement A.3, prompt half, #155): show open debt to the
worker and let a worker resolve an item.

Task 006 built the ledger's WRITE path (`record_debt_entries`, the engine
half). This task closes the loop on the prompt side:

  * `prompts/worker.md` carries an `## Open debt` block (like the steering
    ledger) stating resolution is OPTIONAL and bounded -- minutes, inside
    the task's own files -- and that a worker reports `"debt_resolved":
    ["<id>", ...]` beside its completion payload;
  * `transitions.HAT_SCHEMAS["worker"]` accepts that field;
  * `state.resolve_debt_entries` (bound as `RunDir.resolve_debt`) is the
    ONE write path that flips a ledger entry to `resolved`, and
    `LoopSupervisor._apply_worker_debt_resolution` is what calls it,
    stamping `resolvedBy` with the iteration's own workspace commit sha;
  * `LoopSupervisor._debt_ledger_context` -- appended to EVERY hat's
    prompt through `build_prompt`, the SAME mechanism the steering ledger
    already uses -- carries the open ledger into review's context too;
  * `prompts/review.md` gains a paragraph saying these items arrive
    pre-classified as residuals review may confirm or, with evidence,
    promote to blocking, and never rediscovers.

Unit-level throughout, the same style `tests/test_debt_ledger.py` (task
006) and `tests/test_worker_split_proposal.py` (task 021's own optional
worker side channel) already use: the applier methods are called directly
on a `LoopSupervisor` instance rather than driving a full `_step_worker`
loop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ralphd.engine import contracts, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import DEBT_LEDGER_HEADING, LoopSupervisor
from ralphd.engine.state import (
    DEBT_STATUS_OPEN,
    DEBT_STATUS_RESOLVED,
    RunDir,
    atomic_write_json,
    read_debt_entries,
    record_debt_entries,
    resolve_debt_entries,
)

REPO = Path(__file__).resolve().parents[1]
WORKER_MD = (REPO / "src" / "ralphd" / "prompts" / "worker.md").read_text()
REVIEW_MD = (REPO / "src" / "ralphd" / "prompts" / "review.md").read_text()

NOTE_A = {"clause": "exact command text", "observed": "ran a wrapper script",
          "suggested": "name the observable instead of the command"}
NOTE_B = {"clause": "polling cadence", "observed": "hard-coded 4s",
          "suggested": "no criterion should pin a cadence"}


def _supervisor(tmp_path) -> LoopSupervisor:
    run = RunDir(root=tmp_path / "run")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="unit", vigilant=True,
                    infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    return LoopSupervisor(cfg, run, workspace)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_head(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, check=True,
        capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------------------
# 1. The worker prompt block
# ---------------------------------------------------------------------------

def test_worker_md_has_the_open_debt_block():
    assert "## Open debt" in WORKER_MD
    section = WORKER_MD.split("## Open debt", 1)[1].split("## Sandbox safety", 1)[0]
    normalized = " ".join(section.split())
    assert "OPTIONAL" in normalized
    assert "minutes" in normalized
    assert "that task's own files" in normalized
    assert '"debt_resolved"' in normalized


def test_worker_md_debt_block_names_the_report_shape():
    assert '"debt_resolved": [' in WORKER_MD
    assert "ANY verdict value" in WORKER_MD or "no_verdict" in WORKER_MD.split(
        "## Open debt", 1)[1].split("## Sandbox safety", 1)[0]


# ---------------------------------------------------------------------------
# 2. The worker contract accepts debt_resolved
# ---------------------------------------------------------------------------

def test_hat_schemas_worker_declares_optional_debt_resolved_array():
    schema = transitions.HAT_SCHEMAS["worker"]
    assert schema["properties"]["debt_resolved"] == {"type": "array"}
    assert "debt_resolved" not in schema["required"]


def test_a_worker_payload_with_debt_resolved_validates_clean():
    errors = contracts.validate_payload("worker", {
        "verdict": "complete", "task_id": "021",
        "debt_resolved": ["006/1", "007/2"]})
    assert errors == []


def test_a_non_array_debt_resolved_is_a_schema_violation():
    errors = contracts.validate_payload(
        "worker", {"verdict": "no_verdict", "debt_resolved": "006/1"})
    assert errors


# ---------------------------------------------------------------------------
# 3. state.resolve_debt_entries -- the one write path for the flip
# ---------------------------------------------------------------------------

def test_resolve_debt_entries_flips_open_to_resolved(tmp_path):
    record_debt_entries(tmp_path, "006", [NOTE_A, NOTE_B])
    ids = [e["id"] for e in read_debt_entries(tmp_path)]

    flipped = resolve_debt_entries(tmp_path, [ids[0]], "abc1234")

    assert flipped == [ids[0]]
    entries = read_debt_entries(tmp_path)
    a, b = entries
    assert a["status"] == DEBT_STATUS_RESOLVED
    assert a["resolvedBy"] == "abc1234"
    assert b["status"] == DEBT_STATUS_OPEN
    assert b["resolvedBy"] is None


def test_resolve_debt_entries_ignores_unknown_and_already_resolved_ids(tmp_path):
    record_debt_entries(tmp_path, "006", [NOTE_A])
    debt_id = read_debt_entries(tmp_path)[0]["id"]
    resolve_debt_entries(tmp_path, [debt_id], "sha1")

    flipped_again = resolve_debt_entries(tmp_path, [debt_id, "nope/9"], "sha2")

    assert flipped_again == []
    entries = read_debt_entries(tmp_path)
    assert entries[0]["resolvedBy"] == "sha1"  # unchanged by the second call


def test_resolve_debt_entries_is_a_noop_for_empty_or_non_list_ids(tmp_path):
    record_debt_entries(tmp_path, "006", [NOTE_A])
    before = read_debt_entries(tmp_path)
    assert resolve_debt_entries(tmp_path, [], "sha") == []
    assert resolve_debt_entries(tmp_path, None, "sha") == []
    assert read_debt_entries(tmp_path) == before


def test_resolve_debt_entries_keeps_debt_json_and_debt_md_agreeing(tmp_path):
    record_debt_entries(tmp_path, "006", [NOTE_A])
    debt_id = read_debt_entries(tmp_path)[0]["id"]
    resolve_debt_entries(tmp_path, [debt_id], "task 008")
    md = (tmp_path / "debt.md").read_text()
    assert "resolved by task 008" in md
    assert "## Open (0)" in md


# ---------------------------------------------------------------------------
# 4. LoopSupervisor._apply_worker_debt_resolution -- the caller
# ---------------------------------------------------------------------------

def test_apply_worker_debt_resolution_flips_and_stamps_commit_sha(tmp_path):
    sup = _supervisor(tmp_path)
    record_debt_entries(sup.run.root, "006", [NOTE_A])
    debt_id = read_debt_entries(sup.run.root)[0]["id"]

    _git(sup.workspace, "init", "-q")
    _git(sup.workspace, "config", "user.email", "t@example.com")
    _git(sup.workspace, "config", "user.name", "t")
    (sup.workspace / "f.txt").write_text("x")
    _git(sup.workspace, "add", "-A")
    _git(sup.workspace, "commit", "-q", "-m", "c")
    sha = _git_head(sup.workspace)

    flipped = sup._apply_worker_debt_resolution([debt_id])

    assert flipped == [debt_id]
    entry = read_debt_entries(sup.run.root)[0]
    assert entry["status"] == DEBT_STATUS_RESOLVED
    assert entry["resolvedBy"] == sha


def test_apply_worker_debt_resolution_degrades_to_unknown_without_git(tmp_path):
    sup = _supervisor(tmp_path)  # workspace is a plain, non-git directory
    record_debt_entries(sup.run.root, "006", [NOTE_A])
    debt_id = read_debt_entries(sup.run.root)[0]["id"]

    flipped = sup._apply_worker_debt_resolution([debt_id])

    assert flipped == [debt_id]
    assert read_debt_entries(sup.run.root)[0]["resolvedBy"] == "unknown"


def test_apply_worker_debt_resolution_ignores_malformed_input(tmp_path):
    sup = _supervisor(tmp_path)
    record_debt_entries(sup.run.root, "006", [NOTE_A])
    assert sup._apply_worker_debt_resolution([]) == []
    assert sup._apply_worker_debt_resolution(None) == []
    assert sup._apply_worker_debt_resolution("006/1") == []
    assert sup._apply_worker_debt_resolution([123, None, "  "]) == []
    assert read_debt_entries(sup.run.root)[0]["status"] == DEBT_STATUS_OPEN


# ---------------------------------------------------------------------------
# 5. Review's (and every hat's) prompt context carries the open ledger
# ---------------------------------------------------------------------------

def test_debt_ledger_context_lists_open_entries_with_the_heading(tmp_path):
    sup = _supervisor(tmp_path)
    record_debt_entries(sup.run.root, "006", [NOTE_A])
    debt_id = read_debt_entries(sup.run.root)[0]["id"]

    text = sup._debt_ledger_context()

    assert DEBT_LEDGER_HEADING in text
    assert debt_id in text
    assert NOTE_A["clause"] in text


def test_debt_ledger_context_omits_resolved_entries(tmp_path):
    sup = _supervisor(tmp_path)
    record_debt_entries(sup.run.root, "006", [NOTE_A])
    debt_id = read_debt_entries(sup.run.root)[0]["id"]
    resolve_debt_entries(sup.run.root, [debt_id], "abc1234")

    assert sup._debt_ledger_context() == ""


def test_debt_ledger_context_empty_when_the_ledger_is_empty(tmp_path):
    sup = _supervisor(tmp_path)
    assert sup._debt_ledger_context() == ""


def test_review_prompt_receives_the_open_ledger(tmp_path):
    sup = _supervisor(tmp_path)
    record_debt_entries(sup.run.root, "006", [NOTE_A])

    prompt = sup.build_prompt("review")

    assert DEBT_LEDGER_HEADING in prompt
    assert NOTE_A["clause"] in prompt


def test_worker_prompt_receives_the_open_ledger_too(tmp_path):
    sup = _supervisor(tmp_path)
    record_debt_entries(sup.run.root, "006", [NOTE_A])

    prompt = sup.build_prompt("worker")

    assert DEBT_LEDGER_HEADING in prompt
    assert NOTE_A["clause"] in prompt


# ---------------------------------------------------------------------------
# 6. A lexical pin on review.md's own paragraph
# ---------------------------------------------------------------------------

def test_review_md_has_the_pre_classified_residuals_paragraph():
    assert "## Open debt ledger" in REVIEW_MD
    section = REVIEW_MD.split("## Open debt ledger", 1)[1].split(
        "## Impossibility petitions", 1)[0]
    assert "PRE-CLASSIFIED as a residual" in section
    assert "confirm the\nclassification" in section or "confirm" in section
    assert "promote it to blocking" in section
    assert "never to rediscover it from scratch" in section
