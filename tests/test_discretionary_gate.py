"""The six-item A.7 escalation payload and `state.cure_pass_preview` (task
005's pure predicate, task 006's `invalidates_reopen_walk`) that feeds it.

Task 017 (PRD requirements A.3/A.7, #224) once also pinned trigger conditions
2 and 3 over a SHARED per-approach discretionary gate
(`discretionaryGateSpent`). v0.36 requirement B removed that gate, the
repeated-signature park and the one-re-park-per-ruling park: section 2 below
pins what each old trigger does now (an ordinary rejection of the one ladder,
`tests/test_rejection_ladder.py`), and that the reviewer's own escalation still
carries the full payload."""

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
# 2. v0.36 requirement B retired the discretionary pre-archive gate, the
#    repeated-signature park and the one-re-park-per-ruling park. What each
#    old trigger does now: a cure that would reopen validated tasks, and a
#    rejection repeating the preceding approach's signature, are ordinary
#    rejections -- they cure while the approach has cure rounds left and park
#    (`cure_rounds_spent`) when it has none. The reviewer's own
#    `raise_operator_gate` still parks, and its payload still carries the six
#    A.7 items. Covering nodes for the ladder itself:
#    `tests/test_rejection_ladder.py`.
# ===========================================================================


def _escalating_runner(reason: str):
    async def _run(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": reason,
                                   "raise_operator_gate": True}))
    return _run


def test_a_reviewer_escalation_over_a_reopening_cure_carries_the_six_a7_items(
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
    sup.runner.run = _escalating_runner("lanes moved")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    status = sup.run.read_status()
    assert status["pauseReason"] == "operator_input_needed"
    assert status["inputQuestion"]["trigger"] == "reviewer_escalation"
    assert status["approach"] == 1
    # never actually taken: no cure pass spent, no reopen landed on disk.
    assert status.get("curePasses") in (None, {})
    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert tasks["057"]["status"] == "validated"
    assert tasks["058"]["status"] == "validated"
    assert "discretionaryGateSpent" not in status

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
        sup2.runner.run = _escalating_runner("lanes moved")  # type: ignore[method-assign]
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


def test_a_cure_that_would_reopen_validated_tasks_cures_without_parking(
        tmp_path):
    """Was trigger condition 2 (a cure pass that would re-open a `validated`
    task parked once per approach, the shared discretionary gate). It is an
    ordinary curable rejection now: with rounds left it cures."""
    root = tmp_path / "run"
    sup = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        tasks=_lane_fixture_tasks())
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    status = sup.run.read_status()
    assert "discretionaryGateSpent" not in status
    assert status.get("curePasses") == {"1": 1}
    assert _events(root, "operator_escalation") == []
    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert tasks["057"]["status"] == "pending"  # the cure reopened it


def test_a_cure_pass_that_only_inserts_tasks_cures_and_a_reviewer_gate_still_parks(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file,
                                 _CURE_INSERT_ONLY_FINDING)
    sup.runner.run = _rejecting_runner("record went stale")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    status = sup.run.read_status()
    assert status.get("curePasses") == {"1": 1}
    assert _events(root, "operator_escalation") == []
    tasks = sup.run.read_tasks()["tasks"]
    assert len(tasks) == 2  # the cure task, plus the one new retake task

    # The reviewer's own `raise_operator_gate: true` parks this same
    # rejection (the ladder's step 4b) before any cure is inserted.
    root2 = tmp_path / "run2"
    sup2 = _supervisor(root2, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file,
                                 _CURE_INSERT_ONLY_FINDING)
    sup2.runner.run = _escalating_runner("requires operator")  # type: ignore[method-assign]

    outcome2 = _bounded(sup2._step_review())

    assert outcome2 == ("advance", "paused", "operator_input_needed")
    status2 = sup2.run.read_status()
    assert status2["pauseReason"] == "operator_input_needed"
    assert status2["inputQuestion"]["trigger"] == "reviewer_escalation"
    assert status2.get("curePasses") in (None, {})
    assert len(sup2.run.read_tasks().get("tasks") or []) == 0
    assert not (root2 / "approaches" / "01").exists()


def _mixed_signature() -> str:
    """The exact signature `_MIXED_FINDINGS` produces."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        scratch = _supervisor(Path(d) / "scratch")
        return scratch._review_rejection_signature(_MIXED_FINDINGS, None)


def test_a_rejection_repeating_the_preceding_signature_is_just_a_rejection(
        tmp_path):
    """Was trigger condition 3 (`rejected_repeated_signature`: park once per
    approach when the blocking-clause signature repeated the preceding
    approach's). Now an all-curable repeat cures while rounds are left, and
    parks as `cure_rounds_spent` when they are not; an incurable repeat parks
    as `incurable` (`tests/test_rejection_ladder.py` pins each step)."""
    signature = _mixed_signature()
    repeated = [dict(f, curable=True, cured_when="Fix " + f["clause"])
                for f in _MIXED_FINDINGS]

    roomy = _supervisor(
        tmp_path / "roomy", max_approaches=2, operator_input_needed_pause=True,
        status={"state": "running", "phase": "review", "approach": 1,
                "rejectionSignatures": {"0": signature}})
    roomy._approach = 1
    findings.write_findings_json(roomy.run.findings_json_file, repeated)
    roomy.runner.run = _rejecting_runner("same wall")  # type: ignore[method-assign]
    assert _bounded(roomy._step_review()) == (
        "advance", "cure", "rejected_curable_pass_available")
    assert "discretionaryGateSpent" not in roomy.run.read_status()

    spent = _supervisor(
        tmp_path / "spent", max_approaches=2, operator_input_needed_pause=True,
        cure_pass_ceiling=1,
        status={"state": "running", "phase": "review", "approach": 1,
                "rejectionSignatures": {"0": signature},
                "curePasses": {"1": 1}})
    spent._approach = 1
    findings.write_findings_json(spent.run.findings_json_file, repeated)
    spent.runner.run = _rejecting_runner("same wall")  # type: ignore[method-assign]
    assert _bounded(spent._step_review()) == (
        "advance", "paused", "operator_input_needed")
    status = spent.run.read_status()
    assert status["inputQuestion"]["trigger"] == "cure_rounds_spent"
    assert status["pauseReason"] == "operator_input_needed"
    assert not (tmp_path / "spent" / "approaches" / "01").exists()
    escalations = _events(tmp_path / "spent", "operator_escalation")
    assert [e["reason"] for e in escalations] == ["operator_input_needed"]
