"""Task 025 (#18.4): `ralphctl fault <run>` -- the fault explained, not just
classified.

Everything needed was already on disk and nothing joined it up: the
classifier's verdict per iteration (`faultClass` in
`iterations/NNNN/meta.json`), the retry attempts and their real backoffs
(`infra_retry` events), the degraded half of the status contract
(`health`/`infraWait`/`infraWaitTotalS`). An operator reading
`faultClass: "infra"` still had to know `engine/faults.py`' signature table by
heart to learn WHY, grep `events.jsonl` for the attempt number, and do the
outage-budget arithmetic by hand.

What is pinned here:

  * ONE ladder: `classify_fault` delegates to `explain_fault`, so an
    explanation can never describe a decision the engine did not make -- every
    family case of `faults.INFRA_SIGNATURES` is asserted to yield BOTH the
    infra verdict and a named signature, and every family label has a
    description;
  * the shared shaping/wording (`state.fault_explanation` /
    `fault_summary_lines` / `format_fault_signature` / `format_fault_ladder` /
    `format_fault_budget`), which task 026's hub dialog renders verbatim;
  * the on-disk contract: no container, no live API, no snapshot notice
    (status.json, events.jsonl and the metas are the engine's own writes);
  * unknown is not zero (#15/#10's rule again): a run that never faulted says
    `NO_FAULT`, a fault whose meta.json is unreadable is still reported from
    the run's own retry events instead of being invented or dropped, and a
    verdict divergence is shown rather than silently resolved;
  * the ladder is the run's OWN recorded backoffs, so a wait cut short by
    POST /retry or clamped by the remaining budget reads truthfully.

Tiers: unit (the classifier's explanation + the formatters + the shaping),
black-box `ralphctl fault` over hand-written run dirs (container gone), and one
REAL engine run reusing test_fault_class_meta.py's infra-fault scenario (a
worker invocation hung until the startup watchdog killed it).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ralphd.engine.faults import (
    FAULT_REASON_ABORT_INTERRUPTED,
    FAULT_REASON_ABORT_RECORDED,
    FAULT_REASON_INTERRUPTED,
    FAULT_REASON_NO_TRAFFIC_TIMEOUT,
    FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED,
    FAULT_REASON_NOT_A_FAILURE,
    FAULT_REASON_OPERATOR_ABORT,
    FAULT_REASON_SIGNATURE,
    FAULT_REASON_WORK,
    INFRA_FAMILY_DESCRIPTIONS,
    INFRA_SIGNATURES,
    classify_fault,
    explain_fault,
    matched_signature,
)
from ralphd.engine.state import (
    FAULT_LADDER_NONE,
    FAULT_LADDER_REFLECT_DELAY,
    FAULT_LADDER_UNCAPPED,
    FAULT_REASON_FROM_EVENT,
    FAULT_RECOVERED_NOTICE,
    FAULT_SIGNATURE_NONE,
    FAULT_VERDICT_DIVERGED_NOTICE,
    NO_FAULT,
    fault_explanation,
    fault_summary_lines,
    fault_text,
    format_fault_budget,
    format_fault_ladder,
    format_fault_signature,
    read_events,
    utc_from_epoch,
)
from tests.conftest import RALPHCTL

DNS_ERROR = "request to https://aigw.internal/v1 failed, reason: getaddrinfo EAI_AGAIN aigw.internal"


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _dead_run(tmp_path: Path, run_id: str = "faulty", **status) -> tuple[Path, Path]:
    """(registry, run_dir) for a run with no container at all: no host.json,
    so nothing can even try to reach a live API."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    doc = {"runId": run_id, "state": "failed", "iterationsUsed": 3}
    doc.update(status)
    (run_dir / "status.json").write_text(json.dumps(doc))
    return registry, run_dir


def _write_iteration(run_dir: Path, n: int, meta, **over) -> Path:
    """An iteration dir. `meta=None` writes NO meta.json; a str writes that
    text verbatim (a truncated file)."""
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    if isinstance(meta, str):
        (d / "meta.json").write_text(meta)
    elif meta is not None:
        (d / "meta.json").write_text(json.dumps({**meta, **over}))
    return d


