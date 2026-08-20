"""Task 021 (#18.2): `ralphctl docs <run>` -- a run's own state documents.

`notes.md`, `review-findings.md`, `composite-prd.md` and the effective
`job.yaml` have always been sitting in the registry, and no surface printed
them: an operator asking "what did the worker hand over" or "what was this run
actually started with" had to know the registry layout and `cat` the files --
which is precisely how a credential got read out loud twice in this project's
own history (see `engine/redact.py`'s module doc string).

What is pinned here:

  * the shared shaping (`engine.state.run_documents`) and the single wordings
    (`run_document_summary_lines`, `format_run_document_listing`,
    `RUN_DOCUMENT_ABSENT`/`_EMPTY`) -- task 022's hub dialogs render the same
    dicts, so a second vocabulary cannot be born;
  * the redaction of `job.yaml`, mechanically and on BOTH bounds: masked by
    key NAME (`api_token`, a nested `AWS_SECRET_ACCESS_KEY`) and scrubbed by
    VALUE (a real secret smuggled into an innocently-named key), asserted with
    a staged secret value that must never appear in ANY output -- listing,
    body, or `--json`;
  * "which documents exist" is part of the answer: an absent document is a
    listed row saying so, and asking for it exits 1 cleanly (naming which
    documents ARE on disk) rather than printing an empty body;
  * the on-disk contract: no container, no live API, no snapshot notice.

Tiers: unit (the shaping, the wordings, the redactor), black-box `ralphctl`
subprocesses over hand-written registries (container gone), and one run whose
`job.yaml` was written by the real `ralphctl start` (stub docker).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ralphd.cli.llm_profiles import MASK
from ralphd.engine.redact import (
    build_redaction_map,
    config_dir_secrets,
    is_secret_name,
    mask_secret_names,
    redact_job_yaml,
)
from ralphd.engine.state import (
    JOB_CONFIG_FILE,
    RUN_DOCUMENT_ABSENT,
    RUN_DOCUMENT_EMPTY,
    RUN_DOCUMENT_REDACTED_NOTICE,
    format_run_document_listing,
    run_document,
    run_document_body,
    run_document_key,
    run_document_keys,
    run_document_summary_lines,
    run_document_text,
    run_documents,
)
from tests.conftest import RALPHCTL

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"

SECRET = "ghp_liveTokenValue0123456789"
NOTES = "# Handoff notes\n\n- state: 3/7 done\n- next: task 004\n"
FINDINGS = "# Review findings\n\nApproach 1 missed requirement C.\n"
COMPOSITE = "# Composite PRD\n\nOriginal PRD plus findings.\n"


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _job_yaml(job: dict) -> str:
    """The `key: <json>` per line format `ralphctl start` writes."""
    return "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items())


JOB = {
    "run_id": "docs-run",
    "iterations": 25,
    "vigilant": True,
    "api_token": "tok_abcdefgh12345678",
    "model": "amazon-bedrock/eu.anthropic.claude-opus-5",
    "on_complete_cmd": f"curl -H 'Authorization: Bearer {SECRET}' https://ci",
    "env": {"AWS_SECRET_ACCESS_KEY": "AKIAsecretstuff9999", "AWS_REGION": "eu-west-1"},
    "fast_model": None,
}


def _seed(tmp: Path, run_id: str = "docs-run", *, notes: bool = True,
          findings: bool = True, composite: bool = False,
          job: dict | None = None, creds: bool = True) -> Path:
    """A registry holding one run dir + its config dir, container long gone."""
    registry = tmp / "registry"
    rdir = registry / "runs" / run_id
    cdir = registry / "configs" / run_id
    (rdir / "iterations").mkdir(parents=True)
    cdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"runId": run_id, "state": "failed"}))
    (rdir / "prd.md").write_text("# Original PRD\n")
    if notes:
        (rdir / "notes.md").write_text(NOTES)
    if findings:
        (rdir / "review-findings.md").write_text(FINDINGS)
    if composite:
        (rdir / "composite-prd.md").write_text(COMPOSITE)
    (cdir / JOB_CONFIG_FILE).write_text(_job_yaml(JOB if job is None else job))
    if creds:
        (cdir / "creds").mkdir()
        (cdir / "creds" / "github.env").write_text(f"GITHUB_TOKEN={SECRET}\n")
    return registry


# ---------------------------------------------------------------- unit: shape
def test_run_documents_lists_every_known_document_present_or_not(tmp_path):
    registry = _seed(tmp_path)
    docs = run_documents(registry / "runs" / "docs-run",
                         registry / "configs" / "docs-run")
    assert [d["key"] for d in docs] == run_document_keys() == [
        "notes", "findings", "composite-prd", "job"]
    by_key = {d["key"]: d for d in docs}
    assert by_key["notes"]["exists"] and by_key["notes"]["body"] == NOTES
    assert by_key["notes"]["bytes"] == len(NOTES)
    assert by_key["findings"]["exists"]
    # absent is a ROW, not a dropped entry -- "which exist" is the answer
    assert by_key["composite-prd"]["exists"] is False
    assert "body" not in by_key["composite-prd"]
    assert by_key["composite-prd"]["available"] is True
    assert by_key["job"]["where"] == "config" and by_key["job"]["exists"]


def test_run_documents_without_a_config_dir_says_out_of_reach_not_missing(tmp_path):
    """A caller holding only a run dir must not claim `job.yaml` is missing."""
    registry = _seed(tmp_path)
    docs = {d["key"]: d for d in run_documents(registry / "runs" / "docs-run")}
    assert docs["job"]["available"] is False
    assert docs["job"]["exists"] is False
    assert "path" not in docs["job"]
    assert docs["notes"]["exists"] is True


def test_bodies_false_omits_every_body(tmp_path):
    registry = _seed(tmp_path, composite=True)
    docs = run_documents(registry / "runs" / "docs-run",
                         registry / "configs" / "docs-run", bodies=False)
    assert all("body" not in d for d in docs)
    assert all(d["exists"] for d in docs)


def test_run_document_accepts_key_or_file_name(tmp_path):
    registry = _seed(tmp_path)
    root = registry / "runs" / "docs-run"
    cdir = registry / "configs" / "docs-run"
    assert run_document_key("notes") == "notes"
    assert run_document_key("notes.md") == "notes"
    assert run_document_key("REVIEW-FINDINGS.MD") == "findings"
    assert run_document_key("job.yaml") == "job"
    assert run_document_key("prd.md") is None  # the PRD has its own surface
    assert run_document(root, "notes.md", cdir)["key"] == "notes"
    assert run_document(root, "bogus", cdir) is None


def test_run_document_body_wordings(tmp_path):
    registry = _seed(tmp_path, notes=False)
    root = registry / "runs" / "docs-run"
    (root / "review-findings.md").write_text("   \n")
    assert run_document_body(run_document(root, "notes")) == RUN_DOCUMENT_ABSENT
    # exists but blank is EMPTY, not absent -- two different facts
    assert run_document_body(run_document(root, "findings")) == RUN_DOCUMENT_EMPTY


def test_summary_lines_and_text_are_the_one_rendering(tmp_path):
    registry = _seed(tmp_path)
    doc = run_document(registry / "runs" / "docs-run", "notes")
    lines = run_document_summary_lines(doc)
    assert lines[0] == "document:  notes  (notes.md)"
    assert f"size:      {len(NOTES):,} bytes" in lines
    assert RUN_DOCUMENT_REDACTED_NOTICE not in "\n".join(lines)
    text = run_document_text(doc)
    assert text.startswith("\n".join(lines))
    assert text.endswith(NOTES)
    assert "--- notes.md ---" in text


def test_listing_marks_absent_documents_and_names_every_key(tmp_path):
    registry = _seed(tmp_path)
    docs = run_documents(registry / "runs" / "docs-run",
                         registry / "configs" / "docs-run", bodies=False)
    lines = format_run_document_listing(docs)
    assert lines[0].split() == ["DOCUMENT", "FILE", "SIZE", "DESCRIPTION"]
    assert len(lines) == 1 + len(run_document_keys())
    composite = next(x for x in lines if x.startswith("composite-prd"))
    assert RUN_DOCUMENT_ABSENT in composite
    assert next(x for x in lines if x.startswith("notes")).split()[2] == f"{len(NOTES):,}"


# ------------------------------------------------------------ unit: redaction
def test_is_secret_name_covers_the_shapes_that_leaked():
    assert is_secret_name("api_token")
    assert is_secret_name("AWS_SECRET_ACCESS_KEY")
    assert is_secret_name("AWS_BEARER_TOKEN_BEDROCK")
    assert is_secret_name("password")
    assert not is_secret_name("model")
    assert not is_secret_name("iterations")


def test_mask_secret_names_keeps_shape_and_leaves_nulls_alone():
    doc = {"model": "x", "api_token": "abc", "env": {"OPENAI_API_KEY": "k",
                                                     "AWS_REGION": "eu"},
           "fast_model": None, "keys": [{"token": "t"}]}
    masked = mask_secret_names(doc)
    assert masked["model"] == "x"
    assert masked["api_token"] == MASK
    assert masked["env"] == {"OPENAI_API_KEY": MASK, "AWS_REGION": "eu"}
    # "not set" is not a secret: masking a None would claim a value exists
    assert masked["fast_model"] is None
    # a secret-shaped key holding a list masks the whole subtree
    assert masked["keys"] == MASK


def test_redact_job_yaml_masks_by_name_and_scrubs_by_value(tmp_path):
    registry = _seed(tmp_path)
    cdir = registry / "configs" / "docs-run"
    text = redact_job_yaml((cdir / JOB_CONFIG_FILE).read_text(), config_dir=cdir)
    assert SECRET not in text
    assert JOB["api_token"] not in text
    assert "AKIAsecretstuff9999" not in text
    # masked by name, scrubbed by value -- both bounds visible in the output
    assert 'api_token: "***REDACTED***"' in text
    assert "[REDACTED:github.env:GITHUB_TOKEN]" in text
    # everything not secret survives verbatim, key order and shape included
    assert text.splitlines()[0] == 'run_id: "docs-run"'
    assert "iterations: 25" in text
    assert '"AWS_REGION": "eu-west-1"' in text
    assert "amazon-bedrock/eu.anthropic.claude-opus-5" in text


def test_redact_job_yaml_passes_an_unparseable_line_through(tmp_path):
    """A hand-edited job.yaml must stay readable; the value scrub still runs."""
    cdir = tmp_path / "configs" / "x"
    (cdir / "creds").mkdir(parents=True)
    (cdir / "creds" / "a.env").write_text(f"TOKEN={SECRET}\n")
    text = redact_job_yaml("iterations: not json\nplain line\n"
                           f"note: leaked {SECRET} here\n", config_dir=cdir)
    assert "iterations: not json" in text
    assert "plain line" in text
    assert SECRET not in text


def test_config_dir_secrets_reads_every_staged_source(tmp_path):
    cdir = tmp_path / "configs" / "x"
    (cdir / "creds").mkdir(parents=True)
    (cdir / "pi").mkdir()
    (cdir / "creds" / "a.env").write_text("GITHUB_TOKEN=cred_value_123456\n")
    (cdir / "creds" / "git-credentials").write_text(
        "https://u:gitcred_value_123456@github.com\n")
    (cdir / "creds" / "netrc").write_text("machine x login u password netrc_value_1234\n")
    (cdir / "llm-wiring.json").write_text(json.dumps(
        {"env": {"AWS_BEARER_TOKEN_BEDROCK": "wiring_value_123456"}}))
    (cdir / "env-wiring.json").write_text(json.dumps(
        {"extra_env": ["EXTRA_TOKEN=extra_value_123456"]}))
    (cdir / "pi" / "models.json").write_text(json.dumps(
        {"providers": [{"apiKey": "models_value_123456", "name": "aigw"}]}))
    values = set(config_dir_secrets(cdir))
    assert values == {"cred_value_123456", "gitcred_value_123456",
                      "netrc_value_1234", "wiring_value_123456",
                      "extra_value_123456", "models_value_123456"}
    # never the names, never a short value, never a non-secret field
    assert "aigw" not in values


def test_redaction_also_covers_this_hosts_own_env(tmp_path, monkeypatch):
    """Defense in depth: a value this host forwards is scrubbed even when the
    run's config dir never staged it (`build_redaction_map`)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host_env_secret_value_1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    assert "host_env_secret_value_1" in build_redaction_map()
    text = redact_job_yaml('on_complete_cmd: "run host_env_secret_value_1"\n')
    assert "host_env_secret_value_1" not in text


