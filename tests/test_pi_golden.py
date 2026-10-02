"""pi is unchanged: one iteration's argv, env, transcript and result, pinned.

PRD v0.30 non-negotiable 1 and requirement B: `PiRunner` is the first
implementation of `runner.AgentRunner`, and for a pi hat its argv, its child
env, its `output.jsonl` and its `IterationResult` are byte-identical to what
they were before the runner protocol existed. This module holds them to a
golden file, `tests/data/golden/pi-iteration/`:

- `stream.jsonl`  the NDJSON a fake pi prints: `message_start`, thinking and
                  text deltas, a `toolCall` with `tool_execution_start`/`end`,
                  a priced and an unpriced `message_end`, one merged non-JSON
                  stderr line, and a known fake secret in a tool result and in
                  the final text;
- `argv.json`     the argv `PiRunner.run` builds for a direct call with a
                  model, a thinking level and two extensions;
- `argv-loop.json` the argv for one `worker` iteration dispatched by the loop
                  (model and thinking from the hat bundle, no `-e`);
- `output.jsonl`  the transcript the runner writes: every line scrubbed;
- `result.json`   `asdict(IterationResult)` minus `duration_s`.

The fake pi dumps its argv, its stdin and its exec-time environment
(`/proc/self/environ`, so the interpreter's own locale coercion cannot add a
variable the runner never set) to `$GOLDEN_DUMP`, then prints the stream.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from ralphd.engine import redact
from ralphd.engine.config import CONTROL_PLANE_ENV_VARS
from ralphd.engine.pricing import PricingMap
from ralphd.engine.runner import IterationResult, PiRunner
from tests.conftest import make_supervisor

GOLDEN = Path(__file__).resolve().parent / "data" / "golden" / "pi-iteration"

# The fake secret `stream.jsonl` carries. Obviously not a credential; it is
# made live in the engine's redaction map the way production does it (a
# secret-shaped env var name plus `refresh_redaction_map`).
SECRET_NAME = "GOLDEN_FAKE_API_TOKEN"
SECRET_VALUE = "golden-FAKE-SECRET-not-a-real-token-0001"

PROMPT = "golden prompt: do the one task\n"
MODEL = "anthropic/claude-x"
EXTENSIONS = ["/opt/ralphd/ext/a.ts", "/opt/ralphd/ext/b.ts"]
PRICING = PricingMap.from_config(
    {"models": {MODEL: {"input": 3.0, "output": 15.0,
                        "cacheRead": 0.3, "cacheWrite": 3.75}}})

_FAKE_PI = """\
#!{python}
import json, os, sys
prompt = sys.stdin.read()
with open("/proc/self/environ", "rb") as f:
    raw = f.read()
env = dict(kv.split("=", 1) for kv in raw.decode().split("\\0") if "=" in kv)
with open(os.environ["GOLDEN_DUMP"], "w") as f:
    json.dump({{"argv": sys.argv[1:], "stdin": prompt, "env": env}}, f)
with open({stream!r}, "rb") as f:
    sys.stdout.buffer.write(f.read())
"""


@pytest.fixture
def fake_pi(tmp_path):
    p = tmp_path / "fake-pi"
    p.write_text(_FAKE_PI.format(python=sys.executable,
                                 stream=str(GOLDEN / "stream.jsonl")))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture
def dump_path(tmp_path, monkeypatch):
    path = tmp_path / "golden-dump.json"
    monkeypatch.setenv("GOLDEN_DUMP", str(path))
    return path


@pytest.fixture
def live_fake_secret(monkeypatch, tmp_path):
    """Make SECRET_VALUE live in the engine's in-memory redaction map, with a
    scratch `home` so no real creds file is read, and restore it after."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    redact.refresh_redaction_map(home=fake_home)
    assert redact.live_redaction_map().get(SECRET_VALUE) == f"env:{SECRET_NAME}"
    try:
        yield
    finally:
        monkeypatch.delenv(SECRET_NAME, raising=False)
        redact.refresh_redaction_map(home=fake_home)


def _golden_json(name: str):
    return json.loads((GOLDEN / name).read_text())


def _result_dict(result: IterationResult) -> dict:
    out = dataclasses.asdict(result)
    out.pop("duration_s")
    return out


def _assert_result_matches_golden(result: IterationResult) -> None:
    """Every golden field is equal, and any field the golden file predates
    holds its declared default: a field added for another harness never
    changes a pi result."""
    got = _result_dict(result)
    golden = _golden_json("result.json")
    assert {k: got[k] for k in golden} == golden
    defaults = {f.name: (f.default_factory() if f.default_factory
                         is not dataclasses.MISSING else f.default)
                for f in dataclasses.fields(IterationResult)}
    for key in got.keys() - golden.keys() - {"duration_s"}:
        assert got[key] == defaults[key], (key, got[key])


