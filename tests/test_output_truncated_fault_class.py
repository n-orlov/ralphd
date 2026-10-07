"""v0.32.3 A: a text-free `stopReason: length` final reply is `output_truncated`.

The provider cutting a reply off at its output-token cap used to leave an
`IterationResult` with an empty `final_text`, exit code 0 and `faultClass:
null` -- indistinguishable from a clean iteration. The classifier now has a
sixth class for it, recorded on `meta.json` and `iteration.end`, worded as the
provider's doing, and adding no retry or refund for a non-judging hat.
"""

from __future__ import annotations

import json
import os
import re
import types
from pathlib import Path

import pytest

from ralphd.engine import faults as faults_module
from ralphd.engine.faults import (
    FAULT_CLASS_OUTPUT_TRUNCATED,
    FAULT_CLASSES,
    FAULT_REASON_OUTPUT_TRUNCATED,
    classify_fault,
    explain_fault,
)
from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.engine.state import fault_explanation
from ralphd.harness.normalise import Normaliser
from tests.conftest import call_ralphctl_in_process, scripted_supervisor

REPO = Path(__file__).resolve().parents[1]
REASONING_ONLY_USAGE = {"input": 900, "output": 32000, "cacheRead": 0,
                        "cacheWrite": 0, "totalTokens": 32900}


def _message_end(stop: str, content: list[dict], **extra) -> bytes:
    msg = {"role": "assistant", "provider": "openai", "model": "gpt-5",
           "content": content, "usage": dict(REASONING_ONLY_USAGE),
           "stopReason": stop, **extra}
    return json.dumps({"type": "message_end", "message": msg}).encode()


THINKING = {"type": "thinking", "thinking": "pondering the verdict at length"}
TEXT = {"type": "text", "text": "the verdict is below\n"}
TOOL = {"type": "toolCall", "id": "t1", "name": "read", "arguments": {}}


def _scan(*lines: bytes) -> IterationResult:
    """A pi transcript, line by line, through the runner's own scanner."""
    result = IterationResult(exit_code=0)
    for line in lines:
        assert PiRunner._scan_line(line, result) is True
    return result


_supervisor = scripted_supervisor(run_id="unit", infra_retry_max=3,
                                  infra_retry_backoff_s=[0.01])


def _truncated_result() -> IterationResult:
    r = _scan(_message_end("length", [THINKING]))
    r.duration_s = 120.0
    return r


# ----------------------------------------------------------- 1: detection
def test_the_class_is_in_the_vocabulary():
    assert FAULT_CLASS_OUTPUT_TRUNCATED == "output_truncated"
    assert "output_truncated" in FAULT_CLASSES


def test_a_pi_length_reply_with_reasoning_only_classifies_output_truncated(tmp_path):
    result = _truncated_result()
    assert result.output_truncated is True
    assert result.final_text == ""
    assert result.exit_code == 0 and result.error_message == ""
    assert result.usage["output"] == 32000      # reasoning-only usage
    sup = _supervisor(tmp_path, {"worker": [lambda sup, prompt: result]})
    assert sup._classify_result(result) == "output_truncated"


@pytest.mark.asyncio
async def test_meta_and_iteration_end_carry_the_class(tmp_path):
    result = _truncated_result()
    sup = _supervisor(tmp_path, {"worker": [lambda sup, prompt: result]})
    await sup._run_iteration_once("worker")
    metas = [json.loads(p.read_text())
             for p in sorted((sup.run.root / "iterations").glob("*/meta.json"))]
    assert [m["faultClass"] for m in metas] == ["output_truncated"]
    assert metas[0]["outputTruncated"] is True
    events = [json.loads(x) for x in
              (sup.run.root / "events.jsonl").read_text().splitlines()]
    ends = [e for e in events if e.get("type") == "iteration.end"]
    assert [e["faultClass"] for e in ends] == ["output_truncated"]


