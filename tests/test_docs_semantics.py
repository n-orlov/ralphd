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
* `POST /pause` was documented as reporting `phase: paused` in `/status`; an
  operator pause changes no field at all -- it emits a `log` event and holds the
  loop (v0.18 task 036 adds ONE recorded pause, the engine's own
  `rejected_last_approach` escalation hold, and the check below pins that the
  operator pause is still not it);
* `GET /status`'s documented shape omitted six fields the engine always
  writes (`schemaVersion`, `createdAt`/`updatedAt`/`endedAt`, `reason`,
  `unconsumedSteering`, `graceReview`) and `GET /config`'s omitted two flags;
* every install line named the *console script* (`pipx install ralphctl`) as
  the distribution to install, which is `ralphd`.

Task 043b (#22) is slice 2, the same pass over `docs/cli.md` and
`docs/architecture.md`, and its checks live in the second half of this module:

* `--model-strategy` was documented with a fourth `custom` preset the parser
  rejects and `JobConfig.STRATEGY_TIERS` has never defined, and the presets
  were described in prose that had `balanced` routing only two phases strong;
* `ralphctl runs --state` was documented as a four-value choice set, omitting
  `starting` (it is an exact match on the recorded state, and every state a
  run dir can hold is filterable);
* the hub's log tail was documented as "reimplemented in `app.js`, not shared
  code" three screens after the endpoint it belongs to documents the opposite
  (task 014 moved the rendering server-side into `cli/log_render.py`);
* `resume` was documented as *not* replaying `--forward-env`/`--llm-env`/
  `--env` and as taking those flags again itself — it does replay them (from
  `env-wiring.json`) and has no such flags;
* the run dir was documented as mounted at `/run` holding a redacted
  `job.json`; it is `/run/ralphd`, there is no `job.json` anywhere (the job
  config is `<config-dir>/job.yaml`, verbatim at rest and redacted when read
  out), and the tree omitted five files the code writes;
* the phase-prompt list named a nonexistent `AGENT.md`, omitted `reflect.md`,
  and claimed all prompts are API-overridable (`PROMPT_NAMES` has four);
* the hub was documented as serving two endpoints with its static bundle
  "still pending", and the job image as a published `ghcr.io` reference.

Slice 2's own prose then needed two corrections of its own (found by the
independent verification of task 043b), and they have checks here too:

* `ralphctl watch` was documented as switching to raw event objects on
  "any non-TTY use" (also in `docs/tutorial.md`); `_follow_events` branches on
  `args.json` and nothing else, and the real command with stdout on a pipe
  prints the rendered `[<ts>] <type> {…}` lines;
* the hub's live-answer reads were called "the two (log tail, PRD,
  steering)" -- three names for two -- and run detail was put on the
  "on-disk by design" side although `run_detail()` proxies `GET /status` and
  `GET /tasks` and publishes a `live` flag (which is how `docs/cli.md`
  documents it, so the two docs contradicted each other).

Round 3 (the second independent verification of 043b) found three more, in the
first two sections of `docs/architecture.md`:

* the workspace was offered as "host bind-mount or named volume" (and in
  README's diagram as "mounted or volume"); `_parse_workspace_specs` resolves
  a host directory and exits 2 for anything else, so a volume name never
  reaches docker, and §3 of the same doc always listed the two real modes;
* the shared iteration budget was called `max_iterations`; the field, the
  `job.yaml` key and the published budget are all `iterations` (that check is
  the identifier scan in `test_docs_consistency.py`, the name layer);
* the engine was called `ralphd`, which is the *distribution*; the installed
  console script is `ralphd-engine`.

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
from ralphd.engine.config import MODEL_HATS, JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir

REPO_ROOT = Path(__file__).resolve().parents[1]
API_DOC = REPO_ROOT / "docs" / "api.md"
CLI_DOC = REPO_ROOT / "docs" / "cli.md"
ARCH_DOC = REPO_ROOT / "docs" / "architecture.md"
ENGINE_DIR = REPO_ROOT / "src" / "ralphd" / "engine"
DOC_FILES = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md")),
             REPO_ROOT / "SPEC.md"]


@pytest.fixture
def client(tmp_path):
    """The real engine app over an empty run dir, as an ASGI client factory."""
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
    sup = LoopSupervisor(JobConfig(run_id="docs", infra_retry_backoff_s=[0.0],
                                  infra_retry_backoff_max_s=0.0), run, tmp_path)
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


# ---- a pause is not a phase (and an OPERATOR pause is not a state) --------

PAUSED_PHASE_RE = re.compile(r"phase[^.\n]{0,12}`?[\"']?paused", re.IGNORECASE)
PAUSED_STATE_RE = re.compile(r"state[^.\n]{0,12}`?[\"']?paused", re.IGNORECASE)
# Prose that *denies* the paused phase names it without promising it -- the
# same per-mention escape hatch test_docs_consistency.py gives a deliberately
# wrong example. Deliberately narrow, and only on the mention's own line.
PAUSE_DENIALS = ("there is no", "not a state", "never")
# v0.18 requirement D.1/D.2 (#142, task 036): `state: "paused"` IS a recorded
# value now -- for exactly one writer, the engine pausing ITSELF on the
# `rejected_last_approach` escalation edge, which persists `pauseReason`
# alongside it. A line may claim the paused STATE only while naming that
# writer; a doc that goes back to promising `POST /pause` (or anything else)
# records a paused state names neither marker and is still an offender, and no
# hatch at all exists for a paused PHASE.
ENGINE_PAUSE_MARKERS = ("pausereason", "rejected_last_approach")


def _paused_phase_claims(text: str) -> list[str]:
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        low = line.lower()
        if any(d in low for d in PAUSE_DENIALS):
            continue
        if PAUSED_PHASE_RE.search(line):
            out.append(f"{i}: {line.strip()}")
        elif (PAUSED_STATE_RE.search(line)
              and not any(m in low for m in ENGINE_PAUSE_MARKERS)):
            out.append(f"{i}: {line.strip()}")
    return out


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
        "the loop, and no pause of any kind is a phase; these docs promise a "
        f"paused phase, or a paused state with no engine writer: {offenders}")


def test_the_paused_phase_check_would_catch_the_old_claim():
    assert _paused_phase_claims(
        "Pause holds before the next one (`state: running`, `phase: paused` "
        "reported in `/status`).")
    assert not _paused_phase_claims(
        'there is no `phase: "paused"`: the pause emits a log event')
    # the hatch is per-mention: a claim in its own sentence is still a claim
    assert _paused_phase_claims(
        "`/status` reports `phase: paused` while held at the boundary.")


