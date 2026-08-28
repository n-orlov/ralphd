"""Task 045 (#3): `PATCH /config/budget` tops up the iteration budget in flight.

Driven through the *real* HTTP route (ASGI, same shape as
tests/test_retry_now.py) against a real LoopSupervisor, so the accept/reject
matrix, the audit event and the "no restart needed" contract
(`budget_left()` flips without touching the container) are all asserted
against production code paths.

Task 003 (#94 (a), operator-added scope): the same route also grew three
more knobs -- `maxApproaches`, `iterationTimeoutS`, `jobDeadlineEpochS` --
reusing `resolve_iterations_spec`'s own `"+N"`/absolute grammar. No new
route; `tests/test_route_inventory.py` and `tests/test_control_plane_auth.py`
keep covering it for free. The tests below the original `iterations` matrix
exercise each new knob's relative/absolute forms, its 409 guard (where one
exists -- `maxApproaches` below the current approach, a deadline in the
past), and that combining knobs in one request applies all of them.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir


def _supervisor(tmp_path, iterations: int = 25, used: int = 0,
                **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    sup = LoopSupervisor(JobConfig(run_id="unit", iterations=iterations, **cfg_kw),
                         run, tmp_path)
    sup.iterations_used = used
    run.update_status(state="running", iterationsBudget=iterations,
                      iterationsUsed=used)
    return sup


def _client(sup: LoopSupervisor) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


def _events(run: RunDir, type_: str) -> list[dict]:
    log = run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


@pytest.mark.asyncio
async def test_relative_topup_applies_live_and_is_visible_everywhere(tmp_path):
    sup = _supervisor(tmp_path, iterations=5, used=5)
    # The job is out of budget: the loop would stop at this boundary.
    assert not sup.budget_left()

    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterations": "+10"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"iterations": 15, "previous": 5,
                               "iterationsUsed": 5}
        status = (await client.get("/status")).json()
        config = (await client.get("/config")).json()

    # Applied without restarting anything: the same live supervisor now has
    # budget again.
    assert sup.cfg.iterations == 15
    assert sup.budget_left()
    # ... and every read surface agrees immediately.
    assert status["iterationsBudget"] == 15
    assert config["budgets"]["iterations"] == 15
    assert json.loads((sup.run.root / "status.json").read_text())[
        "iterationsBudget"] == 15
    # Audit trail.
    events = _events(sup.run, "budget_changed")
    assert len(events) == 1
    assert events[0]["field"] == "iterations"
    assert events[0]["previous"] == 5 and events[0]["iterations"] == 15
    assert events[0]["delta"] == 10 and events[0]["iterationsUsed"] == 5
    assert events[0]["source"] == "api"


@pytest.mark.asyncio
async def test_absolute_value_sets_the_budget(tmp_path):
    sup = _supervisor(tmp_path, iterations=25, used=3)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterations": 40})
        assert resp.status_code == 200, resp.text
        assert resp.json()["iterations"] == 40
        assert (await client.get("/config")).json()["budgets"]["iterations"] == 40
    assert sup.cfg.iterations == 40
    assert _events(sup.run, "budget_changed")[0]["delta"] == 15


@pytest.mark.asyncio
async def test_absolute_string_form_is_accepted(tmp_path):
    sup = _supervisor(tmp_path, iterations=10, used=2)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterations": "30"})
    assert resp.status_code == 200, resp.text
    assert sup.cfg.iterations == 30


@pytest.mark.asyncio
async def test_lowering_to_exactly_iterations_used_is_allowed(tmp_path):
    sup = _supervisor(tmp_path, iterations=25, used=7)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterations": 7})
    assert resp.status_code == 200, resp.text
    assert sup.cfg.iterations == 7
    assert not sup.budget_left(), "the run is now at its (lowered) budget"


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [6, "6", 1])
async def test_rejects_a_budget_below_iterations_used(tmp_path, spec):
    sup = _supervisor(tmp_path, iterations=7, used=7)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterations": spec})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["title"] == "budget below iterations used"
    assert "7 iteration(s) already used" in detail["detail"]
    assert sup.cfg.iterations == 7, "rejected requests change nothing"
    assert not _events(sup.run, "budget_changed")


@pytest.mark.asyncio
async def test_refunded_infra_attempts_do_not_block_a_lower_budget(tmp_path):
    """The floor is the *charged* count published as iterationsUsed, not the
    raw attempt counter: an infra-refunded retry must not make a budget look
    more consumed than the operator's own /status says it is."""
    sup = _supervisor(tmp_path, iterations=10, used=7)
    sup._infra_refunded = 3  # 3 attempts were retried after an infra fault
    assert sup.iterations_used_charged == 4
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterations": 4})
    assert resp.status_code == 200, resp.text
    assert resp.json()["iterationsUsed"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("body,fragment", [
    ({}, "iterations required"),
    ({"iterations": None}, "iterations required"),
    ({"iterations": "abc"}, "invalid iterations"),
    ({"iterations": ""}, "invalid iterations"),
    ({"iterations": "+abc"}, "invalid iterations"),
    ({"iterations": -5}, "invalid iterations"),
    ({"iterations": "-5"}, "invalid iterations"),
    ({"iterations": 0}, "invalid iterations"),
    ({"iterations": "+-3"}, "invalid iterations"),
    ({"iterations": 12.5}, "invalid iterations"),
    ({"iterations": True}, "invalid iterations"),
    ({"iterations": [10]}, "invalid iterations"),
])
async def test_rejects_bad_input_with_a_problem_detail(tmp_path, body, fragment):
    sup = _supervisor(tmp_path, iterations=25, used=1)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json=body)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["title"] == fragment
    assert detail["status"] == 422 and detail["detail"], detail
    assert sup.cfg.iterations == 25
    assert not _events(sup.run, "budget_changed")


