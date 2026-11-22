"""v0.38 C: an operator status ruling the engine would refuse is refused by
`ralphctl amend --status` when it is recorded, and one the engine refuses
later (the task moved after recording) stays visible.

C.1 (the door): `amend <run> <task> --status S` loads the authoritative task
record (`<config dir>/record/tasks.json`; never the run dir's mirror, not even when the record is unreadable: then the live engine is asked, and if it cannot answer nothing is recorded) and asks the engine's own legality
check (`state.task_status_change_refusal`: the transition table, the operator
exits of A.2 and the cure-reopened guard of A.3). An illegal ruling exits 2
with the engine's reason and writes no ruling file; every legal case is
recorded. The legal-ruling tests are the controls: they pass with or without
the check.

C.2 (a later refusal): the engine's apply-time refusal emits
`amendment.refused` (ruling file, task, reason); `amend --list` shows that
ruling as `refused: <reason>`; `ralphctl status` prints one line naming it
until a later ruling for that task is applied.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_cli_docker import ctl  # noqa: F401  (fixture)

from ralphd.engine import loop as loop_mod
from ralphd.engine.state import (AMENDMENT_REFUSED_EVENT, atomic_write_json,
                                 outstanding_refused_rulings, write_amendment)
from tests.conftest import make_supervisor

RUN = "tst-door"
ILLEGAL_REASON = "illegal transition"


def _seed(ctl, tasks, *, vigilant=False, pre_cure=None, mirror=None):
    """A run on the host: authoritative record under the config dir, the run
    dir's `tasks.json` mirror (a different status when `mirror` is given),
    `job.yaml` for the variant, and `status.json`."""
    cdir = ctl.registry / "configs" / RUN
    rdir = ctl.registry / "runs" / RUN
    (cdir / "record").mkdir(parents=True)
    rdir.mkdir(parents=True)
    atomic_write_json(cdir / "record" / "tasks.json",
                      {"version": 1, "tasks": tasks})
    atomic_write_json(rdir / "tasks.json",
                      {"version": 1, "tasks": mirror or tasks})
    (cdir / "job.yaml").write_text(
        f"vigilant: {'true' if vigilant else 'false'}\n")
    status = {"runId": RUN, "state": "paused"}
    if pre_cure is not None:
        status["preCureStatuses"] = pre_cure
    atomic_write_json(rdir / "status.json", status)
    return cdir, rdir


def _task(status, tid="001"):
    return {"id": tid, "title": "t", "status": status,
            "successCriteria": "c", "validationAttempts": 0}


def _ruling_files(cdir: Path) -> list[str]:
    adir = cdir / "amendments"
    return sorted(p.name for p in adir.glob("*")) if adir.is_dir() else []


# -- C.1: refused at the door -------------------------------------------------

@pytest.mark.parametrize("vigilant", [False, True])
@pytest.mark.parametrize("current,target", [("completed", "pending"),
                                            ("in-progress", "pending"),
                                            ("petitioned", "completed")])
def test_an_illegal_ruling_is_refused_and_no_file_is_written(
        ctl, current, target, vigilant):
    cdir, _ = _seed(ctl, [_task(current)], vigilant=vigilant)
    res = ctl.run("amend", RUN, "001", "--status", target, "because")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert ILLEGAL_REASON in res.stderr
    assert current in res.stderr and target in res.stderr
    assert _ruling_files(cdir) == []


def test_a_pending_to_validated_ruling_without_a_cure_reopen_is_refused_with_the_engines_reason(
        ctl):
    cdir, _ = _seed(ctl, [_task("pending")], vigilant=True)
    res = ctl.run("amend", RUN, "001", "--status", "validated", "done")
    assert res.returncode == 2
    assert "cure-reconciliation" in res.stderr
    assert _ruling_files(cdir) == []


def test_a_failed_to_skipped_ruling_with_no_justification_is_refused(ctl):
    cdir, _ = _seed(ctl, [_task("failed")])
    res = ctl.run("amend", RUN, "001", "--status", "skipped")
    assert res.returncode == 2
    assert "justification" in res.stderr
    assert _ruling_files(cdir) == []


def test_the_door_reads_the_authoritative_record_not_the_run_dir_mirror(ctl):
    # record: completed (completed -> pending is illegal); mirror: failed.
    cdir, _ = _seed(ctl, [_task("completed")], mirror=[_task("in-progress")])
    res = ctl.run("amend", RUN, "001", "--status", "pending", "again")
    assert res.returncode == 2
    assert "'completed'" in res.stderr
    assert _ruling_files(cdir) == []


def _deny_record(cdir: Path, how: str):
    """Make the authoritative record unreadable to this (host) user the way
    a live run does (`record/` root-owned 0700: here, mode 000 on our own
    directory), or by damaging it. Returns the undo."""
    rec_dir = cdir / "record"
    if how == "denied":
        if os.geteuid() == 0:
            pytest.skip("root reads through mode 000")
        rec_dir.chmod(0)
        return lambda: rec_dir.chmod(0o700)
    (rec_dir / "tasks.json").write_text("{not json")
    return lambda: None


class _RealEngine:
    """The run's real engine (`create_app` over a real `RunDir`, production
    code both sides) on a real loopback port: the host's `host.json` points
    at it, so the CLI asks it exactly as it asks a live container. Its own
    run dir holds the authoritative record; the host-side `record/` is held
    unreadable and the registry's mirror is stale."""

    def __init__(self, tmp_path: Path, tasks, *, vigilant=False,
                 pre_cure=None):
        import asyncio
        import threading
        import time

        import uvicorn

        from ralphd.engine.api import create_app
        from ralphd.engine.config import JobConfig
        from ralphd.engine.loop import LoopSupervisor
        from ralphd.engine.state import RunDir
        from tests.conftest import free_port

        self.run = RunDir(root=tmp_path / "engine-run")
        atomic_write_json(self.run.tasks_file, {"version": 1, "tasks": tasks})
        if pre_cure is not None:
            self.run.update_status(preCureStatuses=pre_cure)
        cfg = JobConfig(run_id=RUN, vigilant=vigilant, max_approaches=3,
                        infra_retry_backoff_s=[0.0],
                        infra_retry_backoff_max_s=0.0)
        loop = LoopSupervisor(cfg, self.run, tmp_path / "engine-ws")
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(
            create_app(cfg, self.run, loop), host="127.0.0.1",
            port=self.port, log_level="warning"))
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()), daemon=True)
        self._thread.start()
        deadline = time.time() + 5
        while not getattr(self.server, "started", False):
            assert time.time() < deadline, "engine API never came up"
            time.sleep(0.01)

    def point_host_at_it(self, rdir: Path):
        (rdir / "host.json").write_text(json.dumps(
            {"runId": RUN, "port": self.port,
             "apiUrl": f"http://127.0.0.1:{self.port}"}))

    def stop(self):
        self.server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def live_engine(ctl, tmp_path):
    """Seed a run whose record is unreadable from the host, whose mirror is
    stale, and whose live engine holds the real record. Call the returned
    function with the engine's tasks; yields `(cdir, undo)` via it."""
    started: list = []

    def make(engine_tasks, mirror_tasks, *, how="denied", vigilant=False,
             pre_cure=None):
        cdir, rdir = _seed(ctl, mirror_tasks, vigilant=vigilant)
        eng = _RealEngine(tmp_path, engine_tasks, vigilant=vigilant,
                          pre_cure=pre_cure)
        started.append(eng)
        eng.point_host_at_it(rdir)
        undo = _deny_record(cdir, how)
        started.append(undo)
        make.engine = eng
        return cdir

    yield make
    for x in started:
        (x.stop if hasattr(x, "stop") else x)()


