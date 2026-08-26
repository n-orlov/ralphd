"""Task 007 (PRD requirement C): thinking is part of the per-hat bundle,
not one global level.

Before this task `cfg.thinking` (`config.py`) was a single string passed
unconditionally into `runner.PiRunner.run` for every hat's invocation
(`loop.py`'s dispatch). That forced whatever vocabulary the operator wrote
there onto every hat regardless of the model family actually resolved for
it -- an Anthropic-shaped level (`off`/`minimal`/`low`/.../`max`) had no way
to become the OpenAI family's own differently-spelled `reasoning_effort`
knob for a hat configured onto an OpenAI-family ref (PRD: "the three AIGW
OpenAI models advertise `reasoning: true` and their effort knob is spelled
differently ... a bundle must not force an Anthropic thinking level onto an
OpenAI ref").

What lands here:

* `config.HatBundle` -- the (model, thinking) bundle itself, provider-shaped:
  at most one of `.thinking` (native pi vocabulary) / `.effort` (OpenAI
  vocabulary) is ever populated;
* `config.JobConfig.bundle_for(phase)` -- the resolver: the old global
  `thinking` field becomes every hat's DEFAULT bundle setting (back-compat,
  requirement C's third bullet) unless `thinking_overrides[phase]` names
  something for that hat, provider-shaped either explicitly (a `{"effort":
  ...}`/`{"thinking": ...}` mapping) or implicitly (a plain string, placed
  by the resolved model ref's own family);
* `loop.py`'s dispatch (`_run_iteration_once`) now reads `bundle_for(phase)`
  and hands `bundle.thinking`/`bundle.effort` to `runner.PiRunner.run`
  instead of the single global `cfg.thinking`;
* `runner.PiRunner.run` composes BOTH `thinking=`/`effort=` kwargs onto
  pi's own single `--thinking <level>` flag (pi's CLI never grew a second
  flag for the OpenAI vocabulary -- it translates internally either way),
  using whichever one the bundle actually set, so an `effort`-only bundle's
  own value reaches pi rather than being silently dropped or replaced by a
  stray Anthropic-shaped default.

Tiers: unit on `HatBundle`/`bundle_for` (no engine at all), a black-box-ish
flag-composition check on `PiRunner.run` against a tiny real subprocess
that dumps its own argv (mirrors `tests/test_iteration_env_strip.py`'s
`env_dumper` technique -- no `tests/stub-pi` needed for this), and an
engine-level wiring proof through `LoopSupervisor.run_iteration` with the
`sup.runner` seam faked (never `_run_iteration_once` itself stubbed/
replaced -- it runs for real and is what is under test).
"""

from __future__ import annotations

import asyncio
import json
import stat

import pytest

from ralphd.engine.config import HatBundle, JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult, PiRunner
from ralphd.engine.state import RunDir

# A ref from each family, matching the PRD's own worked example
# (amazon-bedrock/eu.anthropic.* and aigw-openai/openai.* -- see
# tests/test_model_id_recorded.py's PROVIDER/RAW_ID for the same shapes).
ANTHROPIC_REF = "amazon-bedrock/eu.anthropic.claude-sonnet-5"
OPENAI_REF = "aigw-openai/openai.gpt-5.6-terra"


# ---------------------------------------------------------------------------
# unit tier: HatBundle / JobConfig.bundle_for
# ---------------------------------------------------------------------------

def test_global_thinking_becomes_every_hats_default_bundle_setting():
    """Requirement C's third bullet: a config that sets only the old global
    `thinking` keeps working, landing as every hat's own bundle setting --
    never guessed into `.effort`, since the global field has always spoken
    pi's native vocabulary."""
    cfg = JobConfig(run_id="t007", thinking="high")
    for hat in ("planning", "worker", "verify", "review", "reflect", "router"):
        bundle = cfg.bundle_for(hat)
        assert bundle.thinking == "high", hat
        assert bundle.effort is None, hat


def test_a_hat_with_no_config_at_all_gets_an_empty_bundle():
    cfg = JobConfig(run_id="t007")
    bundle = cfg.bundle_for("worker")
    assert bundle == HatBundle(model=None, thinking=None, effort=None)


def test_explicit_effort_override_is_not_forced_to_an_anthropic_level():
    """An OpenAI-family hat's explicit `{"effort": ...}` override wins over
    the global (Anthropic-shaped) `thinking` default -- the global value
    never lands on this hat's bundle at all."""
    cfg = JobConfig(
        run_id="t007", thinking="high",
        model_overrides={"verify": OPENAI_REF},
        thinking_overrides={"verify": {"effort": "medium"}})
    bundle = cfg.bundle_for("verify")
    assert bundle.model == OPENAI_REF
    assert bundle.effort == "medium"
    assert bundle.thinking is None, (
        "the global Anthropic-shaped 'high' must not be forced onto an "
        "explicitly-overridden OpenAI-family hat")
    # An un-overridden hat on the default (native) family still gets the
    # global default, undisturbed by the other hat's override.
    other = cfg.bundle_for("worker")
    assert other.thinking == "high" and other.effort is None


def test_explicit_thinking_override_mapping_is_honoured_verbatim():
    cfg = JobConfig(
        run_id="t007", thinking="high",
        thinking_overrides={"planning": {"thinking": "xhigh"}})
    bundle = cfg.bundle_for("planning")
    assert bundle.thinking == "xhigh" and bundle.effort is None


