"""Task 057 (#2): the hub's task-detail dialog -- rendering discipline.

The behavioural half (clicking a task row opens a dialog carrying that task's
status/criteria/dependsOn/priority) lives in tests/test_browser_hub.py's
`test_run_detail_opens_a_task_in_a_dialog`, in the `browser` tier. This module
holds the greppable invariants that must hold even when that tier is skipped:

  * the task detail reuses task 056's `openTextDialog` (one dialog
    implementation, one <dialog> alive at a time) rather than a second one;
  * task rows are the affordance (`tr.task-row`, keyboard-reachable);
  * the dialog text is inserted as TEXT NODES only -- a task's
    `successCriteria` is agent-authored prose full of backticks and `<`.
"""

from __future__ import annotations

from pathlib import Path

import ralphd.cli.ui_server as ui_mod

APP_JS = (Path(ui_mod.__file__).parent / "web" / "app.js").read_text()
STYLE_CSS = (Path(ui_mod.__file__).parent / "web" / "style.css").read_text()


def test_task_dialog_reuses_the_single_text_dialog_implementation():
    assert "function openTaskDialog(" in APP_JS
    assert "function taskDialogText(" in APP_JS
    # exactly one dialog builder, and the task dialog goes through it
    assert APP_JS.count("function openTextDialog(") == 1
    block = APP_JS.split("function openTaskDialog(")[1].split("\n}")[0]
    assert "openTextDialog(" in block, block


def test_task_rows_are_clickable_and_keyboard_reachable():
    rows = APP_JS.split("function renderTasks(")[1].split("\nfunction ")[0]
    assert "openTaskDialog(t)" in rows, rows
    assert 'class: "task-row"' in rows, rows
    assert 'tabindex: "0"' in rows, rows
    assert "onkeydown" in rows and 'ev.key === "Enter"' in rows, rows
    assert ".task-row" in STYLE_CSS


def test_task_dialog_text_uses_text_nodes_only():
    block = APP_JS.split("function taskDialogText(")[1].split("\n// ----")[0]
    for forbidden in ("innerHTML", "html:", "insertAdjacentHTML"):
        assert forbidden not in block, \
            f"task dialog rendering must not use {forbidden}: {block}"


def test_task_dialog_text_carries_the_plan_fields():
    """The dialog must name the fields an operator came for -- not just the
    title the row already showed."""
    block = APP_JS.split("function taskDialogText(")[1].split("\n}")[0]
    for field in ("status:", "priority:", "dependsOn:", "successCriteria:"):
        assert field in block, f"{field!r} missing from the task dialog: {block}"
    assert "t.successCriteria" in block, block