@pytest.mark.asyncio
async def test_409_on_a_finished_job(tmp_path):
    sup = _supervisor(tmp_path, iterations=5, used=5)
    sup.run.update_status(state="succeeded")
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterations": "+10"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["title"] == "job finished"
    assert "resume" in detail["detail"]
    assert sup.cfg.iterations == 5
    assert not _events(sup.run, "budget_changed")


# -- task 003 (#94 (a), operator-added scope): the three knobs the route did
# not cover -- maxApproaches, iterationTimeoutS, jobDeadlineEpochS. Reuses
# resolve_iterations_spec's own "+N"/absolute grammar, proven above for
# `iterations`; these tests exercise the SAME grammar against the three new
# fields, plus their two 409 guards (symmetric with `iterations`'s own
# `api.py:441-446` refusal above). ---------------------------------------

@pytest.mark.asyncio
async def test_max_approaches_relative_topup_is_observed_at_next_read(tmp_path):
    sup = _supervisor(tmp_path, max_approaches=3)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"maxApproaches": "+2"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"maxApproaches": 5, "previousMaxApproaches": 3}
        config = (await client.get("/config")).json()
        status = (await client.get("/status")).json()
    assert sup.cfg.max_approaches == 5
    assert config["budgets"]["maxApproaches"] == 5
    assert status["maxApproaches"] == 5
    events = _events(sup.run, "budget_changed")
    assert events[-1]["field"] == "maxApproaches"
    assert events[-1]["previous"] == 3 and events[-1]["maxApproaches"] == 5


@pytest.mark.asyncio
async def test_max_approaches_absolute_value_sets_it(tmp_path):
    sup = _supervisor(tmp_path, max_approaches=3)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"maxApproaches": 9})
        assert resp.status_code == 200, resp.text
    assert sup.cfg.max_approaches == 9


@pytest.mark.asyncio
async def test_max_approaches_below_current_approach_is_refused_with_409(tmp_path):
    sup = _supervisor(tmp_path, max_approaches=5)
    sup.run.update_status(approach=4)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"maxApproaches": 2})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["title"] == "max approaches below current approach"
    assert "4" in detail["detail"]
    assert sup.cfg.max_approaches == 5, "rejected requests change nothing"
    assert not _events(sup.run, "budget_changed")


@pytest.mark.asyncio
async def test_max_approaches_equal_to_current_approach_is_allowed(tmp_path):
    sup = _supervisor(tmp_path, max_approaches=5)
    sup.run.update_status(approach=4)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"maxApproaches": 4})
    assert resp.status_code == 200, resp.text
    assert sup.cfg.max_approaches == 4