def _fault_meta(n: int = 3, **over) -> dict:
    meta = {"number": n, "phase": "worker", "approach": 1,
            "startedAt": "2026-09-04T10:00:00Z", "endedAt": "2026-09-04T10:00:31Z",
            "exitCode": 0, "interrupted": False, "timedOut": False,
            "noTrafficTimeout": False, "sawComplete": False, "sawVerified": False,
            "error": DNS_ERROR, "faultClass": "infra", "usage": {}}
    meta.update(over)
    return meta


def _events_file(run_dir: Path, events: list[dict], truncate_last: bool = False) -> None:
    text = "".join(json.dumps(ev) + "\n" for ev in events)
    if truncate_last:
        text = text.rstrip("\n")[:-12]
    (run_dir / "events.jsonl").write_text(text)


def _retry(attempt: int, backoff: float, waited: float, **over) -> dict:
    ev = {"id": attempt, "type": "infra_retry", "phase": "worker",
          "attempt": attempt, "maxAttempts": None, "error": DNS_ERROR,
          "noTrafficTimeout": False, "instantFailure": False,
          "backoffS": backoff, "waitedS": waited, "budgetS": 14400}
    ev.update(over)
    return ev


def _wait(**over) -> dict:
    wait = {"since": "2026-09-04T10:00:31Z", "attempt": 3, "error": DNS_ERROR,
            "phase": "worker", "nextAttemptAt": "2026-09-04T10:02:31Z",
            "waitedS": 90.0, "budgetS": 14400.0, "remainingS": 14310.0}
    wait.update(over)
    return wait


# ------------------------------------------------- unit: the ONE fault ladder
def test_classify_fault_delegates_to_explain_fault():
    """Both halves of one decision: whatever inputs are thrown at them, the
    verdict `explain_fault` reasons about IS the verdict the engine acts on."""
    cases = [
        {},
        {"error_text": DNS_ERROR, "exit_code": 1, "produced_traffic": True},
        {"exit_code": 1, "produced_traffic": True},
        {"exit_code": 1},
        {"no_traffic_timeout": True},
        {"error_text": "aborted", "operator_abort": True},
        {"error_text": DNS_ERROR, "operator_abort": True},
        {"timed_out": True, "produced_traffic": True},
        {"error_text": "", "exit_code": 0},
    ]
    for kw in cases:
        assert explain_fault(**kw)["faultClass"] == classify_fault(**kw), kw


@pytest.mark.parametrize("family,pattern", INFRA_SIGNATURES)
def test_every_signature_row_is_named_and_described(family, pattern):
    assert family in INFRA_FAMILY_DESCRIPTIONS, \
        f"signature {pattern!r} belongs to an undescribed family {family!r}"
    assert INFRA_FAMILY_DESCRIPTIONS[family].strip()


def test_infra_family_descriptions_have_no_orphans():
    assert set(INFRA_FAMILY_DESCRIPTIONS) == {f for f, _ in INFRA_SIGNATURES}


# The verbatim shapes tests/test_fault_classifier.py pins as infra: every one
# of them must also be *explainable*, i.e. yield a named signature.
_FAMILY_TEXTS = [
    ("dns", "Error: getaddrinfo ENOTFOUND aigw.internal.example"),
    ("dns", DNS_ERROR),
    ("tcp", "connect ECONNREFUSED 127.0.0.1:8080"),
    ("tcp", "read ECONNRESET"),
    ("stream", "write EPIPE"),
    ("stream", "Error: socket hang up"),
    ("stream", "Error: aborted: Premature close"),
    ("tls", "TLS handshake timeout talking to the gateway"),
    ("tls", "unable to get local issuer certificate: certificate verify failed"),
    ("sdk", "Connection error."),
    ("http-5xx", "upstream request failed: 502 Bad Gateway"),
    ("http-5xx", "ServiceUnavailableException: Bedrock is unavailable"),
    ("backpressure", "429 Too Many Requests"),
    ("backpressure", "ThrottlingException: Too many requests, please wait"),
    ("backpressure", "Overloaded"),
    ("bedrock-stream", "ModelStreamErrorException: stream terminated by upstream"),
    ("capacity", "You exceeded your current quota for this model"),
]


@pytest.mark.parametrize("family,text", _FAMILY_TEXTS,
                         ids=[f"{f}-{i}" for i, (f, _) in enumerate(_FAMILY_TEXTS)])
