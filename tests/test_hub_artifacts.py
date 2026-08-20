"""Task 024 (#18.3): the hub's artifacts panel and dialog -- server side.

What a job leaves in `artifacts/` -- above all the reflect phase's post-mortem
(`reflection/report.md`) and the prompt/skill diff it proposes
(`reflection/suggestions.diff`) -- was reachable only by knowing the registry
layout and `cat`-ing files on the host, so the whole output of the reflect phase
was invisible from the hub. `GET /api/runs/<id>/artifacts` now lists the tree and
`GET /api/runs/<id>/artifacts/<path>` serves one file, which `web/app.js` opens
in THE single text dialog.

What is pinned here:

  * one shaping, one wording: the payload is `engine.state.artifact_entries` /
    `artifact` (task 023's dicts) and its `text` is `state.artifact_text` --
    asserted line-for-line against what `ralphctl artifacts <run> show <name>`
    prints, so the hub cannot describe an artifact differently from the CLI, and
    the size cell is `state.format_artifact_size` (`sizeDisplay`), never worded
    in app.js;
  * every spelling a listing shows works as a URL: the well-known key
    (`report`), the path (`reflection/report.md`), the same path with its
    directory, percent-encoded as one segment or spread over several;
  * the traversal guard is the shaping's (`state.artifact_relpath`), which is
    what makes putting an operator-supplied path in a URL safe at all: `..`,
    an absolute path and a NUL get a clean 404, never a file from elsewhere on
    the host;
  * a run that produced nothing gets `state.NO_ARTIFACTS` -- the CLI's own
    line -- not an empty panel;
  * bodies are not in the listing (a 4s poll must not ship a whole reflection
    report) and arrive only when a dialog opens;
  * a binary artifact answers with `state.ARTIFACT_BINARY` instead of spraying
    bytes into a browser;
  * purely on-disk BY DESIGN, like the iteration/document dialogs: no live
    branch, no `live` key, no snapshot notice -- proven by a live StubEngineApi
    that records ZERO requests while the endpoints answer;
  * the browser side lives in tests/test_browser_hub.py
    (`test_run_detail_browses_artifacts_and_opens_the_reflect_report`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ralphd.cli.ui_server import (
    _with_artifact_display,
    artifact_list,
    artifact_view,
)
from ralphd.engine.state import (
    ARTIFACT_BINARY,
    NO_ARTIFACTS,
    RUN_DOCUMENT_ABSENT,
    RUN_DOCUMENT_EMPTY,
    artifact_summary_lines,
    artifact_text,
    format_artifact_size,
)
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

STATIC_APP_JS = (Path(__file__).resolve().parents[1] / "src" / "ralphd"
                 / "cli" / "web" / "app.js")

REPORT = ("# Reflection report\n\nApproach 1 failed on requirement C: the "
          "worker never ran the browser tier.\n")
SUGGESTIONS = ("--- a/prompts/worker.md\n+++ b/prompts/worker.md\n"
               "@@ -1,3 +1,4 @@\n Worker prompt\n+Run <every> tier.\n")
OTHER = "# Pricing anomaly\n\nThe gateway quoted 0 for 505,628 tokens.\n"


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _seed_artifacts(registry: Path, run_id: str = "hub-arts", *,
                    report=REPORT, suggestions=SUGGESTIONS, other=OTHER,
                    binary: bytes | None = None, live=None) -> Path:
    """A registry holding one run dir with an `artifacts/` tree. `live` is a
    StubEngineApi when the test wants a reachable container."""
    if live is None:
        run_dir = _write_dead_run(registry, run_id, state="failed",
                                  verdict="unverified")
    else:
        run_dir = _write_run_with_api(registry, run_id, live, state="running")
    arts = run_dir / "artifacts"
    if report is not None:
        (arts / "reflection").mkdir(parents=True, exist_ok=True)
        (arts / "reflection" / "report.md").write_text(report)
    if suggestions is not None:
        (arts / "reflection").mkdir(parents=True, exist_ok=True)
        (arts / "reflection" / "suggestions.diff").write_text(suggestions)
    if other is not None:
        (arts / "reports").mkdir(parents=True, exist_ok=True)
        (arts / "reports" / "pricing-anomaly.md").write_text(other)
    if binary is not None:
        arts.mkdir(parents=True, exist_ok=True)
        (arts / "screenshot.png").write_bytes(binary)
    return run_dir


# ---------------------------------------------------------------- listing


def test_artifact_list_reports_the_tree_with_keys_and_a_server_size_cell(tmp_path):
    registry = tmp_path / "registry"
    _seed_artifacts(registry)

    payload = artifact_list(registry, "hub-arts")
    assert payload["runId"] == "hub-arts"
    by_path = {a["path"]: a for a in payload["artifacts"]}
    assert set(by_path) == {"reflection/report.md",
                            "reflection/suggestions.diff",
                            "reports/pricing-anomaly.md"}
    assert by_path["reflection/report.md"]["key"] == "report"
    assert by_path["reflection/report.md"]["title"]
    assert by_path["reflection/suggestions.diff"]["key"] == "suggestions"
    # an artifact with no well-known name is still listed, keyless
    assert by_path["reports/pricing-anomaly.md"]["key"] is None
    assert by_path["reflection/report.md"]["sizeDisplay"] == \
        f"{len(REPORT.encode()):,}"
    # the size cell is the CLI's own formatter, not a second spelling
    for a in payload["artifacts"]:
        assert a["sizeDisplay"] == format_artifact_size(a)
    assert payload["notice"] == ""


def test_artifact_list_carries_no_bodies(tmp_path):
    """A 4s poll must not ship a whole reflection report; the body comes with
    the dialog (`artifact_view`)."""
    registry = tmp_path / "registry"
    _seed_artifacts(registry)

    payload = artifact_list(registry, "hub-arts")
    for a in payload["artifacts"]:
        assert "body" not in a, a
    blob = json.dumps(payload)
    assert "requirement C" not in blob and "worker.md" not in blob


def test_artifact_list_says_so_when_a_run_left_nothing(tmp_path):
    registry = tmp_path / "registry"
    _seed_artifacts(registry, report=None, suggestions=None, other=None)

    payload = artifact_list(registry, "hub-arts")
    assert payload["artifacts"] == []
    # the CLI's own line, not a second wording for the same fact
    assert payload["notice"] == NO_ARTIFACTS


def test_artifact_list_is_what_ralphctl_artifacts_ls_reports(tmp_path):
    """One shaping: the hub's rows ARE the CLI's rows (plus `sizeDisplay`)."""
    registry = tmp_path / "registry"
    _seed_artifacts(registry)

    r = _ctl(registry, "--json", "artifacts", "hub-arts", "ls")
    assert r.returncode == 0, r.stderr
    cli = json.loads(r.stdout)["artifacts"]
    hub = artifact_list(registry, "hub-arts")["artifacts"]
    assert [_with_artifact_display(e) for e in cli] == hub


# ---------------------------------------------------------------- one artifact


def test_artifact_view_text_is_what_ralphctl_artifacts_show_prints(tmp_path):
    """One rendering: the dialog body is the CLI's own text, line for line."""
    registry = tmp_path / "registry"
    _seed_artifacts(registry)

    for name in ("report", "suggestions", "reports/pricing-anomaly.md"):
        view = artifact_view(registry, "hub-arts", name)
        assert view is not None and view["runId"] == "hub-arts"
        assert view["summaryLines"] == artifact_summary_lines(view)
        assert view["text"] == artifact_text(view)
        r = _ctl(registry, "artifacts", "hub-arts", "show", name)
        assert r.returncode == 0, r.stderr
        # cmd_artifacts prints its own `run:` line first, then exactly this text
        printed = r.stdout.split("\n", 1)[1]
        assert printed.rstrip("\n") == view["text"].rstrip("\n"), name


