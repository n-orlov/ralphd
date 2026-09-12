"""Task 047 (#177, PRD requirement J.4): "visible, redacted" -- a run's own
`--llm-header` map is shown on FOUR surfaces (`ralphctl llm show` for a
profile's own headers, `ralphctl status`, the redacted `docs job.yaml`, and
the hub's run header under the model line), and on every one of them a
value that came from `${env:}`/`${file:}`/`${cmd:}` renders as `***`
(`llm_profiles.MASK`) while a literal header value (a plain ticket id)
stays visible.

One reader, `llm_profiles.read_llm_wiring_headers`, decides the masking
for three of the four surfaces (status, docs, hub) from the SAME
`<cdir>/llm-wiring.json` `headers`/`headerRefNames` pair `cli.main.
_write_llm_wiring` persists at `start`/`resume` -- pinned here so a
header's redaction cannot read differently on one surface than another.
The fourth surface (`llm show <profile>`) was already covered for the
profile-level `headers:` shorthand by task 046's own tests
(`test_cli_llm_profiles.py`); this module adds the header-specific
assertion (name + masked/literal value both visible in the printed `pi:`
dump) so all four are pinned in one place.

Two things this module's first take got wrong and now pins explicitly:

* A wiring file written BEFORE this task (task-045-era: `${env:}` headers
  already resolved into `headers`, no `headerRefNames` key at all) carries
  no provenance, and reading its values as literals published the resolved
  secret on `status`/the hub/`job.yaml`. Provenance is now recorded
  whenever `headers` is non-empty -- an EMPTY list when every value is a
  literal -- so a missing key means "pre-047, unknown" and every value of
  such a file renders as `***` on every surface (the NAME still shows).
* The hub row is asserted in the real rendered page too, not only in the
  `run_detail` payload: a `@pytest.mark.browser` node here drives Chromium
  through the same `playwright-cli` harness `tests/test_browser_hub.py`
  uses and reads the summary card's own rows, so "under the model line"
  is checked where the operator actually reads it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from test_browser_hub import SCREENSHOTS_DIR, _wait_for, pw  # noqa: F401 (browser fixture)
from test_cli_llm_profiles import Ctl  # noqa: F401 (fixture factory reused)
from test_cli_start_nodetach import _live_env, _wait_for_supervisor
from test_cli_ui import UiServer, _write_dead_run

from ralphd.cli import llm_profiles
from ralphd.cli.llm_profiles import MASK
from ralphd.cli.ui_server import run_detail


@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


# ------------------------------------------------------- unit: ref tracking
def test_resolve_header_specs_tracks_which_names_are_ref_derived():
    ref_names: set[str] = set()
    out = llm_profiles.resolve_header_specs(
        ["X-Jira-ID=EST-1", "X-Token=${env:MY_TOKEN}"],
        host_env={"MY_TOKEN": "top-secret-value"}, ref_names=ref_names)
    assert out == {"X-Jira-ID": "EST-1", "X-Token": "top-secret-value"}
    assert ref_names == {"X-Token"}


def test_resolve_header_specs_a_later_literal_removes_the_ref_flag():
    """Last-one-wins for the ref flag too: a later literal spec for a name
    already marked ref-derived clears the flag, matching `out`'s own
    last-one-wins rule for the value itself."""
    ref_names: set[str] = set()
    out = llm_profiles.resolve_header_specs(
        ["X-Source=${env:SRC}", "X-Source=literal-now"],
        host_env={"SRC": "secret"}, ref_names=ref_names)
    assert out == {"X-Source": "literal-now"}
    assert ref_names == set()


# ------------------------------------------------- unit: read_llm_wiring_headers
def test_read_llm_wiring_headers_masks_ref_derived_leaves_literal(tmp_path):
    cdir = tmp_path / "cdir"
    cdir.mkdir()
    (cdir / "llm-wiring.json").write_text(json.dumps({
        "mode": "acme", "env": {}, "mounts": [],
        "headers": {"X-Jira-ID": "EST-6588", "X-Token": "resolved-secret-value"},
        "headerRefNames": ["X-Token"],
    }))
    headers = llm_profiles.read_llm_wiring_headers(cdir)
    assert headers == {"X-Jira-ID": "EST-6588", "X-Token": MASK}
    assert "resolved-secret-value" not in json.dumps(headers)


def test_read_llm_wiring_headers_empty_for_no_wiring_file(tmp_path):
    cdir = tmp_path / "cdir"
    cdir.mkdir()
    assert llm_profiles.read_llm_wiring_headers(cdir) == {}


def test_read_llm_wiring_headers_masks_every_value_of_a_pre_047_wiring_file(tmp_path):
    """A run started before this task (headers present, no `headerRefNames`
    key at all) records NO provenance: its `${env:}` headers were resolved
    into `headers` and nothing says which ones were references. Rendering
    those as literals is exactly how the first take of this task leaked a
    resolved secret onto `status`/the hub, so every value of such a file is
    masked and only the NAMES stay visible -- never a crash on the missing
    key, and never a guess that a resolved secret was a literal."""
    cdir = tmp_path / "cdir"
    cdir.mkdir()
    (cdir / "llm-wiring.json").write_text(json.dumps({
        "mode": "host", "env": {}, "mounts": [],
        "headers": {"X-Old": "v", "X-Token": "resolved-secret-from-env"}}))
    headers = llm_profiles.read_llm_wiring_headers(cdir)
    assert headers == {"X-Old": MASK, "X-Token": MASK}
    assert "resolved-secret-from-env" not in json.dumps(headers)


def test_read_llm_wiring_headers_keeps_literals_visible_for_an_all_literal_file(tmp_path):
    """The empty `headerRefNames` list is what distinguishes "recorded, no
    refs" from "never recorded" -- an all-literal post-047 file therefore
    still shows its values, which is why the wholesale masking above is
    safe rather than a blanket redaction of every header everywhere."""
    cdir = tmp_path / "cdir"
    cdir.mkdir()
    (cdir / "llm-wiring.json").write_text(json.dumps({
        "mode": "host", "env": {}, "mounts": [],
        "headers": {"X-Jira-ID": "EST-7"}, "headerRefNames": []}))
    assert llm_profiles.read_llm_wiring_headers(cdir) == {"X-Jira-ID": "EST-7"}


def test_start_records_an_empty_ref_name_list_for_literal_only_headers(ctl):
    """`start` writes the provenance key whenever it writes `headers`, so a
    file that omits it can only be a pre-047 one."""
    ctl.write_profile("acme-lit", {"model": "acme/big", "env": {}})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-lit",
                  "--llm-header", "X-Jira-ID=EST-7", "--run-id", "tst-lit-hdr")
    assert res.returncode == 0, res.stderr
    doc = json.loads(
        ctl.config_dir("tst-lit-hdr").joinpath("llm-wiring.json").read_text())
    assert doc["headerRefNames"] == []
    assert llm_profiles.read_llm_wiring_headers(
        ctl.config_dir("tst-lit-hdr")) == {"X-Jira-ID": "EST-7"}


# --------------------------------------------------------- task 010, D.1
def test_read_llm_wiring_headers_returns_a_profile_provided_header_with_no_flag(ctl):
    """Task 010 (#14, PRD requirement D.1): a profile's own top-level
    `headers:` shorthand, resolved with NO `--llm-header` flag on the
    command line at all, is recorded in the same `llm-wiring.json` map
    `--llm-header` itself populates -- so `llm_profiles.
    read_llm_wiring_headers`, the ONE reader `status`/the hub/the redacted
    `job.yaml` all share, returns its name and value rather than `{}`
    (the pre-fix defect this task's own PRD gate names: the header reached
    the container's `pi/models.json` but never the wiring/display map)."""
    ctl.write_profile("acme-noflag", {
        "model": "acme/big",
        "headers": {"X-Jira-ID": "PUBLIC-123"},
        "pi": {"providers": {"acme": {"baseUrl": "https://a.example.com"}}},
    })
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-noflag",
                  "--run-id", "tst-profile-hdr-noflag")
    assert res.returncode == 0, res.stderr
    cdir = ctl.config_dir("tst-profile-hdr-noflag")

    wiring = json.loads(cdir.joinpath("llm-wiring.json").read_text())
    assert wiring["headers"] == {"X-Jira-ID": "PUBLIC-123"}
    assert wiring["headerRefNames"] == []

    headers = llm_profiles.read_llm_wiring_headers(cdir)
    assert headers == {"X-Jira-ID": "PUBLIC-123"}

    # The container's own `pi/models.json` still carries it too (the
    # PRD gate's own "proving it is effective" check) -- this task adds
    # the wiring record beside that, never in place of it.
    models_doc = json.loads(cdir.joinpath("pi", "models.json").read_text())
    assert models_doc["providers"]["acme"]["headers"]["X-Jira-ID"] == "PUBLIC-123"


def test_read_llm_wiring_headers_still_masks_a_pre_047_file_after_task_010(tmp_path):
    """The wholesale-masking node for a provenance-unknown wiring file
    (task 047 re-take) is untouched by this task's profile-header wiring:
    a file with `headers` and no `headerRefNames` key at all still masks
    every value, profile-derived recording changes only how `start`
    populates a FRESH file, never how an old one is read."""
    cdir = tmp_path / "cdir"
    cdir.mkdir()
    (cdir / "llm-wiring.json").write_text(json.dumps({
        "mode": "acme", "env": {}, "mounts": [],
        "headers": {"X-Jira-ID": "PUBLIC-123", "X-Token": "resolved-secret-from-env"}}))
    headers = llm_profiles.read_llm_wiring_headers(cdir)
    assert headers == {"X-Jira-ID": MASK, "X-Token": MASK}
    assert "resolved-secret-from-env" not in json.dumps(headers)


# --------------------------------------------------------- task 011, D.2
def test_profile_shorthand_header_reaches_status_hub_and_job_yaml_with_no_flag(ctl):
    """End to end through the REAL CLI, a real run (task 011, PRD
    requirement D.2): a profile's own top-level `headers:` shorthand,
    started with NO `--llm-header` flag on the command line at all, shows
    its name and value on all THREE surfaces this task's own gate names --
    `ralphctl --json status`, the hub's `run_detail` payload, and the
    redacted `ralphctl docs job.yaml` view -- not merely on the reader
    function task 010's own tests called directly
    (`read_llm_wiring_headers`).

    Uses the STUB_DOCKER_LIVE_ENGINE knob (same mechanism as
    `tests/test_cli_start_nodetach.py`/`tests/test_llm_header_e2e.py`) so
    `status.json` is written by a REAL `ralphd-engine` process reaching a
    real terminal state, not fabricated by a test helper -- `ralphctl
    status` genuinely has something on disk (and, before the supervisor's
    kill, a live API) to read.
    """
    ctl.write_profile("acme-d2", {
        "model": "acme/big",
        "headers": {"X-Jira-ID": "PUBLIC-123"},
        "pi": {"providers": {"acme": {"baseUrl": "https://a.example.com"}}},
    })
    run_id = "tst-profile-hdr-e2e"
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-d2",
                  "--run-id", run_id, "--on-complete", "idle",
                  "--iterations", "10", "--max-approaches", "1",
                  env={**_live_env(), "STUB_TASKS": "1", "STUB_SLEEP": "0"})
    assert res.returncode == 0, res.stderr
    run_dir = ctl.registry / "runs" / run_id
    sup = _wait_for_supervisor(run_dir)
    assert sup["killed"] is True  # the run really did reach a terminal state

    # surface: `ralphctl --json status`
    json_res = ctl.run("--json", "status", run_id)
    assert json_res.returncode == 0, json_res.stderr
    status_doc = json.loads(json_res.stdout)
    assert status_doc["llmHeaders"] == {"X-Jira-ID": "PUBLIC-123"}

    # surface: the hub's `run_detail` payload
    detail = run_detail(ctl.registry, run_id)
    assert detail is not None
    assert detail["status"]["llmHeaders"] == {"X-Jira-ID": "PUBLIC-123"}

    # surface: the redacted `ralphctl docs job.yaml` view
    doc_res = ctl.run("docs", run_id, "job.yaml")
    assert doc_res.returncode == 0, doc_res.stderr
    assert "X-Jira-ID" in doc_res.stdout
    assert "PUBLIC-123" in doc_res.stdout


