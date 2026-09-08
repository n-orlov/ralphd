"""Task 041 (requirement P part 1b): `ralphctl suggestions` shows a run's
reflection diff and what it would touch, and writes only on an explicit act.

Task 028 built the planner (`ralphd.cli.patch`: parse, plan, one all-or-nothing
writer). This is the operator's surface over it, and its whole safety property
is an ordering: **the diff and the plan are printed before anything is
written**, always, including on the run that goes on to write. A verb that
applied sight-unseen is precisely what SPEC §16 was avoiding when it left the
reflection diff unapplied.

What is pinned here:

  * `show` (the default action) prints the diff *and* the exact list of files it
    would touch, and leaves the target tree byte-identical -- proven by the
    image content hash (`cli/image.py`'s `IMAGE_INPUTS`), which is the thing
    that decides which image the next `start` runs;
  * `apply --yes` writes and the image content hash moves; `apply` without
    `--yes` shows the same plan, writes nothing and exits 2;
  * the diff was shown *before* the writer ran -- asserted twice: by the order
    of the markers in the output, and in-process by capturing what had already
    been printed at the moment `patch.apply_plan` was called;
  * a diff that does not fit is refused with the file and the `@@` header of
    every failing hunk on stderr, exit 1, nothing written -- from `show` too;
  * the wordings are the module's constants, not retyped copies, and the verb
    never touches docker or an API (on-disk, like `ralphctl docs`/`artifacts`);
  * the doc claims: docs/cli.md's section and SPEC §10.1's row, each with a
    mutation case, plus the retirement of the "never applied" claim that this
    verb makes false.

Tiers: black-box `ralphctl` subprocesses over a hand-built registry (no
container anywhere), plus two in-process tests for the ordering witness and the
no-checkout default.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ralphd.cli import image, main as cli_main, patch
from ralphd.engine.state import ARTIFACT_ALIASES
from tests.conftest import call_ralphctl_in_process

REPO = Path(__file__).parent.parent
CLI_DOC = REPO / "docs" / "cli.md"
SPEC = REPO / "SPEC.md"
DOC_SECTION = "### `ralphctl suggestions"

WORKER_MD = "# Role: Worker\n\nOne task per iteration.\n"

# The shape a reflect phase actually writes: `git diff` headers with `a/`/`b/`
# prefixes over a prompt file. One literal, so every test below argues about the
# same diff instead of its own dialect.
CLEAN_DIFF = """diff --git a/src/ralphd/prompts/worker.md b/src/ralphd/prompts/worker.md
--- a/src/ralphd/prompts/worker.md
+++ b/src/ralphd/prompts/worker.md
@@ -1,3 +1,4 @@
 # Role: Worker
 
+Read the notes file first.
 One task per iteration.
"""

# Well-formed, and its context is not in the tree: the "the diff is out of date"
# case, which must be a named rejection rather than a guess.
STALE_DIFF = """--- a/src/ralphd/prompts/worker.md
+++ b/src/ralphd/prompts/worker.md
@@ -1,2 +1,3 @@
 # Role: Something Else
 A line that is not in the file.
+Read the notes file first.
"""

# Not a diff at all: `parse_patch` refuses it as text, naming the line.
UNREADABLE_DIFF = "@@ -1,2 +1,3 @@\n a\n+b\n"


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
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


def _registry(tmp: Path, diff: str | None = CLEAN_DIFF,
              run_id: str = "sug-run") -> Path:
    """A registry holding one finished run dir, container long gone."""
    registry = tmp / "registry"
    rdir = registry / "runs" / run_id
    (rdir / "artifacts" / "reflection").mkdir(parents=True)
    (rdir / "status.json").write_text(
        json.dumps({"runId": run_id, "state": "succeeded"}))
    (rdir / "artifacts" / "reflection" / "report.md").write_text("# Reflection\n")
    if diff is not None:
        (rdir / "artifacts" / "reflection" / "suggestions.diff").write_text(diff)
    return registry


def _checkout(dest: Path) -> Path:
    """A copy of this checkout's image inputs: the tree a reflection diff is
    applied to, and the tree whose content hash decides which image the next
    `start` builds and runs."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel in image.IMAGE_INPUTS:
        src = REPO / rel
        if src.is_dir():
            shutil.copytree(src, dest / rel, dirs_exist_ok=True)
        else:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    (dest / "src/ralphd/prompts/worker.md").write_text(WORKER_MD)
    return dest


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    return _checkout(tmp_path / "checkout")


