"""Task 022 (#18.2): the hub's state-document dialogs -- server side.

A run's own prose -- the worker's `notes.md`, the reviewer's
`review-findings.md`, the `composite-prd.md` an approach restart wrote and the
effective `job.yaml` -- was reachable only by knowing the registry layout and
`cat`-ing files on the host (which is how a credential got read out loud twice
in this project's history). `GET /api/runs/<id>/documents` now lists them and
`GET /api/runs/<id>/documents/<name>` serves one, and `web/app.js` opens it in
THE single text dialog.

What is pinned here:

  * one shaping, one wording: the payload is `engine.state.run_documents` /
    `run_document` (task 021's dicts) and its `text` is
    `state.run_document_text` -- asserted line-for-line against what `ralphctl
    docs <run> <name>` prints, so the hub cannot describe a document
    differently from the CLI, and the size cell is
    `state.format_run_document_size` (`sizeDisplay`), never worded in app.js;
  * the redaction travels with the shaping: a staged secret value and a masked
    key name never appear in the listing, the document payload or the dialog
    text -- so the dialog is as safe to screenshot as `ralphctl docs` output is
    to paste;
  * "which documents exist" is part of the answer: an absent document is a
    listed entry saying so (`RUN_DOCUMENT_ABSENT` via `sizeDisplay`), which
    app.js renders as a non-clickable row rather than dropping;
  * purely on-disk BY DESIGN, like the iteration dialog (task 020): no live
    branch, no `live` key, no snapshot notice -- proven by a live StubEngineApi
    that records ZERO requests while the endpoints answer;
  * bodies are not in the listing (a 4s poll must not ship the whole run's
    prose) and arrive only when a dialog opens;
  * clean 404s (unknown run, unknown document name) instead of a 500 or an
    empty dialog;
  * the browser side lives in tests/test_browser_hub.py
    (`test_run_detail_opens_the_state_document_dialogs`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ralphd.cli.llm_profiles import MASK
from ralphd.cli.ui_server import NO_DOCUMENTS, document_list, document_view
from ralphd.engine.state import (
    JOB_CONFIG_FILE,
    RUN_DOCUMENT_ABSENT,
    RUN_DOCUMENT_EMPTY,
    RUN_DOCUMENT_REDACTED_NOTICE,
    format_run_document_size,
    run_document_keys,
    run_document_summary_lines,
    run_document_text,
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

STATIC_APP_JS = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "cli" / "web" / "app.js"

SECRET = "ghp_hubDialogSecret0123456789"
NOTES = "# Handoff notes\n\n- state: 5/7 done\n- next: task 023\n"
FINDINGS = "# Review findings\n\nApproach 1 missed requirement C.\n"
COMPOSITE = "# Composite PRD\n\nOriginal PRD plus findings.\n"
JOB = {
    "run_id": "hub-docs",
    "iterations": 25,
    "api_token": "tok_abcdefgh12345678",
    "on_complete_cmd": f"curl -H 'Authorization: Bearer {SECRET}' https://ci",
    "env": {"AWS_SECRET_ACCESS_KEY": "AKIAsecretstuff9999", "AWS_REGION": "eu-west-1"},
}


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _job_yaml(job: dict) -> str:
    """The `key: <json>` per line format `ralphctl start` writes."""
    return "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items())


def _seed_docs(registry: Path, run_id: str = "hub-docs", *, notes=NOTES,
               findings=FINDINGS, composite: str | None = None,
               job: dict | None = JOB, creds: bool = True, live=None) -> Path:
    """A registry holding one run dir + its config dir. `live` is a
    StubEngineApi when the test wants a reachable container."""
    if live is None:
        run_dir = _write_dead_run(registry, run_id, state="failed",
                                  verdict="unverified")
    else:
        run_dir = _write_run_with_api(registry, run_id, live, state="running")
    if notes is not None:
        (run_dir / "notes.md").write_text(notes)
    if findings is not None:
        (run_dir / "review-findings.md").write_text(findings)
    if composite is not None:
        (run_dir / "composite-prd.md").write_text(composite)
    if job is not None:
        cdir = registry / "configs" / run_id
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / JOB_CONFIG_FILE).write_text(_job_yaml(job))
        if creds:
            (cdir / "creds").mkdir(exist_ok=True)
            (cdir / "creds" / "github.env").write_text(f"GITHUB_TOKEN={SECRET}\n")
    return run_dir


# ---------------------------------------------------------------- listing


def test_document_list_reports_every_known_document_with_a_server_size_cell(tmp_path):
    registry = tmp_path / "registry"
    _seed_docs(registry)

    payload = document_list(registry, "hub-docs")
    assert payload["runId"] == "hub-docs"
    docs = {d["key"]: d for d in payload["documents"]}
    # every KNOWN document is an entry, present or not
    assert list(docs) == run_document_keys()
    assert docs["notes"]["exists"] is True
    assert docs["notes"]["sizeDisplay"] == f"{len(NOTES.encode()):,}"
    assert docs["findings"]["exists"] is True
    assert docs["job"]["exists"] is True
    # `redacted` is a property of a body that was actually read, and the
    # listing reads none (see `test_document_list_carries_no_bodies`); the
    # dialog's payload is where it turns True.
    assert docs["job"]["redacted"] is False
    assert document_view(registry, "hub-docs", "job")["redacted"] is True
    # never written by this run -> the ONE absence wording, not a dropped row
    assert docs["composite-prd"]["exists"] is False
    assert docs["composite-prd"]["sizeDisplay"] == RUN_DOCUMENT_ABSENT
    # a run that wrote SOMETHING gets no "(no state documents)" notice
    assert payload["notice"] == ""
    # the size cell is the CLI's own formatter, not a second spelling
    for d in payload["documents"]:
        assert d["sizeDisplay"] == format_run_document_size(d)


def test_document_list_carries_no_bodies(tmp_path):
    """A 4s poll must not ship the whole run's prose; bodies come with the
    dialog (`document_view`)."""
    registry = tmp_path / "registry"
    _seed_docs(registry, composite=COMPOSITE)

    payload = document_list(registry, "hub-docs")
    for d in payload["documents"]:
        assert "body" not in d, d
    blob = json.dumps(payload)
    assert "Handoff notes" not in blob and "missed requirement C" not in blob


def test_document_list_says_so_when_a_run_wrote_none_of_them(tmp_path):
    registry = tmp_path / "registry"
    _seed_docs(registry, notes=None, findings=None, job=None)

    payload = document_list(registry, "hub-docs")
    assert payload["notice"] == NO_DOCUMENTS
    assert all(d["exists"] is False for d in payload["documents"])
    # `job.yaml` is *not written*, not "out of reach": the hub knows the
    # registry layout, so it looked in the right place and found nothing.
    job = next(d for d in payload["documents"] if d["key"] == "job")
    assert job["available"] is True
    assert job["sizeDisplay"] == RUN_DOCUMENT_ABSENT


# ---------------------------------------------------------------- one document


def test_document_view_text_is_what_ralphctl_docs_prints(tmp_path):
    """One rendering: the dialog body is the CLI's own text, line for line."""
    registry = tmp_path / "registry"
    _seed_docs(registry, composite=COMPOSITE)

    for key in ("notes", "findings", "composite-prd", "job"):
        view = document_view(registry, "hub-docs", key)
        assert view is not None and view["runId"] == "hub-docs"
        assert view["summaryLines"] == run_document_summary_lines(view)
        assert view["text"] == run_document_text(view)
        r = _ctl(registry, "docs", "hub-docs", key)
        assert r.returncode == 0, r.stderr
        # cmd_docs prints its own `run:` line first, then exactly this text
        printed = r.stdout.split("\n", 1)[1]
        assert printed.rstrip("\n") == view["text"].rstrip("\n"), key


