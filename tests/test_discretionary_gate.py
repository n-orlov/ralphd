"""Task 017 (PRD requirements A.3/A.7, #224): trigger conditions 2 and 3
over their SHARED discretionary gate, the six-item A.7 escalation payload,
and `state.cure_pass_preview` (task 005's own pure predicate, task 006's
`invalidates_reopen_walk`) that both feed off.

Trigger condition 2 -- a cure pass that would re-open task(s) already
`validated` -- and trigger condition 3 -- a rejection whose blocking-clause
signature repeats the immediately preceding approach's own recorded
signature -- draw from ONE `status.json`-backed counter per approach
(`LoopSupervisor._discretionary_gate_available`/`_spend_discretionary_
gate`): whichever fires first in an approach spends it, the other (should
it also fire later in the SAME approach) falls through to its ordinary
edge instead, and a fresh approach reads the budget unspent again.
Condition 1's own `operator_input_needed` gate (task 014) is a SEPARATE,
unbudgeted pause and is unaffected either way.

Modelled on `tests/test_ruling_re_park_bound.py`'s own multi-episode shape
for the "condition 2 then condition 3" (and its reverse) probes: a FRESH
`LoopSupervisor` per rejection episode over the SAME on-disk run dir,
never shared Python state across episodes.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    RunDir,
    atomic_write_json,
    cure_pass_preview,
    insert_cure_tasks,
)

from tests.conftest import supervisor_factory


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _finding(severity: str, curable: bool, clause: str, evidence: str,
             cured_when: str | None = None, invalidates=None) -> dict:
    payload = {"severity": severity, "curable": curable, "clause": clause,
               "evidence": evidence}
    if cured_when is None and severity == "blocking" and curable is True:
        cured_when = f"{clause} is discharged"
    if cured_when is not None:
        payload["cured_when"] = cured_when
    if invalidates is not None:
        payload["invalidates"] = invalidates
    return payload


def _task(tid: str, *, status: str = "pending", depends: list | None = None,
          criteria: str = "n/a") -> dict:
    return {"id": tid, "title": tid, "successCriteria": criteria,
            "status": status, "origin": "planner",
            "dependsOn": list(depends or []), "tier": "normal"}


RECORD_PATH = "artifacts/reports/v0.23-lanes.md"


def _lane_fixture_tasks(record_status: str = "validated",
                        verify_status: str = "validated") -> list:
    return [
        _task("057", status=verify_status, criteria="verify every lane"),
        _task("058", status=record_status, depends=["057"],
              criteria=f"One commit adds {RECORD_PATH} naming the shipped sha."),
    ]


# Neither all-curable nor all-incurable -- the same fixture
# `tests/test_operator_input_needed_pause.py`/`test_ruling_re_park_bound.py`
# use, so this rejection lands on the ordinary archive/replan tail below
# `_review_cure_pass_available` -- never `rejected_incurable`/`rejected_
# curable_pass_available`/`rejected_last_approach`.
_MIXED_FINDINGS = [
    _finding("blocking", False, "requirement B (#111)", "incurable one"),
    _finding("blocking", True, "requirement A.1 (#224)", "curable one"),
]

_CURE_REOPEN_FINDING = [
    _finding("blocking", True, "the shipped sha's lanes moved",
              "a later commit landed after the record",
              invalidates=[RECORD_PATH]),
]

_CURE_INSERT_ONLY_FINDING = [
    _finding("blocking", True, "requirement E.3 (#167)",
              "the record went stale", invalidates=["docs/testing.md"]),
]


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _rejecting_runner(reason: str):
    async def _run(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": reason}))
    return _run


_supervisor = supervisor_factory(
    run_id="task017-discretionary-gate", cfg_defaults=False,
    prd="# task 017 fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1})


# ===========================================================================
# 1. `state.cure_pass_preview` itself -- the pure predicate, no `RunDir`.
# ===========================================================================


def test_reopened_validated_tasks_are_named_and_deduped():
    tasks = _lane_fixture_tasks(record_status="validated",
                                verify_status="validated")
    finding = _CURE_REOPEN_FINDING[0]
    preview = cure_pass_preview(tasks, [finding, dict(finding)])

    assert set(preview["reopenedTaskIds"]) == {"057", "058"}
    # deduped even though the SAME record path was named by two findings.
    assert len(preview["reopenedTaskIds"]) == 2
    # two findings, one already-recognised record chain each -- no
    # unresolved path, so no extra retake slot either.
    assert preview["insertCount"] == 2


def test_a_reopened_task_not_currently_validated_is_not_named():
    tasks = _lane_fixture_tasks(record_status="completed",
                                verify_status="pending")
    preview = cure_pass_preview(tasks, _CURE_REOPEN_FINDING)

    assert preview["reopenedTaskIds"] == []
    assert preview["insertCount"] == 1


def test_a_plain_retake_path_only_grows_the_insert_count():
    preview = cure_pass_preview([], _CURE_INSERT_ONLY_FINDING)

    assert preview["reopenedTaskIds"] == []
    # one for the cure task itself, one for the brand-new retake task the
    # unresolved path earns.
    assert preview["insertCount"] == 2


def test_no_invalidates_at_all_is_the_plain_not_a_trigger_case():
    plain = [_finding("blocking", True, "requirement E (#113)", "no paths named")]
    preview = cure_pass_preview([], plain)

    assert preview["reopenedTaskIds"] == []
    assert preview["insertCount"] == 1


def _run_with_tasks(root: Path, tasks: list) -> RunDir:
    """The same minimal on-disk harness `tests/test_cure_invalidates.py`
    drives `insert_cure_tasks` through -- used here only to measure what a
    real cure pass WRITES against what `cure_pass_preview` PREDICTED for
    the identical fixture."""
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    return run


def _rows_added(run: RunDir, blocking: list) -> int:
    before = len(run.read_tasks()["tasks"])
    insert_cure_tasks(run, 1, blocking)
    return len(run.read_tasks()["tasks"]) - before


def test_two_findings_sharing_one_retake_path_are_counted_once(tmp_path):
    # A.7 item 3 is "how many tasks would be inserted" -- so the preview's
    # own number has to be the number of rows the write would actually add.
    # `insert_cure_tasks` collapses two findings naming the SAME unresolved
    # path onto ONE re-take row (#209 defect 3's `_existing_retake_task`
    # reuse), so a preview counting the path twice overstates the page it
    # sends an operator.
    shared = [
        _finding("blocking", True, "requirement E.3 (#167)",
                 "the record went stale", invalidates=["docs/testing.md"]),
        _finding("blocking", True, "requirement F.1 (#198)",
                 "the same record, other clause",
                 invalidates=["docs/testing.md"]),
    ]
    preview = cure_pass_preview([], shared)
    # two cure tasks plus ONE shared re-take row.
    assert preview["insertCount"] == 3

    run = _run_with_tasks(tmp_path / "run", [])
    assert _rows_added(run, shared) == preview["insertCount"]


def test_a_path_with_a_reusable_retake_row_already_on_disk_adds_none(tmp_path):
    # An earlier cure pass over this SAME approach already appended the
    # re-take row for this path: the write re-opens that row in place and
    # appends nothing, so the preview must count only the cure task.
    existing = {"id": "retake-01-01-01", "title": "Re-take docs/testing.md",
                "successCriteria": "docs/testing.md is re-taken.",
                "status": "completed", "origin": "review-cure",
                "dependsOn": ["cure-01-01"], "tier": "normal"}
    preview = cure_pass_preview([existing], _CURE_INSERT_ONLY_FINDING)
    assert preview["insertCount"] == 1

    run = _run_with_tasks(tmp_path / "run", [existing])
    assert _rows_added(run, _CURE_INSERT_ONLY_FINDING) == preview["insertCount"]


def test_a_retake_row_behind_a_dead_dependency_is_counted_again(tmp_path):
    # `_existing_retake_task` DECLINES to re-use a row whose own `dependsOn`
    # names a dead task (it could never be selected again), so the write
    # appends a fresh re-take row for the path -- and the preview, reading
    # that same decision from that same function, counts it.
    dead = {"id": "cure-01-01", "title": "dead cure",
            "successCriteria": "n/a", "status": "failed",
            "origin": "review-cure", "dependsOn": [], "tier": "normal"}
    stranded = {"id": "retake-01-01-01",
                "title": "Re-take docs/testing.md",
                "successCriteria": "docs/testing.md is re-taken.",
                "status": "pending", "origin": "review-cure",
                "dependsOn": ["cure-01-01"], "tier": "normal"}
    preview = cure_pass_preview([dead, stranded], _CURE_INSERT_ONLY_FINDING)
    assert preview["insertCount"] == 2

    run = _run_with_tasks(tmp_path / "run", [dead, stranded])
    assert _rows_added(run, _CURE_INSERT_ONLY_FINDING) == preview["insertCount"]


# ===========================================================================
# 2. Trigger condition 2 over `_step_review`: fires only when the cure
#    pass would re-open an already-`validated` task, never for the
#    not-a-trigger case of a cure pass that only inserts new tasks.
# ===========================================================================


def test_condition_2_holds_before_any_spend_and_carries_the_six_a7_items(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        tasks=_lane_fixture_tasks(),
        status={"state": "running", "phase": "review", "approach": 1,
                "usage": {"byApproach": {"1": {"costUSD": 0.42,
                                               "totalTokens": 321}}}})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    status = sup.run.read_status()
    assert status["pauseReason"] == "operator_input_needed"
    assert status["approach"] == 1
    # never actually taken: no cure pass spent, no reopen landed on disk.
    assert status.get("curePasses") in (None, {})
    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert tasks["057"]["status"] == "validated"
    assert tasks["058"]["status"] == "validated"
    assert status.get("discretionaryGateSpent") == {"1": True}

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    assert ev["reason"] == "operator_input_needed"
    # 1: blocking findings verbatim.
    assert ev["findings"] == _CURE_REOPEN_FINDING
    # 2: task ids validated -> pending.
    assert set(ev["taskIdsValidatedToPending"]) == {"057", "058"}
    # 3: count of tasks that would be inserted.
    assert ev["insertCount"] == 1
    # 4: dollars and tokens spent this approach.
    assert ev["costSpentThisApproach"] == "$0.42"
    assert ev["tokensSpentThisApproach"] == 336
    # 5: approach n of N.
    assert ev["approachOfMax"] == "approach 1 of 2"
    # 6: hub deep link (cure task cure-01-04, PRD requirement A.7, F04:
    # an absolute, operator-usable URL, never the bare relative fragment).
    assert ev["hubLink"] == f"http://127.0.0.1/#/run/{sup.cfg.run_id}"

    text = ev["escalation"]
    assert "057" in text and "058" in text
    # the blocking finding's own clause and evidence, verbatim, in the text.
    assert _CURE_REOPEN_FINDING[0]["clause"] in text
    assert _CURE_REOPEN_FINDING[0]["evidence"] in text
    assert "tasks that would be inserted: 1" in text
    assert "$0.42" in text
    assert "336 tokens" in text
    assert "approach 1 of 2" in text
    assert f"http://127.0.0.1/#/run/{sup.cfg.run_id}" in text
    # cure task cure-01-04 (PRD requirement A.7, F04): the finding's own
    # cured_when text, verbatim, in the delivered escalation body.
    assert _CURE_REOPEN_FINDING[0]["cured_when"] in text

    # Cure task cure-01-04 (PRD requirement A.7, F04): a SECOND episode,
    # same test -- never a new collected node (zero fast-tier headroom,
    # 5997/5997) -- proving the hub deep link genuinely "resolves to the
    # intended hub run page from outside the hub" once an operator sets
    # `hub_base_url` to their own running hub's address. A real
    # `ui_server.make_server` (never a mock/stub) is bound on an OS-
    # assigned loopback port; an independent `urllib.request` call (a
    # plain HTTP client, never importing the hub's own request-handling
    # code) reads the link's own origin -- a hash fragment is a client-
    # side-only concept never sent to any server, so "resolves" here means
    # the origin the link names answers 200 with the hub's own SPA page,
    # which then reads `location.hash` to render this exact run.
    from ralphd.cli import ui_server
    server = ui_server.make_server(tmp_path / "hub-registry", "127.0.0.1", 0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        hub_port = server.server_address[1]
        sup2 = _supervisor(
            tmp_path / "run2", max_approaches=2, operator_input_needed_pause=True,
            tasks=_lane_fixture_tasks(),
            hub_base_url=f"http://127.0.0.1:{hub_port}")
        sup2._approach = 1
        findings.write_findings_json(sup2.run.findings_json_file, _CURE_REOPEN_FINDING)
        sup2.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]
        outcome2 = _bounded(sup2._step_review())
        assert outcome2 == ("advance", "paused", "operator_input_needed")
        ev2 = _events(tmp_path / "run2", "operator_escalation")[-1]
        link = ev2["hubLink"]
        assert link == f"http://127.0.0.1:{hub_port}/#/run/{sup2.cfg.run_id}"
        origin, _, _fragment = link.partition("#")
        with urllib.request.urlopen(origin, timeout=5) as resp:
            status_code = resp.status
            body = resp.read()
        print("HUB LINK RESOLVED", status_code, len(body))
        assert status_code == 200
        assert b"<html" in body.lower()
    finally:
        server.shutdown()
        server.server_close()


def test_a_cure_pass_that_only_inserts_tasks_is_not_a_trigger(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file,
                                 _CURE_INSERT_ONLY_FINDING)
    sup.runner.run = _rejecting_runner("record went stale")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    status = sup.run.read_status()
    # the discretionary gate is UNTOUCHED for the not-a-trigger case.
    assert status.get("discretionaryGateSpent") in (None, {})
    assert status.get("curePasses") == {"1": 1}
    assert _events(root, "operator_escalation") == []
    tasks = sup.run.read_tasks()["tasks"]
    assert len(tasks) == 2  # the cure task, plus the one new retake task

    # Cure task cure-01-03 (PRD requirement A.6, F03): the independent
    # reviewer path is needed PRECISELY in this same mechanical
    # not-a-trigger case -- an all-curable rejection whose cure pass would
    # only insert fresh tasks (never `_review_cure_pass_available`'s own
    # mechanical `reopenedTaskIds` gate) used to reach `_take_cure_pass`/
    # the `cure` edge in the SAME rejection without ever consulting this
    # reply's own `raise_operator_gate: true` -- that field was only read
    # later, on a line the cure-pass-available branch's own `return`
    # already made unreachable. A SECOND, FRESH supervisor over its own
    # tmp subdir -- zero fast-tier headroom (5997/5997) means this is
    # folded into the sibling test above rather than collected as its own
    # node -- differing from it in exactly one thing: the reviewer's own
    # request.
    root2 = tmp_path / "run2"
    sup2 = _supervisor(root2, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file,
                                 _CURE_INSERT_ONLY_FINDING)

    async def _review_raises_the_gate(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected", "reason": "requires operator",
            "raise_operator_gate": True,
        }))
    sup2.runner.run = _review_raises_the_gate  # type: ignore[method-assign]

    outcome2 = _bounded(sup2._step_review())

    assert outcome2 == ("advance", "paused", "operator_input_needed")
    status2 = sup2.run.read_status()
    assert status2["pauseReason"] == "operator_input_needed"
    # NO cure insertion for this reply: no cure pass recorded, no fresh
    # retake task appended, and the shared discretionary gate untouched --
    # this override is unbudgeted, exactly like condition 1's own gate.
    assert status2.get("curePasses") in (None, {})
    assert status2.get("discretionaryGateSpent") in (None, {})
    tasks2 = sup2.run.read_tasks().get("tasks") or []
    assert len(tasks2) == 0
    assert not (root2 / "approaches" / "01").exists()


# ===========================================================================
# 3. Trigger condition 3 over `_step_review`: a repeated blocking-clause
#    signature raises the SAME shared gate, and carries the six A.7 items
#    too, exactly like condition 2.
# ===========================================================================


def _mixed_signature() -> str:
    """The exact signature `_MIXED_FINDINGS` produces -- a scratch
    supervisor's own `_review_rejection_signature` is a pure function of
    its two arguments (`findings.blocking_finding_clauses` plus a text
    normaliser), so this never has to duplicate that logic by hand."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        scratch = _supervisor(Path(d) / "scratch")
        return scratch._review_rejection_signature(_MIXED_FINDINGS, None)