def test_infra_text_yields_both_the_verdict_and_the_signature(family, text):
    sig = matched_signature(text)
    assert sig is not None, text
    assert sig["family"] == family
    assert sig["match"], sig
    assert sig["match"].lower() in text.lower()
    assert sig["description"] == INFRA_FAMILY_DESCRIPTIONS[family]
    exp = explain_fault(error_text=text, exit_code=1, produced_traffic=True)
    assert exp["faultClass"] == "infra"
    assert exp["reason"] == FAULT_REASON_SIGNATURE
    assert exp["signature"] == sig


def test_ordinary_work_failure_has_no_signature():
    assert matched_signature("ruff check failed: 2 errors") is None
    assert matched_signature("") is None
    assert matched_signature(None) is None
    exp = explain_fault(error_text="pytest failed: 3 tests", exit_code=1,
                        produced_traffic=True)
    assert exp == {"faultClass": "work", "reason": FAULT_REASON_WORK,
                   "signature": None}


def test_explain_fault_names_the_branch_that_decided():
    assert explain_fault()["reason"] == FAULT_REASON_NOT_A_FAILURE
    assert explain_fault(no_traffic_timeout=True)["reason"] == \
        FAULT_REASON_NO_TRAFFIC_TIMEOUT
    assert explain_fault(exit_code=1)["reason"] == \
        FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED
    aborted = explain_fault(error_text="aborted", operator_abort=True)
    assert aborted["faultClass"] == "work"
    assert aborted["reason"] == FAULT_REASON_ABORT_RECORDED
    assert explain_fault(error_text="aborted", operator_abort=True,
                         operator_abort_recorded=True)["reason"] == \
        FAULT_REASON_OPERATOR_ABORT


def test_operator_abort_still_reports_the_signature_it_overrode():
    """The carve-out changes the VERDICT, not the facts: an operator has to be
    able to see the error text looked infra and was deliberately not retried."""
    exp = explain_fault(error_text=DNS_ERROR, operator_abort=True,
                        operator_abort_recorded=True)
    assert exp["faultClass"] == "work"
    assert exp["reason"] == FAULT_REASON_OPERATOR_ABORT
    assert exp["signature"]["family"] == "dns"


# ------------------------------ steering 004: a reason may not out-claim its
# input. `operator_abort` is true for a POST /abort, a POST /interrupt AND the
# engine giving up on its own (loop.py's own comment says the flag cannot tell
# them apart), so only `operator_abort_recorded` may be explained as a person.
def test_a_recorded_abort_alone_is_never_blamed_on_the_operator():
    """THE real case: this run's iteration 38 -- a `pkill` against the engine
    set `_abort_reason` to "signal 15", so `operator_abort_requested` was true
    with no operator anywhere near a /abort request. It produced traffic, wrote
    no error text and was interrupted."""
    exp = explain_fault(error_text="", exit_code=None, interrupted=True,
                        produced_traffic=True, operator_abort=True)
    assert exp["faultClass"] == "work", "classification unchanged (issue #23)"
    assert exp["reason"] == FAULT_REASON_ABORT_INTERRUPTED
    assert "operator" not in exp["reason"].lower(), exp["reason"]
    assert exp["reason"] != FAULT_REASON_WORK

    # ... and with nothing interrupted either (an engine-side give-up scored
    # against a plain nonzero exit): still neutral.
    quiet = explain_fault(error_text="boom", exit_code=2, produced_traffic=True,
                          operator_abort=True)
    assert quiet["reason"] == FAULT_REASON_ABORT_RECORDED
    # it may LIST the operator among the possible causes; it may not pick one
    assert "not established" in quiet["reason"], quiet["reason"]
    assert "operator-requested" not in quiet["reason"], quiet["reason"]


def test_an_established_operator_abort_is_allowed_to_say_operator():
    """`_operator_abort_recorded` is what POST /abort sets and the engine's own
    give-up does not -- the one input that establishes a person."""
    exp = explain_fault(error_text="", interrupted=True, produced_traffic=True,
                        operator_abort=True, operator_abort_recorded=True)
    assert exp["faultClass"] == "work"
    assert exp["reason"] == FAULT_REASON_OPERATOR_ABORT
    assert "operator-requested" in exp["reason"]


def test_a_signal_terminated_iteration_is_named_as_one():
    """"What killed my iteration?" -- "the agent reached the model and then
    failed" is the wrong answer when a signal ended it."""
    killed = explain_fault(error_text="", exit_code=None, interrupted=True,
                           produced_traffic=True)
    assert killed["faultClass"] == "work"
    assert killed["reason"] == FAULT_REASON_INTERRUPTED
    # the genuine work failure is untouched
    failed = explain_fault(error_text="pytest failed: 3 tests", exit_code=1,
                           produced_traffic=True)
    assert failed["reason"] == FAULT_REASON_WORK


