"""Task 026 (requirement N, carried from the closed #29): a quiet vigilant gate
says so, exactly once.

#29 itself (colliding keys in `vigilant-verified.json`, so every approach after
the first read as fully verified) is fixed. What was never implemented is the
part that actually cost the time: while the gate was broken the engine simply ran
worker iteration after worker iteration, which from outside -- `ralphctl status`,
the event stream -- is indistinguishable from a run with nothing left to verify,
from a config change, or from a stall. It was silent for 17 consecutive
iterations of a real run.

So the first worker iteration that finds `pending_verify` empty while some
currently-`completed` task was never verified *by this process* emits one
`log`/`warning` event (`LoopSupervisor._warn_if_verify_gate_empty()`). Once per
engine process, not per iteration: the condition holds for every remaining worker
iteration, so repeating it would bury the run's real events under one fact. A
legitimate resume has the same shape, so the message names both explanations
instead of asserting a bug.

Tiers: the helper directly (including its approach-namespaced key, which is the
#29 shape), then whole runs through `LoopSupervisor._run_job_core()` with a
phase-dispatching scripted runner -- the only level at which "several worker
iterations, exactly one warning line in events.jsonl" can be observed -- plus the
untampered control run, which must stay silent.

Mutation cases (matrix in the commit message): dropping the `_verify_gate_warned`
latch makes test_a_tampered_record_warns_once_over_a_whole_run and
test_the_warning_is_emitted_once_per_process_not_once_per_call fail on the count;
dropping the `if not pending_verify` guard at the call site makes
test_a_run_whose_gate_really_works_never_warns fail; dropping the
`_verified_this_process` bookkeeping in `_verify_task` makes it fail too;
reverting the key to a bare task id makes
test_another_approach_s_task_001_is_not_this_process_s_verified_001 fail.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, VERIFIED, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, utcnow

REPO = Path(__file__).resolve().parents[1]

WARNING = "vigilant verification gate computed empty"


# -- scaffolding -----------------------------------------------------------


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedAgent:
    """Stands in for PiRunner, dispatching on the phase status.json records
    just before the agent is launched (same shape as
    tests/test_failed_task_vocabulary.py's)."""

    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.phases: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        self.running = True
        try:
            return self.handlers[phase](self.sup, prompt)
        finally:
            self.running = False


def _steps(*steps):
    calls = {"n": 0}

    def handler(sup: LoopSupervisor, prompt: str) -> IterationResult:
        calls["n"] += 1
        return steps[min(calls["n"] - 1, len(steps) - 1)](sup, prompt)

    return handler


def _supervisor(root: Path, handlers: dict | None = None, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 12, "max_approaches": 1, "vigilant": True,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v07-gate-warning", **kw), run, root)
    if handlers is not None:
        sup.runner = _ScriptedAgent(sup, handlers)   # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff              # type: ignore[method-assign]
    return sup


def _plan_doc(*ids: str, status: str = "pending") -> dict:
    return {"version": 1,
            "tasks": [{"id": i, "title": f"task {i}", "status": status,
                       "successCriteria": f"{i} works"} for i in ids]}


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _warnings(root: Path) -> list[str]:
    return [ev["message"] for ev in _events(root)
            if ev.get("type") == "log" and ev.get("level") == "warning"
            and WARNING in (ev.get("message") or "")]


# -- the helper ------------------------------------------------------------


def test_a_gate_with_nothing_completed_is_not_suspicious(tmp_path):
    """Every run starts here: no completed task, so nothing unverified."""
    sup = _supervisor(tmp_path / "run")
    sup._warn_if_verify_gate_empty(_plan_doc("001", "002"))
    assert _warnings(sup.run.root) == []
    assert sup._verify_gate_warned is False


def test_a_task_this_process_verified_is_not_suspicious(tmp_path):
    """The ordinary vigilant run: this process ran the verify iteration, the
    record and the process agree, and the quiet gate is simply correct."""
    sup = _supervisor(tmp_path / "run")
    sup._verified_this_process.add("1:001")
    sup._warn_if_verify_gate_empty(_plan_doc("001", status="completed"))
    assert _warnings(sup.run.root) == []


def test_the_defect_shape_warns(tmp_path):
    """The record claims a completed task is verified; this process never
    verified it. That is the #29 shape, and it is what gets named."""
    sup = _supervisor(tmp_path / "run")
    sup.run.update_status(approach=3)
    sup._warn_if_verify_gate_empty(_plan_doc("001", "002", status="completed"))
    warned = _warnings(sup.run.root)
    assert len(warned) == 1
    msg = warned[0]
    assert "2 completed task(s) this process never verified" in msg
    assert "001, 002" in msg
    assert "approach 3" in msg
    assert "vigilant-verified.json" in msg and "#29" in msg


def test_the_warning_is_emitted_once_per_process_not_once_per_call(tmp_path):
    """The condition holds for every remaining worker iteration of the run."""
    sup = _supervisor(tmp_path / "run")
    for _ in range(6):
        sup._warn_if_verify_gate_empty(_plan_doc("001", status="completed"))
    assert len(_warnings(sup.run.root)) == 1
    assert sup._verify_gate_warned is True


def test_only_the_tasks_this_process_did_not_verify_are_named(tmp_path):
    sup = _supervisor(tmp_path / "run")
    sup._verified_this_process.add("1:001")
    sup._warn_if_verify_gate_empty(
        _plan_doc("001", "002", "003", status="completed"))
    msg = _warnings(sup.run.root)[0]
    assert "002, 003" in msg and "001" not in msg.split(":")[-2]
    assert "2 completed task(s)" in msg


def test_another_approach_s_task_001_is_not_this_process_s_verified_001(tmp_path):
    """#29's exact collision: approach 2's planning pass renumbers from "001",
    so a bare-id memory of "I verified 001" would silence the tripwire for the
    corrective approach that most needs it. Driven through the real
    bookkeeping (`_verify_task`), so the key that gets WRITTEN has to be
    namespaced too, not merely the key the check builds."""
    root = tmp_path / "run"
    sup = _supervisor(root, {"verify": _verify_pass})
    sup.run.update_status(approach=1)
    atomic_write_json(sup.run.tasks_file, _plan_doc("001", status="completed"))
    assert asyncio.run(sup._verify_task(sup.run.read_tasks()["tasks"][0])) is True
    assert sup._verified_this_process == {"1:001"}
    sup._warn_if_verify_gate_empty(sup.run.read_tasks())
    assert _warnings(root) == [], "this process verified that very task"

    sup.run.update_status(approach=2)
    sup._warn_if_verify_gate_empty(_plan_doc("001", status="completed"))
    warned = _warnings(root)
    assert len(warned) == 1 and "approach 2" in warned[0]


def test_a_fresh_process_says_there_is_no_benign_explanation(tmp_path):
    sup = _supervisor(tmp_path / "run")
    assert sup._resumed_at_start is False
    sup._warn_if_verify_gate_empty(_plan_doc("001", status="completed"))
    assert ("this process started the run, so nothing can legitimately have "
            "verified them") in _warnings(sup.run.root)[0]


def test_a_resumed_process_names_the_benign_explanation(tmp_path):
    """A resume produces the identical shape (an earlier process did the
    verifying), so the tripwire reports rather than accuses -- and the
    resumed-ness is captured at construction, before this process's own
    iterations make _resuming_existing_run_dir() true for everybody."""
    root = tmp_path / "run"
    root.mkdir()
    atomic_write_json(root / "tasks.json", _plan_doc("001", status="completed"))
    itdir = root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    atomic_write_json(itdir / "meta.json",
                      {"n": 1, "phase": "worker", "startedAt": utcnow(),
                       "endedAt": utcnow()})
    sup = _supervisor(root)
    assert sup._resumed_at_start is True
    sup._warn_if_verify_gate_empty(sup.run.read_tasks())
    assert ("this process resumed an existing run dir, so an earlier process "
            "may legitimately have verified them") in _warnings(root)[0]


# -- whole runs ------------------------------------------------------------


def _plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.tasks_file, _plan_doc("001", "002"))
    return _ok("planned two tasks")


def _complete_next(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """One task per worker iteration, as the design requires."""
    doc = sup.run.read_tasks()
    for task in doc["tasks"]:
        if task["status"] != "completed":
            task["status"] = "completed"
            atomic_write_json(sup.run.tasks_file, doc)
            return _ok(f"finished {task['id']}")
    return _ok("nothing left to do")


def _done(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """The worker's last iteration: nothing left in the plan, so it signals."""
    return _ok(f"every task is completed; notes written {COMPLETE}")


def _verify_pass(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    m = re.search(r"\*\*id\*\*: (\S+)", prompt)
    return _ok(f"<task-verified>{m.group(1) if m else '001'}</task-verified>")


def _review_verified(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    return _ok(f"the PRD holds {VERIFIED}")


def _run(root: Path, worker=None, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"planning": _plan,
                             "worker": worker or _steps(_complete_next,
                                                        _complete_next, _done),
                             "verify": _verify_pass,
                             "review": _review_verified}, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def test_a_tampered_record_warns_once_over_a_whole_run(tmp_path):
    """The record is pre-seeded with both task ids (what #29's key collision
    did by accident), so the gate computes empty on every worker iteration of
    the run: no verify iteration ever runs, and the operator gets exactly one
    line saying so."""
    root = tmp_path / "run"
    root.mkdir()
    atomic_write_json(root / "vigilant-verified.json", ["1:001", "1:002"])

    sup, outcome = _run(root)
    phases = sup.runner.phases
    assert phases.count("worker") >= 3, phases
    assert "verify" not in phases, "the tampered record disabled the gate"
    assert len(_warnings(root)) == 1, _warnings(root)
    assert outcome == "succeeded"      # the run itself is unaffected


def test_a_run_whose_gate_really_works_never_warns(tmp_path):
    """The control: same script, untouched record. Every completed task gets
    its verify iteration in this process, so the quiet gate at the end of the
    run is honestly quiet and no warning is written."""
    root = tmp_path / "run"
    sup, outcome = _run(root)
    phases = sup.runner.phases
    assert phases.count("verify") == 2, phases
    assert phases.count("worker") >= 3, phases
    assert _warnings(root) == []
    assert sup.run.read_verified_tasks() == {"001", "002"}
    assert sup._verified_this_process == {"1:001", "1:002"}
    assert outcome == "succeeded"


def test_a_resumed_run_warns_but_names_the_resume(tmp_path):
    """A genuine resume over a run dir whose previous process verified both
    tasks: the shape is the defect's, so the tripwire fires -- once -- and the
    message says a previous process may legitimately be the explanation."""
    root = tmp_path / "run"
    root.mkdir()
    atomic_write_json(root / "tasks.json", _plan_doc("001", "002",
                                                    status="completed"))
    atomic_write_json(root / "vigilant-verified.json", ["1:001", "1:002"])
    for n in (1, 2, 3):
        itdir = root / "iterations" / f"{n:04d}"
        itdir.mkdir(parents=True)
        atomic_write_json(itdir / "meta.json",
                          {"n": n, "phase": "worker", "startedAt": utcnow(),
                           "endedAt": utcnow()})

    sup, outcome = _run(root, worker=_steps(_done))
    warned = _warnings(root)
    assert len(warned) == 1, warned
    assert "may legitimately have verified them" in warned[0]
    assert outcome == "succeeded"


# -- the documents ---------------------------------------------------------


@pytest.mark.parametrize("path, needles", [
    ("SPEC.md",
     ["**The gate warns when it goes quiet (task 026, requirement N carried "
      "from the\nclosed #29).**",
      "silent\nfor 17 consecutive iterations of a real run",
      "one `log` event at level\n`warning` (`_warn_if_verify_gate_empty()`)",
      "Exactly once per engine process, not per iteration",
      "it is a tripwire, not a verdict"]),
    ("docs/architecture.md",
     ["**The quiet gate is a warning (task 026, #29's carried-forward "
      "suggestion).**",
      "`LoopSupervisor._warn_if_verify_gate_empty()`",
      "once per engine process rather than per iteration",
      "the message names both explanations rather than\nclaiming a bug"]),
])
def test_the_docs_state_the_rule(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"
