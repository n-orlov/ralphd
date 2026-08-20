"""Task 027 (#18.5): `ralphctl cost <run>` -- what a run spent, per phase and
per approach.

status.json's `usage` has carried `byPhase`/`byApproach` buckets since PRD
req 19 (`loop._accumulate_usage`), and the only surfaces that ever read them
were `ralphctl status`' one-line summary -- which names planning/worker/review,
two of which a vigilant run does not even use -- and the hub's usage card. So
"which phase burned the tokens" and "how much of that number is actually known"
meant reading raw JSON.

What is pinned here:

  * the shared shaping and wording (`state.cost_breakdown` /
    `cost_breakdown_lines` / `cost_source` / `format_token_total`), which task
    028's hub dialog renders verbatim -- a second cost vocabulary cannot be
    born;
  * the headline stays `format_cost`' own string (`total.costDisplay`), so a
    breakdown can never disagree with the number `ralphctl status` and the hub
    print beside it;
  * every kind of money is LABELLED: provider-quoted, host-derived (task 052's
    `~… derived`), a partial subtotal and `unavailable` -- and the mixed
    fixture carries all of them at once;
  * unknown is not zero (#10/#15's rule again): an implausible zero quote
    (task 049) renders `unavailable` with the anomaly named, a bucket that
    recorded nothing renders `(none)`, and a run with no usage at all says so
    instead of showing a table of `$0.00`s;
  * the on-disk contract: no container, no live API, no snapshot notice
    (status.json is the engine's own atomic write), and a forged display string
    in status.json is always recomputed;
  * an unknown run still exits 3.

Tiers: unit (the formatters + the shaping), black-box `ralphctl cost` over
hand-written run dirs (container gone), and one REAL engine (the `live`
fixture) whose own byPhase/byApproach buckets it renders.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ralphd.engine.state import (
    COST_BREAKDOWN_LEGEND,
    COST_NO_USAGE,
    COST_SOURCE_DERIVED,
    COST_SOURCE_FREE,
    COST_SOURCE_NO_TRAFFIC,
    COST_SOURCE_PARTIAL,
    COST_SOURCE_PROVIDER,
    COST_SOURCE_UNAVAILABLE,
    COST_UNAVAILABLE,
    COST_ZERO_QUOTE_NOTICE,
    USAGE_NONE,
    cost_breakdown,
    cost_breakdown_lines,
    cost_breakdown_text,
    cost_bucket,
    cost_source,
    format_cost,
    format_token_total,
)
from tests.conftest import RALPHCTL, live  # noqa: F401  (fixture)

# The verbatim iteration-1 payload of the v0.6 self-development run: the
# AIGW/Bedrock route quoted $0 for half a million billed tokens (task 049).
ZERO_QUOTE = {"input": 32, "output": 18320, "cacheRead": 438945,
              "cacheWrite": 48331, "totalTokens": 505628, "costUSD": 0}

# A run that mixes all three kinds of money: one provider-priced phase, one
# whose cost was derived from the host-side rate table (task 052) and one the
# provider never priced at all.
MIXED_USAGE = {
    "input": 1200, "output": 3400, "totalTokens": 40000,
    "costUSD": 0.5, "costDerivedUSD": 1.25, "costStatus": "partial",
    "byPhase": {
        "planning": {"input": 200, "output": 400, "totalTokens": 10000,
                     "costUSD": 0.5},
        "worker": {"input": 800, "output": 2600, "totalTokens": 20000,
                   "costDerivedUSD": 1.25, "costStatus": "derived"},
        "verify": {"input": 200, "output": 400, "totalTokens": 10000,
                   "costUSD": 0, "costPriced": False, "costStatus": "unknown"},
        "reflect": {},
    },
    "byApproach": {
        "2": {"totalTokens": 10000, "costDerivedUSD": 1.25, "costStatus": "derived"},
        "10": {"totalTokens": 30000, "costUSD": 0.5, "costStatus": "partial"},
    },
}


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _dead_run(tmp_path: Path, run_id: str = "spender", **status) -> tuple[Path, Path]:
    """(registry, run_dir) for a run with no container at all: no host.json, so
    nothing can even try to reach a live API."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    doc = {"runId": run_id, "state": "succeeded", "iterationsUsed": 4}
    doc.update(status)
    (run_dir / "status.json").write_text(json.dumps(doc))
    return registry, run_dir


# --------------------------------------------------------------- unit: columns
def test_format_token_total_renders_one_number_or_nothing():
    assert format_token_total({"totalTokens": 505628}) == "505,628 tokens"
    # No total reported: the counters that WERE reported are summed rather than
    # printing nothing (and `totalTokens` is not double-counted, see
    # `_token_total`).
    assert format_token_total({"input": 20, "output": 3}) == "23 tokens"
    assert format_token_total({}) == ""
    assert format_token_total(None) == ""
    assert format_token_total({"totalTokens": "junk"}) == ""


