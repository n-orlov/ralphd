"""Task 002 (#62): a code-derived route inventory, classified mutating vs
observation, feeding requirement A's per-route boundary (task 004).

The inventory comes from the live FastAPI app returned by
``ralphd.engine.api.create_app`` -- ``app.routes`` -- not a hand-maintained
list, so a route added or moved fails this test instead of silently
changing the classification the rest of the wave relies on. See
artifacts/reports/requirement-a-boundary-design.md for the design writeup
and why "split the surface" (candidate 1) is the chosen boundary shape.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI

from ralphd.engine.api import classify_method, create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir

# FastAPI/Starlette's own auto-generated documentation UI -- framework
# furniture, not part of the PRD's route inventory. Named explicitly rather
# than silently allow-listed by method, so excluding them is itself visible
# and reviewable.
_FRAMEWORK_PATHS = frozenset(
    {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)

# Pinned inventory of every mutating (method, path) pair the wired app
# serves at HEAD. A route moved to the wrong listener, renamed, or added
# without updating this list fails test_mutating_route_inventory_matches_*
# rather than passing by omission.
EXPECTED_MUTATING = frozenset(
    {
        ("PATCH", "/config/budget"),
        ("PUT", "/config/prompts/{name}"),
        ("PUT", "/config/skills/{name}"),
        ("DELETE", "/config/skills/{name}"),
        ("PUT", "/config/creds/{name}"),
        ("DELETE", "/config/creds/{name}"),
        ("PUT", "/config/llm"),
        ("POST", "/steering"),
        ("POST", "/interrupt"),
        ("POST", "/pause"),
        ("POST", "/resume"),
        ("POST", "/retry"),
        ("POST", "/abort"),
        ("POST", "/shutdown"),
    }
)


def classify(method: str) -> str:
    """Task 002/004 (#62): thin alias over `ralphd.engine.api.classify_method`
    -- kept as a module-level name here so the rest of this file (written
    before the canonical copy moved into `api.py`) does not have to change,
    but there is exactly one classification rule, imported, not
    re-declared. See the mutation-case test below for why 'unclassified'
    must never default either way.
    """
    return classify_method(method)


def app_routes(app: FastAPI) -> list[tuple[str, str]]:
    """(method, path) pairs for every concrete HTTP route the app serves,
    excluding framework furniture, derived from the live app object."""
    out: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or path is None or path in _FRAMEWORK_PATHS:
            continue
        for method in sorted(methods):
            out.append((method, path))
    return out


def _real_app(tmp_path: Path) -> FastAPI:
    run = RunDir(root=tmp_path)
    sup = LoopSupervisor(JobConfig(run_id="unit", iterations=5), run, tmp_path)
    return create_app(sup.cfg, sup.run, sup)


def test_every_real_route_is_classified(tmp_path):
    app = _real_app(tmp_path)
    routes = app_routes(app)
    assert routes, "no routes found -- fixture is broken"

    unclassified = [(m, p) for (m, p) in routes if classify(m) == "unclassified"]
    assert not unclassified, f"unclassified routes: {unclassified}"


def test_mutating_route_inventory_matches_the_wired_app(tmp_path):
    app = _real_app(tmp_path)
    mutating = {(m, p) for (m, p) in app_routes(app) if classify(m) == "mutating"}
    assert mutating == EXPECTED_MUTATING


def test_observation_routes_are_everything_else(tmp_path):
    app = _real_app(tmp_path)
    routes = app_routes(app)
    observation = {(m, p) for (m, p) in routes if classify(m) == "observation"}
    expected_observation = set(routes) - EXPECTED_MUTATING
    assert observation == expected_observation
    # Sanity: the split actually partitions every route, none dropped.
    assert observation | EXPECTED_MUTATING == set(routes)


def test_mutation_case_a_synthetic_unclassified_route_is_caught():
    """Proof that "no unclassified routes" is doing real work: register a
    route on a bare app with a method that is neither a safe read nor one
    of the four mutating verbs (PROPFIND), and assert the classifier flags
    it rather than defaulting it to 'observation' or 'mutating'."""
    app = FastAPI()

    @app.api_route("/synthetic", methods=["PROPFIND"])
    async def synthetic():  # pragma: no cover - never invoked
        return {}

    routes = app_routes(app)
    assert routes == [("PROPFIND", "/synthetic")]

    unclassified = [(m, p) for (m, p) in routes if classify(m) == "unclassified"]
    assert unclassified == [("PROPFIND", "/synthetic")]
