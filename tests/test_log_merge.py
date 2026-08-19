"""Task 038 (#6): the iteration-transcript merge lives in ONE place.

`GET /logs`'s snapshot and the host-side on-disk reader must render the same
transcript for the same run dir -- otherwise a dead run's log (task 039/040)
would be a second, subtly different implementation. These tests build a
fixture run dir with the awkward shapes (a still-open iteration, a meta.json
that is not valid JSON, an iteration dir with no meta at all, a final line
with no trailing newline) and assert the *live API* path and
`ralphd.log_merge.merged_lines` produce byte-identical lines, untailed and
tailed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from ralphd.log_merge import merged_lines


def _iteration(run_root, n: int, *, phase: str, lines: list[str],
               ended: bool = True, trailing_newline: bool = True) -> None:
    d = run_root / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    meta = {"number": n, "phase": phase, "model": "stub-model",
            "approach": 1, "startedAt": f"2025-01-01T00:0{n}:00Z"}
    if ended:
        meta |= {"exitCode": 0, "error": None, "endedAt": f"2025-01-01T00:0{n}:30Z",
                 "usage": {"totalTokens": 42 * n}}
    (d / "meta.json").write_text(json.dumps(meta))
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    (d / "output.jsonl").write_text(text)


def _fixture_run(tmp_path):
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
    _iteration(run.root, 1, phase="planning",
               lines=['{"type":"message_start"}', '{"type":"text","text":"plan"}'])
    _iteration(run.root, 2, phase="worker",
               lines=['{"type":"text","text":"work"}', 'not json at all'])
    # still running: no endedAt -> no end boundary, and the last line was
    # flushed without its newline yet.
    _iteration(run.root, 3, phase="worker", ended=False, trailing_newline=False,
               lines=['{"type":"text","text":"half a line"}'])
    # unreadable meta.json and a dir with no meta at all: both skipped.
    broken = run.root / "iterations" / "0004"
    broken.mkdir()
    (broken / "meta.json").write_text("{not json")
    (broken / "output.jsonl").write_text('{"type":"text","text":"ignored"}\n')
    (run.root / "iterations" / "0005").mkdir()
    return run


def _client(run: RunDir, tmp_path) -> httpx.AsyncClient:
    cfg = JobConfig(run_id="logmerge")
    sup = LoopSupervisor(cfg, run, tmp_path / "workspace")
    app = create_app(cfg, run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


async def _api_lines(run: RunDir, tmp_path, tail: int = 0) -> list[str]:
    async with _client(run, tmp_path) as client:
        res = await client.get("/logs", params={"tail": tail})
    assert res.status_code == 200, res.text
    return res.text.splitlines(keepends=True)


# --------------------------------------------------------------------------
@pytest.mark.parametrize("tail", [0, 1, 3, 100])
async def test_api_and_on_disk_merge_are_identical(tmp_path, tail):
    (tmp_path / "workspace").mkdir()
    run = _fixture_run(tmp_path / "run")
    api = await _api_lines(run, tmp_path, tail)
    assert api == merged_lines(run.root, tail=tail)
    assert api  # the fixture is not accidentally empty


def test_merge_shape(tmp_path):
    """Pins what the shared merge actually renders (both callers get this)."""
    run = _fixture_run(tmp_path / "run")
    lines = merged_lines(run.root)
    parsed = [json.loads(x) if x.startswith("{") else x for x in lines]
    boundaries = [(p["number"], p["event"]) for p in parsed
                  if isinstance(p, dict) and p.get("type") == "ralphd.iteration"]
    assert boundaries == [(1, "start"), (1, "end"),
                          (2, "start"), (2, "end"),
                          (3, "start")]  # 3 is still open; 4/5 skipped
    # every line is newline-terminated, including the un-flushed last one
    assert all(x.endswith("\n") for x in lines)
    assert 'not json at all\n' in lines           # verbatim, not dropped
    assert not any("ignored" in x for x in lines)  # unreadable meta -> skipped
    # end boundaries carry the outcome fields the renderer needs
    end1 = next(p for p in parsed if isinstance(p, dict)
                and p.get("event") == "end" and p.get("number") == 1)
    assert end1["usage"] == {"totalTokens": 42} and end1["exitCode"] == 0


def test_no_duplicate_merge_implementation():
    """The boundary synthesis exists once: only log_merge.py may *build* a
    `ralphd.iteration` boundary out of a meta.json (cli/log_render.py is a
    consumer of the rendered line, which is fine)."""
    import subprocess
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "ralphd"
    out = subprocess.run(["grep", "-rln", "--include=*.py",
                          'meta.get("startedAt")', str(src)],
                         capture_output=True, text=True).stdout
    assert out.split() == [str(src / "log_merge.py")], out
    # nobody else concatenates output.jsonl into a merged transcript
    readers = subprocess.run(["grep", "-rln", "--include=*.py",
                              "output.jsonl", str(src)],
                             capture_output=True, text=True).stdout.split()
    assert set(readers) <= {str(src / "log_merge.py"),
                            str(src / "engine" / "api.py"),     # single-iteration raw route
                            str(src / "engine" / "loop.py"),    # writes it
                            str(src / "engine" / "redact.py")}, readers
