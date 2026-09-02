"""Task 002 (requirement A.2, #119): the widened `_review_contract_ladder`
entry condition -- a review reply with NO parseable contract payload at all
(a prose-only final line, malformed JSON, valid JSON that is not a dict) or
one that IS a dict but fails `review`'s own schema now also rides the same
re-ask ladder every other contract site already has
(`contract_enforcement.enforce_contract`), instead of falling straight
through to `_review_verdict`'s non-verified default with no chance to
correct itself -- mirroring `tests/test_verify_contract_ladder_task001.py`'s
pattern (stubbed `sup.runner`, `infra_retry_backoff_s` zeroed) for the
review site instead of verify's.

Two things pinned here, both driven through a real `_step_review()` --
never `_review_contract_ladder` in isolation -- so that "repaired" and
"exhausted" are checked against the actual downstream consequence (archive
+ composite PRD, or not), not merely against the ladder's own return value:

1. a prose review final line, repaired by the ladder's re-ask into a valid
   `verified` payload, reaches the ordinary VERIFIED exit -- it neither
   archives the approach nor writes a composite PRD for a successor that
   need not exist;
2. ladder exhaustion (the hat never produces a valid payload across every
   attempt `contract_retry_max` grants) still reaches today's ordinary
   archive-with-safe-default path (`rejected_approach_left`): the approach
   IS archived, the composite PRD IS written, and the ladder made exactly
   `1 + sup.cfg.contract_retry_max` calls before giving up.

The engine-side verdict recomputation from blocking findings
(`transitions.review_verdict_findings_errors`) is untouched by this task
and is not re-tested here -- `tests/test_review_verdict_findings.py` already
pins it; reconciling that pre-existing battery with this widened entry is
task 049's deliverable, explicitly out of scope for this module.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=5.0)
    r.final_text = text
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _SequencedRunner:
    """Replays one `IterationResult` per call, in order (the last one
    repeats forever once the sequence runs out) -- same shape as
    `test_verify_contract_ladder_task001.py`'s helper of the same name:
    the charged review call and the uncharged re-ask(s) that follow it
    need to answer differently."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


def _supervisor(tmp_path: Path, results, **cfg_kw) -> LoopSupervisor:
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    run = RunDir(root=root)
    run.update_status(state="running")
    kw = {"max_approaches": 3, "vigilant": True, "on_complete": "exit",
          "infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
          "infra_outage_budget_s": 0.0, **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="task002-review-ladder", **kw),
                          run, root)
    sup.runner = _SequencedRunner(results)      # type: ignore[assignment]
    sup._approach = 1
    return sup


# -- 1. a prose final line is repaired, no archive/composite PRD -----------


def test_a_prose_final_line_is_repaired_on_reask_and_never_archives_or_composites(
        tmp_path):
    prose = "looks solid to me, I think we're genuinely done here"
    corrected = json.dumps({"verdict": "verified"})

    sup = _supervisor(tmp_path, [_ok(prose), _ok(corrected)])
    root = sup.run.root

    outcome = asyncio.run(sup._step_review())

    # the repaired reply reaches the ordinary VERIFIED terminal exit --
    # never a rejection.
    assert outcome == ("done", "succeeded")
    # exactly one charged review iteration plus one uncharged re-ask.
    assert sup.runner.calls == 2
    reask_prompt = sup.runner.prompts[1]
    assert "review" in reask_prompt.lower()

    # neither consequence of a rejection ever fires for a repaired verdict.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()


# -- 2. exhaustion still reaches the ordinary archive/replan path ----------


def test_ladder_exhaustion_still_archives_and_writes_the_composite_prd(
        tmp_path):
    """The hat never produces a valid `review` payload across every
    attempt the ladder grants -- exactly the pre-task-002 outcome for an
    unparseable reply: the approach is archived, a composite PRD is
    written for the next one, and the ladder made its full
    `1 + contract_retry_max` attempts (1 reused + `contract_retry_max`
    uncharged re-asks) before giving up."""
    prose = "still not sure, let me look again"

    sup = _supervisor(tmp_path, [_ok(prose)])   # every call replays `prose`
    root = sup.run.root

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max

    # today's ordinary archive-with-safe-default consequences, unchanged.
    assert (root / "approaches" / "01").exists()
    assert sup.run.composite_prd_file.exists()
