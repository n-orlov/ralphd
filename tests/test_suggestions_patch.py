"""Task 028 (requirement P part 1a): reading a reflection diff, and saying what
it would touch, without writing anything.

`artifacts/reflection/suggestions.diff` has never been applied by anything: the
reflect prompt forbids the agent from applying it and no host-side surface could
either, so the loop's self-improvement was an operator retyping a diff by hand
(SPEC §16's open question, and this project's own v0.6 -> v0.7 history).
`ralphd.cli.patch` is the planner half of the review-and-apply verb: parse,
decide what would change, refuse what does not fit -- and touch the tree only
under a separate, explicit `apply_plan()` call.

What is asserted here, in the order the module is used:

* parsing: the real v0.6 `suggestions.diff` shape (four files, `a/`/`b/`
  prefixes, `diff --git` preamble), creations, deletions, `\\ No newline`, and
  one `PatchError` per way a diff can be unreadable -- each naming the line;
* planning: the paths it would touch, computed content, relocation, and one
  `HunkFailure` per rejection reason, carrying the file AND the `@@` header;
* applying: all-or-nothing (a diff whose second hunk fails writes nothing at
  all), the image-hash consequence that makes this verb worth having (a clean
  apply changes `image.hash_image_inputs()`, a refused one leaves it
  byte-identical), and agreement with `git apply -p1` on the real diff.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ralphd.cli import image, patch

REPO = Path(__file__).parent.parent

# The shape a reflect phase actually produces: `git diff`-style headers with
# `a/`/`b/` prefixes over prompt files. Kept as one literal so every test below
# argues about the same diff rather than its own dialect.
PROMPTS_DIFF = """diff --git a/src/ralphd/prompts/worker.md b/src/ralphd/prompts/worker.md
index 1111111..2222222 100644
--- a/src/ralphd/prompts/worker.md
+++ b/src/ralphd/prompts/worker.md
@@ -1,3 +1,4 @@
 # Role: Worker
 
+Read the notes first.
 One task per iteration.
"""

WORKER_MD = "# Role: Worker\n\nOne task per iteration.\n"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A target tree holding just the one prompt `PROMPTS_DIFF` edits."""
    return _tree(tmp_path / "tree", {"src/ralphd/prompts/worker.md": WORKER_MD})


# --------------------------------------------------------------- parsing

def test_a_real_reflection_diff_parses_into_one_patch_per_file():
    """The v0.6 diff this whole requirement came from: four prompt files."""
    diff = (REPO / "tests" / "data" / "suggestions-v06.diff").read_text()
    patches = patch.parse_patch(diff)
    assert [p.path for p in patches] == [
        "src/ralphd/prompts/planning.md", "src/ralphd/prompts/worker.md",
        "src/ralphd/prompts/task-verify.md", "src/ralphd/prompts/review.md"]
    assert [len(p.hunks) for p in patches] == [3, 3, 2, 1]
    assert {p.change for p in patches} == {patch.CHANGE_MODIFY}


def test_the_a_and_b_prefixes_are_stripped_and_nothing_else_is():
    assert patch.strip_path_prefix("a/src/x.md") == "src/x.md"
    assert patch.strip_path_prefix("b/src/x.md") == "src/x.md"
    assert patch.strip_path_prefix("src/x.md") == "src/x.md"
    # A deeper strip is exactly the guess this module refuses to make.
    assert patch.strip_path_prefix("c/src/x.md") == "c/src/x.md"
    assert patch.strip_path_prefix("/dev/null") == patch.DEV_NULL
    assert patch.strip_path_prefix("a/src/x.md\t2024-01-01 00:00:00") == "src/x.md"


def test_a_hunk_keeps_both_sides_and_its_own_text():
    hunk = patch.parse_patch(PROMPTS_DIFF)[0].hunks[0]
    assert hunk.header == "@@ -1,3 +1,4 @@"
    assert (hunk.old_start, hunk.old_count) == (1, 3)
    assert (hunk.new_start, hunk.new_count) == (1, 4)
    assert hunk.old_lines == ("# Role: Worker", "", "One task per iteration.")
    assert hunk.new_lines == ("# Role: Worker", "", "Read the notes first.",
                              "One task per iteration.")
    # Shown back exactly as proposed, markers included.
    assert hunk.text.splitlines()[0] == hunk.header
    assert "+Read the notes first." in hunk.text


