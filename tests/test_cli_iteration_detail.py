"""Task 019 (#18.1): `ralphctl iteration <run> <n>` -- one iteration's story.

Everything an operator asks when iteration 47 looks wrong has always been on
disk in `iterations/0047/meta.json` (phase, model, start/end, the raw failure
signals, that iteration's tokens and cost) and no surface rendered it:
`ralphctl logs --iteration 47` served the transcript, the hub timeline showed a
summary row, and *why did this one end like that* had to be reconstructed from
`exitCode`/`timedOut`/`noTrafficTimeout`/`error` by hand.

What is pinned here:

  * the shared shaping (`engine.state.iteration_detail`) and the single
    wording of the verdict (`format_exit_reason`) -- task 020's hub dialog
    renders the same dict, so a second vocabulary cannot be born;
  * the on-disk contract: no container, no live API, no fallback notice --
    `meta.json` is written by the engine itself, atomically, so the run dir is
    authoritative whether the job is running or long dead;
  * unknown is not zero (#15/#10's rule applied again): an iteration whose
    `meta.json` is unreadable says so instead of rendering a row of `None`s,
    and an implausible zero cost quote (task 049) renders `unavailable`, never
    `$0.0000`;
  * a missing iteration exits 1 *cleanly*, naming the iterations that do
    exist, and an unknown run still exits 3;
  * the transcript is rendered by the SAME merge + renderer `ralphctl logs
    --iteration N` uses (asserted line-for-line, not by inspection).

Tiers: unit (the formatters and the shaping), black-box `ralphctl`
subprocesses over hand-written run dirs (container gone), and one REAL engine
whose iteration dirs it wrote itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ralphd.cli.main import _iteration_span
from ralphd.engine.state import (
    EXIT_REASON_CLEAN,
    EXIT_REASON_INTERRUPTED,
    EXIT_REASON_NO_TRAFFIC,
    EXIT_REASON_RUNNING,
    EXIT_REASON_TIMEOUT,
    EXIT_REASON_UNKNOWN,
    USAGE_NONE,
    format_exit_reason,
    format_tokens,
    iteration_detail,
    utc_from_epoch,
)
from ralphd.log_merge import NO_TRANSCRIPT, iteration_numbers
from tests.conftest import RALPHCTL

ZERO_QUOTE_USAGE = {"input": 32, "output": 18320, "cacheRead": 438945,
                    "cacheWrite": 48331, "totalTokens": 505628,
                    "costUSD": 0, "costPriced": True}


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _write_iteration(run_dir: Path, n: int, meta: dict | None,
                     texts: list[str] | None = None) -> Path:
    """An iteration dir; `meta=None` writes NO meta.json at all."""
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    if meta is not None:
        (d / "meta.json").write_text(json.dumps(meta))
    if texts is not None:
        (d / "output.jsonl").write_text("".join(
            json.dumps({"type": "message_end",
                        "message": {"content": [{"type": "text", "text": t}]}}) + "\n"
            for t in texts))
    return d


def _base_meta(n: int = 2, **over) -> dict:
    meta = {"number": n, "phase": "worker", "model": None, "approach": 2,
            "startedAt": "2026-09-02T10:00:00Z", "endedAt": "2026-09-02T10:17:51Z",
            "steeringConsumed": [], "exitCode": 0, "interrupted": False,
            "timedOut": False, "noTrafficTimeout": False, "sawComplete": False,
            "sawVerified": False, "error": None, "faultClass": None,
            "modelResolved": "amazon-bedrock/eu.anthropic.claude-opus-5",
            "modelRaw": "eu.anthropic.claude-opus-5",
            "usage": {"input": 18, "output": 2118, "cacheRead": 136849,
                      "cacheWrite": 41676, "totalTokens": 180661,
                      "costUSD": 0.4231, "costPriced": True}}
    meta.update(over)
    return meta


def _dead_run(tmp_path: Path, run_id: str = "itdetail") -> tuple[Path, Path]:
    """(registry, run_dir) for a finished run with no container at all: no
    host.json, so nothing can even try to reach a live API."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "failed", "iterationsUsed": 2}))
    return registry, run_dir


