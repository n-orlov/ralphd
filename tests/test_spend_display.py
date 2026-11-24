"""v0.39 (#321 E): spend against caps on `ralphctl status`, `status --json`,
`GET /status` and `ralphctl cost`.

One shaping (`engine.spend_caps.status_entries` / `cost_entries`) feeds every
surface, so the figures cannot differ; a run with no caps prints no spend
line and publishes no `spendCaps` key. Counters are written by hand into
status.json (no run spends a dollar)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import yaml

from ralphd.engine import spend_caps
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from tests.conftest import call_ralphctl_in_process, make_supervisor

pytestmark = [pytest.mark.smoke, pytest.mark.fast]

CAPS = {"overall": {"review": {"usd": 150.0, "source": "flag"},
                    "worker": {"usd": 20.5, "source": "flag"}},
        "per_approach": {"review": {"usd": 50.0, "source": "flag"}}}
SPEND = {"overall": {"review": 38.1, "worker": 3.0, "total": 41.1},
         "byApproach": {"1": {"review": 25.7, "total": 25.7},
                        "2": {"review": 12.4, "worker": 3.0, "total": 15.4}}}
REVIEW_LINE = "review  $38.10 / $150 overall · $12.40 / $50 this approach"
NOTIONAL = "(notional, list-equivalent)"


def _dead_run(tmp_path: Path, *, caps=None, harnesses=None, spend=None,
              run_id: str = "capped") -> Path:
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    doc = {"runId": run_id, "state": "exhausted", "approach": 2,
           "iterationsUsed": 4,
           "usage": {"costUSD": 1.0, "totalTokens": 10,
                     "byApproach": {"1": {"totalTokens": 5, "costUSD": 0.5},
                                    "2": {"totalTokens": 5, "costUSD": 0.5}}}}
    if spend is not None:
        doc["spend"] = spend
    (run_dir / "status.json").write_text(json.dumps(doc))
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    job = {"run_id": run_id}
    if caps:
        job["spend_caps"] = caps
    if harnesses:
        job["harnesses"] = harnesses
    (cdir / "job.yaml").write_text(yaml.safe_dump(job))
    return registry


def _ctl(registry: Path, capsys, *argv: str):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return call_ralphctl_in_process(list(argv), env=env, capsys=capsys)


def _job(**kw) -> JobConfig:
    return JobConfig(**kw)


# -- the shared shaping ------------------------------------------------------

def test_a_capped_hat_is_one_line_with_both_levels_and_the_notional_mark():
    entries = spend_caps.status_entries(CAPS, SPEND, 2, {"review", "total"})
    review = next(e for e in entries if e["hat"] == "review")
    assert review["line"].startswith(REVIEW_LINE)
    assert review["notional"] is True and review["line"].endswith(NOTIONAL)
    assert review["overall"] == {"cap": 150.0, "spend": 38.1, "source": "flag"}
    assert review["perApproach"] == {"cap": 50.0, "spend": 12.4, "source": "flag"}


def test_a_hat_billed_per_token_is_not_marked_notional_and_a_level_without_a_cap_is_left_out():
    entries = spend_caps.status_entries(CAPS, SPEND, 2, {"review", "total"})
    worker = next(e for e in entries if e["hat"] == "worker")
    assert worker["notional"] is False
    assert worker["perApproach"] is None
    assert worker["line"].rstrip().endswith("$3.00 / $20.50 overall")
    assert "this approach" not in worker["line"]


def test_notional_hats_are_the_subscription_routed_ones_and_total_follows():
    claude = _job(harnesses={"review": "claude-code"})
    assert spend_caps.notional_hats(claude) == {"review", "total"}
    assert spend_caps.notional_hats(_job()) == set()


def test_no_caps_means_no_entries():
    assert spend_caps.status_entries({}, SPEND, 2) == []
    assert spend_caps.cost_entries({}, SPEND) == []


# -- ralphctl status / status --json (container gone) ------------------------

def test_status_prints_one_line_per_capped_hat_with_notional_marked(tmp_path, capsys):
    registry = _dead_run(tmp_path, caps=CAPS, spend=SPEND,
                         harnesses={"review": "claude-code"})
    out = _ctl(registry, capsys, "status", "capped")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    at = lines.index("spend caps:")
    assert lines[at + 2].strip() == f"{REVIEW_LINE}  {NOTIONAL}"
    worker = lines[at + 1].strip()
    assert worker.startswith("worker") and "$3.00 / $20.50 overall" in worker
    assert NOTIONAL not in worker
    assert len([ln for ln in lines if " overall" in ln]) == 2


def test_status_json_carries_one_spendcaps_entry_per_capped_hat(tmp_path, capsys):
    registry = _dead_run(tmp_path, caps=CAPS, spend=SPEND,
                         harnesses={"review": "claude-code"})
    doc = json.loads(_ctl(registry, capsys, "--json", "status", "capped").stdout)
    assert [e["hat"] for e in doc["spendCaps"]] == ["worker", "review"]
    review = doc["spendCaps"][1]
    assert review["notional"] is True
    assert review["overall"]["spend"] == 38.1 and review["overall"]["cap"] == 150.0
    assert review["perApproach"]["spend"] == 12.4
    assert review["perApproach"]["cap"] == 50.0


# -- GET /status -------------------------------------------------------------

def _client(sup) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


@pytest.mark.asyncio
async def test_get_status_carries_the_same_entries_as_the_cli(tmp_path, capsys):
    sup = make_supervisor(tmp_path / "engine", run_id="capped", cfg_defaults=False,
                          iterations=25, spend_caps=CAPS,
                          harnesses={"review": "claude-code"},
                          status={"state": "running", "approach": 2, "spend": SPEND})
    async with _client(sup) as client:
        api_doc = (await client.get("/status")).json()
    registry = _dead_run(tmp_path, caps=CAPS, spend=SPEND,
                         harnesses={"review": "claude-code"})
    cli_doc = json.loads(_ctl(registry, capsys, "--json", "status", "capped").stdout)
    assert api_doc["spendCaps"] == cli_doc["spendCaps"]
    review = next(e for e in api_doc["spendCaps"] if e["hat"] == "review")
    assert review["notional"] is True and review["line"].startswith(REVIEW_LINE)


# -- ralphctl cost -----------------------------------------------------------

def test_cost_prints_each_caps_beside_its_spend_per_approach_and_overall(tmp_path, capsys):
    registry = _dead_run(tmp_path, caps=CAPS, spend=SPEND,
                         harnesses={"review": "claude-code"})
    out = _ctl(registry, capsys, "cost", "capped")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    group = lines[lines.index("spend caps:") + 1:]
    rows = {tuple(ln.split()[:3]) if "approach" in ln else tuple(ln.split()[:2]): ln
            for ln in group}
    assert "$38.10 / $150" in rows[("review", "overall")]
    assert NOTIONAL in rows[("review", "overall")]
    assert "$25.70 / $50" in rows[("review", "approach", "1")]
    assert "$12.40 / $50" in rows[("review", "approach", "2")]
    assert "$3.00 / $20.50" in rows[("worker", "overall")]
    assert NOTIONAL not in rows[("worker", "overall")]
    doc = json.loads(_ctl(registry, capsys, "--json", "cost", "capped").stdout)
    review = next(e for e in doc["spendCaps"] if e["hat"] == "review")
    assert [a["approach"] for a in review["approaches"]] == [1, 2]
    assert review["overall"]["cap"] == 150.0
    assert doc["text"].splitlines()[-len(group):] == group


# -- a run with no caps ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_run_with_no_caps_shows_nothing_on_any_of_the_four_surfaces(tmp_path, capsys):
    registry = _dead_run(tmp_path, spend=SPEND)
    status = _ctl(registry, capsys, "status", "capped")
    assert status.returncode == 0, status.stderr
    assert "spend caps" not in status.stdout
    assert "spendCaps" not in json.loads(
        _ctl(registry, capsys, "--json", "status", "capped").stdout)
    cost = _ctl(registry, capsys, "cost", "capped")
    assert "spend caps" not in cost.stdout
    assert "spendCaps" not in json.loads(
        _ctl(registry, capsys, "--json", "cost", "capped").stdout)
    sup = make_supervisor(tmp_path / "engine", run_id="capped", cfg_defaults=False,
                          iterations=25,
                          status={"state": "running", "approach": 1, "spend": SPEND})
    async with _client(sup) as client:
        assert "spendCaps" not in (await client.get("/status")).json()


# -- the docs name the line, the entry and the cost group ---------------------

@pytest.mark.parametrize("doc,needles", [
    ("docs/cli.md", ["spend caps:", "$38.10 / $150 overall", "spendCaps",
                     "notional, list-equivalent"]),
    ("docs/api.md", ["`spendCaps`", "notional", "perApproach"]),
    ("SPEC.md", ["`spendCaps`", "`spend caps:`", "notional, list-equivalent"]),
])
def test_the_docs_describe_the_status_line_the_entry_and_the_cost_group(doc, needles):
    text = (Path(__file__).resolve().parent.parent / doc).read_text()
    for needle in needles:
        assert needle in text, (doc, needle)