def test_a_single_line_hunk_header_without_counts_means_one_line():
    patches = patch.parse_patch(
        "--- a/f.txt\n+++ b/f.txt\n@@ -2 +2 @@\n-b\n+B\n")
    hunk = patches[0].hunks[0]
    assert (hunk.old_count, hunk.new_count) == (1, 1)
    assert (hunk.old_lines, hunk.new_lines) == (("b",), ("B",))


def test_dev_null_on_the_old_side_is_a_creation_and_on_the_new_side_a_deletion():
    created = patch.parse_patch(
        "--- /dev/null\n+++ b/new.md\n@@ -0,0 +1,2 @@\n+one\n+two\n")[0]
    assert (created.change, created.path, created.creates) == (
        patch.CHANGE_CREATE, "new.md", True)
    deleted = patch.parse_patch(
        "--- a/old.md\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n")[0]
    assert (deleted.change, deleted.path, deleted.deletes) == (
        patch.CHANGE_DELETE, "old.md", True)


def test_the_no_newline_marker_is_recorded_as_a_flag_not_a_line():
    hunk = patch.parse_patch(
        "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
        "\\ No newline at end of file\n")[0].hunks[0]
    assert hunk.new_lines == ("b",)
    assert hunk.new_no_newline is True and hunk.old_no_newline is False


@pytest.mark.parametrize("text,expected", [
    ("", "no file headers"),
    ("just a report, no diff at all\n", "no file headers"),
    ("+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n", "`+++` with no `---` before it"),
    ("@@ -1 +1 @@\n-a\n+b\n", "hunk before any `---`/`+++` file header"),
    ("--- a/f.txt\n+++ b/f.txt\n@@ nonsense @@\n-a\n", "unreadable hunk header"),
    ("--- a/f.txt\n+++ b/f.txt\n", "file header with no hunks"),
    ("--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n-a\n+b\n", "carries"),
    ("--- a/f.txt\n+++ b/g.txt\n@@ -1 +1 @@\n-a\n+b\n", "renames are not supported"),
    ("--- /dev/null\n+++ /dev/null\n@@ -1 +1 @@\n-a\n+b\n", "/dev/null on both sides"),
])
def test_an_unreadable_diff_is_refused_as_text_with_the_reason(text, expected):
    with pytest.raises(patch.PatchError) as excinfo:
        patch.parse_patch(text)
    assert expected in str(excinfo.value)


def test_an_unreadable_hunk_header_names_the_line_it_is_on():
    with pytest.raises(patch.PatchError) as excinfo:
        patch.parse_patch("--- a/f.txt\n+++ b/f.txt\n@@ nope @@\n-a\n")
    message = str(excinfo.value)
    assert "line 3" in message and "@@ nope @@" in message


def test_an_unreadable_hunk_body_names_the_line_number_and_the_marker():
    with pytest.raises(patch.PatchError) as excinfo:
        patch.parse_patch("--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n"
                          "?nonsense\n+b\n")
    message = str(excinfo.value)
    assert "line 5" in message and "'?'" in message


def test_a_hunk_line_stripped_of_its_leading_space_is_still_context():
    """Mail and editors eat the single space of a blank context line; a diff
    that is otherwise applicable must not be refused over it."""
    patches = patch.parse_patch("--- a/f.txt\n+++ b/f.txt\n"
                                "@@ -1,3 +1,4 @@\n a\n\n b\n+c\n")
    assert patches[0].hunks[0].old_lines == ("a", "", "b")


# --------------------------------------------------------------- planning

def test_a_plan_names_the_paths_it_would_touch_and_writes_nothing(tree: Path):
    before = (tree / "src/ralphd/prompts/worker.md").read_text()
    plan = patch.plan_text(tree, PROMPTS_DIFF)
    assert plan.ok is True
    assert plan.paths == ("src/ralphd/prompts/worker.md",)
    assert [c.change for c in plan.changes] == [patch.CHANGE_MODIFY]
    assert plan.changes[0].text == (
        "# Role: Worker\n\nRead the notes first.\nOne task per iteration.\n")
    # The whole point: planning is inert.
    assert (tree / "src/ralphd/prompts/worker.md").read_text() == before


def test_a_failing_plan_still_names_every_file_the_diff_would_have_touched(
        tmp_path: Path):
    root = _tree(tmp_path, {"src/ralphd/prompts/worker.md":
                            "# Role: Worker\n\nsomething else entirely.\n"})
    plan = patch.plan_text(root, PROMPTS_DIFF)
    assert plan.ok is False
    assert plan.paths == ("src/ralphd/prompts/worker.md",)
    assert plan.changes == ()