@pytest.mark.asyncio
async def test_iteration_timeout_relative_topup_is_observed_at_next_read(tmp_path):
    sup = _supervisor(tmp_path, iteration_timeout_s=600)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget",
                                  json={"iterationTimeoutS": "+300"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"iterationTimeoutS": 900,
                               "previousIterationTimeoutS": 600}
        config = (await client.get("/config")).json()
    assert sup.cfg.iteration_timeout_s == 900
    assert config["budgets"]["iterationTimeoutS"] == 900
    events = _events(sup.run, "budget_changed")
    assert events[-1]["field"] == "iterationTimeoutS"
    assert events[-1]["iterationTimeoutS"] == 900


@pytest.mark.asyncio
async def test_iteration_timeout_absolute_value_sets_it(tmp_path):
    sup = _supervisor(tmp_path, iteration_timeout_s=600)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterationTimeoutS": 120})
        assert resp.status_code == 200, resp.text
    assert sup.cfg.iteration_timeout_s == 120


@pytest.mark.asyncio
async def test_job_deadline_relative_topup_moves_both_clocks_and_republishes(tmp_path):
    from ralphd.engine.state import utc_from_epoch

    sup = _supervisor(tmp_path, job_timeout_s=3600)
    before_mono = sup.deadline
    before_epoch = sup.deadline_epoch

    async with _client(sup) as client:
        resp = await client.patch("/config/budget",
                                  json={"jobDeadlineEpochS": "+900"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["jobDeadlineEpochS"] == pytest.approx(before_epoch + 900)
        assert body["previousJobDeadlineEpochS"] == pytest.approx(before_epoch)
        assert body["deadlineAt"] == utc_from_epoch(before_epoch + 900)
        status = (await client.get("/status")).json()
    assert sup.deadline_epoch == pytest.approx(before_epoch + 900)
    assert sup.deadline == pytest.approx(before_mono + 900)
    assert status["deadlineAt"] == utc_from_epoch(before_epoch + 900)
    events = _events(sup.run, "budget_changed")
    assert events[-1]["field"] == "jobDeadlineEpochS"
    # loop.set_job_deadline's own "deadline_set" event fires too -- a
    # different type than this API-level audit event, never conflated.
    assert _events(sup.run, "deadline_set")


@pytest.mark.asyncio
async def test_job_deadline_absolute_value_sets_it(tmp_path):
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    new_epoch = sup.deadline_epoch + 1800
    async with _client(sup) as client:
        resp = await client.patch("/config/budget",
                                  json={"jobDeadlineEpochS": int(new_epoch)})
        assert resp.status_code == 200, resp.text
    assert sup.deadline_epoch == int(new_epoch)


@pytest.mark.asyncio
async def test_job_deadline_in_the_past_is_refused_with_409(tmp_path):
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    past_epoch = int(sup.deadline_epoch - 10_000_000)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget",
                                  json={"jobDeadlineEpochS": past_epoch})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["title"] == "deadline in the past"
    assert not _events(sup.run, "budget_changed")
    assert not _events(sup.run, "deadline_set")


@pytest.mark.asyncio
async def test_a_request_with_only_a_new_knob_does_not_require_iterations(tmp_path):
    """The empty-body 422 (\"iterations required\") is unchanged for a
    request with NO recognised field at all, but a request naming only one
    of the three new knobs must not be forced to also carry `iterations`."""
    sup = _supervisor(tmp_path, iterations=25, max_approaches=3)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"maxApproaches": 4})
    assert resp.status_code == 200, resp.text
    assert sup.cfg.iterations == 25, "untouched -- only maxApproaches was asked for"
    assert sup.cfg.max_approaches == 4


@pytest.mark.asyncio
async def test_multiple_knobs_in_one_request_all_apply(tmp_path):
    sup = _supervisor(tmp_path, iterations=25, used=5, max_approaches=3,
                      iteration_timeout_s=600)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={
            "iterations": "+5",
            "maxApproaches": "+1",
            "iterationTimeoutS": "+60",
        })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["iterations"] == 30
    assert body["maxApproaches"] == 4
    assert body["iterationTimeoutS"] == 660
    assert sup.cfg.iterations == 30
    assert sup.cfg.max_approaches == 4
    assert sup.cfg.iteration_timeout_s == 660

