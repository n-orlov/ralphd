"""v0.36 F.3-F.6 / C.5: where the faults that used to pause the run now land.

Each node drives the whole loop (`_run_job_core`) with fake hats, a fake clock
and zeroed backoffs, and asserts the state the PRD names -- never `paused`:

1. Judge no-verdict, `review_contract_unrepaired` and `review_findings_unusable`
   are, after the `judge_retry_max` retries, infra faults under the unchanged
   outage budget; a spent budget ends the run `failed` (verdict `unreviewed`).
2. The rate-limit ceiling ends `failed` with `ending: rate_limit_ceiling`
   (resumable by `ralphctl resume --rate-limit-wait +N`); a provider refusal
   ends `failed` with `ending: provider_refused`, naming `PUT /config/llm` and
   `resume`.
3. A secret found in a commit parks in `awaiting_input` with
   `inputQuestion.kind: secret`; `ralphctl answer <run> --secret-handled` is
   refused while the commit scan of the offending range still hits and, once
   clean, continues at the worker.

Against the 0.35 engine every node here fails: the faults paused the run
(`state: paused`, `operator_input_needed`) and waited for an unpause.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ralphd.cli import main as cli_main
from ralphd.engine import redact, state
from ralphd.engine.faults import FAULT_CLASS_RATE_LIMIT
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import ScriptedRunner, make_supervisor, scripted_supervisor

TASK_1 = "001"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _plan(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1,
        "tasks": [{"id": TASK_1, "title": "t", "successCriteria": "c"}],
    })
    return _ok(json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _worker(sup, prompt=""):
    return _ok(json.dumps({"verdict": "complete", "task_id": TASK_1}))


def _review(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


def _review_prose(sup, prompt=""):
    return _ok("no contract payload at all, just prose")


def _review_truncated(sup, prompt=""):
    r = IterationResult(exit_code=0)
    r.final_text = ""
    r.output_truncated = True
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 4096, "totalTokens": 4106}
    return r


def _gate_truncated(sup, prompt=""):
    return _review_truncated(sup, prompt)


def _gate_approves(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


_supervisor = scripted_supervisor(run_id="stop-routing", iterations=30,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _build(root: Path, *, review=None, gate=None, **cfg):
    RunDir(root=root).update_status(state="starting")
    scripts = {"planning": [_plan], "worker": [_worker],
               "review": review or [_review]}
    if gate is not None:
        scripts["verdict_gate"] = gate
        cfg.setdefault("peer_verdict_gate", True)
    return _supervisor(root, scripts, **cfg)


async def _drive(sup):
    """Run the whole job; return the final state and every `paused` or
    `awaiting_input` status seen on the way (the engine must show none)."""
    task = asyncio.ensure_future(sup._run_job_core())
    seen: list[dict] = []
    while not task.done():
        status = sup.run.read_status()
        if status.get("state") in ("paused", "awaiting_input"):
            seen.append(status)
        await asyncio.sleep(0.005)
    return await asyncio.wait_for(task, 30.0), seen


def _run(root: Path, **kw):
    sup = _build(root, **kw)
    final, seen = asyncio.run(_drive(sup))
    return sup, final, seen


def _events(sup, *types):
    return state.read_events(sup.run.root, types=list(types))


# ---------------------------------------------------------------------------
# 1. judge faults are infra faults under the outage budget
# ---------------------------------------------------------------------------

JUDGE_FAULTS = {
    "gate_no_verdict": dict(gate=[_gate_truncated]),
    "review_no_verdict": dict(review=[_review_truncated]),
    "review_contract_unrepaired": dict(review=[_review_prose]),
}


@pytest.mark.parametrize("fault", sorted(JUDGE_FAULTS))
def test_a_judge_fault_with_the_outage_budget_spent_ends_failed_never_paused(
        tmp_path, fault):
    sup, final, seen = _run(tmp_path / "run", infra_outage_budget_s=0.0,
                            **JUDGE_FAULTS[fault])
    assert seen == [], "the engine must not pause or park on a judge fault"
    status = sup.run.read_status()
    assert final == "failed" and status["state"] == "failed", status
    assert status["verdict"] == "unreviewed", status
    assert status.get("pauseReason") is None
    assert status.get("reason"), status


@pytest.mark.parametrize("fault", sorted(JUDGE_FAULTS))
def test_a_judge_fault_inside_the_outage_budget_waits_and_asks_again(
        tmp_path, fault):
    kw = {k: list(v) for k, v in JUDGE_FAULTS[fault].items()}
    if fault == "gate_no_verdict":
        kw["gate"] = [_gate_truncated] * 3 + [_gate_approves]
    elif fault == "review_no_verdict":
        kw["review"] = [_review_truncated] * 3 + [_review]
    else:
        kw["review"] = [_review_prose] * 4 + [_review]
    sup, final, seen = _run(tmp_path / "run", **kw)
    assert seen == []
    status = sup.run.read_status()
    assert final == "succeeded" and status["verdict"] == "verified", status
    faults = [e for e in _events(sup, "infra_retry") if e.get("judgeFault")]
    assert faults, "the judge fault is an infra fault on the outage budget"
    # the budget is the unchanged one (cfg.infra_outage_budget_s)
    assert {e["budgetS"] for e in faults} == {1000.0}
    assert status.get("health") == "ok", "recovery clears the degraded health"


def test_review_findings_unusable_with_the_budget_spent_ends_failed(tmp_path):
    sup = _build(tmp_path / "run", infra_outage_budget_s=0.0,
                 review=[lambda s, p: (
                     s.run.findings_json_file.write_text(
                         '[{"severity": "blocking"}]'),
                     _ok(json.dumps({"verdict": "verified"})))[1]])
    final, seen = asyncio.run(_drive(sup))
    assert seen == []
    status = sup.run.read_status()
    assert final == "failed" and status["verdict"] == "unreviewed", status
    assert sup._judge_fault_tail == "review_findings_unusable"


def test_the_outage_budget_is_shared_with_the_infra_schedule(tmp_path):
    """The unchanged budget and schedule: the judge fault waits are the
    schedule's own values, capped by what is left of the budget."""
    sup = _build(tmp_path / "run", review=[_review_truncated],
                 infra_outage_budget_s=2.0, infra_retry_backoff_s=[0.5, 1.0],
                 infra_retry_backoff_max_s=1.0)
    waits = []

    async def fake_wait(backoff):
        waits.append(backoff)
        return backoff, False

    sup._wait_out_backoff = fake_wait  # type: ignore[method-assign]
    final, seen = asyncio.run(_drive(sup))
    assert seen == [] and final == "failed"
    assert waits == [0.5, 1.0, 0.5]