# ------------------------------------------------------------------ unit tier
@pytest.mark.parametrize("meta,expected", [
    (None, EXIT_REASON_UNKNOWN),
    ("not a dict", EXIT_REASON_UNKNOWN),
    ({}, EXIT_REASON_RUNNING),
    ({"startedAt": "x"}, EXIT_REASON_RUNNING),
    ({"endedAt": "x", "exitCode": 0}, EXIT_REASON_CLEAN),
    ({"endedAt": "x", "exitCode": 7}, "exit 7"),
    ({"endedAt": "x", "exitCode": None}, EXIT_REASON_UNKNOWN),
    # the signals overlap; the most specific one wins
    ({"endedAt": "x", "exitCode": 143, "interrupted": True,
      "error": "killed"}, EXIT_REASON_INTERRUPTED),
    ({"endedAt": "x", "exitCode": None, "noTrafficTimeout": True,
      "timedOut": True, "error": "no traffic"}, EXIT_REASON_NO_TRAFFIC),
    ({"endedAt": "x", "exitCode": None, "timedOut": True,
      "error": "timeout"}, EXIT_REASON_TIMEOUT),
    ({"endedAt": "x", "exitCode": 1, "error": "boom\n  and\ttrace"},
     "error (exit 1): boom and trace"),
    ({"endedAt": "x", "exitCode": None, "error": "engine iteration failure"},
     "error: engine iteration failure"),
])
def test_exit_reason_ranks_the_raw_signals(meta, expected):
    assert format_exit_reason(meta) == expected


def test_exit_reason_appends_the_engines_fault_verdict():
    """`faultClass` is why an attempt was retried and refunded -- it goes
    ALONGSIDE the signal it was derived from, never instead of it."""
    assert format_exit_reason({"endedAt": "x", "exitCode": None,
                               "noTrafficTimeout": True,
                               "faultClass": "infra"}) == \
        EXIT_REASON_NO_TRAFFIC + " [infra fault]"
    assert format_exit_reason({"endedAt": "x", "exitCode": 2,
                               "faultClass": "work"}) == "exit 2 [work fault]"
    # a clean iteration has no fault and no suffix
    assert format_exit_reason({"endedAt": "x", "exitCode": 0,
                               "faultClass": None}) == EXIT_REASON_CLEAN


def test_exit_reason_keeps_one_line_and_bounded():
    reason = format_exit_reason({"endedAt": "x", "error": "z" * 5000})
    assert "\n" not in reason and len(reason) < 300 and reason.endswith("...")


def test_format_tokens_names_only_what_was_counted():
    assert format_tokens(_base_meta()["usage"]) == (
        "180,661 total (in 18, out 2,118, cache read 136,849, "
        "cache write 41,676)")
    # no cache split reported -> no zeroed cache fields invented
    assert format_tokens({"input": 10, "output": 5, "totalTokens": 15}) == \
        "15 total (in 10, out 5)"
    assert format_tokens({"totalTokens": 0}) == "0 total"
    assert format_tokens({}) == USAGE_NONE
    assert format_tokens(None) == USAGE_NONE
    assert format_tokens({"totalTokens": "junk"}) == USAGE_NONE


def test_iteration_span_names_what_exists():
    assert _iteration_span([]) == "none recorded yet"
    assert _iteration_span([1]) == "1"
    assert _iteration_span([1, 2, 3]) == "1..3"
    assert _iteration_span([2, 5]) == "2, 5"


def test_iteration_numbers_reads_the_dirs_and_ignores_junk(tmp_path):
    _write_iteration(tmp_path, 1, _base_meta(1))
    _write_iteration(tmp_path, 10, _base_meta(10))
    (tmp_path / "iterations" / "notanumber").mkdir()
    assert iteration_numbers(tmp_path) == [1, 10]


