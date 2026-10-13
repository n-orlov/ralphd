"""Cure task cure-01-01-9 (PRD A.1/A.6, D.1; DoD 1/4), kept current for
v0.36: review drove every advertised answer command through the real CLI
parser + engine `POST` route over the ACTUAL pause, and found the shared
"ralphctl rule ..., or ralphctl unpause ... to accept the default" phrasing
false: `ralphctl rule` alone leaves the loop gate CLOSED -- releasing a
`paused` run always needs a SEPARATE `ralphctl unpause` afterward, whether or
not a ruling was filed first.

v0.36 D.1: `resume_if_ruling_answers_pause` is gone, so NO ruling releases
anything. The two findings parks (`operator_input_needed`,
`verdict_gate_operator_input_needed`) are `awaiting_input` and are left by
`ralphctl answer` alone; their instructions name that command, `rule` only
records, and a bare unpause is refused. An operator pause (`pause`, the
only `paused` run the engine does not take itself once task 012 lands)
keeps the rule-then-unpause / unpause-alone sequence below. The engine's own
stagnation and graph-fault parks are gone (v0.36 requirement F; their
replacement is `tests/test_self_heal_stagnation_graph.py`).

This module drives each of the real pauses end to end (the real
`_pause_and_escalate_*` method, a real notifier stub) and runs every
advertised alternative through the REAL CLI parser + `cmd_*` handler +
engine `POST` route (`fastapi.testclient.TestClient` over `create_app`),
asserting the actual pause-gate result -- `sup._pause.is_set()`, the ONE
predicate `pause()`/`resume()` both flip and every iteration boundary
actually reads, so it is the correct "released or not" measure even for
`rejected_incurable`'s own reason-less, in-memory-only hold (never
persisted to `state`/`pauseReason` at all -- `_pause_and_escalate_
incurable_rejection` calls the bare `self.pause()`):

* `ralphctl rule` ALONE -- records the ruling but must leave the gate
  CLOSED (the defect the prior phrasing hid);
* the advertised `rule` THEN `unpause` sequence -- must open it;
* `ralphctl unpause` ALONE -- the working unpause-only alternative -- must
  also open it, with no ruling recorded at all.

The findings parks run through the identical harness: `ralphctl answer`
opens their gate, `ralphctl rule` alone and a bare unpause do not.
"""

from __future__ import annotations

import json
import re
import shlex

import pytest
from fastapi.testclient import TestClient

from ralphd.cli import main as cli_main
from ralphd.engine.api import create_app
from tests.conftest import supervisor_factory

RUN_ID = "cure-01-01-9-pause-ruling-sequence"

_supervisor = supervisor_factory(
    run_id=RUN_ID, cfg_defaults=False,
    prd="# cure-01-01-9 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"}, operator_input_needed_pause=False)


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


def _finding(clause: str = "requirement Z.1") -> dict:
    return {"severity": "blocking", "curable": True, "clause": clause,
            "evidence": "some evidence", "cured_when": f"{clause} is cured"}


def _sup(tmp_path, monkeypatch, **cfg):
    """Build the supervisor under `<tmp_path>/registry/runs/<RUN_ID>` and
    point `RALPHD_REGISTRY` at `<tmp_path>/registry`, the same layout
    `ralphctl`'s own `run_root()` resolves -- `cmd_rule`/`cmd_unpause`
    both call `_require_run()` before ever reaching the API, so a
    supervisor built anywhere else is invisible to the real CLI handler
    this module drives. `status.json`'s own `approach` field is seeded to
    1 too (distinct from `sup._approach`, the in-memory counter every
    `_pause_and_escalate_*` call below is passed directly) -- `resume_if_
    ruling_answers_pause` resolves the CURRENT approach off status.json,
    never off the in-memory attribute, so a ruling filed through the real
    `/rule` route needs it there to find its `rejectionSignatures` entry
    at all.
    """
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)
    sup = _supervisor(tmp_path, run_root=f"registry/runs/{RUN_ID}",
                      max_approaches=3, **cfg)
    sup._approach = 1
    sup.run.update_status(approach=1)
    sup.notifier = _StubNotifier()
    return sup


def _raise_pause(sup, reason: str):
    if reason == "verdict_gate_operator_input_needed":
        sup._pause_and_escalate_gate_operator_input_needed(
            1, [_finding()], True, all_curable=False, trigger="incurable")
    elif reason == "operator_pause":
        sup.pause()
    elif reason == "operator_input_needed":
        sup._pause_and_escalate_operator_input_needed(
            1, [_finding()], replan_available=True, signature="sig-x")
    else:
        raise AssertionError(reason)
    assert sup._pause.is_set() is False, "the pause did not close the gate"


def _digest_text(sup) -> str:
    assert len(sup.notifier.messages) == 1
    return sup.notifier.messages[0]


def _run_command(cmd: str, tmp_path, monkeypatch, client) -> None:
    """Run one `ralphctl ...` command through the REAL parser + `cmd_*`
    handler, with `cli_main.api` routed to the real engine `TestClient`
    instead of a TCP socket -- the same route a live `ralphctl` reaches."""
    ruling = tmp_path / "ruling.md"
    if not ruling.exists():
        ruling.write_text("the operator's ruling text\n")
    argv = shlex.split(
        cmd.replace("<ruling.md>", str(ruling)).replace("<slug>", "answer")
    )[1:]

    def api(run_id, method, path, body=None, **kw):
        assert run_id == RUN_ID
        resp = client.request(method, path, json=body)
        assert resp.status_code < 400, (path, resp.status_code, resp.text)
        return resp.json()

    monkeypatch.setattr(cli_main, "api", api)
    args = cli_main.build_parser().parse_args(argv)
    args.func(args)