@pytest.mark.parametrize("how", ["denied", "damaged"])
def test_a_denied_record_and_a_stale_mirror_still_refuse_an_illegal_ruling_with_the_engines_reason(
        ctl, live_engine, how):
    # Engine's record: completed (completed -> pending is illegal). The
    # mirror is stale (pending): were it believed, the ruling would be
    # allowed. The host cannot read the record, so the engine decides.
    cdir = live_engine([_task("completed")], [_task("pending")], how=how)
    res = ctl.run("amend", RUN, "001", "--status", "pending", "again")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert ILLEGAL_REASON in res.stderr and "'completed'" in res.stderr
    assert "nothing was recorded" in res.stderr
    assert _ruling_files(cdir) == []


def test_a_denied_record_never_lets_a_stale_mirror_refuse_a_legal_ruling(
        ctl, live_engine):
    # Engine: petitioned (-> skipped is a legal operator exit, A.2); the
    # mirror says pending (pending -> skipped would be refused).
    cdir = live_engine([_task("petitioned")], [_task("pending")])
    res = ctl.run("amend", RUN, "001", "--status", "skipped", "moot")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert len(_ruling_files(cdir)) == 1


@pytest.mark.parametrize("vigilant", [False, True])
def test_a_denied_record_records_the_legal_a3_ruling_on_a_cure_reopened_task(
        ctl, live_engine, vigilant):
    # The engine holds the pre-cure map, the mirror's status.json does not.
    cdir = live_engine([_task("pending")], [_task("completed")],
                       vigilant=vigilant, pre_cure={"001": "validated"})
    res = ctl.run("amend", RUN, "001", "--status", "validated", "restore it")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert len(_ruling_files(cdir)) == 1


def test_a_denied_record_refuses_the_same_ruling_without_the_cure_reopen(
        ctl, live_engine):
    cdir = live_engine([_task("pending")], [_task("completed")],
                       vigilant=True)
    res = ctl.run("amend", RUN, "001", "--status", "validated", "restore it")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "cure-reconciliation" in res.stderr
    assert _ruling_files(cdir) == []


