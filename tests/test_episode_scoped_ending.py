"""Task 022 (#30): `endedAt` and `reason` are episode-scoped.

An *episode* is one engine process over one run dir -- a fresh start or a
resume. Until this task the write that opens an episode
(`_run_job_core`'s move to `running`) re-based `startedAt`, `verdict`,
`health` and `infraWait` but left `endedAt` and `reason` exactly as the
PREVIOUS engine had written them. Consequences, both observed on
`selfdev-v06-release`:

* a run that finished `succeeded / verified` still reported
  `reason: signal 15` from a `pkill` two episodes earlier -- a corrupted
  permanent record, not a cosmetic mid-run display bug;
* a *running* resumed run reported an `ended` timestamp BEFORE its
  `started` one, because `startedAt` is rewritten every episode.

The fix resets both where the other episode-scoped fields are already reset,
and keeps the superseded ending as history in `previousEndings` (the terminal
`state` event in `events.jsonl` records which state an earlier episode reached,
but neither its reason nor its verdict).

Tiers here: whole episodes through `LoopSupervisor._run_job_core` with a
scripted runner over one run dir (no subprocess, milliseconds), the helper
itself for the history/cap/idempotence corners, and `ralphctl status` over the
resulting run dir with no container at all -- which is the surface the incident
was reported from.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_cli_docker import Ctl, ctl  # noqa: F401  (ctl is a fixture)
from test_cli_resume import _seed_run

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import PREVIOUS_ENDINGS_MAX, LoopSupervisor
from ralphd.engine.runner import COMPLETE, VERIFIED, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

REPO = Path(__file__).resolve().parents[1]
ABORT_REASON = "signal 15 (SIGTERM) reached the engine"


# -- scaffolding -----------------------------------------------------------


class _Clock:
    """Advancing wall clock for `loop.utcnow`.

    Real timestamps are second-resolution and a scripted episode runs in
    milliseconds, so without this every write of a test lands on the same
    second and "ended before started" cannot be told from "ended at the same
    instant as started" -- which is exactly the ordering under test.
    """

    def __init__(self, step: int = 60):
        self.now = datetime(2026, 8, 22, 14, 0, 0, tzinfo=timezone.utc)
        self.step = step

    def __call__(self) -> str:
        self.now += timedelta(seconds=self.step)
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr("ralphd.engine.loop.utcnow", c)
    return c


class _ScriptedRunner:
    """Stands in for PiRunner. Each script entry takes the supervisor and
    returns an IterationResult; the last entry repeats forever."""

    def __init__(self, sup: LoopSupervisor, script: list):
        self.sup = sup
        self.script = script
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.running = True
        try:
            step = self.script[min(self.calls - 1, len(self.script) - 1)]
            return step(self.sup)
        finally:
            self.running = False


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0          # not an "instant" failure
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "t", "status": "pending", "successCriteria": "c"}]})
    return _ok("planned")


def _abort(sup: LoopSupervisor) -> IterationResult:
    """The operator kills the run mid-worker (what `pkill`/`ralphctl stop` do
    to a live engine)."""
    sup.abort(ABORT_REASON)
    return _ok("interrupted")


def _finish_task(sup: LoopSupervisor) -> IterationResult:
    doc = sup.run.read_tasks()
    doc["tasks"][0]["status"] = "completed"
    atomic_write_json(sup.run.tasks_file, doc)
    return _ok(f"done {COMPLETE}")


def _review_ok(sup: LoopSupervisor) -> IterationResult:
    return _ok(f"looks good {VERIFIED}")


def _supervisor(root: Path, script: list, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 12, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v07-episodes", **kw), run, root)
    sup.runner = _ScriptedRunner(sup, script)   # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff          # type: ignore[method-assign]
    return sup


def _episode(root: Path, script: list, **cfg_kw) -> tuple[LoopSupervisor, str]:
    """One whole engine process over `root`, including the `starting` write
    `engine/main.py` does before the loop is even constructed -- which is why
    the loop cannot read the previous episode's `state` back."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, script, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _abort_then_resume(root: Path) -> tuple[dict, dict, dict]:
    """Episode 1 is killed by an operator mid-worker; episode 2 resumes the
    same run dir and succeeds. Returns (episode-1 status, a snapshot taken
    while episode 2 was still running, final status).
    """
    sup1, state1 = _episode(root, [_plan, _abort])
    assert state1 == "aborted", state1
    first = sup1.run.read_status()

    seen: list[dict] = []

    def snapshot(sup: LoopSupervisor) -> IterationResult:
        seen.append(sup.run.read_status())
        return _finish_task(sup)

    sup2, state2 = _episode(root, [snapshot, _review_ok])
    assert state2 == "succeeded", state2
    assert seen, "episode 2 ran at least one iteration"
    return first, seen[0], sup2.run.read_status()


