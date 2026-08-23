"""Task 040: the stub's `.stub-env.json` environment marker is written
atomically, and the tests that poll it wait for a PARSABLE document.

The defect this pins was pre-existing and cost a fast lane its green: task
018's sweep (`2208 passed, 1 failed`) died in
`tests/test_llm_api.py::test_llm_env_reaches_next_pi_invocation_no_leak`
with `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char
0)` -- `<run>/.stub-env.json` existed but held zero bytes, because
`tests/stub-pi/pi` dumped it with a plain `open(path, "w")` (which
truncates before the first byte is written) while the test polled for mere
existence. Nothing about the engine was wrong; the harness was.

So this module checks both halves of the fix, and nothing about the leak
assertions those tests make -- they are unchanged and stay exactly as
strict.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_llm_api import wait_for_env_marker

REPO = Path(__file__).parent.parent
STUB_PI = REPO / "tests" / "stub-pi" / "pi"
MARKER = ".stub-env.json"


def _source() -> str:
    return STUB_PI.read_text()


def _run_stub(run_dir: Path, extra_env: dict | None = None) -> None:
    env = {
        "PATH": f"{STUB_PI.parent}:{Path(sys.executable).parent}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/tmp"),
        "STUB_RUN_DIR": str(run_dir),
        **(extra_env or {}),
    }
    res = subprocess.run([str(STUB_PI)], input="unrecognized phase prompt\n",
                         env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr


# --- the writer -------------------------------------------------------------

def test_the_env_marker_is_written_with_os_replace() -> None:
    """A whole document lands in one filesystem operation."""
    src = _source()
    assert "os.replace(_env_tmp, _env_final)" in src
    assert 'with open(marker("env.json"), "w") as _f:' not in src, \
        "the env marker is dumped straight into its final path again"


def test_the_env_markers_temp_file_is_in_the_same_directory() -> None:
    """os.replace is only atomic within one filesystem, so the temp file has
    to be derived from the final path -- not from /tmp."""
    src = _source()
    assert '_env_final = marker("env.json")' in src
    assert '_env_tmp = f"{_env_final}.tmp.{os.getpid()}"' in src


def test_the_env_markers_temp_file_is_removed_if_the_dump_fails() -> None:
    """A leftover temp file would be a second copy of the full environment
    under a name the redaction sweep in tests/test_secret_redaction.py does
    not exclude, so a failed dump must not leave one behind."""
    src = _source()
    tail = src.split('_env_final = marker("env.json")', 1)[1][:600]
    assert "os.unlink(_env_tmp)" in tail
    assert "raise" in tail, "a failed env dump must still fail the invocation"


def test_a_real_invocation_leaves_one_parsable_marker_and_no_temp(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _run_stub(run_dir, {"PROBE_MARKER_KEY": "probe-value"})

    marker = run_dir / MARKER
    assert json.loads(marker.read_text())["PROBE_MARKER_KEY"] == "probe-value"
    leftovers = [p.name for p in run_dir.iterdir()
                 if p.name.startswith(MARKER) and p.name != MARKER]
    assert leftovers == [], f"temp env dumps left behind: {leftovers}"


def test_a_concurrent_reader_never_sees_a_partial_env_marker(tmp_path) -> None:
    """The failure mode itself: a reader polling the marker while invocations
    keep re-dumping it. The dumped environment is padded so that a
    non-atomic write would take many write() calls (json.dump streams), which
    is what made the zero-byte/partial read observable on a loaded host in
    the first place.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # One env var is capped at 128KiB (MAX_ARG_STRLEN), so pad across several.
    pad_len = 100_000
    pad_env = {f"PROBE_PAD_{i}": "p" * pad_len for i in range(4)}
    marker = run_dir / MARKER

    errors: list[str] = []
    reads = {"parsed": 0}

    def dump_repeatedly() -> None:
        for i in range(6):
            _run_stub(run_dir, {**pad_env, "PROBE_ROUND": str(i)})

    writer = threading.Thread(target=dump_repeatedly)
    writer.start()
    try:
        while writer.is_alive():
            try:
                doc = json.loads(marker.read_text())
            except FileNotFoundError:
                continue
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors.append(f"partial read: {exc}")
                continue
            reads["parsed"] += 1
            assert all(len(doc.get(k, "")) == pad_len for k in pad_env), \
                "a reader saw a document whose values were not fully written"
    finally:
        writer.join(timeout=120)

    assert errors == [], errors[:3]
    assert reads["parsed"] > 0, "the reader never observed the marker at all"
    assert json.loads(marker.read_text())["PROBE_ROUND"] == "5"


# --- the readers ------------------------------------------------------------

@pytest.mark.parametrize("module", ["test_llm_api.py",
                                   "test_cli_resume_llm_wiring.py"])
def test_every_env_marker_poller_waits_for_a_parsable_document(module) -> None:
    """The two modules the criteria name must poll through the shared helper:
    no site may read the marker directly, because a direct read is exactly the
    line that raised JSONDecodeError in task 018's sweep."""
    src = (REPO / "tests" / module).read_text()
    assert "wait_for_env_marker(env_marker" in src
    assert "json.loads(env_marker" not in src, \
        f"{module} reads the env marker without waiting for a parsable document"
    assert "env_marker.exists()" not in src, \
        f"{module} still gates on mere existence of the env marker"
    if module != "test_llm_api.py":
        assert "from test_llm_api import wait_for_env_marker" in src

def test_the_reader_waits_through_a_zero_byte_marker(tmp_path) -> None:
    """Exactly the shape that reddened task 018's fast lane: the file exists
    and is empty. Waiting for existence raised; waiting for a parsable
    document waits."""
    marker = tmp_path / MARKER
    marker.write_text("")

    def fill() -> None:
        marker.write_text(json.dumps({"LATE": "yes"}))

    timer = threading.Timer(0.5, fill)
    timer.start()
    try:
        assert wait_for_env_marker(marker, timeout=20) == {"LATE": "yes"}
    finally:
        timer.cancel()


def test_the_reader_waits_for_the_predicate_not_the_first_document(tmp_path) -> None:
    marker = tmp_path / MARKER
    marker.write_text(json.dumps({"KEY": "stale"}))

    def rotate() -> None:
        marker.write_text(json.dumps({"KEY": "fresh"}))

    timer = threading.Timer(0.5, rotate)
    timer.start()
    try:
        seen = wait_for_env_marker(marker, lambda d: d.get("KEY") == "fresh",
                                   timeout=20)
    finally:
        timer.cancel()
    assert seen == {"KEY": "fresh"}


@pytest.mark.parametrize("state", ["absent", "empty", "truncated"])
def test_the_reader_returns_an_empty_document_on_timeout(tmp_path, state) -> None:
    """A miss must reach the caller's own assertion, not blow up inside the
    poller: the test's message is what tells the operator what was expected."""
    marker = tmp_path / MARKER
    if state == "empty":
        marker.write_text("")
    elif state == "truncated":
        marker.write_text('{"PARTIAL": "va')
    assert wait_for_env_marker(marker, timeout=0.4) == {}
