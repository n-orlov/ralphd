"""Task 012 (#14): run state names the model the engine actually talked to.

#14's complaint, verbatim from the PRD: *"All three runs inspected have
`model: None` in `status.json`, so an operator debugging 'why is this unpriced'
cannot see what the engine saw."* The id was never lost -- pi reports it on
every assistant message (`provider` + a provider-side `model` id) -- it was
simply never recorded, so the one place an operator looks knew nothing.

What lands here:

* `state.model_ids()` -- the ONE place the two halves of pi's report are turned
  into a pi-style `provider/model` ref plus, only when it genuinely differs,
  the raw gateway id;
* `runner` observes both off the message stream (not from the ref the engine
  *asked* for, which is `None` whenever the operator pinned nothing -- exactly
  the `model: null` case);
* `loop` records them per iteration (`modelResolved`/`modelRaw` in
  `meta.json`, next to the unchanged `model` = what was requested) and promotes
  them to `status.json`'s `model`/`modelRaw`;
* `GET /status` and `ralphctl status --json` publish both, with explicit nulls
  for a run dir that never observed one (pre-v0.6), never a guessed value.

Tiers: unit on the formatter and the scanner, black-box engine runs over the
stub (gateway-shaped pinned id AND an unpinned one), live `GET /status`, and
the real `ralphctl` over a container-less run dir.
"""

from __future__ import annotations

import json

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_e2e import EngineProc

from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.engine.state import model_ids

__all__ = ["ctl", "unix_sock"]

# This run's own route (see /run/ralphd/iterations/*/meta.json): the gateway
# calls the model `eu.anthropic.claude-opus-5` and pi's ref for it is
# `amazon-bedrock/eu.anthropic.claude-opus-5`.
PROVIDER = "amazon-bedrock"
RAW_ID = "eu.anthropic.claude-opus-5"
GATEWAY_MODEL = f"{PROVIDER}/{RAW_ID}"


# --------------------------------------------------------------------------
# unit tier: state.model_ids
# --------------------------------------------------------------------------

@pytest.mark.parametrize("provider,model,expected", [
    # the live shape: provider prefix added, so the raw id is a second fact
    (PROVIDER, RAW_ID, (GATEWAY_MODEL, RAW_ID)),
    ("aigw-openai", "openai.gpt-5.6-sol",
     ("aigw-openai/openai.gpt-5.6-sol", "openai.gpt-5.6-sol")),
    # already a full ref: one fact, so no raw id is invented
    (PROVIDER, GATEWAY_MODEL, (GATEWAY_MODEL, None)),
    # no provider reported at all: the id stands alone
    (None, "gpt-5", ("gpt-5", None)),
    ("", "gpt-5", ("gpt-5", None)),
    # nothing named: ignorance, not an empty string
    (PROVIDER, None, (None, None)),
    (PROVIDER, "", (None, None)),
    (None, None, (None, None)),
    # whitespace/junk degrades rather than raising (format_duration contract)
    ("  anthropic  ", "  claude-x  ", ("anthropic/claude-x", "claude-x")),
    (7, 9, ("7/9", "9")),
    (True, False, (None, None)),
])
def test_model_ids_renderings(provider, model, expected):
    assert model_ids(provider, model) == expected


def test_model_ids_never_repeats_one_string_as_two_facts():
    resolved, raw = model_ids(PROVIDER, GATEWAY_MODEL)
    assert resolved == GATEWAY_MODEL and raw is None


# --------------------------------------------------------------------------
# unit tier: the runner observes what pi reports, not what it was asked for
# --------------------------------------------------------------------------

def _scan(message: dict, requested: str | None = None) -> IterationResult:
    line = json.dumps({"type": "message_end", "message": message}).encode()
    result = IterationResult()
    PiRunner._scan_line(line, result, pricing=None, model=requested)
    return result