# --------------------------------------------------------- show: nothing written

def test_show_prints_the_diff_and_the_files_it_would_touch(tmp_path, tree):
    registry = _registry(tmp_path)
    r = _ctl(registry, "suggestions", "sug-run", "--target", str(tree))
    assert r.returncode == 0, r.stderr
    # the diff, verbatim
    assert cli_main.SUGGESTIONS_DIFF_HEADER in r.stdout
    assert "+Read the notes file first." in r.stdout
    assert "@@ -1,3 +1,4 @@" in r.stdout
    # ...and the exact list of paths, from the planner's own shaping
    assert cli_main.SUGGESTIONS_PLAN_HEADER in r.stdout
    assert "modify  src/ralphd/prompts/worker.md  (1 hunk)" in r.stdout
    # the run and the target are named, so a plan cannot be read out of context
    assert "run:       sug-run" in r.stdout
    assert str(tree) in r.stdout


def test_show_is_the_default_action_and_leaves_the_tree_byte_identical(
        tmp_path, tree):
    registry = _registry(tmp_path)
    before = image.hash_image_inputs(tree)
    bare = _ctl(registry, "suggestions", "sug-run", "--target", str(tree))
    named = _ctl(registry, "suggestions", "sug-run", "show",
                 "--target", str(tree))
    assert bare.returncode == named.returncode == 0
    assert bare.stdout == named.stdout
    assert (tree / "src/ralphd/prompts/worker.md").read_text() == WORKER_MD
    assert image.hash_image_inputs(tree).digest == before.digest
    assert cli_main.SUGGESTIONS_WROTE_HEADER not in bare.stdout


# ------------------------------------------- apply: the explicit act, and only it

def test_apply_without_yes_shows_the_plan_and_writes_nothing(tmp_path, tree):
    registry = _registry(tmp_path)
    before = image.hash_image_inputs(tree)
    r = _ctl(registry, "suggestions", "sug-run", "apply", "--target", str(tree))
    assert r.returncode == 2, r.stdout
    assert cli_main.SUGGESTIONS_NEEDS_YES in r.stdout
    # everything `--yes` would have done is on screen...
    assert cli_main.SUGGESTIONS_DIFF_HEADER in r.stdout
    assert "modify  src/ralphd/prompts/worker.md" in r.stdout
    # ...and none of it happened
    assert cli_main.SUGGESTIONS_WROTE_HEADER not in r.stdout
    assert (tree / "src/ralphd/prompts/worker.md").read_text() == WORKER_MD
    assert image.hash_image_inputs(tree).digest == before.digest


def test_applying_a_clean_diff_changes_the_image_content_hash(tmp_path, tree):
    """Why the verb is worth having (PRD fact 2): a prompt edit is picked up by
    the *next* `start`, and it is picked up because the content hash moved."""
    registry = _registry(tmp_path)
    before = image.hash_image_inputs(tree)
    r = _ctl(registry, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree))
    assert r.returncode == 0, r.stderr
    after = image.hash_image_inputs(tree)
    assert after.digest != before.digest
    assert before.complete and after.complete
    assert (tree / "src/ralphd/prompts/worker.md").read_text() == (
        "# Role: Worker\n\nRead the notes file first.\nOne task per iteration.\n")
    assert cli_main.SUGGESTIONS_WROTE_HEADER in r.stdout
    assert "wrote   src/ralphd/prompts/worker.md" in r.stdout
    # the hash move is shown, not left for the operator to derive
    assert f"{before.hash} -> {after.hash}" in r.stdout