@pytest.mark.parametrize("kw", [
    {"error_text": "aborted", "interrupted": True, "operator_abort": True},
    {"error_text": "aborted", "interrupted": True, "produced_traffic": True,
     "operator_abort": True},
    {"error_text": DNS_ERROR, "operator_abort": True},
    {"no_traffic_timeout": True, "operator_abort": True},
    {"exit_code": 0},
])
def test_operator_abort_recorded_is_explanation_only(kw):
    """The new input words the reason; it must not move a single verdict --
    re-classifying these shapes is issue #23 and stays out of task 025."""
    plain = explain_fault(**kw)
    known = explain_fault(**kw, operator_abort_recorded=True)
    assert plain["faultClass"] == known["faultClass"]
    assert classify_fault(**kw) == classify_fault(
        **kw, operator_abort_recorded=True)
    assert plain["signature"] == known["signature"]


def test_the_loop_threads_who_recorded_the_abort_into_the_explanation(
        tmp_path, monkeypatch):
    """End of the wire: `LoopSupervisor._classify_result`'s own inputs, fed to
    `explain_fault`, must word an engine-side give-up and an operator's abort
    differently -- while returning the same verdict for both."""
    import ralphd.engine.loop as loop_mod
    from ralphd.engine.config import JobConfig
    from ralphd.engine.runner import IterationResult
    from ralphd.engine.state import RunDir

    seen: list[dict] = []

    def spy(**kwargs):
        seen.append(kwargs)
        return classify_fault(**kwargs)

    monkeypatch.setattr(loop_mod, "classify_fault", spy)

    def reason_for(abort: str, *, recorded: bool):
        root = tmp_path / ("recorded" if recorded else "selfgiveup")
        sup = loop_mod.LoopSupervisor(JobConfig(run_id="unit"),
                                      RunDir(root=root), root)
        sup._abort_reason = abort
        sup._operator_abort_recorded = recorded
        result = IterationResult(exit_code=None, interrupted=True)
        result.final_text = "partial work"  # traffic: it reached the model
        seen.clear()
        verdict = sup._classify_result(result)
        assert len(seen) == 1, seen
        assert seen[-1]["operator_abort"] is True
        assert seen[-1]["operator_abort_recorded"] is recorded
        return explain_fault(**seen[-1])["reason"], verdict

    killed, killed_verdict = reason_for("signal 15", recorded=False)
    asked, asked_verdict = reason_for("aborted by operator", recorded=True)
    assert killed == FAULT_REASON_ABORT_INTERRUPTED
    assert "operator" not in killed.lower(), killed
    assert asked == FAULT_REASON_OPERATOR_ABORT
    assert killed_verdict == asked_verdict == "work", "verdicts unchanged (#23)"


# --------------------------------------------------------- unit: formatters
def test_format_fault_signature_names_family_pattern_and_match():
    line = format_fault_signature(matched_signature(DNS_ERROR))
    assert line.startswith("dns -- ")
    assert INFRA_FAMILY_DESCRIPTIONS["dns"] in line
    assert "pattern EAI_AGAIN" in line
    assert 'matched "EAI_AGAIN"' in line
    for junk in (None, {}, "nope", {"pattern": "x"}):
        assert format_fault_signature(junk) == FAULT_SIGNATURE_NONE


def test_format_fault_ladder_reads_the_runs_own_backoffs():
    ladder = {"attempt": 3, "maxAttempts": None, "backoffsS": [30, 60, 120],
              "nextAttemptAt": "2026-09-04T10:02:31Z",
              "nextAttemptAtLocal": "2026-09-04 12:02:31 +0200"}
    line = format_fault_ladder(ladder)
    assert line.startswith("attempt 3")
    assert FAULT_LADDER_UNCAPPED in line
    assert "waits so far 30s, 1m, 2m" in line
    assert "2026-09-04 12:02:31 +0200" in line
    capped = format_fault_ladder({**ladder, "maxAttempts": 6})
    assert capped.startswith("attempt 3 of 6")
    assert FAULT_LADDER_UNCAPPED not in capped


