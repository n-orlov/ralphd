"""Task 040 (#6): `ralphctl logs` on a run whose API is unreachable.

The transcript an operator most wants is the one belonging to the run that
just died -- and every byte of it is already on disk in
`iterations/NNNN/output.jsonl`. Before this task, all three log modes went
straight through the container API and so failed with exit 4
("API unreachable") the moment the container was gone (or hung, for
`--follow`). Now every mode falls back to the SHARED on-disk merge
(`ralphd.log_merge.merged_lines`, task 038 -- byte-identical to what the
engine's `GET /logs` serves from the inside), exits 0, and says on STDERR
(never stdout, so `--raw` keeps its 1:1 wire contract) that what was shown
is a snapshot.

Black-box: a hand-written run dir (no engine process at all) whose
`host.json` points at a closed port, driven through the real `ralphctl`
executable.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

from tests.conftest import RALPHCTL

SNAPSHOT_NOTICE = "on-disk snapshot"


def _closed_port() -> int:
    """A port nothing listens on -- bind it, read it, close it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port


def _write_iteration(run_dir: Path, n: int, *, phase: str, texts: list[str]) -> None:
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"number": n, "phase": phase, "model": "stub-model", "approach": 1,
         "startedAt": f"2026-01-01T00:0{n}:00Z", "exitCode": 0, "error": None,
         "endedAt": f"2026-01-01T00:0{n}:30Z", "usage": {"totalTokens": 10 * n}}))
    (d / "output.jsonl").write_text("".join(
        json.dumps({"type": "message_end",
                    "message": {"content": [{"type": "text", "text": t}]}}) + "\n"
        for t in texts))


def _dead_run(tmp_path: Path, run_id: str = "deadlogs") -> tuple[Path, Path]:
    """(registry, run_dir) for a run recorded `running` whose API is down."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "running", "verdict": None,
         "phase": "worker", "approach": 1, "iterationsUsed": 2,
         "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z"}))
    (run_dir / "host.json").write_text(json.dumps(
        {"runId": run_id, "container": "d" * 12, "port": _closed_port(),
         "apiUrl": f"http://127.0.0.1:{_closed_port()}", "image": "n/a"}))
    _write_iteration(run_dir, 1, phase="planning", texts=["dead planning line"])
    _write_iteration(run_dir, 2, phase="worker", texts=["dead worker line"])
    return registry, run_dir


def _ctl(registry: Path, *argv: str, timeout: int = 30):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------------ pretty
def test_logs_pretty_on_dead_run_prints_on_disk_merge_and_exits_0(tmp_path):
    registry, _ = _dead_run(tmp_path)

    res = _ctl(registry, "logs", "deadlogs")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "Traceback" not in res.stderr
    assert "API unreachable" not in res.stderr
    assert "dead planning line" in res.stdout
    assert "dead worker line" in res.stdout
    # the merge's synthesized iteration boundaries are rendered too
    assert "iteration 1" in res.stdout and "iteration 2" in res.stdout
    # the notice is on stderr, so stdout stays a clean transcript
    assert SNAPSHOT_NOTICE in res.stderr
    assert SNAPSHOT_NOTICE not in res.stdout


def test_logs_pretty_tail_on_dead_run_trims_rendered_lines(tmp_path):
    """The tail contract (task 057: N RENDERED lines) is the same on the
    on-disk path -- the trim happens after rendering, not over raw events."""
    registry, _ = _dead_run(tmp_path)

    res = _ctl(registry, "logs", "deadlogs", "--tail", "1")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert len([line for line in res.stdout.splitlines() if line.strip()]) == 1
    assert SNAPSHOT_NOTICE in res.stderr


# ------------------------------------------------------------------ follow
def test_logs_follow_on_dead_run_prints_snapshot_and_exits_cleanly(tmp_path):
    """`-f` must neither hang waiting for a container that will never
    answer nor die on connection-refused: it prints what is on disk and
    returns, saying there is nothing to follow."""
    registry, _ = _dead_run(tmp_path)

    res = _ctl(registry, "logs", "deadlogs", "--follow", timeout=30)

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "Traceback" not in res.stderr
    assert "dead planning line" in res.stdout
    assert "dead worker line" in res.stdout
    assert SNAPSHOT_NOTICE in res.stderr
    assert "nothing to follow" in res.stderr


def test_logsf_alias_on_dead_run_behaves_like_follow(tmp_path):
    registry, _ = _dead_run(tmp_path)

    res = _ctl(registry, "logsf", "deadlogs", timeout=30)

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "dead worker line" in res.stdout
    assert SNAPSHOT_NOTICE in res.stderr


def test_logs_raw_follow_on_dead_run_prints_raw_snapshot(tmp_path):
    registry, _ = _dead_run(tmp_path)

    res = _ctl(registry, "logs", "deadlogs", "--raw", "--follow", timeout=30)

    assert res.returncode == 0, (res.stdout, res.stderr)
    lines = [line for line in res.stdout.splitlines() if line.strip()]
    assert lines and all(json.loads(line) for line in lines)
    assert SNAPSHOT_NOTICE in res.stderr


# --------------------------------------------------------------------- raw
def test_logs_raw_on_dead_run_is_the_shared_merge_verbatim(tmp_path):
    """`--raw` stays 1:1 with the wire format: stdout is exactly the shared
    merge for that run dir, nothing added (the notice is on stderr)."""
    from ralphd.log_merge import merged_lines

    registry, run_dir = _dead_run(tmp_path)

    res = _ctl(registry, "logs", "deadlogs", "--raw", "--tail", "0")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert res.stdout == "".join(merged_lines(run_dir))
    assert SNAPSHOT_NOTICE in res.stderr


def test_logs_raw_tail_on_dead_run_matches_engine_tail_semantics(tmp_path):
    """Engine-side `?tail=N` keeps the last N non-boundary lines; the
    on-disk fallback applies the same `log_merge.apply_tail`."""
    from ralphd.log_merge import merged_lines

    registry, run_dir = _dead_run(tmp_path)

    res = _ctl(registry, "logs", "deadlogs", "--raw", "--tail", "1")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert res.stdout == "".join(merged_lines(run_dir, tail=1))


# ----------------------------------------------------------------- corners
def test_logs_single_iteration_on_dead_run_reads_that_transcript(tmp_path):
    registry, _ = _dead_run(tmp_path)

    res = _ctl(registry, "logs", "deadlogs", "--iteration", "2", "--raw",
              "--tail", "0")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "dead worker line" in res.stdout
    assert "dead planning line" not in res.stdout
    assert SNAPSHOT_NOTICE in res.stderr


def test_logs_unknown_run_still_fails_with_run_not_found(tmp_path):
    """The fallback must not turn a typo into a silent empty success: no
    run dir means exit 3, the documented 'run not found' code."""
    registry = tmp_path / "registry"
    (registry / "runs").mkdir(parents=True)

    res = _ctl(registry, "logs", "no-such-run")

    assert res.returncode == 3, (res.stdout, res.stderr)
    assert "not found" in res.stderr


def test_logs_live_run_says_nothing_about_snapshots(tmp_path, live):
    """A reachable run's output is unchanged -- no notice on stderr."""
    run = live(run_id="livelogs", stub_env={"STUB_TASKS": "1"})
    run.wait_terminal()

    res = run.ralphctl("logs", run.run_id, "--tail", "0")

    assert res.returncode == 0, (res.stdout, res.stderr)
    assert SNAPSHOT_NOTICE not in res.stderr
    assert res.stdout.strip()