# ------------------------------------------------------- surface 1: llm show
def test_llm_show_profile_prints_header_name_and_masks_ref_derived_value(ctl):
    ctl.write_profile("acme-hdr", {
        "model": "acme/big",
        "headers": {"X-Jira-ID": "${env:JIRA_ID}", "X-Common": "shared-literal"},
        "pi": {"providers": {"acme": {"baseUrl": "https://a.example.com"}}},
    })
    res = ctl.run("llm", "show", "acme-hdr",
                  env={"JIRA_ID": "top-secret-jira-value"})
    assert res.returncode == 0, res.stderr
    assert "top-secret-jira-value" not in res.stdout
    assert "***" in res.stdout
    assert "X-Jira-ID" in res.stdout
    assert "X-Common" in res.stdout
    assert "shared-literal" in res.stdout  # a literal header value stays visible

    res_json = ctl.run("--json", "llm", "show", "acme-hdr",
                       env={"JIRA_ID": "top-secret-jira-value"})
    doc = json.loads(res_json.stdout)
    providers = doc["pi"]["providers"]
    assert providers["acme"]["headers"]["X-Jira-ID"] == MASK
    assert providers["acme"]["headers"]["X-Common"] == "shared-literal"
    assert "top-secret-jira-value" not in res_json.stdout


