"""Task 030 (requirement P part 3): taking SOME of a reflection diff.

Task 041 built `ralphctl suggestions` (show, then write on an explicit `--yes`)
and task 029 gave it its two refusals. What was still missing is the ordinary
operator need: v0.6's `suggestions.diff` was eight suggestions, of which two were
wrong, so "all of it or none of it" means retyping the good six by hand -- the
very thing requirement P exists to end.

The answer pinned here is a **selection by whole file, made before a plan
exists** (`patch.select_patches`, then `plan_patch`), which is the opposite of a
half-finished write: what is selected is planned, shown and written
all-or-nothing, and what is left out is printed beside it so a partial apply
announces itself. Sub-file selection is refused, with the reason (a `@@` header
is not a stable name -- the same file's hunks renumber as soon as one applies).

What is pinned here:

  * the core criterion: a two-file diff applied with `--only` produces exactly
    the selected file's change and leaves the other file byte-identical;
  * the selection is announced (`selected:  1 of 2 files`, the `--- left out by
    --only ---` block, `--json`'s `only`/`skipped`/`paths`), and the whole diff
    is still shown, so nothing is hidden by selecting;
  * a `--only` path the diff does not touch is a usage error (exit 2) naming the
    paths it does touch, and writes nothing -- never a quiet apply of the rest;
  * all-or-nothing still holds *within* the selection, and a stale file that was
    left out no longer blocks the clean one;
  * `show --only` writes nothing; `apply --only` without `--yes` writes nothing;
  * selection narrows the conflict check (a sibling diff sharing nothing with
    the selection is no conflict) but never the mid-flight refusal;
  * CLI parity: the hub surfaces no suggestions plan/apply/select endpoint at
    all -- it can read the diff as an artifact and nothing more;
  * the doc claims in docs/cli.md, SPEC §10.1 and SPEC §17 entry 2, each with a
    mutation case.

Tiers: in-process tables over `patch.select_patches`, black-box `ralphctl`
subprocesses over a hand-built registry (stub docker for the liveness question,
no engine or container anywhere), and one real `ralphctl ui` server for parity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ralphd.cli import image, main as cli_main, patch, ui_server
from tests.conftest import call_ralphctl_in_process
from tests.test_cli_suggestions_verb import (CLEAN_DIFF, WORKER_MD, _checkout,
                                             _doc_section)

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import UiServer, _write_dead_run, ui  # noqa: E402

__all__ = ["UiServer", "ui"]

REPO = Path(__file__).resolve().parents[1]
CLI_DOC = REPO / "docs" / "cli.md"
SPEC = REPO / "SPEC.md"
DOC_SECTION = "### `ralphctl suggestions"
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"

WORKER = "src/ralphd/prompts/worker.md"
PLANNING = "src/ralphd/prompts/planning.md"
PLANNING_MD = "# Role: Planner\n\nBreak the PRD down.\n"

# The shape this task is about: several independent suggestions in one diff.
# Built out of task 041's CLEAN_DIFF so the worker.md half is the same literal
# every other module argues about.
PLANNING_HUNK = """--- a/src/ralphd/prompts/planning.md
+++ b/src/ralphd/prompts/planning.md
@@ -1,3 +1,4 @@
 # Role: Planner
 
+Measure the test suite once.
 Break the PRD down.
"""
TWO_FILE_DIFF = CLEAN_DIFF + PLANNING_HUNK

# Well-formed, and its context is not in the tree: the file an operator would
# want to leave OUT of the apply.
STALE_PLANNING = """--- a/src/ralphd/prompts/planning.md
+++ b/src/ralphd/prompts/planning.md
@@ -1,2 +1,3 @@
 # Role: Something Else
 A line that is not in the file.
