"""PRD v0.30 C: a `claude-code` iteration's `output.jsonl` is pi-shaped, and
`output.claude.jsonl` (the raw SDK stream) is read by nothing.

A stub `full` scenario (thinking, two tool calls, final text) runs through
`ClaudeCodeRunner`. Every consumer of a transcript (`ralphctl logs`'s pretty
and raw paths, `log_merge`, `collapse_index`, `state.count_tool_calls`) must
give the same answer for it as for the checked-in pi-shaped fixture
`tests/data/golden/claude-parity/pi-equivalent.jsonl`; the one difference
between the two is the tool-call ids, which the stub draws at random and
which `_canon` renames to `toolu_1`, `toolu_2`.

No network, no real token: the OAuth value is an obvious fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from ralphd import collapse_index, log_merge
from ralphd.cli import main as cli_main
from ralphd.engine import redact, state
from ralphd.engine.runner import ClaudeCodeRunner, IterationResult

REPO = Path(__file__).resolve().parents[1]
STUB = REPO / "tests" / "stub-claude" / "claude"
GOLDEN = REPO / "tests" / "data" / "golden" / "claude-parity" / "pi-equivalent.jsonl"
FAKE_OAUTH = "sk-ant-oat01-FAKE-FOR-TESTS-ONLY"
MODEL_REF = "anthropic/claude-sonnet-5-5"
SECRET = "STUB-SECRET-VALUE-0123456789"
META = {"number": 1, "phase": "worker", "model": MODEL_REF, "approach": None,
        "startedAt": "2026-01-01T00:00:00Z", "endedAt": "2026-01-01T00:00:01Z",
        "exitCode": 0, "error": None, "usage": None}
# What the stub's `full` scenario bills: four assistant messages of
# input 2, output 5, cacheRead 100, cacheWrite 10 each.
STUB_USAGE = {"input": 8, "output": 20, "cacheRead": 400, "cacheWrite": 40,
              "totalTokens": 468}


def _stub_env(mp, **stub) -> None:
    mp.setenv("RALPHD_CLAUDE_CLI_PATH", str(STUB))
    mp.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_OAUTH)
    for k in [k for k in os.environ if k.startswith("STUB_CLAUDE_")]:
        mp.delenv(k)
    for k, v in stub.items():
        mp.setenv(k, v)


def _run(tmp_path: Path, name: str = "run") -> tuple[IterationResult, Path]:
    """One iteration through the real wrapper; returns the result and the
    run root (`<root>/iterations/0001/`, with a `meta.json`)."""
    root = tmp_path / name
    itdir = root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    ws = tmp_path / f"{name}-ws"
    ws.mkdir()
    result = asyncio.run(ClaudeCodeRunner(ws).run(
        "the prompt", itdir / "output.jsonl", model=MODEL_REF, timeout_s=60))
    (itdir / "meta.json").write_text(json.dumps(META))
    return result, root


def _canon(text: str) -> str:
    """Rename the stub's random tool-call ids to `toolu_N`, in order."""
    ids: dict[str, str] = {}

    def sub(m):
        return ids.setdefault(m.group(0), f"toolu_{len(ids) + 1}")
    return re.sub(r"toolu_[0-9a-f]{20}", sub, text)


def _pi_root(tmp_path: Path) -> Path:
    root = tmp_path / "pi"
    itdir = root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    shutil.copy(GOLDEN, itdir / "output.jsonl")
    (itdir / "meta.json").write_text(json.dumps(META))
    return root


def _collapse_shape(output: Path) -> list[tuple]:
    """The collapse index's segments with byte offsets reduced to seen/unseen
    (offsets move with the id length, the structure does not)."""
    index = collapse_index.compute_collapse_index(output)
    return [(seg["textSeenAtByte"] is not None, seg["thinkingSeenAtByte"] is not None,
             seg["toolcallSeenAtByte"] is not None) for seg in index["segments"]]


def _surfaces(root: Path) -> dict:
    """Everything a consumer derives from a run's transcript."""
    raw = "".join(log_merge.merged_lines(root))
    return {
        "merged": _canon(raw),
        "pretty": _canon("\n".join(cli_main._pretty_log_lines(raw, False, 0))),
        "iteration": _canon("".join(log_merge.iteration_lines(root, 1))),
        "collapse": _collapse_shape(root / "iterations" / "0001" / "output.jsonl"),
        "tool_calls": state.count_tool_calls(root, 1),
    }


