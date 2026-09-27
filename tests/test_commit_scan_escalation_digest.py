"""Cure task cure-01-01-8 (PRD requirement A.1/D.1, DoD 1, #269):
cure-01-05-2 widened every OTHER real operator-escalation pause to the
same bounded digest contract (`_pause_escalation_digest_text`), but the
task-boundary commit-secret-scan pause (`_pause_for_commit_secret_hit`,
task 030, requirement I.3, #210) predates that contract and was never
folded into it -- at 9b72b700 it posts only a fixed sentence naming the
reason and pointing at the `commit_secret_scan` event, with no run id,
approach/max, spend, hub URL, honest unavailable-field disclosure or
working answer command. This module drives the REAL production pause
(`_pause_for_commit_secret_hit`) over a harmless `commit_secret_scan`
event (a fabricated sha/part/variable name -- never a real secret value
or a secret-bearing commit) through the real notifier POST seam, and
asserts the delivered text carries every digest field plus both live-run
answer levers, while retry/marker delivery semantics (three attempts,
`notification.escalation_undelivered`, the CLI/row/page markers and their
post-answer removal) are unchanged from the pre-existing coverage they
are copied from (`test_escalation_undelivered_marker.py`).
"""
import json
import pytest
from ralphd.engine import hat_tools
from ralphd.engine.loop import MAX_NOTIFY_CHARS
from ralphd.cli import main as cli_main, ui_server
from test_escalation_undelivered_marker import (
    _supervisor, RUN_ID, _ralphctl_status, ESCALATION_UNDELIVERED_MARKER,
)


def trigger(tmp_path, monkeypatch, *, fail=False):
    registry=tmp_path/'registry'
    monkeypatch.setenv('RALPHD_REGISTRY',str(registry))
    monkeypatch.setattr(cli_main,'_dangling_run_entry',lambda run_id:None)
    sent=[]
    def poster(url,text,timeout_s):
        sent.append(text)
        return (503 if fail else 200),None
    monkeypatch.setattr(hat_tools,'_http_post_text',poster)
    sup=_supervisor(tmp_path,run_root=f'registry/runs/{RUN_ID}',
                    notifier_url='http://notifier.invalid/notify',
                    hub_base_url='https://hub.example',max_approaches=2)
    sup._approach=1
    sup.run.update_status(state='running',phase='worker',approach=1,maxApproaches=2)
    sup.run.emit(sup.COMMIT_SECRET_SCAN_EVENT,sha='0'*40,part='blob',variable='REVIEW_TEST_VARIABLE',pushed=False)
    sup._sleep_before_escalation_retry=lambda seconds:None
    sup._pause_for_commit_secret_hit()
    return registry,sup,sent


def test_task_boundary_operator_escalation_has_digest_and_answer_command(tmp_path,monkeypatch):
    registry,sup,sent=trigger(tmp_path,monkeypatch)
    text=sent[0]
    required=[RUN_ID,'pauseReason: operator_input_needed','approach 1 of 2',
              'blocking:','residual:','tasks reopened:','tasks inserted:','spent this approach:',
              'https://hub.example/#/run/'+RUN_ID,
              'ralphctl unpause '+RUN_ID,'ralphctl rule '+RUN_ID]
    missing=[s for s in required if s not in text]
    print('COMMIT_SCAN_DIGEST',repr(text),'MISSING',missing)
    assert sup.run.read_status()['pauseReason']=='operator_input_needed'
    assert len(sent)==1
    assert len(text)<=MAX_NOTIFY_CHARS
    assert not missing


@pytest.mark.parametrize('surface',['cli','row','page'])
def test_task_boundary_undelivered_marker_visible_until_answered(tmp_path,monkeypatch,capsys,surface):
    registry,sup,sent=trigger(tmp_path,monkeypatch,fail=True)
    events=[json.loads(l) for l in (sup.run.root/'events.jsonl').read_text().splitlines()]
    assert len(sent)==3
    assert len([e for e in events if e['type']=='notification.escalation_undelivered'])==1
    assert len([e for e in events if e['type']=='notification.failed'])==3
    assert sup.run.read_status()['state']=='paused'
    def value():
        if surface=='cli':
            out=_ralphctl_status(capsys)
            return next(l for l in out.splitlines() if l.startswith('state:'))
        if surface=='row':
            return next(r for r in ui_server.run_list(registry) if r['runId']==RUN_ID)
        return ui_server.run_detail(registry,RUN_ID)['status']
    before=value()
    sup.resume()
    after=value()
    print('COMMIT_SCAN_MARKER',surface,'ATTEMPTS',len(sent),'BEFORE',before,'AFTER',after)
    if surface=='cli':
        assert ESCALATION_UNDELIVERED_MARKER not in after
        assert ESCALATION_UNDELIVERED_MARKER in before
    else:
        assert after['escalationUndelivered'] is False
        assert before['escalationUndelivered'] is True