def test_an_operator_pause_recording_a_paused_state_is_still_an_offender():
    """Task 036's hatch is only for the engine's own escalation hold: a doc
    that claims `POST /pause` (or an unattributed pause) records `state:
    paused` names neither `pauseReason` nor `rejected_last_approach`, so it is
    caught exactly as it was before the hatch existed."""
    assert _paused_phase_claims(
        "`POST /pause` sets `state: paused` in `/status`.")
    assert _paused_phase_claims(
        "A paused run reports `state: \"paused\"` while held at the boundary.")
    # ... while the engine-attributed claim the docs now really make passes
    assert not _paused_phase_claims(
        'the escalation writes `state: "paused"` plus `pauseReason: '
        '"rejected_last_approach"` to status.json')


def test_pause_reports_itself_only_through_the_event_stream(client, tmp_path):
    before = json.loads((tmp_path / "status.json").read_text())
    resp = _request(client, "POST", "/pause")
    assert resp.status_code == 200 and resp.json() == {"paused": True}
    after = json.loads((tmp_path / "status.json").read_text())
    assert after.get("phase") == before.get("phase")
    assert after.get("state") == before.get("state") == "running"
    # Task 036: an OPERATOR pause records no reason either -- the field only
    # ever appears with the engine's own escalation hold.
    assert after.get("pauseReason") is None
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
        '"onCompleteCmd": null, "reflect": false, "peerPlanReview": false, '
        '"peerVerdictGate": false, "operatorInputNeededPause": true, '
        '"midPlanReview": false, "routeTaskSelection": true},',
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


# ===========================================================================
# Task 043b (#22): the same pass over docs/cli.md and docs/architecture.md.
# ===========================================================================

# ---- a documented value list is the parser's own choice set ---------------

ALTERNATION_RE = re.compile(
    r"(?<![\w./-])([a-z][A-Za-z0-9-]*(?:\\?\|[a-z][A-Za-z0-9-]*)+)(?![\w./-])")


def _parser_choices() -> dict[str, set[str]]:
    """{flag: its argparse choices} over every subcommand."""
    from ralphd.cli.main import build_parser
    parser = build_parser()
    sub = parser._subparsers._group_actions[0]
    found: dict[str, set[str]] = {}
    for command in sub.choices.values():
        for action in command._actions:
            if action.choices and action.option_strings:
                for flag in action.option_strings:
                    found[flag] = set(action.choices)
    return found


def _choice_problems(text: str, choices: dict[str, set[str]]) -> list[str]:
    """Lines documenting a flag with an alternation that is not its choices.

    An alternation (`a|b|c`, or `a\\|b\\|c` inside a markdown table) on a line
    that documents the flag and names at least one real choice is a claim about
    the whole choice set, so it has to *be* the whole choice set.
    """
    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for flag, valid in choices.items():
            if flag not in line:
                continue
            for span in ALTERNATION_RE.findall(line):
                claimed = set(re.split(r"\\?\|", span))
                if not claimed & valid or claimed == valid:
                    continue
                problems.append(
                    f"{lineno}: {flag} documented as {sorted(claimed)}, "
                    f"parser accepts {sorted(valid)}")
    return problems


def test_every_documented_value_list_is_the_parsers_own_choice_set():
    choices = _parser_choices()
    assert "--price-strategy" in choices and "--on-complete" in choices
    problems = _choice_problems(CLI_DOC.read_text(), choices)
    assert not problems, (
        "docs/cli.md documents flag values argparse would reject (or omits "
        f"ones it accepts): {problems}")


def test_the_choice_set_check_would_catch_an_invented_price_strategy():
    """Task 006 retired --model-strategy (the flag this check used to be
    pinned against) along with the STRATEGY_TIERS table it validated --
    --price-strategy is the other enumerated `start` flag with a doc-table
    row, so the mechanism is re-pinned against it instead of going untested.
    """
    choices = _parser_choices()
    old = ("| `--price-strategy S` | none | "
           "`none" + chr(92) + "|aws" + chr(92) + "|invented` " + chr(8212) +
           " derive a cost for unpriced routes |")
    assert _choice_problems(old, choices)
    assert "invented" not in choices["--price-strategy"]
    # the check is not vacuous: the corrected row passes
    assert not _choice_problems(old.replace(chr(92) + "|invented", ""), choices)


# ---- `runs --state` filters on every state a run dir can record -----------

def _documented_state_filter(text: str) -> set[str]:
    span = re.search(r"--state ([a-z_|]+)", text)
    assert span, "docs/cli.md no longer shows `ralphctl runs --state`'s values"
    return set(span.group(1).split("|"))


def test_the_documented_state_filter_names_every_state_a_run_can_record():
    from ralphd.engine.state import NONTERMINAL_STATES, TERMINAL_STATES
    every = set(NONTERMINAL_STATES) | set(TERMINAL_STATES)
    assert _documented_state_filter(CLI_DOC.read_text()) == every, (
        "`ralphctl runs --state` is an exact match on the recorded state, so "
        f"every one of {sorted(every)} is filterable")


def test_the_state_filter_check_would_catch_the_missing_starting_state():
    from ralphd.engine.state import NONTERMINAL_STATES
    old = "ralphctl runs [--state running|succeeded|failed|aborted]"
    assert "starting" in NONTERMINAL_STATES
    assert _documented_state_filter(old) != (
        set(NONTERMINAL_STATES) | {"succeeded", "failed", "aborted"})


# ---- the hub renders no log lines of its own ------------------------------

REIMPLEMENT_RE = re.compile(r"reimplement", re.IGNORECASE)
# Same per-mention hatch as the paused-phase check: prose that *denies* the
# client-side renderer names it without promising it.
REIMPLEMENT_DENIALS = ("no longer", "does not", "never", "used to", "pre-014",
                       "pre-task-014", "reimplements none", "rather than")


