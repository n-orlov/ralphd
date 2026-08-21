"""Task 043 (#22): the semantic doc pass, with a check per correction.

`test_docs_consistency.py` (task 042) is the *mechanical* layer: it catches a
documented flag, subcommand, route or field **name** that does not exist. This
module is the layer that catches a documented name whose *meaning* is wrong --
the class of rot the semantic pass found by hand:

* `GET /version` was documented as returning a `"pi"` key it has never
  returned, and as being version-checked by `ralphctl` on connect (nothing in
  the CLI has ever called the route);
* the error body was documented as RFC 7807 `problem+json` with a `type`
  member; the engine sends FastAPI's `{"detail": {...}}` envelope around
  `{title, status, detail}` as plain `application/json`;
* `POST /pause` was documented as reporting `phase: paused` in `/status`; the
  pause changes no field at all -- it emits a `log` event and holds the loop;
* `GET /status`'s documented shape omitted six fields the engine always
  writes (`schemaVersion`, `createdAt`/`updatedAt`/`endedAt`, `reason`,
  `unconsumedSteering`, `graceReview`) and `GET /config`'s omitted two flags;
* every install line named the *console script* (`pipx install ralphctl`) as
  the distribution to install, which is `ralphd`.

Each check is written against the code, not against the corrected prose, so it
keeps failing if the behaviour moves again.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from ralphd import API_VERSION, __version__
from ralphd.engine.api import create_app, problem
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir

REPO_ROOT = Path(__file__).resolve().parents[1]
API_DOC = REPO_ROOT / "docs" / "api.md"
ENGINE_DIR = REPO_ROOT / "src" / "ralphd" / "engine"
DOC_FILES = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md")),
             REPO_ROOT / "SPEC.md"]


@pytest.fixture
def client(tmp_path):
    """The real engine app over an empty run dir, as an ASGI client factory."""
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
    sup = LoopSupervisor(JobConfig(run_id="docs"), run, tmp_path)
    app = create_app(sup.cfg, run, sup)

    def open_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://engine")

    return open_client


def _request(client, method: str, path: str):
    async def go():
        async with client() as c:
            return await c.request(method, path)
    return asyncio.run(go())


def _section(text: str, heading: str) -> str:
    """One `### `-level section of a doc, heading line included."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#") and not lines[i].startswith("####"):
            end = i
            break
    return "\n".join(lines[start:end])


# ---- GET /version: the documented response *is* the response ---------------

VERSION_SHAPE_RE = re.compile(r"`GET /version`[^`]*`(\{[^`]*\})`")


def _documented_version_keys(text: str) -> set[str]:
    shape = VERSION_SHAPE_RE.search(re.sub(r"\s+", " ", text))
    assert shape, "docs/api.md no longer shows GET /version's response shape"
    return set(re.findall(r'"([A-Za-z][A-Za-z0-9_]*)"\s*:', shape.group(1)))


def test_documented_version_response_keys_are_exactly_the_served_ones(client):
    served = _request(client, "GET", "/version")
    assert served.status_code == 200, served.text
    body = served.json()
    documented = _documented_version_keys(API_DOC.read_text())
    assert documented == set(body), (
        "docs/api.md documents a GET /version response the engine does not "
        f"send: documented {sorted(documented)}, served {sorted(body)}")
    assert body == {"ralphd": __version__, "api": API_VERSION}


def test_the_version_shape_check_would_catch_an_invented_key():
    doc = ('`GET /version` \u2192 `{"ralphd": "0.6.0", "api": 1, '
           '"pi": "<pi version>"}`. Breaking changes bump `api`.')
    assert _documented_version_keys(doc) == {"ralphd", "api", "pi"}


def test_no_doc_claims_ralphctl_version_checks_the_api_on_connect():
    """Nothing in the CLI calls `GET /version` or compares API_VERSION -- so no
    doc may promise it does. Delete this check the day the CLI grows the
    handshake (and assert the handshake instead)."""
    cli = (REPO_ROOT / "src" / "ralphd" / "cli" / "main.py").read_text()
    handshake = "/version" in cli or "API_VERSION" in cli
    claims = []
    for path in DOC_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            low = line.lower()
            if "compatib" not in low and "version check" not in low:
                continue
            if "ralphctl" in low and "connect" in low:
                claims.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert bool(claims) == handshake, (
        f"docs promise a ralphctl/engine version handshake at {claims}, but "
        f"src/ralphd/cli/main.py has none")


