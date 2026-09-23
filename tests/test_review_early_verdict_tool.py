"""Task 025 (PRD requirement H.1/H.4, #264): review's own early-verdict
checkpoint -- the "H.2" task title's own success criteria are actually
H.1 ("write review's verdict to disk the moment it is formed") plus H.4
("atomically, bounded, survives SIGKILL") combined; see notes.md for the
title/criteria mismatch this task inherited.

Mirrors `tests/test_steer_hat_tools_live.py`'s own discipline for the
sibling mechanism this shares its whole spool/broker/extension plumbing
with: the REAL `hat_tools.SteerToolBroker`, restricted to
`hat_tools.REVIEW_WIRED_TOOLS`, executing the REAL `record-verdict`
handler -- no test-local stand-in for the write path under test.

1. `record-verdict` is wired for review, and for review only -- it is not
   a member of `STEER_WIRED_TOOLS`/`WORKER_WIRED_TOOLS`, and the review
   broker's own `wired_tools()` carries nothing else;
2. the generated extension registers exactly that one tool, with a schema
   that matches its documented usage payload;
3. a served call lands `state.write_review_early_verdict`'s own atomic
   write (rename-into-place) at `RunDir.review_early_verdict_file`, with a
   small, fixed key set -- never anything the hat's own args happened to
   include;
4. `reason` is capped at `state.REVIEW_EARLY_VERDICT_REASON_MAX_CHARS`;
5. an illegal `verdict` value is refused, and refusal never writes the
   checkpoint file at all (a previous valid checkpoint, if any, survives
   untouched);
6. `LoopSupervisor._hat_extensions("review")` is empty with no live review
   broker and carries exactly the review broker's own extension file while
   one is set -- the same "only while in flight" shape
   `_hat_extensions("steer")`/`_hat_extensions("worker")` already have.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ralphd.engine import hat_tools
from ralphd.engine.state import (
    REVIEW_EARLY_VERDICT_REASON_MAX_CHARS,
    RunDir,
    read_review_early_verdict,
)


def _run(tmp_path: Path) -> RunDir:
    run = RunDir(root=tmp_path / "run")
    run.root.mkdir(parents=True, exist_ok=True)
    return run


def _broker(run: RunDir) -> hat_tools.SteerToolBroker:
    return hat_tools.SteerToolBroker(
        run, vigilant=False, tools=frozenset(hat_tools.REVIEW_WIRED_TOOLS),
        source="review")


# ---------------------------------------------------------------------------
# 1. record-verdict is wired for review, and for review alone.
# ---------------------------------------------------------------------------


def test_record_verdict_is_review_only():
    assert set(hat_tools.REVIEW_WIRED_TOOLS) == {"record-verdict"}
    assert "record-verdict" not in hat_tools.STEER_WIRED_TOOLS
    assert "record-verdict" not in hat_tools.WORKER_WIRED_TOOLS


def test_the_review_brokers_own_wired_set_is_exactly_that_one_tool(tmp_path):
    broker = _broker(_run(tmp_path))
    assert set(broker.wired_tools()) == {"record-verdict"}


# ---------------------------------------------------------------------------
# 2. the generated extension registers exactly that tool, matching schema.
# ---------------------------------------------------------------------------


def test_the_generated_extension_registers_exactly_record_verdict(tmp_path):
    run = _run(tmp_path)
    broker = _broker(run)
    path = broker.place()
    source = path.read_text()
    names = re.findall(r'name:\s*"([a-z-]+)"', source)
    assert names == ["record-verdict"]
    assert "pi.registerTool" in source
    assert str(run.root) in source


def test_record_verdicts_schema_matches_its_documented_usage_payload():
    documented = set(hat_tools.usage_payload("record-verdict"))
    declared = set(re.findall(r"^      (\w+): ",
                              hat_tools.parameters_source("record-verdict"),
                              re.M))
    assert declared, "record-verdict declares no parameter fields"
    assert declared == documented


# ---------------------------------------------------------------------------
# 3. a served call lands the atomic, bounded checkpoint.
# ---------------------------------------------------------------------------


def test_a_served_call_writes_the_bounded_checkpoint_atomically(tmp_path):
    run = _run(tmp_path)
    run.update_status(approach=3)
    broker = _broker(run)
    result = broker.execute("record-verdict", {
        "verdict": "verified", "reason": "all four criteria checked",
        "findingsPath": "review-findings.md",
        # a hat-supplied field the payload never documents -- must never
        # reach the checkpoint file.
        "budget": 999999,
    })
    assert result["ok"] is True
    doc = read_review_early_verdict(run.root)
    assert doc == {
        "approach": 3,
        "verdict": "verified",
        "reason": "all four criteria checked",
        "findingsPath": "review-findings.md",
        "recordedAt": doc["recordedAt"],
    }
    assert set(doc) == {"approach", "verdict", "reason", "findingsPath",
                        "recordedAt"}
    assert "budget" not in doc


def test_the_write_is_a_rename_into_place(tmp_path, monkeypatch):
    run = _run(tmp_path)
    run.update_status(approach=1)
    broker = _broker(run)
    calls = []
    import os as os_module
    real_replace = os_module.replace

    def spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", spy_replace)
    broker.execute("record-verdict", {"verdict": "verified"})
    assert calls, "no os.replace call observed"
    assert calls[-1][1] == str(run.review_early_verdict_file)
    assert calls[-1][0] != calls[-1][1]  # a distinct tmp path, renamed in


# ---------------------------------------------------------------------------
# 4. `reason` is capped.
# ---------------------------------------------------------------------------


def test_reason_is_capped_at_the_documented_limit(tmp_path):
    run = _run(tmp_path)
    run.update_status(approach=1)
    broker = _broker(run)
    long_reason = "x" * (REVIEW_EARLY_VERDICT_REASON_MAX_CHARS + 500)
    broker.execute("record-verdict", {"verdict": "rejected",
                                      "reason": long_reason})
    doc = read_review_early_verdict(run.root)
    assert len(doc["reason"]) <= REVIEW_EARLY_VERDICT_REASON_MAX_CHARS
    assert doc["reason"].endswith("...")


# ---------------------------------------------------------------------------
# 5. an illegal verdict is refused, and never overwrites a good checkpoint.
# ---------------------------------------------------------------------------


def test_an_illegal_verdict_value_is_refused(tmp_path):
    run = _run(tmp_path)
    run.update_status(approach=1)
    broker = _broker(run)
    result = broker.execute("record-verdict",
                            {"verdict": "maybe-later-ish"})
    assert result["ok"] is False
    assert result["error"]
    assert read_review_early_verdict(run.root) is None


def test_a_refused_call_never_clobbers_a_previous_good_checkpoint(tmp_path):
    run = _run(tmp_path)
    run.update_status(approach=1)
    broker = _broker(run)
    broker.execute("record-verdict", {"verdict": "verified", "reason": "ok"})
    good = read_review_early_verdict(run.root)
    broker.execute("record-verdict", {"verdict": "nonsense"})
    assert read_review_early_verdict(run.root) == good


# ---------------------------------------------------------------------------
# 6. `_hat_extensions("review")` is empty unless a review broker is live.
# ---------------------------------------------------------------------------


def test_hat_extensions_review_is_empty_with_no_live_broker(tmp_path):
    from test_steer import _supervisor

    sup = _supervisor(tmp_path)
    assert sup._hat_extensions("review") == []


def test_hat_extensions_review_carries_the_review_brokers_own_file(tmp_path):
    from test_steer import _supervisor

    sup = _supervisor(tmp_path)
    run = sup.run
    broker = hat_tools.SteerToolBroker(
        run, vigilant=False, tools=frozenset(hat_tools.REVIEW_WIRED_TOOLS),
        source="review")
    path = broker.place()
    sup._review_tool_broker = broker
    try:
        assert sup._hat_extensions("review") == [str(path)]
        # still empty for every OTHER phase while review's own broker lives
        assert sup._hat_extensions("steer") == []
        assert sup._hat_extensions("worker") == []
    finally:
        sup._review_tool_broker = None


# ---------------------------------------------------------------------------
# 7. an ordinary, uninterrupted review still ends with a refined,
#    non-provisional verdict -- the checkpoint tool changes nothing about
#    the ordinary contract-line path.
# ---------------------------------------------------------------------------


def test_an_uninterrupted_review_still_ends_on_its_own_refined_verdict(tmp_path):
    """Drives the real `LoopSupervisor` end to end (`_run_job_core`), a
    scripted `worker`/`review` standing in for the hat's own subprocess --
    same shape `test_steer.py` itself uses. The review step calls the SAME
    live broker `_run_one_review_dispatch` places (`sup._review_tool_broker`
    is non-None exactly while the step runs, per section 6 above) with an
    early, then a refined, checkpoint -- and still ends the run on its own
    ordinary `{"verdict": "verified"}` contract line, non-provisional (this
    task wires no provisional marker at all; that is task 026/H.3's own
    scope)."""
    from test_steer import TASK_1, _ok, _run_resumed, _worker_completes

    seen_broker_during_step = []

    def _review_checkpoints_then_verifies(sup, prompt=""):
        broker = sup._review_tool_broker
        seen_broker_during_step.append(broker)
        assert broker is not None, (
            "the review broker must be live while a review iteration runs")
        first = broker.execute("record-verdict",
                               {"verdict": "rejected",
                                "reason": "still checking requirement B"})
        assert first["ok"] is True
        refined = broker.execute("record-verdict",
                                 {"verdict": "verified",
                                  "reason": "all requirements checked",
                                  "findingsPath": "review-findings.md"})
        assert refined["ok"] is True
        return _ok(json.dumps({"verdict": "verified"}))

    root = tmp_path / "run"
    sup, final_state = _run_resumed(
        root,
        {"worker": [_worker_completes(TASK_1)],
         "review": [_review_checkpoints_then_verifies]},
        tasks=[{"id": TASK_1, "status": "pending", "title": "do stuff"}],
    )

    assert final_state == "succeeded"
    status = sup.run.read_status()
    assert status.get("verdict") == "verified"
    assert "provisional" not in status
    assert sup._review_tool_broker is None, (
        "the review broker must not outlive the iteration it served")
    assert seen_broker_during_step and seen_broker_during_step[0] is not None

    checkpoint = read_review_early_verdict(sup.run.root)
    assert checkpoint["verdict"] == "verified", (
        "the checkpoint must have been REFINED to match the final verdict, "
        f"got {checkpoint!r}")
    assert checkpoint["reason"] == "all requirements checked"