# ---------------------------------------------------------------------------
# 2. rate-limit ceiling and provider refusal: failed, with an `ending`
# ---------------------------------------------------------------------------

NOW = 1_790_000_000.0


def _rate_limited(sup, prompt=""):
    info = {"status": "rejected", "rateLimitType": "five_hour",
            "resetsAt": NOW + 600,
            "unifiedWindows": {"five_hour": {"resetsAt": NOW + 600}}}
    r = IterationResult(exit_code=1, harness="claude-code", rate_limit=info,
                        rate_limit_fresh=True, http_status=429,
                        error_type="rate_limit")
    r.error_message = ('429 rate_limit: API Error: 429 {"type":"error","error":'
                       '{"type":"rate_limit_error","message":"rate limited"}}')
    r.duration_s = 2.0
    return r


def _refused(sup, prompt=""):
    r = IterationResult(exit_code=1)
    r.error_message = (
        '400 {"type":"error","error":{"type":"invalid_request_error",'
        '"message":"Third-party apps now draw from your extra usage, not '
        'your plan limits"}}')
    r.http_status = 400
    r.error_type = "invalid_request_error"
    r.duration_s = 2.0
    return r


def _faulted_worker_run(tmp_path, worker_step, **cfg):
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = make_supervisor(
        tmp_path / "run",
        runner=ScriptedRunner({"planning": [_plan], "worker": [worker_step]}),
        run_id="stop-routing", iterations=30, max_approaches=1,
        vigilant=False, on_complete="exit", **cfg)
    sup._now_epoch = lambda: NOW                         # type: ignore[method-assign]
    sup._rate_limit_jitter_s = lambda: 0.0               # type: ignore[method-assign]

    async def wait(seconds):
        return seconds, False

    sup._wait_out_backoff = wait                         # type: ignore[method-assign]
    final, seen = asyncio.run(_drive(sup))
    return sup, final, seen


def test_the_rate_limit_ceiling_ends_failed_with_the_ending(tmp_path):
    assert FAULT_CLASS_RATE_LIMIT == "rate_limit"
    sup, final, seen = _faulted_worker_run(
        tmp_path, _rate_limited, rate_limit_max_wait_s=300)
    assert seen == [], "the ceiling no longer pauses"
    status = sup.run.read_status()
    assert final == "failed" and status["state"] == "failed", status
    assert status["ending"] == "rate_limit_ceiling", status
    assert "--rate-limit-wait" in status["reason"], status
    assert status.get("pauseReason") is None


def test_a_provider_refusal_ends_failed_naming_put_config_llm_and_resume(
        tmp_path):
    sup, final, seen = _faulted_worker_run(tmp_path, _refused)
    assert seen == [], "a refusal no longer pauses"
    status = sup.run.read_status()
    assert final == "failed" and status["state"] == "failed", status
    assert status["ending"] == "provider_refused", status
    assert "PUT /config/llm" in status["reason"], status
    assert "resume" in status["reason"], status
    assert status.get("pauseReason") is None


