"""Task 032 (#13): a resuming engine appends an explicit `running` state event.

`events.jsonl` is append-only across resumes, so a run dir that already died
once carries a terminal `state` event in the middle of its log. Followers
(`ralphctl watch`, `logs -f`) reconcile a terminal marker against the log's
later *state* events (task 031) -- which only works if a resumed engine
actually records that it started working again. These tests pin that down from
the outside: two real `ralphd-engine` processes over one run dir, then read
`events.jsonl` off disk.
"""

from __future__ import annotations

import json

from test_e2e import engine_factory

__all__ = ["engine_factory"]


def _events(run_dir) -> list[dict]:
    return [json.loads(line) for line in
            (run_dir / "events.jsonl").read_text().splitlines()]


def _state_events(run_dir) -> list[dict]:
    return [ev for ev in _events(run_dir) if ev.get("type") == "state"]


TERMINAL = ("succeeded", "failed", "aborted")


def test_fresh_run_opens_with_a_running_state_event(engine_factory):
    """Baseline: even a fresh run's log opens with its own `running` state
    event, flagged as not-a-resume, before the terminal one."""
    e = engine_factory(job={"on_complete": "exit"})
    assert e.proc.wait(timeout=60) == 0

    states = _state_events(e.run_dir)
    assert [ev["state"] for ev in states] == ["running", "succeeded"]
    assert states[0]["resumed"] is False
    # ids are monotonic, so "the log's last state event" is the terminal one
    assert states[0]["id"] < states[1]["id"]


def test_resume_appends_a_running_state_event_after_the_stale_terminal_marker(
        engine_factory):
    """The real scenario: process 1 exhausts its budget and records a
    terminal `failed` state event; process 2 (same run dir, bumped budget --
    what `ralphctl resume` does) must append a `running` state event with a
    higher id, so the historical marker is no longer the log's last word on
    the run's state."""
    e1 = engine_factory(job={"on_complete": "exit", "iterations": 2,
                             "max_approaches": 1},
                        stub_env={"STUB_TASKS": "5"})
    assert e1.proc.wait(timeout=60) == 1
    assert json.loads((e1.run_dir / "status.json").read_text())["state"] == "failed"

    first_episode = _state_events(e1.run_dir)
    assert [ev["state"] for ev in first_episode] == ["running", "failed"]
    stale_marker = first_episode[-1]

    e2 = engine_factory(job={"on_complete": "exit", "iterations": 20,
                             "max_approaches": 1},
                        stub_env={"STUB_TASKS": "5"})
    assert e2.run_dir == e1.run_dir  # sanity: genuinely the same run dir
    assert e2.proc.wait(timeout=60) == 0

    states = _state_events(e2.run_dir)
    assert [ev["state"] for ev in states] == [
        "running", "failed", "running", "succeeded"]
    resumed_event = states[2]
    assert resumed_event["resumed"] is True, "the second process knows it resumed"
    assert resumed_event["id"] > stale_marker["id"], (
        "the resume's state event must supersede the stale terminal marker")

    # The contract a follower relies on (task 031): the mid-log terminal
    # marker has a later state event, the real terminus does not.
    def has_later_state_event(ev):
        return any(o["id"] > ev["id"] for o in states)

    assert has_later_state_event(stale_marker)
    assert states[-1]["state"] in TERMINAL
    assert not has_later_state_event(states[-1])