def _reimplementation_claims(text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        low = line.lower()
        if not REIMPLEMENT_RE.search(low):
            continue
        if "app.js" not in low and "browser" not in low:
            continue
        if any(d in low for d in REIMPLEMENT_DENIALS):
            continue
        hits.append(f"{lineno}: {line.strip()}")
    return hits


def test_no_doc_claims_the_hub_client_reimplements_the_log_rendering():
    """Task 014 moved the rendering server-side; the code, not the prose, is
    the reason no doc may claim otherwise.

    Named "hub client", not "browser": the fast lane is selected with
    `-k 'not browser'`, which filters by NODE NAME, so a non-browser-tier test
    whose name contains "browser" is silently deselected from every fast-lane
    run (task 032). Only a test in the `-k browser` tier may carry the word.
    """
    hub = (REPO_ROOT / "src" / "ralphd" / "cli" / "ui_server.py").read_text()
    assert "log_render" in hub, "the hub no longer renders through log_render"
    app_js = (REPO_ROOT / "src" / "ralphd" / "cli" / "web" / "app.js").read_text()
    for event_rule in ("thinking_delta", "tool_call", "message_end"):
        assert event_rule not in app_js, (
            f"app.js branches on {event_rule} again: it renders pi events "
            "itself, and the docs' shared-renderer claim is now the wrong one")
    offenders = {}
    for path in DOC_FILES:
        hits = _reimplementation_claims(path.read_text())
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        "the hub displays lines `cli/log_render.py` already rendered; these "
        f"docs still credit the browser with the rendering: {offenders}")


def test_the_reimplementation_check_would_catch_the_old_run_detail_claim():
    assert _reimplementation_claims(
        "markers \u2014 reimplemented in `app.js`, not shared code, since the CLI "
        "is Python and the bundle is browser JS), and a steering form")
    assert not _reimplementation_claims(
        "lines (one per DOM element, via `textContent`); it does not "
        "reimplement any event-to-text rendering rules of its own")


# ---- the run-dir tree is the run dir -------------------------------------

def _run_dir_tree_names(text: str) -> set[str]:
    """Entry names of architecture.md's `~/.ralphd/runs/<run-id>/` tree."""
    block = re.search(r"```\n~/\.ralphd/runs/<run-id>/(.*?)```", text, re.DOTALL)
    assert block, "docs/architecture.md no longer draws the run-dir tree"
    names = set()
    for line in block.group(1).splitlines():
        entry = re.match(r"^[\u2502\u251c\u2514\u2500\s]+([^\s#]+)", line)
        if not entry:
            continue
        name = entry.group(1).split("#")[0]
        if name and not name.endswith("/"):
            names.add(name)
    return names


def _run_dir_file_names(root: Path) -> set[str]:
    """Every file `RunDir` itself puts directly in a run dir."""
    from ralphd.engine.state import (
        HOST_FILE,
        OPERATOR_TERMINATION_FILE,
        TASKS_LAST_GOOD_NAME,
        RunDir,
    )
    run = RunDir(root=root)
    names = set()
    for attr in dir(type(run)):
        if attr.startswith("_") or not isinstance(
                getattr(type(run), attr, None), property):
            continue
        value = getattr(run, attr)
        if isinstance(value, Path) and value.parent == run.root and value.suffix:
            names.add(value.name)
    return names | {HOST_FILE, OPERATOR_TERMINATION_FILE, TASKS_LAST_GOOD_NAME,
                    "events.jsonl"}


def test_the_run_dir_tree_lists_every_file_the_run_dir_holds(tmp_path):
    documented = _run_dir_tree_names(ARCH_DOC.read_text())
    missing = sorted(_run_dir_file_names(tmp_path) - documented)
    assert not missing, (
        "docs/architecture.md's run-dir tree omits files the engine writes "
        f"into every run dir: {missing}")


def test_the_run_dir_tree_invents_no_file():
    """Every name in the tree must be a name the shipped code spells."""
    source = "\n".join(p.read_text() for p in
                       sorted((REPO_ROOT / "src").rglob("*.py")))
    invented = sorted(n for n in _run_dir_tree_names(ARCH_DOC.read_text())
                      if n not in source and not n.startswith("00"))
    assert not invented, (
        "docs/architecture.md's run-dir tree names files nothing writes: "
        f"{invented}")


def test_the_run_dir_tree_checks_would_catch_the_old_tree(tmp_path):
    old = ("```\n~/.ralphd/runs/<run-id>/          # mounted at /run\n"
           "\u251c\u2500\u2500 job.json            # immutable job config as launched\n"
           "\u251c\u2500\u2500 status.json         # engine-maintained\n"
           "\u251c\u2500\u2500 tasks.json          # task state (source of truth)\n```")
    names = _run_dir_tree_names(old)
    assert "job.json" in names and "host.json" not in names
    assert _run_dir_file_names(tmp_path) - names   # the omissions
    source = "\n".join(p.read_text() for p in
                       sorted((REPO_ROOT / "src").rglob("*.py")))
    assert "job.json" not in source                # ...and the invention


def test_the_job_config_is_a_yaml_file_in_the_config_dir():
    """`job.json` never existed: the job config is `<config-dir>/job.yaml`,
    written verbatim by `start` and redacted only when read out."""
    from ralphd.engine.state import JOB_CONFIG_FILE
    assert JOB_CONFIG_FILE == "job.yaml"
    cli = (REPO_ROOT / "src" / "ralphd" / "cli" / "main.py").read_text()
    assert '"job.yaml"' in cli or "'job.yaml'" in cli
    from ralphd.engine.redact import redact_job_yaml
    assert callable(redact_job_yaml)
    offenders = {}
    for path in [*DOC_FILES, *sorted((REPO_ROOT / "src").rglob("*.py"))]:
        hits = [f"{i}: {line.strip()}" for i, line
                in enumerate(path.read_text().splitlines(), start=1)
                if "job.json" in line]
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        "nothing writes a `job.json`; the job config is "
        f"`<config-dir>/{JOB_CONFIG_FILE}`: {offenders}")


# ---- phase prompts: the shipped set, and which of them the API can replace -

PROMPTS_DIR = REPO_ROOT / "src" / "ralphd" / "prompts"


