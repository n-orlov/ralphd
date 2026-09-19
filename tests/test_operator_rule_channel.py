"""Task 013 (v0.23 PRD requirement A.2, #224): the operator ANSWER
channel -- `ralphctl rule <run> --file ruling.md --name <slug>`, `POST
/rule` on the container API, and the hub run page's textarea -- all three
landing a ruling into ONE run-scoped cumulative record keyed to
`LoopSupervisor._review_rejection_signature`'s own signature text, never a
task id, so a replan that restarts task ids at 001 never orphans it.

This module pins the engine half against a real supervisor fixture
(`tests/conftest.py::make_supervisor`, never this run's own directory --
this run's engine is 0.22.0 and holds none of this code):

  * `engine.state.write_ruling`/`ruling_entries` -- the storage primitive,
    a plain function over `run_root`, same shape as `write_amendment`/
    `amendment_entries` but landing under `<run_root>/rulings/` (bind-
    mounted read-write) rather than `<config_root>/amendments/` (the
    host's `:ro` mount) -- deliberately, because a `POST /rule` handled
    IN-container has to be able to write it;
  * `RunDir.add_ruling` -- resolves the ruling's `signature` key from the
    run's OWN `status.json` (`rejectionSignatures[str(approach)]`), never
    from a caller-supplied value;
  * `LoopSupervisor._operator_ruling_review_context` -- renders every
    recorded ruling as authoritative extra context, and reaches BOTH a
    subsequently-built `review` prompt and a subsequently-built `verify`
    prompt;
  * survival across a replan that restarts task ids at 001 -- the record
    resolves unchanged because nothing in it ever named a task id.

The CLI surface (`ralphctl rule`, `POST /rule` end to end) and the hub
proxy are pinned separately in `tests/test_cli_rule.py`/
`tests/test_hub_rule.py`.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine.state import ruling_entries, write_ruling
from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="unit", run_root="run", workspace="ws")


# -- engine.state: write_ruling / ruling_entries -----------------------------

def test_write_ruling_assigns_monotonic_seq_and_round_trips(tmp_path):
    rroot = tmp_path / "run"
    f1 = write_ruling(rroot, "First ruling.", signature="sig-a", name="one")
    f2 = write_ruling(rroot, "Second ruling.", name="two")
    assert f1 == "001-one.md"
    assert f2 == "002-two.md"
    entries = ruling_entries(rroot)
    assert [e["file"] for e in entries] == ["001-one.md", "002-two.md"]
    assert entries[0]["signature"] == "sig-a"
    assert entries[0]["text"] == "First ruling."
    # No `signature=` at all -> `None`, never the literal string "None".
    assert entries[1]["signature"] is None
    assert entries[1]["text"] == "Second ruling."


def test_ruling_entries_empty_when_no_rulings_dir_at_all(tmp_path):
    assert ruling_entries(tmp_path / "run") == []


def test_write_ruling_lands_under_run_root_not_config(tmp_path):
    """The write path is `<run_root>/rulings/`, deliberately NOT
    `config_root` (unlike `write_amendment`): a `POST /rule` handled
    in-container must be able to write it, and `config_root` is the
    host's `:ro` mount."""
    rroot = tmp_path / "runs" / "r1"
    write_ruling(rroot, "text")
    assert (rroot / "rulings" / "001-ruling.md").exists()
    assert not (tmp_path / "configs").exists()


def test_name_with_engine_prefix_already_present_is_not_doubled(tmp_path):
    rroot = tmp_path / "run"
    fname = write_ruling(rroot, "text", name="003-copied")
    assert fname == "001-copied.md"


# -- RunDir.add_ruling: signature resolved from status.json ------------------

def test_add_ruling_resolves_signature_from_current_approachs_entry(tmp_path):
    sup = _supervisor(tmp_path)
    sup.run.update_status(approach=2, rejectionSignatures={"2": "the-wall"})
    fname = sup.run.add_ruling("Ship it, these are residuals.")
    entries = sup.run.ruling_entries()
    assert len(entries) == 1
    assert entries[0]["signature"] == "the-wall"
    assert entries[0]["file"] == fname


