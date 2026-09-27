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
import re
import shlex
import subprocess
import pytest
from ralphd.engine import hat_tools, redact
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
              'answer: ralphctl unpause '+RUN_ID]
    missing=[s for s in required if s not in text]
    print('COMMIT_SCAN_DIGEST',repr(text),'MISSING',missing)
    assert sup.run.read_status()['pauseReason']=='operator_input_needed'
    assert len(sent)==1
    assert len(text)<=MAX_NOTIFY_CHARS
    assert not missing


def _advertised_commands(text):
    """Every `ralphctl ...` command the digest's `answer:` line offers, with
    its placeholder arguments kept verbatim (`<ruling.md>`, `<slug>`)."""
    line=next(l for l in text.splitlines() if l.startswith('answer: '))
    return re.findall(r'ralphctl [a-z-]+ '+re.escape(RUN_ID)+r'(?: --[a-z-]+ <[^>]+>)*',line)


def _run_ralphctl(cmd,tmp_path,monkeypatch,client):
    """Run one advertised command through the REAL `ralphctl` parser and
    `cmd_*` handler, with `cli_main.api` delivering its request to the REAL
    engine app (`create_app` over the paused supervisor) instead of a TCP
    socket -- the same route a live `ralphctl` reaches."""
    ruling=tmp_path/'ruling.md'
    ruling.write_text('the flagged commit is a false positive; proceed\n')
    argv=shlex.split(cmd.replace('<ruling.md>',str(ruling)).replace('<slug>','answer'))[1:]
    def api(run_id,method,path,body=None,**kw):
        assert run_id==RUN_ID
        resp=client.request(method,path,json=body)
        assert resp.status_code<400,(path,resp.status_code,resp.text)
        return resp.json()
    monkeypatch.setattr(cli_main,'api',api)
    args=cli_main.build_parser().parse_args(argv)
    args.func(args)


def test_every_advertised_answer_command_releases_the_commit_scan_pause(tmp_path,monkeypatch,capsys):
    """"Working answer commands": each command the digest names is run end to
    end -- real CLI handler, real engine `POST` route, real supervisor -- over
    a fresh commit-scan pause, and must leave the run `running` with no
    `pauseReason`. A command that merely looks right (a ruling this hold's
    unsigned approach never lets release it) fails here."""
    from fastapi.testclient import TestClient
    from ralphd.engine.api import create_app
    registry,sup,sent=trigger(tmp_path/'probe',monkeypatch)
    commands=_advertised_commands(sent[0])
    print('COMMIT_SCAN_ANSWER_COMMANDS',commands)
    assert commands
    for i,cmd in enumerate(commands):
        root=tmp_path/f'cmd{i}'
        root.mkdir()
        registry,sup,sent=trigger(root,monkeypatch)
        status=sup.run.read_status()
        assert (status['state'],status['pauseReason'])==('paused','operator_input_needed')
        with TestClient(create_app(sup.cfg,sup.run,sup)) as client:
            _run_ralphctl(cmd,root,monkeypatch,client)
        status=sup.run.read_status()
        print('COMMIT_SCAN_ANSWER',cmd,status['state'],status.get('pauseReason'))
        assert status['state']=='running',cmd
        assert status.get('pauseReason') is None,cmd


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


# Synthetic secret material only (PRD requirement I.9): a value made live in
# the engine's real redaction map through a secret-shaped env var NAME, with
# `home` pointed at an empty scratch dir so no real credential file is read.
SYNTHETIC_VALUE='sk-synthetic-digest-0123456789abcdefDIGEST'
SYNTHETIC_NAME='SYNTHETIC_DIGEST_API_TOKEN'


@pytest.fixture
def live_synthetic_secret(monkeypatch, tmp_path):
    fake_home=tmp_path/'fake-home'
    fake_home.mkdir()
    monkeypatch.setenv(SYNTHETIC_NAME,SYNTHETIC_VALUE)
    redact.refresh_redaction_map(home=fake_home)
    assert redact.live_redaction_map().get(SYNTHETIC_VALUE)==f'env:{SYNTHETIC_NAME}'
    try:
        yield
    finally:
        monkeypatch.delenv(SYNTHETIC_NAME,raising=False)
        redact.refresh_redaction_map(home=fake_home)


def _git(cwd,*args):
    return subprocess.run(['git',*args],cwd=cwd,check=True,capture_output=True,text=True).stdout.strip()


