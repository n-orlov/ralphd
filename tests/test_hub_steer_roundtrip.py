"""Task 010 (v0.26 PRD requirement 4, #270): the hub's steer textarea and
`ralphctl steer --file` are two DIFFERENT entry points into the SAME engine
route (`POST /steering`, `RunDir.add_steering`), so a multi-paragraph
markdown steering message sent through either one must land on disk
byte-identical to the other -- never mangled by an intermediate JSON
round-trip, a CLI file read, or a hub proxy hop.

Fails at 9b72b700 for an unrelated reason first (`#steer-message` was a
one-line `<input type="text">` there, pinned separately by
`test_hub_steer_textarea_and_rule_rows.py`); this module's own roundtrip
assertion is new coverage, not a behaviour that regressed.
"""

from __future__ import annotations

from ralphd.engine.state import steering_entries

from test_cli_ui import ui  # noqa: F401

__all__ = ["ui"]

MULTI_PARAGRAPH_MARKDOWN = """\
# Steering note

Please prioritise the retry path over the fresh-clone path for the next
few iterations -- the CI lane is flaky right now and re-cloning wastes
minutes we don't have.

## Checklist

- do not raise any timeout
- keep the existing test green
- leave a note in the handoff file

```python
def retry(fn, attempts=3):
    for i in range(attempts):
        try:
            return fn()
        except TransientError:
            if i == attempts - 1:
                raise
```

That's it for now. Thanks.
"""


def test_steer_hub_and_cli_land_the_same_bytes_in_the_steering_record(
        tmp_path, live, ui):
    """A multi-paragraph markdown steer sent through the hub's `POST
    /api/runs/<id>/steer` and the identical text sent via `ralphctl steer
    --file` must both be readable back off `<run>/steering/` byte-identical
    -- the same body the engine actually received in both cases, via the
    ONE shared reader (`state.steering_entries`), never a hand-rolled file
    read that could hide a mismatch either write path introduces.
    """
    hold_file = tmp_path / "hold-steer-roundtrip"
    run = live(run_id="steerrt", job={"iterations": 12, "max_approaches": 3,
                                      "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()

    server = ui(run.registry)
    code, resp = server.post(f"/api/runs/{run.run_id}/steer",
                              {"message": MULTI_PARAGRAPH_MARKDOWN,
                               "name": "via-hub"})
    assert code == 202, resp

    steer_file = tmp_path / "steer-message.md"
    steer_file.write_text(MULTI_PARAGRAPH_MARKDOWN)
    cli_res = run.ralphctl("steer", run.run_id, "--file", str(steer_file),
                           "--name", "via-cli")
    assert cli_res.returncode == 0, cli_res.stderr

    hold_file.touch()
    run.wait_terminal()

    entries = {e["name"]: e for e in steering_entries(run.run_dir)}
    assert "via-hub" in entries, entries.keys()
    assert "via-cli" in entries, entries.keys()

    hub_body = entries["via-hub"]["body"]
    cli_body = entries["via-cli"]["body"]

    # Both writers go through `add_steering`'s `message.rstrip() + "\\n"`;
    # the byte-identical claim holds against THAT normalisation, applied
    # identically to whichever entry point sent the text.
    expected = MULTI_PARAGRAPH_MARKDOWN.rstrip() + "\n"
    assert hub_body == expected, hub_body
    assert cli_body == expected, cli_body
    assert hub_body == cli_body
