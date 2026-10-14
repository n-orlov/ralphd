"""v0.30 G: a non-transient refusal fails fast.

Every refusal shape, from either harness, is classified `refused`, is not
retried, spends nothing of the outage budget, ends the run `failed` with
`ending: provider_refused` (v0.36 F.5; the message names `PUT /config/llm` and
`ralphctl resume`), and tells the notifier the provider's message, scrubbed.
`ralphctl resume` re-runs the same work.

The refused results are real: pi's come from `PiRunner` driving stub-pi's
in-band error (`STUB_INBAND_ERROR_MESSAGE`), claude-code's from
`ClaudeCodeRunner` driving the real wrapper and SDK against the stub Claude
Code CLI (an error result with a 4xx status, a CLI below the minimum version,
and a root agent uid refused before spawn). The loop then gets each result
through a scripted runner, so the pause can be checked in-process. No
network, no real token: the secret below is an obvious fake.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from ralphd.engine import redact
from ralphd.engine import runner as runner_mod
from ralphd.engine.faults import (
    FAULT_CLASS_RATE_LIMIT,
    FAULT_CLASS_REFUSED,
    explain_fault,
)
from ralphd.engine.runner import ClaudeCodeRunner, IterationResult, PiRunner
from tests.conftest import ScriptedRunner, make_supervisor

REPO = Path(__file__).resolve().parents[1]
STUB_PI = REPO / "tests" / "stub-pi" / "pi"
STUB_CLAUDE = REPO / "tests" / "stub-claude" / "claude"
MODEL_REF = "anthropic/claude-sonnet-5-5"

SECRET_NAME = "REFUSAL_FAKE_API_TOKEN"
SECRET_VALUE = "refusal-FAKE-SECRET-not-a-real-token-0002"
EXTRA_USAGE = "Third-party apps now draw from your extra usage, not your plan limits"


def _api_error(status: int, etype: str, message: str, prefix: str = "") -> str:
    return (f'{prefix}{status} {{"type":"error","error":{{"type":"{etype}",'
            f'"message":"{message}"}}}}')


# (id, family, pi in-band errorMessage). Each provider message names the fake
# secret, so the scrub is visible wherever the message is quoted.
PI_CASES = [
    ("pi-extra-usage", "extra-usage",
     _api_error(400, "invalid_request_error", f"{EXTRA_USAGE} ({SECRET_VALUE})")),
    ("pi-invalid-request", "invalid-request",
     _api_error(400, "invalid_request_error", f"max_tokens too large {SECRET_VALUE}")),
    ("pi-authentication", "authentication",
     _api_error(401, "authentication_error", f"invalid x-api-key {SECRET_VALUE}")),
    ("pi-permission", "permission",
     _api_error(403, "permission_error", f"no access for {SECRET_VALUE}")),
    ("pi-not-found", "not-found",
     _api_error(404, "not_found_error", f"model: claude-nope {SECRET_VALUE}")),
]

# (id, family, stub env, the message carries the secret). `refusal` is the
# stub's own extra-usage refusal; the others set the status, the SDK error
# type and the API error text.
CC_CASES = [
    ("cc-extra-usage", "extra-usage",
     {"STUB_CLAUDE_SCENARIO": "refusal",
      "STUB_CLAUDE_ERROR_MESSAGE": _api_error(
          400, "invalid_request_error", f"{EXTRA_USAGE} ({SECRET_VALUE})",
          prefix="API Error: ")}, True),
    ("cc-invalid-request", "invalid-request",
     {"STUB_CLAUDE_SCENARIO": "refusal", "STUB_CLAUDE_ERROR_STATUS": "400",
      "STUB_CLAUDE_ERROR_TYPE": "invalid_request",
      "STUB_CLAUDE_ERROR_MESSAGE": _api_error(
          400, "invalid_request_error", f"bad request {SECRET_VALUE}",
          prefix="API Error: ")}, True),
    ("cc-authentication", "authentication",
     {"STUB_CLAUDE_SCENARIO": "refusal", "STUB_CLAUDE_ERROR_STATUS": "401",
      "STUB_CLAUDE_ERROR_TYPE": "authentication_failed",
      "STUB_CLAUDE_ERROR_MESSAGE": _api_error(
          401, "authentication_error", f"invalid bearer token {SECRET_VALUE}",
          prefix="API Error: ")}, True),
    ("cc-permission", "permission",
     {"STUB_CLAUDE_SCENARIO": "refusal", "STUB_CLAUDE_ERROR_STATUS": "403",
      "STUB_CLAUDE_ERROR_TYPE": "permission_error",
      "STUB_CLAUDE_ERROR_MESSAGE": _api_error(
          403, "permission_error", f"not allowed {SECRET_VALUE}",
          prefix="API Error: ")}, True),
    ("cc-not-found", "not-found",
     {"STUB_CLAUDE_SCENARIO": "refusal", "STUB_CLAUDE_ERROR_STATUS": "404",
      "STUB_CLAUDE_ERROR_TYPE": "not_found_error",
      "STUB_CLAUDE_ERROR_MESSAGE": _api_error(
          404, "not_found_error", f"model not found {SECRET_VALUE}",
          prefix="API Error: ")}, True),
    ("cc-version", "cc-version", {"STUB_CLAUDE_VERSION": "2.1.75"}, False),
    ("cc-root", "cc-root", None, False),
]

ALL_IDS = [c[0] for c in PI_CASES] + [c[0] for c in CC_CASES]
FAMILY = {c[0]: c[1] for c in PI_CASES + CC_CASES}
CARRIES_SECRET = {**{c[0]: True for c in PI_CASES},
                  **{c[0]: c[3] for c in CC_CASES}}


def _clear_stub_env(mp, prefix: str) -> None:
    for k in [k for k in os.environ if k.startswith(prefix)]:
        mp.delenv(k)


def _pi_result(tmp: Path, message: str) -> IterationResult:
    run_dir = tmp / "run"
    ws = tmp / "ws"
    for d in (run_dir, ws, tmp / "it"):
        d.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        _clear_stub_env(mp, "STUB_")
        mp.setenv("STUB_RUN_DIR", str(run_dir))
        mp.setenv("STUB_INBAND_ERROR_COUNT", "1")
        mp.setenv("STUB_INBAND_ERROR_MESSAGE", message)
        runner = PiRunner(ws, pi_bin=str(STUB_PI))
        return asyncio.run(runner.run("the prompt", tmp / "it" / "output.jsonl",
                                      model=MODEL_REF, timeout_s=60))


def _cc_result(tmp: Path, stub: dict | None) -> IterationResult:
    ws = tmp / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (tmp / "it").mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        _clear_stub_env(mp, "STUB_CLAUDE_")
        mp.setenv("RALPHD_CLAUDE_CLI_PATH", str(STUB_CLAUDE))
        if stub is None:
            # the root refusal: the agent uid resolves to 0, nothing spawns
            mp.setattr(runner_mod, "agent_child_kwargs", dict)
            mp.setattr(os, "geteuid", lambda: 0)
        else:
            for k, v in stub.items():
                mp.setenv(k, v)
        runner = ClaudeCodeRunner(ws)
        return asyncio.run(runner.run("the prompt", tmp / "it" / "output.jsonl",
                                      model=MODEL_REF, timeout_s=60))


@pytest.fixture(scope="module")
def real_results(tmp_path_factory) -> dict[str, IterationResult]:
    """One real refused result per case, produced once for the module."""
    out: dict[str, IterationResult] = {}
    for case_id, _family, message in PI_CASES:
        out[case_id] = _pi_result(tmp_path_factory.mktemp(case_id), message)
    for case_id, _family, stub, _secret in CC_CASES:
        out[case_id] = _cc_result(tmp_path_factory.mktemp(case_id), stub)
    return out


@pytest.fixture
def live_fake_secret(monkeypatch, tmp_path):
    """SECRET_VALUE live in the engine's redaction map, as production makes a
    secret live (a secret-shaped env name plus `refresh_redaction_map`)."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    redact.refresh_redaction_map(home=fake_home)
    try:
        yield
    finally:
        monkeypatch.delenv(SECRET_NAME, raising=False)
        redact.refresh_redaction_map(home=fake_home)


