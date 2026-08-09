"""Black-box end-to-end tests.

Each test launches the real `ralphd-engine` process (the same entrypoint the
container runs) with a stub `pi` on PATH, then observes it strictly from the
outside: the HTTP API and the run-dir files. No engine internals are imported.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
STUB_PI = REPO / "tests" / "stub-pi"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EngineProc:
    """A running ralphd-engine with its run/config dirs and API helpers."""

    def __init__(self, tmp: Path, job: dict, stub_env: dict | None = None):
        self.run_dir = tmp / "run"
        self.config_dir = tmp / "config"
        self.workspace = tmp / "ws"
        for d in (self.run_dir, self.config_dir, self.workspace):
            d.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "prd.md").write_text("# E2E test PRD\n\nDo the thing.\n")
        (self.config_dir / "job.yaml").write_text(
            "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items()))
        self.port = free_port()
        base_env = {
            k: v for k, v in os.environ.items()
            if k not in ("RALPHD_HOST_WORKSPACE", "RALPHD_HOST_RUN_DIR", "RALPHD_RUN_ID")
        }
        # These docker-siblings vars may be set in the ambient environment
        # (e.g. this very test suite running inside a docker-enabled ralphd
        # job); strip them so tests are deterministic regardless of the host
        # environment, and only set them via explicit stub_env.
        env = {
            **base_env,
            "PATH": f"{STUB_PI}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "STUB_RUN_DIR": str(self.run_dir),
            "RALPHD_RUN_DIR": str(self.run_dir),
            "RALPHD_CONFIG_DIR": str(self.config_dir),
            "RALPHD_WORKSPACE_DIR": str(self.workspace),
            "RALPHD_PORT": str(self.port),
            **(stub_env or {}),
        }
        self.proc = subprocess.Popen(
            ["ralphd-engine"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # -- black-box surface -------------------------------------------------
    def api(self, method: str, path: str, body: dict | None = None,
            expect_error: bool = False):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                return resp.status, (json.loads(data) if data else None)
        except urllib.error.HTTPError as e:
            if not expect_error:
                raise
            return e.code, json.loads(e.read() or b"{}")

    def api_raw(self, path: str) -> str:
        """GET a non-JSON (e.g. NDJSON) route and return the decoded body."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()

    def wait_api(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.api("GET", "/healthz")
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        raise TimeoutError("engine API never came up")

    def wait_state(self, states: tuple[str, ...], timeout=60) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = json.loads((self.run_dir / "status.json").read_text()) \
                if (self.run_dir / "status.json").exists() else {}
            if status.get("state") in states:
                return status
            time.sleep(0.2)
        raise TimeoutError(f"never reached {states}; last: {status}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "e2e", "iterations": 12,
                    "max_approaches": 3, "on_complete": "idle"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


# --------------------------------------------------------------------------
def test_happy_path_exit_mode(engine_factory):
    e = engine_factory(job={"on_complete": "exit"})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)
    # iteration records exist: planning + 2 workers + review
    iters = sorted((e.run_dir / "iterations").iterdir())
    assert len(iters) == 4
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert [m["phase"] for m in metas] == ["planning", "worker", "worker", "review"]
    assert all((d / "output.jsonl").exists() for d in iters)
    # usage accumulated across iterations
    assert status["usage"]["totalTokens"] == 4 * 110


def test_review_rejection_starts_new_approach(engine_factory):
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"STUB_REVIEW_FAILS": "1"})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["approach"] == 2
    composite = (e.run_dir / "composite-prd.md").read_text()
    assert "finding from review #1" in composite
    assert "E2E test PRD" in composite
    assert (e.run_dir / "approaches" / "01" / "review-findings.md").exists()


def test_stagnation_guard_fails_job(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "max_approaches": 1},
                       stub_env={"STUB_WORKER_STALLS": "1"})
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"


def test_budget_exhaustion_fails_job(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "iterations": 2,
                            "max_approaches": 1},
                       stub_env={"STUB_TASKS": "5"})
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["iterationsUsed"] == 2