def test_a_denied_record_applies_the_vigilant_table_the_engine_runs_under(
        ctl, live_engine):
    # `awaiting-validation -> validated` is a vigilant-only edge: the host
    # has no way to read job.yaml's variant off the engine, the engine's own
    # config decides.
    cdir = live_engine([_task("awaiting-validation")], [_task("pending")],
                       vigilant=False)
    res = ctl.run("amend", RUN, "001", "--status", "validated", "ok")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert _ruling_files(cdir) == []


def test_a_denied_record_without_a_reachable_engine_records_nothing(ctl):
    # No host.json at all: nobody can supply a decision.
    cdir, _ = _seed(ctl, [_task("petitioned")])
    undo = _deny_record(cdir, "denied")
    try:
        res = ctl.run("amend", RUN, "001", "--status", "skipped", "moot")
    finally:
        undo()
    assert res.returncode != 0, (res.stdout, res.stderr)
    assert "nothing was recorded" in res.stderr
    assert _ruling_files(cdir) == []


def test_a_denied_record_with_a_dead_engine_api_records_nothing(ctl):
    from test_cli_ui import StubEngineApi, _write_run_with_api
    cdir, _ = _seed(ctl, [_task("petitioned")])
    engine = StubEngineApi()
    _write_run_with_api(ctl.registry, "tmp-unused", engine, state="running")
    engine.close()  # the recorded apiUrl now refuses connections
    (ctl.registry / "runs" / RUN / "host.json").write_text(json.dumps(
        {"runId": RUN, "apiUrl": f"http://127.0.0.1:{engine.port}"}))
    undo = _deny_record(cdir, "denied")
    try:
        res = ctl.run("amend", RUN, "001", "--status", "skipped", "moot")
    finally:
        undo()
    assert res.returncode == 4, (res.stdout, res.stderr)
    assert _ruling_files(cdir) == []


def test_a_denied_record_with_an_engine_lacking_the_route_records_nothing(ctl):
    from test_cli_ui import StubEngineApi, _write_run_with_api
    cdir, _ = _seed(ctl, [_task("petitioned")])
    engine = StubEngineApi()   # answers 404 for every route it does not know
    try:
        _write_run_with_api(ctl.registry, "tmp-unused", engine, state="running")
        (ctl.registry / "runs" / RUN / "host.json").write_text(json.dumps(
            {"runId": RUN, "apiUrl": f"http://127.0.0.1:{engine.port}"}))
        undo = _deny_record(cdir, "denied")
        try:
            res = ctl.run("amend", RUN, "001", "--status", "skipped", "moot")
        finally:
            undo()
    finally:
        engine.close()
    assert res.returncode != 0, (res.stdout, res.stderr)
    assert "nothing was recorded" in res.stderr
    assert _ruling_files(cdir) == []


def _break_engine_record(eng: "_RealEngine", how: str, *, cached: bool):
    """Damage the live engine's OWN authoritative record (the host-side
    `record/` is already unreadable). `cached=True` lets the engine read the
    good record first, so its reader holds a last-good payload it would fall
    back on; `cached=False` clears that payload, so the reader has nothing."""
    from ralphd.engine import state as state_mod
    if cached:
        assert eng.run.read_tasks_result().source == "file"
    else:
        with state_mod._tasks_last_good_lock:
            state_mod._tasks_last_good.pop(
                os.path.abspath(eng.run.tasks_file), None)
        (eng.run.root / state_mod.TASKS_LAST_GOOD_NAME).unlink(missing_ok=True)
    rec = eng.run.tasks_file
    if how == "garbage":
        rec.write_text("{not json")
    elif how == "wrong-shape":     # valid JSON, not a record object
        rec.write_text(json.dumps([_task("pending")]))
    elif how == "no-task-list":    # an object that holds no task list
        rec.write_text(json.dumps({"version": 1, "tasks": "001"}))
    elif how == "non-utf8":        # bytes that are not UTF-8 text at all
        rec.write_bytes(b'{"version": 1, "tasks": [\xff\xfe]}')
    elif how == "denied":          # the engine's own read fails with EACCES
        if os.geteuid() == 0:
            pytest.skip("root reads through mode 000")
        rec.chmod(0)
        return lambda: rec.chmod(0o600)
    return lambda: None


@pytest.mark.parametrize("how,cached", [
    # The finding's own two cases:
    ("garbage", False),            # unreadable, no cache: was 200 current:null
    ("garbage", True),             # last-good cache: was 200 from stale data
    # Same rule, cases the finding did not list:
    ("wrong-shape", True),
    ("no-task-list", False),
    ("denied", False),
    ("non-utf8", False),           # was a 500 out of the reader
    ("non-utf8", True),
])
@pytest.mark.parametrize("target,reason", [("skipped", "moot"),
                                           ("pending", "again")])
