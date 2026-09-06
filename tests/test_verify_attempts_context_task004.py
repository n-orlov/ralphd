"""Task 004 (#121, requirement A.4): `_verify_task`'s injected `extra`
context names the task's own `validationAttempts` count against
`VALIDATION_ATTEMPT_LIMIT`, and carries an explicit final-attempt marker
precisely when this is the LAST available attempt -- instead of leaving
task-verify.md's step 4 ("if this rejecting verdict is the task's LAST
available attempt...") to a verifier with no way to tell which attempt it
is actually on.

Two things pinned here:

1. the injected `extra` block for `_verify_task` always carries a
   `validationAttempts` line naming the count and the limit, and carries an
   additional final-attempt marker only when the count is one below the
   limit (`VALIDATION_ATTEMPT_LIMIT - 1`) -- absent at `validationAttempts
   == 0` and at every other count that is not one below the limit;
2. task-verify.md's step 4 refers to the SAME literal field name the
   engine injects -- a lexical test extracts the field name from each side
   independently and fails the moment they diverge (e.g. one side renames
   `validationAttempts` and the other does not follow).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, VALIDATION_ATTEMPT_LIMIT, atomic_write_json
from tests.conftest import make_supervisor

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
LOOP_PY = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "engine" / "loop.py"

TASK = {
    "id": "004",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
}


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
    tmp_path, run_id="task004-verify-attempts", iterations=8, max_approaches=1, vigilant=True,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=0.0, tasks=[dict(task)],
    runner_factory=lambda sup: _FixedRunner(_verified(task['id'])), **cfg)


# -- 1. the extra block names count and limit, and gates the marker --------


def _extra_block(prompt: str) -> str:
    """Only the injected `extra` block, not the whole rendered prompt --
    task-verify.md's own step-4 prose also mentions FINAL ATTEMPT/
    validationAttempts, so asserting against the whole prompt would pass
    for the wrong reason."""
    idx = prompt.index("## Task under verification")
    return prompt[idx:]


def test_the_extra_block_names_the_count_and_the_limit(tmp_path):
    task = dict(TASK, validationAttempts=1)
    sup = _supervisor(tmp_path, task)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = _extra_block(sup.runner.prompts[0])
    assert re.search(
        rf"validationAttempts\*\*:\s*1\s+of\s+{VALIDATION_ATTEMPT_LIMIT}",
        prompt), prompt


def test_the_final_attempt_marker_is_absent_at_zero_attempts(tmp_path):
    task = dict(TASK, validationAttempts=0)
    sup = _supervisor(tmp_path, task)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = _extra_block(sup.runner.prompts[0])
    assert "validationAttempts" in prompt
    assert "FINAL ATTEMPT" not in prompt


def test_the_final_attempt_marker_appears_one_below_the_limit(tmp_path):
    task = dict(TASK, validationAttempts=VALIDATION_ATTEMPT_LIMIT - 1)
    sup = _supervisor(tmp_path, task)

    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is True

    prompt = _extra_block(sup.runner.prompts[0])
    assert "FINAL ATTEMPT" in prompt
    assert re.search(
        rf"validationAttempts\*\*:\s*{VALIDATION_ATTEMPT_LIMIT - 1}\s+of\s+"
        rf"{VALIDATION_ATTEMPT_LIMIT}", prompt), prompt


def test_the_final_attempt_marker_is_absent_at_every_other_non_final_count(
        tmp_path):
    # Anything strictly below "one below the limit" (and not itself the
    # skipped-already-exhausted case) must NOT carry the marker either --
    # only the single count one below VALIDATION_ATTEMPT_LIMIT does.
    for attempts in range(0, VALIDATION_ATTEMPT_LIMIT - 1):
        task = dict(TASK, validationAttempts=attempts)
        sup = _supervisor(tmp_path / str(attempts), task)
        asyncio.run(sup._verify_task(dict(task)))
        prompt = _extra_block(sup.runner.prompts[0])
        assert "FINAL ATTEMPT" not in prompt, (attempts, prompt)


# -- 2. the lexical pin: the field name agrees on both sides ---------------


def _injected_field_name() -> str:
    """Extract the literal field name `_verify_task` labels its
    validationAttempts line with, straight from loop.py's own source --
    never hand-typed, so a rename on this side is what the test actually
    detects."""
    src = LOOP_PY.read_text()
    match = re.search(
        r'f"- \*\*(\w+)\*\*:\s*\{attempts\}\s*of\s*\{VALIDATION_ATTEMPT_LIMIT\}',
        src)
    assert match, (
        "could not find _verify_task's validationAttempts extra-block line "
        "in loop.py -- update this test's extraction regex to match "
        "whatever replaced it")
    return match.group(1)


def _verify_step_four_text() -> str:
    text = (PROMPTS_DIR / "task-verify.md").read_text()
    match = re.search(
        r"^4\.\s(.*?)(?=^\d+[a-z]?\.\s|^#)", text, re.DOTALL | re.MULTILINE)
    assert match, "task-verify.md has no `4.` step"
    return match.group(1)


def test_the_prompt_names_the_same_field_the_engine_injects():
    field_name = _injected_field_name()
    assert field_name == "validationAttempts", (
        f"unexpected extraction {field_name!r} -- fix the regex, not this "
        "assertion")
    step = _verify_step_four_text()
    assert re.search(rf"`{re.escape(field_name)}`", step), (
        f"task-verify.md step 4 never refers to the injected field "
        f"`{field_name}` by its literal name: {step!r}")


def test_the_lexical_pin_fails_if_the_two_names_diverge():
    """A direct demonstration that the pin above is load-bearing: renaming
    ONE side's literal token (simulated here, not written to disk) breaks
    the cross-check the same way a real drift would."""
    field_name = _injected_field_name()
    step = _verify_step_four_text()
    renamed = field_name + "Renamed"
    assert not re.search(rf"`{re.escape(renamed)}`", step), (
        "sanity check itself is broken: the renamed token unexpectedly "
        "matched task-verify.md's step 4")
