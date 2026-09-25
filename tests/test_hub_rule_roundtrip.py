"""Task 010 (v0.26 PRD requirement 4, #270): the hub's ruling textarea and
`ralphctl rule --file --name ...` are two DIFFERENT entry points into the
SAME engine route (`POST /rule`, `state.write_ruling`), so a multi-paragraph
markdown ruling sent through either one must land on disk byte-identical to
the other.

Fails at 9b72b700 for an unrelated reason first (`#rule-text` was a 4-row
textarea there, pinned separately by
`test_hub_steer_textarea_and_rule_rows.py`); this module's own roundtrip
assertion is new coverage, not a behaviour that regressed.
"""

from __future__ import annotations

from ralphd.engine.state import ruling_entries

from test_cli_ui import ui  # noqa: F401

__all__ = ["ui"]

MULTI_PARAGRAPH_MARKDOWN = """\
# Ruling

The rejection on `T-042` is overruled for this approach.

## Reasoning

- the finding's own evidence quotes a line the diff never touched
- the clause it cites was already satisfied by a prior commit

```python
assert True  # the finding is stale
```

Resume review over the same approach.
"""


def test_rule_hub_and_cli_land_the_same_text_in_the_ruling_record(
        tmp_path, live, ui):
    """A multi-paragraph markdown ruling sent through the hub's `POST
    /api/runs/<id>/rule` and the identical text sent via `ralphctl rule
    --file --name ...` must both be readable back off `<run>/rulings/`
    byte-identical -- via the ONE shared reader (`state.ruling_entries`),
    which already strips each entry's own `signature:`/`ts:` header and
    trailing newline, so the comparison is exactly the ruling TEXT the
    engine recorded, not the header that necessarily differs (a fresh
    `ts:` per write).
    """
    hold_file = tmp_path / "hold-rule-roundtrip"
    run = live(run_id="rulert", job={"iterations": 12, "max_approaches": 3,
                                     "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()

    server = ui(run.registry)
    code, resp = server.post(f"/api/runs/{run.run_id}/rule",
                              {"text": MULTI_PARAGRAPH_MARKDOWN,
                               "name": "via-hub"})
    assert code == 202, resp

    rule_file = tmp_path / "ruling.md"
    rule_file.write_text(MULTI_PARAGRAPH_MARKDOWN)
    cli_res = run.ralphctl("rule", run.run_id, "--file", str(rule_file),
                           "--name", "via-cli")
    assert cli_res.returncode == 0, cli_res.stderr

    hold_file.touch()
    run.wait_terminal()

    entries = {}
    for e in ruling_entries(run.run_dir):
        # `file` is `NNN-<name>.md`; strip the engine-assigned prefix the
        # same way `write_ruling`'s own docstring names it, to key by the
        # `--name`/`name` this test actually sent.
        stem = e["file"].removesuffix(".md")
        name = stem.split("-", 1)[1] if "-" in stem else stem
        entries[name] = e

    assert "via-hub" in entries, entries.keys()
    assert "via-cli" in entries, entries.keys()

    hub_text = entries["via-hub"]["text"]
    cli_text = entries["via-cli"]["text"]

    expected = MULTI_PARAGRAPH_MARKDOWN.rstrip()
    assert hub_text == expected, hub_text
    assert cli_text == expected, cli_text
    assert hub_text == cli_text
