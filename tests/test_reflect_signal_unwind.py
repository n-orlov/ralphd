"""Task 016 (#47): no manufactured reflection failure when the engine is
unwinding on a signal.

`reflect: true` runs one extra iteration *after* the job reaches a terminal
state -- including the terminal state a SIGTERM produces. But by the time
`abort_on_signal()` has run, `_record_abort()` has already fired the child
killer (`runner.interrupt()`), main.py's handler has set its stop event, and
whatever sent the signal (`ralphctl stop`, `docker stop`, the container runtime
during a host shutdown) is counting down to SIGKILL. Spawning a fresh agent
into that produces a reflect *failure the engine manufactured itself*, recorded
as `reflect: {ok: false, ...}` with an `artifacts/reflection/FAILED.md`
tombstone that blames the reflection for the teardown -- and, worse, makes a
run that was cleanly stopped look like one whose post-mortem broke.

The phase is now recorded as **not attempted**: `reflect: {ok: null,
attempted: false, skipped: "<why>"}` plus a `reflect_skipped` event, no
FAILED.md, no reflect iteration. `ok: null` is what keeps every existing
consumer intact -- `ralphctl status` and the hub both gate on `ok is False`.

Three tiers:

* `LoopSupervisor._run_reflection()` over a real run dir with a fake
  `run_iteration` (fast): the skip, the recorded shape, and the two cases that
  must still be attempted (no signal at all; an *API* abort, which is not a
  signal);
* the CLI formatter `_format_reflect_lines` over the new shape;
* one real engine, really SIGTERMed mid-job, with `reflect: true`.
"""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import pytest
from test_e2e import EngineProc

from ralphd.cli.main import _format_reflect_lines
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir


# --------------------------------------------------------------------------
# the engine: _run_reflection() over a real run dir, no subprocess
# --------------------------------------------------------------------------

def _supervisor(root: Path) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    return LoopSupervisor(JobConfig(run_id="unit", reflect=True),
                          RunDir(root=root), root)


class _FakeReflect:
    """Stands in for run_iteration('reflect'): records that it was called and
    (optionally) signals the engine from inside the attempt."""

    def __init__(self, sup: LoopSupervisor, signal_mid_attempt: bool = False,
                 write_report: bool = True):
        self.sup = sup
        self.calls: list[str] = []
        self.signal_mid_attempt = signal_mid_attempt
        self.write_report = write_report

    async def __call__(self, phase: str, **kw) -> IterationResult:
        self.calls.append(phase)
        if self.signal_mid_attempt:
            self.sup.abort_on_signal(signal.SIGTERM)
            return IterationResult(interrupted=True, exit_code=-2)
        if self.write_report:
            outdir = self.sup.run.artifacts_dir / "reflection"
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "report.md").write_text("# stub report\n")
        return IterationResult(exit_code=0)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _reflect_dir(sup: LoopSupervisor) -> Path:
    return sup.run.artifacts_dir / "reflection"


def test_a_signalled_engine_does_not_attempt_reflect_at_all(tmp_path):
    sup = _supervisor(tmp_path / "signalled")
    fake = _FakeReflect(sup)
    sup.run_iteration = fake
    sup.abort_on_signal(signal.SIGTERM)

    asyncio.run(sup._run_reflection())

    assert fake.calls == [], "reflect was attempted while the engine was dying"
    reflect = sup.run.read_status()["reflect"]
    assert reflect["ok"] is None, "a teardown is not a reflection failure"
    assert reflect["attempted"] is False
    assert reflect["error"] is None
    assert "15" in reflect["skipped"], reflect["skipped"]
    assert "no reflect iteration was attempted" in reflect["skipped"]
    assert reflect["endedAt"]
    # no tombstone, no report, and the phase field is left clean
    assert not (_reflect_dir(sup) / "FAILED.md").exists()
    assert not (_reflect_dir(sup) / "report.md").exists()
    assert sup.run.read_status()["phase"] is None
    # ... and it says so in the event stream, once
    skipped = _events(sup.run.root, "reflect_skipped")
    assert [(ev["attempted"], ev["signal"]) for ev in skipped] == [(False, "15")]
    assert not _events(sup.run.root, "reflect_done")
    # not even a `phase: reflect` event: nothing was entered
    assert not [ev for ev in _events(sup.run.root, "phase")
                if ev.get("phase") == "reflect"]