def _rule_cmd(sup) -> str:
    return (f"ralphctl rule {sup.cfg.run_id} --file <ruling.md> --name "
            "<slug>")


def _unpause_cmd(sup) -> str:
    return f"ralphctl unpause {sup.cfg.run_id}"


NOT_RULING_RELEASED_REASONS = ["operator_pause"]
FINDINGS_PARKS = ["operator_input_needed",
                  "verdict_gate_operator_input_needed"]


def _answer_cmd(sup) -> str:
    return f"ralphctl answer {sup.cfg.run_id}"


@pytest.mark.parametrize("reason", NOT_RULING_RELEASED_REASONS)
def test_rule_alone_does_not_release_the_pause(reason, tmp_path, monkeypatch):
    sup = _sup(tmp_path, monkeypatch)
    _raise_pause(sup, reason)

    with TestClient(create_app(sup.cfg, sup.run, sup)) as client:
        _run_command(_rule_cmd(sup), tmp_path, monkeypatch, client)
    assert sup._pause.is_set() is False, reason  # still held


@pytest.mark.parametrize("reason", NOT_RULING_RELEASED_REASONS)
def test_rule_then_unpause_sequence_releases_the_pause(
        reason, tmp_path, monkeypatch):
    sup = _sup(tmp_path, monkeypatch)
    _raise_pause(sup, reason)

    with TestClient(create_app(sup.cfg, sup.run, sup)) as client:
        _run_command(_rule_cmd(sup), tmp_path, monkeypatch, client)
        assert sup._pause.is_set() is False, reason  # rule alone: unreleased
        _run_command(_unpause_cmd(sup), tmp_path, monkeypatch, client)
    assert sup._pause.is_set() is True, reason
    status = sup.run.read_status()
    assert status.get("pauseReason") is None, reason


@pytest.mark.parametrize("reason", NOT_RULING_RELEASED_REASONS)
def test_unpause_alone_releases_the_pause(reason, tmp_path, monkeypatch):
    sup = _sup(tmp_path, monkeypatch)
    _raise_pause(sup, reason)

    with TestClient(create_app(sup.cfg, sup.run, sup)) as client:
        _run_command(_unpause_cmd(sup), tmp_path, monkeypatch, client)
    assert sup._pause.is_set() is True, reason
    status = sup.run.read_status()
    assert status.get("pauseReason") is None, reason


@pytest.mark.parametrize("reason", FINDINGS_PARKS)
def test_findings_park_is_left_by_answer_alone(reason, tmp_path, monkeypatch):
    """v0.36: neither `rule` nor a bare unpause leaves an `awaiting_input`
    park; `answer` does (covering node: tests/test_answer_outcomes.py)."""
    sup = _sup(tmp_path, monkeypatch)
    _raise_pause(sup, reason)
    text = _digest_text(sup)
    assert _answer_cmd(sup) in text
    assert _unpause_cmd(sup) not in text
    assert "to accept the default" not in text, reason
    assert sup.run.read_status()["state"] == "awaiting_input"

    with TestClient(create_app(sup.cfg, sup.run, sup)) as client:
        _run_command(_rule_cmd(sup), tmp_path, monkeypatch, client)
        assert sup._pause.is_set() is False, reason  # a ruling releases nothing
        assert len(sup.run.ruling_entries()) == 1
        refused = client.post("/resume")
        assert refused.status_code == 409, refused.text
        assert _answer_cmd(sup) in refused.text
        assert sup._pause.is_set() is False, reason
        _run_command(_answer_cmd(sup), tmp_path, monkeypatch, client)
    assert sup._pause.is_set() is True, reason
    assert sup.run.read_status().get("pauseReason") is None, reason


# --- the FULL pause instructions (`operator_escalation.escalation`) ---------
#
# Cure-01-01-9 retry (validationAttempts 2): the digest above was fixed, but
# the FULL escalation text each pause method emits on its own
# `operator_escalation` event still said the run "will not proceed until" a
# ruling is recorded AND it is unpaused, with no unpause-only alternative --
# false, since `ralphctl unpause` alone opens each of these gates. These
# nodes read that emitted event text after the ACTUAL pause, pull every
# advertised command out of it, and execute each advertised route through
# the real CLI + engine, asserting the gate result the text claims.

_BACKTICKED = re.compile(r"`(ralphctl [^`]+)`")


def _raise_full_text_pause(sup, variant: str):
    _raise_pause(sup, variant)


def _escalation_event_text(sup) -> str:
    events = [json.loads(line) for line in
              (sup.run.root / "events.jsonl").read_text().splitlines()]
    texts = [e.get("escalation") for e in events
             if e.get("type") == "operator_escalation"]
    assert len(texts) == 1, texts
    return texts[0]


def _advertised(text: str) -> list[str]:
    return _BACKTICKED.findall(text)


@pytest.mark.parametrize("reason", FINDINGS_PARKS)
def test_findings_park_full_instructions_name_answer_not_unpause(
        reason, tmp_path, monkeypatch):
    sup = _sup(tmp_path, monkeypatch)
    _raise_pause(sup, reason)
    text = _escalation_event_text(sup)
    assert "will not proceed until" not in text
    assert "to accept the default" not in text
    assert f"`ralphctl answer {sup.cfg.run_id} [--waive <fid>...]" in text
    assert "records a ruling but does not answer the park" in text
    assert _unpause_cmd(sup) not in text
