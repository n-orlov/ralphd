"""Task 004 (requirement A.1, #154): the verify prompt the engine builds
carries the SAME resolved materiality rubric review receives
(`findings.resolve_materiality_rubric` -- inline, adopted by reference, or
the engine default), and `task-verify.md`'s "Be strict" paragraph is
rewritten to reject only on an absent/wrong deliverable, a named failing
test, a false factual claim in a record, a command the criterion names
never run, or an exposed secret -- verifying form, wording, provenance and
process clauses with a note, folding "disclose on tails" in as a special
case of that same rule.

Two things pinned here, one per success-criteria clause:

1. `test_a_built_verify_prompt_carries_the_resolved_rubric_text` -- drives
   a real `_verify_task` call (the same harness `tests/
   test_verify_attempts_context_task004.py` uses) and inspects the actual
   rendered prompt text the engine handed the (stubbed) agent, not a helper
   method in isolation -- proof that the rubric text really reaches the
   built prompt, not merely that some method could produce it.
2. `test_the_rewritten_be_strict_paragraph_is_lexically_pinned` -- pins the
   literal rewritten paragraph's load-bearing phrases directly against
   `task-verify.md`'s own text, and that the retired duplicate "Reject on
   substance, disclose on tails" bullet no longer exists as a SEPARATE
   bullet (folded into "Be strict" instead).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import make_supervisor

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

TASK = {
    "id": "004",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
    "validationAttempts": 0,
}

RUBRIC_BEARING_CONTRACT = """# Some PRD

## Goal

Ship the thing.

## Materiality rubric

**Blocking** -- a red test at the shipped sha, task004-marker-blocking.

**Residual** -- a stale snapshot label, task004-marker-residual.
"""


def _verified(tid: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=1.0)
    r.final_text = json.dumps({"task_id": tid, "verdict": "verified"})
    r.usage = {"input": 5, "output": 2, "totalTokens": 7}
    return r


class _FixedRunner:
    def __init__(self, result: IterationResult):
        self.result = result
        self.calls = 0
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        self.calls += 1
        return self.result


_supervisor = lambda tmp_path, task, **cfg: make_supervisor(
    tmp_path, run_id="task004-verify-rubric", iterations=8, max_approaches=1, vigilant=True,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=0.0, tasks=[dict(task)],
    runner_factory=lambda sup: _FixedRunner(_verified(task['id'])), **cfg)


# ---------------------------------------------------------------------------
# 1. A built verify prompt carries the resolved rubric text.
# ---------------------------------------------------------------------------


def test_a_built_verify_prompt_carries_the_resolved_rubric_text(tmp_path):
    task = dict(TASK)
    sup = _supervisor(tmp_path, task)
    sup.run.prd_file.write_text(RUBRIC_BEARING_CONTRACT)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = sup.runner.prompts[0]
    # The literal marker text from the PRD's own rubric section, so this
    # is unambiguously THE resolved rubric -- not some other rubric-shaped
    # text -- and not read off a helper in isolation but off the actual
    # rendered prompt handed to the agent.
    assert "task004-marker-blocking" in prompt
    assert "task004-marker-residual" in prompt
    assert "Materiality rubric" in prompt


def test_a_built_verify_prompt_falls_back_to_the_engine_default_rubric(
        tmp_path):
    # No PRD file written at all -- the "no rubric supplied anywhere"
    # case: verify still receives a real blocking/residual boundary (the
    # engine default), same as review does, never silence.
    task = dict(TASK)
    sup = _supervisor(tmp_path, task)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = sup.runner.prompts[0]
    assert findings.NO_RUBRIC_NOTICE in prompt
    assert findings.DEFAULT_RUBRIC in prompt


# ---------------------------------------------------------------------------
# 2. The lexical pin over the rewritten "Be strict" paragraph.
# ---------------------------------------------------------------------------


def _verify_text() -> str:
    path = PROMPTS_DIR / "task-verify.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _be_strict_paragraph(text: str) -> str:
    match = re.search(
        r"- \*\*Be strict, but reject only on substance.*?"
        r"(?=\n- \*\*Budget your test runs)", text, re.DOTALL)
    assert match, (
        "task-verify.md has no 'Be strict, but reject only on substance' "
        "bullet immediately before the 'Budget your test runs' bullet")
    return match.group(0)


def test_the_rewritten_be_strict_paragraph_is_lexically_pinned():
    text = _verify_text()
    para = _be_strict_paragraph(text)

    # The five reject-worthy substance grounds, named explicitly.
    assert "deliverable" in para
    assert re.search(r"test.{0,30}is red", para, re.DOTALL)
    assert "factual claim" in para
    assert "command the criterion names was never run" in para
    assert "secret is exposed" in para

    # Form/wording/provenance/process clauses verify with a note.
    assert "form" in para.lower()
    assert "wording" in para.lower()
    assert "provenance" in para.lower()
    assert "process" in para.lower()
    assert "note" in para.lower()

    # "Disclose on tails" folded in as a special case of this same rule.
    assert "disclose on tails" in para.lower()
    assert "special case" in para.lower()
    assert "open-set" in para.lower()


def test_the_old_standalone_reject_on_substance_bullet_is_gone():
    text = _verify_text()
    # It must not survive as its OWN separate bullet any more -- folded
    # into "Be strict" instead. A bullet starts a line with "- **".
    assert not re.search(
        r"^- \*\*Reject on substance, disclose on tails", text,
        re.MULTILINE)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
