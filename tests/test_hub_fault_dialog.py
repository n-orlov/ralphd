"""Task 026 (#18.4): the hub's fault dialog -- server side.

A degraded card said the run was sitting out an infra outage and a failed card
said it failed; neither said WHY. Which row of `engine/faults.py`' signature
table fired, how far up the retry ladder the run has climbed and how much of
the outage budget is already spent were on disk all along
(`iterations/NNNN/meta.json`, `events.jsonl`, status.json's degraded contract)
and reachable only by knowing the signature table by heart and doing the budget
arithmetic by hand. `GET /api/runs/<id>/fault` now answers that question, and
`web/app.js` opens it in THE single text dialog from the badge on the card.

What is pinned here:

  * ONE shaping and ONE wording: the payload IS `state.fault_explanation` (task
    025's dict, `summaryLines` and all) and its `text` is `state.fault_text` --
    asserted byte-for-byte against `ralphctl fault <run> --json` and line-for-
    line against what `ralphctl fault <run>` prints, so the hub cannot explain a
    fault differently from the CLI;
  * the four facts #18.4 asked for are in that text: classification, the matched
    signature (family + pattern + the exact substring), the retry-ladder
    position and the outage-budget spend;
  * a run that never faulted is not an error: `hasFault: false` and
    `state.NO_FAULT` as the text, so a badge is never a lie about having
    something to say;
  * unknown is not zero (#15's rule): a fault whose meta.json is unreadable is
    still explained from the run's own retry events;
  * purely on-disk BY DESIGN, like the iteration/document/artifact views: no
    live branch, no `live` key, no snapshot notice -- proven by a live
    StubEngineApi that records ZERO requests while the endpoint answers;
  * app.js words nothing itself and renders text nodes only;
  * the browser side lives in tests/test_browser_hub.py
    (`test_run_detail_opens_the_fault_dialog_from_the_badge`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ralphd.cli.ui_server import fault_view
from ralphd.engine.state import (
    FAULT_SIGNATURE_NONE,
    NO_FAULT,
    fault_explanation,
    fault_text,
)
from tests.conftest import RALPHCTL

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_fault_explanation import (
    _events_file,
    _fault_meta,
    _retry,
    _wait,
    _write_iteration,
)
from test_cli_ui import (
    StubEngineApi,
    UiServer,
    _write_dead_run,
    _write_run_with_api,
    ui,
)

# re-exported so the imported `ui` fixture is not flagged as unused
__all__ = ["StubEngineApi", "UiServer", "ui"]

STATIC_APP_JS = (Path(__file__).resolve().parents[1] / "src" / "ralphd"
                 / "cli" / "web" / "app.js")


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _seed_fault(registry: Path, run_id: str = "hub-fault", *,
                meta=..., events=..., live=None, **status) -> Path:
    """A registry holding one run dir that faulted: a DNS outage the engine
    retried three times and then gave up on. `live` is a StubEngineApi when the
    test wants a reachable container.
    """
    fields = {"state": "failed", "verdict": "unverified", "health": "degraded",
              "infraWait": _wait(), "infraWaitTotalS": 210.0,
              "abortReason": "infra outage budget exhausted", **status}
    if live is None:
        run_dir = _write_dead_run(registry, run_id, **fields)
    else:
        run_dir = _write_run_with_api(registry, run_id, live, **fields)
    if meta is ...:
        meta = _fault_meta(3)
    if meta is not None:
        _write_iteration(run_dir, 3, meta)
    if events is ...:
        events = [_retry(1, 30.0, 30.0), _retry(2, 60.0, 90.0),
                  _retry(3, 120.0, 210.0)]
    if events is not None:
        _events_file(run_dir, events)
    return run_dir


# ------------------------------------------------------------ one shaping


def test_fault_view_is_the_shared_explanation_plus_its_text(tmp_path):
    registry = tmp_path / "registry"
    _seed_fault(registry)

    view = fault_view(registry, "hub-fault")
    exp = fault_explanation(registry / "runs" / "hub-fault")
    assert view == {"runId": "hub-fault", **exp, "text": fault_text(exp)}
    # the shaping already carries the lines; the hub does not re-word them
    assert view["summaryLines"] == exp["summaryLines"]
    assert view["text"] == "\n".join(view["summaryLines"])


def test_fault_view_payload_is_what_ralphctl_fault_json_prints(tmp_path):
    """Byte-for-byte the same document, so `--json` and the dialog's endpoint
    can never drift into two shapes of the same answer."""
    registry = tmp_path / "registry"
    _seed_fault(registry)

    r = _ctl(registry, "--json", "fault", "hub-fault")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == fault_view(registry, "hub-fault")


def test_fault_view_text_is_what_ralphctl_fault_prints(tmp_path):
    """One rendering: the dialog body is the CLI's own block, line for line.
    `cmd_fault` prints its own `run:` line first, then exactly this text."""
    registry = tmp_path / "registry"
    _seed_fault(registry)

    r = _ctl(registry, "fault", "hub-fault")
    assert r.returncode == 0, r.stderr
    printed = r.stdout.split("\n", 1)[1]
    assert printed.rstrip("\n") == fault_view(registry, "hub-fault")["text"]


def test_fault_view_text_carries_the_four_facts_18_4_asked_for(tmp_path):
    registry = tmp_path / "registry"
    _seed_fault(registry)

    view = fault_view(registry, "hub-fault")
    text = view["text"]
    # 1. the classification the engine acted on
    assert "fault:     infra" in text, text
    assert view["faultClass"] == "infra"
    # 2. WHICH signature matched, with the pattern and the matched substring
    assert "signature: dns" in text, text
    assert "EAI_AGAIN" in text, text
    assert view["signature"]["family"] == "dns"
    # 3. where the run is on the retry ladder (its OWN recorded backoffs)
    assert "ladder:    attempt 3" in text, text
    assert view["ladder"]["attempts"] == 3
    # 4. how much of the outage budget is spent
    assert "budget:    " in text, text
    assert view["budget"]["budgetS"] == 14400.0
    assert view["budget"]["remainingS"] == 14310.0


def test_fault_view_says_so_when_a_run_never_faulted(tmp_path):
    """"Nothing went wrong" is an answer (the `NO_ARTIFACTS`/absent-document
    discipline) -- so the badge that opens this dialog never has to lie."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "hub-clean", state="succeeded",
                    verdict="verified")

    view = fault_view(registry, "hub-clean")
    assert view["hasFault"] is False
    assert view["text"] == NO_FAULT
    assert view["signatureDisplay"] == FAULT_SIGNATURE_NONE