# ---- the error body -------------------------------------------------------

BRACE_SPAN_RE = re.compile(r"`(\{[^`]*\})`")


def _claimed_error_members(paragraph: str) -> set[str]:
    """Field names the prose presents *inside a shape* (`{...}`).

    A name mentioned in plain prose ("there is no `type` member") is not a
    claim about the shape; a name inside braces is.
    """
    members: set[str] = set()
    for span in BRACE_SPAN_RE.findall(paragraph):
        if ":" in span:  # an object literal: keys only, never values
            members |= set(re.findall(r'"([A-Za-z][A-Za-z0-9_]*)"\s*:', span))
        else:            # a set of member names
            members |= set(re.findall(r'"([A-Za-z][A-Za-z0-9_]*)"', span))
    return members


def _errors_paragraph(text: str) -> str:
    return next(par for par in text.split("\n\n") if "Errors are" in par)


def test_the_documented_error_shape_is_the_shape_the_engine_sends(client):
    resp = _request(client, "GET", "/iterations/41")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"detail"}, body
    assert set(body["detail"]) == {"title", "status", "detail"}, body
    assert resp.headers["content-type"].startswith("application/json")

    doc = API_DOC.read_text()
    claimed = _claimed_error_members(_errors_paragraph(doc))
    assert claimed == set(body) | set(body["detail"]), (
        "docs/api.md presents error members the engine does not send (or "
        f"omits ones it does): documented {sorted(claimed)}")
    assert "application/problem+json" not in doc, (
        "the engine serves errors as application/json; problem+json is the "
        "shape it borrows, not the media type it sends")


def test_the_error_shape_check_would_catch_the_rfc_7807_claim():
    old = ('Errors are RFC 7807 problem+json: `{"type", "title", "status", '
           '"detail"}`.')
    assert _claimed_error_members(old) == {"type", "title", "status", "detail"}
    assert problem(404, "no such iteration").detail.keys() == {
        "title", "status", "detail"}


# ---- a pause is not a phase ------------------------------------------------

PAUSED_PHASE_RE = re.compile(r"(?:phase|state)[^.\n]{0,12}`?[\"']?paused",
                             re.IGNORECASE)
# Prose that *denies* the paused phase names it without promising it -- the
# same per-mention escape hatch test_docs_consistency.py gives a deliberately
# wrong example. Deliberately narrow, and only on the mention's own line.
PAUSE_DENIALS = ("there is no", "not a state", "never")


def _paused_phase_claims(text: str) -> list[str]:
    return [f"{i}: {line.strip()}" for i, line in
            enumerate(text.splitlines(), start=1)
            if PAUSED_PHASE_RE.search(line)
            and not any(d in line.lower() for d in PAUSE_DENIALS)]


def test_neither_the_engine_nor_the_docs_knows_a_paused_phase():
    src = "\n".join(path.read_text() for path in
                    sorted((REPO_ROOT / "src").rglob("*.py")))
    assert 'phase="paused"' not in src and '"phase": "paused"' not in src
    offenders = {}
    for path in DOC_FILES:
        hits = _paused_phase_claims(path.read_text())
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        "POST /pause changes no status field -- it emits a log event and holds "
        f"the loop; these docs promise a paused phase/state: {offenders}")


def test_the_paused_phase_check_would_catch_the_old_claim():
    assert _paused_phase_claims(
        "Pause holds before the next one (`state: running`, `phase: paused` "
        "reported in `/status`).")
    assert not _paused_phase_claims(
        'there is no `phase: "paused"`: the pause emits a log event')
    # the hatch is per-mention: a claim in its own sentence is still a claim
    assert _paused_phase_claims(
        "`/status` reports `phase: paused` while held at the boundary.")


def test_pause_reports_itself_only_through_the_event_stream(client, tmp_path):
    before = json.loads((tmp_path / "status.json").read_text())
    resp = _request(client, "POST", "/pause")
    assert resp.status_code == 200 and resp.json() == {"paused": True}
    after = json.loads((tmp_path / "status.json").read_text())
    assert after.get("phase") == before.get("phase")
    assert after.get("state") == before.get("state") == "running"
    events = (tmp_path / "events.jsonl").read_text()
    assert "paused at next iteration boundary" in events


# ---- GET /status and GET /config: documented shape vs written shape --------

