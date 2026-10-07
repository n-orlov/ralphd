"""The hub's undefer proxy explains a missing route, and the one dotted-version
helper it uses compares numerically."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_cli_ui import StubEngineApi, _write_run_with_api, ui  # noqa: F401

from ralphd.cli.versions import compare_versions, parse_version

SENTENCE = "predates undefer (0.31+); resume it on a newer image to use it"
MISSING_ROUTE = {"detail": "Not Found"}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _undefer(tmp_path, ui, run_id, status, **engine_kw):
    engine = StubEngineApi(**engine_kw)
    try:
        registry = tmp_path / "registry"
        _write_run_with_api(registry, run_id, engine, state="running", **status)
        server = ui(registry)
        before = _snapshot(registry)
        code, body = server.post(f"/api/runs/{run_id}/tasks/002/undefer", {})
        assert _snapshot(registry) == before, "the hub wrote host-side"
        return code, body
    finally:
        engine.close()


def _displayed(body) -> str:
    """What app.js's `innermostReason` shows for this body."""
    cur = body
    while isinstance(cur, dict) and isinstance(cur.get("detail"), dict):
        cur = cur["detail"]
    if isinstance(cur, dict):
        if isinstance(cur.get("detail"), str):
            return cur["detail"]
        if isinstance(cur.get("title"), str):
            return cur["title"]
    return body.get("error", "") if isinstance(body, dict) else ""


def test_missing_route_names_the_engine_version(tmp_path, ui):
    code, body = _undefer(tmp_path, ui, "run-v", {"engineVersion": "0.30.0"},
                          undefer_status=404, undefer_body=MISSING_ROUTE)
    assert code == 404, (code, body)
    assert _displayed(body) == f"this run's engine (v0.30.0) {SENTENCE}", body


def test_missing_route_without_a_version_says_the_same_sentence_bare(tmp_path, ui):
    code, body = _undefer(tmp_path, ui, "run-nov", {},
                          undefer_status=404, undefer_body=MISSING_ROUTE)
    assert code == 404, (code, body)
    assert _displayed(body) == f"this run's engine {SENTENCE}", body


def test_missing_route_with_a_malformed_version_is_treated_as_absent(tmp_path, ui):
    code, body = _undefer(tmp_path, ui, "run-bad", {"engineVersion": "dev-build"},
                          undefer_status=404, undefer_body=MISSING_ROUTE)
    assert code == 404, (code, body)
    assert _displayed(body) == f"this run's engine {SENTENCE}", body


def test_engines_own_404_no_such_task_passes_through_unchanged(tmp_path, ui):
    own = {"title": "no such task", "detail": "unknown task '002'"}
    code, body = _undefer(tmp_path, ui, "run-nt", {"engineVersion": "0.30.0"},
                          undefer_status=404, undefer_body=own)
    assert (code, body) == (404, own)


def test_a_title_only_404_no_such_task_passes_through_unchanged(tmp_path, ui):
    own = {"title": "no such task"}
    code, body = _undefer(tmp_path, ui, "run-nt2", {"engineVersion": "0.30.0"},
                          undefer_status=404, undefer_body=own)
    assert (code, body) == (404, own)


@pytest.mark.parametrize("status_code,payload", [
    (409, {"title": "task not deferred", "detail": "task '002' is not deferred"}),
    (503, {"title": "unavailable", "detail": "loop not ready"}),
])
def test_409_and_503_from_the_engine_pass_through_unchanged(tmp_path, ui, status_code, payload):
    code, body = _undefer(tmp_path, ui, f"run-{status_code}", {"engineVersion": "0.30.0"},
                          undefer_status=status_code, undefer_body=payload)
    assert (code, body) == (status_code, payload)


# ---------------------------------------------------------- the version helper

def test_version_compares_numerically_not_as_text():
    assert compare_versions("0.9.0", "0.31.0") == -1
    assert compare_versions("0.31.0", "0.9.0") == 1
    assert compare_versions("0.31.0", "0.31.0") == 0
    assert compare_versions("0.31", "0.31.0") == 0
    assert parse_version("0.31.0") == (0, 31, 0)


@pytest.mark.parametrize("bad", ["", "abc", "0.31.x", "0..1", "v0.31.0", "1.2.3-rc1",
                                 " ", ".", None, 31, ["0", "31"]])
def test_non_numeric_or_malformed_version_is_absent(bad):
    assert parse_version(bad) is None
    assert compare_versions(bad, "0.31.0") is None
    assert compare_versions("0.31.0", bad) is None