def test_iteration_detail_is_none_for_an_iteration_that_is_not_there(tmp_path):
    _write_iteration(tmp_path, 1, _base_meta(1))
    assert iteration_detail(tmp_path, 2) is None
    assert iteration_detail(tmp_path / "nope", 1) is None


def test_iteration_detail_shapes_meta_plus_derived_fields(tmp_path):
    _write_iteration(tmp_path, 2, _base_meta(), texts=["hello"])
    d = iteration_detail(tmp_path, 2)

    # meta.json passes through verbatim...
    assert d["phase"] == "worker" and d["approach"] == 2
    assert d["modelResolved"] == "amazon-bedrock/eu.anthropic.claude-opus-5"
    assert d["usage"]["totalTokens"] == 180661
    # ...with the display fields both surfaces need alongside it
    assert d["hasMeta"] is True
    assert d["durationS"] == 1071 and d["durationDisplay"] == "17m 51s"
    assert d["durationLabel"] == "total"
    assert d["exitReason"] == EXIT_REASON_CLEAN
    assert d["costDisplay"] == "$0.4231" and d["costStatus"] is None
    assert d["tokensDisplay"].startswith("180,661 total")
    assert d["startedAtLocal"] and d["endedAtLocal"]
    assert d["hasTranscript"] is True and d["transcriptBytes"] > 0


def test_iteration_detail_says_what_it_does_not_know(tmp_path):
    """An iteration dir whose meta.json never landed (crash mid-write): the
    transcript is still readable and the metadata is genuinely unknown --
    `hasMeta: False` + `unknown`, not a clean exit."""
    _write_iteration(tmp_path, 1, None, texts=["only the transcript"])
    d = iteration_detail(tmp_path, 1)
    assert d["hasMeta"] is False and d["number"] == 1
    assert d["exitReason"] == EXIT_REASON_UNKNOWN
    assert d["durationS"] is None and d["durationDisplay"] == "n/a"
    assert d["tokensDisplay"] == USAGE_NONE and d["costDisplay"] == USAGE_NONE
    assert "startedAtLocal" not in d and "endedAtLocal" not in d
    assert d["hasTranscript"] is True

    # ... and a truncated meta.json is the same kind of ignorance
    (tmp_path / "iterations" / "0001" / "meta.json").write_text('{"number": 1,')
    assert iteration_detail(tmp_path, 1)["hasMeta"] is False


def test_iteration_detail_running_iteration_has_no_end(tmp_path):
    """An unfinished iteration's duration is elapsed-so-far, and says so --
    never presented as a total, never withheld."""
    started = utc_from_epoch(time.time() - 65)
    _write_iteration(tmp_path, 3, _base_meta(3, startedAt=started, endedAt=None,
                                             exitCode=None, usage=None), texts=[])
    d = iteration_detail(tmp_path, 3)
    assert d["exitReason"] == EXIT_REASON_RUNNING
    assert 60 <= d["durationS"] < 120 and d["durationDisplay"].startswith("1m")
    assert d["durationLabel"] == "elapsed" and "endedAtLocal" not in d
    assert d["hasTranscript"] is False and d["transcriptBytes"] == 0


def test_iteration_detail_never_prices_an_implausible_zero(tmp_path):
    """Task 049's rule, on this surface too: a provider quote of $0 next to
    half a million billed tokens is an unpriced route, not free money."""
    _write_iteration(tmp_path, 1, _base_meta(1, usage=ZERO_QUOTE_USAGE))
    d = iteration_detail(tmp_path, 1)
    assert d["costStatus"] == "unknown" and d["costDisplay"] == "unavailable"
    assert "0.00" not in d["costDisplay"]
    assert d["tokensDisplay"].startswith("505,628 total")


