"""Task 003 (#83 detection): verify placement instead of assuming it, and
make a later disappearance loud.

Task 001 root-caused #83 (a startup wiping ambient $HOME) and task 002 made
`place_skills()` refuse to run without an explicit `home=`/its own
`CONFIG_DIR` -- but neither of those stops a *mid-run* loss: steer 003 found
this run's own live container still losing a placed skill to a SEPARATE
engine's un-isolated startup, well after this engine's own placement had
already succeeded and been logged. A check that only runs once, at this
engine's own startup, cannot see that -- so this module tests a check that
runs at every iteration boundary and reports through `events.jsonl` (a run
EVENT, not just an INFO log line) both when a placement resolves and when
one that should still be there does not.

Three things get proven:
  (a) a successful placement emits `skills.placement.verified` with the
      placed names -- so "the check ran and found nothing wrong" is itself
      a fact on disk, not merely the absence of a complaint;
  (b) deleting the symlink behind the engine's back (simulating exactly
      steer 003's incident: another process's startup silently discarding
      it) makes the *next* check emit `skills.placement.lost` with that
      name, readable straight out of events.jsonl;
  (c) a lost-skill event does not crash or fail the iteration it's detected
      in -- the run keeps going, the iteration is recorded as an ordinary
      success, exactly as steer 003 asks ("would this design have caught
      today's incident without taking anything else down").

Per this run's standing rules: stubs `sup.runner` (a fake PiRunner), never
`sup._run_iteration_once` itself -- the iteration boundary hook
(`LoopSupervisor._verify_skills_placement`) is driven through the real
method, the same way test_iteration_cancellation_contained.py drives the
containment boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine import loop, skills
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_with_runner


# -- unit level: skills.verify_placement / verify_placement_and_report -----


class _RecordingRun:
    """Stand-in for RunDir's `.emit()` -- just records the calls, so the
    unit-level tests don't need a real run dir to assert against."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, type_: str, **data) -> dict:
        event = {"type": type_, **data}
        self.events.append(event)
        return event


def _mount(config_dir: Path, name: str) -> None:
    d = config_dir / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {name}\n")


def test_successful_placement_emits_verified_event_with_placed_names(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    _mount(config_dir, "telegram-notify")
    home = tmp_path / "home"
    home.mkdir()

    placed = skills.place_skills(config_dir, home=home)
    assert placed == ["telegram-notify"]

    run = _RecordingRun()
    skills.verify_placement_and_report(run, config_dir, home)

    verified = [e for e in run.events if e["type"] == "skills.placement.verified"]
    lost = [e for e in run.events if e["type"] == "skills.placement.lost"]
    assert lost == []
    assert len(verified) == 1
    assert verified[0]["names"] == ["telegram-notify"]


def test_deleting_the_symlink_makes_the_next_check_emit_the_loss_event(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    _mount(config_dir, "telegram-notify")
    _mount(config_dir, "other-skill")
    home = tmp_path / "home"
    home.mkdir()

    skills.place_skills(config_dir, home=home)
    run = _RecordingRun()
    skills.verify_placement_and_report(run, config_dir, home)
    assert run.events[-1]["type"] == "skills.placement.verified"

    # Behind the engine's back: exactly what steer 003 found in the wild --
    # some OTHER process's startup (not this one's own place_skills() call)
    # clearing the link out from under an engine that already logged it as
    # placed.
    (home / ".pi" / "agent" / "skills" / "telegram-notify").unlink()

    run.events.clear()
    skills.verify_placement_and_report(run, config_dir, home)

    lost = [e for e in run.events if e["type"] == "skills.placement.lost"]
    verified = [e for e in run.events if e["type"] == "skills.placement.verified"]
    assert len(lost) == 1
    assert lost[0]["names"] == ["telegram-notify"]
    assert len(verified) == 1
    assert verified[0]["names"] == ["other-skill"]


def test_verification_never_raises_even_if_the_underlying_check_blows_up(
    tmp_path: Path, monkeypatch
) -> None:
    def boom(_config_dir, _home):
        raise OSError("simulated filesystem error")

    monkeypatch.setattr(skills, "verify_placement", boom)
    run = _RecordingRun()

    skills.verify_placement_and_report(run, tmp_path, tmp_path)  # must not raise

    assert run.events == []


# -- integration level: the iteration-boundary hook in LoopSupervisor ------


class _ScriptedRunner:
    """Stands in for PiRunner -- sup.runner, never sup._run_iteration_once,
    per this run's standing rule."""

    def __init__(self, run: RunDir) -> None:
        self.rundir = run

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        r = IterationResult(exit_code=0)
        r.final_text = "working"
        r.duration_s = 30.0
        r.usage = {"input": 10, "output": 5, "totalTokens": 15}
        return r


def _events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


_supervisor = supervisor_with_runner(
    lambda sup, config_dir: _ScriptedRunner(sup.run),
    run_id="unit", iterations=8, max_approaches=1, vigilant=False, on_complete="exit",
    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


@pytest.mark.asyncio
async def test_iteration_boundary_recheck_emits_verified_event(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(loop, "CONFIG_DIR", config_dir)
    _mount(config_dir, "telegram-notify")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    skills.place_skills(config_dir, home=home)

    sup = _supervisor(tmp_path / "run", config_dir)
    result = await sup._run_iteration_once("worker")

    assert result.exit_code == 0
    events = _events(sup.run.root)
    verified = [e for e in events if e["type"] == "skills.placement.verified"]
    assert len(verified) == 1
    assert verified[0]["names"] == ["telegram-notify"]


@pytest.mark.asyncio
async def test_a_mid_run_loss_surfaces_at_the_next_iteration_boundary_without_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(skills, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(loop, "CONFIG_DIR", config_dir)
    _mount(config_dir, "telegram-notify")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    skills.place_skills(config_dir, home=home)

    sup = _supervisor(tmp_path / "run", config_dir)
    first = await sup._run_iteration_once("worker")
    assert first.exit_code == 0

    # Simulated steer-003 incident: something OTHER than this engine's own
    # placement path removes the link mid-run.
    (home / ".pi" / "agent" / "skills" / "telegram-notify").unlink()

    second = await sup._run_iteration_once("worker")

    # (c): the loss is detected loudly, but the iteration itself is an
    # ordinary recorded success -- detection never costs an iteration or
    # takes the loop down.
    assert second.exit_code == 0
    events = _events(sup.run.root)
    lost = [e for e in events if e["type"] == "skills.placement.lost"]
    assert len(lost) == 1
    assert lost[0]["names"] == ["telegram-notify"]

    meta_files = sorted((sup.run.root / "iterations").iterdir())
    last_meta = json.loads((meta_files[-1] / "meta.json").read_text())
    assert last_meta["endedAt"], "second iteration recorded as a normal end"
    assert last_meta["faultClass"] is None, "detection scored no fault"
