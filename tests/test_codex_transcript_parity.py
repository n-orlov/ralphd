"""v0.33 C.6: a `codex` iteration's `output.jsonl` is pi-shaped, and the raw
Codex stream (`output.codex.jsonl`) is read by nothing.

The checked-in `tool_call`, `file_change` and `mcp_call` fixtures run through
`CodexNormaliser`; the pi events it returns are written as an `output.jsonl`.
Every consumer of a transcript (`log_render`, `log_merge`, `collapse_index`,
`render_index`, `state.count_tool_calls`) must accept it, and the claude-code
golden transcript is the reference for the event and message shape.

No network, no real token, no real `codex`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ralphd import collapse_index, log_merge, render_index
from ralphd.cli import log_render
from ralphd.cli import main as cli_main
from ralphd.engine import state
from ralphd.harness.codex_normalise import CODEX_RAW_ENVELOPE_TYPE, CodexNormaliser

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "codex"
GOLDEN = REPO / "tests" / "data" / "golden" / "claude-parity" / "pi-equivalent.jsonl"
MODEL_REF = "openai/gpt-6.1-sol"
META = {"number": 1, "phase": "worker", "model": MODEL_REF, "approach": None,
        "startedAt": "2026-01-01T00:00:00Z", "endedAt": "2026-01-01T00:00:01Z",
        "exitCode": 0, "error": None, "usage": None}


def _transcript(name: str, tmp_path: Path) -> tuple[Path, list[dict]]:
    """Normalise one fixture the way the wrapper does: pi events to
    `output.jsonl`, one raw envelope per Codex event to `output.codex.jsonl`."""
    norm = CodexNormaliser(model_ref=MODEL_REF)
    pi_events: list[dict] = []
    raw_lines: list[str] = []
    for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines():
        ev = json.loads(line)
        raw_lines.append(json.dumps({"type": CODEX_RAW_ENVELOPE_TYPE, "raw": ev}))
        pi_events.extend(norm.feed(ev))
    pi_events.extend(norm.finish(None))
    root = tmp_path / name
    itdir = root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    (itdir / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in pi_events))
    (itdir / "output.codex.jsonl").write_text("\n".join(raw_lines) + "\n")
    (itdir / "meta.json").write_text(json.dumps(META))
    return root, pi_events


@pytest.fixture(params=["tool_call", "file_change", "mcp_call", "text_only"])
def run(request, tmp_path):
    root, events = _transcript(request.param, tmp_path)
    return {"name": request.param, "root": root, "events": events,
            "out": root / "iterations" / "0001" / "output.jsonl"}


def _starts(events):
    return [e for e in events if e["type"] == "tool_execution_start"]


def test_event_and_message_shape_matches_the_pi_golden(run):
    golden = [json.loads(line) for line in GOLDEN.read_text().splitlines()]
    golden_types = {e["type"] for e in golden}
    golden_keys = {e["type"]: set(e) for e in golden}
    golden_msg = next(set(e["message"]) for e in golden if e["type"] == "message_end")
    for ev in run["events"]:
        assert ev["type"] in golden_types
        assert set(ev) >= golden_keys[ev["type"]] - {"ralphd"}, ev["type"]
        if ev["type"] == "message_end":
            assert set(ev["message"]) >= golden_msg
            assert ev["message"]["provider"] == "openai"


def test_log_render_accepts_the_transcript(run):
    text = log_render.render_to_text(run["out"].read_text(), False, log_render.new_render_state())
    assert text.strip()
    for start in _starts(run["events"]):
        assert start["toolName"] in text


def test_pretty_and_merged_logs_accept_the_transcript(run):
    merged = "".join(log_merge.merged_lines(run["root"]))
    # The merge adds only its own iteration markers around the transcript.
    lines = merged.splitlines()
    assert [l for l in lines if json.loads(l)["type"] != "ralphd.iteration"] == \
        run["out"].read_text().splitlines()
    own = "".join(log_merge.iteration_lines(run["root"], 1))
    assert own.splitlines() == run["out"].read_text().splitlines()
    pretty = "\n".join(cli_main._pretty_log_lines(merged, False, 0))
    assert pretty.strip()


def test_collapse_index_accepts_the_transcript(run):
    index = collapse_index.compute_collapse_index(run["out"])
    assert index["transcriptBytes"] == run["out"].stat().st_size
    assert index["segments"]
    assert any(seg["textSeenAtByte"] is not None or seg["toolcallSeenAtByte"] is not None
               for seg in index["segments"])


def test_render_index_accepts_the_transcript(run):
    index = render_index.compute_render_index(run["out"])
    assert index["transcriptBytes"] == run["out"].stat().st_size
    assert len(index["checkpoints"]) == len(run["out"].read_text().splitlines())
    assert index["checkpoints"][-1]["renderedLines"] > 0
    assert not index.get("poisoned")


def test_count_tool_calls_counts_the_tool_executions(run):
    assert state.count_tool_calls(run["root"], 1) == len(_starts(run["events"]))
    expected = {"tool_call": 1, "file_change": None, "mcp_call": None, "text_only": 0}[run["name"]]
    if expected is not None:
        assert state.count_tool_calls(run["root"], 1) == expected


def test_the_raw_file_is_read_by_no_consumer(run, tmp_path):
    root = run["root"]
    before = (log_merge.merged_lines(root), state.count_tool_calls(root, 1))
    (root / "iterations" / "0001" / "output.codex.jsonl").unlink()
    after = (log_merge.merged_lines(root), state.count_tool_calls(root, 1))
    assert list(before[0]) == list(after[0])
    assert before[1] == after[1]
    detail = state.iteration_detail(root, 1)
    assert detail["transcriptBytes"] == run["out"].stat().st_size


def test_output_jsonl_never_holds_a_raw_envelope(run):
    assert CODEX_RAW_ENVELOPE_TYPE not in run["out"].read_text()
    for line in run["out"].read_text().splitlines():
        assert json.loads(line)["type"] != CODEX_RAW_ENVELOPE_TYPE


def test_no_reader_globs_iteration_dirs_for_jsonl():
    """`output.codex.jsonl` sits beside `output.jsonl`, so a reader that
    globbed `*.jsonl` over an iteration dir would take it for a transcript."""
    pat = re.compile(r"""glob\(\s*["'][^"']*\*[^"']*\.jsonl["']""")
    offenders = [str(p.relative_to(REPO)) for p in (REPO / "src" / "ralphd").rglob("*.py")
                 if pat.search(p.read_text())]
    assert offenders == []
