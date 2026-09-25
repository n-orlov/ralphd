"""Task 003: every live-run pause and escalation text names `ralphctl
unpause <run>` to proceed and `ralphctl rule <run> --file ... --name ...`
to answer -- never `ralphctl resume <run>`.

`ralphctl unpause` (`cli/main.py::cmd_unpause`) is the command that actually
clears a LIVE paused run's `pauseReason` (it POSTs `/resume`, the engine's
own verb for the same operation, but the CLI's own name for it is
`unpause`). `ralphctl resume` is a DIFFERENT command entirely -- it starts a
fresh container over an existing run dir (crash recovery / budget top-up),
so telling an operator sitting on a live, running process to run `ralphctl
resume` names the wrong tool. This module drives every `_*_escalation_text`
builder `grep -n "def _.*escalation_text" src/ralphd/engine/loop.py` finds,
plus the petition-hold text and the `pause()`/`_persist_pause` message, and
asserts none of them still says `ralphctl resume` and every one names
`ralphctl unpause <run>`. Every text whose pause waits on an operator
ruling (the rejection, stagnation and incurable texts, the
operator-input-needed text, the petition-hold notice and the `pause()`
message) also names `ralphctl rule <run> --file ... --name ...`; the
dependency-cycle/unresolvable-edge and broken-review-channel texts name a
different remedy (a tasks.json edit, a working reviewer) and `unpause`.
"""

from __future__ import annotations

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(
    run_id="task003-escalation-commands", cfg_defaults=False,
    prd="# task 003 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"})


def _finding(clause: str = "requirement C.1", curable: bool = True) -> dict:
    return {
        "severity": "blocking",
        "curable": curable,
        "clause": clause,
        "evidence": "some evidence",
        **({"cured_when": f"{clause} is discharged"} if curable else {}),
    }


def _assert_names_unpause_not_resume(text: str) -> None:
    assert "ralphctl resume" not in text
    assert f"ralphctl unpause {SUP.cfg.run_id}" in text


def _assert_names_rule_and_unpause(text: str) -> None:
    assert "ralphctl resume" not in text
    assert (f"ralphctl rule {SUP.cfg.run_id} --file <ruling.md> "
            "--name <slug>") in text
    assert f"ralphctl unpause {SUP.cfg.run_id}" in text


SUP = None


def _sup(tmp_path):
    global SUP
    SUP = _supervisor(tmp_path / "run")
    return SUP


def test_persist_pause_message_names_unpause(tmp_path):
    sup = _sup(tmp_path)
    sup._persist_pause("rejected_last_approach")
    # `_persist_pause` emits its own `log` event with the message; read it
    # back the same way every other consumer of `events.jsonl` does.
    import json
    log = sup.run.root / "events.jsonl"
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    messages = [e.get("message", "") for e in lines if e.get("type") == "log"]
    assert any("ralphctl unpause task003-escalation-commands" in m
               for m in messages)
    assert any("ralphctl rule task003-escalation-commands --file" in m
               and "--name" in m for m in messages)
    assert not any("ralphctl resume" in m for m in messages)


def test_last_approach_rejection_escalation_text_names_rule_and_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._last_approach_rejection_escalation_text(1, [_finding()])
    _assert_names_rule_and_unpause(text)


def test_repeated_rejection_signature_escalation_text_names_rule_and_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._repeated_rejection_signature_escalation_text(
        2, [_finding()], "sig-1")
    _assert_names_rule_and_unpause(text)


def test_operator_input_needed_escalation_text_names_rule_and_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._operator_input_needed_escalation_text(
        1, [_finding()], replan_available=True)
    _assert_names_rule_and_unpause(text)


def test_review_contract_unrepaired_escalation_text_names_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._review_contract_unrepaired_escalation_text(
        1, ["$.verdict: 'fail' is not one of ['verified', 'rejected']"])
    _assert_names_unpause_not_resume(text)


def test_review_findings_unusable_escalation_text_names_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._review_findings_unusable_escalation_text(
        1, ["findings sidecar present but invalid"])
    _assert_names_unpause_not_resume(text)


def test_stagnation_last_approach_escalation_text_names_rule_and_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._stagnation_last_approach_escalation_text(
        1, [{"id": "001", "status": "pending"}], is_last_approach=True)
    _assert_names_rule_and_unpause(text)


def test_stagnation_second_exhaustion_escalation_text_names_rule_and_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._stagnation_last_approach_escalation_text(
        1, [{"id": "001c", "status": "pending"}], is_last_approach=False,
        approach_progressed=True,
        exhausted_split_child={"id": "001c", "splitFrom": "001p",
                               "used": 3, "budget": 3})
    _assert_names_rule_and_unpause(text)


def test_petition_held_escalation_text_names_rule_and_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._petition_held_escalation_text(["001"], ["002", "003"])
    # The petition-hold notice names the operator's answer lever
    # (`ralphctl rule ... --file ... --name ...`) and the proceed lever
    # (`ralphctl unpause`), and never the wrong command `ralphctl resume`.
    _assert_names_rule_and_unpause(text)
    assert "--file <ruling.md> --name <slug>" in text


def test_incurable_rejection_escalation_text_names_rule_and_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._incurable_rejection_escalation_text(1, [_finding(curable=False)])
    _assert_names_rule_and_unpause(text)


def test_unschedulable_cycle_escalation_text_names_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._unschedulable_cycle_escalation_text({
        "cycleTaskIds": ["001", "002"],
        "cycleEdges": [{"from": "001", "to": "002"}, {"from": "002", "to": "001"}],
        "dependentTaskIds": ["003"],
    })
    _assert_names_unpause_not_resume(text)


def test_unresolvable_edge_escalation_text_names_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._unresolvable_edge_escalation_text({
        "unresolvedTaskIds": ["001"],
        "unresolvedEdges": [{"from": "001", "to": "999"}],
        "dependentTaskIds": ["002"],
    })
    _assert_names_unpause_not_resume(text)


def test_operator_escalation_digest_text_names_rule_and_unpause(tmp_path):
    sup = _sup(tmp_path)
    text = sup._operator_escalation_digest_text(
        "operator_input_needed", 1, [_finding()])
    _assert_names_rule_and_unpause(text)
    text2 = sup._operator_escalation_digest_text(
        "rejected_repeated_signature", 2, [_finding()])
    _assert_names_unpause_not_resume(text2)
