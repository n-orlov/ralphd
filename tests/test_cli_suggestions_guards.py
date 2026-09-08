"""Task 029 (requirement P part 2): the two refusals that make `ralphctl
suggestions apply` safe on a host that also *hosts* runs.

Task 041 built the verb: show, then write on an explicit `--yes`. Two things it
could still do are the ones SPEC §16 was worried about when it left the
reflection diff unapplied:

  * **mid-flight.** A run recorded non-terminal whose container is really
    running can be tied to the tree being written: it may be executing the image
    built from exactly this tree (`host.json`'s `imageHash`), or the tree may be
    its mounted workspace. The first tie is the subtle one -- the write cannot
    reach the live container at all, because the prompts are baked into its
    image, and that *is* the damage: the checkout silently stops describing the
    run being watched. Refused, exit 1, both run and reason named, no override.
  * **two runs proposing edits to the same prompt** -- SPEC §16's own conflict
    question. Applying both corrupts nothing (the matcher is context-exact); it
    produces a third version of the prompt that neither run proposed and nobody
    reviewed, silently. Refused with both diffs named, exit 1, unless the
    operator picks with `--ignore-conflicts`; and the diff that lost is then
    refused by the *matcher* rather than landing on top -- asserted end to end
    below, because "silently taking the second one" is the outcome the PRD
    forbids.

What is pinned here: both refusals over every corner of the tie (image hash /
workspace / both / neither, and each liveness state); that neither fires for a
finished run, a stale non-terminal record whose container is gone, or a sibling
diff that no longer applies; that `show` never refuses but always reports;
`--ignore-conflicts` does not override the live-run refusal; the `--json`
`liveRuns`/`conflicts` keys; and the doc claims in docs/cli.md, SPEC §10.1 and
the §16 -> §17 move, each with a mutation case.

Tiers: black-box `ralphctl` subprocesses over a hand-built registry with the
recording stub docker (STUB_DOCKER_CONTAINERS/STUB_DOCKER_RUNNING deciding
liveness -- no container, no engine, no API anywhere), plus in-process tables
over the two helpers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from ralphd.cli import image, main as cli_main, patch
from tests.conftest import call_ralphctl_in_process
from tests.test_cli_suggestions_verb import (CLEAN_DIFF, STALE_DIFF, WORKER_MD,
                                             _checkout, _doc_section)

REPO = Path(__file__).resolve().parents[1]
CLI_DOC = REPO / "docs" / "cli.md"
SPEC = REPO / "SPEC.md"
DOC_SECTION = "### `ralphctl suggestions"
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"

# A second run's proposal for the same file: same context, different added line.
# The pair is the conflict -- two diffs that BOTH apply to one prompt right now.
OTHER_DIFF = CLEAN_DIFF.replace("Read the notes file first.",
                                "Read the standing rules first.")
# Touches a different prompt, so it is never a conflict with CLEAN_DIFF.
ELSEWHERE_DIFF = """--- a/src/ralphd/prompts/review.md
+++ b/src/ralphd/prompts/review.md
@@ -1,1 +1,2 @@
 # Role: Reviewer
