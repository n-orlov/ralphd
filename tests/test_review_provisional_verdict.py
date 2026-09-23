"""Task 026 (PRD requirement H.2/H.3, #264): a review killed by
`iteration_timeout_s` concludes the run on its own early-verdict checkpoint
(task 025's `record-verdict`) instead of holding with a null verdict, and
the record marks that verdict `provisional` -- legible on all four surfaces
a verdict is legible on: `status.json`, the CLI, the hub, and the terminal
record (`run-record.json`/`.md`). One test per surface, each reading the
explicit `provisional` field directly rather than deriving it from
`recordedAt`/`endedAt` timestamp arithmetic.

Fixture shape mirrors `tests/test_deadline.py` section A / `tests/
test_review_interrupted_not_rejection.py`: `_rir_supervisor(root, handlers,
iterations=3)` runs exactly plan -> worker(COMPLETE) -> review(interrupted),
with budget spent the instant that third attempt returns -- the same named
regression shape (`_review_unjudged_at_budget_end`), except this time the
review hat already called `record-verdict` before being cut short.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ralphd.engine import state
from ralphd.engine.run_record import RUN_RECORD_JSON_FILE, RUN_RECORD_MD_FILE
from ralphd.cli.main import _format_provisional_verdict_lines

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import _wait_for, pw  # noqa: F401 (browser fixture)
from test_review_interrupted_not_rejection import (
    _complete_task as _rir_complete_task,
    _interrupted as _rir_interrupted,
    _ok as _rir_ok,
    _plan as _rir_plan,
    _supervisor as _rir_supervisor,
)


def _bounded(coro, timeout: float = 30.0):
    """`_run_job_core()` self-loops on `review_interrupted` while budget
    is still available -- a bare `asyncio.run(...)` over it can wait
    forever on a repeating scripted-runner step (`test_loop_driving_await_
    guard.py`'s own rule). Same shape task 072 added to `tests/
    test_peer_verdict_gate.py`/`tests/test_peer_plan_review_gate.py`."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _fresh_verified_handlers():
    """A brand-new handlers dict with brand-new one-element lists on every
    call -- `_ScriptedAgent.run` pops from these in place, so sharing the
    SAME list objects across two scenario runs (even via a shallow
    `dict(...)` copy) silently empties the queue on the second run and
    hangs the planning retry ladder instead of failing loudly."""
    return {
        "planning": [_rir_plan],
        "worker": [_rir_complete_task],
        "review": [_rir_interrupted],
    }


def _run_verified_provisional_scenario(tmp_path):
    """The named scenario every surface test below reads from: review
    records an early `verified` checkpoint (task 025) and is then
    interrupted with no budget left to ask again."""
    sup = _rir_supervisor(tmp_path / "run", _fresh_verified_handlers(),
                          iterations=3)
    state.write_review_early_verdict(
        sup.run.root, approach=1, verdict="verified",
        reason="all requirements checked, cheapest-first",
        findings_path="review-findings.md")
    result = _bounded(sup._run_job_core())
    return sup, result


# ---------------------------------------------------------------------------
# Surface 1: status.json
# ---------------------------------------------------------------------------

def test_status_json_carries_the_explicit_provisional_field(tmp_path):
    sup, result = _run_verified_provisional_scenario(tmp_path)

    # The run concludes DECISIVELY -- never a null-verdict/"unreviewed"
    # hold, the #264 defect this whole task exists to close.
    assert result == "succeeded"
    status = sup.run.read_status()
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    marker = status.get("provisionalVerdict")
    assert isinstance(marker, dict), (
        "status.json must carry an explicit provisionalVerdict object when "
        "the run concludes on an early-verdict checkpoint that never got "
        "its refinement pass")
    # The explicit field a reader checks -- never derived from comparing
    # `recordedAt` against `endedAt` by hand.
    assert marker["provisional"] is True
    assert marker["verdict"] == "verified"
    assert marker.get("recordedAt")
    assert marker.get("refinementScope"), (
        "the record must name what the refinement would have covered")


def test_status_json_provisional_field_absent_for_an_ordinary_verified_run(
        tmp_path):
    """Paired contrast: an ORDINARY, fully-refined verified review (no
    interruption, no checkpoint involved) carries no provisionalVerdict at
    all -- the marker is specific to the salvage path, never a blanket
    addition to every verified run."""
    handlers = {
        "planning": [_rir_plan],
        "worker": [_rir_complete_task],
        "review": [lambda sup, _p="": _rir_ok(
            json.dumps({"verdict": "verified",
                       "reason": "checked everything"}))],
    }
    sup = _rir_supervisor(tmp_path / "run-ordinary", handlers, iterations=5)
    result = _bounded(sup._run_job_core())

    assert result == "succeeded"
    status = sup.run.read_status()
    assert status["verdict"] == "verified"
    assert status.get("provisionalVerdict") is None


def test_a_rejected_checkpoint_also_concludes_decisively_and_provisionally(
        tmp_path):
    """The sibling of the named `verified` scenario above: a `rejected`
    checkpoint concludes the RUN too (state `failed`, verdict `rejected`),
    never the `null`/`"unreviewed"` hold this same edge used to take
    before this task -- and carries the identical explicit
    `provisionalVerdict` shape."""
    sup = _rir_supervisor(tmp_path / "run-rejected",
                         _fresh_verified_handlers(), iterations=3)
    state.write_review_early_verdict(
        sup.run.root, approach=1, verdict="rejected",
        reason="found a blocking gap in the retry path", findings_path=None)
    result = _bounded(sup._run_job_core())

    assert result == "failed"
    status = sup.run.read_status()
    assert status["state"] == "failed"
    assert status["verdict"] == "rejected"
    marker = status.get("provisionalVerdict")
    assert isinstance(marker, dict)
    assert marker["provisional"] is True
    assert marker["verdict"] == "rejected"
    assert marker.get("refinementScope")


# ---------------------------------------------------------------------------
# Surface 2: the CLI (`ralphctl status`)
# ---------------------------------------------------------------------------

def test_cli_status_prints_the_provisional_marker_line():
    assert _format_provisional_verdict_lines(None) == []
    assert _format_provisional_verdict_lines({}) == []
    assert _format_provisional_verdict_lines(
        {"verdict": "verified", "provisional": False}) == []

    marker = {
        "verdict": "verified",
        "provisional": True,
        "reason": "all requirements checked",
        "recordedAt": "2026-09-23T15:00:00Z",
        "refinementScope": (
            "review's own refinement pass -- neither could run because "
            "the engine's iteration budget was spent"),
    }
    lines = _format_provisional_verdict_lines(marker)
    assert lines, "a provisional marker must render at least one line"
    joined = "\n".join(lines)
    assert "provisional" in joined.lower()
    assert "verified" in joined
    assert marker["recordedAt"] in joined


def test_cli_status_prints_the_marker_from_a_real_run(tmp_path, capsys):
    sup, result = _run_verified_provisional_scenario(tmp_path)
    assert result == "succeeded"
    status = sup.run.read_status()
    lines = _format_provisional_verdict_lines(status.get("provisionalVerdict"))
    assert any("provisional" in line.lower() for line in lines)


# ---------------------------------------------------------------------------
# Surface 3: the hub (`web/app.js`'s `renderProvisionalVerdict`)
# ---------------------------------------------------------------------------

@pytest.mark.browser
def test_hub_run_detail_renders_the_provisional_verdict_warning(tmp_path, pw):
    from test_cli_ui import UiServer, _write_dead_run

    registry = tmp_path / "registry"
    _write_dead_run(
        registry, "run-provisional", state="succeeded", verdict="verified",
        provisionalVerdict={
            "verdict": "verified", "provisional": True,
            "reason": "all requirements checked",
            "recordedAt": "2026-09-23T15:00:00Z",
            "refinementScope": "review's own refinement pass",
        })
    _write_dead_run(registry, "run-clean", state="succeeded", verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-provisional")
        body_text = _wait_for(pw, "document.body.innerText", "PROVISIONAL")
        assert "PROVISIONAL" in body_text
        n = int(pw.eval_js(
            "document.querySelectorAll('.provisional-verdict').length"))
        assert n == 1

        pw.open(f"{server.base}/#/run/run-clean")
        body_text = _wait_for(pw, "document.body.innerText", "succeeded")
        assert "PROVISIONAL" not in body_text
        n = int(pw.eval_js(
            "document.querySelectorAll('.provisional-verdict').length"))
        assert n == 0
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Surface 4: the terminal record (`run-record.json`/`.md`)
# ---------------------------------------------------------------------------

def test_terminal_record_carries_the_provisional_verdict(tmp_path):
    sup, result = _run_verified_provisional_scenario(tmp_path)
    assert result == "succeeded"

    record_json = json.loads(
        (sup.run.artifacts_dir / RUN_RECORD_JSON_FILE).read_text())
    marker = record_json.get("provisionalVerdict")
    assert isinstance(marker, dict)
    assert marker["provisional"] is True
    assert marker["verdict"] == "verified"

    record_md = (sup.run.artifacts_dir / RUN_RECORD_MD_FILE).read_text()
    assert "provisional" in record_md.lower()
    assert marker["verdict"] in record_md