def _documented_prompt_files(text: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z_-]+\.md)`",
                         _section(text, "### Phase prompts")))


def test_the_documented_phase_prompts_are_the_shipped_ones():
    shipped = {p.name for p in PROMPTS_DIR.glob("*.md")}
    assert shipped, PROMPTS_DIR
    assert _documented_prompt_files(ARCH_DOC.read_text()) == shipped, (
        f"docs/architecture.md's phase-prompt list is not {sorted(shipped)}")


def test_only_the_api_replaceable_prompts_are_documented_as_such():
    from ralphd.engine.config import PROMPT_NAMES
    section = _section(ARCH_DOC.read_text(), "### Phase prompts")
    shipped = {p.stem for p in PROMPTS_DIR.glob("*.md")}
    mount_only = shipped - set(PROMPT_NAMES)
    assert mount_only, (
        "every shipped prompt is now in PROMPT_NAMES -- say so plainly and "
        "delete this check")
    for name in mount_only:
        assert name in section, (
            f"`{name}` cannot be replaced through PUT /config/prompts/"
            f"{{name}} (PROMPT_NAMES = {list(PROMPT_NAMES)}); the section must "
            "say so instead of promising all prompts are overridable")


def test_the_prompt_checks_would_catch_the_old_claim():
    old = ("### Phase prompts\n\nPrompt templates live in the image at "
           "`/opt/ralphd/prompts/` -- one per phase (`planning.md`, "
           "`worker.md`, `review.md`, `task-verify.md`, plus the "
           "workspace-level agent instructions file `AGENT.md`). All are "
           "**overridable**.\n")
    documented = _documented_prompt_files(old)
    shipped = {p.name for p in PROMPTS_DIR.glob("*.md")}
    assert documented != shipped
    assert "AGENT.md" in documented and not list(REPO_ROOT.rglob("AGENT.md"))
    assert "reflect.md" in shipped and "reflect.md" not in documented


# ---- the model-resolution section names the retired knobs' replacement ----

def test_the_model_resolution_section_names_every_hat_and_no_retired_knob():
    """Task 006 retired `STRATEGY_TIERS`/`model_strategy`/`fast_model`
    entirely -- docs/architecture.md's section must no longer describe that
    table, and must instead name every hat `MODEL_HATS` lists."""
    section = _section(ARCH_DOC.read_text(), "### Model resolution")
    assert "STRATEGY_TIERS" not in section
    for hat in MODEL_HATS:
        assert hat in section, f"{hat!r} is not named in docs/architecture.md's Model resolution section"


def test_the_model_resolution_check_would_catch_the_old_strategy_table():
    old = ("### Model resolution\n\nPresets: `quality-first` (default; one "
           "strong model everywhere), `cost-optimized` (strong model for "
           "planning only), `balanced` (strong for planning + review).\n")
    section = _section(old, "### Model resolution")
    assert not all(hat in section for hat in MODEL_HATS)


# ---- the hub: the bundle ships, and the endpoint list is complete ---------

# The claim spans a line break in the wording it had, so this reads the
# paragraph, not the line (`_paused_phase_claims`' line scope would miss it).
PENDING_BUNDLE_RE = re.compile(
    r"bundle[^.]{0,160}?(still pending|is pending|not implemented|not yet "
    r"(?:shipped|implemented))", re.IGNORECASE)


def _pending_bundle_claims(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    return [m.group(0) for m in PENDING_BUNDLE_RE.finditer(flat)]


def test_the_static_hub_bundle_ships_and_no_doc_calls_it_pending():
    from ralphd.cli.ui_server import STATIC_DIR
    for name in ("index.html", "app.js", "style.css"):
        assert (STATIC_DIR / name).is_file(), name
    offenders = {}
    for path in DOC_FILES:
        hits = _pending_bundle_claims(path.read_text())
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        f"the hub bundle ships in {STATIC_DIR.relative_to(REPO_ROOT)}; these "
        f"docs still call it pending: {offenders}")


def _hub_route_segments() -> set[str]:
    """The literals the hub's dispatcher compares path segments against."""
    source = (REPO_ROOT / "src" / "ralphd" / "cli" / "ui_server.py").read_text()
    segments: set[str] = set()
    for line in source.splitlines():
        if "segs" in line:
            segments |= set(re.findall(r'"([a-z][a-z-]*)"', line))
    return segments - {"api", "runs"}


def test_every_hub_endpoint_segment_is_documented_in_the_ui_section():
    section = _section(CLI_DOC.read_text(), "### `ralphctl ui")
    missing = sorted(s for s in _hub_route_segments() if s not in section)
    assert not missing, (
        "docs/cli.md's `ralphctl ui` section presents the whole JSON surface, "
        f"so it must document these routes too: {missing}")


def test_every_ralphctl_subcommand_has_its_own_section_in_cli_md():
    from ralphd.cli.main import build_parser
    verbs = set(build_parser()._subparsers._group_actions[0].choices)
    doc = CLI_DOC.read_text()
    documented = {verb for line in doc.splitlines() if line.startswith("#")
                  for verb in re.findall(r"`ralphctl ([a-z-]+)", line)}
    missing = sorted(verbs - documented)
    assert not missing, (
        f"docs/cli.md documents no section for: {missing}")


def test_the_hub_and_command_completeness_checks_are_substantive():
    assert len(_hub_route_segments()) >= 10, _hub_route_segments()
    section = _section(CLI_DOC.read_text(), "### `ralphctl ui")
    assert "nonexistent-route" not in section
    assert not _section(CLI_DOC.read_text(), "### `ralphctl ui").startswith("#\n")


# ---- the job image is built, not pulled -----------------------------------

def test_no_doc_names_a_published_registry_image_for_the_job_image():
    from ralphd.cli import image
    assert "/" not in image.IMAGE_REPO, image.IMAGE_REPO
    assert image.image_tag("deadbee") == f"{image.IMAGE_REPO}:deadbee"
    offenders = {}
    for path in DOC_FILES:
        hits = [f"{i}: {line.strip()}" for i, line
                in enumerate(path.read_text().splitlines(), start=1)
                if "ghcr.io" in line or "docker.io/" in line]
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        f"v0.6 publishes no image; the job image is {image.IMAGE_REPO}:<hash> "
        f"built from the checkout: {offenders}")


def test_the_pending_bundle_check_would_catch_the_old_claim():
    assert _pending_bundle_claims(
        "gracefully instead of erroring (see [cli.md](cli.md)). The static "
        "bundle\nserved at non-`/api` paths is still pending (v0.3, task 034).")
    assert not _pending_bundle_claims(
        "The static bundle served at non-`/api` paths (`src/ralphd/cli/web/`) "
        "landed in v0.3 task 034.")


# ---- `ralphctl watch` is an event stream, not a TUI ------------------------

TUI_CLAIM_RE = re.compile(
    r"(?:^|[^\w-])(TUI|curses|gauges?|scrolling tail|press `?q`?)", re.IGNORECASE)
# The docs are allowed -- required, even -- to say the opposite.
TUI_DENIALS = ("not a tui", "no tui", "no curses", "ships no", "bubbletea",
               "instead of drawing", "rather than a tui", "no framework")


def _tui_claims(text: str) -> list[str]:
    """Lines that credit `watch` with a full-screen UI it does not have."""
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        low = line.lower()
        if "watch" not in low and "tui" not in low:
            continue
        if not TUI_CLAIM_RE.search(line):
            continue
        if any(d in low for d in TUI_DENIALS):
            continue
        hits.append(f"{lineno}: {line.strip()}")
    return hits