def test_the_scanner_records_the_model_pi_reports():
    result = _scan({"role": "assistant", "provider": PROVIDER, "model": RAW_ID,
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input": 1, "output": 1, "totalTokens": 2}})
    assert result.model == GATEWAY_MODEL
    assert result.model_raw == RAW_ID


def test_the_scanner_prefers_the_reported_model_over_the_requested_ref():
    """The requested ref is `None` whenever pi picks its own model -- which is
    precisely the run whose status.json used to say `model: null`."""
    result = _scan({"role": "assistant", "provider": "openai", "model": "gpt-5",
                    "content": [], "usage": {"totalTokens": 1}},
                   requested=None)
    assert result.model == "openai/gpt-5"


def test_a_message_naming_no_model_observes_nothing():
    """An in-band error message (zero traffic, no model named) must not be
    recorded as an observation -- the engine leaves run state alone instead."""
    result = _scan({"role": "assistant", "stopReason": "error",
                    "errorMessage": "Connection error.", "content": [],
                    "usage": {"input": 0, "output": 0, "totalTokens": 0}},
                   requested=GATEWAY_MODEL)
    assert result.model is None and result.model_raw is None


def test_a_later_message_without_a_model_does_not_erase_an_observation():
    line_with = json.dumps({"type": "message_end", "message": {
        "role": "assistant", "provider": PROVIDER, "model": RAW_ID,
        "content": [], "usage": {"totalTokens": 1}}}).encode()
    line_without = json.dumps({"type": "message_end", "message": {
        "role": "assistant", "content": [], "usage": {"totalTokens": 1}}}).encode()
    result = IterationResult()
    PiRunner._scan_line(line_with, result)
    PiRunner._scan_line(line_without, result)
    assert result.model == GATEWAY_MODEL


# --------------------------------------------------------------------------
# black-box tier: a stubbed engine run
# --------------------------------------------------------------------------

@pytest.fixture
def model_engine(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "model-id-e2e", "iterations": 6,
                    "max_approaches": 1, "on_complete": "exit"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def test_a_run_over_a_gateway_shaped_id_records_both_ids(model_engine):
    e = model_engine({"model": GATEWAY_MODEL})
    assert e.proc.wait(timeout=60) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["model"] == GATEWAY_MODEL
    assert status["modelRaw"] == RAW_ID

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas
    for meta in metas:
        # what was requested stays put, what was resolved lands beside it
        assert meta["model"] == GATEWAY_MODEL, meta
        assert meta["modelResolved"] == GATEWAY_MODEL, meta
        assert meta["modelRaw"] == RAW_ID, meta


def test_an_unpinned_run_records_the_model_pi_chose(model_engine):
    """The exact defect: nothing pinned, so `meta["model"]` is null -- and
    run state must still name the model that answered."""
    e = model_engine()
    assert e.proc.wait(timeout=60) == 0

    metas = [json.loads(p.read_text())
             for p in sorted(e.run_dir.glob("iterations/*/meta.json"))]
    assert metas and all(m["model"] is None for m in metas)
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["model"] == "stub-provider/stub-model-1"
    assert status["modelRaw"] == "stub-model-1"


def test_a_run_whose_provider_reports_a_full_ref_records_no_raw_id(model_engine):
    e = model_engine({"model": "stubby/stub-model-1"},
                     stub_env={"STUB_MODEL": "stubby/stub-model-1"})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["model"] == "stubby/stub-model-1"
    assert status["modelRaw"] is None


def test_get_status_publishes_the_observed_model_live(model_engine):
    e = model_engine({"model": GATEWAY_MODEL, "on_complete": "idle"})
    e.wait_api()
    e.wait_state(("succeeded",), timeout=90)
    code, doc = e.api("GET", "/status")
    assert code == 200
    assert doc["model"] == GATEWAY_MODEL
    assert doc["modelRaw"] == RAW_ID


def test_get_status_publishes_explicit_nulls_before_any_traffic(model_engine):
    """A run dir that has observed nothing yet (and a pre-v0.6 one) publishes
    nulls, so absence is never a third case for a consumer."""
    e = model_engine({"model": GATEWAY_MODEL, "on_complete": "idle"},
                     stub_env={"STUB_INSTANT_FAIL_COUNT": "99"})
    e.wait_api()
    code, doc = e.api("GET", "/status")
    assert code == 200
    assert doc["model"] is None
    assert doc["modelRaw"] is None


# --------------------------------------------------------------------------
# black-box tier: ralphctl over a container-less run dir
# --------------------------------------------------------------------------

_BASE_STATUS = {
    "state": "failed",
    "verdict": "unverified",
    "phase": "worker",
    "iterationsUsed": 7,
    "iterationsBudget": 250,
    "startedAt": "2024-01-01T00:00:00Z",
    "schemaVersion": 1,
}


def _seed_status(ctl: Ctl, run_id: str, **status_over) -> None:
    rdir, _cdir = _seed_run(ctl, run_id)
    (rdir / "host.json").unlink()   # no container record: on-disk path only
    (rdir / "status.json").write_text(
        json.dumps({**_BASE_STATUS, "runId": run_id, **status_over}))


def _model_line(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("model:"):
            return line
    return None


def test_status_json_carries_both_ids_with_no_container(ctl):
    _seed_status(ctl, "json-both-ids", model=GATEWAY_MODEL, modelRaw=RAW_ID)
    doc = json.loads(ctl.run("--json", "status", "json-both-ids").stdout)
    assert doc["model"] == GATEWAY_MODEL
    assert doc["modelRaw"] == RAW_ID


def test_status_json_says_null_for_a_pre_v06_run_dir(ctl):
    _seed_status(ctl, "json-null-model")
    doc = json.loads(ctl.run("--json", "status", "json-null-model").stdout)
    assert doc["model"] is None
    assert doc["modelRaw"] is None


def test_status_text_names_the_model_and_the_gateway_id(ctl):
    _seed_status(ctl, "text-both-ids", model=GATEWAY_MODEL, modelRaw=RAW_ID)
    res = ctl.run("status", "text-both-ids")
    assert res.returncode == 0, res.stderr
    line = _model_line(res.stdout)
    assert line is not None, res.stdout
    assert GATEWAY_MODEL in line
    assert f"(gateway id: {RAW_ID})" in line


def test_status_text_omits_the_gateway_id_when_it_adds_nothing(ctl):
    _seed_status(ctl, "text-one-id", model="stubby/stub-model-1")
    res = ctl.run("status", "text-one-id")
    line = _model_line(res.stdout)
    assert line is not None and line.split() == ["model:", "stubby/stub-model-1"]


def test_status_text_omits_the_model_line_when_none_was_observed(ctl):
    """Never `model: None`: the same discipline as the approach segment."""
    _seed_status(ctl, "text-no-model")
    res = ctl.run("status", "text-no-model")
    assert res.returncode == 0, res.stderr
    assert _model_line(res.stdout) is None
    assert "None" not in res.stdout