# --------------------------------------------------------- black-box: ralphctl
def test_docs_lists_the_documents_of_a_dead_run(tmp_path):
    registry = _seed(tmp_path)
    res = _ctl(registry, "docs", "docs-run")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert lines[0] == "run:       docs-run"
    assert lines[1].split() == ["DOCUMENT", "FILE", "SIZE", "DESCRIPTION"]
    assert [line.split()[0] for line in lines[2:]] == run_document_keys()
    assert RUN_DOCUMENT_ABSENT in next(x for x in lines if x.startswith("composite-prd"))
    # a listing never prints a body, so it can never print a secret either
    assert SECRET not in res.stdout
    assert "3/7 done" not in res.stdout


def test_docs_prints_one_document_body(tmp_path):
    registry = _seed(tmp_path, composite=True)
    for name, body in (("notes", NOTES), ("findings", FINDINGS),
                       ("composite-prd", COMPOSITE), ("notes.md", NOTES)):
        res = _ctl(registry, "docs", "docs-run", name)
        assert res.returncode == 0, res.stderr
        assert res.stdout.startswith("run:       docs-run\ndocument:  ")
        assert res.stdout.endswith(body)


def test_docs_job_yaml_never_prints_a_staged_secret(tmp_path):
    registry = _seed(tmp_path)
    res = _ctl(registry, "docs", "docs-run", "job")
    assert res.returncode == 0, res.stderr
    assert SECRET not in res.stdout and SECRET not in res.stderr
    assert JOB["api_token"] not in res.stdout
    assert "AKIAsecretstuff9999" not in res.stdout
    assert RUN_DOCUMENT_REDACTED_NOTICE in res.stdout
    assert MASK in res.stdout
    assert "iterations: 25" in res.stdout
    # --json is the same redacted bytes, not a raw back door
    res = _ctl(registry, "--json", "docs", "docs-run", "job.yaml")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert SECRET not in res.stdout
    assert doc["key"] == "job" and doc["redacted"] is True
    assert doc["text"].endswith(doc["body"])
    assert doc["runId"] == "docs-run"