# --------------------------------------------------------- surface 2: status
def _seed_status_run(registry: Path, run_id: str, *, headers: dict,
                     ref_names: list[str]) -> None:
    _write_dead_run(registry, run_id, model="acme/big-model")
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    (cdir / "llm-wiring.json").write_text(json.dumps({
        "mode": "acme", "env": {}, "mounts": [],
        "headers": headers, "headerRefNames": ref_names}))


def test_status_shows_headers_under_the_model_line_masked_and_literal(ctl):
    _seed_status_run(ctl.registry, "tst-status-hdr",
                     headers={"X-Jira-ID": "EST-6588", "X-Token": "resolved-secret"},
                     ref_names=["X-Token"])
    text_res = ctl.run("status", "tst-status-hdr")
    assert text_res.returncode == 0, text_res.stderr
    assert "resolved-secret" not in text_res.stdout
    model_idx = text_res.stdout.index("model:")
    headers_idx = text_res.stdout.index("llm headers:")
    assert headers_idx > model_idx  # under the model line, not above it
    assert "X-Jira-ID: EST-6588" in text_res.stdout
    assert "X-Token: " + MASK in text_res.stdout

    json_res = ctl.run("--json", "status", "tst-status-hdr")
    doc = json.loads(json_res.stdout)
    assert doc["llmHeaders"] == {"X-Jira-ID": "EST-6588", "X-Token": MASK}
    assert "resolved-secret" not in json_res.stdout