def test_a_rejection_carries_the_file_the_hunk_header_and_the_reason(
        tmp_path: Path):
    root = _tree(tmp_path, {"src/ralphd/prompts/worker.md":
                            "# Role: Worker\n\nsomething else entirely.\n"})
    failure, = patch.plan_text(root, PROMPTS_DIFF).failures
    assert failure.path == "src/ralphd/prompts/worker.md"
    assert failure.header == "@@ -1,3 +1,4 @@"
    assert failure.reason == patch.REASON_NOT_FOUND
    rendered = str(failure)
    assert "src/ralphd/prompts/worker.md" in rendered
    assert "@@ -1,3 +1,4 @@" in rendered and patch.REASON_NOT_FOUND in rendered


def test_a_missing_file_is_a_whole_file_reason_not_a_hunk_one(tmp_path: Path):
    plan = patch.plan_text(_tree(tmp_path, {}), PROMPTS_DIFF)
    failure, = plan.failures
    assert (failure.reason, failure.header) == (patch.REASON_MISSING, "")
    assert "the whole file" in str(failure)


def test_creating_a_file_that_already_exists_is_refused(tmp_path: Path):
    root = _tree(tmp_path, {"new.md": "already here\n"})
    plan = patch.plan_text(
        root, "--- /dev/null\n+++ b/new.md\n@@ -0,0 +1 @@\n+one\n")
    assert [f.reason for f in plan.failures] == [patch.REASON_EXISTS]


def test_an_ambiguous_context_is_refused_and_the_count_is_in_the_reason(
        tmp_path: Path):
    root = _tree(tmp_path, {"f.txt": "x\nsame\nx\nsame\nx\nsame\nx\n"})
    plan = patch.plan_text(
        root, "--- a/f.txt\n+++ b/f.txt\n@@ -40,1 +40,2 @@\n same\n+added\n")
    failure, = plan.failures
    assert failure.reason == patch.REASON_AMBIGUOUS.format(count=3)
    assert "3 times" in failure.reason


def test_a_diff_already_present_in_the_tree_says_so(tree: Path):
    patch.apply_plan(patch.plan_text(tree, PROMPTS_DIFF))
    plan = patch.plan_text(tree, PROMPTS_DIFF)
    assert [f.reason for f in plan.failures] == [patch.REASON_ALREADY_APPLIED]


def test_a_hunk_that_moved_still_applies_and_the_move_is_recorded(
        tmp_path: Path):
    root = _tree(tmp_path, {"src/ralphd/prompts/worker.md":
                            "a preamble line\n" + WORKER_MD})
    plan = patch.plan_text(root, PROMPTS_DIFF)
    assert plan.ok is True
    change, = plan.changes
    assert change.offsets == (1,) and change.relocated is True
    assert change.text == "a preamble line\n# Role: Worker\n\n" \
                          "Read the notes first.\nOne task per iteration.\n"


def test_several_hunks_in_one_file_do_not_look_relocated_because_of_each_other(
        tmp_path: Path):
    root = _tree(tmp_path, {"f.txt": "".join(f"line{i}\n" for i in range(1, 11))})
    plan = patch.plan_text(root, (
        "--- a/f.txt\n+++ b/f.txt\n"
        "@@ -1,2 +1,3 @@\n line1\n line2\n+added early\n"
        "@@ -8,2 +9,3 @@\n line8\n line9\n+added late\n"))
    assert plan.ok is True, [str(f) for f in plan.failures]
    change, = plan.changes
    assert change.offsets == (0, 0) and change.relocated is False
    assert change.text.splitlines()[2] == "added early"
    assert change.text.splitlines()[-2] == "added late"


def test_a_deletion_plans_removal_only_when_the_content_matches(tmp_path: Path):
    root = _tree(tmp_path, {"old.md": "one\ntwo\n", "other.md": "different\n"})
    diff = "--- a/old.md\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
    plan = patch.plan_text(root, diff)
    assert plan.ok is True and plan.changes[0].text is None
    root.joinpath("old.md").write_text("one\ntwo\nthree\n")
    stale = patch.plan_text(root, diff)
    assert [f.reason for f in stale.failures] == [patch.REASON_DELETE_MISMATCH]