def test_the_diff_and_the_plan_are_printed_before_the_write_line(tmp_path, tree):
    registry = _registry(tmp_path)
    r = _ctl(registry, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree))
    assert r.returncode == 0, r.stderr
    positions = [r.stdout.index(marker) for marker in (
        cli_main.SUGGESTIONS_DIFF_HEADER, cli_main.SUGGESTIONS_PLAN_HEADER,
        cli_main.SUGGESTIONS_WROTE_HEADER)]
    assert positions == sorted(positions), r.stdout


def test_nothing_is_written_until_the_diff_has_actually_been_shown(
        tmp_path, tree, monkeypatch, capsys):
    """The ordering as a *witness*, not as an output layout: at the moment the
    one writer (`patch.apply_plan`) is called, the diff and the plan are already
    on stdout. An implementation that wrote first and printed a transcript
    afterwards would pass the marker-order test and fail this one."""
    registry = _registry(tmp_path)
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    seen: dict[str, str] = {}
    real_apply = patch.apply_plan

    def spy(plan):
        seen["stdout"] = capsys.readouterr().out
        print(seen["stdout"], end="")  # keep the stream intact for the caller
        return real_apply(plan)

    monkeypatch.setattr(cli_main.patch, "apply_plan", spy)
    args = cli_main.build_parser().parse_args(
        ["suggestions", "sug-run", "apply", "--yes", "--target", str(tree)])
    args.func(args)
    assert cli_main.SUGGESTIONS_DIFF_HEADER in seen["stdout"]
    assert "+Read the notes file first." in seen["stdout"]
    assert cli_main.SUGGESTIONS_PLAN_HEADER in seen["stdout"]
    assert "modify  src/ralphd/prompts/worker.md" in seen["stdout"]
    # ...and the write really did happen through the spied writer
    assert "Read the notes file first." in (
        tree / "src/ralphd/prompts/worker.md").read_text()


def test_re_applying_a_landed_diff_is_refused_not_applied_twice(tmp_path, tree):
    registry = _registry(tmp_path)
    first = _ctl(registry, "suggestions", "sug-run", "apply", "--yes",
                 "--target", str(tree))
    assert first.returncode == 0, first.stderr
    landed = (tree / "src/ralphd/prompts/worker.md").read_text()
    again = _ctl(registry, "suggestions", "sug-run", "apply", "--yes",
                 "--target", str(tree))
    assert again.returncode == 1
    assert patch.REASON_ALREADY_APPLIED in again.stderr
    assert (tree / "src/ralphd/prompts/worker.md").read_text() == landed


# ------------------------------------------------- refusals name the file/hunk

def test_a_diff_that_does_not_apply_is_refused_naming_the_file_and_the_hunk(
        tmp_path, tree):
    registry = _registry(tmp_path, diff=STALE_DIFF)
    before = image.hash_image_inputs(tree)
    r = _ctl(registry, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree))
    assert r.returncode == 1
    assert cli_main.SUGGESTIONS_REFUSED in r.stderr
    assert "src/ralphd/prompts/worker.md" in r.stderr
    assert "@@ -1,2 +1,3 @@" in r.stderr
    assert patch.REASON_NOT_FOUND in r.stderr
    # the diff is still shown -- the rejection is only legible beside it
    assert cli_main.SUGGESTIONS_DIFF_HEADER in r.stdout
    assert "<- does not apply" in r.stdout
    assert cli_main.SUGGESTIONS_WROTE_HEADER not in r.stdout
    assert image.hash_image_inputs(tree).digest == before.digest