def test_docs_json_listing_carries_every_row_without_bodies(tmp_path):
    registry = _seed(tmp_path)
    res = _ctl(registry, "--json", "docs", "docs-run")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["runId"] == "docs-run"
    assert [d["key"] for d in doc["documents"]] == run_document_keys()
    assert all("body" not in d for d in doc["documents"])
    assert {d["key"]: d["exists"] for d in doc["documents"]} == {
        "notes": True, "findings": True, "composite-prd": False, "job": True}


def test_docs_absent_document_exits_1_naming_what_is_on_disk(tmp_path):
    registry = _seed(tmp_path, findings=False)
    res = _ctl(registry, "docs", "docs-run", "review-findings.md")
    assert res.returncode == 1
    assert "review-findings.md" in res.stderr
    assert RUN_DOCUMENT_ABSENT in res.stderr
    assert "notes" in res.stderr and "job" in res.stderr
    assert res.stdout == ""


def test_docs_unknown_document_exits_2_and_unknown_run_exits_3(tmp_path):
    registry = _seed(tmp_path)
    res = _ctl(registry, "docs", "docs-run", "secrets")
    assert res.returncode == 2
    for key in run_document_keys():
        assert key in res.stderr
    res = _ctl(registry, "docs", "no-such-run")
    assert res.returncode == 3