def test_task_boundary_digest_excludes_the_matched_secret_value_and_keeps_worker_routing(
        tmp_path,monkeypatch,live_synthetic_secret):
    """The whole production path over a REAL hit: a worker commit whose diff
    AND message carry a live (synthetic) secret value is found by
    `_scan_task_boundary_commits`, then `_pause_for_commit_secret_hit` posts
    its digest. The POSTed text names the commit sha, the part and the
    variable NAME, and never the value -- not even redacted, since the digest
    is built only off the value-free `commit_secret_scan` event. The pause
    emits no `operator_escalation` event, and a fresh engine over the same
    run dir resumes into `worker`, never into `review`."""
    ws=tmp_path/'ws'
    ws.mkdir()
    _git(ws,'init','-q')
    _git(ws,'config','user.email','t@example.com')
    _git(ws,'config','user.name','t')
    (ws/'seed.txt').write_text('seed\n')
    _git(ws,'add','-A')
    _git(ws,'commit','-q','-m','seed')
    base=_git(ws,'rev-parse','HEAD')
    (ws/'leak.txt').write_text(f'token = {SYNTHETIC_VALUE}\n')
    _git(ws,'add','leak.txt')
    _git(ws,'commit','-q','--no-verify','-m',f'add {SYNTHETIC_VALUE}')
    head=_git(ws,'rev-parse','HEAD')

    registry=tmp_path/'registry'
    monkeypatch.setenv('RALPHD_REGISTRY',str(registry))
    sent=[]
    def poster(url,text,timeout_s):
        sent.append(text)
        return 200,None
    monkeypatch.setattr(hat_tools,'_http_post_text',poster)
    kw=dict(run_root=f'registry/runs/{RUN_ID}',workspace='ws',
            notifier_url='http://notifier.invalid/notify',
            hub_base_url='https://hub.example',max_approaches=2)
    sup=_supervisor(tmp_path,**kw)
    sup._approach=1
    sup.run.update_status(state='running',phase='worker',approach=1,maxApproaches=2)
    sup.run.write_tasks({'tasks':[{'id':'t1','title':'t','successCriteria':'c',
                                   'status':'failed','priority':1}]})
    sup._sleep_before_escalation_retry=lambda seconds:None

    hit=sup._scan_task_boundary_commits(base,head)
    assert hit is not None and hit['sha']==head
    sup._pause_for_commit_secret_hit()

    assert len(sent)==1
    text=sent[0]
    assert len(text)<=MAX_NOTIFY_CHARS
    assert SYNTHETIC_VALUE not in text
    assert 'REDACTED' not in text
    assert f'sha: {head[:12]}' in text
    assert f'variable: {hit["variable"]}' in text
    assert f'part: {hit["part"]}' in text
    assert 'pauseReason: operator_input_needed' in text
    assert 'ralphctl unpause '+RUN_ID in text

    raw_events=(sup.run.root/'events.jsonl').read_text()
    assert SYNTHETIC_VALUE not in raw_events
    events=[json.loads(l) for l in raw_events.splitlines()]
    scans=[e for e in events if e['type']==sup.COMMIT_SECRET_SCAN_EVENT]
    assert len(scans)==1
    assert scans[0]['sha']==head and scans[0]['pushed'] is False
    assert scans[0]['variable']==hit['variable']
    assert not [e for e in events if e['type']=='operator_escalation']
    status=sup.run.read_status()
    assert status['state']=='paused'
    assert status['pauseReason']=='operator_input_needed'
    assert SYNTHETIC_VALUE not in json.dumps(status)
    assert sup._paused_awaiting_operator_input_needed_ruling(1) is False

    fresh=_supervisor(tmp_path,**kw)
    fresh.iterations_used=1
    approach,skip_planning,step=fresh._resume_point()
    assert (approach,skip_planning)==(1,True)
    assert step!='review'