def test_condition_3_holds_before_any_spend_and_carries_the_six_a7_items(
        tmp_path):
    root = tmp_path / "run"
    signature = _mixed_signature()
    sup = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        status={"state": "running", "phase": "review", "approach": 1,
                "rejectionSignatures": {"0": signature}})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup.runner.run = _rejecting_runner("mixed curability")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "rejected_repeated_signature")
    status = sup.run.read_status()
    assert status["pauseReason"] == "rejected_repeated_signature"
    assert status.get("discretionaryGateSpent") == {"1": True}
    # never archived -- the ordinary archive/replan tail never ran.
    assert not (root / "approaches" / "01").exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    assert ev["reason"] == "rejected_repeated_signature"
    assert ev["signature"] == signature
    assert ev["taskIdsValidatedToPending"] == []
    # Task 003 (this wave): `_MIXED_FINDINGS` is never curable at all (one
    # incurable finding) -- a cure pass never runs over it, ceiling spent
    # or not, so the preview reads zero here too, covered by `tests/test_
    # escalation_preview_real_edge.py::test_review_rejection_preview_
    # matches_cure_availability[mixed_1c_1i_ceiling_spent]`.
    assert ev["insertCount"] == 0
    assert ev["approachOfMax"] == "approach 1 of 2"
    assert ev["hubLink"] == f"http://127.0.0.1/#/run/{sup.cfg.run_id}"

    # Task 003 (PRD finding F01): a SECOND episode, over a REAL archive/
    # replan (never a seeded `rejectionSignatures` map), where the repeat
    # rejection's own cure pass would be mechanically AVAILABLE (all
    # findings now curable, each naming an `invalidates` path) and would
    # reopen only `completed` -- never `validated` -- records, so
    # condition 2 itself never fires. F01: `_step_review` used to return
    # `("advance", "cure", "rejected_curable_pass_available")` here,
    # spending `curePasses` and moving 057/058 back to `pending`, because
    # the cure-pass branch ran before this comparison was ever reached.
    sup2 = _supervisor(tmp_path / "run2", max_approaches=3, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file, _MIXED_FINDINGS)
    sup2.runner.run = _rejecting_runner("mixed curability, second episode")  # type: ignore[method-assign]
    outcome2a = _bounded(sup2._step_review())
    assert outcome2a[1] == "paused"  # condition 1's own unbudgeted gate
    sup2.resume()
    sup2._archive_approach(1)
    sup2._step_replan()
    assert sup2._approach == 2

    sup2.run.write_tasks({"version": 1,
                         "tasks": _lane_fixture_tasks(record_status="completed",
                                                      verify_status="completed")})
    repeated = [dict(f, curable=True, cured_when="Fix " + f["clause"],
                     invalidates=[RECORD_PATH]) for f in _MIXED_FINDINGS]
    assert (sup2._review_rejection_signature(repeated, None)
            == sup2.run.read_status()["rejectionSignatures"]["1"])
    findings.write_findings_json(sup2.run.findings_json_file, repeated)

    outcome2b = _bounded(sup2._step_review())

    assert outcome2b == ("advance", "paused", "rejected_repeated_signature")
    status2 = sup2.run.read_status()
    assert status2["pauseReason"] == "rejected_repeated_signature"
    assert status2.get("discretionaryGateSpent") == {"2": True}
    # no cure task inserted, and the completed records untouched.
    assert status2.get("curePasses", {}).get("2", 0) == 0
    tasks2 = {t["id"]: t for t in sup2.run.read_tasks()["tasks"]}
    assert tasks2["057"]["status"] == "completed"
    assert tasks2["058"]["status"] == "completed"
    # the approach is left unarchived -- `resume` re-reviews the same one.
    assert not (sup2.run.root / "approaches" / "02").exists()
    assert sup2._approach == 2