def _status_fields_the_engine_writes() -> set[str]:
    """Every field name the engine passes to `update_status()` literally."""
    fields: set[str] = set()
    for path in sorted(ENGINE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "update_status":
                continue
            fields |= {kw.arg for kw in node.keywords if kw.arg}
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    fields |= {k.value for k in arg.keys
                               if isinstance(k, ast.Constant)}
    return fields


def test_every_status_field_the_engine_writes_is_documented(client):
    fields = _status_fields_the_engine_writes()
    # the writers are real: `updatedAt` is stamped by update_status itself and
    # so is not a keyword anywhere -- assert it separately, from the file.
    assert len(fields) >= 20, sorted(fields)
    served = _request(client, "GET", "/status")
    assert served.status_code == 200, served.text
    assert "updatedAt" in served.json()
    doc = API_DOC.read_text()
    missing = sorted(f for f in fields | {"updatedAt"}
                     if f"`{f}`" not in doc and f'"{f}"' not in doc)
    assert not missing, (
        "docs/api.md's GET /status contract omits fields the engine writes "
        f"into status.json: {missing}")


def _documented_config_shape(text: str) -> dict:
    """`GET /config`'s example object, comments stripped, as real JSON."""
    block = re.search(r"```json\n(\{.*?)```", _section(text, "### `GET /config`"),
                      re.DOTALL)
    assert block, "docs/api.md no longer shows GET /config's response shape"
    body = re.sub(r"//[^\n]*", "", block.group(1))
    body = re.sub(r"\.\.\.\s*,?", "", body)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    return json.loads(body)


def test_the_documented_config_shape_has_the_effective_configs_own_keys():
    documented = _documented_config_shape(API_DOC.read_text())
    effective = JobConfig(run_id="docs").effective()
    added_by_the_route = {"prompts", "skills", "creds", "llmEnvKeys"}
    assert set(documented) == set(effective) | added_by_the_route
    for key in ("budgets", "flags", "model"):
        assert set(documented[key]) == set(effective[key]), key


def test_the_config_shape_check_would_catch_the_missing_flags():
    doc = API_DOC.read_text().replace(
        '"flags": {"vigilant": false, "onComplete": "idle", '
        '"onCompleteCmd": null, "reflect": false},',
        '"flags": {"vigilant": false, "onComplete": "idle"},')
    documented = _documented_config_shape(doc)
    assert set(documented["flags"]) != set(
        JobConfig(run_id="docs").effective()["flags"])


# ---- install lines name the distribution, not the console script -----------

INSTALL_RE = re.compile(r"\b(?:pipx|pip3?|uvx|uv tool) install "
                        r"([A-Za-z0-9._/-]+)")


def _packaging_names() -> tuple[str, set[str]]:
    """(distribution name, console script names) straight from pyproject."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    name = re.search(r'(?m)^name = "([^"]+)"', text).group(1)
    scripts = set(re.findall(r'(?m)^([A-Za-z0-9_-]+) = "ralphd\.', text))
    return name, scripts


def test_every_documented_install_command_names_the_distribution():
    distribution, scripts = _packaging_names()
    assert distribution == "ralphd" and "ralphctl" in scripts
    allowed = {distribution, ".", "./", "-e", "git+https://github.com/n-orlov/ralphd.git"}
    offenders = {}
    for path in DOC_FILES:
        bad = []
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for token in INSTALL_RE.findall(line):
                if token in allowed or token.startswith("-"):
                    continue
                if "/" in token:   # a path or a URL, not a distribution name
                    continue
                bad.append(f"{lineno}: install {token}")
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = bad
    assert not offenders, (
        f"the installable distribution is {distribution!r} (it *provides* "
        f"{sorted(scripts)}); these lines install something else: {offenders}")


def test_the_install_check_would_catch_the_console_script_spelling():
    _distribution, scripts = _packaging_names()
    assert INSTALL_RE.findall("Install: `pipx install ralphctl` (or `uvx "
                              "ralphctl \u2026`).") == ["ralphctl"]
    assert "ralphctl" in scripts  # ...which is exactly what made it wrong
    assert INSTALL_RE.findall("`pipx install .` from a checkout") == ["."]


def test_the_install_check_is_substantive():
    found = [token for path in DOC_FILES
             for token in INSTALL_RE.findall(path.read_text())]
    assert len(found) >= 4, found