@pytest.fixture
def aborted_then_resumed(tmp_path) -> tuple[Path, dict, dict, dict]:
    root = tmp_path / "run"
    return (root, *_abort_then_resume(root))


# -- the defect: a previous episode's ending read as this one's -------------


def test_a_resumed_run_never_reports_ended_before_started(aborted_then_resumed):
    """THE observable symptom on a live resumed run: `ended` earlier than
    `started`, which is only visible while the episode is still running (its
    own terminal write later covers the stale value up)."""
    _root, first, mid, final = aborted_then_resumed
    assert first["endedAt"], "episode 1 really did record an ending"
    assert first["endedAt"] < mid["startedAt"], (
        "episode 2 started after episode 1 ended, so a kept value would be "
        "the earlier one")
    assert mid["state"] == "running"
    assert mid["endedAt"] is None or mid["endedAt"] >= mid["startedAt"], (
        f"ended {mid['endedAt']} predates started {mid['startedAt']}")
    assert final["endedAt"] >= final["startedAt"]


def test_a_run_that_aborted_and_then_succeeded_keeps_no_aborted_reason(
        aborted_then_resumed):
    """THE corrupted permanent record: `succeeded / verified` plus
    `reason: signal 15` from an episode two engines ago."""
    _root, first, _mid, final = aborted_then_resumed
    assert ABORT_REASON in first["reason"], "episode 1 recorded the reason"
    assert final["state"] == "succeeded" and final["verdict"] == "verified"
    assert not final.get("reason"), final.get("reason")
    assert ABORT_REASON not in json.dumps(
        {k: v for k, v in final.items() if k not in
         ("previousEndings", "termination")}), (
        "the aborted episode's reason survives somewhere it can be read as "
        "this episode's")


def test_the_ending_fields_are_cleared_while_the_episode_is_still_running(
        aborted_then_resumed):
    """Not only at the terminal write: the reset happens with the move to
    `running`, so a resumed run is honest for its whole life."""
    _root, _first, mid, _final = aborted_then_resumed
    assert mid["state"] == "running"
    assert mid["endedAt"] is None, mid["endedAt"]
    assert mid["reason"] is None, mid["reason"]
    assert mid["verdict"] is None, mid["verdict"]


def test_a_terminal_runs_ending_is_exactly_what_its_final_episode_wrote(
        aborted_then_resumed):
    """The rule is not "always clear it": once terminal, the values are this
    episode's own."""
    root, _first, _mid, final = aborted_then_resumed
    metas = [json.loads((d / "meta.json").read_text())
             for d in sorted((root / "iterations").iterdir())
             if (d / "meta.json").exists()]
    last_end = max(m["endedAt"] for m in metas if m.get("endedAt"))
    assert final["endedAt"] >= last_end, (
        "the terminal write must be the final episode's own, later than "
        "every iteration it ran")
    assert final["endedAt"] > final["previousEndings"][-1]["endedAt"]


# -- history rather than deletion ------------------------------------------


def test_the_superseded_ending_is_kept_as_history(aborted_then_resumed):
    _root, first, _mid, final = aborted_then_resumed
    assert final["previousEndings"] == [
        {"endedAt": first["endedAt"], "reason": first["reason"],
         "verdict": first["verdict"]}]
    assert ABORT_REASON in final["previousEndings"][0]["reason"], (
        "the evidence an operator needs -- why the earlier episode ended -- "
        "is still in the run dir")


def test_a_first_episode_records_an_empty_history(tmp_path):
    sup, state = _episode(tmp_path / "run", [_plan, _finish_task, _review_ok])
    assert state == "succeeded"
    assert sup.run.read_status()["previousEndings"] == []


def test_the_history_grows_by_one_entry_per_ending_oldest_first(tmp_path):
    """Three episodes, each aborted: the two SUPERSEDED endings are history and
    the third is the run's current one, never both."""
    root = tmp_path / "run"
    _episode(root, [_plan, _abort])
    for _ in range(2):
        _episode(root, [_abort])
    status = RunDir(root=root).read_status()
    endings = status["previousEndings"]
    assert len(endings) == 2, endings
    stamps = [e["endedAt"] for e in endings]
    assert stamps == sorted(stamps) and len(set(stamps)) == 2, "oldest first"
    assert all(ABORT_REASON in e["reason"] for e in endings)
    assert status["endedAt"] not in stamps, "the current ending is not history"


