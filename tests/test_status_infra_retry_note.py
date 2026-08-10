"""Task 001a criterion 4 (surface it): while an infra-fault retry is
backing off, `currentIteration.note` carries a human-readable "retrying
after infra fault..." message. This must be visible via plain
`ralphctl status` (not just `--json`), and the hub run-detail page must
render `currentIteration.note` too (checked via the JSON the hub consumes,
since app.js itself isn't exercised by the plain pytest tier).
"""

from __future__ import annotations

import json
import time


def test_ralphctl_status_shows_infra_retry_note_during_backoff(live):
    # Planning succeeds normally; the first worker invocation hangs with
    # zero LLM traffic (STUB_INFRA_HANG_COUNT=1), gets killed by the
    # startup watchdog, is classified as an infra fault, and the engine
    # backs off for a few seconds before retrying -- during that backoff
    # window `ralphctl status` must show the note.
    run = live(run_id="infra-retry-note", job={"iterations": 6},
               stub_env={
                   "STUB_TASKS": "1",
                   "STUB_INFRA_HANG_SKIP": "1",
                   "STUB_INFRA_HANG_COUNT": "1",
                   "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
                   "RALPHD_INFRA_RETRY_MAX": "3",
                   "RALPHD_INFRA_RETRY_BACKOFF_S": "5,5,5",
               })
    run.wait_api()

    deadline = time.time() + 20
    seen_human, seen_json = False, False
    while time.time() < deadline and not (seen_human and seen_json):
        jres = run.ralphctl("--json", "status", run.run_id)
        if jres.returncode == 0:
            try:
                status = json.loads(jres.stdout)
            except json.JSONDecodeError:
                status = {}
            cur_it = status.get("currentIteration") or {}
            note = cur_it.get("note") or ""
            if "retrying after infra fault" in note:
                seen_json = True
                human = run.ralphctl("status", run.run_id)
                assert human.returncode == 0, (human.stdout, human.stderr)
                assert "retrying after infra fault" in human.stdout
                assert "note:" in human.stdout
                seen_human = True
        time.sleep(0.3)

    assert seen_json, "currentIteration.note never showed a retry message in --json status"
    assert seen_human, "plain `ralphctl status` never rendered the retry note"

    status = run.wait_terminal(timeout=30)
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