# ===========================================================================
# 4. The shared budget: one gate for conditions 2 and 3 together, in
#    either order, within one approach; condition 1's own pause is
#    separate/unbudgeted; a fresh approach's own budget is unspent again.
# ===========================================================================


def test_condition_2_then_condition_3_in_one_approach_share_one_gate(
        tmp_path):
    root = tmp_path / "run"
    signature = _mixed_signature()
    # A ficticious approach 0 already recorded this rejection's own
    # signature, so episode B's rejection -- still over approach 1 -- reads
    # as "the same wall as the immediately preceding approach".
    sup_a = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        tasks=_lane_fixture_tasks(),
        status={"state": "running", "phase": "review", "approach": 1,
                "rejectionSignatures": {"0": signature}})
    sup_a._approach = 1
    findings.write_findings_json(sup_a.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup_a.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome_a = _bounded(sup_a._step_review())
    assert outcome_a == ("advance", "paused", "operator_input_needed")
    assert sup_a.run.read_status().get("discretionaryGateSpent") == {"1": True}
    sup_a.resume()

    # Episode B: fresh supervisor, same run dir, same approach -- a MIXED
    # rejection whose signature matches approach 0's recorded one, driven
    # under the DEFAULT `operator_input_needed_pause=True`, because that is
    # the configuration every run ships with: a shared budget that only
    # holds while condition 1's own gate is switched off would be no budget
    # at all. A condition-3 match refused its pause by the spent shared gate
    # does not then take condition 1's gate for the SAME rejection -- it
    # reaches the ordinary archive/replan tail.
    sup_b = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup_b._approach = 1
    findings.write_findings_json(sup_b.run.findings_json_file, _MIXED_FINDINGS)
    sup_b.runner.run = _rejecting_runner("still mixed")  # type: ignore[method-assign]

    outcome_b = _bounded(sup_b._step_review())

    # condition 3 would otherwise fire here -- the gate is already spent
    # for approach 1, so this falls through to the ordinary archive/replan
    # tail instead of a second pause.
    assert outcome_b == ("advance", "replan", "rejected_approach_left")
    status = sup_b.run.read_status()
    assert status["pauseReason"] is None
    assert (root / "approaches" / "01").exists()
    # exactly ONE escalation total across both episodes.
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"


def test_condition_3_then_condition_2_in_one_approach_share_the_same_gate(
        tmp_path):
    root = tmp_path / "run"
    signature = _mixed_signature()
    sup_a = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        status={"rejectionSignatures": {"0": signature}})
    sup_a._approach = 1
    findings.write_findings_json(sup_a.run.findings_json_file, _MIXED_FINDINGS)
    sup_a.runner.run = _rejecting_runner("mixed curability")  # type: ignore[method-assign]

    outcome_a = _bounded(sup_a._step_review())
    assert outcome_a == ("advance", "paused", "rejected_repeated_signature")
    assert sup_a.run.read_status().get("discretionaryGateSpent") == {"1": True}
    sup_a.resume()

    # Episode B: a cure pass that would re-open validated tasks -- the
    # gate is already spent, so it is taken silently instead of pausing.
    sup_b = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                        tasks=_lane_fixture_tasks())
    sup_b._approach = 1
    findings.write_findings_json(sup_b.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup_b.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome_b = _bounded(sup_b._step_review())

    assert outcome_b == ("advance", "cure", "rejected_curable_pass_available")
    tasks = {t["id"]: t for t in sup_b.run.read_tasks()["tasks"]}
    assert tasks["057"]["status"] == "pending"
    assert tasks["058"]["status"] == "pending"
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_repeated_signature"


def test_a_reviewer_raised_gate_still_pauses_once_the_shared_gate_is_spent(
        tmp_path):
    # A.6's reviewer field is a fresh per-verdict decision, never subject to
    # the shared discretionary budget: a repeated-signature rejection that
    # the spent gate refuses still pauses when THIS verdict asked for it.
    root = tmp_path / "run"
    signature = _mixed_signature()
    sup = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        status={"state": "running", "phase": "review", "approach": 1,
                "rejectionSignatures": {"0": signature},
                "discretionaryGateSpent": {"1": True}})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)

    async def _review_raises_the_gate(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected", "reason": "the predicate cannot count this",
            "raise_operator_gate": True,
        }))
    sup.runner.run = _review_raises_the_gate  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup.run.read_status()["pauseReason"] == "operator_input_needed"
    assert not (root / "approaches" / "01").exists()