def test_add_ruling_with_no_recorded_signature_is_unscoped(tmp_path):
    sup = _supervisor(tmp_path)
    sup.run.update_status(approach=1)
    sup.run.add_ruling("Filed ahead of any rejection.")
    entries = sup.run.ruling_entries()
    assert entries[0]["signature"] is None


# -- LoopSupervisor._operator_ruling_review_context --------------------------

def test_empty_when_no_ruling_has_ever_been_recorded(tmp_path):
    sup = _supervisor(tmp_path)
    assert sup._operator_ruling_review_context() == ""


def test_context_names_it_authoritative_and_carries_the_text(tmp_path):
    sup = _supervisor(tmp_path)
    write_ruling(sup.run.root, "These three findings are residuals; ship it.",
                signature="wall-1")
    ctx = sup._operator_ruling_review_context()
    assert "Operator rulings" in ctx
    assert "authoritative" in ctx.lower()
    assert "wall-1" in ctx
    assert "These three findings are residuals; ship it." in ctx


def test_context_reaches_the_rendered_review_prompt(tmp_path):
    sup = _supervisor(tmp_path)
    write_ruling(sup.run.root, "Waived: no docker socket in this env.",
                signature="wall-2")
    extra = sup._operator_ruling_review_context()
    prompt = sup.build_prompt("review", extra=extra)
    assert "Waived: no docker socket in this env." in prompt


def test_context_reaches_the_rendered_verify_prompt(tmp_path):
    """A.2's own cure condition names this explicitly: verify reads
    `tasks.json` and honours no amendment today, so a ruling that only
    reached review would come back as a verify failure."""
    sup = _supervisor(tmp_path)
    write_ruling(sup.run.root, "Accept the stub implementation for task 004.",
                signature="wall-3")
    extra = sup._operator_ruling_review_context(audience="verify")
    prompt = sup.build_prompt("verify", extra=extra, prompt_name="task-verify")
    assert "Accept the stub implementation for task 004." in prompt


def test_review_extra_context_includes_the_ruling(tmp_path):
    """`_review_extra_context()` is the one join point every review call
    site uses; a ruling must show up through it, not only through the
    standalone method."""
    sup = _supervisor(tmp_path)
    write_ruling(sup.run.root, "Operator ruling via the shared join point.",
                signature="wall-4")
    ctx = sup._review_extra_context()
    assert "Operator ruling via the shared join point." in ctx


def test_multiple_rulings_all_render_cumulatively(tmp_path):
    sup = _supervisor(tmp_path)
    write_ruling(sup.run.root, "First ruling text.", signature="s1")
    write_ruling(sup.run.root, "Second ruling text.", signature="s2")
    ctx = sup._operator_ruling_review_context()
    assert "First ruling text." in ctx
    assert "Second ruling text." in ctx


# -- survives a replan that restarts task ids at 001 -------------------------

def test_ruling_record_survives_a_replan_that_restarts_task_ids(tmp_path):
    """The whole point of keying the record to a signature rather than a
    task id: after a replan, `tasks.json` restarts at "001" for a
    completely different task, and the ruling still resolves unchanged --
    there is no task-membership check for a replan to break."""
    sup = _supervisor(tmp_path, tasks=[
        {"id": "001", "title": "old plan task", "status": "validated",
         "successCriteria": "old"},
    ])
    write_ruling(sup.run.root, "Ruling recorded against approach 1's wall.",
                signature="repeat-wall")
    before = sup._operator_ruling_review_context()
    assert "Ruling recorded against approach 1's wall." in before

    # Simulate the replan: a brand-new plan whose ids restart at 001 for an
    # entirely different task -- the shape a fresh approach's planning
    # phase produces.
    sup.run.tasks_file.write_text(
        '{"version": 1, "tasks": [{"id": "001", "title": "brand new task", '
        '"status": "pending", "successCriteria": "new"}]}')

    after = sup._operator_ruling_review_context()
    assert after == before
    assert "Ruling recorded against approach 1's wall." in after