+Measure the test suite once.
"""
TWO_FILE_ONE_STALE = CLEAN_DIFF + STALE_PLANNING

# Another run proposing an edit to the file this apply leaves out: a conflict
# for the whole diff, and none at all for a `--only worker.md` selection.
OTHER_PLANNING_DIFF = PLANNING_HUNK.replace("Measure the test suite once.",
                                            "Measure the suite once.")
OTHER_WORKER_DIFF = CLEAN_DIFF.replace("Read the notes file first.",
                                       "Read the standing rules first.")


# ------------------------------------------------------------------ harness
def _registry(tmp: Path) -> Path:
    reg = tmp / "registry"
    (reg / "runs").mkdir(parents=True)
    return reg


def _run(reg: Path, run_id: str, *, state: str = "succeeded",
         diff: str | None = None, host: dict | None = None) -> Path:
    rdir = reg / "runs" / run_id
    (rdir / "artifacts" / "reflection").mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"runId": run_id,
                                                  "state": state}))
    if diff is not None:
        (rdir / "artifacts" / "reflection" / "suggestions.diff").write_text(diff)
    if host is not None:
        (rdir / "host.json").write_text(json.dumps(host))
    return rdir


def _ctl(reg: Path, *argv: str, live: str = "", timeout: int = 60):
    def names(ids: str) -> str:
        return ",".join(cli_main.job_container_name(i)
                        for i in ids.split(",") if i)
    env = {**os.environ, "RALPHD_REGISTRY": str(reg),
           "RALPHD_DOCKER": str(STUB_DOCKER),
           "STUB_DOCKER_CONTAINERS": names(live),
           "STUB_DOCKER_RUNNING": names(live)}
    return call_ralphctl_in_process(list(argv), env=env, capsys=_CAPSYS)


@pytest.fixture(autouse=True)
def _capture_cli_output(capsys):
    """Task 038 (F.3): drive `ralphctl` in-process (task 036's helper) --
    module-global capsys, never concurrent."""
    global _CAPSYS
    _CAPSYS = capsys
    yield
    _CAPSYS = None


_CAPSYS = None


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A checkout whose two prompt files are exactly what TWO_FILE_DIFF's
    context expects."""
    t = _checkout(tmp_path / "checkout")
    (t / PLANNING).write_text(PLANNING_MD)
    return t


def _hash(tree: Path) -> str:
    return image.hash_image_inputs(tree).hash


# ------------------------------------------------- the selection, in process

def test_no_selection_means_the_whole_diff():
    """Absence of a selection is not an empty selection: no `--only` must plan
    every file the diff names."""
    patches = patch.parse_patch(TWO_FILE_DIFF)
    for wanted in (None, (), []):
        selected, unknown = patch.select_patches(patches, wanted)
        assert [p.path for p in selected] == [WORKER, PLANNING]
        assert unknown == ()


def test_selection_keeps_only_the_named_file():
    patches = patch.parse_patch(TWO_FILE_DIFF)
    selected, unknown = patch.select_patches(patches, [PLANNING])
    assert [p.path for p in selected] == [PLANNING]
    assert unknown == ()


def test_a_name_pasted_out_of_the_diff_selects_the_file():
    """`a/src/...`/`b/src/...` is what the operator is looking at in the diff
    body, so it selects the path the plan calls `src/...`."""
    patches = patch.parse_patch(TWO_FILE_DIFF)
    for spelling in (f"a/{WORKER}", f"b/{WORKER}", f"  {WORKER} "):
        selected, unknown = patch.select_patches(patches, [spelling])
        assert [p.path for p in selected] == [WORKER], spelling
        assert unknown == ()


def test_selection_is_in_the_diffs_order_not_the_operators():
    patches = patch.parse_patch(TWO_FILE_DIFF)
    selected, _ = patch.select_patches(patches, [PLANNING, WORKER])
    assert [p.path for p in selected] == [WORKER, PLANNING]


def test_an_unknown_name_is_reported_not_ignored():
    patches = patch.parse_patch(TWO_FILE_DIFF)
    selected, unknown = patch.select_patches(patches, [WORKER, "src/nope.md"])
    assert unknown == ("src/nope.md",)
    assert [p.path for p in selected] == [WORKER]


def test_hunk_count_label_is_worded_once():
    assert patch.hunk_count_label(1) == "1 hunk"
    assert patch.hunk_count_label(3) == "3 hunks"


# --------------------------------------------- apply: exactly the selection

def test_apply_only_writes_exactly_the_selected_file(tmp_path, tree):
    """The criterion: a subset is selected out of a multi-suggestion diff and
    produces exactly the selected changes and nothing else."""
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    r = _ctl(reg, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree), "--only", WORKER)
    assert r.returncode == 0, r.stderr
    assert (tree / WORKER).read_text() == \
        "# Role: Worker\n\nRead the notes file first.\nOne task per iteration.\n"
    # the file that was left out is byte-identical: nothing else moved
    assert (tree / PLANNING).read_text() == PLANNING_MD
    assert f"wrote   {WORKER}" in r.stdout
    assert f"wrote   {PLANNING}" not in r.stdout