def test_an_engine_that_cannot_read_its_own_record_current_supplies_no_decision(
        ctl, live_engine, how, cached, target, reason):
    # Engine's record says petitioned (-> skipped legal, -> pending legal
    # too: both are operator exits) -- but the engine can no longer read it
    # current. Neither a stale cache nor an empty read may decide: the CLI
    # fails and records nothing, for a ruling that would be legal and for
    # one that would not alike.
    cdir = live_engine([_task("petitioned")], [_task("petitioned")])
    undo = _break_engine_record(live_engine.engine, how, cached=cached)
    try:
        res = ctl.run("amend", RUN, "001", "--status", target, reason)
    finally:
        undo()
    assert res.returncode != 0, (res.stdout, res.stderr)
    assert "nothing was recorded" in res.stderr
    assert "task record unreadable" in res.stderr
    assert _ruling_files(cdir) == []


def test_an_unknown_id_on_an_unreadable_engine_record_is_not_forced_through(
        ctl, live_engine):
    # `--force` records a ruling for an id the record does not hold -- but
    # only when the engine KNOWS the record does not hold it. An unreadable
    # record yields no such knowledge.
    cdir = live_engine([_task("petitioned")], [_task("petitioned")])
    undo = _break_engine_record(live_engine.engine, "garbage", cached=False)
    try:
        res = ctl.run("amend", RUN, "017", "--status", "skipped", "moot",
                      "--force")
    finally:
        undo()
    assert res.returncode != 0, (res.stdout, res.stderr)
    assert _ruling_files(cdir) == []


