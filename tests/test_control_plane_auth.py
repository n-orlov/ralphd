"""Task 004 (#62 candidate 1, requirement A): "split the surface" -- the
boundary itself, not the defence-in-depth task 003 already landed.

`artifacts/reports/requirement-a-boundary-design.md` explains why task 003
(stripping RALPHD_PORT/RALPHD_API_TOKEN/RALPHD_BIND from a spawned
iteration's own environment) is not sufficient on its own: it only helps
when the operator opted into `--api-token` in the first place, and by
default nobody does, so the mutating half of the API (`/abort`,
`/interrupt`, `/pause`, `/resume`, `/retry`, `/shutdown`,
`PATCH /config/budget`, every `PUT`/`DELETE` under `/config`) was reachable
by anyone who could reach the loopback port at all -- no secret needed.

This module proves the actual boundary: `JobConfig.control_token`
(`engine/config.py`) gates *only* the mutating half of the app
(`classify_method`, task 002's inventory), checked against a header
(`X-Ralphd-Control-Token`) distinct from the existing, still-optional
`Authorization: Bearer <api_token>` gate -- and `ralphctl start`/`resume`
(`cli/main.py`) provision it on every real run *unconditionally*, not as an
operator opt-in, so the boundary holds by default.

The inventory this module parametrises over comes from
`tests/test_route_inventory.py::EXPECTED_MUTATING` (task 002's code-derived
list), not a private copy -- a route added to the wrong listener fails
here too, automatically.
"""

from __future__ import annotations

import httpx
import pytest

from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from test_route_inventory import EXPECTED_MUTATING
from tests.conftest import make_supervisor

CONTROL_TOKEN = "control-secret-for-tests"
CONTROL_HEADER = "X-Ralphd-Control-Token"

# Minimal request bodies so a route's OWN validation doesn't 422/empty-body
# reject before the auth middleware even gets a chance to matter -- not
# load-bearing for the boundary assertion itself (a 422 is not a 401
# either), but it keeps the "correct token is accepted" test's failure
# messages about auth, not about an unrelated body-shape complaint.
_BODIES: dict[tuple[str, str], dict] = {
    ("PATCH", "/config/budget"): {"json": {"iterations": "+1"}},
    ("PUT", "/config/prompts/{name}"): {"content": b"hello"},
    ("PUT", "/config/skills/{name}"): {"content": b"not-a-real-tar"},
    ("PUT", "/config/creds/{name}"): {"content": b"KEY=value\n"},
    ("PUT", "/config/llm"): {"json": {"env": {}}},
    ("POST", "/steering"): {"json": {"message": "hi"}},
    ("POST", "/abort"): {"json": {"reason": "test"}},
}


_supervisor = lambda tmp_path, control_token=CONTROL_TOKEN, **cfg: make_supervisor(
    tmp_path, run_id="unit", cfg_defaults=False, status={"state": "running",
    "iterationsBudget": 5, "iterationsUsed": 0}, iterations=5, control_token=control_token,
    **cfg)


def _client(sup: LoopSupervisor) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


def _concrete(path: str) -> str:
    return path.replace("{name}", "planted").replace("{task_id}", "planted")


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", sorted(EXPECTED_MUTATING))
async def test_iteration_equivalent_client_is_refused(tmp_path, method, path):
    """The acceptance test's own wording: "an iteration (stub pi, with the
    environment a real iteration inherits) fails on every mutating route".
    An iteration never has RALPHD_CONTROL_TOKEN (task 003's env-strip
    covers this coordinate too -- see tests/test_iteration_env_strip.py),
    so a client presenting no `X-Ralphd-Control-Token` header at all is
    exactly what an iteration's own HTTP client would be able to build.
    Asserted per route: a route later added to the wrong listener fails
    this parametrised case instead of passing by omission."""
    sup = _supervisor(tmp_path)
    kwargs = _BODIES.get((method, path), {})
    async with _client(sup) as client:
        resp = await client.request(method, _concrete(path), **kwargs)
    assert resp.status_code == 401, (
        f"{method} {path}: expected 401 without the control token, got "
        f"{resp.status_code}: {resp.text}")


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", sorted(EXPECTED_MUTATING))
async def test_the_correct_control_token_passes_the_gate(tmp_path, method, path):
    """The gate is a real check, not a route that always 401s: the correct
    token reaches the route handler (whatever the handler then does with a
    synthetic body/precondition is that route's own business, asserted
    elsewhere -- only 401 is this test's concern)."""
    sup = _supervisor(tmp_path)
    kwargs = dict(_BODIES.get((method, path), {}))
    kwargs["headers"] = {CONTROL_HEADER: CONTROL_TOKEN}
    async with _client(sup) as client:
        resp = await client.request(method, _concrete(path), **kwargs)
    assert resp.status_code != 401, (
        f"{method} {path}: the correct control token was refused "
        f"({resp.status_code}): {resp.text}")


@pytest.mark.asyncio
async def test_observation_routes_stay_open_for_a_tokenless_client(tmp_path):
    """The other half of the split: the observation routes an iteration
    legitimately needs stay reachable even while the control token is
    configured and the client presents none -- see docs/api.md for the
    full list."""
    sup = _supervisor(tmp_path)
    async with _client(sup) as client:
        for path in ("/status", "/config", "/tasks", "/healthz", "/prd",
                     "/notes", "/steering"):
            resp = await client.get(path)
            assert resp.status_code != 401, f"GET {path}: {resp.status_code}"


@pytest.mark.asyncio
async def test_mutating_routes_stay_open_when_no_control_token_is_configured(tmp_path):
    """Backward compatibility, stated as a test rather than left implicit:
    a JobConfig built directly with no `control_token` (every component
    test in this suite that exercises a mutating route's own business
    logic -- test_budget_patch.py, test_retry_now.py, test_creds_api.py,
    ...) must see exactly the pre-task-004 behaviour. The boundary is
    something `ralphctl start`/`resume` provision by default for a REAL
    run, not something this module's own auth gate invents out of thin
    air for a JobConfig nobody configured."""
    sup = _supervisor(tmp_path, control_token=None)
    async with _client(sup) as client:
        resp = await client.patch("/config/budget", json={"iterations": "+1"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_control_token_never_appears_in_get_config(tmp_path):
    """Same discipline as `api_token` (engine/config.py's `effective()`
    docstring): a secret configured for the control gate must never be
    surfaced back through the observation surface it is meant to keep
    closed to."""
    sup = _supervisor(tmp_path)
    async with _client(sup) as client:
        resp = await client.get("/config")
    assert resp.status_code == 200
    assert CONTROL_TOKEN not in resp.text