def test_a_partial_apply_announces_itself(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    r = _ctl(reg, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree), "--only", WORKER)
    assert r.returncode == 0, r.stderr
    assert "selected:  1 of 2 files (--only)" in r.stdout
    assert cli_main.SUGGESTIONS_ONLY_HEADER in r.stdout
    assert f"  skip  {PLANNING}  (1 hunk)" in r.stdout
    # the plan itself lists only the selection
    assert f"modify  {WORKER}  (1 hunk)" in r.stdout
    assert f"modify  {PLANNING}" not in r.stdout


def test_selecting_hides_nothing_the_diff_proposed(tmp_path, tree):
    """The whole diff is still printed: the operator sees both suggestions and
    the fact that one of them is not being taken."""
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    r = _ctl(reg, "suggestions", "sug-run", "--target", str(tree),
             "--only", WORKER)
    assert r.returncode == 0, r.stderr
    assert "+Read the notes file first." in r.stdout
    assert "+Measure the test suite once." in r.stdout


def test_two_only_flags_select_two_files(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    r = _ctl(reg, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree), "--only", WORKER, "--only", PLANNING)
    assert r.returncode == 0, r.stderr
    assert "selected:  2 of 2 files (--only)" in r.stdout
    assert cli_main.SUGGESTIONS_ONLY_HEADER not in r.stdout
    assert "Read the notes file first." in (tree / WORKER).read_text()
    assert "Measure the test suite once." in (tree / PLANNING).read_text()


def test_show_with_a_selection_writes_nothing(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    before = _hash(tree)
    r = _ctl(reg, "suggestions", "sug-run", "show", "--target", str(tree),
             "--only", WORKER)
    assert r.returncode == 0, r.stderr
    assert _hash(tree) == before
    assert (tree / WORKER).read_text() == WORKER_MD


def test_apply_with_a_selection_and_no_yes_writes_nothing(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    before = _hash(tree)
    r = _ctl(reg, "suggestions", "sug-run", "apply", "--target", str(tree),
             "--only", WORKER)
    assert r.returncode == 2, r.stdout + r.stderr
    assert _hash(tree) == before
    assert cli_main.SUGGESTIONS_NEEDS_YES in r.stdout
    assert f"modify  {WORKER}  (1 hunk)" in r.stdout


# ------------------------------------------------------ the refusals it keeps

def test_a_path_the_diff_does_not_touch_is_a_usage_error(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    before = _hash(tree)
    r = _ctl(reg, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree), "--only", "src/ralphd/prompts/nope.md")
    assert r.returncode == 2, r.stdout + r.stderr
    assert cli_main.SUGGESTIONS_ONLY_UNKNOWN in r.stderr
    assert "src/ralphd/prompts/nope.md" in r.stderr
    # the answer is the list of what the diff DOES touch
    assert WORKER in r.stderr and PLANNING in r.stderr
    assert _hash(tree) == before


def test_a_hunk_selector_is_refused_with_the_documented_workflow(tmp_path, tree):
    """`--only path:12` is the plausible attempt at hunk granularity; it is not
    a path in the diff, so it lands in the same refusal, which says hunks are
    not separately selectable and what to do instead."""
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    r = _ctl(reg, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree), "--only", f"{WORKER}:12")
    assert r.returncode == 2, r.stdout + r.stderr
    assert cli_main.SUGGESTIONS_ONLY_WORKFLOW in r.stderr
    assert "hunks are not separately selectable" in r.stderr


def test_all_or_nothing_still_holds_inside_the_selection(tmp_path, tree):
    """Selecting a file whose hunks no longer fit is refused with the hunk
    named -- a selection is not a licence to write what applies."""
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_ONE_STALE)
    before = _hash(tree)
    r = _ctl(reg, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree), "--only", PLANNING)
    assert r.returncode == 1, r.stdout + r.stderr
    assert cli_main.SUGGESTIONS_REFUSED in r.stderr
    assert patch.REASON_NOT_FOUND in r.stderr
    assert _hash(tree) == before


