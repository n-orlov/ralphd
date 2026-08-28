"""Task 004 (steer 002 item 3, requirement B model decision): which model
`LoopSupervisor._ask_contract_fix` resolves for its re-ask.

Decision recorded here and in `_ask_contract_fix`'s own docstring: option
(a) -- the re-ask runs on the OFFENDING HAT's own model
(`self.cfg.model_for(hat)`), never a fixed/borrowed entry. Before this task,
`_ask_contract_fix` resolved `self.cfg.model_for("contract")`; `"contract"`
is not a `MODEL_HATS` member, so `config.py`'s `UNRECOGNISED_HAT_FALLBACK`
("planning") silently repaired every hat's payload with planning's own
model, regardless of which hat produced the violation -- e.g. a `sonnet`
worker's malformed verdict being fixed by `opus` (planning's configured
model) even though the worker's own tier is provisioned and routable.

This test pins the NEW behaviour for a hat whose own model differs from
planning's: the re-ask must use THAT hat's model, not planning's borrowed
entry and not a hardcoded "contract" lookup. Against the pre-task-004 code
(`model_for("contract")`), this fails -- see the commit message for the
quoted red run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

PLANNING_MODEL = "amazon-bedrock/eu.anthropic.claude-opus-5"
WORKER_MODEL = "amazon-bedrock/eu.anthropic.claude-sonnet-5"

VALIDATION_ERROR = "$.task_id: expected string, got int"


class _StubRunner:
    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, prompt, transcript, **kw):
        self.calls.append({"prompt": prompt, "transcript": Path(transcript), **kw})
        Path(transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript).write_text('{"type": "assistant", "text": "stub"}\n')
        r = IterationResult(exit_code=0)
        r.final_text = '{"task_id": "042", "verdict": "complete"}'
        r.duration_s = 0.01
        r.usage = {"input": 12, "output": 6, "totalTokens": 18}
        return r


def _supervisor(tmp_path: Path) -> LoopSupervisor:
    cfg = JobConfig(
        run_id="task004-contract-reask-model",
        model_overrides={"planning": PLANNING_MODEL, "worker": WORKER_MODEL},
    )
    return LoopSupervisor(cfg, RunDir(root=tmp_path), tmp_path)


@pytest.mark.asyncio
async def test_ask_contract_fix_uses_the_offending_hats_own_model(tmp_path):
    """A worker-hat re-ask must resolve WORKER's own model, not planning's
    (the fallback `model_for("contract")` used to borrow) and not `None`
    (pi's own default, what a literal but unconfigured "contract" hat
    would resolve to)."""
    sup = _supervisor(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("worker", [VALIDATION_ERROR])

    assert stub.calls[0]["model"] == WORKER_MODEL
    assert stub.calls[0]["model"] != PLANNING_MODEL


@pytest.mark.asyncio
async def test_ask_contract_fix_model_matches_model_for_the_hat(tmp_path):
    """The re-ask's model must always equal `cfg.model_for(hat)` for
    whichever hat is being repaired -- pinned for a second hat (`planning`
    itself) too, so this is not just "always worker"."""
    sup = _supervisor(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("planning", [VALIDATION_ERROR])

    assert stub.calls[0]["model"] == sup.cfg.model_for("planning") == PLANNING_MODEL
