"""Task 013 (v0.23 requirement A.2): the suite's own isolation from a ralphd
JOB container's root-owned task-record mount.

Inside a ralphd job container `/var/lib/ralphd/record`
(`privsep.DEFAULT_TASK_RECORD_DIR`) already exists, root-owned 0700, put
there by the engine running that job for its own authoritative record.
`privsep.task_record_dir()` resolves it truthfully -- `is_dir()` needs only
`x` on the 0755 parent -- so every `RunDir.write_tasks()`/`read_tasks()` in
this suite aimed at it and died with `PermissionError` on
`.tasks-last-good.json` at the agent uid, taking with it both the in-process
supervisor fixtures and (before it could bind a port) every spawned live
engine. `tests/conftest.py::_isolate_ambient_task_record_dir` neutralises
that by pointing `$RALPHD_TASK_RECORD_DIR` at a path that does not exist --
the state a developer host is in, where the degrade to the run-dir location
(`privsep.legacy_protected_tasks_dir`) is what privsep.py itself documents
for "the test suite's own unprivileged engine".

The predicate deciding whether to fire is pinned here in both directions
over `tmp_path` stand-ins, so neither half depends on a root-owned directory
being to hand (CI runners and developer hosts have none). The fixture's own
effect is pinned by the outcome it exists for: whatever this process's
ambient environment is, `privsep.task_record_dir()` never resolves to a
directory this uid cannot write.
"""

from __future__ import annotations

import os

from ralphd.engine import privsep
from tests.conftest import ambient_task_record_dir_unusable


def test_a_missing_default_is_not_the_leaking_shape(tmp_path):
    """A developer host / CI runner: nothing ever created the directory, so
    `task_record_dir()` already returns None and there is nothing to
    isolate."""
    assert ambient_task_record_dir_unusable(tmp_path / "never-created") is False


def test_a_usable_default_is_left_alone(tmp_path):
    record = tmp_path / "record"
    record.mkdir()
    assert ambient_task_record_dir_unusable(record) is False


def test_an_existing_but_unwritable_default_is_the_leaking_shape(tmp_path):
    """The job-container shape, reproduced without root: a directory that
    exists but is closed to this uid."""
    record = tmp_path / "record"
    record.mkdir(mode=0o500)
    try:
        assert ambient_task_record_dir_unusable(record) is True
    finally:
        record.chmod(0o700)


def test_a_file_where_a_directory_belongs_is_not_the_leaking_shape(tmp_path):
    """`is_dir()` is false for a regular file, and `task_record_dir()` returns
    None for it -- a degrade, not a permission wall."""
    plain = tmp_path / "record"
    plain.write_text("not a directory")
    assert ambient_task_record_dir_unusable(plain) is False


def test_the_record_dir_this_process_resolves_is_never_one_it_cannot_write():
    """The fixture's purpose, asserted as an outcome rather than as an env
    value: in THIS process, whatever the ambient container did, the record
    directory the engine would write through is either absent (the documented
    run-dir degrade) or genuinely writable here.

    Green on a developer host and on a CI runner because nothing created the
    default there; green inside a ralphd job container because the autouse
    fixture pointed the override at an absent path. Red in a job container
    with the fixture removed -- which is the regression it exists to stop.
    """
    resolved = privsep.task_record_dir()
    if resolved is None:
        return
    assert os.access(resolved, os.R_OK | os.W_OK | os.X_OK), resolved
