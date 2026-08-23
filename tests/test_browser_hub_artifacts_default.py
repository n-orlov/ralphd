"""Pins the browser tier's screenshot-directory default rule (#57 part 1).

Lives outside `test_browser_hub.py` on purpose: that module's `pytestmark`
applies `@pytest.mark.browser` (and its `skipif` when `playwright-cli` is
absent) to every test collected from it, but the default-resolution rule
itself is a pure function with no dependency on a real browser and should
run in the fast tier so a developer host without `playwright-cli` still
checks it.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import _default_artifacts_dir


def test_default_falls_back_to_a_user_writable_tempdir_when_unset(monkeypatch):
    monkeypatch.delenv("RALPHD_ARTIFACTS_DIR", raising=False)

    result = _default_artifacts_dir()

    assert result == Path(tempfile.gettempdir()) / "ralphd-artifacts"
    # The defect this fixes: the old default was /run/ralphd/artifacts,
    # which `mkdir(parents=True)` cannot create for an ordinary user on a
    # developer host. Assert the fallback never names /run/ralphd at all.
    assert "/run/ralphd" not in str(result)
    # And it must actually be creatable by whoever is running the tests.
    result.mkdir(parents=True, exist_ok=True)
    assert result.is_dir()


def test_default_honours_the_env_var_when_set_including_run_ralphd(monkeypatch):
    monkeypatch.setenv("RALPHD_ARTIFACTS_DIR", "/run/ralphd/artifacts")

    result = _default_artifacts_dir()

    # Today's in-container behaviour is preserved when a caller opts in
    # explicitly -- e.g. the job's own engine, which sets this env var.
    assert result == Path("/run/ralphd/artifacts")


def test_default_honours_an_arbitrary_env_var_path(monkeypatch, tmp_path):
    custom = tmp_path / "custom-artifacts"
    monkeypatch.setenv("RALPHD_ARTIFACTS_DIR", str(custom))

    result = _default_artifacts_dir()

    assert result == custom
