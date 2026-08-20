"""Task 020 (#18.1): the hub's iteration-detail dialog -- server side.

A timeline row used to be a dead summary (phase, model, duration): "why did
iteration 47 end like that, and what did the agent actually do in it" meant
leaving the hub for `ralphctl iteration` or the run dir. `GET
/api/runs/<id>/iterations/<n>` now serves that iteration's whole story and
`web/app.js` puts it in THE single text dialog.

What is pinned here:

  * one shaping, one wording: the payload is `engine.state.iteration_detail`
    (task 019's dict) and its `text` is `state.iteration_summary_lines` +
    `state.format_iteration_log_header` + the shared `log_render` output --
    asserted line-for-line against what `ralphctl iteration` prints, so the hub
    cannot word an exit reason, duration or token count differently;
  * purely on-disk BY DESIGN: unlike the log tail / PRD / steering endpoints
    there is no live branch, because the engine writes `meta.json` and the
    transcript into the run dir itself -- proven by a live StubEngineApi that
    records ZERO requests while the endpoint answers;
  * unknown is not zero: an iteration with no readable meta.json says so, and
    one with no transcript gets `log_merge.NO_TRANSCRIPT` rather than an empty
    log that looks like a broken page;
  * clean 404s (unknown run, unknown iteration, junk iteration number) instead
    of a 500 or an empty dialog;
  * `log=0` omits the transcript (key ABSENT, never an empty list -- `ralphctl
    iteration --no-log`'s own rule);
  * the browser side lives in tests/test_browser_hub.py
    (`test_timeline_row_opens_the_iteration_dialog`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ralphd.cli.ui_server import iteration_view
from ralphd.engine.state import (
    EXIT_REASON_CLEAN,
    EXIT_REASON_RUNNING,
    ITERATION_NO_META_NOTICE,
    USAGE_NONE,
    format_iteration_log_header,
    iteration_detail,
    iteration_summary_lines,
)
from ralphd.log_merge import NO_TRANSCRIPT
from tests.conftest import RALPHCTL

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import (
    StubEngineApi,
    UiServer,
    _write_dead_run,
    _write_run_with_api,
    ui,
)

# re-exported so the imported `ui` fixture is not flagged as unused
__all__ = ["StubEngineApi", "UiServer", "ui"]

STATIC_APP_JS = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "cli" / "web" / "app.js"


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _meta(n: int = 2, **over) -> dict:
    meta = {"number": n, "phase": "worker", "model": None, "approach": 2,
            "startedAt": "2026-09-03T10:00:00Z", "endedAt": "2026-09-03T10:17:51Z",
            "steeringConsumed": [], "exitCode": 0, "interrupted": False,
            "timedOut": False, "noTrafficTimeout": False, "error": None,
            "faultClass": None,
            "modelResolved": "amazon-bedrock/eu.anthropic.claude-opus-5",
            "modelRaw": "eu.anthropic.claude-opus-5",
            "usage": {"input": 18, "output": 2118, "cacheRead": 136849,
                      "cacheWrite": 41676, "totalTokens": 180661,
                      "costUSD": 0.4231, "costPriced": True}}
    meta.update(over)
    return meta


def _write_iteration(run_dir: Path, n: int, meta: dict | None,
                     texts: list[str] | None = None) -> Path:
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True, exist_ok=True)
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta))
    if texts is not None:
        (d / "output.jsonl").write_text("".join(
            json.dumps({"type": "message_end",
                        "message": {"content": [{"type": "text", "text": t}]}}) + "\n"
            for t in texts))
    return d


def _run(tmp_path: Path, run_id: str = "run-iter") -> tuple[Path, Path]:
    """(registry, run_dir) for a finished run with no container at all."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, run_id, state="failed", verdict="unverified")
    return registry, run_dir


# --------------------------------------------------------------- shaping tier
def test_view_is_the_shared_detail_plus_the_shared_wording(tmp_path):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 2, _meta(), texts=["hello from the agent"])

    view = iteration_view(registry, "run-iter", 2)
    detail = iteration_detail(run_dir, 2)

    # every key of task 019's shaping survives, verbatim
    for key, value in detail.items():
        assert view[key] == value, key
    assert view["runId"] == "run-iter"
    # ...and the rendered block is the ONE shared wording, not a hub copy
    assert view["summaryLines"] == iteration_summary_lines(detail)
    assert view["text"].split("\n")[:len(view["summaryLines"])] == view["summaryLines"]
    assert format_iteration_log_header(len(view["log"])) in view["text"].split("\n")
    assert any("hello from the agent" in line for line in view["log"])
    assert view["text"].endswith(view["log"][-1])


def test_summary_lines_say_what_meta_json_says(tmp_path):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 2, _meta(), texts=["x"])
    text = iteration_view(registry, "run-iter", 2)["text"]
    assert "iteration: 2  phase worker  approach 2" in text
    assert f"exit:      {EXIT_REASON_CLEAN}" in text
    assert "duration:  17m 51s  (total)" in text
    assert "tokens:    180,661 total (in 18, out 2,118," in text
    assert "cost:      $0.4231" in text
    assert "model:     amazon-bedrock/eu.anthropic.claude-opus-5" in text
    assert "(gateway id: eu.anthropic.claude-opus-5)" in text


def test_missing_iteration_is_none_not_an_empty_view(tmp_path):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 1, _meta(1), texts=["x"])
    assert iteration_view(registry, "run-iter", 9) is None
    assert iteration_view(registry, "no-such-run", 1) is None


def test_log_false_omits_the_key_entirely(tmp_path):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 1, _meta(1), texts=["x"])
    view = iteration_view(registry, "run-iter", 1, log=False)
    # absent, not empty: an empty list would claim the iteration wrote nothing
    assert "log" not in view
    assert format_iteration_log_header(0) not in view["text"]
    assert view["text"] == "\n".join(view["summaryLines"])