def test_an_unsignalled_engine_still_attempts_reflect_and_records_the_verdict(tmp_path):
    """The mutation guard in the other direction: the skip must be narrow."""
    sup = _supervisor(tmp_path / "clean")
    fake = _FakeReflect(sup)
    sup.run_iteration = fake

    asyncio.run(sup._run_reflection())

    assert fake.calls == ["reflect"]
    assert sup.run.read_status()["reflect"] == {
        "ok": True, "error": None,
        "endedAt": sup.run.read_status()["reflect"]["endedAt"]}
    assert not (_reflect_dir(sup) / "FAILED.md").exists()
    assert [ev["ok"] for ev in _events(sup.run.root, "reflect_done")] == [True]


def test_an_api_abort_is_not_a_signal_so_reflect_is_still_attempted(tmp_path):
    """`POST /abort` ends the job but leaves the engine alive and able to write
    its post-mortem -- the operator veto only costs reflect its retries
    (_begin_reflect_retry_window), never the attempt."""
    sup = _supervisor(tmp_path / "api-abort")
    fake = _FakeReflect(sup, write_report=False)
    sup.run_iteration = fake
    sup.abort("wrong PRD")

    asyncio.run(sup._run_reflection())

    assert fake.calls == ["reflect"]
    status = sup.run.read_status()
    assert status["reflect"]["ok"] is False, \
        "a real reflect failure must still be recorded as one"
    assert (_reflect_dir(sup) / "FAILED.md").exists()


def test_a_signal_during_the_attempt_is_recorded_as_not_completed(tmp_path):
    """The other half of the same defect: the signal can arrive after the
    attempt started. What the interrupted iteration returned describes the
    teardown, so it must not become a reflection failure either."""
    sup = _supervisor(tmp_path / "mid-attempt")
    fake = _FakeReflect(sup, signal_mid_attempt=True)
    sup.run_iteration = fake

    asyncio.run(sup._run_reflection())

    assert fake.calls == ["reflect"], "the attempt did start"
    reflect = sup.run.read_status()["reflect"]
    assert reflect["ok"] is None
    assert reflect["attempted"] is True
    assert "during the reflect phase" in reflect["skipped"]
    assert not (_reflect_dir(sup) / "FAILED.md").exists()
    assert [ev["attempted"] for ev in _events(sup.run.root, "reflect_skipped")] \
        == [True]


def test_an_operator_abort_followed_by_a_signal_still_skips_reflect(tmp_path):
    """`ralphctl stop` = POST /abort *and then* SIGTERM. The abort keeps its
    operator class (task 015), but the signal still means the process is going
    away, so there is no attempt to be had."""
    sup = _supervisor(tmp_path / "stop")
    fake = _FakeReflect(sup)
    sup.run_iteration = fake
    sup.abort("stopped by operator")
    sup.abort_on_signal(signal.SIGTERM)

    asyncio.run(sup._run_reflection())

    assert fake.calls == []
    assert sup.run.read_status()["reflect"]["attempted"] is False
    assert not (_reflect_dir(sup) / "FAILED.md").exists()


# --------------------------------------------------------------------------
# the CLI formatter: the third shape reads as itself, not as a failure
# --------------------------------------------------------------------------

def test_status_renders_a_not_attempted_reflection_as_its_own_line():
    lines = _format_reflect_lines(
        {"ok": None, "attempted": False, "error": None,
         "skipped": "signal 15 ended the engine first",
         "endedAt": "2026-08-22T09:31:20Z"})
    assert lines == ["reflection: not attempted (signal 15 ended the engine first)"]


def test_status_renders_a_cut_short_reflection_as_not_completed():
    lines = _format_reflect_lines(
        {"ok": None, "attempted": True, "error": None,
         "skipped": "signal 15 ended the engine mid-phase"})
    assert lines == ["reflection: not completed (signal 15 ended the engine mid-phase)"]


def test_a_long_skipped_reason_wraps_like_the_failure_line_does():
    reason = "signal 15 ended the engine " + ("x" * 200)
    lines = _format_reflect_lines({"ok": None, "attempted": False,
                                   "skipped": reason})
    assert len(lines) > 1
    assert lines[0].startswith("reflection: not attempted (")
    for extra in lines[1:]:
        assert extra.startswith("            ")
    rejoined = " ".join(line.removeprefix("reflection: ")
                            .removeprefix("            ") for line in lines)
    assert rejoined.replace(" ", "") == f"notattempted({reason})".replace(" ", "")