def test_leaving_out_the_stale_file_lets_the_clean_one_land(tmp_path, tree):
    """The other half of the same diff: the whole thing is refused, and the
    selection that drops the stale suggestion applies cleanly."""
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_ONE_STALE)
    whole = _ctl(reg, "suggestions", "sug-run", "apply", "--yes",
                 "--target", str(tree))
    assert whole.returncode == 1, whole.stdout
    assert (tree / WORKER).read_text() == WORKER_MD
    part = _ctl(reg, "suggestions", "sug-run", "apply", "--yes",
                "--target", str(tree), "--only", WORKER)
    assert part.returncode == 0, part.stderr
    assert "Read the notes file first." in (tree / WORKER).read_text()
    assert (tree / PLANNING).read_text() == PLANNING_MD


def test_a_conflict_on_a_file_left_out_is_not_a_conflict_for_this_apply(
        tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=TWO_FILE_DIFF)
    _run(reg, "theirs", diff=OTHER_PLANNING_DIFF)
    whole = _ctl(reg, "suggestions", "mine", "apply", "--yes",
                 "--target", str(tree))
    assert whole.returncode == 1, whole.stdout
    assert cli_main.SUGGESTIONS_CONFLICT_REFUSED in whole.stderr
    part = _ctl(reg, "suggestions", "mine", "apply", "--yes",
                "--target", str(tree), "--only", WORKER)
    assert part.returncode == 0, part.stderr
    assert "Read the notes file first." in (tree / WORKER).read_text()


def test_a_conflict_on_the_selected_file_is_still_refused(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=TWO_FILE_DIFF)
    _run(reg, "theirs", diff=OTHER_WORKER_DIFF)
    r = _ctl(reg, "suggestions", "mine", "apply", "--yes",
             "--target", str(tree), "--only", WORKER)
    assert r.returncode == 1, r.stdout
    assert cli_main.SUGGESTIONS_CONFLICT_REFUSED in r.stderr
    assert (tree / WORKER).read_text() == WORKER_MD


def test_a_selection_never_overrides_the_midflight_refusal(tmp_path, tree):
    """The live-run refusal is about the TREE, not about which of its files
    move, so selecting one file does not make the write acceptable."""
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=TWO_FILE_DIFF)
    _run(reg, "live", state="running",
         host={"runId": "live", "workspace": str(tree)})
    r = _ctl(reg, "suggestions", "mine", "apply", "--yes",
             "--target", str(tree), "--only", WORKER, live="live")
    assert r.returncode == 1, r.stdout
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED in r.stderr
    assert (tree / WORKER).read_text() == WORKER_MD


def test_json_carries_the_selection(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "sug-run", diff=TWO_FILE_DIFF)
    r = _ctl(reg, "--json", "suggestions", "sug-run", "--target", str(tree),
             "--only", WORKER)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["only"] == [WORKER]
    assert payload["skipped"] == [PLANNING]
    assert payload["paths"] == [WORKER]
    assert payload["applies"] is True and payload["applied"] is False
    plain = _ctl(reg, "--json", "suggestions", "sug-run", "--target", str(tree))
    whole = json.loads(plain.stdout)
    assert whole["only"] == [] and whole["skipped"] == []
    assert whole["paths"] == [WORKER, PLANNING]


# --------------------------------------------------------------- CLI parity

