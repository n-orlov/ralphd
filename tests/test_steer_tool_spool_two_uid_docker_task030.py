"""Docker tier: the steer tool spool across the REAL uid boundary (task 030,
requirement G.2, #130).

`tests/test_steer_tool_spool_two_uid_task030.py` drives the same spool at the
fast lane's subprocess tier -- a real engine process, a real steer-hat process
and a real `python -m ralphd.engine.hat_tools` client process -- but there
"the agent uid" and "the owner of `tasks.json`" are necessarily the SAME uid:
this repo's own suite runs unprivileged, with no root to separate from
(`tests/test_engine_uid_boundary.py`'s documented limit). That tier proves the
process boundary and the spool protocol crossing it; it structurally cannot
prove the ownership half.

This module is the other half, and it is the only place the whole property can
be true at once: the shipped `container/Dockerfile` (built and run exactly as
`tests/test_docker_sibling_e2e.py` builds and runs it, whose fixtures and
cleanup discipline this file reuses by import) starts the engine as root, the
engine drops to the `agent` uid for every child it spawns
(`privsep.agent_child_kwargs()`), and task 105 (#60 requirement D) makes
`tasks.json` root-owned so that no iteration can write it directly. So here:

* the steer hat -- and the hat-tools client it spawns -- really run as uid
  1000, and really CANNOT write `tasks.json` (the stub records its own
  credentials and a non-destructive `open(tasks.json, "r+")` probe in
  `.stub-steer-uid.json`; the errno is EACCES);
* the request the hat writes into `<run>/hat-tools/requests/` is answered, and
  the corresponding `tasks.json` write performed, by the ENGINE's own process
  as the owner of that file (root-owned on disk afterwards) -- with the
  applier's forced fields (`origin=steer`) in place of what the hat asked for,
  and an illegal status jump in the same batch refused;
* a malformed request dropped straight into the spool is answered `ok: false`
  and does not end the run.

One container run serves all three tests (a module-scoped fixture): the three
assertions are three views of one real run, and a second and third build+run
would cost minutes for nothing. `--on-complete idle` keeps the container alive
so the agent-uid write attempt can be made from inside it, as the exact uid
every iteration runs as -- ownership seen from this container would prove
nothing about the container's own permission check.
"""

from __future__ import annotations

import errno
import json
import subprocess
from types import SimpleNamespace

import pytest
from test_docker_sibling_e2e import (  # noqa: F401  (fixtures used by name)
    REAL_DOCKER,
    Ctl,
    _container_running,
    _unique_run_id,
    _wait_status,
    gateway_ip,
    pytestmark,
    test_image,
    work_root,
)

# Asked for by the hat, and every one of them refused or overridden by the
# applier on the engine's side of the spool: `origin`/`status` are stamped by
# `state.STEER_HAT_TOOLS["task-insert"]` (task 010, requirement H, #98), and
# `pending -> validated` is not a legal transition for a non-vigilant run.
SPOOLED_CALLS = [
    {"tool": "task-insert",
     "args": {"task": {"id": "spooled-1", "title": "via the spool",
                       "status": "completed", "origin": "attacker"}}},
    {"tool": "task-update",
     "args": {"taskId": "spooled-1", "changes": {"status": "validated"}}},
]


@pytest.fixture(scope="module")
def spool_run(work_root, gateway_ip, test_image):
    """One real job container, steered once, driven to its terminal state.

    Its own unique run id (never this job's `RALPHD_RUN_ID`), and teardown
    force-removes that exact container name -- never a label query, which
    could match this very job's container.
    """
    ctl = Ctl(work_root, gateway_ip, test_image)
    run_id = _unique_run_id("e130-spool")
    # task 009 (B.3): STUB_HOLD_FILE (a container-side path under
    # /workspace, mounted from work_root/ws below) holds the first worker
    # iteration -- after it has flipped its task to in-progress, the same
    # position `tests/stub-pi/pi`'s own `_apply_hold("worker")` uses --
    # deterministically wide enough for the `ralphctl steer` call below to
    # land while a worker iteration remains, instead of racing a fixed
    # STUB_SLEEP window across two tasks against how fast the run finishes.
    # Released (touched) right after the steer call; the shared hold file
    # then passes every later worker invocation (including the second
    # task's) through instantly, per the primitive's own documented
    # once-released-stays-released behaviour.
    hold_rel = ".hold-spool-steer"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id,
                    extra_env={"STUB_TASKS": "2",
                               "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                               "STUB_HOLD_PHASE": "worker",
                               "STUB_HOLD_TIMEOUT_S": "60",
                               "STUB_STEER_TOOL_CALLS": json.dumps(SPOOLED_CALLS),
                               "STUB_STEER_MALFORMED_REQUEST": "1"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    # Steer while worker iterations remain: the `worker -> steer` edge is
    # taken when unsettled steering exists, so a message arriving after the
    # last task is done would never reach the steer hat at all. The hold
    # above (not yet released) guarantees a worker iteration is still
    # blocked, mid-run, when this call is made. Sent through the real
    # `ralphctl steer` (the operator's own channel), not an internal import.
    _wait_status(meta["apiUrl"], lambda s: s.get("state") == "running", timeout=60)
    # `ralphctl steer <run> <message>` -- the message is POSITIONAL (there is
    # no `--message` flag; `--file`/stdin are the alternatives).
    steered = ctl.run("steer", run_id, "insert a task through the spool",
                      "--name", "spool")
    assert steered.returncode == 0, steered.stderr
    hold_file_host.touch()  # release: let the held worker iteration proceed
    status = _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                          ("succeeded", "failed", "aborted"), timeout=180)
    yield SimpleNamespace(ctl=ctl, run_id=run_id, container=f"ralphd-{run_id}",
                          rdir=ctl.registry / "runs" / run_id, status=status,
                          api=meta["apiUrl"])
    ctl.force_cleanup()


