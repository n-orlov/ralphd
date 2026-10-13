"""v0.36 requirement C.2: cure rounds are counted PER APPROACH.

`cure_pass_ceiling` bounds the CURRENT approach's own round count, which
review (`curePasses`) and the peer verdict gate (`gateCurePasses`) draw on
together. A new approach starts at zero; `ralphctl budget <run> --cure-passes
+N` adds N to the current approach's allowance only; a same-set or superset
(no-progress) rejection with rounds left spends one round and cures instead
of archiving. The run-wide totals stay readable for display
(`_cure_passes_for_run`) but gate nothing -- every node below fails against
the 0.35 run-wide counter.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_cure_pass import (
    _cure_once,
    _cure_tasks,
    _finding,
    _first_cure_id,
    _rejects_with,
    _supervisor_bounded,
)

from ralphd.engine import findings
from ralphd.engine.state import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]


def _curable(clause: str) -> list[dict]:
    return [_finding("blocking", True, clause, "still broken")]


def _step(sup):
    return asyncio.run(asyncio.wait_for(sup._step_review(), 30))


def _reject_with(sup, items):
    findings.write_findings_json(sup.run.findings_json_file, items)
    reviewer = _rejects_with(items)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]


# -- 1. per-approach count, a fresh approach starts at zero -----------------

def test_fresh_approach_first_rejection_cures_though_an_earlier_one_spent_all(
        tmp_path):
    sup = _supervisor_bounded(tmp_path / "run", max_approaches=3,
                              cure_pass_ceiling=2)
    sup.run.update_status(curePasses={"1": 1}, gateCurePasses={"1": 1})
    sup._approach = 2
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    assert sup._cure_passes_for_run() == 2 == sup.cfg.cure_pass_ceiling

    _reject_with(sup, [_finding("blocking", True, "requirement X",
                                "approach two's first wall")])

    assert _step(sup) == ("advance", "cure", "rejected_curable_pass_available")
    status = sup.run.read_status()
    assert status["curePasses"] == {"1": 1, "2": 1}
    assert sup._cure_rounds_used(2) == 1
    assert sup._cure_rounds_used(1) == 2


def test_review_and_gate_draw_on_one_per_approach_count(tmp_path):
    sup = _supervisor_bounded(tmp_path / "run", max_approaches=3,
                              cure_pass_ceiling=2, peer_verdict_gate=True)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.update_status(curePasses={"1": 1})
    findings.write_findings_json(sup.run.findings_json_file,
                                 _curable("requirement A"))
    findings.write_findings_json(sup.run.verdict_gate_findings_json_file,
                                 _curable("requirement A"))
    assert sup._cure_rounds_used(1) == 1
    assert sup._gate_cure_pass_available(1) is True

    sup.run.update_status(gateCurePasses={"1": 1})
    assert sup._cure_rounds_used(1) == 2
    assert sup._gate_cure_pass_available(1) is False
    assert sup._review_cure_pass_available(1) is False
    # the other approach is untouched by approach 1's spend
    assert sup._cure_rounds_used(2) == 0
    assert sup._cure_pass_ceiling_spent(2) is False


# -- 2. a no-progress rejection spends a round and cures --------------------

def test_no_progress_rejection_with_rounds_left_spends_one_round_and_cures(
        tmp_path):
    sup = _cure_once(tmp_path / "run", approach=1, max_approaches=3,
                     cure_pass_ceiling=3)
    assert sup._cure_rounds_used(1) == 1

    # the identical wall restated: same clause set, reopening the cure task
    _reject_with(sup, [_finding("blocking", True, "requirement C.1 (#141)",
                                "still not fixed",
                                reopens=_first_cure_id(sup.run))])

    assert _step(sup) == ("advance", "cure", "rejected_curable_pass_available")
    assert sup.run.read_status()["curePasses"] == {"1": 2}
    assert sup._cure_rounds_used(1) == 2
    assert not sup.run.composite_prd_file.exists(), "never archived directly"


def test_no_progress_rejection_with_the_last_round_spent_is_not_a_cure(
        tmp_path):
    sup = _cure_once(tmp_path / "run", approach=1, max_approaches=3,
                     cure_pass_ceiling=2)
    items = [_finding("blocking", True, "requirement C.1 (#141)",
                      "still not fixed", reopens=_first_cure_id(sup.run))]
    _reject_with(sup, items)
    assert _step(sup)[2] == "rejected_curable_pass_available"  # round 2 of 2
    cure_ids = [t["id"] for t in _cure_tasks(sup.run)]

    _reject_with(sup, [_finding("blocking", True, "requirement C.1 (#141)",
                                "still not fixed", reopens=cure_ids[-1])])
    outcome = _step(sup)

    assert outcome[2] != "rejected_curable_pass_available"
    assert sup.run.read_status()["curePasses"] == {"1": 2}


# -- 3. --cure-passes +N lands on the current approach only -----------------

def test_cure_passes_topup_lands_on_the_current_approach_only(tmp_path):
    sup = _supervisor_bounded(tmp_path / "run", max_approaches=3,
                              cure_pass_ceiling=1)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.update_status(curePasses={"1": 1})
    assert sup._cure_pass_ceiling_spent() is True

    sup.set_cure_pass_ceiling(sup.cure_pass_allowance + 2)  # `+2`

    assert sup.run.read_status()["cureRoundTopUps"] == {"1": 2}
    assert sup._cure_rounds_allowed(1) == 3
    assert sup._cure_pass_ceiling_spent(1) is False
    # a later approach starts back at the configured base, top-up or not
    assert sup._cure_rounds_allowed(2) == 1
    sup._approach = 2
    assert sup.cure_pass_allowance == 1
    assert sup.cfg.cure_pass_ceiling == 1, "the base is not rewritten"


def test_topup_on_a_spent_approach_lets_the_next_rejection_cure(tmp_path):
    sup = _cure_once(tmp_path / "run", approach=1, max_approaches=3,
                     cure_pass_ceiling=1)
    _reject_with(sup, [_finding("blocking", True, "requirement C.1 (#141)",
                                "still not fixed",
                                reopens=_first_cure_id(sup.run))])
    sup.set_cure_pass_ceiling(sup.cure_pass_allowance + 1)

    assert _step(sup) == ("advance", "cure", "rejected_curable_pass_available")
    assert sup.run.read_status()["curePasses"] == {"1": 2}


# -- 4. run-wide totals stay readable ---------------------------------------

def test_run_wide_totals_stay_readable_for_display(tmp_path):
    sup = _supervisor_bounded(tmp_path / "run", max_approaches=3,
                              cure_pass_ceiling=1)
    sup.run.update_status(curePasses={"1": 1, "2": 1},
                          gateCurePasses={"2": 1})
    assert sup._cure_passes_for_run() == 3
    status = sup.run.read_status()
    assert status["curePasses"] == {"1": 1, "2": 1}
    assert status["gateCurePasses"] == {"2": 1}


# -- 5. docs ----------------------------------------------------------------

def test_spec_and_cli_docs_describe_the_per_approach_count():
    spec = (ROOT / "SPEC.md").read_text()
    assert "cureRoundTopUps" in spec
    assert "per approach" in spec
    cli = (ROOT / "docs" / "cli.md").read_text()
    section = cli.split("`--cure-passes`", 1)[1][:3000]
    assert "current approach" in section