def test_resume_takes_a_rate_limit_wait_top_up_into_job_yaml():
    """`ralphctl resume --rate-limit-wait +N` raises the ceiling the resumed
    engine seeds from (job.yaml `rate_limit_max_wait_s`)."""
    args = cli_main.build_parser().parse_args(
        ["resume", "some-run", "--rate-limit-wait", "+3600"])
    assert args.rate_limit_wait == "+3600"
    job: dict = {"rate_limit_max_wait_s": 1000}
    cli_main._apply_rate_limit_wait_topup(job, args.rate_limit_wait)
    assert job["rate_limit_max_wait_s"] == 4600
    job = {}
    cli_main._apply_rate_limit_wait_topup(job, "7200")
    assert job["rate_limit_max_wait_s"] == 7200


# ---------------------------------------------------------------------------
# 3. a secret in a commit: awaiting_input / secret, one answer
# ---------------------------------------------------------------------------

SYNTHETIC_VALUE = "sk-synthetic-stoproute-0123456789abcdefSTOP"
SYNTHETIC_NAME = "SYNTHETIC_STOPROUTE_API_TOKEN"


@pytest.fixture
def live_secret(monkeypatch, tmp_path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv(SYNTHETIC_NAME, SYNTHETIC_VALUE)
    redact.refresh_redaction_map(home=fake_home)
    try:
        yield
    finally:
        monkeypatch.delenv(SYNTHETIC_NAME, raising=False)
        redact.refresh_redaction_map(home=fake_home)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _leaked_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    (ws / "seed.txt").write_text("seed\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "seed")
    base = _git(ws, "rev-parse", "HEAD")
    (ws / "leak.txt").write_text(f"token = {SYNTHETIC_VALUE}\n")
    _git(ws, "add", "leak.txt")
    _git(ws, "commit", "-q", "--no-verify", "-m", "add the file")
    return ws, base, _git(ws, "rev-parse", "HEAD")


def _parked_on_a_secret(tmp_path):
    ws, base, head = _leaked_workspace(tmp_path)
    sup = make_supervisor(tmp_path / "run", workspace=str(ws), prd=True,
                          max_approaches=2, run_id="stop-routing")
    sup._approach = 1
    sup.run.update_status(state="running", phase="worker", approach=1)
    hit = sup._scan_task_boundary_commits(base, head)
    assert hit is not None and hit["sha"] == head
    sup._park_for_commit_secret(base, head)
    return sup, ws, base, head


def test_a_secret_in_a_commit_parks_awaiting_input_with_a_secret_question(
        tmp_path, live_secret):
    sup, _ws, base, head = _parked_on_a_secret(tmp_path)
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input", status
    question = status["inputQuestion"]
    assert question["kind"] == "secret"
    assert question["expiresAt"] is None, "a secret park never expires"
    assert (question["baseSha"], question["headSha"]) == (base, head)
    assert question["sha"] == head and question["part"] == "diff"
    assert SYNTHETIC_VALUE not in json.dumps(status)
    assert sup._pause.is_set() is False


def test_secret_handled_is_refused_while_the_scan_hits_and_continues_once_clean(
        tmp_path, live_secret):
    from ralphd.engine import answer as answering

    sup, ws, base, _head = _parked_on_a_secret(tmp_path)
    # anything but --secret-handled is refused, naming it
    with pytest.raises(answering.AnswerRefused) as bare:
        sup.answer(cure_rounds=1)
    assert "--secret-handled" in bare.value.message
    # the range still hits: refused, naming sha/part/variable, never the value
    with pytest.raises(answering.AnswerRefused) as hit:
        sup.answer(secret_handled=True)
    assert "still hits" in hit.value.message
    assert SYNTHETIC_VALUE not in hit.value.message
    assert sup.run.read_status()["state"] == "awaiting_input"
    assert sup._pause.is_set() is False

    # the operator removes the secret from history; now the scan is clean
    _git(ws, "reset", "-q", "--hard", base)
    record = sup.answer(secret_handled=True)
    assert record["outcome"] == "secret_handled"
    status = sup.run.read_status()
    assert status["state"] == "running" and status.get("inputQuestion") is None
    assert sup._pause.is_set() is True, "the worker is released"
    assert [e for e in _events(sup, "input.answered")
            if e.get("kind") == "secret"]


def test_ralphctl_answer_secret_handled_posts_the_flag(monkeypatch):
    seen = {}

    def api(run_id, method, path, body=None, **kw):
        seen.update(run_id=run_id, method=method, path=path, body=body)
        return {"kind": "secret", "outcome": "secret_handled"}

    monkeypatch.setattr(cli_main, "api", api)
    monkeypatch.setattr(cli_main, "_require_run", lambda run_id: None)
    args = cli_main.build_parser().parse_args(
        ["answer", "some-run", "--secret-handled"])
    args.func(args)
    assert seen["path"] == "/answer"
    assert seen["body"] == {"secretHandled": True}