def _tasks(run) -> list[dict]:
    return json.loads((run.rdir / "tasks.json").read_text())["tasks"]


def test_the_hat_uid_cannot_write_tasks_json_and_the_engine_owns_it(spool_run):
    """The premise the whole spool exists for, measured on a live container:
    the hat's process is uid 1000, `tasks.json` is root's, and a write from
    uid 1000 -- attempted from inside the container, as the exact uid every
    iteration runs as -- is refused by the kernel."""
    probe_path = spool_run.rdir / ".stub-steer-uid.json"
    assert probe_path.is_file(), "the steer hat never ran in this container"
    # written by the hat's own process, so its owner IS the hat's uid
    assert probe_path.stat().st_uid == 1000, probe_path.stat().st_uid
    probe = json.loads(probe_path.read_text())
    assert probe["uid"] == probe["euid"] == 1000, probe
    assert probe["tasksOpenRwErrno"] == errno.EACCES, (
        "the steer hat's own process could open tasks.json for writing "
        f"(errno {probe['tasksOpenRwErrno']!r}): there is no uid boundary "
        "here, so nothing this module asserts about the spool would mean "
        "the engine performed the write")

    tasks_path = spool_run.rdir / "tasks.json"
    before = json.loads(tasks_path.read_text())
    assert tasks_path.stat().st_uid == 0, (
        f"tasks.json is owned by uid {tasks_path.stat().st_uid}, not root -- "
        "task 105 (#60 requirement D) makes it root-owned precisely so the "
        "agent uid cannot write its own task record")

    denied = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", spool_run.container,
         "sh", "-c", "echo '{\"tasks\": []}' > /run/ralphd/tasks.json; echo RC=$?"],
        capture_output=True, text=True, timeout=15)
    assert denied.returncode == 0, denied.stderr  # docker exec itself worked
    assert "RC=0" not in denied.stdout, denied.stdout  # the shell write failed
    combined = denied.stdout + denied.stderr
    assert "Permission denied" in combined, combined
    assert json.loads(tasks_path.read_text()) == before  # untouched


def test_the_spooled_request_is_applied_by_the_engine_not_by_the_agent_uid(spool_run):
    """The hat asked for `status: completed` and `origin: attacker` on the
    inserted task, then for an illegal `pending -> validated` jump. What came
    back is the APPLIER's answer -- stamped `origin=steer`, the jump refused
    with the transition validator's own diagnostic -- and the file that
    changed is the root-owned one the hat cannot write. A hat that could
    write `tasks.json` itself would simply have what it asked for."""
    responses_path = spool_run.rdir / ".stub-steer-tool-responses.json"
    assert responses_path.is_file(), "the hat's client subprocess wrote no answers"
    assert responses_path.stat().st_uid == 1000, responses_path.stat().st_uid
    responses = json.loads(responses_path.read_text())
    assert len(responses) == 2, responses
    assert responses[0]["ok"] is True, responses[0]
    assert responses[0]["tool"] == "task-insert", responses[0]
    assert responses[1]["ok"] is False, responses[1]
    assert "status change refused" in responses[1]["error"], responses[1]

    inserted = next((t for t in _tasks(spool_run) if t["id"] == "spooled-1"), None)
    assert inserted is not None, _tasks(spool_run)
    assert inserted["origin"] == "steer", inserted
    # `status` is deliberately not asserted to `pending` (the applier's other
    # forced field): the ordinary worker lifecycle picks the new task up and
    # completes it through the normal, LEGAL transition afterwards. What must
    # never appear is the illegal jump's target.
    assert inserted["status"] != "validated", inserted

    # The engine's own event log -- written by the engine's process, never the
    # hat's -- recorded the call as served, naming the steering that caused it.
    events = [json.loads(line) for line
              in (spool_run.rdir / "events.jsonl").read_text().splitlines()]
    served = [ev for ev in events if ev.get("type") == "steer.tool_call"]
    assert any(ev["tool"] == "task-insert" and ev["ok"] is True
               and "001-spool.md" in ev.get("steering", []) for ev in served), served

    # Nothing left outstanding: the broker unlinks every request it serves.
    assert list((spool_run.rdir / "hat-tools" / "requests").glob("*.json")) == []


def test_a_malformed_spool_request_is_refused_without_ending_the_run(spool_run):
    """The same steer iteration also dropped one hand-written, unparseable
    file straight into the spool (never through `hat_tools.call()`). It is
    answered `ok: false` -- `SteerToolBroker.serve_once()`'s `except (OSError,
    json.JSONDecodeError)` branch -- and the run still reaches its ordinary
    terminal, verified state with its steering settled and its container up."""
    answer_path = spool_run.rdir / "hat-tools" / "responses" / "stub-malformed.json"
    assert answer_path.is_file(), "the malformed request was never answered"
    answer = json.loads(answer_path.read_text())
    assert answer["ok"] is False, answer
    assert "unreadable" in answer["error"], answer

    assert spool_run.status["state"] == "succeeded", spool_run.status
    assert spool_run.status["verdict"] == "verified", spool_run.status
    assert _container_running(spool_run.container) is True

    consumed = json.loads(
        (spool_run.rdir / "steering" / ".consumed.json").read_text())
    assert "001-spool.md" in consumed, consumed