def test_api_observation_and_idle_lifecycle(engine_factory):
    e = engine_factory()  # idle mode
    e.wait_api()
    e.wait_state(("succeeded",))

    code, status = e.api("GET", "/status")
    assert code == 200 and status["verdict"] == "verified"
    assert status["tasks"]["completed"] == status["tasks"]["total"] == 2

    code, tasks = e.api("GET", "/tasks")
    assert len(tasks["tasks"]) == 2

    code, iterations = e.api("GET", "/iterations")
    assert [i["phase"] for i in iterations] == \
        ["planning", "worker", "worker", "review"]

    code, meta = e.api("GET", "/iterations/1")
    assert meta["phase"] == "planning" and meta["exitCode"] == 0

    # steering a finished job is rejected
    code, _ = e.api("POST", "/steering", {"message": "late"}, expect_error=True)
    assert code == 409
    # so is aborting or pausing it
    code, _ = e.api("POST", "/abort", {}, expect_error=True)
    assert code == 409
    code, _ = e.api("POST", "/pause", expect_error=True)
    assert code == 409

    # events replay contains the full story
    req = urllib.request.Request(
        f"http://127.0.0.1:{e.port}/events?since=0")
    types = []
    with urllib.request.urlopen(req, timeout=10) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data: "):
                types.append(json.loads(line[6:])["type"])
            if types and types[-1] == "state":
                break
    assert "iteration.start" in types and "signal" in types
    assert types.count("phase") >= 2  # planning + review

    # engine stays up (idle), then shuts down on request
    assert e.proc.poll() is None
    code, _ = e.api("POST", "/shutdown")
    assert code == 200
    assert e.proc.wait(timeout=10) == 0


def test_steering_reaches_next_iteration(engine_factory):
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"STUB_SLEEP": "1.5", "STUB_TASKS": "3"})
    e.wait_api()
    code, res = e.api("POST", "/steering", {"message": "prefer the shortcut",
                                            "name": "hint"})
    assert code == 202 and res["file"] == "001-hint.md"
    assert e.proc.wait(timeout=90) == 0
    # the stub records steering text it saw in the final worker prompt
    saw = (e.run_dir / ".stub-saw-steering").read_text()
    assert "prefer the shortcut" in saw
    # and the API/journal marks it consumed
    consumed = json.loads((e.run_dir / "steering" / ".consumed.json").read_text())
    assert "001-hint.md" in consumed


def test_abort_via_api(engine_factory):
    e = engine_factory(stub_env={"STUB_SLEEP": "2", "STUB_TASKS": "10"})
    e.wait_api()
    e.wait_state(("running",))
    code, _ = e.api("POST", "/abort", {"reason": "test abort"})
    assert code == 200
    status = e.wait_state(("aborted",), timeout=30)
    assert status["reason"] == "test abort"
    # idle mode: engine still up and queryable after abort
    code, s = e.api("GET", "/status")
    assert code == 200 and s["state"] == "aborted"
    e.api("POST", "/shutdown")
    assert e.proc.wait(timeout=10) == 1


def test_huge_output_line_does_not_kill_job(engine_factory):
    """pi emits full message snapshots per NDJSON event; a single line can be
    hundreds of KiB. Regression: this used to raise 'Separator is not found'
    and fail the entire job instead of at most one iteration."""
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"STUB_HUGE_LINE": "1"})
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"