def test_no_transcript_says_so_instead_of_an_empty_log(tmp_path):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 1, _meta(1))          # no output.jsonl at all
    view = iteration_view(registry, "run-iter", 1)
    assert view["log"] == [NO_TRANSCRIPT]
    assert view["hasTranscript"] is False
    assert NO_TRANSCRIPT in view["text"]


def test_unreadable_meta_is_ignorance_not_zeroes(tmp_path):
    registry, run_dir = _run(tmp_path)
    d = _write_iteration(run_dir, 3, None, texts=["the transcript survived"])
    (d / "meta.json").write_text('{"number": 3, "phase": "wor')   # truncated

    view = iteration_view(registry, "run-iter", 3)
    assert view["hasMeta"] is False
    assert ITERATION_NO_META_NOTICE in view["text"]
    assert view["tokensDisplay"] == USAGE_NONE
    assert view["costDisplay"] == USAGE_NONE
    assert "$0.0000" not in view["text"]
    assert any("the transcript survived" in line for line in view["log"])


def test_a_running_iteration_is_elapsed_not_total(tmp_path):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 4, _meta(4, endedAt=None, exitCode=None), texts=["…"])
    text = iteration_view(registry, "run-iter", 4)["text"]
    assert f"exit:      {EXIT_REASON_RUNNING}" in text
    assert "(elapsed)" in text
    assert "ended:" not in text


def test_forged_display_keys_in_meta_json_are_recomputed(tmp_path):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 1, _meta(1, exitReason="everything is fine",
                                       costDisplay="$0.00", tokensDisplay="0"),
                     texts=["x"])
    view = iteration_view(registry, "run-iter", 1)
    assert view["exitReason"] == EXIT_REASON_CLEAN
    assert "everything is fine" not in view["text"]
    assert view["costDisplay"] == "$0.4231"


# ------------------------------------------------------- CLI/hub agreement
def test_hub_text_is_exactly_what_ralphctl_iteration_prints(tmp_path):
    """The dialog body and the CLI's output are the same lines: the CLI adds
    only its own `run:` line (the id the operator typed)."""
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 2, _meta(steeringConsumed=["001-cost.md"],
                                       verifiedTask="019", verifyOutcome="passed"),
                     texts=["a first message", "and a second"])

    res = _ctl(registry, "iteration", "run-iter", "2")
    assert res.returncode == 0, res.stderr
    printed = res.stdout.rstrip("\n").split("\n")
    assert printed[0] == "run:       run-iter"

    view = iteration_view(registry, "run-iter", 2)
    assert printed[1:] == view["text"].split("\n")
    assert "steering:  001-cost.md" in view["text"]
    assert "verified:  task 019 -> passed" in view["text"]


# ------------------------------------------------------------ black-box tier
def test_endpoint_serves_the_view_for_a_run_with_no_container(tmp_path, ui):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 1, _meta(1), texts=["first"])
    _write_iteration(run_dir, 2, _meta(2), texts=["second"])

    server = ui(registry)
    code, body = server.get("/api/runs/run-iter/iterations/2")
    assert code == 200
    assert body["number"] == 2
    assert body["exitReason"] == EXIT_REASON_CLEAN
    assert "iteration: 2" in body["text"]
    assert any("second" in line for line in body["log"])
    assert not any("first" in line for line in body["log"])
    # no live/notice vocabulary: there is nothing to fall back FROM
    assert "live" not in body


def test_endpoint_log_query_can_skip_the_transcript(tmp_path, ui):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 1, _meta(1), texts=["a chatty iteration"])
    server = ui(registry)
    code, body = server.get("/api/runs/run-iter/iterations/1?log=0")
    assert code == 200
    assert "log" not in body
    assert "a chatty iteration" not in body["text"]


@pytest.mark.parametrize("path,fragment", [
    ("/api/runs/nope/iterations/1", "not found"),
    ("/api/runs/run-iter/iterations/47", "no iteration 47"),
    ("/api/runs/run-iter/iterations/zzz", "bad iteration number"),
])
def test_endpoint_404s_cleanly(tmp_path, ui, path, fragment):
    registry, run_dir = _run(tmp_path)
    _write_iteration(run_dir, 1, _meta(1), texts=["x"])
    server = ui(registry)
    code, body = server.get(path)
    assert code == 404
    assert fragment in body["error"]


def test_endpoint_never_touches_the_live_api(tmp_path, ui):
    """Purely on-disk by design: `meta.json` and the transcript are the
    engine's OWN atomic writes into the run dir, so a live container has
    nothing better to say -- asserted, not documented."""
    engine = StubEngineApi(status={"state": "running", "phase": "worker"})
    try:
        registry = tmp_path / "registry"
        run_dir = _write_run_with_api(registry, "run-live", engine, state="running")
        _write_iteration(run_dir, 1, _meta(1), texts=["on-disk only"])
        server = ui(registry)
        code, body = server.get("/api/runs/run-live/iterations/1")
        assert code == 200
        assert any("on-disk only" in line for line in body["log"])
        assert engine.requests == []
        # ...while the detail view, the control, does proxy
        assert server.get("/api/runs/run-live")[0] == 200
        assert engine.requests != []
    finally:
        engine.close()


# ------------------------------------------------------------- app.js guards
def test_app_js_uses_the_endpoint_and_the_shared_dialog():
    src = STATIC_APP_JS.read_text()
    assert "/iterations/" in src
    assert "openIterationDialog" in src
    # the dialog body is the server's `text`; no client-side re-wording of the
    # exit reason / duration / token vocabulary
    assert "body.text" in src
    for word in ("clean exit", "still running", "total (in", "--- log ("):
        assert word not in src, word
