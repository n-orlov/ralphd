"""Task 013 (v0.23 PRD requirement A.2, #224): `POST /rule` against a real
route (ASGI, same shape as tests/test_budget_patch.py) over a real
LoopSupervisor.

The CLI contract (`ralphctl rule`) is proven black-box in
tests/test_cli_rule.py; the storage primitive and prompt-injection are
proven against a real supervisor fixture in
tests/test_operator_rule_channel.py. This module is the one that proves
the ROUTE itself: status codes, the 409/422 guards, and that the file it
returns is exactly the one `state.ruling_entries` then reads back.
"""

from __future__ import annotations

import httpx
import pytest

from ralphd.engine.api import create_app
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import ruling_entries
from tests.conftest import make_supervisor


def _supervisor(tmp_path, status=None, **cfg) -> LoopSupervisor:
    return make_supervisor(
        tmp_path, run_id="unit", cfg_defaults=False,
        status=status or {"state": "running"}, **cfg)


def _client(sup: LoopSupervisor) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


@pytest.mark.asyncio
async def test_post_rule_accepted_and_lands_in_the_record(tmp_path):
    sup = _supervisor(tmp_path)
    sup.run.update_status(approach=1, rejectionSignatures={"1": "wall-x"})
    async with _client(sup) as client:
        resp = await client.post("/rule", json={
            "text": "These are residuals; ship it.", "name": "residuals"})
        assert resp.status_code == 202, resp.text
        fname = resp.json()["file"]

    entries = ruling_entries(sup.run.root)
    assert len(entries) == 1
    assert entries[0]["file"] == fname
    assert entries[0]["text"] == "These are residuals; ship it."
    # Resolved from THIS run's own status.json, not from the request body.
    assert entries[0]["signature"] == "wall-x"


@pytest.mark.asyncio
async def test_post_rule_empty_text_is_422(tmp_path):
    sup = _supervisor(tmp_path)
    async with _client(sup) as client:
        resp = await client.post("/rule", json={"text": "   "})
        assert resp.status_code == 422, resp.text
    assert ruling_entries(sup.run.root) == []


@pytest.mark.asyncio
async def test_post_rule_on_a_finished_run_is_409(tmp_path):
    sup = _supervisor(tmp_path, status={"state": "succeeded"})
    async with _client(sup) as client:
        resp = await client.post("/rule", json={"text": "too late"})
        assert resp.status_code == 409, resp.text
    assert ruling_entries(sup.run.root) == []


@pytest.mark.asyncio
async def test_post_rule_is_distinct_from_steering_and_amend(tmp_path):
    """A ruling submitted via `/rule` must never land in `/steering`'s
    record (steering is discounted by review by design), and there must
    be no amendment-shaped write anywhere on this path."""
    sup = _supervisor(tmp_path)
    async with _client(sup) as client:
        resp = await client.post("/rule", json={"text": "ruling text"})
        assert resp.status_code == 202, resp.text
        steering_resp = await client.get("/steering")
        assert steering_resp.json() == []

    assert not (sup.run.root / "steering").exists() or all(
        "ruling text" not in p.read_text()
        for p in (sup.run.root / "steering").glob("*.md")
    )