def test_status_omits_headers_block_for_a_run_with_none(ctl):
    _write_dead_run(ctl.registry, "tst-status-nohdr", model="acme/big-model")
    res = ctl.run("status", "tst-status-nohdr")
    assert res.returncode == 0, res.stderr
    assert "llm headers:" not in res.stdout
    json_res = ctl.run("--json", "status", "tst-status-nohdr")
    assert json.loads(json_res.stdout)["llmHeaders"] == {}


def test_status_masks_a_pre_047_runs_headers_wholesale(ctl):
    """The same provenance-unknown rule, end to end on the surface that
    consumes the reader: a task-045-era run's resolved `${env:}` value is
    never printed, while its header NAME still is."""
    _write_dead_run(ctl.registry, "tst-status-old", model="acme/big-model")
    cdir = ctl.registry / "configs" / "tst-status-old"
    cdir.mkdir(parents=True)
    (cdir / "llm-wiring.json").write_text(json.dumps({
        "mode": "acme", "env": {}, "mounts": [],
        "headers": {"X-Token": "resolved-secret-from-env"}}))
    res = ctl.run("status", "tst-status-old")
    assert res.returncode == 0, res.stderr
    assert "resolved-secret-from-env" not in res.stdout
    assert "X-Token: " + MASK in res.stdout
    json_res = ctl.run("--json", "status", "tst-status-old")
    assert json.loads(json_res.stdout)["llmHeaders"] == {"X-Token": MASK}
    assert "resolved-secret-from-env" not in json_res.stdout


# ----------------------------------------------------- surface 3: docs job.yaml
def test_docs_job_yaml_lists_header_names_masked_and_literal_values(ctl):
    """`start`'s own job.yaml `llm_headers` key (task 047) is a DISPLAY
    copy -- ref-derived values already replaced by MASK at write time --
    so `ralphctl docs <run> job.yaml`'s mechanical redaction on top of it
    never needs to see the real value at all, and the real value is never
    even written to job.yaml in the first place."""
    ctl.write_profile("acme-docs", {
        "model": "acme/big", "env": {},
        "pi": {"providers": {"acme": {"baseUrl": "https://a.example.com"}}},
    })
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-docs",
                  "--llm-header", "X-Jira-ID=EST-9",
                  "--llm-header", "X-Token=${env:MY_TOKEN}",
                  "--run-id", "tst-docs-hdr",
                  env={"MY_TOKEN": "must-never-appear-anywhere"})
    assert res.returncode == 0, res.stderr

    doc_res = ctl.run("docs", "tst-docs-hdr", "job.yaml")
    assert doc_res.returncode == 0, doc_res.stderr
    assert "must-never-appear-anywhere" not in doc_res.stdout
    assert "X-Jira-ID" in doc_res.stdout
    assert "EST-9" in doc_res.stdout
    assert "X-Token" in doc_res.stdout
    assert MASK in doc_res.stdout

    # The real value never even reaches disk in job.yaml (only the
    # display copy does) -- read the raw file directly to be sure.
    job_yaml_text = ctl.config_dir("tst-docs-hdr").joinpath("job.yaml").read_text()
    assert "must-never-appear-anywhere" not in job_yaml_text
    assert MASK in job_yaml_text


