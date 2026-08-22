"""A crashed iteration's transcript survives the resume (task 019, #44).

`RunDir.max_iteration_number()` counts only iterations whose `meta.json` has
an `endedAt`, so the slot of an engine killed mid-iteration is handed to the
resumed engine's next attempt. That rule is deliberate (monotonic numbering,
no gaps, no renumbering) -- but until this task the reused slot was simply
written over, and the prompt, transcript and partial `meta.json` of the very
iteration an operator most wants to read were gone by the time they looked
(the run this task was written in lost iteration 38 that way).

`RunDir.begin_iteration_dir()` now moves whatever an earlier attempt left in
the slot into `iterations/NNNN/attempts/NN/` first -- same 2-digit,
oldest-first shape as `approaches/NN/` -- and says so once
(`iteration.attempt_archived`, `archivedAttempts` on the detail views).

Covered here: the archiving helper (numbering, generality, the untouched
slot, best effort), that an archive is never mistaken for an iteration of its
own, the detail/render surfaces, a REAL engine SIGKILLed mid-worker-iteration
and resumed, and the doc claims.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from pathlib import Path

import pytest
from test_e2e import engine_factory

from ralphd.engine.state import (
    RunDir,
    iteration_detail,
    iteration_summary_lines,
)
from ralphd.log_merge import (
    ITERATION_ATTEMPTS_DIR,
    iteration_attempt_dirs,
    iteration_numbers,
)

__all__ = ["engine_factory"]

REPO = Path(__file__).resolve().parent.parent


def _run(tmp_path) -> RunDir:
    return RunDir(tmp_path / "run")


def _write_attempt(itdir: Path, tag: str, ended: bool = False) -> None:
    (itdir / "prompt.md").write_text(f"prompt {tag}")
    (itdir / "output.jsonl").write_text(
        json.dumps({"type": "text", "text": tag}) + "\n")
    meta = {"number": int(itdir.name), "phase": "worker", "tag": tag,
            "startedAt": "2026-01-01T00:00:00Z"}
    if ended:
        meta["endedAt"] = "2026-01-01T00:10:00Z"
    (itdir / "meta.json").write_text(json.dumps(meta))


def _events(run: RunDir) -> list[dict]:
    path = run.root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- the archiving helper ---------------------------------------------------
def test_a_reused_slots_earlier_attempt_is_archived_not_overwritten(tmp_path):
    run = _run(tmp_path)
    first = run.begin_iteration_dir(7)
    _write_attempt(first, "crashed")

    second = run.begin_iteration_dir(7)
    assert second == first, "the slot number itself is still reused"
    archived = iteration_attempt_dirs(run.root, 7)
    assert [d.name for d in archived] == ["01"]
    assert (archived[0] / "prompt.md").read_text() == "prompt crashed"
    assert (archived[0] / "output.jsonl").read_text().strip().endswith('"crashed"}')
    assert json.loads((archived[0] / "meta.json").read_text())["tag"] == "crashed"
    # ...and the live slot is empty for the new attempt, holding only the archive
    assert sorted(p.name for p in second.iterdir()) == [ITERATION_ATTEMPTS_DIR]


def test_the_new_attempts_own_files_do_not_touch_the_archived_ones(tmp_path):
    run = _run(tmp_path)
    _write_attempt(run.begin_iteration_dir(7), "crashed")
    _write_attempt(run.begin_iteration_dir(7), "resumed", ended=True)

    live = json.loads((run.iteration_dir(7) / "meta.json").read_text())
    assert live["tag"] == "resumed" and live["endedAt"]
    old = json.loads(
        (iteration_attempt_dirs(run.root, 7)[0] / "meta.json").read_text())
    assert old["tag"] == "crashed" and "endedAt" not in old


def test_a_third_attempt_archives_beside_the_second_oldest_first(tmp_path):
    run = _run(tmp_path)
    for tag in ("first", "second", "third"):
        _write_attempt(run.begin_iteration_dir(9), tag)
    names = [d.name for d in iteration_attempt_dirs(run.root, 9)]
    assert names == ["01", "02"], "2-digit, oldest first, like approaches/NN"
    tags = [json.loads((d / "meta.json").read_text())["tag"]
            for d in iteration_attempt_dirs(run.root, 9)]
    assert tags == ["first", "second"]
    assert json.loads((run.iteration_dir(9) / "meta.json").read_text())["tag"] == "third"


def test_an_untouched_slot_archives_nothing_and_says_nothing(tmp_path):
    run = _run(tmp_path)
    run.begin_iteration_dir(1)
    run.begin_iteration_dir(2)
    assert iteration_attempt_dirs(run.root, 1) == []
    assert not (run.iteration_dir(1) / ITERATION_ATTEMPTS_DIR).exists()
    assert [e for e in _events(run) if e["type"] == "iteration.attempt_archived"] == []


def test_archiving_moves_whatever_the_attempt_left_not_a_list_of_names(tmp_path):
    """A record that grows a fourth file is archived too -- the helper moves
    everything in the slot rather than three hard-coded names."""
    run = _run(tmp_path)
    itdir = run.begin_iteration_dir(3)
    _write_attempt(itdir, "crashed")
    (itdir / "surprise.txt").write_text("a future field")
    (itdir / "subdir").mkdir()
    (itdir / "subdir" / "deep.json").write_text("{}")

    run.begin_iteration_dir(3)
    archived = iteration_attempt_dirs(run.root, 3)[0]
    assert sorted(p.name for p in archived.iterdir()) == [
        "meta.json", "output.jsonl", "prompt.md", "subdir", "surprise.txt"]
    assert (archived / "subdir" / "deep.json").exists()


def test_the_archive_is_announced_once_with_what_moved(tmp_path):
    run = _run(tmp_path)
    _write_attempt(run.begin_iteration_dir(7), "crashed")
    run.begin_iteration_dir(7)
    evs = [e for e in _events(run) if e["type"] == "iteration.attempt_archived"]
    assert len(evs) == 1
    ev = evs[0]
    assert ev["number"] == 7
    assert ev["attempt"] == 1
    assert ev["path"] == f"iterations/0007/{ITERATION_ATTEMPTS_DIR}/01"
    assert ev["files"] == ["meta.json", "output.jsonl", "prompt.md"]


def test_an_unarchivable_slot_still_runs_its_iteration(tmp_path, monkeypatch):
    """Best effort: losing a crashed transcript is bad, refusing to run the
    next iteration over it is worse."""
    run = _run(tmp_path)
    _write_attempt(run.begin_iteration_dir(4), "crashed")

    def boom(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr("ralphd.engine.state.shutil.move", boom)
    itdir = run.begin_iteration_dir(4)  # must not raise
    assert itdir == run.iteration_dir(4)
    assert [e for e in _events(run) if e["type"] == "iteration.attempt_archived"] == []


# -- an archive is a record OF an iteration, never an extra one ------------
def test_an_archived_attempt_is_not_counted_as_an_iteration(tmp_path):
    run = _run(tmp_path)
    _write_attempt(run.begin_iteration_dir(1), "done", ended=True)
    _write_attempt(run.begin_iteration_dir(2), "crashed")
    run.begin_iteration_dir(2)

    assert run.max_iteration_number() == 1, (
        "the unfinished slot is still not counted -- the numbering rule is "
        "preserved, only the record is kept")
    assert iteration_numbers(run.root) == [1, 2], "attempts/ is not an iteration"


def test_a_stray_name_under_attempts_is_ignored_not_counted(tmp_path):
    """`attempts/` is read like `iterations/` itself: a name that is not a
    number is ignored rather than counted or raising, so a stray file cannot
    inflate `archivedAttempts` or break a reader."""
    run = _run(tmp_path)
    _write_attempt(run.begin_iteration_dir(6), "crashed")
    _write_attempt(run.begin_iteration_dir(6), "resumed", ended=True)
    attempts = run.iteration_dir(6) / ITERATION_ATTEMPTS_DIR
    (attempts / "README").write_text("not an attempt")
    (attempts / "scratch").mkdir()

    assert [d.name for d in iteration_attempt_dirs(run.root, 6)] == ["01"]
    assert iteration_detail(run.root, 6)["archivedAttempts"] == 1


# -- the detail / render surfaces ------------------------------------------
def test_iteration_detail_counts_the_archived_attempts(tmp_path):
    run = _run(tmp_path)
    _write_attempt(run.begin_iteration_dir(5), "crashed")
    _write_attempt(run.begin_iteration_dir(5), "resumed", ended=True)
    detail = iteration_detail(run.root, 5)
    assert detail["archivedAttempts"] == 1
    assert detail["endedAt"], "the detail is the newest attempt's"


def test_a_single_attempt_iteration_reports_zero_and_renders_no_extra_line(tmp_path):
    run = _run(tmp_path)
    _write_attempt(run.begin_iteration_dir(5), "only", ended=True)
    detail = iteration_detail(run.root, 5)
    assert detail["archivedAttempts"] == 0
    assert not [ln for ln in iteration_summary_lines(detail)
                if ln.startswith("attempts:")]


@pytest.mark.parametrize("count,word", [(1, "attempt"), (2, "attempts")])
def test_the_summary_names_the_archived_attempts(tmp_path, count, word):
    run = _run(tmp_path)
    for i in range(count):
        _write_attempt(run.begin_iteration_dir(5), f"crash{i}")
    _write_attempt(run.begin_iteration_dir(5), "resumed", ended=True)
    lines = [ln for ln in iteration_summary_lines(iteration_detail(run.root, 5))
             if ln.startswith("attempts:")]
    assert len(lines) == 1
    assert f"{count} earlier {word} archived" in lines[0]
    assert f"{ITERATION_ATTEMPTS_DIR}/" in lines[0]


# -- a real engine, killed mid-iteration and resumed -----------------------
def _wait_for(predicate, timeout=30, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError("condition never became true")


def test_a_sigkilled_iterations_three_files_survive_the_resume(engine_factory):
    """The whole point, black box: SIGKILL a real engine mid-worker-iteration,
    resume over the same run dir, and read the crashed attempt's `prompt.md`,
    `output.jsonl` and `meta.json` back afterwards -- byte for byte, with the
    resumed attempt's own files sitting in the same slot."""
    e1 = engine_factory(
        job={"on_complete": "idle", "iterations": 15, "max_approaches": 1},
        # STUB_RICH_EVENTS makes the worker emit (and flush) a tool-call
        # preamble BEFORE it starts "working", so the 5s window this test kills
        # inside has a partially written transcript on disk -- the file whose
        # loss motivated #44. Without it the stub prints only at the very end
        # and there would be nothing to preserve.
        stub_env={"STUB_TASKS": "3", "STUB_SLEEP": "5", "STUB_RICH_EVENTS": "1"},
    )
    e1.wait_api()

    def crashed_worker():
        itdir = e1.run_dir / "iterations"
        if not itdir.is_dir():
            return None
        for d in sorted(itdir.iterdir(), reverse=True):
            meta_path = d / "meta.json"
            out = d / "output.jsonl"
            if not (meta_path.exists() and out.exists() and out.stat().st_size):
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                continue
            if meta.get("phase") == "worker" and "endedAt" not in meta:
                return d
        return None

    # wait until the worker iteration has a prompt, a partial transcript and a
    # startedAt-only meta.json -- i.e. all three files are worth preserving
    victim = _wait_for(crashed_worker, timeout=30)
    number = int(victim.name)
    before = {name: (victim / name).read_bytes()
              for name in ("prompt.md", "output.jsonl", "meta.json")}
    assert b"endedAt" not in before["meta.json"]

    pid = e1.proc.pid  # this specific pid, never a pattern
    os.kill(pid, signal.SIGKILL)
    e1.proc.wait(timeout=10)

    # Resume: a fresh engine over the SAME run dir (what `ralphctl resume` does)
    e2 = engine_factory(
        job={"on_complete": "exit", "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "3", "STUB_SLEEP": "0", "STUB_RICH_EVENTS": "1"},
    )
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0

    # the slot number was reused, as before...
    numbers = iteration_numbers(e2.run_dir)
    assert numbers == sorted(numbers) and len(numbers) == len(set(numbers))
    assert number in numbers
    live = json.loads(
        (e2.run_dir / "iterations" / f"{number:04d}" / "meta.json").read_text())
    assert "endedAt" in live, "the resumed attempt finished in that slot"

    # ...and all three pre-crash files are still readable, unmodified
    archived = iteration_attempt_dirs(e2.run_dir, number)
    assert len(archived) == 1, f"expected one archived attempt, got {archived}"
    for name, blob in before.items():
        assert (archived[0] / name).read_bytes() == blob, name
    crashed_meta = json.loads((archived[0] / "meta.json").read_text())
    assert crashed_meta["phase"] == "worker" and "endedAt" not in crashed_meta

    # the archive is announced, and counted by the detail surface
    evs = [json.loads(line)
           for line in (e2.run_dir / "events.jsonl").read_text().splitlines()
           if line.strip()]
    archived_evs = [ev for ev in evs
                    if ev["type"] == "iteration.attempt_archived"
                    and ev["number"] == number]
    assert len(archived_evs) == 1
    assert set(archived_evs[0]["files"]) >= {"prompt.md", "output.jsonl", "meta.json"}
    assert iteration_detail(e2.run_dir, number)["archivedAttempts"] == 1

    # the run still completed, and the archive did not inflate the budget
    status = json.loads((e2.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["iterationsUsed"] == max(numbers)


# -- the documented claims ------------------------------------------------
def _fenced_blocks(text: str) -> list[str]:
    return text.split("```")[1::2]


def _run_dir_tree(doc: str) -> str:
    """The doc's run-dir layout listing (the fenced block spelling the
    per-iteration files under `iterations/`), so a claim about the layout is
    checked where the layout is actually spelled -- not anywhere in a
    4000-line file."""
    blocks = [b for b in _fenced_blocks((REPO / doc).read_text())
              if "iterations/" in b and "approaches/" in b and "prompt.md" in b]
    assert blocks, f"{doc}: no run-dir tree found"
    return blocks[0]


@pytest.mark.parametrize("doc", ["SPEC.md", "docs/architecture.md"])
def test_the_run_dir_layout_shows_the_archive(doc):
    tree = _run_dir_tree(doc)
    assert f"{ITERATION_ATTEMPTS_DIR}/" in tree, doc


@pytest.mark.parametrize("doc", ["SPEC.md", "docs/api.md"])
def test_the_event_is_listed_in_the_event_table(doc):
    text = (REPO / doc).read_text()
    rows = re.findall(r"^\| `iteration\.attempt_archived` \|.*$", text, re.MULTILINE)
    assert len(rows) == 1, f"{doc}: {rows}"
    assert ITERATION_ATTEMPTS_DIR in rows[0], rows[0]


@pytest.mark.parametrize("doc", ["SPEC.md", "docs/api.md", "docs/cli.md"])
def test_the_detail_field_is_documented(doc):
    assert "archivedAttempts" in (REPO / doc).read_text(), doc


def test_the_json_field_list_names_the_new_field():
    """`ralphctl iteration --json`'s documented key list is exhaustive -- the
    bullet that enumerates it must name `archivedAttempts` too."""
    bullets = [b for b in re.findall(r"^- `--json` prints.*?(?=^- |\Z)",
                                     (REPO / "docs" / "cli.md").read_text(),
                                     re.MULTILINE | re.DOTALL)
               if "`meta.json` verbatim" in b]
    assert len(bullets) == 1, bullets
    assert "archivedAttempts" in bullets[0]


def test_the_cli_bullet_names_where_the_archive_lives():
    bullets = re.findall(r"^- \*\*`attempts:`\*\*.*?(?=^- |\Z)",
                         (REPO / "docs" / "cli.md").read_text(), re.MULTILINE | re.DOTALL)
    assert len(bullets) == 1, bullets
    assert f"iterations/NNNN/{ITERATION_ATTEMPTS_DIR}/NN/" in bullets[0]
