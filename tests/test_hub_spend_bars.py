"""v0.39 (#321 E): the hub run detail's spend-against-caps bars.

`run_detail` (the data the hub renders) carries `status.spendCaps` -- the same
entries `GET /status` serves, derived from job.yaml + status.json for a run
with no live engine -- and `status.spendBars`, one bar per cap, worded by
`spend_caps.bar_entries`. A run with no caps carries neither key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ralphd.cli.ui_server import run_detail
from ralphd.engine import spend_caps

pytestmark = [pytest.mark.smoke, pytest.mark.fast]

CAPS = {"overall": {"review": {"usd": 150.0, "source": "flag"},
                    "worker": {"usd": 20.5, "source": "flag"}},
        "per_approach": {"review": {"usd": 50.0, "source": "flag"}}}
SPEND = {"overall": {"review": 38.1, "worker": 3.0, "total": 41.1},
         "byApproach": {"1": {"review": 25.7, "total": 25.7},
                        "2": {"review": 12.4, "worker": 3.0, "total": 15.4}}}


def _run(tmp_path: Path, *, caps=None, run_id="capped") -> Path:
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "exhausted", "approach": 2, "iterationsUsed": 4,
        "spend": SPEND}))
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": []}))
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    job = {"run_id": run_id, "harnesses": {"review": "claude-code"}}
    if caps:
        job["spend_caps"] = caps
    (cdir / "job.yaml").write_text(yaml.safe_dump(job))
    return registry


def test_run_detail_serves_the_spendcaps_entries_and_one_bar_per_cap(tmp_path):
    status = run_detail(_run(tmp_path, caps=CAPS), "capped")["status"]
    entries = status["spendCaps"]
    assert [e["hat"] for e in entries] == ["worker", "review"]
    review = next(e for e in entries if e["hat"] == "review")
    assert review["overall"] == {"cap": 150.0, "spend": 38.1, "source": "flag"}
    assert review["perApproach"] == {"cap": 50.0, "spend": 12.4, "source": "flag"}
    bars = {(b["hat"], b["level"]): b for b in status["spendBars"]}
    assert set(bars) == {("worker", "overall"), ("review", "overall"),
                         ("review", "per_approach")}
    assert (bars[("review", "overall")]["spend"], bars[("review", "overall")]["cap"]) == (38.1, 150.0)
    assert bars[("review", "per_approach")]["percent"] == 24.8
    assert bars[("worker", "overall")]["percent"] == 14.6
    assert bars[("review", "overall")]["text"].startswith("$38.10 / $150 overall")
    assert bars[("review", "per_approach")]["text"].startswith("$12.40 / $50 this approach")


def test_the_notional_hat_is_marked_on_each_of_its_bars_and_a_priced_hat_is_not(tmp_path):
    status = run_detail(_run(tmp_path, caps=CAPS), "capped")["status"]
    for bar in status["spendBars"]:
        if bar["hat"] == "review":
            assert bar["notional"] is True
            assert bar["text"].endswith(f"({spend_caps.NOTIONAL_NOTE})")
        else:
            assert bar["notional"] is False
            assert "notional" not in bar["text"]


def test_a_run_with_no_caps_has_no_spendcaps_and_no_bar(tmp_path):
    status = run_detail(_run(tmp_path), "capped")["status"]
    assert "spendCaps" not in status and "spendBars" not in status


def test_the_bars_are_worded_from_the_entries_status_serves():
    entries = spend_caps.status_entries(CAPS, SPEND, 2, {"review"})
    bars = spend_caps.bar_entries(entries)
    assert len(bars) == 3
    assert spend_caps.bar_entries([]) == [] and spend_caps.bar_entries(None) == []


def test_a_spent_cap_fills_the_bar_and_never_overflows():
    entries = spend_caps.status_entries(
        {"overall": {"worker": {"usd": 10.0, "source": "flag"}}},
        {"overall": {"worker": 12.5}}, 1, set())
    (bar,) = spend_caps.bar_entries(entries)
    assert bar["percent"] == 100.0 and bar["text"] == "$12.50 / $10 overall"