def test_docs_job_yaml_json_listing_carries_the_masked_headers(ctl):
    ctl.write_profile("acme-docs2", {"model": "acme/big", "env": {}})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-docs2",
                  "--llm-header", "X-Ticket=EST-2", "--run-id", "tst-docs-hdr2")
    assert res.returncode == 0, res.stderr
    doc_res = ctl.run("--json", "docs", "tst-docs-hdr2", "job.yaml")
    assert doc_res.returncode == 0, doc_res.stderr
    doc = json.loads(doc_res.stdout)
    assert "X-Ticket" in doc["text"]
    assert "EST-2" in doc["text"]


# ----------------------------------------------------------- surface 4: hub
def test_hub_run_detail_attaches_masked_headers_from_wiring_file(tmp_path):
    registry = tmp_path / "registry"
    run_id = "tst-hub-hdr"
    _write_dead_run(registry, run_id, model="acme/big-model")
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    cdir.joinpath("llm-wiring.json").write_text(json.dumps({
        "mode": "acme", "env": {}, "mounts": [],
        "headers": {"X-Jira-ID": "EST-1", "X-Token": "resolved-secret"},
        "headerRefNames": ["X-Token"]}))

    detail = run_detail(registry, run_id)
    assert detail is not None
    assert detail["status"]["llmHeaders"] == {"X-Jira-ID": "EST-1", "X-Token": MASK}
    assert "resolved-secret" not in json.dumps(detail)


def test_hub_run_detail_empty_llm_headers_for_a_run_with_no_wiring(tmp_path):
    registry = tmp_path / "registry"
    run_id = "tst-hub-nohdr"
    _write_dead_run(registry, run_id)
    detail = run_detail(registry, run_id)
    assert detail is not None
    assert detail["status"]["llmHeaders"] == {}


def test_hub_run_detail_masks_a_pre_047_runs_headers_wholesale(tmp_path):
    registry = tmp_path / "registry"
    run_id = "tst-hub-old"
    _write_dead_run(registry, run_id, model="acme/big-model")
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    cdir.joinpath("llm-wiring.json").write_text(json.dumps({
        "mode": "acme", "env": {}, "mounts": [],
        "headers": {"X-Token": "resolved-secret-from-env"}}))
    detail = run_detail(registry, run_id)
    assert detail is not None
    assert detail["status"]["llmHeaders"] == {"X-Token": MASK}
    assert "resolved-secret-from-env" not in json.dumps(detail)


# --------------------------------------------------------- resume ref tracking
def test_resume_llm_header_updates_ref_names_last_one_wins(ctl):
    """A literal `--llm-header` at resume for a name the ORIGINAL start
    had marked ref-derived clears that flag going forward, and a brand
    new ref-derived name at resume is added -- the persisted
    `headerRefNames` set follows the same last-one-wins rule the header
    VALUES themselves already do (task 045)."""
    ctl.write_profile("acme-resume", {"model": "acme/big", "env": {}})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-resume",
                  "--llm-header", "X-Was-Ref=${env:SRC}",
                  "--run-id", "tst-resume-hdr", env={"SRC": "orig-secret"})
    assert res.returncode == 0, res.stderr
    wiring_before = json.loads(
        ctl.config_dir("tst-resume-hdr").joinpath("llm-wiring.json").read_text())
    assert wiring_before.get("headerRefNames") == ["X-Was-Ref"]

    res2 = ctl.run("resume", "tst-resume-hdr",
                   "--llm-header", "X-Was-Ref=now-literal",
                   "--llm-header", "X-New-Ref=${env:SRC2}",
                   env={"SRC2": "new-secret"})
    assert res2.returncode == 0, res2.stderr
    wiring_after = json.loads(
        ctl.config_dir("tst-resume-hdr").joinpath("llm-wiring.json").read_text())
    assert wiring_after["headers"]["X-Was-Ref"] == "now-literal"
    assert wiring_after["headers"]["X-New-Ref"] == "new-secret"
    assert set(wiring_after.get("headerRefNames") or []) == {"X-New-Ref"}

    display = llm_profiles.read_llm_wiring_headers(ctl.config_dir("tst-resume-hdr"))
    assert display == {"X-Was-Ref": "now-literal", "X-New-Ref": MASK}