def test_iteration_detail_recomputes_forged_display_fields(tmp_path):
    """A hand-edited (or hostile) meta.json cannot smuggle in a display string
    its raw fields do not support -- the `_with_approach_display` discipline,
    applied at the source."""
    _write_iteration(tmp_path, 1, _base_meta(1, exitReason="clean exit",
                                             costDisplay="$0.00", hasMeta=True,
                                             durationDisplay="1s",
                                             usage=ZERO_QUOTE_USAGE,
                                             exitCode=9, endedAt="2026-09-02T10:00:09Z"))
    d = iteration_detail(tmp_path, 1)
    assert d["exitReason"] == "exit 9"
    assert d["costDisplay"] == "unavailable"
    assert d["durationDisplay"] == "9s"


# ------------------------------------------------------- black-box, no container
def test_iteration_prints_the_detail_and_the_log(tmp_path):
    registry, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 2, _base_meta(steeringConsumed=["001-focus.md"]),
                     texts=["first agent line", "second agent line"])

    res = _ctl(registry, "iteration", "itdetail", "2")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "Traceback" not in res.stderr
    # no live API was ever needed, so no snapshot notice is warranted
    assert "on-disk snapshot" not in res.stderr
    out = res.stdout
    for expected in ("run:       itdetail", "iteration: 2", "phase worker",
                     "approach 2", "duration:  17m 51s  (total)",
                     f"exit:      {EXIT_REASON_CLEAN}",
                     "model:     amazon-bedrock/eu.anthropic.claude-opus-5",
                     "(gateway id: eu.anthropic.claude-opus-5)",
                     "tokens:    180,661 total", "cost:      $0.4231",
                     "steering:  001-focus.md", "started:   ", "ended:     "):
        assert expected in out, (expected, out)
    assert "first agent line" in out and "second agent line" in out
    assert "--- log (" in out


def test_iteration_log_matches_logs_iteration(tmp_path):
    """Same merge, same renderer: the transcript this command prints is
    line-for-line what `ralphctl logs --iteration N` prints."""
    registry, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 1, _base_meta(1), texts=["alpha", "beta"])

    detail = _ctl(registry, "iteration", "itdetail", "1")
    logs = _ctl(registry, "logs", "itdetail", "--iteration", "1")
    assert detail.returncode == 0 and logs.returncode == 0

    header, _, log_part = detail.stdout.partition("--- log (")
    printed = log_part.split(")---\n" if ")---\n" in log_part else ") ---\n", 1)[1]
    assert printed.splitlines() == logs.stdout.splitlines()
    assert "iteration: 1" in header


def test_iteration_no_log_prints_header_only(tmp_path):
    registry, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 1, _base_meta(1), texts=["secret transcript line"])

    res = _ctl(registry, "iteration", "itdetail", "1", "--no-log")

    assert res.returncode == 0, res.stderr
    assert "secret transcript line" not in res.stdout
    assert "--- log" not in res.stdout
    assert "tokens:" in res.stdout


def test_iteration_json_carries_raw_and_rendered(tmp_path):
    registry, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 2, _base_meta(), texts=["json log line"])

    res = _ctl(registry, "--json", "iteration", "itdetail", "2")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)

    assert doc["runId"] == "itdetail" and doc["number"] == 2
    assert doc["exitCode"] == 0 and doc["usage"]["totalTokens"] == 180661
    assert doc["exitReason"] == EXIT_REASON_CLEAN
    assert doc["durationS"] == 1071
    assert any("json log line" in line for line in doc["log"])
    # no ANSI escapes in a machine document, ever
    assert not any("\x1b[" in line for line in doc["log"])

    # --no-log OMITS the key: an empty list would claim the iteration produced
    # no transcript at all
    bare = json.loads(_ctl(registry, "--json", "iteration", "itdetail", "2",
                           "--no-log").stdout)
    assert "log" not in bare and bare["hasTranscript"] is True


def test_iteration_with_no_transcript_says_so(tmp_path):
    registry, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 1, _base_meta(1))

    res = _ctl(registry, "iteration", "itdetail", "1")
    assert res.returncode == 0, res.stderr
    assert NO_TRANSCRIPT in res.stdout


