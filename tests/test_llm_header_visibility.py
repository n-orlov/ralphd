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
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_cli_llm_profiles import Ctl  # noqa: F401 (fixture factory reused)
from test_cli_ui import _write_dead_run

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


def test_read_llm_wiring_headers_empty_for_a_pre_047_wiring_file(tmp_path):
    """A run started before this task (headers present, no
    `headerRefNames` key at all) renders every header as literal -- the
    same behaviour it always showed, never a crash on the missing key."""
    cdir = tmp_path / "cdir"
    cdir.mkdir()
    (cdir / "llm-wiring.json").write_text(json.dumps({
        "mode": "host", "env": {}, "mounts": [], "headers": {"X-Old": "v"}}))
    assert llm_profiles.read_llm_wiring_headers(cdir) == {"X-Old": "v"}


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