def test_format_fault_ladder_degrades_and_names_the_reflect_delay():
    for junk in (None, {}, "nope", {"attempt": None}):
        assert format_fault_ladder(junk) == FAULT_LADDER_NONE
    # attempt 0 is the pre-reflect delay, deliberately not a retry
    assert format_fault_ladder({"attempt": 0}) == FAULT_LADDER_REFLECT_DELAY


def test_format_fault_budget_says_spent_left_and_the_run_total():
    line = format_fault_budget({"waitedS": 90.0, "budgetS": 14400.0,
                                "remainingS": 14310.0, "totalWaitedS": 900.0})
    assert "1m 30s of 4h spent waiting" in line
    assert "3h 58m left" in line
    assert "15m of infra waits in this run" in line
    # an episode that is all of the run's waiting does not repeat itself
    only = format_fault_budget({"waitedS": 90.0, "budgetS": 14400.0,
                                "remainingS": 14310.0, "totalWaitedS": 90.0})
    assert "in this run" not in only
    # no budget arithmetic recorded at all: never "0s of 0s"
    assert "0s" not in format_fault_budget({})
    assert format_fault_budget({"budgetS": None, "totalWaitedS": 60.0}) == \
        "1m of infra waits in this run"


# --------------------------------------------------- unit: the shaping itself
def test_read_events_filters_and_survives_a_half_written_line(tmp_path):
    _events_file(tmp_path, [_retry(1, 30, 0), {"id": 2, "type": "log"},
                            _retry(2, 60, 30)], truncate_last=True)
    events = read_events(tmp_path)
    assert [ev.get("type") for ev in events] == ["infra_retry", "log"]
    assert [ev["attempt"] for ev in read_events(tmp_path, ("infra_retry",))] == [1]
    assert read_events(tmp_path / "nope") == []
    (tmp_path / "empty").mkdir()
    assert read_events(tmp_path / "empty") == []


def test_no_fault_recorded_is_said_out_loud(tmp_path):
    _, run_dir = _dead_run(tmp_path, state="succeeded")
    _write_iteration(run_dir, 1, _fault_meta(1, error=None, faultClass=None,
                                             usage={"totalTokens": 10}))
    exp = fault_explanation(run_dir)
    assert exp["hasFault"] is False
    assert exp["faultClass"] is None and exp["signature"] is None
    assert exp["ladder"] is None and exp["budget"] is None
    assert fault_summary_lines(exp) == [NO_FAULT]
    assert fault_text(exp) == NO_FAULT


def test_explanation_joins_meta_events_and_status(tmp_path):
    _, run_dir = _dead_run(tmp_path, state="running", health="degraded",
                           infraWaitTotalS=90.0, infraWait=_wait())
    _write_iteration(run_dir, 1, _fault_meta(1, error=None, faultClass=None))
    _write_iteration(run_dir, 3, _fault_meta(3))
    _events_file(run_dir, [_retry(1, 30, 0), _retry(2, 60, 30), _retry(3, 120, 90)])

    exp = fault_explanation(run_dir)
    assert exp["hasFault"] is True
    assert exp["faultClass"] == "infra"
    assert exp["reason"] == FAULT_REASON_SIGNATURE
    assert exp["signature"]["family"] == "dns"
    assert exp["iteration"] == 3 and exp["phase"] == "worker"
    assert exp["ladder"]["attempt"] == 3
    assert exp["ladder"]["backoffsS"] == [30, 60, 120]
    assert exp["ladder"]["maxAttempts"] is None
    assert exp["budget"] == {"waitedS": 90.0, "budgetS": 14400.0,
                             "remainingS": 14310.0, "totalWaitedS": 90.0,
                             "display": exp["budget"]["display"]}
    assert exp["waiting"] is True and exp["health"] == "degraded"
    assert exp["notices"] == []
    # the iteration's own shaping comes along, so the exit reason is not
    # re-worded here
    assert exp["iterationDetail"]["exitReason"].endswith("[infra fault]")


def test_last_fault_wins_over_an_earlier_one(tmp_path):
    _, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 1, _fault_meta(1))
    _write_iteration(run_dir, 2, _fault_meta(2, error="pytest failed: 3 tests",
                                             exitCode=1, faultClass="work",
                                             usage={"totalTokens": 900}))
    exp = fault_explanation(run_dir)
    assert exp["iteration"] == 2
    assert exp["faultClass"] == "work"
    assert exp["reason"] == FAULT_REASON_WORK
    assert exp["signature"] is None