def test_ralphctl_watch_is_an_event_stream_and_no_doc_promises_a_tui():
    """`cmd_watch` prints one line per SSE event; there is no screen, no
    key handling and no gauge anywhere in the CLI (SPEC: no TUI framework)."""
    import inspect

    from ralphd.cli import main as cli_main
    assert "_follow_events" in inspect.getsource(cli_main.cmd_watch)
    follow = inspect.getsource(cli_main._follow_events)
    assert "data: " in follow and "print(" in follow, follow
    for absent in ("curses", "gauge", "tcsetattr", "cbreak"):
        assert absent not in follow.lower(), (
            f"_follow_events now does {absent!r}: `watch` grew a UI and the "
            "docs' event-stream description is the wrong one")
    # ...and the whole CLI imports no TUI/curses framework
    cli_source = "\n".join(p.read_text() for p in
                          sorted((REPO_ROOT / "src" / "ralphd" / "cli").rglob("*.py")))
    assert not re.search(r"^\s*import (curses|rich|textual|blessed)",
                         cli_source, re.MULTILINE), "the CLI imports a TUI library"
    offenders = {}
    for path in DOC_FILES:
        hits = _tui_claims(path.read_text())
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        "`ralphctl watch` prints `[ts] type {...}` lines (NDJSON with "
        f"--json); these docs still describe a TUI: {offenders}")


def test_the_tui_check_would_catch_the_old_watch_description():
    assert _tui_claims(
        "Live TUI: task table, phase/approach/iteration header, budget + cost "
        "gauges,\nscrolling tail of agent output, pending steering. Read-only; "
        "`q` quits.") == [
        ("1: Live TUI: task table, phase/approach/iteration header, budget + "
         "cost gauges,")]
    assert _tui_claims("A live TUI, `q` to quit")
    assert _tui_claims("the `watch` cost gauge renders through format_cost")
    assert _tui_claims("`ralphctl watch` renders a TUI:\ntask table, current "
                       "phase/iteration, tail of agent output, budget gauge.")
    assert not _tui_claims(
        "Live **event stream**, not a TUI: `watch` subscribes to the run's\n"
        "`GET /events?since=0` and prints one line per event as it arrives.")


# ---- `watch`'s raw-event format is the --json flag, not the stream ---------
#
# Task 043b, second pass: the rewritten `watch` paragraph carried the old
# claim that "any non-TTY use" (a pipe) also switches the output to raw event
# objects. `_follow_events` branches on `args.json` and on nothing else -- the
# flag is a plain top-level `store_true` and no code assigns it -- so a script
# reading a pipe gets exactly the same `[<ts>] <type> {...}` lines a terminal
# does unless it asks for `--json`.

STREAM_SHAPE_WORDS_RE = re.compile(r"raw event|event object|NDJSON|JSON object",
                                   re.IGNORECASE)
STREAM_PIPE_WORDS_RE = re.compile(
    r"non-tty|not a tty|no tty|\bpipes?\b|\bpiped\b|\bpiping\b|non-interactive|"
    r"not a terminal|isn't a terminal", re.IGNORECASE)
# Saying that the format does NOT depend on the stream is the correction, not
# the defect.
STREAM_DENIALS_RE = re.compile(
    r"does not depend|never depends|only switch|nothing else changes|"
    r"does not turn it into|regardless of", re.IGNORECASE)


def _tty_conditional_stream_claims(text: str) -> list[str]:
    """Sentences that make the raw-event output of an event stream conditional
    on the stream being a pipe rather than on the `--json` flag."""
    hits = []
    for para in re.split(r"\n\s*\n", text):
        flat = re.sub(r"\s+", " ", para).strip()
        if "--json" not in flat:
            continue
        for sentence in re.split(r"(?<=[.;]) ", flat):
            if "--json" not in sentence:
                continue
            if not STREAM_SHAPE_WORDS_RE.search(sentence):
                continue
            if not STREAM_PIPE_WORDS_RE.search(sentence):
                continue
            if STREAM_DENIALS_RE.search(sentence):
                continue
            hits.append(sentence)
    return hits


def test_the_event_stream_format_switches_on_the_json_flag_only():
    """Code side of the claim: one branch, `args.json`, nowhere assigned."""
    import inspect

    from ralphd.cli import main as cli_main
    follow = inspect.getsource(cli_main._follow_events)
    assert "args.json" in follow, follow
    for absent in ("isatty", "stdout.isatty", "os.isatty"):
        assert absent not in follow, (
            f"_follow_events now consults {absent!r}: the docs' 'the format "
            "depends only on --json' claim would be the wrong one")
    action = next(a for a in cli_main.build_parser()._actions
                  if "--json" in a.option_strings)
    assert action.nargs == 0 and action.const is True and action.default is False, (
        "--json is no longer a plain flag; its default may now come from the "
        f"stream: {action!r}")
    cli_source = "\n".join(p.read_text() for p in
                         sorted((REPO_ROOT / "src" / "ralphd" / "cli").rglob("*.py")))
    assert not re.search(r"\bargs\.json\s*=[^=]", cli_source), (
        "something assigns args.json, so --json may no longer be the only "
        "switch the event stream honours")


def test_no_doc_makes_the_event_stream_format_depend_on_a_pipe():
    offenders = {}
    for path in DOC_FILES:
        hits = _tty_conditional_stream_claims(path.read_text())
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        "`ralphctl watch` prints its rendered lines whether stdout is a "
        "terminal or a pipe; only the global --json flag switches to raw "
        f"event objects. These docs still claim otherwise: {offenders}")


def test_the_stream_format_check_would_catch_the_old_non_tty_claim():
    assert _tty_conditional_stream_claims(
        "run from event 0 first. With `--json` (or any non-TTY use) each line "
        "is the\nraw event object instead, i.e. NDJSON a script can parse.")
    assert _tty_conditional_stream_claims(
        "Non-TTY/`--json`: streams SSE events as NDJSON.")
    assert _tty_conditional_stream_claims(
        "With `--json` (or piped to a script) each line is the raw event "
        "object instead - NDJSON.")
    assert not _tty_conditional_stream_claims(
        "With the global `--json` flag each line is the raw event object "
        "instead, i.e. NDJSON a script can parse. That flag is the only "
        "switch: piping the default output still gives you the rendered "
        "lines.")