def test_commit_scan_hold_answer_command_matches_actual_release_capability(
        tmp_path, monkeypatch):
    """Cure task cure-01-01-9 (PRD A.1/A.6, D.1; DoD 1/4): review found that
    a commit-scan hold does NOT always stay unanswerable by a ruling --
    `_pause_for_commit_secret_hit` shares the `operator_input_needed`
    reason STRING with review's own pause, and `resume_if_ruling_answers_
    pause` releases ANY `operator_input_needed` pause whose approach
    already carries a `rejectionSignatures` entry, whichever pause on that
    approach is the one currently holding. This drives the ACTUAL history
    the review probe found broken: a real `operator_input_needed` gate
    (review's own, task 014) parks first, an operator's ruling answers it
    (leaving `rejectionSignatures["1"]` recorded and the run running
    again), and only THEN does a commit-scan hit park a SECOND,
    unrelated hold on the SAME approach. The digest for that second hold
    must advertise `rule` as a real answer (not just `unpause`), and
    running it through the real CLI/engine route must actually release
    the gate -- the opposite of the fresh-approach case (`test_every_
    advertised_answer_command_releases_the_commit_scan_pause` above),
    where no signature exists yet and `rule` is rightly not advertised at
    all.
    """
    registry=tmp_path/'registry'
    monkeypatch.setenv('RALPHD_REGISTRY',str(registry))
    monkeypatch.setattr(cli_main,'_dangling_run_entry',lambda run_id:None)
    sent=[]
    def poster(url,text,timeout_s):
        sent.append(text)
        return 200,None
    monkeypatch.setattr(hat_tools,'_http_post_text',poster)
    sup=_supervisor(tmp_path,run_root=f'registry/runs/{RUN_ID}',
                    notifier_url='http://notifier.invalid/notify',
                    hub_base_url='https://hub.example',max_approaches=2)
    sup._approach=1
    sup.run.update_status(state='running',phase='review',approach=1,maxApproaches=2)
    sup._sleep_before_escalation_retry=lambda seconds:None

    # 1. A real review-side `operator_input_needed` gate parks first.
    finding={'severity':'blocking','curable':True,'clause':'requirement Z.1',
             'evidence':'ev','cured_when':'Z.1 is discharged'}
    sup._pause_and_escalate_operator_input_needed(
        1,[finding],replan_available=True,signature='sig-earlier')
    assert sup.run.read_status()['pauseReason']=='operator_input_needed'

    # 2. An operator's ruling answers it -- POST /rule through the REAL
    #    engine route -- which releases it (task 015) and leaves this
    #    approach's own rejection signature recorded on status.json.
    from fastapi.testclient import TestClient
    from ralphd.engine.api import create_app
    with TestClient(create_app(sup.cfg,sup.run,sup)) as client:
        ruling=tmp_path/'ruling.md'
        ruling.write_text('the flagged rejection is a false positive\n')
        def api(run_id,method,path,body=None,**kw):
            resp=client.request(method,path,json=body)
            assert resp.status_code<400,(path,resp.status_code,resp.text)
            return resp.json()
        monkeypatch.setattr(cli_main,'api',api)
        args=cli_main.build_parser().parse_args(
            ['rule',RUN_ID,'--file',str(ruling),'--name','answer'])
        args.func(args)
    status=sup.run.read_status()
    assert status['state']=='running' and status.get('pauseReason') is None
    assert status['rejectionSignatures']['1']=='sig-earlier'

    # 3. Only now, later in the SAME approach, a commit-scan hit parks a
    #    second, unrelated hold that inherits the approach's own signature.
    sent.clear()
    sup.run.update_status(phase='worker')
    sup.run.emit(sup.COMMIT_SECRET_SCAN_EVENT,sha='1'*40,part='blob',
                variable='LATER_VARIABLE',pushed=False)
    sup._pause_for_commit_secret_hit()
    assert sup.run.read_status()['pauseReason']=='operator_input_needed'
    text=sent[0]
    print('COMMIT_SCAN_WITH_SIGNATURE_DIGEST',repr(text))
    assert 'ralphctl rule '+RUN_ID in text
    assert 'releases this hold too' in text
    commands=_advertised_commands(text)
    assert len(commands)>=1
    rule_cmd=next(c for c in commands if c.startswith('ralphctl rule '))

    with TestClient(create_app(sup.cfg,sup.run,sup)) as client:
        _run_ralphctl(rule_cmd,tmp_path,monkeypatch,client)
    status=sup.run.read_status()
    assert status['state']=='running',rule_cmd
    assert status.get('pauseReason') is None,rule_cmd


def test_user_docs_state_the_conditional_commit_scan_ruling_release():
    """Cure task cure-01-01-9 retry (PRD A.1/A.6, D.1; DoD 4): the
    signed-approach history above proves `POST /rule` DOES release a
    commit-scan hold whose approach already carries a rejection signature,
    so no user doc or SPEC passage may describe that hold as universally
    unanswerable by a ruling. docs/api.md's `pauseReason` row once said a
    ruling's release never applies to it and only `ralphctl unpause`
    answers it -- the opposite of the measured behaviour. Every surface
    that describes the hold's answer must name the conditional rule
    (the approach's own rejection signature) alongside the unpause lever.
    """
    import pathlib
    root=pathlib.Path(__file__).resolve().parent.parent
    forbidden=("nor a ruling's release applies to it",
               "(only `ralphctl unpause <run>` answers it)")
    for rel in ('SPEC.md','docs/api.md','docs/cli.md','docs/architecture.md'):
        text=(root/rel).read_text()
        for phrase in forbidden:
            assert phrase not in text,(rel,phrase)
    api=(root/'docs/api.md').read_text()
    row=next(l for l in api.splitlines() if l.startswith('| `pauseReason` |'))
    hold=row[row.index('commit-secret-scan hold'):]
    hold=hold[:hold.index('Either way the approach')]
    assert '`rejectionSignatures`' in hold
    assert 'ralphctl unpause <run>' in hold
    assert 'ralphctl rule <run>' in hold and 'releases it too' in hold
    spec=(root/'SPEC.md').read_text()
    row=next(l for l in spec.splitlines() if l.startswith('| `pauseReason` |'))
    hold=row[row.index('commit-secret-scan hold'):]
    assert '`rejectionSignatures`' in hold and 'releases it too' in hold