def test_a_direct_pi_run_matches_the_golden_argv_env_transcript_and_result(
        fake_pi, dump_path, live_fake_secret, tmp_path, monkeypatch):
    # Control-plane coordinates in BOTH the base environ and extra_env: the
    # child env is (os.environ | extra_env) minus CONTROL_PLANE_ENV_VARS.
    monkeypatch.setenv("RALPHD_PORT", "38123")
    monkeypatch.setenv("RALPHD_API_TOKEN", "golden-fake-control-token")
    extra_env = {"GOLDEN_EXTRA": "from-extra-env",
                 "RALPHD_CONTROL_TOKEN": "golden-fake-control-token-2"}
    ws = tmp_path / "ws"
    ws.mkdir()
    transcript = tmp_path / "it" / "output.jsonl"
    transcript.parent.mkdir()
    expected_env = {**os.environ, **extra_env}
    for name in CONTROL_PLANE_ENV_VARS:
        expected_env.pop(name, None)

    runner = PiRunner(ws, pi_bin=str(fake_pi), pricing=PRICING)
    result = asyncio.run(runner.run(
        PROMPT, transcript, model=MODEL, thinking="high", timeout_s=30,
        extra_env=extra_env, startup_timeout_s=30, extensions=EXTENSIONS))

    dump = json.loads(dump_path.read_text())
    assert dump["argv"] == _golden_json("argv.json")
    assert dump["stdin"] == PROMPT
    assert dump["env"] == expected_env
    assert transcript.read_bytes() == (GOLDEN / "output.jsonl").read_bytes()
    assert SECRET_VALUE not in transcript.read_text()
    _assert_result_matches_golden(result)
    assert result.saw_complete
    assert runner.running is False


def test_a_loop_dispatched_worker_iteration_matches_the_golden_argv_and_transcript(
        fake_pi, dump_path, live_fake_secret, tmp_path):
    sup = make_supervisor(tmp_path, prd=True, model=MODEL, thinking="high")
    sup.runner = PiRunner(sup.workspace, pi_bin=str(fake_pi))
    asyncio.run(sup._run_iteration_once("worker"))

    dump = json.loads(dump_path.read_text())
    assert dump["argv"] == _golden_json("argv-loop.json")
    written = sup.run.iteration_dir(1) / "output.jsonl"
    assert written.read_bytes() == (GOLDEN / "output.jsonl").read_bytes()


def test_pi_runner_is_the_first_agent_runner_and_ignores_hat_tools(
        fake_pi, dump_path, live_fake_secret, tmp_path):
    """`PiRunner` satisfies `AgentRunner`, and the protocol's `hat_tools`
    slot changes nothing about a pi iteration: pi gets its tools as
    `extensions`, so the argv, transcript and result stay golden."""
    from ralphd.engine.runner import AgentRunner

    runner = PiRunner(tmp_path, pi_bin=str(fake_pi), pricing=PRICING)
    assert isinstance(runner, AgentRunner)
    transcript = tmp_path / "output.jsonl"
    result = asyncio.run(runner.run(
        PROMPT, transcript, model=MODEL, thinking="high", timeout_s=30,
        extensions=EXTENSIONS, hat_tools=["task-insert"]))
    assert json.loads(dump_path.read_text())["argv"] == _golden_json("argv.json")
    assert transcript.read_bytes() == (GOLDEN / "output.jsonl").read_bytes()
    _assert_result_matches_golden(result)
    assert result.harness == "pi"


def test_the_cost_step_is_a_hook_on_the_shared_path(
        fake_pi, dump_path, live_fake_secret, tmp_path):
    """A runner on the shared child-process path replaces only the cost
    step by overriding `_account_cost`; every other field it scans is
    unchanged. The hook sees each assistant `message_end`'s usage once."""
    seen: list[dict] = []

    class _CostProbe(PiRunner):
        def _account_cost(self, usage, result, pricing=None, model=None):
            seen.append(dict(usage))

    result = asyncio.run(_CostProbe(tmp_path, pi_bin=str(fake_pi)).run(
        PROMPT, tmp_path / "output.jsonl", model=MODEL, timeout_s=30))
    assert [u["totalTokens"] for u in seen] == [1620, 1560]
    golden = _golden_json("result.json")
    assert result.final_text == golden["final_text"]
    assert {k: result.usage[k] for k in ("input", "output", "totalTokens")} == \
        {k: golden["usage"][k] for k in ("input", "output", "totalTokens")}
    assert not any(k.startswith("cost") for k in result.usage)
