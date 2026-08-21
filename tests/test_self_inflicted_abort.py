"""Task 015 (#46): a self-inflicted abort is a distinct class from an operator
abort.

`LoopSupervisor.abort()` used to be the single path for "this run is ending on
someone's instruction", and it wrote `operator-termination.json`
unconditionally -- so a run shot from inside its own container (the agent's own
`pkill -f ralphd-engine`, a `docker stop` of the job from a sibling it started)
recorded `{"action": "abort", "reason": "signal 15", "source": "engine"}` with no
operator anywhere near it. That marker is load-bearing: auto-resume refuses to
resurrect a run carrying it. Right for a run the operator killed, exactly wrong
for one killed by accident from inside, which is the case self-recovery exists
for.

Four tiers, no engine and no real container:

* the vocabulary and the evidence reader in `engine/state.py` (unit);
* `LoopSupervisor.abort()` vs `.abort_on_signal()` over a real run dir --
  one test per class asserting the two are distinguishable in `status.json`;
* `engine/main.py`'s signal handler, read out of its own AST (the wiring is the
  whole point: a handler still calling `abort()` reinstates the defect);
* the two host-side consequences through `ralphctl`: `status` stops printing a
  bare `reason: signal 15`, and `doctor --fix` still never resurrects an
  operator abort while it does resume a self-inflicted one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.cli.main import _format_termination_lines
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    TERMINATION_CLASS_OPERATOR,
    TERMINATION_CLASS_SELF,
    TERMINATION_CLASSES,
    TERMINATION_EVIDENCE_ARGS_MAX,
    format_last_tool_call,
    is_operator_termination,
    last_tool_call,
    read_operator_termination,
    record_operator_termination,
    termination_class,
)

__all__ = ["ctl", "unix_sock"]

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "ralphd"


# --------------------------------------------------------------------------
# the vocabulary and the back-compat reading
# --------------------------------------------------------------------------

def test_there_are_exactly_two_termination_classes():
    assert TERMINATION_CLASSES == (TERMINATION_CLASS_OPERATOR,
                                   TERMINATION_CLASS_SELF)
    assert TERMINATION_CLASS_OPERATOR != TERMINATION_CLASS_SELF


def test_no_marker_has_no_class_and_is_not_an_operator_termination():
    assert termination_class(None) is None
    assert termination_class({}) is None
    assert termination_class("nonsense") is None
    assert is_operator_termination(None) is False


def test_a_marker_written_before_v0_7_reads_as_an_operator_termination():
    """The back-compat default, and the only safe direction: an old marker can
    only ever *refuse* a resume, never cause one."""
    old = {"action": "abort", "at": "2026-01-01T00:00:00Z",
           "reason": "aborted by operator", "source": "cli"}
    assert termination_class(old) == TERMINATION_CLASS_OPERATOR
    assert is_operator_termination(old) is True
    # ... as does a marker carrying a class this build does not know
    assert termination_class({**old, "class": "martian"}) == \
        TERMINATION_CLASS_OPERATOR


def test_recording_defaults_to_the_operator_class(tmp_path):
    record_operator_termination(tmp_path, "stop", reason="stopped by operator",
                               source="cli")
    doc = read_operator_termination(tmp_path)
    assert doc["class"] == TERMINATION_CLASS_OPERATOR
    assert is_operator_termination(doc) is True
    assert "evidence" not in doc


def test_a_self_inflicted_record_is_not_an_operator_termination(tmp_path):
    record_operator_termination(
        tmp_path, "abort", reason="self-inflicted termination: signal 15 ...",
        source="engine", termination_class=TERMINATION_CLASS_SELF,
        evidence={"tool": "bash"})
    doc = read_operator_termination(tmp_path)
    assert termination_class(doc) == TERMINATION_CLASS_SELF
    assert is_operator_termination(doc) is False
    assert doc["evidence"] == {"tool": "bash"}


# --------------------------------------------------------------------------
# the evidence: the last tool call before the signal, out of output.jsonl
# --------------------------------------------------------------------------

def _transcript(root: Path, number: int, *events: dict) -> Path:
    d = root / "iterations" / f"{number:04d}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "output.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def _tool_start(name: str, **args) -> dict:
    return {"type": "tool_execution_start", "toolCallId": "t1",
            "toolName": name, "args": args}


def test_evidence_is_the_last_tool_call_of_the_last_iteration(tmp_path):
    _transcript(tmp_path, 1, _tool_start("read", path="/workspace/x"))
    _transcript(tmp_path, 2,
                _tool_start("bash", command="ls"),
                _tool_start("bash", command="pkill -f ralphd-engine"),
                {"type": "message_end", "message": {}})
    ev = last_tool_call(tmp_path)
    assert ev == {"iteration": 2, "tool": "bash",
                  "args": '{"command": "pkill -f ralphd-engine"}',
                  "transcript": "iterations/0002/output.jsonl"}
    line = format_last_tool_call(ev)
    assert line.startswith("last tool call before the signal: bash(")
    assert "pkill -f ralphd-engine" in line
    assert "iterations/0002/output.jsonl" in line


def test_evidence_falls_back_to_an_earlier_iteration_with_no_tool_calls(tmp_path):
    _transcript(tmp_path, 1, _tool_start("bash", command="git log"))
    _transcript(tmp_path, 2, {"type": "message_start"})
    assert last_tool_call(tmp_path)["iteration"] == 1


def test_no_transcript_no_evidence_and_no_line(tmp_path):
    assert last_tool_call(tmp_path) is None
    _transcript(tmp_path, 1, {"type": "message_end"})
    assert last_tool_call(tmp_path) is None
    assert format_last_tool_call(None) == ""
    assert format_last_tool_call({}) == ""


def test_a_junk_transcript_line_never_raises(tmp_path):
    d = tmp_path / "iterations" / "0001"
    d.mkdir(parents=True)
    # the junk sits AFTER the good line, i.e. it is what the newest-first scan
    # hits FIRST -- a reader that lets it raise takes the abort path down with it
    (d / "output.jsonl").write_text(
        json.dumps(_tool_start("bash", command="true")) + "\n"
        + '{"type": "tool_execution_start" truncated\n'
        + "not json at all\n")
    assert last_tool_call(tmp_path)["args"] == '{"command": "true"}'


def test_evidence_arguments_are_truncated(tmp_path):
    _transcript(tmp_path, 1, _tool_start("bash", command="x" * 5000))
    args = last_tool_call(tmp_path)["args"]
    assert len(args) <= TERMINATION_EVIDENCE_ARGS_MAX + 3
    assert args.endswith("...")


def test_evidence_arguments_are_scrubbed_of_secrets(tmp_path, monkeypatch):
    """The evidence lands in two on-disk documents, so it goes through the same
    scrubbing `events.jsonl` gets."""
    from ralphd.engine import redact

    monkeypatch.setenv("GH_TOKEN", "ghp-supersecret-value")
    redact.refresh_redaction_map()
    try:
        _transcript(tmp_path, 1,
                    _tool_start("bash", command="curl -H ghp-supersecret-value"))
        assert "ghp-supersecret-value" not in last_tool_call(tmp_path)["args"]
    finally:
        monkeypatch.delenv("GH_TOKEN")
        redact.refresh_redaction_map()


# --------------------------------------------------------------------------
# one test per class, at the engine boundary that writes them
# --------------------------------------------------------------------------

def _supervisor(root: Path) -> LoopSupervisor:
    from ralphd.engine.state import RunDir

    root.mkdir(parents=True, exist_ok=True)
    return LoopSupervisor(JobConfig(run_id="unit"), RunDir(root=root), root)


def test_an_operator_abort_records_the_operator_class(tmp_path):
    sup = _supervisor(tmp_path / "operator")
    sup.abort("wrong PRD")

    doc = read_operator_termination(sup.run.root)
    assert termination_class(doc) == TERMINATION_CLASS_OPERATOR
    assert is_operator_termination(doc) is True
    assert doc["reason"] == "wrong PRD"
    status = sup.run.read_status()
    assert status["termination"]["class"] == TERMINATION_CLASS_OPERATOR
    assert status["termination"]["signal"] is None
    # the pre-existing abort bookkeeping is untouched
    assert sup._abort_reason == "wrong PRD"
    assert sup.operator_abort_requested is True
    assert sup._operator_abort_recorded is True


def test_a_signal_nobody_asked_for_records_the_self_inflicted_class(tmp_path):
    sup = _supervisor(tmp_path / "selfkill")
    _transcript(sup.run.root, 7,
                _tool_start("bash", command="pkill -f ralphd-engine"))
    sup.abort_on_signal(15)

    doc = read_operator_termination(sup.run.root)
    assert termination_class(doc) == TERMINATION_CLASS_SELF
    assert is_operator_termination(doc) is False, \
        "a self-inflicted kill must stay eligible for auto-resume"
    status = sup.run.read_status()
    term = status["termination"]
    assert term["class"] == TERMINATION_CLASS_SELF
    assert term["signal"] == "15"
    assert term["evidence"]["tool"] == "bash"
    assert term["evidence"]["transcript"] == "iterations/0007/output.jsonl"
    # the two documents agree -- the marker is what auto-resume reads, the
    # status field is what every other reader polls
    assert doc["evidence"] == term["evidence"]
    assert doc["reason"] == term["reason"] == sup._abort_reason


def test_the_two_classes_are_distinguishable_in_status_json(tmp_path):
    """The bare requirement, side by side: same file, same field, two answers."""
    op = _supervisor(tmp_path / "a")
    op.abort("operator asked")
    self_ = _supervisor(tmp_path / "b")
    self_.abort_on_signal(15)
    assert (op.run.read_status()["termination"]["class"]
            != self_.run.read_status()["termination"]["class"])


def test_the_self_inflicted_reason_is_not_a_bare_signal_number(tmp_path):
    """Requirement E: `reason: signal 15` told an operator nothing. The reason
    now names the class, why the engine believes it, the resume eligibility and
    the last tool call before the signal."""
    sup = _supervisor(tmp_path / "reason")
    _transcript(sup.run.root, 3,
                _tool_start("bash", command="pkill -f ralphd-engine"))
    sup.abort_on_signal(15)
    reason = sup._abort_reason
    assert reason != "signal 15"
    assert "self-inflicted" in reason
    assert "no operator abort recorded" in reason
    assert "auto-resume" in reason
    assert "pkill -f ralphd-engine" in reason
    assert "signal 15" in reason, "the raw signal is still reported, just not alone"


def test_a_signal_after_an_operator_abort_stays_the_operator_class(tmp_path):
    """`ralphctl abort` (or `stop`) posts /abort and the container then takes a
    SIGTERM. The operator's claim is the one that counts -- the class must not
    be downgraded by the signal that follows it."""
    sup = _supervisor(tmp_path / "both")
    sup.abort("aborted by operator")
    sup.abort_on_signal(15)
    doc = read_operator_termination(sup.run.root)
    assert is_operator_termination(doc) is True
    assert sup.run.read_status()["termination"]["class"] == \
        TERMINATION_CLASS_OPERATOR
    assert "signal 15" in doc["reason"], \
        "the signal is still recorded, it just does not reclassify the abort"


def test_abort_on_signal_still_ends_the_run_like_abort_does(tmp_path):
    """The class is the only thing that changed: the run still unwinds."""
    sup = _supervisor(tmp_path / "unwind")
    assert sup.operator_abort_requested is False
    sup.abort_on_signal(15)
    assert sup.operator_abort_requested is True
    assert sup._operator_interrupted is True
    assert sup.budget_left() is False


# --------------------------------------------------------------------------
# the wiring: engine/main.py's signal handler
# --------------------------------------------------------------------------

def _signal_handler_source() -> str:
    """The `add_signal_handler` call in engine/main.py, as source."""
    tree = ast.parse((SRC / "engine" / "main.py").read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_signal_handler"):
            return ast.unparse(node)
    raise AssertionError("engine/main.py registers no signal handler")


def test_the_engine_signal_handler_classifies_instead_of_aborting():
    src = _signal_handler_source()
    assert "abort_on_signal" in src, src
    assert "abort(f'signal" not in src.replace('"', "'"), src


# --------------------------------------------------------------------------
# `ralphctl status`
# --------------------------------------------------------------------------

_SELF_TERM = {
    "class": TERMINATION_CLASS_SELF, "action": "abort", "signal": "15",
    "at": "2024-01-01T01:02:03Z",
    "reason": ("self-inflicted termination: signal 15 ended the engine with no "
               "operator abort recorded, so it came from inside this run's own "
               "container"),
    "evidence": {"iteration": 37, "tool": "bash",
                 "args": '{"command": "pkill -f ralphd-engine"}',
                 "transcript": "iterations/0037/output.jsonl"},
}


def test_the_status_line_names_the_class_the_signal_and_the_evidence():
    lines = _format_termination_lines(_SELF_TERM)
    assert lines[0].startswith("termination: self-inflicted (signal 15)")
    assert "eligible for auto-resume" in lines[0]
    body = " ".join(ln.strip() for ln in lines[1:])
    assert "last tool call before the signal" in body
    assert "pkill -f ralphd-engine" in body
    assert "iterations/0037/output.jsonl" in body


def test_no_status_line_for_an_operator_abort_or_a_run_nobody_stopped():
    assert _format_termination_lines(
        {"class": TERMINATION_CLASS_OPERATOR, "action": "abort",
         "reason": "aborted by operator"}) == []
    assert _format_termination_lines(None) == []
    assert _format_termination_lines("nonsense") == []


def test_a_self_inflicted_termination_with_no_evidence_still_names_the_class():
    lines = _format_termination_lines(
        {"class": TERMINATION_CLASS_SELF, "signal": "15", "evidence": None})
    assert len(lines) == 1 and "self-inflicted (signal 15)" in lines[0]


_BASE_STATUS = {
    "state": "aborted", "verdict": "unverified", "phase": None, "approach": 1,
    "iterationsUsed": 4, "iterationsBudget": 250, "schemaVersion": 1,
    "startedAt": "2024-01-01T00:00:00Z", "endedAt": "2024-01-01T01:02:03Z",
    "tasks": {"total": 3, "completed": 1, "pending": 2},
    "usage": {"costUSD": 0.5, "totalTokens": 12000},
}


def _seed_status(c: Ctl, run_id: str, **over) -> Path:
    rdir, _cdir = _seed_run(c, run_id)
    (rdir / "status.json").write_text(
        json.dumps({**_BASE_STATUS, "runId": run_id, **over}))
    return rdir


def test_status_reports_the_self_inflicted_class_instead_of_a_bare_signal(ctl: Ctl):
    _seed_status(ctl, "tst-selfkill", reason=_SELF_TERM["reason"],
                 termination=_SELF_TERM)
    res = ctl.run("status", "tst-selfkill")
    assert res.returncode == 0, res.stderr
    assert "reason:    signal 15" not in res.stdout, res.stdout
    assert "termination: self-inflicted (signal 15)" in res.stdout, res.stdout
    # the evidence line wraps, so compare on normalised whitespace
    flat = " ".join(res.stdout.split())
    assert "pkill -f ralphd-engine" in flat, res.stdout
    assert json.loads(ctl.run("--json", "status",
                              "tst-selfkill").stdout)["termination"] == _SELF_TERM


def test_status_output_is_unchanged_for_an_operator_abort(ctl: Ctl):
    """Every run that is not a self-inflicted kill prints exactly the bytes it
    printed before this task existed."""
    _seed_status(ctl, "tst-opabort", reason="aborted by operator",
                 termination={"class": TERMINATION_CLASS_OPERATOR,
                              "action": "abort", "signal": None,
                              "reason": "aborted by operator",
                              "evidence": None})
    _seed_status(ctl, "tst-plainabort", reason="aborted by operator")
    with_field = ctl.run("status", "tst-opabort")
    without = ctl.run("status", "tst-plainabort")
    assert with_field.returncode == 0 and without.returncode == 0
    assert (with_field.stdout.replace("tst-opabort", "RUN")
            == without.stdout.replace("tst-plainabort", "RUN"))
    assert "termination:" not in with_field.stdout


# --------------------------------------------------------------------------
# auto-resume: the never-resurrect guarantee, and the new eligibility
# --------------------------------------------------------------------------

def _start(c: Ctl, run_id: str) -> None:
    res = c.run("start", "--prd", str(c.prd), "--llm", "none",
                "--run-id", run_id, "--auto-resume")
    assert res.returncode == 0, res.stderr


def _mark(c: Ctl, run_id: str, **over) -> None:
    doc = {"action": "abort", "at": "2026-01-01T00:00:00Z",
           "reason": "signal 15", "source": "engine", **over}
    (c.registry / "runs" / run_id / "operator-termination.json").write_text(
        json.dumps(doc))


def _crashed(c: Ctl, run_id: str) -> None:
    """The shape of a run whose container vanished: non-terminal state, no
    container."""
    (c.registry / "runs" / run_id / "status.json").write_text(
        json.dumps({"state": "running", "schemaVersion": 1,
                    "iterationsUsed": 3}))


def _doctor_fix(c: Ctl) -> dict:
    res = c.run("--json", "doctor", "--fix", env={
        "STUB_DOCKER_INSPECT_OK": "1",
        "STUB_DOCKER_CONTAINERS": "some-unrelated-container"})
    assert res.stdout, res.stderr
    return json.loads(res.stdout)


def test_an_operator_terminated_run_is_still_never_resurrected(ctl: Ctl):
    _start(ctl, "tst-opterm")
    _crashed(ctl, "tst-opterm")
    _mark(ctl, "tst-opterm", **{"class": TERMINATION_CLASS_OPERATOR,
                                "reason": "aborted by operator",
                                "source": "cli"})
    doc = _doctor_fix(ctl)
    assert doc["autoResume"]["resumed"] == []
    assert [t["runId"] for t in doc["autoResume"]["operatorTerminated"]] == [
        "tst-opterm"]


def test_a_pre_v0_7_marker_with_no_class_is_still_never_resurrected(ctl: Ctl):
    _start(ctl, "tst-oldterm")
    _crashed(ctl, "tst-oldterm")
    _mark(ctl, "tst-oldterm", reason="aborted by operator", source="cli")
    doc = _doctor_fix(ctl)
    assert doc["autoResume"]["resumed"] == []
    assert [t["runId"] for t in doc["autoResume"]["operatorTerminated"]] == [
        "tst-oldterm"]


def test_a_self_inflicted_run_is_eligible_for_auto_resume(ctl: Ctl):
    """The defect this task fixes: identical run dir, self-inflicted class ->
    resumed, and NOT bucketed as operator-terminated."""
    _start(ctl, "tst-selfterm")
    _crashed(ctl, "tst-selfterm")
    _mark(ctl, "tst-selfterm", **{"class": TERMINATION_CLASS_SELF})
    doc = _doctor_fix(ctl)
    assert doc["autoResume"]["operatorTerminated"] == []
    assert doc["autoResume"]["resumed"] == ["tst-selfterm"]
    assert doc["autoResume"]["skipped"] == []
    runs = [a for a in ctl.recorded() if a[:2] == ["run", "-d"]]
    assert len(runs) == 2, "the self-inflicted run must actually be restarted"


# --------------------------------------------------------------------------
# the doc claims
# --------------------------------------------------------------------------

def _doc(name: str) -> str:
    return (REPO_ROOT / name).read_text()


def test_api_md_documents_the_termination_field_and_both_classes():
    api = _doc("docs/api.md")
    section = api[api.index("#### `termination`"):]
    section = section[:section.index("### `GET /tasks`")]
    for needle in (f'"{TERMINATION_CLASS_SELF}"', f'"{TERMINATION_CLASS_OPERATOR}"',
                   "`evidence`", "read back out of",
                   "`iterations/NNNN/output.jsonl`", "auto-resume",
                   "operator-termination.json"):
        assert needle in section, needle
    assert "| `termination` |" in api


def test_cli_md_documents_the_status_line_and_the_resume_eligibility():
    cli = _doc("docs/cli.md")
    assert "termination: self-inflicted (signal 15)" in cli
    assert "last tool call before the signal" in cli
    # the doctor section says the class decides the refusal, not the file
    assert 'class: "self-inflicted"' in cli
    assert 'class: "operator"' in cli


def test_spec_documents_the_status_field_and_the_class_vocabulary():
    spec = _doc("SPEC.md")
    assert "| `termination` |" in spec
    assert "self-inflicted" in spec
    assert "TERMINATION_CLASS_SELF" in spec
    # ... and that the auto-resume refusal asks the CLASS, not whether the
    # marker file happens to exist
    assert "is_operator_termination()" in spec


@pytest.mark.parametrize("name", ["docs/api.md", "docs/cli.md", "SPEC.md"])
def test_no_doc_claims_a_self_inflicted_kill_is_an_operator_abort(name: str):
    text = _doc(name)
    for line in text.splitlines():
        if "self-inflicted" in line and "operator" in line:
            assert ("not" in line or "no " in line or "versus" in line
                    or "instead" in line or "distinguish" in line
                    or "|" in line or "different" in line), line