def test_a_piped_watch_prints_rendered_lines_and_json_only_on_demand(tmp_path):
    """Empirical side: the real `ralphctl watch` over a stub SSE API, with
    stdout on a pipe (never a TTY in a test), prints `[<ts>] <type> {...}`
    lines that are NOT parseable as JSON -- and the same run with the global
    `--json` flag prints one JSON object per line."""
    import os
    import subprocess

    from tests.conftest import RALPHCTL
    from tests.test_cli_watch_resumed_run import StubEventApi, _seed

    events = [
        {"id": 1, "ts": "2026-01-01T00:00:00Z", "type": "iteration", "n": 1},
        {"id": 2, "ts": "2026-01-01T00:00:01Z", "type": "state",
         "state": "succeeded"},
    ]
    outputs = {}
    for label, argv in (("default", []), ("json", ["--json"])):
        registry = tmp_path / label
        api: StubEventApi
        _, api = _seed(registry, "piped-run", events, state="succeeded")
        try:
            proc = subprocess.run(
                [str(RALPHCTL), *argv, "watch", "piped-run"],
                env={**os.environ, "RALPHD_REGISTRY": str(registry)},
                capture_output=True, text=True, timeout=60)
        finally:
            api.close()
        assert proc.returncode == 0, proc.stderr
        outputs[label] = [ln for ln in proc.stdout.splitlines() if ln.strip()]

    assert outputs["default"] == [
        '[2026-01-01T00:00:00Z] iteration {"n": 1}',
        '[2026-01-01T00:00:01Z] state {"state": "succeeded"}',
    ], outputs["default"]
    for line in outputs["default"]:
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)
    assert [json.loads(ln) for ln in outputs["json"]] == events, outputs["json"]


# ---- which hub reads answer live, and which are on-disk by design ---------

UI_SERVER = REPO_ROOT / "src" / "ralphd" / "cli" / "ui_server.py"

# The words architecture.md's hub paragraph uses for each hub view function
# that asks the run's container API first. The *set* of such functions is
# derived from the code (`_proxying_hub_views`); this map only translates a
# function name into the prose's label for it.
LIVE_VIEW_LABELS = {
    "run_detail": "run detail",
    "rendered_log_lines": "log tail",
    "prd_text": "PRD",
    "steering_list": "steering history",
}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _proxying_hub_views() -> set[str]:
    """Hub view functions that read the run's live container API before
    falling back to the on-disk snapshot -- i.e. those calling `_proxy_text`
    or `_proxy_json` with a `GET`. The `POST` proxies (`steer`, `retry`) are
    mutations, not reads, and are deliberately not in this set."""
    tree = ast.parse(UI_SERVER.read_text())
    views: set[str] = set()
    for top in tree.body:
        if not isinstance(top, ast.FunctionDef):
            continue
        for node in ast.walk(top):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == "_proxy_text":
                views.add(top.name)
            elif node.func.id == "_proxy_json":
                method = node.args[2] if len(node.args) > 2 else None
                if isinstance(method, ast.Constant) and method.value == "GET":
                    views.add(top.name)
    return views


def _live_answer_problems(text: str, labels: dict[str, str]) -> list[str]:
    """Ways the prose's live-vs-on-disk split disagrees with `labels`."""
    flat = re.sub(r"\s+", " ", text)
    match = re.search(r"the ([a-z]+) live-answer reads \(([^)]*)\)", flat)
    if not match:
        return ["no doc states which of the hub's reads answer live"]
    problems = []
    if NUMBER_WORDS.get(match.group(1)) != len(labels):
        problems.append(f"counted {match.group(1)!r}, there are {len(labels)}")
    listed = {part.strip(" `") for part in match.group(2).split(",")}
    if listed != set(labels.values()):
        problems.append(f"lists {sorted(listed)}, the code says "
                        f"{sorted(labels.values())}")
    rest = flat.split("while the rest", 1)[-1].split("The static bundle", 1)[0]
    for label in labels.values():
        if label in rest:
            problems.append(f"calls {label!r} on-disk by design; it proxies")
    return problems


def test_the_hub_reads_the_docs_call_live_are_the_ones_that_proxy():
    proxying = _proxying_hub_views()
    assert proxying == set(LIVE_VIEW_LABELS), (
        "the set of hub views that proxy a live run changed; docs/architecture."
        f"md's live-answer sentence and LIVE_VIEW_LABELS must follow: {proxying}")
    # Every one of them publishes the `live` flag the docs promise, and none
    # of the on-disk-only routes does.
    assert UI_SERVER.read_text().count('"live"') == len(proxying)
    problems = _live_answer_problems(ARCH_DOC.read_text(), LIVE_VIEW_LABELS)
    assert not problems, f"docs/architecture.md: {problems}"


def test_cli_md_documents_the_same_live_reads_as_the_code():
    section = _section(CLI_DOC.read_text(), "### `ralphctl ui")
    flat = re.sub(r"\s+", " ", section)
    # The run-detail endpoint is one of the live-answer reads: its own entry
    # must say so, since architecture.md now classifies it that way too.
    detail = flat.split("`GET /api/runs/<id>`", 1)[-1].split(
        "`GET /api/runs/<id>/logs", 1)[0]
    assert "live" in detail and "on-disk" in detail, detail[:400]


def test_the_live_answer_check_would_catch_the_old_two_reads_claim():
    old = ("the rule they share is that the two live-answer reads (log tail, "
           "PRD, steering) proxy each run's container API when reachable and "
           "fall back to the on-disk snapshot otherwise, so a dead run "
           "degrades gracefully instead of erroring, while the rest are "
           "on-disk by design. The static bundle served at non-`/api` paths "
           "landed in v0.3.")
    problems = _live_answer_problems(old, LIVE_VIEW_LABELS)
    assert any("counted" in p for p in problems), problems
    assert any("lists" in p for p in problems), problems
    fixed = ("the four live-answer reads (run detail, log tail, PRD, steering "
             "history) proxy each run's container API when reachable, while "
             "the rest - the run list, one iteration - are on-disk by design. "
             "The static bundle landed in v0.3.")
    assert not _live_answer_problems(fixed, LIVE_VIEW_LABELS)


# ---- the workspace has two modes, and neither is a volume ----------------
# Round 3 of the semantic pass (found by the independent verification of
# 043b): the overview bullet offered "host bind-mount or named volume" and
# README's diagram "(mounted or volume)", while the only workspace flag
# resolves a host path and exits 2 for anything that is not a directory --
# a volume name can never reach docker. §3 of the same doc has always
# enumerated the two real modes.

CLI_MAIN = REPO_ROOT / "src" / "ralphd" / "cli" / "main.py"

