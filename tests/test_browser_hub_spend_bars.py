"""v0.39 (#321 E): browser-tier coverage for the run detail's spend bars --
one `.spend-bar` per cap, none for a run with no caps. Same harness as
`tests/test_browser_hub.py` (playwright-cli, real Chromium; skips cleanly
when the binary is absent)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from test_browser_hub import (  # noqa: F401
    PLAYWRIGHT_CLI,
    SCREENSHOTS_DIR,
    _wait_for,
    pw,
)
from test_cli_ui import UiServer, _write_dead_run

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(PLAYWRIGHT_CLI is None,
                                 reason="playwright-cli not on PATH")]

CAPS = {"overall": {"review": {"usd": 150.0, "source": "flag"}},
        "per_approach": {"review": {"usd": 50.0, "source": "flag"}}}
SPEND = {"overall": {"review": 38.1, "total": 38.1},
         "byApproach": {"1": {"review": 12.4, "total": 12.4}}}


def _write(registry: Path, run_id: str, caps) -> None:
    _write_dead_run(registry, run_id, state="exhausted", spend=SPEND)
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    job = {"run_id": run_id, "harnesses": {"review": "claude-code"}}
    if caps:
        job["spend_caps"] = caps
    (cdir / "job.yaml").write_text(yaml.safe_dump(job))


def test_one_bar_per_cap_for_a_capped_run_and_none_without_caps(tmp_path, pw):
    registry = tmp_path / "registry"
    _write(registry, "run-capped", CAPS)
    _write(registry, "run-plain", None)
    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-capped")
        _wait_for(pw, "document.body.innerText", "exhausted")
        assert int(pw.eval_js("document.querySelectorAll('.spend-bar').length")) == 2
        levels = pw.eval_js(
            "Array.from(document.querySelectorAll('.spend-bar'))"
            ".map(b => b.dataset.level).join(',')")
        assert levels.strip('"') == "overall,per_approach"
        text = pw.eval_js("document.querySelector('.spend-bars').innerText")
        assert "$38.10 / $150 overall" in text
        assert "$12.40 / $50 this approach" in text
        assert "notional" in text
        pw.screenshot(SCREENSHOTS_DIR / "spend-bars.png")

        pw.open(f"{server.base}/#/run/run-plain")
        _wait_for(pw, "document.body.innerText", "run-plain")
        _wait_for(pw, "document.body.innerText", "exhausted")
        assert int(pw.eval_js("document.querySelectorAll('.spend-bar').length")) == 0
    finally:
        server.stop()
