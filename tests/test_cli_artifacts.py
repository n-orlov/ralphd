"""Task 023 (#18.3): `ralphctl artifacts` shows what the job left behind.

`artifacts/` is the job's own output tray -- above all the reflect phase's
post-mortem (`reflection/report.md`) and the prompt/skill diff it proposes
(`reflection/suggestions.diff`). Until now the CLI could only *list* the tree
or copy the whole thing somewhere, so reading the one file an operator
actually wanted meant knowing the registry layout and `cat`-ing it.

What is pinned here:

  * the shared shaping (`engine.state.artifact_entries`/`artifact`) and the
    single wordings (`artifact_summary_lines`, `format_artifact_listing`,
    `NO_ARTIFACTS`, `ARTIFACT_BINARY`, `RUN_DOCUMENT_ABSENT`/`_EMPTY`) --
    task 024's hub panel renders the same dicts, so a second vocabulary
    cannot be born;
  * the traversal guard lives in ONE resolver (`artifact_relpath`), because
    task 024 puts that string in a URL: `..`, an absolute path and a NUL are
    not artifact names, in the resolver and through the CLI;
  * `show` extends `ralphctl artifacts` rather than adding a parallel command,
    and `ls`/`pull` keep working (`pull`'s optional destination included);
  * a binary artifact is described, never printed;
  * the on-disk contract: no container, no live API, no snapshot notice.

Tiers: unit (the shaping and the wordings), black-box `ralphctl` subprocesses
over hand-written registries (container gone), and one REAL engine reflect run
whose `artifacts/reflection/` the CLI prints after the engine is stopped.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from ralphd.engine.state import (
    ARTIFACT_ALIASES,
    ARTIFACT_BINARY,
    NO_ARTIFACTS,
    RUN_DOCUMENT_ABSENT,
    RUN_DOCUMENT_EMPTY,
    artifact,
    artifact_body,
    artifact_entries,
    artifact_key,
    artifact_names,
    artifact_relpath,
    artifact_summary_lines,
    artifact_text,
    artifact_title,
    format_artifact_listing,
    format_artifact_size,
)
from tests.conftest import RALPHCTL

REPORT = "# Reflection\n\nThe ladder worked; the reviewer was too strict.\n"
DIFF = "--- a/prompts/worker.md\n+++ b/prompts/worker.md\n@@\n-old\n+new\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00"


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _seed(tmp: Path, run_id: str = "art-run", *, report: str | None = REPORT,
          diff: str | None = DIFF, png: bool = True,
          extra: dict[str, str] | None = None) -> Path:
    """A registry holding one finished run dir, container long gone."""
    registry = tmp / "registry"
    rdir = registry / "runs" / run_id
    adir = rdir / "artifacts"
    (adir / "reflection").mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"runId": run_id,
                                                  "state": "succeeded"}))
    if report is not None:
        (adir / "reflection" / "report.md").write_text(report)
    if diff is not None:
        (adir / "reflection" / "suggestions.diff").write_text(diff)
    if png:
        (adir / "screenshot.png").write_bytes(PNG)
    for rel, body in (extra or {}).items():
        path = adir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return registry


def _empty_registry(tmp: Path, run_id: str = "bare-run") -> Path:
    registry = tmp / "registry"
    (registry / "runs" / run_id / "artifacts").mkdir(parents=True)
    return registry


# ------------------------------------------------------------- unit: resolver
def test_well_known_names_resolve_to_their_paths_both_spellings():
    assert artifact_names() == ["report", "suggestions", "reflect-failed"]
    assert artifact_relpath("report") == "reflection/report.md"
    assert artifact_relpath("suggestions") == "reflection/suggestions.diff"
    assert artifact_relpath("reflect-failed") == "reflection/FAILED.md"
    # the path a listing shows, with or without the directory, is an argument
    assert artifact_relpath("reflection/report.md") == "reflection/report.md"
    assert artifact_relpath("artifacts/reflection/report.md") == \
        "reflection/report.md"
    assert artifact_key("REPORT") == "report"
    assert artifact_key("artifacts/reflection/report.md") == "report"
    assert artifact_key("reports/pricing-anomaly.md") is None
    assert artifact_title("reflection/suggestions.diff")
    assert artifact_title("whatever.txt") == ""


def test_arbitrary_paths_resolve_and_illegal_names_do_not():
    assert artifact_relpath("reports/pricing-anomaly.md") == \
        "reports/pricing-anomaly.md"
    assert artifact_relpath("./notes.txt") == "notes.txt"
    for bad in ("", "   ", None, "..", "../../etc/passwd",
                "reflection/../../escape.md", "/etc/passwd", "a\x00b"):
        assert artifact_relpath(bad) is None, bad


def test_every_alias_path_lives_under_reflection_and_is_unique():
    keys = [k for k, _, _ in ARTIFACT_ALIASES]
    paths = [p for _, p, _ in ARTIFACT_ALIASES]
    assert len(set(keys)) == len(keys) and len(set(paths)) == len(paths)
    assert all(t for _, _, t in ARTIFACT_ALIASES), "every alias needs a purpose"


# --------------------------------------------------------------- unit: shaping
def test_artifact_entries_lists_the_whole_tree_in_path_order(tmp_path):
    registry = _seed(tmp_path, extra={"reports/pricing-anomaly.md": "# rates\n"})
    entries = artifact_entries(registry / "runs" / "art-run")
    assert [e["path"] for e in entries] == [
        "reflection/report.md", "reflection/suggestions.diff",
        "reports/pricing-anomaly.md", "screenshot.png"]
    by_path = {e["path"]: e for e in entries}
    assert by_path["reflection/report.md"]["key"] == "report"
    assert by_path["reflection/report.md"]["bytes"] == len(REPORT)
    assert by_path["reports/pricing-anomaly.md"]["key"] is None
    assert by_path["screenshot.png"]["isText"] is False
    # a listing must not ship the artifacts themselves (the hub polls it)
    assert all("body" not in e for e in entries)


def test_artifact_entries_of_a_run_with_no_artifacts_is_empty(tmp_path):
    assert artifact_entries(_empty_registry(tmp_path) / "runs" / "bare-run") == []
    # not even an artifacts/ dir (a pre-v0.6 or half-created run dir)
    assert artifact_entries(tmp_path / "nowhere") == []


def test_artifact_reads_one_body_and_reports_absence_as_an_answer(tmp_path):
    root = _seed(tmp_path, report=None) / "runs" / "art-run"
    got = artifact(root, "suggestions")
    assert got["exists"] and got["body"] == DIFF and got["isText"]
    missing = artifact(root, "report")
    assert missing["exists"] is False and "body" not in missing
    assert missing["bytes"] == 0 and missing["available"] is True
    assert artifact_body(missing) == RUN_DOCUMENT_ABSENT
    # an illegal name is not an artifact at all -- the caller's usage error
    assert artifact(root, "../../etc/passwd") is None


def test_artifact_body_wordings_cover_blank_and_binary(tmp_path):
    root = _seed(tmp_path, report="   \n") / "runs" / "art-run"
    assert artifact_body(artifact(root, "report")) == RUN_DOCUMENT_EMPTY
    png = artifact(root, "screenshot.png")
    assert png["isText"] is False and "body" not in png
    assert artifact_body(png) == ARTIFACT_BINARY
    assert "pull" in ARTIFACT_BINARY, "say how to get a binary artifact out"


def test_a_multibyte_character_cut_by_the_sniff_window_is_still_text(tmp_path):
    registry = _seed(tmp_path)
    big = registry / "runs" / "art-run" / "artifacts" / "long.md"
    big.write_text("\u00e9" * 6000)  # 12000 bytes, chopped mid-character
    entry = artifact(registry / "runs" / "art-run", "long.md")
    assert entry["isText"] is True and entry["body"].startswith("\u00e9")


def test_listing_and_header_wordings_are_shared_and_size_agrees(tmp_path):
    root = _seed(tmp_path) / "runs" / "art-run"
    entries = artifact_entries(root)
    lines = format_artifact_listing(entries)
    assert lines[0].split() == ["SIZE", "NAME", "PATH"]
    assert len(lines) == len(entries) + 1
    assert "report" in lines[1] and "reflection/report.md" in lines[1]
    assert f"{len(REPORT):,}" in lines[1]
    entry = artifact(root, "report")
    assert format_artifact_size(entry) == f"{len(REPORT):,}"
    assert format_artifact_size(artifact(root, "reflect-failed")) == \
        RUN_DOCUMENT_ABSENT
    head = artifact_summary_lines(entry)
    assert head[0] == "artifact:  reflection/report.md  (report)"
    assert head[1].startswith("purpose:")
    assert head[-1] == f"size:      {len(REPORT):,} bytes"
    # no purpose line invented for a file ralphd knows nothing about
    assert len(artifact_summary_lines(artifact(root, "screenshot.png"))) == 2
    text = artifact_text(entry)
    assert text.startswith(head[0])
    assert "--- reflection/report.md ---" in text
    assert text.endswith(REPORT)
    assert artifact_summary_lines("not a dict") == []


# ------------------------------------------------------------ black-box: ls
def test_ls_labels_the_well_known_names_and_needs_no_container(tmp_path):
    registry = _seed(tmp_path)
    res = _ctl(registry, "artifacts", "art-run", "ls")
    assert res.returncode == 0, res.stderr
    assert "run:       art-run" in res.stdout
    for line in format_artifact_listing(artifact_entries(
            registry / "runs" / "art-run")):
        assert line in res.stdout
    assert "suggestions" in res.stdout and "screenshot.png" in res.stdout
    assert res.stderr == "", "an on-disk read has nothing to warn about"


def test_ls_is_the_default_action(tmp_path):
    registry = _seed(tmp_path)
    assert _ctl(registry, "artifacts", "art-run").stdout == \
        _ctl(registry, "artifacts", "art-run", "ls").stdout


def test_ls_json_carries_the_shaping(tmp_path):
    registry = _seed(tmp_path)
    doc = json.loads(_ctl(registry, "--json", "artifacts", "art-run",
                          "ls").stdout)
    assert doc["runId"] == "art-run"
    assert doc["artifacts"] == artifact_entries(registry / "runs" / "art-run")


def test_ls_of_a_run_with_no_artifacts_says_so(tmp_path):
    registry = _empty_registry(tmp_path)
    res = _ctl(registry, "artifacts", "bare-run", "ls")
    assert res.returncode == 0, res.stderr
    assert NO_ARTIFACTS in res.stdout
    doc = json.loads(_ctl(registry, "--json", "artifacts", "bare-run").stdout)
    assert doc["artifacts"] == []


def test_unknown_run_exits_3_for_every_action(tmp_path):
    registry = _seed(tmp_path)
    for argv in (("artifacts", "ghost", "ls"),
                 ("artifacts", "ghost", "show", "report"),
                 ("artifacts", "ghost", "pull", str(tmp_path / "out"))):
        res = _ctl(registry, *argv)
        assert res.returncode == 3, argv
        assert "not found" in res.stderr


# ---------------------------------------------------------- black-box: show
def test_show_prints_the_reflection_report_inline(tmp_path):
    registry = _seed(tmp_path)
    res = _ctl(registry, "artifacts", "art-run", "show", "report")
    assert res.returncode == 0, res.stderr
    assert res.stdout == ("run:       art-run\n"
                          + artifact_text(artifact(registry / "runs" / "art-run",
                                                   "report")))
    assert REPORT in res.stdout
    assert res.stdout.endswith("\n") and not res.stdout.endswith("\n\n")


def test_show_prints_the_suggested_diff_by_key_and_by_path(tmp_path):
    registry = _seed(tmp_path)
    by_key = _ctl(registry, "artifacts", "art-run", "show", "suggestions")
    by_path = _ctl(registry, "artifacts", "art-run", "show",
                   "reflection/suggestions.diff")
    with_dir = _ctl(registry, "artifacts", "art-run", "show",
                    "artifacts/reflection/suggestions.diff")
    assert by_key.returncode == 0 and DIFF in by_key.stdout
    assert by_key.stdout == by_path.stdout == with_dir.stdout


def test_show_json_carries_body_and_the_same_text(tmp_path):
    registry = _seed(tmp_path)
    doc = json.loads(_ctl(registry, "--json", "artifacts", "art-run", "show",
                          "report").stdout)
    assert doc["runId"] == "art-run" and doc["key"] == "report"
    assert doc["body"] == REPORT
    assert doc["text"] == artifact_text(artifact(registry / "runs" / "art-run",
                                                 "report"))
    assert doc["isText"] is True and doc["bytes"] == len(REPORT)


def test_show_describes_a_binary_artifact_instead_of_printing_it(tmp_path):
    registry = _seed(tmp_path)
    res = _ctl(registry, "artifacts", "art-run", "show", "screenshot.png")
    assert res.returncode == 0, res.stderr
    assert ARTIFACT_BINARY in res.stdout
    assert "PNG" not in res.stdout and "IHDR" not in res.stdout


def test_show_of_an_absent_artifact_exits_1_naming_what_is_on_disk(tmp_path):
    registry = _seed(tmp_path, report=None)
    res = _ctl(registry, "artifacts", "art-run", "show", "report")
    assert res.returncode == 1
    assert "reflection/report.md" in res.stderr
    assert RUN_DOCUMENT_ABSENT in res.stderr
    assert "reflection/suggestions.diff" in res.stderr, "name what IS there"
    assert res.stdout == ""


def test_show_of_an_absent_artifact_in_an_empty_run_says_nothing_is_there(tmp_path):
    res = _ctl(_empty_registry(tmp_path), "artifacts", "bare-run", "show",
               "report")
    assert res.returncode == 1 and "nothing" in res.stderr


def test_show_refuses_an_illegal_name_and_a_missing_one(tmp_path):
    registry = _seed(tmp_path)
    escape = _ctl(registry, "artifacts", "art-run", "show",
                  "../../../etc/passwd")
    assert escape.returncode == 2 and "not an artifact name" in escape.stderr
    assert "report" in escape.stderr, "list the names that do work"
    bare = _ctl(registry, "artifacts", "art-run", "show")
    assert bare.returncode == 2 and "needs a name" in bare.stderr
    # and nothing outside the run dir was read on the way to that refusal
    assert "root:" not in escape.stdout


# ---------------------------------------------------------- black-box: pull
def test_pull_still_copies_the_tree_with_and_without_a_destination(tmp_path):
    registry = _seed(tmp_path)
    dest = tmp_path / "out"
    res = _ctl(registry, "artifacts", "art-run", "pull", str(dest))
    assert res.returncode == 0, res.stderr
    assert (dest / "reflection" / "report.md").read_text() == REPORT
    assert (dest / "screenshot.png").read_bytes() == PNG
    assert json.loads(_ctl(registry, "--json", "artifacts", "art-run", "pull",
                           str(tmp_path / "out2")).stdout)["pulled"] == \
        str(tmp_path / "out2")
    # the default destination is relative to the caller's cwd
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    done = subprocess.run([str(RALPHCTL), "artifacts", "art-run", "pull"],
                          cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    assert (cwd / "artifacts" / "reflection" / "report.md").read_text() == REPORT


# -------------------------------------------------------- real engine tier
def test_real_reflect_run_report_is_printable_after_the_engine_is_gone(live):
    """The fixture is `artifacts/reflection/` as the REFLECT PHASE wrote it:
    `ralphctl artifacts` must list and print it once the engine (the stand-in
    for the container) is gone."""
    r = live(run_id="artreal", job={"iterations": 2, "max_approaches": 1,
                                    "reflect": True, "on_complete": "idle"},
             stub_env={"STUB_TASKS": "1"})
    r.wait_terminal(timeout=90)
    report = r.run_dir / "artifacts" / "reflection" / "report.md"
    deadline = time.time() + 30
    while time.time() < deadline and not report.exists():
        time.sleep(0.2)
    assert report.exists(), "the reflect phase wrote no report to show"
    r.stop()  # container gone: nothing live to ask

    listing = r.ralphctl("artifacts", "artreal", "ls")
    assert listing.returncode == 0, (listing.stdout, listing.stderr)
    assert "reflection/report.md" in listing.stdout
    assert "report" in listing.stdout and "suggestions" in listing.stdout

    shown = r.ralphctl("artifacts", "artreal", "show", "report")
    assert shown.returncode == 0, (shown.stdout, shown.stderr)
    assert report.read_text() in shown.stdout

    diff = r.ralphctl("--json", "artifacts", "artreal", "show", "suggestions")
    doc = json.loads(diff.stdout)
    assert doc["body"] == (r.run_dir / "artifacts" / "reflection"
                           / "suggestions.diff").read_text()
    # the phase succeeded, so its failure note is absent -- a clean exit 1
    failed = r.ralphctl("artifacts", "artreal", "show", "reflect-failed")
    assert failed.returncode == 1 and "FAILED.md" in failed.stderr
