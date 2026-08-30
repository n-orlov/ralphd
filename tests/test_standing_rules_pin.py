"""Task 107 (#60 pillar 3): the notes' `## Standing rules` block is pinned
by the harness, supplied once through the planning contract's optional
`standing_rules` field, and re-injected into notes.md after every iteration
-- so an agent that rewrites notes.md wholesale (worker.md's "preserve
verbatim" convention is a request, not an enforcement) can still lose
everything ELSE it wrote, but never this block.

Three layers, each proven independently:

1. `state.reinject_standing_rules` -- the pure splice, no I/O.
2. `RunDir.read_standing_rules`/`write_standing_rules` -- the harness's own
   pinned copy, a file an agent's own writes never touch.
3. A real `LoopSupervisor._run_job_core` drive: a scripted planning phase
   supplies `standing_rules`, a scripted worker phase overwrites notes.md
   wholesale with unrelated content and no heading at all, and the block
   is back, byte-for-byte, by the time the next iteration's prompt is
   built -- while the worker's own content survives exactly as written.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

PINNED = "## Standing rules\n- commit as task NNN: <title>\n- never force-push\n"


# ---------------------------------------------------------------------------
# 1. state.reinject_standing_rules -- the pure splice
# ---------------------------------------------------------------------------

def test_prepends_the_block_when_the_agent_dropped_it_entirely():
    notes = "# Handoff notes\n\nsome free-form worker prose\n"
    out = state.reinject_standing_rules(notes, PINNED)
    assert out.startswith(PINNED.rstrip("\n"))
    assert "some free-form worker prose" in out


def test_replaces_an_existing_block_leaving_everything_else_untouched():
    notes = (
        "# Handoff notes\n\n"
        "## Standing rules\n- an agent's own stale/garbled copy\n\n"
        "## Next steps\nfinish task 999\n"
    )
    out = state.reinject_standing_rules(notes, PINNED)
    assert PINNED.rstrip("\n") in out
    assert "stale/garbled copy" not in out
    assert "## Next steps\nfinish task 999" in out


def test_a_block_at_the_very_end_of_the_file_is_replaced_too():
    notes = "some prose\n\n## Standing rules\n- old rule\n"
    out = state.reinject_standing_rules(notes, PINNED)
    assert PINNED.rstrip("\n") in out
    assert "old rule" not in out
    assert "some prose" in out


def test_is_idempotent_once_the_block_already_matches():
    notes = state.reinject_standing_rules("free text\n", PINNED)
    again = state.reinject_standing_rules(notes, PINNED)
    assert again == notes


# ---------------------------------------------------------------------------
# 2. RunDir's pinned copy
# ---------------------------------------------------------------------------

def test_rundir_round_trips_the_pinned_copy(tmp_path):
    run = RunDir(root=tmp_path)
    assert run.read_standing_rules() == ""  # nothing pinned yet
    run.write_standing_rules(PINNED)
    assert run.read_standing_rules() == PINNED
    # Never surfaced as an operator-editable RUN_DOCUMENTS entry.
    assert state.STANDING_RULES_FILE not in [d[1] for d in state.RUN_DOCUMENTS]


# ---------------------------------------------------------------------------
# 3. a real drive through LoopSupervisor
# ---------------------------------------------------------------------------

def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 10.0
    r.usage = {"input": 5, "output": 5, "totalTokens": 10}
    return r


class _ScriptedAgent:
    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.phases: list[str] = []

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        return self.handlers[phase](self.sup, prompt)


def _plan_with_pinned_rules(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing",
         "successCriteria": "the thing is done"},
    ]})
    payload = {"proposal_path": "plan-proposal.json", "summary": "planned",
               "standing_rules": PINNED}
    return _ok("planned\n" + json.dumps(payload))


def _worker_overwrites_notes_wholesale(sup: LoopSupervisor,
                                       prompt: str = "") -> IterationResult:
    # Blow away notes.md entirely, the way a careless/overwriting agent
    # would, with NO "## Standing rules" heading anywhere in the new text --
    # and report the harness-owned `complete` verdict for the task the
    # scheduler assigned (task 015, requirement O, #100), instead of writing
    # tasks.json directly and ending with the now-retired
    # `<promise>COMPLETE</promise>` marker.
    task_id = sup.run.read_tasks()["tasks"][0]["id"]
    sup.run.notes_file.write_text(
        "# Fresh notes\n\nonly the worker's own content survives here.\n")
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": task_id}))


def _review_verified(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    return _ok("looks right\n" + json.dumps({"verdict": "verified"}))


def _run(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="task107-standing-rules", **kw), run, root)
    sup.runner = _ScriptedAgent(sup, handlers)  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup, asyncio.run(sup._run_job_core())


def test_a_wholesale_notes_overwrite_still_ends_with_the_pinned_block_intact(tmp_path):
    root = tmp_path / "run"
    handlers = {"planning": _plan_with_pinned_rules,
                "worker": _worker_overwrites_notes_wholesale,
                "review": _review_verified}
    sup, final_state = _run(root, handlers)
    assert final_state == "succeeded"
    notes = sup.run.notes_file.read_text()
    # The pinned block survives byte-for-byte...
    assert PINNED.rstrip("\n") in notes
    # ...and the worker's own rewrite is preserved as written, not merged
    # or judged.
    assert "only the worker's own content survives here." in notes
    # The harness's own pinned copy is exactly what the planner supplied.
    assert sup.run.read_standing_rules() == PINNED