def test_iteration_without_meta_json_is_honest_on_the_cli(tmp_path):
    registry, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 1, None, texts=["transcript only"])

    res = _ctl(registry, "iteration", "itdetail", "1")
    assert res.returncode == 0, res.stderr
    assert "no readable meta.json" in res.stdout
    assert f"exit:      {EXIT_REASON_UNKNOWN}" in res.stdout
    assert "duration:  n/a" in res.stdout
    assert "transcript only" in res.stdout


def test_missing_iteration_exits_1_naming_what_exists(tmp_path):
    registry, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 1, _base_meta(1))
    _write_iteration(run_dir, 2, _base_meta(2))

    res = _ctl(registry, "iteration", "itdetail", "47")

    assert res.returncode == 1, (res.returncode, res.stdout, res.stderr)
    assert res.stdout == ""
    assert "Traceback" not in res.stderr
    assert "no iteration 47" in res.stderr and "1..2" in res.stderr


def test_missing_iteration_on_a_run_with_none_yet(tmp_path):
    registry, _ = _dead_run(tmp_path)
    res = _ctl(registry, "iteration", "itdetail", "1")
    assert res.returncode == 1, res.stdout
    assert "none recorded yet" in res.stderr


def test_unknown_run_still_exits_3(tmp_path):
    registry, _ = _dead_run(tmp_path)
    res = _ctl(registry, "iteration", "ghost-run", "1")
    assert res.returncode == 3, (res.returncode, res.stderr)
    assert "not found" in res.stderr


def test_running_iteration_reads_as_still_running(tmp_path):
    registry, run_dir = _dead_run(tmp_path)
    started = utc_from_epoch(time.time() - 65)
    _write_iteration(run_dir, 3, _base_meta(3, startedAt=started, endedAt=None,
                                            exitCode=None, usage=None),
                     texts=["mid-flight"])

    res = _ctl(registry, "iteration", "itdetail", "3")
    assert res.returncode == 0, res.stderr
    assert f"exit:      {EXIT_REASON_RUNNING}" in res.stdout
    assert "(elapsed)" in res.stdout and "(total)" not in res.stdout
    assert f"cost:      {USAGE_NONE}" in res.stdout
    assert "ended:" not in res.stdout


def test_verify_iteration_shows_its_verdict(tmp_path):
    registry, run_dir = _dead_run(tmp_path)
    _write_iteration(run_dir, 4, _base_meta(4, phase="verify",
                                            verifiedTask="007",
                                            verifyOutcome="fail"))
    res = _ctl(registry, "iteration", "itdetail", "4", "--no-log")
    assert res.returncode == 0, res.stderr
    assert "verified:  task 007 -> fail" in res.stdout


# ------------------------------------------------------------- real engine tier
def test_real_engine_iteration_dir_renders_end_to_end(live):
    """The fixture is an iteration dir the ENGINE wrote: `ralphctl iteration`
    must agree with that meta.json, and keep working once the engine (the
    stand-in for the container) is gone."""
    r = live(run_id="itreal", job={"iterations": 2, "max_approaches": 1,
                                   "on_complete": "idle"})
    r.wait_terminal(timeout=90)
    r.stop()  # container gone: nothing live to ask

    meta = json.loads((r.run_dir / "iterations" / "0001" / "meta.json").read_text())
    res = r.ralphctl("iteration", "itreal", "1")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert f"phase {meta['phase']}" in res.stdout
    assert f"exit:      {EXIT_REASON_CLEAN}" in res.stdout
    assert f"{meta['usage']['totalTokens']:,} total" in res.stdout

    doc = json.loads(r.ralphctl("--json", "iteration", "itreal", "1").stdout)
    for key in ("number", "phase", "startedAt", "endedAt", "exitCode", "usage"):
        assert doc[key] == meta[key], key
    assert doc["log"], "the engine's own transcript should render"

    # one past the last recorded iteration is a clean exit 1, not a traceback
    last = iteration_numbers(r.run_dir)[-1]
    missing = r.ralphctl("iteration", "itreal", str(last + 1))
    assert missing.returncode == 1 and "no iteration" in missing.stderr