def test_artifact_view_accepts_every_spelling_a_listing_shows(tmp_path):
    registry = tmp_path / "registry"
    _seed_artifacts(registry)

    by_key = artifact_view(registry, "hub-arts", "report")
    assert by_key == artifact_view(registry, "hub-arts", "reflection/report.md")
    assert by_key == artifact_view(registry, "hub-arts",
                                   "artifacts/reflection/report.md")
    assert by_key == artifact_view(registry, "hub-arts", "REPORT")
    assert "requirement C" in by_key["text"]


def test_artifact_view_of_an_absent_artifact_is_not_an_error(tmp_path):
    """A listing can be a poll cycle behind the disk -- the answer is the
    absence wording, not a 404 and not an empty dialog."""
    registry = tmp_path / "registry"
    _seed_artifacts(registry, report=None)

    view = artifact_view(registry, "hub-arts", "report")
    assert view is not None and view["exists"] is False
    assert RUN_DOCUMENT_ABSENT in view["text"]


def test_artifact_view_of_a_blank_artifact_says_empty(tmp_path):
    registry = tmp_path / "registry"
    _seed_artifacts(registry, report="")

    view = artifact_view(registry, "hub-arts", "report")
    assert view["exists"] is True
    assert RUN_DOCUMENT_EMPTY in view["text"]


