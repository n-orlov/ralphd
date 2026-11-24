"""v0.39 K.1: a run with no caps is exactly the run 0.38 drove.

Every 0.39 spend path is keyed on a configured cap: the dispatch check, the
mid-iteration watch, the warning, the notifier POSTs, the display entries and
the hub bars. A fixture plan with no caps therefore

* schedules the same tasks in the same order (highest `priority` among tasks
  whose `dependsOn` are all completed, ties by list order);
* dispatches the same hats (one planning, one worker per task, one review);
* emits no `spend.*` event and no event type 0.38 did not emit;
* shows no spend line in `ralphctl status`, no `spendCaps` key in
  `status --json` or `GET /status`;
* writes no `spend_caps:` key into `job.yaml`.

Driven through the real `run_job` with fake hats and a fake clock source;
nothing sleeps.
"""

from __future__ import annotations

import json

import httpx
import pytest
from test_cli_docker import ctl, unix_sock          # noqa: F401
from test_cli_spend_caps import _job_path, _start
from test_no_petition_run_unchanged import (EVENT_TYPES_0_37, EXPECTED_ORDER,
                                            PLAN, _all_events, _drive)
from test_spend_display import _ctl, _dead_run

from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.spend_caps import run_entries

pytestmark = [pytest.mark.smoke, pytest.mark.fast]

__all__ = ["ctl", "unix_sock"]

# the events 0.38 added on top of 0.37's, none of them spend related
EVENT_TYPES_0_38 = EVENT_TYPES_0_37 | {"siblings.reaped"}


@pytest.fixture
def driven(tmp_path, monkeypatch):
    return _drive(tmp_path, monkeypatch, docker=False)


def test_the_same_tasks_run_in_the_same_order(driven):
    _sup, final, order, _reviews, _fake = driven
    assert final == "succeeded"
    assert order == EXPECTED_ORDER


def test_the_same_hats_are_dispatched_one_review_ends_the_run(driven):
    sup, _final, order, reviews, _fake = driven
    assert len(order) == len(PLAN) and len(reviews) == 1
    # one planning, one worker per task, one review: nothing else was dispatched
    assert sup.runner.calls == 1 + len(PLAN) + 1
    assert sup.run.read_status()["verdict"] == "verified"


def test_no_spend_event_and_no_event_type_beyond_0_38s(driven):
    sup, _final, _order, _reviews, _fake = driven
    types = {e["type"] for e in _all_events(sup.run.root)}
    assert [t for t in types if t.startswith("spend.")] == []
    assert types - EVENT_TYPES_0_38 == set()
    assert sup.cfg.spend_caps == {}


@pytest.mark.asyncio
async def test_no_surface_shows_a_spend_line_or_key(driven, tmp_path, capsys):
    sup, *_rest = driven
    status = sup.run.read_status()
    assert run_entries(sup.cfg, status) == []
    assert run_entries(sup.cfg, status, cost=True) == []

    registry = _dead_run(tmp_path / "dead", spend=status.get("spend"))
    (registry / "runs" / "capped" / "status.json").write_text(json.dumps(
        {**status, "runId": "capped", "state": "succeeded"}))
    text = _ctl(registry, capsys, "status", "capped")
    assert text.returncode == 0, text.stderr
    assert "spend caps" not in text.stdout.lower()
    doc = json.loads(_ctl(registry, capsys, "--json", "status", "capped").stdout)
    assert "spendCaps" not in doc and "spendBars" not in doc

    app = create_app(sup.cfg, sup.run, sup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://engine") as client:
        assert "spendCaps" not in (await client.get("/status")).json()


def test_a_start_with_no_caps_writes_no_spend_caps_key(ctl):
    res = _start(ctl, "guard-none")
    assert res.returncode == 0, res.stderr
    assert "spend_caps" not in _job_path(ctl, "guard-none").read_text()
    assert JobConfig.load(_job_path(ctl, "guard-none")).spend_caps == {}