def _serve_json_once(body: dict):
    """A stand-in engine that answers every GET with `body` (200)."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("body", [
    # An answer that does not say it was read current off the record.
    {"taskId": "001", "status": "skipped", "current": "petitioned",
     "allowed": True, "reason": ""},
    {"taskId": "001", "status": "skipped", "authoritative": False,
     "current": None, "allowed": False, "reason": "no matching task"},
    {"taskId": "001", "status": "skipped", "authoritative": "yes",
     "current": "petitioned", "allowed": True, "reason": ""},
])
def test_an_engine_answer_not_marked_authoritative_records_nothing(ctl, body):
    cdir, rdir = _seed(ctl, [_task("petitioned")])
    srv = _serve_json_once(body)
    try:
        (rdir / "host.json").write_text(json.dumps(
            {"runId": RUN, "apiUrl": f"http://127.0.0.1:{srv.server_port}"}))
        undo = _deny_record(cdir, "denied")
        try:
            res = ctl.run("amend", RUN, "001", "--status", "skipped", "moot")
        finally:
            undo()
    finally:
        srv.shutdown()
    assert res.returncode != 0, (res.stdout, res.stderr)
    assert "nothing was recorded" in res.stderr
    assert _ruling_files(cdir) == []


def test_the_engines_status_ruling_route_answers_503_on_an_unreadable_record(
        tmp_path):
    import urllib.error
    import urllib.request
    eng = _RealEngine(tmp_path, [_task("petitioned")])
    try:
        url = (f"http://127.0.0.1:{eng.port}/tasks/001/status-ruling"
               "?status=skipped")
        with urllib.request.urlopen(url, timeout=5) as r:
            assert json.loads(r.read())["authoritative"] is True
        for cached in (True, False):
            undo = _break_engine_record(eng, "garbage", cached=cached)
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(url, timeout=5)
            undo()
            assert err.value.code == 503
            body = json.loads(err.value.read())
            assert "task record unreadable" in json.dumps(body)
            atomic_write_json(eng.run.tasks_file,
                              {"version": 1, "tasks": [_task("petitioned")]})
        # A record that does not exist (no plan yet) is a fact, not
        # ignorance: authoritative, the id simply absent.
        eng.run.tasks_file.unlink()
        with urllib.request.urlopen(url, timeout=5) as r:
            got = json.loads(r.read())
        assert (got["authoritative"], got["found"], got["current"]) == (
            True, False, None)
    finally:
        eng.stop()


def test_the_engines_status_ruling_route_decides_and_writes_nothing(tmp_path):
    eng = _RealEngine(tmp_path, [_task("completed"), _task("failed", "002")])
    try:
        before = (eng.run.tasks_file.read_bytes(),
                  len(list(eng.run.root.rglob("*"))))
        import urllib.request

        def ask(task, status, reason=""):
            url = (f"http://127.0.0.1:{eng.port}/tasks/{task}/status-ruling"
                   f"?status={status}&reason={reason}")
            with urllib.request.urlopen(url, timeout=5) as r:
                return json.loads(r.read())

        no = ask("001", "pending")
        assert (no["authoritative"], no["allowed"], no["current"],
                no["reason"]) == (True, False, "completed", ILLEGAL_REASON)
        assert ask("001", "completed")["allowed"] is True   # no-op
        assert ask("002", "skipped")["allowed"] is False    # no justification
        assert ask("002", "skipped", "moot")["allowed"] is True
        unknown = ask("404", "skipped")
        assert (unknown["found"], unknown["current"]) == (False, None)
        assert ask("001", "pending")["found"] is True
        assert before == (eng.run.tasks_file.read_bytes(),
                          len(list(eng.run.root.rglob("*"))))
    finally:
        eng.stop()


# -- A task whose status is not a status string is a task, not an unknown id --

def _odd(status, tid="017"):
    return {**_task("pending", tid), "status": status}


@pytest.mark.parametrize("how", ["denied", "damaged"])
@pytest.mark.parametrize("target", ["pending", "skipped"])
@pytest.mark.parametrize("force", [False, True])
def test_a_null_status_task_on_the_engines_record_is_refused_not_treated_as_unknown(
        ctl, live_engine, how, target, force):
    # The finding's case: the engine's record holds task 017 with a null
    # status (the applier refuses every move out of it); the host record is
    # unreadable and the mirror is stale (pending). The engine says
    # `allowed: false`; the CLI must not read `current: null` as "unknown
    # id" and record the ruling -- not even with --force, which overrides
    # plan membership only, never legality.
    cdir = live_engine([_odd(None)], [_task("pending", "017")], how=how)
    args = ["amend", RUN, "017", "--status", target, "because"]
    res = ctl.run(*args, *(["--force"] if force else []))
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert ILLEGAL_REASON in res.stderr
    assert "nothing was recorded" in res.stderr
    assert _ruling_files(cdir) == []


# Same rule, cases the finding did not list: a status that is a number, a
# list or an object (an unhashable value used to crash the legality check:
# a 500 from the route, a TypeError in the applier).
@pytest.mark.parametrize("odd", [5, ["completed"], {"s": "completed"}, ""])
def test_a_non_string_status_on_the_engines_record_is_refused_with_the_engines_reason(
        ctl, live_engine, odd):
    cdir = live_engine([_odd(odd)], [_task("pending", "017")])
    res = ctl.run("amend", RUN, "017", "--status", "skipped", "moot")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert ILLEGAL_REASON in res.stderr
    assert _ruling_files(cdir) == []


@pytest.mark.parametrize("odd", [None, 7, ["completed"]])
def test_a_non_string_status_on_a_readable_record_is_refused_too(ctl, odd):
    cdir, _ = _seed(ctl, [_odd(odd)], mirror=[_task("pending", "017")])
    res = ctl.run("amend", RUN, "017", "--status", "skipped", "moot")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert ILLEGAL_REASON in res.stderr
    assert _ruling_files(cdir) == []


@pytest.mark.parametrize("odd", [None, 5, ["completed"], {"s": 1}])
def test_the_applier_refuses_a_ruling_on_a_non_string_status_without_crashing(
        tmp_path, odd):
    from ralphd.engine.state import RunDir, apply_task_status_change
    run = RunDir(root=tmp_path / "r")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [_odd(odd)]})
    got = apply_task_status_change(run, "017", "skipped", False,
                                   source="operator", reason="moot")
    assert (got["applied"], got["reason"]) == (False, ILLEGAL_REASON)
    assert run.read_tasks()["tasks"][0]["status"] == odd


# -- An id the authoritative record does not hold needs --force ---------------

def test_an_id_only_the_stale_mirror_names_is_refused_without_force_on_the_engine_path(
        ctl, live_engine):
    cdir = live_engine([_task("pending")], [_task("pending"),
                                           _task("petitioned", "017")])
    res = ctl.run("amend", RUN, "017", "--status", "skipped", "moot")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "--force" in res.stderr and "nothing was recorded" in res.stderr
    assert _ruling_files(cdir) == []
    # --force records it: the engine KNOWS its record does not hold 017.
    res = ctl.run("amend", RUN, "017", "--status", "skipped", "moot",
                  "--force")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert len(_ruling_files(cdir)) == 1


def test_an_id_only_the_stale_mirror_names_is_refused_without_force_on_a_readable_record(
        ctl):
    cdir, _ = _seed(ctl, [_task("pending")],
                    mirror=[_task("pending"), _task("petitioned", "017")])
    res = ctl.run("amend", RUN, "017", "--status", "skipped", "moot")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "record/tasks.json" in res.stderr
    assert _ruling_files(cdir) == []


# -- The route: 503 for bytes that are not text, and a read that writes nothing

def test_the_route_answers_503_not_500_on_a_non_utf8_record(tmp_path):
    import urllib.error
    import urllib.request
    eng = _RealEngine(tmp_path, [_task("petitioned")])
    try:
        url = (f"http://127.0.0.1:{eng.port}/tasks/001/status-ruling"
               "?status=skipped&reason=moot")
        for raw in (b"\xff\xfe\x00{", "{\"tasks\": []}".encode("utf-16"),
                    b'{"version": 1, "tasks": [{"id": "\xe9"}]}'):
            for cached in (True, False):
                atomic_write_json(eng.run.tasks_file,
                                  {"version": 1, "tasks": [_task("petitioned")]})
                _break_engine_record(eng, "garbage", cached=cached)
                eng.run.tasks_file.write_bytes(raw)
                with pytest.raises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(url, timeout=5)
                assert err.value.code == 503, raw
                assert "task record unreadable" in err.value.read().decode()
    finally:
        eng.stop()


@pytest.mark.parametrize("how", ["garbage", "non-utf8", "wrong-shape"])
def test_the_route_writes_no_last_good_cache_on_a_broken_record(tmp_path, how):
    import urllib.error
    import urllib.request

    from ralphd.engine import state as state_mod
    eng = _RealEngine(tmp_path, [_task("petitioned")])
    cache = eng.run.root / state_mod.TASKS_LAST_GOOD_NAME
    try:
        _break_engine_record(eng, how, cached=True)   # warm in-memory cache
        cache.unlink(missing_ok=True)
        before = sorted(p.name for p in eng.run.root.rglob("*"))
        url = (f"http://127.0.0.1:{eng.port}/tasks/001/status-ruling"
               "?status=skipped&reason=moot")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(url, timeout=5)
        assert err.value.code == 503
        assert not cache.exists()
        assert sorted(p.name for p in eng.run.root.rglob("*")) == before
        # An existing cache is left byte-for-byte as it was, too.
        cache.write_text('{"version": 1, "tasks": []}')
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url, timeout=5)
        assert cache.read_text() == '{"version": 1, "tasks": []}'
    finally:
        eng.stop()


def test_the_route_reports_a_null_status_task_as_found_and_refused(tmp_path):
    import urllib.request
    eng = _RealEngine(tmp_path, [_odd(None)])
    try:
        url = (f"http://127.0.0.1:{eng.port}/tasks/017/status-ruling"
               "?status=pending")
        with urllib.request.urlopen(url, timeout=5) as r:
            got = json.loads(r.read())
        assert (got["authoritative"], got["found"], got["current"],
                got["allowed"], got["reason"]) == (
            True, True, None, False, ILLEGAL_REASON)
    finally:
        eng.stop()


def test_the_task_reader_treats_non_utf8_bytes_as_unparseable(tmp_path):
    from ralphd.engine.state import read_tasks_doc
    (tmp_path / "tasks.json").write_bytes(b"\xff\xfe{")
    res = read_tasks_doc(tmp_path, attempts=1, persist=False)
    assert (res.source, res.stale) == ("unreadable", True)
    assert "UnicodeDecodeError" in res.error
    assert not (tmp_path / ".tasks-last-good.json").exists()


def test_without_a_record_mount_the_door_reads_the_run_dirs_protected_record(
        ctl):
    # No `record/` mount: the engine keeps the record in the run dir's
    # `.tasks-protected/` (privsep's degrade), which beats the mirror.
    cdir, rdir = _seed(ctl, [_task("completed")], mirror=[_task("failed")])
    (cdir / "record" / "tasks.json").unlink()
    (cdir / "record").rmdir()
    (rdir / ".tasks-protected").mkdir()
    atomic_write_json(rdir / ".tasks-protected" / "tasks.json",
                      {"version": 1, "tasks": [_task("completed")]})
    res = ctl.run("amend", RUN, "001", "--status", "pending", "again")
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert "'completed'" in res.stderr
    assert _ruling_files(cdir) == []


@pytest.mark.parametrize("vigilant", [False, True])
@pytest.mark.parametrize("target", ["validated", "skipped"])
def test_control_a_petitioned_task_ruling_exit_is_recorded(
        ctl, target, vigilant):
    """A.2: `petitioned -> validated|skipped` by an operator ruling."""
    cdir, _ = _seed(ctl, [_task("petitioned")], vigilant=vigilant)
    res = ctl.run("amend", RUN, "001", "--status", target, "operator ruling")
    assert res.returncode == 0, res.stderr
    assert len(_ruling_files(cdir)) == 1


@pytest.mark.parametrize("vigilant", [False, True])
def test_control_a_ruling_on_a_cure_reopened_task_is_recorded(ctl, vigilant):
    """A.3: `pending -> validated` on a task a cure pass re-opened."""
    cdir, _ = _seed(ctl, [_task("pending")], vigilant=vigilant,
                    pre_cure={"001": "validated"})
    res = ctl.run("amend", RUN, "001", "--status", "validated", "restore it")
    assert res.returncode == 0, res.stderr
    assert len(_ruling_files(cdir)) == 1


def test_control_a_ruling_to_the_status_the_task_holds_is_recorded(ctl):
    cdir, _ = _seed(ctl, [_task("skipped")])
    res = ctl.run("amend", RUN, "001", "--status", "skipped", "already so")
    assert res.returncode == 0, res.stderr
    assert len(_ruling_files(cdir)) == 1


# -- C.2: a refusal the engine makes later is visible --------------------------

def _engine_with_refused_ruling(ctl, monkeypatch, *, task_status="pending"):
    """A ruling recorded while it was legal (`petitioned -> skipped`), after
    which the task moved: the engine refuses it when it applies it."""
    cdir, rdir = _seed(ctl, [_task("petitioned")])
    fname = write_amendment(cdir, "001", "stand it down", status="skipped")
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    sup = make_supervisor(ctl.tmp, run_id=RUN, run_root=rdir, tasks=[
        _task(task_status)], vigilant=False,
        notifier_url="http://notifier.example/notify")
    sup._apply_operator_status_rulings()
    return sup, cdir, rdir, fname


def _events(rdir: Path, kind: str) -> list[dict]:
    path = rdir / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in rows if e.get("type") == kind]


def test_a_ruling_the_engine_refuses_at_apply_time_emits_amendment_refused(
        ctl, monkeypatch):
    sup, _cdir, rdir, fname = _engine_with_refused_ruling(ctl, monkeypatch)
    refused = _events(rdir, AMENDMENT_REFUSED_EVENT)
    assert AMENDMENT_REFUSED_EVENT == "amendment.refused"
    assert len(refused) == 1, refused
    assert refused[0]["file"] == fname
    assert refused[0]["taskId"] == "001"
    assert refused[0]["reason"]
    assert refused[0]["previous"] == "pending"
    # reported once, not once per boundary
    sup._apply_operator_status_rulings()
    assert len(_events(rdir, AMENDMENT_REFUSED_EVENT)) == 1
    assert sup.run.read_tasks()["tasks"][0]["status"] == "pending"


def test_control_an_applied_ruling_emits_no_amendment_refused(ctl, monkeypatch):
    _sup, _cdir, rdir, _f = _engine_with_refused_ruling(
        ctl, monkeypatch, task_status="petitioned")
    assert _events(rdir, AMENDMENT_REFUSED_EVENT) == []
    assert len(_events(rdir, "amendment_status_applied")) == 1


def test_amend_list_shows_the_refused_ruling_with_the_engines_reason(
        ctl, monkeypatch):
    _sup, _cdir, rdir, fname = _engine_with_refused_ruling(ctl, monkeypatch)
    reason = _events(rdir, AMENDMENT_REFUSED_EVENT)[0]["reason"]
    res = ctl.run("amend", RUN, "--list")
    assert res.returncode == 0, res.stderr
    assert f"refused: {reason}" in res.stdout
    doc = json.loads(ctl.run("--json", "amend", RUN, "--list").stdout)
    assert doc["entries"][0]["refused"] == reason


def test_control_amend_list_shows_no_refusal_for_an_applied_ruling(
        ctl, monkeypatch):
    _engine_with_refused_ruling(ctl, monkeypatch, task_status="petitioned")
    res = ctl.run("amend", RUN, "--list")
    assert res.returncode == 0, res.stderr
    assert "refused:" not in res.stdout


def test_status_prints_one_line_naming_the_refused_ruling(ctl, monkeypatch):
    _sup, _cdir, rdir, fname = _engine_with_refused_ruling(ctl, monkeypatch)
    reason = _events(rdir, AMENDMENT_REFUSED_EVENT)[0]["reason"]
    res = ctl.run("status", RUN)
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in res.stdout.splitlines() if fname in ln]
    assert len(lines) == 1, res.stdout
    assert "001" in lines[0] and reason in lines[0]
    doc = json.loads(ctl.run("--json", "status", RUN).stdout)
    assert [r["file"] for r in doc["refusedRulings"]] == [fname]


def test_status_names_the_refusal_until_a_later_ruling_for_the_task_is_applied(
        ctl, monkeypatch):
    sup, cdir, rdir, fname = _engine_with_refused_ruling(ctl, monkeypatch)
    assert [r["file"] for r in outstanding_refused_rulings(rdir)] == [fname]
    # the operator rules again, legally from where the task now stands
    write_amendment(cdir, "001", "hand it back", status="in-progress")
    sup._apply_operator_status_rulings()
    assert sup.run.read_tasks()["tasks"][0]["status"] == "in-progress"
    assert outstanding_refused_rulings(rdir) == []
    res = ctl.run("status", RUN)
    assert fname not in res.stdout
    assert json.loads(ctl.run("--json", "status", RUN).stdout)[
        "refusedRulings"] == []


def _engine_with_pending_task(ctl, monkeypatch, *, pre_cure=None):
    cdir, rdir = _seed(ctl, [_task("pending")], pre_cure=pre_cure)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    status = {"preCureStatuses": pre_cure} if pre_cure else None
    sup = make_supervisor(ctl.tmp, run_id=RUN, run_root=rdir, tasks=[
        _task("pending")], vigilant=False, status=status,
        notifier_url="http://notifier.example/notify")
    return sup, cdir, rdir


def _status_clear(ctl, fname):
    res = ctl.run("status", RUN)
    assert res.returncode == 0, res.stderr
    assert fname not in res.stdout, res.stdout
    assert json.loads(ctl.run("--json", "status", RUN).stdout)[
        "refusedRulings"] == []


def test_status_stays_clear_when_the_engine_retries_a_superseded_ruling(
        ctl, monkeypatch):
    """The later ruling ends the warning for good: the engine re-attempts the
    older ruling at every boundary, and refusing it again from the task's new
    status does not bring the warning back."""
    sup, cdir, rdir = _engine_with_pending_task(ctl, monkeypatch)
    old = write_amendment(cdir, "001", "call it done", status="validated")
    sup._apply_operator_status_rulings()
    assert [r["file"] for r in outstanding_refused_rulings(rdir)] == [old]
    write_amendment(cdir, "001", "hand it back", status="in-progress")
    sup._apply_operator_status_rulings()
    assert sup.run.read_tasks()["tasks"][0]["status"] == "in-progress"
    # the next boundary retries `old` from in-progress, still illegal
    sup._apply_operator_status_rulings()
    assert sup.run.read_tasks()["tasks"][0]["status"] == "in-progress"
    assert outstanding_refused_rulings(rdir) == []
    assert len(_events(rdir, AMENDMENT_REFUSED_EVENT)) == 1
    _status_clear(ctl, old)


def test_a_refusal_logged_again_after_a_later_ruling_applied_stays_cleared(ctl):
    """The reader alone: a refusal of an older ruling file written after a
    later ruling for that task applied is still superseded."""
    _cdir, rdir = _seed(ctl, [_task("in-progress")])
    rows = [
        {"type": AMENDMENT_REFUSED_EVENT, "file": "001-001.md",
         "taskId": "001", "previous": "pending", "status": "validated",
         "reason": ILLEGAL_REASON},
        {"type": "amendment_status_applied", "file": "002-001.md",
         "taskId": "001", "previous": "pending", "status": "in-progress",
         "source": "operator"},
        {"type": AMENDMENT_REFUSED_EVENT, "file": "001-001.md",
         "taskId": "001", "previous": "in-progress", "status": "validated",
         "reason": ILLEGAL_REASON},
    ]
    (rdir / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    assert outstanding_refused_rulings(rdir) == []
    _status_clear(ctl, "001-001.md")


def test_an_earlier_ruling_applied_after_a_later_refusal_does_not_clear_it(ctl):
    """An earlier ruling re-applied after a later one was refused does not
    clear the later one: it is not a later ruling."""
    _cdir, rdir = _seed(ctl, [_task("in-progress")])
    rows = [
        {"type": AMENDMENT_REFUSED_EVENT, "file": "002-001.md",
         "taskId": "001", "previous": "pending", "status": "validated",
         "reason": ILLEGAL_REASON},
        {"type": "amendment_status_applied", "file": "001-001.md",
         "taskId": "001", "previous": "pending", "status": "in-progress",
         "source": "operator"},
    ]
    (rdir / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    assert [r["file"] for r in outstanding_refused_rulings(rdir)] == [
        "002-001.md"]


def test_a_later_cure_reopen_ruling_clears_the_refusal(ctl, monkeypatch):
    """A later ruling that restores a cure-reopened task (A.3) takes effect as
    `task.un_reopened` (source operator, naming its ruling file) and ends the
    warning about the older refused ruling."""
    sup, cdir, rdir = _engine_with_pending_task(
        ctl, monkeypatch, pre_cure={"001": "validated"})
    old = write_amendment(cdir, "001", "send it to verify",
                          status="awaiting-validation")
    sup._apply_operator_status_rulings()
    assert [r["file"] for r in outstanding_refused_rulings(rdir)] == [old]
    later = write_amendment(cdir, "001", "restore it", status="validated")
    sup._apply_operator_status_rulings()
    sup._apply_operator_status_rulings()
    assert sup.run.read_tasks()["tasks"][0]["status"] == "validated"
    restored = _events(rdir, "task.un_reopened")
    assert [(e["source"], e["file"]) for e in restored] == [
        ("operator", later)]
    assert outstanding_refused_rulings(rdir) == []
    _status_clear(ctl, old)


def test_control_status_prints_no_refusal_line_for_a_run_without_one(ctl):
    _seed(ctl, [_task("petitioned")])
    res = ctl.run("status", RUN)
    assert res.returncode == 0, res.stderr
    assert "was refused" not in res.stdout