def test_a_file_with_no_trailing_newline_keeps_none(tmp_path: Path):
    root = _tree(tmp_path, {"f.txt": "a"})
    plan = patch.plan_text(
        root, "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n"
              "\\ No newline at end of file\n+b\n"
              "\\ No newline at end of file\n")
    assert plan.ok is True and plan.changes[0].text == "b"


def test_a_diff_that_gives_a_file_a_trailing_newline_says_so(tmp_path: Path):
    root = _tree(tmp_path, {"f.txt": "a"})
    plan = patch.plan_text(
        root, "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n"
              "\\ No newline at end of file\n+b\n")
    assert plan.ok is True and plan.changes[0].text == "b\n"


def test_removing_every_line_leaves_an_empty_file_not_a_blank_one(
        tmp_path: Path):
    root = _tree(tmp_path, {"f.txt": "a\nb\n"})
    plan = patch.plan_text(root,
                           "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +0,0 @@\n-a\n-b\n")
    assert plan.ok is True and plan.changes[0].text == ""


def test_every_file_is_planned_even_after_an_earlier_one_fails(tmp_path: Path):
    root = _tree(tmp_path, {"good.txt": "a\n", "bad.txt": "unexpected\n"})
    plan = patch.plan_text(root, (
        "--- a/bad.txt\n+++ b/bad.txt\n@@ -1 +1,2 @@\n a\n+more\n"
        "--- a/good.txt\n+++ b/good.txt\n@@ -1 +1,2 @@\n a\n+more\n"))
    assert plan.paths == ("bad.txt", "good.txt")
    assert [f.path for f in plan.failures] == ["bad.txt"]
    # The clean file is still computed -- and, per all-or-nothing, not written.
    assert [c.path for c in plan.changes] == ["good.txt"]
    assert root.joinpath("good.txt").read_text() == "a\n"


def test_an_empty_diff_cannot_be_planned_at_all():
    with pytest.raises(patch.PatchError):
        patch.plan_text(REPO, "")


def test_format_plan_lines_name_the_verb_the_path_and_the_rejections(
        tmp_path: Path):
    root = _tree(tmp_path, {"src/ralphd/prompts/worker.md": "unexpected\n",
                            "old.md": "one\n"})
    plan = patch.plan_text(root, PROMPTS_DIFF + (
        "--- a/old.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-one\n"
        "--- /dev/null\n+++ b/new.md\n@@ -0,0 +1 @@\n+hello\n"))
    lines = patch.format_plan(plan)
    assert any(line.startswith("modify") and "worker.md" in line
               and "does not apply" in line for line in lines), lines
    assert any(line.startswith("delete") and "old.md" in line for line in lines)
    assert any(line.startswith("create") and "new.md" in line for line in lines)
    assert any(line.startswith("reject") and "@@ -1,3 +1,4 @@" in line
               for line in lines), lines


# --------------------------------------------------------------- applying

def test_apply_writes_exactly_the_planned_content(tree: Path):
    plan = patch.plan_text(tree, PROMPTS_DIFF)
    written = patch.apply_plan(plan)
    assert written == ("src/ralphd/prompts/worker.md",)
    assert (tree / "src/ralphd/prompts/worker.md").read_text() == \
        plan.changes[0].text


def test_apply_creates_and_deletes_the_files_the_diff_says(tmp_path: Path):
    root = _tree(tmp_path, {"old.md": "one\n"})
    patch.apply_text(root, "--- a/old.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-one\n"
                           "--- /dev/null\n+++ b/sub/new.md\n@@ -0,0 +1 @@\n+hi\n")
    assert not (root / "old.md").exists()
    assert (root / "sub" / "new.md").read_text() == "hi\n"


def test_a_diff_whose_second_hunk_fails_writes_nothing_at_all(tmp_path: Path):
    """All-or-nothing: the tree is never left in a state nobody proposed."""
    root = _tree(tmp_path, {"f.txt": "a\nb\nc\nd\ne\n"})
    before = (root / "f.txt").read_text()
    diff = ("--- a/f.txt\n+++ b/f.txt\n"
            "@@ -1,2 +1,3 @@\n a\n b\n+inserted\n"
            "@@ -4,2 +5,3 @@\n NOT-THERE\n e\n+also inserted\n")
    plan = patch.plan_text(root, diff)
    assert plan.ok is False
    with pytest.raises(patch.PatchError) as excinfo:
        patch.apply_plan(plan)
    assert "does not apply" in str(excinfo.value)
    assert "@@ -4,2 +5,3 @@" in str(excinfo.value)
    assert (root / "f.txt").read_text() == before


