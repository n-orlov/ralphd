"""B.3 -- the engine warns ONCE, at startup, when the `notify` hat tool is
wired (it always is) but no notifier URL is configured (task 011, #197).

Drives a real `LoopSupervisor(...)` constructor over a real run dir --
never a mock of `_check_notifier_configured` itself -- proving the exact
condition `hat_tools.send_notification`'s own "no notifier URL configured"
branch answers with a no-op: an unconfigured run's every `notify` call is
silently swallowed unless the operator is told so once, up front.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import run_record
from tests.conftest import make_supervisor


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def test_an_unconfigured_run_emits_the_startup_warning_exactly_once(tmp_path):
    root = tmp_path / "run"
    root.mkdir()

    sup = make_supervisor(root, cfg_defaults=False)

    assert sup.notifier is None
    events = _events(root, run_record.NOTIFIER_UNCONFIGURED_EVENT)
    assert len(events) == 1


def test_a_configured_run_emits_no_such_warning(tmp_path):
    root = tmp_path / "run"
    root.mkdir()

    sup = make_supervisor(root, cfg_defaults=False,
                          notifier_url="http://notifier.example/notify")

    assert sup.notifier is not None
    assert _events(root, run_record.NOTIFIER_UNCONFIGURED_EVENT) == []