def test_artifact_view_of_a_binary_artifact_points_at_pull(tmp_path):
    registry = tmp_path / "registry"
    _seed_artifacts(registry, binary=b"\x89PNG\r\n\x1a\n\x00\x00binary")

    view = artifact_view(registry, "hub-arts", "screenshot.png")
    assert view["isText"] is False
    assert ARTIFACT_BINARY in view["text"]
    assert "binary" not in view["text"].split(ARTIFACT_BINARY)[0]


def test_artifact_view_refuses_anything_that_is_not_an_artifact(tmp_path):
    registry = tmp_path / "registry"
    _seed_artifacts(registry)

    for bad in ("", "..", "../../etc/passwd", "/etc/passwd",
                "reflection/../../../etc/passwd", "a\x00b"):
        assert artifact_view(registry, "hub-arts", bad) is None, bad


def test_artifact_display_fields_are_always_recomputed(tmp_path):
    """A forged `sizeDisplay` cannot survive a round trip -- same discipline as
    `costDisplay`/`approachDisplay`/the document panel."""
    forged = _with_artifact_display({"path": "reflection/report.md",
                                     "exists": True, "bytes": 12,
                                     "sizeDisplay": "1,000,000"})
    assert forged["sizeDisplay"] == "12"


# ---------------------------------------------------------------- HTTP surface