def test_docs_never_creates_anything_in_the_run_dir(tmp_path):
    """A read-only viewer: the run dir and config dir are untouched (no
    `RunDir` construction, which would mkdir into somebody else's run)."""
    registry = _seed(tmp_path)
    rdir, cdir = registry / "runs" / "docs-run", registry / "configs" / "docs-run"
    before = ({p.name for p in rdir.iterdir()}, {p.name for p in cdir.iterdir()})
    assert _ctl(registry, "docs", "docs-run").returncode == 0
    assert _ctl(registry, "docs", "docs-run", "job").returncode == 0
    assert ({p.name for p in rdir.iterdir()}, {p.name for p in cdir.iterdir()}) == before


def test_docs_reads_a_run_with_no_documents_at_all(tmp_path):
    """A run that died in planning: every row says so, nothing crashes."""
    registry = tmp_path / "registry"
    (registry / "runs" / "bare" / "iterations").mkdir(parents=True)
    (registry / "configs" / "bare").mkdir(parents=True)
    res = _ctl(registry, "docs", "bare")
    assert res.returncode == 0, res.stderr
    assert res.stdout.count(RUN_DOCUMENT_ABSENT) == len(run_document_keys())
    res = _ctl(registry, "docs", "bare", "job")
    assert res.returncode == 1
    assert "none of them" in res.stderr