@pytest.mark.asyncio
async def test_the_same_message_with_text_is_not_this_class(tmp_path):
    result = _scan(_message_end("length", [THINKING, TEXT]))
    assert result.output_truncated is False
    assert result.final_text.strip()
    sup = _supervisor(tmp_path, {"worker": [lambda sup, prompt: result]})
    assert sup._classify_result(result) is None
    await sup._run_iteration_once("worker")
    metas = [json.loads(p.read_text())
             for p in sorted((sup.run.root / "iterations").glob("*/meta.json"))]
    assert [m["faultClass"] for m in metas] == [None]
    assert "outputTruncated" not in metas[0]


@pytest.mark.parametrize("stop, content", [
    ("stop", [TEXT]),
    ("stop", [THINKING]),
    ("toolUse", [TOOL]),
])
def test_other_stop_reasons_classify_as_before(tmp_path, stop, content):
    result = _scan(_message_end(stop, content))
    assert result.output_truncated is False
    sup = _supervisor(tmp_path)
    assert sup._classify_result(result) is None


def test_an_error_stop_reason_classifies_as_before(tmp_path):
    line = _message_end("error", [], errorMessage="Connection error.")
    result = _scan(line)
    assert result.output_truncated is False
    assert result.error_message == "Connection error."
    sup = _supervisor(tmp_path)
    before = classify_fault(error_text="Connection error.", exit_code=0,
                            produced_traffic=True)
    assert before == "infra"
    assert sup._classify_result(result) == before


def test_a_later_message_clears_an_earlier_truncation(tmp_path):
    result = _scan(_message_end("length", [THINKING]),
                   _message_end("stop", [TEXT]))
    assert result.output_truncated is False
    assert _supervisor(tmp_path)._classify_result(result) is None