def test_recovered_episode_reports_recovery_and_no_ladder(tmp_path):
    """`infra_recovered` is the engine's own episode boundary: the attempts
    before it belong to an outage that is over."""
    _, run_dir = _dead_run(tmp_path, state="succeeded", health="ok",
                           infraWaitTotalS=90.0)
    _write_iteration(run_dir, 3, _fault_meta(3))
    _events_file(run_dir, [_retry(1, 30, 0), _retry(2, 60, 30),
                           {"id": 9, "type": "infra_recovered", "health": "ok",
                            "infraWaitTotalS": 90.0}])
    exp = fault_explanation(run_dir)
    assert exp["recovered"] is True
    assert exp["ladder"] is None, "a finished episode is not a live ladder"
    assert exp["budget"]["totalWaitedS"] == 90.0
    lines = fault_summary_lines(exp)
    assert any(FAULT_RECOVERED_NOTICE in line for line in lines), lines


def test_unreadable_meta_still_reports_the_episode(tmp_path):
    """A truncated meta.json is ignorance, not an absent fault: the retry
    events prove the engine is acting on one, and the class is its own."""
    _, run_dir = _dead_run(tmp_path, state="running", health="degraded",
                           infraWaitTotalS=90.0, infraWait=_wait())
    _write_iteration(run_dir, 3, '{"number": 3, "phase": "wor')
    _events_file(run_dir, [_retry(1, 30, 0), _retry(2, 60, 30), _retry(3, 120, 90)])
    exp = fault_explanation(run_dir)
    assert exp["hasFault"] is True
    assert exp["faultClass"] == "infra"
    assert exp["reason"] == FAULT_REASON_FROM_EVENT
    assert exp["iteration"] is None
    assert exp["signature"]["family"] == "dns"
    assert exp["ladder"]["attempt"] == 3


def test_verdict_divergence_is_shown_not_resolved(tmp_path):
    """An operator abort is recorded as `work` though the text looks infra --
    the ENGINE's verdict stands and the divergence is said out loud."""
    _, run_dir = _dead_run(tmp_path, state="aborted",
                           abortReason="operator abort: wrong PRD")
    _write_iteration(run_dir, 2, _fault_meta(2, faultClass="work"))
    exp = fault_explanation(run_dir)
    assert exp["faultClass"] == "work", "never overwritten by the re-derivation"
    assert FAULT_VERDICT_DIVERGED_NOTICE in exp["notices"]
    lines = fault_summary_lines(exp)
    assert FAULT_VERDICT_DIVERGED_NOTICE in lines
    assert any(line.startswith("gave up:") and "wrong PRD" in line
               for line in lines), lines


def test_a_signal_killed_iteration_is_not_explained_as_an_operators_doing(tmp_path):
    """Steering 004, through the whole shaping: this run's own iteration 38 --
    interrupted, traffic recorded, no error text, and `abortReason: signal 15`
    from a `pkill` nobody's operator asked for. The explanation may say a signal
    ended it and may quote the recorded reason verbatim; it may NOT say a person
    did it, and it may not say the agent "reached the model and then failed"."""
    registry, run_dir = _dead_run(tmp_path, state="failed",
                                  abortReason="signal 15")
    _write_iteration(run_dir, 38, _fault_meta(
        38, error=None, exitCode=None, interrupted=True, faultClass="work",
        usage={"totalTokens": 505628, "outputTokens": 18320}))

    exp = fault_explanation(run_dir)
    assert exp["faultClass"] == "work", "the engine's verdict is untouched (#23)"
    assert exp["reason"] == FAULT_REASON_INTERRUPTED
    assert exp["reason"] not in (FAULT_REASON_OPERATOR_ABORT, FAULT_REASON_WORK)
    assert exp["notices"] == [], "derived and recorded agree: nothing diverged"

    lines = fault_summary_lines(exp)
    because = [line for line in lines if line.startswith("because:")]
    assert because and "operator" not in because[0].lower(), lines
    assert any(line.startswith("gave up:") and "signal 15" in line
               for line in lines), lines

    # and the same through the CLI an operator would actually type
    out = _ctl(registry, "fault", "faulty")
    assert out.returncode == 0, out.stderr
    assert FAULT_REASON_INTERRUPTED in out.stdout
    assert "operator-requested" not in out.stdout
    assert "operator-initiated" not in out.stdout