# ------------------------------------------ a job.yaml written by `start` itself
def test_job_yaml_written_by_real_start_prints_redacted(tmp_path):
    """End to end on the file `ralphctl start` actually writes: a staged
    credential plus a secret inlined into an innocently-named key
    (`on_complete_cmd`) -- neither may come back out of `ralphctl docs`."""
    registry = tmp_path / "registry"
    registry.mkdir()
    prd = tmp_path / "prd.md"
    prd.write_text("# PRD\n")
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "github.env").write_text(f"GITHUB_TOKEN={SECRET}\n")
    env = {**os.environ, "RALPHD_DOCKER": str(STUB_DOCKER),
           "RALPHD_REGISTRY": str(registry),
           "STUB_DOCKER_LOG": str(tmp_path / "docker-argv.jsonl")}
    res = subprocess.run(
        [str(RALPHCTL), "start", "--prd", str(prd), "--llm", "none",
         "--run-id", "started", "--creds", str(creds), "--iterations", "7",
         "--on-complete-cmd", f"curl -H 'Authorization: Bearer {SECRET}' https://ci"],
        env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    raw = (registry / "configs" / "started" / JOB_CONFIG_FILE).read_text()
    assert SECRET in raw  # the file on disk really does hold it

    res = _ctl(registry, "docs", "started", "job")
    assert res.returncode == 0, res.stderr
    assert SECRET not in res.stdout
    assert "iterations: 7" in res.stdout
    assert "[REDACTED:" in res.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="posix permissions")
def test_unreadable_document_says_so_instead_of_crashing(tmp_path):
    registry = _seed(tmp_path)
    notes = registry / "runs" / "docs-run" / "notes.md"
    notes.chmod(0o000)
    try:
        res = _ctl(registry, "docs", "docs-run", "notes")
        if os.geteuid() != 0:  # root can read anything; nothing to assert
            assert res.returncode == 0, res.stderr
            assert "(unreadable)" in res.stdout
    finally:
        notes.chmod(0o644)
