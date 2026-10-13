"""Task 015 (v0.23 PRD requirement A.4, #224): once an operator ruling
lands against the signature an `operator_input_needed` pause (task 014)
recorded, resuming the run re-asks `review` EXACTLY ONCE -- the ordinary
resume-into-review routing every sibling pause family already has -- with
the ruling rendered as an authoritative clause of that SAME prompt
(`_operator_ruling_review_context`, task 013), and the fresh verdict is
recorded with a new `operator_ruling_honoured` event naming which ruling
review actually honoured.

Two things this task adds on top of tasks 013/014 (both cured here, not
merely inspected):

1. `_pause_and_escalate_operator_input_needed` now persists the rejection's
   own signature (`_record_rejection_signature`) BEFORE it pauses -- without
   this, `RunDir.add_ruling` (which reads `status.json`'s own
   `rejectionSignatures[str(approach)]`) always resolved `None` for a
   ruling filed against this specific pause, and no later comparison could
   ever tell "this ruling answers THIS rejection" from "unscoped".
2. `LoopSupervisor._ruling_honoured_for_approach` plus the
   `operator_ruling_honoured` event -- a plain signature-equality lookup
   (no new verb vocabulary, parser or classifier for the ruling's own
   text), emitted once review renders a legible fresh verdict over a
   signature a ruling addresses.

Modelled on `tests/test_operator_input_needed_pause.py`'s own two-episode
shape: episode 1 drives a real `_step_review()` to the
`operator_input_needed` pause; the ruling is filed (`RunDir.add_ruling`)
while parked; episode 2 is a FRESH `LoopSupervisor` over the SAME on-disk
run dir (what a killed-and-restarted container leaves behind) whose
`_step_review()` re-asks review with a counting runner, so "exactly one
re-ask" is a genuine count across a process boundary, never an artefact of
the test happening to call a method once.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from ralphd.engine import findings
from ralphd.engine.api import create_app
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import PAUSED_STATE, ruling_entries, write_ruling
from tests.conftest import supervisor_factory


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _finding(severity: str, curable: bool, clause: str, evidence: str,
             cured_when: str | None = None) -> dict:
    payload = {"severity": severity, "curable": curable, "clause": clause,
               "evidence": evidence}
    if cured_when is None and severity == "blocking" and curable is True:
        cured_when = f"{clause} is discharged"
    if cured_when is not None:
        payload["cured_when"] = cured_when
    return payload


# Neither all-curable nor all-incurable -- lands on the ordinary
# archive/replan tail `operator_input_needed` intercepts, exactly
# `tests/test_operator_input_needed_pause.py`'s own fixture.
_MIXED_FINDINGS = [
    _finding("blocking", False, "requirement B (#111)", "incurable one"),
    _finding("blocking", True, "requirement A.1 (#224)", "curable one"),
]


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _post_rule(sup, text: str, name: str | None = None) -> httpx.Response:
    """Cure (F01, task 015): the real `/rule` route over a real ASGI app --
    the SAME route `ralphctl rule`/the hub textarea's proxy land on -- so
    the auto-release fix below is proven against production wiring, never
    the storage primitive (`RunDir.add_ruling`) alone. Deliberately NOT its
    own `test_` function: the fast tier's own ceiling stands at zero
    headroom (task 024), so this cure folds its coverage into the existing
    tests below rather than adding a fresh collected item."""
    async def _post():
        app = create_app(sup.cfg, sup.run, sup)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://engine") as client:
            body = {"text": text}
            if name is not None:
                body["name"] = name
            return await client.post("/rule", json=body)
    return _bounded(_post())


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


_supervisor = supervisor_factory(
    run_id="task015-ruling-reask", cfg_defaults=False,
    prd="# task 015 fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1})


def _pause_on_operator_input_needed(root: Path):
    """Episode 1: a real `_step_review()` call that reaches
    `operator_input_needed` on the mixed-curability findings above."""
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps(
            {"verdict": "rejected", "reason": "mixed curability"}))
    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    return sup


# ===========================================================================
# 1. The signature-scoping fix: the pause persists the rejection signature
#    so a ruling filed while parked resolves it, never `None`.
# ===========================================================================

def test_the_pause_persists_the_rejection_signature_before_it_holds(tmp_path):
    root = tmp_path / "run"
    sup1 = _pause_on_operator_input_needed(root)

    status = sup1.run.read_status()
    signature = (status.get("rejectionSignatures") or {}).get("1")
    assert signature is not None

    fname = sup1.run.add_ruling("Ship it; these are residuals now.")
    entries = ruling_entries(root)
    assert len(entries) == 1
    assert entries[0]["file"] == fname
    assert entries[0]["signature"] == signature

    # Cure (F01): an UNRELATED live pause must never be released by a
    # ruling POST -- neither a ruling filed with no current rejection
    # signature to match (the escalation recorded none at all) nor one
    # filed while parked for some OTHER reason than `operator_input_
    # needed`. Folded into this test rather than a fresh one: the fast
    # tier's own ceiling stands at zero headroom (task 024).
    sup1.run.update_status(rejectionSignatures={})
    resp = _post_rule(sup1, "unscoped ruling over the route")
    assert resp.status_code == 202, resp.text
    status = sup1.run.read_status()
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "operator_input_needed"
    assert not sup1._pause.is_set()

    # a genuinely different live pause: a stagnation hold is `paused` (v0.36
    # parks findings in `awaiting_input` instead; this one is not a findings
    # question, so a ruling must still not release it).
    sup1.run.update_status(rejectionSignatures={"1": signature},
                           state=PAUSED_STATE, inputQuestion=None,
                           pauseReason="stagnation")
    resp = _post_rule(sup1, "ruling filed against an unrelated live pause")
    assert resp.status_code == 202, resp.text
    status = sup1.run.read_status()
    assert status["state"] == PAUSED_STATE
    assert status["pauseReason"] == "stagnation"
    assert not sup1._pause.is_set()


# ===========================================================================
# 2. The re-ask itself: exactly one review call, the ruling authoritative in
#    its prompt, and the fresh verdict recorded with the honoured event.
# ===========================================================================

def test_resuming_with_a_ruling_reasks_review_exactly_once_authoritatively_and_records_the_honoured_event(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _pause_on_operator_input_needed(root)
    signature = (sup1.run.read_status().get("rejectionSignatures") or {}).get("1")
    assert signature is not None

    ruling_text = ("Waived: findings B and A.1 are residuals, not "
                   "blockers -- ship it.")
    # Cure (F01, task 015): filing the ANSWER over the real `/rule` route
    # must itself release the current `operator_input_needed` pause -- no
    # separate `resume()`/`POST /resume` call anywhere in this test.
    resp = _post_rule(sup1, ruling_text)
    assert resp.status_code == 202, resp.text
    released_status = sup1.run.read_status()
    assert released_status["state"] == "running"
    assert released_status.get("pauseReason") is None
    assert sup1._pause.is_set()
    assert ruling_entries(root)[-1]["signature"] == signature

    # Episode 2: a brand-new process over the same on-disk run dir.
    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1

    calls = []

    async def _review_honours_it(prompt, transcript, **kw):
        calls.append(prompt)
        findings.write_findings_json(sup2.run.findings_json_file, [])
        return _result(json.dumps({"verdict": "verified"}))
    sup2.runner.run = _review_honours_it  # type: ignore[method-assign]

    outcome = _bounded(sup2._step_review())

    assert outcome == ("done", "succeeded")
    # the probe that counts review attempts: exactly one re-ask.
    assert len(calls) == 1
    prompt = calls[0]
    assert ruling_text in prompt
    assert "### Ruling 1 (" in prompt
    assert "authoritative" in prompt.lower()

    honoured = _events(root, "operator_ruling_honoured")
    assert len(honoured) == 1
    ev = honoured[0]
    assert ev["approach"] == 1
    assert ev["signature"] == signature
    assert ev["rulingSeq"] == 1
    assert ev["rulingFile"].endswith(".md")
    assert ev["verdict"] == "verified"


def test_a_ruling_filed_ahead_of_time_with_no_signature_is_never_reported_honoured(
        tmp_path):
    """A ruling filed with `signature=None` ("unscoped", filed ahead of any
    rejection) must not be reported as the ruling review honoured just
    because one happens to exist -- the lookup is a signature equality
    check, never "any ruling is present"."""
    root = tmp_path / "run"
    sup1 = _pause_on_operator_input_needed(root)
    write_ruling(root, "Filed ahead of time, unscoped.")  # signature=None
    sup1.resume()

    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1

    async def _review_verifies(prompt, transcript, **kw):
        findings.write_findings_json(sup2.run.findings_json_file, [])
        return _result(json.dumps({"verdict": "verified"}))
    sup2.runner.run = _review_verifies  # type: ignore[method-assign]

    outcome = _bounded(sup2._step_review())

    assert outcome == ("done", "succeeded")
    assert _events(root, "operator_ruling_honoured") == []


def test_a_ruling_filed_against_a_different_signature_is_not_honoured(tmp_path):
    root = tmp_path / "run"
    sup1 = _pause_on_operator_input_needed(root)
    write_ruling(root, "Ruling for an unrelated wall.",
                signature="totally-different-wall")
    sup1.resume()

    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1

    async def _review_verifies(prompt, transcript, **kw):
        findings.write_findings_json(sup2.run.findings_json_file, [])
        return _result(json.dumps({"verdict": "verified"}))
    sup2.runner.run = _review_verifies  # type: ignore[method-assign]

    outcome = _bounded(sup2._step_review())

    assert outcome == ("done", "succeeded")
    assert _events(root, "operator_ruling_honoured") == []


def test_resuming_without_any_ruling_at_all_emits_no_honoured_event(tmp_path):
    """Task 009 (PRD v0.27 requirement C.2, #274): rewritten from the
    version that used to pin the pause/re-review/re-park loop C.2 closes
    -- a bare `resume()` with no ruling filed now records a one-shot
    'proceed' for this exact rejection's signature, and the SAME rejection
    reaching `_step_review()` a second time consumes it and takes the
    ordinary archive-and-replan edge instead of parking a second time (see
    `tests/test_unpause_one_shot_proceed.py` for the dedicated coverage of
    that mechanism). This test's own remaining job is unchanged: no
    ruling was ever filed, so no `operator_ruling_honoured` event fires
    either way.
    """
    root = tmp_path / "run"
    sup1 = _pause_on_operator_input_needed(root)
    sup1.resume()

    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file, _MIXED_FINDINGS)

    calls = []

    async def _review_rejects_again(prompt, transcript, **kw):
        calls.append(prompt)
        return _result(json.dumps(
            {"verdict": "rejected", "reason": "still no ruling"}))
    sup2.runner.run = _review_rejects_again  # type: ignore[method-assign]

    outcome = _bounded(sup2._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert len(calls) == 1
    assert "### Ruling 1 (" not in calls[0]
    assert _events(root, "operator_ruling_honoured") == []
    assert len(_events(root, "operator_escalation")) == 1


# ===========================================================================
# 3. The honoured event is never emitted on an ILLEGIBLE fresh verdict
#    (review's own contract ladder exhausted with no readable verdict at
#    all) -- there is no "fresh verdict" to attribute a ruling to yet.
# ===========================================================================

def test_an_unrepaired_contract_on_the_reask_emits_no_honoured_event(tmp_path):
    root = tmp_path / "run"
    sup1 = _pause_on_operator_input_needed(root)
    sup1.run.add_ruling("Ship it; these are residuals now.")
    sup1.resume()

    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1

    async def _review_says_nothing_parseable(prompt, transcript, **kw):
        return _result("no contract payload at all, just prose")
    sup2.runner.run = _review_says_nothing_parseable  # type: ignore[method-assign]

    outcome = _bounded(sup2._step_review())

    assert outcome == ("advance", "paused", "review_contract_unrepaired")
    assert _events(root, "operator_ruling_honoured") == []