def test_document_view_accepts_the_file_name_as_an_alias(tmp_path):
    registry = tmp_path / "registry"
    _seed_docs(registry)

    by_key = document_view(registry, "hub-docs", "notes")
    by_file = document_view(registry, "hub-docs", "notes.md")
    assert by_file == by_key
    assert document_view(registry, "hub-docs", "NOTES.MD") == by_key


def test_document_view_redacts_job_yaml(tmp_path):
    registry = tmp_path / "registry"
    _seed_docs(registry)

    view = document_view(registry, "hub-docs", "job")
    blob = json.dumps(view)
    # value bound: the staged cred's value, wherever it was smuggled
    assert SECRET not in blob
    # name bound: a secret-looking key, even nested in `env`
    assert "tok_abcdefgh12345678" not in blob
    assert "AKIAsecretstuff9999" not in blob
    assert MASK in view["text"]
    # ...and the harmless values survive, so the dialog is still useful
    assert "eu-west-1" in view["text"]
    assert "iterations: 25" in view["text"]
    assert RUN_DOCUMENT_REDACTED_NOTICE in view["text"]


def test_document_view_of_an_absent_document_is_not_an_error(tmp_path):
    registry = tmp_path / "registry"
    _seed_docs(registry)

    view = document_view(registry, "hub-docs", "composite-prd")
    assert view is not None and view["exists"] is False
    assert RUN_DOCUMENT_ABSENT in view["text"]


def test_document_view_of_a_blank_document_says_empty(tmp_path):
    registry = tmp_path / "registry"
    _seed_docs(registry, notes="")

    view = document_view(registry, "hub-docs", "notes")
    assert view["exists"] is True
    assert RUN_DOCUMENT_EMPTY in view["text"]