def test_vigilant_happy_path(engine_factory):
    """Vigilant mode: every completed task gets one verify iteration; phases are
    planning → (worker, verify) × n → review; job succeeds with verdict verified."""
    e = engine_factory(job={"on_complete": "idle", "vigilant": True})
    e.wait_api()
    e.wait_state(("succeeded",), timeout=90)
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    # Phase sequence: planning, worker, verify, worker, verify, review (2 tasks)
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    assert phases == ["planning", "worker", "verify", "worker", "verify", "review"]

    # Verify iterations carry task metadata and show passing outcome
    verify_metas = [m for m in metas if m["phase"] == "verify"]
    assert len(verify_metas) == 2
    for vm in verify_metas:
        assert "verifiedTask" in vm
        assert vm["verifyOutcome"] == "pass"

    # All tasks completed
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert all(t["status"] == "completed" for t in tasks)

    # taskVerified signal emitted once per task
    events_text = (e.run_dir / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
    tv_events = [ev for ev in events
                 if ev.get("type") == "signal" and ev.get("signal") == "taskVerified"]
    assert len(tv_events) == 2

    # API: /iterations also shows the verify phases
    _, iterations = e.api("GET", "/iterations")
    assert [i["phase"] for i in iterations] == phases

    # API: /status works after vigilant run
    _, api_status = e.api("GET", "/status")
    assert api_status["state"] == "succeeded" and api_status["verdict"] == "verified"


def test_steering_not_lost_across_verify_phase(engine_factory):
    """Regression (task 046): steering that arrives while a worker iteration is
    in flight must be seen by the very next iteration boundary regardless of
    phase. If that next iteration happens to be a vigilant "verify" (a pure
    verification role whose prompt never tells the agent to act on operator
    guidance), the engine must NOT mark the steering consumed there -- doing
    so would silently discard it forever, since nothing ever acts on it. It
    must stay pending until an actionable phase (planning/worker) runs."""
    e = engine_factory(job={"on_complete": "idle", "vigilant": True},
                       stub_env={"STUB_SLEEP": "0.6"})
    e.wait_api()

    def iter_meta(n: int) -> dict | None:
        p = e.run_dir / "iterations" / f"{n:04d}" / "meta.json"
        return json.loads(p.read_text()) if p.exists() else None

    # Wait for iteration 2 (the first worker iteration) to have started -- by
    # the time its meta.json exists it has already computed its own pending
    # steering (there was none yet), so anything we send now is guaranteed to
    # be missed by iteration 2 and first visible to iteration 3.
    deadline = time.time() + 30
    while time.time() < deadline and iter_meta(2) is None:
        time.sleep(0.05)
    assert iter_meta(2) is not None, "worker iteration 2 never started"
    assert iter_meta(2)["phase"] == "worker"

    code, res = e.api("POST", "/steering",
                      {"message": "double-check the edge cases", "name": "midrun"})
    assert code == 202 and res["file"] == "001-midrun.md"

    e.wait_state(("succeeded",), timeout=90)

    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    assert phases == ["planning", "worker", "verify", "worker", "verify", "review"]

    # Iteration 3 is the verify iteration that immediately follows the worker
    # iteration during which we sent steering. It must NOT claim to have
    # consumed it, and its prompt must not present it as an actionable
    # instruction (message text withheld -- only a passive notice, if any).
    verify1_meta = metas[2]
    verify1_prompt = (iters[2] / "prompt.md").read_text()
    assert verify1_meta["steeringConsumed"] == []
    assert "MUST take priority" not in verify1_prompt
    assert "double-check the edge cases" not in verify1_prompt

    # (verify1_meta["steeringConsumed"] == [] above already proves the verify
    # iteration itself did not mark it consumed; .consumed.json is checked
    # for its final state below, once the whole job has finished.)

    # Iteration 4 is the next worker iteration (actionable) -- it must be the
    # one that finally sees and consumes the steering.
    worker2_meta = metas[3]
    worker2_prompt = (iters[3] / "prompt.md").read_text()
    assert worker2_meta["phase"] == "worker"
    assert "001-midrun.md" in worker2_meta["steeringConsumed"]
    assert "MUST take priority" in worker2_prompt
    assert "double-check the edge cases" in worker2_prompt

    consumed_final = json.loads((e.run_dir / "steering" / ".consumed.json").read_text())
    assert "001-midrun.md" in consumed_final

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded" and status["verdict"] == "verified"


def test_verified_review_refused_while_steering_pending(engine_factory):
    """Task 005 regression: steering that lands during the review iteration of
    what would otherwise be the final VERIFIED verdict must not be silently
    stranded by a terminal-succeeded run. The engine must discard that verdict,
    route back through one more (actionable) worker iteration to actually
    consume the steering, then re-review -- only succeeding once no steering
    is left pending."""
    e = engine_factory(job={"on_complete": "idle"},
                       stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "1.0"})
    e.wait_api()

    def iter_meta(n: int) -> dict | None:
        p = e.run_dir / "iterations" / f"{n:04d}" / "meta.json"
        return json.loads(p.read_text()) if p.exists() else None

    # Iteration 3 is the first review (1=planning, 2=worker with a single
    # task -> COMPLETE). It sleeps STUB_SLEEP seconds before doing anything,
    # giving us a real window to land steering while it's in flight, strictly
    # before the engine's post-review pending-steering check can run.
    deadline = time.time() + 30
    while time.time() < deadline and iter_meta(3) is None:
        time.sleep(0.05)
    assert iter_meta(3) is not None, "review iteration 3 never started"
    assert iter_meta(3)["phase"] == "review"

    code, res = e.api("POST", "/steering",
                      {"message": "hold on, check one more thing", "name": "lastcall"})
    assert code == 202 and res["file"] == "001-lastcall.md"

    status = e.wait_state(("succeeded", "failed", "aborted"), timeout=90)
    assert status["state"] == "succeeded" and status["verdict"] == "verified"

    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    # The engine must NOT go terminal after the first review: it has to
    # insert one more worker+review pair to actually consume the steering.
    assert phases == ["planning", "worker", "review", "worker", "review"]

    review1_meta = metas[2]
    assert review1_meta["steeringConsumed"] == []

    worker2_meta = metas[3]
    assert worker2_meta["phase"] == "worker"
    assert "001-lastcall.md" in worker2_meta["steeringConsumed"]

    # It was never silently discarded: recorded consumed, and the run only
    # went terminal-succeeded once it was.
    consumed_final = json.loads((e.run_dir / "steering" / ".consumed.json").read_text())
    assert "001-lastcall.md" in consumed_final

    events_text = (e.run_dir / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
    warnings = [ev for ev in events
                if ev.get("type") == "log" and ev.get("level") == "warning"
                and "deferring" in ev.get("message", "")]
    assert warnings, "expected a deferred-VERIFIED warning log event"
    assert "001-lastcall.md" in warnings[0]["message"]


def test_vigilant_verify_fail_then_recovery(engine_factory):
    """Vigilant mode: one task fails verification once, then the worker retries it
    and the second verification passes; job ends succeeded."""
    e = engine_factory(
        job={"on_complete": "idle", "vigilant": True},
        stub_env={"STUB_VERIFY_FAILS": "1"},
    )
    e.wait_api()
    e.wait_state(("succeeded",), timeout=120)

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    # Phase sequence: planning, worker, verify(fail), worker(retry),
    # verify(pass), worker(task2+COMPLETE), verify(task2 pass), review
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    assert phases == [
        "planning",
        "worker", "verify",  # task 1: first verify fails
        "worker", "verify",  # task 1: worker retries, second verify passes
        "worker", "verify",  # task 2: worker completes, verify passes
        "review",
    ]

    # Failed verify iteration recorded outcome "fail"
    verify_metas = [m for m in metas if m["phase"] == "verify"]
    assert len(verify_metas) == 3
    assert verify_metas[0]["verifyOutcome"] == "fail"
    assert verify_metas[1]["verifyOutcome"] == "pass"
    assert verify_metas[2]["verifyOutcome"] == "pass"

    # The retried task (task 1) should show validationAttempts==1 and validationNotes
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert all(t["status"] == "completed" for t in tasks)
    retried = [t for t in tasks if t.get("validationAttempts", 0) >= 1]
    assert len(retried) == 1, "exactly one task should have been retried"
    rt = retried[0]
    assert rt["validationAttempts"] == 1
    assert rt.get("validationNotes"), "validationNotes should be set after failed verify"

    # events.jsonl: exactly 2 taskVerified signals (one for each task's final pass)
    events_text = (e.run_dir / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
    tv_events = [ev for ev in events
                 if ev.get("type") == "signal" and ev.get("signal") == "taskVerified"]
    assert len(tv_events) == 2

    # events.jsonl also captured the task going to validation-failed
    task_events = [ev for ev in events
                   if ev.get("type") == "task"
                   and ev.get("newStatus") == "validation-failed"]
    assert len(task_events) >= 1


def test_vigilant_verify_transient_error_retries(engine_factory):
    """Regression (task 050): a verify iteration that errors out mid-stream
    (agent/provider failure, e.g. a Bedrock 502) before ever emitting a
    verdict sentinel must NOT be scored as a validation failure -- the
    engine retries verification instead, leaving the task's status and
    validationAttempts completely untouched, and the retry succeeds."""
    e = engine_factory(
        job={"on_complete": "idle", "vigilant": True},
        stub_env={"STUB_TASKS": "1", "STUB_VERIFY_ERRORS": "1"},
    )
    e.wait_api()
    e.wait_state(("succeeded",), timeout=90)

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    # Phase sequence: planning, worker, verify(error), verify(pass), review
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    assert phases == ["planning", "worker", "verify", "verify", "review"]

    verify_metas = [m for m in metas if m["phase"] == "verify"]
    assert len(verify_metas) == 2
    assert verify_metas[0]["verifyOutcome"] == "error"
    assert verify_metas[1]["verifyOutcome"] == "pass"

    # The task was never scored as a validation failure: status went
    # straight to completed, validationAttempts was never incremented, and
    # no validationNotes were ever set.
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "completed"
    assert tasks[0].get("validationAttempts", 0) == 0
    assert not tasks[0].get("validationNotes")

    # taskVerified signal emitted once (for the retry that actually passed)
    events_text = (e.run_dir / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
    tv_events = [ev for ev in events
                 if ev.get("type") == "signal" and ev.get("signal") == "taskVerified"]
    assert len(tv_events) == 1

    # Never recorded as a task validation failure
    task_events = [ev for ev in events
                   if ev.get("type") == "task" and ev.get("newStatus") == "validation-failed"]
    assert task_events == []

    # A warning log names the retry and makes explicit that no validation
    # attempt was consumed
    warn_logs = [ev for ev in events
                 if ev.get("type") == "log" and ev.get("level") == "warning"
                 and "retrying verification" in ev.get("message", "")]
    assert len(warn_logs) == 1
    assert "without consuming a validation attempt" in warn_logs[0]["message"]


def test_vigilant_verify_error_exhausts_retries_without_failing_task(engine_factory):
    """Regression (task 050): if a verify iteration keeps erroring out past
    the bounded retry budget, the engine still must not mark the task
    validation-failed or touch validationAttempts -- it just gives up on
    verifying (for now) and surfaces an error log, leaving the task's status
    exactly as the worker left it."""
    e = engine_factory(
        job={"on_complete": "idle", "vigilant": True},
        stub_env={"STUB_TASKS": "1", "STUB_VERIFY_ERRORS": "999"},
    )
    e.wait_api()
    e.wait_state(("succeeded",), timeout=90)

    # Phase sequence: planning, worker, verify x4 (1 initial + 3 retries,
    # all erroring), review -- the un-verified task doesn't block review.
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    assert phases == ["planning", "worker", "verify", "verify", "verify", "verify", "review"]

    verify_metas = [m for m in metas if m["phase"] == "verify"]
    assert len(verify_metas) == 4
    assert all(m["verifyOutcome"] == "error" for m in verify_metas)

    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "completed"
    assert tasks[0].get("validationAttempts", 0) == 0
    assert not tasks[0].get("validationNotes")

    events_text = (e.run_dir / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
    tv_events = [ev for ev in events
                 if ev.get("type") == "signal" and ev.get("signal") == "taskVerified"]
    assert tv_events == []
    task_events = [ev for ev in events
                   if ev.get("type") == "task" and ev.get("newStatus") == "validation-failed"]
    assert task_events == []
    error_logs = [ev for ev in events
                 if ev.get("type") == "log" and ev.get("level") == "error"
                 and "kept erroring" in ev.get("message", "")]
    assert len(error_logs) == 1
    assert "not a validation failure" in error_logs[0]["message"]


def test_vigilant_three_strikes(engine_factory):
    """Vigilant 3-strikes: a task that persistently fails verification is set to
    `failed` after exactly 3 attempts; the job does not end succeeded."""
    e = engine_factory(
        job={"on_complete": "exit", "vigilant": True,
             "iterations": 20, "max_approaches": 1},
        stub_env={"STUB_VERIFY_FAILS": "999"},
    )
    assert e.proc.wait(timeout=120) != 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] != "succeeded"

    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    failed_tasks = [t for t in tasks if t["status"] == "failed"]
    assert len(failed_tasks) >= 1
    for ft in failed_tasks:
        assert ft.get("validationAttempts") == 3

    # The first-worked task had exactly 3 verify iterations, all failing
    first_failed = failed_tasks[0]
    fid = first_failed["id"]
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    verify_metas_for_fid = [
        m for m in metas
        if m.get("phase") == "verify" and m.get("verifiedTask") == fid
    ]
    assert len(verify_metas_for_fid) == 3
    assert all(m["verifyOutcome"] == "fail" for m in verify_metas_for_fid)

    # No taskVerified signals emitted (all verifications failed)
    events_text = (e.run_dir / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
    tv_events = [ev for ev in events
                 if ev.get("type") == "signal" and ev.get("signal") == "taskVerified"]
    assert len(tv_events) == 0


def test_non_vigilant_no_verify_iterations(engine_factory):
    """Non-vigilant mode: no verify iterations are produced; the job runs the
    standard planning → worker×n → review sequence and ends succeeded."""
    e = engine_factory(job={"on_complete": "exit"})  # vigilant not set
    assert e.proc.wait(timeout=60) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    # No iteration should have phase 'verify' in meta.json files
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    assert "verify" not in phases

    # Standard planning + 2 workers + review
    assert phases == ["planning", "worker", "worker", "review"]

    # Confirm via API as well
    e2 = engine_factory(job={"on_complete": "idle"})  # second engine to check API
    e2.wait_api()
    e2.wait_state(("succeeded",), timeout=60)
    _, iterations = e2.api("GET", "/iterations")
    assert all(i["phase"] != "verify" for i in iterations)


def test_docker_siblings_guidance_in_prompt(engine_factory):
    """With RALPHD_HOST_WORKSPACE set (ralphctl --allow-docker), every prompt
    carries a Docker siblings section pointing the agent at HOST paths."""
    e = engine_factory(job={"on_complete": "exit"},
                       stub_env={"RALPHD_HOST_WORKSPACE": "/host/path/ws",
                                 "RALPHD_HOST_RUN_DIR": "/host/path/run",
                                 "RALPHD_RUN_ID": "e2e"})
    assert e.proc.wait(timeout=60) == 0
    prompt = (e.run_dir / "iterations" / "0001" / "prompt.md").read_text()
    assert "## Docker siblings" in prompt
    assert "/host/path/ws" in prompt
    assert "/host/path/run" in prompt
    assert "ralphd.run=$RALPHD_RUN_ID" in prompt


def test_no_docker_siblings_guidance_without_env(engine_factory):
    e = engine_factory(job={"on_complete": "exit"})
    assert e.proc.wait(timeout=60) == 0
    prompt = (e.run_dir / "iterations" / "0001" / "prompt.md").read_text()
    assert "Docker siblings" not in prompt


def test_api_token_auth(engine_factory, tmp_path):
    os.environ["RALPHD_API_TOKEN"] = "sekret"
    try:
        e = engine_factory()
        e.wait_api()  # healthz is unauthenticated
        with pytest.raises(urllib.error.HTTPError) as exc:
            e.api("GET", "/status")
        assert exc.value.code == 401
        req = urllib.request.Request(f"http://127.0.0.1:{e.port}/status")
        req.add_header("Authorization", "Bearer sekret")
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
    finally:
        del os.environ["RALPHD_API_TOKEN"]


# -- self-protection: --help/--version must be pristine (PRD req 29a) -----
def _snapshot(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")}


@pytest.mark.parametrize("flag", ["--help", "-h", "--version"])
def test_engine_help_version_pristine_and_exit_zero(tmp_path, flag):
    """In an empty tmp dir with no RALPHD_* env, --help/--version must exit 0,
    print usage/version, and leave the cwd byte-identical: no dirs/files
    created, no server started, no port bound."""
    workdir = tmp_path / "pristine"
    workdir.mkdir()
    before = _snapshot(workdir)

    env = {k: v for k, v in os.environ.items() if not k.startswith("RALPHD_")}
    env["PATH"] = f"{Path(sys.executable).parent}:{os.environ['PATH']}"

    proc = subprocess.run(
        ["ralphd-engine", flag], cwd=workdir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)

    assert proc.returncode == 0
    if flag == "--version":
        assert "ralphd-engine" in proc.stdout
    else:
        assert "usage:" in proc.stdout

    after = _snapshot(workdir)
    assert before == after, f"pristine-dir violated: created {after - before}"

    # No server was started / port bound: subprocess.run() only returns
    # because the process exited on its own (a bound uvicorn server would
    # block forever serving, not return within the timeout), and the
    # "API listening" log line the real startup path emits is absent.
    assert "API listening" not in proc.stdout


# -- self-protection: exclusive run-dir lock (PRD req 29b) ----------------

def test_second_engine_on_locked_run_dir_refused_first_keeps_serving(
        engine_factory):
    """A second ralphd-engine pointed at a run dir a live engine already
    holds must exit with a documented distinct exit code and a clear
    diagnostic on stderr, while the first engine keeps serving /healthz."""
    e1 = engine_factory(job={"on_complete": "idle"})
    e1.wait_api()

    # engine_factory() reuses the same tmp_path -> same run_dir/config_dir,
    # so e2 points at the exact run dir e1 already holds the flock on.
    e2 = engine_factory(job={"on_complete": "idle"})
    rc = e2.proc.wait(timeout=15)
    out = e2.proc.stdout.read()

    assert rc == 3, f"expected documented lock-refused exit code 3, got {rc}: {out}"
    assert "locked by another live engine" in out
    assert str(e1.run_dir) in out

    # e1 is unaffected and still serving.
    status, _ = e1.api("GET", "/healthz")
    assert status == 200


def test_locked_run_dir_available_again_after_sigkill(engine_factory):
    """flock releases on process death: after SIGKILL of the holder, a new
    engine started against the same run dir must NOT see a stale-lock false
    positive."""
    e1 = engine_factory(job={"on_complete": "idle"})
    e1.wait_api()

    e1.proc.kill()  # SIGKILL, not a pattern-based pkill; we own this PID
    assert e1.proc.wait(timeout=10) == -9 or e1.proc.returncode is not None

    e2 = engine_factory(job={"on_complete": "idle"})
    e2.wait_api(timeout=15)
    status, _body = e2.api("GET", "/healthz")
    assert status == 200


def test_get_logs_whole_job_merge_and_tail(engine_factory):
    """GET /logs merges every iteration's transcript in order, bracketed by
    synthetic ralphd.iteration start/end boundary lines carrying
    number/phase/model/approach (end also exit/error/usage); ?tail=N bounds
    transcript lines only, boundaries are not counted."""
    e = engine_factory(job={"on_complete": "idle"})
    e.wait_api()
    e.wait_state(("succeeded",), timeout=60)

    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    transcripts = [(d / "output.jsonl").read_text().splitlines() for d in iters]

    raw = e.api_raw("/logs")
    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]

    # boundaries frame every iteration, in order, carrying the right fields
    boundary_pairs = []
    idx = 0
    for meta, transcript in zip(metas, transcripts):
        start = lines[idx]
        assert start["type"] == "ralphd.iteration" and start["event"] == "start"
        assert start["number"] == meta["number"]
        assert start["phase"] == meta["phase"]
        assert start["model"] == meta["model"]
        assert start["approach"] == meta["approach"]
        idx += 1
        for raw_line in transcript:
            assert lines[idx] == json.loads(raw_line)
            idx += 1
        end = lines[idx]
        assert end["type"] == "ralphd.iteration" and end["event"] == "end"
        assert end["number"] == meta["number"]
        assert end["exitCode"] == meta["exitCode"]
        assert end["error"] == meta.get("error")
        assert end["usage"] == meta.get("usage")
        idx += 1
        boundary_pairs.append((start, end))
    assert idx == len(lines)  # nothing extra, nothing missing

    total_content_lines = sum(len(t) for t in transcripts)
    assert total_content_lines >= len(iters)  # sanity: every iteration produced output

    # ?tail=N bounds transcript (non-boundary) lines only
    tail_n = 3
    tail_raw = e.api_raw(f"/logs?tail={tail_n}")
    tail_lines = [json.loads(line) for line in tail_raw.splitlines() if line.strip()]
    content = [l for l in tail_lines if l.get("type") != "ralphd.iteration"]
    assert len(content) == tail_n
    # the tailed content lines are exactly the last tail_n transcript lines
    all_content_raw = [json.loads(l) for t in transcripts for l in t]
    assert content == all_content_raw[-tail_n:]


def test_get_logs_follow_streams_across_iteration_boundaries(engine_factory):
    """GET /logs?follow=true&tail=N opened early on a slow multi-iteration
    job delivers lines from at least two different iterations on the same
    open connection, and the stream closes once the job reaches a terminal
    state (never hangs forever, never truncates before the job finishes)."""
    e = engine_factory(job={"on_complete": "idle"},
                       stub_env={"STUB_SLEEP": "1.5", "STUB_TASKS": "3"})
    e.wait_api()

    req = urllib.request.Request(
        f"http://127.0.0.1:{e.port}/logs?follow=true&tail=5")
    seen_iteration_numbers = set()
    saw_end_of_stream = False
    with urllib.request.urlopen(req, timeout=90) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") == "ralphd.iteration":
                seen_iteration_numbers.add(obj["number"])
        saw_end_of_stream = True  # the generator returned -> server closed cleanly

    assert saw_end_of_stream
    assert len(seen_iteration_numbers) >= 2
    # the stream only closed because the job had already reached a terminal
    # state by then (never truncates mid-job)
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"

    # a plain (non-follow) /logs now matches: every iteration present, in order
    full_raw = e.api_raw("/logs")
    full_numbers = [json.loads(l)["number"] for l in full_raw.splitlines()
                    if json.loads(l).get("type") == "ralphd.iteration"
                    and json.loads(l)["event"] == "start"]
    assert full_numbers == sorted(full_numbers)
    assert seen_iteration_numbers <= set(full_numbers)

    # engine stays up (idle mode); shut it down cleanly
    e.api("POST", "/shutdown")
    assert e.proc.wait(timeout=10) == 0


def test_task_in_progress_visible_while_worker_iteration_running(engine_factory):
    """The engine must surface pending -> in-progress task transitions as
    "task" events (and via GET /tasks) at the moment they happen, not only
    once the whole worker iteration has finished (PRD/task 047: an operator
    watching events/ralphctl during an iteration must see the exact task
    being worked). The stub worker writes in-progress, sleeps (simulating
    real work), then writes completed -- giving this test a real window in
    which the iteration is still running (no meta.json endedAt yet) but
    tasks.json already shows in-progress."""
    e = engine_factory(job={"on_complete": "idle"},
                       stub_env={"STUB_SLEEP": "2", "STUB_TASKS": "1"})
    e.wait_api()
    e.wait_state(("running",))

    # -- poll GET /tasks until we observe in-progress while iteration 2
    # (the first worker iteration) has not yet ended -----------------------
    deadline = time.time() + 20
    observed_in_progress_while_running = False
    while time.time() < deadline:
        _code, tasks = e.api("GET", "/tasks")
        if "tasks" not in tasks:
            time.sleep(0.1)
            continue
        statuses = {t["id"]: t["status"] for t in tasks["tasks"]}
        meta_path = e.run_dir / "iterations" / "0002" / "meta.json"
        iteration_still_running = (
            meta_path.exists()
            and "endedAt" not in json.loads(meta_path.read_text()))
        if statuses.get("001") == "in-progress" and iteration_still_running:
            observed_in_progress_while_running = True
            break
        if statuses.get("001") == "completed":
            break  # too slow to catch it -- fail below with context
        time.sleep(0.1)
    assert observed_in_progress_while_running, (
        f"never observed task 001 as in-progress while its worker iteration "
        f"was still running; last statuses seen: {statuses}")

    # -- the "task" event stream carries the transition live, before the
    # iteration.end event for the same iteration -----------------------------
    req = urllib.request.Request(f"http://127.0.0.1:{e.port}/events?since=0")
    saw_task_in_progress_idx = saw_iteration_2_end_idx = None
    with urllib.request.urlopen(req, timeout=10) as resp:
        for idx, raw in enumerate(resp):
            line = raw.decode().strip()
            if not line.startswith("data: "):
                continue
            obj = json.loads(line[6:])
            if (obj.get("type") == "task" and obj.get("taskId") == "001"
                    and obj.get("newStatus") == "in-progress"):
                saw_task_in_progress_idx = idx
            if (obj.get("type") == "iteration.end" and obj.get("number") == 2):
                saw_iteration_2_end_idx = idx
                break
    assert saw_task_in_progress_idx is not None
    assert saw_iteration_2_end_idx is not None
    assert saw_task_in_progress_idx < saw_iteration_2_end_idx

    e.wait_state(("succeeded",))
    e.api("POST", "/shutdown")
    assert e.proc.wait(timeout=10) == 0
