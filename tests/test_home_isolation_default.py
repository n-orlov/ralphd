"""Task 001 (#83 family): `LiveRun`/`LiveRun` isolate a spawned engine's
`HOME` by default, with exactly one greppable opt-in (`AMBIENT_HOME`) for a
call site that genuinely needs the operator's own, real `$HOME`.

Both directions are asserted against the child engine's *actual* observed
environment (via the stub `pi`'s `.stub-env.json` marker -- the same
black-box technique tests/test_engine_child_import_path.py uses), not merely
against the dict this test process happened to build: a fix that isolated the
env dict passed to `subprocess.Popen` but leaked the real HOME back in via
some other path (e.g. a PATH-resolved rcfile) would not be caught by
inspecting `e.proc`'s argv alone.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

# xdist_group (task 003, G.3): an AMBIENT_HOME-family module -- keeps this
# module's tests on one worker under a loaded parallel (`-n 8`) run rather
# than racing another AMBIENT_HOME/nested-pytest module over the real
# ambient $HOME on a different worker.
pytestmark = pytest.mark.xdist_group(name="ambient_home")

from tests.conftest import AMBIENT_HOME, LiveRun

ENV_MARKER = ".stub-env.json"


def _wait_for_env_marker(path: Path, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while True:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            if time.time() >= deadline:
                return {}
            time.sleep(0.2)


def _defaults(run_id: str) -> dict:
    return {"run_id": run_id, "iterations": 1,
            "max_approaches": 1, "on_complete": "idle"}


def test_engine_proc_default_home_is_isolated_from_the_ambient_one(tmp_path):
    e = LiveRun(tmp_path, _defaults("home-default-e2e"))
    try:
        e.wait_api()
        seen = _wait_for_env_marker(e.run_dir / ENV_MARKER)
        assert seen, "the stub never ran an iteration"

        ambient = Path(os.environ["HOME"]).resolve()
        observed = Path(seen["HOME"]).resolve()
        assert observed != ambient
        assert ambient not in (observed, *observed.parents), (
            "the default child HOME must not be the ambient $HOME, nor a "
            "directory inside it -- it must be its own tree under the "
            "test's own tmp_path"
        )
        # ...and it really is inside this test's own tmp tree.
        assert observed.is_relative_to(tmp_path.resolve())
    finally:
        e.stop()


def test_engine_proc_ambient_home_opt_in_gets_the_real_home(tmp_path):
    e = LiveRun(tmp_path, _defaults("home-ambient-e2e"),
                   stub_env={"HOME": AMBIENT_HOME})
    try:
        e.wait_api()
        seen = _wait_for_env_marker(e.run_dir / ENV_MARKER)
        assert seen, "the stub never ran an iteration"
        assert seen["HOME"] == os.environ["HOME"]
    finally:
        e.stop()


def test_live_run_default_home_is_isolated_from_the_ambient_one(live, tmp_path):
    r = live(run_id="home-default-live", job=_defaults("home-default-live"))
    r.wait_api()
    seen = _wait_for_env_marker(r.run_dir / ENV_MARKER)
    assert seen, "the stub never ran an iteration"

    ambient = Path(os.environ["HOME"]).resolve()
    observed = Path(seen["HOME"]).resolve()
    assert observed != ambient
    assert ambient not in (observed, *observed.parents)
    assert observed.is_relative_to(tmp_path.resolve())


def test_live_run_ambient_home_opt_in_gets_the_real_home(live, tmp_path):
    r = live(run_id="home-ambient-live", job=_defaults("home-ambient-live"),
             stub_env={"HOME": AMBIENT_HOME})
    r.wait_api()
    seen = _wait_for_env_marker(r.run_dir / ENV_MARKER)
    assert seen, "the stub never ran an iteration"
    assert seen["HOME"] == os.environ["HOME"]
