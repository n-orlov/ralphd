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
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import REMOVE_STATUS_KEY, RunDir, atomic_write_json

# test_worker_hat.py absorbed test_failed_task_vocabulary.py (task 028,
# requirement D.7); the aliases restore the names this module already used.
from test_worker_hat import (
    _drive_until_paused,
)
from tests.conftest import supervisor_with_runner

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
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t", "successCriteria": "c"}]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _abort(sup: LoopSupervisor) -> IterationResult:
    """The operator kills the run mid-worker (what `pkill`/`ralphctl stop` do
    to a live engine)."""
    sup.abort(ABORT_REASON)
    return _ok("interrupted")


def _finish_task(sup: LoopSupervisor) -> IterationResult:
    # Task 015 (requirement O, #100): report the harness-owned `complete`
    # verdict instead of writing tasks.json directly and ending with the
    # retired `<promise>COMPLETE</promise>` marker -- with only one task in
    # every plan this module seeds, applying it already leaves
    # `_outstanding_for_completion` empty, so the SAME iteration's
    # `applied_this_iteration` branch (task 116) ends the worker loop.
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": "001"}))


def _review_ok(sup: LoopSupervisor) -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract, not the retired `<promise>VERIFIED</promise>` sentinel.
    return _ok("looks good\n" + json.dumps({"verdict": "verified"}))


_supervisor = supervisor_with_runner(
    lambda sup, script: _ScriptedRunner(sup, script),
    run_id="v07-episodes", iterations=12, max_approaches=1, vigilant=False, on_complete="exit",
    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


def _episode(root: Path, script: list, **cfg_kw) -> tuple[LoopSupervisor, str]:
    """One whole engine process over `root`, including the `starting` write
    `engine/main.py` does before the loop is even constructed -- which is why
    the loop cannot read the previous episode's `state` back."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, script, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _episode_until_paused(root: Path, script: list, **cfg_kw) -> LoopSupervisor:
    """Task 042 (steer 004): same shape as `_episode` above, for a script
    whose worker never yields a verdict. Since task 012 (`abdc914`) the
    stagnation guard on the (default) last approach pauses and escalates
    instead of ending the run `failed`, and then parks at `_gate()` forever
    waiting for an operator `resume()` that no scripted test here ever
    sends -- so drive it with the bounded-wait `_drive_until_paused` helper
    (`tests/test_worker_hat.py`) instead of letting
    `asyncio.run(sup._run_job_core())` block forever, and return the
    supervisor for the caller to read `status.json` off of directly.
    """
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, script, **cfg_kw)
    asyncio.run(_drive_until_paused(sup))
    return sup


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
    # Task 005 (#173): and the same for `termination`, which is REMOVED rather
    # than nulled -- a live resumed episode is not "an aborted run" either.
    assert "termination" not in mid, mid["termination"]


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
         "verdict": first["verdict"], "termination": first["termination"]}]
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
    # Resume lands back on `worker` here (task 001 is still `pending`, no
    # worker iteration ever finished it) -- a filler with no JSON payload
    # at all, safe regardless of which phase this script's one entry (it
    # repeats forever) actually gets dispatched to: absent from `worker`'s
    # `verdict` it is silently ignored (ordinary no-progress iteration),
    # and absent from `review`'s contract it is an ordinary non-verified
    # miss -- this test only cares about the ending record, not the outcome.
    # Since task 012 (`abdc914`) a worker that never reports a verdict
    # trips the stagnation guard on the (default) last approach into a
    # PAUSE (escalate) rather than a terminal `succeeded`/`failed`, so this
    # third episode is driven with `_episode_until_paused` (task 042, steer
    # 004) instead of letting it run forever -- the ending record this test
    # actually cares about is written at episode START, before any of that.
    sup3 = _episode_until_paused(
        root, [lambda sup: _ok("nothing conclusive to report")])
    status3 = sup3.run.read_status()
    assert status3["previousEndings"] == \
        crashed["previousEndings"] == [
            {"endedAt": after_abort["endedAt"], "reason": after_abort["reason"],
             "verdict": after_abort["verdict"],
             "termination": after_abort["termination"]}]
    assert status3["state"] == "paused", status3
    assert status3["pauseReason"] == "stagnation", status3


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


# -- what is now archived, not carried forward (task 005, #173) -----------


def test_the_termination_record_is_archived_into_previous_endings_on_resume(
        aborted_then_resumed):
    """Task 005 (#173): what USED to be true (`termination` carried forward
    unreset across a resume) is now the bug -- the record is folded into the
    matching `previousEndings` entry instead of left at the top level, and
    the on-disk marker is archived out of the run dir root alongside it."""
    root, first, _mid, final = aborted_then_resumed
    assert first["termination"]["class"] == "operator"
    assert ABORT_REASON in first["termination"]["reason"]
    assert final["previousEndings"][-1]["termination"] == first["termination"]
    assert not (root / "operator-termination.json").exists(), (
        "the marker must not still be readable at the run dir root once the "
        "episode it recorded is superseded")
    archived = sorted((root / "previous-endings").glob(
        "*-operator-termination.json"))
    assert len(archived) == 1, archived
    on_disk = json.loads(archived[0].read_text())
    assert on_disk["class"] == first["termination"]["class"] == "operator"
    assert on_disk["reason"] == first["termination"]["reason"]


def test_a_resume_after_an_operator_termination_leaves_no_termination_key_once_it_ends_clean(
        aborted_then_resumed):
    """THE regression this task fixes, as reported by #173: a run stopped by
    an operator two episodes ago, then resumed into a clean success, must
    not still carry `termination` (a `--json` consumer, the hub, or any
    future classifier reading it would see an aborted run that in fact
    succeeded).

    Absence, not `termination: null`: the ONE question every consumer of this
    field asks is "was this run told to stop?", and a present-but-null key
    answers that with "the key is here" for anyone testing presence (`"in"`,
    `hasOwnProperty`, a jq `has("termination")`). So this asserts the key is
    gone from status.json outright -- the first attempt at this task left it
    at `null` and the looser "absent OR null" assertion hid it.
    """
    root, _first, _mid, final = aborted_then_resumed
    assert final["state"] == "succeeded" and final["verdict"] == "verified"
    assert "termination" not in final, final["termination"]
    # ... and not merely absent from the parsed dict this test was handed:
    # gone from the bytes on disk any other reader would parse.
    on_disk = json.loads((root / "status.json").read_text())
    assert "termination" not in on_disk, on_disk["termination"]
    assert on_disk["previousEndings"][-1]["termination"]["class"] == "operator", (
        "the record is relocated, not deleted")


def test_update_status_removes_a_key_given_the_remove_sentinel(tmp_path):
    """The primitive the fix above rests on (task 005, #173): `update_status`
    can DELETE a key, not only overwrite it with `None`, and asking it to
    delete a key that is not there is a no-op rather than an error."""
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    run.update_status(state="aborted", termination={"class": "operator"})
    assert run.read_status()["termination"] == {"class": "operator"}

    after = run.update_status(termination=REMOVE_STATUS_KEY, state="running")

    assert "termination" not in after, after["termination"]
    assert "termination" not in json.loads((root / "status.json").read_text())
    assert after["state"] == "running", "ordinary keys still patch normally"
    assert "termination" not in run.update_status(termination=REMOVE_STATUS_KEY)


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
      "the SAME write that appends the superseded ending also\nfolds that episode's `termination`"]),
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