def test_a_crashed_episode_appends_nothing_and_no_ending_is_recorded_twice(
        tmp_path):
    """An engine that died mid-episode wrote no ending at all (this same write
    cleared the fields when it started), so a resume must not duplicate the
    last real ending -- nor invent an empty one."""
    root = tmp_path / "run"
    _episode(root, [_plan, _abort])
    after_abort = RunDir(root=root).read_status()

    # a crashed episode: it opens, runs an iteration, and never goes terminal
    sup = _supervisor(root, [_finish_task])
    RunDir(root=root).update_status(state="starting")
    sup.run.update_status(state="running", startedAt="2030-01-01T00:00:00Z",
                          endedAt=None, reason=None, verdict=None,
                          previousEndings=sup._previous_endings())
    crashed = sup.run.read_status()
    assert len(crashed["previousEndings"]) == 1

    # ... and the resume after it still has exactly that one ending
    sup3, state3 = _episode(root, [_review_ok])
    assert sup3.run.read_status()["previousEndings"] == \
        crashed["previousEndings"] == [
            {"endedAt": after_abort["endedAt"], "reason": after_abort["reason"],
             "verdict": after_abort["verdict"]}]
    assert state3 in ("succeeded", "failed")


def test_the_history_is_bounded(tmp_path):
    """status.json is rewritten in full on every update, so an auto-resume loop
    must not be able to grow it without limit."""
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    run.update_status(previousEndings=[{"endedAt": f"e{i}", "reason": "r",
                                       "verdict": None}
                                      for i in range(PREVIOUS_ENDINGS_MAX + 5)],
                      endedAt="e-new", reason="newest", verdict="unverified")
    sup = _supervisor(root, [_ok])

    endings = sup._previous_endings()

    assert len(endings) == PREVIOUS_ENDINGS_MAX
    assert endings[-1] == {"endedAt": "e-new", "reason": "newest",
                           "verdict": "unverified"}, "the newest is kept"
    assert PREVIOUS_ENDINGS_MAX >= 20, PREVIOUS_ENDINGS_MAX


def test_an_ending_with_a_reason_but_no_timestamp_is_still_history(tmp_path):
    """Either field on its own is a previous episode's ending: a reason with no
    `endedAt` (a truncated terminal write) must not be silently dropped."""
    root = tmp_path / "run"
    root.mkdir()
    RunDir(root=root).update_status(reason="engine error: boom", verdict=None)
    sup = _supervisor(root, [_ok])
    assert sup._previous_endings() == [
        {"endedAt": None, "reason": "engine error: boom", "verdict": None}]


def test_a_garbled_history_does_not_crash_the_new_episode(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    RunDir(root=root).update_status(previousEndings=["not a dict", {"a": 1}],
                                    endedAt="e1", reason=None, verdict=None)
    sup = _supervisor(root, [_ok])
    assert sup._previous_endings() == [
        {"a": 1}, {"endedAt": "e1", "reason": None, "verdict": None}]


# -- what is deliberately NOT reset ---------------------------------------


def test_the_termination_record_is_not_cleared_by_a_resume(
        aborted_then_resumed):
    """It records WHO stopped the run -- what doctor/auto-resume reason about
    -- and a later abort overwrites it wholesale."""
    _root, first, _mid, final = aborted_then_resumed
    assert final.get("termination") == first.get("termination")
    assert final["termination"]["class"] == "operator"
    assert ABORT_REASON in final["termination"]["reason"]


# -- the operator surface --------------------------------------------------


def test_ralphctl_status_of_the_resumed_run_shows_no_stale_reason(ctl: Ctl):
    """The incident as reported: `ralphctl status` on a finished run, container
    long gone, printing a reason from two episodes earlier."""
    run_id = "v07-episodes"
    rdir, _cdir = _seed_run(ctl, run_id)
    _first, _mid, final = _abort_then_resume(rdir)
    assert final["state"] == "succeeded"

    proc = ctl.run("status", run_id)
    out = proc.stdout + proc.stderr

    assert "succeeded" in out and "verified" in out, out
    assert "reason:" not in out, out
    assert "signal 15" not in out, out
    assert "ended:" in out, out


# -- the documents --------------------------------------------------------


@pytest.mark.parametrize("path, needles", [
    ("SPEC.md",
     ["**Ending fields are episode-scoped.**",
      "| `previousEndings` | array | earlier episodes' endings, oldest first",
      "reset when the next episode enters `running`",
      "**episode-scoped** like `endedAt`",
      "`termination` is deliberately\n*not* reset"]),
    ("docs/api.md",
     ["| `previousEndings` | earlier episodes' endings, oldest first",
      "reset when a resumed episode starts (so it can never predate "
      "`startedAt`)",
      "also episode-scoped, so a resumed run never reports the previous "
      "engine's reason",
      '"previousEndings": [],']),
])
def test_the_docs_state_the_new_rule(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"


def test_spec_5_2_argues_why_the_history_is_kept_rather_than_dropped():
    spec = (REPO / "SPEC.md").read_text()
    section = spec.split("### 5.2")[1].split("### 5.3")[0]
    assert "one engine process over one\nrun dir" in section
    assert "reason: signal 15" in section
    assert "before** its `started`" in section
    assert "neither\nits reason nor its verdict" in section
    assert "A crashed episode appends nothing" in section
    assert "remain exactly what its final episode wrote." in " ".join(
        section.split())