def test_show_refuses_a_non_applying_diff_the_same_way(tmp_path, tree):
    """So the check is scriptable with no write path involved at all."""
    registry = _registry(tmp_path, diff=STALE_DIFF)
    r = _ctl(registry, "suggestions", "sug-run", "--target", str(tree))
    assert r.returncode == 1
    assert cli_main.SUGGESTIONS_REFUSED in r.stderr
    assert "@@ -1,2 +1,3 @@" in r.stderr


def test_a_multi_file_diff_writes_neither_file_when_one_hunk_fails(
        tmp_path, tree):
    """All-or-nothing, through the verb: the good half of a bad diff would be a
    tree nobody proposed, and the next `start` would bake it into an image."""
    (tree / "src/ralphd/prompts/review.md").write_text("# Role: Reviewer\n")
    registry = _registry(tmp_path, diff=CLEAN_DIFF + STALE_DIFF.replace(
        "worker.md", "review.md"))
    r = _ctl(registry, "suggestions", "sug-run", "apply", "--yes",
             "--target", str(tree))
    assert r.returncode == 1
    assert (tree / "src/ralphd/prompts/worker.md").read_text() == WORKER_MD
    assert (tree / "src/ralphd/prompts/review.md").read_text() == \
        "# Role: Reviewer\n"


def test_an_unreadable_diff_is_refused_as_text_naming_the_line(tmp_path, tree):
    registry = _registry(tmp_path, diff=UNREADABLE_DIFF)
    r = _ctl(registry, "suggestions", "sug-run", "--target", str(tree))
    assert r.returncode == 1
    assert "unreadable diff" in r.stderr
    assert "line 1" in r.stderr


# ------------------------------------------------------ the arguments' own edges

def test_a_run_with_no_suggestions_diff_says_what_is_on_disk(tmp_path, tree):
    registry = _registry(tmp_path, diff=None)
    r = _ctl(registry, "suggestions", "sug-run", "--target", str(tree))
    assert r.returncode == 1
    assert "reflection/suggestions.diff" in r.stderr
    # the `ralphctl docs`/`artifacts` rule: name what IS there
    assert "reflection/report.md" in r.stderr


def test_an_unknown_run_is_exit_three_before_any_tree_is_read(tmp_path, tree):
    registry = _registry(tmp_path)
    r = _ctl(registry, "suggestions", "nope", "--target", str(tree))
    assert r.returncode == 3
    assert "not found" in r.stderr


def test_a_target_that_is_not_a_directory_is_a_usage_error(tmp_path, tree):
    registry = _registry(tmp_path)
    r = _ctl(registry, "suggestions", "sug-run", "--target",
             str(tmp_path / "nowhere"))
    assert r.returncode == 2
    assert "--target" in r.stderr