# ------------------------------------------------- 4: the claude-code path
def _claude_final_message(stop_reason: str, blocks: list[dict]) -> IterationResult:
    norm = Normaliser(model_ref="anthropic/claude-sonnet-5-5")
    events: list[dict] = []
    events += norm.feed("assistant", {
        "content": blocks, "model": "claude-sonnet-5-5", "message_id": "m1",
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 2, "output_tokens": 32000,
                  "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}})
    events += norm.feed("result", {"is_error": False, "result": ""})
    events += norm.finish(None)
    result = IterationResult(exit_code=0, harness="claude-code")
    for event in events:
        PiRunner._scan_line(json.dumps(event).encode(), result)
    return result


def test_a_normalised_claude_max_tokens_message_without_text_classifies(tmp_path):
    result = _claude_final_message(
        "max_tokens", [{"thinking": "reasoning without end", "signature": "s"}])
    assert result.final_text == ""
    assert result.output_truncated is True
    assert _supervisor(tmp_path)._classify_result(result) == "output_truncated"


def test_a_normalised_claude_max_tokens_message_with_text_does_not(tmp_path):
    result = _claude_final_message("max_tokens", [{"text": "partial verdict"}])
    assert result.output_truncated is False
    assert _supervisor(tmp_path)._classify_result(result) is None


def test_a_normalised_claude_end_turn_message_does_not(tmp_path):
    result = _claude_final_message(
        "end_turn", [{"thinking": "short", "signature": "s"}])
    assert result.output_truncated is False
    assert _supervisor(tmp_path)._classify_result(result) is None


def test_a_normalised_claude_error_message_classifies_as_before(tmp_path):
    norm = Normaliser(model_ref="anthropic/claude-sonnet-5-5")
    events = norm.finish({"status": 500, "type": "api_error",
                          "message": "Internal server error"})
    result = IterationResult(exit_code=0, harness="claude-code")
    for event in events:
        PiRunner._scan_line(json.dumps(event).encode(), result)
    assert result.error_message
    assert result.output_truncated is False
    assert _supervisor(tmp_path)._classify_result(result) != "output_truncated"


# ----------------------------------------------------------- 2: the wording
def test_explain_fault_words_the_class_as_the_provider_cutting_off():
    exp = explain_fault(exit_code=0, produced_traffic=True,
                        output_truncated=True)
    assert exp["faultClass"] == "output_truncated"
    assert exp["reason"] == FAULT_REASON_OUTPUT_TRUNCATED
    assert "provider cut the final reply off" in exp["reason"]
    assert "not a work failure" in exp["reason"]


def _dead_run(tmp_path: Path) -> tuple[Path, Path]:
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / "cutoff"
    (run_dir / "iterations" / "0004").mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": "cutoff", "state": "failed", "iterationsUsed": 4}))
    (run_dir / "iterations" / "0004" / "meta.json").write_text(json.dumps(
        {"number": 4, "phase": "verdict_gate", "approach": 1,
         "startedAt": "2026-10-07T10:00:00Z", "endedAt": "2026-10-07T10:09:00Z",
         "exitCode": 0, "interrupted": False, "timedOut": False,
         "noTrafficTimeout": False, "sawComplete": False, "sawVerified": False,
         "error": None, "faultClass": "output_truncated",
         "outputTruncated": True, "usage": dict(REASONING_ONLY_USAGE)}))
    (run_dir / "events.jsonl").write_text("")
    return registry, run_dir


def test_ralphctl_fault_words_the_class_as_the_provider_cutting_the_reply_off(
        tmp_path, capsys):
    registry, run_dir = _dead_run(tmp_path)
    exp = fault_explanation(run_dir)
    assert exp["faultClass"] == "output_truncated"
    assert exp["notices"] == []          # re-derived, no divergence notice
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    r = call_ralphctl_in_process(["fault", "cutoff"], env=env, capsys=capsys)
    assert r.returncode == 0, r.stderr
    assert "fault:     output_truncated (iteration 4, phase verdict_gate)" in r.stdout
    assert "the provider cut the final reply off" in r.stdout
    assert "not a work failure" in r.stdout


# --------------------------------- the detection depends on the branch
def test_the_detection_fails_when_the_classification_branch_is_removed(tmp_path):
    """The rung is `if output_truncated and not ...: return {...}` in
    `explain_fault`. Re-executing `faults.py` with that block cut out makes the
    same truncated input classify as a clean iteration, so this node's
    positive assertion above cannot pass without the branch."""
    src = Path(faults_module.__file__).read_text()
    rung = re.search(
        r"    if output_truncated and not \(error_text or \"\"\)\.strip\(\):\n"
        r"(?:        .*\n|\n)+?(?=    if no_traffic_timeout:)", src)
    assert rung, "the classification rung moved: update this mutation"
    mutated = src.replace(rung.group(0), "", 1)
    assert mutated != src
    module = types.ModuleType("faults_without_the_rung")
    module.__file__ = faults_module.__file__
    exec(compile(mutated, "faults_without_the_rung.py", "exec"), module.__dict__)
    kwargs = dict(exit_code=0, produced_traffic=True, output_truncated=True)
    assert classify_fault(**kwargs) == "output_truncated"
    assert module.classify_fault(**kwargs) != "output_truncated"


# --------------------- 3: no new retry or refund for a non-judging hat
@pytest.mark.parametrize("phase", ["worker", "verify", "planning"])
@pytest.mark.asyncio
async def test_a_truncated_non_judging_iteration_is_not_redispatched(tmp_path, phase):
    sup = _supervisor(tmp_path)
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None, task_id=None):
        calls.append(phase)
        return _truncated_result()

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    result = await sup.run_iteration(phase)
    assert calls == [phase], calls
    assert result.output_truncated is True
    assert sup._infra_refunded == 0
    log = sup.run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    kinds = {json.loads(x).get("type") for x in lines}
    assert not kinds & {"infra_retry", "infra_wait", "rate_limit_wait_started",
                        "provider_refused"}, kinds


# ------------------------------------------------------------- 4: the docs
@pytest.mark.parametrize("path, needles", [
    ("SPEC.md", ['`"output_truncated"`', "provider cut the reply off"]),
    ("docs/cli.md", ["`output_truncated`", "cut the final"]),
])
def test_the_docs_state_the_new_class(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"