def test_hub_endpoints_serve_the_listing_and_one_artifact(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_artifacts(registry)
    server = ui(registry)

    code, body = server.get("/api/runs/hub-arts/artifacts")
    assert code == 200
    assert body == artifact_list(registry, "hub-arts")

    code, body = server.get("/api/runs/hub-arts/artifacts/report")
    assert code == 200
    assert body["text"] == artifact_view(registry, "hub-arts", "report")["text"]
    assert "requirement C" in body["text"]


def test_hub_artifact_url_accepts_a_path_encoded_or_split(tmp_path, ui):
    """app.js sends one percent-encoded segment; a human (or a copied link)
    sends the path with real slashes. Both must reach the same file."""
    registry = tmp_path / "registry"
    _seed_artifacts(registry)
    server = ui(registry)

    code, encoded = server.get(
        "/api/runs/hub-arts/artifacts/reflection%2Fsuggestions.diff")
    assert code == 200, encoded
    code, split = server.get(
        "/api/runs/hub-arts/artifacts/reflection/suggestions.diff")
    assert code == 200
    assert encoded == split
    assert "worker.md" in split["text"]
    # ...and the `artifacts/`-prefixed spelling a listing/report quotes
    code, prefixed = server.get(
        "/api/runs/hub-arts/artifacts/artifacts/reflection/suggestions.diff")
    assert code == 200 and prefixed == split


def test_hub_artifact_endpoints_404_cleanly(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_artifacts(registry)
    server = ui(registry)

    code, body = server.get("/api/runs/nope/artifacts")
    assert code == 404 and "not found" in body["error"]
    code, body = server.get("/api/runs/nope/artifacts/report")
    assert code == 404 and "not found" in body["error"]
    code, body = server.get("/api/runs/hub-arts/artifacts/..")
    assert code == 404 and "not an artifact name" in body["error"]


def test_hub_artifact_url_cannot_escape_the_artifacts_dir(tmp_path, ui):
    """The traversal guard is the shaping's, and this is why it exists: the
    name arrives from a URL."""
    registry = tmp_path / "registry"
    run_dir = _seed_artifacts(registry)
    (run_dir / "notes.md").write_text("SECRET-NOTES-BODY\n")
    server = ui(registry)

    for bad in ("..%2F..%2Fnotes.md", "../notes.md", "reflection/../../notes.md",
                "%2Fetc%2Fpasswd"):
        code, body = server.get(f"/api/runs/hub-arts/artifacts/{bad}")
        assert code == 404, (bad, code, body)
        assert "SECRET-NOTES-BODY" not in json.dumps(body), bad


def test_hub_artifact_endpoints_never_touch_the_live_api(tmp_path, ui):
    """On-disk BY DESIGN (the `iteration_view`/`document_list` contract): the
    agent writes these files into a directory this host holds, so a reachable
    container must not be consulted at all."""
    engine = StubEngineApi()
    try:
        registry = tmp_path / "registry"
        _seed_artifacts(registry, live=engine)
        server = ui(registry)

        code, listing = server.get("/api/runs/hub-arts/artifacts")
        assert code == 200
        code, view = server.get("/api/runs/hub-arts/artifacts/report")
        assert code == 200
        assert engine.requests == []
        # no live/snapshot vocabulary in the payloads at all
        assert "live" not in listing and "live" not in view
        # control: a route that DOES proxy records requests on the same stub
        server.get("/api/runs/hub-arts")
        assert engine.requests != []
    finally:
        engine.close()


def test_two_runs_do_not_borrow_each_others_artifacts(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_artifacts(registry, "run-a", report="# A report\n", suggestions=None,
                    other=None)
    _seed_artifacts(registry, "run-b", report="# B report\n", suggestions=None,
                    other=None)
    server = ui(registry)

    _, a = server.get("/api/runs/run-a/artifacts/report")
    _, b = server.get("/api/runs/run-b/artifacts/report")
    assert "# A report" in a["text"] and "# B report" not in a["text"]
    assert "# B report" in b["text"]


# ---------------------------------------------------------------- app.js guards


def test_app_js_renders_artifacts_as_text_from_server_strings():
    src = STATIC_APP_JS.read_text()
    # the panel exists and is loaded from the endpoint
    assert "artifacts-box" in src
    assert "/artifacts" in src
    # ...and opens THE single shared dialog with the server's own `text`
    assert "openArtifactDialog" in src
    assert "openTextDialog(artifactTitle(runId, a), text, null)" in src
    # display strings come from the server, never worded here
    assert "sizeDisplay" in src
    assert NO_ARTIFACTS not in src
    assert ARTIFACT_BINARY not in src
    assert RUN_DOCUMENT_ABSENT not in src
    # text nodes only: no markup path for an artifact body
    region = src[src.index("function renderArtifacts"):
                 src.index("async function loadArtifacts")]
    assert "innerHTML" not in region.replace('box.innerHTML = "";', "")