def test_a_condition_2_gate_early_leaves_condition_1_raised_later(
        tmp_path):
    root = tmp_path / "run"
    sup_a = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                        tasks=_lane_fixture_tasks())
    sup_a._approach = 1
    findings.write_findings_json(sup_a.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup_a.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome_a = _bounded(sup_a._step_review())
    assert outcome_a == ("advance", "paused", "operator_input_needed")
    assert sup_a.run.read_status().get("discretionaryGateSpent") == {"1": True}
    sup_a.resume()

    # Episode B: an ordinary mixed rejection with NO repeated signature
    # (no approach 0 ever recorded one) -- condition 1's own unbudgeted
    # gate still raises, even though the shared discretionary budget for
    # approach 1 is already spent.
    sup_b = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup_b._approach = 1
    findings.write_findings_json(sup_b.run.findings_json_file, _MIXED_FINDINGS)
    sup_b.runner.run = _rejecting_runner("still mixed")  # type: ignore[method-assign]

    outcome_b = _bounded(sup_b._step_review())

    assert outcome_b == ("advance", "paused", "operator_input_needed")
    status = sup_b.run.read_status()
    assert status["pauseReason"] == "operator_input_needed"
    assert status["approach"] == 1
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 2
    assert [e["reason"] for e in escalations] == [
        "operator_input_needed", "operator_input_needed"]
    # Cure task cure-01-04 (PRD requirement A.7, F04): condition 1's own
    # escalation is NOT a driven discretionary gate (nothing was spent for
    # it, and `taskIdsValidatedToPending`/`insertCount` read empty/zero --
    # this rejection's own cure-pass preview has nothing to reopen), but
    # the review found its payload missing outright rather than merely
    # empty; it now carries the full six-item A.7 payload every other
    # `operator_input_needed` pause does.
    condition_1_event = escalations[1]
    assert condition_1_event["taskIdsValidatedToPending"] == []
    # Task 003 (this wave): same fix as the sibling pin above -- a mixed
    # rejection never earns a cure pass, so `insertCount` reads zero,
    # covered by `tests/test_escalation_preview_real_edge.py::test_
    # review_rejection_preview_matches_cure_availability[mixed_1c_1i_
    # ceiling_spent]`.
    assert condition_1_event["insertCount"] == 0
    assert condition_1_event["approachOfMax"] == "approach 1 of 2"
    assert condition_1_event["hubLink"] == f"http://127.0.0.1/#/run/{sup_b.cfg.run_id}"


def test_the_gate_is_available_again_once_the_approach_is_crossed(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    sup._spend_discretionary_gate(1)
    assert sup._discretionary_gate_available(1) is False
    assert sup._discretionary_gate_available(2) is True


# ===========================================================================
# 5. Task 005 (J.2, PRD requirement A.3, #224): the two A.3 trigger
# questions the v0.23 review's own probes (`_review_new_edges.py`/
# `_review_positive.py`, not present on this host -- reconstructed here from
# the v0.24 PRD's own descriptions of them) raised against this file's
# shipped code.
# ===========================================================================


def test_A3_repeated_insert_only_is_still_explicit_nontrigger(tmp_path):
    """Probe 3 (reconstructed): "A.3 explicitly excludes findings that
    merely insert tasks without a replan or reopen." Judged PROBE-RIGHT,
    and already true of shipped code -- this test pins the ALREADY-CORRECT
    behaviour rather than changing anything, settling the tracked-test half
    of the question the same way probe 2 of J.1 did.

    The deciding source line is `mechanical_reopen_trigger`'s own
    `cure_preview["reopenedTaskIds"]` conjunct (:11020) -- an insert-only
    finding's preview (`state.cure_pass_preview`) always returns an EMPTY
    `reopenedTaskIds` (nothing walked from its `invalidates` path is
    currently `validated`; `_CURE_INSERT_ONLY_FINDING`'s own path names no
    lane-evidence record chain at all), so this conjunct is false and
    condition 2 never fires -- `test_a_cure_pass_that_only_inserts_tasks_
    is_not_a_trigger` above already pins this for ONE such rejection. The
    named question is whether REPEATING the identical insert-only
    rejection manufactures a trigger the single occurrence does not --
    it does not, because `mechanical_reopen_trigger` reads the SAME
    per-rejection `cure_preview`, never an accumulated count, and
    `discretionaryGateSpent` (trigger conditions 2/3's own shared budget)
    stays completely untouched across both episodes below even though the
    SECOND episode does end up pausing -- via condition 1's own separate,
    unbudgeted `operator_input_needed` gate (:11217-11221), once this
    approach's own one cure pass is already spent and the rejection falls
    through to the ordinary archive-before-pause tail. That pause is real
    but orthogonal to A.3's own discretionary-gate exclusion, which this
    test's `discretionaryGateSpent` assertions isolate.

    Two episodes, one on-disk run dir, a fresh `LoopSupervisor` each time:

    1. the insert-only finding rejects for the first time -- taken as an
       ungated, ungated cure pass (`rejected_curable_pass_available`),
       spending this approach's one cure pass but no discretionary gate.
    2. the IDENTICAL finding rejects again -- this approach's cure pass is
       already spent, so it falls to the ordinary archive-before-pause
       tail and pauses via condition 1 (`operator_input_needed`) -- but
       `discretionaryGateSpent` is STILL untouched: condition 2 never even
       considered this rejection a candidate, repeated or not.
    """
    root = tmp_path / "run"
    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file,
                                 _CURE_INSERT_ONLY_FINDING)
    sup1.runner.run = _rejecting_runner("record went stale")  # type: ignore[method-assign]

    outcome1 = _bounded(sup1._step_review())

    assert outcome1 == ("advance", "cure", "rejected_curable_pass_available")
    status1 = sup1.run.read_status()
    assert status1.get("discretionaryGateSpent") in (None, {})
    assert status1.get("curePasses") == {"1": 1}
    sup1.resume()

    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    # Episode 2 is a CONFIRMATION review (episode 1's cure pass was
    # granted): the IDENTICAL finding, re-filed as still open, names the
    # cure task episode 1 built for it in `reopens` -- an unlinked one
    # would be downgraded to residual (B.3, task 006, tests/
    # test_confirmation_downgrade.py) and never reach this edge at all.
    cure_id = next(t["id"] for t in sup2.run.read_tasks()["tasks"]
                   if t.get("origin") == "review-cure")
    findings.write_findings_json(
        sup2.run.findings_json_file,
        [dict(f, reopens=cure_id) for f in _CURE_INSERT_ONLY_FINDING])
    sup2.runner.run = _rejecting_runner("record went stale again")  # type: ignore[method-assign]

    outcome2 = _bounded(sup2._step_review())

    # the repeat DOES pause -- this approach's one cure pass is already
    # spent -- but through condition 1's own separate, unbudgeted gate,
    # never through trigger condition 2's discretionary one.
    assert outcome2 == ("advance", "paused", "operator_input_needed")
    status2 = sup2.run.read_status()
    assert status2["pauseReason"] == "operator_input_needed"
    # unchanged from episode 1 -- still no cure task inserted for this
    # repeat, and the discretionary gate never spent by either episode.
    assert status2.get("curePasses") == {"1": 1}
    assert status2.get("discretionaryGateSpent") in (None, {})
    tasks = sup2.run.read_tasks()["tasks"]
    assert len(tasks) == 2  # the one cure task episode 1 inserted, unchanged
    escalations = _events(root, "operator_escalation")
    assert [e["reason"] for e in escalations] == ["operator_input_needed"]


def test_A3_discretionary_gate_renews_after_actual_replan(tmp_path):
    """Probe 4 (reconstructed): observed `('advance', 'paused',
    'rejected_repeated_signature')`, expected `('advance', 'paused',
    'operator_input_needed')`. Judged PROBE-OVER-REACHES against A.3's own
    text.

    A.3 gives trigger conditions 2 and 3 exactly ONE shared discretionary
    gate per approach (this file's own module docstring; `docs/api.md`'s
    `discretionaryGateSpent` field description: "whichever of the two
    fires FIRST in an approach spends the entry for that approach's own
    string key"), and `_step_review`'s own dispatch order is what decides
    which one that is: trigger condition 3's repeated-signature check
    (:10895-10903) runs strictly BEFORE the cure-pass branch that houses
    trigger condition 2's `mechanical_reopen_trigger` (:11018-11022) --
    already exercised for a SEEDED signature map by
    `test_condition_3_then_condition_2_in_one_approach_share_the_same_
    gate` above. Nothing in A.3's text gives condition 2 precedence over
    condition 3 when both predicates hold for the identical rejection, or
    ties either one to whether the discretionary gate happens to have just
    renewed across a replan -- the gate renewing is real (proved below,
    `_discretionary_gate_available(2)` reads `True` going into this
    rejection) but orthogonal to WHICH condition spends it once it fires,
    which the code's own dispatch order alone decides. The probe's
    expectation that a freshly-renewed gate should redirect the outcome to
    condition 2's own `operator_input_needed` pauseReason has no basis in
    A.3's text and is not what the shared-budget design it names
    describes.

    Drives an ACTUAL archive/replan (never a seeded approach field, task
    004/J.1's own precedent) and constructs a SINGLE rejection in the
    fresh approach whose blocking-clause signature repeats the
    immediately preceding approach's own recorded one (condition 3's own
    predicate) AND whose cure-pass preview would reopen an already-
    `validated` task (condition 2's own predicate) -- both true at once,
    on the identical rejection, to settle which one the shared gate
    actually answers.
    """
    root = tmp_path / "run"

    # Approach 1: an ordinary MIXED rejection matching no prior signature
    # -- condition 1's own unbudgeted gate pauses it (the default-enabled
    # `operator_input_needed_pause`), which records ITS OWN signature via
    # `_pause_and_escalate_operator_input_needed` exactly as the ordinary
    # archive tail would. Resuming and driving `_archive_approach`/`_step_
    # replan` by hand (this module's own precedent, `test_condition_3_
    # holds_before_any_spend_and_carries_the_six_a7_items`'s second
    # episode) is what makes this an ACTUAL replan, not a seeded field.
    sup1 = _supervisor(root, max_approaches=3, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, _MIXED_FINDINGS)
    sup1.runner.run = _rejecting_runner("mixed curability")  # type: ignore[method-assign]

    outcome1 = _bounded(sup1._step_review())

    assert outcome1 == ("advance", "paused", "operator_input_needed")
    signature = sup1._review_rejection_signature(_MIXED_FINDINGS, None)
    assert signature is not None
    assert sup1.run.read_status()["rejectionSignatures"]["1"] == signature
    sup1.resume()
    sup1._archive_approach(1)
    replan_outcome = sup1._step_replan()
    assert replan_outcome[0] == "advance"
    assert sup1._approach == 2

    # Approach 2 (an ACTUAL fresh approach): the shared gate has renewed.
    sup2 = _supervisor(root, max_approaches=3, operator_input_needed_pause=True,
                       tasks=_lane_fixture_tasks())
    sup2._approach = 2
    assert sup2._discretionary_gate_available(2) is True

    # The SAME wall recurs -- identical blocking-clause signature to
    # approach 1's -- but this time it is ALL curable and would reopen a
    # `validated` task: condition 2's own predicate is also true on this
    # very rejection.
    repeated = [dict(f, curable=True, cured_when=f"{f['clause']} is discharged",
                     invalidates=[RECORD_PATH])
                for f in _MIXED_FINDINGS]
    assert sup2._review_rejection_signature(repeated, None) == signature
    findings.write_findings_json(sup2.run.findings_json_file, repeated)
    sup2.runner.run = _rejecting_runner("same wall recurs in approach 2")  # type: ignore[method-assign]

    outcome2 = _bounded(sup2._step_review())

    # condition 3 (checked FIRST in `_step_review`'s own dispatch order)
    # wins the shared gate -- exactly the "observed" tuple the probe
    # itself names, and the CORRECT one: never `operator_input_needed`,
    # even though condition 2's own mechanical reopen trigger would
    # plainly apply to this identical rejection.
    assert outcome2 == ("advance", "paused", "rejected_repeated_signature")
    status2 = sup2.run.read_status()
    assert status2["pauseReason"] == "rejected_repeated_signature"
    assert status2.get("discretionaryGateSpent") == {"2": True}
    # no cure pass taken, and the validated task never reopened -- the
    # rejection paused before the cure-pass branch (and condition 2's own
    # check inside it) ever ran.
    assert status2.get("curePasses") in (None, {})
    tasks2 = {t["id"]: t for t in sup2.run.read_tasks()["tasks"]}
    assert tasks2["058"]["status"] == "validated"
    assert not (root / "approaches" / "02").exists()

    escalations = _events(root, "operator_escalation")
    assert [e["reason"] for e in escalations] == [
        "operator_input_needed", "rejected_repeated_signature"]