# -- the runner's own report ---------------------------------------------------

@pytest.mark.parametrize("case_id", ALL_IDS)
def test_the_real_refused_result_classifies_refused(real_results, case_id):
    r = real_results[case_id]
    assert r.error_message, case_id
    exp = explain_fault(error_text=r.error_message, exit_code=r.exit_code,
                        interrupted=r.interrupted, timed_out=r.timed_out,
                        no_traffic_timeout=r.no_traffic_timeout,
                        rate_limit=r.rate_limit,
                        rate_limit_fresh=r.rate_limit_fresh,
                        http_status=r.http_status, error_type=r.error_type)
    assert exp["faultClass"] == FAULT_CLASS_REFUSED, (case_id, r.error_message)
    assert exp["signature"]["family"] == FAMILY[case_id]


def test_pi_reports_the_refusal_as_text_only(real_results):
    """stub-pi's in-band error reaches `IterationResult` verbatim, with no
    structured fields: the text path alone decides the class."""
    r = real_results["pi-extra-usage"]
    assert r.harness == "pi" and r.exit_code == 0
    assert r.http_status is None and r.error_type is None
    assert EXTRA_USAGE in r.error_message


def test_claude_code_reports_the_refusal_structured(real_results):
    r = real_results["cc-authentication"]
    assert r.harness == "claude-code"
    assert r.http_status == 401 and r.error_type == "authentication_failed"


