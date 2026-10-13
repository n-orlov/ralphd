"""v0.36 (#304): the clause-bound operator waiver store and its authoritative
"Operator waivers" section in the review, peer-verdict-gate and planning
prompts.

A waiver lives at `<run_root>/waivers/NNN.json`, bound to a PRD clause and the
finding text, never to a task id, so an approach archive plus a replan that
renumbers every task id leaves all of them readable by all three judges.
"""

from __future__ import annotations

import json

import pytest

from ralphd.engine.state import waiver_entries, write_waiver
from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="unit", run_root="run", workspace="ws")

WAIVERS = [
    dict(clause="DoD 4: the nightly lane concludes success",
         finding="Nightly run 123 was cancelled by a runner shortage.",
         judge="review", note="Infra, not a defect; accepted."),
    dict(clause="C.2: coverage of the retired pause reason",
         finding="No node pins the retired timed_out reason.\nSecond line.",
         judge="verdict_gate", note=""),
    dict(clause="Req 9 / criterion (3)",
         finding="Planning split the task into more than five clauses.",
         judge="planning", note="Fine as written."),
]


def _store(root):
    return [write_waiver(root, **w) for w in WAIVERS]


def test_writer_stores_one_file_per_waiver_with_every_field(tmp_path):
    names = _store(tmp_path / "run")
    assert names == ["001.json", "002.json", "003.json"]
    files = sorted((tmp_path / "run" / "waivers").iterdir())
    assert [f.name for f in files] == names
    for f, w in zip(files, WAIVERS):
        rec = json.loads(f.read_text())
        assert {k: rec[k] for k in ("clause", "finding", "judge", "note")} == w
        assert rec["ts"]


def test_reader_returns_every_stored_waiver_and_none_when_absent(tmp_path):
    assert waiver_entries(tmp_path / "run") == []
    _store(tmp_path / "run")
    got = waiver_entries(tmp_path / "run")
    assert [(e["clause"], e["finding"], e["judge"], e["note"]) for e in got] == [
        (w["clause"], w["finding"], w["judge"], w["note"]) for w in WAIVERS]
    assert [e["seq"] for e in got] == [1, 2, 3]


def test_a_waiver_without_a_clause_or_finding_is_refused(tmp_path):
    with pytest.raises(ValueError):
        write_waiver(tmp_path, clause=" ", finding="x", judge="review")
    with pytest.raises(ValueError):
        write_waiver(tmp_path, clause="c", finding="", judge="review")
    assert waiver_entries(tmp_path) == []


def test_waivers_survive_an_archive_and_a_renumbering_replan_into_all_three_prompts(tmp_path):
    sup = _supervisor(tmp_path, prd=True, tasks=[
        {"id": "001", "title": "old", "status": "validated", "successCriteria": "old"},
        {"id": "002", "title": "old two", "status": "validated", "successCriteria": "old"},
    ])
    for w in WAIVERS:
        sup.run.add_waiver(**w)

    sup._archive_approach(1)
    # The replan: a new plan whose ids are all different from approach 1's.
    sup.run.tasks_file.write_text(json.dumps({"version": 1, "tasks": [
        {"id": "101", "title": "new", "status": "pending", "successCriteria": "n"},
        {"id": "102", "title": "new two", "status": "pending", "successCriteria": "n"},
    ]}))

    for phase in ("review", "verdict_gate", "planning"):
        prompt = sup.build_prompt(phase)
        assert "## Operator waivers (authoritative)" in prompt, phase
        assert "authoritative and unconditional" in prompt, phase
        for w in WAIVERS:
            assert w["clause"] in prompt, (phase, w["clause"])
            assert w["finding"] in prompt, (phase, w["finding"])
            assert w["judge"] in prompt, (phase, w["judge"])
            if w["note"]:
                assert w["note"] in prompt, (phase, w["note"])


def test_the_section_renders_empty_when_nothing_is_stored(tmp_path):
    sup = _supervisor(tmp_path, prd=True)
    assert sup._operator_waivers_context() == ""


@pytest.mark.parametrize("name", ["review", "verdict_gate", "planning"])
def test_each_prompt_template_carries_an_authoritative_operator_waivers_section(name):
    from ralphd.engine.loop import PROMPTS_BUILTIN
    text = (PROMPTS_BUILTIN / f"{name}.md").read_text()
    assert "\n## Operator waivers\n" in text
    section = text.split("\n## Operator waivers\n", 1)[1]
    assert "authoritative and unconditional" in section