VOLUME_MODE_RE = re.compile(
    r"(?:bind[- ])?mount(?:ed|s)?\s+or\s+(?:a\s+)?(?:named\s+)?volume"
    r"|(?:named\s+)?volume\s+or\s+(?:a\s+)?(?:bind[- ])?mount(?:ed)?",
    re.IGNORECASE)
VOLUME_DENIALS = ("there is no", "no named-volume", "never a volume",
                  "not a volume")


def _volume_workspace_claims(text: str) -> list[str]:
    """Sentences offering a volume as a way to supply the workspace."""
    flat = re.sub(r"\s+", " ", text)
    claims = []
    for m in VOLUME_MODE_RE.finditer(flat):
        window = flat[max(0, m.start() - 140):m.end() + 140]
        if "workspace" not in window.lower():
            continue  # the sibling-container cache volumes are real
        if any(d in window.lower() for d in VOLUME_DENIALS):
            continue
        claims.append(window.strip())
    return claims


def test_the_workspace_is_a_host_path_and_no_doc_offers_a_volume():
    from ralphd.cli.main import _parse_workspace_specs

    # Code side: the one workspace flag resolves a host directory, so a
    # volume name is a startup error, not a second mode.
    with pytest.raises(SystemExit) as exc:
        _parse_workspace_specs(["ralphd-workspace-volume-that-is-no-dir"])
    assert exc.value.code == 2
    assert _parse_workspace_specs([str(REPO_ROOT)]) == [(None, REPO_ROOT)]
    main_src = CLI_MAIN.read_text()
    assert "volume create" not in main_src and "--mount" not in main_src

    offenders = {}
    for path in DOC_FILES:
        claims = _volume_workspace_claims(path.read_text())
        if claims:
            offenders[str(path.relative_to(REPO_ROOT))] = claims
    assert not offenders, (
        f"docs offer a workspace mode the CLI cannot take: {offenders}")


def test_the_volume_mode_check_would_catch_the_old_overview_bullet():
    assert _volume_workspace_claims(
        "- a **workspace**: the code being worked on (host bind-mount or "
        "named volume)")
    assert _volume_workspace_claims(
        "|  ~/.ralphd/llm-profiles/    |  /workspace (mounted or volume)   |")
    assert _volume_workspace_claims(
        "the workspace arrives as a named volume or bind mount")
    # A cache volume for a sibling toolchain is a different thing entirely,
    # and the corrected bullet names the two real modes.
    assert not _volume_workspace_claims(
        "Mount a **named volume** for the toolchain's caches, or bind-mount "
        "a host dir, so a rebuild does not re-download its dependencies.")
    assert not _volume_workspace_claims(
        "- a **workspace**: the code being worked on, either an existing host "
        "checkout bind-mounted in or a directory the engine creates on the "
        "run dir (there is no named-volume mode)")


# ---- the engine is the console script pyproject installs ------------------

PYPROJECT = REPO_ROOT / "pyproject.toml"
ENGINE_ENTRY_POINT = "ralphd.engine.main"


def _console_scripts() -> dict[str, str]:
    import tomllib

    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["scripts"]


def test_the_overview_names_the_engines_real_console_script():
    scripts = _console_scripts()
    engine = [name for name, target in scripts.items()
              if target.startswith(ENGINE_ENTRY_POINT)]
    assert len(engine) == 1, scripts
    match = re.search(r"the \*\*engine\*\* \(`([A-Za-z0-9_.-]+)`",
                      ARCH_DOC.read_text())
    assert match, "docs/architecture.md's overview no longer names the engine"
    assert match.group(1) == engine[0], (
        f"the overview calls the engine {match.group(1)!r}; the installed "
        f"console script is {engine[0]!r} (the distribution is named "
        f"{'ralphd'!r}, which is not a command)")


# ---- the hub's render rule is stated, and derived from the guard ----------

APP_JS = REPO_ROOT / "src" / "ralphd" / "cli" / "web" / "app.js"

# Task 016 (#27)'s stale-response guard: every render closure re-checks its
# own captured generation against the live one before touching the DOM or
# installing a refresh timer. This is the mechanism the render-rule prose
# (docs/architecture.md §7, task 018) names as *why* the hub "never applies
# a response it did not ask for" -- so the check for the prose's claim is a
# check that this exact guard is still present in both renderers, not a
# check on the prose itself.
RENDER_GUARD_RE = re.compile(r"myGen\s*!==\s*renderGeneration")


def _render_rule_stated(text: str) -> bool:
    low = " ".join(text.lower().split())
    return ("renders decisions python made" in low
            and "never applies a response it did not ask for" in low)


def test_the_render_rule_is_stated_and_the_guard_exists_in_app_js():
    assert _render_rule_stated(ARCH_DOC.read_text()), (
        "docs/architecture.md no longer states the hub's render rule (task "
        "018, #27): that it renders decisions Python made and never applies "
        "a response it did not ask for")
    app_js = APP_JS.read_text()
    hits = RENDER_GUARD_RE.findall(app_js)
    # renderRunList and renderRunDetail each check it before painting the
    # DOM, and again before installing their refresh timer -- at least one
    # hit per renderer's two call sites is the derived evidence for the
    # doc's claim; losing the guard from either renderer breaks the rule
    # the prose asserts, so require it to appear more than once (a single
    # remaining occurrence would mean one renderer regressed to unguarded).
    assert len(hits) >= 2, (
        "app.js no longer carries the myGen !== renderGeneration guard in "
        "both renderers -- the render-rule claim in docs/architecture.md is "
        f"no longer backed by the code (found {len(hits)} occurrence(s))")


def test_the_render_rule_check_would_catch_a_removed_guard():
    """Mutation case: strip the guard from a copy of app.js's text and the
    same check that passes above must fail."""
    app_js = APP_JS.read_text()
    assert RENDER_GUARD_RE.search(app_js), "nothing to strip -- fix the fixture"
    mutated = RENDER_GUARD_RE.sub("true", app_js)
    hits = RENDER_GUARD_RE.findall(mutated)
    assert len(hits) == 0
    assert not (len(hits) >= 2)


def test_the_render_rule_prose_check_would_catch_a_missing_statement():
    assert not _render_rule_stated(
        "the hub is a plain JS client with no build step, talking only to "
        "the JSON endpoints ui_server.py serves")
    assert _render_rule_stated(
        "The hub renders decisions Python made and never applies a "
        "response it did not ask for.")


# ---- a self-dev wave runs the installed engine, not the workspace's ------
# (v0.20.1 requirement E.6)