def test_resume_keeps_a_pre_047_headers_provenance_unknown(ctl):
    """A `resume --llm-header` over a task-045-era wiring file records
    provenance for the name it sets and keeps the pre-existing names
    masked: `_read_llm_wiring` reports every header of a file with no
    `headerRefNames` key as ref-derived, so re-persisting the merged map
    cannot silently promote an old resolved secret to "literal"."""
    ctl.write_profile("acme-old", {"model": "acme/big", "env": {}})
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "acme-old",
                  "--run-id", "tst-resume-old")
    assert res.returncode == 0, res.stderr
    cdir = ctl.config_dir("tst-resume-old")
    # rewrite the persisted wiring into the pre-047 shape (headers, no
    # provenance key at all) -- exactly what a task-045-era run left behind
    cdir.joinpath("llm-wiring.json").write_text(json.dumps({
        "mode": "acme-old", "env": {}, "mounts": [],
        "headers": {"X-Token": "resolved-secret-from-env"}}))

    res2 = ctl.run("resume", "tst-resume-old", "--llm-header", "X-Jira-ID=EST-3")
    assert res2.returncode == 0, res2.stderr
    wiring_after = json.loads(cdir.joinpath("llm-wiring.json").read_text())
    assert wiring_after["headers"] == {"X-Token": "resolved-secret-from-env",
                                       "X-Jira-ID": "EST-3"}
    assert set(wiring_after["headerRefNames"]) == {"X-Token"}
    assert llm_profiles.read_llm_wiring_headers(cdir) == {
        "X-Token": MASK, "X-Jira-ID": "EST-3"}


# ------------------------------------------- surface 4, in the real browser
def _summary_rows(session) -> list[str]:
    """The run-detail summary card's own rows, top to bottom, as text."""
    raw = session.eval_js(
        "[...document.querySelectorAll('div.card p > div')]"
        ".map(d => d.textContent).join('\\n')")
    return [line for line in raw.strip('"').split("\\n") if line]


@pytest.mark.browser
@pytest.mark.skipif(shutil.which("playwright-cli") is None,
                    reason="playwright-cli not on PATH")
def test_browser_hub_run_header_shows_headers_under_the_model_line(tmp_path, pw):
    """The hub half of this task, asserted in the RENDERED page rather than
    in `run_detail`'s payload: a real Chromium loads the run view and the
    summary card shows one `header: <name>` row per header, positioned
    after the `model:` row, with the ref-derived value as `***` and the
    literal one intact -- and the resolved secret nowhere in the page."""
    registry = tmp_path / "registry"
    run_id = "run-llm-headers"
    _write_dead_run(registry, run_id, state="succeeded", model="acme/big-model")
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    cdir.joinpath("llm-wiring.json").write_text(json.dumps({
        "mode": "acme", "env": {}, "mounts": [],
        "headers": {"X-Jira-ID": "EST-6588", "X-Token": "resolved-secret-from-env"},
        "headerRefNames": ["X-Token"]}))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run_id}")
        body_text = _wait_for(pw, "document.body.innerText", "header: X-Token")
        assert "resolved-secret-from-env" not in body_text, body_text
        page_html = pw.eval_js("document.documentElement.outerHTML")
        assert "resolved-secret-from-env" not in page_html

        rows = _summary_rows(pw)
        model_row = next(i for i, r in enumerate(rows) if r.startswith("model: "))
        token_row = next(i for i, r in enumerate(rows)
                         if r.startswith("header: X-Token: "))
        jira_row = next(i for i, r in enumerate(rows)
                        if r.startswith("header: X-Jira-ID: "))
        assert model_row < token_row and model_row < jira_row, rows
        assert rows[token_row] == "header: X-Token: " + MASK, rows[token_row]
        assert rows[jira_row] == "header: X-Jira-ID: EST-6588", rows[jira_row]
        assert rows[model_row].startswith("model: acme/big-model"), rows[model_row]
        pw.screenshot(SCREENSHOTS_DIR / "20-llm-headers-under-model-line.png")
    finally:
        server.stop()
