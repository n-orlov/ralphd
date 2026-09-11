"""Flow (task 019, requirement C.8): **the hub over a live engine** -- the
hub's own server object driven IN-PROCESS, in this test's interpreter,
against a REAL `ralphd-engine` subprocess with the stub `pi` binary on PATH,
`STUB_SLEEP=0`.

Every other hub test drives the hub one of two other ways: `tests/test_cli_ui.py`'s
`ui` fixture spawns `ralphctl ui` as a *second subprocess* and talks to it over
HTTP, or (`StubEngineApi`) mocks the live engine behind a bare
`ThreadingHTTPServer` that only ever answers the handful of routes the test
scripted. Neither exercises `ralphd.cli.ui_server`'s functions the way this
module does: constructed directly (`make_server`) and threaded inside the
SAME process that also holds the `LiveRun` handle on the real engine
subprocess underneath it -- the "in-process hub" the task asks for, wired to
a run the engine itself is still driving.

The one task's worker iteration is held open (`STUB_HOLD_FILE`/
`STUB_HOLD_PHASE="worker"`, task 007/008's primitive -- the same one
`tests/test_cli_ui.py::test_steering_post_proxies_and_creates_steering_file`
uses) so the hub's mutating action -- `POST /api/runs/<id>/steer` -- is
guaranteed to land on a run that is still genuinely RUNNING, not merely
delivered before the process happens to finish. Run detail and the run-list's
task counts are read from the hub, live, both before and after the hold is
released, and checked against the engine's own on-disk `tasks.json` tally
(`ralphd.engine.state.task_counts`/`format_task_fraction`) so the hub's number
is provably the engine's own, not a second guess.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from ralphd.cli.ui_server import make_server, run_list
from ralphd.engine.state import format_task_fraction, task_counts

from tests.conftest import free_port, wait_hold_entered
from tests.flows.flowlib import assert_steps_include, flow_steps

# Subsequence, not exact: unlike the happy path's own reference sequence,
# a steering message pending across a review pass adds a
# `review -> worker` bounce (task 014's own C.3 path) whose exact shape
# depends on whether the steer hat's settlement runs before or after this
# module's own held worker iteration is released -- what THIS flow owns is
# that the hub's own mutating write reaches the run (`steering:received`)
# somewhere between the run starting and the run ending, not the steer
# hat's full consumed/settled mechanics (task 014 already owns those).
HUB_LIVE_ORDERED_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",
    "steering:received:001-flow019.md",
    "state:succeeded",
]


class _InProcessHub:
    """The hub server, constructed and served IN-PROCESS (a background
    thread of this same test, not a second subprocess) -- see module
    docstring."""

    def __init__(self, registry):
        self.port = free_port()
        self.httpd = make_server(registry, "127.0.0.1", self.port)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def get(self, path: str) -> tuple[int, dict]:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read())

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST", data=data)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _engine_task_counts(run) -> dict:
    doc = json.loads((run.run_dir / "tasks.json").read_text())
    return task_counts(doc.get("tasks", []))


def test_hub_reads_and_steers_a_real_engine_in_process(tmp_path, live):
    hold_file = tmp_path / "hold-flow019"
    run = live(run_id="flow-hub-live",
              job={"iterations": 12, "max_approaches": 3, "on_complete": "idle"},
              stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "0",
                       "STUB_HOLD_FILE": str(hold_file),
                       "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()
    # `wait_api()` only proves the HTTP server is up; it says nothing about
    # how far the run itself has gotten. `_engine_task_counts()`/the hub's
    # own run-detail read below need the planning iteration to have already
    # ingested its plan into `tasks.json`, and the ordered-steps assertion
    # at the bottom needs `iteration:worker` to have already landed in
    # `events.jsonl` before this test's own steer POST can legally follow
    # it. `iteration.start` (loop.py) is emitted before the stub `pi`
    # child is even spawned, and that child writes
    # `<hold_file>.stub-hold-entered` as the FIRST act inside its own hold
    # (`_apply_hold`, tests/stub-pi/pi) -- strictly after both of the above
    # -- so waiting for that marker (`wait_hold_entered`, tests/conftest.py,
    # the same CAUSE-and-OBSERVE primitive `test_e2e.py`/`test_durations.py`
    # already use to avoid this exact class of race) is the barrier this
    # test was missing.
    wait_hold_entered(hold_file)

    hub = _InProcessHub(run.registry)
    try:
        # -- run detail, live, while the worker iteration is held open ----
        code, detail = hub.get(f"/api/runs/{run.run_id}")
        assert code == 200, detail
        assert detail["runId"] == run.run_id
        assert detail["live"] is True, (
            f"flow hub live: run detail should read the engine live while "
            f"it is still up, got {detail}")
        assert detail["status"].get("state") == "running", detail["status"]

        # -- task counts: the hub's run-list row must equal the engine's own
        # tally over its own tasks.json, not a second guess ---------------
        engine_counts = _engine_task_counts(run)
        rows = {r["runId"]: r for r in run_list(run.registry)}
        row = rows[run.run_id]
        assert row["tasksTotal"] == engine_counts.get("total", 0) == 1, row
        assert row["tasksDisplay"] == format_task_fraction(engine_counts), row

        # -- the one mutating hub action: POST /api/runs/<id>/steer, proxied
        # straight through to the run's own live `POST /steering` --------
        code, resp = hub.post(f"/api/runs/{run.run_id}/steer",
                              {"message": "hub steering (flow 019)",
                               "name": "flow019"})
        assert code == 202, (
            f"flow hub live: the hub's steer POST should be accepted (202), "
            f"got {code}: {resp}")
        steering_files = list((run.run_dir / "steering").glob("*.md"))
        assert steering_files, (
            "flow hub live: the hub's steer POST never wrote a steering file "
            "into the run's own directory")
        assert any("hub steering (flow 019)" in f.read_text()
                   for f in steering_files), steering_files
    finally:
        hold_file.touch()
        st = run.wait_terminal(timeout=60)
        assert st.get("state") == "succeeded", (
            f"flow hub live: the run should still reach 'succeeded' after "
            f"the hub's steer POST and the hold's release, got "
            f"{st.get('state')!r} (status.json: {st})")

        # -- run detail and task counts again, now that the run is over --
        code, detail = hub.get(f"/api/runs/{run.run_id}")
        assert code == 200, detail
        assert detail["status"].get("state") == "succeeded", detail["status"]

        final_counts = _engine_task_counts(run)
        rows = {r["runId"]: r for r in run_list(run.registry)}
        row = rows[run.run_id]
        assert row["tasksCompleted"] == final_counts.get("completed", 0) == 1, row
        assert row["tasksDisplay"] == format_task_fraction(final_counts), row

        assert_steps_include(flow_steps(run), HUB_LIVE_ORDERED_STEPS,
                             flow="flow hub live")

        hub.close()
