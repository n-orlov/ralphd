"""Task 040 (steer 002): the test suite must never talk to the operator's
real notifier, even though this container's own ambient environment
legitimately exports `RALPHD_NOTIFIER_URL` for the run's own engine.

The chain steer 002 traced: `tests/conftest.py` used to build every
spawned engine/`ralphctl` child's env as `{**os.environ, ...}`;
`JobConfig.load()` (`engine/config.py`) reads `RALPHD_NOTIFIER_URL`
straight out of `os.environ`; and task 012's last-approach stagnation
pause escalates through `self.notifier` whenever `cfg.notifier_url` is
set. So every test engine that reached that pause -- and every in-process
call to `JobConfig.load()` -- POSTed (or was one config read away from
POSTing) to the operator's real Telegram.

Two tests, both under the SAME simulated ambient export (the module-scoped
`_ambient_notifier_export` fixture below, set up before either test's own
function-scoped fixtures because it is a HIGHER scope -- pytest always
instantiates a higher-scoped fixture before a same-test lower-scoped one,
autouse or not):

1. `test_a_spawned_engine_that_pauses_never_gets_the_ambient_notifier_url`
   opts OUT of `tests/conftest.py`'s autouse in-process scrub (the
   `NOTIFIER_ENV` marker) so its own `os.environ` still carries the
   ambient export, then spawns a real `LiveRun` (the same subprocess
   shape `tests/test_e2e.py` uses) that reaches the last-approach
   stagnation pause -- proving the CHILD-env scrub in
   `LiveRun`/`ralphctl` holds even when the parent process's own env is
   not clean.
2. `test_jobconfig_load_in_process_never_sees_the_ambient_notifier_url`
   does NOT opt out, so the autouse fixture strips the same ambient export
   before this test's body runs, and a direct in-process `JobConfig.load()`
   call proves it: `notifier_url` is `None`.
"""

from __future__ import annotations

import json
import os

import pytest

from ralphd.engine.config import JobConfig
from tests.conftest import NOTIFIER_ENV_VAR, LiveRun

AMBIENT_NOTIFIER_URL = "http://127.0.0.1:9/notify"


@pytest.fixture(scope="module", autouse=True)
def _ambient_notifier_export():
    """Simulates this container's own real ambient export. Module-scoped
    (broader than the function-scoped autouse scrub fixture in
    tests/conftest.py) so pytest sets THIS one up first for every test in
    this module -- exactly like a real shell export already sitting in
    os.environ before pytest even starts collecting -- and the
    conftest-level fixture's own delenv/restore dance around each test
    body is what is actually under test here, not this fixture itself."""
    os.environ[NOTIFIER_ENV_VAR] = AMBIENT_NOTIFIER_URL
    yield
    os.environ.pop(NOTIFIER_ENV_VAR, None)


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[LiveRun] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> LiveRun:
        defaults = {"run_id": "notifier-scrub", "iterations": 20,
                    "max_approaches": 1, "on_complete": "idle"}
        e = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


@pytest.mark.NOTIFIER_ENV
def test_a_spawned_engine_that_pauses_never_gets_the_ambient_notifier_url(
        engine_factory):
    # This test's own os.environ still carries the ambient export (the
    # NOTIFIER_ENV marker opted it out of the autouse scrub) -- the exact
    # precondition that used to leak into a spawned child via
    # `{**os.environ, ...}`.
    assert os.environ.get(NOTIFIER_ENV_VAR) == AMBIENT_NOTIFIER_URL

    # v0.36 F.3: an unreadable review verdict is an infra fault, not a pause;
    # with the outage budget spent at once the engine ends `failed` (verdict
    # `unreviewed`) -- the same "stops on its own initiative" situation this
    # probe needs, where an ambient notifier would be used if it leaked.
    e = engine_factory(job={"infra_outage_budget_s": 0},
                       stub_env={"STUB_REVIEW_MALFORMED_COUNT": "99"})
    status = e.wait_terminal(timeout=90)
    assert status["state"] == "failed" and status["verdict"] == "unreviewed"

    events_path = e.run_dir / "events.jsonl"
    events = []
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    sent = [ev for ev in events if ev.get("type") == "notification.sent"]
    # Either no notification.sent event was ever emitted (self.notifier
    # stayed None because the child's own cfg.notifier_url was never set),
    # or -- if one exists for some other reason -- it must never carry the
    # operator's real endpoint.
    for ev in sent:
        assert ev.get("endpoint") is None


def test_jobconfig_load_in_process_never_sees_the_ambient_notifier_url():
    # No NOTIFIER_ENV marker on this test: the autouse fixture in
    # tests/conftest.py already stripped the same ambient export (set up
    # by the module fixture above) from this process's os.environ before
    # this body started running.
    assert os.environ.get(NOTIFIER_ENV_VAR) is None
    cfg = JobConfig.load()
    assert cfg.notifier_url is None