def test_a_multi_file_diff_with_one_bad_file_writes_neither(tmp_path: Path):
    root = _tree(tmp_path, {"good.txt": "a\n", "bad.txt": "unexpected\n"})
    with pytest.raises(patch.PatchError):
        patch.apply_text(root, (
            "--- a/good.txt\n+++ b/good.txt\n@@ -1 +1,2 @@\n a\n+more\n"
            "--- a/bad.txt\n+++ b/bad.txt\n@@ -1 +1,2 @@\n a\n+more\n"))
    assert root.joinpath("good.txt").read_text() == "a\n"
    assert root.joinpath("bad.txt").read_text() == "unexpected\n"


# ------------------------------------------- the consequence: the image hash

def _copy_inputs(dest: Path) -> Path:
    """A copy of this checkout's image inputs -- the tree a diff is applied to,
    and the tree whose content hash decides which image the next `start` runs."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel in image.IMAGE_INPUTS:
        src = REPO / rel
        if src.is_dir():
            shutil.copytree(src, dest / rel, dirs_exist_ok=True)
        elif src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    return dest


def test_applying_a_clean_diff_changes_the_image_content_hash(tmp_path: Path):
    """Why the verb is worth having (fact 2): a prompt edit is only picked up
    by the *next* `start`, and it is picked up because the hash moved."""
    root = _copy_inputs(tmp_path / "checkout")
    before = image.hash_image_inputs(root)
    prompt = root / "src/ralphd/prompts/worker.md"
    diff = ("--- a/src/ralphd/prompts/worker.md\n"
            "+++ b/src/ralphd/prompts/worker.md\n"
            "@@ -1,1 +1,2 @@\n"
            f" {prompt.read_text().splitlines()[0]}\n"
            "+A line the reflection phase proposed.\n")
    assert patch.apply_text(root, diff) == ("src/ralphd/prompts/worker.md",)
    after = image.hash_image_inputs(root)
    assert after.digest != before.digest
    assert after.hash != before.hash
    assert before.complete and after.complete


def test_a_refused_diff_leaves_the_image_content_hash_byte_identical(
        tmp_path: Path):
    root = _copy_inputs(tmp_path / "checkout")
    before = image.hash_image_inputs(root)
    diff = ("--- a/src/ralphd/prompts/worker.md\n"
            "+++ b/src/ralphd/prompts/worker.md\n"
            "@@ -1,1 +1,2 @@\n NOT WHAT THE FILE SAYS\n+proposed\n")
    plan = patch.plan_text(root, diff)
    assert plan.ok is False
    with pytest.raises(patch.PatchError):
        patch.apply_plan(plan)
    assert image.hash_image_inputs(root).digest == before.digest


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_the_real_v06_diff_applies_exactly_as_git_apply_would(tmp_path: Path):
    """The strongest correctness check available: on the diff this requirement
    came from, over the tree it was written against, this module's output is
    byte-identical to `git apply -p1`'s."""
    diff = REPO / "tests" / "data" / "suggestions-v06.diff"
    base = REPO / "tests" / "data" / "suggestions-v06-base"
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    shutil.copytree(base, ours)
    shutil.copytree(base, theirs)
    assert patch.apply_text(ours, diff.read_text()) == (
        "src/ralphd/prompts/planning.md", "src/ralphd/prompts/worker.md",
        "src/ralphd/prompts/task-verify.md", "src/ralphd/prompts/review.md")
    subprocess.run(["git", "init", "-q", "."], cwd=theirs, check=True)
    subprocess.run(["git", "apply", "-p1", str(diff)], cwd=theirs, check=True)
    for name in ("planning", "worker", "task-verify", "review"):
        rel = f"src/ralphd/prompts/{name}.md"
        assert (ours / rel).read_text() == (theirs / rel).read_text(), rel


def test_the_spec_module_map_names_this_module():
    """A shipped module with no §3.5 row is a module nobody can find."""
    row = [line for line in (REPO / "SPEC.md").read_text().splitlines()
           if line.startswith("| `src/ralphd/cli/patch.py` |")]
    assert len(row) == 1, row
    assert "suggestions.diff" in row[0]
    for claim in ("plan", "all-or-nothing"):
        assert claim in row[0].lower(), row[0]
