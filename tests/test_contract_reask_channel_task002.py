"""Task 002 (#60 pillar 3, requirement B): `LoopSupervisor._ask_contract_fix`
in isolation -- the uncharged contract re-ask channel added directly on
`_ask_router`'s precedent, wired to nothing yet (the five refusal-site
cutovers are tasks 003-007).

Drives the method with a stubbed `sup.runner` -- never `_run_iteration_once`
-- and checks, from disk and state, every half of the contract:

- `iterations_used` is untouched;
- no `iterations/` directory is created at all;
- `usage.byPhase["contract"]` is non-empty after the call;
- the transcript file exists under `artifacts/contract/NNNN.jsonl`;
- the prompt text handed to the stubbed runner states the validation error
  verbatim;
- the prompt does NOT contain the worker task prompt's body, and does not
  invite the hat to do further workspace work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory

# The exact schema-violation shape task 002's own acceptance text names.
VALIDATION_ERROR = "$.task_id: expected string, got int"

# A recognisable fragment of the worker hat's own task prompt body (from
# src/ralphd/prompts/worker.md) -- must never leak into a contract re-ask.
WORKER_PROMPT_FRAGMENT = "THE ONE RULE: exactly one task, then stop"


_supervisor = supervisor_factory(run_id="task002-contract-reask", cfg_defaults=False)


class _StubRunner:
    """Records every call's prompt/transcript and returns a scripted,
    successful `IterationResult` -- this test never spawns a subprocess."""

    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, prompt, transcript, **kw):
        self.calls.append({"prompt": prompt, "transcript": Path(transcript), **kw})
        # Mirror the real `PiRunner.run`: the transcript file is created (and
        # appended to) as a side effect of running, not by the caller.
        Path(transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript).write_text('{"type": "assistant", "text": "stub"}\n')
        r = IterationResult(exit_code=0)
        r.final_text = '{"task_id": "042", "verdict": "complete"}'
        r.duration_s = 0.01
        r.usage = {"input": 12, "output": 6, "totalTokens": 18}
        return r


@pytest.mark.asyncio
async def test_ask_contract_fix_never_charges_an_iteration(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner()
    sup.runner = stub  # never sup._run_iteration_once
    iterations_before = sup.iterations_used

    text, usage = await sup._ask_contract_fix(
        "worker", [VALIDATION_ERROR])

    assert sup.iterations_used == iterations_before
    assert not (tmp_path / "iterations").exists() or \
        list((tmp_path / "iterations").iterdir()) == []
    assert usage  # a non-empty usage dict was handed back


@pytest.mark.asyncio
async def test_ask_contract_fix_records_usage_and_transcript_under_own_bucket(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("worker", [VALIDATION_ERROR])

    status = sup.run.read_status()
    by_phase = status.get("usage", {}).get("byPhase", {})
    assert by_phase.get("contract"), "usage.byPhase['contract'] must be non-empty"

    assert len(stub.calls) == 1
    transcript = stub.calls[0]["transcript"]
    assert transcript.parent == tmp_path / "artifacts" / "contract"
    assert transcript.name == "0001.jsonl"
    assert transcript.exists()


@pytest.mark.asyncio
async def test_ask_contract_fix_prompt_states_the_error_verbatim_and_nothing_else(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("worker", [VALIDATION_ERROR])

    prompt = stub.calls[0]["prompt"]
    assert VALIDATION_ERROR in prompt
    assert WORKER_PROMPT_FRAGMENT not in prompt
    assert "do the thing" not in prompt.lower()
    for invite in ("do the work", "continue working", "keep working",
                   "make more changes", "implement the task"):
        assert invite not in prompt.lower()


@pytest.mark.asyncio
async def test_ask_contract_fix_uses_its_own_counter_across_calls(tmp_path):
    sup = _supervisor(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("worker", [VALIDATION_ERROR])
    await sup._ask_contract_fix("worker", ["a second, different error"])

    names = sorted(c["transcript"].name for c in stub.calls)
    assert names == ["0001.jsonl", "0002.jsonl"]
    # the router's own counter must stay untouched by contract calls
    assert sup._router_calls == 0
    assert sup._contract_calls == 2
