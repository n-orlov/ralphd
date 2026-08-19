"""Task 051 (#10): every surface renders an unknown/partial cost as
*unavailable*, never as `$0.0000`.

Issue #10's real damage was not the missing number, it was the confident
wrong one: a run whose provider quoted no prices was displayed as costing
`$0.0000`, indistinguishable from a genuinely free run. Tasks 049/050 made
the *data* honest (`usage.costPriced` per iteration, `costStatus` per
bucket); this test file pins the *rendering* of that data on all four
surfaces plus the one shared formatter they go through:

* `engine/state.format_cost` / `cost_status` -- the single formatter (unit);
* `ralphctl status` -- `_summarize_usage`, plus a black-box run over an
  on-disk fixture through the unreachable-run fallback;
* the `ralphctl logs` iteration footer -- `log_render.render_to_lines`
  over a synthesized `ralphd.iteration` end boundary;
* the hub -- `ui_server.run_detail`, which ships the formatted string as
  `usage.costDisplay` (the `startedAtLocal` pattern) so `app.js` never
  re-derives a number; the browser-tier assertion lives in
  tests/test_browser_hub.py.

Every case is paired with a fully-priced fixture asserted byte-for-byte
against a recorded expectation, because "don't regress the normal run's
output" is half the contract.
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_cli_ui import _write_dead_run

from ralphd.cli.log_render import new_render_state, render_to_lines
from ralphd.cli.main import _summarize_usage
from ralphd.cli.ui_server import run_detail
from ralphd.engine.state import (
    COST_UNAVAILABLE,
    cost_status,
    format_cost,
    format_local_time,
)
from ralphd.log_merge import boundary_line

__all__ = ["ctl", "unix_sock"]


# --------------------------------------------------------------------------
# the one shared formatter
# --------------------------------------------------------------------------

def test_cost_status_reads_both_published_shapes():
    # bucket shape (task 050)
    assert cost_status({"costUSD": 1.5}) is None
    assert cost_status({"costUSD": 1.5, "costStatus": "partial"}) == "partial"
    assert cost_status({"costStatus": "unknown"}) == "unknown"
    # iteration shape (task 049)
    assert cost_status({"costUSD": 0.42, "costPriced": True}) is None
    assert cost_status({"costPriced": False, "totalTokens": 900}) == "unknown"
    assert cost_status({"costUSD": 0.42, "costPriced": False}) == "partial"
    # no-traffic iteration: the historical int 0 is a real "$0", not unknown
    assert cost_status({"costUSD": 0}) is None
    assert cost_status({}) is None
    assert cost_status(None) is None


def test_format_cost_priced_is_unchanged_and_unknown_is_never_zero():
    assert format_cost({"costUSD": 0.56, "totalTokens": 10}) == "$0.56"
    assert format_cost({"costUSD": 0.0, "costPriced": True}) == "$0.00"  # free != unknown
    assert format_cost({"costUSD": 14.2}, decimals=4) == "$14.2000"
    assert format_cost({"costUSD": 0.5}, decimals=None) == "$0.5"

    unknown = format_cost({"costStatus": "unknown", "totalTokens": 900})
    assert unknown == COST_UNAVAILABLE
    assert "$" not in unknown
    assert format_cost({"costPriced": False, "totalTokens": 900}) == COST_UNAVAILABLE

    partial = format_cost({"costUSD": 0.12, "costStatus": "partial"})
    assert partial == "$0.12+ (partial, rest unavailable)"
    assert COST_UNAVAILABLE in partial

    # nothing known at all -> the caller decides (None, not "$0.00")
    assert format_cost({"totalTokens": 10}) is None
    assert format_cost({}) is None


# --------------------------------------------------------------------------
# `ralphctl status`
# --------------------------------------------------------------------------

_PRICED_USAGE = {
    "costUSD": 0.56,
    "totalTokens": 625_000,
    "byPhase": {
        "planning": {"costUSD": 0.10},
        "worker": {"costUSD": 0.40},
        "review": {"costUSD": 0.06},
    },
}
# Recorded expectation: a fully-priced run's `usage:` line must stay
# byte-identical to what it printed before task 051.
_PRICED_SUMMARY = "$0.56, 625k tokens (planning $0.10 / worker $0.40 / review $0.06)"


def test_status_usage_summary_for_a_fully_priced_run_is_byte_identical():
    assert _summarize_usage(_PRICED_USAGE) == _PRICED_SUMMARY


def test_status_usage_summary_renders_unknown_cost_as_unavailable():
    summary = _summarize_usage({
        "costStatus": "unknown",
        "totalTokens": 625_000,
        "byPhase": {"worker": {"costStatus": "unknown", "totalTokens": 625_000}},
    })
    assert summary == "unavailable, 625k tokens (worker unavailable)"
    assert "$0.00" not in summary


def test_status_usage_summary_renders_partial_cost_as_a_lower_bound():
    summary = _summarize_usage({
        "costUSD": 0.4, "costStatus": "partial", "totalTokens": 1_500,
        "byPhase": {"planning": {"costUSD": 0.4, "costStatus": "partial"}},
    })
    assert summary == ("$0.40+ (partial, rest unavailable), 1.5k tokens "
                       "(planning $0.40+ (partial, rest unavailable))")


_BASE_STATUS = {
    "state": "failed",
    "verdict": "unverified",
    "phase": "worker",
    "approach": 1,
    "iterationsUsed": 4,
    "iterationsBudget": 25,
    "startedAt": "2024-01-01T00:00:00Z",
    "schemaVersion": 1,
}


def _seed_status(ctl: Ctl, run_id: str, usage: dict):
    rdir, _cdir = _seed_run(ctl, run_id)
    (rdir / "status.json").write_text(json.dumps(
        {**_BASE_STATUS, "runId": run_id, "usage": usage}))
    return rdir


def test_status_cli_prints_unavailable_for_an_unpriced_run(ctl: Ctl):
    _seed_status(ctl, "tst-unpriced", {"costStatus": "unknown", "totalTokens": 12000})
    res = ctl.run("status", "tst-unpriced")
    assert res.returncode == 0, res.stderr
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("usage:")]
    assert len(line) == 1, res.stdout
    assert COST_UNAVAILABLE in line[0], line[0]
    assert "$" not in line[0], line[0]
    # --json still carries the raw contract fields, untouched
    res = ctl.run("--json", "status", "tst-unpriced")
    doc = json.loads(res.stdout)
    assert doc["usage"] == {"costStatus": "unknown", "totalTokens": 12000}


def test_status_cli_priced_run_line_is_unchanged(ctl: Ctl):
    _seed_status(ctl, "tst-priced", _PRICED_USAGE)
    res = ctl.run("status", "tst-priced")
    assert res.returncode == 0, res.stderr
    assert f"usage:     {_PRICED_SUMMARY}" in res.stdout, res.stdout


# --------------------------------------------------------------------------
# the `ralphctl logs` iteration footer
# --------------------------------------------------------------------------

def _footer(usage: dict) -> str:
    meta = {"number": 2, "phase": "worker", "model": "m", "approach": 1,
            "startedAt": "2024-01-01T00:00:00Z", "endedAt": "2024-01-01T00:01:00Z",
            "exitCode": 0, "usage": usage}
    lines = render_to_lines(boundary_line(meta, "end"), tty=False,
                            state=new_render_state())
    return "\n".join(lines)


def test_logs_footer_of_a_priced_iteration_is_unchanged():
    at = format_local_time("2024-01-01T00:01:00Z")
    assert _footer({"totalTokens": 900, "costUSD": 0.5, "costPriced": True}) == \
        f"  iteration 2 done, at {at}, took 1m, exit=0, tokens=900, cost=$0.5"


def test_logs_footer_of_an_unpriced_iteration_says_unavailable():
    footer = _footer({"totalTokens": 900, "costPriced": False})
    assert "tokens=900" in footer
    assert f"cost={COST_UNAVAILABLE}" in footer
    assert "$" not in footer


def test_logs_footer_of_a_mixed_iteration_marks_the_subtotal_partial():
    footer = _footer({"totalTokens": 900, "costUSD": 0.25, "costPriced": False})
    assert "cost=$0.25+ (partial, rest unavailable)" in footer


def test_logs_footer_of_a_no_traffic_iteration_keeps_the_historical_zero():
    # pi zero-fills usage on an in-band error: $0 is the truth there.
    assert "cost=$0" in _footer({"totalTokens": 0, "costUSD": 0})


# --------------------------------------------------------------------------
# the hub (server-rendered `costDisplay`; browser tier in test_browser_hub.py)
# --------------------------------------------------------------------------

def test_hub_run_detail_ships_unavailable_cost_strings(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-unpriced", state="failed", usage={
        "costStatus": "unknown", "totalTokens": 900,
        "byPhase": {"worker": {"costStatus": "unknown", "totalTokens": 900}},
        "byApproach": {"1": {"costStatus": "unknown", "totalTokens": 900}},
    })
    usage = run_detail(registry, "run-unpriced")["status"]["usage"]
    assert usage["costDisplay"] == COST_UNAVAILABLE
    assert usage["byPhase"]["worker"]["costDisplay"] == COST_UNAVAILABLE
    assert usage["byApproach"]["1"]["costDisplay"] == COST_UNAVAILABLE
    # raw contract fields are untouched alongside the rendered string
    assert usage["costStatus"] == "unknown" and usage["totalTokens"] == 900
    assert "costUSD" not in usage


def test_hub_run_detail_marks_a_partial_bucket_and_keeps_priced_ones_numeric(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-mixed", state="succeeded", usage={
        "costUSD": 14.2, "costStatus": "partial", "totalTokens": 900,
        "byPhase": {"worker": {"costUSD": 14.2, "costStatus": "partial"},
                    "review": {"costUSD": 1.6}},
    })
    usage = run_detail(registry, "run-mixed")["status"]["usage"]
    assert usage["costDisplay"] == "$14.2000+ (partial, rest unavailable)"
    assert usage["byPhase"]["worker"]["costDisplay"] == \
        "$14.2000+ (partial, rest unavailable)"
    assert usage["byPhase"]["review"]["costDisplay"] == "$1.6000"


def test_hub_run_detail_priced_usage_is_unchanged_apart_from_the_added_string(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-priced", state="succeeded", usage=_PRICED_USAGE)
    usage = run_detail(registry, "run-priced")["status"]["usage"]
    assert usage["costDisplay"] == "$0.5600"
    assert usage["byPhase"]["planning"]["costDisplay"] == "$0.1000"
    stripped = {k: v for k, v in usage.items() if k != "costDisplay"}
    stripped["byPhase"] = {p: {k: v for k, v in b.items() if k != "costDisplay"}
                           for p, b in usage["byPhase"].items()}
    assert stripped == _PRICED_USAGE


def test_hub_run_detail_without_usage_is_untouched(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-nousage", state="succeeded")
    assert "usage" not in run_detail(registry, "run-nousage")["status"]


def test_hub_app_js_never_derives_a_cost_number_without_the_shared_string():
    """The rendering-discipline grep (same style as the textContent-only
    checks): every cost number app.js formats is a *fallback* behind
    `costDisplay` on the same expression, so a payload carrying the
    unknown/partial marker can never be rendered as `$0.0000` by the
    browser."""
    from pathlib import Path

    import ralphd.cli.ui_server as ui_mod
    app_js = (Path(ui_mod.__file__).parent / "web" / "app.js").read_text()
    cost_lines = [ln for ln in app_js.splitlines() if "toFixed(4)" in ln]
    assert cost_lines, "expected app.js to still render cost numbers somewhere"
    for line in cost_lines:
        assert "costDisplay" in line, \
            f"cost number rendered without the costDisplay fallback: {line!r}"
