"""B.3 -- the engine warns ONCE, at startup, when the `notify` hat tool is
wired (it always is) but no notifier URL is configured (task 011, #197).

Drives a real `LoopSupervisor(...)` constructor over a real run dir --
never a mock of `_check_notifier_configured` itself -- proving the exact
condition `hat_tools.send_notification`'s own "no notifier URL configured"
branch answers with a no-op: an unconfigured run's every `notify` call is
silently swallowed unless the operator is told so once, up front.

Cure of requirement B.3 (DoD 9): `notifier_url: ""` in a real `job.yaml`
fixture is a SUPPORTED YAML representation of "no endpoint", distinct from
the key being omitted, that `_check_notifier_configured` originally missed
(it tested `cfg.notifier_url is None` only) -- the fixture-driven tests
below load that literal file through `JobConfig.load()` itself, the same
reader a real run uses, rather than constructing a `JobConfig` by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import hat_tools, run_record
from ralphd.engine.config import JobConfig
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


def _load_notifier_url_fixture(tmp_path: Path, yaml_text: str) -> JobConfig:
    """A real `job.yaml` fixture on disk, loaded through `JobConfig.load()`
    itself -- never a hand-built `JobConfig(notifier_url=...)` -- so these
    tests prove the exact YAML representation a job.yaml key produces,
    not merely what the engine does once handed an already-resolved value.
    """
    path = tmp_path / "fixture-job.yaml"
    path.write_text(yaml_text)
    return JobConfig.load(path)


def test_a_job_yaml_empty_string_notifier_url_warns_as_unconfigured(tmp_path):
    """Cure of B.3 DoD 9: `notifier_url: ""` is a real, supported YAML
    representation of "no endpoint" -- distinct from the key being absent
    -- and must warn exactly like the omitted/`None` case, not be silently
    read as configured.
    """
    cfg = _load_notifier_url_fixture(tmp_path, 'notifier_url: ""\n')
    assert cfg.notifier_url == ""

    root = tmp_path / "run"
    root.mkdir()
    sup = make_supervisor(root, cfg_defaults=False,
                          notifier_url=cfg.notifier_url)

    assert sup.notifier is None
    events = _events(root, run_record.NOTIFIER_UNCONFIGURED_EVENT)
    assert len(events) == 1

    result = hat_tools.send_notification(sup.run, "probe", cfg.notifier_url)
    assert result["ok"] is False
    assert "no notifier URL configured" in result["error"]


def test_an_omitted_job_yaml_notifier_url_warns_the_same_as_empty_string(tmp_path):
    """Same fixture shape as the empty-string case above, minus the key
    entirely -- both are "no endpoint", and both must warn identically.
    """
    cfg = _load_notifier_url_fixture(tmp_path, "run_id: fixture\n")
    assert cfg.notifier_url is None

    root = tmp_path / "run"
    root.mkdir()
    sup = make_supervisor(root, cfg_defaults=False,
                          notifier_url=cfg.notifier_url)

    assert sup.notifier is None
    assert len(_events(root, run_record.NOTIFIER_UNCONFIGURED_EVENT)) == 1


def test_a_job_yaml_nonempty_notifier_url_warns_nothing(tmp_path):
    """A configured fixture (nonempty string) is the control case: no
    `notifier.unconfigured` event, loaded through the same `JobConfig.
    load()` path as the two unconfigured fixtures above.
    """
    cfg = _load_notifier_url_fixture(
        tmp_path, 'notifier_url: "http://notifier.example/notify"\n')
    assert cfg.notifier_url == "http://notifier.example/notify"

    root = tmp_path / "run"
    root.mkdir()
    sup = make_supervisor(root, cfg_defaults=False,
                          notifier_url=cfg.notifier_url)

    assert sup.notifier is not None
    assert _events(root, run_record.NOTIFIER_UNCONFIGURED_EVENT) == []