@pytest.mark.parametrize("model_ref,expect_field", [
    (ANTHROPIC_REF, "thinking"),
    (OPENAI_REF, "effort"),
    (None, "thinking"),          # no ref at all: native vocabulary
])
def test_a_bare_string_override_is_auto_placed_by_family(model_ref, expect_field):
    """A plain-string `thinking_overrides` entry (no explicit
    `{"effort"|"thinking": ...}` mapping) is placed onto the bundle field
    matching the resolved ref's own family -- an operator overriding a
    hat's setting need not know that hat's provider vocabulary."""
    kw = {"thinking_overrides": {"review": "low"}}
    if model_ref is not None:
        kw["model_overrides"] = {"review": model_ref}
    cfg = JobConfig(run_id="t007", **kw)
    bundle = cfg.bundle_for("review")
    assert getattr(bundle, expect_field) == "low"
    other_field = "effort" if expect_field == "thinking" else "thinking"
    assert getattr(bundle, other_field) is None


# ---------------------------------------------------------------------------
# flag-composition tier: PiRunner.run's actual argv, for both families
# ---------------------------------------------------------------------------

_ARGV_DUMPER = """\
#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
with open(os.environ["ARGV_DUMP_PATH"], "w") as f:
    json.dump(sys.argv[1:], f)
print(json.dumps({"type": "message_end",
                  "message": {"role": "assistant", "content": []},
                  "usage": {}}))
"""


@pytest.fixture
def argv_dumper(tmp_path):
    p = tmp_path / "fake-pi"
    p.write_text(_ARGV_DUMPER)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _run_and_dump_argv(argv_dumper, tmp_path, monkeypatch, **run_kwargs):
    dump_path = tmp_path / "argv-dump.json"
    monkeypatch.setenv("ARGV_DUMP_PATH", str(dump_path))
    runner = PiRunner(tmp_path, pi_bin=str(argv_dumper))
    asyncio.run(runner.run("hi", tmp_path / "t.jsonl", timeout_s=10,
                           **run_kwargs))
    return json.loads(dump_path.read_text())


def test_anthropic_family_bundle_composes_the_thinking_flag(
        argv_dumper, tmp_path, monkeypatch):
    argv = _run_and_dump_argv(argv_dumper, tmp_path, monkeypatch,
                              model=ANTHROPIC_REF, thinking="high",
                              effort=None)
    assert "--thinking" in argv
    assert argv[argv.index("--thinking") + 1] == "high"


def test_openai_family_bundle_composes_its_own_effort_value_not_anthropics(
        argv_dumper, tmp_path, monkeypatch):
    """The composed flag carries the OpenAI-family bundle's OWN `effort`
    value ("medium") -- never a leftover Anthropic-shaped level (e.g. the
    "high" the other family/test above used), proving the two are
    independently composed rather than one leaking into the other."""
    argv = _run_and_dump_argv(argv_dumper, tmp_path, monkeypatch,
                              model=OPENAI_REF, thinking=None,
                              effort="medium")
    assert "--thinking" in argv
    assert argv[argv.index("--thinking") + 1] == "medium"
    assert "high" not in argv


def test_neither_setting_composes_no_thinking_flag_at_all(
        argv_dumper, tmp_path, monkeypatch):
    argv = _run_and_dump_argv(argv_dumper, tmp_path, monkeypatch,
                              model=ANTHROPIC_REF, thinking=None, effort=None)
    assert "--thinking" not in argv


# ---------------------------------------------------------------------------
# engine wiring tier: loop.py's dispatch actually threads the bundle through
# ---------------------------------------------------------------------------

class _RecordingAgent:
    """Stands in for PiRunner via the `sup.runner` seam (never a stub of
    `_run_iteration_once` itself, which runs for real here and is exactly
    what is under test)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls.append(kw)
        self.running = True
        try:
            r = IterationResult(exit_code=0)
            r.final_text = "ok"
            r.duration_s = 0.01
            r.usage = {}
            return r
        finally:
            self.running = False


def _supervisor(root, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    kw = {"iterations": 5, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="t007-wiring", **kw), run, root)
    sup.runner = _RecordingAgent()   # type: ignore[assignment]
    return sup


def test_dispatch_carries_the_bundle_not_the_global_thinking_string(tmp_path):
    """`loop.py`'s dispatch must hand `runner.PiRunner.run` the resolved
    bundle's own `thinking`/`effort`, per phase -- not a single value
    shared by every hat. Drives `LoopSupervisor.run_iteration` for real,
    for two different phases resolving to two different families."""
    sup = _supervisor(
        tmp_path / "run", thinking="high",
        model_overrides={"verify": OPENAI_REF},
        thinking_overrides={"verify": {"effort": "medium"}})

    asyncio.run(sup.run_iteration("worker"))
    asyncio.run(sup.run_iteration("verify", prompt_name="task-verify"))

    agent = sup.runner
    assert len(agent.calls) == 2
    worker_call, verify_call = agent.calls

    # worker: no per-hat override -> the global default, unconverted.
    assert worker_call["thinking"] == "high"
    assert worker_call.get("effort") is None

    # verify: explicit OpenAI-family override -> its own effort value,
    # the global Anthropic-shaped level nowhere in sight.
    assert verify_call.get("thinking") is None
    assert verify_call["effort"] == "medium"
    assert verify_call["model"] == OPENAI_REF


def test_a_config_with_only_the_old_global_thinking_reaches_every_hat_unchanged(
        tmp_path):
    """Requirement C's back-compat bullet, proven at the dispatch level
    (not just the config unit level above): with nothing but the old
    global `thinking` set, every phase's call carries that exact value and
    no `effort`."""
    sup = _supervisor(tmp_path / "run", thinking="xhigh")

    asyncio.run(sup.run_iteration("planning"))
    asyncio.run(sup.run_iteration("worker"))
    asyncio.run(sup.run_iteration("review"))

    for call in sup.runner.calls:
        assert call["thinking"] == "xhigh"
        assert call.get("effort") is None
