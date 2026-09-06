"""Task 003 (requirement A.3, #119): push the uncharged re-ask ladder
INSIDE `_verdict_gate_verdict` itself, so both gate sites that consult it
-- the ordinary `_step_verdict_gate` state and the off-budget
`_grace_verdict_gate` call `_maybe_grace_review` makes -- inherit the
ladder automatically the moment they `await` this one parser, instead of
each needing its own `_verdict_gate_contract_ladder`-style call inserted
before it (the shape `_review_contract_ladder`/`_verify_contract_ladder`
use at the review/verify sites).

Mirrors `tests/test_review_contract_ladder_task002.py`'s pattern (a
stubbed `sup.runner` replaying a fixed sequence of `IterationResult`s,
driven through the real state method rather than the parser in
isolation) but for the verdict-gate site: this module only exercises
`_step_verdict_gate` directly, since it and `_grace_verdict_gate` share
the exact same parser -- pinning the parser's behaviour once, through
either site, pins it for both by construction.

Two things pinned here:

1. a prose (unparseable) `verdict_gate` final line, repaired by the
   ladder's uncharged re-ask into a valid `{"verdict": "verified"}`
   payload, reaches the ordinary approved exit -- it neither archives the
   approach nor writes a composite PRD -- and the ladder made exactly one
   charged call (`_step_verdict_gate`'s own `run_iteration`) plus one
   uncharged re-ask (`_ask_contract_fix`), i.e. `sup.runner.calls == 2`;
2. ladder exhaustion (the hat never produces a valid payload across every
   attempt `contract_retry_max` grants) keeps TODAY's safe default --
   `_verdict_gate_verdict` collapses to `(approved=False, payload=None)`
   exactly as an unparseable reply always has -- so the approach is
   archived, the composite PRD is written, and (with no approach left)
   the run reaches `("tail",)` fail-open, having made exactly
   `1 + sup.cfg.contract_retry_max` total runner calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_with_runner


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=5.0)
    r.final_text = text
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _SequencedRunner:
    """Replays one `IterationResult` per call, in order (the last one
    repeats forever once the sequence runs out) -- same shape as
    `test_verify_contract_ladder_task001.py`/`test_review_contract_ladder_
    task002.py`'s helper of the same name: the charged verdict_gate call
    and the uncharged re-ask(s) that follow it need to answer
    differently."""

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


_supervisor = supervisor_with_runner(
    lambda sup, results: _SequencedRunner(results),
    run_id="task003-verdict-gate-ladder", run_root="run", workspace="run",
    prd="# test PRD\n\nDo the thing.\n", status={"state": "running"}, approach=1,
    iterations=10, max_approaches=2, vigilant=False, on_complete="exit",
    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0,
    peer_verdict_gate=True)


# -- 1. a prose final line is repaired, no archive/composite PRD -----------


def test_a_prose_verdict_gate_final_line_is_repaired_on_reask_and_spends_no_approach(
        tmp_path):
    prose = "yep, this genuinely looks fully verified to me"
    corrected = json.dumps({"verdict": "verified"})

    sup = _supervisor(tmp_path, [_ok(prose), _ok(corrected)])
    root = sup.run.root

    outcome = asyncio.run(sup._step_verdict_gate())

    # the repaired reply reaches the ordinary approved terminal exit.
    assert outcome == ("done", "succeeded")
    # exactly one charged verdict_gate iteration plus one uncharged re-ask.
    assert sup.runner.calls == 2
    reask_prompt = sup.runner.prompts[1]
    assert "verdict_gate" in reask_prompt.lower()

    # neither consequence of a rejection ever fires for a repaired verdict.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup.run.read_status().get("verdict") == "verified"


# -- 2. exhaustion still keeps today's safe default ------------------------


def test_ladder_exhaustion_on_a_prose_reply_keeps_todays_safe_default(tmp_path):
    """The hat never produces a valid `verdict_gate` payload across every
    attempt the ladder grants -- exactly the pre-task-003 outcome for an
    unparseable reply: the approach is archived, a composite PRD is
    written (no approach left to try again), and the ladder made its full
    `1 + contract_retry_max` attempts (1 reused + `contract_retry_max`
    uncharged re-asks) before giving up."""
    prose = "hard to say, would need another look at this"

    sup = _supervisor(tmp_path, [_ok(prose)], max_approaches=1)
    root = sup.run.root

    outcome = asyncio.run(sup._step_verdict_gate())

    # no approach left -> fail-open to finishing, never a deadlocked run.
    assert outcome == ("tail",)
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max

    # today's ordinary archive-with-safe-default consequences, unchanged.
    assert (root / "approaches" / "01").exists()
    assert sup.run.composite_prd_file.exists()
