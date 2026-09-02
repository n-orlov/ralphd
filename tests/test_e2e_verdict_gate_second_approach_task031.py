"""Task 031 (requirement G.3, #130): a run-level black-box test that drives
`verdict_gate` all the way around the approach boundary in one run --
approach 1's SECOND, independent verdict gate invocation rejects, a full
approach 2 runs (its own real `planning`/`worker`/`review`), and approach
2's own verdict gate invocation approves, ending the run at its real
success edge.

Task 029 (G.1, #130) already covers plain approval and unconditional
rejection (no approach left) at this same subprocess tier
(`tests/test_e2e_peer_gates_task029.py`); this module is the third
scenario named by requirement G that neither of those exercises: a
rejection that is NOT terminal because an approach is still available,
followed by that fresh approach's own gate pass. It uses the new counted
knob `STUB_VERDICT_GATE_REJECT_COUNT` (task 031, `tests/stub-pi/pi`) rather
than the plain boolean `STUB_VERDICT_GATE_REJECT` task 029 added, because
this scenario needs the SECOND gate invocation to actually approve.

Per-approach bookkeeping is asserted from the run's OWN recorded state,
never only from the stub having been asked to reject once:

  - `status.json`'s `approach` field ends at 2 (the counter bumped exactly
    once, at the replan boundary between the two approaches).
  - `approaches/01/` exists (task 020's `_archive_approach`, called on
    every rejection that still has an approach left) and carries the
    archived `tasks.json`/`notes.md` from approach 1, plus the peer
    verdict gate's OWN findings channel -- `verdict-gate-findings.md`
    (free markdown) and `verdict-gate-findings.json` (the structured
    sidecar, task 019/#125) -- both written by approach 1's rejecting
    `verdict_gate` invocation.
  - the archived JSON sidecar's finding carries `author: "verdict_gate"`
    even though the stub's own payload never set one -- proving the
    ENGINE's own attribution (`findings.attribute_authorship`, called from
    `_archive_findings_json`) put it there, not the hat's say-so.
  - `approaches/02/` does NOT exist -- the run's real, terminal success
    approach is never archived; only a rejected approach ever is.
  - both approaches' phase sequences are recorded with the correct
    `approach` number on every iteration's own `meta.json`, not merely in
    the right order.
"""

from __future__ import annotations

import json


def _status(run) -> dict:
    f = run.run_dir / "status.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:  # mid-write
        return {}


def _iteration_metas(run) -> list[dict]:
    iters = sorted((run.run_dir / "iterations").iterdir())
    return [json.loads((d / "meta.json").read_text()) for d in iters]


def test_verdict_gate_rejects_approach_one_then_approves_approach_two(live):
    run = live(run_id="g3-verdict-gate-second-approach",
               job={"peer_verdict_gate": True, "on_complete": "exit",
                    "max_approaches": 2},
               stub_env={"STUB_VERDICT_GATE_REJECT_COUNT": "1"})
    assert run.proc.wait(timeout=60) == 0
    status = _status(run)
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status

    # -- the run's real success edge is approach 2's own gate pass, and
    # the whole sequence -- rejection, replan, a fresh planning/worker/
    # review, and a SECOND, approving verdict_gate invocation -- is
    # recorded as a plain (phase, approach) sequence with no gap and no
    # extra round trip.
    metas = _iteration_metas(run)
    phase_approach = [(m["phase"], m["approach"]) for m in metas]
    assert phase_approach == [
        ("planning", 1), ("worker", 1), ("worker", 1), ("review", 1),
        ("verdict_gate", 1),
        ("planning", 2), ("worker", 2), ("worker", 2), ("review", 2),
        ("verdict_gate", 2),
    ], phase_approach

    # -- the approach counter itself, read back from status.json, ends at
    # approach 2 (not merely "the gate approved somewhere").
    assert status.get("approach") == 2, status

    # -- approach 1 was archived (rejected-with-budget-left, task 020's
    # `_archive_approach`); approach 2, the run's real terminal success,
    # never is.
    approaches_dir = run.run_dir / "approaches"
    approach_one = approaches_dir / "01"
    assert approach_one.is_dir(), sorted(p.name for p in approaches_dir.iterdir())
    assert not (approaches_dir / "02").exists()

    assert (approach_one / "tasks.json").exists()
    assert (approach_one / "notes.md").exists()

    # -- the gate's OWN findings channel, from approach 1's rejection, is
    # present in the archive under its own fixed name (never review's
    # `review-findings.md`/`.json`, a deliberately different path -- task
    # 019/#125).
    gate_md = approach_one / "verdict-gate-findings.md"
    assert gate_md.exists()
    assert "stub" in gate_md.read_text() or gate_md.read_text().strip()
    assert not (approach_one / "review-findings.md").exists()

    gate_json = approach_one / "verdict-gate-findings.json"
    assert gate_json.exists()
    findings = json.loads(gate_json.read_text())
    assert findings, findings
    for entry in findings:
        # attributed by the ENGINE (`findings.attribute_authorship`), not
        # by the stub's own payload, which never set this field.
        assert entry.get("author") == "verdict_gate", entry
        assert entry.get("severity") == "blocking", entry

    # -- ordinary plan completion still holds for the run's LAST approach
    # (the one whose tasks.json is left at the run root).
    tasks = json.loads((run.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)