def test_fault_view_explains_an_outage_whose_meta_is_unreadable(tmp_path):
    """#15's rule again: a mid-write meta.json is not an absence of fault --
    the episode's own retry events still answer."""
    registry = tmp_path / "registry"
    _seed_fault(registry, meta='{"number": 3, "phase": "wo')

    view = fault_view(registry, "hub-fault")
    assert view["hasFault"] is True and view["faultClass"] == "infra"
    assert "EAI_AGAIN" in view["text"]
    assert view["signature"]["family"] == "dns"


def test_fault_view_of_a_degraded_running_run_reads_the_live_episode(tmp_path):
    """The other badge's case: a run still IN the outage, not one that died of
    it -- there is no abort reason and the wait is pending."""
    registry = tmp_path / "registry"
    _seed_fault(registry, "hub-degraded", state="running", verdict=None,
                abortReason=None)

    view = fault_view(registry, "hub-degraded")
    assert view["waiting"] is True and view["health"] == "degraded"
    assert "gave up" not in view["text"], view["text"]
    assert "next attempt" in view["text"], view["text"]


# ------------------------------------------------------------ HTTP surface


def test_hub_endpoint_serves_the_fault_explanation(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_fault(registry)
    server = ui(registry)

    code, body = server.get("/api/runs/hub-fault/fault")
    assert code == 200
    assert body == fault_view(registry, "hub-fault")
    assert "EAI_AGAIN" in body["text"]


def test_hub_fault_endpoint_404s_for_an_unknown_run(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_fault(registry)
    server = ui(registry)

    code, body = server.get("/api/runs/nope/fault")
    assert code == 404 and "not found" in body["error"]


def test_hub_fault_endpoint_answers_for_a_clean_run_too(tmp_path, ui):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "hub-clean", state="succeeded",
                    verdict="verified")
    server = ui(registry)

    code, body = server.get("/api/runs/hub-clean/fault")
    assert code == 200 and body["text"] == NO_FAULT


def test_hub_fault_endpoint_never_touches_the_live_api(tmp_path, ui):
    """On-disk BY DESIGN (the `iteration_view`/`document_list`/`artifact_list`
    contract): status.json, events.jsonl and the iteration metas are the
    engine's own writes, so a reachable container must not be consulted."""
    engine = StubEngineApi()
    try:
        registry = tmp_path / "registry"
        _seed_fault(registry, live=engine)
        server = ui(registry)

        code, body = server.get("/api/runs/hub-fault/fault")
        assert code == 200
        assert engine.requests == []
        # no live/snapshot vocabulary in the payload at all
        assert "live" not in body
        blob = json.dumps(body)
        assert "snapshot" not in blob and "unreachable" not in blob
        # control: a route that DOES proxy records requests on the same stub
        server.get("/api/runs/hub-fault")
        assert engine.requests != []
    finally:
        engine.close()


def test_two_runs_do_not_borrow_each_others_faults(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_fault(registry, "run-a")
    _seed_fault(registry, "run-b", meta=_fault_meta(
        2, error="socket hang up", faultClass="infra"),
        events=[_retry(1, 30.0, 30.0, error="socket hang up")],
        infraWait=_wait(attempt=1, error="socket hang up"))
    server = ui(registry)

    _, a = server.get("/api/runs/run-a/fault")
    _, b = server.get("/api/runs/run-b/fault")
    assert "EAI_AGAIN" in a["text"] and "socket hang up" not in a["text"]
    assert "socket hang up" in b["text"] and "EAI_AGAIN" not in b["text"]
    assert a["iteration"] == 3 and b["iteration"] == 2


# ------------------------------------------------------------ app.js guards


def test_app_js_opens_the_fault_dialog_from_both_badges():
    src = STATIC_APP_JS.read_text()
    # the endpoint, and THE single shared dialog with the server's own `text`
    assert "/fault`" in src
    assert "openFaultDialog" in src
    assert "openTextDialog(faultTitle(runId), text, null)" in src
    # both ways in: a failed/aborted run's state pill and a degraded card
    assert 'faultBadge(detail.runId, "state", pillEl)' in src
    assert src.count('faultBadge(runId, "infra-wait"') == 2
    assert 'FAULT_BADGE_STATES = new Set(["failed", "aborted"])' in src


def test_app_js_words_no_fault_fact_of_its_own():
    """Every string the dialog shows was formatted in Python (task 025's
    wording): app.js must not respell any of it."""
    from ralphd.engine import state as st

    src = STATIC_APP_JS.read_text()
    for wording in (NO_FAULT, FAULT_SIGNATURE_NONE, st.FAULT_LADDER_NONE,
                    st.FAULT_LADDER_UNCAPPED, st.FAULT_RECOVERED_NOTICE,
                    st.FAULT_REASON_FROM_EVENT,
                    st.FAULT_VERDICT_DIVERGED_NOTICE):
        assert wording not in src, wording
    for label in ("signature:", "ladder:", "budget:"):
        assert label not in src, label


def test_app_js_renders_the_fault_dialog_as_text_nodes_only():
    src = STATIC_APP_JS.read_text()
    region = src[src.index("const FAULT_LOAD_FAILED"):
                 src.index("// --------------------------------------------------- "
                           "steering history (#17)")]
    assert "innerHTML" not in region
    assert "html" not in region.replace("openFaultDialog", "")