def _usage_sum(path: Path) -> dict:
    total = dict.fromkeys(STUB_USAGE, 0)
    for line in path.read_text().splitlines():
        ev = json.loads(line)
        if ev.get("type") == "message_end":
            for k in total:
                total[k] += ev["message"].get("usage", {}).get(k, 0)
    return total


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("parity")
    with pytest.MonkeyPatch.context() as mp:
        _stub_env(mp, STUB_CLAUDE_SCENARIO="full")
        result, root = _run(tmp)
    return {"tmp": tmp, "result": result, "root": root,
            "out": root / "iterations" / "0001" / "output.jsonl"}


def test_output_jsonl_is_the_pi_equivalent_fixture(full):
    assert full["result"].exit_code == 0
    assert _canon(full["out"].read_text()) == GOLDEN.read_text()


def test_every_consumer_matches_the_pi_fixture(full):
    claude = _surfaces(full["root"])
    pi = _surfaces(_pi_root(full["tmp"]))
    assert claude == pi
    assert claude["tool_calls"] == 2
    assert "bash" in claude["pretty"] and "read" in claude["pretty"]


def test_usage_sums_are_the_stubs_numbers(full):
    assert _usage_sum(full["out"]) == STUB_USAGE
    assert _usage_sum(GOLDEN) == STUB_USAGE
    usage = full["result"].usage
    assert {k: usage[k] for k in STUB_USAGE} == STUB_USAGE


def test_the_raw_file_holds_sdk_messages_and_no_consumer_reads_it(full, tmp_path):
    raw_path = full["out"].with_name("output.claude.jsonl")
    kinds = [json.loads(line)["kind"] for line in raw_path.read_text().splitlines()]
    assert kinds[0] == "SystemMessage"
    assert kinds[-1] == "ResultMessage"
    assert kinds.count("AssistantMessage") == 4
    assert kinds.count("UserMessage") == 2
    # A copy of the run with the raw file deleted reads identically.
    twin = tmp_path / "twin"
    shutil.copytree(full["root"], twin)
    (twin / "iterations" / "0001" / "output.claude.jsonl").unlink()
    assert _surfaces(twin) == _surfaces(full["root"])
    # And the iteration record's size/existence views never count it.
    detail = state.iteration_detail(full["root"], 1)
    assert detail["transcriptBytes"] == full["out"].stat().st_size


def test_no_reader_globs_iteration_dirs_for_jsonl():
    """`output.claude.jsonl` sits beside `output.jsonl`, so a reader that
    globbed `*.jsonl` over an iteration dir would take it for a transcript."""
    pat = re.compile(r"""glob\(\s*["'][^"']*\*[^"']*\.jsonl["']""")
    offenders = [str(p.relative_to(REPO)) for p in (REPO / "src" / "ralphd").rglob("*.py")
                 if pat.search(p.read_text())]
    assert offenders == []


def test_a_tool_secret_is_scrubbed_from_both_files(tmp_path, monkeypatch):
    monkeypatch.setitem(redact._map, SECRET, "env:STUB_SECRET")
    with pytest.MonkeyPatch.context() as mp:
        _stub_env(mp, STUB_CLAUDE_SCENARIO="full",
                  STUB_CLAUDE_TOOL_OUTPUT=f"token={SECRET} end")
        _result, root = _run(tmp_path, "secret")
    itdir = root / "iterations" / "0001"
    for name in ("output.jsonl", "output.claude.jsonl"):
        text = (itdir / name).read_text()
        assert SECRET not in text, name
        assert "[REDACTED:env:STUB_SECRET]" in text, name


def test_an_error_result_is_an_error_message_end(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        _stub_env(mp, STUB_CLAUDE_SCENARIO="error")
        result, root = _run(tmp_path)
    events = [json.loads(line) for line in
              (root / "iterations" / "0001" / "output.jsonl").read_text().splitlines()]
    last = events[-1]
    assert last["type"] == "message_end"
    assert last["message"]["stopReason"] == "error"
    assert "500" in last["message"]["errorMessage"]
    assert "server_error" in last["message"]["errorMessage"]
    assert result.http_status == 500
    assert result.error_type == "server_error"


def test_a_rate_limited_result_carries_structured_evidence(tmp_path):
    with pytest.MonkeyPatch.context() as mp:
        _stub_env(mp, STUB_CLAUDE_SCENARIO="rate_limited")
        result, _root = _run(tmp_path)
    assert result.rate_limit["status"] == "rejected"
    assert result.http_status == 429
    assert result.error_type == "rate_limit"
    assert result.rate_limit_fresh is True