def test_an_install_with_no_checkout_names_target_instead_of_guessing(
        tmp_path, monkeypatch, capsys):
    """A wheel/`pipx` install has no `container/Dockerfile` beside it, so there
    is no tree to default to -- and absence is an answer, not a guess."""
    monkeypatch.setenv("RALPHD_REGISTRY", str(_registry(tmp_path)))
    monkeypatch.setattr(cli_main.image, "source_root", lambda *a, **k: None)
    args = cli_main.build_parser().parse_args(["suggestions", "sug-run"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 2
    assert "--target" in capsys.readouterr().err


def test_json_prints_the_plan_before_a_write_and_the_hashes_after(
        tmp_path, tree):
    registry = _registry(tmp_path)
    dry = json.loads(_ctl(registry, "--json", "suggestions", "sug-run",
                          "--target", str(tree)).stdout)
    assert dry["runId"] == "sug-run"
    assert dry["diff"] == "artifacts/reflection/suggestions.diff"
    assert dry["target"] == str(tree)
    assert dry["paths"] == ["src/ralphd/prompts/worker.md"]
    assert dry["applies"] is True and dry["failures"] == []
    assert dry["applied"] is False and dry["written"] == []
    assert dry["imageHash"] == image.hash_image_inputs(tree).hash
    wet = json.loads(_ctl(registry, "--json", "suggestions", "sug-run",
                          "apply", "--yes", "--target", str(tree)).stdout)
    assert wet["applied"] is True
    assert wet["written"] == ["src/ralphd/prompts/worker.md"]
    assert wet["imageHashBefore"] == dry["imageHash"]
    assert wet["imageHash"] == image.hash_image_inputs(tree).hash
    assert wet["imageHash"] != wet["imageHashBefore"]


def test_the_verb_reads_the_run_dir_and_a_tree_and_never_an_api(
        tmp_path, tree):
    """On-disk like `ralphctl docs`/`artifacts`: no API, and so no
    live/snapshot distinction to report. Task 029 narrowed this by exactly one
    docker question -- "is a run that is tied to this tree still running" --
    which lives in its own helper and is pinned by
    tests/test_cli_suggestions_guards.py; the *verb* still talks to neither."""
    import inspect
    source = inspect.getsource(cli_main.cmd_suggestions)
    for forbidden in ("DOCKER", "docker(", "api(", "urllib",
                      "job_container_name"):
        assert forbidden not in source, forbidden
    registry = _registry(tmp_path)
    r = _ctl(registry, "suggestions", "sug-run", "--target", str(tree))
    assert r.returncode == 0
    assert "snapshot" not in r.stdout.lower()


# ----------------------------------------------------------------- doc claims

def _doc_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("### "):
            end = i
            break
    return "\n".join(lines[start:end])


# What docs/cli.md must actually promise about this verb. A list rather than a
# prose test, so the mutation case below can drop any one of them and show that
# the check -- not the reader -- catches it.
DOC_CLAIMS = ("show", "apply", "--yes", "--target", "**All-or-nothing",
              "content hash", "@@", "Exit codes", "`0`", "`1`", "`2`", "`3`",
              "nothing written", "tests/test_cli_suggestions_verb.py")


def _doc_problems(section: str) -> list[str]:
    return [claim for claim in DOC_CLAIMS if claim not in section]


def test_docs_cli_documents_the_verb_its_act_and_its_exit_codes():
    section = _doc_section(CLI_DOC.read_text(), DOC_SECTION)
    assert not _doc_problems(section), _doc_problems(section)
    # the refusal wording is the module's constant, not a retyped copy
    assert cli_main.SUGGESTIONS_NEEDS_YES.split(" -- ")[0] in section


@pytest.mark.parametrize("claim", DOC_CLAIMS)
def test_the_doc_section_check_catches_every_dropped_claim(claim):
    """The mutation case, one row per promise: a section that stops saying it
    fails this module, so the docs cannot quietly fall behind the verb."""
    section = _doc_section(CLI_DOC.read_text(), DOC_SECTION)
    mutated = section.replace(claim, "")
    assert _doc_problems(mutated) == [claim], mutated


def test_the_spec_command_table_has_a_row_for_the_verb():
    rows = [line for line in SPEC.read_text().splitlines()
            if line.startswith("| `suggestions ")]
    assert len(rows) == 1, rows
    assert "`show`" in rows[0] and "`apply`" in rows[0]
    assert "--yes" in rows[0]


def test_no_surface_still_claims_the_reflection_diff_is_never_applied():
    """The claim this verb makes false. It was in three places: the alias
    description both surfaces show, docs/cli.md, and SPEC. Whatever still says
    `never applied` must say *by whom* -- the loop -- because an operator's
    explicit `apply --yes` now does."""
    title = [t for k, _, t in ARTIFACT_ALIASES if k == "suggestions"][0]
    assert "never applied" not in title, title
    assert "ralphctl suggestions" in title
    offenders = []
    for path in (CLI_DOC, SPEC, REPO / "docs" / "architecture.md",
                 REPO / "docs" / "api.md"):
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r"never applied|nothing applies it", line) and \
                    "by the loop" not in line:
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, offenders
