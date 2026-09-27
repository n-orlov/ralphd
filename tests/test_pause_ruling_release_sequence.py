"""Cure task cure-01-01-9 (PRD A.1/A.6, D.1; DoD 1/4): review drove every
advertised answer command through the real CLI parser + engine `POST`
route over the ACTUAL pause `resume_if_ruling_answers_pause` applies to,
and found the shared "ralphctl rule ..., or ralphctl unpause ... to accept
the default" phrasing false for `rejected_last_approach`, `rejected_
incurable` and `stagnation`: none of those three reasons is in `resume_if_
ruling_answers_pause`'s own reason allow-list, so `ralphctl rule` alone
leaves the loop gate CLOSED -- releasing it always needs a SEPARATE
`ralphctl unpause` afterward, whether or not a ruling was filed first.

This module drives each of the five real pauses end to end (the real
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

The two genuinely ruling-answerable reasons (`operator_input_needed`,
`rejected_repeated_signature`) are run through the identical harness as a
preserved control: `ralphctl rule` ALONE must open their gate (`resume_if_
ruling_answers_pause` DOES cover both), so the fix above must never widen
its "needs a separate unpause" claim to these two.
"""

from __future__ import annotations

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
    if reason == "rejected_last_approach":
        sup._pause_and_escalate_last_approach_rejection(1)
    elif reason == "rejected_incurable":
        sup._pause_and_escalate_incurable_rejection(1)
    elif reason == "stagnation":
        sup._pause_and_escalate_stagnation_last_approach(
            1, [{"id": "t1", "status": "pending"}])
    elif reason == "operator_input_needed":
        sup._pause_and_escalate_operator_input_needed(
            1, [_finding()], replan_available=True, signature="sig-x")
    elif reason == "rejected_repeated_signature":
        sup._pause_and_escalate_repeated_rejection_signature(1, "sig-y")
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


NOT_RULING_RELEASED_REASONS = [
    "rejected_last_approach", "rejected_incurable", "stagnation"]
RULING_RELEASED_REASONS = [
    "operator_input_needed", "rejected_repeated_signature"]


@pytest.mark.parametrize("reason", NOT_RULING_RELEASED_REASONS)
def test_rule_alone_does_not_release_the_pause(reason, tmp_path, monkeypatch):
    sup = _sup(tmp_path, monkeypatch)
    _raise_pause(sup, reason)
    text = _digest_text(sup)
    assert _rule_cmd(sup) in text
    assert _unpause_cmd(sup) in text
    # Cure-01-01-9's own fix: the prior digest phrasing ("rule ..., or
    # unpause ... to accept the default") read as two interchangeable
    # single-step alternatives, which is false for these three reasons --
    # the digest must instead spell out the two-step sequence and never
    # claim `rule` alone is a full "accept the default" alternative.
    assert "to accept the default" not in text, reason
    assert "then ralphctl unpause" in text, reason
    assert "alone to resume with none recorded" in text, reason

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


@pytest.mark.parametrize("reason", RULING_RELEASED_REASONS)
def test_rule_alone_releases_the_genuinely_ruling_answerable_pause(
        reason, tmp_path, monkeypatch):
    """Preserved control: the two reasons `resume_if_ruling_answers_pause`
    actually covers must keep working with `rule` alone -- the fix above
    must never widen its "needs a separate unpause" claim to these."""
    sup = _sup(tmp_path, monkeypatch)
    _raise_pause(sup, reason)
    text = _digest_text(sup)
    assert _rule_cmd(sup) in text
    assert _unpause_cmd(sup) in text
    # Unlike the three reasons above, these two really are interchangeable
    # single-step alternatives -- the fix must not have widened its
    # sequential phrasing to these.
    assert "to accept the default" in text, reason

    with TestClient(create_app(sup.cfg, sup.run, sup)) as client:
        _run_command(_rule_cmd(sup), tmp_path, monkeypatch, client)
    assert sup._pause.is_set() is True, reason
    status = sup.run.read_status()
    assert status.get("pauseReason") is None, reason