def _states_installed_engine_runs_selfdev(text: str) -> bool:
    low = " ".join(text.lower().split())
    return (
        "self-dev wave" in low
        and "installed" in low
        and ("not the workspace" in low or "never the tree" in low
             or "not about this engine" in low)
    )


def _states_permissive_then_required(text: str) -> bool:
    low = " ".join(text.lower().split())
    return (
        "permissive" in low
        and "required" in low
        and "wave n" in low
        and ("n+1" in low or "n + 1" in low)
    )


def test_architecture_doc_states_selfdev_runs_the_installed_engine():
    text = ARCH_DOC.read_text()
    assert _states_installed_engine_runs_selfdev(text), (
        "docs/architecture.md no longer states that the engine executing a "
        "self-dev wave is the installed version rather than the "
        "workspace's (v0.20.1 requirement E.6)")


def test_architecture_doc_states_the_permissive_then_required_rule():
    text = ARCH_DOC.read_text()
    assert _states_permissive_then_required(text), (
        "docs/architecture.md no longer states that a newly required "
        "contract/findings field must land permissive in wave N and only "
        "become required in wave N+1 (v0.20.1 requirement E.6)")


def test_selfdev_and_permissive_checks_would_catch_a_missing_statement():
    unrelated = (
        "The hub renders decisions Python made and never applies a "
        "response it did not ask for."
    )
    assert not _states_installed_engine_runs_selfdev(unrelated)
    assert not _states_permissive_then_required(unrelated)
    assert _states_installed_engine_runs_selfdev(
        "A self-dev wave runs the installed engine, never the tree the "
        "current iteration is editing -- not the workspace's copy.")
    assert _states_permissive_then_required(
        "A newly required contract field lands permissive in wave N and "
        "becomes required in wave N+1.")


# ---- a derived-cost figure is a lower bound with no rate entry -----------
# (v0.20.1 requirement F.4)

def _states_derived_figure_is_a_lower_bound(text: str) -> bool:
    low = " ".join(text.lower().split())
    return (
        "lower bound" in low
        and "derived" in low
        and "no rate entry" in low
        and "observable" in low
        # the per-hat observable: a `byPhase` bucket with tokens and no
        # `costDerivedUSD` of its own (plus the marker when it is set)
        and "byphase" in low
        and "costderivedusd" in low
        and "coststatus" in low
        # the per-model observable, and the correction that the published
        # `priceTables` figure is a COUNT and cannot answer per model
        and "rate_for" in low
        and "pricetables.tables[*].models" in low
        and "count" in low
    )


def test_api_doc_states_a_derived_figure_is_a_lower_bound_with_no_rate_entry():
    text = API_DOC.read_text()
    assert _states_derived_figure_is_a_lower_bound(text), (
        "docs/api.md no longer states that a `~$\u2026 derived` figure is a "
        "lower bound when a hat's model has no rate entry, and names the "
        "observable a reader uses to tell (v0.20.1 requirement F.4)")


def test_derived_lower_bound_check_would_catch_a_missing_statement():
    unrelated = (
        "The hub renders decisions Python made and never applies a "
        "response it did not ask for."
    )
    assert not _states_derived_figure_is_a_lower_bound(unrelated)
    # missing the "observable" clause alone must still fail -- the criterion
    # requires naming HOW a reader tells, not just that it can happen.
    assert not _states_derived_figure_is_a_lower_bound(
        "A derived figure is a lower bound when a hat's model has no rate "
        "entry in either pricing table.")
    # naming only `priceTables[*].models` (an integer count) is NOT naming a
    # usable observable -- that is the defect this task's second attempt fixed
    assert not _states_derived_figure_is_a_lower_bound(
        "A derived figure is a lower bound when a hat's model has no rate "
        "entry in either table; the observable a reader uses to tell is "
        "the model's absence from `GET /config`'s `pricing.models` and "
        "`priceTables[*].models`.")
    assert _states_derived_figure_is_a_lower_bound(
        "A derived figure is a lower bound when a hat's model has no rate "
        "entry in either table. The observable: the hat's own byPhase "
        "bucket carries tokens but no costDerivedUSD (and costStatus "
        "partial/unknown when the gap was marked); per model, "
        "pricing_map().rate_for(id) is None, because "
        "priceTables.tables[*].models is only an integer count.")


def test_the_observables_the_derived_lower_bound_passage_names_are_real():
    """The doc's two observables have to WORK, not just be mentioned.

    This is the half the first attempt at requirement F.4 missed: it named
    `priceTables[*].models` as the per-model lookup, and that field is an
    integer count, so a reader could not look a model id up against it. The
    assertions below pin each documented observable against the code that
    produces it, so a doc passage naming a non-answer fails here.
    """
    from ralphd.engine import pricing_aws, state

    # 1. the published `priceTables` figure really is a COUNT, not a listing --
    #    so the doc is right to send a reader elsewhere for per-model coverage
    assert isinstance(pricing_aws.staleness()["models"], int)
    assert pricing_aws.staleness()["models"] == len(pricing_aws.RATES)

    # 2. `rate_for` DOES answer per model id: None for an uncovered id (the
    #    doc's own example), a rate for a covered one, through the alias hop
    priced = pricing_aws.pricing_map()
    assert priced.rate_for("openai/gpt-6-astra-pro") is None
    assert priced.rate_for("aigw-openai/openai.gpt-6-astra-pro") is None
    assert priced.rate_for("aigw-openai/openai.gpt-5.6-sol") is not None

    # 3. the bucket-level observable: an uncovered hat's own bucket ends up
    #    with tokens and no `costDerivedUSD`, and the marked case reads
    #    `unknown` alone / `partial` once mixed with derived money
    no_rate = {"input": 2000, "output": 200, "totalTokens": 2200,
               "costPriced": False, "costDerived": False}
    hat_bucket = LoopSupervisor._merge_usage({}, dict(no_rate))
    assert hat_bucket["totalTokens"] == 2200
    assert "costDerivedUSD" not in hat_bucket
    assert hat_bucket["costStatus"] == "unknown"
    assert state.format_cost(hat_bucket) == state.COST_UNAVAILABLE

    total = LoopSupervisor._merge_usage(
        {}, {"input": 1000, "output": 100, "totalTokens": 1100,
             "costPriced": False, "costDerived": True, "costDerivedUSD": 0.45})
    assert total["costStatus"] == "derived"
    assert state.format_cost(total) == "~$0.45 derived"
    LoopSupervisor._merge_usage(total, dict(no_rate))
    assert total["costStatus"] == "partial"
    assert state.format_cost(total) == "~$0.45 derived, partial (rest unavailable)"
