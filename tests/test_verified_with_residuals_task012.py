"""Task 012 (requirement D.3): a review that VERIFIES over one or more
`severity: "residual"` findings in the structured findings channel records
`verdict: "verified-with-residuals"` in `status.json` -- never the plain
`"verified"` a clean channel still gets, and never a fallback to
`"unknown"` on the CLI/hub read side. `state` is `"succeeded"` in both
cases: a residual finding is by definition never blocking (`findings.
compute_verdict`), so it never changes whether the run succeeds, only how
that success is *labelled*.

`LoopSupervisor._review_success_verdict_label()` is the one place this
label is decided -- every `_finish("succeeded", ...)` write that follows a
review VERIFIED verdict (`_step_review`'s own direct success; the grace
review's off-budget success; the ordinary peer-verdict-gate approval)
calls it instead of hardcoding `"verified"`. This module drives a real
`LoopSupervisor._step_review()` for both cases (clean channel,
residual-carrying channel), the same construction shape
`tests/test_rejected_incurable_edge.py`/`tests/test_settled_vs_done_
task013.py` already use, plus a pure unit pin on `findings.has_residual_
findings` and a CLI-rendering pin over `ralphctl status`'s real
`cmd_status` (registry rooted at a tmp dir via `RALPHD_REGISTRY`, so no
daemon/container is needed) and the hub's `pill()`/sort-vocabulary source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from ralphd.engine import findings, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory


def _finding(severity: str, **overrides) -> dict:
    payload = {
        "severity": severity,
        "curable": True,
        "clause": "requirement D.3",
        "evidence": "fixture evidence",
    }
    payload.update(overrides)
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


_supervisor = supervisor_factory(run_id="task012-verified-with-residuals",
                                 cfg_defaults=False,
                                 prd="# task 012 D.3 fixture PRD\n\nDo the thing.\n",
                                 status={"state": "running"})


# ---------------------------------------------------------------------------
# 1. Pure unit pin on the new findings-module predicate.
# ---------------------------------------------------------------------------

def test_has_residual_findings_is_true_only_with_a_residual_entry():
    assert findings.has_residual_findings([]) is False
    assert findings.has_residual_findings([_finding("blocking")]) is False
    assert findings.has_residual_findings([_finding("residual")]) is True
    assert findings.has_residual_findings(
        [_finding("blocking"), _finding("residual")]) is True


# ---------------------------------------------------------------------------
# 2. A clean findings channel still records the plain "verified" verdict,
#    state "succeeded".
# ---------------------------------------------------------------------------

def test_a_clean_findings_channel_still_verifies_plainly(tmp_path):
    sup = _supervisor(tmp_path / "run")
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, [])

    async def _review_verifies(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "verified", "reason": "nothing outstanding",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_verifies  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("done", "succeeded")
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified"


# ---------------------------------------------------------------------------
# 3. A VERIFIED review whose findings channel still carries a residual
#    finding records "verified-with-residuals", state still "succeeded".
# ---------------------------------------------------------------------------

def test_a_residual_carrying_verified_review_records_verified_with_residuals(
        tmp_path):
    sup = _supervisor(tmp_path / "run")
    sup._approach = 1
    residual_only = [
        _finding("residual", curable=False, clause="requirement Z",
                  evidence="a stale snapshot label, disclosed"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, residual_only)

    async def _review_verifies(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "verified", "reason": "only residual findings remain",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_verifies  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("done", "succeeded")
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified-with-residuals"


def test_a_mixed_blocking_and_residual_rejection_never_reaches_this_label(
        tmp_path):
    """Sanity: a genuinely rejected outcome never gets NEAR the new label
    at all -- `_review_success_verdict_label` is only ever consulted once a
    verdict is already VERIFIED (requirement D.3's own scope: a residual
    finding beside a blocking one still rejects)."""
    sup = _supervisor(tmp_path / "run")
    sup._approach = 1
    mixed = [_finding("blocking"), _finding("residual")]
    findings.write_findings_json(sup.run.findings_json_file, mixed)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected", "reason": "requirement D.3 unmet",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome != ("done", "succeeded")
    status = sup.run.read_status()
    assert status.get("verdict") != "verified-with-residuals"
    assert status.get("state") != "succeeded"


def test_the_label_helper_reads_the_channel_independently_of_review_verdict(
        tmp_path):
    """`_review_success_verdict_label` re-reads the channel itself rather
    than depending on a value threaded through `_review_verdict`'s own
    return shape -- pinned directly, isolated from the async `_step_review`
    machinery above."""
    sup = _supervisor(tmp_path / "run")
    assert sup._review_success_verdict_label() == "verified"
    findings.write_findings_json(sup.run.findings_json_file,
                                 [_finding("residual")])
    assert sup._review_success_verdict_label() == "verified-with-residuals"


# ---------------------------------------------------------------------------
# 4. CLI rendering: `ralphctl status`'s real `cmd_status` prints the new
#    value verbatim, never "unknown" -- registry rooted at a tmp dir
#    (`RALPHD_REGISTRY`), so no daemon/container is needed to exercise it.
# ---------------------------------------------------------------------------

def _seeded_run(cli_main, tmp_path, monkeypatch, run_id: str) -> RunDir:
    monkeypatch.setenv("RALPHD_REGISTRY", str(tmp_path / "registry"))
    root = cli_main.run_root(run_id)
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="succeeded", verdict="verified-with-residuals",
                      startedAt="2026-01-01T00:00:00Z",
                      endedAt="2026-01-01T00:05:00Z")
    return run


def test_cmd_status_renders_verified_with_residuals_verbatim_never_unknown(
        tmp_path, monkeypatch):
    from ralphd.cli import main as cli_main

    _seeded_run(cli_main, tmp_path, monkeypatch, "task012-cli-render")
    args = argparse.Namespace(run_id="task012-cli-render", json=False)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(
        " ".join(str(x) for x in a)))

    cli_main.cmd_status(args)

    output = "\n".join(printed)
    assert "verdict:   verified-with-residuals" in output
    assert "unknown" not in output.lower()


def test_cmd_status_json_carries_the_new_verdict_verbatim(tmp_path, monkeypatch):
    from ralphd.cli import main as cli_main

    _seeded_run(cli_main, tmp_path, monkeypatch, "task012-cli-render-json")
    args = argparse.Namespace(run_id="task012-cli-render-json", json=True)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(
        " ".join(str(x) for x in a)))

    cli_main.cmd_status(args)

    # `die(4, ...)` (the swallowed "no API endpoint recorded" fallback
    # notice) also goes through `print`, ahead of the JSON body -- find the
    # JSON object itself rather than assuming it is the only captured line.
    combined = "\n".join(printed)
    payload = json.loads(combined[combined.index("{"):])
    assert payload["verdict"] == "verified-with-residuals"


def test_cmd_status_lines_render_the_new_verdict_verbatim():
    """A narrower, dependency-free pin over the exact rendering line
    `cmd_status` builds for `verdict:` -- plus the sort/lifecycle
    vocabulary, which now knows the new value too (an unrecognised value
    would still sort after every known one rather than crash, but this
    value is now a KNOWN one, ranked strictly after "verified")."""
    from ralphd.cli import main as cli_main

    status = {"runId": "r1", "state": "succeeded",
              "verdict": "verified-with-residuals"}
    line = f"verdict:   {status.get('verdict')}"
    assert line == "verdict:   verified-with-residuals"
    assert "verified-with-residuals" in cli_main.RUN_VERDICT_ORDER
    assert (cli_main.RUN_VERDICT_ORDER.index("verified-with-residuals")
           > cli_main.RUN_VERDICT_ORDER.index("verified"))


# ---------------------------------------------------------------------------
# 5. Hub rendering: `pill()`/the summary row never fall back to "unknown"
#    for the new value, and the sort vocabulary knows it too.
# ---------------------------------------------------------------------------

def test_app_js_pill_never_special_cases_verdict_to_unknown():
    """`pill(text)` (cli/web/app.js) only substitutes "unknown" for a
    null/empty value -- never for a recognised-but-not-hardcoded string
    like "verified-with-residuals". Read the source rather than running a
    JS engine (no such runtime in this checkout) -- the same style
    `tests/test_peer_verdict_gate.py::_review_verdict_attributions`
    already uses for this same file."""
    app_js = (Path(__file__).resolve().parents[1] / "src" / "ralphd" /
             "cli" / "web" / "app.js").read_text()
    pill_fn = re.search(r"function pill\(text\) \{.*?\n\}", app_js, re.DOTALL)
    assert pill_fn, "pill() not found in app.js"
    body = pill_fn.group(0)
    # the ONLY places "unknown" appears inside pill() are the two
    # null-coalescing fallbacks (class name, text) for a missing/empty
    # value -- never a value-specific branch naming a real verdict string.
    assert body.count("unknown") == 2
    assert "verified-with-residuals" not in body
    # the lifecycle sort vocabulary was extended alongside the CLI's.
    assert ('const RUN_VERDICT_ORDER = ["", "unverified", "verified", '
           '"verified-with-residuals"];') in app_js


def test_style_css_gives_the_new_pill_a_real_colour_not_the_unknown_one():
    css = (Path(__file__).resolve().parents[1] / "src" / "ralphd" / "cli" /
          "web" / "style.css").read_text()
    assert ".pill-verified-with-residuals" in css