def test_the_error_text_is_never_printed_twice(tmp_path):
    """The ranked verdict (`error (exit N): ...`) already quotes the error, so
    the standalone `error:` line appears only when the verdict does not carry
    it -- a watchdog kill, or an explanation sourced from the retry events."""
    _, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 3, _fault_meta(3))
    lines = fault_summary_lines(fault_explanation(run_dir))
    assert not [line for line in lines if line.startswith("error:")], lines
    assert len([line for line in lines if DNS_ERROR in line]) == 1, lines

    # a no-traffic watchdog kill has no error text at all: no line, no signature
    _, quiet = _dead_run(tmp_path / "b", "quiet")
    _write_iteration(quiet, 2, _fault_meta(2, error=None, exitCode=None,
                                           noTrafficTimeout=True, timedOut=True))
    quiet_lines = fault_summary_lines(fault_explanation(quiet))
    assert not [line for line in quiet_lines if line.startswith("error:")]
    assert f"signature: {FAULT_SIGNATURE_NONE}" in quiet_lines

    # event-sourced (unreadable meta): the error is the only place it can be said
    _, evonly = _dead_run(tmp_path / "c", "evonly", health="degraded",
                          infraWait=_wait())
    _events_file(evonly, [_retry(1, 30, 0)])
    ev_lines = fault_summary_lines(fault_explanation(evonly))
    assert any(line.startswith("error:") and "EAI_AGAIN" in line
               for line in ev_lines), ev_lines


def test_junk_status_and_no_iterations_do_not_crash(tmp_path):
    run_dir = tmp_path / "junk"
    run_dir.mkdir()
    (run_dir / "status.json").write_text("[1, 2, 3")
    exp = fault_explanation(run_dir)
    assert exp["hasFault"] is False
    assert fault_summary_lines(exp) == [NO_FAULT]


def test_pre_reflect_delay_is_not_reported_as_a_retry(tmp_path):
    _, run_dir = _dead_run(tmp_path, state="failed", health="degraded",
                           infraWaitTotalS=0.0,
                           infraWait=_wait(attempt=0, phase="reflect",
                                           waitedS=0.0, budgetS=600.0,
                                           remainingS=600.0))
    _write_iteration(run_dir, 3, _fault_meta(3))
    _events_file(run_dir, [{"id": 7, "type": "reflect_infra_delay",
                            "phase": "reflect", "delayS": 30, "error": DNS_ERROR,
                            "budgetS": 600}])
    exp = fault_explanation(run_dir)
    assert exp["ladder"]["attempt"] == 0
    assert exp["ladder"]["display"].startswith(FAULT_LADDER_REFLECT_DELAY)
    assert "attempt 0" not in exp["ladder"]["display"]
    assert exp["budget"]["budgetS"] == 600.0


# --------------------------------------------------------- black-box: the CLI
def test_ralphctl_fault_explains_a_dead_runs_last_fault(tmp_path):
    registry, run_dir = _dead_run(tmp_path, state="failed",
                                  infraWaitTotalS=210.0,
                                  abortReason=("infra fault: worker iteration "
                                               "failed throughout a 210s infra "
                                               "outage"))
    _write_iteration(run_dir, 3, _fault_meta(3))
    _events_file(run_dir, [_retry(1, 30, 0), _retry(2, 60, 30),
                           _retry(3, 120, 90, maxAttempts=3)])
    r = _ctl(registry, "fault", "faulty")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "run:       faulty" in out
    assert "fault:     infra (iteration 3, phase worker)" in out
    assert FAULT_REASON_SIGNATURE in out
    assert "signature: dns --" in out and "pattern EAI_AGAIN" in out
    assert "attempt 3 of 3" in out
    assert "waits so far 30s, 1m, 2m" in out
    assert "1m 30s of 4h spent waiting" in out
    assert "3m 30s of infra waits in this run" in out
    assert "gave up:" in out
    # on-disk only: no snapshot/liveness notice on either stream
    assert "snapshot" not in r.stderr.lower()
    assert r.stderr.strip() == ""