+Cross-check the evidence.
"""


# ------------------------------------------------------------------ harness
def _registry(tmp: Path) -> Path:
    reg = tmp / "registry"
    (reg / "runs").mkdir(parents=True)
    return reg


def _run(reg: Path, run_id: str, *, state: str = "succeeded",
         diff: str | None = None, host: dict | None = None) -> Path:
    """One run dir: a recorded state, optionally a reflection diff and a
    host.json (which is what ties a run to a tree)."""
    rdir = reg / "runs" / run_id
    (rdir / "artifacts" / "reflection").mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"runId": run_id,
                                                  "state": state}))
    (rdir / "artifacts" / "reflection" / "report.md").write_text("# Reflection\n")
    if diff is not None:
        (rdir / "artifacts" / "reflection" / "suggestions.diff").write_text(diff)
    if host is not None:
        (rdir / "host.json").write_text(json.dumps(host))
    return rdir


def _ctl(reg: Path, *argv: str, live: str = "", exists: str = "",
         timeout: int = 60):
    """`ralphctl` with the stub docker deciding liveness. `live`/`exists` are
    comma-separated *run ids*, spelled into container names here so no test
    has to know how the name is built."""
    def names(ids: str) -> str:
        return ",".join(cli_main.job_container_name(i)
                        for i in ids.split(",") if i)
    env = {**os.environ, "RALPHD_REGISTRY": str(reg),
           "RALPHD_DOCKER": str(STUB_DOCKER),
           "STUB_DOCKER_CONTAINERS": names(exists or live),
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
    t = _checkout(tmp_path / "checkout")
    (t / "src/ralphd/prompts/review.md").write_text("# Role: Reviewer\n")
    return t


def _tree_hash(tree: Path) -> str:
    return image.hash_image_inputs(tree).hash


def _unchanged(tree: Path) -> bool:
    return (tree / "src/ralphd/prompts/worker.md").read_text() == WORKER_MD


# ------------------------------------------------- refusal 1: never mid-flight
def test_apply_is_refused_while_a_live_run_executes_this_trees_image(tmp_path,
                                                                    tree):
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "live", state="running",
         host={"imageHash": _tree_hash(tree), "image": "ralphd:x"})
    before = image.hash_image_inputs(tree)
    r = _ctl(reg, "suggestions", "done", "apply", "--yes", "--target",
             str(tree), live="live")
    assert r.returncode == 1, r.stdout
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED in r.stderr
    # which run, and why
    assert "run live" in r.stderr
    assert f"imageHash {before.hash}" in r.stderr
    # the workflow, not just a no
    assert "--target" in r.stderr and "next `start`" in r.stderr
    # and nothing happened
    assert cli_main.SUGGESTIONS_WROTE_HEADER not in r.stdout
    assert _unchanged(tree)
    assert image.hash_image_inputs(tree).digest == before.digest


def test_apply_is_refused_while_a_live_run_has_this_tree_as_its_workspace(
        tmp_path, tree):
    """The other tie: the write would land in a directory a live iteration is
    editing, on top of whatever it has half-finished there."""
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "live", state="running", host={"workspace": str(tree)})
    r = _ctl(reg, "suggestions", "done", "apply", "--yes", "--target",
             str(tree), live="live")
    assert r.returncode == 1, r.stdout
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED in r.stderr
    assert "mounted workspace" in r.stderr and str(tree) in r.stderr
    assert _unchanged(tree)


def test_a_named_multi_workspace_mount_is_the_same_tie(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "live", state="running",
         host={"workspaces": {"ralphd": str(tree), "other": str(tmp_path)}})
    r = _ctl(reg, "suggestions", "done", "apply", "--yes", "--target",
             str(tree), live="live")
    assert r.returncode == 1
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED in r.stderr


def test_the_refusal_holds_even_for_the_runs_own_diff(tmp_path, tree):
    """A run whose own reflection proposes edits to the tree it is still running
    in is the case requirement P names first -- it must not apply to itself."""
    reg = _registry(tmp_path)
    _run(reg, "live", state="running", diff=CLEAN_DIFF,
         host={"imageHash": _tree_hash(tree)})
    r = _ctl(reg, "suggestions", "live", "apply", "--yes", "--target",
             str(tree), live="live")
    assert r.returncode == 1
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED in r.stderr
    assert _unchanged(tree)


def test_ignore_conflicts_does_not_override_the_live_run_refusal(tmp_path, tree):
    """`--ignore-conflicts` picks between two diffs. It is not a `--force`."""
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "live", state="running", host={"imageHash": _tree_hash(tree)})
    r = _ctl(reg, "suggestions", "done", "apply", "--yes",
             "--ignore-conflicts", "--target", str(tree), live="live")
    assert r.returncode == 1
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED in r.stderr
    assert _unchanged(tree)


def test_apply_without_yes_is_refused_for_the_same_reason_not_as_usage(tmp_path,
                                                                      tree):
    """The command was well formed; the answer is "not while that run runs", so
    it is exit 1 (a refusal) and not the exit 2 of `apply` without `--yes`."""
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "live", state="running", host={"imageHash": _tree_hash(tree)})
    r = _ctl(reg, "suggestions", "done", "apply", "--target", str(tree),
             live="live")
    assert r.returncode == 1
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED in r.stderr
    assert cli_main.SUGGESTIONS_NEEDS_YES not in r.stdout


def test_show_reports_the_live_run_and_still_exits_zero(tmp_path, tree):
    """`show` writes nothing, so it never refuses -- but an operator has to be
    able to see that the apply would be refused before typing it."""
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "live", state="running", host={"imageHash": _tree_hash(tree)})
    r = _ctl(reg, "suggestions", "done", "--target", str(tree), live="live")
    assert r.returncode == 0, r.stderr
    assert cli_main.SUGGESTIONS_MIDFLIGHT_HEADER in r.stdout
    assert "run live (running)" in r.stdout
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED not in r.stderr
    assert _unchanged(tree)


@pytest.mark.parametrize("state", ["starting", "running"])
def test_both_non_terminal_states_count_as_mid_flight(tmp_path, tree, state):
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "live", state=state, host={"imageHash": _tree_hash(tree)})
    r = _ctl(reg, "suggestions", "done", "apply", "--yes", "--target",
             str(tree), live="live")
    assert r.returncode == 1, r.stdout
    assert cli_main.SUGGESTIONS_MIDFLIGHT_REFUSED in r.stderr


def test_a_finished_run_tied_to_this_tree_blocks_nothing(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "over", state="succeeded", host={"imageHash": _tree_hash(tree)})
    r = _ctl(reg, "suggestions", "done", "apply", "--yes", "--target",
             str(tree), live="over")
    assert r.returncode == 0, r.stderr
    assert not _unchanged(tree)


@pytest.mark.parametrize("exists,live", [("", ""), ("stale", "")])
def test_a_stale_non_terminal_record_whose_container_is_not_running_does_not_block(
        tmp_path, tree, exists, live):
    """The dangling shapes (#31): container absent, and container present but
    exited. Neither is a live engine, and a run dir nobody cleaned up must not
    block every apply forever -- `doctor` owns that condition."""
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "stale", state="running", host={"imageHash": _tree_hash(tree)})
    r = _ctl(reg, "suggestions", "done", "apply", "--yes", "--target",
             str(tree), exists=exists, live=live)
    assert r.returncode == 0, r.stderr + r.stdout
    assert not _unchanged(tree)


def test_a_live_run_tied_to_no_tree_of_ours_is_not_the_operators_problem(
        tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "done", diff=CLEAN_DIFF)
    _run(reg, "live", state="running",
         host={"imageHash": "0f1e2d3c4b5a", "workspace": str(tmp_path / "elsewhere")})
    r = _ctl(reg, "suggestions", "done", "apply", "--yes", "--target",
             str(tree), live="live")
    assert r.returncode == 0, r.stderr
    assert not _unchanged(tree)


# --------------------------------------------- refusal 2: two runs, one prompt
def test_apply_is_refused_while_another_runs_diff_also_fits_the_same_file(
        tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "theirs", diff=OTHER_DIFF)
    before = image.hash_image_inputs(tree)
    r = _ctl(reg, "suggestions", "mine", "apply", "--yes", "--target", str(tree))
    assert r.returncode == 1, r.stdout
    assert cli_main.SUGGESTIONS_CONFLICT_REFUSED in r.stderr
    # BOTH diffs named: the run being applied is in the header, the other here
    assert "run theirs" in r.stderr
    assert "artifacts/reflection/suggestions.diff" in r.stderr
    assert "src/ralphd/prompts/worker.md" in r.stderr
    assert "run:       mine" in r.stdout
    # the way out is named too
    assert "--ignore-conflicts" in r.stderr
    assert _unchanged(tree)
    assert image.hash_image_inputs(tree).digest == before.digest


def test_the_second_diff_is_never_taken_silently(tmp_path, tree):
    """The whole point, end to end. The operator picks one with
    `--ignore-conflicts`; the loser is then refused by the *matcher*, naming the
    hunk, rather than landing on top of a change nobody re-reviewed."""
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "theirs", diff=OTHER_DIFF)
    first = _ctl(reg, "suggestions", "mine", "apply", "--yes",
                 "--ignore-conflicts", "--target", str(tree))
    assert first.returncode == 0, first.stderr
    landed = (tree / "src/ralphd/prompts/worker.md").read_text()
    assert "Read the notes file first." in landed
    second = _ctl(reg, "suggestions", "theirs", "apply", "--yes", "--target",
                  str(tree))
    assert second.returncode == 1, second.stdout
    assert cli_main.SUGGESTIONS_REFUSED in second.stderr
    assert patch.REASON_NOT_FOUND in second.stderr
    assert "@@" in second.stderr
    # ...and the winner's text is untouched: nothing merged, nothing overwritten
    assert (tree / "src/ralphd/prompts/worker.md").read_text() == landed
    assert "standing rules" not in landed


def test_show_lists_the_other_run_and_exits_zero(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "theirs", diff=OTHER_DIFF)
    r = _ctl(reg, "suggestions", "mine", "--target", str(tree))
    assert r.returncode == 0, r.stderr
    assert cli_main.SUGGESTIONS_CONFLICT_HEADER in r.stdout
    assert "run theirs" in r.stdout
    assert "src/ralphd/prompts/worker.md" in r.stdout
    assert _unchanged(tree)


def test_a_sibling_diff_touching_another_file_is_no_conflict(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "theirs", diff=ELSEWHERE_DIFF)
    r = _ctl(reg, "suggestions", "mine", "apply", "--yes", "--target", str(tree))
    assert r.returncode == 0, r.stderr
    assert cli_main.SUGGESTIONS_CONFLICT_HEADER not in r.stdout
    assert not _unchanged(tree)


@pytest.mark.parametrize("sibling", [STALE_DIFF, "@@ -1,2 +1,3 @@\n a\n+b\n",
                                     CLEAN_DIFF])
def test_a_sibling_that_cannot_be_written_is_not_a_conflict(tmp_path, tree,
                                                            sibling):
    """A stale sibling, an unreadable one, and one that is already applied: none
    of them can ever be written, so none of them can take anything from this
    diff. (The third is `CLEAN_DIFF` itself, applied first.)"""
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "theirs", diff=sibling)
    if sibling == CLEAN_DIFF:
        patch.apply_text(tree, CLEAN_DIFF)
        expect_written = False
    else:
        expect_written = True
    r = _ctl(reg, "suggestions", "mine", "apply", "--yes", "--target", str(tree))
    assert cli_main.SUGGESTIONS_CONFLICT_REFUSED not in r.stderr
    if expect_written:
        assert r.returncode == 0, r.stderr
        assert not _unchanged(tree)
    else:
        # already applied -- refused by the matcher, which is a different story
        assert r.returncode == 1
        assert patch.REASON_ALREADY_APPLIED in r.stderr


def test_a_multi_file_sibling_that_only_half_fits_is_no_conflict(tmp_path, tree):
    """The case the `ok` gate exists for, and the reason "which paths does it
    mention" is the wrong question: a sibling diff whose worker.md hunk applies
    but whose *other* file does not can never be written at all -- application is
    all-or-nothing -- so it can take nothing from this diff."""
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "theirs",
         diff=OTHER_DIFF + STALE_DIFF.replace("worker.md", "review.md"))
    r = _ctl(reg, "suggestions", "mine", "apply", "--yes", "--target", str(tree))
    assert r.returncode == 0, r.stderr + r.stdout
    assert cli_main.SUGGESTIONS_CONFLICT_REFUSED not in r.stderr
    assert not _unchanged(tree)


def test_a_run_with_no_diff_at_all_is_not_a_conflict(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "empty")
    r = _ctl(reg, "suggestions", "mine", "apply", "--yes", "--target", str(tree))
    assert r.returncode == 0, r.stderr


def test_three_runs_proposing_the_same_file_are_all_named(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "b", diff=OTHER_DIFF)
    _run(reg, "c", diff=CLEAN_DIFF.replace("notes file", "PRD"))
    r = _ctl(reg, "suggestions", "mine", "apply", "--yes", "--target", str(tree))
    assert r.returncode == 1
    assert "run b" in r.stderr and "run c" in r.stderr


# ----------------------------------------------------------- the --json surface
def test_json_carries_both_conditions_before_any_write(tmp_path, tree):
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    _run(reg, "theirs", diff=OTHER_DIFF)
    _run(reg, "live", state="running", host={"imageHash": _tree_hash(tree)})
    doc = json.loads(_ctl(reg, "--json", "suggestions", "mine", "--target",
                          str(tree), live="live").stdout)
    assert doc["applied"] is False and doc["written"] == []
    assert [r["runId"] for r in doc["liveRuns"]] == ["live"]
    assert doc["liveRuns"][0]["state"] == "running"
    assert any("imageHash" in reason for reason in doc["liveRuns"][0]["reasons"])
    assert doc["conflicts"] == [
        {"runId": "theirs", "diff": "artifacts/reflection/suggestions.diff",
         "paths": ["src/ralphd/prompts/worker.md"]}]
    assert _unchanged(tree)


def test_json_says_both_lists_are_empty_when_nothing_is_in_the_way(tmp_path,
                                                                   tree):
    """Absence is an explicit answer, never a missing key (the
    `maxApproaches`/`model` discipline)."""
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    doc = json.loads(_ctl(reg, "--json", "suggestions", "mine", "--target",
                          str(tree)).stdout)
    assert doc["liveRuns"] == [] and doc["conflicts"] == []


# ------------------------------------------------------------- the two helpers
def test_the_tie_helper_reports_every_reason_it_finds(tmp_path, tree):
    reg = _registry(tmp_path)
    rdir = _run(reg, "live", state="running",
                host={"imageHash": _tree_hash(tree), "workspace": str(tree)})
    reasons = cli_main._suggestions_tree_ties(rdir, tree, _tree_hash(tree))
    assert len(reasons) == 2, reasons
    assert any("imageHash" in r for r in reasons)
    assert any("mounted workspace" in r for r in reasons)


@pytest.mark.parametrize("workspace, tied", [
    (".", True),                     # the tree itself
    ("..", True),                    # the workspace contains the tree
    ("./src", True),                 # the workspace sits inside the tree
    ("../elsewhere", False),         # unrelated
])
def test_workspace_containment_counts_in_both_directions(tmp_path, tree,
                                                         workspace, tied):
    """A refusal, so the generous reading is the safe one: a diff path under a
    workspace mounted inside the tree is as much a live iteration's file as the
    other way round."""
    reg = _registry(tmp_path)
    ws = (tree / workspace).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    rdir = _run(reg, "live", state="running", host={"workspace": str(ws)})
    reasons = cli_main._suggestions_tree_ties(rdir, tree, _tree_hash(tree))
    assert bool(reasons) is tied, reasons


def test_a_host_json_that_says_nothing_ties_nothing(tmp_path, tree):
    reg = _registry(tmp_path)
    for host in ({}, {"workspace": None}, {"workspaces": {}},
                 {"imageHash": None}, {"imageHash": "  "}):
        rdir = _run(reg, f"r{abs(hash(str(host)))}", state="running", host=host)
        assert cli_main._suggestions_tree_ties(rdir, tree, _tree_hash(tree)) == []


def test_liveness_is_asked_only_for_a_run_that_is_tied_and_non_terminal(
        tmp_path, tree, monkeypatch):
    """A registry full of finished runs costs no docker calls at all -- the
    reason this verb is still usable on a host with a hundred old run dirs."""
    reg = _registry(tmp_path)
    _run(reg, "over", host={"imageHash": _tree_hash(tree)})       # terminal
    _run(reg, "untied", state="running", host={"imageHash": "beef"})
    _run(reg, "live", state="running", host={"imageHash": _tree_hash(tree)})
    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))
    asked: list[str] = []
    monkeypatch.setattr(cli_main, "_container_liveness",
                        lambda name: asked.append(name) or cli_main.CONTAINER_RUNNING)
    found = cli_main._suggestions_midflight(tree, _tree_hash(tree))
    assert [r["runId"] for r in found] == ["live"]
    assert asked == [cli_main.job_container_name("live")]


def test_the_verb_asks_docker_one_question_and_no_api_at_all(tmp_path, tree):
    """Task 041's on-disk property, narrowed by exactly one docker call: the
    liveness question. No API, no `docker logs`, nothing that needs the run's
    container to still exist."""
    import inspect
    sources = "".join(inspect.getsource(fn) for fn in (
        cli_main.cmd_suggestions, cli_main._suggestions_midflight,
        cli_main._suggestions_tree_ties, cli_main._suggestions_conflicts))
    for forbidden in ("api(", "urllib", "DOCKER", "docker("):
        assert forbidden not in sources, forbidden
    assert sources.count("_container_liveness(") == 1
    reg = _registry(tmp_path)
    _run(reg, "mine", diff=CLEAN_DIFF)
    r = _ctl(reg, "suggestions", "mine", "--target", str(tree))
    assert r.returncode == 0 and "snapshot" not in r.stdout.lower()


# ----------------------------------------------------------------- doc claims
DOC_CLAIMS = ("--ignore-conflicts", "Never mid-flight", "imageHash",
              "mounted workspace", "no override flag", "docker inspect",
              "refused, not merged", "context-exact", "liveRuns",
              "conflicts}", "Almost purely on-disk",
              "tests/test_cli_suggestions_guards.py")


def _doc_problems(section: str) -> list[str]:
    return [claim for claim in DOC_CLAIMS if claim not in section]


def test_docs_cli_states_both_refusals_and_the_override():
    section = _doc_section(CLI_DOC.read_text(), DOC_SECTION)
    assert not _doc_problems(section), _doc_problems(section)
    assert cli_main.SUGGESTIONS_CONFLICT_HEADER in section


def test_the_docs_synopsis_names_every_flag_the_parser_accepts():
    """Derived from `build_parser`, not from a list here: a flag added to the verb
    and not to its heading is exactly how a documented synopsis goes stale."""
    sub = cli_main.build_parser()._subparsers._group_actions[0]
    flags = {opt for action in sub.choices["suggestions"]._actions
             for opt in action.option_strings if opt.startswith("--")
             and opt != "--help"}
    heading = _doc_section(CLI_DOC.read_text(), DOC_SECTION).splitlines()[0]
    missing = sorted(f for f in flags if f not in heading)
    assert not missing, f"docs/cli.md's synopsis omits {missing}"


@pytest.mark.parametrize("claim", DOC_CLAIMS)
def test_the_doc_check_catches_every_dropped_claim(claim):
    section = _doc_section(CLI_DOC.read_text(), DOC_SECTION)
    assert _doc_problems(section.replace(claim, "")) == [claim]


def test_the_spec_command_row_names_the_refusals_and_the_override():
    rows = [line for line in SPEC.read_text().splitlines()
            if line.startswith("| `suggestions ")]
    assert len(rows) == 1, rows
    assert "--ignore-conflicts" in rows[0]
    assert "live run" in rows[0] and "same file" in rows[0]


# The answer as SPEC §17 must record it: not a verdict, but the reasoning and the
# mechanism, so a later reader can tell what was decided from what.
ANSWER_CLAIMS = (
    "Should the reflection diff ever be applied automatically?",
    "answered by tasks 028, 041\n   and 029",
    "eight GitHub issues",             # the evidence: v0.6's diff, by hand
    "**Never mid-flight**",
    "`imageHash`",
    "mounted\n   workspace",
    "No override exists",
    "the **next**\n   `start`",
    "named, never merged",
    "context-exact",
    "`--ignore-conflicts`",
    "requirement P part 3",            # what it deliberately leaves open
)


def _answered_section() -> str:
    return SPEC.read_text().split("## 17. Answered questions")[1]


def test_the_open_question_moved_out_of_section_16_with_its_answer():
    """R's rule: a resolved open question is MOVED, with its answer and its
    reasoning, not rewritten in place inside §16."""
    text = SPEC.read_text()
    questions = text.split("## 16. Open questions")[1] \
                    .split("## 17. Answered questions")[0]
    assert "reflection diff ever be applied automatically" not in questions, \
        "the answered question is still sitting in §16"
    answered = _answered_section()
    missing = [claim for claim in ANSWER_CLAIMS if claim not in answered]
    assert not missing, missing
    # §16 keeps the questions that are genuinely open, and its numbering is
    # still a run of integers from 1
    numbers = [int(m.group(1)) for m in re.finditer(r"(?m)^(\d+)\. \*\*",
                                                    questions)]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    assert "Does the outage budget belong per-episode or per-job?" in questions


@pytest.mark.parametrize("claim", ANSWER_CLAIMS)
def test_the_answer_check_catches_every_dropped_element(claim):
    answered = _answered_section().replace(claim, "")
    assert [c for c in ANSWER_CLAIMS if c not in answered] == [claim]
