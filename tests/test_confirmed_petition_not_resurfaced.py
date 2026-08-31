"""Task 001 (requirement C.4, #112): an obligation already CONFIRMED at
RUN scope (`petitions.confirmed_obligations`/`record_confirmed_
adjudications`, task 011) must not be re-surfaced to review as an OPEN
petition just because its own `<wave>-petitions.md` filing's
`Adjudication` field is still blank -- a per-file, workspace fact that
task 011's run-scope record has already overtaken. PRD requirement C.4:
"a CONFIRMED petition removes its obligation from the blocking set" for
the REST OF THE RUN, not only the review pass that confirmed it, so a
'MUST adjudicate' instruction naming an already-settled obligation on
every later review pass would either waste a rehearing or invite review
to flip a CONFIRMED call back open.

Wiring lives engine-side, not test-side:
`LoopSupervisor._petitions_review_context` (`src/ralphd/engine/loop.py`)
passes `self.run` through to `engine.petitions.petitions_review_context`,
which is the one place the exclusion is applied
(`_obligation_confirmed`/`confirmed_obligations`).

Three directions pinned:
1. a CONFIRMED obligation is dropped from the rendered context while a
   second, unconfirmed obligation in the SAME file still renders;
2. a tampered record (`confirmed_obligations` returning `tampered=True`)
   surfaces the obligation again -- fail towards adjudication, never
   towards a manufactured exemption;
3. `run=None` (every pre-existing caller) is unaffected -- behaviour is
   byte-for-byte the same as before this task.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, read_json

TWO_OPEN_PETITIONS_MD = """# v0.15 petitions

## Requirement B, DoD predicate 9 cannot be met

**Obligation**: Requirement B, DoD predicate 9 (verbatim: "every finding is
included in the escalation").
**Type**: contradictory-clauses
**Evidence**: requirement A's own residual-finding carve-out means a
residual finding never appears in a BLOCKING escalation; the two clauses
cannot both hold.

## A second, unrelated obligation

**Obligation**: A second, wholly unrelated obligation nobody has ruled on.
**Type**: environmental
**Evidence**: unrelated evidence for the unrelated obligation.
"""


def _supervisor(tmp_path: Path) -> LoopSupervisor:
    run = RunDir(root=tmp_path / "run")
    cfg = JobConfig(run_id="task001-confirmed-not-resurfaced")
    return LoopSupervisor(cfg, run, tmp_path / "ws")


def _write_two_open_petitions(tmp_path: Path) -> None:
    reports = tmp_path / "ws" / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.15-petitions.md").write_text(TWO_OPEN_PETITIONS_MD)


# ---------------------------------------------------------------------------
# Direction 1: a CONFIRMED obligation is excluded; the unconfirmed one stays.
# ---------------------------------------------------------------------------

def test_confirmed_obligation_is_not_resurfaced_but_the_other_one_still_is(
        tmp_path):
    _write_two_open_petitions(tmp_path)
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    ctx = sup._petitions_review_context()
    assert "DoD predicate 9" not in ctx
    assert "wholly unrelated obligation" in ctx
    assert "MUST" in ctx  # the surviving instruction is still present


def test_engine_petitions_function_itself_honours_the_run_argument(tmp_path):
    """The same exclusion, called directly at the `engine.petitions`
    layer (no supervisor involved) -- pins that the wiring lives in
    `petitions_review_context` itself, not merely in the loop.py call
    site."""
    _write_two_open_petitions(tmp_path)
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    ctx = petitions.petitions_review_context(tmp_path / "ws", run)
    assert "DoD predicate 9" not in ctx
    assert "wholly unrelated obligation" in ctx


def test_with_no_confirmed_obligations_both_petitions_still_render(tmp_path):
    _write_two_open_petitions(tmp_path)
    sup = _supervisor(tmp_path)
    ctx = sup._petitions_review_context()
    assert "DoD predicate 9" in ctx
    assert "wholly unrelated obligation" in ctx


# ---------------------------------------------------------------------------
# Direction 2: a tampered record fails towards adjudication, not exemption.
# ---------------------------------------------------------------------------

def test_a_tampered_record_surfaces_the_obligation_again(tmp_path):
    _write_two_open_petitions(tmp_path)
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    # simulate a worker-phase hand-edit of the run-scope record, exactly
    # as tests/test_petition_adjudications_run_scope.py's own tamper
    # probes do -- append a fabricated entry, recompute the unkeyed
    # digest, and write it back WITHOUT going through
    # `record_confirmed_adjudications`.
    doc = read_json(sup.run.petition_adjudications_file)
    doc["entries"].append({"obligation": "A fabricated exemption",
                            "adjudication": "CONFIRMED"})
    doc["digest"] = petitions._adjudications_digest(doc["entries"])
    sup.run.petition_adjudications_file.write_text(json.dumps(doc))

    confirmed, tampered = petitions.confirmed_obligations(sup.run)
    assert tampered is True
    assert confirmed == set()

    ctx = sup._petitions_review_context()
    assert "DoD predicate 9" in ctx  # surfaced again, not exempted
    assert "wholly unrelated obligation" in ctx


# ---------------------------------------------------------------------------
# Direction 3: `run=None` (every pre-existing caller) is unaffected.
# ---------------------------------------------------------------------------

def test_run_none_keeps_prior_behaviour_unchanged(tmp_path):
    _write_two_open_petitions(tmp_path)
    without_run = petitions.petitions_review_context(tmp_path / "ws")
    assert "DoD predicate 9" in without_run
    assert "wholly unrelated obligation" in without_run
