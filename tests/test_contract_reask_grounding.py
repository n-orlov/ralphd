"""Task 032 (operator-inserted): ground the contract re-ask in the hat's
actual final text.

Motivation, observed in this v0.16 run's own transcripts
(`artifacts/contract/0001.jsonl`/`0002.jsonl`, iteration 36): the steer
hat's final text was prose followed by a fenced ```json block carrying the
correct payload (`{"settled": true}`), but the pre-task-032 re-ask ladder
threw that text away entirely and asked a CONTEXT-FREE model call for "the
corrected JSON payload". The model duly fabricated `{"response": "steer"}`
from nothing -- there was nothing in the prompt to ground it in what the
hat had actually said -- and the SECOND re-ask's `{"settled": true}` was
right only by coincidence (the stub happened to answer correctly, not
because the prompt gave it any reason to).

Two independent fixes, both in `LoopSupervisor._ask_contract_fix`:

1. Fenced-payload extraction, before any model call: if the hat's own
   final text contains a fenced ```json block that both parses and
   validates against the hat's contract schema, use it directly -- zero
   model calls.
2. A grounded re-ask, when a model call is still needed: the prompt
   carries the hat's final text verbatim (bounded to the last 4000
   characters) alongside the error list, so the model repairs what the
   hat actually said instead of starting from nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import CONTRACT_REASK_FINAL_TEXT_MAX_CHARS, LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

STEER_SCHEMA_ERROR = "$.settled: is a required property"
WORKER_SCHEMA_ERROR = "$.verdict: is a required property"


class _StubRunner:
    """Records every call; a call site that fires this means a real model
    invocation happened. `final_text` is the reply the stub hands back,
    for the "a model re-ask is still genuinely required" tests."""

    def __init__(self, final_text: str = '{"verdict": "no_verdict"}'):
        self.calls: list[dict] = []
        self._final_text = final_text

    async def run(self, prompt, transcript, **kw):
        self.calls.append({"prompt": prompt, "transcript": Path(transcript), **kw})
        Path(transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript).write_text('{"type": "assistant", "text": "stub"}\n')
        r = IterationResult(exit_code=0)
        r.final_text = self._final_text
        r.duration_s = 0.01
        r.usage = {"input": 12, "output": 6, "totalTokens": 18}
        return r


def _supervisor(tmp_path: Path) -> LoopSupervisor:
    cfg = JobConfig(run_id="task032-contract-reask-grounding")
    sup = LoopSupervisor(cfg, RunDir(root=tmp_path), tmp_path)
    return sup


def _fenced(payload_json: str, *, prose: str = "Here is my reasoning about the situation.\nAfter thinking it through, my final answer is below.\n") -> str:
    return prose + "\n```json\n" + payload_json + "\n```\n"


# ---------------------------------------------------------------------------
# 1. Prose + fenced VALID payload settles with zero re-ask model calls.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_fenced_payload_settles_with_no_model_call(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    final_text = _fenced('{"verdict": "no_verdict"}')
    raw, usage = await sup._ask_contract_fix(
        "worker", [WORKER_SCHEMA_ERROR], final_text=final_text)

    assert stub.calls == []  # the stub runner was never invoked
    assert usage == {}
    import json
    assert json.loads(raw) == {"verdict": "no_verdict"}


# ---------------------------------------------------------------------------
# 2. An invalid fenced payload falls through to the re-ask.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_fenced_payload_falls_through_to_reask(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner(final_text='{"verdict": "no_verdict"}')
    sup.runner = stub

    # The fenced block is syntactically valid JSON but fails the worker
    # schema (missing the required "verdict" key) -- extraction must
    # refuse it, not use it.
    final_text = _fenced('{"task_id": "042"}')
    raw, usage = await sup._ask_contract_fix(
        "worker", [WORKER_SCHEMA_ERROR], final_text=final_text)

    assert len(stub.calls) == 1  # the model WAS asked
    assert raw == '{"verdict": "no_verdict"}'


# ---------------------------------------------------------------------------
# 3. The re-ask prompt carries both the verbatim errors and the final-text
#    excerpt.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reask_prompt_carries_errors_and_final_text_excerpt(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner(final_text='{"verdict": "no_verdict"}')
    sup.runner = stub

    final_text = "My reasoning goes here, no fenced block at all this time."
    errors = ["response: malformed JSON (Expecting value at line 1 column 1)"]
    await sup._ask_contract_fix("worker", errors, final_text=final_text)

    assert len(stub.calls) == 1
    prompt = stub.calls[0]["prompt"]
    assert errors[0] in prompt
    assert final_text in prompt


@pytest.mark.asyncio
async def test_reask_prompt_excerpt_is_bounded_to_last_4000_chars(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner(final_text='{"verdict": "no_verdict"}')
    sup.runner = stub

    long_prefix = "x" * 5000
    tail = "THIS IS THE TAIL THAT MUST SURVIVE TRUNCATION"
    final_text = long_prefix + tail
    await sup._ask_contract_fix(
        "worker", ["some error"], final_text=final_text)

    prompt = stub.calls[0]["prompt"]
    assert tail in prompt
    assert final_text not in prompt  # the full untruncated text is NOT there
    excerpt = final_text[-CONTRACT_REASK_FINAL_TEXT_MAX_CHARS:]
    assert excerpt in prompt


# ---------------------------------------------------------------------------
# 4. The iteration-36 regression shape: prose + fenced {"settled": true}
#    against the steer contract resolves to settled=true with NO model
#    call -- the exact shape that used to fabricate {"response": "steer"}.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_iteration_36_regression_shape_resolves_without_model_call(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    final_text = _fenced(
        '{"settled": true}',
        prose=(
            "I reviewed the pending operator messages and applied the "
            "task-table edits described in steer-001-proposed-task-edits.json.\n"
        ),
    )
    raw, usage = await sup._ask_contract_fix(
        "steer", [STEER_SCHEMA_ERROR], final_text=final_text)

    assert stub.calls == []
    assert usage == {}
    import json
    payload = json.loads(raw)
    assert payload == {"settled": True}
