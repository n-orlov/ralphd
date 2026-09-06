"""Task 010 (requirement C, #112): OPEN impossibility petitions reach
review the same way `LoopSupervisor._operator_amendment_review_context`
already surfaces operator rulings -- `engine.petitions` (parse/render) and
the `_petitions_review_context`/`_review_extra_context` join point in
`engine/loop.py`, plus `HAT_SCHEMAS["review"]`'s new optional
`petition_adjudications` field.

Mirrors `tests/test_operator_ruling.py`'s own shape for the sibling
channel: parse-layer unit tests first, then the prompt-reaching test
(`test_open_petition_reaches_the_rendered_review_prompt`), then the join
test, then the contract-shape test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralphd.engine import contracts
from ralphd.engine import loop as loop_mod
from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory

OPEN_PETITION_MD = """# v0.15 petitions

## Requirement B, DoD predicate 9 cannot be met

**Obligation**: Requirement B, DoD predicate 9 (verbatim: "every finding is
included in the escalation").
**Type**: contradictory-clauses
**Evidence**: requirement A's own residual-finding carve-out means a
residual finding never appears in a BLOCKING escalation; the two clauses
cannot both hold.
"""

CONFIRMED_PETITION_MD = """# v0.14 petitions

## An already-adjudicated obligation

**Obligation**: Some earlier requirement.
**Type**: environmental
**Evidence**: no docker socket in this container.
**Adjudication**: CONFIRMED
"""

REFUTED_PETITION_MD = """# v0.13 petitions

## A refuted obligation

**Obligation**: Some other requirement.
**Type**: temporal
**Evidence**: claimed the deadline had passed.
**Adjudication**: REFUTED -- file the report before the deadline next time.
"""


# The shared harness (tests/conftest.py, G.1): this module's run dir and
# workspace sit side by side under the test's own tmp_path.
_supervisor = supervisor_factory(run_id="unit", run_root="run",
                                workspace="ws")



def test_open_petition_has_no_adjudication():
    parsed = petitions.parse_petitions(OPEN_PETITION_MD)
    assert len(parsed) == 1
    p = parsed[0]
    assert p["type"] == "contradictory-clauses"
    assert "DoD predicate 9" in p["obligation"]
    assert "residual-finding carve-out" in p["evidence"]
    assert p["adjudication"] is None
    assert petitions.open_petitions(OPEN_PETITION_MD) == parsed


def test_confirmed_petition_is_not_open():
    parsed = petitions.parse_petitions(CONFIRMED_PETITION_MD)
    assert parsed[0]["adjudication"] == "CONFIRMED"
    assert petitions.open_petitions(CONFIRMED_PETITION_MD) == []


def test_refuted_petition_carries_its_remedy_and_is_not_open():
    parsed = petitions.parse_petitions(REFUTED_PETITION_MD)
    assert parsed[0]["adjudication"] == "REFUTED"
    assert "file the report before the deadline" in parsed[0]["remedy"]
    assert petitions.open_petitions(REFUTED_PETITION_MD) == []


def test_a_heading_with_no_recognised_type_is_not_a_petition():
    text = "# Some report\n\nJust prose, no petition here.\n"
    assert petitions.parse_petitions(text) == []


def test_discover_petition_files_globs_artifacts_reports(tmp_path):
    reports = tmp_path / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.15-petitions.md").write_text(OPEN_PETITION_MD)
    (reports / "v0.15-operator-notifications.md").write_text("not a petition file")
    found = petitions.discover_petition_files(tmp_path)
    assert [f.name for f in found] == ["v0.15-petitions.md"]


def test_discover_petition_files_empty_when_no_reports_dir(tmp_path):
    assert petitions.discover_petition_files(tmp_path) == []


# -- LoopSupervisor._petitions_review_context() -------------------------------

def test_empty_when_workspace_has_no_petitions_file(tmp_path):
    sup = _supervisor(tmp_path)
    assert sup._petitions_review_context() == ""


def test_context_lists_the_open_petition_and_its_fields(tmp_path):
    reports = tmp_path / "ws" / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.15-petitions.md").write_text(OPEN_PETITION_MD)
    sup = _supervisor(tmp_path)
    ctx = sup._petitions_review_context()
    assert "Open impossibility petitions" in ctx
    assert "DoD predicate 9" in ctx
    assert "contradictory-clauses" in ctx
    assert "residual-finding carve-out" in ctx
    assert "petition_adjudications" in ctx


def test_context_omits_an_already_adjudicated_petition(tmp_path):
    reports = tmp_path / "ws" / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.14-petitions.md").write_text(CONFIRMED_PETITION_MD)
    sup = _supervisor(tmp_path)
    assert sup._petitions_review_context() == ""


def test_open_petition_reaches_the_rendered_review_prompt(tmp_path):
    reports = tmp_path / "ws" / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.15-petitions.md").write_text(OPEN_PETITION_MD)
    sup = _supervisor(tmp_path)
    extra = sup._petitions_review_context()
    prompt = sup.build_prompt("review", extra=extra)
    assert "DoD predicate 9" in prompt
    assert "residual-finding carve-out" in prompt


def test_review_extra_context_joins_petitions_alongside_amendments(tmp_path, monkeypatch):
    """`_review_extra_context()` is the one join point every review call
    site uses; the petitions block must show up beside whatever else is
    already firing, the same proof
    `test_review_extra_context_joins_flagged_criteria_and_amendments`
    already makes for the flagged-criteria/amendment pair."""
    from ralphd.engine.state import write_amendment

    reports = tmp_path / "ws" / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.15-petitions.md").write_text(OPEN_PETITION_MD)
    cdir = tmp_path / "config"
    write_amendment(cdir, "005", "Operator waiver.", waive=True)
    sup = _supervisor(tmp_path)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    ctx = sup._review_extra_context()
    assert "Operator rulings" in ctx
    assert "Open impossibility petitions" in ctx
    assert "DoD predicate 9" in ctx


# -- HAT_SCHEMAS["review"]: petition_adjudications ----------------------------

def test_review_verdict_accepts_petition_adjudications():
    payload = {
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": "Requirement B, DoD predicate 9",
             "adjudication": "REFUTED",
             "remedy": "Escalate every finding, not only blocking ones."},
        ],
    }
    assert contracts.validate_payload("review", payload) == []


def test_review_verdict_still_valid_with_no_petition_adjudications():
    assert contracts.validate_payload("review", {"verdict": "verified"}) == []


def test_petition_adjudications_must_be_an_array():
    payload = {"verdict": "verified", "petition_adjudications": "not an array"}
    errors = contracts.validate_payload("review", payload)
    assert errors
    assert "petition_adjudications" in errors[0]