def test_format_token_total_never_double_counts_the_total():
    """`billable_tokens` sums `totalTokens` along with the split counters (it
    answers 'was anything billable at all'); the column must not."""
    assert format_token_total(ZERO_QUOTE) == "505,628 tokens"


@pytest.mark.parametrize("usage,expected", [
    ({"totalTokens": 100, "costUSD": 0.25}, COST_SOURCE_PROVIDER),
    ({"totalTokens": 100, "costDerivedUSD": 0.25}, COST_SOURCE_DERIVED),
    ({"totalTokens": 100, "costUSD": 0.25, "costStatus": "partial"},
     COST_SOURCE_PARTIAL),
    ({"totalTokens": 100, "costStatus": "unknown"}, COST_SOURCE_UNAVAILABLE),
    # Task 049: an implausible zero is unknown however it was marked.
    (ZERO_QUOTE, COST_SOURCE_UNAVAILABLE),
    ({**ZERO_QUOTE, "costPriced": True}, COST_SOURCE_UNAVAILABLE),
    # A route that DECLARED itself free keeps its honest $0.00.
    ({**ZERO_QUOTE, "costFree": True}, COST_SOURCE_FREE),
    # The historical int-0 no-traffic sentinel of #10.
    ({"costUSD": 0}, COST_SOURCE_NO_TRAFFIC),
    ({"totalTokens": 0, "costUSD": 0}, COST_SOURCE_NO_TRAFFIC),
    # Tokens billed, no cost information whatsoever recorded.
    ({"totalTokens": 100}, None),
    ({}, None),
    (None, None),
])
def test_cost_source_words_each_kind_of_money(usage, expected):
    assert cost_source(usage) == expected


def test_cost_bucket_recomputes_every_display_string():
    """The `ITERATION_DERIVED_KEYS` discipline: a hand-edited status.json cannot
    smuggle in a display string its own numbers do not support."""
    bucket = cost_bucket({**ZERO_QUOTE, "costDisplay": "$0.00",
                          "costSource": COST_SOURCE_PROVIDER,
                          "tokens": 1, "tokensDisplay": "nothing",
                          "tokensTotalDisplay": "nothing"}, "worker")
    assert bucket["key"] == "worker"
    assert bucket["costDisplay"] == COST_UNAVAILABLE
    assert bucket["costSource"] == COST_SOURCE_UNAVAILABLE
    assert bucket["tokens"] == 505628
    assert bucket["tokensTotalDisplay"] == "505,628 tokens"
    assert "$0.00" not in json.dumps(bucket)


def test_cost_bucket_drops_the_nested_breakdowns():
    """A bucket row is one bucket: the total's own `byPhase`/`byApproach` are
    shaped into lists at the top level, never repeated inside it."""
    bucket = cost_bucket(MIXED_USAGE)
    assert "byPhase" not in bucket and "byApproach" not in bucket
    assert bucket["key"] == "total"


# ------------------------------------------------------------- unit: shaping
def test_cost_breakdown_shapes_a_mixed_run(tmp_path):
    _, run_dir = _dead_run(tmp_path, usage=MIXED_USAGE,
                           model="amazon-bedrock/eu.anthropic.claude-opus-5",
                           modelRaw="eu.anthropic.claude-opus-5")
    bd = cost_breakdown(run_dir)
    assert bd["hasUsage"] is True
    assert bd["costStatus"] == "partial"
    # The headline IS `format_cost`' string for the on-disk total -- not a
    # number this surface computed for itself.
    assert bd["costDisplay"] == format_cost(MIXED_USAGE, decimals=4)
    assert bd["costDisplay"] == bd["total"]["costDisplay"]
    assert [b["key"] for b in bd["byPhase"]] == ["planning", "worker", "verify",
                                                 "reflect"]
    # Approaches sort numerically ("10" after "2"), phases keep the engine's
    # own insertion order.
    assert [b["key"] for b in bd["byApproach"]] == ["2", "10"]
    sources = {b["key"]: b["costSource"] for b in bd["byPhase"]}
    assert sources == {"planning": COST_SOURCE_PROVIDER,
                       "worker": COST_SOURCE_DERIVED,
                       "verify": COST_SOURCE_UNAVAILABLE,
                       "reflect": None}
    assert bd["model"] == "amazon-bedrock/eu.anthropic.claude-opus-5"
    assert bd["modelRaw"] == "eu.anthropic.claude-opus-5"