def test_the_pre_existing_reflect_shapes_render_exactly_as_before():
    assert _format_reflect_lines({"ok": False, "error": "Connection error."}) \
        == ["reflection: failed (Connection error.)"]
    assert _format_reflect_lines({"ok": True, "error": None}) == []
    assert _format_reflect_lines(None) == []
    assert _format_reflect_lines({}) == []
    assert _format_reflect_lines("nonsense") == []
    # `ok: null` with no reason recorded (a pre-0.7 run dir, or the phase not
    # ended yet) is still silence, not an empty "not attempted ()"
    assert _format_reflect_lines({"ok": None}) == []
    assert _format_reflect_lines({"ok": None, "skipped": "  "}) == []


# --------------------------------------------------------------------------
# one real engine, really SIGTERMed
# --------------------------------------------------------------------------

@pytest.fixture
def engine_factory(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "reflect-signal", "iterations": 12,
                    "max_approaches": 1, "on_complete": "exit"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def test_a_sigtermed_engine_records_reflect_as_not_attempted(engine_factory):
    """The whole path, end to end: a real engine with reflect enabled, taken
    down by a real SIGTERM mid-job (what `ralphctl stop` and `docker stop` both
    do)."""
    e = engine_factory(job={"reflect": True, "iterations": 6},
                       stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "1.5"})
    e.wait_api()
    e.wait_state(("running",), timeout=30)

    e.proc.send_signal(signal.SIGTERM)
    assert e.proc.wait(timeout=60) != 0, "a signalled run is not a success"

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "aborted"
    assert status["termination"]["signal"] == "15"
    # the reflection: not attempted, not failed, and no tombstone
    reflect = status["reflect"]
    assert reflect["ok"] is None, \
        f"the engine manufactured a reflection verdict: {reflect}"
    assert reflect["attempted"] is False
    assert "no reflect iteration was attempted" in reflect["skipped"]
    reflection = e.run_dir / "artifacts" / "reflection"
    assert not (reflection / "FAILED.md").exists(), \
        "a tombstone blaming the reflection for the operator's SIGTERM"
    assert not (reflection / "report.md").exists()
    # no reflect iteration was spawned
    metas = [json.loads(p.read_text())
             for p in sorted((e.run_dir / "iterations").glob("*/meta.json"))]
    assert "reflect" not in [m.get("phase") for m in metas], metas
    assert [ev["attempted"] for ev in _events(e.run_dir, "reflect_skipped")] \
        == [False]


# --------------------------------------------------------------------------
# the documents: every claim above is written down where operators read it
# --------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path, needles", [
    ("SPEC.md",
     ["no attempt, no tombstone (\u00a78.4)",
      "`ok: null` with `{attempted, skipped}` instead means a signal was "
      "already taking the engine down",
      "| `reflect_skipped` | `attempted`, `signal`, `reason` |",
      "_record_reflect_not_attempted()",
      "deliberately writes **no** `FAILED.md`",
      "An *API* abort is\nnot a signal"]),
    ("docs/api.md",
     ['"reflect": {"ok": null, "attempted": false, "error": null,',
      "**no `artifacts/reflection/FAILED.md` is written**",
      "| `reflect_skipped` | the post-terminal `reflect` phase produced no "
      "verdict"]),
    ("docs/cli.md",
     ["  reflection: not attempted (signal 15 ended the engine before the "
      "reflect\n              phase could start, so no reflect iteration was "
      "attempted)",
      "Neither is a failure and neither leaves an "
      "`artifacts/reflection/FAILED.md`\n  behind"]),
])
def test_the_docs_state_the_new_rule(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"


def test_spec_8_4_explains_why_no_tombstone_is_written():
    """The reasoning, not just the behaviour: the tombstone is an assertion
    ("tried and failed") that this path would make falsely."""
    section = (REPO / "SPEC.md").read_text()
    section = section.split("### 8.4 Retry, backoff and the outage budget")[1] \
                     .split("### 8.5 Skipping the wait")[0]
    assert "abort_on_signal()" in section
    assert "the tombstone asserts the reflection was" in section
    assert "reflect_skipped" in section
    assert "gate on `ok === false`" in section