# -- the loop: zero retries, zero outage, the pause, the notifier --------------

def _ok() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = "done"
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    r.duration_s = 30.0
    return r


def _events(sup, type_):
    log = sup.run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in map(json.loads, lines) if ev.get("type") == type_]


def _status(sup):
    return json.loads((sup.run.root / "status.json").read_text())


def _harness(tmp_path, refused: IterationResult, *, unpause: bool = True):
    """A supervisor whose worker attempts are `refused` then a success, with
    the notifier recorded, and a park that records what the run looked like
    while parked and then unpauses it (as `ralphctl unpause` would)."""
    sup = make_supervisor(tmp_path, prd=True, job_timeout_s=3600)
    sup.runner = ScriptedRunner({"worker": [lambda s, p: refused,
                                            lambda s, p: _ok()]})
    sup.runner.sup = sup
    rec = {"notes": [], "parked": [], "calls": [], "backoffs": []}
    sup.notifier = rec["notes"].append

    async def park():
        rec["parked"].append(_status(sup))
        if unpause:
            sup.resume()
        else:
            # what POST /abort does: record the abort, then release the park
            sup.abort("operator says stop")
            sup.resume()
        return 42.0

    async def no_backoff(seconds):
        rec["backoffs"].append(seconds)
        return seconds, False

    sup._park_until_resumed = park                       # type: ignore[method-assign]
    sup._wait_out_backoff = no_backoff                   # type: ignore[method-assign]
    real_once = sup._run_iteration_once

    async def once(phase, extra="", prompt_name=None, tier=None, task_id=None):
        rec["calls"].append((phase, extra, prompt_name, tier, task_id))
        return await real_once(phase, extra, prompt_name, tier, task_id=task_id)

    sup._run_iteration_once = once                       # type: ignore[method-assign]
    return sup, rec


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", ALL_IDS)
async def test_a_refusal_ends_the_run_failed_with_no_retry_and_no_outage(
        real_results, live_fake_secret, tmp_path, case_id):
    refused = real_results[case_id]
    sup, rec = _harness(tmp_path, refused)

    result = await sup.run_iteration("worker", "EXTRA", "worker", "standard",
                                     task_id="T7")

    # the refused iteration's verdict
    meta1 = json.loads((sup.run.iteration_dir(1) / "meta.json").read_text())
    assert meta1["faultClass"] == FAULT_CLASS_REFUSED
    # exactly one refused attempt: no retry, no backoff, no outage accounting
    assert _events(sup, "infra_retry") == []
    assert rec["backoffs"] == []
    assert sup._infra_episode_waited_s == 0.0
    assert sup._infra_episode_attempts == 0
    assert sup._infra_wait_total_s == 0.0
    assert _status(sup).get("infraWaitTotalS", 0) == 0
    # v0.36 F.5: the run is not parked or paused; it ends `failed` with
    # `ending: provider_refused` (tests/test_stop_routing_faults.py drives
    # the whole run)
    assert rec["parked"] == []
    assert sup._failed_ending == "provider_refused"
    assert sup._abort_reason is not None
    assert "PUT /config/llm" in sup._abort_reason
    assert f"ralphctl resume {sup.cfg.run_id}" in sup._abort_reason
    (event,) = _events(sup, "provider_refused")
    assert event["family"] == FAMILY[case_id] and event["phase"] == "worker"
    # never an operator_escalation (that shape routes a restart into review)
    assert _events(sup, "operator_escalation") == []
    # the notifier quotes the provider's message, scrubbed, and names the way on
    (note,) = rec["notes"]
    assert "provider refused" in note and FAMILY[case_id] in note
    assert "PUT /config/llm" in note
    assert f"ralphctl resume {sup.cfg.run_id}" in note
    quoted = event["message"]
    assert quoted and quoted in note
    assert SECRET_VALUE not in note
    assert SECRET_VALUE not in json.dumps(event)
    assert SECRET_VALUE not in sup._abort_reason
    if CARRIES_SECRET[case_id]:
        assert f"[REDACTED:env:{SECRET_NAME}]" in note
    # the refused result comes back and the work is not re-run
    assert result is refused
    assert rec["calls"] == [("worker", "EXTRA", "worker", "standard", "T7")]
    status = _status(sup)
    assert status.get("state") != "paused"
    assert status.get("pauseReason") is None and status.get("pauseDetail") is None
    # the refused attempt is refunded
    assert sup.iterations_used == 1 and sup.iterations_used_charged == 0
    assert sup._last_fault_class == FAULT_CLASS_REFUSED


