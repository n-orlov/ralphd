"""E.3 -- a workspace whose own recorded version differs from the running
engine's records one startup event naming both versions and alerts through
the engine's own alert path (task 017, PRD requirement E.3, #190/#191).

Drives a real `LoopSupervisor(...)` constructor over a real run dir and a
real `src/ralphd/__init__.py` under the workspace it is handed -- never a
mock of `_check_workspace_version_drift` itself. The notifier path is the
SAME production seam D.1/E.5 already use (`self.notifier`,
`hat_tools.send_notification`), monkeypatched at its one HTTP boundary
(`hat_tools._http_post_text`), never a second channel.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd import __version__ as ENGINE_VERSION
from ralphd.engine import hat_tools
from ralphd.engine.loop import _read_workspace_version
from tests.conftest import make_supervisor


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _write_workspace_version(workspace: Path, version: str) -> None:
    pkg = workspace / "src" / "ralphd"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n')


# ===========================================================================
# 1. A differing workspace version emits exactly one startup event naming
#    both versions.
# ===========================================================================


def test_a_differing_workspace_version_emits_one_startup_event_naming_both_versions(
        tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    ws = root / "ws"
    ws.mkdir()
    differing = "0.0.1-not-the-engine"
    assert differing != ENGINE_VERSION
    _write_workspace_version(ws, differing)

    make_supervisor(root, workspace=ws, cfg_defaults=False)

    events = _events(root, "workspace_version_mismatch")
    assert len(events) == 1
    assert events[0]["workspaceVersion"] == differing
    assert events[0]["engineVersion"] == ENGINE_VERSION
    # G.3 (task 035): the drifting version is on status.json too.
    assert json.loads(
        (root / "status.json").read_text())["workspaceVersion"] == differing


# ===========================================================================
# 2. That event reaches the operator through the SAME configured-notifier
#    path D.1/E.5 already use -- one notification, one send.
# ===========================================================================


def test_the_mismatch_notifies_through_the_configured_notifier(
        tmp_path, monkeypatch):
    calls = []

    def stub_poster(url, text, timeout_s):
        calls.append((url, text, timeout_s))
        return 200, None

    monkeypatch.setattr(hat_tools, "_http_post_text", stub_poster)

    root = tmp_path / "run"
    root.mkdir()
    ws = root / "ws"
    ws.mkdir()
    differing = "0.0.1-not-the-engine"
    _write_workspace_version(ws, differing)

    make_supervisor(root, workspace=ws, cfg_defaults=False,
                     notifier_url="http://notifier.example/notify")

    assert len(calls) == 1
    posted_url, posted_text, _ = calls[0]
    assert posted_url == "http://notifier.example/notify"
    assert differing in posted_text
    assert ENGINE_VERSION in posted_text

    sent = _events(root, hat_tools.NOTIFICATION_SENT_EVENT)
    assert len(sent) == 1


def test_no_notifier_configured_still_emits_the_event_but_sends_nothing(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **kw: calls.append(a) or (200, None))

    root = tmp_path / "run"
    root.mkdir()
    ws = root / "ws"
    ws.mkdir()
    _write_workspace_version(ws, "0.0.1-not-the-engine")

    sup = make_supervisor(root, workspace=ws, cfg_defaults=False)

    assert sup.notifier is None
    assert calls == []
    assert len(_events(root, "workspace_version_mismatch")) == 1


# ===========================================================================
# 3. A matching version, or no readable version at all, is silent -- no
#    event, no notification.
# ===========================================================================


def test_a_matching_workspace_version_emits_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **kw: calls.append(a) or (200, None))

    root = tmp_path / "run"
    root.mkdir()
    ws = root / "ws"
    ws.mkdir()
    _write_workspace_version(ws, ENGINE_VERSION)

    make_supervisor(root, workspace=ws, cfg_defaults=False,
                     notifier_url="http://notifier.example/notify")

    assert _events(root, "workspace_version_mismatch") == []
    assert calls == []
    # ... and yet the version is KNOWABLE, so G.3 (task 035) still persists
    # it on status.json for the hub run page to show beside F.4's
    # `engineVersion`: silence is about the drift alert, not about the field.
    status = json.loads((root / "status.json").read_text())
    assert status["workspaceVersion"] == ENGINE_VERSION
    assert status["engineVersion"] == ENGINE_VERSION


def test_a_workspace_with_no_version_literal_emits_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **kw: calls.append(a) or (200, None))

    root = tmp_path / "run"
    root.mkdir()
    ws = root / "ws"        # no src/ralphd/__init__.py at all -- the
    ws.mkdir()              # ordinary shape of every other test's workspace

    make_supervisor(root, workspace=ws, cfg_defaults=False,
                     notifier_url="http://notifier.example/notify")

    assert _events(root, "workspace_version_mismatch") == []
    assert calls == []
    # Nothing knowable here, so G.3's field stays ABSENT -- its absence
    # means "not knowable", never "equal to the engine's version".
    assert "workspaceVersion" not in json.loads(
        (root / "status.json").read_text())


# ===========================================================================
# 4. The bare reader helper itself.
# ===========================================================================


def test_read_workspace_version_returns_the_literal(tmp_path):
    _write_workspace_version(tmp_path, "1.2.3")
    assert _read_workspace_version(tmp_path) == "1.2.3"


def test_read_workspace_version_returns_none_when_absent(tmp_path):
    assert _read_workspace_version(tmp_path) is None
