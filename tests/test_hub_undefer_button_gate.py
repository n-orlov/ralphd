"""The hub hides the Undefer button on a run whose engine predates 0.31.0.

The run payload carries `undeferAvailable`, computed server-side with the one
dotted-version helper; `app.js` only places the button when it is true and
does no version arithmetic."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_cli_ui import StubEngineApi, _write_run_with_api, ui  # noqa: F401

from ralphd.cli import ui_server

APP_JS = (Path(ui_server.__file__).parent / "web" / "app.js").read_text()


def _payload(tmp_path, ui, status):
    engine = StubEngineApi()
    try:
        registry = tmp_path / "registry"
        _write_run_with_api(registry, "run-g", engine, state="running", **status)
        code, body = ui(registry).get("/api/runs/run-g")
        assert code == 200, body
        return body
    finally:
        engine.close()


@pytest.mark.parametrize("version,expected", [
    ("0.30.0", False),
    ("0.9.0", False),
    ("0.31.0", True),
    ("0.32.2", True),
    ("0.100.0", True),
    (None, True),
    ("not-a-version", True),
    ("0.31.x", True),
    (31, True),
])
def test_payload_boolean_follows_the_engine_version(tmp_path, ui, version, expected):
    status = {} if version is None else {"engineVersion": version}
    body = _payload(tmp_path, ui, status)
    assert body["undeferAvailable"] is expected, (version, body.get("undeferAvailable"))


def test_the_boolean_is_computed_by_the_version_helper():
    src = Path(ui_server.__file__).read_text()
    assert "compare_versions(engine_version, UNDEFER_MIN_ENGINE_VERSION)" in src


def _block(name: str) -> str:
    return APP_JS.split(f"function {name}(")[1].split("\nfunction ")[0]


def test_app_js_places_both_buttons_only_on_the_payload_boolean():
    tasks = _block("renderTasks")
    assert "undefer.undeferAvailable === true" in tasks, tasks
    assert tasks.index("undefer.undeferAvailable === true") < tasks.index("undeferButton("), tasks
    cur = _block("renderCurrentIteration")
    assert "undeferAvailable === true" in cur, cur
    assert cur.index("undeferAvailable === true") < cur.index("undeferButton("), cur
    assert "lastUndeferAvailable = body.undeferAvailable === true" in APP_JS


def test_app_js_does_no_version_arithmetic_for_the_gate():
    gate = "\n".join([_block("renderTasks"), _block("renderCurrentIteration"),
                      _block("undeferButton")])
    assert not re.search(r"engineVersion|parseVersion|compareVersion|0\.31|split\(\"\.\"\)", gate), \
        "app.js must not compute the gate from a version"
    assert not re.search(r"parseVersion|compareVersion|compare_versions", APP_JS)
