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
    assert "--model-strategy" in choices and "--on-complete" in choices
    problems = _choice_problems(CLI_DOC.read_text(), choices)
    assert not problems, (
        "docs/cli.md documents flag values argparse would reject (or omits "
        f"ones it accepts): {problems}")


def test_the_choice_set_check_would_catch_the_invented_model_strategy():
    choices = _parser_choices()
    old = ("| `--model-strategy <s>` | quality-first | "
           "`quality-first\\|cost-optimized\\|balanced\\|custom` \u2014 which phase "
           "gets `--model` |")
    assert _choice_problems(old, choices)
    assert "custom" not in choices["--model-strategy"]
    # ...and `custom` is not a preset either: it would silently resolve as
    # quality-first, which is why the doc could not be quietly "made true".
    assert "custom" not in JobConfig.STRATEGY_TIERS
    # the check is not vacuous: the corrected row passes
    assert not _choice_problems(old.replace("\\|custom", ""), choices)


# ---- `runs --state` filters on every state a run dir can record -----------

def _documented_state_filter(text: str) -> set[str]:
    span = re.search(r"--state ([a-z|]+)", text)
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


def test_no_doc_claims_the_browser_reimplements_the_log_rendering():
    """Task 014 moved the rendering server-side; the code, not the prose, is
    the reason no doc may claim otherwise."""
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
    return set(re.findall(r"`([A-Za-z-]+\.md)`",
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


# ---- the model-strategy table is STRATEGY_TIERS ---------------------------

def _documented_strategy_table(text: str) -> dict[str, dict[str, str]]:
    section = _section(text, "### Model strategy")
    header = None
    table: dict[str, dict[str, str]] = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip(" `") for c in line.strip("|").split("|")]
        if header is None:
            header = cells[1:]
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        preset = cells[0].split(" ")[0].strip(" `")
        table[preset] = dict(zip(header, cells[1:]))
    return table


def test_the_documented_model_strategy_presets_are_strategy_tiers():
    assert _documented_strategy_table(ARCH_DOC.read_text()) == \
        JobConfig.STRATEGY_TIERS, (
        "docs/architecture.md's preset table is not JobConfig.STRATEGY_TIERS")


def test_the_strategy_table_check_would_catch_the_old_prose():
    old = ("### Model strategy\n\nPresets: `quality-first` (default; one "
           "strong model everywhere), `cost-optimized` (strong model for "
           "planning only), `balanced` (strong for planning + review).\n")
    assert _documented_strategy_table(old) != JobConfig.STRATEGY_TIERS
    # the prose was wrong about `balanced`, not just vague: reflect is strong
    assert JobConfig.STRATEGY_TIERS["balanced"]["reflect"] == "strong"


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