def test_cost_breakdown_lines_label_priced_derived_and_unavailable(tmp_path):
    _, run_dir = _dead_run(tmp_path, usage=MIXED_USAGE)
    lines = cost_breakdown_lines(cost_breakdown(run_dir))
    text = "\n".join(lines)
    assert lines[0] == f"cost:      {format_cost(MIXED_USAGE, decimals=4)}"
    assert "by phase:" in lines and "by approach:" in lines
    phase_rows = {row.split()[0]: row for row in lines
                  if row.startswith("  ")}
    assert "$0.5000" in phase_rows["planning"]
    assert f"~$1.2500 {COST_SOURCE_DERIVED}" in phase_rows["worker"]
    assert COST_UNAVAILABLE in phase_rows["verify"]
    assert "10,000 tokens" in phase_rows["planning"]
    # A bucket that recorded nothing says so instead of claiming a free phase.
    assert USAGE_NONE in phase_rows["reflect"]
    assert "$0.00" not in text.replace("$0.0000", "")
    # The legend is printed because the vocabulary is actually in use here.
    assert COST_BREAKDOWN_LEGEND in lines


def test_a_fully_priced_run_gets_no_legend_and_names_the_provider(tmp_path):
    usage = {"totalTokens": 300, "costUSD": 0.75,
             "byPhase": {"worker": {"totalTokens": 300, "costUSD": 0.75}}}
    _, run_dir = _dead_run(tmp_path, usage=usage)
    lines = cost_breakdown_lines(cost_breakdown(run_dir))
    assert lines[0] == "cost:      $0.7500"
    assert f"source:    {COST_SOURCE_PROVIDER}" in lines
    # Nothing derived, nothing partial, nothing unavailable -> no vocabulary to
    # explain, so no legend padding.
    assert COST_BREAKDOWN_LEGEND not in lines
    assert not any(COST_UNAVAILABLE in ln for ln in lines)


def test_the_source_word_is_not_repeated_when_the_money_string_says_it(tmp_path):
    """`format_cost` spells `derived`/`partial`/`unavailable` itself; the
    breakdown does not say the same thing twice."""
    for i, usage in enumerate(({"totalTokens": 10, "costDerivedUSD": 2.0},
                               {"totalTokens": 10, "costUSD": 1.0,
                                "costStatus": "partial"},
                               ZERO_QUOTE)):
        _, run_dir = _dead_run(tmp_path / f"case{i}", usage=usage)
        lines = cost_breakdown_lines(cost_breakdown(run_dir))
        assert not any(ln.startswith("source:") for ln in lines), usage


def test_an_implausible_zero_is_unavailable_and_names_the_anomaly(tmp_path):
    """The v0.6 self-development run's own shape: every bucket quoted $0 for
    hundreds of millions of billed tokens."""
    usage = {**ZERO_QUOTE, "byPhase": {"worker": dict(ZERO_QUOTE)},
             "byApproach": {"1": dict(ZERO_QUOTE)}}
    _, run_dir = _dead_run(tmp_path, usage=usage)
    bd = cost_breakdown(run_dir)
    text = cost_breakdown_text(bd)
    assert bd["costDisplay"] == COST_UNAVAILABLE
    assert COST_ZERO_QUOTE_NOTICE in bd["notices"]
    assert f"!! {COST_ZERO_QUOTE_NOTICE}" in text
    assert "$0.00" not in text
    assert text.count(COST_UNAVAILABLE) >= 3  # total + phase + approach rows


def test_a_zero_quote_inside_one_bucket_only_still_names_the_anomaly(tmp_path):
    """The total may look priced while one phase carries the anomaly."""
    usage = {"totalTokens": 600, "costUSD": 0.4,
             "byPhase": {"worker": dict(ZERO_QUOTE)}}
    _, run_dir = _dead_run(tmp_path, usage=usage)
    bd = cost_breakdown(run_dir)
    assert bd["notices"] == [COST_ZERO_QUOTE_NOTICE]


@pytest.mark.parametrize("idx,status", list(enumerate(
    [{}, {"usage": {}}, {"usage": "junk"},
     {"usage": {"byPhase": {}, "byApproach": {}}}])))
def test_a_run_with_no_usage_says_so_instead_of_zero(tmp_path, idx, status):
    _, run_dir = _dead_run(tmp_path / f"case{idx}", **status)
    bd = cost_breakdown(run_dir)
    assert bd["hasUsage"] is False
    assert bd["byPhase"] == [] and bd["byApproach"] == []
    assert cost_breakdown_lines(bd) == [COST_NO_USAGE]
    assert "$" not in cost_breakdown_text(bd)


def test_a_missing_or_junk_status_json_is_not_an_exception(tmp_path):
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "status.json").write_text("{not json")
    for root in (tmp_path / "nothing-here", tmp_path / "junk"):
        assert cost_breakdown_lines(cost_breakdown(root)) == [COST_NO_USAGE]