def test_ralphctl_fault_json_carries_the_shaping_and_the_text(tmp_path):
    registry, run_dir = _dead_run(tmp_path, state="running", health="degraded",
                                  infraWaitTotalS=90.0, infraWait=_wait())
    _write_iteration(run_dir, 3, _fault_meta(3))
    _events_file(run_dir, [_retry(1, 30, 0), _retry(2, 60, 30), _retry(3, 120, 90)])
    r = _ctl(registry, "--json", "fault", "faulty")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["runId"] == "faulty"
    assert doc["faultClass"] == "infra"
    assert doc["signature"]["pattern"] == "EAI_AGAIN"
    assert doc["ladder"]["attempt"] == 3 and doc["ladder"]["backoffsS"] == [30, 60, 120]
    assert doc["budget"]["budgetS"] == 14400.0
    assert doc["waiting"] is True
    # the human rendering, byte-for-byte, is the same block --json carries
    human = _ctl(registry, "fault", "faulty")
    assert human.stdout.rstrip("\n").split("\n")[1:] == doc["summaryLines"]
    assert doc["text"] == "\n".join(doc["summaryLines"])


def test_ralphctl_fault_on_a_clean_run_says_no_fault(tmp_path):
    registry, run_dir = _dead_run(tmp_path, state="succeeded", health="ok")
    _write_iteration(run_dir, 1, _fault_meta(1, error=None, faultClass=None,
                                             usage={"totalTokens": 42}))
    r = _ctl(registry, "fault", "faulty")
    assert r.returncode == 0, r.stderr
    assert NO_FAULT in r.stdout
    assert "signature:" not in r.stdout


def test_ralphctl_fault_unknown_run_exits_3(tmp_path):
    registry, _ = _dead_run(tmp_path)
    r = _ctl(registry, "fault", "nope")
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)


def test_ralphctl_fault_reads_a_live_looking_run_without_a_container(tmp_path):
    """A run whose status.json says `running` with a pending backoff wait:
    nothing here needs the container, and the countdown target is rendered as
    an absolute local instant rather than a stale relative one."""
    registry, run_dir = _dead_run(
        tmp_path, state="running", health="degraded", infraWaitTotalS=30.0,
        infraWait=_wait(nextAttemptAt=utc_from_epoch(time.time() + 58)))
    _write_iteration(run_dir, 3, _fault_meta(3))
    _events_file(run_dir, [_retry(1, 30, 0), _retry(2, 60, 30), _retry(3, 120, 90)])
    r = _ctl(registry, "fault", "faulty")
    assert r.returncode == 0, r.stderr
    assert "health:    degraded (sitting out a backoff wait right now)" in r.stdout
    assert "next attempt at " in r.stdout


# --------------------------------------------------------- the REAL engine
def test_engine_infra_fault_is_explained_end_to_end(live):
    """The fixture is test_fault_class_meta.py's own infra scenario: one worker
    invocation hangs with zero NDJSON output until the startup-window watchdog
    kills it, so the run dir carries a REAL `faultClass: "infra"` iteration,
    real `infra_retry` events and a real budget spend -- read here after the
    engine (the stand-in for the container) is gone, with nothing
    hand-written."""
    e = live(
        run_id="faultreal",
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",   # invocation 1 (planning) is healthy
            "STUB_INFRA_HANG_COUNT": "2",  # then two hung worker attempts
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_MAX": "4",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.2,0.4,0.4",
        })
    status = e.wait_terminal(timeout=90)
    assert status["state"] == "succeeded"
    e.stop()  # container gone: nothing live to ask

    exp = fault_explanation(e.run_dir)
    assert exp["hasFault"] is True
    assert exp["faultClass"] == "infra"
    # the watchdog branch, not a text signature: nothing was ever said
    assert exp["reason"] == FAULT_REASON_NO_TRAFFIC_TIMEOUT
    assert exp["phase"] == "worker"
    assert exp["iteration"] is not None
    # the ladder is the engine's own: two attempts, its own backoffs, its cap
    assert exp["recovered"] is True, "the third attempt reached the model"
    assert exp["budget"]["totalWaitedS"] > 0
    retries = read_events(e.run_dir, ("infra_retry",))
    assert [ev["attempt"] for ev in retries] == [1, 2]
    assert all(ev["maxAttempts"] == 4 for ev in retries)

    # ... and the CLI prints exactly that, from the run dir alone.
    r = e.ralphctl("fault", e.run_id)
    assert r.returncode == 0, r.stderr
    assert "fault:     infra" in r.stdout
    assert FAULT_REASON_NO_TRAFFIC_TIMEOUT in r.stdout
    assert FAULT_SIGNATURE_NONE in r.stdout, \
        "a watchdog kill has no error text, so no signature may be claimed"
    assert FAULT_RECOVERED_NOTICE in r.stdout
    assert r.stdout.rstrip("\n").split("\n")[1:] == exp["summaryLines"]