@pytest.mark.asyncio
async def test_a_refusal_returns_promptly_without_parking(real_results, tmp_path):
    """No stubbed park: the iteration really returns at once (covering the
    former "a real park waits for resume and re-runs")."""
    sup = make_supervisor(tmp_path, prd=True, job_timeout_s=3600, real_sleep=True)
    refused = real_results["pi-authentication"]
    sup.runner = ScriptedRunner({"worker": [lambda s, p: refused,
                                            lambda s, p: _ok()]})
    sup.runner.sup = sup
    result = await asyncio.wait_for(sup.run_iteration("worker"), timeout=10)
    assert result is refused
    assert sup.runner.calls == {"worker": 1}
    assert _status(sup).get("state") != "paused"


# -- a ruling has nothing to release on a refusal ------------------------------

@pytest.mark.asyncio
async def test_a_ruling_is_recorded_and_there_is_no_pause_to_release(
        real_results, tmp_path):
    sup, rec = _harness(tmp_path, real_results["pi-permission"])
    sup._approach = 1
    sup.run.update_status(approach=1, rejectionSignatures={"1": "sig-abc"})
    sup.run.add_ruling("a ruling filed against the hold")
    await sup.run_iteration("worker")
    assert rec["parked"] == []
    assert len(sup.run.ruling_entries()) == 1
    assert not _status(sup).get("noRulingProceed")
    assert sup._failed_ending == "provider_refused"


# -- what is still not a refusal ------------------------------------------------

@pytest.mark.parametrize("stub,expected", [
    ({"STUB_CLAUDE_SCENARIO": "error"}, "infra"),
    ({"STUB_CLAUDE_SCENARIO": "rate_limited"}, FAULT_CLASS_RATE_LIMIT),
], ids=["cc-5xx", "cc-429"])
def test_a_5xx_is_infra_and_a_429_is_a_rate_limit(tmp_path, stub, expected):
    r = _cc_result(tmp_path, stub)
    sup = make_supervisor(tmp_path / "sup")
    assert sup._classify_result(r) == expected


def test_a_pi_5xx_in_band_error_stays_infra(tmp_path):
    r = _pi_result(tmp_path, _api_error(500, "api_error", "Internal server error"))
    sup = make_supervisor(tmp_path / "sup")
    assert sup._classify_result(r) == "infra"


# -- ralphctl status: the `paused:` line ----------------------------------------

class _StatusArgs:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.json = False


def test_ralphctl_status_prints_the_refusal_pause(tmp_path, monkeypatch, capsys):
    from ralphd.cli import main as cli_main
    from ralphd.engine.state import format_pause_detail_line

    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)
    root = registry / "runs" / "refused"
    root.mkdir(parents=True)
    detail = {"reason": "provider_refused", "family": "extra-usage",
              "message": _api_error(400, "invalid_request_error", EXTRA_USAGE),
              "httpStatus": 400, "phase": "worker", "iteration": 1}
    (root / "status.json").write_text(json.dumps({
        "runId": "refused", "state": "paused",
        "pauseReason": "operator_input_needed", "pauseDetail": detail,
        "iterationsUsed": 0, "iterationsBudget": 6,
        "startedAt": "2026-10-02T14:00:00Z"}))
    line = format_pause_detail_line(detail)
    assert line.startswith("paused:    provider refused (extra-usage): 400 ")
    assert EXTRA_USAGE in line and line.endswith("then ralphctl unpause")
    assert format_pause_detail_line(None) is None

    cli_main.cmd_status(_StatusArgs("refused"))
    out = capsys.readouterr().out
    assert [ln for ln in out.splitlines() if ln.startswith("paused:")] == [line]