def test_the_hub_exposes_no_plan_apply_or_select_endpoint(tmp_path, ui):
    """CLI parity: the hub surfaces the verb not at all, so it cannot expose
    more than the CLI. Writing the material the loop runs on stays an act typed
    at a terminal."""
    registry = tmp_path / "hub-registry"
    rdir = _write_dead_run(registry, "sug-run")
    (rdir / "artifacts" / "reflection").mkdir(parents=True)
    (rdir / "artifacts" / "reflection" / "suggestions.diff").write_text(
        TWO_FILE_DIFF)
    server = ui(registry)
    for path in ("/api/runs/sug-run/suggestions",
                 "/api/runs/sug-run/suggestions/apply",
                 "/api/runs/sug-run/patch"):
        code, _ = server.get(path)
        assert code == 404, path
        code, _ = server.post(path, {"apply": True, "yes": True})
        assert code == 404, path
    # ...while the diff itself is readable, read-only, as an artifact
    code, payload = server.get(
        "/api/runs/sug-run/artifacts/reflection/suggestions.diff")
    assert code == 200
    assert "+Read the notes file first." in payload["text"]
    # nothing was written by any of that
    assert (rdir / "artifacts" / "reflection"
            / "suggestions.diff").read_text() == TWO_FILE_DIFF


def test_the_hub_module_cannot_plan_select_or_write_a_diff():
    """Derived from the source, not from a route list: the hub never reaches the
    planner at all, which is why there is nothing for it to expose."""
    src = Path(ui_server.__file__).read_text()
    for forbidden in ("cli import patch", "cli.patch", "apply_plan",
                      "apply_text", "plan_text", "plan_patch",
                      "select_patches"):
        assert forbidden not in src, forbidden


# ------------------------------------------------------------- doc claims
DOC_CLAIMS = ("--only PATH", "subset is selected by file, up front",
              "left out by --only", "before the plan is built",
              "Hunks are not separately selectable", "usage error",
              "only` and `skipped", "written, applied, only, skipped",
              "narrows the conflict check",
              "hub does not surface this verb at all",
              "tests/test_cli_suggestions_subset.py")


def _doc_problems(section: str) -> list[str]:
    return [claim for claim in DOC_CLAIMS if claim not in section]


def test_docs_cli_states_how_a_subset_is_taken():
    section = _doc_section(CLI_DOC.read_text(), DOC_SECTION)
    assert not _doc_problems(section), _doc_problems(section)
    # the headings and wordings are the module's constants, not retyped copies
    assert cli_main.SUGGESTIONS_ONLY_HEADER in section


@pytest.mark.parametrize("claim", DOC_CLAIMS)
def test_the_doc_check_catches_every_dropped_claim(claim):
    section = _doc_section(CLI_DOC.read_text(), DOC_SECTION)
    assert _doc_problems(section.replace(claim, "")) == [claim]


def test_the_docs_synopsis_names_the_selection_flag():
    heading = _doc_section(CLI_DOC.read_text(), DOC_SECTION).splitlines()[0]
    assert "--only" in heading


def test_the_spec_command_row_names_the_subset():
    rows = [line for line in SPEC.read_text().splitlines()
            if line.startswith("| `suggestions ")]
    assert len(rows) == 1, rows
    assert "--only" in rows[0]


def test_spec_answered_question_two_settles_partial_application():
    """§17 entry 2 held the question open, pointing at this task. It must now
    carry the answer, not the pointer."""
    text = SPEC.read_text()
    entry = text[text.index("## 17. Answered questions"):]
    entry = entry[entry.index("2. **Should the reflection diff"):]
    entry = entry[:entry.index("\n## ")] if "\n## " in entry else entry
    assert "--only PATH" in entry
    assert "part 3" in entry
    assert "Hunk-level selection is refused" in entry
    # the deferral this task closes is gone
    assert "What this entry does not settle: taking six of eight" not in text


def test_the_spec_module_row_names_the_selection_helper():
    rows = [line for line in SPEC.read_text().splitlines()
            if line.startswith("| `src/ralphd/cli/patch.py`")]
    assert len(rows) == 1, rows
    assert "select_patches" in rows[0]