def test_cost_breakdown_carries_its_own_rendering(tmp_path):
    """The `fault_explanation` shape: `summaryLines` inside the dict, `text`
    the same lines joined -- what task 028's dialog shows."""
    _, run_dir = _dead_run(tmp_path, usage=MIXED_USAGE)
    bd = cost_breakdown(run_dir)
    assert bd["summaryLines"] == cost_breakdown_lines(bd)
    assert cost_breakdown_text(bd) == "\n".join(bd["summaryLines"])


# ---------------------------------------------------- black-box: ralphctl cost
def test_cli_cost_prints_the_breakdown_for_a_dead_run(tmp_path):
    registry, _ = _dead_run(tmp_path, usage=MIXED_USAGE)
    out = _ctl(registry, "cost", "spender")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == "run:       spender"
    assert lines[1] == f"cost:      {format_cost(MIXED_USAGE, decimals=4)}"
    assert "by phase:" in lines and "by approach:" in lines
    for phase in ("planning", "worker", "verify"):
        assert any(ln.startswith(f"  {phase}") for ln in lines), phase
    # No container, no live API, and therefore no snapshot notice to print.
    assert out.stderr == ""


def test_cli_cost_json_carries_the_raw_numbers_and_the_same_text(tmp_path):
    registry, _ = _dead_run(tmp_path, usage=MIXED_USAGE)
    out = _ctl(registry, "--json", "cost", "spender")
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert doc["runId"] == "spender"
    assert doc["total"]["costUSD"] == 0.5
    assert doc["total"]["costDerivedUSD"] == 1.25
    assert doc["byPhase"][1]["key"] == "worker"
    assert doc["byPhase"][1]["costSource"] == COST_SOURCE_DERIVED
    human = _ctl(registry, "cost", "spender").stdout.splitlines()
    assert doc["text"].splitlines() == human[1:]  # minus the `run:` line
    assert doc["summaryLines"] == doc["text"].splitlines()


def test_cli_cost_on_a_run_that_recorded_nothing(tmp_path):
    registry, _ = _dead_run(tmp_path)
    out = _ctl(registry, "cost", "spender")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["run:       spender", COST_NO_USAGE]


def test_cli_cost_on_an_unknown_run_exits_3(tmp_path):
    registry, _ = _dead_run(tmp_path)
    out = _ctl(registry, "cost", "ghost")
    assert out.returncode == 3
    assert "not found" in out.stderr


def test_cli_cost_and_status_agree_on_the_headline(tmp_path):
    """The criterion: the headline number still comes from `costDisplay` (the
    one shared `format_cost`), so `status` and `cost` cannot disagree about a
    price nobody quoted."""
    usage = {**ZERO_QUOTE, "byPhase": {"worker": dict(ZERO_QUOTE)}}
    registry, _ = _dead_run(tmp_path, usage=usage)
    cost = _ctl(registry, "cost", "spender").stdout
    status = _ctl(registry, "status", "spender").stdout
    assert COST_UNAVAILABLE in cost and COST_UNAVAILABLE in status
    assert "$0.00" not in cost and "$0.00" not in status


def test_cli_cost_is_documented(tmp_path):
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text()
    assert "ralphctl cost" in doc
    assert COST_BREAKDOWN_LEGEND.split(":", 1)[1].strip()[:30] in doc \
        or "legend" in doc
    help_out = _ctl(tmp_path, "--help")
    assert help_out.returncode == 0
    assert "per phase" in help_out.stdout  # the `cost` verb's own help line


# ------------------------------------------------------------- one REAL engine
def test_cost_renders_a_real_engines_own_buckets(live):  # noqa: F811
    """A real `ralphd-engine` (stub pi) writes the byPhase/byApproach buckets;
    `ralphctl cost` must render exactly those numbers and nothing else."""
    r = live(run_id="costly", job={"iterations": 12, "max_approaches": 2,
                                   "vigilant": True, "on_complete": "exit"},
             stub_env={"STUB_TASKS": "2"})
    r.wait_api()
    r.wait_terminal(timeout=120)

    usage = json.loads((r.run_dir / "status.json").read_text())["usage"]
    out = r.ralphctl("cost", "costly")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[1] == f"cost:      {format_cost(usage, decimals=4)}"
    for phase in usage["byPhase"]:
        row = next(ln for ln in lines if ln.startswith(f"  {phase} "))
        bucket = usage["byPhase"][phase]
        if bucket.get("totalTokens"):
            assert f"{bucket['totalTokens']:,} tokens" in row
    assert any(ln.startswith("  1 ") for ln in lines), "approach 1 bucket"
    # And the same run read through --json agrees with the file it came from.
    doc = json.loads(r.ralphctl("--json", "cost", "costly").stdout)
    assert doc["total"]["totalTokens"] == usage["totalTokens"]
    assert {b["key"] for b in doc["byPhase"]} == set(usage["byPhase"])
