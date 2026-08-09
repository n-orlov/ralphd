"""Task 011: Run.add_steering (RunDir.add_steering) must never produce a
doubled sequence prefix like ``022-019-steering.md`` when the caller's
``--name`` already carries its own ``NNN-`` prefix (e.g. copy-pasted from a
previous steering filename)."""

from ralphd.engine.state import RunDir


def test_plain_name_gets_single_engine_prefix(tmp_path):
    run = RunDir(tmp_path)
    fname = run.add_steering("hello", name="steering")
    assert fname == "001-steering.md"


def test_name_with_existing_seq_prefix_is_not_doubled(tmp_path):
    run = RunDir(tmp_path)
    # First steering file occupies seq 001, so a second call assigns 002.
    run.add_steering("first", name="steering")
    fname = run.add_steering("second", name="019-steering")
    assert fname == "002-steering.md"
    assert "019" not in fname


def test_bare_name_without_prefix_is_used_verbatim(tmp_path):
    run = RunDir(tmp_path)
    fname = run.add_steering("hi", name="my-note")
    assert fname == "001-my-note.md"


def test_default_name_when_none_given(tmp_path):
    run = RunDir(tmp_path)
    fname = run.add_steering("hi")
    assert fname == "001-steering.md"