def test_document_view_returns_none_for_an_unknown_name(tmp_path):
    registry = tmp_path / "registry"
    _seed_docs(registry)
    assert document_view(registry, "hub-docs", "secrets") is None
    assert document_view(registry, "hub-docs", "../../etc/passwd") is None


def test_document_display_fields_are_always_recomputed(tmp_path):
    """A forged `sizeDisplay` cannot survive a round trip -- same discipline as
    `costDisplay`/`approachDisplay`."""
    from ralphd.cli.ui_server import _with_document_display

    forged = _with_document_display({"key": "notes", "name": "notes.md",
                                     "exists": True, "bytes": 12,
                                     "sizeDisplay": "1,000,000"})
    assert forged["sizeDisplay"] == "12"


# ---------------------------------------------------------------- HTTP surface


def test_hub_endpoints_serve_the_listing_and_one_document(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_docs(registry, composite=COMPOSITE)
    server = ui(registry)

    code, body = server.get("/api/runs/hub-docs/documents")
    assert code == 200
    assert body == document_list(registry, "hub-docs")

    code, body = server.get("/api/runs/hub-docs/documents/notes")
    assert code == 200
    assert body["text"] == document_view(registry, "hub-docs", "notes")["text"]
    assert "Handoff notes" in body["text"]

    # file-name alias through the URL too
    code, body = server.get("/api/runs/hub-docs/documents/review-findings.md")
    assert code == 200 and "missed requirement C" in body["text"]

    # no raw back door: the redacted body is the only body served
    code, body = server.get("/api/runs/hub-docs/documents/job")
    assert code == 200 and SECRET not in json.dumps(body)


def test_hub_document_endpoints_404_cleanly(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_docs(registry)
    server = ui(registry)

    code, body = server.get("/api/runs/nope/documents")
    assert code == 404 and "not found" in body["error"]
    code, body = server.get("/api/runs/nope/documents/notes")
    assert code == 404 and "not found" in body["error"]
    code, body = server.get("/api/runs/hub-docs/documents/secrets")
    assert code == 404 and "unknown document" in body["error"]


def test_hub_document_endpoints_never_touch_the_live_api(tmp_path, ui):
    """On-disk BY DESIGN (the `iteration_view` contract): these files are
    written into the run dir / config dir by the agent, the engine and `start`
    itself, so there is nothing to fall back FROM -- a reachable container must
    not be consulted at all."""
    engine = StubEngineApi()
    try:
        registry = tmp_path / "registry"
        _seed_docs(registry, live=engine)
        server = ui(registry)

        code, listing = server.get("/api/runs/hub-docs/documents")
        assert code == 200
        code, view = server.get("/api/runs/hub-docs/documents/notes")
        assert code == 200
        assert engine.requests == []
        # no live/snapshot vocabulary in the payloads at all
        assert "live" not in listing and "live" not in view
        # control: a route that DOES proxy records requests on the same stub
        server.get("/api/runs/hub-docs")
        assert engine.requests != []
    finally:
        engine.close()


def test_two_runs_do_not_borrow_each_others_documents(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_docs(registry, "run-a", notes="# A notes\n", findings=None,
               job=None)
    _seed_docs(registry, "run-b", notes="# B notes\n", findings=FINDINGS,
               job=None)
    server = ui(registry)

    _, a = server.get("/api/runs/run-a/documents/notes")
    _, b = server.get("/api/runs/run-b/documents/notes")
    assert "# A notes" in a["text"] and "# B notes" not in a["text"]
    assert "# B notes" in b["text"]
    _, a_list = server.get("/api/runs/run-a/documents")
    keys = {d["key"]: d["exists"] for d in a_list["documents"]}
    assert keys["notes"] is True and keys["findings"] is False


# ---------------------------------------------------------------- app.js guards


def test_app_js_renders_documents_as_text_from_server_strings():
    src = STATIC_APP_JS.read_text()
    # the panel exists and is loaded from the endpoint
    assert "documents-box" in src
    assert "/documents" in src
    # ...and opens THE single shared dialog with the server's own `text`
    assert "openDocumentDialog" in src
    assert "openTextDialog(documentTitle(runId, doc), text, null)" in src
    # display strings come from the server, never worded here
    assert "sizeDisplay" in src
    assert RUN_DOCUMENT_ABSENT not in src
    assert NO_DOCUMENTS not in src
    # text nodes only: no markup path for a document body
    assert "innerHTML = \"\"" in src and ".innerHTML =" in src
    body_region = src[src.index("function renderDocuments"):src.index("async function loadDocuments")]
    assert "innerHTML" not in body_region.replace('box.innerHTML = "";', "")
